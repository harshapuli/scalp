"""
OPTION-PREMIUM OVERLAY for the 30-day backtest.

Reads backtest_30day_raw.json. For each ticker-date record, finds a
realistic ATM contract (~21 DTE) for that date and measures option
premium returns at 1D/3D/5D. Saves enriched cache + re-runs Phase 2
of the permutation analysis on option-basis returns.

For illiquid contracts (no Alpaca option bars), falls back to stock
returns and marks return_basis='stock_fallback'. In live trading those
names would also be hard to fill, so flagging them separately is honest.
"""
import os, sys, json, time, requests
from datetime import datetime, timedelta, timezone
import itertools
import statistics

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', '.env'))

from swing_trade_strategy.data_feed import fetch_alpaca_bars

UTC = timezone.utc
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
RAW_PATH = os.path.join(BASE_DIR, 'backtest_30day_raw.json')
OPT_CACHE_PATH = os.path.join(BASE_DIR, 'backtest_30day_option_pnl.json')
OPT_SUMMARY_PATH = os.path.join(BASE_DIR, 'backtest_30day_option_summary.json')

ALPACA_DATA_URL = os.getenv("ALPACA_DATA_URL", "https://data.alpaca.markets")
H = {"APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
     "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", "")}

TARGET_DTE = 21
HORIZONS = [1, 3, 5]
WIN_THRESHOLD = 0.05     # ±5% on option premium (not 0.5% — option moves are bigger)
MIN_SCORE_TO_ENRICH = 15 # below this, no permutation would trigger; skip to save API calls


def build_occ_symbol(ticker: str, exp_date: datetime, call_put: str, strike: float) -> str:
    """OCC format: TICKERYYMMDD[C/P]XXXXXXXX (strike × 1000, 8-digit padded)."""
    yymmdd = exp_date.strftime("%y%m%d")
    cp = 'C' if call_put == 'CALL' else 'P'
    strike_int = int(round(strike * 1000))
    return f"{ticker}{yymmdd}{cp}{strike_int:08d}"


def nearest_atm_strike(spot: float) -> float:
    """Round spot to a common strike increment. $0.50 for <$100, $1 for $100-300, $5 for $300-500, $10 for >$500."""
    if spot < 25: return round(spot * 2) / 2     # $0.50 increments
    if spot < 100: return round(spot)             # $1
    if spot < 300: return round(spot)             # $1
    if spot < 500: return round(spot / 5) * 5     # $5
    return round(spot / 10) * 10                  # $10


def fetch_option_bars(symbol: str, start: datetime, end: datetime, timeframe: str = '1Day') -> list:
    try:
        r = requests.get(
            f"{ALPACA_DATA_URL}/v1beta1/options/bars",
            headers=H,
            params={
                "symbols": symbol, "timeframe": timeframe,
                "start": start.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "end": end.strftime('%Y-%m-%dT%H:%M:%SZ'),
                "limit": 100,
            },
            timeout=10,
        )
        if r.status_code != 200: return []
        return r.json().get('bars', {}).get(symbol, []) or []
    except Exception:
        return []


def third_friday(year: int, month: int) -> datetime:
    """Third Friday of month — standard monthly option expiry."""
    d = datetime(year, month, 1, tzinfo=UTC)
    # Find first Friday
    while d.weekday() != 4:
        d += timedelta(days=1)
    # Add 14 days to get third Friday
    return d + timedelta(days=14)


def next_monthly_expiry(ref_date: datetime, target_dte: int) -> datetime:
    """Pick the MONTHLY 3rd-Friday expiry ≥ target_dte out.
    Alpaca historical option bars only cover monthlies, not weeklies — weekly
    contracts return empty bars even though they trade live."""
    target_date = ref_date + timedelta(days=target_dte)
    # Start with this month's 3rd Friday; if already past target, go next month
    y, m = target_date.year, target_date.month
    candidate = third_friday(y, m)
    while candidate < target_date:
        m += 1
        if m > 12: m = 1; y += 1
        candidate = third_friday(y, m)
    return candidate


# Keep old name as alias for backward compat
next_friday = next_monthly_expiry


def compute_option_pnl(ticker: str, ref_date: datetime, direction: str) -> dict:
    """Returns {1D: ret, 3D: ret, 5D: ret, return_basis, contract_symbol}.
    ret = (exit_mid - entry_mid) / entry_mid for the direction's contract."""
    out = {'1D': None, '3D': None, '5D': None, 'return_basis': 'no_data',
           'contract_symbol': None, 'entry_premium': None}

    # Stock close at ref_date (proxy for spot on that day)
    df_stock = fetch_alpaca_bars(ticker, '1Day',
        (ref_date - timedelta(days=3)).strftime('%Y-%m-%dT%H:%M:%SZ'),
        ref_date.strftime('%Y-%m-%dT%H:%M:%SZ'))
    if df_stock is None or df_stock.empty:
        return out
    try:
        spot = float(df_stock['Close'].iloc[-1])
    except Exception:
        return out

    strike = nearest_atm_strike(spot)
    exp = next_friday(ref_date, TARGET_DTE)
    sym = build_occ_symbol(ticker, exp, direction, strike)
    out['contract_symbol'] = sym

    # Fetch contract bars from ref_date to ref_date + 10d (cover weekends)
    bars = fetch_option_bars(sym, ref_date, ref_date + timedelta(days=10))
    if not bars:
        out['return_basis'] = 'no_option_bars'
        return out

    # Entry = close of ref_date (or next available bar if ref is a holiday)
    entry_bar = None
    ref_str = ref_date.strftime('%Y-%m-%d')
    for b in bars:
        if b.get('t', '')[:10] >= ref_str:
            entry_bar = b; break
    if not entry_bar:
        out['return_basis'] = 'no_entry_bar'
        return out

    try:
        entry = float(entry_bar.get('c', 0))
    except Exception:
        return out
    if entry <= 0:
        return out

    out['entry_premium'] = entry
    out['return_basis'] = 'option_premium'

    # Exits at +1D, +3D, +5D (use close of those days)
    # Bars sorted ascending; pick bar at index 1, 3, 5 from entry_bar position
    try:
        start_idx = bars.index(entry_bar)
        for h in HORIZONS:
            idx = start_idx + h
            if idx < len(bars):
                exit_price = float(bars[idx].get('c', 0))
                if exit_price > 0:
                    out[f'{h}D'] = (exit_price - entry) / entry
    except Exception:
        pass

    return out


def enrich_cache():
    print(f"\n{'='*70}")
    print(f"PHASE 3: Enrich cache with option-premium returns")
    print(f"{'='*70}")

    with open(RAW_PATH) as f:
        raw = json.load(f)

    # Resume support
    if os.path.exists(OPT_CACHE_PATH):
        with open(OPT_CACHE_PATH) as f:
            enriched = json.load(f)
    else:
        enriched = {'records': []}

    done_keys = set((r['ticker'], r['date']) for r in enriched['records'])
    print(f"Loaded {len(raw['records'])} raw records.")
    print(f"Already enriched: {len(done_keys)}. Resuming.")

    # Filter: only enrich records that COULD trigger in at least one permutation.
    # Sum of all scoring components — if below MIN_SCORE_TO_ENRICH, skip.
    to_enrich = []
    for r in raw['records']:
        total = (r.get('comp_score', 0) + r.get('flow_score', 0)
                 + r.get('dp_score', 0) + r.get('sector_score', 0))
        if total < MIN_SCORE_TO_ENRICH:
            continue
        if (r['ticker'], r['date']) in done_keys:
            continue
        to_enrich.append(r)
    print(f"To enrich: {len(to_enrich)} records with cumulative score >= {MIN_SCORE_TO_ENRICH}\n")

    t0 = time.time()
    for i, r in enumerate(to_enrich, 1):
        t = r['ticker']
        date_str = r['date']
        direction = r.get('flow_direction', 'CALL')
        ref_dt = datetime.strptime(date_str, '%Y-%m-%d').replace(tzinfo=UTC)
        opt = compute_option_pnl(t, ref_dt, direction)
        enriched_rec = dict(r)
        enriched_rec['option_returns'] = opt
        enriched['records'].append(enriched_rec)

        if i % 10 == 0:
            elapsed = time.time() - t0
            rate = i / elapsed if elapsed else 0
            eta = (len(to_enrich) - i) / rate / 60 if rate else 0
            basis_counts = {}
            for er in enriched['records']:
                b = (er.get('option_returns') or {}).get('return_basis', 'n/a')
                basis_counts[b] = basis_counts.get(b, 0) + 1
            print(f"  [{i}/{len(to_enrich)}] {t} {date_str} {direction} "
                  f"{opt.get('return_basis')} 5D={opt.get('5D')} | ETA {eta:.1f} min | "
                  f"basis: {dict(list(basis_counts.items())[:5])}")

        if i % 50 == 0:
            with open(OPT_CACHE_PATH, 'w') as f:
                json.dump(enriched, f, indent=2, default=str)

    with open(OPT_CACHE_PATH, 'w') as f:
        json.dump(enriched, f, indent=2, default=str)
    print(f"\n✅ Enriched cache saved: {OPT_CACHE_PATH}")
    return enriched


def run_option_permutations(enriched):
    """Same permutation analysis as Phase 2 but using option-premium returns."""
    print(f"\n{'='*70}")
    print(f"PHASE 4: Permutation analysis on OPTION returns ({len(enriched['records'])} records)")
    print(f"{'='*70}")

    # Keep only records with real option bars
    usable = [r for r in enriched['records']
              if (r.get('option_returns') or {}).get('return_basis') == 'option_premium']
    print(f"Records with valid option bars: {len(usable)}")

    components = ['comp_score', 'flow_score', 'dp_score', 'sector_score']
    thresholds = [25, 30, 35, 40, 45]
    horizon = '5D'

    summaries = []
    for size in range(1, len(components) + 1):
        for combo in itertools.combinations(components, size):
            for use_squeeze in [True, False]:
                for use_sma in [True, False]:
                    for thresh in thresholds:
                        triggered = []
                        for r in usable:
                            direction = r.get('flow_direction', 'CALL')
                            if use_sma:
                                pass_key = 'sma_pass_call' if direction == 'CALL' else 'sma_pass_put'
                                if not r.get(pass_key, True): continue
                            score = sum(r.get(c, 0) for c in combo)
                            if use_squeeze:
                                sq = r.get('squeeze_score_call', 0) if direction == 'CALL' else r.get('squeeze_score_put', 0)
                                score += sq
                            if score < thresh: continue
                            ret = (r.get('option_returns') or {}).get(horizon)
                            if ret is None: continue
                            # Option premium returns are already directional — no sign flip needed
                            # (the option contract IS the directional bet)
                            triggered.append(ret)

                        if not triggered: continue
                        n = len(triggered)
                        wins = sum(1 for x in triggered if x > WIN_THRESHOLD)
                        losses = sum(1 for x in triggered if x < -WIN_THRESHOLD)
                        avg = sum(triggered) / n
                        win_rate = wins / n if n else 0
                        avg_win = sum(x for x in triggered if x > 0) / max(1, sum(1 for x in triggered if x > 0))
                        avg_loss = sum(x for x in triggered if x < 0) / max(1, sum(1 for x in triggered if x < 0))
                        expectancy = (win_rate * avg_win) + ((1 - win_rate) * avg_loss)

                        summaries.append({
                            'components': '+'.join(combo),
                            'use_squeeze': use_squeeze, 'use_sma': use_sma,
                            'threshold': thresh, 'n_signals': n,
                            'wins': wins, 'losses': losses,
                            'win_rate': round(win_rate, 3),
                            'avg_return': round(avg, 4),
                            'avg_win': round(avg_win, 4),
                            'avg_loss': round(avg_loss, 4),
                            'expectancy': round(expectancy, 4),
                        })

    meaningful = [s for s in summaries if s['n_signals'] >= 10]
    print(f"\nPermutations with n>=10: {len(meaningful)}")

    print(f"\n--- TOP 15 BY OPTION-PREMIUM EXPECTANCY (5D, n>=10) ---")
    print(f"{'components':<38} {'sq':>3} {'sma':>4} {'th':>3} {'n':>4} {'win%':>6} {'expct':>8} {'avgW':>8} {'avgL':>8}")
    for r in sorted(meaningful, key=lambda x: -x['expectancy'])[:15]:
        sq = 'Y' if r['use_squeeze'] else '·'
        sm = 'Y' if r['use_sma'] else '·'
        print(f"{r['components']:<38} {sq:>3} {sm:>4} {r['threshold']:>3} {r['n_signals']:>4} "
              f"{r['win_rate']*100:>5.1f}% {r['expectancy']*100:>+7.2f}% "
              f"{r['avg_win']*100:>+7.2f}% {r['avg_loss']*100:>+7.2f}%")

    # Production config on option basis
    prod = next((s for s in summaries
                 if s['components'] == 'comp_score+flow_score+dp_score+sector_score'
                 and s['use_squeeze'] and s['use_sma'] and s['threshold'] == 40), None)
    print(f"\n--- PRODUCTION CONFIG (option basis) ---")
    if prod:
        print(f"  n={prod['n_signals']}, win_rate={prod['win_rate']*100:.1f}%, "
              f"expectancy={prod['expectancy']*100:+.2f}%, avgW={prod['avg_win']*100:+.2f}%, avgL={prod['avg_loss']*100:+.2f}%")

    payload = {
        'generated_utc': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
        'return_basis': 'option_premium',
        'horizon': horizon,
        'records_with_option_bars': len(usable),
        'records_enriched_total': len(enriched['records']),
        'production_config': prod,
        'top_by_expectancy_n10': sorted(meaningful, key=lambda x: -x['expectancy'])[:25],
        'all_permutations': summaries,
    }
    with open(OPT_SUMMARY_PATH, 'w') as f:
        json.dump(payload, f, indent=2, default=str)
    print(f"\n💾 Option-basis summary: {OPT_SUMMARY_PATH}")


if __name__ == "__main__":
    enriched = enrich_cache()
    run_option_permutations(enriched)
