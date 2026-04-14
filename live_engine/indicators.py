"""
LIVE TRADING ENGINE — INDICATOR COMPUTATION
=============================================
All technical indicators + SMC pattern detection for live bars.
Runs on DataFrames with OHLCV + NumTrades columns.
"""
import pandas as pd
import numpy as np
from typing import Optional


# ============================================================
# BASIC INDICATORS (EMA, RSI, VWAP, BB, ADX, ATR, SMA)
# ============================================================

def add_basic_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add all standard technical indicators to a DataFrame."""
    df = df.copy()
    c = df['Close']
    h = df['High']
    l = df['Low']
    v = df['Volume']

    # EMAs
    df['EMA_9']  = c.ewm(span=9, adjust=False).mean()
    df['EMA_21'] = c.ewm(span=21, adjust=False).mean()
    df['EMA_50'] = c.ewm(span=50, adjust=False).mean()
    df['SMA_20'] = c.rolling(20).mean()
    df['SMA_50'] = c.rolling(50).mean()

    # RSI-14
    delta = c.diff()
    gain = delta.clip(lower=0).rolling(14).mean()
    loss = (-delta.clip(upper=0)).rolling(14).mean()
    rs = gain / (loss + 1e-10)
    df['RSI_14'] = 100 - (100 / (1 + rs))

    # ATR-14
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    df['ATR'] = tr.rolling(14).mean()

    # Bollinger Bands
    df['BB_Upper'] = df['SMA_20'] + 2 * c.rolling(20).std()
    df['BB_Lower'] = df['SMA_20'] - 2 * c.rolling(20).std()
    bb_width = df['BB_Upper'] - df['BB_Lower']
    bb_avg = bb_width.rolling(20).mean()
    df['BB_Squeeze'] = bb_width < (bb_avg * 0.8)

    # VWAP (intraday — resets daily)
    if 'Date' in df.columns:
        df['_date'] = df['Date'].dt.date
        cum_vp = (df['Close'] * df['Volume']).groupby(df['_date']).cumsum()
        cum_v = df['Volume'].groupby(df['_date']).cumsum()
        df['VWAP'] = cum_vp / (cum_v + 1e-10)
        df.drop('_date', axis=1, inplace=True)
    else:
        df['VWAP'] = df['SMA_20']

    # Volume MA
    df['Volume_MA_20'] = v.rolling(20).mean()

    # ADX-14
    df = _compute_adx(df)

    # Trend direction (EMA-based)
    df['Trend_Dir'] = np.where(df['EMA_9'] > df['EMA_21'], 1,
                      np.where(df['EMA_9'] < df['EMA_21'], -1, 0))

    return df


def _compute_adx(df: pd.DataFrame, period: int = 14) -> pd.DataFrame:
    """ADX indicator."""
    h, l, c = df['High'], df['Low'], df['Close']
    plus_dm = h.diff().clip(lower=0)
    minus_dm = (-l.diff()).clip(lower=0)
    # When +DM > -DM, keep +DM, else 0 (and vice versa)
    plus_dm = np.where(plus_dm > minus_dm, plus_dm, 0)
    minus_dm_v = np.where(pd.Series(minus_dm) > pd.Series(h.diff().clip(lower=0)), minus_dm, 0)

    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(span=period, adjust=False).mean()

    plus_di = 100 * pd.Series(plus_dm).ewm(span=period, adjust=False).mean() / (atr + 1e-10)
    minus_di = 100 * pd.Series(minus_dm_v).ewm(span=period, adjust=False).mean() / (atr + 1e-10)

    dx = 100 * (plus_di - minus_di).abs() / (plus_di + minus_di + 1e-10)
    df['ADX_14'] = dx.ewm(span=period, adjust=False).mean()
    df['Plus_DI'] = plus_di
    df['Minus_DI'] = minus_di
    return df


# ============================================================
# SMC PATTERN DETECTION
# ============================================================

def detect_swing_points(df: pd.DataFrame, left: int = 5, right: int = 5) -> pd.DataFrame:
    """Detect swing highs and lows — vectorized with rolling max/min."""
    df = df.copy()
    highs = df['High'].values
    lows = df['Low'].values
    n = len(df)

    # Vectorized: a swing high at i means highs[i] == max of window [i-left : i+right+1]
    h_series = pd.Series(highs)
    l_series = pd.Series(lows)
    window = left + right + 1

    rolling_max = h_series.rolling(window, center=True, min_periods=window).max().values
    rolling_min = l_series.rolling(window, center=True, min_periods=window).min().values

    swing_high = np.where(highs == rolling_max, highs, np.nan)
    swing_low = np.where(lows == rolling_min, lows, np.nan)

    # Clear edges where rolling window is incomplete
    swing_high[:left] = np.nan
    swing_high[n-right:] = np.nan
    swing_low[:left] = np.nan
    swing_low[n-right:] = np.nan

    df['Swing_High'] = swing_high
    df['Swing_Low'] = swing_low
    df['Last_Swing_High'] = pd.Series(swing_high).ffill().values
    df['Last_Swing_Low'] = pd.Series(swing_low).ffill().values
    df['Recent_Swing_High'] = df['Last_Swing_High']
    df['Recent_Swing_Low'] = df['Last_Swing_Low']
    return df


def detect_smc_patterns(df: pd.DataFrame) -> pd.DataFrame:
    """Full SMC/ICT pattern detection — vectorized (no Python loops)."""
    df = df.copy()
    n = len(df)
    h, l, c, o = df['High'].values, df['Low'].values, df['Close'].values, df['Open'].values

    # --- FVG (Fair Value Gap) — vectorized ---
    bull_fvg = np.zeros(n, dtype=bool)
    bear_fvg = np.zeros(n, dtype=bool)
    if n > 2:
        bull_fvg[2:] = l[2:] > h[:-2]   # bar[i] low > bar[i-2] high
        bear_fvg[2:] = h[2:] < l[:-2]   # bar[i] high < bar[i-2] low
    df['Bull_FVG'] = bull_fvg
    df['Bear_FVG'] = bear_fvg

    # --- FVG Stack Count (rolling 5-bar) ---
    df['Bull_FVG_Stack'] = pd.Series(bull_fvg.astype(int)).rolling(5, min_periods=1).sum().values
    df['Bear_FVG_Stack'] = pd.Series(bear_fvg.astype(int)).rolling(5, min_periods=1).sum().values

    # --- Displacement Candles (body > 1.5x ATR) ---
    body = np.abs(c - o)
    atr = df['ATR'].values if 'ATR' in df.columns else np.ones(n) * 0.01
    disp_mask = body > atr * 1.0
    is_bull_candle = c > o
    is_bear_candle = c < o
    df['Displacement_Bull'] = disp_mask & is_bull_candle
    df['Displacement_Bear'] = disp_mask & is_bear_candle

    # --- Order Blocks — vectorized ---
    bull_ob = np.zeros(n, dtype=bool)
    bear_ob = np.zeros(n, dtype=bool)
    if n > 1:
        # Bull OB: current bar is bull displacement + prior bar was bearish
        bull_ob[1:] = disp_mask[1:] & is_bull_candle[1:] & is_bear_candle[:-1]
        # Bear OB: current bar is bear displacement + prior bar was bullish
        bear_ob[1:] = disp_mask[1:] & is_bear_candle[1:] & is_bull_candle[:-1]
    df['Bull_OB'] = bull_ob
    df['Bear_OB'] = bear_ob

    # --- Break of Structure (BOS) — vectorized via ffill ---
    if 'Last_Swing_High' in df.columns and 'Last_Swing_Low' in df.columns:
        last_sh = df['Last_Swing_High'].values
        last_sl = df['Last_Swing_Low'].values
        bos_bull = np.zeros(n, dtype=bool)
        bos_bear = np.zeros(n, dtype=bool)
        valid_sh = ~np.isnan(last_sh)
        valid_sl = ~np.isnan(last_sl)
        bos_bull = valid_sh & (c > last_sh)
        bos_bear = valid_sl & (c < last_sl)
        # Remove consecutive BOS (only flag the first break)
        bos_bull[1:] = bos_bull[1:] & ~bos_bull[:-1]
        bos_bear[1:] = bos_bear[1:] & ~bos_bear[:-1]
    else:
        bos_bull = np.zeros(n, dtype=bool)
        bos_bear = np.zeros(n, dtype=bool)
    df['BOS_Bull'] = bos_bull
    df['BOS_Bear'] = bos_bear

    # --- Change of Character (CHoCH) — sequential (must track trend state) ---
    # This one truly needs state, but optimized with numpy pre-extraction
    choch_bull = np.zeros(n, dtype=bool)
    choch_bear = np.zeros(n, dtype=bool)
    bos_b = bos_bull.astype(np.int8)
    bos_e = bos_bear.astype(np.int8)
    trend = 0
    for i in range(1, n):
        bb, be = bos_b[i], bos_e[i]
        if bb and trend <= 0:
            choch_bull[i] = True
            trend = 1
        elif be and trend >= 0:
            choch_bear[i] = True
            trend = -1
        elif bb:
            trend = 1
        elif be:
            trend = -1
    df['CHoCH_Bull'] = choch_bull
    df['CHoCH_Bear'] = choch_bear

    # --- Liquidity Sweeps — vectorized ---
    rolling_high = pd.Series(h).rolling(20).max().values
    rolling_low = pd.Series(l).rolling(20).min().values
    bull_sweep = np.zeros(n, dtype=bool)
    bear_sweep = np.zeros(n, dtype=bool)
    if n > 21:
        # Swept high but closed below = bearish sweep (of bull liquidity)
        rh_prev = np.roll(rolling_high, 1)
        rl_prev = np.roll(rolling_low, 1)
        bull_sweep[21:] = (h[21:] > rh_prev[21:]) & (c[21:] < rh_prev[21:])
        bear_sweep[21:] = (l[21:] < rl_prev[21:]) & (c[21:] > rl_prev[21:])
    df['Bull_Sweep'] = bull_sweep
    df['Bear_Sweep'] = bear_sweep

    # --- Rejection Candles — already vectorized ---
    wick_up = h - np.maximum(c, o)
    wick_dn = np.minimum(c, o) - l
    df['Reject_Bull'] = wick_dn > body * 2
    df['Reject_Bear'] = wick_up > body * 2

    # --- Premium / Discount Zones ---
    range_high = pd.Series(h).rolling(50).max()
    range_low = pd.Series(l).rolling(50).min()
    mid = (range_high + range_low) / 2
    df['In_Discount'] = c < mid.values
    df['In_Premium'] = c > mid.values

    # --- OTE Zones (Fibonacci 0.618 - 0.786 of swing) ---
    swing_range = range_high - range_low
    df['In_OTE_Long'] = (c >= (range_low + swing_range * 0.618).values) & (c <= (range_low + swing_range * 0.786).values)
    df['In_OTE_Short'] = (c >= (range_low + swing_range * 0.214).values) & (c <= (range_low + swing_range * 0.382).values)

    # --- Kill Zone Detection (EST: 9:30-10:30, 14:00-15:00) ---
    if 'Date' in df.columns:
        hour = df['Date'].dt.hour
        minute = df['Date'].dt.minute
        t = hour * 60 + minute
        df['In_Killzone'] = ((t >= 570) & (t <= 630)) | ((t >= 840) & (t <= 900))
    else:
        df['In_Killzone'] = True

    return df


# ============================================================
# WHALE / INSTITUTIONAL INDICATORS
# ============================================================

def detect_whale_activity(df: pd.DataFrame, lookback: int = 50) -> pd.DataFrame:
    """Detect institutional activity from trade-level signatures."""
    df = df.copy()
    vol = df['Volume'].values.astype(float)
    nt = df.get('NumTrades', pd.Series(np.ones(len(df)))).values.astype(float)
    n = len(df)
    c = df['Close'].values

    # Average trade size (Volume / NumTrades)
    avg_trade_size = np.where(nt > 0, vol / nt, 0)
    df['Avg_Trade_Size'] = avg_trade_size

    # Z-score of trade size
    ats_mean = pd.Series(avg_trade_size).rolling(lookback, min_periods=10).mean().values
    ats_std = pd.Series(avg_trade_size).rolling(lookback, min_periods=10).std().values
    ats_z = np.where(ats_std > 0, (avg_trade_size - ats_mean) / ats_std, 0)
    df['Whale_Trade_Z'] = ats_z
    df['Is_Whale_Bar'] = ats_z > 2.0
    df['Is_Mega_Whale'] = ats_z > 3.0

    # Absorption: high volume + tiny price move
    vol_z = np.where(
        pd.Series(vol).rolling(lookback, min_periods=10).std().values > 0,
        (vol - pd.Series(vol).rolling(lookback, min_periods=10).mean().values) /
        pd.Series(vol).rolling(lookback, min_periods=10).std().values,
        0
    )
    move = np.abs(np.diff(c, prepend=c[0])) / (c + 1e-10)
    df['Is_Absorption'] = (vol_z > 1.5) & (move < 0.0005)

    # Cumulative delta proxy (buy vs sell pressure)
    buy_vol = np.where(c > df['Open'].values, vol, vol * 0.4)
    sell_vol = np.where(c < df['Open'].values, vol, vol * 0.4)
    df['Cum_Delta'] = pd.Series(buy_vol - sell_vol).cumsum().values

    # Block trade ratio
    df['Block_Trade_Ratio'] = np.where(nt > 0, avg_trade_size / (ats_mean + 1e-10), 0)

    # Whale confirmation: recent whale bar within N bars
    df['Whale_Confirm_5'] = pd.Series(df['Is_Whale_Bar'].astype(int)).rolling(5, min_periods=1).max().fillna(0).astype(bool)
    df['Whale_Confirm_10'] = pd.Series(df['Is_Whale_Bar'].astype(int)).rolling(10, min_periods=1).max().fillna(0).astype(bool)

    return df


# ============================================================
# TIMEFRAME AGGREGATION
# ============================================================

def aggregate_bars(df_1min: pd.DataFrame, target_tf: str) -> pd.DataFrame:
    """Aggregate 1-min bars into higher timeframes."""
    resample_map = {
        '5min': '5min', '15min': '15min', '1hr': '1h',
        '4hr': '4h', 'daily': '1D', 'weekly': '1W',
    }
    rule = resample_map.get(target_tf)
    if rule is None or 'Date' not in df_1min.columns:
        return df_1min

    df = df_1min.set_index('Date').copy()
    agg = df.resample(rule).agg({
        'Open': 'first', 'High': 'max', 'Low': 'min', 'Close': 'last',
        'Volume': 'sum',
    }).dropna(subset=['Open'])

    if 'NumTrades' in df.columns:
        agg['NumTrades'] = df['NumTrades'].resample(rule).sum()

    agg = agg.reset_index().rename(columns={'index': 'Date'})
    if 'Date' not in agg.columns and agg.index.name == 'Date':
        agg = agg.reset_index()
    return agg


# ============================================================
# FULL INDICATOR PIPELINE
# ============================================================

def compute_all_indicators(df: pd.DataFrame, include_whale: bool = True,
                            include_liquidity: bool = True) -> pd.DataFrame:
    """Run full indicator pipeline: basic → swing → SMC → whale → liquidity."""
    if len(df) < 30:
        return df
    df = add_basic_indicators(df)
    df = detect_swing_points(df)
    df = detect_smc_patterns(df)
    if include_whale and len(df) > 50:
        df = detect_whale_activity(df)
    return df


def incremental_update(df: pd.DataFrame, include_whale: bool = True) -> pd.DataFrame:
    """
    Fast incremental indicator update when only 1 bar was appended.

    Strategy: pandas EMA/rolling operations are O(n) regardless, but the
    vectorized numpy operations we can restrict to a tail window.

    For the live engine, the main win is:
      - basic indicators: EWM/rolling are fast (pandas C-layer), ~5ms
      - swing points: only last 11 bars matter (left=5, right=5)
      - SMC patterns: only last ~25 bars matter for FVG/OB/BOS
      - whale: rolling lookback is 50, but pandas handles this efficiently

    This avoids the .copy() overhead and minimizes Python-layer work.
    """
    if len(df) < 30:
        return df

    n = len(df)
    c = df['Close']
    h = df['High']
    l = df['Low']
    v = df['Volume']

    # ── Fast EMA update: just update last value ──
    # EMA(new) = alpha * new_val + (1-alpha) * EMA(old)
    for col, span in [('EMA_9', 9), ('EMA_21', 21), ('EMA_50', 50)]:
        if col in df.columns and n > 1:
            alpha = 2.0 / (span + 1)
            df.iloc[-1, df.columns.get_loc(col)] = alpha * c.iloc[-1] + (1 - alpha) * df[col].iloc[-2]

    # SMA: just update the last value
    for col, win in [('SMA_20', 20), ('SMA_50', 50)]:
        if col in df.columns and n >= win:
            df.iloc[-1, df.columns.get_loc(col)] = c.iloc[-win:].mean()

    # RSI: incremental with Wilder smoothing
    if 'RSI_14' in df.columns and n > 15:
        delta = c.iloc[-1] - c.iloc[-2]
        gain = max(delta, 0)
        loss = max(-delta, 0)
        # Approximate: use rolling average of last 14
        recent_gains = c.diff().iloc[-14:].clip(lower=0).mean()
        recent_losses = (-c.diff().iloc[-14:]).clip(lower=0).mean()
        rs = recent_gains / (recent_losses + 1e-10)
        df.iloc[-1, df.columns.get_loc('RSI_14')] = 100 - (100 / (1 + rs))

    # ATR: incremental (SMA-14 of True Range, matching full compute)
    if 'ATR' in df.columns and n > 15:
        # Compute TR for last 14 bars and take mean (matches rolling(14).mean())
        tr_vals = []
        for k in range(max(1, n-14), n):
            tr_k = max(h.iloc[k] - l.iloc[k],
                       abs(h.iloc[k] - c.iloc[k-1]),
                       abs(l.iloc[k] - c.iloc[k-1]))
            tr_vals.append(tr_k)
        df.iloc[-1, df.columns.get_loc('ATR')] = np.mean(tr_vals)

    # BB: update from SMA_20
    if 'BB_Upper' in df.columns and n >= 20:
        sma = df['SMA_20'].iloc[-1]
        std = c.iloc[-20:].std()
        df.iloc[-1, df.columns.get_loc('BB_Upper')] = sma + 2 * std
        df.iloc[-1, df.columns.get_loc('BB_Lower')] = sma - 2 * std

    # Volume MA
    if 'Volume_MA_20' in df.columns and n >= 20:
        df.iloc[-1, df.columns.get_loc('Volume_MA_20')] = v.iloc[-20:].mean()

    # ADX: forward-fill from previous bar (changes slowly via EWM of DX)
    if 'ADX_14' in df.columns and n > 15:
        df.iloc[-1, df.columns.get_loc('ADX_14')] = df['ADX_14'].iloc[-2]
    # Also forward-fill ATR_14 if it exists
    if 'ATR_14' in df.columns and n > 15:
        df.iloc[-1, df.columns.get_loc('ATR_14')] = df['ATR_14'].iloc[-2]

    # Trend direction
    if 'Trend_Dir' in df.columns:
        if df['EMA_9'].iloc[-1] > df['EMA_21'].iloc[-1]:
            df.iloc[-1, df.columns.get_loc('Trend_Dir')] = 1
        elif df['EMA_9'].iloc[-1] < df['EMA_21'].iloc[-1]:
            df.iloc[-1, df.columns.get_loc('Trend_Dir')] = -1
        else:
            df.iloc[-1, df.columns.get_loc('Trend_Dir')] = 0

    # VWAP: cumulative, just extend
    if 'VWAP' in df.columns and 'Date' in df.columns:
        try:
            dates = pd.to_datetime(df['Date'])
            today = dates.iloc[-1].date()
            mask = dates.dt.date == today
            today_df = df[mask]
            cum_vp = (today_df['Close'] * today_df['Volume']).sum()
            cum_v = today_df['Volume'].sum()
            df.iloc[-1, df.columns.get_loc('VWAP')] = cum_vp / (cum_v + 1e-10)
        except Exception:
            pass  # VWAP update not critical for single bar

    # ── Swing points: only check if bar at position n-1-right could be a swing ──
    # (new bar won't create a swing at the very end — need 'right' bars to confirm)
    # Just forward-fill the last swing values
    if 'Last_Swing_High' in df.columns:
        df.iloc[-1, df.columns.get_loc('Last_Swing_High')] = df['Last_Swing_High'].iloc[-2]
        df.iloc[-1, df.columns.get_loc('Last_Swing_Low')] = df['Last_Swing_Low'].iloc[-2]
        df.iloc[-1, df.columns.get_loc('Recent_Swing_High')] = df['Last_Swing_High'].iloc[-1]
        df.iloc[-1, df.columns.get_loc('Recent_Swing_Low')] = df['Last_Swing_Low'].iloc[-1]

    # ── SMC: only update last bar's signals ──
    idx = n - 1
    hv, lv, cv, ov = h.values, l.values, c.values, df['Open'].values
    body_val = abs(cv[idx] - ov[idx])
    atr_val = df['ATR'].iloc[-1] if 'ATR' in df.columns else 0.01

    # FVG
    if idx >= 2:
        df.iloc[-1, df.columns.get_loc('Bull_FVG')] = lv[idx] > hv[idx - 2]
        df.iloc[-1, df.columns.get_loc('Bear_FVG')] = hv[idx] < lv[idx - 2]

    # Displacement (relaxed from 1.5x to 1.0x ATR for live)
    disp = body_val > atr_val * 1.0
    df.iloc[-1, df.columns.get_loc('Displacement_Bull')] = disp and cv[idx] > ov[idx]
    df.iloc[-1, df.columns.get_loc('Displacement_Bear')] = disp and cv[idx] < ov[idx]

    # OB
    if idx >= 1:
        bull_disp = disp and cv[idx] > ov[idx]
        bear_disp = disp and cv[idx] < ov[idx]
        df.iloc[-1, df.columns.get_loc('Bull_OB')] = bull_disp and cv[idx-1] < ov[idx-1]
        df.iloc[-1, df.columns.get_loc('Bear_OB')] = bear_disp and cv[idx-1] > ov[idx-1]

    # BOS
    if 'Last_Swing_High' in df.columns:
        lsh = df['Last_Swing_High'].iloc[-1]
        lsl = df['Last_Swing_Low'].iloc[-1]
        df.iloc[-1, df.columns.get_loc('BOS_Bull')] = (not np.isnan(lsh)) and cv[idx] > lsh
        df.iloc[-1, df.columns.get_loc('BOS_Bear')] = (not np.isnan(lsl)) and cv[idx] < lsl

    # Sweep
    if idx >= 21 and 'Bull_Sweep' in df.columns:
        rh = pd.Series(hv[:idx]).rolling(20).max().iloc[-1]
        rl = pd.Series(lv[:idx]).rolling(20).min().iloc[-1]
        df.iloc[-1, df.columns.get_loc('Bull_Sweep')] = hv[idx] > rh and cv[idx] < rh
        df.iloc[-1, df.columns.get_loc('Bear_Sweep')] = lv[idx] < rl and cv[idx] > rl

    # Rejection
    if 'Reject_Bull' in df.columns:
        wick_dn = min(cv[idx], ov[idx]) - lv[idx]
        wick_up = hv[idx] - max(cv[idx], ov[idx])
        df.iloc[-1, df.columns.get_loc('Reject_Bull')] = wick_dn > body_val * 2
        df.iloc[-1, df.columns.get_loc('Reject_Bear')] = wick_up > body_val * 2

    # Premium/Discount
    if 'In_Discount' in df.columns and n >= 50:
        rh50 = hv[-50:].max()
        rl50 = lv[-50:].min()
        mid = (rh50 + rl50) / 2
        df.iloc[-1, df.columns.get_loc('In_Discount')] = cv[idx] < mid
        df.iloc[-1, df.columns.get_loc('In_Premium')] = cv[idx] > mid

    # Whale (incremental)
    if include_whale and 'Whale_Trade_Z' in df.columns and n > 50:
        nt = df['NumTrades'].values[-1] if 'NumTrades' in df.columns else 1
        vol_val = v.iloc[-1]
        ats = vol_val / max(nt, 1)
        ats_arr = df['Avg_Trade_Size'].iloc[-50:].values
        ats_mean = np.mean(ats_arr)
        ats_std = np.std(ats_arr) + 1e-10
        z = (ats - ats_mean) / ats_std
        df.iloc[-1, df.columns.get_loc('Avg_Trade_Size')] = ats
        df.iloc[-1, df.columns.get_loc('Whale_Trade_Z')] = z
        df.iloc[-1, df.columns.get_loc('Is_Whale_Bar')] = z > 2.0
        df.iloc[-1, df.columns.get_loc('Is_Mega_Whale')] = z > 3.0

    return df
