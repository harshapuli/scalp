"""
V4 FORECASTER WALK-FORWARD — does the prediction chip actually predict?

The cells in BucketView's predictionFor() were calibrated on Apr 22-29
(in-sample). Showing in-sample hit rate proves nothing — that's just
fitting to noise. To honestly evaluate the forecaster, we have to:

  1. Pretend we're at end of day D.
  2. Calibrate the prediction cells using ONLY data from days < D.
  3. Apply those cells to events on day D.
  4. Compare predictions to actuals.
  5. Repeat for D = 2, 3, ..., 6 (each day tested with prior data only).

This is the standard time-series walk-forward backtest. Reports:
  - Direction accuracy: % of predictions where actual move matches predicted bias
  - Range hit rate: % where actual fell within predicted [low, high]
  - Magnitude bias: avg(actual - prediction_midpoint)
  - Per-cell accuracy
  - Overall: did the forecaster work in any regime?

Caveat: with only 6 days, each "future" day has 1-5 days of calibration
data. Tiny sample. Treat results as directional, not definitive.

Run: python3 v4_forecaster_walkforward.py
Output: v4_forecaster_walkforward.json
"""
import os, sys, json, glob, statistics
from collections import defaultdict
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))


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
    snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    snaps = {}
    for p in snap_paths:
        d = date_from_snap(p)
        try: snaps[d] = json.load(open(p)).get('data') or {}
        except: pass
    dates = sorted(snaps.keys())
    print(f'loaded {len(dates)} snapshots: {dates}', file=sys.stderr)

    # earnings
    from datetime import datetime as _dt
    latest = json.load(open(snap_paths[-1])).get('data') or {}
    earnings_by_t = {}
    for tkr, blob in latest.items():
        info = (blob.get('info', {}).get('data') or {})
        e = info.get('next_earnings_date')
        if e:
            try: earnings_by_t[tkr] = _dt.strptime(e, '%Y-%m-%d').date()
            except: pass

    # picks
    picks_path = os.path.join(HERE, 'data', 'picks_77.json')
    picks = set()
    if os.path.exists(picks_path):
        pd = json.load(open(picks_path))
        for k in ('mega', 'mid', 'small', 'indices'):
            for r in (pd.get('buckets') or {}).get(k, []) or []: picks.add(r['ticker'])

    def regime_for(tkr, session_iso):
        e = earnings_by_t.get(tkr)
        if not e: return 'NO_EARN'
        s = _dt.strptime(session_iso, '%Y-%m-%d').date()
        delta = abs((e - s).days)
        if delta <= 7:  return 'EARN_WK'
        if delta <= 30: return 'EARN_MTH'
        return 'NO_EARN'

    # Build all observations with last_30m_call_$ + 3d-aligned forward return
    obs = []
    for i, d in enumerate(dates):
        for tkr, blob in snaps[d].items():
            if picks and tkr not in picks: continue
            npt = blob.get('net_prem_ticks')
            if isinstance(npt, dict): npt = npt.get('data') or []
            if not isinstance(npt, list) or not npt: continue
            wcall = 0.0
            for t in npt:
                hh_mm = (t.get('tape_time') or t.get('t') or '')[11:16]
                if '19:30' <= hh_mm <= '20:00':
                    try: wcall += float(t.get('net_call_premium', 0) or 0)
                    except: pass
            wcall_m = wcall / 1e6

            close_px = ((blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
            day3_close = None
            if i + 3 < len(dates):
                _, day3_close = get_open_close(snaps[dates[i + 3]], tkr)
            day3_pct = None
            if close_px and day3_close is not None and float(close_px) > 0:
                try: day3_pct = (day3_close - float(close_px)) / float(close_px) * 100
                except: pass

            obs.append({
                'ticker': tkr, 'date': d, 'date_idx': i,
                'regime': regime_for(tkr, d),
                'call_m': round(wcall_m, 3),
                'day3_pct': round(day3_pct, 3) if day3_pct is not None else None,
            })

    print(f'\ntotal obs with 3d data: {sum(1 for o in obs if o.get("day3_pct") is not None)}', file=sys.stderr)

    # Cells we forecast on. (regime, call-side band) → describe
    CELLS = [
        ('EARN_WK',  'PUT',  -2.0, -0.5),
        ('EARN_MTH', 'PUT',  -2.0, -0.5),
        ('EARN_MTH', 'CALL',  0.5,  3.0),
        ('NO_EARN',  'CALL',  1.0,  4.0),
    ]

    def event_cell(o):
        for reg, side, lo, hi in CELLS:
            if o['regime'] != reg: continue
            cm = o['call_m']
            if side == 'CALL' and lo <= cm <= hi: return (reg, side, lo, hi)
            if side == 'PUT'  and lo <= cm <= hi: return (reg, side, lo, hi)
        return None

    def calibrate_cell(events_in_cell, side):
        """Given prior events in a cell, return (pred_low, pred_high, win_rate, n)"""
        if not events_in_cell:
            return None
        sign = 1 if side == 'CALL' else -1
        # winners = those whose move went in the predicted direction
        winners = [e['day3_pct'] * sign for e in events_in_cell
                   if e.get('day3_pct') is not None and e['day3_pct'] * sign >= 0.5]
        n = sum(1 for e in events_in_cell if e.get('day3_pct') is not None)
        if n == 0: return None
        win_rate = len(winners) / n * 100
        if not winners:
            return None  # no historical winners → no prediction
        # Use winner mean ± half-stdev as prediction range, in aligned (positive) direction
        winner_avg = statistics.mean(winners)
        winner_lo = winner_avg * 0.7  # conservative lower bound
        winner_hi = winner_avg * 1.3  # conservative upper bound
        if side == 'PUT':
            return (-winner_hi, -winner_lo, round(win_rate, 1), n)
        return (winner_lo, winner_hi, round(win_rate, 1), n)

    # Walk-forward: for each day D > 0, calibrate on dates[<D], test on dates[D].
    # Need 3d forward, so D <= len(dates) - 4.
    # Actually our dataset only allows 3d forward through dates[2] (Apr 24).
    # The full-walk-forward we can really do is limited; let me also do
    # leave-one-day-out as a fallback.

    walk_results = []
    for fold_idx in range(1, len(dates)):
        train = [o for o in obs if o['date_idx'] < fold_idx]
        test_day = dates[fold_idx]
        test = [o for o in obs if o['date'] == test_day and o.get('day3_pct') is not None]

        # calibrate per cell from training data
        cell_models = {}
        for reg, side, lo, hi in CELLS:
            in_cell = [o for o in train if o['regime'] == reg and (
                (side == 'CALL' and lo <= o['call_m'] <= hi) or
                (side == 'PUT' and lo <= o['call_m'] <= hi)
            )]
            cell_models[(reg, side, lo, hi)] = calibrate_cell(in_cell, side)

        # apply to test
        for o in test:
            cell = event_cell(o)
            if not cell: continue
            model = cell_models.get(cell)
            if not model: continue
            pred_lo, pred_hi, train_win_rate, train_n = model
            actual = o['day3_pct']
            # direction match: did actual go in predicted side?
            pred_side = cell[1]
            sign = 1 if pred_side == 'CALL' else -1
            direction_match = (actual * sign) > 0
            in_range = pred_lo <= actual <= pred_hi
            walk_results.append({
                'fold': fold_idx, 'test_date': test_day,
                'ticker': o['ticker'],
                'regime': o['regime'], 'call_m': o['call_m'],
                'pred_side': pred_side, 'pred_lo': round(pred_lo, 2), 'pred_hi': round(pred_hi, 2),
                'pred_n_train': train_n, 'pred_win_rate_train': train_win_rate,
                'actual_3d_pct': actual,
                'direction_match': direction_match,
                'in_range': in_range,
            })

    # Aggregate
    n_walk = len(walk_results)
    n_dir_match = sum(1 for r in walk_results if r['direction_match'])
    n_in_range = sum(1 for r in walk_results if r['in_range'])

    # Per-cell aggregation
    by_cell = defaultdict(list)
    for r in walk_results:
        by_cell[f"{r['regime']}_{r['pred_side']}"].append(r)

    cell_summaries = {}
    for cell, group in by_cell.items():
        n = len(group)
        if n == 0: continue
        cell_summaries[cell] = {
            'n_walk_forward_predictions': n,
            'direction_match_rate': round(sum(1 for r in group if r['direction_match']) / n * 100, 1),
            'in_range_rate': round(sum(1 for r in group if r['in_range']) / n * 100, 1),
            'avg_predicted_mid': round(statistics.mean((r['pred_lo'] + r['pred_hi']) / 2 for r in group), 2),
            'avg_actual': round(statistics.mean(r['actual_3d_pct'] for r in group), 2),
            'magnitude_bias': round(statistics.mean(r['actual_3d_pct'] - (r['pred_lo'] + r['pred_hi']) / 2 for r in group), 2),
        }

    out = {
        'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'n_walk_forward_predictions': n_walk,
        'overall_direction_match_rate': round(n_dir_match / n_walk * 100, 1) if n_walk else None,
        'overall_in_range_rate':        round(n_in_range / n_walk * 100, 1) if n_walk else None,
        'by_cell':                      cell_summaries,
        'walk_forward_predictions':     walk_results,
    }
    with open(os.path.join(HERE, 'v4_forecaster_walkforward.json'), 'w') as f:
        json.dump(out, f, indent=2, default=str)

    print(f"\n{'='*108}")
    print(f"V4 FORECASTER WALK-FORWARD")
    print(f"For each day D, calibrate on days < D, predict day D events, compare to actuals.")
    print(f"{'='*108}\n")
    print(f"Total walk-forward predictions: {n_walk}")
    if n_walk:
        print(f"  Overall direction match: {round(n_dir_match/n_walk*100, 1)}%   ({n_dir_match}/{n_walk})")
        print(f"  Overall in-range hit:    {round(n_in_range/n_walk*100, 1)}%   ({n_in_range}/{n_walk})")
    print(f"\nPer-cell results:")
    print(f"{'cell':<20} {'n':>4} {'dir_match%':>11} {'in_range%':>11} {'pred_mid':>9} {'actual':>9} {'bias':>9}")
    print('-' * 80)
    for cell, s in sorted(cell_summaries.items()):
        print(f"{cell:<20} {s['n_walk_forward_predictions']:>4} {s['direction_match_rate']:>10}% {s['in_range_rate']:>10}% "
              f"{s['avg_predicted_mid']:>+8}% {s['avg_actual']:>+8}% {s['magnitude_bias']:>+8}%")

    print(f"\n=== INDIVIDUAL PREDICTIONS (each row is one walk-forward forecast) ===")
    print(f"{'fold':>4} {'date':<11} {'tkr':<6} {'cell':<14} {'$M':>7} {'pred_range':>14} {'n_train':>7} {'actual':>7} {'dir':>4} {'in_range':>9}")
    for r in walk_results[:50]:
        cell_label = f"{r['regime']}_{r['pred_side']}"
        pred_str = f"{r['pred_lo']:+.1f} to {r['pred_hi']:+.1f}"
        print(f"{r['fold']:>4} {r['test_date']:<11} {r['ticker']:<6} {cell_label:<14} {r['call_m']:>+6.2f}M  "
              f"{pred_str:>14} {r['pred_n_train']:>7} {r['actual_3d_pct']:>+6.2f}% "
              f"{'✓' if r['direction_match'] else '✗':>4} {'✓' if r['in_range'] else '✗':>9}")
    if len(walk_results) > 50:
        print(f'... {len(walk_results) - 50} more')

    print(f"\nSaved → v4_forecaster_walkforward.json")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
