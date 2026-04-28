"""scripts/render_trading_dashboard.py — render the trader-focused dashboard.

  $ python3 scripts/render_trading_dashboard.py
  → output/trading_dashboard.html
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
    """Returns (account_dict, positions_list, today_orders_list).

    today_orders are filtered to orders whose submitted_at falls on TODAY's
    UTC date — this matches the "Live trades today" section heading.
    """
    try:
        from data_clients.alpaca import AlpacaClient
        with AlpacaClient() as a:
            acct = a.get_account()
            positions = a.get_positions()
            # Pull a wide window so we can client-side filter to today.
            after = (datetime.now(tz=timezone.utc) - timedelta(hours=36)).isoformat(timespec="seconds")
            try:
                raw_orders = a.get_orders(status="all", after=after, limit=500)
            except Exception as oe:
                print(f"[render_trading] alpaca orders fetch failed: {oe}", file=sys.stderr)
                raw_orders = []

            # Pick the most recent session-date with order activity. That's
            # "today" if the market's been live; otherwise it's whatever the
            # last live session was. Either way it's the freshest data the
            # trader cares about.
            session_dates = sorted({
                (o.get("submitted_at") or o.get("created_at") or "")[:10]
                for o in raw_orders
            } - {""}, reverse=True)
            target_date = session_dates[0] if session_dates else \
                datetime.now(tz=timezone.utc).date().isoformat()
            today_orders = [
                o for o in raw_orders
                if (o.get("submitted_at") or o.get("created_at") or "")[:10] == target_date
                or (o.get("filled_at") or "")[:10] == target_date
            ]
            # Tag the session date onto each order so the dashboard can label it.
            for o in today_orders:
                o["__session_date"] = target_date
            return {
                "equity": acct.equity,
                "buying_power": acct.buying_power,
                "daytrade_count": acct.daytrade_count,
                "is_live": a.is_live,
            }, [
                {"symbol": p.symbol, "qty": p.qty,
                 "avg_entry_price": p.avg_entry_price,
                 "current_price": p.current_price,
                 "market_value": p.market_value,
                 "unrealized_pl": p.unrealized_pl,
                 "unrealized_plpc": p.unrealized_plpc}
                for p in positions
            ], today_orders
    except Exception as e:
        print(f"[render_trading] alpaca probe failed: {e}", file=sys.stderr)
        return {}, [], []


def main() -> int:
    print("[render_trading] loading secrets + config...")
    load_secrets()
    cfg = load_thresholds()

    print("[render_trading] loading latest backtest...")
    from journal.trading_dashboard import gather_backtest_summary, render_trading_dashboard
    bt_path = _latest_backtest()
    if bt_path is None:
        print("[render_trading] no backtest run found — run scripts/backtest_all.py first")
        backtest_summary, per_ticker, backtest_trades = {}, {}, []
    else:
        print(f"[render_trading] backtest source: {bt_path.name}")
        backtest_summary, per_ticker, backtest_trades = gather_backtest_summary(bt_path)

    print("[render_trading] gathering today's decisions (decision_log)...")
    decisions = _gather_decisions()

    print("[render_trading] gathering Alpaca account + positions + live orders...")
    account, positions, live_orders = _gather_alpaca()

    # Mode = env override > inferred from Alpaca endpoint > default 'paper'
    mode = os.environ.get("TRADING_MODE")
    if not mode:
        mode = "live" if account.get("is_live") else "paper"

    universe_rules = {
        "s2": cfg.get("s2", {}).get("universe_allowlist") or [],
        "s3": cfg.get("s3", {}).get("universe_allowlist"),    # None = all allowed
    }

    cfg_caps = cfg.get("s5", {}).get("risk", {}) or {}

    ctx = {
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z",
        "backtest_summary": backtest_summary,
        "per_ticker_breakdown": per_ticker,
        "backtest_trades": backtest_trades,
        "today_decisions": decisions,
        "alpaca_account": account,
        "alpaca_positions": positions,
        "live_trades": live_orders,
        "universe_rules": universe_rules,
        "cfg_caps": cfg_caps,
        "trading_mode": mode,
    }

    # Identify which session date the live_orders represent (could be today
    # or last live session if today is pre-open / weekend).
    session_date = next(
        (o.get("__session_date") for o in live_orders if o.get("__session_date")),
        datetime.now(tz=timezone.utc).date().isoformat(),
    )

    print(f"[render_trading] context summary:")
    print(f"  · backtest trades:    {len(backtest_trades)}")
    print(f"  · today's decisions:  {len(decisions)} (decision_log)")
    print(f"  · open positions:     {len(positions)}")
    print(f"  · live orders shown:  {len(live_orders)} (session {session_date})")
    print(f"  · trading mode:       {mode}")

    html = render_trading_dashboard(ctx)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = OUTPUT_DIR / "trading_dashboard.html"
    out_path.write_text(html)
    print(f"[render_trading] wrote {out_path} ({len(html):,} bytes)")
    print(f"\n  open file://{out_path}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
