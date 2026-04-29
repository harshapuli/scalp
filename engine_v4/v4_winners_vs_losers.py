"""
WINNERS-FIRST ANALYSIS

Across all (ticker × entry day) where we have a forward 3d close, find every
WINNER (3d return ≥ +5%). Then look for what those winners had in common
on entry day — across features, sectors, beta, IV, flow, day-of-window.

This widens the sample beyond the 3-pick variant intersection — uses every
ticker in the universe, not just those scoring ≥300.
"""
import os, sys, json, glob, statistics
from collections import defaultdict, Counter
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
WIN_PCT = 5.0  # winner = +5% in 3d


def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict): return default
        cur = cur.get(k)
        if cur is None: return default
    return cur


def date_from_snap_path(p):
    return os.path.basename(p).replace('v4_snapshot_', '').replace('.json', '')


def relaxed_features(ticker, blob, hist_closes):
    """Like extract_features but only requires ≥2 closes (instead of ≥4)
    so we can analyze tickers with shorter history."""
    if not blob or not hist_closes or len(hist_closes) < 2:
        return None
    info = _safe(blob, 'info', 'data') or {}
    ov = (_safe(blob, 'options_volume', 'data') or [{}])[0]
    iv_arr = _safe(blob, 'iv_rank', 'data') or []
    dp_today = _safe(blob, 'darkpool', 'data') or []
    npt = _safe(blob, 'net_prem_ticks', 'data') or []
    fps = _safe(blob, 'flow_per_strike') or []
    if isinstance(fps, dict): fps = fps.get('data') or []

    closes = [b['c'] for b in hist_closes]
    today_close = closes[-1]
    today_pct = (closes[-1] - closes[-2]) / closes[-2] * 100 if (len(closes) >= 2 and closes[-2]) else 0
    # cum_3d from whatever priors we have
    cum_3d = sum((closes[i] - closes[i-1]) / closes[i-1] * 100
                 for i in range(max(1, len(closes)-3), len(closes)) if closes[i-1])
    last3_pcts = [(closes[i] - closes[i-1]) / closes[i-1] * 100
                  for i in range(max(1, len(closes)-3), len(closes)) if closes[i-1]]
    had_recent_5pct = any(p >= 5 for p in last3_pcts)
    last5 = hist_closes[-5:] if len(hist_closes) >= 5 else hist_closes
    high_5d = max(b.get('h', b['c']) for b in last5)
    pct_below_5d_high = (high_5d - today_close) / today_close * 100 if today_close else 0

    call_vol = float(ov.get('call_volume', 0) or 0)
    put_vol = float(ov.get('put_volume', 0) or 0)
    call_ask = float(ov.get('call_volume_ask_side', 0) or 0)
    bull = float(ov.get('bullish_premium', 0) or 0) / 1e6
    bear = float(ov.get('bearish_premium', 0) or 0) / 1e6

    iv_today = float(iv_arr[-1].get('iv_rank_1y', 0) or 0) if iv_arr else 0
    iv_5d_ago = float(iv_arr[0].get('iv_rank_1y', 0) or 0) if iv_arr else iv_today
    iv_change_5d = iv_today - iv_5d_ago

    dp_prem_m = sum(float(d.get('premium', 0) or 0) for d in dp_today) / 1e6

    last_hr_call_prem = 0.0
    if len(npt) >= 24:
        for t in npt[-12:]:
            last_hr_call_prem += float(t.get('net_call_premium', 0) or 0)
    last_hr_call_prem_m = last_hr_call_prem / 1e6

    otm_call_prem_m = 0.0
    if fps and today_close > 0:
        for f in fps:
            try:
                strike = float(f.get('strike', 0) or 0)
                if 1.02 * today_close <= strike <= 1.10 * today_close:
                    otm_call_prem_m += (float(f.get('call_premium_ask_side', 0) or 0)
                                        - float(f.get('call_premium_bid_side', 0) or 0))
            except: pass
    otm_call_prem_m /= 1e6

    return {
        'ticker': ticker,
        'sector': info.get('sector') or '',
        'beta': float(info.get('beta', 0) or 0),
        'mcap_b': float(info.get('marketcap', 0) or 0) / 1e9,
        'today_close': today_close,
        'today_pct': today_pct,
        'cum_3d': cum_3d,
        'pct_below_5d_high': pct_below_5d_high,
        'had_recent_5pct': had_recent_5pct,
        'iv_today': iv_today,
        'iv_change_5d': iv_change_5d,
        'dp_prem_m': dp_prem_m,
        'put_call_ratio': put_vol / max(call_vol, 1),
        'call_ask_share': call_ask / max(call_vol, 1),
        'last_hr_call_prem_m': last_hr_call_prem_m,
        'otm_call_prem_m': otm_call_prem_m,
        'bull_m': bull,
        'bear_m': bear,
        'n_priors': len(closes),
    }


# ---- Load snapshots ----
snap_paths = sorted(glob.glob(os.path.join(HERE, 'v4_snapshot_*.json')))
snaps = {}
for p in snap_paths:
    d = date_from_snap_path(p)
    if not d: continue
    try:
        snaps[d] = (json.load(open(p)).get('data') or {})
    except: pass
dates = sorted(snaps.keys())
print(f'snapshots: {dates}', file=sys.stderr)

bars_by_ticker = defaultdict(list)
for d in dates:
    for tkr, blob in snaps[d].items():
        db = (blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}
        c = db.get('c')
        if c is None: continue
        try:
            bars_by_ticker[tkr].append({'date': d, 'c': float(c),
                'h': float(db.get('h') or c), 'o': float(db.get('o') or c)})
        except: pass

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


def close_at(tkr, idx):
    if idx >= len(dates): return None
    blob = snaps[dates[idx]].get(tkr)
    if not blob: return None
    fc = ((blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
    try: return float(fc) if fc is not None else None
    except: return None


# ---- For every (ticker × entry day with 3d forward), compute features + ret ----
all_rows = []  # one per (ticker, entry_day)
for i, d in enumerate(dates):
    if i + 3 >= len(dates): continue  # need ≥3 trading days forward
    fwd_idx = i + 3
    for tkr, blob in snaps[d].items():
        hist = [b for b in bars_by_ticker.get(tkr, []) if b['date'] <= d]
        f = relaxed_features(tkr, blob, hist)
        if not f: continue
        ec = f['today_close']
        if not ec: continue
        fc = close_at(tkr, fwd_idx)
        if fc is None: continue
        ret = (fc - ec) / ec * 100
        all_rows.append({
            **f, 'entry_day': d, 'fwd_day': dates[fwd_idx],
            'ret_3d': ret, 'win': ret >= WIN_PCT,
        })

print(f'total (ticker × day) obs with 3d forward: {len(all_rows)}', file=sys.stderr)
winners = [r for r in all_rows if r['win']]
losers = [r for r in all_rows if not r['win']]
print(f'winners: {len(winners)}  losers: {len(losers)}', file=sys.stderr)

# ---- 1. WINNERS LIST (all 3d winners across whole window) ----
winners.sort(key=lambda r: -r['ret_3d'])
print(f"\n{'='*120}")
print(f"ALL WINNERS (3d return ≥ {WIN_PCT}%) — n={len(winners)} across {len(set(r['entry_day'] for r in all_rows))} entry days")
print(f"{'='*120}")
print(f"{'tkr':<6} {'entry':<11} {'sector':<22} {'beta':>5} {'mcap$B':>7} {'tdy%':>6} {'3d%':>6} {'IV':>4} {'IVΔ':>4} {'p/c':>4} {'cAsk':>5} {'DP$M':>6} {'lhr$M':>6} {'ret_3d':>7}")
print('-' * 120)
for r in winners:
    sec = (r['sector'] or '')[:22]
    print(f"{r['ticker']:<6} {r['entry_day']:<11} {sec:<22} {r['beta']:>5.2f} "
          f"{r['mcap_b']:>7.1f} {r['today_pct']:>+5.1f} {r['cum_3d']:>+5.1f} "
          f"{r['iv_today']:>4.0f} {r['iv_change_5d']:>+4.0f} {r['put_call_ratio']:>4.2f} "
          f"{r['call_ask_share']:>5.2f} {r['dp_prem_m']:>6.0f} {r['last_hr_call_prem_m']:>+6.1f} "
          f"{r['ret_3d']:>+6.1f}%")

# ---- 2. SECTOR + BETA bucket comparison ----
print(f"\n{'='*100}")
print(f"WHO WINS — sector & beta breakdown")
print(f"{'='*100}")
sec_w = Counter(r['sector'] for r in winners)
sec_all = Counter(r['sector'] for r in all_rows)
print(f"{'sector':<24} {'win_n':>6} {'all_n':>6} {'win_rate':>10} {'lift':>6}")
print('-' * 60)
for sec in sorted(sec_all.keys(), key=lambda s: -sec_w[s]):
    wn, an = sec_w[sec], sec_all[sec]
    if an < 5: continue  # skip tiny
    wr = wn / an * 100
    base = len(winners) / len(all_rows) * 100
    lift = wr / base if base > 0 else 0
    print(f"{(sec or '—')[:24]:<24} {wn:>6} {an:>6} {wr:>9.1f}% {lift:>5.2f}x")

# Beta buckets
print(f"\n{'beta bucket':<14} {'win_n':>6} {'all_n':>6} {'win_rate':>10} {'lift':>6}")
print('-' * 60)
buckets = [(0, 0.5, '<0.5'), (0.5, 1.0, '0.5-1.0'), (1.0, 1.5, '1.0-1.5'),
           (1.5, 2.0, '1.5-2.0'), (2.0, 2.5, '2.0-2.5'), (2.5, 3.0, '2.5-3.0'),
           (3.0, 99, '≥3.0')]
for lo, hi, lbl in buckets:
    win_n = sum(1 for r in winners if lo <= r['beta'] < hi)
    all_n = sum(1 for r in all_rows if lo <= r['beta'] < hi)
    if all_n < 5: continue
    wr = win_n / all_n * 100
    base = len(winners) / len(all_rows) * 100
    lift = wr / base if base > 0 else 0
    print(f"{lbl:<14} {win_n:>6} {all_n:>6} {wr:>9.1f}% {lift:>5.2f}x")

# Mcap buckets
print(f"\n{'mcap bucket':<14} {'win_n':>6} {'all_n':>6} {'win_rate':>10} {'lift':>6}")
print('-' * 60)
mbk = [(0, 5, '<5B'), (5, 20, '5-20B'), (20, 100, '20-100B'),
       (100, 500, '100-500B'), (500, 9999, '≥500B')]
for lo, hi, lbl in mbk:
    win_n = sum(1 for r in winners if lo <= r['mcap_b'] < hi)
    all_n = sum(1 for r in all_rows if lo <= r['mcap_b'] < hi)
    if all_n < 5: continue
    wr = win_n / all_n * 100
    base = len(winners) / len(all_rows) * 100
    lift = wr / base if base > 0 else 0
    print(f"{lbl:<14} {win_n:>6} {all_n:>6} {wr:>9.1f}% {lift:>5.2f}x")

# Entry-day breakdown (was it ALL one good day?)
print(f"\n{'entry day':<14} {'win_n':>6} {'all_n':>6} {'win_rate':>10}")
print('-' * 50)
for d in sorted(set(r['entry_day'] for r in all_rows)):
    win_n = sum(1 for r in winners if r['entry_day'] == d)
    all_n = sum(1 for r in all_rows if r['entry_day'] == d)
    wr = win_n / all_n * 100 if all_n else 0
    print(f"{d:<14} {win_n:>6} {all_n:>6} {wr:>9.1f}%")

# ---- 3. WHICH FEATURES SEPARATE WINNERS FROM LOSERS? ----
print(f"\n{'='*100}")
print(f"FEATURE MEANS — WINNERS (n={len(winners)}) vs LOSERS (n={len(losers)})")
print(f"{'='*100}")
print(f"{'feature':<22} {'WIN_avg':>10} {'LOSE_avg':>10} {'diff':>8} {'WIN_med':>10} {'LOSE_med':>10}")
print('-' * 75)

def avg(rows, k):
    v = [r[k] for r in rows if isinstance(r.get(k), (int,float))]
    return sum(v)/len(v) if v else None
def med(rows, k):
    v = [r[k] for r in rows if isinstance(r.get(k), (int,float))]
    return statistics.median(v) if v else None

for k in ('beta','mcap_b','today_pct','cum_3d','pct_below_5d_high',
         'iv_today','iv_change_5d','put_call_ratio','call_ask_share',
         'dp_prem_m','last_hr_call_prem_m','otm_call_prem_m',
         'bull_m','bear_m'):
    w_avg, l_avg = avg(winners, k), avg(losers, k)
    w_med, l_med = med(winners, k), med(losers, k)
    if w_avg is None or l_avg is None: continue
    print(f"{k:<22} {w_avg:>10.2f} {l_avg:>10.2f} {w_avg-l_avg:>+8.2f} {w_med:>10.2f} {l_med:>10.2f}")

# Z-score winner vs loser separation (which features differ most?)
print(f"\n{'='*100}")
print(f"FEATURE-LEVEL EDGE — winner mean vs loser mean, normalized by population SD")
print(f"{'='*100}")
sigs = []
for k in ('beta','mcap_b','today_pct','cum_3d','pct_below_5d_high',
         'iv_today','iv_change_5d','put_call_ratio','call_ask_share',
         'dp_prem_m','last_hr_call_prem_m','otm_call_prem_m',
         'bull_m','bear_m'):
    pop = [r[k] for r in all_rows if isinstance(r.get(k), (int,float))]
    if len(pop) < 5: continue
    sd = statistics.pstdev(pop) or 1
    w_avg, l_avg = avg(winners, k), avg(losers, k)
    if w_avg is None or l_avg is None: continue
    z = (w_avg - l_avg) / sd
    sigs.append((k, w_avg, l_avg, z))
sigs.sort(key=lambda x: -abs(x[3]))
print(f"{'feature':<22} {'WIN_avg':>10} {'LOSE_avg':>10} {'z':>7} {'sig':<14}")
print('-' * 70)
for k, wa, la, z in sigs:
    sig = '★★ STRONG' if abs(z) >= 0.4 else '★ moderate' if abs(z) >= 0.2 else 'noise'
    print(f"{k:<22} {wa:>10.2f} {la:>10.2f} {z:>+7.2f}  {sig}")

# ---- 4. Save raw data for later ----
out = {
    'generated_utc': datetime.utcnow().isoformat(timespec='seconds'),
    'snapshots_used': dates,
    'win_threshold_pct': WIN_PCT,
    'all_rows_n': len(all_rows),
    'winners_n': len(winners),
    'winners': winners,
    'feature_z_scores': [{'feature': k, 'win_avg': wa, 'lose_avg': la, 'z': z} for k,wa,la,z in sigs],
}
with open(os.path.join(HERE, 'v4_winners_analysis.json'), 'w') as f:
    json.dump(out, f, indent=2, default=str)
print(f"\nsaved → v4_winners_analysis.json")
