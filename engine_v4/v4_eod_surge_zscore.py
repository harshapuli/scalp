"""
V4 EOD SURGE — Z-SCORE BACKTEST (per-ticker baseline)

User insight: instead of absolute $ thresholds (which conflate "this name
always has $5M flow" with "this is an unusual spike"), use a DELTA from
the ticker's own baseline. NVDA at +$5M is routine; MARA at +$5M is
6 standard deviations above its norm.

Compute, for each ticker:
  baseline = median(last_30m_net_call_$) across our 6 sessions
  std      = stdev of that metric for the same ticker
  z(t,d)   = (today's last_30m_call_$ - baseline) / std

Then bucket events by |z| and see whether z-score thresholds produce
cleaner edge than absolute-$ thresholds.

CAVEAT: 6 days is too short to estimate a real baseline (need 20+).
But we can compare the relative performance of z-score vs absolute-$
within this small window to see if the framing is promising.

Run: python3 v4_eod_surge_zscore.py
Output: v4_eod_surge_zscore.json
"""
import os, sys, json, glob, statistics
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


def date_from_snap(p):
    return os.path.basename(p).replace('v4_snapshot_', '').replace('.json', '')


def get_close(snap_data, tkr):
    blob = snap_data.get(tkr) or {}
    db = (blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}
    c = db.get('c')
    try: return float(c) if c is not None else None
    except: return None


def main():
    snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    snaps = {}
    for p in snap_paths:
        d = date_from_snap(p)
        try: snaps[d] = json.load(open(p)).get('data') or {}
        except: pass
    dates = sorted(snaps.keys())

    # picks
    picks_path = os.path.join(HERE, 'data', 'picks_77.json')
    picks = set()
    if os.path.exists(picks_path):
        pd = json.load(open(picks_path))
        for k in ('mega', 'mid', 'small', 'indices'):
            for r in (pd.get('buckets') or {}).get(k, []) or []: picks.add(r['ticker'])

    # Compute last_30m_call_$ for every (ticker, date)
    flow_by_ticker = defaultdict(list)  # ticker → [(date, call_m, close_px)]
    for i, d in enumerate(dates):
        for tkr, blob in snaps[d].items():
            if picks and tkr not in picks: continue
            npt = blob.get('net_prem_ticks')
            if isinstance(npt, dict): npt = npt.get('data') or []
            if not isinstance(npt, list) or not npt: continue
            wcall = 0.0
            ticks = 0
            for t in npt:
                hh_mm = (t.get('tape_time') or t.get('t') or '')[11:16]
                if '19:30' <= hh_mm <= '20:00':
                    ticks += 1
                    try: wcall += float(t.get('net_call_premium', 0) or 0)
                    except: pass
            if ticks == 0: continue
            wcall_m = wcall / 1e6
            close_px = ((blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
            flow_by_ticker[tkr].append((i, d, wcall_m, close_px))

    # Compute baseline + std per ticker (using ALL its days — small window)
    baseline_by_t = {}
    for tkr, rows in flow_by_ticker.items():
        if len(rows) < 3: continue  # need at least 3 days for any baseline
        vals = [r[2] for r in rows]
        median = statistics.median(vals)
        try: std = statistics.stdev(vals)
        except: std = 0
        baseline_by_t[tkr] = {
            'median': median,
            'std': std if std > 0 else max(abs(median) * 0.5, 0.5),  # floor std
            'n_days': len(rows),
            'all_values': [round(v, 2) for v in vals],
        }

    print(f"baselines computed for {len(baseline_by_t)} tickers", file=sys.stderr)
    print(f"sample baselines:", file=sys.stderr)
    for t in ['NVDA', 'INTC', 'MARA', 'OKLO', 'AMZN', 'META', 'SPY']:
        if t in baseline_by_t:
            b = baseline_by_t[t]
            print(f"  {t:<6} median=${b['median']:+6.2f}M  std=${b['std']:>5.2f}M  values={b['all_values']}", file=sys.stderr)

    # Build observations with z-score and 3d return
    obs = []
    for tkr, rows in flow_by_ticker.items():
        if tkr not in baseline_by_t: continue
        b = baseline_by_t[tkr]
        for idx, d, call_m, close_px in rows:
            z = (call_m - b['median']) / b['std']
            # 3d forward
            day3_close = None
            if idx + 3 < len(dates):
                day3_close = get_close(snaps[dates[idx + 3]], tkr)
            day3_pct = None
            if close_px and day3_close is not None and float(close_px) > 0:
                try: day3_pct = (day3_close - float(close_px)) / float(close_px) * 100
                except: pass
            obs.append({
                'ticker': tkr, 'date': d,
                'call_m': round(call_m, 2),
                'baseline_m': round(b['median'], 2),
                'std_m': round(b['std'], 2),
                'z_score': round(z, 2),
                'day3_pct': round(day3_pct, 3) if day3_pct is not None else None,
            })
    print(f"\ntotal events with baseline + 3d data: {sum(1 for o in obs if o.get('day3_pct') is not None)}", file=sys.stderr)

    # Sweep z-score thresholds
    z_buckets = [
        ('z >= +3.0', lambda z: z >= 3.0, 'CALL'),
        ('z >= +2.0', lambda z: z >= 2.0, 'CALL'),
        ('z >= +1.5', lambda z: z >= 1.5, 'CALL'),
        ('z >= +1.0', lambda z: z >= 1.0, 'CALL'),
        ('z in [+0.5, +1.0]', lambda z: 0.5 <= z < 1.0, 'CALL'),
        ('|z| < 0.5', lambda z: abs(z) < 0.5, 'NEUTRAL'),
        ('z in [-1.0, -0.5]', lambda z: -1.0 < z <= -0.5, 'PUT'),
        ('z <= -1.0', lambda z: z <= -1.0, 'PUT'),
        ('z <= -1.5', lambda z: z <= -1.5, 'PUT'),
        ('z <= -2.0', lambda z: z <= -2.0, 'PUT'),
        ('z <= -3.0', lambda z: z <= -3.0, 'PUT'),
    ]

    def stats_for(group, side):
        sign = 1 if side == 'CALL' else -1 if side == 'PUT' else 0
        with_3d = [r for r in group if r.get('day3_pct') is not None]
        n = len(with_3d)
        if n == 0: return {'n_total': len(group), 'n_3d': 0}
        if side == 'NEUTRAL':
            wins_up = sum(1 for r in with_3d if r['day3_pct'] >= 0.5)
            wins_dn = sum(1 for r in with_3d if r['day3_pct'] <= -0.5)
            return {'n_total': len(group), 'n_3d': n,
                    'up_3d_win': round(wins_up / n * 100, 1),
                    'down_3d_win': round(wins_dn / n * 100, 1),
                    'avg_3d': round(statistics.mean(r['day3_pct'] for r in with_3d), 2)}
        wins = sum(1 for r in with_3d if (r['day3_pct'] * sign) >= 0.5)
        avg_aligned = statistics.mean(r['day3_pct'] * sign for r in with_3d)
        return {'n_total': len(group), 'n_3d': n,
                'aligned_win_pct': round(wins / n * 100, 1),
                'avg_aligned': round(avg_aligned, 2)}

    # Baseline (all events)
    all_3d = [o for o in obs if o.get('day3_pct') is not None]
    baseline_call = stats_for(all_3d, 'CALL')
    baseline_put  = stats_for(all_3d, 'PUT')

    sweep = []
    for label, predicate, side in z_buckets:
        group = [o for o in obs if predicate(o['z_score'])]
        s = stats_for(group, side)
        sweep.append({'bucket': label, 'side': side, **s})

    # Console
    print(f"\n{'='*108}")
    print(f"V4 EOD SURGE — Z-SCORE BACKTEST")
    print(f"Z-score = (today's last_30m_call_$ - ticker's median) / ticker's stdev")
    print(f"Baselines from {len(baseline_by_t)} tickers, computed across our 6 sessions (small window!)")
    print(f"{'='*108}\n")
    print(f"BASELINE (all events with 3d data, n={baseline_call['n_3d']}):")
    print(f"  call-side aligned 3d-win = {baseline_call.get('aligned_win_pct')}%   avg = {baseline_call.get('avg_aligned'):+.2f}%")
    print(f"  put-side  aligned 3d-win = {baseline_put.get('aligned_win_pct')}%   avg = {baseline_put.get('avg_aligned'):+.2f}%")

    print(f"\n{'bucket':<22} {'side':<8} {'n_total':>7} {'n_3d':>5} {'aligned_win%':>13} {'avg_aligned':>12} {'edge':>8}")
    print('-' * 95)
    for s in sweep:
        if s['n_3d'] == 0:
            print(f"{s['bucket']:<22} {s['side']:<8} {s['n_total']:>7} {s['n_3d']:>5}     —")
            continue
        side = s['side']
        if side == 'NEUTRAL':
            print(f"{s['bucket']:<22} {side:<8} {s['n_total']:>7} {s['n_3d']:>5}  "
                  f"up={s['up_3d_win']}% down={s['down_3d_win']}%  avg={s['avg_3d']:+.2f}%")
        else:
            base = baseline_call['aligned_win_pct'] if side == 'CALL' else baseline_put['aligned_win_pct']
            edge = s['aligned_win_pct'] - base if base is not None else 0
            print(f"{s['bucket']:<22} {side:<8} {s['n_total']:>7} {s['n_3d']:>5} "
                  f"{s['aligned_win_pct']:>11}% {s['avg_aligned']:>+10}% {edge:>+6.1f}pp")

    # Save
    out = {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'methodology': 'Per-ticker median + stdev of last_30m_net_call_$ across 6 sessions; events bucketed by z-score',
        'baseline_call': baseline_call,
        'baseline_put':  baseline_put,
        'baseline_by_ticker': baseline_by_t,
        'z_buckets': sweep,
        'all_obs': obs,
    }
    with open(os.path.join(HERE, 'v4_eod_surge_zscore.json'), 'w') as f:
        json.dump(out, f, indent=2, default=str)

    print(f"\n=== TOP 10 HIGHEST-Z (most unusual CALL surges) ===")
    print(f"{'tkr':<6} {'date':<11} {'$M':>8} {'baseline':>9} {'z':>6} {'3d_pct':>8}")
    high_z = sorted([o for o in obs if o.get('day3_pct') is not None], key=lambda o: -o['z_score'])[:10]
    for o in high_z:
        print(f"{o['ticker']:<6} {o['date']:<11} {o['call_m']:>+7.2f}M {o['baseline_m']:>+8.2f}M "
              f"{o['z_score']:>+5.2f} {o['day3_pct']:>+7.2f}%")

    print(f"\n=== TOP 10 LOWEST-Z (most unusual PUT surges) ===")
    low_z = sorted([o for o in obs if o.get('day3_pct') is not None], key=lambda o: o['z_score'])[:10]
    for o in low_z:
        print(f"{o['ticker']:<6} {o['date']:<11} {o['call_m']:>+7.2f}M {o['baseline_m']:>+8.2f}M "
              f"{o['z_score']:>+5.2f} {o['day3_pct']:>+7.2f}%")

    print(f"\nSaved → v4_eod_surge_zscore.json")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
