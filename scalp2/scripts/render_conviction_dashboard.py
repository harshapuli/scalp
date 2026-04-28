"""scripts/render_conviction_dashboard.py — write the conviction page.

  $ python3 scripts/render_conviction_dashboard.py
  → output/conviction.html

Mirrors the engine_v4 /conviction visual, but data comes from scalp 2:
  · backtest run JSON (per-ticker per-strategy stats)
  · today's decision_log (live TRADE decisions)
  · Alpaca account/positions (held badges)
  · last close + 14-bar ATR per ticker (1d daily bars, 1 call per ticker)
"""
from __future__ import annotations

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
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
                "ml_prob, expected_value, direction "
                "FROM decision_log "
                "WHERE date(timestamp) = date('now') "
                "ORDER BY timestamp DESC LIMIT 100"
            )
            rows = cur.fetchall()
            return [
                {"timestamp": r[0], "strategy": r[1], "ticker": r[2],
                 "decision": r[3], "pass_reason": r[4],
                 "ml_prob": r[5], "expected_value": r[6],
                 "direction": r[7]}
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
                "is_live": a.is_live,
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
        print(f"[render_conviction] alpaca probe failed: {e}", file=sys.stderr)
        return {}, []


def _last_close_and_atr(tickers: list[str]) -> tuple[dict, dict]:
    """Per-ticker last close + 14-day ATR via Alpaca Pro daily bars.

    Wraps each call so a single bad ticker doesn't kill the page.
    """
    from data_clients.alpaca import AlpacaClient
    last_closes: dict[str, float] = {}
    atrs: dict[str, float] = {}
    end = datetime.now(tz=timezone.utc).date().isoformat()
    start = (datetime.now(tz=timezone.utc).date() - timedelta(days=30)).isoformat()
    try:
        with AlpacaClient() as a:
            for t in tickers:
                try:
                    bars = a.get_bars(t, start=start, end=end,
                                      timeframe="1Day", limit=30)
                    if not bars:
                        continue
                    last_closes[t] = bars[-1].c
                    # 14-bar ATR (true range)
                    if len(bars) >= 15:
                        trs = []
                        for i in range(1, len(bars)):
                            h, l, pc = bars[i].h, bars[i].l, bars[i-1].c
                            tr = max(h - l, abs(h - pc), abs(l - pc))
                            trs.append(tr)
                        atrs[t] = sum(trs[-14:]) / 14
                except Exception as e:
                    print(f"[render_conviction]   {t}: bars failed ({e})",
                          file=sys.stderr)
                    continue
    except Exception as e:
        print(f"[render_conviction] alpaca client failed: {e}", file=sys.stderr)
    return last_closes, atrs


def main() -> int:
    print("[render_conviction] loading secrets + config...")
    load_secrets()
    cfg = load_thresholds()

    print("[render_conviction] loading latest backtest...")
    from journal.conviction_dashboard import (
        gather_conviction_data, render_conviction_dashboard,
    )
    bt_path = _latest_backtest()
    if bt_path is None:
        print("[render_conviction] no backtest run found — run backtest first")
        per_ticker, per_strategy, meta = {}, {}, {}
    else:
        print(f"[render_conviction] backtest source: {bt_path.name}")
        per_ticker, per_strategy, meta = gather_conviction_data(bt_path)

    print("[render_conviction] gathering today's TRADE decisions...")
    decisions = _gather_decisions()

    print("[render_conviction] gathering Alpaca account + positions...")
    account, positions = _gather_alpaca()

    universe = meta.get("tickers", []) or list(per_ticker.keys())
    print(f"[render_conviction] fetching last close + ATR for {len(universe)} tickers...")
    last_closes, atrs = _last_close_and_atr(universe)
    print(f"[render_conviction]   got {len(last_closes)} closes, {len(atrs)} ATRs")

    mode = os.environ.get("TRADING_MODE")
    if not mode:
        mode = "live" if account.get("is_live") else "paper"

    ctx = {
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z",
        "per_ticker": per_ticker,
        "per_strategy": per_strategy,
        "meta": meta,
        "today_decisions": decisions,
        "positions": positions,
        "last_closes": last_closes,
        "atrs": atrs,
        "trading_mode": mode,
    }

    print("[render_conviction] context summary:")
    print(f"  · universe size:      {len(universe)}")
    print(f"  · tickers with edge:  {sum(1 for v in per_ticker.values() if v)}")
    print(f"  · today's decisions:  {len(decisions)} (decision_log)")
    print(f"  · trade-now today:    {sum(1 for d in decisions if d.get('decision')=='TRADE')}")
    print(f"  · open positions:     {len(positions)}")
    print(f"  · trading mode:       {mode}")

    html = render_conviction_dashboard(ctx)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "conviction.html"
    out_path.write_text(html)
    print(f"[render_conviction] wrote {out_path} ({len(html):,} bytes)")
    print(f"\n  open file://{out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
