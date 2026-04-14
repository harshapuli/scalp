"""
PHASE 2+3: LOAD SCALING + STRESS TEST — Find the Breaking Point
=================================================================
Phase 2: Load test with 10-30 random tickers
Phase 3: Scale until failure (tickers, bars, strategies, burst, memory)

Run: python test_stress.py
"""
import sys
import os
import time
import json
import tracemalloc
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

RESULTS = {}


# ============================================================
# HELPERS
# ============================================================

def make_gbm_df(n, seed=42, start_price=500.0):
    np.random.seed(seed)
    dt = 1.0 / (252 * 390)
    drift = 0.0001
    vol = 0.015
    returns = np.exp((drift - 0.5 * vol**2) * dt + vol * np.sqrt(dt) * np.random.randn(n))
    close = start_price * np.cumprod(returns)
    noise = np.random.rand(n)
    spread = close * 0.001
    high = close + spread * (0.5 + noise)
    low = close - spread * (0.5 + np.random.rand(n))
    opn = close * (1 + np.random.randn(n) * 0.0003)
    high = np.maximum(high, np.maximum(close, opn))
    low = np.minimum(low, np.minimum(close, opn))
    dates = pd.date_range('2024-01-02 09:30', periods=n, freq='1min')
    return pd.DataFrame({
        'Date': dates[:n], 'Open': opn, 'High': high, 'Low': low,
        'Close': close, 'Volume': np.random.randint(500000, 8000000, n),
        'NumTrades': np.random.randint(1000, 15000, n),
    })


def random_bar(base_price=500):
    c = base_price + np.random.randn() * 0.3
    return pd.DataFrame([{
        'Date': pd.Timestamp.now().tz_localize(None),
        'Open': c - 0.1, 'High': c + 0.2, 'Low': c - 0.2, 'Close': c,
        'Volume': np.random.randint(500000, 3000000),
        'NumTrades': np.random.randint(1000, 8000),
    }])


FAKE_TICKERS = [
    'SPY','QQQ','AAPL','TSLA','NVDA','PLTR','GOOGL','AMZN','META','MSFT',
    'AMD','INTC','NFLX','DIS','BA','JPM','GS','V','MA','WMT',
    'HD','LOW','COST','TGT','PEP','KO','MCD','SBUX','NKE','LULU',
]


# ============================================================
# PHASE 2A: SCALE TESTING (10 → 30 tickers)
# ============================================================

def test_2a_scale():
    print("\n═══════ 2A. SCALE TESTING (10 → 30 tickers) ═══════")
    from indicators import compute_all_indicators, incremental_update
    from signals import detect_signals
    from v3_filters import detect_liquidity_levels, MarketModeDetector
    from config import STRATEGIES

    mode_det = MarketModeDetector()

    print(f"  {'Tickers':>8} {'Build(s)':>8} {'Scan(ms)':>10} {'Per-tkr':>8} {'Mem(MB)':>8}")
    print(f"  {'─'*8} {'─'*8} {'─'*10} {'─'*8} {'─'*8}")

    scale_results = []

    for n_tickers in [10, 15, 20, 25, 30]:
        tickers = FAKE_TICKERS[:n_tickers]

        # Build DataFrames
        tracemalloc.start()
        t0 = time.time()
        data = {}
        for i, ticker in enumerate(tickers):
            data[ticker] = compute_all_indicators(make_gbm_df(2000, seed=42+i), include_whale=True)
        build_time = time.time() - t0

        # Simulate 1 minute of trading: all tickers get new bar + scan
        t0 = time.time()
        for ticker in tickers:
            df = data[ticker]
            new_bar = random_bar(df['Close'].iloc[-1])
            df = pd.concat([df, new_bar], ignore_index=True)
            df = incremental_update(df, include_whale=True)

            sweep_b, sweep_e = detect_liquidity_levels(df)
            mode = mode_det.determine_mode(df, len(df)-1, sweep_b, sweep_e)

            for strategy in STRATEGIES:
                detect_signals(df, strategy['signal_func'])
        scan_ms = (time.time() - t0) * 1000

        _, peak = tracemalloc.get_traced_memory()
        tracemalloc.stop()
        mem_mb = peak / (1024 * 1024)

        per_ticker = scan_ms / n_tickers
        print(f"  {n_tickers:>8} {build_time:>8.1f} {scan_ms:>10.0f} {per_ticker:>8.1f} {mem_mb:>8.1f}")

        scale_results.append({
            'tickers': n_tickers,
            'build_s': round(build_time, 2),
            'scan_ms': round(scan_ms, 1),
            'per_ticker_ms': round(per_ticker, 1),
            'memory_mb': round(mem_mb, 1),
        })

    RESULTS['scale_test'] = scale_results
    return scale_results


# ============================================================
# 2B. SUSTAINED LOAD (20 tickers, 60 min)
# ============================================================

def test_2b_sustained():
    print("\n═══════ 2B. SUSTAINED LOAD (20 tickers, 60 min) ═══════")
    from indicators import compute_all_indicators, incremental_update
    from signals import detect_signals
    from v3_filters import detect_liquidity_levels, MarketModeDetector
    from config import STRATEGIES

    mode_det = MarketModeDetector()
    tickers = FAKE_TICKERS[:20]

    # Build
    data = {}
    for i, ticker in enumerate(tickers):
        data[ticker] = compute_all_indicators(make_gbm_df(500, seed=42+i), include_whale=True)

    bar_times = []
    memory_samples = []

    n_minutes = 60
    tracemalloc.start()

    for minute in range(n_minutes):
        for ticker in tickers:
            df = data[ticker]
            new_bar = random_bar(df['Close'].iloc[-1])

            t0 = time.perf_counter()
            df = pd.concat([df, new_bar], ignore_index=True)
            df = incremental_update(df, include_whale=True)
            sweep_b, sweep_e = detect_liquidity_levels(df)
            mode = mode_det.determine_mode(df, len(df)-1, sweep_b, sweep_e)
            for strategy in STRATEGIES[:5]:
                detect_signals(df, strategy['signal_func'])
            bar_ms = (time.perf_counter() - t0) * 1000
            bar_times.append(bar_ms)

            data[ticker] = df

        # Memory check every 10 minutes
        if (minute + 1) % 10 == 0:
            _, peak = tracemalloc.get_traced_memory()
            memory_samples.append({'minute': minute + 1, 'peak_mb': peak / (1024*1024)})

    tracemalloc.stop()

    p50 = np.percentile(bar_times, 50)
    p95 = np.percentile(bar_times, 95)
    p99 = np.percentile(bar_times, 99)
    max_lat = max(bar_times)

    print(f"  {n_minutes} min × {len(tickers)} tickers = {len(bar_times)} events")
    print(f"  Latency: P50={p50:.2f}ms  P95={p95:.2f}ms  P99={p99:.2f}ms  MAX={max_lat:.2f}ms")
    mem_strs = [f"{m['minute']}min={m['peak_mb']:.0f}MB" for m in memory_samples]
    print(f"  Memory: {mem_strs}")

    # Check for memory leak
    if len(memory_samples) >= 2:
        first_mem = memory_samples[0]['peak_mb']
        last_mem = memory_samples[-1]['peak_mb']
        growth = last_mem - first_mem
        print(f"  Memory growth: {growth:.1f} MB over {n_minutes} min")
        if growth > 100:
            print(f"  ⚠ POTENTIAL MEMORY LEAK: {growth:.0f}MB growth")

    # Drift check
    first_q = bar_times[:len(bar_times)//4]
    last_q = bar_times[-len(bar_times)//4:]
    drift = np.mean(last_q) / np.mean(first_q)
    print(f"  Performance drift: {drift:.2f}x (last quarter / first quarter)")

    RESULTS['sustained'] = {
        'p50_ms': round(p50, 2), 'p95_ms': round(p95, 2),
        'p99_ms': round(p99, 2), 'max_ms': round(max_lat, 2),
        'drift': round(drift, 2),
        'memory_samples': memory_samples,
    }


# ============================================================
# 2C. WEBSOCKET BURST (30 tickers)
# ============================================================

def test_2c_ws_burst():
    print("\n═══════ 2C. WEBSOCKET BURST (30 tickers) ═══════")
    from data_feed import LiveDataFeed
    from indicators import compute_all_indicators

    class MockClient:
        def get_bars(self, *a, **kw): return []
        def get_latest_bar(self, *a, **kw): return None

    tickers = FAKE_TICKERS[:30]
    feed = LiveDataFeed(MockClient(), tickers, {'1min'}, use_websocket=False)

    # Pre-load bars
    for i, ticker in enumerate(tickers):
        feed.bars[ticker]['1min'] = compute_all_indicators(
            make_gbm_df(500, seed=42+i), include_whale=True)

    callback_count = [0]
    feed.register_bar_callback(lambda t, b: callback_count.__setitem__(0, callback_count[0]+1))

    # Fire all 30 bars
    t0 = time.perf_counter()
    for i, ticker in enumerate(tickers):
        msg = {
            'T': 'b', 'S': ticker,
            't': f'2024-06-15T10:{i:02d}:00Z',
            'o': 500+i, 'h': 501+i, 'l': 499+i, 'c': 500.5+i,
            'v': 1500000, 'n': 4000,
        }
        feed._handle_ws_bar(msg)
    burst_ms = (time.perf_counter() - t0) * 1000

    # Verify integrity
    all_correct = True
    for ticker in tickers:
        df = feed.bars[ticker]['1min']
        if len(df) != 501:
            all_correct = False
            print(f"  ✗ {ticker}: expected 501 bars, got {len(df)}")

    print(f"  30-ticker burst: {burst_ms:.0f}ms")
    print(f"  Callbacks fired: {callback_count[0]}")
    print(f"  Data integrity: {'ALL OK' if all_correct else 'ERRORS'}")

    RESULTS['ws_burst_30'] = {
        'time_ms': round(burst_ms, 1),
        'callbacks': callback_count[0],
        'integrity': all_correct,
    }


# ============================================================
# 3A. TICKER SCALING UNTIL FAILURE
# ============================================================

def test_3a_ticker_scaling():
    print("\n═══════ 3A. TICKER SCALING UNTIL FAILURE ═══════")
    from indicators import compute_all_indicators, incremental_update
    from signals import detect_signals
    from v3_filters import detect_liquidity_levels, MarketModeDetector
    from config import STRATEGIES

    mode_det = MarketModeDetector()
    max_tickers = 0

    for n_tickers in [10, 20, 30, 50, 75, 100, 150, 200]:
        try:
            # Build data
            data = {}
            for i in range(n_tickers):
                data[f'T{i}'] = compute_all_indicators(
                    make_gbm_df(2000, seed=42+i), include_whale=True)

            # Full cycle
            t0 = time.perf_counter()
            for i in range(n_tickers):
                df = data[f'T{i}']
                new_bar = random_bar(df['Close'].iloc[-1])
                df = pd.concat([df, new_bar], ignore_index=True)
                df = incremental_update(df, include_whale=True)
                sweep_b, sweep_e = detect_liquidity_levels(df)
                mode = mode_det.determine_mode(df, len(df)-1, sweep_b, sweep_e)
                for strategy in STRATEGIES:
                    detect_signals(df, strategy['signal_func'])
            cycle_ms = (time.perf_counter() - t0) * 1000

            print(f"  {n_tickers:>4} tickers: {cycle_ms:>8.0f}ms ({cycle_ms/n_tickers:.1f}ms/ticker)")

            if cycle_ms > 60000:
                print(f"  ⛔ BREAKING POINT: {n_tickers} tickers = {cycle_ms/1000:.1f}s (> 60s limit)")
                RESULTS['max_tickers'] = max_tickers
                break

            max_tickers = n_tickers

        except Exception as e:
            print(f"  ⛔ CRASHED at {n_tickers} tickers: {e}")
            RESULTS['max_tickers'] = max_tickers
            break
    else:
        RESULTS['max_tickers'] = max_tickers
        print(f"  ✓ Survived all scales up to {max_tickers} tickers!")


# ============================================================
# 3B. BAR HISTORY SCALING
# ============================================================

def test_3b_bar_scaling():
    print("\n═══════ 3B. BAR HISTORY SCALING ═══════")
    from indicators import compute_all_indicators
    from signals import detect_signals
    from v3_filters import detect_liquidity_levels
    from config import STRATEGIES

    max_bars = 0

    for n_bars in [2000, 5000, 10000, 20000, 50000, 100000]:
        try:
            df = make_gbm_df(n_bars, seed=42)

            t0 = time.perf_counter()
            df = compute_all_indicators(df, include_whale=True)
            ind_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            sweep_b, sweep_e = detect_liquidity_levels(df)
            liq_ms = (time.perf_counter() - t0) * 1000

            t0 = time.perf_counter()
            for strategy in STRATEGIES:
                detect_signals(df, strategy['signal_func'])
            sig_ms = (time.perf_counter() - t0) * 1000

            total_ms = ind_ms + liq_ms + sig_ms
            mem_mb = df.memory_usage(deep=True).sum() / (1024*1024)

            print(f"  {n_bars:>7,} bars: ind={ind_ms:>7.0f}ms  liq={liq_ms:>5.0f}ms  "
                  f"sig={sig_ms:>5.0f}ms  TOTAL={total_ms:>7.0f}ms  mem={mem_mb:.0f}MB")

            if total_ms > 1000:
                print(f"  ⛔ BREAKING POINT: {n_bars:,} bars = {total_ms/1000:.1f}s (> 1s limit)")
                RESULTS['max_bars'] = max_bars
                break

            max_bars = n_bars

        except Exception as e:
            print(f"  ⛔ CRASHED at {n_bars:,} bars: {e}")
            RESULTS['max_bars'] = max_bars
            break
    else:
        RESULTS['max_bars'] = max_bars
        print(f"  ✓ Survived all sizes up to {max_bars:,} bars!")


# ============================================================
# 3C. STRATEGY SCALING
# ============================================================

def test_3c_strategy_scaling():
    print("\n═══════ 3C. STRATEGY SCALING ═══════")
    from indicators import compute_all_indicators
    from signals import detect_signals
    from v3_filters import detect_liquidity_levels, MarketModeDetector, v3_master_filter, VolatilityRegime
    from config import STRATEGIES

    df = compute_all_indicators(make_gbm_df(2000, seed=42), include_whale=True)
    sweep_b, sweep_e = detect_liquidity_levels(df)
    mode_det = MarketModeDetector()
    vol_regime = VolatilityRegime()
    max_strats = 0

    for n_strats in [20, 40, 80, 160, 320]:
        # Duplicate strategies to hit target count
        strats = (STRATEGIES * (n_strats // len(STRATEGIES) + 1))[:n_strats]

        t0 = time.perf_counter()
        for strategy in strats:
            call_arr, put_arr = detect_signals(df, strategy['signal_func'])
            # Filter recent signals
            recent = len(df) - 3
            call_idx = np.where(call_arr)[0]
            call_idx = call_idx[call_idx >= recent]
            for idx in call_idx:
                v3_master_filter(df, idx, 'CALL', strategy, float(df['Close'].iloc[idx]),
                                 sweep_bull=sweep_b, sweep_bear=sweep_e, vol_regime=vol_regime)
        cycle_ms = (time.perf_counter() - t0) * 1000

        print(f"  {n_strats:>4} strategies: {cycle_ms:>8.0f}ms ({cycle_ms/n_strats:.1f}ms/strat)")

        if cycle_ms > 1000:
            print(f"  ⛔ BREAKING POINT: {n_strats} strategies = {cycle_ms/1000:.1f}s (> 1s limit)")
            RESULTS['max_strategies'] = max_strats
            break

        max_strats = n_strats
    else:
        RESULTS['max_strategies'] = max_strats
        print(f"  ✓ Survived all scales up to {max_strats} strategies!")


# ============================================================
# 3D. CONCURRENT WEBSOCKET STORM (50 tickers, 10ms intervals)
# ============================================================

def test_3d_ws_storm():
    print("\n═══════ 3D. WEBSOCKET STORM (50 tickers) ═══════")
    from data_feed import LiveDataFeed
    from indicators import compute_all_indicators

    class MockClient:
        def get_bars(self, *a, **kw): return []
        def get_latest_bar(self, *a, **kw): return None

    # Generate 50 tickers
    tickers_50 = [f'TICK{i:02d}' for i in range(50)]
    feed = LiveDataFeed(MockClient(), tickers_50, {'1min'}, use_websocket=False)

    for i, ticker in enumerate(tickers_50):
        feed.bars[ticker]['1min'] = compute_all_indicators(
            make_gbm_df(500, seed=42+i), include_whale=True)

    callback_count = [0]
    errors = [0]
    feed.register_bar_callback(lambda t, b: callback_count.__setitem__(0, callback_count[0]+1))

    t0 = time.perf_counter()
    for i, ticker in enumerate(tickers_50):
        msg = {
            'T': 'b', 'S': ticker,
            't': f'2024-06-15T10:00:{i:02d}Z',
            'o': 500+i*0.1, 'h': 501+i*0.1, 'l': 499+i*0.1, 'c': 500.5+i*0.1,
            'v': 1500000, 'n': 4000,
        }
        try:
            feed._handle_ws_bar(msg)
        except Exception as e:
            errors[0] += 1
    storm_ms = (time.perf_counter() - t0) * 1000

    # Verify all bars appended
    correct_count = sum(1 for t in tickers_50 if len(feed.bars[t]['1min']) == 501)

    print(f"  50-ticker storm: {storm_ms:.0f}ms")
    print(f"  Callbacks: {callback_count[0]}/50")
    print(f"  Errors: {errors[0]}")
    print(f"  Correct row counts: {correct_count}/50")

    RESULTS['ws_storm_50'] = {
        'time_ms': round(storm_ms, 1),
        'callbacks': callback_count[0],
        'errors': errors[0],
        'correct': correct_count,
    }


# ============================================================
# 3E. MEMORY PRESSURE TEST
# ============================================================

def test_3e_memory_pressure():
    print("\n═══════ 3E. MEMORY PRESSURE (100 tickers × 10K bars) ═══════")
    from indicators import compute_all_indicators

    tracemalloc.start()

    try:
        data = {}
        for i in range(100):
            data[f'MEM{i:03d}'] = compute_all_indicators(
                make_gbm_df(10000, seed=42+i), include_whale=True)
            if (i + 1) % 20 == 0:
                _, peak = tracemalloc.get_traced_memory()
                print(f"    {i+1} tickers loaded: {peak/(1024*1024):.0f} MB")

        _, final_peak = tracemalloc.get_traced_memory()
        peak_mb = final_peak / (1024 * 1024)
        print(f"  Final peak: {peak_mb:.0f} MB for 100 tickers × 10K bars")

        RESULTS['memory_pressure'] = {
            'tickers': 100,
            'bars_each': 10000,
            'peak_mb': round(peak_mb, 1),
        }

    except MemoryError:
        _, peak = tracemalloc.get_traced_memory()
        print(f"  ⛔ OOM at peak={peak/(1024*1024):.0f} MB")
        RESULTS['memory_pressure'] = {'oom': True, 'peak_mb': peak/(1024*1024)}
    finally:
        tracemalloc.stop()


# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    print("=" * 70)
    print("PHASE 2+3: LOAD SCALING + STRESS TEST")
    print("=" * 70)

    t0 = time.time()

    # Phase 2
    test_2a_scale()
    test_2b_sustained()
    test_2c_ws_burst()

    # Phase 3
    test_3a_ticker_scaling()
    test_3b_bar_scaling()
    test_3c_strategy_scaling()
    test_3d_ws_storm()
    test_3e_memory_pressure()

    elapsed = time.time() - t0

    # ═══════ BREAKING POINTS SUMMARY ═══════
    breaking_points = {
        'max_tickers_before_60s': RESULTS.get('max_tickers', 'N/A'),
        'max_bars_before_1s': RESULTS.get('max_bars', 'N/A'),
        'max_strategies_before_1s': RESULTS.get('max_strategies', 'N/A'),
        'ws_burst_30_ms': RESULTS.get('ws_burst_30', {}).get('time_ms', 'N/A'),
        'ws_storm_50_ms': RESULTS.get('ws_storm_50', {}).get('time_ms', 'N/A'),
        'ws_storm_50_errors': RESULTS.get('ws_storm_50', {}).get('errors', 'N/A'),
        'memory_100x10k_mb': RESULTS.get('memory_pressure', {}).get('peak_mb', 'N/A'),
        'sustained_p99_ms': RESULTS.get('sustained', {}).get('p99_ms', 'N/A'),
        'sustained_drift': RESULTS.get('sustained', {}).get('drift', 'N/A'),
    }

    # Save results
    os.makedirs('/sessions/dazzling-epic-planck/mnt/outputs/live_engine/test_results', exist_ok=True)
    with open('/sessions/dazzling-epic-planck/mnt/outputs/live_engine/test_results/breaking_points.json', 'w') as f:
        json.dump(breaking_points, f, indent=2)
    with open('/sessions/dazzling-epic-planck/mnt/outputs/live_engine/test_results/full_results.json', 'w') as f:
        json.dump(RESULTS, f, indent=2, default=str)

    print(f"\n{'=' * 70}")
    print(f"STRESS TEST COMPLETE ({elapsed:.0f}s)")
    print(f"{'=' * 70}")
    print(f"\n{'─' * 50}")
    print(f"{'BREAKING POINTS':^50}")
    print(f"{'─' * 50}")
    for k, v in breaking_points.items():
        print(f"  {k:<35} {v}")
    print(f"{'─' * 50}")
    print(f"\nResults saved to test_results/")
