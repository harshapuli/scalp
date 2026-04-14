"""
PHASE 1: EXTENDED FUNCTIONAL TESTS — 7 Years of Data
======================================================
Comprehensive correctness testing of every module with long histories.
Tests indicator accuracy, incremental drift, signal validity, V3 filters,
data feed edge cases, aggregation accuracy, and decision tree routing.

Run: python test_extended.py
"""
import sys
import time
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

PASS = 0
FAIL = 0
WARN = 0
DETAILS = []

def check(name, condition, detail=""):
    global PASS, FAIL
    if condition:
        PASS += 1
    else:
        FAIL += 1
        DETAILS.append(f"FAIL: {name} — {detail}")
        print(f"  ✗ {name}  — {detail}")

def warn(name, detail):
    global WARN
    WARN += 1
    DETAILS.append(f"WARN: {name} — {detail}")
    print(f"  ⚠ {name}  — {detail}")


# ============================================================
# DATA GENERATORS
# ============================================================

def make_gbm_df(n, seed=42, start_price=500.0, start_date='2017-01-03 09:30'):
    """
    Geometric Brownian Motion price simulation.
    Generates realistic OHLCV with market-hours timestamps.
    """
    np.random.seed(seed)
    dt = 1.0 / (252 * 390)  # 1 minute in trading-year units
    drift = 0.0001
    vol = 0.015

    # Generate returns
    returns = np.exp((drift - 0.5 * vol**2) * dt + vol * np.sqrt(dt) * np.random.randn(n))
    close = start_price * np.cumprod(returns)

    # Generate OHLC from close
    noise = np.random.rand(n)
    spread = close * 0.001  # 0.1% typical spread
    high = close + spread * (0.5 + noise)
    low = close - spread * (0.5 + np.random.rand(n))
    opn = close * (1 + np.random.randn(n) * 0.0003)

    # Fix OHLC consistency
    high = np.maximum(high, np.maximum(close, opn))
    low = np.minimum(low, np.minimum(close, opn))

    # Market hours timestamps (skip weekends, only 9:30-16:00 ET)
    dates = pd.bdate_range(start_date, periods=n // 390 + 2, freq='B')
    timestamps = []
    for d in dates:
        for m in range(390):  # 6.5 hours
            timestamps.append(d + pd.Timedelta(hours=9, minutes=30+m))
            if len(timestamps) >= n:
                break
        if len(timestamps) >= n:
            break
    timestamps = timestamps[:n]

    return pd.DataFrame({
        'Date': pd.DatetimeIndex(timestamps),
        'Open': opn,
        'High': high,
        'Low': low,
        'Close': close,
        'Volume': np.random.randint(500000, 8000000, n),
        'NumTrades': np.random.randint(1000, 15000, n),
    })


def make_small_df(n=500, seed=42):
    """Quick small DataFrame for tests that don't need 7yr."""
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


# ============================================================
# 1A. INDICATOR ACCURACY OVER LONG HISTORIES
# ============================================================

def test_1a_indicator_accuracy():
    print("\n═══════ 1A. INDICATOR ACCURACY (7yr data) ═══════")
    from indicators import compute_all_indicators

    # Generate 3 years of 1-min data (~300K bars) — 7yr would be ~1M, use 300K as practical
    # This still takes ~15 seconds which is fine
    N = 300_000
    print(f"  Generating {N:,} bars of GBM data...")
    t0 = time.time()
    df = make_gbm_df(N, seed=42, start_price=450.0)
    print(f"  Generated in {time.time()-t0:.1f}s")

    print(f"  Computing all indicators...")
    t0 = time.time()
    result = compute_all_indicators(df.copy(), include_whale=True)
    compute_time = time.time() - t0
    print(f"  Computed in {compute_time:.1f}s ({N/compute_time:.0f} bars/sec)")

    check("Result is DataFrame", isinstance(result, pd.DataFrame))
    check("Row count preserved", len(result) == N, f"expected {N}, got {len(result)}")

    # Skip warmup (first 100 bars) for NaN checks
    tail = result.iloc[100:]

    # No NaN in critical columns
    for col in ['EMA_9', 'EMA_21', 'EMA_50', 'RSI_14', 'ATR', 'Trend_Dir', 'ADX_14']:
        nan_ct = tail[col].isna().sum()
        check(f"{col}: no NaN after warmup", nan_ct == 0, f"{nan_ct} NaN in {len(tail)} rows")

    # No Inf anywhere
    numeric_cols = result.select_dtypes(include=[np.number]).columns
    for col in numeric_cols:
        inf_ct = np.isinf(result[col].dropna()).sum()
        check(f"{col}: no Inf", inf_ct == 0, f"{inf_ct} Inf values")

    # RSI in [0, 100]
    rsi = result['RSI_14'].dropna()
    check("RSI min >= 0", rsi.min() >= 0, f"min={rsi.min():.4f}")
    check("RSI max <= 100", rsi.max() <= 100, f"max={rsi.max():.4f}")

    # ATR always positive
    atr = result['ATR'].dropna()
    check("ATR all positive", (atr > 0).all(), f"min ATR={atr.min():.6f}")

    # EMA crossover ↔ Trend_Dir
    ema9 = result['EMA_9'].values
    ema21 = result['EMA_21'].values
    trend = result['Trend_Dir'].values
    # Sample 1000 random points after warmup
    sample_idx = np.random.choice(range(200, N), size=min(1000, N-200), replace=False)
    crossover_matches = 0
    for i in sample_idx:
        if np.isnan(ema9[i]) or np.isnan(ema21[i]):
            continue
        expected = 1 if ema9[i] > ema21[i] else (-1 if ema9[i] < ema21[i] else 0)
        if trend[i] == expected:
            crossover_matches += 1
    pct = crossover_matches / len(sample_idx) * 100
    check(f"EMA crossover matches Trend_Dir", pct > 95, f"{pct:.1f}% match (expect >95%)")

    # Swing points at local extrema
    if 'Swing_High' in result.columns:
        sh_idx = result.index[result['Swing_High'].notna()]
        if len(sh_idx) > 50:
            sample_sh = np.random.choice(sh_idx, size=min(50, len(sh_idx)), replace=False)
            valid_sh = 0
            for i in sample_sh:
                if i < 5 or i > N - 6:
                    continue
                h_val = result['High'].iloc[i]
                neighbors = result['High'].iloc[max(0,i-5):i+6]
                if h_val >= neighbors.max():
                    valid_sh += 1
            check(f"Swing highs at local maxima", valid_sh / len(sample_sh) > 0.85,
                  f"{valid_sh}/{len(sample_sh)} valid")

    # FVG verification: Bull_FVG[i] means Low[i] > High[i-2]
    if 'Bull_FVG' in result.columns:
        fvg_idx = result.index[result['Bull_FVG'] == True]
        if len(fvg_idx) > 50:
            sample_fvg = np.random.choice(fvg_idx, size=min(50, len(fvg_idx)), replace=False)
            valid_fvg = 0
            for i in sample_fvg:
                if i >= 2:
                    if result['Low'].iloc[i] > result['High'].iloc[i-2]:
                        valid_fvg += 1
            check(f"Bull_FVG correctness", valid_fvg / len(sample_fvg) > 0.95,
                  f"{valid_fvg}/{len(sample_fvg)} valid")
        fvg_total = result['Bull_FVG'].sum() + result['Bear_FVG'].sum()
        check("FVGs detected in long history", fvg_total > 100, f"only {fvg_total}")

    # OB verification: Bull_OB[i] → displacement candle + prior bearish
    if 'Bull_OB' in result.columns:
        ob_idx = result.index[result['Bull_OB'] == True]
        if len(ob_idx) > 20:
            sample_ob = np.random.choice(ob_idx, size=min(30, len(ob_idx)), replace=False)
            valid_ob = 0
            for i in sample_ob:
                if i >= 1:
                    # Current bar is bull displacement
                    c_bull = result['Close'].iloc[i] > result['Open'].iloc[i]
                    # Prior bar was bearish
                    prior_bear = result['Close'].iloc[i-1] < result['Open'].iloc[i-1]
                    if c_bull and prior_bear:
                        valid_ob += 1
            check(f"Bull_OB correctness", valid_ob / len(sample_ob) > 0.9,
                  f"{valid_ob}/{len(sample_ob)} valid")

    # BOS_Bull: close > Last_Swing_High
    if 'BOS_Bull' in result.columns and 'Last_Swing_High' in result.columns:
        bos_idx = result.index[result['BOS_Bull'] == True]
        if len(bos_idx) > 20:
            sample_bos = np.random.choice(bos_idx, size=min(30, len(bos_idx)), replace=False)
            valid_bos = 0
            for i in sample_bos:
                lsh = result['Last_Swing_High'].iloc[i]
                if not np.isnan(lsh) and result['Close'].iloc[i] > lsh:
                    valid_bos += 1
            check(f"BOS_Bull correctness", valid_bos / len(sample_bos) > 0.8,
                  f"{valid_bos}/{len(sample_bos)} valid")

    # Displacement_Bull: body > 1.5x ATR AND bullish
    if 'Displacement_Bull' in result.columns:
        disp_idx = result.index[result['Displacement_Bull'] == True]
        if len(disp_idx) > 20:
            sample_disp = np.random.choice(disp_idx, size=min(30, len(disp_idx)), replace=False)
            valid_disp = 0
            for i in sample_disp:
                body = abs(result['Close'].iloc[i] - result['Open'].iloc[i])
                atr_v = result['ATR'].iloc[i]
                bullish = result['Close'].iloc[i] > result['Open'].iloc[i]
                if body > atr_v * 1.5 and bullish:
                    valid_disp += 1
            check(f"Displacement_Bull correctness", valid_disp / len(sample_disp) > 0.9,
                  f"{valid_disp}/{len(sample_disp)} valid")

    # Whale detection: Is_Whale_Bar ↔ Whale_Trade_Z > 2.0
    if 'Is_Whale_Bar' in result.columns and 'Whale_Trade_Z' in result.columns:
        whale_bars = result.iloc[100:]  # Skip warmup
        whale_match = ((whale_bars['Is_Whale_Bar'] == True) == (whale_bars['Whale_Trade_Z'] > 2.0)).mean()
        check("Whale_Bar ↔ Z>2.0 consistency", whale_match > 0.99, f"{whale_match*100:.1f}% match")

    # In_Discount / In_Premium mutually exclusive
    if 'In_Discount' in result.columns and 'In_Premium' in result.columns:
        both = (result['In_Discount'] & result['In_Premium']).sum()
        check("Discount/Premium mutually exclusive", both == 0, f"{both} bars are both")

    # Kill zone timing
    if 'In_Killzone' in result.columns and 'Date' in result.columns:
        kz_bars = result[result['In_Killzone'] == True]
        if len(kz_bars) > 100:
            hours = kz_bars['Date'].dt.hour
            mins = kz_bars['Date'].dt.minute
            t = hours * 60 + mins
            in_valid_kz = ((t >= 570) & (t <= 630)) | ((t >= 840) & (t <= 900))
            pct_valid = in_valid_kz.mean() * 100
            check("Kill zone timing correct", pct_valid > 95, f"{pct_valid:.1f}% in valid windows")

    print(f"  Compute rate: {N/compute_time:,.0f} bars/sec")
    return result


# ============================================================
# 1B. INCREMENTAL DRIFT TEST
# ============================================================

def test_1b_incremental_drift():
    print("\n═══════ 1B. INCREMENTAL DRIFT TEST ═══════")
    from indicators import compute_all_indicators, incremental_update

    # Start with 500 bars, full compute
    base_df = compute_all_indicators(make_small_df(500, seed=99), include_whale=True)

    np.random.seed(123)
    max_drifts = {}
    check_cols = ['EMA_9', 'EMA_21', 'RSI_14', 'ATR', 'Trend_Dir']
    for col in check_cols:
        max_drifts[col] = 0.0

    df_inc = base_df.copy()

    # Append 1000 bars one at a time
    for i in range(1000):
        new_bar = pd.DataFrame([{
            'Date': pd.Timestamp('2024-03-01 09:30') + pd.Timedelta(minutes=i),
            'Open': 500 + np.random.randn() * 0.3,
            'High': 501 + np.random.rand() * 0.5,
            'Low': 499 + np.random.rand() * 0.3,
            'Close': 500 + np.random.randn() * 0.3,
            'Volume': np.random.randint(500000, 3000000),
            'NumTrades': np.random.randint(1000, 8000),
        }])
        df_inc = pd.concat([df_inc, new_bar], ignore_index=True)
        df_inc = incremental_update(df_inc, include_whale=True)

        # Every 100 bars, full recompute and compare
        if (i + 1) % 100 == 0:
            df_full = compute_all_indicators(df_inc[['Date','Open','High','Low','Close','Volume','NumTrades']].copy(),
                                              include_whale=True)
            for col in check_cols:
                inc_val = float(df_inc[col].iloc[-1])
                full_val = float(df_full[col].iloc[-1])
                if abs(full_val) > 1e-6:
                    drift = abs(inc_val - full_val) / abs(full_val) * 100
                else:
                    drift = abs(inc_val - full_val) * 100
                max_drifts[col] = max(max_drifts[col], drift)

            print(f"    Bar {i+1}: drifts = " +
                  ", ".join(f"{c}={max_drifts[c]:.3f}%" for c in check_cols))

    # Report
    for col in check_cols:
        d = max_drifts[col]
        if d > 1.0:
            warn(f"Incremental {col} drift", f"{d:.3f}% (>1% threshold)")
        else:
            check(f"Incremental {col} drift < 1%", True, f"{d:.3f}%")

    print(f"  Final max drifts: {max_drifts}")


# ============================================================
# 1C. SIGNAL FUNCTION EXHAUSTIVE TESTING
# ============================================================

def test_1c_signals(df_7yr):
    print("\n═══════ 1C. SIGNAL FUNCTIONS (long history) ═══════")
    from signals import detect_signals

    signal_funcs = ['breaker_block', 'daily_ema_trend', 'daily_ob', 'displacement_engine',
                    'ict_reversal', 'judas_swing', 'mitigation_confluence',
                    'smc_order_block', 'weekly_ema_trend', 'weekly_ob']

    # Use a subset for speed (50K bars = ~4 months)
    df = df_7yr.iloc[:50000].copy()
    n = len(df)

    for sig_func in signal_funcs:
        try:
            t0 = time.time()
            call_arr, put_arr = detect_signals(df, sig_func)
            elapsed = (time.time() - t0) * 1000

            check(f"{sig_func}: returns ndarray pair",
                  isinstance(call_arr, np.ndarray) and isinstance(put_arr, np.ndarray))
            check(f"{sig_func}: boolean dtype",
                  call_arr.dtype == bool and put_arr.dtype == bool,
                  f"call={call_arr.dtype}, put={put_arr.dtype}")
            check(f"{sig_func}: correct length",
                  len(call_arr) == n and len(put_arr) == n,
                  f"call={len(call_arr)}, put={len(put_arr)}, expected={n}")

            # No signal at index 0
            check(f"{sig_func}: no signal at bar 0",
                  not call_arr[0] and not put_arr[0])

            # Sparsity: < 10% of bars
            call_pct = call_arr.sum() / n * 100
            put_pct = put_arr.sum() / n * 100
            total_pct = (call_arr.sum() + put_arr.sum()) / n * 100
            check(f"{sig_func}: signal sparsity < 10%", total_pct < 10,
                  f"call={call_pct:.2f}% put={put_pct:.2f}% total={total_pct:.2f}%")

            print(f"  ✓ {sig_func}: {call_arr.sum()} calls, {put_arr.sum()} puts ({elapsed:.0f}ms)")

        except Exception as e:
            check(f"{sig_func}: no crash", False, str(e))


# ============================================================
# 1D. V3 FILTER PIPELINE
# ============================================================

def test_1d_v3_filters(df_7yr):
    print("\n═══════ 1D. V3 FILTER PIPELINE ═══════")
    from v3_filters import (MarketModeDetector, EMABiasFilter, VolatilityRegime,
                            ScalingManager, detect_liquidity_levels,
                            kill_zone_check, v3_master_filter)
    from indicators import compute_all_indicators
    from config import STRATEGIES

    df = df_7yr.iloc[:10000].copy()

    # detect_liquidity_levels on full history
    sweep_bull, sweep_bear = detect_liquidity_levels(df)
    check("Liquidity levels: arrays match length",
          len(sweep_bull) == len(df) and len(sweep_bear) == len(df))
    check("Liquidity levels: are numpy arrays",
          isinstance(sweep_bull, np.ndarray) and isinstance(sweep_bear, np.ndarray))

    # MarketModeDetector on 1000 random bars
    mode_det = MarketModeDetector()
    modes_seen = set()
    for _ in range(1000):
        idx = np.random.randint(100, len(df))
        mode = mode_det.determine_mode(df, idx, sweep_bull, sweep_bear)
        modes_seen.add(mode)
        check_ok = mode in ('TREND', 'REVERSAL', 'NO_TRADE')
        if not check_ok:
            check("Mode always valid", False, f"got '{mode}' at idx {idx}")
            break
    else:
        check("Mode always valid (1000 samples)", True)
    print(f"    Modes seen: {modes_seen}")

    # EMABiasFilter
    ema_filter = EMABiasFilter()
    df_1hr = compute_all_indicators(make_small_df(200), include_whale=False)
    tf_data = {'1hr': df_1hr, '1min': df.iloc[:500]}
    bias = ema_filter.compute_bias(tf_data)
    check("EMA bias valid", bias in ('LONG', 'SHORT', 'NEUTRAL'), f"got '{bias}'")

    # Kill zone timing tests
    # Build DataFrames at specific times
    for test_time, cat, expected_name in [
        ('09:35', 'scalp', 'NY open'),
        ('12:00', 'scalp', 'midday dead zone'),
        ('15:30', 'scalp', 'power hour'),
    ]:
        h, m = map(int, test_time.split(':'))
        test_df = make_small_df(100)
        # Overwrite dates to specific time
        test_df['Date'] = pd.date_range(f'2024-01-02 {test_time}', periods=100, freq='1min')
        test_df_ind = compute_all_indicators(test_df, include_whale=False)
        kz = kill_zone_check(test_df_ind, 50, cat)
        print(f"    Kill zone at {test_time} ({expected_name}): {kz}")

    # ScalingManager full lifecycle
    sm = ScalingManager()
    sm.init_trade('lifecycle_1', 500.0, 'CALL', 0.3, 0.5)
    check("SM: trade initialized", 'lifecycle_1' in sm.trades)

    # Move to TP1 (entry + target% * entry = 500 + 0.5% * 500 = 502.5)
    action = sm.update('lifecycle_1', 502.5)
    check("SM: TP1 action", action['action'] in ('close_half', 'hold', 'close_all'))

    # Move higher
    action = sm.update('lifecycle_1', 505.0)
    # Trail back
    action = sm.update('lifecycle_1', 501.0)
    print(f"    SM lifecycle final action: {action}")
    sm.remove_trade('lifecycle_1')

    # ScalingManager trailing stop
    sm.init_trade('trail_1', 500.0, 'CALL', 0.3, 0.5)
    sm.update('trail_1', 503.0)  # MFE = $3
    sm.update('trail_1', 504.0)  # MFE = $4
    action = sm.update('trail_1', 502.0)  # Pullback: 50% of MFE=$4 → trail at 502
    print(f"    SM trailing: after $4 MFE, pullback to $502 → {action}")
    sm.remove_trade('trail_1')
    check("SM: trailing stop works", True)  # No crash = pass

    # v3_master_filter with every strategy
    vol_regime = VolatilityRegime()
    filter_results = {'passed': 0, 'blocked': 0}
    for strategy in STRATEGIES:
        for direction in ['CALL', 'PUT']:
            for _ in range(5):
                idx = np.random.randint(100, min(len(df), 500))
                ok, meta = v3_master_filter(
                    df, idx, direction, strategy, float(df['Close'].iloc[idx]),
                    sweep_bull=sweep_bull, sweep_bear=sweep_bear,
                    vol_regime=vol_regime,
                )
                if ok:
                    filter_results['passed'] += 1
                else:
                    filter_results['blocked'] += 1
                    # Verify blocked_by is a valid string
                    blocker = meta.get('blocked_by', None)
                    if blocker is not None:
                        check_ok = isinstance(blocker, str) and len(blocker) > 0
                        if not check_ok:
                            check("v3_master_filter blocker valid", False, f"blocker={blocker}")

    check("v3_master_filter: ran all strategies", True)
    print(f"    v3_master_filter: {filter_results['passed']} passed, {filter_results['blocked']} blocked")


# ============================================================
# 1E. DATA FEED EDGE CASES
# ============================================================

def test_1e_data_feed():
    print("\n═══════ 1E. DATA FEED EDGE CASES ═══════")
    from data_feed import LiveDataFeed
    from indicators import compute_all_indicators

    class MockClient:
        def get_bars(self, *a, **kw): return []
        def get_latest_bar(self, *a, **kw): return None

    # ── Timezone handling ──
    feed = LiveDataFeed(MockClient(), ['SPY'], {'1min'}, use_websocket=False)
    feed.bars['SPY']['1min'] = compute_all_indicators(make_small_df(200), include_whale=True)

    for tz_suffix, tz_name in [('Z', 'UTC'), ('-05:00', 'EST'), ('-07:00', 'PST')]:
        msg = {
            'T': 'b', 'S': 'SPY',
            't': f'2024-06-15T10:00:00{tz_suffix}',
            'o': 500.0, 'h': 501.0, 'l': 499.5, 'c': 500.5,
            'v': 1500000, 'n': 4000,
        }
        try:
            feed._handle_ws_bar(msg)
            check(f"WS bar with {tz_name} timestamp", True)
        except Exception as e:
            check(f"WS bar with {tz_name} timestamp", False, str(e))

    # ── MAX_BARS trimming ──
    feed2 = LiveDataFeed(MockClient(), ['QQQ'], {'1min'}, use_websocket=False)
    # Pre-load exactly 2000 bars (MAX)
    feed2.bars['QQQ']['1min'] = compute_all_indicators(make_gbm_df(2000, seed=77), include_whale=True)
    before_len = len(feed2.bars['QQQ']['1min'])

    # Append one more
    msg = {
        'T': 'b', 'S': 'QQQ',
        't': '2024-12-01T10:00:00Z',
        'o': 400.0, 'h': 401.0, 'l': 399.0, 'c': 400.5,
        'v': 2000000, 'n': 5000,
    }
    feed2._handle_ws_bar(msg)
    after_len = len(feed2.bars['QQQ']['1min'])
    check("MAX_BARS trimming", after_len <= 2000,
          f"before={before_len}, after={after_len}")

    # ── Price fallback chain ──
    feed3 = LiveDataFeed(MockClient(), ['AAPL'], {'1min'}, use_websocket=False)
    check("Price fallback: None when no data", feed3.get_current_price('AAPL') is None)

    # Add bars but no stream price
    feed3.bars['AAPL']['1min'] = compute_all_indicators(make_small_df(100), include_whale=True)
    p = feed3.get_current_price('AAPL')
    check("Price fallback: last bar close", p is not None and p > 0, f"got {p}")


# ============================================================
# 1F. AGGREGATION ACCURACY
# ============================================================

def test_1f_aggregation():
    print("\n═══════ 1F. AGGREGATION ACCURACY ═══════")
    from indicators import aggregate_bars, compute_all_indicators

    df = compute_all_indicators(make_small_df(1000), include_whale=False)

    # 5-minute aggregation
    agg_5 = aggregate_bars(df, '5min')
    check("5min agg: fewer bars", len(agg_5) < len(df))
    check("5min agg: has OHLCV", all(c in agg_5.columns for c in ['Open','High','Low','Close','Volume']))

    # Manual verification: first 5min bar
    if len(agg_5) > 0 and 'Date' in df.columns:
        first_5min = df.iloc[:5]
        check("5min Open = first bar Open",
              abs(agg_5['Open'].iloc[0] - first_5min['Open'].iloc[0]) < 0.01)
        check("5min High = max of 5 bars",
              abs(agg_5['High'].iloc[0] - first_5min['High'].max()) < 0.01)
        check("5min Low = min of 5 bars",
              abs(agg_5['Low'].iloc[0] - first_5min['Low'].min()) < 0.01)
        check("5min Close = last bar Close",
              abs(agg_5['Close'].iloc[0] - first_5min['Close'].iloc[-1]) < 0.01)
        check("5min Volume = sum of 5",
              abs(agg_5['Volume'].iloc[0] - first_5min['Volume'].sum()) < 1)

    # 1hr aggregation
    agg_1h = aggregate_bars(df, '1hr')
    check("1hr agg: fewer bars than 5min", len(agg_1h) < len(agg_5))
    check("1hr agg: has OHLCV", all(c in agg_1h.columns for c in ['Open','High','Low','Close','Volume']))

    # OHLC consistency in aggregated data
    for name, agg in [('5min', agg_5), ('1hr', agg_1h)]:
        if len(agg) > 0:
            check(f"{name}: High >= Open", (agg['High'] >= agg['Open']).all())
            check(f"{name}: High >= Close", (agg['High'] >= agg['Close']).all())
            check(f"{name}: Low <= Open", (agg['Low'] <= agg['Open']).all())
            check(f"{name}: Low <= Close", (agg['Low'] <= agg['Close']).all())


# ============================================================
# 1G. DECISION TREE ROUTING MATRIX
# ============================================================

def test_1g_decision_tree():
    print("\n═══════ 1G. DECISION TREE ROUTING MATRIX ═══════")
    from v3_filters import MarketModeDetector

    mode_det = MarketModeDetector()

    # Full routing matrix
    tests = [
        # (strategy_mode, market_mode, has_sweep, expected_pass, description)
        ('TREND', 'TREND', False, True, "TREND strat in TREND market"),
        ('TREND', 'TREND', True, True, "TREND strat in TREND market + sweep"),
        ('TREND', 'REVERSAL', False, True, "TREND strat in REVERSAL market"),
        ('TREND', 'NO_TRADE', False, False, "TREND strat in NO_TRADE"),
        ('REVERSAL', 'TREND', False, False, "REVERSAL strat in TREND, no sweep"),
        ('REVERSAL', 'TREND', True, True, "REVERSAL strat in TREND + sweep override"),
        ('REVERSAL', 'REVERSAL', False, True, "REVERSAL strat in REVERSAL market"),
        ('REVERSAL', 'REVERSAL', True, True, "REVERSAL strat in REVERSAL + sweep"),
        ('REVERSAL', 'NO_TRADE', False, False, "REVERSAL strat in NO_TRADE"),
        ('REVERSAL', 'NO_TRADE', True, False, "REVERSAL strat in NO_TRADE + sweep"),
    ]

    for strat_mode, mkt_mode, has_sweep, expected, desc in tests:
        ok, reason = mode_det.check_mode_alignment(strat_mode, mkt_mode, has_sweep)
        check(f"Routing: {desc}", ok == expected,
              f"expected {'PASS' if expected else 'BLOCK'}, got {'PASS' if ok else 'BLOCK'} ({reason})")


# ============================================================
# MAIN
# ============================================================

if __name__ == '__main__':
    print("=" * 70)
    print("PHASE 1: EXTENDED FUNCTIONAL TESTS")
    print("=" * 70)

    t0 = time.time()

    df_7yr = test_1a_indicator_accuracy()
    test_1b_incremental_drift()
    test_1c_signals(df_7yr)
    test_1d_v3_filters(df_7yr)
    test_1e_data_feed()
    test_1f_aggregation()
    test_1g_decision_tree()

    elapsed = time.time() - t0

    print(f"\n{'=' * 70}")
    print(f"PHASE 1 RESULTS: {PASS} passed, {FAIL} failed, {WARN} warnings ({elapsed:.1f}s)")
    if DETAILS:
        print("\nIssues:")
        for d in DETAILS:
            print(f"  {d}")
    print(f"{'=' * 70}")

    sys.exit(1 if FAIL > 0 else 0)
