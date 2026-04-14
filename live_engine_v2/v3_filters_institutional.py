"""
MASTER SYSTEM FILTERS — V3 ENHANCED WITH INSTITUTIONAL + WHALE SIGNALS
=========================================================================
Complete 16-filter pipeline combining V1 core (11 filters) + institutional module (5 filters).

CORE FILTER PIPELINE (1-11, from V1):
  1. Kill zone filter
  2. Volatility regime filter
  3. Sweep required filter
  4. Displacement confirmation filter
  5. RSI gate
  6. Volume confirmation
  7. LTF CHoCH confirmation
  8. Mode alignment
  9. EMA bias
  10. Entry refinement (zone, pullback, confluence)
  11. Scaling (TP1/TP2/trailing)

INSTITUTIONAL FILTERS (12-16, NEW):
  12. Institutional Flow Confirmation — InstitutionalFlowAnalyzer
  13. Whale Alignment — WhaleTracker
  14. Liquidity Map Validation — LiquidityMapper
  15. Volume Profile Gate — WhaleTracker.build_volume_profile()
  16. Institutional Confluence Score — Meta-filter tallying all institutional signals

OUTPUTS:
  - allowed (bool): Pass all 16 filters?
  - meta (dict): All filter metadata + institutional signals
  - pipeline_log (list): Per-filter results with details
"""

import numpy as np
import pandas as pd
from typing import Dict, Tuple, Optional, List
try:
    from config_v2 import V3_CONFIG
except ImportError:
    from config import V3_CONFIG

# Import institutional modules from same directory
try:
    from institutional_flow import InstitutionalFlowAnalyzer
    from whale_tracker import WhaleTracker
    from liquidity_map import LiquidityMapper
    from whale_intent import (
        WhaleIntentClassifier, ExecutionPlanner, WhaleState,
        ExecutionGrade, ICTSetupType, detect_continuation_flow
    )
    HAS_WHALE_INTENT = True
except ImportError:
    HAS_WHALE_INTENT = False
    # Fallback: define minimal stubs for testing
    class InstitutionalFlowAnalyzer:
        def get_institutional_score(self, price, direction=None):
            return 50
    class WhaleTracker:
        def get_whale_signal(self, price):
            return 'NEUTRAL', 0.5
        def build_volume_profile(self, df):
            return {'poc': df['Close'].median(), 'vah': df['High'].max(), 'val': df['Low'].min()}
        def find_accumulation_zones(self, df):
            return []
    class LiquidityMapper:
        def find_draw_targets(self, direction, current_price):
            return []
        def find_swept_pools(self, df, idx, direction):
            return []
        def find_blocking_icebergs(self, df, idx, price):
            return []


# ============================================================
# CORE FILTERS (1-11) — ADAPTED FROM V1
# ============================================================

class MarketModeDetector:
    """Prop-desk style decision tree (FILTER 8)."""
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
        """Check if strategy mode aligns with market mode (FILTER 8)."""
        if current_mode == 'NO_TRADE':
            return False, 'mode_no_trade'
        if current_mode == 'TREND' and strategy_mode == 'REVERSAL':
            if has_sweep:
                return True, 'reversal_with_sweep_override'
            return False, 'mode_mismatch'
        return True, 'ok'


class EMABiasFilter:
    """Strict 1H EMA21 directional filter (FILTER 9)."""
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
        """Check if trade direction aligns with EMA bias (FILTER 9)."""
        if category == 'monthly':
            return True
        if self.current_bias == 'LONG' and direction == 'PUT':
            return False
        if self.current_bias == 'SHORT' and direction == 'CALL':
            return False
        return True


def check_ltf_choch(df_5m, htf_signal_time, direction):
    """5M CHoCH confirmation for HTF OB entries (FILTER 7)."""
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


def volume_confirmation_check(df, idx):
    """Volume > 1.5x 20-bar average for breaker entries (FILTER 6)."""
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


def detect_liquidity_levels(df):
    """Detect equal H/L + PDH/PDL sweeps (FILTER 3). Returns (sweep_bull, sweep_bear)."""
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


def kill_zone_check(df, idx, category):
    """Block midday dead zone for scalps/0DTE (FILTER 1)."""
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


class VolatilityRegime:
    """ATR expansion + ADX regime classification (FILTER 2)."""
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


def displacement_confirmation(df, idx, direction):
    """Body > 1.5x ATR near signal for sweep-based strategies (FILTER 4)."""
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


def risk_gate_check(df, idx, direction):
    """Block overbought CALL / oversold PUT (FILTER 5)."""
    config = V3_CONFIG.get('rsi_gate', {})
    if 'RSI_14' in df.columns:
        rsi = df['RSI_14'].iloc[idx]
        if direction == 'CALL' and rsi > config.get('overbought', 75):
            return False
        if direction == 'PUT' and rsi < config.get('oversold', 25):
            return False
    return True


def compute_refined_entry(df, idx, direction, signal_func):
    """Per-strategy limit entry (FILTER 10). Returns refined price or None."""
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


class ScalingManager:
    """TP1 (50% close at target), TP2 runner (2.5x, trail at 50% MFE) (FILTER 11)."""
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


# ============================================================
# INSTITUTIONAL FILTERS (12-16) — NEW
# ============================================================

def institutional_flow_check(signal, row, df, context, config):
    """
    FILTER 12: Institutional Flow Confirmation
    Uses InstitutionalFlowAnalyzer to gauge institutional backing.
    """
    result = {
        'passed': True,
        'reason': 'pass',
        'institutional_score': 50,
        'tag': 'institutional_neutral',
        'size_modifier': 1.0,
    }

    try:
        analyzer = InstitutionalFlowAnalyzer()
        direction = signal.get('direction', 'CALL')
        price = signal.get('entry_price', row.get('Close', 0))

        inst_score = analyzer.get_institutional_score(price, direction)
        result['institutional_score'] = inst_score

        if inst_score >= 60:
            result['tag'] = 'institutional_confirmed'
            result['reason'] = 'pass'
        elif 40 <= inst_score < 60:
            result['tag'] = 'institutional_mixed'
            result['reason'] = 'warning'
            result['size_modifier'] = 0.70  # reduce by 30%
        else:
            result['passed'] = False
            result['reason'] = 'retail_dominated_no_institutional_backing'
            result['tag'] = 'retail_dominated'
    except Exception as e:
        result['error'] = str(e)

    return result


def whale_alignment_check(signal, row, df, context, config):
    """
    FILTER 13: Whale Intent-Based Execution (V3 UPGRADE)

    Replaces old binary confirm/deny + size boost with 3-state intent model:
      ACCUMULATION / DISTRIBUTION / AGGRESSION

    Maps (ICT setup type × whale state) → execution plan:
      Grade A+ = structure + whale alignment → aggressive entry
      Grade A  = structure only → standard entry
      Grade B  = conflict → reduced or skip

    Returns execution_plan with grade, entry_mode, size_multiplier, tp_mode, stop_mode.
    """
    result = {
        'passed': True,
        'reason': 'pass',
        'whale_direction': 'NEUTRAL',
        'whale_confidence': 0,
        'whale_state': 'NEUTRAL',
        'execution_grade': 'A',
        'entry_mode': 'confirmation_candle',
        'tp_mode': 'standard_scalp',
        'stop_mode': 'standard',
        'size_modifier': 1.0,
        'whale_evidence': [],
    }

    try:
        direction = signal.get('direction', 'CALL')
        signal_type = signal.get('signal_type', signal.get('signal_func', 'smc_ob'))
        inst_state = context.get('inst_state', {})

        # === NEW: Use WhaleIntentClassifier if available ===
        if HAS_WHALE_INTENT:
            # Get cached whale_intent from engine context, or classify fresh
            whale_intent = inst_state.get('whale_intent')

            if not whale_intent:
                # Classify fresh using cached tracker from context
                classifier = WhaleIntentClassifier()
                tracker = inst_state.get('_whale_tracker')
                whale_intent = classifier.classify(
                    signal.get('ticker', ''),
                    whale_tracker=tracker,
                )

            result['whale_direction'] = whale_intent.direction
            result['whale_confidence'] = whale_intent.confidence
            result['whale_state'] = whale_intent.state.value
            result['whale_evidence'] = whale_intent.evidence

            # Get execution plan from behavior map
            planner = ExecutionPlanner()
            setup_type = planner.classify_ict_setup(signal_type)
            plan = planner.get_plan(setup_type, whale_intent, direction)

            result['execution_grade'] = plan.grade.value
            result['entry_mode'] = plan.entry_mode
            result['tp_mode'] = plan.tp_mode
            result['stop_mode'] = plan.stop_mode
            result['size_modifier'] = plan.size_multiplier
            result['reason'] = plan.reason

            # Block if plan says skip
            if not plan.should_trade:
                result['passed'] = False
                result['reason'] = f"BLOCKED_{plan.reason}"

        else:
            # === FALLBACK: Old binary logic (no whale_intent module) ===
            whale_dir = inst_state.get('whale_direction', 'NEUTRAL')
            whale_conf = inst_state.get('whale_confidence', 0)

            if 'BUY' in str(whale_dir) or 'LONG' in str(whale_dir):
                whale_dir = 'LONG'
            elif 'SELL' in str(whale_dir) or 'SHORT' in str(whale_dir):
                whale_dir = 'SHORT'
            else:
                whale_dir = 'NEUTRAL'

            result['whale_direction'] = whale_dir
            result['whale_confidence'] = whale_conf

            call_aligned = (whale_dir in ('LONG', 'BUY'))
            put_aligned = (whale_dir in ('SHORT', 'SELL'))

            if (direction == 'CALL' and call_aligned) or (direction == 'PUT' and put_aligned):
                result['reason'] = 'pass_whale_aligned'
                if whale_conf >= 70:
                    result['size_modifier'] = 1.15
            elif whale_dir == 'NEUTRAL':
                result['reason'] = 'warning_no_whale_confirmation'
                result['size_modifier'] = 0.80
            else:
                if config.get('whale_tracker', {}).get('opposing_whale_block', True):
                    result['passed'] = False
                    result['reason'] = 'BLOCKED_trading_against_whales'
                else:
                    result['size_modifier'] = 0.50

    except Exception as e:
        result['error'] = str(e)

    return result


def liquidity_map_check(signal, row, df, context, config, idx):
    """
    FILTER 14: Liquidity Map Validation
    Uses cached liquidity state from engine or computes fresh.
    Checks: untapped liquidity draw, swept pools behind entry, blocking icebergs.
    """
    result = {
        'passed': True,
        'reason': 'pass',
        'has_draw': False,
        'has_swept_pool': False,
        'is_blocked': False,
        'draw_targets': [],
        'swept_pools_count': 0,
        'blocking_icebergs_count': 0,
        'bsl_count': 0,
        'ssl_count': 0,
        'voids_count': 0,
    }

    try:
        direction = signal.get('direction', 'CALL')
        price = signal.get('entry_price', row.get('Close', 0))
        inst_state = context.get('inst_state', {})

        # Use cached liquidity state from engine if available
        if inst_state.get('liquidity_pools') is not None or inst_state.get('bsl_pools') is not None:
            bsl_pools = inst_state.get('bsl_pools', inst_state.get('liquidity_pools', []))
            ssl_pools = inst_state.get('ssl_pools', [])
            voids = inst_state.get('liquidity_voids', [])
            sweeps = inst_state.get('recent_sweeps', [])
            icebergs = inst_state.get('iceberg_orders', [])
            draw = inst_state.get('draw_on_liquidity')

            result['bsl_count'] = len(bsl_pools) if isinstance(bsl_pools, list) else 0
            result['ssl_count'] = len(ssl_pools) if isinstance(ssl_pools, list) else 0
            result['voids_count'] = len(voids) if isinstance(voids, list) else 0

            # Check draw on liquidity
            if draw:
                draw_bias = getattr(draw, 'bias', str(draw)) if not isinstance(draw, dict) else draw.get('bias', '')
                has_upside = bool(getattr(draw, 'upside_targets', None)) if not isinstance(draw, dict) else bool(draw.get('upside_targets'))
                has_downside = bool(getattr(draw, 'downside_targets', None)) if not isinstance(draw, dict) else bool(draw.get('downside_targets'))
                if direction == 'CALL' and (has_upside or 'BULL' in str(draw_bias).upper()):
                    result['has_draw'] = True
                elif direction == 'PUT' and (has_downside or 'BEAR' in str(draw_bias).upper()):
                    result['has_draw'] = True
                elif has_upside or has_downside:
                    result['has_draw'] = True  # Some draw exists

            # Check swept pools (recent sweeps = fuel for the move)
            result['swept_pools_count'] = len(sweeps) if isinstance(sweeps, list) else 0
            result['has_swept_pool'] = result['swept_pools_count'] > 0

            # Check blocking icebergs
            if isinstance(icebergs, list) and icebergs:
                blocking = []
                for iceberg in icebergs:
                    ice_price = getattr(iceberg, 'price_level', 0) if not isinstance(iceberg, dict) else iceberg.get('price_level', 0)
                    ice_dir = getattr(iceberg, 'direction', '') if not isinstance(iceberg, dict) else iceberg.get('direction', '')
                    # Iceberg blocks if it's between entry and target
                    if direction == 'CALL' and ice_price > price and 'SELL' in str(ice_dir).upper():
                        blocking.append(ice_price)
                    elif direction == 'PUT' and ice_price < price and 'BUY' in str(ice_dir).upper():
                        blocking.append(ice_price)
                result['blocking_icebergs_count'] = len(blocking)
                result['is_blocked'] = len(blocking) > 0

        else:
            # Fallback: compute fresh
            mapper = LiquidityMapper()
            liq_state = mapper.get_liquidity_state(df)

            result['bsl_count'] = len(liq_state.bsl_pools)
            result['ssl_count'] = len(liq_state.ssl_pools)
            result['voids_count'] = len(liq_state.voids)
            result['swept_pools_count'] = len(liq_state.recent_sweeps)
            result['has_swept_pool'] = result['swept_pools_count'] > 0

            # Check draw
            if liq_state.draw_on_liquidity:
                dol = liq_state.draw_on_liquidity
                if direction == 'CALL' and (dol.upside_targets or 'BULL' in str(dol.bias).upper()):
                    result['has_draw'] = True
                elif direction == 'PUT' and (dol.downside_targets or 'BEAR' in str(dol.bias).upper()):
                    result['has_draw'] = True

            # Check icebergs via whale tracker
            try:
                tracker = WhaleTracker()
                icebergs = tracker.detect_iceberg_orders(df)
                for iceberg in icebergs:
                    ice_price = getattr(iceberg, 'price_level', 0)
                    if direction == 'CALL' and ice_price > price:
                        result['is_blocked'] = True
                        result['blocking_icebergs_count'] += 1
                    elif direction == 'PUT' and ice_price < price:
                        result['is_blocked'] = True
                        result['blocking_icebergs_count'] += 1
            except Exception:
                pass

        # === SCORING ===
        checks_passed = [result['has_draw'], result['has_swept_pool'], not result['is_blocked']]
        pass_count = sum(checks_passed)

        if pass_count == 3:
            result['reason'] = 'pass_full_liquidity_confluence'
        elif pass_count == 2:
            if not result['has_draw']:
                result['reason'] = 'warning_missing_draw_target'
            else:
                result['reason'] = 'pass_partial'
        elif result['is_blocked']:
            result['passed'] = False
            result['reason'] = 'BLOCKED_iceberg_resistance_blocking_path'
        else:
            result['reason'] = 'warning_marginal_liquidity'

    except Exception as e:
        result['error'] = str(e)

    return result


def volume_profile_gate(signal, row, df, context, config):
    """
    FILTER 15: Volume Profile Gate
    Uses cached volume profile from engine or computes fresh.
    Evaluates entry position relative to POC, HVN, LVN, VAH/VAL.
    """
    result = {
        'passed': True,
        'reason': 'pass',
        'poc': None,
        'vah': None,
        'val': None,
        'entry_position': 'unknown',
        'node_type': 'unknown',
        'inside_value_area': False,
        'near_poc': False,
        'at_hvn': False,
        'at_lvn': False,
        'vwap_deviation': None,
    }

    try:
        price = signal.get('entry_price', row.get('Close', 0))
        direction = signal.get('direction', 'CALL')
        inst_state = context.get('inst_state', {})

        # Use cached volume profile or compute fresh
        vp_data = inst_state.get('volume_profile')
        vwap_data = inst_state.get('vwap_data')

        if vp_data and not isinstance(vp_data, dict):
            # It's a VolumeProfile dataclass
            poc = getattr(vp_data, 'poc', None)
            vah = getattr(vp_data, 'vah', None)
            val = getattr(vp_data, 'val', None)
            hvn_levels = getattr(vp_data, 'hvn_levels', [])
            lvn_levels = getattr(vp_data, 'lvn_levels', [])
        elif isinstance(vp_data, dict):
            poc = vp_data.get('poc')
            vah = vp_data.get('vah')
            val = vp_data.get('val')
            hvn_levels = vp_data.get('hvn_levels', [])
            lvn_levels = vp_data.get('lvn_levels', [])
        else:
            # Fallback: compute fresh
            tracker = WhaleTracker()
            profile = tracker.build_volume_profile(df)
            poc = getattr(profile, 'poc', None)
            vah = getattr(profile, 'vah', None)
            val = getattr(profile, 'val', None)
            hvn_levels = getattr(profile, 'hvn_levels', [])
            lvn_levels = getattr(profile, 'lvn_levels', [])

        result['poc'] = poc
        result['vah'] = vah
        result['val'] = val

        # VWAP deviation
        if vwap_data:
            vwap_val = getattr(vwap_data, 'vwap', None) if not isinstance(vwap_data, dict) else vwap_data.get('vwap')
            if vwap_val and vwap_val > 0:
                result['vwap_deviation'] = (price - vwap_val) / vwap_val * 100

        if vah and val:
            result['inside_value_area'] = val <= price <= vah

        # Check proximity to POC (within 0.3%)
        if poc and poc > 0:
            poc_dist = abs(price - poc) / poc
            result['near_poc'] = poc_dist < 0.003

        # Check HVN/LVN positioning
        atr = float(row.get('ATR_14', row.get('atr_14', abs(price * 0.005)))) if hasattr(row, 'get') else abs(price * 0.005)
        for hvn in (hvn_levels or []):
            hvn_price = hvn if isinstance(hvn, (int, float)) else getattr(hvn, 'price', 0)
            if abs(price - hvn_price) < atr * 0.5:
                result['at_hvn'] = True
                result['node_type'] = 'HVN'
                break
        for lvn in (lvn_levels or []):
            lvn_price = lvn if isinstance(lvn, (int, float)) else getattr(lvn, 'price', 0)
            if abs(price - lvn_price) < atr * 0.5:
                result['at_lvn'] = True
                result['node_type'] = 'LVN'
                break

        # === SCORING ===
        if result['inside_value_area']:
            result['entry_position'] = 'inside_value_area'
            if result['at_hvn'] or result['near_poc']:
                result['reason'] = 'pass_at_institutional_support'
            else:
                result['reason'] = 'pass_inside_value_area'
        elif result['at_lvn']:
            # At LVN — check if heading TOWARD POC (good) or AWAY (risky)
            if poc and ((direction == 'CALL' and price < poc) or (direction == 'PUT' and price > poc)):
                result['entry_position'] = 'lvn_toward_poc'
                result['reason'] = 'pass_lvn_fast_move_toward_poc'
            else:
                result['entry_position'] = 'lvn_away_from_poc'
                result['reason'] = 'warning_trading_away_from_fair_value'
        else:
            # Outside value area
            inst_score = context.get('inst_state', {}).get('flow_score')
            if inst_score is None:
                inst_score = signal.get('institutional_score', 50)
            else:
                inst_score = getattr(inst_score, 'score', inst_score) if not isinstance(inst_score, (int, float)) else inst_score
            if inst_score >= 70:
                result['entry_position'] = 'outside_va_institutional'
                result['reason'] = 'pass_outside_value_area_strong_institutional'
            else:
                result['passed'] = False
                result['entry_position'] = 'outside_va_weak'
                result['reason'] = 'BLOCKED_outside_value_area_weak_institutional'

    except Exception as e:
        result['error'] = str(e)

    return result


def institutional_confluence_score(meta, config):
    """
    FILTER 16: Institutional Confluence Score (Meta-filter)
    Counts how many institutional signals agree with trade.
    Tallies: institutional_flow + whale_direction + liquidity_draw + volume_profile + wyckoff_phase
    """
    result = {
        'passed': True,
        'reason': 'pass',
        'confluence_count': 0,
        'agreeing_factors': [],
        'disagreeing_factors': [],
        'size_modifier': 1.0,
    }

    try:
        # Extract signals from meta
        inst_ok = meta.get('institutional_score', 50) >= 40
        whale_ok = meta.get('whale_direction') in ('LONG', 'SHORT', 'NEUTRAL')
        liquidity_ok = meta.get('has_draw', False) or meta.get('has_swept_pool', False)
        volume_ok = meta.get('inside_value_area', False) or meta.get('node_type') == 'hvn'

        confluence_count = 0
        if inst_ok:
            result['agreeing_factors'].append('institutional_flow')
            confluence_count += 1
        else:
            result['disagreeing_factors'].append('institutional_flow')

        if whale_ok:
            result['agreeing_factors'].append('whale_alignment')
            confluence_count += 1
        else:
            result['disagreeing_factors'].append('whale_alignment')

        if liquidity_ok:
            result['agreeing_factors'].append('liquidity_map')
            confluence_count += 1
        else:
            result['disagreeing_factors'].append('liquidity_map')

        if volume_ok:
            result['agreeing_factors'].append('volume_profile')
            confluence_count += 1
        else:
            result['disagreeing_factors'].append('volume_profile')

        # Wyckoff phase (if available in meta)
        wyckoff = meta.get('wyckoff_phase', None)
        if wyckoff in ('accumulation', 'distribution'):
            result['agreeing_factors'].append('wyckoff_phase')
            confluence_count += 1

        result['confluence_count'] = confluence_count

        if confluence_count >= 4:
            result['reason'] = 'strong_confluence'
            result['size_modifier'] = 1.25  # increase by 25%
        elif confluence_count == 3:
            result['reason'] = 'good_confluence'
            result['size_modifier'] = 1.0  # standard
        elif confluence_count == 2:
            result['reason'] = 'marginal_confluence'
            result['size_modifier'] = 0.75  # reduce by 25%
        else:
            result['passed'] = False
            result['reason'] = 'insufficient_institutional_confluence'

    except Exception as e:
        result['error'] = str(e)

    return result


# ============================================================
# MASTER 16-FILTER FUNCTION (MAIN ENTRY POINT)
# ============================================================

def v3_master_filter_institutional(signal, row, df, context, config):
    """
    Complete 16-filter pipeline (V1 core + institutional).

    Args:
        signal (dict): Signal data (direction, entry_price, strategy, etc.)
        row (pd.Series): Current candle data
        df (pd.DataFrame): Full timeframe OHLCV data
        context (dict): Market context (mode, mode_detector, vol_regime, etc.)
        config (dict): Filter configuration

    Returns:
        (allowed: bool, meta: dict, pipeline_log: list)
    """
    meta = {
        'blocked_by': None,
        'regime': 'unknown',
        'refined_entry': None,
        'position_size_modifier': 1.0,
        # V1 fields
        'new_stop_pct': signal.get('stop_pct', config.get('default_stop', 0.02)),
        'new_target_pct': signal.get('target_pct', config.get('default_target', 0.04)),
        # Institutional fields
        'institutional_score': 50,
        'whale_confidence': 0.5,
        'whale_direction': 'NEUTRAL',
        'liquidity_draw_target': None,
        'swept_pools_count': 0,
        'volume_profile_position': 'unknown',
        'institutional_confluence_count': 0,
    }
    pipeline_log = []
    idx = context.get('idx', 0)
    direction = signal.get('direction', 'CALL')
    category = signal.get('category', signal.get('cat', 'scalp'))
    strategy_module = signal.get('module', 'core_ob')
    signal_func = signal.get('signal_func', signal.get('sig', ''))

    # ---- CORE FILTERS (1-11) ----

    # FILTER 1: Kill Zone
    if not kill_zone_check(df, idx, category):
        meta['blocked_by'] = 'kill_zone'
        pipeline_log.append({'filter': 1, 'name': 'kill_zone', 'passed': False, 'reason': 'session_dead_zone'})
        return False, meta, pipeline_log
    pipeline_log.append({'filter': 1, 'name': 'kill_zone', 'passed': True})

    # FILTER 2: Volatility Regime
    vol_regime = context.get('vol_regime')
    if vol_regime:
        vol_ok, regime = vol_regime.check(df, idx, category)
        meta['regime'] = regime
        if not vol_ok:
            meta['blocked_by'] = f'volatility_{regime}'
            pipeline_log.append({'filter': 2, 'name': 'volatility_regime', 'passed': False, 'reason': regime})
            return False, meta, pipeline_log
    pipeline_log.append({'filter': 2, 'name': 'volatility_regime', 'passed': True, 'regime': meta.get('regime')})

    # FILTER 3: Sweep Required
    sweep_bull = context.get('sweep_bull')
    sweep_bear = context.get('sweep_bear')
    if sweep_bull is not None and sweep_bear is not None and idx < len(sweep_bull):
        if direction == 'CALL' and not sweep_bull[idx]:
            meta['blocked_by'] = 'sweep_required'
            pipeline_log.append({'filter': 3, 'name': 'sweep_required', 'passed': False, 'reason': 'no_bullish_sweep'})
            return False, meta, pipeline_log
        if direction == 'PUT' and idx < len(sweep_bear) and not sweep_bear[idx]:
            meta['blocked_by'] = 'sweep_required'
            pipeline_log.append({'filter': 3, 'name': 'sweep_required', 'passed': False, 'reason': 'no_bearish_sweep'})
            return False, meta, pipeline_log
    pipeline_log.append({'filter': 3, 'name': 'sweep_required', 'passed': True})

    # FILTER 4: Displacement Confirmation
    needs_disp = strategy_module in ('sweep', 'displacement') or signal.get('needs_displacement', False)
    if needs_disp and not displacement_confirmation(df, idx, direction):
        meta['blocked_by'] = 'displacement'
        pipeline_log.append({'filter': 4, 'name': 'displacement_confirmation', 'passed': False, 'reason': 'insufficient_body_size'})
        return False, meta, pipeline_log
    pipeline_log.append({'filter': 4, 'name': 'displacement_confirmation', 'passed': True})

    # FILTER 5: RSI Gate
    if not risk_gate_check(df, idx, direction):
        meta['blocked_by'] = 'rsi_gate'
        pipeline_log.append({'filter': 5, 'name': 'rsi_gate', 'passed': False, 'reason': f'{direction}_overbought_oversold'})
        return False, meta, pipeline_log
    pipeline_log.append({'filter': 5, 'name': 'rsi_gate', 'passed': True})

    # FILTER 6: Volume Confirmation
    if signal.get('needs_volume_confirm', False) and not volume_confirmation_check(df, idx):
        meta['blocked_by'] = 'volume_confirm'
        pipeline_log.append({'filter': 6, 'name': 'volume_confirmation', 'passed': False, 'reason': 'volume_too_low'})
        return False, meta, pipeline_log
    pipeline_log.append({'filter': 6, 'name': 'volume_confirmation', 'passed': True})

    # FILTER 7: LTF CHoCH Confirmation
    df_5m = context.get('df_5m')
    if signal.get('needs_ltf_choch', False) and df_5m is not None and 'Date' in df.columns:
        sig_time = df['Date'].iloc[idx]
        if not check_ltf_choch(df_5m, sig_time, direction):
            meta['blocked_by'] = 'ltf_choch'
            pipeline_log.append({'filter': 7, 'name': 'ltf_choch_confirmation', 'passed': False, 'reason': 'no_5m_choch'})
            return False, meta, pipeline_log
    pipeline_log.append({'filter': 7, 'name': 'ltf_choch_confirmation', 'passed': True})

    # FILTER 8: Mode Alignment
    mode_detector = context.get('mode_detector')
    if mode_detector:
        strategy_mode = signal.get('strategy_mode', 'TREND')
        current_mode = mode_detector.current_mode
        has_sweep = (sweep_bull is not None and idx < len(sweep_bull) and sweep_bull[idx]) or \
                    (sweep_bear is not None and idx < len(sweep_bear) and sweep_bear[idx])
        mode_ok, mode_reason = mode_detector.check_mode_alignment(strategy_mode, current_mode, has_sweep)
        if not mode_ok:
            meta['blocked_by'] = mode_reason
            pipeline_log.append({'filter': 8, 'name': 'mode_alignment', 'passed': False, 'reason': mode_reason})
            return False, meta, pipeline_log
    pipeline_log.append({'filter': 8, 'name': 'mode_alignment', 'passed': True})

    # FILTER 9: EMA Bias
    ema_filter = context.get('ema_filter')
    if ema_filter:
        if not ema_filter.check_alignment(direction, category):
            meta['blocked_by'] = 'ema_bias'
            pipeline_log.append({'filter': 9, 'name': 'ema_bias', 'passed': False, 'reason': f'misaligned_with_{ema_filter.current_bias}'})
            return False, meta, pipeline_log
    pipeline_log.append({'filter': 9, 'name': 'ema_bias', 'passed': True})

    # FILTER 10: Entry Refinement
    max_dev = config.get('entry_refinement', {}).get('max_deviation_pct', 0.005)
    entry_price = signal.get('entry_price', row.get('Close', 0))
    refined = compute_refined_entry(df, idx, direction, signal_func)
    if refined is not None and refined > 0:
        diff = abs(refined - entry_price) / entry_price if entry_price > 0 else 0
        if diff < max_dev:
            meta['refined_entry'] = refined
    pipeline_log.append({'filter': 10, 'name': 'entry_refinement', 'passed': True, 'refined': meta.get('refined_entry')})

    # FILTER 11: Scaling (TP1/TP2/trailing) — Always passes, used at exit
    pipeline_log.append({'filter': 11, 'name': 'scaling_manager', 'passed': True})

    # ---- INSTITUTIONAL FILTERS (12-16) ----

    # FILTER 12: Institutional Flow Confirmation
    inst_result = institutional_flow_check(signal, row, df, context, config)
    meta['institutional_score'] = inst_result.get('institutional_score', 50)
    if not inst_result['passed']:
        meta['blocked_by'] = 'institutional_flow'
        pipeline_log.append({'filter': 12, 'name': 'institutional_flow_confirmation', 'passed': False, **inst_result})
        return False, meta, pipeline_log
    meta['position_size_modifier'] *= inst_result.get('size_modifier', 1.0)
    pipeline_log.append({'filter': 12, 'name': 'institutional_flow_confirmation', 'passed': True, **inst_result})

    # FILTER 13: Whale Intent Execution (V3 — 3-state intent model)
    whale_result = whale_alignment_check(signal, row, df, context, config)
    meta['whale_direction'] = whale_result.get('whale_direction', 'NEUTRAL')
    meta['whale_confidence'] = whale_result.get('whale_confidence', 0.5)
    meta['whale_state'] = whale_result.get('whale_state', 'NEUTRAL')
    meta['execution_grade'] = whale_result.get('execution_grade', 'A')
    meta['entry_mode'] = whale_result.get('entry_mode', 'confirmation_candle')
    meta['tp_mode'] = whale_result.get('tp_mode', 'standard_scalp')
    meta['stop_mode'] = whale_result.get('stop_mode', 'standard')
    if not whale_result['passed']:
        meta['blocked_by'] = 'whale_intent'
        pipeline_log.append({'filter': 13, 'name': 'whale_intent', 'passed': False, **whale_result})
        return False, meta, pipeline_log
    meta['position_size_modifier'] *= whale_result.get('size_modifier', 1.0)
    pipeline_log.append({'filter': 13, 'name': 'whale_intent', 'passed': True, **whale_result})

    # FILTER 14: Liquidity Map Validation
    liq_result = liquidity_map_check(signal, row, df, context, config, idx)
    meta['liquidity_draw_target'] = liq_result.get('draw_targets', [])[0] if liq_result.get('draw_targets') else None
    meta['swept_pools_count'] = len(liq_result.get('swept_pools', []))
    if not liq_result['passed']:
        meta['blocked_by'] = 'liquidity_map'
        pipeline_log.append({'filter': 14, 'name': 'liquidity_map_validation', 'passed': False, **liq_result})
        return False, meta, pipeline_log
    pipeline_log.append({'filter': 14, 'name': 'liquidity_map_validation', 'passed': True, **liq_result})

    # FILTER 15: Volume Profile Gate
    vol_prof_result = volume_profile_gate(signal, row, df, context, config)
    meta['volume_profile_position'] = vol_prof_result.get('entry_position', 'unknown')
    if not vol_prof_result['passed']:
        meta['blocked_by'] = 'volume_profile'
        pipeline_log.append({'filter': 15, 'name': 'volume_profile_gate', 'passed': False, **vol_prof_result})
        return False, meta, pipeline_log
    pipeline_log.append({'filter': 15, 'name': 'volume_profile_gate', 'passed': True, **vol_prof_result})

    # FILTER 16: Institutional Confluence Score (Meta-filter)
    confluence_result = institutional_confluence_score(meta, config)
    meta['institutional_confluence_count'] = confluence_result.get('confluence_count', 0)
    if not confluence_result['passed']:
        meta['blocked_by'] = 'institutional_confluence'
        pipeline_log.append({'filter': 16, 'name': 'institutional_confluence_score', 'passed': False, **confluence_result})
        return False, meta, pipeline_log
    meta['position_size_modifier'] *= confluence_result.get('size_modifier', 1.0)
    pipeline_log.append({'filter': 16, 'name': 'institutional_confluence_score', 'passed': True, **confluence_result})

    # All filters passed
    pipeline_log.append({'summary': 'all_16_filters_passed', 'final_size_modifier': meta['position_size_modifier']})
    return True, meta, pipeline_log


# ============================================================
# LOGGED VERSION — Per-filter detailed logging
# ============================================================

def v3_master_filter_institutional_logged(signal, row, df, context, config, logger=None):
    """
    Wrapper around v3_master_filter_institutional with per-filter logging.
    Same signature, returns (allowed, meta, pipeline_log) with logged output.
    """
    allowed, meta, pipeline_log = v3_master_filter_institutional(signal, row, df, context, config)

    if logger:
        logger.info(f"=== INSTITUTIONAL FILTER PIPELINE (16 FILTERS) ===")
        logger.info(f"Signal: {signal.get('signal_func', 'unknown')} | Direction: {signal.get('direction')} | Entry: {signal.get('entry_price')}")

        for entry in pipeline_log:
            if 'summary' in entry:
                logger.info(f"[SUMMARY] {entry['summary']} | Size Modifier: {entry.get('final_size_modifier', 1.0):.2f}x")
            elif 'filter' in entry:
                fnum = entry['filter']
                fname = entry.get('name', 'unknown')
                passed = entry.get('passed', False)
                status = 'PASS' if passed else 'FAIL'
                logger.info(f"[Filter {fnum:02d}] {fname:<35} [{status}]", extra={'entry': entry})

        logger.info(f"=== RESULT: {'ALLOWED' if allowed else 'BLOCKED'} ===")
        if not allowed:
            logger.warning(f"Blocked by: {meta.get('blocked_by', 'unknown')}")
        logger.info(f"Final Position Size Modifier: {meta.get('position_size_modifier', 1.0):.2f}x")

    return allowed, meta, pipeline_log
