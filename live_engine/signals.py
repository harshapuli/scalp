"""
LIVE TRADING ENGINE — SIGNAL DETECTION (MASTER SYSTEM)
========================================================
20-strategy master system signal detectors + V2 filters.

Active signal functions (master system):
  Core:          smc_order_block, judas_swing, breaker_block
  Confluence:    ict_reversal, mitigation_confluence
  Displacement:  displacement_engine (merged BOS+Propulsion+CISD+Trap_Shift)
  HTF:           daily_ob, weekly_ob, daily_ema_trend, weekly_ema_trend

Legacy signal functions (kept for reference, not used in master):
  ICT/SMC:       market_maker_model, propulsion, unicorn_model, ce_fvg, cisd,
                 power_of_three, smt_divergence, turtle_soup, imbalance_stack,
                 london_close, silver_bullet, silver_bullet_am
  Standard:      ema_crossover, rsi_divergence, volume_spike, vwap_bounce,
                 fvg_entry, break_of_structure
"""
import numpy as np
import pandas as pd
from typing import Tuple, Optional


# ============================================================
# SIGNAL DETECTION — MASTER DISPATCHER
# ============================================================

def detect_signals(df: pd.DataFrame, signal_func: str) -> Tuple[np.ndarray, np.ndarray]:
    """
    Detect CALL and PUT signals for a given strategy.
    Returns: (signal_call, signal_put) boolean arrays same length as df.
    """
    n = len(df)
    signal_call = np.zeros(n, dtype=bool)
    signal_put = np.zeros(n, dtype=bool)

    has_smc = 'Bull_OB' in df.columns  # Full SMC indicators available

    try:
        # ══════════════════════════════════════════════════════════
        # ICT / SMC STRATEGIES
        # ══════════════════════════════════════════════════════════

        if signal_func == 'smc_order_block':
            # Order Block: last opposing candle before displacement
            if has_smc:
                signal_call = df['Bull_OB'].fillna(False).values
                signal_put = df['Bear_OB'].fillna(False).values
            else:
                signal_call, signal_put = _fallback_fvg_trend(df)

        elif signal_func == 'judas_swing':
            # Sweep of prior session high/low → CHoCH reversal
            if has_smc and 'Bull_Sweep' in df.columns and 'CHoCH_Bull' in df.columns:
                sweep_bear = pd.Series(df['Bear_Sweep']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                sweep_bull = pd.Series(df['Bull_Sweep']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                signal_call = (sweep_bear & df['CHoCH_Bull'].fillna(False)).values
                signal_put = (sweep_bull & df['CHoCH_Bear'].fillna(False)).values
            else:
                signal_call, signal_put = _fallback_sweep_reversal(df)

        elif signal_func == 'market_maker_model':
            # BB Squeeze → Liquidity Sweep → CHoCH
            if has_smc and 'BB_Squeeze' in df.columns:
                squeeze = pd.Series(df['BB_Squeeze']).rolling(10, min_periods=1).max().fillna(0).astype(bool)
                sweep_bear = pd.Series(df.get('Bear_Sweep', pd.Series(False, index=df.index))).rolling(3, min_periods=1).max().fillna(0).astype(bool)
                sweep_bull = pd.Series(df.get('Bull_Sweep', pd.Series(False, index=df.index))).rolling(3, min_periods=1).max().fillna(0).astype(bool)
                choch_bull = df.get('CHoCH_Bull', pd.Series(False, index=df.index)).fillna(False)
                choch_bear = df.get('CHoCH_Bear', pd.Series(False, index=df.index)).fillna(False)
                signal_call = (squeeze & sweep_bear & choch_bull).values
                signal_put = (squeeze & sweep_bull & choch_bear).values
            else:
                if 'BB_Squeeze' in df.columns:
                    sq = df['BB_Squeeze'].fillna(False)
                    c = df['Close'].values
                    sma = df['SMA_20'].values if 'SMA_20' in df.columns else c
                    signal_call = sq.values & (c > sma)
                    signal_put = sq.values & (c < sma)

        elif signal_func == 'breaker_block':
            # OB retest + rejection candle
            if has_smc and 'Reject_Bull' in df.columns:
                ob_b = pd.Series(df['Bull_OB']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                ob_r = pd.Series(df['Bear_OB']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                signal_call = (ob_b & df['Reject_Bull'].fillna(False)).values
                signal_put = (ob_r & df['Reject_Bear'].fillna(False)).values
            else:
                signal_call, signal_put = _fallback_fvg_trend(df)

        elif signal_func == 'propulsion':
            # BOS + FVG overlap (trend continuation)
            if has_smc and 'BOS_Bull' in df.columns and 'Bull_FVG' in df.columns:
                bos_b = pd.Series(df['BOS_Bull']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                bos_r = pd.Series(df['BOS_Bear']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                signal_call = (bos_b & df['Bull_FVG'].fillna(False)).values
                signal_put = (bos_r & df['Bear_FVG'].fillna(False)).values
            else:
                signal_call, signal_put = _fallback_fvg_trend(df)

        elif signal_func == 'unicorn_model':
            # Triple: FVG + OTE zone + discount/premium
            if has_smc and 'In_OTE_Long' in df.columns and 'Bull_FVG' in df.columns:
                ote = df['In_OTE_Long'].fillna(False) | df['In_OTE_Short'].fillna(False)
                disc = df.get('In_Discount', pd.Series(False, index=df.index)).fillna(False)
                prem = df.get('In_Premium', pd.Series(False, index=df.index)).fillna(False)
                signal_call = (df['Bull_FVG'].fillna(False) & ote & disc).values
                signal_put = (df['Bear_FVG'].fillna(False) & ote & prem).values
            else:
                rsi = df['RSI_14'].values if 'RSI_14' in df.columns else np.full(n, 50)
                if 'Bull_FVG' in df.columns:
                    signal_call = df['Bull_FVG'].values & (rsi < 40)
                    signal_put = df['Bear_FVG'].values & (rsi > 60)

        elif signal_func == 'ce_fvg':
            # Consequent encroachment — FVG entry in OTE zone
            if has_smc and 'Bull_FVG' in df.columns and 'In_OTE_Long' in df.columns:
                ote_l = df['In_OTE_Long'].fillna(False)
                ote_s = df['In_OTE_Short'].fillna(False)
                signal_call = (df['Bull_FVG'].fillna(False) & ote_l).values
                signal_put = (df['Bear_FVG'].fillna(False) & ote_s).values
            elif 'Bull_FVG' in df.columns:
                rsi = df['RSI_14'].values if 'RSI_14' in df.columns else np.full(n, 50)
                signal_call = df['Bull_FVG'].fillna(False).values & (rsi < 50)
                signal_put = df['Bear_FVG'].fillna(False).values & (rsi > 50)

        elif signal_func == 'cisd':
            # Consecutive displacement bars (institutional accumulation)
            if has_smc and 'Displacement_Bull' in df.columns:
                disp_b = df['Displacement_Bull'].fillna(False)
                disp_r = df['Displacement_Bear'].fillna(False)
                consec_b = disp_b & disp_b.shift(1).fillna(False)
                consec_r = disp_r & disp_r.shift(1).fillna(False)
                signal_call = consec_b.values
                signal_put = consec_r.values
            else:
                atr = df['ATR'].values if 'ATR' in df.columns else np.ones(n) * 0.01
                body = np.abs(df['Close'].values - df['Open'].values)
                displacement = body > atr * 1.5
                signal_call = displacement & (df['Close'].values > df['Open'].values)
                signal_put = displacement & (df['Close'].values < df['Open'].values)

        elif signal_func == 'power_of_three':
            # First 30min range → fake breakout → real move
            if 'Date' in df.columns:
                hour = df['Date'].dt.hour
                minute = df['Date'].dt.minute
                t = hour * 60 + minute
                after_open = (t >= 600) & (t < 660)  # 10:00-11:00 ET
                c = df['Close'].values
                vwap = df['VWAP'].values if 'VWAP' in df.columns else c
                signal_call = after_open.values & (c > vwap)
                signal_put = after_open.values & (c < vwap)

        elif signal_func == 'smt_divergence':
            # Smart Money divergence: price vs volume mismatch
            if has_smc and 'SM_Divergence_Bull' in df.columns:
                sm_b = df['SM_Divergence_Bull'].fillna(False)
                sm_r = df['SM_Divergence_Bear'].fillna(False)
                if 'Bull_FVG' in df.columns:
                    signal_call = (sm_b & df['Bull_FVG'].fillna(False)).values
                    signal_put = (sm_r & df['Bear_FVG'].fillna(False)).values
                else:
                    rsi = df['RSI_14'].values if 'RSI_14' in df.columns else np.full(n, 50)
                    signal_call = sm_b.values & (rsi < 40)
                    signal_put = sm_r.values & (rsi > 60)
            else:
                # Fallback: price-RSI divergence
                rsi = df['RSI_14'].values if 'RSI_14' in df.columns else np.full(n, 50)
                c = df['Close'].values
                for i in range(20, n):
                    if c[i] < c[i-10] and rsi[i] > rsi[i-10] and rsi[i] < 40:
                        signal_call[i] = True
                    if c[i] > c[i-10] and rsi[i] < rsi[i-10] and rsi[i] > 60:
                        signal_put[i] = True

        elif signal_func == 'turtle_soup':
            # Failed 20-bar breakout reversal
            h, l, c = df['High'].values, df['Low'].values, df['Close'].values
            rh = pd.Series(h).rolling(20).max().values
            rl = pd.Series(l).rolling(20).min().values
            for i in range(21, n):
                if h[i] > rh[i-1] and c[i] < rh[i-1]:  # false breakout high
                    signal_put[i] = True
                if l[i] < rl[i-1] and c[i] > rl[i-1]:  # false breakout low
                    signal_call[i] = True

        elif signal_func == 'imbalance_stack':
            # Multiple stacked FVGs — require 2+ for strong signal
            if has_smc and 'Bull_FVG_Stack' in df.columns:
                signal_call = (df['Bull_FVG_Stack'].fillna(0) >= 2).values
                signal_put = (df['Bear_FVG_Stack'].fillna(0) >= 2).values
            elif 'Bull_FVG' in df.columns:
                fb = df['Bull_FVG'].fillna(False).astype(int).rolling(5, min_periods=1).sum()
                fr = df['Bear_FVG'].fillna(False).astype(int).rolling(5, min_periods=1).sum()
                signal_call = (fb >= 2).values
                signal_put = (fr >= 2).values

        elif signal_func == 'london_close':
            # Reversal during London/NY overlap with confluence
            if 'Date' in df.columns:
                hour = df['Date'].dt.hour
                london = (hour >= 14) & (hour < 16)  # 10-12 ET (UTC)
                rsi = df['RSI_14'].values if 'RSI_14' in df.columns else np.full(n, 50)
                if has_smc and 'Bull_FVG' in df.columns:
                    signal_call = london.values & (rsi < 35) & df['Bull_FVG'].fillna(False).values
                    signal_put = london.values & (rsi > 65) & df['Bear_FVG'].fillna(False).values
                else:
                    signal_call = london.values & (rsi < 30)
                    signal_put = london.values & (rsi > 70)

        elif signal_func in ('silver_bullet', 'silver_bullet_am'):
            # FVG entry during kill zone windows
            if has_smc and 'Bull_FVG' in df.columns and 'In_Killzone' in df.columns:
                kz = df['In_Killzone'].fillna(False)
                signal_call = (df['Bull_FVG'].fillna(False) & kz).values
                signal_put = (df['Bear_FVG'].fillna(False) & kz).values
            elif 'Bull_FVG' in df.columns and 'Date' in df.columns:
                hour = df['Date'].dt.hour
                if signal_func == 'silver_bullet_am':
                    kz = (hour >= 10) & (hour < 11)  # 10-11 ET
                else:
                    kz = (hour >= 14) & (hour < 15)  # 2-3 PM ET
                signal_call = (df['Bull_FVG'].fillna(False) & kz).values
                signal_put = (df['Bear_FVG'].fillna(False) & kz).values

        # ══════════════════════════════════════════════════════════
        # STANDARD STRATEGIES
        # ══════════════════════════════════════════════════════════

        elif signal_func == 'rsi_divergence':
            # RSI extreme + VWAP confirmation
            rsi = df['RSI_14'].values if 'RSI_14' in df.columns else np.full(n, 50)
            c = df['Close'].values
            vwap = df['VWAP'].values if 'VWAP' in df.columns else c
            signal_call = (rsi < 30) & (c > vwap)
            signal_put = (rsi > 70) & (c < vwap)

        elif signal_func == 'break_of_structure':
            # BOS + displacement confluence
            if has_smc and 'BOS_Bull' in df.columns:
                bos_b = df['BOS_Bull'].fillna(False)
                bos_r = df['BOS_Bear'].fillna(False)
                if 'Displacement_Bull' in df.columns:
                    disp_b = df['Displacement_Bull'].fillna(False)
                    disp_r = df['Displacement_Bear'].fillna(False)
                    signal_call = (bos_b & disp_b).values
                    signal_put = (bos_r & disp_r).values
                elif 'In_Killzone' in df.columns:
                    kz = df['In_Killzone'].fillna(True)
                    signal_call = (bos_b & kz).values
                    signal_put = (bos_r & kz).values
                else:
                    signal_call = bos_b.values
                    signal_put = bos_r.values
            else:
                signal_call, signal_put = _fallback_breakout(df)

        elif signal_func == 'ema_crossover':
            # EMA 9/21 cross + VWAP alignment
            ema9 = df['EMA_9'].values if 'EMA_9' in df.columns else np.zeros(n)
            ema21 = df['EMA_21'].values if 'EMA_21' in df.columns else np.zeros(n)
            c = df['Close'].values
            vwap = df['VWAP'].values if 'VWAP' in df.columns else c
            for i in range(1, n):
                if ema9[i-1] <= ema21[i-1] and ema9[i] > ema21[i] and c[i] > vwap[i]:
                    signal_call[i] = True
                if ema9[i-1] >= ema21[i-1] and ema9[i] < ema21[i] and c[i] < vwap[i]:
                    signal_put[i] = True

        elif signal_func == 'volume_spike':
            # 2x volume spike + directional candle
            vol_ratio = df['Volume'].values / (df['Volume_MA_20'].values + 1e-10) if 'Volume_MA_20' in df.columns else np.ones(n)
            spike = vol_ratio > 2.0
            c = df['Close'].values
            o = df['Open'].values
            signal_call = spike & (c > o)
            signal_put = spike & (c < o)

        elif signal_func == 'vwap_bounce':
            # Price touches VWAP + bounces with volume
            c = df['Close'].values
            vwap = df['VWAP'].values if 'VWAP' in df.columns else df.get('SMA_20', pd.Series(c)).values
            near_vwap = np.abs(c - vwap) / (vwap + 1e-10) < 0.001  # within 0.1%
            vol_ratio = df['Volume'].values / (df['Volume_MA_20'].values + 1e-10) if 'Volume_MA_20' in df.columns else np.ones(n)
            vol_ok = vol_ratio > 1.2
            for i in range(2, n):
                if near_vwap[i-1] and c[i] > vwap[i] and vol_ok[i]:
                    signal_call[i] = True
                if near_vwap[i-1] and c[i] < vwap[i] and vol_ok[i]:
                    signal_put[i] = True

        elif signal_func == 'fvg_entry':
            # FVG + kill zone filter
            if has_smc and 'Bull_FVG' in df.columns:
                kz = df['In_Killzone'].fillna(True) if 'In_Killzone' in df.columns else pd.Series(True, index=df.index)
                signal_call = (df['Bull_FVG'].fillna(False) & kz).values
                signal_put = (df['Bear_FVG'].fillna(False) & kz).values
            elif 'Bull_FVG' in df.columns:
                signal_call = df['Bull_FVG'].fillna(False).values
                signal_put = df['Bear_FVG'].fillna(False).values

        # ══════════════════════════════════════════════════════════
        # COMBO STRATEGIES — Multi-confluence ICT setups
        # ══════════════════════════════════════════════════════════

        elif signal_func == 'ict_reversal':
            # 4-layer: Sweep → CHoCH → FVG → Rejection
            if has_smc and 'Bull_Sweep' in df.columns and 'CHoCH_Bull' in df.columns:
                sweep_b = pd.Series(df['Bear_Sweep']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                sweep_r = pd.Series(df['Bull_Sweep']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                choch_b = pd.Series(df['CHoCH_Bull']).rolling(3, min_periods=1).max().fillna(0).astype(bool)
                choch_r = pd.Series(df['CHoCH_Bear']).rolling(3, min_periods=1).max().fillna(0).astype(bool)
                fvg_b = df.get('Bull_FVG', pd.Series(False, index=df.index)).fillna(False)
                fvg_r = df.get('Bear_FVG', pd.Series(False, index=df.index)).fillna(False)
                signal_call = (sweep_b & choch_b & fvg_b).values
                signal_put = (sweep_r & choch_r & fvg_r).values
            else:
                h, l, c = df['High'].values, df['Low'].values, df['Close'].values
                rl = pd.Series(l).rolling(20).min().values
                rh = pd.Series(h).rolling(20).max().values
                for i in range(21, n):
                    if l[i] < rl[i-1] and c[i] > c[i-1]:
                        signal_call[i] = True
                    if h[i] > rh[i-1] and c[i] < c[i-1]:
                        signal_put[i] = True

        elif signal_func == 'trap_shift':
            # Liquidity sweep + displacement
            if has_smc and 'Bull_Sweep' in df.columns and 'Displacement_Bull' in df.columns:
                sweep_b = pd.Series(df['Bear_Sweep']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                sweep_r = pd.Series(df['Bull_Sweep']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                disp_b = df['Displacement_Bull'].fillna(False)
                disp_r = df['Displacement_Bear'].fillna(False)
                signal_call = (sweep_b & disp_b).values
                signal_put = (sweep_r & disp_r).values
            else:
                h, l, c, o = df['High'].values, df['Low'].values, df['Close'].values, df['Open'].values
                rl = pd.Series(l).rolling(20).min().values
                rh = pd.Series(h).rolling(20).max().values
                body = np.abs(c - o)
                atr = pd.Series(h - l).rolling(14).mean().values
                for i in range(21, n):
                    if l[i] < rl[i-1] and body[i] > atr[i] * 1.5 and c[i] > o[i]:
                        signal_call[i] = True
                    if h[i] > rh[i-1] and body[i] > atr[i] * 1.5 and c[i] < o[i]:
                        signal_put[i] = True

        elif signal_func == 'mitigation_confluence':
            # OB retest + FVG + premium/discount alignment
            if has_smc and 'Bull_OB' in df.columns and 'Bull_FVG' in df.columns:
                ob_b = pd.Series(df['Bull_OB']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                ob_r = pd.Series(df['Bear_OB']).rolling(5, min_periods=1).max().fillna(0).astype(bool)
                disc = df.get('In_Discount', pd.Series(True, index=df.index)).fillna(True)
                prem = df.get('In_Premium', pd.Series(True, index=df.index)).fillna(True)
                signal_call = (ob_b & df['Bull_FVG'].fillna(False) & disc).values
                signal_put = (ob_r & df['Bear_FVG'].fillna(False) & prem).values
            else:
                if 'Bull_FVG' in df.columns:
                    c = df['Close'].values
                    sma = df['SMA_50'].values if 'SMA_50' in df.columns else c
                    signal_call = df['Bull_FVG'].fillna(False).values & (c < sma)
                    signal_put = df['Bear_FVG'].fillna(False).values & (c > sma)

        elif signal_func == 'golden_array':
            # Fibonacci OTE zone (0.618-0.786) + FVG confluence
            if has_smc and 'In_OTE_Long' in df.columns and 'Bull_FVG' in df.columns:
                ote_l = df['In_OTE_Long'].fillna(False)
                ote_s = df['In_OTE_Short'].fillna(False)
                signal_call = (ote_l & df['Bull_FVG'].fillna(False)).values
                signal_put = (ote_s & df['Bear_FVG'].fillna(False)).values
            else:
                rsi = df['RSI_14'].values if 'RSI_14' in df.columns else np.full(n, 50)
                c = df['Close'].values
                sma = df['SMA_50'].values if 'SMA_50' in df.columns else c
                for i in range(50, n):
                    if rsi[i] < 40 and c[i] > sma[i]:
                        signal_call[i] = True
                    if rsi[i] > 60 and c[i] < sma[i]:
                        signal_put[i] = True

        # ══════════════════════════════════════════════════════════
        # DAILY / WEEKLY / MONTHLY VARIANTS
        # ══════════════════════════════════════════════════════════

        elif signal_func in ('daily_ema_trend', 'weekly_ema_trend', 'monthly_ema_trend'):
            ema9 = df['EMA_9'].values if 'EMA_9' in df.columns else np.zeros(n)
            ema21 = df['EMA_21'].values if 'EMA_21' in df.columns else np.zeros(n)
            for i in range(1, n):
                if ema9[i-1] <= ema21[i-1] and ema9[i] > ema21[i]:
                    signal_call[i] = True
                if ema9[i-1] >= ema21[i-1] and ema9[i] < ema21[i]:
                    signal_put[i] = True

        elif signal_func in ('daily_ob', 'weekly_ob', 'monthly_ob'):
            if has_smc:
                signal_call = df['Bull_OB'].fillna(False).values
                signal_put = df['Bear_OB'].fillna(False).values
            elif 'Bull_FVG' in df.columns:
                signal_call = df['Bull_FVG'].fillna(False).values
                signal_put = df['Bear_FVG'].fillna(False).values

        elif signal_func in ('daily_fvg', 'weekly_fvg', 'monthly_fvg'):
            if 'Bull_FVG' in df.columns:
                signal_call = df['Bull_FVG'].fillna(False).values
                signal_put = df['Bear_FVG'].fillna(False).values

        elif signal_func in ('daily_rsi_div', 'weekly_rsi_div', 'monthly_rsi_div'):
            rsi = df['RSI_14'].values if 'RSI_14' in df.columns else np.full(n, 50)
            c = df['Close'].values
            vwap = df['VWAP'].values if 'VWAP' in df.columns else c
            signal_call = (rsi < 30) & (c > vwap)
            signal_put = (rsi > 70) & (c < vwap)

        elif signal_func == 'displacement_engine':
            # Unified Displacement Engine: BOS + Displacement candle + optional FVG
            # One clean model replacing BOS, Propulsion, CISD, Trap_Shift
            signal_call, signal_put = _detect_displacement_engine(df)

        else:
            # Generic fallback: FVG + trend
            signal_call, signal_put = _fallback_fvg_trend(df)

    except Exception as e:
        pass  # Return empty signals on error

    return signal_call, signal_put


# ============================================================
# V2 FILTERS — Apply to raw signals before execution
# ============================================================

def apply_v2_filters(
    df: pd.DataFrame,
    signal_indices: np.ndarray,
    direction: str,
    strategy: dict,
    htf_trend: Optional[pd.DataFrame] = None,
    v2_config: Optional[dict] = None,
) -> list:
    """
    Apply all V2 filters to raw signal indices:
      1. Confirmation bar (enter on next bar open)
      2. HTF trend alignment (sub-1hr must agree with 1hr EMA)
      3. ADX chop filter (skip if ADX < 20)
      4. Structural stops (swing-based stops when wider)
      5. Cooldown (min bars between entries)

    Returns: list of (entry_idx, stop_pct, target_pct) tuples
    """
    if v2_config is None:
        from config import V2_FILTERS
        v2_config = V2_FILTERS

    filtered = []
    last_entry_idx = -999
    n = len(df)
    tf = strategy['timeframe']

    # Pre-fetch columns
    adx = df['ADX_14'].values if 'ADX_14' in df.columns else np.full(n, 30)
    trend = df['Trend_Dir'].values if 'Trend_Dir' in df.columns else np.zeros(n)
    swing_lo = df['Recent_Swing_Low'].values if 'Recent_Swing_Low' in df.columns else None
    swing_hi = df['Recent_Swing_High'].values if 'Recent_Swing_High' in df.columns else None

    cooldown = v2_config.get('cooldown_bars', {}).get(tf, 5)
    needs_htf = tf in ('1min', '3min', '5min', '15min')
    adx_min = v2_config.get('adx_min', 20)

    for idx in signal_indices:
        # Cooldown
        if idx - last_entry_idx < cooldown:
            continue

        # Confirmation bar must exist
        confirm_idx = idx + 1
        if confirm_idx >= n:
            continue

        # ADX chop filter
        if adx[idx] < adx_min:
            continue

        # HTF trend alignment
        if needs_htf and htf_trend is not None and 'Date' in df.columns:
            try:
                signal_date = df['Date'].iloc[idx]
                htf_idx = htf_trend.index.get_indexer([signal_date], method='pad')[0]
                if htf_idx >= 0:
                    htf_dir = htf_trend.iloc[htf_idx]['Trend_Dir']
                    if direction == 'CALL' and htf_dir < 0:
                        continue
                    if direction == 'PUT' and htf_dir > 0:
                        continue
            except:
                pass

        # Structural stop calculation
        stop_pct = strategy['stop_pct']
        target_pct = strategy['target_pct']
        entry_price = df['Open'].iloc[confirm_idx]

        if swing_lo is not None and entry_price > 0:
            rr_ratio = strategy['target_pct'] / strategy['stop_pct']
            max_mult = v2_config.get('structural_stop_max_mult', 3)

            if direction == 'CALL':
                struct_level = swing_lo[idx]
                struct_dist = (entry_price - struct_level) / entry_price * 100
                if 0.01 < struct_dist < strategy['stop_pct'] * max_mult:
                    stop_pct = max(struct_dist * 1.05, strategy['stop_pct'])
            else:
                struct_level = swing_hi[idx] if swing_hi is not None else entry_price * 1.01
                struct_dist = (struct_level - entry_price) / entry_price * 100
                if 0.01 < struct_dist < strategy['stop_pct'] * max_mult:
                    stop_pct = max(struct_dist * 1.05, strategy['stop_pct'])

            target_pct = stop_pct * rr_ratio

        filtered.append((confirm_idx, round(stop_pct, 4), round(target_pct, 4)))
        last_entry_idx = idx

    return filtered


# ============================================================
# WHALE FILTER — Additional filter for whale-eligible strategies
# ============================================================

def whale_filter(df: pd.DataFrame, idx: int, strategy: dict) -> bool:
    """
    Check if whale activity confirms the signal.
    Only called for whale-eligible strategies with positive whale_boost.
    Returns True if whale confirms (or whale data not available).
    """
    if not strategy.get('whale_eligible', False):
        return True

    # Skip whale filter if it HURTS this strategy
    if strategy.get('whale_boost', 0) < 0:
        return True  # Don't filter — whale hurts this strategy

    # Check for recent whale activity
    if 'Whale_Confirm_10' in df.columns:
        return bool(df['Whale_Confirm_10'].iloc[idx])

    if 'Is_Whale_Bar' in df.columns:
        start = max(0, idx - 10)
        return df['Is_Whale_Bar'].iloc[start:idx+1].any()

    return True  # No whale data = allow trade


# ============================================================
# FALLBACK SIGNAL GENERATORS
# ============================================================

def _fallback_fvg_trend(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray]:
    """FVG + trend direction fallback."""
    n = len(df)
    if 'Bull_FVG' in df.columns:
        c = df['Close'].values
        sma = df['SMA_20'].values if 'SMA_20' in df.columns else c
        return (
            df['Bull_FVG'].fillna(False).values & (c > sma),
            df['Bear_FVG'].fillna(False).values & (c < sma),
        )
    return np.zeros(n, dtype=bool), np.zeros(n, dtype=bool)


def _fallback_sweep_reversal(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray]:
    """Price sweep + reversal fallback for Judas Swing."""
    n = len(df)
    h, l, c = df['High'].values, df['Low'].values, df['Close'].values
    sc = np.zeros(n, dtype=bool)
    sp = np.zeros(n, dtype=bool)
    rh = pd.Series(h).rolling(20).max().values
    rl = pd.Series(l).rolling(20).min().values
    for i in range(21, n):
        if h[i-1] > rh[i-2] and c[i] < c[i-1]:
            sp[i] = True
        if l[i-1] < rl[i-2] and c[i] > c[i-1]:
            sc[i] = True
    return sc, sp


def _fallback_breakout(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray]:
    """Simple breakout fallback for BOS."""
    n = len(df)
    h, l, c = df['High'].values, df['Low'].values, df['Close'].values
    sc = np.zeros(n, dtype=bool)
    sp = np.zeros(n, dtype=bool)
    rh = pd.Series(h).rolling(20).max().values
    rl = pd.Series(l).rolling(20).min().values
    for i in range(21, n):
        if c[i] > rh[i-1]:
            sc[i] = True
        if c[i] < rl[i-1]:
            sp[i] = True
    return sc, sp


# ============================================================
# DISPLACEMENT ENGINE — Unified Signal (NEW)
# Merges: BOS + Propulsion + CISD + Trap_Shift → ONE model
# ============================================================

def _detect_displacement_engine(df: pd.DataFrame) -> Tuple[np.ndarray, np.ndarray]:
    """
    Unified Displacement Engine.
    Fires when:
      1. Break of Structure (price breaks 20-bar extreme)  OR
         Sweep + reversal (trap shift pattern)             OR
         Displacement + FVG (propulsion pattern)
      2. AND displacement candle present (body > 1.5x ATR)

    One clean model instead of 4 noisy ones.
    """
    n = len(df)
    sig_call = np.zeros(n, dtype=bool)
    sig_put = np.zeros(n, dtype=bool)

    if n < 25:
        return sig_call, sig_put

    h = df['High'].values
    l = df['Low'].values
    c = df['Close'].values
    o = df['Open'].values

    # ATR for displacement check
    if 'ATR_14' in df.columns:
        atr = df['ATR_14'].values
    else:
        tr = np.maximum(h - l, np.abs(h - np.roll(c, 1)), np.abs(l - np.roll(c, 1)))
        tr[0] = h[0] - l[0]
        atr = pd.Series(tr).rolling(14).mean().fillna(tr[0]).values

    # Rolling 20-bar extremes for BOS
    roll_high = pd.Series(h).rolling(20, min_periods=5).max().values
    roll_low = pd.Series(l).rolling(20, min_periods=5).min().values

    # FVG detection (bonus confluence)
    fvg_bull = np.zeros(n, dtype=bool)
    fvg_bear = np.zeros(n, dtype=bool)
    if n > 3:
        fvg_bull[2:] = l[2:] > h[:-2]
        fvg_bear[2:] = h[2:] < l[:-2]

    for i in range(22, n):
        body = abs(c[i] - o[i])
        displacement = body > atr[i] * 1.5

        if not displacement:
            continue

        # BULLISH displacement engine
        if c[i] > o[i]:
            bos = h[i] > roll_high[i - 1] if i > 0 else False
            sweep_low = l[i] < roll_low[i - 1] if i > 0 else False
            reversal_up = c[i] > (h[i] + l[i]) / 2

            if bos or (sweep_low and reversal_up):
                sig_call[i] = True
            elif fvg_bull[i] or (i >= 2 and fvg_bull[i - 1]):
                sig_call[i] = True

        # BEARISH displacement engine
        elif c[i] < o[i]:
            bos = l[i] < roll_low[i - 1] if i > 0 else False
            sweep_high = h[i] > roll_high[i - 1] if i > 0 else False
            reversal_down = c[i] < (h[i] + l[i]) / 2

            if bos or (sweep_high and reversal_down):
                sig_put[i] = True
            elif fvg_bear[i] or (i >= 2 and fvg_bear[i - 1]):
                sig_put[i] = True

    return sig_call, sig_put
