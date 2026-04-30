"""
V4 INTRADAY SURGE — TIME-STRATIFIED BACKTEST

User correction: CALL surges DO happen at the open too — INTC today had
$31M CALL at 14:09 UTC (7:09 AM PT) → +12% close. Don't dismiss them.

This script slides 30-min window across each session, finds max CALL
AND max PUT windows, then bins by:
  - TIME OF DAY (open hour 13-14, midday 14-18, power 19-20)
  - MAGNITUDE tier ($1-3M, $3-7M, $7M+)
  - BUCKET (mega/mid/small/indices)
  - SIDE (CALL/PUT)

Reports forward 3d hit rate per cell. Identifies actually-predictive
combos vs noise.

Run: python3 v4_intraday_surge_time_backtest.py
"""
import os, sys, json, glob, statistics
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_THRESHOLD = 0.5
WINDOW_MIN = 30


def date_from(p, prefix='v4_snapshot_'):
    return os.path.basename(p).replace(prefix, '').replace('.json', '')


def best_call_put_windows(ticks):
    """Return (max_call_m, max_call_t, max_put_m, max_put_t)"""
    if not isinstance(ticks, list) or len(ticks) < WINDOW_MIN:
        return None, None, None, None
    sorted_ticks = sorted(ticks, key=lambda t: (t.get('tape_time') or t.get('t') or ''))
    best_call = best_put = 0.0
    best_call_t = best_put_t = None
    for i in range(len(sorted_ticks) - WINDOW_MIN + 1):
        s = 0.0
        for t in sorted_ticks[i:i + WINDOW_MIN]:
            try: s += float(t.get('net_call_premium', 0) or 0)
            except: pass
        t_iso = sorted_ticks[i].get('tape_time') or sorted_ticks[i].get('t')
        if s > best_call:
            best_call = s; best_call_t = t_iso
        if s < best_put:
            best_put = s; best_put_t = t_iso
    return best_call / 1e6, best_call_t, best_put / 1e6, best_put_t


def time_window_label(t_iso):
    if not t_iso or len(t_iso) < 16: return 'unknown'
    hh = int(t_iso[11:13])
    if hh < 14:    return 'open (13-14 UTC)'      # 6:30-7:30 AM PT
    if hh < 17:    return 'morning (14-17)'       # 7-10 AM PT
    if hh < 19:    return 'midday (17-19)'        # 10-12 PT
    return 'power hour (19-20)'                   # 12-1 PT


def magnitude_tier(m):
    a = abs(m)
    if a < 1: return '< $1M'
    if a < 3: return '$1-3M'
    if a < 7: return '$3-7M'
    if a < 15: return '$7-15M'
    return '$15M+'


def main():
    snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    snaps = {}
    for p in snap_paths:
        d = date_from(p)
        try: snaps[d] = json.load(open(p)).get('data') or {}
        except: pass
    dates = sorted(snaps.keys())
    print(f'snapshots: {dates}', file=sys.stderr)

    # picks + bucket
    pd = json.load(open(os.path.join(HERE, 'data', 'picks_77.json')))
    bucket_of = {}
    for k in ('mega', 'mid', 'small', 'indices'):
        for r in (pd.get('buckets') or {}).get(k, []) or []:
            bucket_of[r['ticker']] = k

    # Build observations
    obs = []
    for i, d in enumerate(dates):
        for tkr, blob in snaps[d].items():
            if tkr not in bucket_of: continue
            npt = blob.get('net_prem_ticks')
            if isinstance(npt, dict): npt = npt.get('data') or []
            mc, mct, mp, mpt = best_call_put_windows(npt)
            if mc is None: continue

            # Forward 3d return
            close_px = ((blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
            ret_3d = None
            if i + 3 < len(dates) and close_px:
                fblob = snaps[dates[i + 3]].get(tkr) or {}
                fc = ((fblob.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
                if fc and float(close_px) > 0:
                    try: ret_3d = (float(fc) - float(close_px)) / float(close_px) * 100
                    except: pass

            obs.append({
                'tkr': tkr, 'date': d, 'bucket': bucket_of[tkr],
                'max_call_m': round(mc, 3), 'max_call_t': mct,
                'max_call_window': time_window_label(mct),
                'max_call_mag_tier': magnitude_tier(mc),
                'max_put_m': round(mp, 3), 'max_put_t': mpt,
                'max_put_window': time_window_label(mpt),
                'max_put_mag_tier': magnitude_tier(mp),
                'ret_3d': round(ret_3d, 3) if ret_3d is not None else None,
            })

    n3 = sum(1 for o in obs if o.get('ret_3d') is not None)
    print(f'observations: {len(obs)}, with 3d forward: {n3}', file=sys.stderr)

    # Stats helpers
    def hit_rate(group, side='CALL'):
        sign = 1 if side == 'CALL' else -1
        vals = [o['ret_3d'] for o in group if o.get('ret_3d') is not None]
        if not vals: return None, 0
        wins = sum(1 for v in vals if (v * sign) >= WIN_THRESHOLD)
        return round(wins / len(vals) * 100, 1), len(vals)

    # CALL side: cross by time × magnitude
    print(f"\n{'='*108}")
    print(f"CALL SIDE — 3d hit rate by [time of day] × [magnitude tier]")
    print(f"{'='*108}")
    print(f"\n{'window':<22} {'$1-3M':<14} {'$3-7M':<14} {'$7-15M':<14} {'$15M+':<14}")
    for window in ['open (13-14 UTC)', 'morning (14-17)', 'midday (17-19)', 'power hour (19-20)']:
        row = f"{window:<22}"
        for tier in ['$1-3M', '$3-7M', '$7-15M', '$15M+']:
            grp = [o for o in obs if o['max_call_window'] == window and o['max_call_mag_tier'] == tier]
            wr, n = hit_rate(grp, 'CALL')
            cell = f"{wr}% (n={n})" if wr is not None else f"(n={n})"
            row += f"{cell:<14}"
        print(row)

    print(f"\nPUT SIDE — 3d hit rate by [time] × [magnitude]")
    print(f"\n{'window':<22} {'$1-3M':<14} {'$3-7M':<14} {'$7-15M':<14} {'$15M+':<14}")
    for window in ['open (13-14 UTC)', 'morning (14-17)', 'midday (17-19)', 'power hour (19-20)']:
        row = f"{window:<22}"
        for tier in ['$1-3M', '$3-7M', '$7-15M', '$15M+']:
            grp = [o for o in obs if o['max_put_window'] == window and o['max_put_mag_tier'] == tier]
            wr, n = hit_rate(grp, 'PUT')
            cell = f"{wr}% (n={n})" if wr is not None else f"(n={n})"
            row += f"{cell:<14}"
        print(row)

    # Bucket-aware: where do CALLs/PUTs work per bucket?
    print(f"\n{'='*108}")
    print(f"BY BUCKET — 3d hit rate at [time × magnitude] cross")
    print(f"{'='*108}")
    for bucket in ('mega', 'mid', 'small', 'indices'):
        bk_obs = [o for o in obs if o['bucket'] == bucket]
        if not bk_obs: continue
        print(f"\n--- {bucket.upper()} (n={len(bk_obs)}) ---")
        # CALL by window × tier (just $1-3M and $7-15M to keep table small)
        print(f"  CALL  {'$1-3M':<12} {'$3-7M':<12} {'$7-15M':<12} {'$15M+':<12}")
        for window in ['open (13-14 UTC)', 'morning (14-17)', 'midday (17-19)', 'power hour (19-20)']:
            row = f"  {window[:14]:<14}"
            for tier in ['$1-3M', '$3-7M', '$7-15M', '$15M+']:
                grp = [o for o in bk_obs if o['max_call_window'] == window and o['max_call_mag_tier'] == tier]
                wr, n = hit_rate(grp, 'CALL')
                cell = f"{wr}%(n{n})" if wr is not None else f"(n{n})"
                row += f"{cell:<12}"
            print(row)
        print(f"  PUT   {'$1-3M':<12} {'$3-7M':<12} {'$7-15M':<12} {'$15M+':<12}")
        for window in ['open (13-14 UTC)', 'morning (14-17)', 'midday (17-19)', 'power hour (19-20)']:
            row = f"  {window[:14]:<14}"
            for tier in ['$1-3M', '$3-7M', '$7-15M', '$15M+']:
                grp = [o for o in bk_obs if o['max_put_window'] == window and o['max_put_mag_tier'] == tier]
                wr, n = hit_rate(grp, 'PUT')
                cell = f"{wr}%(n{n})" if wr is not None else f"(n{n})"
                row += f"{cell:<12}"
            print(row)

    # Save
    with open(os.path.join(HERE, 'v4_intraday_surge_time_backtest.json'), 'w') as f:
        json.dump({'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                   'n_observations': len(obs), 'n_3d': n3,
                   'all_obs': obs}, f, indent=2, default=str)
    print(f"\nSaved → v4_intraday_surge_time_backtest.json")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
