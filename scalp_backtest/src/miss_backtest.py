"""
miss_backtest.py — investigate specific missed signals.

User-flagged misses for 2026-04-27:
  • NVDA call (today, and reportedly Friday)
  • ARM put (missed)
  • QCOM put (missed)

For each ticker, pull:
  - lifecycle timeline (engine state, day%, peak/trough, kinds fired)
  - Alpaca daily bars for the last 30 days (the "what happened" reference)
  - lifecycle drilldown (compare what the engine SAW vs the actual move)
  - any scalp signals we DID generate (catch vs miss vs just observed)

Output: human-readable terminal report + JSON dump for the dashboard.
"""
from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from lifecycle_drilldown import load_timeline, INTRADAY_ROOT
from loader_lifecycle import load_lifecycle_for_date
from loader_scalp import load_all as load_scalp_all
from outcome_intraday import compute_all as compute_scalp_all, dedup_signals as dedup_scalp
from alpaca_bars import fetch_daily_bars_for_swing_tickers
from uw_flow import fetch_global_flow, aggregate_by_ticker
from loader_historical import load_historical


PROJECT_ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = PROJECT_ROOT / "data" / "miss_reports"


TICKERS_TO_INVESTIGATE = [
    ("NVDA", "expected: CALL"),
    ("ARM",  "expected: PUT"),
    ("QCOM", "expected: PUT"),
]


def _hhmm(s):
    if not s:
        return "—"
    s = str(s)
    return s[11:16] if len(s) > 16 else s


def investigate(ticker: str, date_str: str, expected: str,
                scalp_outs, daily_bars: dict, uw_aggs: dict) -> dict:
    print(f"\n{'='*78}")
    print(f"  {ticker}  ·  {date_str}  ·  {expected}")
    print(f"{'='*78}")

    report = {"ticker": ticker, "date": date_str, "expected": expected}

    # 1. Lifecycle timeline — what did the engine see?
    tl = load_timeline(ticker, date_str)
    if not tl:
        print(f"  ⚠ no lifecycle for {ticker} on {date_str} — engine wasn't tracking it")
        report["had_lifecycle"] = False
    else:
        report["had_lifecycle"] = True
        report["n_snapshots"] = tl.n_snapshots
        report["kinds_fired"] = list(tl.kinds_fired_chronological)
        # Day move stats
        day_pcts = [p.day_pct for p in tl.points if p.day_pct is not None]
        rvols = [p.rvol for p in tl.points if p.rvol is not None]
        if day_pcts:
            report["day_pct_min"] = min(day_pcts)
            report["day_pct_max"] = max(day_pcts)
            report["day_pct_first"] = day_pcts[0]
            report["day_pct_last"] = day_pcts[-1]
        if rvols:
            report["rvol_max"] = max(rvols)
        # Modes seen
        modes = {}
        for p in tl.points:
            modes[p.mode or "?"] = modes.get(p.mode or "?", 0) + 1
        report["modes"] = modes

        # Final open trade
        if tl.final_open_trade:
            ft = tl.final_open_trade
            report["final_open_trade"] = {
                "kind": ft.get("kind"),
                "side": ft.get("side"),
                "entry": ft.get("entry_price"),
                "underlying_pct": ft.get("last_underlying_pct"),
                "best_pct": ft.get("best_option_pnl_pct"),
                "worst_pct": ft.get("worst_option_pnl_pct"),
                "state": ft.get("state"),
                "exit_reason": ft.get("exit_reason"),
            }

        # News / flow context
        report["n_news"] = len(tl.news_seen)
        report["n_flow"] = len(tl.flow_seen)
        report["had_conviction"] = any(p.has_conviction for p in tl.points)

        print(f"  snapshots: {tl.n_snapshots}")
        print(f"  modes seen: {modes}")
        if day_pcts:
            print(f"  day% first→last: {day_pcts[0]:+.2f}% → {day_pcts[-1]:+.2f}%  "
                  f"(range {min(day_pcts):+.2f}% to {max(day_pcts):+.2f}%)")
        else:
            print(f"  day% first→last: — (no day_pct in any snapshot)")
        if rvols:
            print(f"  RVOL peak: {report['rvol_max']:.2f}")
        print(f"  kinds fired: {[k for _, k in tl.kinds_fired_chronological] or 'NONE'}")
        if tl.final_open_trade:
            ft = tl.final_open_trade
            pnl = ft.get('last_underlying_pct')
            pnl_str = f"{pnl:+.2f}%" if pnl is not None else "—"
            print(f"  final trade: kind={ft.get('kind')} side={ft.get('side')} "
                  f"state={ft.get('state')} pnl={pnl_str}")
        print(f"  news items: {len(tl.news_seen)} | flow events: {len(tl.flow_seen)} | "
              f"conviction: {report['had_conviction']}")

        # When did each mode change?
        prev_mode = None
        mode_changes = []
        for p in tl.points:
            if p.mode != prev_mode:
                mode_changes.append({"time_utc": p.as_of_utc, "mode": p.mode,
                                     "day_pct": p.day_pct, "rvol": p.rvol})
                prev_mode = p.mode
        report["mode_transitions"] = mode_changes
        if len(mode_changes) > 1:
            print(f"  mode transitions: {len(mode_changes)}")
            for c in mode_changes[:8]:
                print(f"    {_hhmm(c['time_utc'])}  {c['mode']:8s}  "
                      f"day%={c['day_pct']:+.2f}% rvol={c.get('rvol') or 0:.2f}")

    # 2. Scalp signals we DID generate
    matching_outs = [o for o in scalp_outs if o.ticker == ticker and o.date_str == date_str]
    report["scalp_signals_generated"] = len(matching_outs)
    if matching_outs:
        print(f"\n  ► scalp signals we generated for {ticker}: {len(matching_outs)}")
        for o in matching_outs:
            atr_pnl = o.atr_pnl_stop_first_pct
            atr_pnl_str = f"{atr_pnl:+.2f}%" if atr_pnl is not None else "—"
            side_str = (o.side or "—")[:5].ljust(5)
            conf_str = f"{o.confidence:>5.1f}" if o.confidence is not None else "  —  "
            label_str = f"{o.label_underlying:+d}" if o.label_underlying is not None else " —"
            print(f"    {_hhmm(o.detected_utc)}  {o.kind:24s} side={side_str} "
                  f"conf={conf_str} | ATR-pnl={atr_pnl_str}  "
                  f"label_under={label_str}")
        report["scalp_details"] = [
            {"time": _hhmm(o.detected_utc), "kind": o.kind, "side": o.side,
             "conf": o.confidence, "atr_pnl_pct": o.atr_pnl_stop_first_pct,
             "label_under": o.label_underlying, "label_opt": o.label_option_5p5x,
             "fire_has_flow": o.at_fire_has_whale_flow,
             "fire_has_news": o.at_fire_has_news,
             "fire_has_conv": o.at_fire_has_conviction}
            for o in matching_outs
        ]
    else:
        print(f"\n  ► NO scalp signals generated for {ticker}")

    # 3. Alpaca daily bars — multi-day context
    bars = (daily_bars or {}).get(ticker) or []
    if bars:
        last5 = bars[-5:]
        print(f"\n  ► Alpaca daily bars (last 5):")
        print(f"    {'DATE':12s} {'open':>8s} {'high':>8s} {'low':>8s} {'close':>8s} "
              f"{'%chg':>7s} {'volume':>12s}")
        prev_c = None
        bar_summary = []
        for b in last5:
            chg = ((b.c - prev_c) / prev_c * 100) if prev_c else 0.0
            print(f"    {b.date:12s} {b.o:>8.2f} {b.h:>8.2f} {b.l:>8.2f} {b.c:>8.2f} "
                  f"{chg:>+6.2f}% {b.v:>12,}")
            bar_summary.append({"date": b.date, "o": b.o, "h": b.h, "l": b.l,
                                "c": b.c, "v": b.v, "chg_pct": chg})
            prev_c = b.c
        report["alpaca_last5"] = bar_summary

        # Today's intraday range from bars
        if bars:
            today = bars[-1]
            range_pct = (today.h - today.l) / today.o * 100 if today.o else 0
            close_v_open_pct = (today.c - today.o) / today.o * 100 if today.o else 0
            print(f"\n  ► today's stats (Alpaca daily): "
                  f"range={range_pct:.2f}%  close_vs_open={close_v_open_pct:+.2f}%")
            report["today_range_pct"] = range_pct
            report["today_close_vs_open_pct"] = close_v_open_pct

    # 4. Live UW flow for this ticker
    agg = (uw_aggs or {}).get(ticker)
    if agg:
        print(f"\n  ► live UW flow (last 24h):")
        print(f"    alerts={agg.n_alerts} sweeps={agg.sweep_n} "
              f"call$M={agg.call_prem_m:.2f} put$M={agg.put_prem_m:.2f} "
              f"net={agg.net_call_skew_m:+.2f}M call_share={agg.call_share*100:.0f}%")
        report["uw_flow"] = {
            "n_alerts": agg.n_alerts,
            "sweep_n": agg.sweep_n,
            "call_prem_m": agg.call_prem_m,
            "put_prem_m": agg.put_prem_m,
            "net_call_skew_m": agg.net_call_skew_m,
            "call_share": agg.call_share,
        }
    else:
        print(f"\n  ► no live UW flow for {ticker}")

    # 5. Diagnosis (historical archive cross-check is added later by main())
    print(f"\n  ► diagnosis:")
    diagnosis = []
    if not tl:
        diagnosis.append("ticker NOT in engine_v4 universe — needs to be added to scan list")
    elif not tl.kinds_fired_chronological:
        if tl.points and any(p.mode == "TRADE" for p in tl.points):
            diagnosis.append("engine was in TRADE mode but never opened a trade — coil/breakout never confirmed")
        elif tl.points and all(p.mode == "WATCH" for p in tl.points):
            diagnosis.append("engine stayed in WATCH the entire session — RVOL/conviction gates never tripped")
        else:
            diagnosis.append("no kinds fired — investigate detector thresholds")
    else:
        kinds_str = ", ".join(k for _, k in tl.kinds_fired_chronological)
        diagnosis.append(f"engine fired: {kinds_str} — check if direction matches expected ({expected})")

    # Direction check vs Alpaca close-vs-open
    if bars and "today_close_vs_open_pct" in report:
        cvo = report["today_close_vs_open_pct"]
        if "CALL" in expected and cvo < 0:
            diagnosis.append(f"⚠ Alpaca daily shows CLOSE BELOW OPEN ({cvo:+.2f}%) — call expectation may be wrong")
        elif "PUT" in expected and cvo > 0:
            diagnosis.append(f"⚠ Alpaca daily shows CLOSE ABOVE OPEN ({cvo:+.2f}%) — put expectation may be wrong")
        else:
            diagnosis.append(f"✓ Alpaca close-vs-open ({cvo:+.2f}%) aligns with expected direction")

    for d in diagnosis:
        print(f"    • {d}")
    report["diagnosis"] = diagnosis

    return report


def historical_lookup(ticker: str, dates: list[str], hist_sigs: list) -> dict:
    """Pull engine_scalp historical signals for a ticker on specific dates.
    Returns {date: [signal_summary, ...]} — empty list means we DID see this
    ticker historically but not on that date; absent date means no archive data."""
    by_date: dict[str, list] = {d: [] for d in dates}
    for s in hist_sigs:
        if s.ticker != ticker:
            continue
        if s.date_str in by_date:
            by_date[s.date_str].append({
                "strategy": s.strategy_id,
                "label": s.strategy_label,
                "trigger": s.trigger,
                "direction": s.direction,
                "entry_t": s.entry_t,
                "exit_t": s.exit_t,
                "entry_px": s.entry_px,
                "exit_px": s.exit_px,
                "minutes_held": s.minutes_held,
                "pnl_pct": s.pnl_pct,
                "outcome": s.outcome,
                "exit_reason": s.exit_reason,
                "category": s.category,
            })
    return by_date


def print_historical_block(ticker: str, expected: str, dates: list[str],
                           hist_sigs: list, hist_lookup: dict) -> None:
    print(f"\n{'─'*78}")
    print(f"  HISTORICAL ARCHIVE (engine_scalp) — {ticker}  [{expected}]")
    print(f"{'─'*78}")

    n_total = sum(1 for s in hist_sigs if s.ticker == ticker)
    if n_total == 0:
        print(f"  ✗ {ticker} NOT in engine_scalp archive ({len(hist_sigs)} signals scanned)")
        return

    # Top-line for ticker
    sigs_t = [s for s in hist_sigs if s.ticker == ticker]
    n_call = sum(1 for s in sigs_t if s.direction == "CALL")
    n_put = sum(1 for s in sigs_t if s.direction == "PUT")
    n_win = sum(1 for s in sigs_t if s.is_win)
    n_loss = sum(1 for s in sigs_t if s.is_loss)
    pnls = [s.pnl_pct for s in sigs_t if s.pnl_pct is not None]
    avg = sum(pnls)/len(pnls) if pnls else 0.0
    median = sorted(pnls)[len(pnls)//2] if pnls else 0.0
    print(f"  archive coverage: {n_total} signals  (CALL={n_call}, PUT={n_put})  "
          f"W={n_win} L={n_loss}  avg pnl={avg:+.2f}%  median={median:+.2f}%")

    # Top winners and worst losers for this ticker
    have_pnl = [s for s in sigs_t if s.pnl_pct is not None]
    if have_pnl:
        top5 = sorted(have_pnl, key=lambda s: s.pnl_pct, reverse=True)[:5]
        worst5 = sorted(have_pnl, key=lambda s: s.pnl_pct)[:5]
        print(f"  top 5 winners (all-time):")
        for s in top5:
            print(f"    {s.date_str}  {s.direction or '—':5s}  {s.trigger or '—':22s}  "
                  f"pnl={s.pnl_pct:+8.1f}%  ({s.exit_reason or '—'})")
        print(f"  worst 5 losers (all-time):")
        for s in worst5:
            print(f"    {s.date_str}  {s.direction or '—':5s}  {s.trigger or '—':22s}  "
                  f"pnl={s.pnl_pct:+8.1f}%  ({s.exit_reason or '—'})")

    # Per-date lookups
    print(f"\n  ► lookups for requested dates:")
    for d in dates:
        rows = hist_lookup.get(d) or []
        if rows:
            print(f"    {d}: {len(rows)} signal(s) in archive:")
            for r in rows:
                print(f"      {r['entry_t'][:16] if r['entry_t'] else '—':16s}  "
                      f"{(r['direction'] or '—'):5s}  "
                      f"{r['trigger'] or '—':22s}  "
                      f"strat={r['strategy'][:30]:30s}  "
                      f"pnl={r.get('pnl_pct') or 0:+8.2f}%  "
                      f"({r.get('exit_reason') or '—'})")
        else:
            # ticker IS in archive but no signal that date
            print(f"    {d}: no signals in archive  (engine_scalp didn't fire {ticker} that day)")


def main():
    today = "2026-04-27"
    friday = "2026-04-24"          # last trading day before today
    print(f"\n{'#'*78}")
    print(f"  Miss backtest — investigating user-flagged tickers")
    print(f"  Today: {today}  ·  Last trading day: {friday}")
    print(f"{'#'*78}")

    # Load scalp outcomes (with fire-time enrichment if cached)
    print("[load] scalp signals + outcomes...")
    sigs, bars = load_scalp_all()
    dedup = dedup_scalp(sigs)
    scalp_outs = compute_scalp_all(dedup, bars)
    # Try to enrich
    try:
        from fire_time_enrich import enrich
        n = enrich(scalp_outs)
        print(f"[load] enriched {n}/{len(scalp_outs)} outcomes with fire-time context")
    except Exception as e:
        print(f"[load] enrichment skipped: {e!r}")

    # Pull Alpaca daily bars for our 3 tickers
    print("[load] Alpaca daily bars (30-day lookback)...")
    tickers = [t for t, _ in TICKERS_TO_INVESTIGATE]
    daily_bars = fetch_daily_bars_for_swing_tickers(tickers, lookback_days=30)

    # Pull live UW flow
    print("[load] UW flow...")
    try:
        alerts = fetch_global_flow(lookback_hours=24)
        uw_aggs = aggregate_by_ticker(alerts)
        print(f"[load] {len(alerts)} alerts → {len(uw_aggs)} ticker(s)")
    except Exception as e:
        print(f"[load] UW flow failed: {e!r}")
        uw_aggs = {}

    # Pull historical archive (multi-year backtest signals)
    print("[load] historical archive (engine_scalp)...")
    hist_sigs = load_historical()
    print(f"[load] historical: {len(hist_sigs)} signals across "
          f"{len(set(s.date_str for s in hist_sigs))} dates")

    # Investigate each
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    all_reports = []
    lookup_dates = [today, friday]
    for ticker, expected in TICKERS_TO_INVESTIGATE:
        rpt = investigate(ticker, today, expected, scalp_outs, daily_bars, uw_aggs)
        # Add Friday-as-historical block
        hist_lookup = historical_lookup(ticker, lookup_dates, hist_sigs)
        rpt["historical_lookup"] = hist_lookup
        rpt["historical_total"] = sum(1 for s in hist_sigs if s.ticker == ticker)
        print_historical_block(ticker, expected, lookup_dates, hist_sigs, hist_lookup)
        all_reports.append(rpt)

    # Dump JSON
    out_path = REPORT_DIR / f"miss_report_{today}.json"
    out_path.write_text(json.dumps(all_reports, indent=2, default=str))
    print(f"\n[done] report saved to {out_path}")


if __name__ == "__main__":
    main()
