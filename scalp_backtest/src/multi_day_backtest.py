"""
multi_day_backtest.py — sweep the last 4 trading days × all tickers we know.

What this audits:
  • Universe = (today's lifecycle tickers) ∪ (engine_scalp historical tickers)
  • For each (ticker, date): pull the Alpaca daily bar (open/high/low/close/vol)
  • Compute close-vs-open % move, intraday range %
  • Overlay every signal we generated:
      - today (engine_v4): scalp outcomes + lifecycle kinds_fired
      - past 3 days (engine_scalp): historical archive signals
  • Report: top movers, catches, misses (>=3% move w/ no signal), wrong-side fires
  • Cross-reference today's UW flow_context for directional agreement

We don't have minute bars for past dates, so PnL on past misses is inferred
from close-vs-open (a directional proxy, not real option PnL). For TODAY,
engine_v4 outcomes use ATR-resimulated PnL.

Output:
  data/multi_day_backtest/run_<TODAY>.json
  Terminal report (this file's main())
"""
from __future__ import annotations

import json
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loader_scalp import load_all as load_scalp_all
from outcome_intraday import compute_all as compute_scalp_all, dedup_signals as dedup_scalp
from loader_lifecycle import load_all_lifecycles
from loader_historical import load_historical
from alpaca_bars import fetch_daily_bars_for_swing_tickers


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = PROJECT_ROOT / "data" / "multi_day_backtest"
ENGINE_V4_ROOT = Path("/Users/harshapuli/Downloads/spy QQQ/claude/gemini /engine_v4")


# ──────────────────────────────────────────────────────────────────────────────
# Date helpers
# ──────────────────────────────────────────────────────────────────────────────


def last_n_trading_days(n: int = 4, today: str = "2026-04-27") -> list[str]:
    """Return last n trading days (Mon-Fri only, US holidays not handled — fine
    for this audit since the user just wants the recent window)."""
    d = datetime.strptime(today, "%Y-%m-%d").date()
    out = []
    while len(out) < n:
        if d.weekday() < 5:  # 0=Mon ... 4=Fri
            out.append(d.isoformat())
        d = d.fromordinal(d.toordinal() - 1)
    return sorted(out)


# ──────────────────────────────────────────────────────────────────────────────
# Build universe and fetch bars
# ──────────────────────────────────────────────────────────────────────────────


def build_universe(scalp_outs, lifecycles, hist_sigs) -> list[str]:
    """Union of tickers from today's lifecycle, today's outcomes, and historical archive."""
    s: set[str] = set()
    for o in scalp_outs:
        if o.ticker:
            s.add(o.ticker)
    for r in lifecycles:
        if r.ticker:
            s.add(r.ticker)
    for h in hist_sigs:
        if h.ticker:
            s.add(h.ticker)
    return sorted(s)


def fetch_bars_for_universe(tickers: list[str], lookback_days: int = 30) -> dict:
    """Use the existing Alpaca cache + fetch. Returns {ticker: [DailyBar]}."""
    return fetch_daily_bars_for_swing_tickers(tickers, lookback_days=lookback_days)


def index_bars_by_date(bars_by_ticker: dict) -> dict:
    """Re-index to {ticker: {date_str: DailyBar}} for O(1) date lookups."""
    out: dict = {}
    for tk, bars in bars_by_ticker.items():
        out[tk] = {b.date: b for b in bars}
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Signal indexing
# ──────────────────────────────────────────────────────────────────────────────


def index_today_outcomes(scalp_outs, today: str) -> dict:
    """{(ticker, date): [outcome,...]} for today only (engine_v4)."""
    idx: dict = defaultdict(list)
    for o in scalp_outs:
        if o.date_str != today:
            continue
        idx[(o.ticker, o.date_str)].append(o)
    return idx


def index_today_lifecycle(lifecycles) -> dict:
    """{ticker: lifecycle_record} — covers today's tracking even when no signal fired."""
    return {r.ticker: r for r in lifecycles if r.ticker}


def index_historical(hist_sigs, dates: list[str]) -> dict:
    """{(ticker, date): [HistoricalSignal,...]} for the requested dates."""
    dset = set(dates)
    idx: dict = defaultdict(list)
    for s in hist_sigs:
        if s.date_str in dset and s.ticker:
            idx[(s.ticker, s.date_str)].append(s)
    return idx


# ──────────────────────────────────────────────────────────────────────────────
# UW flow_context (today only — engine_v4 persists per-ticker)
# ──────────────────────────────────────────────────────────────────────────────


def load_flow_context(date_str: str) -> dict:
    """Read engine_v4/data/intraday/<DATE>/flow_context/<TICKER>.json.
    Returns {ticker: dict} with keys flow_alerts, stock_state, fetched_utc."""
    fc_dir = ENGINE_V4_ROOT / "data" / "intraday" / date_str / "flow_context"
    out: dict = {}
    if not fc_dir.exists():
        return out
    for fp in fc_dir.glob("*.json"):
        try:
            d = json.loads(fp.read_text())
            tk = d.get("ticker") or fp.stem
            out[tk] = d
        except Exception:
            continue
    return out


def summarize_flow(fc_record: dict) -> dict:
    """Net call/put premium and dominant side from a flow_context record."""
    alerts = (fc_record or {}).get("flow_alerts") or []
    if not alerts:
        return {"n_alerts": 0}
    call_prem = 0.0
    put_prem = 0.0
    for a in alerts:
        prem = float(a.get("total_premium") or 0.0)
        t = (a.get("type") or "").upper()
        if "CALL" in t:
            call_prem += prem
        elif "PUT" in t:
            put_prem += prem
    net = call_prem - put_prem
    side = "CALL" if net > 0 else ("PUT" if net < 0 else "NEUTRAL")
    return {
        "n_alerts": len(alerts),
        "call_prem": call_prem,
        "put_prem": put_prem,
        "net_prem": net,
        "dominant_side": side,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Per-cell verdict
# ──────────────────────────────────────────────────────────────────────────────


def classify_cell(bar, today_outs, lc_record, hist_sigs_at_date,
                  is_today: bool, threshold: float = 3.0) -> dict:
    """Compute the verdict for one (ticker, date):
       - actual_dir_pct: close vs open % move
       - tradeable: True if abs(actual_dir_pct) >= threshold
       - signaled_sides: set of sides we fired (CALL / PUT)
       - n_signals: count of signals fired
       - verdict: HIT_RIGHT / HIT_WRONG / SPLIT / MISS / NA (no bar)
    """
    if bar is None or not bar.o:
        return {"verdict": "NA"}

    actual = (bar.c - bar.o) / bar.o * 100.0
    range_pct = (bar.h - bar.l) / bar.o * 100.0 if bar.o else 0.0
    expected = "CALL" if actual > 0 else ("PUT" if actual < 0 else "FLAT")

    sides: set[str] = set()
    n_sig = 0

    if is_today:
        for o in (today_outs or []):
            if o.side:
                sides.add(o.side.upper())
                n_sig += 1
        # also pull lifecycle kinds_fired (signals beyond the deduped outcomes)
        if lc_record and lc_record.kinds_fired:
            for k in lc_record.kinds_fired:
                up = (k or "").upper()
                if "CALL" in up:
                    sides.add("CALL")
                elif "PUT" in up:
                    sides.add("PUT")
                # n_sig already counted via outs above; lifecycle kinds may double
    else:
        for h in (hist_sigs_at_date or []):
            if h.direction:
                sides.add(h.direction.upper())
                n_sig += 1

    tradeable = abs(actual) >= threshold

    if not n_sig:
        verdict = "MISS" if tradeable else "QUIET"
    elif len(sides) == 1:
        only = next(iter(sides))
        verdict = "HIT_RIGHT" if only == expected else "HIT_WRONG"
    else:
        # CALL and PUT both fired
        verdict = "SPLIT"

    return {
        "verdict": verdict,
        "actual_dir_pct": round(actual, 2),
        "range_pct": round(range_pct, 2),
        "expected_side": expected,
        "tradeable": tradeable,
        "signaled_sides": sorted(sides),
        "n_signals": n_sig,
        "open": bar.o,
        "high": bar.h,
        "low": bar.l,
        "close": bar.c,
        "volume": bar.v,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────


def main():
    today = "2026-04-27"
    dates = last_n_trading_days(4, today=today)
    print(f"\n{'#'*80}")
    print(f"  Multi-day backtest — {len(dates)} trading days × all tickers")
    print(f"  Window: {dates[0]} → {dates[-1]}  ({', '.join(dates)})")
    print(f"{'#'*80}\n")

    t0 = time.time()

    # Load ALL signal sources
    print("[load] today's scalp outcomes (engine_v4)...")
    sigs, bars = load_scalp_all()
    dedup = dedup_scalp(sigs)
    scalp_outs = compute_scalp_all(dedup, bars)
    print(f"        {len(scalp_outs)} outcomes")

    print("[load] today's lifecycle (engine_v4)...")
    lifecycles = load_all_lifecycles()
    print(f"        {len(lifecycles)} ticker lifecycles")

    print("[load] historical archive (engine_scalp)...")
    hist_sigs = load_historical()
    print(f"        {len(hist_sigs)} signals across {len(set(s.date_str for s in hist_sigs))} dates")

    print("[load] today's UW flow_context (engine_v4 per-ticker persist)...")
    flow_ctx = load_flow_context(today)
    print(f"        {len(flow_ctx)} tickers with flow_context  "
          f"(usable: {sum(1 for v in flow_ctx.values() if v.get('flow_alerts'))})")

    # Build universe
    universe = build_universe(scalp_outs, lifecycles, hist_sigs)
    print(f"\n[universe] {len(universe)} unique tickers across all sources")

    # Fetch Alpaca bars (cached, fast)
    print(f"[bars] fetching Alpaca daily bars for {len(universe)} tickers...")
    t_bars = time.time()
    bars_by_tk = fetch_bars_for_universe(universe, lookback_days=30)
    bars_idx = index_bars_by_date(bars_by_tk)
    n_with_bars = sum(1 for v in bars_by_tk.values() if v)
    print(f"        {n_with_bars}/{len(universe)} tickers got bars in {time.time()-t_bars:.1f}s")

    # Index signals
    today_outs = index_today_outcomes(scalp_outs, today)
    today_lc = index_today_lifecycle(lifecycles)
    hist_idx = index_historical(hist_sigs, dates)

    # ──────────────────────────────────────────────────────────────────────
    # Run the backtest cell-by-cell
    # ──────────────────────────────────────────────────────────────────────
    print(f"\n[run] classifying {len(universe)} tickers × {len(dates)} dates = "
          f"{len(universe)*len(dates):,} cells...")

    cells = []  # list of {ticker, date, ...verdict fields}
    for tk in universe:
        bdict = bars_idx.get(tk) or {}
        for d in dates:
            bar = bdict.get(d)
            verdict = classify_cell(
                bar=bar,
                today_outs=today_outs.get((tk, d)) if d == today else None,
                lc_record=today_lc.get(tk) if d == today else None,
                hist_sigs_at_date=hist_idx.get((tk, d)) if d != today else None,
                is_today=(d == today),
                threshold=3.0,
            )
            verdict["ticker"] = tk
            verdict["date"] = d
            cells.append(verdict)

    # ──────────────────────────────────────────────────────────────────────
    # Aggregate stats
    # ──────────────────────────────────────────────────────────────────────
    by_verdict: Counter = Counter(c["verdict"] for c in cells)
    by_verdict_today: Counter = Counter(c["verdict"] for c in cells if c["date"] == today)
    by_verdict_past: Counter = Counter(c["verdict"] for c in cells if c["date"] != today)

    print(f"\n{'='*80}")
    print(f"  AGGREGATE VERDICTS")
    print(f"{'='*80}")
    print(f"  Across {len(cells):,} cells ({len(universe)} tickers × {len(dates)} dates):")
    for v in ["HIT_RIGHT", "HIT_WRONG", "SPLIT", "MISS", "QUIET", "NA"]:
        n = by_verdict[v]
        print(f"    {v:10s}  {n:>6d}  ({100*n/max(len(cells),1):>5.1f}%)")
    print(f"\n  Split by today vs past 3 days:")
    print(f"    {'verdict':10s}  {'today':>10s}  {'past3':>10s}")
    for v in ["HIT_RIGHT", "HIT_WRONG", "SPLIT", "MISS", "QUIET", "NA"]:
        print(f"    {v:10s}  {by_verdict_today[v]:>10d}  {by_verdict_past[v]:>10d}")

    # ──────────────────────────────────────────────────────────────────────
    # Per-day section: top movers + signal status
    # ──────────────────────────────────────────────────────────────────────
    for d in dates:
        day_cells = [c for c in cells if c["date"] == d and c["verdict"] != "NA"]
        if not day_cells:
            continue
        day_cells.sort(key=lambda c: abs(c["actual_dir_pct"]), reverse=True)
        n_trade = sum(1 for c in day_cells if c["tradeable"])
        n_hit_r = sum(1 for c in day_cells if c["verdict"] == "HIT_RIGHT")
        n_hit_w = sum(1 for c in day_cells if c["verdict"] == "HIT_WRONG")
        n_miss = sum(1 for c in day_cells if c["verdict"] == "MISS")
        cap_str = " (top 25 by abs move)" if len(day_cells) > 25 else ""
        print(f"\n{'─'*80}")
        print(f"  {d}  ·  {len(day_cells)} tickers w/ bar  ·  {n_trade} tradeable (>=3%)  ·  "
              f"hit-right {n_hit_r}  hit-wrong {n_hit_w}  miss {n_miss}{cap_str}")
        print(f"{'─'*80}")
        print(f"  {'TICKER':7s}  {'open':>8s}  {'close':>8s}  {'%chg':>7s}  {'rng%':>5s}  "
              f"{'expect':>6s}  {'fired':>10s}  {'verdict':>10s}")
        for c in day_cells[:25]:
            sides = "/".join(c.get("signaled_sides") or []) or "—"
            print(f"  {c['ticker']:7s}  {c['open']:>8.2f}  {c['close']:>8.2f}  "
                  f"{c['actual_dir_pct']:>+6.2f}%  {c['range_pct']:>4.1f}%  "
                  f"{c['expected_side']:>6s}  {sides:>10s}  {c['verdict']:>10s}")

    # ──────────────────────────────────────────────────────────────────────
    # Per-ticker 4-day directional summary
    # ──────────────────────────────────────────────────────────────────────
    by_ticker: dict = defaultdict(list)
    for c in cells:
        if c["verdict"] != "NA":
            by_ticker[c["ticker"]].append(c)

    # Sum of |actual_dir_pct| as "movement budget" — who moved most across the window
    ticker_moves: list[tuple[str, float, int, int]] = []
    for tk, cs in by_ticker.items():
        total_abs = sum(abs(c["actual_dir_pct"]) for c in cs)
        n_trade = sum(1 for c in cs if c["tradeable"])
        n_caught = sum(1 for c in cs if c["verdict"] == "HIT_RIGHT")
        ticker_moves.append((tk, total_abs, n_trade, n_caught))
    ticker_moves.sort(key=lambda x: x[1], reverse=True)

    print(f"\n{'='*80}")
    print(f"  TOP 30 TICKERS BY 4-DAY MOVEMENT BUDGET (sum of |daily % moves|)")
    print(f"{'='*80}")
    print(f"  {'rank':>4s}  {'ticker':7s}  {'total_abs%':>10s}  "
          f"{'tradeable':>9s}  {'caught':>7s}  {'capture%':>9s}")
    for i, (tk, total, n_trade, n_caught) in enumerate(ticker_moves[:30], 1):
        cap = (100 * n_caught / n_trade) if n_trade else 0.0
        print(f"  {i:>4d}  {tk:7s}  {total:>9.2f}%  "
              f"{n_trade:>9d}  {n_caught:>7d}  {cap:>8.1f}%")

    # ──────────────────────────────────────────────────────────────────────
    # Worst misses — biggest moves we never fired on
    # ──────────────────────────────────────────────────────────────────────
    misses = [c for c in cells if c["verdict"] == "MISS"]
    misses.sort(key=lambda c: abs(c["actual_dir_pct"]), reverse=True)

    print(f"\n{'='*80}")
    print(f"  TOP 30 MISSES — biggest |moves| where we generated NO signal")
    print(f"{'='*80}")
    print(f"  {'date':10s}  {'ticker':7s}  {'open':>8s}  {'close':>8s}  "
          f"{'%chg':>7s}  {'rng%':>5s}  {'expect':>6s}")
    for c in misses[:30]:
        print(f"  {c['date']:10s}  {c['ticker']:7s}  {c['open']:>8.2f}  {c['close']:>8.2f}  "
              f"{c['actual_dir_pct']:>+6.2f}%  {c['range_pct']:>4.1f}%  {c['expected_side']:>6s}")

    # ──────────────────────────────────────────────────────────────────────
    # Wrong-side fires
    # ──────────────────────────────────────────────────────────────────────
    wrongs = [c for c in cells if c["verdict"] == "HIT_WRONG"]
    wrongs.sort(key=lambda c: abs(c["actual_dir_pct"]), reverse=True)

    print(f"\n{'='*80}")
    print(f"  TOP 25 WRONG-SIDE FIRES — biggest |moves| where we fired the OPPOSITE side")
    print(f"{'='*80}")
    if not wrongs:
        print("  (none)")
    else:
        print(f"  {'date':10s}  {'ticker':7s}  {'open':>8s}  {'close':>8s}  "
              f"{'%chg':>7s}  {'expect':>6s}  {'fired':>10s}")
        for c in wrongs[:25]:
            sides = "/".join(c.get("signaled_sides") or [])
            print(f"  {c['date']:10s}  {c['ticker']:7s}  {c['open']:>8.2f}  {c['close']:>8.2f}  "
                  f"{c['actual_dir_pct']:>+6.2f}%  {c['expected_side']:>6s}  {sides:>10s}")

    # ──────────────────────────────────────────────────────────────────────
    # UW flow_context cross-check (today only — flow_context is per-day)
    # ──────────────────────────────────────────────────────────────────────
    print(f"\n{'='*80}")
    print(f"  UW FLOW vs OUTCOME — today's tradeable moves cross-checked against flow_context")
    print(f"{'='*80}")
    today_trade = [c for c in cells if c["date"] == today and c["tradeable"]]
    n_with_uw = 0
    n_uw_agree = 0
    n_uw_disagree = 0
    rows = []
    for c in today_trade:
        fc = flow_ctx.get(c["ticker"])
        if not fc:
            continue
        s = summarize_flow(fc)
        if s.get("n_alerts", 0) == 0:
            continue
        n_with_uw += 1
        agree = s.get("dominant_side") == c["expected_side"]
        if agree:
            n_uw_agree += 1
        else:
            n_uw_disagree += 1
        rows.append((c, s, agree))
    if rows:
        print(f"  {n_with_uw} of {len(today_trade)} today-tradeable tickers have UW flow")
        print(f"  UW dominant side AGREES with close-vs-open in {n_uw_agree} cases, "
              f"DISAGREES in {n_uw_disagree}")
        rows.sort(key=lambda x: abs(x[0]["actual_dir_pct"]), reverse=True)
        print(f"\n  {'TICKER':7s}  {'%chg':>7s}  {'expect':>6s}  "
              f"{'uw_n':>5s}  {'uw_net$M':>9s}  {'uw_side':>7s}  {'agree':>6s}  {'verdict':>10s}")
        for c, s, agree in rows[:25]:
            net_m = s.get("net_prem", 0) / 1_000_000
            print(f"  {c['ticker']:7s}  {c['actual_dir_pct']:>+6.2f}%  "
                  f"{c['expected_side']:>6s}  {s['n_alerts']:>5d}  "
                  f"{net_m:>+8.2f}M  {s['dominant_side']:>7s}  "
                  f"{'YES' if agree else 'no':>6s}  {c['verdict']:>10s}")
    else:
        print(f"  No UW data available for today's tradeable tickers")

    # ──────────────────────────────────────────────────────────────────────
    # Dump JSON
    # ──────────────────────────────────────────────────────────────────────
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    out_path = REPORT_DIR / f"run_{today}.json"
    out_path.write_text(json.dumps({
        "generated_utc": datetime.now(timezone.utc).isoformat(),
        "today": today,
        "dates": dates,
        "n_universe": len(universe),
        "n_with_bars": n_with_bars,
        "by_verdict": dict(by_verdict),
        "by_verdict_today": dict(by_verdict_today),
        "by_verdict_past": dict(by_verdict_past),
        "ticker_movement_budget": [
            {"ticker": tk, "total_abs_pct": total, "n_tradeable": n_t, "n_caught": n_c}
            for tk, total, n_t, n_c in ticker_moves[:60]
        ],
        "top_misses": misses[:50],
        "wrong_side_fires": wrongs[:25],
        "cells": cells,
    }, indent=2, default=str))
    print(f"\n[done] report → {out_path}  ({out_path.stat().st_size/1024:.1f} KB)")
    print(f"[done] full run in {time.time()-t0:.1f}s\n")


if __name__ == "__main__":
    main()
