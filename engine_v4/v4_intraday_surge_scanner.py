"""
V4 INTRADAY SURGE SCANNER — backtest "find the biggest surge anywhere in
the day", not just last 30 min.

User insight: "why are we not tracking every minute surges?"

The video taught last-30-min scanning. But our backfill data has per-minute
flow for the WHOLE 6.5-hour session (390 ticks). Surges happen at any time:
  - Open hour (institutions positioning)
  - Lunch reversal
  - Mid-afternoon momentum
  - Power hour (last 30 min — what we already track)

This script slides a 30-min rolling window across each session, finds the
moment of MAX |net_call_premium|, and uses that as the signal. Tests
whether intraday max surge predicts forward 1d / 3d returns better
than the fixed last-30-min window.

Run: python3 v4_intraday_surge_scanner.py
Output: v4_intraday_surge_scanner.json
"""
import os, sys, json, glob, statistics
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_THRESHOLD = 0.5
WINDOW_MIN = 30  # rolling window size in minutes


def date_from(p, prefix='v4_snapshot_'):
    return os.path.basename(p).replace(prefix, '').replace('.json', '')


def load_picks_with_earnings():
    p = os.path.join(HERE, 'data', 'picks_77.json')
    d = json.load(open(p))
    out = {}
    for k in ('mega', 'mid', 'small', 'indices'):
        for r in (d.get('buckets') or {}).get(k, []) or []:
            out[r['ticker']] = {**r, 'bucket': k}
    return out


def best_window_surge(ticks):
    """Find the 30-min window with max |sum(net_call_premium)|.
    Returns (max_call_$M, window_start_time, kind) where kind ∈ {CALL, PUT}.
    """
    if not isinstance(ticks, list) or len(ticks) < WINDOW_MIN:
        return None, None, None
    # Sort by tape_time
    sorted_ticks = sorted(ticks, key=lambda t: (t.get('tape_time') or t.get('t') or ''))
    best_call = best_put = 0.0
    best_call_t = best_put_t = None
    # Sliding window
    for i in range(len(sorted_ticks) - WINDOW_MIN + 1):
        window = sorted_ticks[i:i + WINDOW_MIN]
        s = 0.0
        for t in window:
            try: s += float(t.get('net_call_premium', 0) or 0)
            except: pass
        if s > best_call:
            best_call = s
            best_call_t = window[0].get('tape_time') or window[0].get('t')
        if s < best_put:
            best_put = s
            best_put_t = window[0].get('tape_time') or window[0].get('t')
    # Return the bigger one (in absolute value)
    if abs(best_call) >= abs(best_put):
        return best_call / 1e6, best_call_t, 'CALL'
    return best_put / 1e6, best_put_t, 'PUT'


def main():
    snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    picks = load_picks_with_earnings()
    print(f'tickers in picks: {len(picks)}', file=sys.stderr)
    print(f'snapshots: {len(snap_paths)}', file=sys.stderr)

    # Load snapshots — these have full per-minute tick data per ticker
    snaps = {}
    for p in snap_paths:
        d = date_from(p)
        try: snaps[d] = json.load(open(p)).get('data') or {}
        except: pass
    dates = sorted(snaps.keys())

    # For each (ticker, day):
    #   intraday_max_surge: max |30-min sum(net_call_$)| across whole session
    #   eod_surge: same but only in 19:30-20:00 UTC window
    # Then compare predictive power.
    obs = []
    for i, d in enumerate(dates):
        for tkr, blob in snaps[d].items():
            if tkr not in picks: continue
            npt = blob.get('net_prem_ticks')
            if isinstance(npt, dict): npt = npt.get('data') or []
            if not isinstance(npt, list) or not npt: continue

            # Best window (anywhere in session)
            intraday_m, intraday_t, intraday_side = best_window_surge(npt)
            # End-of-day window
            eod_call = 0.0
            for t in npt:
                hh_mm = (t.get('tape_time') or t.get('t') or '')[11:16]
                if '19:30' <= hh_mm <= '20:00':
                    try: eod_call += float(t.get('net_call_premium', 0) or 0)
                    except: pass
            eod_m = eod_call / 1e6

            # Forward returns
            close_px = ((blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
            ret_3d = None
            if i + 3 < len(dates) and close_px:
                fblob = snaps[dates[i + 3]].get(tkr) or {}
                fc = ((fblob.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
                if fc and float(close_px) > 0:
                    try: ret_3d = (float(fc) - float(close_px)) / float(close_px) * 100
                    except: pass

            obs.append({
                'tkr': tkr, 'date': d, 'bucket': picks[tkr]['bucket'],
                'intraday_max_m': round(intraday_m, 3) if intraday_m is not None else None,
                'intraday_max_time': intraday_t,
                'intraday_max_side': intraday_side,
                'eod_call_m': round(eod_m, 3),
                'ret_3d': round(ret_3d, 3) if ret_3d is not None else None,
            })

    print(f'observations: {len(obs)}', file=sys.stderr)
    n3 = sum(1 for o in obs if o.get('ret_3d') is not None)
    print(f'  with 3d forward: {n3}', file=sys.stderr)

    # ──── Compare intraday-max vs eod-window ────
    def stats_for(group, side):
        sign = 1 if side == 'CALL' else -1
        vals = [r['ret_3d'] for r in group if r.get('ret_3d') is not None]
        if not vals: return {'n': 0}
        wins = sum(1 for v in vals if (v * sign) >= WIN_THRESHOLD)
        return {'n': len(vals),
                'win_pct': round(wins / len(vals) * 100, 1),
                'avg_aligned': round(statistics.mean(v * sign for v in vals), 2)}

    # Bucket by tier in each metric
    # Intraday max: stratified by absolute magnitude
    intraday_strat = {}
    intraday_call = [o for o in obs if o.get('intraday_max_side') == 'CALL']
    intraday_put = [o for o in obs if o.get('intraday_max_side') == 'PUT']
    for thresh in (1, 2, 3, 5, 7.5, 10, 15):
        call_grp = [o for o in intraday_call if o['intraday_max_m'] >= thresh]
        put_grp = [o for o in intraday_put if o['intraday_max_m'] <= -thresh]
        intraday_strat[f'CALL ≥+${thresh}M'] = stats_for(call_grp, 'CALL')
        intraday_strat[f'PUT ≤-${thresh}M'] = stats_for(put_grp, 'PUT')

    # EOD comparison
    eod_strat = {}
    for thresh in (1, 2, 3, 5, 7.5, 10):
        call_grp = [o for o in obs if o['eod_call_m'] >= thresh]
        put_grp = [o for o in obs if o['eod_call_m'] <= -thresh]
        eod_strat[f'CALL ≥+${thresh}M'] = stats_for(call_grp, 'CALL')
        eod_strat[f'PUT ≤-${thresh}M'] = stats_for(put_grp, 'PUT')

    # Distribution of intraday max times — when do surges typically peak?
    from collections import Counter
    time_hist = Counter()
    for o in obs:
        t = o.get('intraday_max_time') or ''
        if len(t) >= 16:
            hh = t[11:13]
            time_hist[hh] += 1

    out = {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'n_observations': len(obs),
        'n_3d_forward': n3,
        'intraday_strat': intraday_strat,
        'eod_strat': eod_strat,
        'time_histogram': dict(time_hist),
        'all_obs': obs,
    }
    op = os.path.join(HERE, 'v4_intraday_surge_scanner.json')
    with open(op, 'w') as f:
        json.dump(out, f, indent=2, default=str)

    print(f"\n{'='*108}")
    print(f"V4 INTRADAY SURGE SCANNER — anywhere-in-day surge vs end-of-day-only")
    print(f"{len(obs)} obs · {n3} with 3d forward · win = ±{WIN_THRESHOLD}% in 3d")
    print(f"{'='*108}\n")

    print(f"WHEN do max surges happen? (UTC hour, 13=open, 19=power hour, 20=close)")
    for hh in sorted(time_hist.keys()):
        bar = '█' * (time_hist[hh] // 5)
        print(f"  {hh}:00  ({time_hist[hh]:>3})  {bar}")

    print(f"\n--- INTRADAY MAX SURGE (any window in day) ---")
    print(f"{'cell':<22} {'n':>4} {'win%':>7} {'avg':>7}")
    for cell, s in intraday_strat.items():
        if s.get('n', 0) < 5: continue
        print(f"  {cell:<20} {s.get('n'):>4} {s.get('win_pct')}% {s.get('avg_aligned'):+.2f}%")

    print(f"\n--- EOD SURGE (last 30 min only — current chip) ---")
    print(f"{'cell':<22} {'n':>4} {'win%':>7} {'avg':>7}")
    for cell, s in eod_strat.items():
        if s.get('n', 0) < 5: continue
        print(f"  {cell:<20} {s.get('n'):>4} {s.get('win_pct')}% {s.get('avg_aligned'):+.2f}%")

    print(f"\n=== SHOULD WE SWITCH TO INTRADAY-MAX? ===")
    print(f"Compare same-tier cells. If intraday-max consistently shows better")
    print(f"win rate AND larger sample at the same threshold → switch.")
    print(f"\nSaved → {op}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
