"""Quick re-run that ALSO dumps every pick + sector breakdown so we can see
whether the negative ALL-universe return is dragging the picks down or whether
the picks themselves are just bad."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from v4_formula_experiment import (
    main as run_main, VARIANTS, SCORE_THRESHOLD, WIN_PCT,
    extract_features, load_uw_signals_for, date_from_snap_path
)
import json, glob
from collections import defaultdict
HERE = os.path.dirname(os.path.abspath(__file__))

# Re-do the loop, but capture ALL picks per variant for 3d
snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
snaps = {}
for p in snap_paths:
    d = date_from_snap_path(p)
    if not d: continue
    try:
        snaps[d] = (json.load(open(p)).get('data') or {})
    except: pass
dates = sorted(snaps.keys())

bars_by_ticker = defaultdict(list)
for d in dates:
    for tkr, blob in snaps[d].items():
        db = (blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}
        c = db.get('c')
        if c is None: continue
        try:
            bars_by_ticker[tkr].append({
                'date': d, 'c': float(c),
                'h': float(db.get('h') or c),
                'o': float(db.get('o') or c),
            })
        except: pass

# seed prior day from prevDailyBar
from datetime import datetime, timedelta
first_d = dates[0]
for tkr, blob in snaps[first_d].items():
    pdb = (blob.get('alpaca_snapshot') or {}).get('prevDailyBar') or {}
    c = pdb.get('c')
    if c is None: continue
    try:
        dt = datetime.strptime(first_d, '%Y-%m-%d') - timedelta(days=1)
        bars_by_ticker[tkr].insert(0, {'date': dt.strftime('%Y-%m-%d'), 'c': float(c),
            'h': float(pdb.get('h') or c), 'o': float(pdb.get('o') or c)})
    except: pass

picks_3d = {v[0]: [] for v in VARIANTS}
sector_counts = {v[0]: defaultdict(int) for v in VARIANTS}

for i, d in enumerate(dates):
    if i < 1 or i >= len(dates) - 1: continue
    uw = load_uw_signals_for(d)
    for tkr, blob in snaps[d].items():
        hist = [b for b in bars_by_ticker.get(tkr, []) if b['date'] <= d]
        if len(hist) < 4: continue
        f = extract_features(tkr, blob, hist)
        if not f: continue
        ec = f['today_close']
        if not ec: continue
        # 3d forward
        if i + 3 >= len(dates): continue
        fd = dates[i + 3]
        fblob = snaps[fd].get(tkr)
        if not fblob: continue
        fc = ((fblob.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
        if fc is None: continue
        try:
            ret = (float(fc) - ec) / ec * 100
        except: continue
        for vname, vfunc in VARIANTS:
            score = vfunc(f, uw=uw.get(tkr))
            if score >= SCORE_THRESHOLD:
                picks_3d[vname].append({
                    'day': d, 'ticker': tkr, 'sector': f.get('sector'),
                    'score': score, 'ret_3d': round(ret, 2),
                    'win': ret >= WIN_PCT,
                    'beta': round(f.get('beta', 0), 2),
                })
                sector_counts[vname][f.get('sector')] += 1

print("\n" + "="*100)
print(f"FULL 3d PICKS (score >= {SCORE_THRESHOLD}, win = +{WIN_PCT}% in 3d)")
print("="*100)
for vname, _ in VARIANTS:
    picks = sorted(picks_3d[vname], key=lambda r: -r['score'])
    if not picks:
        print(f"\n{vname}: (no picks)")
        continue
    win_n = sum(1 for p in picks if p['win'])
    avg_ret = sum(p['ret_3d'] for p in picks) / len(picks)
    print(f"\n{vname}: n={len(picks)} wins={win_n}/{len(picks)} ({win_n/len(picks)*100:.0f}%) avg={avg_ret:+.2f}%")
    print(f"  sectors: {dict(sector_counts[vname])}")
    print(f"  {'day':<11} {'tkr':<6} {'sec':<22} {'beta':>5} {'score':>5} {'ret_3d':>7} {'win':>4}")
    for p in picks:
        print(f"  {p['day']:<11} {p['ticker']:<6} {p['sector'][:22]:<22} {p['beta']:>5} {p['score']:>5} {p['ret_3d']:>+6.1f}% {'✓' if p['win'] else ' ':>4}")

# Compare picks distribution by day
print("\n" + "="*100)
print("PICKS BY ENTRY DAY (which day did each variant fire on)")
print("="*100)
for vname, _ in VARIANTS:
    by_day = defaultdict(list)
    for p in picks_3d[vname]:
        by_day[p['day']].append(p)
    print(f"\n{vname}:")
    for day in sorted(by_day.keys()):
        ps = by_day[day]
        wins = sum(1 for x in ps if x['win'])
        print(f"  {day}: {len(ps)} picks → {wins} wins, avg {sum(x['ret_3d'] for x in ps)/len(ps):+.2f}%")
