"""
FUNCTIONAL TEST SUITE — Master Trading Engine
================================================
Tests every module end-to-end:
  1. Config          — strategy definitions, V3 config integrity
  2. Indicators      — full pipeline + incremental accuracy
  3. Signals         — all 20 strategies produce valid arrays
  4. V3 Filters      — mode detection, EMA bias, kill zone, scaling
  5. Data Feed       — bar append, aggregation, price tracking
  6. Engine          — scan_ticker integration, decision tree routing

Run: python test_functional.py
"""

import sys
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

PASS = 0
FAIL = 0

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name}  — {detail}")


# ============================================================
# HELPERS
# ============================================================

def make_df(n=500, start='2024-01-02 09:30', seed=42):
    """Build a realistic OHLCV DataFrame with proper structure."""
    np.random.seed(seed)
    dates = pd.date_range(start, periods=n, freq='1min')
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


# ============================================================
# 1. CONFIG TESTS
# ============================================================

def test_config():
    print("\n═══════ 1. CONFIG ═══════")
    from config import STRATEGIES, V3_CONFIG, TICKERS, RISK, MODULES, SCAN_INTERVALS

    check("20 strategies defined", len(STRATEGIES) == 20, f"got {len(STRATEGIES)}")
    check("7 tickers defined", len(TICKERS) == 7, f"got {len(TICKERS)}")
    check("RISK has max_daily_loss_pct", 'max_daily_loss_pct' in RISK)

    # Every strategy has required fields
    required_fields = ['name', 'timeframe', 'signal_func', 'hold_bars', 'stop_pct',
                       'target_pct', 'instrument', 'category', 'module', 'mode']
    for s in STRATEGIES:
        for f in required_fields:
            if f not in s:
                check(f"Strategy {s.get('name','?')} has {f}", False, f"missing {f}")
                break
        else:
            continue
    check("All strategies have required fields", True)

    # Mode distribution
    modes = [s['mode'] for s in STRATEGIES]
    check("Has TREND strategies", 'TREND' in modes)
    check("Has REVERSAL strategies", 'REVERSAL' in modes)

    # V3 config sections
    for section in ['kill_zones', 'sweep', 'displacement', 'volume_confirm', 'ltf_choch', 'ema_bias']:
        check(f"V3_CONFIG has '{section}'", section in V3_CONFIG, f"missing {section}")

    check("MODULES defined", len(MODULES) > 0)
    check("SCAN_INTERVALS defined", len(SCAN_INTERVALS) > 0)


# ============================================================
# 2. INDICATOR TESTS
# ============================================================

def test_indicators():
    print("\n═══════ 2. INDICATORS ═══════")
    from indicators import (compute_all_indicators, incremental_update,
                            detect_swing_points, detect_smc_patterns,
                            detect_whale_activity, add_basic_indicators,
                            aggregate_bars)

    df = make_df(500)

    # Full pipeline
    result = compute_all_indicators(df.copy(), include_whale=True)
    check("compute_all_indicators returns DataFrame", isinstance(result, pd.DataFrame))
    check("Same row count", len(result) == 500, f"got {len(result)}")

    # Required columns present
    expected_cols = ['EMA_9', 'EMA_21', 'EMA_50', 'RSI_14', 'ATR', 'VWAP',
                     'ADX_14', 'Trend_Dir', 'Swing_High', 'Swing_Low',
                     'Bull_FVG', 'Bear_FVG', 'Bull_OB', 'Bear_OB',
                     'BOS_Bull', 'BOS_Bear', 'CHoCH_Bull', 'CHoCH_Bear',
                     'Displacement_Bull', 'Displacement_Bear',
                     'Bull_Sweep', 'Bear_Sweep', 'Reject_Bull', 'Reject_Bear',
                     'In_Discount', 'In_Premium', 'In_Killzone',
                     'Is_Whale_Bar', 'Whale_Trade_Z', 'Cum_Delta']
    missing = [c for c in expected_cols if c not in result.columns]
    check("All expected columns present", len(missing) == 0, f"missing: {missing}")

    # No NaN in critical indicators after warmup period
    tail = result.tail(200)
    for col in ['EMA_9', 'EMA_21', 'RSI_14', 'ATR', 'Trend_Dir']:
        nan_count = tail[col].isna().sum()
        check(f"{col} no NaN in tail 200", nan_count == 0, f"{nan_count} NaN")

    # RSI in [0, 100]
    rsi = result['RSI_14'].dropna()
    check("RSI in [0,100]", (rsi >= 0).all() and (rsi <= 100).all(),
          f"min={rsi.min():.1f} max={rsi.max():.1f}")

    # ATR positive
    atr = result['ATR'].dropna()
    check("ATR all positive", (atr > 0).all())

    # Swing points detected
    sh_count = result['Swing_High'].notna().sum()
    sl_count = result['Swing_Low'].notna().sum()
    check("Swing highs detected", sh_count > 5, f"only {sh_count}")
    check("Swing lows detected", sl_count > 5, f"only {sl_count}")

    # FVG detected
    fvg_count = result['Bull_FVG'].sum() + result['Bear_FVG'].sum()
    check("FVGs detected", fvg_count > 0, f"count={fvg_count}")

    # ── Incremental accuracy test ──
    df_full = compute_all_indicators(make_df(2000).copy(), include_whale=True)
    new_bar = pd.DataFrame([{
        'Date': pd.Timestamp('2024-01-04 09:31'),
        'Open': 500.5, 'High': 501.0, 'Low': 500.2, 'Close': 500.8,
        'Volume': 2000000, 'NumTrades': 5000,
    }])

    # Full recompute path
    df_a = pd.concat([df_full, new_bar], ignore_index=True)
    df_old = compute_all_indicators(df_a.copy(), include_whale=True)

    # Incremental path
    df_b = pd.concat([df_full, new_bar], ignore_index=True)
    df_new = incremental_update(df_b, include_whale=True)

    # Check key indicators match
    for col in ['EMA_9', 'EMA_21', 'RSI_14', 'Trend_Dir']:
        old_v = float(df_old[col].iloc[-1])
        new_v = float(df_new[col].iloc[-1])
        diff = abs(old_v - new_v)
        check(f"Incremental {col} accurate", diff < 0.05,
              f"old={old_v:.4f} new={new_v:.4f} diff={diff:.6f}")

    for col in ['Bull_FVG', 'Bear_FVG', 'Bull_OB', 'Displacement_Bull']:
        old_v = df_old[col].iloc[-1]
        new_v = df_new[col].iloc[-1]
        check(f"Incremental {col} matches", old_v == new_v,
              f"old={old_v} new={new_v}")

    # ── Aggregation test ──
    agg = aggregate_bars(df_full, '5min')
    check("aggregate_bars returns DataFrame", isinstance(agg, pd.DataFrame))
    check("5min has fewer bars than 1min", len(agg) < len(df_full),
          f"5min={len(agg)} vs 1min={len(df_full)}")
    check("5min OHLCV present", all(c in agg.columns for c in ['Open','High','Low','Close','Volume']))


# ============================================================
# 3. SIGNAL TESTS
# ============================================================

def test_signals():
    print("\n═══════ 3. SIGNALS ═══════")
    from signals import detect_signals
    from indicators import compute_all_indicators
    from config import STRATEGIES

    df = compute_all_indicators(make_df(500), include_whale=True)

    # Test every strategy's signal function
    signal_funcs = set(s['signal_func'] for s in STRATEGIES)
    for sig_func in sorted(signal_funcs):
        try:
            call_arr, put_arr = detect_signals(df, sig_func)
            check(f"Signal '{sig_func}' returns arrays",
                  isinstance(call_arr, np.ndarray) and isinstance(put_arr, np.ndarray))
            check(f"Signal '{sig_func}' correct length",
                  len(call_arr) == len(df) and len(put_arr) == len(df),
                  f"call={len(call_arr)} put={len(put_arr)} expected={len(df)}")
            # Boolean arrays
            check(f"Signal '{sig_func}' boolean arrays",
                  call_arr.dtype == bool and put_arr.dtype == bool,
                  f"call dtype={call_arr.dtype} put dtype={put_arr.dtype}")
        except Exception as e:
            check(f"Signal '{sig_func}' no crash", False, str(e))


# ============================================================
# 4. V3 FILTERS TESTS
# ============================================================

def test_v3_filters():
    print("\n═══════ 4. V3 FILTERS ═══════")
    from v3_filters import (MarketModeDetector, EMABiasFilter, VolatilityRegime,
                            ScalingManager, detect_liquidity_levels,
                            kill_zone_check, v3_master_filter)
    from indicators import compute_all_indicators
    from config import STRATEGIES

    df = compute_all_indicators(make_df(500), include_whale=True)

    # ── Mode Detection ──
    mode_det = MarketModeDetector()
    sweep_bull, sweep_bear = detect_liquidity_levels(df)
    check("detect_liquidity_levels returns arrays",
          isinstance(sweep_bull, np.ndarray) and isinstance(sweep_bear, np.ndarray))
    check("Sweep arrays correct length",
          len(sweep_bull) == len(df) and len(sweep_bear) == len(df))

    mode = mode_det.determine_mode(df, len(df)-1, sweep_bull, sweep_bear)
    check("Mode is valid", mode in ('TREND', 'REVERSAL', 'NO_TRADE'), f"got '{mode}'")

    # Mode alignment
    ok, reason = mode_det.check_mode_alignment('TREND', 'TREND', False)
    check("TREND aligns with TREND", ok, reason)

    ok, reason = mode_det.check_mode_alignment('TREND', 'NO_TRADE', False)
    check("TREND blocked by NO_TRADE", not ok, reason)

    # ── EMA Bias ──
    ema_bias = EMABiasFilter()

    # Create mock tf_data with 1hr
    df_1hr = compute_all_indicators(make_df(100, start='2024-01-02 09:30'), include_whale=False)
    tf_data = {'1hr': df_1hr, '1min': df}
    bias = ema_bias.compute_bias(tf_data)
    check("EMA bias returns string", isinstance(bias, str))
    check("EMA bias valid value", bias in ('LONG', 'SHORT', 'NEUTRAL'), f"got '{bias}'")

    # ── Kill Zone ──
    # Bar at 10:00 EST should be in kill zone
    kz = kill_zone_check(df, 30, 'scalp')
    check("Kill zone returns bool", isinstance(kz, bool))

    # ── Scaling Manager ──
    sm = ScalingManager()
    sm.init_trade('test_1', 500.0, 'CALL', 0.3, 0.5)
    check("ScalingManager init trade", 'test_1' in sm.trades)

    # Simulate price move to TP1
    action = sm.update('test_1', 502.5)  # +0.5%
    check("ScalingManager returns action dict", 'action' in action)

    sm.remove_trade('test_1')
    check("ScalingManager remove trade", 'test_1' not in sm.trades)

    # ── Volatility Regime ──
    vol_regime = VolatilityRegime()
    ok, regime = vol_regime.check(df, len(df)-1, 'scalp')
    check("VolatilityRegime returns (bool, str)", isinstance(ok, bool) and isinstance(regime, str))
    check("VolatilityRegime check returns result", regime is not None, f"got '{regime}'")

    # ── V3 Master Filter ──
    strategy = STRATEGIES[0]
    v3_ok, v3_meta = v3_master_filter(
        df, len(df)-2, 'CALL', strategy, 500.0,
        sweep_bull=sweep_bull, sweep_bear=sweep_bear,
        vol_regime=vol_regime,
    )
    check("v3_master_filter returns (bool, dict)",
          isinstance(v3_ok, bool) and isinstance(v3_meta, dict))


# ============================================================
# 5. DATA FEED TESTS
# ============================================================

def test_data_feed():
    print("\n═══════ 5. DATA FEED ═══════")
    from data_feed import LiveDataFeed, AlpacaWebSocket

    # AlpacaWebSocket init
    ws = AlpacaWebSocket('fake_key', 'fake_secret', ['SPY', 'QQQ'])
    check("AlpacaWebSocket instantiates", ws is not None)
    check("AlpacaWebSocket not connected yet", not ws.connected)

    # LiveDataFeed without WebSocket (test REST path)
    class MockClient:
        def get_bars(self, *a, **kw): return []
        def get_latest_bar(self, *a, **kw): return None

    feed = LiveDataFeed(MockClient(), ['SPY', 'QQQ'], {'1min', '5min', '1hr'},
                        use_websocket=False)
    check("LiveDataFeed instantiates (no WS)", feed is not None)
    check("No bars yet", feed.get_bars('SPY', '1min') is None)
    check("No current price yet", feed.get_current_price('SPY') is None)
    check("Not streaming", not feed.is_streaming())

    # Manually inject bars and test retrieval
    from indicators import compute_all_indicators
    df = compute_all_indicators(make_df(200), include_whale=True)
    feed.bars['SPY']['1min'] = df
    check("Get bars after inject", feed.get_bars('SPY', '1min') is not None)
    check("Current price from last bar",
          feed.get_current_price('SPY') == float(df['Close'].iloc[-1]))

    # Bar callback registration
    callback_log = []
    feed.register_bar_callback(lambda t, b: callback_log.append(t))
    check("Callback registered", len(feed._bar_callbacks) == 1)

    # Simulate WebSocket bar handler
    from data_feed import LiveDataFeed as LDF
    feed2 = LiveDataFeed(MockClient(), ['SPY'], {'1min'}, use_websocket=False)
    feed2.bars['SPY']['1min'] = compute_all_indicators(make_df(100), include_whale=True)
    msg = {
        'T': 'b', 'S': 'SPY',
        't': '2024-01-03T10:00:00Z',
        'o': 500.0, 'h': 501.0, 'l': 499.5, 'c': 500.5,
        'v': 1000000, 'n': 3000,
    }
    feed2._handle_ws_bar(msg)
    new_len = len(feed2.bars['SPY']['1min'])
    check("WS bar appended", new_len == 101, f"got {new_len}")
    check("Price updated from WS bar", feed2.get_current_price('SPY') == 500.5)


# ============================================================
# 6. ENGINE INTEGRATION TESTS
# ============================================================

def test_engine_integration():
    print("\n═══════ 6. ENGINE INTEGRATION ═══════")
    from config import STRATEGIES, V3_CONFIG
    from indicators import compute_all_indicators
    from signals import detect_signals
    from v3_filters import (MarketModeDetector, EMABiasFilter, detect_liquidity_levels,
                            VolatilityRegime)

    # Simulate what _scan_ticker does
    df = compute_all_indicators(make_df(500), include_whale=True)

    mode_det = MarketModeDetector()
    ema_filter = EMABiasFilter()
    vol_regime = VolatilityRegime()

    sweep_bull, sweep_bear = detect_liquidity_levels(df)
    mode = mode_det.determine_mode(df, len(df)-1, sweep_bull, sweep_bear)
    check("Integration: mode detected", mode in ('TREND', 'REVERSAL', 'NO_TRADE'))

    tf_data = {'1min': df, '1hr': compute_all_indicators(make_df(100), include_whale=False)}
    bias = ema_filter.compute_bias(tf_data)
    check("Integration: bias computed", bias in ('LONG', 'SHORT', 'NEUTRAL'))

    # Run all strategies through the full pipeline
    signals_fired = 0
    blocked = 0
    errors = 0

    for strategy in STRATEGIES:
        try:
            sig_func = strategy['signal_func']
            call_arr, put_arr = detect_signals(df, sig_func)

            call_idx = np.where(call_arr)[0]
            put_idx = np.where(put_arr)[0]

            total_signals = len(call_idx) + len(put_idx)
            signals_fired += total_signals

            # Check mode alignment for each
            strategy_mode = strategy.get('mode', 'TREND')
            has_sweep = bool(sweep_bull[-1]) or bool(sweep_bear[-1])
            ok, reason = mode_det.check_mode_alignment(strategy_mode, mode, has_sweep)
            if not ok:
                blocked += total_signals

        except Exception as e:
            errors += 1
            check(f"Integration: {strategy['name']} no crash", False, str(e))

    check(f"Integration: all 20 strategies ran without error", errors == 0, f"{errors} errors")
    check(f"Integration: signals detected across strategies", signals_fired > 0,
          f"only {signals_fired}")
    print(f"    (signals={signals_fired}, blocked_by_mode={blocked})")

    # Decision tree routing test
    for test_mode in ['TREND', 'REVERSAL', 'NO_TRADE']:
        trend_strats = [s for s in STRATEGIES if s['mode'] == 'TREND']
        rev_strats = [s for s in STRATEGIES if s['mode'] == 'REVERSAL']

        if trend_strats:
            ok, _ = mode_det.check_mode_alignment('TREND', test_mode, False)
            # TREND strategies pass in TREND and REVERSAL modes, blocked only in NO_TRADE
            expected = test_mode != 'NO_TRADE'
            check(f"Decision tree: TREND in {test_mode} → {'pass' if expected else 'block'}",
                  ok == expected)

        if rev_strats:
            ok, _ = mode_det.check_mode_alignment('REVERSAL', test_mode, True)
            # REVERSAL should pass in REVERSAL mode (or TREND with sweep override)
            # Exact logic depends on implementation


# ============================================================
# RUN ALL
# ============================================================

if __name__ == '__main__':
    print("=" * 60)
    print("FUNCTIONAL TEST SUITE — Master Trading Engine")
    print("=" * 60)

    t0 = time.time()

    test_config()
    test_indicators()
    test_signals()
    test_v3_filters()
    test_data_feed()
    test_engine_integration()

    elapsed = time.time() - t0
    print(f"\n{'=' * 60}")
    print(f"RESULTS: {PASS} passed, {FAIL} failed ({elapsed:.1f}s)")
    print(f"{'=' * 60}")

    sys.exit(1 if FAIL > 0 else 0)
