"""
30-DAY BACKTEST + PERMUTATION ANALYSIS for Strategy B (preposition scanner).

Runs the preposition scanner's scoring logic across the last 30 trading days
using HISTORICAL data, then evaluates every permutation of the 5 historically-
replayable scoring components × multiple score thresholds × 200 SMA gate
on/off, and ranks by expectancy and win rate.

Components testable with historical data:
  - compression (Alpaca daily bars)
  - persistent_flow (UW /flow-per-strike?date=YYYY-MM-DD, 5d trailing)
  - darkpool_accumulation (Alpaca bars; internal cluster logic)
  - sector_strength (Alpaca bars vs SPY)
  - squeeze (UW /shorts/{T}/interest-float — bi-monthly snapshots)

NOT testable retroactively (GEX/skew use current-only endpoints):
  - gex_flip
  - skew_flat

Forward returns: stock-based (1D/3D/5D from ref_date open). Direction:
+1 for CALL, -1 for PUT (from persistent_flow.direction; CALL default if disabled).

Output:
  backtest_30day_results.json — raw per-(date,ticker) scores + forward returns
  backtest_30day_summary.json — aggregated results per permutation

Run: python3 engine_v4/backtest_30day.py
Runtime: ~60-90 min (UW rate-limited at 200 req/min).
Resume: re-run continues from last cached entry.
"""
import os
import sys
import json
import time
import itertools
from datetime import datetime, timedelta, timezone
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from v4_preposition_scanner import (
    TIER1_WATCHLIST, fetch_tier2_universe,
    score_compression, score_persistent_flow, score_darkpool,
    score_sector_strength, score_trend_regime,
)
from v4_uw_helpers import squeeze_score
from swing_trade_strategy.data_feed import fetch_alpaca_bars

UTC = timezone.utc
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

# Optional end-date override from CLI (YYYY-MM-DD) so we can run a prior window
# alongside the main one without overwriting caches.
_CLI_END = None
_SUFFIX = ''
for arg in sys.argv[1:]:
    if arg.startswith('--end='):
        _CLI_END = datetime.strptime(arg.split('=',1)[1], '%Y-%m-%d').replace(tzinfo=UTC)
        _SUFFIX = '_' + arg.split('=',1)[1]
    elif arg.startswith('--suffix='):
        _SUFFIX = '_' + arg.split('=',1)[1]

RAW_PATH = os.path.join(BASE_DIR, f'backtest_30day_raw{_SUFFIX}.json')
SUMMARY_PATH = os.path.join(BASE_DIR, f'backtest_30day_summary{_SUFFIX}.json')

LOOKBACK_DAYS = 30           # how many trading days back to test
FWD_HORIZONS = [1, 3, 5]     # forward return horizons in trading days
WIN_THRESHOLD = 0.005        # ±0.5% for win/loss classification


def trading_days(end_date, n):
    """Returns last n trading days ending at end_date (excludes weekends)."""
    days = []
    d = end_date
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return list(reversed(days))


def load_cache():
    if os.path.exists(RAW_PATH):
        with open(RAW_PATH) as f:
            return json.load(f)
    return {"records": []}


def save_cache(cache):
    import tempfile
    fd, tmp = tempfile.mkstemp(dir=BASE_DIR, prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(cache, f, indent=2, default=str)
        os.replace(tmp, RAW_PATH)
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise


def compute_forward_returns(ticker, ref_date):
    """Stock forward returns at +1D/+3D/+5D from ref_date open.
    Uses 1Day bars, takes Open of ref_date as entry, Close of horizon as exit."""
    out = {}
    end_pad = ref_date + timedelta(days=12)  # buffer for weekends
    df = fetch_alpaca_bars(ticker, '1Day',
        ref_date.strftime('%Y-%m-%dT%H:%M:%SZ'),
        end_pad.strftime('%Y-%m-%dT%H:%M:%SZ'))
    if df is None or df.empty or len(df) < 1:
        return {f"{h}D": None for h in FWD_HORIZONS}
    try:
        entry = float(df['Open'].iloc[0])
        for h in FWD_HORIZONS:
            if len(df) > h:
                exit_close = float(df['Close'].iloc[h])
                out[f"{h}D"] = (exit_close - entry) / entry if entry > 0 else None
            else:
                out[f"{h}D"] = None
    except Exception:
        out = {f"{h}D": None for h in FWD_HORIZONS}
    return out


def score_one(ticker, ref_dt, spy_5d):
    """Compute all replayable component scores for (ticker, ref_dt). Returns flat dict."""
    out = {
        'ticker': ticker,
        'date': ref_dt.strftime('%Y-%m-%d'),
        'comp_score': 0, 'flow_score': 0, 'flow_direction': 'CALL',
        'dp_score': 0, 'sector_score': 0, 'squeeze_score_call': 0, 'squeeze_score_put': 0,
        'sma_pass_call': True, 'sma_pass_put': True,
    }
    try:
        c = score_compression(ticker, ref_dt)
        out['comp_score'] = c.get('score', 0)
    except Exception: pass
    try:
        f = score_persistent_flow(ticker, ref_dt)
        out['flow_score'] = f.get('score', 0)
        out['flow_direction'] = f.get('direction', 'CALL')
    except Exception: pass
    try:
        d = score_darkpool(ticker, ref_dt)
        out['dp_score'] = d.get('score', 0)
    except Exception: pass
    try:
        s = score_sector_strength(ticker, ref_dt, spy_5d)
        out['sector_score'] = s.get('score', 0)
    except Exception: pass
    # Squeeze for both directions (CALL gets bonus, PUT gets penalty)
    try:
        sc, _ = squeeze_score(ticker, 'CALL')
        out['squeeze_score_call'] = sc
        sp, _ = squeeze_score(ticker, 'PUT')
        out['squeeze_score_put'] = sp
    except Exception: pass
    # 200 SMA regime — historical
    try:
        tc = score_trend_regime(ticker, 'CALL', ref_dt)
        out['sma_pass_call'] = tc.get('pass', True)
        tp = score_trend_regime(ticker, 'PUT', ref_dt)
        out['sma_pass_put'] = tp.get('pass', True)
    except Exception: pass
    return out


def get_spy_5d(ref_dt):
    df = fetch_alpaca_bars('SPY', '1Day',
        (ref_dt - timedelta(days=10)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        ref_dt.strftime('%Y-%m-%dT%H:%M:%SZ'))
    if df is None or df.empty or len(df) < 5:
        return 0
    try:
        first = float(df['Close'].iloc[-5])
        last = float(df['Close'].iloc[-1])
        return (last - first) / first if first > 0 else 0
    except Exception:
        return 0


def phase1_cache_data():
    """Build the (ticker, date) → scores+returns cache. Resumable."""
    print(f"\n{'='*70}")
    print(f"PHASE 1: Cache historical scores + forward returns")
    print(f"{'='*70}")

    cache = load_cache()
    done_keys = set((r['ticker'], r['date']) for r in cache['records'])
    print(f"Already cached: {len(done_keys)} ticker-days. Resuming.")

    # Universe
    try:
        tier2 = fetch_tier2_universe()
    except Exception:
        tier2 = []
    universe = TIER1_WATCHLIST + [r['ticker'] for r in tier2]
    universe = list(dict.fromkeys(universe))  # dedupe preserving order
    print(f"Universe: {len(universe)} tickers ({len(TIER1_WATCHLIST)} mega + {len(tier2)} dynamic)")

    # Trading days (last 30 ending yesterday so forward returns are computable)
    if _CLI_END:
        end = _CLI_END
    else:
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        end = today - timedelta(days=8)  # buffer so we have 5d forward returns even for last day
    days = trading_days(end, LOOKBACK_DAYS)
    print(f"Date range: {days[0].strftime('%Y-%m-%d')} → {days[-1].strftime('%Y-%m-%d')}")

    # Cache SPY 5d returns per ref_date (used by sector strength)
    spy_cache = {}
    for d in days:
        spy_cache[d.strftime('%Y-%m-%d')] = get_spy_5d(d)

    total = len(days) * len(universe)
    done = len(done_keys)
    print(f"Total ticker-days to process: {total} (already done: {done})\n")

    t0 = time.time()
    for d in days:
        date_str = d.strftime('%Y-%m-%d')
        spy_5d = spy_cache[date_str]
        for t in universe:
            if (t, date_str) in done_keys:
                continue
            try:
                scores = score_one(t, d, spy_5d)
                fwd = compute_forward_returns(t, d)
                scores['fwd_returns'] = fwd
                cache['records'].append(scores)
                done += 1
                if done % 10 == 0:
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed else 0
                    eta_min = (total - done) / rate / 60 if rate else 0
                    print(f"  [{done}/{total}] {t} {date_str} comp={scores['comp_score']} "
                          f"flow={scores['flow_score']} dir={scores['flow_direction']} "
                          f"fwd_5D={scores['fwd_returns'].get('5D')} | ETA {eta_min:.1f} min")
                if done % 50 == 0:
                    save_cache(cache)
            except Exception as e:
                print(f"  ERR {t} {date_str}: {type(e).__name__}: {str(e)[:80]}")
    save_cache(cache)
    print(f"\n✅ Phase 1 done — cached {len(cache['records'])} ticker-days to {RAW_PATH}")
    return cache


def phase2_run_permutations(cache):
    """Replay the cached scores under every permutation of feature toggles + thresholds.
    Pure math — no API calls. Reports top permutations by expectancy."""
    print(f"\n{'='*70}")
    print(f"PHASE 2: Permutation analysis ({len(cache['records'])} ticker-days)")
    print(f"{'='*70}")

    components = ['comp_score', 'flow_score', 'dp_score', 'sector_score']  # squeeze handled separately by direction
    thresholds = [25, 30, 35, 40, 45]
    sma_options = [True, False]
    horizon = '5D'  # primary horizon

    summaries = []

    # Iterate every subset of components (16 combinations), × thresholds × sma
    for size in range(1, len(components) + 1):
        for combo in itertools.combinations(components, size):
            for use_squeeze in [True, False]:
                for use_sma in sma_options:
                    for thresh in thresholds:
                        triggered = []
                        for r in cache['records']:
                            direction = r.get('flow_direction', 'CALL')
                            # 200 SMA gate
                            if use_sma:
                                pass_key = 'sma_pass_call' if direction == 'CALL' else 'sma_pass_put'
                                if not r.get(pass_key, True):
                                    continue
                            # Sum enabled components
                            score = sum(r.get(c, 0) for c in combo)
                            if use_squeeze:
                                sq = r.get('squeeze_score_call', 0) if direction == 'CALL' else r.get('squeeze_score_put', 0)
                                score += sq
                            if score < thresh:
                                continue
                            fwd = (r.get('fwd_returns') or {}).get(horizon)
                            if fwd is None:
                                continue
                            # Sign-adjust for direction
                            sign = 1 if direction == 'CALL' else -1
                            triggered.append(sign * fwd)

                        if not triggered:
                            continue
                        n = len(triggered)
                        wins = sum(1 for r in triggered if r > WIN_THRESHOLD)
                        losses = sum(1 for r in triggered if r < -WIN_THRESHOLD)
                        flat = n - wins - losses
                        avg = sum(triggered) / n
                        win_rate = wins / n if n else 0
                        avg_win = sum(r for r in triggered if r > 0) / max(1, wins)
                        avg_loss = sum(r for r in triggered if r < 0) / max(1, losses)
                        expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

                        summaries.append({
                            'components': '+'.join(combo),
                            'use_squeeze': use_squeeze,
                            'use_sma': use_sma,
                            'threshold': thresh,
                            'n_signals': n,
                            'wins': wins, 'losses': losses, 'flat': flat,
                            'win_rate': round(win_rate, 3),
                            'avg_return': round(avg, 4),
                            'avg_win': round(avg_win, 4),
                            'avg_loss': round(avg_loss, 4),
                            'expectancy': round(expectancy, 4),
                        })

    # Filter to permutations with statistically meaningful sample
    meaningful = [s for s in summaries if s['n_signals'] >= 10]
    print(f"Total permutations evaluated: {len(summaries)}")
    print(f"With n>=10: {len(meaningful)}")

    # Top by expectancy
    print(f"\n--- TOP 15 BY EXPECTANCY (5D forward, n>=10) ---")
    print(f"{'components':<35} {'sq':<3} {'sma':<4} {'th':<3} {'n':>4} {'win%':>6} {'expctcy':>9} {'avg_w':>8} {'avg_l':>8}")
    for s in sorted(meaningful, key=lambda x: -x['expectancy'])[:15]:
        sq = 'Y' if s['use_squeeze'] else '·'
        sma = 'Y' if s['use_sma'] else '·'
        print(f"  {s['components']:<33} {sq:<3} {sma:<4} {s['threshold']:<3} {s['n_signals']:>4} "
              f"{s['win_rate']*100:>5.1f}% {s['expectancy']*100:>+8.2f}% "
              f"{s['avg_win']*100:>+7.2f}% {s['avg_loss']*100:>+7.2f}%")

    print(f"\n--- TOP 15 BY WIN RATE (5D forward, n>=20) ---")
    high_n = [s for s in meaningful if s['n_signals'] >= 20]
    for s in sorted(high_n, key=lambda x: -x['win_rate'])[:15]:
        sq = 'Y' if s['use_squeeze'] else '·'
        sma = 'Y' if s['use_sma'] else '·'
        print(f"  {s['components']:<33} {sq:<3} {sma:<4} {s['threshold']:<3} {s['n_signals']:>4} "
              f"{s['win_rate']*100:>5.1f}% {s['expectancy']*100:>+8.2f}% "
              f"{s['avg_win']*100:>+7.2f}% {s['avg_loss']*100:>+7.2f}%")

    # Current production config (compression + flow + dp + sector + squeeze + SMA, threshold 40)
    prod = next((s for s in summaries if s['components'] == 'comp_score+flow_score+dp_score+sector_score'
                 and s['use_squeeze'] and s['use_sma'] and s['threshold'] == 40), None)
    print(f"\n--- CURRENT PRODUCTION CONFIG ---")
    if prod:
        print(f"  comp+flow+dp+sector + squeeze + SMA, threshold=40")
        print(f"  n={prod['n_signals']}, wins={prod['wins']}, losses={prod['losses']}, "
              f"win_rate={prod['win_rate']*100:.1f}%, expectancy={prod['expectancy']*100:+.2f}%")
    else:
        print(f"  (no signals matched production config — too restrictive on this sample)")

    # Save full summary
    payload = {
        'generated_utc': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'lookback_days': LOOKBACK_DAYS,
        'horizon': horizon,
        'total_records_cached': len(cache['records']),
        'permutations_with_n_ge_10': len(meaningful),
        'production_config': prod,
        'top_by_expectancy_n10': sorted(meaningful, key=lambda x: -x['expectancy'])[:25],
        'top_by_win_rate_n20': sorted(high_n, key=lambda x: -x['win_rate'])[:25],
        'all_permutations': summaries,
    }
    with open(SUMMARY_PATH, 'w') as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\n💾 Full summary: {SUMMARY_PATH}")


if __name__ == "__main__":
    cache = phase1_cache_data()
    phase2_run_permutations(cache)
