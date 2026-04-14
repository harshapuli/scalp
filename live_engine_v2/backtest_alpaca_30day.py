"""
V2 BACKTEST — 30-Day Backtest Using REAL Alpaca + Unusual Whales Data
=====================================================================

Uses:
- Alpaca paid historical bars (1-min, 5-min) for price data
- Unusual Whales API for institutional flow / dark pool / options data
- Full V2 signal pipeline with institutional + whale layer

This replaces the previous yfinance-based backtest.
"""

import sys
import json
import time
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta
from typing import Dict, List, Optional
from collections import defaultdict

# Alpaca SDK
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

# V2 modules
from config_v2 import (
    ALPACA_API_KEY, ALPACA_API_SECRET, TICKERS, RISK, STRATEGIES,
    INSTITUTIONAL_CONFIG, UW_API_KEY,
)
from unusual_whales import UnusualWhalesClient
from whale_tracker import WhaleTracker
from institutional_flow import InstitutionalFlowAnalyzer
from liquidity_map import LiquidityMapper
from institutional_signals import InstitutionalSignalGenerator
from v3_filters_institutional import v3_master_filter_institutional_logged
from v3_filters import MarketModeDetector, EMABiasFilter, VolatilityRegime
from whale_intent import (
    WhaleIntentClassifier, ExecutionPlanner, WhaleState,
    ExecutionGrade, ICTSetupType, detect_continuation_flow
)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(name)-12s | %(levelname)-5s | %(message)s'
)
logger = logging.getLogger('backtest_alpaca')


# ============================================================
# ALPACA DATA FETCHER
# ============================================================

class AlpacaDataFetcher:
    """Fetch historical bars from Alpaca paid API."""

    def __init__(self):
        self.client = StockHistoricalDataClient(
            api_key=ALPACA_API_KEY,
            secret_key=ALPACA_API_SECRET,
        )

    def get_bars(self, ticker: str, timeframe: str = '5min',
                 days: int = 30, limit: int = 50000) -> pd.DataFrame:
        """
        Fetch historical bars from Alpaca.

        Args:
            ticker: Stock symbol
            timeframe: '1min', '5min', '15min', '1hr', '4hr', 'daily'
            days: Number of days of history
            limit: Max bars to return

        Returns:
            DataFrame with open, high, low, close, volume columns
        """
        tf_map = {
            '1min': TimeFrame.Minute,
            '5min': TimeFrame(5, TimeFrame.Unit.Minute) if hasattr(TimeFrame, 'Unit') else TimeFrame.Minute,
            '15min': TimeFrame(15, TimeFrame.Unit.Minute) if hasattr(TimeFrame, 'Unit') else TimeFrame.Minute,
            '1hr': TimeFrame.Hour,
            '4hr': TimeFrame(4, TimeFrame.Unit.Hour) if hasattr(TimeFrame, 'Unit') else TimeFrame.Hour,
            'daily': TimeFrame.Day,
        }

        # Default to minute for unsupported timeframes
        alpaca_tf = tf_map.get(timeframe, TimeFrame.Minute)

        end = datetime.now()
        start = end - timedelta(days=days)

        try:
            req = StockBarsRequest(
                symbol_or_symbols=ticker,
                timeframe=alpaca_tf,
                start=start,
                end=end,
                limit=limit,
            )
            bars = self.client.get_stock_bars(req)
            df = bars.df

            if isinstance(df.index, pd.MultiIndex):
                # Drop the symbol level
                df = df.droplevel('symbol')

            # Standardize column names
            df.columns = [c.lower() for c in df.columns]

            # Ensure we have required columns
            for col in ['open', 'high', 'low', 'close', 'volume']:
                if col not in df.columns:
                    raise ValueError(f"Missing column: {col}")

            logger.info(f"Fetched {len(df)} {timeframe} bars for {ticker} ({days}d)")
            return df

        except Exception as e:
            logger.error(f"Failed to fetch {ticker} {timeframe}: {e}")
            return pd.DataFrame()

    def get_multi_timeframe(self, ticker: str, days: int = 30) -> Dict[str, pd.DataFrame]:
        """Fetch multiple timeframes for a single ticker."""
        timeframes = {
            '5min': ('5min', days),
            '1hr': ('1hr', days),
            'daily': ('daily', max(days * 2, 60)),  # More daily bars for context
        }

        data = {}
        for tf_name, (tf, d) in timeframes.items():
            df = self.get_bars(ticker, tf, d)
            if len(df) > 0:
                data[tf_name] = df
            time.sleep(0.3)  # Rate limit courtesy

        return data


# ============================================================
# BACKTEST ENGINE
# ============================================================

class AlpacaBacktester:
    """
    30-day backtest using real Alpaca + UW data.

    Simulates the V2 institutional signal pipeline:
    1. Fetch real historical bars from Alpaca
    2. Fetch real institutional flow from UW
    3. Scan for institutional signals
    4. Apply V3 institutional filters
    5. Track simulated P&L
    """

    def __init__(self, tickers: List[str] = None, days: int = 30):
        self.tickers = tickers or list(TICKERS.keys())[:7]  # Top 7 tickers
        self.days = days

        # Data source
        self.data_fetcher = AlpacaDataFetcher()

        # UW client — NOT used for signal scanning in backtest.
        # UW returns CURRENT state, not historical. OHLCV-only is correct for backtest.
        # We keep one for reference but don't pass it to scanners.
        self.uw_client = None
        if UW_API_KEY:
            self.uw_client = UnusualWhalesClient(api_key=UW_API_KEY)
            logger.info("UW client available (NOT used for scanning — OHLCV only)")

        # V2 components — NO UW client = OHLCV only = no wasted API calls
        self.signal_gen = InstitutionalSignalGenerator()  # No uw_client
        self.flow_analyzer = InstitutionalFlowAnalyzer()   # No uw_client
        self.whale_tracker = WhaleTracker()                 # No uw_client
        self.liquidity_mapper = LiquidityMapper()
        self.mode_detector = MarketModeDetector()
        self.ema_bias = EMABiasFilter()
        self.vol_regime = VolatilityRegime()

        # Results
        self.trades = []
        self.signals_generated = defaultdict(int)
        self.signals_filtered = defaultdict(int)
        self.signals_passed = defaultdict(int)

    def run(self) -> Dict:
        """Run the full 30-day backtest."""
        logger.info(f"Starting {self.days}-day backtest on {len(self.tickers)} tickers")
        logger.info(f"Tickers: {self.tickers}")
        logger.info(f"UW data: {'ENABLED' if self.uw_client else 'DISABLED'}")

        start_time = time.time()
        all_results = {}

        for ticker in self.tickers:
            logger.info(f"\n{'='*60}")
            logger.info(f"Processing {ticker}")
            logger.info(f"{'='*60}")

            try:
                result = self._backtest_ticker(ticker)
                all_results[ticker] = result
            except Exception as e:
                logger.error(f"Error backtesting {ticker}: {e}")
                all_results[ticker] = {'error': str(e)}

            # Rate limit between tickers
            time.sleep(1)

        elapsed = time.time() - start_time
        logger.info(f"\nBacktest complete in {elapsed:.1f}s")

        # Compile summary
        summary = self._compile_summary(all_results)
        summary['elapsed_seconds'] = elapsed
        summary['data_source'] = 'alpaca_paid'
        summary['uw_data'] = bool(self.uw_client)

        return summary

    def _backtest_ticker(self, ticker: str) -> Dict:
        """Backtest a single ticker using walk-forward simulation."""

        # Fetch real historical data
        data = self.data_fetcher.get_multi_timeframe(ticker, self.days)

        if '5min' not in data or len(data['5min']) < 100:
            logger.warning(f"Insufficient data for {ticker}")
            return {'error': 'insufficient_data'}

        df_5min = data['5min']
        df_1hr = data.get('1hr', pd.DataFrame())
        df_daily = data.get('daily', pd.DataFrame())

        logger.info(f"{ticker}: {len(df_5min)} 5min bars, "
                   f"{len(df_1hr)} 1hr bars, {len(df_daily)} daily bars")

        # Set ticker for UW data
        self.whale_tracker.set_ticker(ticker)
        self.flow_analyzer.set_ticker(ticker)

        ticker_trades = []
        ticker_signals = []

        # ── Optimized Multi-Expiry Walk-Forward ──────────────────────
        # Single scan pass at moderate interval (every 10 bars = ~50min).
        # Each signal is then assigned to BEST matching expiry type.
        # This is 10x faster than scanning 4 separate passes.
        daily_trade_counts = defaultdict(lambda: defaultdict(int))
        scan_interval = 10  # Single pass every ~50 min

        for i in range(200, len(df_5min), scan_interval):
            window = df_5min.iloc[max(0, i-200):i].copy()
            if len(window) < 50:
                continue

            # Get institutional state (computed once per window)
            try:
                inst_state = self._get_institutional_state(ticker, window)
            except Exception as e:
                logger.debug(f"Inst state error at bar {i}: {e}")
                continue

            # Scan for signals (once per window)
            try:
                signals = self.signal_gen.scan_all_signals(window, ticker, '5min')
            except Exception as e:
                logger.debug(f"Signal scan error at bar {i}: {e}")
                signals = []

            for signal in signals:
                self.signals_generated[signal.signal_type] += 1

                # Classify into best expiry type based on signal + conviction
                best_expiry = self._classify_expiry(signal)
                expiry_cfg = self.EXPIRY_TYPES[best_expiry]

                # Check daily trade cap
                bar_time = window.iloc[-1].get('timestamp', None)
                trade_date = bar_time.date() if bar_time is not None and hasattr(bar_time, 'date') else f"day_{i // 78}"
                daily_cap = max(1, int(expiry_cfg['target_per_day'] * 3))
                if daily_trade_counts[trade_date][best_expiry] >= daily_cap:
                    continue

                ticker_signals.append({
                    'bar_idx': i,
                    'signal_type': signal.signal_type,
                    'direction': signal.direction,
                    'conviction': signal.conviction,
                    'entry_price': signal.entry_price,
                    'stop_loss': signal.stop_loss,
                    'tp1': signal.tp1,
                    'institutional_score': signal.institutional_score,
                    'expiry_type': best_expiry,
                })

                # Apply institutional filters
                filter_result = self._apply_filters(signal, window, inst_state)

                # ── Directional Conflict Filter (weekly/monthly) ─────────
                # CRITICAL: Don't buy CALL during DISTRIBUTION or PUT during
                # ACCUMULATION for longer-dated expiries. These are fighting
                # the whales and the wider TP/max_hold just amplifies losses.
                whale_state = filter_result.get('whale_state', 'NEUTRAL')
                if best_expiry in ('weekly', 'monthly'):
                    if whale_state == 'DISTRIBUTION' and signal.direction == 'CALL':
                        filter_result['passed'] = False
                        filter_result['reasons'].append(
                            f"BLOCKED: CALL + DISTRIBUTION = directional conflict for {best_expiry}")
                    elif whale_state == 'ACCUMULATION' and signal.direction == 'PUT':
                        filter_result['passed'] = False
                        filter_result['reasons'].append(
                            f"BLOCKED: PUT + ACCUMULATION = directional conflict for {best_expiry}")

                # Whale state preference bonus/penalty
                preferred_states = self.EXPIRY_WHALE_PREFERENCE.get(best_expiry, [])
                if preferred_states and whale_state in preferred_states:
                    filter_result['size_modifier'] *= 1.15
                    filter_result['reasons'].append(
                        f"{whale_state} preferred for {best_expiry}")
                elif preferred_states and whale_state not in preferred_states:
                    filter_result['size_modifier'] *= 0.85
                    filter_result['reasons'].append(
                        f"{whale_state} non-preferred for {best_expiry}")

                # Tag expiry type into filter result
                filter_result['expiry_type'] = best_expiry
                filter_result['expiry_label'] = expiry_cfg['label']
                filter_result['tp_mult'] = expiry_cfg['tp_mult']
                filter_result['sl_mult'] = expiry_cfg['sl_mult']
                filter_result['max_hold'] = expiry_cfg['max_hold']

                if filter_result['passed']:
                    self.signals_passed[signal.signal_type] += 1
                    trade = self._simulate_trade(
                        signal, window, df_5min, i, filter_result
                    )
                    if trade:
                        ticker_trades.append(trade)
                        self.trades.append(trade)
                        daily_trade_counts[trade_date][best_expiry] += 1
                else:
                    self.signals_filtered[signal.signal_type] += 1

        # Ticker summary
        result = {
            'ticker': ticker,
            'bars_analyzed': len(df_5min),
            'signals_total': len(ticker_signals),
            'trades_taken': len(ticker_trades),
            'signals': ticker_signals,
            'trades': ticker_trades,
        }

        if ticker_trades:
            pnls = [t['pnl_pct'] for t in ticker_trades]
            result['total_pnl_pct'] = sum(pnls)
            result['avg_pnl_pct'] = np.mean(pnls)
            result['win_rate'] = len([p for p in pnls if p > 0]) / len(pnls) * 100
            result['max_win'] = max(pnls)
            result['max_loss'] = min(pnls)
            result['profit_factor'] = (
                sum(p for p in pnls if p > 0) / abs(sum(p for p in pnls if p < 0))
                if any(p < 0 for p in pnls) else float('inf')
            )

        logger.info(f"{ticker}: {len(ticker_signals)} signals → {len(ticker_trades)} trades")
        return result

    def _classify_expiry(self, signal) -> str:
        """Classify a signal into the best expiry type.

        NO SCALPS — only 0dte, weekly, monthly.
        Rules:
        - iceberg_fade → 0dte (fast mean-reversion, same-day resolution)
        - inst_sweep_reversal → 0dte (needs same-day resolution)
        - smart_money_div with conviction >= 80 → weekly (high confidence divergence)
        - smart_money_div with conviction < 80 → 0dte (quick divergence fade)
        - whale_accumulation_entry → monthly (slow accumulation thesis)
        - whale_exhaustion_fade → 0dte (exhaustion plays resolve fast)
        - inst_ob → 0dte (order block fill and bounce)
        - Default → 0dte
        """
        sig_type = signal.signal_type
        conv = signal.conviction

        if sig_type == 'iceberg_fade':
            return '0dte'
        elif sig_type == 'inst_sweep_reversal':
            return '0dte'
        elif sig_type == 'smart_money_div':
            if conv >= 80:
                return 'weekly'  # High conviction → let it cook
            elif conv >= 65:
                return '0dte'
            else:
                return '0dte'  # Was scalp, now 0dte
        elif sig_type == 'whale_accumulation_entry':
            return 'monthly' if conv >= 80 else 'weekly'
        elif sig_type == 'whale_exhaustion_fade':
            return '0dte'
        elif sig_type == 'inst_ob':
            return '0dte'
        else:
            return '0dte'

    def _get_institutional_state(self, ticker: str, df: pd.DataFrame) -> Dict:
        """Build institutional state for filter context."""
        try:
            flow_score = self.flow_analyzer.get_institutional_score(df)
        except:
            flow_score = type('obj', (object,), {'score': 50, 'bias': None})()

        try:
            whale_signal = self.whale_tracker.get_whale_signal(df)
        except:
            whale_signal = type('obj', (object,), {
                'direction': 'NEUTRAL', 'confidence': 0, 'momentum': None,
                'volume_profile': None, 'distribution_signal': None,
            })()

        try:
            liquidity = self.liquidity_mapper.get_liquidity_state(df)
        except:
            liquidity = None

        try:
            wyckoff = self.flow_analyzer.wyckoff_phase_detection(df)
        except:
            wyckoff = None

        # Classify whale intent (V3 upgrade)
        # In backtest: use OHLCV-only classification (UW data is current, not historical)
        try:
            ohlcv_tracker = WhaleTracker()  # OHLCV only
            ohlcv_tracker.get_whale_signal(df)
            classifier = WhaleIntentClassifier()  # OHLCV only
            whale_intent = classifier.classify(ticker, whale_tracker=ohlcv_tracker)
        except:
            whale_intent = None

        return {
            'flow_score': flow_score.score if hasattr(flow_score, 'score') else 50,
            'whale_direction': getattr(whale_signal, 'direction', 'NEUTRAL'),
            'whale_confidence': getattr(whale_signal, 'confidence', 0),
            'whale_momentum': getattr(whale_signal, 'momentum', None),
            'volume_profile': getattr(whale_signal, 'volume_profile', None),
            'distribution_signal': getattr(whale_signal, 'distribution_signal', None),
            'accumulation_zones': self.whale_tracker._last_accumulation_zones,
            'iceberg_orders': [],
            'liquidity_state': liquidity,
            'wyckoff_phase': wyckoff,
            'whale_intent': whale_intent,
            '_whale_tracker': self.whale_tracker,
        }

    # ── Signal Tier Definitions ──────────────────────────────────────
    # T1 = proven edge signals (smart_money_div, iceberg_fade)
    # T2 = structural signals (SMC_OB, mitigation, continuation)
    # T3 = conditional signals (inst_sweep_reversal — sniper only)
    SIGNAL_TIERS = {
        'smart_money_div':    {'tier': 1, 'size_mult': 1.3, 'label': 'T1-proven'},
        'iceberg_fade':       {'tier': 1, 'size_mult': 1.2, 'label': 'T1-proven'},
        'inst_ob':            {'tier': 2, 'size_mult': 1.0, 'label': 'T2-structural'},
        'mitigation_block':   {'tier': 2, 'size_mult': 1.0, 'label': 'T2-structural'},
        'continuation_flow':  {'tier': 2, 'size_mult': 1.0, 'label': 'T2-structural'},
        'inst_sweep_reversal':{'tier': 3, 'size_mult': 0.6, 'label': 'T3-sniper'},
        'whale_accumulation_entry': {'tier': 2, 'size_mult': 1.0, 'label': 'T2-structural'},
        'whale_exhaustion_fade':    {'tier': 2, 'size_mult': 0.8, 'label': 'T2-structural'},
    }

    # ── Per-State Confidence Thresholds ───────────────────────────────
    # Loosened from V3.1 to allow more trades through (block only extreme cases)
    STATE_CONFIDENCE_THRESHOLDS = {
        'ACCUMULATION': {'block': 20, 'reduce': 45, 'reduce_mult': 0.7},
        'DISTRIBUTION': {'block': 15, 'reduce': 35, 'reduce_mult': 0.6},
        'AGGRESSION':   {'block': 10, 'reduce': 30, 'reduce_mult': 0.75},
        'NEUTRAL':      {'block': 15, 'reduce': 40, 'reduce_mult': 0.65},
    }

    # ── Multi-Expiry Trade Types ──────────────────────────────────────
    # Each expiry type has its own scan interval, TP/SL multipliers, and max hold
    EXPIRY_TYPES = {
        'scalp': {
            'scan_interval': 5,     # Every 25 min (5 × 5min bars)
            'tp_mult': 1.0,         # Standard TP for 20% targets
            'sl_mult': 1.0,         # Standard SL
            'max_hold': 20,         # ~1.5 hours
            'min_conviction': 70,   # Lower bar — volume game
            'target_per_day': 2,
            'label': 'SCALP',
        },
        '0dte': {
            'scan_interval': 10,    # Every 50 min
            'tp_mult': 2.2,         # Aggressive TP for 80% targets
            'sl_mult': 1.5,         # Aggressive SL room to survive options delta swings
            'max_hold': 15,         # ~1.25 hours (was 40 — way too long for 0DTE)
            'min_conviction': 80,   # Higher bar for 0DTE
            'target_per_day': 2,
            'label': '0DTE',
        },
        'weekly': {
            'scan_interval': 50,    # Every ~4 hours
            'tp_mult': 2.0,         # 2.0x TP for 50% weekly target
            'sl_mult': 1.5,         # 1.5x SL to survive weekly choppiness
            'max_hold': 200,        # ~2.5 days
            'min_conviction': 70,   # Lowered from 75 — need more weekly trades
            'target_per_day': 0.5,  # ~2-3 per week
            'label': 'WEEKLY',
        },
        'monthly': {
            'scan_interval': 200,   # ~Daily scan
            'tp_mult': 3.0,         # 3x TP target
            'sl_mult': 1.5,         # Wide SL but not crazy
            'max_hold': 500,        # ~6 days
            'min_conviction': 65,   # Lowered from 85 — whale_accum never fires otherwise
            'target_per_day': 0.1,  # ~2 per month
            'label': 'MONTHLY',
        },
    }

    # Which signals are allowed for each expiry type
    EXPIRY_SIGNAL_MAP = {
        'scalp':   ['iceberg_fade', 'smart_money_div', 'inst_ob'],
        '0dte':    ['smart_money_div', 'iceberg_fade', 'inst_ob', 'inst_sweep_reversal'],
        'weekly':  ['smart_money_div', 'iceberg_fade', 'whale_accumulation_entry', 'whale_exhaustion_fade'],
        'monthly': ['smart_money_div', 'whale_accumulation_entry'],
    }

    # Which whale states are preferred for each expiry
    EXPIRY_WHALE_PREFERENCE = {
        'scalp':   ['AGGRESSION'],                          # Fast moves
        '0dte':    ['AGGRESSION', 'DISTRIBUTION'],           # Directional conviction
        'weekly':  ['ACCUMULATION', 'DISTRIBUTION'],         # Time for thesis
        'monthly': ['ACCUMULATION'],                         # Big moves building
    }

    def _apply_filters(self, signal, df, inst_state) -> Dict:
        """Apply whale intent-based filters to a signal.

        V3 UPGRADE: Uses 3-state whale intent model instead of binary confirm/deny.
        Maps (ICT setup type × whale state) → execution plan with grade, entry mode,
        size multiplier, TP mode, and stop mode.

        V3.1 UPGRADE: Signal tier weighting + per-state confidence thresholds.
        - T1 signals (smart_money_div, iceberg_fade) get increased size
        - T3 signals (inst_sweep_reversal) get restricted size
        - Each whale state has its own confidence block/reduce thresholds
        """
        passed = True
        reasons = []
        size_modifier = 1.0
        execution_grade = 'A'
        whale_state = 'NEUTRAL'
        entry_mode = 'confirmation_candle'
        tp_mode = 'standard_scalp'
        stop_mode = 'standard'

        # ── Filter 1: Signal Tier Weighting ───────────────────────────
        tier_info = self.SIGNAL_TIERS.get(signal.signal_type,
                                          {'tier': 2, 'size_mult': 1.0, 'label': 'T2-default'})
        if tier_info['tier'] == 1:
            passed = False
            reasons.append(f"BLOCKED: Tier 1 signals disabled for expanded targets")
        size_modifier *= tier_info['size_mult']
        reasons.append(f"{tier_info['label']} size={tier_info['size_mult']:.1f}x")

        # ── Filter 2: Institutional flow score ────────────────────────
        # OHLCV-only flow scores are lower (dark_pool + block_trade = 0)
        flow_score = inst_state.get('flow_score', 50)
        if flow_score < 20:
            passed = False
            reasons.append(f"flow_score={flow_score:.0f} < 20")
        elif flow_score < 40:
            size_modifier *= 0.8
            reasons.append(f"flow_score={flow_score:.0f} warning zone")

        # ── Filter 3: Whale Intent Execution (V3) ────────────────────
        # OHLCV-only whale tracker for point-in-time classification.
        try:
            ohlcv_tracker = WhaleTracker()  # No UW client = OHLCV only
            ohlcv_tracker.get_whale_signal(df)  # Prime it with current window data
            classifier = WhaleIntentClassifier()  # No UW client
            whale_intent = classifier.classify(
                signal.ticker,
                whale_tracker=ohlcv_tracker,
            )

            planner = ExecutionPlanner()
            setup_type = planner.classify_ict_setup(signal.signal_type)
            plan = planner.get_plan(setup_type, whale_intent, signal.direction)

            whale_state = whale_intent.state.value
            execution_grade = plan.grade.value
            entry_mode = plan.entry_mode
            size_modifier *= plan.size_multiplier
            tp_mode = plan.tp_mode
            stop_mode = plan.stop_mode

            if not plan.should_trade:
                passed = False
                reasons.append(f"whale_intent: {plan.reason}")
            else:
                if execution_grade != 'A+':
                    passed = False
                    reasons.append(f"BLOCKED: execution_grade {execution_grade} != A+")
                reasons.append(f"whale_{whale_state}_{execution_grade}: {plan.reason}")

            # ── Filter 4: Per-State Confidence Gate ───────────────────
            conf = whale_intent.confidence
            thresholds = self.STATE_CONFIDENCE_THRESHOLDS.get(
                whale_state, self.STATE_CONFIDENCE_THRESHOLDS['NEUTRAL'])

            if conf < thresholds['block']:
                passed = False
                reasons.append(
                    f"confidence={conf:.0f}% < {thresholds['block']}% block for {whale_state}")
            elif conf < thresholds['reduce']:
                size_modifier *= thresholds['reduce_mult']
                reasons.append(
                    f"confidence={conf:.0f}% < {thresholds['reduce']}% — reduced size {thresholds['reduce_mult']:.1f}x for {whale_state}")

        except Exception as e:
            # Fallback: pass with standard size
            reasons.append(f"whale_intent_error: {e}")

        # ── Filter 5: Signal-specific adjustments ─────────────────────
        # inst_sweep_reversal: ONLY allow during AGGRESSION or DISTRIBUTION
        if signal.signal_type == 'inst_sweep_reversal':
            if whale_state == 'ACCUMULATION':
                passed = False
                reasons.append(f"BLOCKED: sweep_reversal during ACCUMULATION (chop zone)")
            elif whale_state == 'NEUTRAL':
                passed = False
                reasons.append(f"BLOCKED: sweep_reversal during NEUTRAL (no conviction)")
            elif signal.conviction < 85:
                size_modifier *= 0.5
                reasons.append(f"sweep conviction {signal.conviction} < 85 — half size")

        # iceberg_fade: BLOCK during AGGRESSION (13% WR = fighting momentum)
        if signal.signal_type == 'iceberg_fade':
            if whale_state == 'AGGRESSION':
                passed = False
                reasons.append(f"BLOCKED: iceberg_fade during AGGRESSION (fighting momentum)")
            elif whale_state == 'NEUTRAL':
                size_modifier *= 0.7
                reasons.append(f"iceberg_fade NEUTRAL — reduced size 0.7x")

        return {
            'passed': passed,
            'reasons': reasons,
            'size_modifier': size_modifier,
            'execution_grade': execution_grade,
            'whale_state': whale_state,
            'entry_mode': entry_mode,
            'tp_mode': tp_mode,
            'stop_mode': stop_mode,
        }

    def _simulate_trade(self, signal, window, full_df, start_idx,
                        filter_result) -> Optional[Dict]:
        """Simulate a trade forward from signal bar.

        Multi-Expiry Trade Management:
        - TP and SL are scaled by expiry-specific multipliers
        - Max hold time varies by expiry (0DTE=15, weekly=200, monthly=500)
        - Trailing stop: move SL to breakeven after 50% of TP reached
        - Time decay: tighten TP after 60% of max_hold bars elapsed
        """
        entry_price = signal.entry_price
        stop_loss = signal.stop_loss
        tp1 = signal.tp1

        if not all([entry_price, stop_loss, tp1]):
            return None

        # ── Apply expiry-specific TP/SL multipliers ───────────────────
        tp_mult = filter_result.get('tp_mult', 1.0)
        sl_mult = filter_result.get('sl_mult', 1.0)
        max_bars = filter_result.get('max_hold', 100)

        is_long = signal.direction in ('CALL', 'LONG')

        # Scale TP distance from entry
        tp_dist = abs(tp1 - entry_price)
        if is_long:
            adj_tp1 = entry_price + tp_dist * tp_mult
        else:
            adj_tp1 = entry_price - tp_dist * tp_mult

        # Scale SL distance from entry
        sl_dist = abs(stop_loss - entry_price)
        if is_long:
            adj_sl = entry_price - sl_dist * sl_mult
        else:
            adj_sl = entry_price + sl_dist * sl_mult

        end_idx = min(start_idx + max_bars, len(full_df))

        # ── Trailing stop state ──
        best_price = entry_price  # Track best favorable price
        breakeven_activated = False
        half_tp_dist = tp_dist * tp_mult * 0.5  # 50% of TP distance
        time_decay_bar = int(max_bars * 0.6)     # After 60% of time, tighten

        for i in range(start_idx + 1, end_idx):
            bar = full_df.iloc[i]
            bars_elapsed = i - start_idx

            # ── Update trailing stop ──
            if is_long:
                if bar['high'] > best_price:
                    best_price = bar['high']
                # Move to breakeven once 50% of TP reached
                if not breakeven_activated and (best_price - entry_price) >= half_tp_dist:
                    adj_sl = entry_price  # Breakeven
                    breakeven_activated = True
                # Trail stop behind best price (keep 40% of gains)
                if breakeven_activated:
                    trail_sl = best_price - (best_price - entry_price) * 0.4
                    adj_sl = max(adj_sl, trail_sl)
            else:
                if bar['low'] < best_price:
                    best_price = bar['low']
                if not breakeven_activated and (entry_price - best_price) >= half_tp_dist:
                    adj_sl = entry_price
                    breakeven_activated = True
                if breakeven_activated:
                    trail_sl = best_price + (entry_price - best_price) * 0.4
                    adj_sl = min(adj_sl, trail_sl)

            # ── Time decay: tighten TP after 60% of hold time ──
            if bars_elapsed >= time_decay_bar:
                decay_factor = 1.0 - 0.3 * ((bars_elapsed - time_decay_bar) /
                                              max(1, max_bars - time_decay_bar))
                decay_factor = max(0.5, decay_factor)
                if is_long:
                    adj_tp1_decayed = entry_price + tp_dist * tp_mult * decay_factor
                else:
                    adj_tp1_decayed = entry_price - tp_dist * tp_mult * decay_factor
            else:
                adj_tp1_decayed = adj_tp1

            # ── Check SL / TP ──
            if is_long:
                if bar['low'] <= adj_sl:
                    pnl_pct = ((adj_sl - entry_price) / entry_price) * 100
                    exit_reason = 'trailing_stop' if breakeven_activated else 'stop_loss'
                    return self._make_trade(signal, entry_price, adj_sl,
                                          pnl_pct, exit_reason, bars_elapsed,
                                          filter_result)
                if bar['high'] >= adj_tp1_decayed:
                    pnl_pct = ((adj_tp1_decayed - entry_price) / entry_price) * 100
                    return self._make_trade(signal, entry_price, adj_tp1_decayed,
                                          pnl_pct, 'tp1', bars_elapsed,
                                          filter_result)
            else:
                if bar['high'] >= adj_sl:
                    pnl_pct = ((entry_price - adj_sl) / entry_price) * 100
                    exit_reason = 'trailing_stop' if breakeven_activated else 'stop_loss'
                    return self._make_trade(signal, entry_price, adj_sl,
                                          pnl_pct, exit_reason, bars_elapsed,
                                          filter_result)
                if bar['low'] <= adj_tp1_decayed:
                    pnl_pct = ((entry_price - adj_tp1_decayed) / entry_price) * 100
                    return self._make_trade(signal, entry_price, adj_tp1_decayed,
                                          pnl_pct, 'tp1', bars_elapsed,
                                          filter_result)

        # Timeout — close at last price
        exit_price = full_df.iloc[min(end_idx - 1, len(full_df) - 1)]['close']
        if is_long:
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
        else:
            pnl_pct = ((entry_price - exit_price) / entry_price) * 100

        return self._make_trade(signal, entry_price, exit_price,
                              pnl_pct, 'timeout', max_bars, filter_result)

    def _make_trade(self, signal, entry, exit_price, pnl_pct, exit_reason,
                    bars_held, filter_result) -> Dict:
        tier_info = self.SIGNAL_TIERS.get(signal.signal_type,
                                          {'tier': 2, 'size_mult': 1.0, 'label': 'T2-default'})
        size_adj_pnl = pnl_pct * filter_result['size_modifier']
        return {
            'signal_type': signal.signal_type,
            'direction': signal.direction,
            'ticker': signal.ticker,
            'entry_price': entry,
            'exit_price': exit_price,
            'pnl_pct': pnl_pct,
            'size_adj_pnl': size_adj_pnl,
            'exit_reason': exit_reason,
            'bars_held': bars_held,
            'conviction': signal.conviction,
            'institutional_score': signal.institutional_score,
            'size_modifier': filter_result['size_modifier'],
            'signal_tier': tier_info['tier'],
            'signal_tier_label': tier_info['label'],
            'filter_reasons': filter_result['reasons'],
            # V3 Whale Intent fields
            'execution_grade': filter_result.get('execution_grade', 'A'),
            'whale_state': filter_result.get('whale_state', 'NEUTRAL'),
            'entry_mode': filter_result.get('entry_mode', 'confirmation_candle'),
            'tp_mode': filter_result.get('tp_mode', 'standard_scalp'),
            'stop_mode': filter_result.get('stop_mode', 'standard'),
            # Multi-expiry fields
            'expiry_type': filter_result.get('expiry_type', 'scalp'),
            'expiry_label': filter_result.get('expiry_label', 'SCALP'),
        }

    def _compile_summary(self, all_results: Dict) -> Dict:
        """Compile overall backtest summary."""
        total_trades = len(self.trades)
        if total_trades == 0:
            return {
                'total_trades': 0,
                'tickers': list(all_results.keys()),
                'signals_generated': dict(self.signals_generated),
                'signals_filtered': dict(self.signals_filtered),
                'signals_passed': dict(self.signals_passed),
                'per_ticker': all_results,
            }

        pnls = [t['pnl_pct'] for t in self.trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        summary = {
            'total_trades': total_trades,
            'win_rate': len(wins) / total_trades * 100,
            'total_pnl_pct': sum(pnls),
            'avg_pnl_pct': np.mean(pnls),
            'max_win_pct': max(pnls) if pnls else 0,
            'max_loss_pct': min(pnls) if pnls else 0,
            'avg_win_pct': np.mean(wins) if wins else 0,
            'avg_loss_pct': np.mean(losses) if losses else 0,
            'profit_factor': sum(wins) / abs(sum(losses)) if losses else float('inf'),
            'avg_bars_held': np.mean([t['bars_held'] for t in self.trades]),
            'avg_conviction': np.mean([t['conviction'] for t in self.trades]),
            'avg_inst_score': np.mean([t['institutional_score'] for t in self.trades]),

            # Signal breakdown
            'signals_generated': dict(self.signals_generated),
            'signals_filtered': dict(self.signals_filtered),
            'signals_passed': dict(self.signals_passed),

            # Exit reasons
            'exit_reasons': dict(pd.Series([t['exit_reason'] for t in self.trades]).value_counts()),

            # Per signal type performance
            'per_signal_type': {},

            # Per ticker
            'per_ticker': all_results,

            # Trades
            'trades': self.trades,
        }

        # Per signal type breakdown
        for sig_type in set(t['signal_type'] for t in self.trades):
            sig_trades = [t for t in self.trades if t['signal_type'] == sig_type]
            sig_pnls = [t['pnl_pct'] for t in sig_trades]
            sig_wins = [p for p in sig_pnls if p > 0]

            summary['per_signal_type'][sig_type] = {
                'trades': len(sig_trades),
                'win_rate': len(sig_wins) / len(sig_trades) * 100 if sig_trades else 0,
                'total_pnl': sum(sig_pnls),
                'avg_pnl': np.mean(sig_pnls),
            }

        # ── V3: Per whale state performance breakdown ──
        summary['per_whale_state'] = {}
        for ws in set(t.get('whale_state', 'NEUTRAL') for t in self.trades):
            ws_trades = [t for t in self.trades if t.get('whale_state', 'NEUTRAL') == ws]
            ws_pnls = [t['pnl_pct'] for t in ws_trades]
            ws_wins = [p for p in ws_pnls if p > 0]
            ws_losses = [p for p in ws_pnls if p < 0]

            summary['per_whale_state'][ws] = {
                'trades': len(ws_trades),
                'win_rate': len(ws_wins) / len(ws_trades) * 100 if ws_trades else 0,
                'total_pnl': sum(ws_pnls),
                'avg_pnl': np.mean(ws_pnls),
                'profit_factor': sum(ws_wins) / abs(sum(ws_losses)) if ws_losses else float('inf'),
            }

        # ── V3: Per execution grade performance breakdown ──
        summary['per_execution_grade'] = {}
        for eg in set(t.get('execution_grade', 'A') for t in self.trades):
            eg_trades = [t for t in self.trades if t.get('execution_grade', 'A') == eg]
            eg_pnls = [t['pnl_pct'] for t in eg_trades]
            eg_wins = [p for p in eg_pnls if p > 0]
            eg_losses = [p for p in eg_pnls if p < 0]

            summary['per_execution_grade'][eg] = {
                'trades': len(eg_trades),
                'win_rate': len(eg_wins) / len(eg_trades) * 100 if eg_trades else 0,
                'total_pnl': sum(eg_pnls),
                'avg_pnl': np.mean(eg_pnls),
                'profit_factor': sum(eg_wins) / abs(sum(eg_losses)) if eg_losses else float('inf'),
            }

        # ── V3.2: Per expiry type performance breakdown ──
        summary['per_expiry_type'] = {}
        for et in set(t.get('expiry_type', 'scalp') for t in self.trades):
            et_trades = [t for t in self.trades if t.get('expiry_type', 'scalp') == et]
            et_pnls = [t['pnl_pct'] for t in et_trades]
            et_adj = [t.get('size_adj_pnl', t['pnl_pct']) for t in et_trades]
            et_wins = [p for p in et_pnls if p > 0]
            et_losses = [p for p in et_pnls if p < 0]

            label = et_trades[0].get('expiry_label', et.upper()) if et_trades else et.upper()
            summary['per_expiry_type'][et] = {
                'label': label,
                'trades': len(et_trades),
                'win_rate': len(et_wins) / len(et_trades) * 100 if et_trades else 0,
                'total_pnl': sum(et_pnls),
                'size_adj_pnl': sum(et_adj),
                'avg_pnl': np.mean(et_pnls),
                'profit_factor': sum(et_wins) / abs(sum(et_losses)) if et_losses else float('inf'),
                'avg_bars_held': np.mean([t['bars_held'] for t in et_trades]),
            }

        # ── V3.1: Per signal tier performance breakdown ──
        summary['per_signal_tier'] = {}
        for tier in set(t.get('signal_tier', 2) for t in self.trades):
            tier_trades = [t for t in self.trades if t.get('signal_tier', 2) == tier]
            tier_pnls = [t['pnl_pct'] for t in tier_trades]
            tier_adj_pnls = [t.get('size_adj_pnl', t['pnl_pct']) for t in tier_trades]
            tier_wins = [p for p in tier_pnls if p > 0]
            tier_losses = [p for p in tier_pnls if p < 0]
            tier_adj_wins = [p for p in tier_adj_pnls if p > 0]
            tier_adj_losses = [p for p in tier_adj_pnls if p < 0]

            label = f"T{tier}"
            for t in tier_trades:
                label = t.get('signal_tier_label', label)
                break

            summary['per_signal_tier'][f"T{tier}"] = {
                'label': label,
                'trades': len(tier_trades),
                'win_rate': len(tier_wins) / len(tier_trades) * 100 if tier_trades else 0,
                'total_pnl': sum(tier_pnls),
                'size_adj_pnl': sum(tier_adj_pnls),
                'profit_factor': sum(tier_wins) / abs(sum(tier_losses)) if tier_losses else float('inf'),
                'adj_profit_factor': sum(tier_adj_wins) / abs(sum(tier_adj_losses)) if tier_adj_losses else float('inf'),
            }

        # ── V3.1: Size-adjusted totals ──
        adj_pnls = [t.get('size_adj_pnl', t['pnl_pct']) for t in self.trades]
        adj_wins = [p for p in adj_pnls if p > 0]
        adj_losses = [p for p in adj_pnls if p < 0]
        summary['size_adj_total_pnl'] = sum(adj_pnls)
        summary['size_adj_profit_factor'] = sum(adj_wins) / abs(sum(adj_losses)) if adj_losses else float('inf')

        return summary


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description='V2 Backtest with Alpaca + UW')
    parser.add_argument('--days', type=int, default=30, help='Days of history')
    parser.add_argument('--tickers', type=str, default=None,
                       help='Comma-separated tickers (default: config)')
    parser.add_argument('--output', type=str, default='backtest_alpaca_results.json',
                       help='Output JSON file')
    args = parser.parse_args()

    tickers = args.tickers.split(',') if args.tickers else None

    print("\n" + "=" * 80)
    print("V2 BACKTEST — REAL ALPACA + UNUSUAL WHALES DATA")
    print("=" * 80)
    print(f"Days: {args.days}")
    print(f"Tickers: {tickers or 'config default'}")
    print(f"UW API: {'ENABLED' if UW_API_KEY else 'DISABLED'}")
    print(f"Alpaca: PAID ({ALPACA_API_KEY[:8]}...)")
    print("=" * 80 + "\n")

    bt = AlpacaBacktester(tickers=tickers, days=args.days)
    results = bt.run()

    # Save results
    # Convert non-serializable values
    def clean_for_json(obj):
        if isinstance(obj, (np.integer,)):
            return int(obj)
        if isinstance(obj, (np.floating,)):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        if isinstance(obj, float) and (np.isinf(obj) or np.isnan(obj)):
            return str(obj)
        return obj

    import json

    class NumpyEncoder(json.JSONEncoder):
        def default(self, obj):
            result = clean_for_json(obj)
            if result is not obj:
                return result
            return super().default(obj)

    with open(args.output, 'w') as f:
        json.dump(results, f, indent=2, cls=NumpyEncoder)

    print(f"\nResults saved to {args.output}")

    # Print summary
    print("\n" + "=" * 80)
    print("BACKTEST RESULTS SUMMARY")
    print("=" * 80)
    print(f"Total trades: {results.get('total_trades', 0)}")
    if results.get('total_trades', 0) > 0:
        print(f"Win rate: {results['win_rate']:.1f}%")
        print(f"Total P&L: {results['total_pnl_pct']:.2f}%")
        print(f"Avg P&L per trade: {results['avg_pnl_pct']:.3f}%")
        print(f"Profit factor: {results['profit_factor']}")
        print(f"Max win: {results['max_win_pct']:.2f}%")
        print(f"Max loss: {results['max_loss_pct']:.2f}%")
        print(f"Avg bars held: {results['avg_bars_held']:.0f}")
        print(f"Avg conviction: {results['avg_conviction']:.0f}")
        print(f"Avg inst score: {results['avg_inst_score']:.0f}")

        print(f"\nSignals generated: {dict(results['signals_generated'])}")
        print(f"Signals passed: {dict(results['signals_passed'])}")
        print(f"Signals filtered: {dict(results['signals_filtered'])}")

        print(f"\nPer signal type:")
        for sig, data in results.get('per_signal_type', {}).items():
            print(f"  {sig}: {data['trades']} trades, "
                  f"WR={data['win_rate']:.0f}%, P&L={data['total_pnl']:.2f}%")

        # V3: Whale Intent Performance
        if results.get('per_whale_state'):
            print(f"\n{'─' * 60}")
            print("WHALE STATE PERFORMANCE:")
            print(f"{'─' * 60}")
            for ws, data in sorted(results['per_whale_state'].items()):
                pf = data['profit_factor']
                pf_str = f"{pf:.2f}" if pf != float('inf') else "INF"
                print(f"  {ws:15s}: {data['trades']:3d} trades | "
                      f"WR={data['win_rate']:5.1f}% | P&L={data['total_pnl']:+6.2f}% | "
                      f"PF={pf_str}")

        if results.get('per_execution_grade'):
            print(f"\nEXECUTION GRADE PERFORMANCE:")
            print(f"{'─' * 60}")
            for eg, data in sorted(results['per_execution_grade'].items()):
                pf = data['profit_factor']
                pf_str = f"{pf:.2f}" if pf != float('inf') else "INF"
                print(f"  Grade {eg:3s}: {data['trades']:3d} trades | "
                      f"WR={data['win_rate']:5.1f}% | P&L={data['total_pnl']:+6.2f}% | "
                      f"PF={pf_str}")

        # V3.2: Expiry Type Performance
        if results.get('per_expiry_type'):
            print(f"\nEXPIRY TYPE PERFORMANCE:")
            print(f"{'─' * 60}")
            for et_key in ['scalp', '0dte', 'weekly', 'monthly']:
                data = results['per_expiry_type'].get(et_key)
                if not data:
                    continue
                pf = data['profit_factor']
                pf_str = f"{pf:.2f}" if pf != float('inf') else "INF"
                print(f"  {data['label']:8s}: {data['trades']:3d} trades | "
                      f"WR={data['win_rate']:5.1f}% | P&L={data['total_pnl']:+6.2f}% | "
                      f"PF={pf_str} | Avg hold={data['avg_bars_held']:.0f} bars")

        # V3.1: Signal Tier Performance
        if results.get('per_signal_tier'):
            print(f"\nSIGNAL TIER PERFORMANCE:")
            print(f"{'─' * 60}")
            for tier_key, data in sorted(results['per_signal_tier'].items()):
                pf = data['profit_factor']
                pf_str = f"{pf:.2f}" if pf != float('inf') else "INF"
                apf = data.get('adj_profit_factor', pf)
                apf_str = f"{apf:.2f}" if apf != float('inf') else "INF"
                print(f"  {data['label']:16s}: {data['trades']:3d} trades | "
                      f"WR={data['win_rate']:5.1f}% | P&L={data['total_pnl']:+6.2f}% | "
                      f"PF={pf_str} | SizeAdj={data.get('size_adj_pnl', 0):+6.2f}% APF={apf_str}")

        # V3.1: Size-Adjusted Totals
        if 'size_adj_total_pnl' in results:
            print(f"\n{'─' * 60}")
            print(f"SIZE-ADJUSTED TOTALS (reflecting tier weighting + confidence gates):")
            print(f"  Raw P&L:          {results['total_pnl_pct']:+.2f}%")
            print(f"  Size-Adj P&L:     {results['size_adj_total_pnl']:+.2f}%")
            apf = results.get('size_adj_profit_factor', 0)
            apf_str = f"{apf:.2f}" if apf != float('inf') else "INF"
            print(f"  Size-Adj PF:      {apf_str}")

    print("=" * 80)
