"""
LOAD / STRESS TEST — Master Trading Engine
=============================================
Simulates real trading conditions to measure:
  1. Per-bar processing latency (WebSocket path)
  2. Full scan cycle time per ticker
  3. Throughput under 7-ticker concurrent load
  4. Memory footprint
  5. Incremental vs full recompute under sustained load
  6. Worst-case burst (all 7 tickers fire bars simultaneously)

Run: python test_load.py
"""

import sys
import time
import tracemalloc
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

# ============================================================
# HELPERS
# ============================================================

def make_df(n=2000, seed=42):
    np.random.seed(seed)
    dates = pd.date_range('2024-01-02 09:30', periods=n, freq='1min')
    close = 500 + np.cumsum(np.random.randn(n) * 0.1)
    return pd.DataFrame({
        'Date': dates,
        'Open': close - np.random.rand(n) * 0.2,
        'High': close + np.random.rand(n) * 0.3,
        'Low': close - np.random.rand(n) * 0.3,
        'Close': close,
        'Volume': np.random.randint(100000, 5000000, n),
        'NumTrades': np.random.randint(500, 10000, n),
    })


def random_bar(base_price=500):
    """Generate a random new bar."""
    c = base_price + np.random.randn() * 0.3
    return pd.DataFrame([{
        'Date': pd.Timestamp.now(),
        'Open': c - 0.1, 'High': c + 0.2, 'Low': c - 0.2, 'Close': c,
        'Volume': np.random.randint(500000, 3000000),
        'NumTrades': np.random.randint(1000, 8000),
    }])


# ============================================================
# 1. INDICATOR LATENCY BENCHMARK
# ============================================================

def test_indicator_latency():
    print("\n═══════ 1. INDICATOR LATENCY ═══════")
    from indicators import compute_all_indicators, incremental_update

    df = compute_all_indicators(make_df(2000), include_whale=True)

    # Full recompute latency (100 iterations)
    times_full = []
    for i in range(100):
        new_bar = random_bar(df['Close'].iloc[-1])
        df_test = pd.concat([df, new_bar], ignore_index=True)
        t0 = time.perf_counter()
        compute_all_indicators(df_test.copy(), include_whale=True)
        times_full.append((time.perf_counter() - t0) * 1000)

    # Incremental latency (100 iterations)
    times_inc = []
    for i in range(100):
        new_bar = random_bar(df['Close'].iloc[-1])
        df_test = pd.concat([df, new_bar], ignore_index=True)
        t0 = time.perf_counter()
        incremental_update(df_test, include_whale=True)
        times_inc.append((time.perf_counter() - t0) * 1000)

    full_p50 = np.percentile(times_full, 50)
    full_p99 = np.percentile(times_full, 99)
    inc_p50 = np.percentile(times_inc, 50)
    inc_p99 = np.percentile(times_inc, 99)

    print(f"  Full recompute (2000 bars):")
    print(f"    P50: {full_p50:.2f} ms  |  P99: {full_p99:.2f} ms  |  Mean: {np.mean(times_full):.2f} ms")
    print(f"  Incremental update (1 bar):")
    print(f"    P50: {inc_p50:.2f} ms  |  P99: {inc_p99:.2f} ms  |  Mean: {np.mean(times_inc):.2f} ms")
    print(f"  Speedup: {full_p50/inc_p50:.1f}x (P50)  {full_p99/inc_p99:.1f}x (P99)")

    # Thresholds
    assert inc_p99 < 20, f"FAIL: incremental P99 = {inc_p99:.1f}ms (limit 20ms)"
    print(f"  ✓ Incremental P99 under 20ms")

    return {'full_p50': full_p50, 'inc_p50': inc_p50}


# ============================================================
# 2. SCAN CYCLE LATENCY
# ============================================================

def test_scan_cycle():
    print("\n═══════ 2. SCAN CYCLE (per ticker) ═══════")
    from indicators import compute_all_indicators
    from signals import detect_signals
    from v3_filters import (MarketModeDetector, EMABiasFilter, VolatilityRegime,
                            detect_liquidity_levels, v3_master_filter)
    from config import STRATEGIES

    df = compute_all_indicators(make_df(2000), include_whale=True)
    mode_det = MarketModeDetector()
    ema_filter = EMABiasFilter()
    vol_regime = VolatilityRegime()

    # Simulate _scan_ticker: context build + 20 strategy scans
    times = []
    for _ in range(50):
        t0 = time.perf_counter()

        # Context build
        sweep_bull, sweep_bear = detect_liquidity_levels(df)
        tf_data = {'1min': df, '1hr': df.iloc[-100:]}
        bias = ema_filter.compute_bias(tf_data)
        mode = mode_det.determine_mode(df, len(df)-1, sweep_bull, sweep_bear)

        # Scan all 20 strategies
        for strategy in STRATEGIES:
            call_arr, put_arr = detect_signals(df, strategy['signal_func'])
            call_idx = np.where(call_arr)[0]
            put_idx = np.where(put_arr)[0]

            # Mode check
            strategy_mode = strategy.get('mode', 'TREND')
            has_sweep = bool(sweep_bull[-1]) or bool(sweep_bear[-1])
            ok, reason = mode_det.check_mode_alignment(strategy_mode, mode, has_sweep)

            # V3 filter on recent signals (last 3 bars)
            recent = len(df) - 3
            for idx in call_idx[call_idx >= recent]:
                v3_master_filter(df, idx, 'CALL', strategy, 500.0,
                                 sweep_bull=sweep_bull, sweep_bear=sweep_bear,
                                 vol_regime=vol_regime)
            for idx in put_idx[put_idx >= recent]:
                v3_master_filter(df, idx, 'PUT', strategy, 500.0,
                                 sweep_bull=sweep_bull, sweep_bear=sweep_bear,
                                 vol_regime=vol_regime)

        times.append((time.perf_counter() - t0) * 1000)

    p50 = np.percentile(times, 50)
    p99 = np.percentile(times, 99)
    print(f"  Per-ticker scan (context + 20 strategies + V3 filters):")
    print(f"    P50: {p50:.1f} ms  |  P99: {p99:.1f} ms  |  Mean: {np.mean(times):.1f} ms")
    print(f"  7-ticker total: P50={p50*7:.0f}ms  P99={p99*7:.0f}ms")

    assert p99 < 500, f"FAIL: scan cycle P99 = {p99:.1f}ms (limit 500ms)"
    print(f"  ✓ Single-ticker scan P99 under 500ms")

    return {'scan_p50': p50, 'scan_p99': p99}


# ============================================================
# 3. BURST TEST (all tickers fire simultaneously)
# ============================================================

def test_burst():
    print("\n═══════ 3. BURST TEST (7 tickers simultaneous) ═══════")
    from indicators import compute_all_indicators, incremental_update
    from signals import detect_signals
    from v3_filters import (MarketModeDetector, EMABiasFilter, VolatilityRegime,
                            detect_liquidity_levels, v3_master_filter)
    from config import STRATEGIES, TICKERS

    tickers = list(TICKERS.keys())
    mode_det = MarketModeDetector()
    ema_filter = EMABiasFilter()
    vol_regime = VolatilityRegime()

    # Pre-build data for all tickers
    data = {}
    for i, ticker in enumerate(tickers):
        data[ticker] = compute_all_indicators(make_df(2000, seed=42+i), include_whale=True)

    # Simulate: all 7 bars arrive at once, process sequentially (like real engine)
    burst_times = []
    for trial in range(20):
        t0 = time.perf_counter()

        for ticker in tickers:
            df = data[ticker]

            # Incremental update (new bar)
            new_bar = random_bar(df['Close'].iloc[-1])
            df = pd.concat([df, new_bar], ignore_index=True)
            df = incremental_update(df, include_whale=True)

            # Context
            sweep_bull, sweep_bear = detect_liquidity_levels(df)
            mode = mode_det.determine_mode(df, len(df)-1, sweep_bull, sweep_bear)

            # Scan all strategies
            for strategy in STRATEGIES:
                call_arr, put_arr = detect_signals(df, strategy['signal_func'])

        burst_times.append((time.perf_counter() - t0) * 1000)

    p50 = np.percentile(burst_times, 50)
    p99 = np.percentile(burst_times, 99)
    print(f"  7-ticker burst (append + incremental + context + 20 strats each):")
    print(f"    P50: {p50:.0f} ms  |  P99: {p99:.0f} ms  |  Mean: {np.mean(burst_times):.0f} ms")

    # Must complete within 1 second (before next minute bar)
    assert p99 < 1000, f"FAIL: burst P99 = {p99:.0f}ms (limit 1000ms)"
    print(f"  ✓ Full burst completes within 1 second")

    return {'burst_p50': p50, 'burst_p99': p99}


# ============================================================
# 4. SUSTAINED THROUGHPUT (simulate 30 minutes of trading)
# ============================================================

def test_sustained():
    print("\n═══════ 4. SUSTAINED THROUGHPUT (30 min simulation) ═══════")
    from indicators import compute_all_indicators, incremental_update
    from signals import detect_signals
    from v3_filters import detect_liquidity_levels, MarketModeDetector
    from config import STRATEGIES, TICKERS

    tickers = list(TICKERS.keys())
    mode_det = MarketModeDetector()

    # Pre-build data
    data = {}
    for i, ticker in enumerate(tickers):
        data[ticker] = compute_all_indicators(make_df(500, seed=42+i), include_whale=True)

    # 30 minutes = 30 bars per ticker = 210 total bar events
    n_minutes = 30
    bar_times = []
    scan_times = []

    for minute in range(n_minutes):
        for ticker in tickers:
            df = data[ticker]

            # Append + incremental
            new_bar = random_bar(df['Close'].iloc[-1])
            t0 = time.perf_counter()
            df = pd.concat([df, new_bar], ignore_index=True)
            df = incremental_update(df, include_whale=True)
            bar_ms = (time.perf_counter() - t0) * 1000
            bar_times.append(bar_ms)

            # Scan
            t0 = time.perf_counter()
            sweep_bull, sweep_bear = detect_liquidity_levels(df)
            mode = mode_det.determine_mode(df, len(df)-1, sweep_bull, sweep_bear)
            for strategy in STRATEGIES[:5]:  # Sample 5 strategies per tick to simulate interval gating
                detect_signals(df, strategy['signal_func'])
            scan_ms = (time.perf_counter() - t0) * 1000
            scan_times.append(scan_ms)

            data[ticker] = df  # Keep growing

    print(f"  {n_minutes} minutes × {len(tickers)} tickers = {len(bar_times)} bar events")
    print(f"  Bar append + incremental:")
    print(f"    P50: {np.percentile(bar_times, 50):.2f} ms  P99: {np.percentile(bar_times, 99):.2f} ms")
    print(f"  Context + signal scan (5 strats):")
    print(f"    P50: {np.percentile(scan_times, 50):.2f} ms  P99: {np.percentile(scan_times, 99):.2f} ms")
    print(f"  Final DataFrame sizes: {[len(data[t]) for t in tickers]}")

    # No degradation — P99 should stay under 50ms throughout
    last_quarter = bar_times[len(bar_times)*3//4:]
    first_quarter = bar_times[:len(bar_times)//4]
    drift = np.mean(last_quarter) / np.mean(first_quarter)
    print(f"  Performance drift (last quarter / first quarter): {drift:.2f}x")
    assert drift < 2.0, f"FAIL: performance degraded {drift:.1f}x over 30 minutes"
    print(f"  ✓ No significant performance degradation")


# ============================================================
# 5. MEMORY FOOTPRINT
# ============================================================

def test_memory():
    print("\n═══════ 5. MEMORY FOOTPRINT ═══════")
    from indicators import compute_all_indicators

    tracemalloc.start()
    snapshot1 = tracemalloc.take_snapshot()

    # Build full dataset for 7 tickers × 2000 bars (what lives in memory during trading)
    data = {}
    for i in range(7):
        data[f'TICKER_{i}'] = compute_all_indicators(make_df(2000, seed=42+i), include_whale=True)

    snapshot2 = tracemalloc.take_snapshot()
    stats = snapshot2.compare_to(snapshot1, 'lineno')

    total_mb = sum(s.size_diff for s in stats) / (1024 * 1024)
    print(f"  7 tickers × 2000 bars (with all indicators): {total_mb:.1f} MB")

    # Individual DataFrame size
    single_mb = data['TICKER_0'].memory_usage(deep=True).sum() / (1024 * 1024)
    print(f"  Single ticker DataFrame: {single_mb:.1f} MB ({len(data['TICKER_0'].columns)} columns)")
    print(f"  Total estimated: {single_mb * 7:.1f} MB for 7 tickers")

    tracemalloc.stop()

    assert total_mb < 500, f"FAIL: memory = {total_mb:.0f}MB (limit 500MB)"
    print(f"  ✓ Memory under 500MB")


# ============================================================
# 6. WEBSOCKET BAR HANDLER LATENCY
# ============================================================

def test_ws_handler():
    print("\n═══════ 6. WEBSOCKET BAR HANDLER ═══════")
    from data_feed import LiveDataFeed
    from indicators import compute_all_indicators

    class MockClient:
        def get_bars(self, *a, **kw): return []
        def get_latest_bar(self, *a, **kw): return None

    feed = LiveDataFeed(MockClient(), ['SPY'], {'1min', '5min'}, use_websocket=False)

    # Pre-load bars
    feed.bars['SPY']['1min'] = compute_all_indicators(make_df(2000), include_whale=True)

    # Simulate 100 WebSocket bar arrivals
    times = []
    callback_count = [0]
    feed.register_bar_callback(lambda t, b: callback_count.__setitem__(0, callback_count[0] + 1))

    for i in range(100):
        msg = {
            'T': 'b', 'S': 'SPY',
            't': f'2024-01-03T{10+i//60:02d}:{i%60:02d}:00Z',
            'o': 500 + i*0.01, 'h': 500.3 + i*0.01,
            'l': 499.8 + i*0.01, 'c': 500.1 + i*0.01,
            'v': 1500000, 'n': 4000,
        }
        t0 = time.perf_counter()
        feed._handle_ws_bar(msg)
        times.append((time.perf_counter() - t0) * 1000)

    p50 = np.percentile(times, 50)
    p99 = np.percentile(times, 99)
    print(f"  _handle_ws_bar (append + incremental + callback):")
    print(f"    P50: {p50:.2f} ms  |  P99: {p99:.2f} ms  |  Mean: {np.mean(times):.2f} ms")
    print(f"  Callbacks fired: {callback_count[0]}")
    print(f"  Final 1min bars: {len(feed.bars['SPY']['1min'])}")

    assert p99 < 50, f"FAIL: WS handler P99 = {p99:.1f}ms (limit 50ms)"
    print(f"  ✓ WS handler P99 under 50ms")


# ============================================================
# RUN ALL
# ============================================================

if __name__ == '__main__':
    print("=" * 60)
    print("LOAD / STRESS TEST — Master Trading Engine")
    print("=" * 60)

    t0 = time.time()

    results = {}
    try:
        results.update(test_indicator_latency())
        results.update(test_scan_cycle())
        results.update(test_burst())
        test_sustained()
        test_memory()
        test_ws_handler()

        elapsed = time.time() - t0
        print(f"\n{'=' * 60}")
        print(f"ALL LOAD TESTS PASSED ({elapsed:.1f}s)")
        print(f"{'=' * 60}")

        # Summary table
        print(f"\n{'─' * 45}")
        print(f"{'METRIC':<35} {'VALUE':>8}")
        print(f"{'─' * 45}")
        print(f"{'Incremental update P50':<35} {results.get('inc_p50', 0):.1f} ms")
        print(f"{'Full recompute P50':<35} {results.get('full_p50', 0):.1f} ms")
        print(f"{'Single-ticker scan P50':<35} {results.get('scan_p50', 0):.0f} ms")
        print(f"{'7-ticker burst P50':<35} {results.get('burst_p50', 0):.0f} ms")
        print(f"{'─' * 45}")

    except AssertionError as e:
        print(f"\nFAILED: {e}")
        sys.exit(1)
    except Exception as e:
        print(f"\nERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
