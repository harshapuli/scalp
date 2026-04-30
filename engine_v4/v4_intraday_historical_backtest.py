"""
V4 INTRADAY HISTORICAL BACKTEST — validate intraday CALL/PUT signals at scale.

Earlier analysis (snapshot period n=195) found:
  PUT at OPEN $1-3M:  79% 3d-down hit rate (n=24)
  PUT at OPEN $3-7M:  86% 3d-down hit rate (n=14)
  CALL at $15M+ anywhere: 67%+ 3d-up
  CALL $1-3M at OPEN: 0% (noise)

But the earlier action-label backtest (also small sample) showed PUT
at +19pp edge → with 60d data it INVERTED to -20pp. So we have to
test the intraday cells on the full 60d sample to know if THEY hold.

Requires: data/eod_flow_history_60d.json populated with intraday_max
fields (run v4_historical_flow_backfill.py first).

This script:
  1. For each (ticker, day) in history with intraday_max_call/put_m
     and close_px:
     a. Determine the time window (open/morning/midday/power)
     b. Determine the magnitude tier
  2. Compute forward 1d / 3d returns from close_px
  3. Stratify by [time × magnitude × side] cell — same as the snapshot
     period scanner — and report hit rate.
  4. Compare directly: same cells, big sample → does the edge persist?

Run: python3 v4_intraday_historical_backtest.py
Output: v4_intraday_historical_backtest.json
"""
import os, sys, json, statistics
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_THRESHOLD = 0.5


def time_window(t_hhmm):
    """Map HH:MM (UTC) to time-of-day window."""
    if not t_hhmm or len(t_hhmm) < 5: return 'unknown'
    try: hh = int(t_hhmm[:2])
    except: return 'unknown'
    if hh < 14: return 'open'         # 13-14 UTC = 6:30-7:30 PT
    if hh < 17: return 'morning'      # 14-17 = 7-10 PT
    if hh < 19: return 'midday'       # 17-19 = 10-12 PT
    return 'power'                    # 19-20 = 12-1 PT


def magnitude_tier(m):
    a = abs(m)
    if a < 1: return '<$1M'
    if a < 3: return '$1-3M'
    if a < 7: return '$3-7M'
    if a < 15: return '$7-15M'
    return '$15M+'


def main():
    hist = json.load(open(os.path.join(HERE, 'data', 'eod_flow_history_60d.json')))['history']
    picks = json.load(open(os.path.join(HERE, 'data', 'picks_77.json')))
    bucket_of = {}
    for k in ('mega', 'mid', 'small', 'indices'):
        for r in (picks.get('buckets') or {}).get(k, []) or []:
            bucket_of[r['ticker']] = k

    # Build (ticker → date → close_px) for forward returns
    closes = {tkr: {d: e['close_px'] for d, e in days.items() if e.get('close_px') is not None}
              for tkr, days in hist.items()}

    # Observations: only those with intraday_max fields populated
    obs = []
    n_no_intraday = 0
    for tkr, days in hist.items():
        if tkr not in bucket_of: continue
        sorted_days = sorted(days.keys())
        for i, d in enumerate(sorted_days):
            e = days[d]
            close = e.get('close_px')
            mc = e.get('intraday_max_call_m')
            mp = e.get('intraday_max_put_m')
            mct = e.get('intraday_max_call_t')
            mpt = e.get('intraday_max_put_t')
            if close is None: continue
            if mc is None or mp is None:
                n_no_intraday += 1
                continue

            # Forward returns
            future = sorted_days[i+1:i+8]
            fcloses = [(fd, closes[tkr].get(fd)) for fd in future if closes[tkr].get(fd) is not None]
            ret_1d = ret_3d = None
            if fcloses:
                ret_1d = (fcloses[0][1] - close) / close * 100
            if len(fcloses) >= 3:
                ret_3d = (fcloses[2][1] - close) / close * 100

            obs.append({
                'tkr': tkr, 'date': d, 'bucket': bucket_of[tkr],
                'max_call_m': mc, 'max_call_t': mct, 'max_call_window': time_window(mct),
                'max_call_tier': magnitude_tier(mc),
                'max_put_m': mp, 'max_put_t': mpt, 'max_put_window': time_window(mpt),
                'max_put_tier': magnitude_tier(mp),
                'ret_1d': round(ret_1d, 3) if ret_1d is not None else None,
                'ret_3d': round(ret_3d, 3) if ret_3d is not None else None,
            })

    n3 = sum(1 for o in obs if o.get('ret_3d') is not None)
    print(f'observations with intraday + close: {len(obs)}', file=sys.stderr)
    print(f'  with 3d forward: {n3}', file=sys.stderr)
    print(f'  ticker-days WITHOUT intraday data: {n_no_intraday}', file=sys.stderr)
    if len(obs) < 100:
        print(f'\n⚠ too few observations — backfill may not be complete yet.', file=sys.stderr)
        print(f'   Wait for v4_historical_flow_backfill.py to finish before running this.', file=sys.stderr)

    # Hit-rate helper
    def hit_rate(group, side):
        sign = 1 if side == 'CALL' else -1
        vals = [o['ret_3d'] for o in group if o.get('ret_3d') is not None]
        if not vals: return None, 0, None
        wins = sum(1 for v in vals if (v * sign) >= WIN_THRESHOLD)
        return round(wins / len(vals) * 100, 1), len(vals), round(statistics.mean(v * sign for v in vals), 2)

    # Universe baselines
    base_call_wr, _, base_call_avg = hit_rate(obs, 'CALL')
    base_put_wr, _, base_put_avg = hit_rate(obs, 'PUT')

    # CALL by time × magnitude
    call_results = {}
    for window in ('open', 'morning', 'midday', 'power'):
        for tier in ('$1-3M', '$3-7M', '$7-15M', '$15M+'):
            grp = [o for o in obs if o['max_call_window'] == window and o['max_call_tier'] == tier]
            wr, n, avg = hit_rate(grp, 'CALL')
            call_results[f'{window}_{tier}'] = {
                'window': window, 'tier': tier, 'n_total': len(grp), 'n_3d': n,
                'win_pct': wr, 'avg_aligned': avg,
                'edge_vs_baseline': round(wr - base_call_wr, 1) if (wr is not None and base_call_wr is not None) else None,
            }

    # PUT by time × magnitude
    put_results = {}
    for window in ('open', 'morning', 'midday', 'power'):
        for tier in ('$1-3M', '$3-7M', '$7-15M', '$15M+'):
            grp = [o for o in obs if o['max_put_window'] == window and o['max_put_tier'] == tier]
            wr, n, avg = hit_rate(grp, 'PUT')
            put_results[f'{window}_{tier}'] = {
                'window': window, 'tier': tier, 'n_total': len(grp), 'n_3d': n,
                'win_pct': wr, 'avg_aligned': avg,
                'edge_vs_baseline': round(wr - base_put_wr, 1) if (wr is not None and base_put_wr is not None) else None,
            }

    out = {
        'generated_utc':    datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'n_observations':   len(obs),
        'n_3d_forward':     n3,
        'n_no_intraday':    n_no_intraday,
        'baselines':        {'CALL': {'win_pct': base_call_wr, 'avg': base_call_avg},
                             'PUT':  {'win_pct': base_put_wr,  'avg': base_put_avg}},
        'call_by_window_tier': call_results,
        'put_by_window_tier':  put_results,
    }
    op = os.path.join(HERE, 'v4_intraday_historical_backtest.json')
    with open(op, 'w') as f:
        json.dump(out, f, indent=2, default=str)

    # Console
    print(f"\n{'='*100}")
    print(f"V4 INTRADAY HISTORICAL BACKTEST — does intraday-anywhere signal hold at scale?")
    print(f"n={len(obs)} obs · n_3d={n3} · win = ±{WIN_THRESHOLD}% in 3d")
    print(f"BASELINES — CALL win: {base_call_wr}% (avg {base_call_avg:+.2f}%) · PUT win: {base_put_wr}% (avg {base_put_avg:+.2f}%)")
    print(f"{'='*100}\n")

    # CALL table
    print(f"CALL — 3d hit rate by [time × magnitude]:")
    print(f"{'window':<10} {'$1-3M':<20} {'$3-7M':<20} {'$7-15M':<20} {'$15M+':<20}")
    for window in ('open', 'morning', 'midday', 'power'):
        row = f"{window:<10}"
        for tier in ('$1-3M', '$3-7M', '$7-15M', '$15M+'):
            r = call_results[f'{window}_{tier}']
            n = r.get('n_3d', 0)
            if n < 5:
                row += f"{'(n='+str(n)+')':<20}"; continue
            wr = r.get('win_pct')
            edge = r.get('edge_vs_baseline')
            cell = f"{wr}% n={n} ({edge:+.0f}pp)"
            row += f"{cell:<20}"
        print(row)

    print(f"\nPUT — 3d hit rate by [time × magnitude]:")
    print(f"{'window':<10} {'$1-3M':<20} {'$3-7M':<20} {'$7-15M':<20} {'$15M+':<20}")
    for window in ('open', 'morning', 'midday', 'power'):
        row = f"{window:<10}"
        for tier in ('$1-3M', '$3-7M', '$7-15M', '$15M+'):
            r = put_results[f'{window}_{tier}']
            n = r.get('n_3d', 0)
            if n < 5:
                row += f"{'(n='+str(n)+')':<20}"; continue
            wr = r.get('win_pct')
            edge = r.get('edge_vs_baseline')
            cell = f"{wr}% n={n} ({edge:+.0f}pp)"
            row += f"{cell:<20}"
        print(row)

    # Spotlight on the cells that worked at small scale
    print(f"\n=== SPOTLIGHT: cells that showed edge in snapshot period (n=195), validated? ===")
    spotlight = [
        ('PUT', 'open', '$1-3M', 79, 'snapshot 79% n=24 — best signal we found'),
        ('PUT', 'open', '$3-7M', 86, 'snapshot 86% n=14 — strongest in study'),
        ('PUT', 'morning', '$1-3M', 61, 'snapshot 61%'),
        ('PUT', 'midday', '$1-3M', 73, 'snapshot 73%'),
        ('PUT', 'power', '$1-3M', 80, 'snapshot 80% n=5'),
        ('CALL', 'power', '$1-3M', 80, 'snapshot 80% n=5'),
        ('CALL', 'morning', '$15M+', 67, 'snapshot 67% n=6'),
    ]
    for side, window, tier, snap_wr, note in spotlight:
        cell = call_results.get(f'{window}_{tier}') if side == 'CALL' else put_results.get(f'{window}_{tier}')
        n = cell.get('n_3d', 0)
        wr = cell.get('win_pct')
        edge = cell.get('edge_vs_baseline')
        if n < 5:
            print(f"  {side} {window} {tier:<8}  n={n} (insufficient)")
            continue
        delta = wr - snap_wr
        verdict = '★ HOLDS' if abs(delta) <= 10 and edge >= 5 else ('✗ FAILS' if (edge or 0) < -3 else '~ borderline')
        print(f"  {side} {window} {tier:<8}  60d: {wr}% n={n} edge={edge:+.0f}pp  vs snap {snap_wr}%  → {verdict}  ({note})")

    print(f"\nSaved → {op}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
