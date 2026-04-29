"""
V4 EOD BASELINES COMPUTE — per-ticker last-30-min flow baselines.

For each ticker in picks_77, compute from ALL accumulated snapshots:
  - median(last_30m_net_call_$)  — center
  - stdev                        — spread
  - n_days                       — sample
  - n_nonzero                    — days with non-trivial flow
  - quality                      — STRONG / USABLE / THIN

Writes data/eod_flow_baselines.json. Re-run daily after the snapshot
refreshes so baselines update as more sessions accumulate.

Quality classification (used by BucketView's predictor):
  STRONG (★★) — std > $1M and ≥4 non-zero days → use z-score
  USABLE (★)  — std > $0.3M and ≥3 non-zero days → use z-score
  THIN (○)    — fall back to absolute-$ thresholds (or skip prediction)

Run: python3 v4_eod_baselines_compute.py
Output: data/eod_flow_baselines.json
"""
import os, sys, json, glob, statistics
from datetime import datetime, timezone
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, 'data', 'eod_flow_baselines.json')
PICKS = os.path.join(HERE, 'data', 'picks_77.json')


def date_from_snap(p):
    return os.path.basename(p).replace('v4_snapshot_', '').replace('.json', '')


def main():
    snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    if not snap_paths:
        print('no snapshots', file=sys.stderr); return 1

    # Load picks (all 4 buckets)
    picks_data = json.load(open(PICKS)) if os.path.exists(PICKS) else {}
    all_picks = set()
    for k in ('mega', 'mid', 'small', 'indices'):
        for r in (picks_data.get('buckets') or {}).get(k, []) or []:
            all_picks.add(r['ticker'])

    # For each (ticker, snapshot), compute last_30m_net_call_$
    flow = defaultdict(list)  # ticker → [(date, call_m)]
    for p in snap_paths:
        d = date_from_snap(p)
        try:
            data = json.load(open(p)).get('data') or {}
        except: continue
        for tkr, blob in data.items():
            if all_picks and tkr not in all_picks: continue
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
            flow[tkr].append((d, wcall / 1e6))

    # Compute baseline + classify quality
    baselines = {}
    for tkr, rows in flow.items():
        vals = [v for _, v in rows]
        if not vals: continue
        median = statistics.median(vals)
        try: std = statistics.stdev(vals)
        except: std = 0
        # Floor std so z-score doesn't blow up on near-zero stocks
        std_floor = max(std, max(abs(median) * 0.5, 0.1))
        n_days = len(vals)
        n_nonzero = sum(1 for v in vals if abs(v) > 0.05)

        if std > 1.0 and n_nonzero >= 4:
            quality = 'STRONG'
        elif std > 0.3 and n_nonzero >= 3:
            quality = 'USABLE'
        else:
            quality = 'THIN'

        baselines[tkr] = {
            'median_m': round(median, 3),
            'std_m':    round(std, 3),
            'std_floor_m': round(std_floor, 3),
            'n_days':   n_days,
            'n_nonzero': n_nonzero,
            'quality':  quality,
        }

    # Aggregate quality counts per bucket
    bucket_quality = {}
    for k in ('mega', 'mid', 'small', 'indices'):
        cnt = {'STRONG': 0, 'USABLE': 0, 'THIN': 0, 'NO_DATA': 0}
        for r in (picks_data.get('buckets') or {}).get(k, []) or []:
            t = r['ticker']
            if t not in baselines:
                cnt['NO_DATA'] += 1
            else:
                cnt[baselines[t]['quality']] += 1
        bucket_quality[k] = cnt

    out = {
        'generated_utc':    datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'sessions_loaded':  len(snap_paths),
        'tickers_baselined': len(baselines),
        'quality_thresholds': {
            'STRONG': 'std > $1M AND n_nonzero >= 4',
            'USABLE': 'std > $0.3M AND n_nonzero >= 3',
            'THIN':   'falls back to absolute-$ thresholds',
        },
        'bucket_quality':   bucket_quality,
        'baselines':        baselines,
    }
    with open(OUT, 'w') as f:
        json.dump(out, f, indent=2, default=str)

    print(f"wrote {OUT}")
    print(f"  sessions: {len(snap_paths)}")
    print(f"  tickers baselined: {len(baselines)}")
    print(f"  bucket quality:")
    for k, c in bucket_quality.items():
        print(f"    {k:<8}  STRONG={c['STRONG']}  USABLE={c['USABLE']}  THIN={c['THIN']}  NO_DATA={c['NO_DATA']}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
