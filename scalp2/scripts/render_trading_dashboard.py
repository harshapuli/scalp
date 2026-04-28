"""scripts/render_trading_dashboard.py — render the trader-focused dashboard.

  $ python3 scripts/render_trading_dashboard.py
  → output/trading_dashboard.html
"""
from __future__ import annotations

import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config_loader import load_thresholds
from infra.secrets import load_secrets


PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_ROOT / "output"
DEFAULT_DB = PROJECT_ROOT / "data" / "dev_journal.db"
BACKTEST_DIR = PROJECT_ROOT / "data" / "backtest"


def _latest_backtest() -> Optional[Path]:
    if not BACKTEST_DIR.exists():
        return None
    runs = sorted(BACKTEST_DIR.glob("run_37tickers_v*_*.json"))
    if not runs:
        runs = sorted(BACKTEST_DIR.glob("run_*.json"))
    return runs[-1] if runs else None


def _gather_decisions(db_path: Path = DEFAULT_DB) -> list[dict]:
    if not db_path.exists():
        return []
    try:
        with sqlite3.connect(db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                "SELECT timestamp, strategy, ticker, decision, pass_reason, "
                "ml_prob, expected_value FROM decision_log "
                "WHERE date(timestamp) = date('now') "
                "ORDER BY timestamp DESC LIMIT 50"
            )
            rows = cur.fetchall()
            return [
                {"timestamp": r[0], "strategy": r[1], "ticker": r[2],
                 "decision": r[3], "pass_reason": r[4],
                 "ml_prob": r[5], "expected_value": r[6]}
                for r in rows
            ]
    except Exception:
        return []


def _gather_alpaca():
    try:
        from data_clients.alpaca import AlpacaClient
        with AlpacaClient() as a:
            acct = a.get_account()
            positions = a.get_positions()
            return {
                "equity": acct.equity,
                "buying_power": acct.buying_power,
                "daytrade_count": acct.daytrade_count,
            }, [
                {"symbol": p.symbol, "qty": p.qty,
                 "avg_entry_price": p.avg_entry_price,
                 "current_price": p.current_price,
                 "market_value": p.market_value,
                 "unrealized_pl": p.unrealized_pl,
                 "unrealized_plpc": p.unrealized_plpc}
                for p in positions
            ]
    except Exception as e:
        print(f"[render_trading] alpaca probe failed: {e}", file=sys.stderr)
        return {}, []


def main() -> int:
    print("[render_trading] loading secrets + config...")
    load_secrets()
    cfg = load_thresholds()

    print("[render_trading] loading latest backtest...")
    from journal.trading_dashboard import gather_backtest_summary, render_trading_dashboard
    bt_path = _latest_backtest()
    if bt_path is None:
        print("[render_trading] no backtest run found — run scripts/backtest_all.py first")
        backtest_summary, per_ticker = {}, {}
    else:
        print(f"[render_trading] backtest source: {bt_path.name}")
        backtest_summary, per_ticker = gather_backtest_summary(bt_path)

    print("[render_trading] gathering today's decisions...")
    decisions = _gather_decisions()

    print("[render_trading] gathering Alpaca account + positions...")
    account, positions = _gather_alpaca()

    universe_rules = {
        "s2": cfg.get("s2", {}).get("universe_allowlist") or [],
        "s3": cfg.get("s3", {}).get("universe_allowlist"),    # None = all allowed
    }

    ctx = {
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z",
        "backtest_summary": backtest_summary,
        "per_ticker_breakdown": per_ticker,
        "today_decisions": decisions,
        "alpaca_account": account,
        "alpaca_positions": positions,
        "universe_rules": universe_rules,
    }

    html = render_trading_dashboard(ctx)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "trading_dashboard.html"
    out_path.write_text(html)
    print(f"[render_trading] wrote {out_path} ({len(html):,} bytes)")
    print(f"\n  open file://{out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
