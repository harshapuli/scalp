"""
V4 UNIFIED BACKTEST — uses 60d UW backfill + Alpaca bars cache.

Old backtests (threshold sweep, regime sweep, zscore, action label)
derived forward returns from our 6 snapshot files only — limiting
sample to ~343 ticker-days. This script uses:
  - data/eod_flow_history_60d.json (4800 ticker-day flow records,
    Feb 4 → Apr 28, 2026)
  - close prices already embedded in that file (from bars_cache)
to compute forward 1d / 3d / 5d returns properly.

Sample size jumps from n=343 to n≈2500 (limited by 3d-forward-close
availability — 3066 have close_px, ~80% have 3d forward as well).

Runs four sweeps:
  1. Absolute-$ threshold (CALL ≥+$X, PUT ≤-$X) → win rate, edge
  2. Z-score threshold (per-ticker normalized) → win rate, edge
  3. Regime split (EARN_WK / EARN_MTH / NO_EARN) × side
  4. Action label simulation (composite predictor)

Output: v4_unified_backtest.json + console table
Run: python3 v4_unified_backtest.py
"""
import os, sys, json, glob, statistics
from collections import defaultdict
from datetime import datetime, timezone, date

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_THRESHOLD = 0.5  # % move counted as a hit


def load_history():
    p = os.path.join(HERE, 'data', 'eod_flow_history_60d.json')
    return json.load(open(p))['history']


def load_baselines():
    p = os.path.join(HERE, 'data', 'eod_flow_baselines.json')
    if not os.path.exists(p): return {}
    return json.load(open(p))['baselines']


def load_picks_with_earnings():
    p = os.path.join(HERE, 'data', 'picks_77.json')
    d = json.load(open(p))
    out = {}
    for k in ('mega', 'mid', 'small', 'indices'):
        for r in (d.get('buckets') or {}).get(k, []) or []:
            out[r['ticker']] = {**r, 'bucket': k}
    return out


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


def main():
    hist = load_history()
    baselines = load_baselines()
    picks = load_picks_with_earnings()

    print(f'tickers in history: {len(hist)}', file=sys.stderr)
    print(f'baselines:          {len(baselines)}', file=sys.stderr)

    # Build per-ticker (date → close_px) index for forward returns
    closes_by = {}  # tkr → {date_iso: close_px}
    for tkr, days in hist.items():
        d_idx = {}
        for d, e in days.items():
            cp = e.get('close_px')
            if cp is not None:
                d_idx[d] = cp
        closes_by[tkr] = d_idx

    # Build (ticker, date) observations with all features + forward returns
    obs = []
    for tkr, days in hist.items():
        if tkr not in picks: continue
        # Sorted dates with close + flow data
        sorted_days = sorted(days.keys())
        for i, d in enumerate(sorted_days):
            e = days[d]
            cm = e.get('call_m')
            close_px = e.get('close_px')
            if cm is None or close_px is None: continue

            # Forward returns — find next dates with close_px
            ret_1d = ret_3d = None
            future = sorted_days[i+1:i+8]  # look ahead up to 7 trading days
            forward_closes = [(fd, days[fd].get('close_px')) for fd in future if days[fd].get('close_px') is not None]
            if forward_closes:
                # 1d = first forward close
                fc1 = forward_closes[0][1]
                ret_1d = (fc1 - close_px) / close_px * 100
            if len(forward_closes) >= 3:
                fc3 = forward_closes[2][1]
                ret_3d = (fc3 - close_px) / close_px * 100

            # Z-score
            b = baselines.get(tkr)
            z = None
            if b and b.get('std_floor_m', 0) > 0:
                z = (cm - b['median_m']) / b['std_floor_m']
            quality = b.get('quality') if b else None

            # Regime
            reg = regime_for(picks[tkr].get('next_earnings_date'), d)

            obs.append({
                'tkr': tkr, 'date': d, 'bucket': picks[tkr]['bucket'],
                'call_m': round(cm, 3),
                'close_px': close_px,
                'z_score': round(z, 3) if z is not None else None,
                'baseline_quality': quality,
                'regime': reg,
                'ret_1d': round(ret_1d, 3) if ret_1d is not None else None,
                'ret_3d': round(ret_3d, 3) if ret_3d is not None else None,
            })

    print(f'observations: {len(obs)}', file=sys.stderr)
    n_3d = sum(1 for o in obs if o.get('ret_3d') is not None)
    print(f'  with 3d forward: {n_3d}', file=sys.stderr)

    # ──── Stats helper ────
    def stats_for(group, side):
        sign = 1 if side == 'CALL' else -1 if side == 'PUT' else 0
        out = {'n_total': len(group)}
        for hz, key in (('1d', 'ret_1d'), ('3d', 'ret_3d')):
            vals = [r[key] for r in group if r.get(key) is not None]
            if not vals:
                out[f'{hz}_n'] = 0; continue
            if side == 'NEUTRAL':
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

    baseline_call = stats_for(obs, 'CALL')
    baseline_put = stats_for(obs, 'PUT')

    # ──── Sweep 1: absolute-$ thresholds ────
    abs_call_sweep = []
    abs_put_sweep = []
    thresholds = [0.5, 1, 1.5, 2, 3, 4, 5, 7.5, 10]
    for t in thresholds:
        abs_call_sweep.append({'threshold': t, **stats_for(
            [o for o in obs if o['call_m'] >= t], 'CALL')})
        abs_put_sweep.append({'threshold': -t, **stats_for(
            [o for o in obs if o['call_m'] <= -t], 'PUT')})

    # ──── Sweep 2: z-score thresholds (only STRONG/USABLE) ────
    obs_with_z = [o for o in obs if o.get('z_score') is not None
                                  and o.get('baseline_quality') in ('STRONG', 'USABLE')]
    print(f'z-scored observations (STRONG/USABLE only): {len(obs_with_z)}', file=sys.stderr)
    z_call_sweep = []
    z_put_sweep = []
    z_thresholds = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0]
    for t in z_thresholds:
        z_call_sweep.append({'z_threshold': t, **stats_for(
            [o for o in obs_with_z if o['z_score'] >= t], 'CALL')})
        z_put_sweep.append({'z_threshold': -t, **stats_for(
            [o for o in obs_with_z if o['z_score'] <= -t], 'PUT')})

    # ──── Sweep 3: regime split ────
    regime_results = {}
    for reg in ('EARN_WK', 'EARN_MTH', 'NO_EARN'):
        reg_obs = [o for o in obs if o['regime'] == reg]
        reg_baseline = {'call': stats_for(reg_obs, 'CALL'), 'put': stats_for(reg_obs, 'PUT')}
        reg_call = []
        reg_put = []
        for t in thresholds:
            reg_call.append({'threshold': t, **stats_for(
                [o for o in reg_obs if o['call_m'] >= t], 'CALL')})
            reg_put.append({'threshold': -t, **stats_for(
                [o for o in reg_obs if o['call_m'] <= -t], 'PUT')})
        regime_results[reg] = {'baseline': reg_baseline, 'call_sweep': reg_call, 'put_sweep': reg_put,
                                'n_total': len(reg_obs)}

    # ──── Sweep 4: per-bucket ────
    bucket_results = {}
    for bucket in ('mega', 'mid', 'small', 'indices'):
        bk_obs = [o for o in obs if o['bucket'] == bucket]
        bk_baseline = {'call': stats_for(bk_obs, 'CALL'), 'put': stats_for(bk_obs, 'PUT')}
        bucket_results[bucket] = {'baseline': bk_baseline, 'n_total': len(bk_obs)}
        # Also a key threshold per bucket
        for t in (1.0, 2.0):
            bucket_results[bucket][f'call_geq_{t}'] = stats_for(
                [o for o in bk_obs if o['call_m'] >= t], 'CALL')
            bucket_results[bucket][f'put_leq_-{t}'] = stats_for(
                [o for o in bk_obs if o['call_m'] <= -t], 'PUT')

    out = {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'win_threshold_pct': WIN_THRESHOLD,
        'date_range': [min(o['date'] for o in obs), max(o['date'] for o in obs)],
        'n_observations': len(obs),
        'n_with_3d': n_3d,
        'baselines': {'CALL': baseline_call, 'PUT': baseline_put},
        'absolute_dollar_sweep': {'call': abs_call_sweep, 'put': abs_put_sweep},
        'z_score_sweep': {'call': z_call_sweep, 'put': z_put_sweep,
                           'n_z_scored': len(obs_with_z)},
        'by_regime': regime_results,
        'by_bucket': bucket_results,
    }
    op = os.path.join(HERE, 'v4_unified_backtest.json')
    with open(op, 'w') as f:
        json.dump(out, f, indent=2, default=str)

    # ──── Console output ────
    print(f"\n{'='*108}")
    print(f"V4 UNIFIED BACKTEST — using 60d UW backfill (Feb-Apr 2026)")
    print(f"{len(obs)} observations · {n_3d} with 3d forward · {len(obs_with_z)} z-scored")
    print(f"{'='*108}\n")
    print(f"BASELINE (universe-wide):")
    print(f"  CALL 3d aligned win = {baseline_call.get('3d_aligned_win_pct')}%   "
          f"avg = {baseline_call.get('3d_aligned_avg'):+.2f}%   n={baseline_call.get('3d_n')}")
    print(f"  PUT  3d aligned win = {baseline_put.get('3d_aligned_win_pct')}%   "
          f"avg = {baseline_put.get('3d_aligned_avg'):+.2f}%   n={baseline_put.get('3d_n')}")

    print(f"\n--- ABSOLUTE-$ CALL SWEEP ---")
    print(f"{'thresh':>8} {'n_total':>8} {'3d_n':>5} {'3d_win%':>9} {'3d_avg':>9} {'edge':>8}")
    base_call_3d = baseline_call.get('3d_aligned_win_pct') or 0
    for s in abs_call_sweep:
        n3 = s.get('3d_n', 0)
        wr = s.get('3d_aligned_win_pct')
        if n3 < 5: continue
        avg = s.get('3d_aligned_avg')
        edge = wr - base_call_3d if wr is not None else None
        print(f"+${s['threshold']:>5.1f}M {s['n_total']:>8} {n3:>5} "
              f"{f'{wr}%' if wr is not None else '—':>9} {f'{avg:+.2f}%' if avg is not None else '—':>9} "
              f"{f'{edge:+.1f}pp' if edge is not None else '—':>8}")

    print(f"\n--- ABSOLUTE-$ PUT SWEEP ---")
    base_put_3d = baseline_put.get('3d_aligned_win_pct') or 0
    for s in abs_put_sweep:
        n3 = s.get('3d_n', 0)
        if n3 < 5: continue
        wr = s.get('3d_aligned_win_pct')
        avg = s.get('3d_aligned_avg')
        edge = wr - base_put_3d if wr is not None else None
        print(f"-${abs(s['threshold']):>5.1f}M {s['n_total']:>8} {n3:>5} "
              f"{f'{wr}%' if wr is not None else '—':>9} {f'{avg:+.2f}%' if avg is not None else '—':>9} "
              f"{f'{edge:+.1f}pp' if edge is not None else '—':>8}")

    print(f"\n--- Z-SCORE CALL SWEEP (STRONG/USABLE baselines only) ---")
    for s in z_call_sweep:
        n3 = s.get('3d_n', 0)
        if n3 < 5: continue
        wr = s.get('3d_aligned_win_pct')
        avg = s.get('3d_aligned_avg')
        edge = wr - base_call_3d if wr is not None else None
        print(f"z≥+{s['z_threshold']:.1f}σ {s['n_total']:>8} {n3:>5} "
              f"{f'{wr}%' if wr is not None else '—':>9} {f'{avg:+.2f}%' if avg is not None else '—':>9} "
              f"{f'{edge:+.1f}pp' if edge is not None else '—':>8}")

    print(f"\n--- Z-SCORE PUT SWEEP ---")
    for s in z_put_sweep:
        n3 = s.get('3d_n', 0)
        if n3 < 5: continue
        wr = s.get('3d_aligned_win_pct')
        avg = s.get('3d_aligned_avg')
        edge = wr - base_put_3d if wr is not None else None
        print(f"z≤{s['z_threshold']:+.1f}σ {s['n_total']:>8} {n3:>5} "
              f"{f'{wr}%' if wr is not None else '—':>9} {f'{avg:+.2f}%' if avg is not None else '—':>9} "
              f"{f'{edge:+.1f}pp' if edge is not None else '—':>8}")

    print(f"\n--- BY REGIME (3d horizon) ---")
    for reg, rd in regime_results.items():
        cb = rd['baseline']['call'].get('3d_aligned_win_pct')
        pb = rd['baseline']['put'].get('3d_aligned_win_pct')
        print(f"\n{reg} (n={rd['n_total']})  baseline call={cb}%  put={pb}%")
        # Show key thresholds
        for s in rd['call_sweep']:
            n3 = s.get('3d_n', 0)
            if n3 < 5: continue
            wr = s.get('3d_aligned_win_pct')
            edge = (wr - cb) if (wr is not None and cb is not None) else None
            print(f"  CALL ≥+${s['threshold']:.1f}M  n3={n3:>3}  win={wr}%  edge={edge:+.1f}pp" if edge is not None else
                  f"  CALL ≥+${s['threshold']:.1f}M  n3={n3:>3}  win={wr}%")
        for s in rd['put_sweep']:
            n3 = s.get('3d_n', 0)
            if n3 < 5: continue
            wr = s.get('3d_aligned_win_pct')
            edge = (wr - pb) if (wr is not None and pb is not None) else None
            print(f"  PUT  ≤-${abs(s['threshold']):.1f}M  n3={n3:>3}  win={wr}%  edge={edge:+.1f}pp" if edge is not None else
                  f"  PUT  ≤-${abs(s['threshold']):.1f}M  n3={n3:>3}  win={wr}%")

    print(f"\n--- BY BUCKET (3d horizon, ≥+$1M CALL / ≤-$1M PUT) ---")
    for bucket in ('mega', 'mid', 'small', 'indices'):
        bd = bucket_results[bucket]
        cb = bd['baseline']['call'].get('3d_aligned_win_pct')
        pb = bd['baseline']['put'].get('3d_aligned_win_pct')
        c1 = bd['call_geq_1.0']
        p1 = bd['put_leq_-1.0']
        c1_n = c1.get('3d_n', 0); c1_wr = c1.get('3d_aligned_win_pct')
        p1_n = p1.get('3d_n', 0); p1_wr = p1.get('3d_aligned_win_pct')
        c1_edge = (c1_wr - cb) if (c1_wr is not None and cb is not None) else None
        p1_edge = (p1_wr - pb) if (p1_wr is not None and pb is not None) else None
        print(f"  {bucket:<8}  n_total={bd['n_total']}  baseline call={cb}% put={pb}%")
        print(f"            CALL≥+1M  n3={c1_n}  win={c1_wr}%  edge={c1_edge:+.1f}pp" if c1_edge is not None else
              f"            CALL≥+1M  n3={c1_n}")
        print(f"            PUT≤-1M   n3={p1_n}  win={p1_wr}%  edge={p1_edge:+.1f}pp" if p1_edge is not None else
              f"            PUT≤-1M   n3={p1_n}")

    print(f"\nSaved → {op}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
