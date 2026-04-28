"""scripts/render_dashboard.py — pull live data, render dashboard.

Usage:
  $ python3 scripts/render_dashboard.py
  → output/dashboard.html
"""
from __future__ import annotations

import json
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config_loader import load_thresholds
from infra.secrets import load_secrets


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_DB = PROJECT_ROOT / "data" / "dev_journal.db"


def _gather_foundation() -> dict:
    """Probe live Alpaca + UW + Redis for the dashboard."""
    fnd = {
        "uw_cadence_min": None,
        "uw_snapshots_per_session": None,
        "alpaca_equity": None,
        "alpaca_buying_power": None,
        "alpaca_positions_count": 0,
        "alpaca_endpoint": None,
        "alpaca_is_live": False,
        "redis_reachable": False,
    }

    # UW cadence — cached single probe; in production this would be a daily cron
    try:
        from data_clients.unusual_whales import UWClient
        with UWClient() as uw:
            cad = uw.verify_gex_cadence("SPY")
            fnd["uw_cadence_min"] = cad.get("verified_cadence_min")
            fnd["uw_snapshots_per_session"] = cad.get("snapshots_per_session")
    except Exception as e:
        print(f"[render] UW probe failed: {e}", file=sys.stderr)

    # Alpaca
    try:
        from data_clients.alpaca import AlpacaClient
        with AlpacaClient() as a:
            acct = a.get_account()
            fnd["alpaca_equity"] = acct.equity
            fnd["alpaca_buying_power"] = acct.buying_power
            fnd["alpaca_endpoint"] = a.trading_url
            fnd["alpaca_is_live"] = a.is_live
            positions = a.get_positions()
            fnd["alpaca_positions_count"] = len(positions)
            fnd["_alpaca_positions"] = [
                {
                    "symbol": p.symbol, "qty": p.qty,
                    "avg_entry_price": p.avg_entry_price,
                    "current_price": p.current_price,
                    "market_value": p.market_value,
                    "unrealized_pl": p.unrealized_pl,
                    "unrealized_plpc": p.unrealized_plpc,
                }
                for p in positions
            ]
    except Exception as e:
        print(f"[render] Alpaca probe failed: {e}", file=sys.stderr)

    # Redis
    try:
        from infra.redis_client import is_reachable
        fnd["redis_reachable"] = is_reachable()
    except Exception:
        pass

    return fnd


def _gather_decisions(db_path: Path = DEFAULT_DB) -> tuple[list[dict], dict]:
    """Read recent decisions + pass_reason histogram from SQLite journal."""
    if not db_path.exists():
        return [], {}
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT timestamp, strategy, ticker, decision, pass_reason, "
                "ml_prob, expected_value FROM decision_log "
                "ORDER BY timestamp DESC LIMIT 50"
            )
            rows = cur.fetchall()
            decisions = [
                {
                    "timestamp": r[0], "strategy": r[1], "ticker": r[2],
                    "decision": r[3], "pass_reason": r[4],
                    "ml_prob": r[5], "expected_value": r[6],
                }
                for r in rows
            ]
            cur.execute(
                "SELECT pass_reason, COUNT(*) FROM decision_log "
                "WHERE decision = 'PASS' AND pass_reason IS NOT NULL "
                "GROUP BY pass_reason ORDER BY 2 DESC"
            )
            hist = {row[0]: row[1] for row in cur.fetchall()}
        return decisions, hist
    except Exception as e:
        print(f"[render] decision_log read failed: {e}", file=sys.stderr)
        return [], {}


def _gather_pre_staged() -> list[dict]:
    """Lookup pre-staged candidates from Redis (sync). Empty if no Redis."""
    cands = []
    try:
        from infra.redis_client import get_sync_client, KEY_PRE_STAGED_FMT
        c = get_sync_client()
        if not c.ping():
            return []
        pattern = KEY_PRE_STAGED_FMT.format(strategy="s5", ticker="*")
        keys = list(c.scan_iter(match=pattern))
        for k in keys:
            raw = c.get(k)
            if raw:
                try:
                    d = json.loads(raw)
                    ttl = c.ttl(k)
                    d["ttl_remaining_min"] = max(0, ttl // 60) if ttl > 0 else 0
                    cands.append(d)
                except Exception:
                    continue
    except Exception:
        pass
    return cands


def _gather_tests() -> list[dict]:
    """Run unit tests + collect status."""
    tests = []
    test_files = sorted((PROJECT_ROOT / "tests" / "unit").glob("test_*.py"))
    for tf in test_files:
        try:
            r = subprocess.run(
                ["python3", str(tf)],
                capture_output=True, text=True, timeout=30,
            )
            ok = r.returncode == 0 and "all tests passed" in r.stdout
            # Count PASS lines
            n_pass = r.stdout.count("PASS  ")
            tests.append({
                "module": tf.name,
                "test_count": n_pass,
                "status": "PASS" if ok else "FAIL",
                "notes": "" if ok else (r.stdout.splitlines()[-1] if r.stdout else r.stderr[:80]),
            })
        except Exception as e:
            tests.append({
                "module": tf.name, "test_count": "?",
                "status": "ERROR", "notes": str(e)[:80],
            })
    return tests


def main() -> int:
    print("[render_dashboard] loading secrets + config...")
    load_secrets()
    cfg = load_thresholds()

    print("[render_dashboard] gathering foundation status (UW + Alpaca + Redis)...")
    foundation = _gather_foundation()
    alpaca_positions = foundation.pop("_alpaca_positions", [])

    print("[render_dashboard] reading decision log...")
    decisions, pass_hist = _gather_decisions()

    print("[render_dashboard] checking pre-staged candidates in Redis...")
    pre_staged = _gather_pre_staged()

    print("[render_dashboard] running test catalog (~30s)...")
    tests = _gather_tests()

    print("[render_dashboard] composing dashboard...")
    from journal.dashboard import render_dashboard
    ctx = {
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z",
        "foundation": foundation,
        "recent_decisions": decisions,
        "pass_reason_histogram": pass_hist,
        "pre_staged_candidates": pre_staged,
        "alpaca_positions": alpaca_positions,
        "tests": tests,
    }
    html = render_dashboard(ctx)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "dashboard.html"
    out_path.write_text(html)
    print(f"[render_dashboard] wrote {out_path} ({len(html):,} bytes)")
    print(f"\n  open file://{out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
