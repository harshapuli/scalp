"""
REAL-TIME ALPACA INTEGRATION TEST
====================================
Hits the LIVE Alpaca API (paper account) and measures the actual
end-to-end pipeline:

  API fetch → DataFrame build → indicators → signals → filters → decision

This is the REAL bottleneck test. No synthetic data.

Run: python test_realtime.py
"""
import sys
import time
import json
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

sys.path.insert(0, '.')

from config import (
    ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_BASE_URL, ALPACA_DATA_URL,
    TICKERS, STRATEGIES, V3_CONFIG, RISK
)
from execution import AlpacaClient
from indicators import compute_all_indicators, incremental_update
from signals import detect_signals, apply_v2_filters
from v3_filters import (
    MarketModeDetector, EMABiasFilter, VolatilityRegime, ScalingManager,
    detect_liquidity_levels, v3_master_filter, kill_zone_check
)


# ============================================================
# SETUP
# ============================================================

client = AlpacaClient(
    api_key=ALPACA_API_KEY,
    api_secret=ALPACA_API_SECRET,
    base_url=ALPACA_BASE_URL,
    data_url=ALPACA_DATA_URL,
)

mode_det = MarketModeDetector()
ema_filter = EMABiasFilter()
vol_regime = VolatilityRegime()
scaling_mgr = ScalingManager()

tickers = list(TICKERS.keys())


# ============================================================
# TEST 1: API CONNECTIVITY + ACCOUNT
# ============================================================

def test_connectivity():
    print("\n═══════ 1. API CONNECTIVITY ═══════")

    # Account
    t0 = time.perf_counter()
    try:
        acct = client.get_account()
        acct_ms = (time.perf_counter() - t0) * 1000
        equity = float(acct.get('equity', 0))
        bp = float(acct.get('buying_power', 0))
        status = acct.get('status', 'unknown')
        print(f"  ✓ Account: status={status}, equity=${equity:,.2f}, buying_power=${bp:,.2f}")
        print(f"    Latency: {acct_ms:.0f}ms")
    except Exception as e:
        print(f"  ✗ Account check failed: {e}")
        return False

    # Clock
    t0 = time.perf_counter()
    try:
        clock = client.get_clock()
        clock_ms = (time.perf_counter() - t0) * 1000
        is_open = clock.get('is_open', False)
        print(f"  ✓ Clock: market={'OPEN' if is_open else 'CLOSED'}")
        print(f"    Latency: {clock_ms:.0f}ms")
        if not is_open:
            next_open = clock.get('next_open', 'unknown')
            print(f"    Next open: {next_open}")
    except Exception as e:
        print(f"  ✗ Clock check failed: {e}")

    return True


# ============================================================
# TEST 2: BAR FETCH LATENCY PER TICKER
# ============================================================

def test_bar_fetch():
    print("\n═══════ 2. BAR FETCH LATENCY (per ticker) ═══════")

    # Fetch different timeframes and sizes
    results = {}
    start_3d = (datetime.now() - timedelta(days=3)).strftime('%Y-%m-%dT00:00:00Z')
    start_30d = (datetime.now() - timedelta(days=30)).strftime('%Y-%m-%dT00:00:00Z')

    print(f"  {'Ticker':>7} {'1min(500)':>10} {'1min(1000)':>11} {'5min':>8} {'1hr':>8} {'daily':>8} {'bars':>6}")
    print(f"  {'─'*7} {'─'*10} {'─'*11} {'─'*8} {'─'*8} {'─'*8} {'─'*6}")

    for ticker in tickers:
        row = {}

        # 500 1-min bars
        t0 = time.perf_counter()
        try:
            bars = client.get_bars(ticker, '1Min', start=start_3d, limit=500)
            row['1min_500'] = (time.perf_counter() - t0) * 1000
            row['bar_count'] = len(bars)
        except Exception as e:
            row['1min_500'] = -1
            row['bar_count'] = 0
            print(f"  ✗ {ticker} 1min fetch failed: {e}")

        # 1000 1-min bars
        t0 = time.perf_counter()
        try:
            bars = client.get_bars(ticker, '1Min', start=start_3d, limit=1000)
            row['1min_1000'] = (time.perf_counter() - t0) * 1000
        except:
            row['1min_1000'] = -1

        # 5-min bars
        t0 = time.perf_counter()
        try:
            bars = client.get_bars(ticker, '5Min', start=start_30d, limit=200)
            row['5min'] = (time.perf_counter() - t0) * 1000
        except:
            row['5min'] = -1

        # 1-hour bars
        t0 = time.perf_counter()
        try:
            bars = client.get_bars(ticker, '1Hour', start=start_30d, limit=100)
            row['1hr'] = (time.perf_counter() - t0) * 1000
        except:
            row['1hr'] = -1

        # daily bars
        t0 = time.perf_counter()
        try:
            bars = client.get_bars(ticker, '1Day', limit=60)
            row['daily'] = (time.perf_counter() - t0) * 1000
        except:
            row['daily'] = -1

        results[ticker] = row
        print(f"  {ticker:>7} {row.get('1min_500',0):>9.0f}ms {row.get('1min_1000',0):>10.0f}ms "
              f"{row.get('5min',0):>7.0f}ms {row.get('1hr',0):>7.0f}ms {row.get('daily',0):>7.0f}ms "
              f"{row.get('bar_count',0):>5}")

        time.sleep(0.2)  # Rate limit respect

    # Summary
    all_1min = [v['1min_500'] for v in results.values() if v['1min_500'] > 0]
    if all_1min:
        print(f"\n  1min fetch (500 bars): avg={np.mean(all_1min):.0f}ms, "
              f"p95={np.percentile(all_1min, 95):.0f}ms, max={max(all_1min):.0f}ms")
    total_7ticker = sum(all_1min)
    print(f"  Total for 7 tickers (sequential): {total_7ticker:.0f}ms")

    return results


# ============================================================
# TEST 3: FULL WARMUP CYCLE (what happens on engine.start())
# ============================================================

def test_warmup():
    print("\n═══════ 3. FULL WARMUP CYCLE ═══════")

    needed_tfs = {'1min', '5min', '15min', '1hr', 'daily'}
    tf_map = {'1min': '1Min', '5min': '5Min', '15min': '15Min', '1hr': '1Hour', 'daily': '1Day'}
    warmup_bars = {'1min': 500, '5min': 200, '15min': 100, '1hr': 100, 'daily': 60}
    lookbacks = {'1min': 3, '5min': 7, '15min': 7, '1hr': 30, 'daily': 120}

    data = {}  # {ticker: {tf: DataFrame}}
    total_api_ms = 0
    total_compute_ms = 0

    warmup_start = time.perf_counter()

    for ticker in tickers:
        data[ticker] = {}
        ticker_start = time.perf_counter()
        api_ms = 0

        for tf in needed_tfs:
            start = (datetime.now() - timedelta(days=lookbacks[tf])).strftime('%Y-%m-%dT00:00:00Z')
            alpaca_tf = tf_map[tf]
            limit = warmup_bars[tf]

            t0 = time.perf_counter()
            try:
                raw_bars = client.get_bars(ticker, alpaca_tf, start=start, limit=limit)
                fetch_ms = (time.perf_counter() - t0) * 1000
                api_ms += fetch_ms
                total_api_ms += fetch_ms

                if not raw_bars:
                    continue

                df = pd.DataFrame(raw_bars)
                df = df.rename(columns={
                    't': 'Date', 'o': 'Open', 'h': 'High', 'l': 'Low',
                    'c': 'Close', 'v': 'Volume', 'n': 'NumTrades',
                })
                if 'Date' in df.columns:
                    df['Date'] = pd.to_datetime(df['Date']).dt.tz_localize(None)

                t0 = time.perf_counter()
                df = compute_all_indicators(df, include_whale=True)
                compute_ms = (time.perf_counter() - t0) * 1000
                total_compute_ms += compute_ms

                data[ticker][tf] = df

            except Exception as e:
                print(f"    ✗ {ticker}/{tf}: {e}")

            time.sleep(0.15)  # Rate limit

        ticker_ms = (time.perf_counter() - ticker_start) * 1000
        bar_counts = {tf: len(df) for tf, df in data[ticker].items()}
        print(f"  {ticker}: {ticker_ms:.0f}ms (API={api_ms:.0f}ms) — bars={bar_counts}")

    warmup_total = (time.perf_counter() - warmup_start) * 1000

    print(f"\n  WARMUP TOTAL: {warmup_total/1000:.1f}s")
    print(f"    API time:     {total_api_ms:.0f}ms ({total_api_ms/warmup_total*100:.0f}%)")
    print(f"    Compute time: {total_compute_ms:.0f}ms ({total_compute_ms/warmup_total*100:.0f}%)")
    print(f"    Sleep/other:  {warmup_total - total_api_ms - total_compute_ms:.0f}ms")

    return data


# ============================================================
# TEST 4: FULL SCAN CYCLE ON REAL DATA
# ============================================================

def test_scan_cycle(data):
    print("\n═══════ 4. FULL SCAN CYCLE (real data) ═══════")

    if not data:
        print("  ✗ No data from warmup, skipping")
        return

    # Simulate exactly what the engine does every tick
    cycle_start = time.perf_counter()
    timings = {}

    signals_found = 0
    signals_blocked = 0
    signals_passed = 0

    for ticker in tickers:
        if ticker not in data or '1min' not in data[ticker]:
            continue

        ticker_start = time.perf_counter()

        tf_data = data[ticker]
        df_1min = tf_data.get('1min')
        df_5m = tf_data.get('5min')
        if df_1min is None or len(df_1min) < 50:
            continue

        # ═══ Context Build ═══
        t0 = time.perf_counter()
        sweep_bull, sweep_bear = detect_liquidity_levels(df_1min)
        ctx_liq_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        htf_bias = ema_filter.compute_bias(tf_data)
        ctx_bias_ms = (time.perf_counter() - t0) * 1000

        t0 = time.perf_counter()
        current_mode = mode_det.determine_mode(df_1min, len(df_1min)-1, sweep_bull, sweep_bear)
        ctx_mode_ms = (time.perf_counter() - t0) * 1000

        # HTF trend
        htf_trend = None
        if '1hr' in tf_data and 'Trend_Dir' in tf_data['1hr'].columns:
            htf_trend = tf_data['1hr'][['Date', 'Trend_Dir']].copy()
            htf_trend.set_index('Date', inplace=True)

        # ═══ Scan All 20 Strategies ═══
        t0 = time.perf_counter()
        for strategy in STRATEGIES:
            tf = strategy['timeframe']
            df = tf_data.get(tf, df_1min)
            if df is None or len(df) < 50:
                continue

            # Mode routing
            strategy_mode = strategy.get('mode', 'TREND')
            has_sweep = bool(sweep_bull[-1]) or bool(sweep_bear[-1])
            mode_ok, _ = mode_det.check_mode_alignment(strategy_mode, current_mode, has_sweep)

            # Signal detection
            call_arr, put_arr = detect_signals(df, strategy['signal_func'])
            n = len(df)
            recent = n - 3
            call_idx = np.where(call_arr)[0]
            put_idx = np.where(put_arr)[0]
            call_idx = call_idx[call_idx >= recent]
            put_idx = put_idx[put_idx >= recent]

            for direction, indices in [('CALL', call_idx), ('PUT', put_idx)]:
                for idx in indices:
                    signals_found += 1
                    if not mode_ok:
                        signals_blocked += 1
                        continue

                    # EMA bias
                    if not ema_filter.check_alignment(direction, strategy.get('category', 'scalp')):
                        signals_blocked += 1
                        continue

                    # V3 master filter
                    entry_price = float(df['Close'].iloc[idx])
                    v3_ok, v3_meta = v3_master_filter(
                        df, idx, direction, strategy, entry_price,
                        sweep_bull=sweep_bull, sweep_bear=sweep_bear,
                        vol_regime=vol_regime, df_5m=df_5m,
                    )
                    if v3_ok:
                        signals_passed += 1
                    else:
                        signals_blocked += 1

        scan_ms = (time.perf_counter() - t0) * 1000

        ticker_ms = (time.perf_counter() - ticker_start) * 1000
        timings[ticker] = {
            'total_ms': round(ticker_ms, 1),
            'ctx_liq_ms': round(ctx_liq_ms, 1),
            'ctx_bias_ms': round(ctx_bias_ms, 1),
            'ctx_mode_ms': round(ctx_mode_ms, 1),
            'scan_ms': round(scan_ms, 1),
            'mode': current_mode,
            'bias': htf_bias,
            'bars': len(df_1min),
        }

    cycle_ms = (time.perf_counter() - cycle_start) * 1000

    # Report
    print(f"\n  {'Ticker':>7} {'Total':>8} {'Liq':>6} {'Bias':>6} {'Mode':>6} {'Scan':>6} {'Market':>10} {'EMA':>7} {'Bars':>5}")
    print(f"  {'─'*7} {'─'*8} {'─'*6} {'─'*6} {'─'*6} {'─'*6} {'─'*10} {'─'*7} {'─'*5}")
    for ticker, t in timings.items():
        print(f"  {ticker:>7} {t['total_ms']:>7.1f}ms {t['ctx_liq_ms']:>5.1f}ms "
              f"{t['ctx_bias_ms']:>5.1f}ms {t['ctx_mode_ms']:>5.1f}ms "
              f"{t['scan_ms']:>5.1f}ms {t['mode']:>10} {t['bias']:>7} {t['bars']:>5}")

    total_scan = sum(t['total_ms'] for t in timings.values())
    print(f"\n  FULL CYCLE: {cycle_ms:.0f}ms ({len(timings)} tickers)")
    print(f"  Signals found: {signals_found}")
    print(f"  Signals blocked: {signals_blocked}")
    print(f"  Signals passed (tradeable): {signals_passed}")

    return timings, cycle_ms


# ============================================================
# TEST 5: LATEST BAR FETCH + INCREMENTAL (live tick simulation)
# ============================================================

def test_live_tick(data):
    print("\n═══════ 5. LIVE TICK SIMULATION ═══════")
    print("  (Fetch latest bar → append → incremental update → scan)")

    if not data:
        print("  ✗ No data, skipping")
        return

    tick_results = []

    for ticker in tickers:
        if ticker not in data or '1min' not in data[ticker]:
            continue

        df = data[ticker]['1min'].copy()

        # Fetch latest bar from Alpaca
        t0 = time.perf_counter()
        try:
            latest = client.get_latest_bar(ticker)
            fetch_ms = (time.perf_counter() - t0) * 1000
        except Exception as e:
            print(f"  ✗ {ticker}: latest bar fetch failed: {e}")
            continue

        if not latest:
            continue

        # Append
        t0 = time.perf_counter()
        new_row = pd.DataFrame([{
            'Date': pd.to_datetime(latest.get('t')).tz_localize(None),
            'Open': float(latest.get('o', 0)),
            'High': float(latest.get('h', 0)),
            'Low': float(latest.get('l', 0)),
            'Close': float(latest.get('c', 0)),
            'Volume': int(latest.get('v', 0)),
            'NumTrades': int(latest.get('n', 1)),
        }])
        df = pd.concat([df, new_row], ignore_index=True)
        append_ms = (time.perf_counter() - t0) * 1000

        # Incremental indicator update
        t0 = time.perf_counter()
        df = incremental_update(df, include_whale=True)
        inc_ms = (time.perf_counter() - t0) * 1000

        # Context + scan
        t0 = time.perf_counter()
        sweep_b, sweep_e = detect_liquidity_levels(df)
        mode = mode_det.determine_mode(df, len(df)-1, sweep_b, sweep_e)
        for strategy in STRATEGIES:
            detect_signals(df, strategy['signal_func'])
        scan_ms = (time.perf_counter() - t0) * 1000

        total_ms = fetch_ms + append_ms + inc_ms + scan_ms
        price = float(latest.get('c', 0))

        tick_results.append({
            'ticker': ticker, 'price': price,
            'fetch_ms': fetch_ms, 'append_ms': append_ms,
            'inc_ms': inc_ms, 'scan_ms': scan_ms, 'total_ms': total_ms,
        })

        print(f"  {ticker:>7} ${price:>8.2f} | fetch={fetch_ms:>6.0f}ms  "
              f"append={append_ms:>4.1f}ms  inc={inc_ms:>4.1f}ms  "
              f"scan={scan_ms:>4.1f}ms  TOTAL={total_ms:>7.0f}ms")

        time.sleep(0.2)  # Rate limit

    # Summary
    if tick_results:
        fetch_times = [r['fetch_ms'] for r in tick_results]
        compute_times = [r['inc_ms'] + r['scan_ms'] for r in tick_results]
        total_times = [r['total_ms'] for r in tick_results]

        print(f"\n  ─── LIVE TICK SUMMARY (per ticker) ───")
        print(f"  API fetch:     avg={np.mean(fetch_times):.0f}ms  p95={np.percentile(fetch_times,95):.0f}ms")
        print(f"  Compute:       avg={np.mean(compute_times):.0f}ms  p95={np.percentile(compute_times,95):.0f}ms")
        print(f"  End-to-end:    avg={np.mean(total_times):.0f}ms  p95={np.percentile(total_times,95):.0f}ms")
        print(f"  7-ticker total: {sum(total_times):.0f}ms (sequential)")
        print(f"\n  ─── WHERE THE TIME GOES ───")
        total_fetch = sum(fetch_times)
        total_compute = sum(compute_times)
        total_all = sum(total_times)
        print(f"  API:     {total_fetch:>6.0f}ms ({total_fetch/total_all*100:.0f}%)")
        print(f"  Compute: {total_compute:>6.0f}ms ({total_compute/total_all*100:.0f}%)")

    return tick_results


# ============================================================
# TEST 6: WEBSOCKET CONNECTION TEST
# ============================================================

def test_websocket():
    print("\n═══════ 6. WEBSOCKET CONNECTION ═══════")
    from data_feed import AlpacaWebSocket

    bars_received = []
    trades_received = []

    def on_bar(msg):
        bars_received.append(msg)

    def on_trade(msg):
        trades_received.append(msg)

    ws = AlpacaWebSocket(
        api_key=ALPACA_API_KEY,
        api_secret=ALPACA_API_SECRET,
        tickers=tickers,
        on_bar=on_bar,
        on_trade=on_trade,
        use_sip=False,
    )

    print(f"  Connecting to WebSocket (IEX feed)...")
    t0 = time.perf_counter()
    ws.start()

    # Wait up to 10s for connection
    connected = False
    for i in range(100):
        if ws.connected:
            connect_ms = (time.perf_counter() - t0) * 1000
            connected = True
            break
        time.sleep(0.1)

    if connected:
        print(f"  ✓ Connected in {connect_ms:.0f}ms")
        print(f"  Listening for 15 seconds...")

        time.sleep(15)

        print(f"  Bars received: {len(bars_received)}")
        print(f"  Trades received: {len(trades_received)}")

        if bars_received:
            for b in bars_received[:3]:
                print(f"    Bar: {b.get('S')} O={b.get('o')} H={b.get('h')} "
                      f"L={b.get('l')} C={b.get('c')} V={b.get('v')}")
        if trades_received:
            trade_strs = [f"{t.get('S')}@{t.get('p')}" for t in trades_received[-5:]]
            print(f"    Latest trades: {trade_strs}")
    else:
        print(f"  ⚠ WebSocket did not connect within 10s")
        print(f"    This is normal if market is closed — WS only streams during market hours")

    ws.stop()
    print(f"  WebSocket stopped.")

    return connected, len(bars_received), len(trades_received)


# ============================================================
# TEST 7: SNAPSHOT LATENCY (fastest possible price check)
# ============================================================

def test_snapshot():
    print("\n═══════ 7. SNAPSHOT LATENCY (fastest price check) ═══════")

    snap_times = []
    for ticker in tickers:
        t0 = time.perf_counter()
        try:
            snap = client.get_snapshot(ticker)
            ms = (time.perf_counter() - t0) * 1000
            snap_times.append(ms)

            trade = snap.get('latestTrade', snap.get('latest_trade', {}))
            bar = snap.get('minuteBar', snap.get('minute_bar', {}))
            price = trade.get('p', bar.get('c', 'N/A'))
            print(f"  {ticker:>7}: ${price} ({ms:.0f}ms)")
        except Exception as e:
            print(f"  {ticker:>7}: FAILED — {e}")
        time.sleep(0.15)

    if snap_times:
        print(f"\n  Snapshot avg: {np.mean(snap_times):.0f}ms, "
              f"p95: {np.percentile(snap_times, 95):.0f}ms, "
              f"7-ticker total: {sum(snap_times):.0f}ms")


# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    print("=" * 70)
    print("REAL-TIME ALPACA INTEGRATION TEST")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print(f"Tickers: {tickers}")
    print("=" * 70)

    t0 = time.time()

    # 1. Connectivity
    if not test_connectivity():
        print("\n⛔ Cannot connect to Alpaca. Check API keys.")
        sys.exit(1)

    # 2. Bar fetch latency
    fetch_results = test_bar_fetch()

    # 3. Full warmup
    data = test_warmup()

    # 4. Scan cycle on real data
    scan_timings, cycle_ms = test_scan_cycle(data)

    # 5. Live tick simulation
    tick_results = test_live_tick(data)

    # 6. WebSocket
    ws_connected, ws_bars, ws_trades = test_websocket()

    # 7. Snapshot
    test_snapshot()

    # ═══════ FINAL REPORT ═══════
    elapsed = time.time() - t0
    print(f"\n{'=' * 70}")
    print(f"REAL-TIME TEST COMPLETE ({elapsed:.0f}s)")
    print(f"{'=' * 70}")

    print(f"\n{'─' * 55}")
    print(f"{'END-TO-END PIPELINE BREAKDOWN':^55}")
    print(f"{'─' * 55}")

    if tick_results:
        total_fetch = sum(r['fetch_ms'] for r in tick_results)
        total_compute = sum(r['inc_ms'] + r['scan_ms'] for r in tick_results)
        total_all = sum(r['total_ms'] for r in tick_results)

        print(f"  {'Stage':<30} {'Time':>8} {'%':>6}")
        print(f"  {'─'*30} {'─'*8} {'─'*6}")
        print(f"  {'API fetch (7 tickers)':<30} {total_fetch:>7.0f}ms {total_fetch/total_all*100:>5.0f}%")
        print(f"  {'Incremental indicators':<30} {sum(r['inc_ms'] for r in tick_results):>7.0f}ms {sum(r['inc_ms'] for r in tick_results)/total_all*100:>5.0f}%")
        print(f"  {'Context + 20-strat scan':<30} {sum(r['scan_ms'] for r in tick_results):>7.0f}ms {sum(r['scan_ms'] for r in tick_results)/total_all*100:>5.0f}%")
        print(f"  {'DataFrame append':<30} {sum(r['append_ms'] for r in tick_results):>7.0f}ms {sum(r['append_ms'] for r in tick_results)/total_all*100:>5.0f}%")
        print(f"  {'─'*30} {'─'*8} {'─'*6}")
        print(f"  {'TOTAL (sequential)':<30} {total_all:>7.0f}ms {100:>5.0f}%")

    print(f"\n  WebSocket: {'connected' if ws_connected else 'not connected (market closed?)'}")
    print(f"  WS bars/trades in 15s: {ws_bars}/{ws_trades}")
    print(f"{'─' * 55}")

    # Save results
    import os
    os.makedirs('test_results', exist_ok=True)
    results = {
        'timestamp': datetime.now().isoformat(),
        'tickers': tickers,
        'fetch_results': {k: {kk: round(vv, 1) for kk, vv in v.items()} for k, v in (fetch_results or {}).items()},
        'scan_timings': scan_timings,
        'tick_results': tick_results,
        'ws_connected': ws_connected,
        'ws_bars': ws_bars,
        'ws_trades': ws_trades,
    }
    with open('test_results/realtime_results.json', 'w') as f:
        json.dump(results, f, indent=2, default=str)
    print(f"\n  Results saved to test_results/realtime_results.json")
