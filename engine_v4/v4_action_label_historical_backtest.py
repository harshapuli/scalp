"""
V4 ACTION LABEL HISTORICAL BACKTEST — replays the action_for logic
against the 60-day backfill (n≈3000 vs n=343 from snapshot-only window).

Replicates the EOD-surge-driven paths of actionFor() — the only paths
we can simulate historically (we don't have per-day patrol/conviction
state for 60 days back). Specifically:

  PATH A: z-score path
    z >= 2.0 + reg != EARN_WK   → BUY
    z >= 1.5 + reg != EARN_WK   → BUY
    z <= -0.5 to -1.5 + reg in (EARN_WK, EARN_MTH) → PUT
    z <= -1.5 + reg in (EARN_WK, EARN_MTH) → PUT

  PATH B: absolute-$ fallback
    EARN_WK PUT  -$0.5 to -$2M  → PUT
    EARN_MTH PUT -$0.5 to -$2M  → PUT
    EARN_MTH CALL +$0.5 to +$3M → BUY
    NO_EARN CALL +$1 to +$4M    → BUY

  Else: HOLD

This backtest measures:
  - n_BUY, n_PUT, n_HOLD per regime/bucket
  - 1d/3d hit rate per (action, bucket, regime)
  - Whether composite > universe baseline

Run: python3 v4_action_label_historical_backtest.py
Output: v4_action_label_historical_backtest.json
"""
import os, sys, json, statistics
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_THRESHOLD = 0.5


def regime_for(earnings_str, session_iso):
    if not earnings_str: return None
    try:
        e = datetime.strptime(earnings_str, '%Y-%m-%d').date()
        s = datetime.strptime(session_iso, '%Y-%m-%d').date()
        days = abs((e - s).days)
        if days <= 7: return 'EARN_WK'
        if days <= 30: return 'EARN_MTH'
        return 'NO_EARN'
    except: return None


def historical_action(call_m, z, baseline_quality, regime):
    """Replicates actionFor()'s EOD-surge paths. Returns BUY / PUT / HOLD."""
    if call_m is None: return 'HOLD'

    # PATH A: z-score-based
    if z is not None and baseline_quality in ('STRONG', 'USABLE'):
        if z >= 2.0 and regime != 'EARN_WK': return 'BUY'
        if z >= 1.5 and regime != 'EARN_WK': return 'BUY'
        if z <= -0.5 and z > -1.5 and regime in ('EARN_WK', 'EARN_MTH'): return 'PUT'
        if z <= -1.5 and regime in ('EARN_WK', 'EARN_MTH'): return 'PUT'

    # PATH B: absolute-$ fallback
    if regime == 'EARN_WK' and -2.0 <= call_m <= -0.5: return 'PUT'
    if regime == 'EARN_MTH' and -2.0 <= call_m <= -0.5: return 'PUT'
    if regime == 'EARN_MTH' and 0.5 <= call_m <= 3.0: return 'BUY'
    if regime == 'NO_EARN' and 1.0 <= call_m <= 4.0: return 'BUY'

    return 'HOLD'


def main():
    hist = json.load(open(os.path.join(HERE, 'data', 'eod_flow_history_60d.json')))['history']
    baselines = json.load(open(os.path.join(HERE, 'data', 'eod_flow_baselines.json')))['baselines']
    picks_data = json.load(open(os.path.join(HERE, 'data', 'picks_77.json')))
    bucket_of = {}
    earnings_of = {}
    for k in ('mega', 'mid', 'small', 'indices'):
        for r in (picks_data.get('buckets') or {}).get(k, []) or []:
            bucket_of[r['ticker']] = k
            earnings_of[r['ticker']] = r.get('next_earnings_date')

    # Build (ticker → date → close) index
    closes = {tkr: {d: e['close_px'] for d, e in days.items() if e.get('close_px') is not None}
              for tkr, days in hist.items()}

    # For each (ticker, day), simulate the action label and compute forward returns
    obs = []
    for tkr, days in hist.items():
        if tkr not in bucket_of: continue
        b = baselines.get(tkr) or {}
        bucket = bucket_of[tkr]
        sorted_days = sorted(days.keys())
        for i, d in enumerate(sorted_days):
            e = days[d]
            cm = e.get('call_m')
            close = e.get('close_px')
            if cm is None or close is None: continue

            z = None
            if b.get('std_floor_m', 0) > 0:
                z = (cm - b['median_m']) / b['std_floor_m']
            reg = regime_for(earnings_of.get(tkr), d)
            action = historical_action(cm, z, b.get('quality'), reg)

            # Forward returns from closes index
            ret_1d = ret_3d = None
            future = sorted_days[i+1:i+8]
            future_closes = [(fd, closes[tkr].get(fd)) for fd in future if closes[tkr].get(fd) is not None]
            if future_closes:
                ret_1d = (future_closes[0][1] - close) / close * 100
            if len(future_closes) >= 3:
                ret_3d = (future_closes[2][1] - close) / close * 100

            obs.append({
                'tkr': tkr, 'date': d, 'bucket': bucket,
                'action': action, 'call_m': round(cm, 3),
                'z': round(z, 3) if z is not None else None,
                'regime': reg,
                'ret_1d': round(ret_1d, 3) if ret_1d is not None else None,
                'ret_3d': round(ret_3d, 3) if ret_3d is not None else None,
            })

    print(f'observations: {len(obs)}', file=sys.stderr)
    n_3d = sum(1 for o in obs if o.get('ret_3d') is not None)
    print(f'  with 3d forward: {n_3d}', file=sys.stderr)

    # Aggregate
    def stats_for(group, side):
        sign = 1 if side == 'BUY' else -1 if side == 'PUT' else 0
        out = {'n_total': len(group)}
        for hz, key in (('1d', 'ret_1d'), ('3d', 'ret_3d')):
            vals = [r[key] for r in group if r.get(key) is not None]
            if not vals:
                out[f'{hz}_n'] = 0
                continue
            if side == 'HOLD':
                # For HOLD, just track the distribution
                up = sum(1 for v in vals if v >= WIN_THRESHOLD)
                dn = sum(1 for v in vals if v <= -WIN_THRESHOLD)
                out[f'{hz}_n'] = len(vals)
                out[f'{hz}_up_pct'] = round(up / len(vals) * 100, 1)
                out[f'{hz}_dn_pct'] = round(dn / len(vals) * 100, 1)
                out[f'{hz}_avg'] = round(statistics.mean(vals), 2)
            else:
                wins = sum(1 for v in vals if (v * sign) >= WIN_THRESHOLD)
                out[f'{hz}_n'] = len(vals)
                out[f'{hz}_aligned_win_pct'] = round(wins / len(vals) * 100, 1)
                out[f'{hz}_aligned_avg'] = round(statistics.mean(v * sign for v in vals), 2)
        return out

    universe_buy = stats_for(obs, 'BUY')
    universe_put = stats_for(obs, 'PUT')

    # By action label
    by_action = defaultdict(list)
    for o in obs: by_action[o['action']].append(o)
    action_summary = {action: stats_for(grp, action) for action, grp in by_action.items()}

    # By bucket × action
    bucket_action = defaultdict(lambda: defaultdict(list))
    for o in obs: bucket_action[o['bucket']][o['action']].append(o)
    bucket_summary = {}
    for bk, ad in bucket_action.items():
        bucket_summary[bk] = {action: stats_for(grp, action) for action, grp in ad.items()}
        bucket_summary[bk]['_baselines'] = {
            'BUY': stats_for([o for o in obs if o['bucket'] == bk], 'BUY'),
            'PUT': stats_for([o for o in obs if o['bucket'] == bk], 'PUT'),
        }

    out = {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'win_threshold_pct': WIN_THRESHOLD,
        'date_range': [min(o['date'] for o in obs), max(o['date'] for o in obs)],
        'n_observations': len(obs),
        'n_with_3d': n_3d,
        'universe_baselines': {'BUY': universe_buy, 'PUT': universe_put},
        'by_action': action_summary,
        'by_bucket_action': bucket_summary,
    }
    with open(os.path.join(HERE, 'v4_action_label_historical_backtest.json'), 'w') as f:
        json.dump(out, f, indent=2, default=str)

    # Console
    print(f"\n{'='*108}")
    print(f"V4 ACTION LABEL — HISTORICAL BACKTEST (60d, n={len(obs)})")
    print(f"Replays the EOD-surge paths of actionFor() vs 60-day forward returns")
    print(f"{'='*108}\n")
    base_buy = universe_buy.get('3d_aligned_win_pct')
    base_put = universe_put.get('3d_aligned_win_pct')
    print(f"UNIVERSE BASELINE (treating ALL ticker-days as if they were that side):")
    print(f"  BUY-side 3d hit: {base_buy}%   avg aligned: {universe_buy.get('3d_aligned_avg'):+.2f}%   n={universe_buy.get('3d_n')}")
    print(f"  PUT-side 3d hit: {base_put}%   avg aligned: {universe_put.get('3d_aligned_avg'):+.2f}%   n={universe_put.get('3d_n')}")

    print(f"\nPER-ACTION RESULTS:")
    print(f"{'action':<10} {'n':>5} | {'1d_n':>5} {'1d_win%':>9} {'1d_avg':>8} | {'3d_n':>5} {'3d_win%':>9} {'3d_avg':>8} {'3d_edge':>9}")
    print('-' * 100)
    for action in ('BUY', 'PUT', 'HOLD'):
        s = action_summary.get(action) or {}
        if action == 'HOLD':
            for hz in ('1d', '3d'):
                pass
            print(f"{action:<10} {s.get('n_total', 0):>5} | "
                  f"up={s.get('1d_up_pct')}% dn={s.get('1d_dn_pct')}% avg={s.get('1d_avg')}% | "
                  f"up={s.get('3d_up_pct')}% dn={s.get('3d_dn_pct')}% avg={s.get('3d_avg')}%")
            continue
        side_base = base_buy if action == 'BUY' else base_put
        wr_3d = s.get('3d_aligned_win_pct')
        edge = (wr_3d - side_base) if (wr_3d is not None and side_base is not None) else None
        print(f"{action:<10} {s.get('n_total', 0):>5} | "
              f"{s.get('1d_n'):>5} {s.get('1d_aligned_win_pct')}% {s.get('1d_aligned_avg'):+.2f}% | "
              f"{s.get('3d_n'):>5} {wr_3d}% {s.get('3d_aligned_avg'):+.2f}% "
              f"{f'{edge:+.1f}pp' if edge is not None else '—':>9}")

    print(f"\nPER-BUCKET (3d horizon, action vs same-bucket baseline):")
    for bk in ('mega', 'mid', 'small', 'indices'):
        bsd = bucket_summary.get(bk)
        if not bsd: continue
        bk_buy_base = bsd['_baselines']['BUY'].get('3d_aligned_win_pct')
        bk_put_base = bsd['_baselines']['PUT'].get('3d_aligned_win_pct')
        n_bk = sum(s['n_total'] for k, s in bsd.items() if k != '_baselines')
        print(f"\n  {bk:<8}  n_total={n_bk}  baseline BUY={bk_buy_base}% PUT={bk_put_base}%")
        for action in ('BUY', 'PUT'):
            s = bsd.get(action) or {}
            n3 = s.get('3d_n', 0)
            if n3 < 5: continue
            wr = s.get('3d_aligned_win_pct')
            avg = s.get('3d_aligned_avg')
            base = bk_buy_base if action == 'BUY' else bk_put_base
            edge = (wr - base) if (wr is not None and base is not None) else None
            print(f"    {action} n={s.get('n_total')}  3d_n={n3}  win={wr}%  avg={avg:+.2f}%  edge={edge:+.1f}pp" if edge is not None else
                  f"    {action} n={s.get('n_total')}  3d_n={n3}  win={wr}%")

    print(f"\nSaved → v4_action_label_historical_backtest.json")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
