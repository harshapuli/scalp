"""
V4 EOD SURGE THRESHOLD SWEEP — find the sweet spot.

Bucket boundaries ($5M, $1M) were guessed from the video. Real question:
at what dollar threshold does the late-30-min call-premium signal actually
produce edge above baseline?

This script:
  1. Loads v4_late_hour_flow_backtest.json — every ticker-day with the
     last_30m_call_$ value AND forward returns (window, gap, 1d, 3d).
  2. Sweeps thresholds from -$20M to +$20M in $0.5M steps.
  3. At each threshold, computes:
     - n events ≥ threshold (call side) or ≤ −threshold (put side)
     - Win rate at 1d and 3d horizons (signal-aligned)
     - Avg signal-aligned move
     - Edge vs baseline
  4. Finds the threshold where edge peaks AND sample size remains useful.

Run: python3 v4_eod_surge_threshold_sweep.py
Output: v4_eod_surge_threshold_sweep.json + console
"""
import os, sys, json, glob, statistics
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_THRESHOLD = 0.5  # % move counted as a hit


def date_from_snap(p):
    return os.path.basename(p).replace('v4_snapshot_', '').replace('.json', '')


def get_open_close(snap_data, tkr):
    blob = snap_data.get(tkr) or {}
    db = (blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}
    close = db.get('c')
    bars5 = blob.get('alpaca_bars_5m')
    if isinstance(bars5, dict): bars5 = bars5.get('bars') or bars5.get('data') or []
    open_px = None
    for b in (bars5 or []):
        if (b.get('t') or '')[11:16] == '13:30':
            open_px = b.get('o') or b.get('c'); break
    try:
        return (float(open_px) if open_px is not None else None,
                float(close) if close is not None else None)
    except: return None, None


def main():
    # Optional flag --exclude-earnings-within N : drop ticker-days within
    # N days of earnings (binary event distorts the signal).
    earnings_window_days = 0
    if '--exclude-earnings-within' in sys.argv:
        i = sys.argv.index('--exclude-earnings-within')
        if i + 1 < len(sys.argv):
            try: earnings_window_days = int(sys.argv[i + 1])
            except: pass

    # Re-run the per-ticker-day computation from scratch so we control the data
    snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    snaps = {}
    for p in snap_paths:
        d = date_from_snap(p)
        try: snaps[d] = json.load(open(p)).get('data') or {}
        except: pass
    dates = sorted(snaps.keys())
    print(f'loaded {len(dates)} snapshots: {dates}', file=sys.stderr)

    # Build {ticker: earnings_date} from latest snapshot — used to exclude
    # earnings-binary ticker-days when the flag is set.
    from datetime import datetime as _dt
    earnings_by_t = {}
    if earnings_window_days > 0:
        latest = json.load(open(snap_paths[-1])).get('data') or {}
        for tkr, blob in latest.items():
            info = (blob.get('info', {}).get('data') or {})
            e = info.get('next_earnings_date')
            if e:
                try: earnings_by_t[tkr] = _dt.strptime(e, '%Y-%m-%d').date()
                except: pass
        print(f'earnings dates loaded for {len(earnings_by_t)} tickers; '
              f'excluding within ±{earnings_window_days} days', file=sys.stderr)

    def is_earnings_binary(tkr, session_iso):
        if earnings_window_days <= 0: return False
        e = earnings_by_t.get(tkr)
        if not e: return False
        try:
            s = _dt.strptime(session_iso, '%Y-%m-%d').date()
            return abs((e - s).days) <= earnings_window_days
        except: return False

    # picks_77 set
    picks_path = os.path.join(HERE, 'data', 'picks_77.json')
    if os.path.exists(picks_path):
        pd = json.load(open(picks_path))
        picks = set()
        for k in ('mega', 'mid', 'small', 'indices'):
            for r in (pd.get('buckets') or {}).get(k, []) or []: picks.add(r['ticker'])
    else:
        picks = None  # use universe

    # For each ticker-day, compute last_30m_call_$ + forward returns
    obs = []
    excluded_count = 0
    for i, d in enumerate(dates):
        for tkr, blob in snaps[d].items():
            if picks and tkr not in picks: continue
            if is_earnings_binary(tkr, d):
                excluded_count += 1
                continue
            npt = blob.get('net_prem_ticks')
            if isinstance(npt, dict): npt = npt.get('data') or []
            if not isinstance(npt, list) or not npt: continue

            window_call = 0.0
            for t in npt:
                hh_mm = (t.get('tape_time') or t.get('t') or '')[11:16]
                if '19:30' <= hh_mm <= '20:00':
                    try: window_call += float(t.get('net_call_premium', 0) or 0)
                    except: pass
            window_call_m = window_call / 1e6

            # Window return
            bars5 = blob.get('alpaca_bars_5m')
            if isinstance(bars5, dict): bars5 = bars5.get('bars') or bars5.get('data') or []
            start_px, end_px, close_px = None, None, None
            for b in (bars5 or []):
                hh_mm = (b.get('t') or '')[11:16]
                if hh_mm == '19:30': start_px = b.get('o') or b.get('c')
                if '19:30' <= hh_mm <= '20:00': end_px = b.get('c')
                close_px = b.get('c')
            window_ret = None
            if start_px and end_px and start_px > 0:
                try: window_ret = (float(end_px) - float(start_px)) / float(start_px) * 100
                except: pass

            # Forward returns
            next_open, next_close = (None, None)
            day3_close = None
            if i + 1 < len(dates):
                next_open, next_close = get_open_close(snaps[dates[i+1]], tkr)
            if i + 3 < len(dates):
                _, day3_close = get_open_close(snaps[dates[i+3]], tkr)

            gap_pct = next_close_pct = day3_pct = None
            if close_px and next_open is not None and float(close_px) > 0:
                gap_pct = (next_open - float(close_px)) / float(close_px) * 100
            if close_px and next_close is not None and float(close_px) > 0:
                next_close_pct = (next_close - float(close_px)) / float(close_px) * 100
            if close_px and day3_close is not None and float(close_px) > 0:
                day3_pct = (day3_close - float(close_px)) / float(close_px) * 100

            obs.append({
                'ticker': tkr, 'date': d,
                'call_m': window_call_m,
                'window_ret':     round(window_ret, 3) if window_ret is not None else None,
                'gap_pct':        round(gap_pct, 3) if gap_pct is not None else None,
                'next_close_pct': round(next_close_pct, 3) if next_close_pct is not None else None,
                'day3_pct':       round(day3_pct, 3) if day3_pct is not None else None,
            })

    print(f'\ntotal ticker-days: {len(obs)} (excluded {excluded_count} earnings-binary)', file=sys.stderr)

    # Baseline (all)
    def stats_for(group, side):
        sign = 1 if side == 'call' else -1
        out = {'n': len(group)}
        for metric, key in [('window', 'window_ret'),
                            ('gap',    'gap_pct'),
                            ('next_close', 'next_close_pct'),
                            ('day3', 'day3_pct')]:
            vals = [r[key] for r in group if r.get(key) is not None]
            if not vals:
                out[f'{metric}_n'] = 0
                continue
            wins = sum(1 for v in vals if (v * sign) >= WIN_THRESHOLD)
            out[f'{metric}_n']      = len(vals)
            out[f'{metric}_win_pct'] = round(wins / len(vals) * 100, 1)
            out[f'{metric}_avg']     = round(statistics.mean(v * sign for v in vals), 3)
        return out

    baseline_call = stats_for(obs, 'call')
    baseline_put  = stats_for(obs, 'put')

    # Sweep thresholds
    thresholds = [0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 6, 7.5, 10, 12, 15, 20]
    call_sweep = []
    for t in thresholds:
        group = [r for r in obs if r['call_m'] >= t]
        s = stats_for(group, 'call')
        call_sweep.append({'threshold_m': t, **s,
                           'edge_3d_pp': (s.get('day3_win_pct') - baseline_call['day3_win_pct'])
                                         if s.get('day3_win_pct') is not None else None})

    put_sweep = []
    for t in thresholds:
        group = [r for r in obs if r['call_m'] <= -t]
        s = stats_for(group, 'put')
        put_sweep.append({'threshold_m': -t, **s,
                          'edge_3d_pp': (s.get('day3_win_pct') - baseline_put['day3_win_pct'])
                                        if s.get('day3_win_pct') is not None else None})

    # Find peak edge
    def find_peak(sweep, metric_n='day3_n', metric_win='day3_win_pct', min_n=10):
        best = None
        for s in sweep:
            n = s.get(metric_n) or 0
            wr = s.get(metric_win)
            if n < min_n or wr is None: continue
            if best is None or wr > best[metric_win]:
                best = s
        return best
    call_peak = find_peak(call_sweep)
    put_peak  = find_peak(put_sweep)

    out = {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'win_threshold_pct': WIN_THRESHOLD,
        'total_obs': len(obs),
        'sessions': dates,
        'baseline_call_side': baseline_call,
        'baseline_put_side':  baseline_put,
        'call_sweep': call_sweep,
        'put_sweep':  put_sweep,
        'call_peak':  call_peak,
        'put_peak':   put_peak,
    }
    with open(os.path.join(HERE, 'v4_eod_surge_threshold_sweep.json'), 'w') as f:
        json.dump(out, f, indent=2, default=str)

    # Console
    print(f"\n{'='*108}")
    print(f"EOD SURGE THRESHOLD SWEEP — find the sweet spot")
    print(f"n={len(obs)} ticker-days  ·  sessions: {dates[0]} → {dates[-1]}  ·  win thresh ±{WIN_THRESHOLD}%")
    print(f"{'='*108}")
    print(f"\nBASELINE (all ticker-days):")
    print(f"  call-side baseline 3d win = {baseline_call['day3_win_pct']}%   avg aligned = {baseline_call['day3_avg']:+.2f}%   n={baseline_call['day3_n']}")
    print(f"  put-side  baseline 3d win = {baseline_put['day3_win_pct']}%   avg aligned = {baseline_put['day3_avg']:+.2f}%   n={baseline_put['day3_n']}")

    print(f"\n--- CALL-SIDE SWEEP (≥ +$X M last-30-min net call premium) ---")
    print(f"{'thresh':>8} {'n':>4} | {'win_n':>5} {'1d_win%':>9} {'1d_avg':>8} | {'3d_win%':>9} {'3d_avg':>8} | edge_3d")
    print('-' * 90)
    for s in call_sweep:
        n = s.get('day3_n') or 0
        if (s.get('window_n') or 0) == 0: continue
        wr3 = f"{s['day3_win_pct']}%" if s.get('day3_win_pct') is not None else '—'
        avg3 = f"{s['day3_avg']:+.2f}%" if s.get('day3_avg') is not None else '—'
        wr1 = f"{s['next_close_win_pct']}%" if s.get('next_close_win_pct') is not None else '—'
        avg1 = f"{s['next_close_avg']:+.2f}%" if s.get('next_close_avg') is not None else '—'
        edge = f"+{s['edge_3d_pp']:.1f}pp" if s.get('edge_3d_pp') is not None else '—'
        marker = ' ← PEAK' if call_peak and s['threshold_m'] == call_peak['threshold_m'] else ''
        print(f"+${s['threshold_m']:>5.1f}M {s['n']:>4} | {n:>5} {wr1:>9} {avg1:>8} | {wr3:>9} {avg3:>8} | {edge:>7}{marker}")

    print(f"\n--- PUT-SIDE SWEEP (≤ -$X M last-30-min net call premium) ---")
    print(f"{'thresh':>8} {'n':>4} | {'win_n':>5} {'1d_win%':>9} {'1d_avg':>8} | {'3d_win%':>9} {'3d_avg':>8} | edge_3d")
    print('-' * 90)
    for s in put_sweep:
        n = s.get('day3_n') or 0
        if (s.get('window_n') or 0) == 0: continue
        wr3 = f"{s['day3_win_pct']}%" if s.get('day3_win_pct') is not None else '—'
        avg3 = f"{s['day3_avg']:+.2f}%" if s.get('day3_avg') is not None else '—'
        wr1 = f"{s['next_close_win_pct']}%" if s.get('next_close_win_pct') is not None else '—'
        avg1 = f"{s['next_close_avg']:+.2f}%" if s.get('next_close_avg') is not None else '—'
        edge = f"+{s['edge_3d_pp']:.1f}pp" if s.get('edge_3d_pp') is not None else '—'
        marker = ' ← PEAK' if put_peak and s['threshold_m'] == put_peak['threshold_m'] else ''
        print(f"-${abs(s['threshold_m']):>5.1f}M {s['n']:>4} | {n:>5} {wr1:>9} {avg1:>8} | {wr3:>9} {avg3:>8} | {edge:>7}{marker}")

    print(f"\n=== RECOMMENDED CUTOFFS (peaks with n≥10) ===")
    if call_peak:
        print(f"CALL: ≥ +${call_peak['threshold_m']:.1f}M  →  {call_peak['day3_win_pct']}% 3d win   "
              f"avg aligned {call_peak['day3_avg']:+.2f}%   "
              f"n={call_peak['day3_n']}   edge {call_peak['edge_3d_pp']:+.1f}pp")
    else:
        print('CALL: no threshold with n≥10 found')
    if put_peak:
        print(f"PUT:  ≤ -${abs(put_peak['threshold_m']):.1f}M  →  {put_peak['day3_win_pct']}% 3d win   "
              f"avg aligned {put_peak['day3_avg']:+.2f}%   "
              f"n={put_peak['day3_n']}   edge {put_peak['edge_3d_pp']:+.1f}pp")
    else:
        print('PUT:  no threshold with n≥10 found')

    print(f"\nSaved → v4_eod_surge_threshold_sweep.json")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
