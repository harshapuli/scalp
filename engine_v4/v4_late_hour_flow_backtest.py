"""
V4 LATE-HOUR FLOW BACKTEST — testing the "someone always knows" thesis.

Hypothesis (from Real Peter Tarr's UW tutorial, Dec 2021):
  When institutional call premium surges in the LAST 30 minutes of RTH,
  the underlying moves UP in those final minutes AND/OR gaps up next day.

What we measure (using our 6 snapshots of per-minute UW flow + 5-min bars):
  For each (ticker, day):
    1. last_30m_net_call_$ — sum of net_call_premium in 19:30-20:00Z
    2. last_30m_return_pct — price move in those 30 min
    3. next_day_gap_pct   — next-day open vs today's close

  Then bucket ticker-days by last_30m_net_call_$ and compare:
    - hit rate of "moved up ≥0.5% in last 30 min"
    - hit rate of "gapped up ≥0.5% next day"
    - average return in each bucket

Caveat: our flow data is TOTAL flow (all expirations), not 0DTE-specific.
The video's strict tactic is 0DTE-only. So this backtest is a DEGRADED
version — but still tests the core "late-day institutional buying →
imminent move" thesis.

Run: python3 v4_late_hour_flow_backtest.py
Output: v4_late_hour_flow_backtest.json
"""
import os, json, glob, sys, statistics
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'v4_late_hour_flow_backtest.json')

# Window: 19:30-20:00 UTC = 12:30-1:00 PM PT = last 30 min of RTH
WINDOW_START_UTC = '19:30'
WINDOW_END_UTC   = '20:00'
WIN_THRESHOLD = 0.5  # % move that counts as a "win"


def parse_iso(t):
    return datetime.fromisoformat(t.replace('Z', '+00:00'))


def date_from_snap_path(p):
    return os.path.basename(p).replace('v4_snapshot_', '').replace('.json', '')


def main():
    snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    if len(snap_paths) < 2:
        print('Need ≥2 snapshots for next-day gap measurement', file=sys.stderr)
        return 1

    # Load all snapshots
    snaps = {}
    for p in snap_paths:
        d = date_from_snap_path(p)
        try:
            snaps[d] = json.load(open(p)).get('data') or {}
            print(f'  loaded {d}', file=sys.stderr)
        except: pass
    dates = sorted(snaps.keys())

    # For each (ticker, day), compute the test variables
    rows = []
    for i, d in enumerate(dates):
        next_d = dates[i + 1] if i + 1 < len(dates) else None
        for tkr, blob in snaps[d].items():
            # 1. Last-30-min net_call_premium
            npt = blob.get('net_prem_ticks')
            if isinstance(npt, dict): npt = npt.get('data') or []
            if not isinstance(npt, list) or not npt: continue

            window_call_prem = 0.0
            window_put_prem = 0.0
            window_call_ask_vol = 0
            window_call_total_vol = 0
            ticks_in_window = 0
            for t in npt:
                tape_t = t.get('tape_time') or t.get('t') or ''
                if not tape_t: continue
                hh_mm = tape_t[11:16]  # 'HH:MM'
                if hh_mm < WINDOW_START_UTC or hh_mm > WINDOW_END_UTC:
                    continue
                ticks_in_window += 1
                try:
                    window_call_prem += float(t.get('net_call_premium', 0) or 0)
                    window_put_prem  += float(t.get('net_put_premium', 0) or 0)
                    window_call_ask_vol += int(t.get('call_volume_ask_side', 0) or 0)
                    window_call_total_vol += int(t.get('call_volume', 0) or 0)
                except: continue
            if ticks_in_window == 0: continue

            window_call_prem_m = window_call_prem / 1e6
            window_put_prem_m  = window_put_prem  / 1e6
            window_net_call_minus_put_m = window_call_prem_m - window_put_prem_m
            ask_share = (window_call_ask_vol / max(window_call_total_vol, 1)) if window_call_total_vol else 0

            # 2. Price action in window — use 5-min bars
            bars5 = blob.get('alpaca_bars_5m')
            if isinstance(bars5, dict): bars5 = bars5.get('bars') or bars5.get('data') or []
            if not isinstance(bars5, list) or not bars5: continue

            # Find bar at 19:30 (start of window) and 19:55 (last bar in window)
            start_px, end_px, max_px, min_px = None, None, None, None
            close_px = None
            for b in bars5:
                t = (b.get('t') or '')
                hh_mm = t[11:16]
                if hh_mm == '19:30':
                    start_px = b.get('o') or b.get('c')
                if WINDOW_START_UTC <= hh_mm <= WINDOW_END_UTC:
                    h = b.get('h')
                    l = b.get('l')
                    end_px = b.get('c')
                    if h is not None: max_px = max(max_px or h, h)
                    if l is not None: min_px = min(min_px or l, l)
                # Track final close of day (last bar)
                close_px = b.get('c')

            if start_px is None or end_px is None or start_px <= 0:
                continue

            window_return_pct = (end_px - start_px) / start_px * 100
            window_max_run_pct = (max_px - start_px) / start_px * 100 if max_px else None

            # 3. Next-day gap
            next_open = None
            next_open_close = None
            if next_d and tkr in snaps[next_d]:
                next_blob = snaps[next_d][tkr]
                next_bars = next_blob.get('alpaca_bars_5m')
                if isinstance(next_bars, dict):
                    next_bars = next_bars.get('bars') or next_bars.get('data') or []
                # Find first RTH bar (13:30Z = market open)
                for b in (next_bars or []):
                    if (b.get('t') or '')[11:16] == '13:30':
                        next_open = b.get('o') or b.get('c')
                        break
                # Today's close from snapshot dailyBar
                next_open_close = ((next_blob.get('alpaca_snapshot') or {}).get('prevDailyBar') or {}).get('c')

            gap_pct = None
            if next_open is not None and close_px and close_px > 0:
                try: gap_pct = (float(next_open) - float(close_px)) / float(close_px) * 100
                except: pass

            rows.append({
                'ticker': tkr, 'date': d,
                'window_call_prem_m':       round(window_call_prem_m, 3),
                'window_put_prem_m':        round(window_put_prem_m, 3),
                'window_net_diff_m':        round(window_net_call_minus_put_m, 3),
                'window_call_ask_share':    round(ask_share, 3),
                'window_return_pct':        round(window_return_pct, 3),
                'window_max_run_pct':       round(window_max_run_pct, 3) if window_max_run_pct is not None else None,
                'gap_pct':                  round(gap_pct, 3) if gap_pct is not None else None,
                'ticks_in_window':          ticks_in_window,
            })

    print(f'\nTotal ticker-days with full data: {len(rows)}', file=sys.stderr)
    if not rows:
        print('No usable data — abort', file=sys.stderr); return 1

    # ─── Bucket by last-30-min net call premium ───────────────────
    # Use absolute thresholds + percentile groups
    rows.sort(key=lambda r: -r['window_call_prem_m'])
    n = len(rows)
    top_decile_cutoff = rows[max(0, n // 10 - 1)]['window_call_prem_m']
    top_quintile_cutoff = rows[max(0, n // 5 - 1)]['window_call_prem_m']
    median_cutoff = rows[max(0, n // 2 - 1)]['window_call_prem_m']

    def bucketize(r):
        v = r['window_call_prem_m']
        if v >= 5.0: return 'SURGE_5M+'
        if v >= 1.0: return 'STRONG_1-5M'
        if v >= 0.1: return 'MODERATE'
        if v >= -0.1: return 'NEUTRAL'
        if v >= -1.0: return 'WEAK_BEAR'
        return 'STRONG_BEAR'

    buckets = defaultdict(list)
    for r in rows:
        buckets[bucketize(r)].append(r)

    def stats(group):
        if not group:
            return {'n': 0, 'window_win_rate': None, 'gap_win_rate': None,
                    'avg_window_ret': None, 'avg_gap': None, 'max_run_avg': None}
        win_window = sum(1 for r in group if r['window_return_pct'] >= WIN_THRESHOLD)
        gaps = [r['gap_pct'] for r in group if r.get('gap_pct') is not None]
        win_gap = sum(1 for g in gaps if g >= WIN_THRESHOLD)
        runs = [r['window_max_run_pct'] for r in group if r.get('window_max_run_pct') is not None]
        return {
            'n': len(group),
            'window_win_rate': round(win_window / len(group) * 100, 1),
            'avg_window_ret':  round(statistics.mean(r['window_return_pct'] for r in group), 3),
            'window_max_run_avg': round(statistics.mean(runs), 3) if runs else None,
            'gap_n':           len(gaps),
            'gap_win_rate':    round(win_gap / len(gaps) * 100, 1) if gaps else None,
            'avg_gap':         round(statistics.mean(gaps), 3) if gaps else None,
            'sample':          [(r['ticker'], r['date'], r['window_call_prem_m'], r['window_return_pct'])
                                for r in sorted(group, key=lambda x: -x['window_call_prem_m'])[:5]],
        }

    bucket_order = ['SURGE_5M+', 'STRONG_1-5M', 'MODERATE', 'NEUTRAL', 'WEAK_BEAR', 'STRONG_BEAR']
    summary = {b: stats(buckets[b]) for b in bucket_order}

    # Universe baseline
    baseline = stats(rows)

    # Top-N by call premium
    top_20 = sorted(rows, key=lambda r: -r['window_call_prem_m'])[:20]
    bot_20 = sorted(rows, key=lambda r: r['window_call_prem_m'])[:20]

    out = {
        'generated_utc':       datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'window_utc':          f'{WINDOW_START_UTC} → {WINDOW_END_UTC} (last 30 min of RTH)',
        'win_threshold_pct':   WIN_THRESHOLD,
        'snapshots_used':      dates,
        'total_obs':           n,
        'thresholds':          {'top_decile': top_decile_cutoff,
                                'top_quintile': top_quintile_cutoff,
                                'median': median_cutoff},
        'baseline_universe':   baseline,
        'by_bucket':           summary,
        'top_20_by_late_call': top_20,
        'bottom_20_by_late_call': bot_20,
    }
    with open(OUT, 'w') as f:
        json.dump(out, f, indent=2, default=str)

    # Console table
    print(f"\n{'='*108}")
    print(f"V4 LATE-HOUR FLOW BACKTEST — 'Someone Always Knows' thesis")
    print(f"Window: 19:30-20:00 UTC (12:30-1:00 PT = last 30 min RTH)")
    print(f"Win threshold: ±{WIN_THRESHOLD}% in window OR overnight gap")
    print(f"Total ticker-days: {n}  ·  Snapshots: {dates[0]} → {dates[-1]}")
    print(f"{'='*108}")
    print(f"\nBASELINE (universe-wide): n={baseline['n']}  "
          f"window_win={baseline['window_win_rate']}%  avg_ret={baseline['avg_window_ret']:+.2f}%  "
          f"gap_win={baseline['gap_win_rate']}%  avg_gap={baseline['avg_gap'] if baseline['avg_gap'] is not None else '—'}")
    print(f"\n{'BUCKET':<14} {'N':>4} {'win_rate':>9} {'avg_ret':>9} {'max_run':>9} | "
          f"{'gap_n':>5} {'gap_win':>8} {'avg_gap':>9} | edge_window  edge_gap")
    print('-' * 108)
    for b in bucket_order:
        s = summary[b]
        wr = f"{s['window_win_rate']}%" if s['window_win_rate'] is not None else '—'
        ar = f"{s['avg_window_ret']:+.2f}%" if s['avg_window_ret'] is not None else '—'
        mr = f"{s['window_max_run_avg']:+.2f}%" if s['window_max_run_avg'] is not None else '—'
        gr = f"{s['gap_win_rate']}%" if s['gap_win_rate'] is not None else '—'
        gap = f"{s['avg_gap']:+.2f}%" if s['avg_gap'] is not None else '—'
        edge_w = f"+{s['window_win_rate'] - baseline['window_win_rate']:.1f}pp" if s['window_win_rate'] is not None and baseline['window_win_rate'] is not None else '—'
        edge_g = f"+{s['gap_win_rate'] - baseline['gap_win_rate']:.1f}pp" if s['gap_win_rate'] is not None and baseline['gap_win_rate'] is not None else '—'
        print(f"{b:<14} {s['n']:>4} {wr:>9} {ar:>9} {mr:>9} | {s['gap_n']:>5} {gr:>8} {gap:>9} | {edge_w:>10}  {edge_g:>9}")

    print(f"\n--- TOP 10 BY LATE-30-MIN NET CALL PREMIUM ---")
    print(f"{'tkr':<6} {'date':<11} {'late_call_$M':>11} {'win_ret':>8} {'max_run':>8} {'next_gap':>9}")
    for r in top_20[:10]:
        gap_s = f"{r['gap_pct']:+.2f}%" if r.get('gap_pct') is not None else '—'
        max_s = f"{r['window_max_run_pct']:+.2f}%" if r.get('window_max_run_pct') is not None else '—'
        print(f"{r['ticker']:<6} {r['date']:<11} {r['window_call_prem_m']:>+10.2f}M "
              f"{r['window_return_pct']:>+7.2f}% {max_s:>8} {gap_s:>9}")

    print(f"\n--- BOTTOM 10 BY LATE-30-MIN NET CALL PREMIUM (most bearish flow) ---")
    print(f"{'tkr':<6} {'date':<11} {'late_call_$M':>11} {'win_ret':>8} {'max_run':>8} {'next_gap':>9}")
    for r in bot_20[:10]:
        gap_s = f"{r['gap_pct']:+.2f}%" if r.get('gap_pct') is not None else '—'
        max_s = f"{r['window_max_run_pct']:+.2f}%" if r.get('window_max_run_pct') is not None else '—'
        print(f"{r['ticker']:<6} {r['date']:<11} {r['window_call_prem_m']:>+10.2f}M "
              f"{r['window_return_pct']:>+7.2f}% {max_s:>8} {gap_s:>9}")

    print(f"\nSaved → {OUT}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
