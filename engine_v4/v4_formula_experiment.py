"""
V4 FORMULA EXPERIMENT — does the v7 staging score actually have edge?

Re-runs v7 + 3 variants on the 6 historical snapshots we have (Apr 22-29)
and compares each variant's forward 1d/3d hit rate to the 32.1% universe
baseline (P(any ticker × any day → +5% in 3d) for our universe).

Variants:
  V7_ORIGINAL    — full formula as shipped
  V7_NO_STRUCT   — drop +85 beta and +70 Tech sector bonuses (flow-only)
  V7_COLLAPSED_IV — keep only `iv_today >= 80`, drop iv_change_5d /
                    iv_rv_ratio / rv_pct_60d (de-dup the IV property)
  V7_LOOSE_GATES — remove `had_recent_5pct` hard gate (allow continuations)

For each entry-day D and ticker T:
  entry_close = snap_D.dailyBar.c
  fwd1_close  = snap_D+1.dailyBar.c
  fwd3_close  = snap_D+3.dailyBar.c (where snap_D+3 means 3 trading days
                ahead — uses the next available snap if exact day missing)
  win = forward return >= +5%

Universe baseline: 32.1% (P(any ticker hits +5% in 3d) over our universe).
A variant only beats the system if its hit rate at score>=300 is materially
above 32.1% AND has decent sample size.

Run: python3 v4_formula_experiment.py
Output: v4_formula_experiment.json
"""
import os, json, glob, sys
from collections import defaultdict
from statistics import mean

HERE = os.path.dirname(os.path.abspath(__file__))
SNAP_GLOB = os.path.join(HERE, 'v4_snapshot_*.json')
OUT = os.path.join(HERE, 'v4_formula_experiment.json')

# Same threshold as the live scanner
SCORE_THRESHOLD = 300
WIN_PCT = 5.0  # forward return >= +5% counts as a hit


def _safe(d, *keys, default=None):
    cur = d
    for k in keys:
        if not isinstance(cur, dict): return default
        cur = cur.get(k)
        if cur is None: return default
    return cur


def date_from_snap_path(p):
    base = os.path.basename(p)
    return base.replace('v4_snapshot_', '').replace('.json', '')


def load_uw_signals_for(date_str):
    """Load v4_uw_signals_<date>.json if present — these add the iv_rv_ratio /
    rv_pct_60d / gamma_compression bonuses. Missing → empty dict (graceful)."""
    p = os.path.join(HERE, f'v4_uw_signals_{date_str}.json')
    if not os.path.exists(p): return {}
    try:
        d = json.load(open(p))
        return d.get('per_ticker') or {}
    except Exception:
        return {}


# ---------- feature extraction (mirror of v4_staging_scanner.extract_features
# but using only one snapshot's frozen state — no live overlay, no live WS) ----------

def extract_features(ticker, blob, hist_closes):
    """Extract v7 features from a frozen snapshot blob + historical close list.

    hist_closes: list of {'date': 'YYYY-MM-DD', 'c': float, 'h': float}
                 in chronological order; LAST entry is today.
    """
    if not blob or not hist_closes or len(hist_closes) < 4:
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
    today_pct = (closes[-1] - closes[-2]) / closes[-2] * 100 if closes[-2] else 0
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
    }


# ---------- 4 score variants ----------

def score_v7_original(f, uw=None):
    if not f: return 0
    if f.get('had_recent_5pct'): return 0
    if (f.get('today_pct') or 0) >= 7: return 0
    s = 0
    if (f.get('dp_prem_m') or 0) >= 100: s += 19
    if (f.get('iv_today') or 0) >= 80: s += 50
    if (f.get('put_call_ratio') or 0) >= 1.5: s += 87
    if (f.get('call_ask_share') or 0) >= 0.55: s += 38
    if (f.get('beta') or 0) >= 2.0: s += 85
    if (f.get('iv_change_5d') or 0) >= 10: s += 36
    if f.get('sector') == 'Technology': s += 70
    cum_3d = f.get('cum_3d') or 0
    if 0 <= cum_3d <= 10: s += 95
    if 1 <= (f.get('pct_below_5d_high') or 0) <= 5: s += 30
    if (f.get('last_hr_call_prem_m') or 0) >= 5 and (f.get('otm_call_prem_m') or 0) >= 1:
        s += 50
    elif (f.get('last_hr_call_prem_m') or 0) >= 5:
        s += 12
    if (f.get('iv_today') or 0) <= 30: s -= 69
    if (f.get('today_pct') or 0) >= 5: s -= 15
    if uw:
        if uw.get('vr_iv_cheap'): s += 32
        if uw.get('vr_rv_high_regime'): s += 17
        if uw.get('ge_gamma_compression'): s += 10
    return s


def score_v7_no_struct(f, uw=None):
    """Drop +85 beta and +70 Tech bonuses — flow-only edge test."""
    if not f: return 0
    if f.get('had_recent_5pct'): return 0
    if (f.get('today_pct') or 0) >= 7: return 0
    s = 0
    if (f.get('dp_prem_m') or 0) >= 100: s += 19
    if (f.get('iv_today') or 0) >= 80: s += 50
    if (f.get('put_call_ratio') or 0) >= 1.5: s += 87
    if (f.get('call_ask_share') or 0) >= 0.55: s += 38
    # SKIP: beta, sector
    if (f.get('iv_change_5d') or 0) >= 10: s += 36
    cum_3d = f.get('cum_3d') or 0
    if 0 <= cum_3d <= 10: s += 95
    if 1 <= (f.get('pct_below_5d_high') or 0) <= 5: s += 30
    if (f.get('last_hr_call_prem_m') or 0) >= 5 and (f.get('otm_call_prem_m') or 0) >= 1:
        s += 50
    elif (f.get('last_hr_call_prem_m') or 0) >= 5:
        s += 12
    if (f.get('iv_today') or 0) <= 30: s -= 69
    if (f.get('today_pct') or 0) >= 5: s -= 15
    if uw:
        if uw.get('vr_iv_cheap'): s += 32
        if uw.get('vr_rv_high_regime'): s += 17
        if uw.get('ge_gamma_compression'): s += 10
    return s


def score_v7_collapsed_iv(f, uw=None):
    """Keep only iv_today >= 80; drop iv_change_5d, vr_iv_cheap, vr_rv_high_regime
    — IV-property de-dup test (those four signals are correlated)."""
    if not f: return 0
    if f.get('had_recent_5pct'): return 0
    if (f.get('today_pct') or 0) >= 7: return 0
    s = 0
    if (f.get('dp_prem_m') or 0) >= 100: s += 19
    if (f.get('iv_today') or 0) >= 80: s += 50
    if (f.get('put_call_ratio') or 0) >= 1.5: s += 87
    if (f.get('call_ask_share') or 0) >= 0.55: s += 38
    if (f.get('beta') or 0) >= 2.0: s += 85
    # SKIP: iv_change_5d
    if f.get('sector') == 'Technology': s += 70
    cum_3d = f.get('cum_3d') or 0
    if 0 <= cum_3d <= 10: s += 95
    if 1 <= (f.get('pct_below_5d_high') or 0) <= 5: s += 30
    if (f.get('last_hr_call_prem_m') or 0) >= 5 and (f.get('otm_call_prem_m') or 0) >= 1:
        s += 50
    elif (f.get('last_hr_call_prem_m') or 0) >= 5:
        s += 12
    if (f.get('iv_today') or 0) <= 30: s -= 69
    if (f.get('today_pct') or 0) >= 5: s -= 15
    # SKIP: vr_iv_cheap, vr_rv_high_regime; KEEP gamma_compression (different property)
    if uw:
        if uw.get('ge_gamma_compression'): s += 10
    return s


def score_v7_loose_gates(f, uw=None):
    """Remove `had_recent_5pct` hard gate — allow continuations into the score."""
    if not f: return 0
    # SKIP: had_recent_5pct gate
    if (f.get('today_pct') or 0) >= 7: return 0
    s = 0
    if (f.get('dp_prem_m') or 0) >= 100: s += 19
    if (f.get('iv_today') or 0) >= 80: s += 50
    if (f.get('put_call_ratio') or 0) >= 1.5: s += 87
    if (f.get('call_ask_share') or 0) >= 0.55: s += 38
    if (f.get('beta') or 0) >= 2.0: s += 85
    if (f.get('iv_change_5d') or 0) >= 10: s += 36
    if f.get('sector') == 'Technology': s += 70
    cum_3d = f.get('cum_3d') or 0
    if 0 <= cum_3d <= 10: s += 95
    if 1 <= (f.get('pct_below_5d_high') or 0) <= 5: s += 30
    if (f.get('last_hr_call_prem_m') or 0) >= 5 and (f.get('otm_call_prem_m') or 0) >= 1:
        s += 50
    elif (f.get('last_hr_call_prem_m') or 0) >= 5:
        s += 12
    if (f.get('iv_today') or 0) <= 30: s -= 69
    if (f.get('today_pct') or 0) >= 5: s -= 15
    if uw:
        if uw.get('vr_iv_cheap'): s += 32
        if uw.get('vr_rv_high_regime'): s += 17
        if uw.get('ge_gamma_compression'): s += 10
    return s


VARIANTS = [
    ('V7_ORIGINAL', score_v7_original),
    ('V7_NO_STRUCT', score_v7_no_struct),
    ('V7_COLLAPSED_IV', score_v7_collapsed_iv),
    ('V7_LOOSE_GATES', score_v7_loose_gates),
]


# ---------- main backtest loop ----------

def main():
    snap_paths = sorted(glob.glob(SNAP_GLOB))
    if not snap_paths:
        print('no snapshots found', file=sys.stderr); return 1

    # Load each snapshot once → {date: {ticker: blob}}
    snaps = {}
    for p in snap_paths:
        d = date_from_snap_path(p)
        if not d: continue
        try:
            with open(p) as f:
                snaps[d] = (json.load(f).get('data') or {})
        except Exception as e:
            print(f'skip {p}: {e}', file=sys.stderr)
    dates = sorted(snaps.keys())
    if not dates:
        print('no snapshots could be loaded', file=sys.stderr); return 1
    print(f'loaded {len(dates)} snapshots: {dates}', file=sys.stderr)

    # Build daily-bars history per ticker from ALL snapshots' dailyBar fields.
    # Each ticker gets a list of {date, c, h, o} entries.
    bars_by_ticker = defaultdict(list)
    for d in dates:
        for tkr, blob in snaps[d].items():
            db = (blob.get('alpaca_snapshot') or {}).get('dailyBar') or {}
            c = db.get('c')
            if c is None: continue
            try:
                bars_by_ticker[tkr].append({
                    'date': d,
                    'c': float(c),
                    'h': float(db.get('h') or c),
                    'o': float(db.get('o') or c),
                })
            except Exception: pass

    # ALSO seed earlier history from prevDailyBar in the FIRST snapshot we have
    # (gives us 1 extra day of history at the start).
    first_d = dates[0]
    for tkr, blob in snaps[first_d].items():
        pdb = (blob.get('alpaca_snapshot') or {}).get('prevDailyBar') or {}
        c = pdb.get('c')
        if c is None: continue
        try:
            from datetime import datetime, timedelta
            dt = datetime.strptime(first_d, '%Y-%m-%d') - timedelta(days=1)
            # crude: just call it "prev_<first_d>" so date sort still works
            bars_by_ticker[tkr].insert(0, {
                'date': dt.strftime('%Y-%m-%d'),
                'c': float(c),
                'h': float(pdb.get('h') or c),
                'o': float(pdb.get('o') or c),
            })
        except Exception: pass

    # Per (variant, horizon): list of {ticker, day, score, ret, win}
    results_by_variant = {v[0]: {'1d': [], '3d': []} for v in VARIANTS}

    # For each entry day where we ALSO have ≥1 day of history AND ≥1 forward day:
    for i, d in enumerate(dates):
        if i < 1: continue  # need ≥1 prior day for cum_3d-style features
        if i >= len(dates) - 1: continue  # need ≥1 forward day
        uw = load_uw_signals_for(d)
        ts = snaps[d]
        for tkr, blob in ts.items():
            # Build hist closes ≤ d (chronological)
            hist = [b for b in bars_by_ticker.get(tkr, []) if b['date'] <= d]
            if len(hist) < 4: continue
            f = extract_features(tkr, blob, hist)
            if not f: continue

            entry_close = f['today_close']
            if not entry_close or entry_close <= 0: continue

            # Forward 1d / 3d closes
            def close_at(idx):
                if idx >= len(dates): return None
                fd = dates[idx]
                fblob = snaps[fd].get(tkr)
                if not fblob: return None
                fc = ((fblob.get('alpaca_snapshot') or {}).get('dailyBar') or {}).get('c')
                try: return float(fc) if fc is not None else None
                except: return None
            fwd1 = close_at(i + 1)
            fwd3 = close_at(i + 3)

            ret1 = (fwd1 - entry_close) / entry_close * 100 if fwd1 else None
            ret3 = (fwd3 - entry_close) / entry_close * 100 if fwd3 else None

            for vname, vfunc in VARIANTS:
                score = vfunc(f, uw=uw.get(tkr))
                rec_base = {'ticker': tkr, 'day': d, 'score': score}
                if ret1 is not None:
                    results_by_variant[vname]['1d'].append({
                        **rec_base, 'ret_pct': ret1, 'win': ret1 >= WIN_PCT,
                    })
                if ret3 is not None:
                    results_by_variant[vname]['3d'].append({
                        **rec_base, 'ret_pct': ret3, 'win': ret3 >= WIN_PCT,
                    })

    # Aggregate per variant + horizon
    summary = {}
    for vname, _ in VARIANTS:
        v_summary = {}
        for hz in ('1d', '3d'):
            recs = results_by_variant[vname][hz]
            picks = [r for r in recs if r['score'] >= SCORE_THRESHOLD]
            v_summary[hz] = {
                'all_n': len(recs),
                'all_win_rate': round(sum(1 for r in recs if r['win']) / len(recs) * 100, 1) if recs else None,
                'all_avg_ret': round(mean(r['ret_pct'] for r in recs), 2) if recs else None,

                'picks_n': len(picks),
                'picks_at_threshold': SCORE_THRESHOLD,
                'picks_win_rate': round(sum(1 for r in picks if r['win']) / len(picks) * 100, 1) if picks else None,
                'picks_avg_ret': round(mean(r['ret_pct'] for r in picks), 2) if picks else None,
                'picks_top5': sorted(picks, key=lambda r: -r['score'])[:5],
            }
        summary[vname] = v_summary

    out = {
        'generated_at_utc': __import__('datetime').datetime.utcnow().isoformat(timespec='seconds'),
        'snapshots_used': dates,
        'win_threshold_pct': WIN_PCT,
        'score_threshold': SCORE_THRESHOLD,
        'baseline_3d_pct': 32.1,  # measured universe-wide P(+5% in 3d)
        'summary': summary,
    }
    with open(OUT, 'w') as f:
        json.dump(out, f, indent=2, default=str)

    # Console table
    print(f"\n{'='*100}")
    print(f"V4 FORMULA EXPERIMENT — 4 variants × {len(dates)} snapshots ({dates[0]} → {dates[-1]})")
    print(f"Universe baseline: 32.1% hit (any ticker × any day → +5% in 3d)")
    print(f"{'='*100}\n")
    print(f"{'Variant':<20} {'horiz':<5} {'all n':>7} {'all win%':>10} {'all avg':>9} | {'picks n':>8} {'picks win%':>11} {'picks avg':>10} {'edge vs base':>14}")
    print('-' * 110)
    for vname, _ in VARIANTS:
        for hz in ('1d', '3d'):
            s = summary[vname][hz]
            base = 32.1 if hz == '3d' else None
            edge = (f"+{s['picks_win_rate']-base:.1f} pp"
                    if (s['picks_win_rate'] is not None and base is not None) else '—')
            wr = f"{s['all_win_rate']:.1f}%" if s['all_win_rate'] is not None else '—'
            ar = f"{s['all_avg_ret']:+.2f}%" if s['all_avg_ret'] is not None else '—'
            pwr = f"{s['picks_win_rate']:.1f}%" if s['picks_win_rate'] is not None else '—'
            par = f"{s['picks_avg_ret']:+.2f}%" if s['picks_avg_ret'] is not None else '—'
            print(f"{vname:<20} {hz:<5} {s['all_n']:>7} {wr:>10} {ar:>9} | {s['picks_n']:>8} {pwr:>11} {par:>10} {edge:>14}")
    print(f"\nSaved → {OUT}")
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
