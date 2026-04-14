"""
MASTER SYSTEM FILTERS — COMPLETE FILTER STACK
================================================
All filters from backtest (V2 + V3 + 6 missing pieces) for live trading.

FILTER PIPELINE (in order):
  1. MODE DETECTION    — TREND / REVERSAL / NO_TRADE (decision tree)
  2. MODE ROUTING      — Strategy mode must match market mode
  3. STRICT EMA BIAS   — 1H EMA21 directional filter (longs above, shorts below)
  4. KILL ZONE         — Block midday dead zone for scalps/0DTE
  5. VOLATILITY REGIME — Block ranging/calm markets
  6. LIQUIDITY SWEEP   — Require prior sweep of equal H/L or PDH/PDL
  7. DISPLACEMENT      — Require body > 1.5x ATR for sweep-based strategies
  8. RSI RISK GATE     — Block overbought CALL / oversold PUT
  9. VOLUME CONFIRM    — Breaker entries need volume > 1.5x average
  10. LTF CHoCH        — HTF Director entries need 5M CHoCH confirmation
  11. ENTRY REFINEMENT  — OB 50% retrace, Breaker 30%, FVG midpoint, etc.

EXIT MANAGEMENT:
  - 2-stage exits: TP1 (50% close), TP2 runner (2.5x, trail at 50% MFE)
  - ScalingManager tracks partial exits and trailing stops
"""

import numpy as np
import pandas as pd
from typing import Dict, Tuple, Optional
from config import V3_CONFIG


# ============================================================
# MODE STATE MACHINE + DECISION TREE
# ============================================================

class MarketModeDetector:
    """
    Prop-desk style decision tree.

    MODE 1: TREND     — ADX > 20, ATR not contracting → OB, Confluence, Displacement, Swing
    MODE 2: REVERSAL  — Sweep detected, ADX < 30 → Breaker, Sweep (Judas)
    MODE 3: NO_TRADE  — Midday dead zone, ADX < 15 + no sweep → Block all
    """

    def __init__(self):
        self.current_mode = 'TREND'
        self.mode_config = V3_CONFIG.get('mode', {})

    def determine_mode(self, df_1min, idx, sweep_bull, sweep_bear):
        """Determine market mode at given index."""
        if idx >= len(df_1min):
            return 'NO_TRADE'

        if 'Date' in df_1min.columns:
            dt = df_1min['Date'].iloc[idx]
            minutes = dt.hour * 60 + dt.minute
            dead = V3_CONFIG.get('kill_zones', {}).get('dead_zone', (690, 840))
            if dead[0] <= minutes < dead[1]:
                return 'NO_TRADE'

        adx_val = df_1min['ADX_14'].iloc[idx] if 'ADX_14' in df_1min.columns else 25
        has_sweep = False
        if idx < len(sweep_bull):
            has_sweep = bool(sweep_bull[idx]) or bool(sweep_bear[idx])

        atr_ok = True
        if 'ATR_14' in df_1min.columns and idx >= 20:
            atr = df_1min['ATR_14'].values
            atr_ok = atr[idx] > np.mean(atr[max(0, idx-20):idx]) * 0.8

        no_trade_adx = self.mode_config.get('no_trade_adx', 15)
        reversal_adx = self.mode_config.get('reversal_adx_max', 30)
        trend_adx = self.mode_config.get('trend_adx_min', 20)

        if adx_val < no_trade_adx and not has_sweep:
            self.current_mode = 'NO_TRADE'
        elif has_sweep and adx_val < reversal_adx:
            self.current_mode = 'REVERSAL'
        elif adx_val >= trend_adx and atr_ok:
            self.current_mode = 'TREND'
        elif has_sweep:
            self.current_mode = 'REVERSAL'
        elif adx_val >= no_trade_adx:
            self.current_mode = 'TREND'
        else:
            self.current_mode = 'NO_TRADE'

        return self.current_mode

    def check_mode_alignment(self, strategy_mode, current_mode, has_sweep):
        """Check if strategy mode aligns with market mode."""
        if current_mode == 'NO_TRADE':
            return False, 'mode_no_trade'
        if current_mode == 'TREND' and strategy_mode == 'REVERSAL':
            if has_sweep:
                return True, 'reversal_with_sweep_override'
            return False, 'mode_mismatch'
        return True, 'ok'


# ============================================================
# STRICT 1H EMA BIAS
# ============================================================

class EMABiasFilter:
    """Strict 1H EMA21 directional filter + Daily OB confirmation."""

    def __init__(self):
        self.config = V3_CONFIG.get('ema_bias', {})
        self.current_bias = 'NEUTRAL'

    def compute_bias(self, tf_data):
        """Compute strict EMA bias from 1H + Daily OB data."""
        ema_bias = 'NEUTRAL'
        ob_bias = 'NEUTRAL'
        neutral_zone = self.config.get('neutral_zone_pct', 0.001)

        if '1hr' in tf_data:
            df_1h = tf_data['1hr']
            ema_col = f"EMA_{self.config.get('ema_period', 21)}"
            if ema_col in df_1h.columns and len(df_1h) > 0:
                last_close = df_1h['Close'].iloc[-1]
                ema = df_1h[ema_col].iloc[-1]
                dist = (last_close - ema) / ema if ema > 0 else 0
                if dist > neutral_zone:
                    ema_bias = 'LONG'
                elif dist < -neutral_zone:
                    ema_bias = 'SHORT'

        if 'daily' in tf_data:
            df_d = tf_data['daily']
            if 'Bull_OB' in df_d.columns and len(df_d) > 5:
                last5 = df_d.tail(5)
                has_bull = last5['Bull_OB'].fillna(False).any()
                has_bear = last5['Bear_OB'].fillna(False).any()
                if has_bull and not has_bear:
                    ob_bias = 'LONG'
                elif has_bear and not has_bull:
                    ob_bias = 'SHORT'

        if ema_bias == ob_bias:
            self.current_bias = ema_bias
        elif ema_bias != 'NEUTRAL':
            self.current_bias = ema_bias
        else:
            self.current_bias = ob_bias
        return self.current_bias

    def check_alignment(self, direction, category):
        """Check if trade direction aligns with EMA bias."""
        if category == 'monthly':
            return True
        if self.current_bias == 'LONG' and direction == 'PUT':
            return False
        if self.current_bias == 'SHORT' and direction == 'CALL':
            return False
        return True


# ============================================================
# LTF CHoCH CONFIRMATION
# ============================================================

def check_ltf_choch(df_5m, htf_signal_time, direction):
    """5M CHoCH confirmation for HTF OB entries."""
    if df_5m is None or len(df_5m) < 30 or 'Date' not in df_5m.columns:
        return True
    lookforward = V3_CONFIG.get('ltf_choch', {}).get('lookforward_bars', 10)
    h, l = df_5m['High'].values, df_5m['Low'].values
    try:
        match_idx = df_5m['Date'].searchsorted(htf_signal_time)
    except:
        return True
    if match_idx >= len(df_5m) - lookforward:
        return True
    start = max(match_idx, 5)
    end = min(match_idx + lookforward, len(df_5m))

    if direction == 'CALL':
        swing_high = np.max(h[max(0, start-5):start])
        for j in range(start, end):
            recent_low = np.min(l[max(0, j-3):j+1])
            prev_low = np.min(l[max(0, start-5):start])
            if recent_low > prev_low and h[j] > swing_high:
                return True
    else:
        swing_low = np.min(l[max(0, start-5):start])
        for j in range(start, end):
            recent_high = np.max(h[max(0, j-3):j+1])
            prev_high = np.max(h[max(0, start-5):start])
            if recent_high < prev_high and l[j] < swing_low:
                return True
    return False


# ============================================================
# VOLUME CONFIRMATION (BREAKERS)
# ============================================================

def volume_confirmation_check(df, idx):
    """Volume > 1.5x 20-bar average for breaker entries."""
    config = V3_CONFIG.get('volume_confirm', {})
    mult = config.get('multiplier', 1.5)
    lb = config.get('lookback', 20)
    if 'Volume' not in df.columns or idx < lb:
        return True
    vol = df['Volume'].values
    vol_ma = np.mean(vol[max(0, idx-lb):idx])
    if vol_ma <= 0:
        return True
    for j in range(max(0, idx-1), idx+1):
        if vol[j] > vol_ma * mult:
            return True
    return False


# ============================================================
# LIQUIDITY SWEEP DETECTION
# ============================================================

def detect_liquidity_levels(df):
    """Detect equal H/L + PDH/PDL sweeps. Returns (sweep_bull, sweep_bear)."""
    n = len(df)
    h, l, c = df['High'].values, df['Low'].values, df['Close'].values
    tolerance = V3_CONFIG.get('sweep', {}).get('equal_hl_tolerance', 0.0002)
    lookback = V3_CONFIG.get('sweep', {}).get('lookback_bars', 10)

    eq_high = np.zeros(n, dtype=bool)
    eq_low = np.zeros(n, dtype=bool)
    if n > 6:
        sh_mask = (h[2:-2] > h[1:-3]) & (h[2:-2] > h[3:-1]) & (h[2:-2] > h[:-4])
        sh_idx = np.where(sh_mask)[0] + 2
        if len(sh_idx) > 1:
            sh_v = h[sh_idx]
            for i in range(1, len(sh_idx)):
                s = max(0, i-10)
                if np.any(np.abs(sh_v[i] - sh_v[s:i]) / (sh_v[s:i] + 1e-10) < tolerance):
                    eq_high[sh_idx[i]] = True
        sl_mask = (l[2:-2] < l[1:-3]) & (l[2:-2] < l[3:-1]) & (l[2:-2] < l[:-4])
        sl_idx = np.where(sl_mask)[0] + 2
        if len(sl_idx) > 1:
            sl_v = l[sl_idx]
            for i in range(1, len(sl_idx)):
                s = max(0, i-10)
                if np.any(np.abs(sl_v[i] - sl_v[s:i]) / (sl_v[s:i] + 1e-10) < tolerance):
                    eq_low[sl_idx[i]] = True

    swept_pdh = np.zeros(n, dtype=bool)
    swept_pdl = np.zeros(n, dtype=bool)
    if 'Date' in df.columns and n > 0:
        dates = df['Date'].dt.date.values
        dc = np.where(dates[1:] != dates[:-1])[0] + 1
        ds = np.concatenate([[0], dc])
        de = np.concatenate([dc, [n]])
        for d in range(1, len(ds)):
            ps, pe, cs, ce = ds[d-1], de[d-1], ds[d], de[d]
            pdh, pdl = h[ps:pe].max(), l[ps:pe].min()
            swept_pdh[cs:ce] = (h[cs:ce] > pdh) & (c[cs:ce] < pdh)
            swept_pdl[cs:ce] = (l[cs:ce] < pdl) & (c[cs:ce] > pdl)

    bull_raw = (swept_pdl | eq_low).astype(np.int8)
    bear_raw = (swept_pdh | eq_high).astype(np.int8)
    cs_b, cs_r = np.cumsum(bull_raw), np.cumsum(bear_raw)
    sweep_bull = np.zeros(n, dtype=bool)
    sweep_bear = np.zeros(n, dtype=bool)
    for i in range(n):
        s = max(0, i - lookback + 1)
        sweep_bull[i] = (cs_b[i] - (cs_b[s-1] if s > 0 else 0)) > 0
        sweep_bear[i] = (cs_r[i] - (cs_r[s-1] if s > 0 else 0)) > 0
    return sweep_bull, sweep_bear


# ============================================================
# KILL ZONE SESSION FILTER
# ============================================================

def kill_zone_check(df, idx, category):
    """Block midday dead zone for scalps/0DTE."""
    if category == 'monthly':
        return True
    if 'Date' not in df.columns or idx >= len(df):
        return True
    dt = df['Date'].iloc[idx]
    minutes = dt.hour * 60 + dt.minute
    kz = V3_CONFIG.get('kill_zones', {})
    dead = kz.get('dead_zone', (690, 840))
    ny = kz.get('ny_open', (570, 690))
    power = kz.get('power_hour', (900, 960))
    if dead[0] <= minutes < dead[1]:
        return category not in ('scalp', '0dte')
    if (ny[0] <= minutes < ny[1]) or (power[0] <= minutes < power[1]) or (840 <= minutes < 900):
        return True
    return category not in ('scalp', '0dte')


# ============================================================
# VOLATILITY REGIME
# ============================================================

class VolatilityRegime:
    """ATR expansion + ADX regime classification."""
    def __init__(self):
        self.config = V3_CONFIG.get('volatility', {})

    def check(self, df, idx, category):
        if idx < 30:
            return True, 'default'
        atr = df['ATR_14'].values if 'ATR_14' in df.columns else None
        adx = df['ADX_14'].values if 'ADX_14' in df.columns else None
        if atr is None:
            return True, 'no_data'
        current_atr = atr[idx]
        avg_atr = np.mean(atr[max(0, idx-20):idx])
        atr_ratio = current_atr / (avg_atr + 1e-10)
        current_adx = adx[idx] if adx is not None else 25
        if atr_ratio > self.config.get('atr_expansion_threshold', 2.0):
            regime = 'volatile'
        elif current_adx > self.config.get('adx_trending', 30) and atr_ratio > 1.0:
            regime = 'trending'
        elif current_adx < self.config.get('adx_calm', 15):
            regime = 'calm'
        elif current_adx < self.config.get('adx_ranging', 20) and atr_ratio < self.config.get('atr_contraction', 0.8):
            regime = 'ranging'
        else:
            regime = 'trending'
        if regime == 'ranging':
            return False, regime
        if regime == 'calm' and category in ('scalp', '0dte'):
            return False, regime
        return True, regime


# ============================================================
# DISPLACEMENT CONFIRMATION
# ============================================================

def displacement_confirmation(df, idx, direction):
    """Body > 1.5x ATR near signal for sweep-based strategies."""
    config = V3_CONFIG.get('displacement', {})
    mult = config.get('atr_multiplier', 1.5)
    lf = config.get('lookforward_bars', 3)
    if 'ATR_14' not in df.columns:
        return True
    atr, c, o = df['ATR_14'].values, df['Close'].values, df['Open'].values
    for j in range(idx, min(idx + lf, len(df))):
        body = abs(c[j] - o[j])
        if body > atr[j] * mult:
            if direction == 'CALL' and c[j] > o[j]:
                return True
            if direction == 'PUT' and c[j] < o[j]:
                return True
    return False


# ============================================================
# RSI RISK GATE
# ============================================================

def risk_gate_check(df, idx, direction):
    """Block overbought CALL / oversold PUT."""
    config = V3_CONFIG.get('rsi_gate', {})
    if 'RSI_14' in df.columns:
        rsi = df['RSI_14'].iloc[idx]
        if direction == 'CALL' and rsi > config.get('overbought', 75):
            return False
        if direction == 'PUT' and rsi < config.get('oversold', 25):
            return False
    return True


# ============================================================
# ENTRY REFINEMENT
# ============================================================

def compute_refined_entry(df, idx, direction, signal_func):
    """Per-strategy limit entry. Returns refined price or None."""
    config = V3_CONFIG.get('entry_refinement', {})
    if idx < 1 or idx >= len(df):
        return None
    c, h, l, o = df['Close'].values, df['High'].values, df['Low'].values, df['Open'].values

    if signal_func in ('smc_order_block', 'daily_ob', 'weekly_ob', 'mitigation_confluence'):
        oi = idx - 1
        if oi < 0:
            return None
        oh, ol = max(o[oi], c[oi]), min(o[oi], c[oi])
        r = config.get('ob_retrace_pct', 0.50)
        return (ol + (oh - ol) * r) if direction == 'CALL' else (oh - (oh - ol) * r)
    elif signal_func == 'breaker_block':
        bh, bl = max(o[idx], c[idx]), min(o[idx], c[idx])
        r = config.get('breaker_retrace_pct', 0.30)
        return (bl + (bh - bl) * r) if direction == 'CALL' else (bh - (bh - bl) * r)
    elif signal_func == 'judas_swing':
        mid, rng = (h[idx] + l[idx]) / 2, h[idx] - l[idx]
        return (mid - rng * 0.20) if direction == 'CALL' else (mid + rng * 0.20)
    elif signal_func == 'displacement_engine':
        mid, rng = (h[idx] + l[idx]) / 2, h[idx] - l[idx]
        return (mid - rng * 0.15) if direction == 'CALL' else (mid + rng * 0.15)
    elif signal_func == 'ict_reversal' and idx >= 2:
        if direction == 'CALL':
            gt, gb = l[idx], h[idx - 2]
            return (gt + gb) / 2 if gt > gb else None
        else:
            gt, gb = l[idx - 2], h[idx]
            return (gt + gb) / 2 if gt > gb else None
    elif signal_func in ('daily_ema_trend', 'weekly_ema_trend') and 'EMA_21' in df.columns:
        ema = df['EMA_21'].iloc[idx]
        return min(ema, c[idx]) if direction == 'CALL' else max(ema, c[idx])
    return None


# ============================================================
# V3 MASTER FILTER — Called by engine for each signal
# ============================================================

def v3_master_filter(df, idx, direction, strategy, entry_price,
                     sweep_bull=None, sweep_bear=None,
                     vol_regime=None, df_5m=None):
    """
    Complete master filter pipeline. Returns (allowed, meta_dict).
    """
    meta = {
        'blocked_by': None,
        'regime': 'unknown',
        'refined_entry': None,
        'new_stop_pct': strategy.get('stop_pct', strategy.get('stop', 0.2)),
        'new_target_pct': strategy.get('target_pct', strategy.get('target', 0.4)),
    }

    cat = strategy.get('category', strategy.get('cat', 'scalp'))
    module = strategy.get('module', 'core_ob')
    sig = strategy.get('signal_func', strategy.get('sig', ''))

    if not kill_zone_check(df, idx, cat):
        meta['blocked_by'] = 'kill_zone'
        return False, meta

    if vol_regime:
        vol_ok, regime = vol_regime.check(df, idx, cat)
        meta['regime'] = regime
        if not vol_ok:
            meta['blocked_by'] = f'volatility_{regime}'
            return False, meta

    if sweep_bull is not None and sweep_bear is not None and idx < len(sweep_bull):
        if direction == 'CALL' and not sweep_bull[idx]:
            meta['blocked_by'] = 'sweep_required'
            return False, meta
        if direction == 'PUT' and idx < len(sweep_bear) and not sweep_bear[idx]:
            meta['blocked_by'] = 'sweep_required'
            return False, meta

    needs_disp = module in ('sweep', 'displacement') or strategy.get('needs_displacement', False)
    if needs_disp and not displacement_confirmation(df, idx, direction):
        meta['blocked_by'] = 'displacement'
        return False, meta

    if not risk_gate_check(df, idx, direction):
        meta['blocked_by'] = 'rsi_gate'
        return False, meta

    if strategy.get('needs_volume_confirm', False) and not volume_confirmation_check(df, idx):
        meta['blocked_by'] = 'volume_confirm'
        return False, meta

    if strategy.get('needs_ltf_choch', False) and df_5m is not None and 'Date' in df.columns:
        sig_time = df['Date'].iloc[idx]
        if not check_ltf_choch(df_5m, sig_time, direction):
            meta['blocked_by'] = 'ltf_choch'
            return False, meta

    max_dev = config_val = V3_CONFIG.get('entry_refinement', {}).get('max_deviation_pct', 0.005)
    refined = compute_refined_entry(df, idx, direction, sig)
    if refined is not None and refined > 0:
        diff = abs(refined - entry_price) / entry_price
        if diff < max_dev:
            meta['refined_entry'] = refined

    return True, meta


# ============================================================
# SCALING MANAGER — 2-Stage Exits
# ============================================================

class ScalingManager:
    """TP1 (50% close at target), TP2 runner (2.5x, trail at 50% MFE)."""

    def __init__(self):
        self.config = V3_CONFIG.get('scaling', {})
        self.trades = {}

    def init_trade(self, trade_id, entry_price, direction, stop_pct, target_pct):
        self.trades[trade_id] = {
            'entry': entry_price, 'direction': direction,
            'stop_pct': stop_pct, 'target_pct': target_pct,
            'tp1_hit': False, 'max_favorable': 0.0, 'trail_stop_pct': stop_pct,
        }

    def update(self, trade_id, current_price):
        """Returns {'action': 'hold'|'close_half'|'close_all', 'reason': str}"""
        t = self.trades.get(trade_id)
        if t is None:
            return {'action': 'hold'}
        entry = t['entry']
        tp1 = t['target_pct'] * self.config.get('tp1_pct', 1.0)
        tp2 = t['target_pct'] * self.config.get('tp2_multiplier', 2.5)
        trail_frac = self.config.get('trail_pct_of_mfe', 0.50)

        move = ((current_price - entry) / entry * 100) if t['direction'] == 'CALL' \
               else ((entry - current_price) / entry * 100)
        if move > t['max_favorable']:
            t['max_favorable'] = move

        if not t['tp1_hit']:
            if move <= -t['stop_pct']:
                return {'action': 'close_all', 'reason': 'STOP'}
            if move >= tp1:
                t['tp1_hit'] = True
                t['trail_stop_pct'] = -0.02
                return {'action': 'close_half', 'reason': 'TP1'}
        else:
            new_trail = t['max_favorable'] * trail_frac
            if new_trail > t['trail_stop_pct']:
                t['trail_stop_pct'] = new_trail
            if move >= tp2:
                return {'action': 'close_all', 'reason': 'TP2'}
            if move <= t['trail_stop_pct']:
                return {'action': 'close_all', 'reason': 'TRAIL'}
        return {'action': 'hold'}

    def remove_trade(self, trade_id):
        self.trades.pop(trade_id, None)
