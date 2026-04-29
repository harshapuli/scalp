"""
V4 FORMULA WEEKLY COMPARE — v7 vs v8 vs ground-truth winners

Re-runs both staging_score_v7 and staging_score_v8 on every historical
snapshot we have, looks up forward 1d/3d closes (also from snapshots),
and prints/saves win-rate-by-variant + win-rate-by-sector tables.

Designed to be re-run every week (Saturday). As more snapshots accumulate,
the sample gets bigger and either:
  - confirms v8 has a real edge → keep
  - shows v7 was right or v8 is no better → revert SCORE_FN_VERSION to 'v7'
  - exposes a NEW pattern → tune v9

Run: python3 v4_formula_weekly_compare.py
Output: v4_formula_compare_<YYYY-MM-DD>.json + console table
"""
import os, sys, json, glob, statistics
from collections import defaultdict, Counter
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

# We re-use the scanner's score functions and feature extractor — but bypass
# the WS live overlay (use frozen snapshot data only, so historical compare
# is fair).
from v4_staging_scanner import (
    staging_score_v7, staging_score_v8,
    staging_score_v7_put, staging_score_v8_put,
    V7_BUY_THRESHOLD, V8_BUY_THRESHOLD,
    _apply_directional_gate,
)
THRESHOLDS = {'v7': V7_BUY_THRESHOLD, 'v8': V8_BUY_THRESHOLD}
# Use the relaxed feature extractor from v4_winners_vs_losers (≥2 priors).
from v4_winners_vs_losers import relaxed_features

WIN_PCT = 5.0
HORIZONS = [1, 3]


def date_from_snap_path(p):
    return os.path.basename(p).replace('v4_snapshot_', '').replace('.json', '')


def load_all_snapshots():
    paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
    snaps = {}
    for p in paths:
        d = date_from_snap_path(p)
        if not d: continue
        try:
            snaps[d] = (json.load(open(p)).get('data') or {})
        except: pass
    return snaps


def build_bars_index(snaps, dates):
    bars = defaultdict(list)
    for d in dates:
        for tkr, blob in snaps[d].items():
            db = (blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}
            c = db.get('c')
            if c is None: continue
            try:
                bars[tkr].append({'date': d, 'c': float(c),
                    'h': float(db.get('h') or c), 'o': float(db.get('o') or c)})
            except: pass
    # Seed prev-day from prevDailyBar of FIRST snapshot
    first_d = dates[0]
    for tkr, blob in snaps[first_d].items():
        pdb = (blob.get('alpaca_snapshot') or {}).get('prevDailyBar') or {}
        c = pdb.get('c')
        if c is None: continue
        try:
            dt = datetime.strptime(first_d, '%Y-%m-%d') - timedelta(days=1)
            bars[tkr].insert(0, {'date': dt.strftime('%Y-%m-%d'), 'c': float(c),
                'h': float(pdb.get('h') or c), 'o': float(pdb.get('o') or c)})
        except: pass
    return bars


def load_uw_signals_for(date_str):
    p = os.path.join(HERE, f'v4_uw_signals_{date_str}.json')
    if not os.path.exists(p): return {}
    try:
        return (json.load(open(p)).get('per_ticker') or {})
    except: return {}


def main():
    snaps = load_all_snapshots()
    dates = sorted(snaps.keys())
    if len(dates) < 4:
        print(f'need ≥4 snapshots for 3d horizon, have {len(dates)}', file=sys.stderr)
        return 1
    bars = build_bars_index(snaps, dates)
    print(f'loaded {len(dates)} snapshots: {dates}', file=sys.stderr)

    def close_at(tkr, idx):
        if idx >= len(dates) or idx < 0: return None
        b = snaps[dates[idx]].get(tkr)
        if not b: return None
        c = ((b.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
        try: return float(c) if c is not None else None
        except: return None

    # rows[variant][horizon] = list of {ticker, day, score, ret, win, sector, beta_band, mcap_band}
    rows = {v: {h: [] for h in HORIZONS} for v in ['v7', 'v8']}

    for i, d in enumerate(dates):
        if i + max(HORIZONS) >= len(dates): continue  # need forward data
        uw = load_uw_signals_for(d)
        for tkr, blob in snaps[d].items():
            hist = [b for b in bars.get(tkr, []) if b['date'] <= d]
            f = relaxed_features(tkr, blob, hist)
            if not f: continue
            ec = f['today_close']
            if not ec: continue

            # Compute raw scores then apply the same directional gate
            # the live scanner uses (zero-out CALL on clearly-down days,
            # PUT on clearly-up days). Apples-to-apples for both versions.
            v7s_raw = staging_score_v7(f, uw_signals=uw.get(tkr))
            v8s_raw = staging_score_v8(f, uw_signals=uw.get(tkr))
            v7s, _ = _apply_directional_gate(f, v7s_raw, 0)  # only need CALL side here
            v8s, _ = _apply_directional_gate(f, v8s_raw, 0)

            sector = f.get('sector') or '—'
            beta = f.get('beta') or 0
            if   beta < 0.5: beta_band = '<0.5'
            elif beta < 1.0: beta_band = '0.5-1'
            elif beta < 1.5: beta_band = '1-1.5'
            elif beta < 2.0: beta_band = '1.5-2'
            elif beta < 2.5: beta_band = '2-2.5'
            elif beta < 3.0: beta_band = '2.5-3'
            else:            beta_band = '≥3'
            mcap = f.get('mcap_b') or 0
            if   mcap < 5:   mcap_band = '<5B'
            elif mcap < 20:  mcap_band = '5-20B'
            elif mcap < 100: mcap_band = '20-100B'
            elif mcap < 500: mcap_band = '100-500B'
            else:            mcap_band = '≥500B'

            for h in HORIZONS:
                fc = close_at(tkr, i + h)
                if fc is None: continue
                ret = (fc - ec) / ec * 100
                base = {
                    'ticker': tkr, 'day': d, 'ret': ret, 'win': ret >= WIN_PCT,
                    'sector': sector, 'beta_band': beta_band, 'mcap_band': mcap_band,
                }
                rows['v7'][h].append({**base, 'score': v7s})
                rows['v8'][h].append({**base, 'score': v8s})

    # Aggregate
    def agg(picks):
        if not picks: return {'n': 0, 'win_rate': None, 'avg_ret': None}
        return {
            'n': len(picks),
            'win_rate': round(sum(1 for p in picks if p['win']) / len(picks) * 100, 1),
            'avg_ret': round(statistics.mean(p['ret'] for p in picks), 2),
        }

    summary = {}
    for v in ('v7', 'v8'):
        summary[v] = {'threshold': THRESHOLDS[v]}
        for h in HORIZONS:
            recs = rows[v][h]
            picks = [r for r in recs if r['score'] >= THRESHOLDS[v]]
            sec_breakdown = {}
            for sec in sorted(set(r['sector'] for r in picks)):
                sp = [p for p in picks if p['sector'] == sec]
                sec_breakdown[sec] = agg(sp)
            beta_breakdown = {}
            for bb in sorted(set(r['beta_band'] for r in picks)):
                sp = [p for p in picks if p['beta_band'] == bb]
                beta_breakdown[bb] = agg(sp)
            summary[v][f'{h}d'] = {
                'all': agg(recs),
                'picks_at_300': agg(picks),
                'picks_top_n': sorted(
                    [{'ticker': p['ticker'], 'day': p['day'], 'score': p['score'],
                      'ret': round(p['ret'], 2), 'win': p['win'],
                      'sector': p['sector']}
                     for p in picks], key=lambda x: -x['score'])[:20],
                'by_sector': sec_breakdown,
                'by_beta':   beta_breakdown,
            }

    # Weekly file with date stamp
    today_iso = datetime.now(timezone.utc).strftime('%Y-%m-%d')
    out = {
        'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
        'snapshots_used': dates,
        'snapshots_n': len(dates),
        'win_threshold_pct': WIN_PCT,
        'score_thresholds': THRESHOLDS,
        'summary': summary,
    }
    out_path = os.path.join(HERE, f'v4_formula_compare_{today_iso}.json')
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2, default=str)

    # Console table
    print(f"\n{'='*100}")
    print(f"V4 FORMULA WEEKLY COMPARE — v7 vs v8")
    print(f"Snapshots: {dates[0]} → {dates[-1]} ({len(dates)} days)")
    print(f"Win = forward return ≥ {WIN_PCT}% in horizon. Threshold v7={V7_BUY_THRESHOLD} v8={V8_BUY_THRESHOLD}")
    print(f"{'='*100}")

    for h in HORIZONS:
        print(f"\n=== {h}d horizon ===")
        print(f"{'variant':<6} {'all_n':>7} {'all_win%':>9} {'all_avg':>8} | {'picks_n':>8} {'picks_win%':>11} {'picks_avg':>10}")
        print('-' * 80)
        for v in ('v7', 'v8'):
            s = summary[v][f'{h}d']
            a, p = s['all'], s['picks_at_300']
            wr_a = f"{a['win_rate']:.1f}%" if a['win_rate'] is not None else '—'
            ar_a = f"{a['avg_ret']:+.2f}%" if a['avg_ret'] is not None else '—'
            wr_p = f"{p['win_rate']:.1f}%" if p['win_rate'] is not None else '—'
            ar_p = f"{p['avg_ret']:+.2f}%" if p['avg_ret'] is not None else '—'
            print(f"{v:<6} {a['n']:>7} {wr_a:>9} {ar_a:>8} | {p['n']:>8} {wr_p:>11} {ar_p:>10}")

        # Sector breakdown for picks
        print(f"\n{h}d picks by sector:")
        for v in ('v7', 'v8'):
            print(f"  {v}:")
            for sec, a in summary[v][f'{h}d']['by_sector'].items():
                if a['n'] == 0: continue
                wr = f"{a['win_rate']:.1f}%" if a['win_rate'] is not None else '—'
                ar = f"{a['avg_ret']:+.2f}%" if a['avg_ret'] is not None else '—'
                print(f"    {sec[:22]:<22} n={a['n']:>3}  win%={wr:>6}  avg={ar:>7}")

    # Top picks per variant
    print(f"\n=== top-10 PICKS (3d) ===")
    for v in ('v7', 'v8'):
        picks = summary[v]['3d']['picks_top_n'][:10]
        if not picks: print(f"  {v}: (none)"); continue
        wins = sum(1 for p in picks if p['win'])
        print(f"  {v}: {wins}/{len(picks)} won")
        for p in picks:
            mark = '✓' if p['win'] else ' '
            print(f"    {p['day']} {p['ticker']:<6} score={p['score']:>4} ret={p['ret']:>+6.1f}% {mark} ({p['sector'][:18]})")

    print(f"\nSaved → {out_path}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
