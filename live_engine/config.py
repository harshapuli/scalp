"""
LIVE TRADING ENGINE — MASTER SYSTEM CONFIGURATION
====================================================
THE ONE SYSTEM: 20 strategies, 7 modules, decision tree routing.
Built from 7-year backtest: 5,436 trades, 66.7% WR, $0.46/trade.

Modules:
  A. HTF Director   — Weekly/Daily/4HR OB retests (sniper, $2.96/trade)
  B. Core OB        — SMC Order Blocks 1M/5M/15M/1H (71.5% WR, engine)
  C. Confluence      — Mitigation + ICT Reversal (64.1% WR)
  D. Breaker         — Post-sweep reversals (55.8% WR, volume confirmed)
  E. Sweep           — Judas Swing reversals (64.9% WR)
  F. Displacement    — Merged BOS+Propulsion+CISD+Trap_Shift (one clean model)
  G. Swing           — Daily/Weekly trend follows (high PnL/trade)

Decision Tree:
  MODE 1: TREND      → OB, Confluence, Displacement, Swing, HTF Director
  MODE 2: REVERSAL   → Breaker, Sweep (only after liquidity sweep)
  MODE 3: NO_TRADE   → Midday chop, low ATR, no sweep
"""

# ============================================
# API CREDENTIALS
# ============================================
ALPACA_API_KEY = "PKBO4VZLU4KHQEQ5MXJWMJZOI7"
ALPACA_API_SECRET = "7hHmRMRhxpaaz8TJajMFpZ4zEdhfjA3JJrFMbGSM4iyg"
ALPACA_BASE_URL = "https://paper-api.alpaca.markets"   # Switch to live when ready
ALPACA_DATA_URL = "https://data.alpaca.markets"

# Unusual Whales (for whale overlay)
UW_API_KEY = "b9262618-c22a-41c9-9d24-acbef81b9547"
UW_BASE_URL = "https://api.unusualwhales.com/api"

# ============================================
# TICKER UNIVERSE
# ============================================
TICKERS = {
    'SPY':  {'type': 'etf',   'beta': 1.0, 'spread_pct': 0.01, 'sector': 'Market'},
    'QQQ':  {'type': 'etf',   'beta': 1.2, 'spread_pct': 0.01, 'sector': 'Market'},
    'AAPL': {'type': 'stock', 'beta': 1.2, 'spread_pct': 0.012, 'sector': 'Tech/HW'},
    'TSLA': {'type': 'stock', 'beta': 2.0, 'spread_pct': 0.03, 'sector': 'EV/Tech'},
    'NVDA': {'type': 'stock', 'beta': 1.8, 'spread_pct': 0.02, 'sector': 'Tech/AI'},
    'PLTR': {'type': 'stock', 'beta': 1.7, 'spread_pct': 0.04, 'sector': 'Tech/Gov'},
    'GOOGL':{'type': 'stock', 'beta': 1.1, 'spread_pct': 0.015, 'sector': 'Tech/Ad'},
}

# ============================================
# RISK MANAGEMENT
# ============================================
RISK = {
    'max_positions': 5,
    'max_per_ticker': 2,
    'max_daily_loss_pct': 3.0,
    'max_daily_trades': 30,
    'position_size_pct': 2.0,
    'scale_by_wr': True,
    'account_size': 25000,
}

# ============================================
# MASTER SYSTEM: 20 STRATEGIES — 7 MODULES
# ============================================
# Each strategy:
#   name, timeframe, signal_func, hold_bars, stop_pct, target_pct,
#   instrument, category, module, mode,
#   backtest_wr, backtest_pnl, pnl_per_trade,
#   needs_ltf_choch, needs_displacement, needs_volume_confirm

STRATEGIES = [
    # ══════════════════════════════════════════════════════════════
    # MODULE A — HTF DIRECTOR (sniper trades, $2.96/trade)
    # MODE: TREND | LTF CHoCH required for entry confirmation
    # ══════════════════════════════════════════════════════════════
    {
        'name': 'Weekly_OB_Retest',
        'timeframe': 'weekly',
        'signal_func': 'weekly_ob',
        'hold_bars': 2,
        'stop_pct': 3.20,
        'target_pct': 5.50,
        'instrument': 'Monthly_DeepITM',
        'category': 'monthly',
        'module': 'htf_director',
        'mode': 'TREND',
        'backtest_wr': 100.0,
        'backtest_pnl': 125.20,
        'pnl_per_trade': 13.91,
        'needs_ltf_choch': True,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },
    {
        'name': 'Daily_OB_Retest',
        'timeframe': 'daily',
        'signal_func': 'daily_ob',
        'hold_bars': 4,
        'stop_pct': 1.50,
        'target_pct': 2.60,
        'instrument': 'Monthly_DeepITM',
        'category': 'monthly',
        'module': 'htf_director',
        'mode': 'TREND',
        'backtest_wr': 87.5,
        'backtest_pnl': 113.60,
        'pnl_per_trade': 4.73,
        'needs_ltf_choch': True,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },
    {
        'name': '4HR_OB_Retest',
        'timeframe': '4hr',
        'signal_func': 'smc_order_block',
        'hold_bars': 15,
        'stop_pct': 0.75,
        'target_pct': 1.30,
        'instrument': 'Monthly_DeepITM',
        'category': 'monthly',
        'module': 'htf_director',
        'mode': 'TREND',
        'backtest_wr': 84.7,
        'backtest_pnl': 477.36,
        'pnl_per_trade': 2.28,
        'needs_ltf_choch': True,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },

    # ══════════════════════════════════════════════════════════════
    # MODULE B — CORE OB ENGINE (primary money maker, 71.5% WR)
    # MODE: TREND
    # ══════════════════════════════════════════════════════════════
    {
        'name': 'SMC_OB_1M',
        'timeframe': '1min',
        'signal_func': 'smc_order_block',
        'hold_bars': 14,
        'stop_pct': 0.10,
        'target_pct': 0.18,
        'instrument': '0DTE_ATM',
        'category': 'scalp',
        'module': 'core_ob',
        'mode': 'TREND',
        'backtest_wr': 67.8,
        'backtest_pnl': 451.77,
        'pnl_per_trade': 0.16,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },
    {
        'name': 'SMC_OB_5M',
        'timeframe': '5min',
        'signal_func': 'smc_order_block',
        'hold_bars': 18,
        'stop_pct': 0.13,
        'target_pct': 0.22,
        'instrument': '0DTE_ATM',
        'category': '0dte',
        'module': 'core_ob',
        'mode': 'TREND',
        'backtest_wr': 80.2,
        'backtest_pnl': 135.68,
        'pnl_per_trade': 0.41,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },
    {
        'name': 'SMC_OB_15M',
        'timeframe': '15min',
        'signal_func': 'smc_order_block',
        'hold_bars': 12,
        'stop_pct': 0.22,
        'target_pct': 0.40,
        'instrument': 'Weekly_ATM',
        'category': 'weekly',
        'module': 'core_ob',
        'mode': 'TREND',
        'backtest_wr': 84.6,
        'backtest_pnl': 100.99,
        'pnl_per_trade': 0.44,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },
    {
        'name': 'SMC_OB_1H',
        'timeframe': '1hr',
        'signal_func': 'smc_order_block',
        'hold_bars': 8,
        'stop_pct': 0.42,
        'target_pct': 0.72,
        'instrument': 'Weekly_DeepITM',
        'category': 'weekly',
        'module': 'core_ob',
        'mode': 'TREND',
        'backtest_wr': 85.0,
        'backtest_pnl': 537.10,
        'pnl_per_trade': 1.68,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },

    # ══════════════════════════════════════════════════════════════
    # MODULE C — CONFLUENCE ENGINE (OB + FVG + premium/discount)
    # MODE: TREND | Displacement required for Mitigation
    # ══════════════════════════════════════════════════════════════
    {
        'name': 'Mitigation_Confluence_5M',
        'timeframe': '5min',
        'signal_func': 'mitigation_confluence',
        'hold_bars': 22,
        'stop_pct': 0.14,
        'target_pct': 0.28,
        'instrument': '0DTE_ATM',
        'category': '0dte',
        'module': 'confluence',
        'mode': 'TREND',
        'backtest_wr': 100.0,
        'backtest_pnl': 1.57,
        'pnl_per_trade': 0.52,
        'needs_ltf_choch': False,
        'needs_displacement': True,
        'needs_volume_confirm': False,
    },
    {
        'name': 'Mitigation_Confluence_15M',
        'timeframe': '15min',
        'signal_func': 'mitigation_confluence',
        'hold_bars': 13,
        'stop_pct': 0.24,
        'target_pct': 0.48,
        'instrument': 'Weekly_ATM',
        'category': 'weekly',
        'module': 'confluence',
        'mode': 'TREND',
        'backtest_wr': 0.0,
        'backtest_pnl': -0.59,
        'pnl_per_trade': -0.30,
        'needs_ltf_choch': False,
        'needs_displacement': True,
        'needs_volume_confirm': False,
    },
    {
        'name': 'ICT_Reversal_5M',
        'timeframe': '5min',
        'signal_func': 'ict_reversal',
        'hold_bars': 25,
        'stop_pct': 0.14,
        'target_pct': 0.28,
        'instrument': '0DTE_ATM',
        'category': '0dte',
        'module': 'confluence',
        'mode': 'TREND',
        'backtest_wr': 63.9,
        'backtest_pnl': 26.21,
        'pnl_per_trade': 0.32,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },
    {
        'name': 'ICT_Reversal_15M',
        'timeframe': '15min',
        'signal_func': 'ict_reversal',
        'hold_bars': 14,
        'stop_pct': 0.24,
        'target_pct': 0.48,
        'instrument': 'Weekly_ATM',
        'category': 'weekly',
        'module': 'confluence',
        'mode': 'TREND',
        'backtest_wr': 64.6,
        'backtest_pnl': 17.92,
        'pnl_per_trade': 0.28,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },

    # ══════════════════════════════════════════════════════════════
    # MODULE D — BREAKER ENGINE (post-sweep reversals)
    # MODE: REVERSAL | Volume confirmation required
    # ══════════════════════════════════════════════════════════════
    {
        'name': 'Breaker_Block_5M',
        'timeframe': '5min',
        'signal_func': 'breaker_block',
        'hold_bars': 25,
        'stop_pct': 0.14,
        'target_pct': 0.25,
        'instrument': '0DTE_ATM',
        'category': '0dte',
        'module': 'breaker',
        'mode': 'REVERSAL',
        'backtest_wr': 58.6,
        'backtest_pnl': 21.83,
        'pnl_per_trade': 0.22,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': True,
    },
    {
        'name': 'Breaker_Block_15M',
        'timeframe': '15min',
        'signal_func': 'breaker_block',
        'hold_bars': 12,
        'stop_pct': 0.22,
        'target_pct': 0.42,
        'instrument': 'Weekly_ATM',
        'category': 'weekly',
        'module': 'breaker',
        'mode': 'REVERSAL',
        'backtest_wr': 52.1,
        'backtest_pnl': 13.43,
        'pnl_per_trade': 0.18,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': True,
    },

    # ══════════════════════════════════════════════════════════════
    # MODULE E — SWEEP ENGINE (Judas swing reversals)
    # MODE: REVERSAL
    # ══════════════════════════════════════════════════════════════
    {
        'name': 'Judas_Swing_1M',
        'timeframe': '1min',
        'signal_func': 'judas_swing',
        'hold_bars': 30,
        'stop_pct': 0.10,
        'target_pct': 0.18,
        'instrument': '0DTE_ATM',
        'category': 'scalp',
        'module': 'sweep',
        'mode': 'REVERSAL',
        'backtest_wr': 62.4,
        'backtest_pnl': 25.64,
        'pnl_per_trade': 0.21,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },
    {
        'name': 'Judas_Swing_3M',
        'timeframe': '3min',
        'signal_func': 'judas_swing',
        'hold_bars': 15,
        'stop_pct': 0.14,
        'target_pct': 0.25,
        'instrument': '0DTE_ATM',
        'category': 'scalp',
        'module': 'sweep',
        'mode': 'REVERSAL',
        'backtest_wr': 72.1,
        'backtest_pnl': 15.00,
        'pnl_per_trade': 0.35,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },

    # ══════════════════════════════════════════════════════════════
    # MODULE F — DISPLACEMENT ENGINE (merged BOS+Propulsion+CISD+Trap_Shift)
    # MODE: TREND | One clean model replaces 4 noisy strategies
    # ══════════════════════════════════════════════════════════════
    {
        'name': 'Displacement_Engine_5M',
        'timeframe': '5min',
        'signal_func': 'displacement_engine',
        'hold_bars': 22,
        'stop_pct': 0.14,
        'target_pct': 0.30,
        'instrument': '0DTE_ATM',
        'category': '0dte',
        'module': 'displacement',
        'mode': 'TREND',
        'backtest_wr': 57.9,
        'backtest_pnl': 75.08,
        'pnl_per_trade': 0.28,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },
    {
        'name': 'Displacement_Engine_15M',
        'timeframe': '15min',
        'signal_func': 'displacement_engine',
        'hold_bars': 14,
        'stop_pct': 0.24,
        'target_pct': 0.50,
        'instrument': 'Weekly_ATM',
        'category': 'weekly',
        'module': 'displacement',
        'mode': 'TREND',
        'backtest_wr': 47.4,
        'backtest_pnl': 47.87,
        'pnl_per_trade': 0.18,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },
    {
        'name': 'Displacement_Engine_1H',
        'timeframe': '1hr',
        'signal_func': 'displacement_engine',
        'hold_bars': 8,
        'stop_pct': 0.42,
        'target_pct': 0.80,
        'instrument': 'Weekly_DeepITM',
        'category': 'weekly',
        'module': 'displacement',
        'mode': 'TREND',
        'backtest_wr': 43.1,
        'backtest_pnl': 57.06,
        'pnl_per_trade': 0.21,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },

    # ══════════════════════════════════════════════════════════════
    # MODULE G — SWING (HTF trend follows)
    # MODE: TREND
    # ══════════════════════════════════════════════════════════════
    {
        'name': 'Daily_Trend_Follow',
        'timeframe': 'daily',
        'signal_func': 'daily_ema_trend',
        'hold_bars': 5,
        'stop_pct': 1.30,
        'target_pct': 2.40,
        'instrument': 'Monthly_DeepITM',
        'category': 'monthly',
        'module': 'swing',
        'mode': 'TREND',
        'backtest_wr': 43.7,
        'backtest_pnl': 97.32,
        'pnl_per_trade': 0.42,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },
    {
        'name': 'Weekly_Trend_Follow',
        'timeframe': 'weekly',
        'signal_func': 'weekly_ema_trend',
        'hold_bars': 2,
        'stop_pct': 2.80,
        'target_pct': 5.00,
        'instrument': 'Monthly_DeepITM',
        'category': 'monthly',
        'module': 'swing',
        'mode': 'TREND',
        'backtest_wr': 56.2,
        'backtest_pnl': 164.35,
        'pnl_per_trade': 5.14,
        'needs_ltf_choch': False,
        'needs_displacement': False,
        'needs_volume_confirm': False,
    },
]

# ============================================
# V2 FILTERS (carried forward)
# ============================================
V2_FILTERS = {
    'adx_threshold': 20,
    'htf_alignment': True,
    'confirmation_bar': True,
    'structural_stops': True,
    'cooldown_bars': {
        '1min': 10, '3min': 7, '5min': 5, '15min': 4,
        '1hr': 3, '4hr': 2, 'daily': 1, 'weekly': 1, 'monthly': 1,
    },
}

# ============================================
# V3 / MASTER SYSTEM FILTERS
# ============================================
V3_CONFIG = {
    'enabled': True,
    'scaling_enabled': True,

    # Kill zones (ET minutes from midnight)
    'kill_zones': {
        'ny_open': (570, 690),      # 9:30-11:30 AM
        'power_hour': (900, 960),   # 3:00-4:00 PM
        'dead_zone': (690, 840),    # 11:30 AM - 2:00 PM (BLOCKED for scalps/0DTE)
    },

    # Volatility regime thresholds
    'volatility': {
        'atr_expansion_threshold': 2.0,  # volatile
        'adx_trending': 30,              # trending regime
        'adx_calm': 15,                  # calm regime
        'adx_ranging': 20,               # ranging regime
        'atr_contraction': 0.8,          # ranging
    },

    # Liquidity sweep
    'sweep': {
        'equal_hl_tolerance': 0.0002,
        'lookback_bars': 10,
    },

    # Displacement
    'displacement': {
        'atr_multiplier': 1.5,
        'lookforward_bars': 3,
    },

    # Entry refinement
    'entry_refinement': {
        'ob_retrace_pct': 0.50,       # Enter at 50% of OB
        'breaker_retrace_pct': 0.30,  # Enter at 30% of breaker body
        'max_deviation_pct': 0.005,   # Max 0.5% from market price
    },

    # 2-stage exits
    'scaling': {
        'tp1_pct': 1.0,     # TP1 = 100% of target (50% position)
        'tp2_multiplier': 2.5,  # TP2 = 2.5x target (runner)
        'trail_pct_of_mfe': 0.50,  # Trail at 50% of max favorable
    },

    # RSI risk gate
    'rsi_gate': {
        'overbought': 75,
        'oversold': 25,
    },

    # Volume confirmation (breakers)
    'volume_confirm': {
        'multiplier': 1.5,  # Volume must be > 1.5x 20-bar average
        'lookback': 20,
    },

    # LTF CHoCH (HTF Director entries)
    'ltf_choch': {
        'timeframe': '5min',
        'lookforward_bars': 10,  # Check 10 bars (50 min) after HTF signal
    },

    # Strict EMA bias
    'ema_bias': {
        'ema_period': 21,
        'neutral_zone_pct': 0.001,  # Within 0.1% = neutral
    },

    # Decision tree mode thresholds
    'mode': {
        'no_trade_adx': 15,     # ADX below this + no sweep = NO_TRADE
        'reversal_adx_max': 30, # ADX below this + sweep = REVERSAL
        'trend_adx_min': 20,    # ADX above this = TREND
    },
}

# ============================================
# TIMEFRAME RESAMPLE MAP
# ============================================
TF_RESAMPLE = {
    '1min': '1min',
    '3min': '3min',
    '5min': '5min',
    '15min': '15min',
    '1hr': '1h',
    '4hr': '4h',
    'daily': '1D',
    'weekly': '1W',
}

# ============================================
# SCAN INTERVALS — How often to check each category
# ============================================
SCAN_INTERVALS = {
    'scalp': 60,     # seconds
    '0dte': 60,
    'weekly': 60,
    'monthly': 300,
}

# ============================================
# MODULE INFO (for logging / dashboard)
# ============================================
MODULES = {
    'htf_director': {
        'name': 'HTF Director',
        'description': 'Weekly/Daily/4HR OB retests with 5M CHoCH confirmation',
        'mode': 'TREND',
        'priority': 1,
    },
    'core_ob': {
        'name': 'Core OB Engine',
        'description': 'SMC Order Blocks across 1M/5M/15M/1H',
        'mode': 'TREND',
        'priority': 2,
    },
    'confluence': {
        'name': 'Confluence Engine',
        'description': 'Mitigation + ICT Reversal with displacement filter',
        'mode': 'TREND',
        'priority': 3,
    },
    'breaker': {
        'name': 'Breaker Engine',
        'description': 'Post-sweep breaker blocks with volume confirmation',
        'mode': 'REVERSAL',
        'priority': 4,
    },
    'sweep': {
        'name': 'Sweep Engine',
        'description': 'Judas swing reversals at market open after PDH/PDL sweep',
        'mode': 'REVERSAL',
        'priority': 5,
    },
    'displacement': {
        'name': 'Displacement Engine',
        'description': 'Merged BOS+Propulsion+CISD+Trap_Shift into one model',
        'mode': 'TREND',
        'priority': 6,
    },
    'swing': {
        'name': 'Swing Module',
        'description': 'Daily/Weekly trend follows for position trades',
        'mode': 'TREND',
        'priority': 7,
    },
}
