"""journal/eod_analyzer.py — End-of-day review engine.

Purpose: after each trading day, produce a structured review covering BOTH
scalp and swing setups, capturing every UW endpoint we have access to so we
can later mine for patterns.

For every ticker that produced a setup card today (FORMING or TRADE), the
analyzer pulls:
  · flow_recent (today's bought_at_ask / sold_at_bid records)
  · flow_alerts_historical (UW-flagged unusual prints for the day)
  · greek_exposure (current snapshot)
  · greek_exposure_history (last 5 sessions for context)
  · iv_rank_history (IV percentile time series)
  · iv_term_structure (current term curve)
  · earnings_flow_summary (next-event flow stats)

Output:
  data/reviews/YYYY-MM-DD.json — full review dump (kept forever)
  Returned dict also has compact summary for the /review.html page.

Usage:
  from journal.eod_analyzer import analyze_day
  review = analyze_day("2026-04-28")          # current day
  review = analyze_day("2026-04-28", force=True)  # re-run + overwrite
"""
from __future__ import annotations

import json
import sqlite3
import sys
from dataclasses import asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

DB_PATH = PROJECT_ROOT / "data" / "dev_journal.db"
REVIEW_DIR = PROJECT_ROOT / "data" / "reviews"


# ─── Loaders from local SQLite ──────────────────────────────────────────────


def _load_setups_for_date(date_str: str) -> list[dict]:
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT * FROM setups WHERE session_date = ? ORDER BY created_at",
            (date_str,),
        ).fetchall()
    return [dict(r) for r in rows]


def _load_decisions_for_date(date_str: str) -> list[dict]:
    """Pulls decision_log rows for the date — includes both TRADE and PASS."""
    if not DB_PATH.exists():
        return []
    with sqlite3.connect(DB_PATH) as c:
        c.row_factory = sqlite3.Row
        rows = c.execute(
            "SELECT timestamp, strategy, ticker, candidate_id, direction, "
            "       gex_level, distance_to_gex_atr, reversal_state, "
            "       deterministic_score, ml_prob, decision, pass_reason, "
            "       expected_value, risk_dollars, iv_at_entry, order_id "
            "FROM decision_log WHERE date(timestamp) = ? "
            "ORDER BY timestamp DESC",
            (date_str,),
        ).fetchall()
    return [dict(r) for r in rows]


# ─── Loaders from external services ──────────────────────────────────────────


def _load_alpaca_orders(date_str: str) -> list[dict]:
    """Fills + cancellations on the paper account for the date."""
    try:
        from data_clients.alpaca import AlpacaClient
        with AlpacaClient() as a:
            after = f"{date_str}T00:00:00Z"
            until = f"{date_str}T23:59:59Z"
            orders = a.get_orders(status="all", after=after, until=until,
                                    limit=500)
            return orders or []
    except Exception as e:
        print(f"[eod] alpaca order fetch failed: {e}", file=sys.stderr)
        return []


def _capture_uw_snapshot(uw, ticker: str, target_dt: date) -> dict:
    """Pulls every UW endpoint we have for this ticker on `target_dt`.
    Per-call exception-isolated so a single 404 doesn't drop the whole snapshot."""
    snap: dict = {"ticker": ticker, "target_date": target_dt.isoformat()}

    def _try(label, fn):
        try:
            r = fn()
            # GEXSnapshot / FlowRecord dataclasses → dict
            if isinstance(r, list):
                r = [asdict(x) if hasattr(x, "__dataclass_fields__") else x for x in r]
            elif hasattr(r, "__dataclass_fields__"):
                r = asdict(r)
            snap[label] = r
            snap[f"{label}_error"] = None
        except Exception as e:
            snap[label] = None
            snap[f"{label}_error"] = f"{type(e).__name__}: {e}"

    _try("flow_recent", lambda: uw.flow_recent(ticker, target_date=target_dt))
    _try("flow_alerts", lambda: uw.flow_alerts_historical(ticker, target_dt))
    _try("greek_exposure", lambda: uw.greek_exposure(ticker))
    _try("greek_exposure_history", lambda: uw.greek_exposure_history(ticker, days=5))
    _try("iv_rank_history", lambda: uw.iv_rank_history(ticker))
    _try("iv_term_structure", lambda: uw.iv_term_structure(ticker))
    _try("earnings_flow_summary", lambda: uw.earnings_flow_summary(ticker))
    return snap


# ─── Analysis ───────────────────────────────────────────────────────────────


def _analyze_trade(decision: dict, alpaca_orders: list[dict],
                   uw_snap: dict) -> dict:
    """Per-trade review: links a decision_log TRADE row to the Alpaca fill,
    computes alignment with flow/GEX, and exposes UW context inline."""
    ticker = decision["ticker"]
    direction = decision.get("direction") or ""

    # Find the Alpaca order whose client_order_id matches the candidate_id
    matched_order = None
    cand_id = decision.get("candidate_id") or ""
    for o in alpaca_orders:
        coid = o.get("client_order_id") or ""
        if cand_id and cand_id in coid:
            matched_order = o
            break

    # Flow alignment: was net flow on the day same direction as our trade?
    flow_records = uw_snap.get("flow_recent") or []
    net_call_premium = sum(
        (r.get("premium") or 0) * (1 if r.get("action") == "buy" else -1)
        for r in flow_records if r.get("side") == "call"
    )
    net_put_premium = sum(
        (r.get("premium") or 0) * (1 if r.get("action") == "buy" else -1)
        for r in flow_records if r.get("side") == "put"
    )
    flow_bias = "bullish" if net_call_premium - net_put_premium > 0 else "bearish"
    aligned = (
        (direction == "long" and flow_bias == "bullish")
        or (direction == "short" and flow_bias == "bearish")
    )

    # GEX context
    gex = uw_snap.get("greek_exposure") or {}
    gex_summary = {}
    if gex:
        gex_summary = {
            "spot_price": gex.get("spot_price"),
            "gamma_flip": gex.get("gamma_flip"),
            "major_pos_gex_strike": gex.get("major_pos_gex_strike"),
            "major_neg_gex_strike": gex.get("major_neg_gex_strike"),
        }

    # IV context — use latest entry from rank history
    iv_rank_hist = uw_snap.get("iv_rank_history") or []
    latest_iv = iv_rank_hist[-1] if iv_rank_hist else None

    return {
        "ticker": ticker,
        "strategy": decision.get("strategy"),
        "direction": direction,
        "decision_at": decision.get("timestamp"),
        "ml_prob": decision.get("ml_prob"),
        "expected_value": decision.get("expected_value"),
        "candidate_id": cand_id,
        "alpaca_fill": {
            "order_id": (matched_order or {}).get("id"),
            "filled_qty": (matched_order or {}).get("filled_qty"),
            "filled_avg_price": (matched_order or {}).get("filled_avg_price"),
            "status": (matched_order or {}).get("status"),
        } if matched_order else None,
        "flow_alignment": {
            "net_call_premium": net_call_premium,
            "net_put_premium": net_put_premium,
            "flow_bias": flow_bias,
            "aligned_with_trade": aligned,
            "n_flow_records": len(flow_records),
        },
        "gex_at_close": gex_summary,
        "latest_iv_rank": latest_iv,
    }


def analyze_day(date_str: Optional[str] = None,
                force: bool = False) -> dict:
    """Top-level: produce + persist the daily review.

    If date_str is None, uses today's UTC date.
    If `force=False` and a cached review exists, returns it.
    """
    target_dt = (date.fromisoformat(date_str) if date_str
                  else datetime.now(tz=timezone.utc).date())
    date_str = target_dt.isoformat()
    REVIEW_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REVIEW_DIR / f"{date_str}.json"

    if out_path.exists() and not force:
        try:
            return json.loads(out_path.read_text())
        except Exception:
            pass  # corrupt cache → rebuild

    print(f"[eod] analyzing {date_str}...")

    # 1. Pull from local SQLite
    setups = _load_setups_for_date(date_str)
    decisions = _load_decisions_for_date(date_str)
    print(f"[eod]   decisions={len(decisions)} setups={len(setups)}")

    # 2. Pull from Alpaca (orders fired today)
    orders = _load_alpaca_orders(date_str)
    print(f"[eod]   alpaca orders={len(orders)}")

    # 3. Determine which tickers fired and capture UW snapshots
    fired_tickers = sorted(set(
        [d["ticker"] for d in decisions if d.get("ticker")]
        + [s["ticker"] for s in setups if s.get("ticker")]
    ))
    print(f"[eod]   fired tickers ({len(fired_tickers)}): {fired_tickers}")

    uw_snapshots: dict = {}
    if fired_tickers:
        try:
            from data_clients.unusual_whales import UWClient
            uw = UWClient()
            for tk in fired_tickers:
                uw_snapshots[tk] = _capture_uw_snapshot(uw, tk, target_dt)
                print(f"[eod]   uw {tk}: "
                      f"flow={len(uw_snapshots[tk].get('flow_recent') or [])} "
                      f"alerts={len(uw_snapshots[tk].get('flow_alerts') or [])}")
        except Exception as e:
            print(f"[eod] UW unavailable: {e}", file=sys.stderr)

    # 4. Per-trade reviews (only for TRADE decisions)
    trade_decisions = [d for d in decisions if d.get("decision") == "TRADE"]
    trade_reviews = [
        _analyze_trade(d, orders, uw_snapshots.get(d["ticker"], {}))
        for d in trade_decisions
    ]

    # 5. Per-strategy summary
    per_strategy: dict = {}
    for s in ("S1", "S2", "S3", "S4", "S5"):
        s_decisions = [d for d in decisions if d.get("strategy") == s]
        n_trade = sum(1 for d in s_decisions if d.get("decision") == "TRADE")
        n_pass = sum(1 for d in s_decisions if d.get("decision") == "PASS")
        # Pass reasons
        pass_reasons: dict = {}
        for d in s_decisions:
            if d.get("decision") == "PASS" and d.get("pass_reason"):
                pass_reasons[d["pass_reason"]] = pass_reasons.get(d["pass_reason"], 0) + 1
        if n_trade or n_pass:
            per_strategy[s] = {
                "n_trade": n_trade, "n_pass": n_pass,
                "pass_reasons": pass_reasons,
            }

    # 6. Missed setups: FORMING cards that never reached TRADE
    taken_ids = {s["id"] for s in setups if s.get("taken")}
    missed = [
        s for s in setups
        if s.get("stage") == "FORMING" and s["id"] not in taken_ids
    ]

    # 7. Day P&L (paper account snapshot at close)
    day_pnl = None
    try:
        from data_clients.alpaca import AlpacaClient
        with AlpacaClient() as a:
            acct = a.get_account()
            day_pnl = {
                "equity": acct.equity, "buying_power": acct.buying_power,
                "daytrade_count": acct.daytrade_count,
            }
    except Exception:
        pass

    # 8. Intraday timeseries — captured every 5min by the snapshot thread
    intraday_account: list[dict] = []
    intraday_per_ticker: dict[str, list[dict]] = {}
    intraday_uw: dict[str, list[dict]] = {}
    try:
        from journal.intraday_snapshot import (
            load_account_timeseries, load_all_ticker_timeseries,
            load_all_uw_timeseries,
        )
        intraday_account = load_account_timeseries(DB_PATH, date_str)
        intraday_per_ticker = load_all_ticker_timeseries(DB_PATH, date_str)
        intraday_uw = load_all_uw_timeseries(DB_PATH, date_str)
        n_flow_total = sum(
            sum(s.get("n_flow_records", 0) for s in slist)
            for slist in intraday_uw.values()
        )
        print(f"[eod]   intraday: account_snaps={len(intraday_account)} "
              f"ticker_snaps={sum(len(v) for v in intraday_per_ticker.values())} "
              f"uw_dumps={sum(len(v) for v in intraday_uw.values())} "
              f"flow_records_total={n_flow_total}")
    except Exception as e:
        print(f"[eod] intraday timeseries unavailable: {e}", file=sys.stderr)

    # Compress per-ticker timeseries into useful summaries
    ticker_evolution: dict[str, dict] = {}
    for tk, snaps in intraday_per_ticker.items():
        if not snaps:
            continue
        gex_pos_series = [s.get("gex_pos_strike") for s in snaps if s.get("gex_pos_strike")]
        gex_neg_series = [s.get("gex_neg_strike") for s in snaps if s.get("gex_neg_strike")]
        flip_series = [s.get("gex_gamma_flip") for s in snaps if s.get("gex_gamma_flip")]
        iv_series = [s.get("iv_percentile") for s in snaps if s.get("iv_percentile") is not None]
        states_seen = sorted({s.get("scalp_state") for s in snaps if s.get("scalp_state")})

        ticker_evolution[tk] = {
            "n_snapshots": len(snaps),
            "first_seen": snaps[0]["ts"],
            "last_seen": snaps[-1]["ts"],
            "states_traversed": states_seen,
            "gex_pos_strike_range": [min(gex_pos_series), max(gex_pos_series)] if gex_pos_series else None,
            "gex_neg_strike_range": [min(gex_neg_series), max(gex_neg_series)] if gex_neg_series else None,
            "gamma_flip_range": [min(flip_series), max(flip_series)] if flip_series else None,
            "iv_pct_range": [min(iv_series), max(iv_series)] if iv_series else None,
            "max_active_setups": max((s.get("n_active_setups", 0) for s in snaps), default=0),
            "setup_appearances": sum(1 for s in snaps if s.get("n_active_setups", 0) > 0),
        }

    # Account equity curve
    equity_curve = [
        {"ts": s["ts"], "equity": s.get("equity"),
         "unrealized_pl": s.get("unrealized_pl"),
         "n_positions": s.get("n_positions")}
        for s in intraday_account
    ]

    review = {
        "date": date_str,
        "generated_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z",
        "summary": {
            "n_decisions": len(decisions),
            "n_trades_fired": len(trade_decisions),
            "n_setups_tracked": len(setups),
            "n_setups_taken": len(taken_ids),
            "n_missed": len(missed),
            "n_alpaca_orders": len(orders),
            "fired_tickers": fired_tickers,
            "n_intraday_snapshots_account": len(intraday_account),
            "n_intraday_snapshots_per_ticker": {
                tk: len(snaps) for tk, snaps in intraday_per_ticker.items()
            },
        },
        "account": day_pnl,
        "per_strategy": per_strategy,
        "trade_reviews": trade_reviews,
        "missed_setups": missed,
        "decisions_full": decisions,
        "setups_full": setups,
        "alpaca_orders": orders,
        "uw_snapshots": uw_snapshots,
        # Intraday timeseries
        "ticker_evolution": ticker_evolution,
        "equity_curve": equity_curve,
        "intraday_per_ticker": intraday_per_ticker,
        "intraday_account": intraday_account,
        # NEW: raw UW dumps (full GEX strike maps + flow records list + IV term)
        "intraday_uw": intraday_uw,
    }

    out_path.write_text(json.dumps(review, default=str, indent=2))
    print(f"[eod] wrote {out_path} ({out_path.stat().st_size:,} bytes)")
    return review


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None,
                    help="ISO date (default: today UTC)")
    ap.add_argument("--force", action="store_true",
                    help="Re-run even if cached review exists")
    args = ap.parse_args()
    r = analyze_day(args.date, force=args.force)
    print(f"\n=== {r['date']} review ===")
    print(json.dumps(r["summary"], indent=2))
    print(f"\nper-strategy:")
    for s, m in r["per_strategy"].items():
        print(f"  {s}: trade={m['n_trade']} pass={m['n_pass']} "
              f"top_pass={list(m['pass_reasons'].items())[:3]}")
