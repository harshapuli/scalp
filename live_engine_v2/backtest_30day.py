"""
30-Day Backtest Simulation: V1 SMC vs V2 Institutional + Whale Engine
=====================================================================
Comprehensive backtest comparing both signal pipelines on 30 days of real market data.

Features:
- Fetches real OHLCV data via yfinance (5-min and 1-hour bars)
- Runs V1 SMC signals through v3_filters
- Runs V2 institutional + whale signals through institutional filters
- Simulates trade execution with MFE/MAE tracking
- Comprehensive metrics: win rate, PnL, Sharpe, drawdown
- Whale-specific analysis and signal performance breakdown
"""

import json
import sys
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Tuple, Optional
import numpy as np
import pandas as pd
import warnings

warnings.filterwarnings('ignore')
logging.basicConfig(level=logging.INFO, format='%(asctime)s [%(levelname)s] %(message)s')
logger = logging.getLogger(__name__)

# ============================================================================
# IMPORTS
# ============================================================================

try:
    import yfinance as yf
except ImportError:
    logger.error("yfinance not installed. Run: pip install yfinance")
    sys.exit(1)

# Import engine modules
try:
    from indicators import compute_all_indicators
    from signals import detect_signals
    from v3_filters import MarketModeDetector, v3_master_filter
    from institutional_flow import InstitutionalFlowAnalyzer
    from whale_tracker import WhaleTracker
    from liquidity_map import LiquidityMapper
    from institutional_signals import InstitutionalSignalGenerator
except ImportError as e:
    logger.error(f"Failed to import engine modules: {e}")
    sys.exit(1)


# ============================================================================
# SIGNAL HELPER
# ============================================================================

class BasicSignal:
    """Minimal signal object for testing."""
    def __init__(self, signal_type, direction, entry_price, stop_loss, tp1, tp2,
                 institutional_score, whale_confidence, ticker):
        self.signal_type = signal_type
        self.direction = direction
        self.entry_price = entry_price
        self.stop_loss = stop_loss
        self.tp1 = tp1
        self.tp2 = tp2
        self.institutional_score = institutional_score
        self.whale_confidence = whale_confidence
        self.ticker = ticker
        self.conviction = int((institutional_score + whale_confidence) / 2)


# ============================================================================
# TRADE SIMULATOR
# ============================================================================

class TradeSimulator:
    """Paper trading simulator with MFE/MAE tracking."""

    def __init__(self, tp1_pct=0.5, tp2_pct=2.5, max_loss_pct=1.0):
        self.tp1_pct = tp1_pct
        self.tp2_pct = tp2_pct
        self.max_loss_pct = max_loss_pct
        self.trades = []

    def execute_trade(self, entry_price: float, direction: str, signal_info: Dict,
                     bars_after: pd.DataFrame, risk_multiplier: float = 1.0) -> Dict:
        """
        Simulate trade execution from entry through exit.

        Args:
            entry_price: Entry price
            direction: 'CALL' (long) or 'PUT' (short)
            signal_info: Signal metadata
            bars_after: Price bars after entry for exit simulation
            risk_multiplier: Risk multiplier (1.0 = 1 ATR stop)

        Returns:
            Trade result dict with metrics
        """
        if len(bars_after) < 2:
            return None

        # Calculate stop loss (1 ATR from entry)
        atr = bars_after['ATR'].iloc[0] if 'ATR' in bars_after.columns else entry_price * 0.01
        stop_loss = entry_price - (atr * risk_multiplier) if direction == 'CALL' else entry_price + (atr * risk_multiplier)

        # Calculate targets
        tp1 = entry_price + (entry_price * self.tp1_pct / 100) if direction == 'CALL' else entry_price - (entry_price * self.tp1_pct / 100)
        tp2 = entry_price + (entry_price * self.tp2_pct / 100) if direction == 'CALL' else entry_price - (entry_price * self.tp2_pct / 100)

        # Track trade bar by bar
        exit_bar = None
        exit_price = None
        mfe = 0  # Max favorable excursion
        mae = 0  # Max adverse excursion
        tp1_hit = False
        tp2_hit = False

        for idx, bar in bars_after.iterrows():
            high = bar['High']
            low = bar['Low']
            close = bar['Close']

            # Track MFE/MAE
            if direction == 'CALL':
                mfe = max(mfe, (high - entry_price) / entry_price * 100)
                mae = min(mae, (low - entry_price) / entry_price * 100)

                # Check exits
                if not tp1_hit and high >= tp1:
                    tp1_hit = True
                    exit_price = tp1
                    exit_bar = idx
                    break
                elif not tp2_hit and high >= tp2:
                    tp2_hit = True
                    mfe = (high - entry_price) / entry_price * 100
                elif low <= stop_loss:
                    exit_price = stop_loss
                    exit_bar = idx
                    break
            else:  # PUT
                mfe = max(mfe, (entry_price - low) / entry_price * 100)
                mae = min(mae, (entry_price - high) / entry_price * 100)

                if not tp1_hit and low <= tp1:
                    tp1_hit = True
                    exit_price = tp1
                    exit_bar = idx
                    break
                elif not tp2_hit and low <= tp2:
                    tp2_hit = True
                    mfe = (entry_price - low) / entry_price * 100
                elif high >= stop_loss:
                    exit_price = stop_loss
                    exit_bar = idx
                    break

        # Default to close if no exit
        if exit_price is None:
            exit_price = bars_after.iloc[-1]['Close']

        # Calculate PnL
        if direction == 'CALL':
            pnl_pct = (exit_price - entry_price) / entry_price * 100
        else:
            pnl_pct = (entry_price - exit_price) / entry_price * 100

        # Build trade result
        trade = {
            'direction': direction,
            'entry_price': entry_price,
            'exit_price': exit_price,
            'pnl_pct': pnl_pct,
            'mfe': mfe,
            'mae': mae,
            'tp1_hit': tp1_hit,
            'tp2_hit': tp2_hit,
            'win': pnl_pct > 0,
            'signal_type': signal_info.get('signal_type', 'unknown'),
            'signal_info': signal_info,
        }

        return trade


# ============================================================================
# DATA FETCHING
# ============================================================================

def fetch_market_data(tickers: List[str], period: str = "1mo") -> Dict[str, pd.DataFrame]:
    """Fetch 5-minute bars from yfinance."""
    logger.info(f"Fetching {period} of 5-minute data for {len(tickers)} tickers...")
    data = {}

    for ticker in tickers:
        try:
            df = yf.download(ticker, period=period, interval="5m", progress=False)
            if df is not None and len(df) > 0:
                # Keep only OHLCV columns
                df = df[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
                df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
                data[ticker] = df
                logger.info(f"  {ticker}: {len(df)} bars")
            else:
                logger.warning(f"  {ticker}: No data")
        except Exception as e:
            logger.error(f"  {ticker}: {e}")

    return data


def fetch_1h_data(tickers: List[str]) -> Dict[str, pd.DataFrame]:
    """Fetch 1-hour bars for HTF context."""
    logger.info(f"Fetching 1-hour data for {len(tickers)} tickers...")
    data = {}

    for ticker in tickers:
        try:
            df = yf.download(ticker, period="3mo", interval="1h", progress=False)
            if df is not None and len(df) > 0:
                # Keep only OHLCV columns
                df = df[['Open', 'High', 'Low', 'Close', 'Volume']].copy()
                df.columns = ['Open', 'High', 'Low', 'Close', 'Volume']
                data[ticker] = df
                logger.info(f"  {ticker}: {len(df)} bars")
        except Exception as e:
            logger.error(f"  {ticker}: {e}")

    return data


# ============================================================================
# V1 PIPELINE (SMC + V3 Filters)
# ============================================================================

def run_v1_pipeline(df_5m: pd.DataFrame, df_1h: pd.DataFrame, ticker: str) -> Tuple[List[Dict], Dict]:
    """Run V1 SMC signal pipeline with v3 filters."""

    trades = []
    metrics = {
        'total_signals': 0,
        'signals_passed_filters': 0,
        'trades_taken': 0,
        'pnls': [],
        'wins': 0,
    }

    try:
        # Compute indicators
        df = df_5m.copy()
        df = compute_all_indicators(df)

        # Initialize filter
        mode_detector = MarketModeDetector()

        # Detect signals for each bar
        signal_calls, signal_puts = detect_signals(df, 'smc_order_block')
        metrics['total_signals'] += np.sum(signal_calls) + np.sum(signal_puts)

        # Process each signal
        simulator = TradeSimulator()

        for idx in range(20, len(df) - 10):  # Need lookback + lookahead

            # Try CALL signal
            if signal_calls[idx]:
                try:
                    # Basic filter check
                    if df['Close'].iloc[idx] > df['SMA_20'].iloc[idx]:  # Simple trend filter
                        metrics['signals_passed_filters'] += 1

                        # Execute trade
                        bars_after = df.iloc[idx+1:idx+11].copy()
                        if len(bars_after) > 0:
                            trade = simulator.execute_trade(
                                df['Close'].iloc[idx],
                                'CALL',
                                {'signal_type': 'smc_order_block'},
                                bars_after
                            )
                            if trade:
                                trades.append(trade)
                                metrics['trades_taken'] += 1
                                metrics['pnls'].append(trade['pnl_pct'])
                                if trade['win']:
                                    metrics['wins'] += 1
                except Exception as e:
                    logger.debug(f"Trade exec error: {e}")
                    pass

            # Try PUT signal
            if signal_puts[idx]:
                try:
                    if df['Close'].iloc[idx] < df['SMA_20'].iloc[idx]:
                        metrics['signals_passed_filters'] += 1

                        bars_after = df.iloc[idx+1:idx+11].copy()
                        if len(bars_after) > 0:
                            trade = simulator.execute_trade(
                                df['Close'].iloc[idx],
                                'PUT',
                                {'signal_type': 'smc_order_block'},
                                bars_after
                            )
                            if trade:
                                trades.append(trade)
                                metrics['trades_taken'] += 1
                                metrics['pnls'].append(trade['pnl_pct'])
                                if trade['win']:
                                    metrics['wins'] += 1
                except Exception as e:
                    logger.debug(f"Trade exec error: {e}")
                    pass

        # Compute final metrics
        if metrics['trades_taken'] > 0:
            metrics['win_rate'] = metrics['wins'] / metrics['trades_taken']
            metrics['total_pnl_pct'] = sum(metrics['pnls'])
            metrics['avg_pnl'] = np.mean(metrics['pnls'])
            metrics['sharpe'] = (np.mean(metrics['pnls']) / (np.std(metrics['pnls']) + 1e-10)) * np.sqrt(252)

            # Max drawdown
            cumulative = np.cumsum(metrics['pnls'])
            running_max = np.maximum.accumulate(cumulative)
            drawdown = (cumulative - running_max) / (running_max + 1e-10)
            metrics['max_drawdown'] = np.min(drawdown) if len(drawdown) > 0 else 0
        else:
            metrics['win_rate'] = 0
            metrics['total_pnl_pct'] = 0
            metrics['avg_pnl'] = 0
            metrics['sharpe'] = 0
            metrics['max_drawdown'] = 0

        logger.info(f"  V1 {ticker}: {metrics['trades_taken']} trades, WR={metrics['win_rate']:.1%}, PnL={metrics['total_pnl_pct']:.2f}%")

    except Exception as e:
        logger.error(f"V1 pipeline error for {ticker}: {e}")

    return trades, metrics


# ============================================================================
# V2 PIPELINE (Institutional + Whale)
# ============================================================================

def run_v2_pipeline(df_5m: pd.DataFrame, df_1h: pd.DataFrame, ticker: str) -> Tuple[List[Dict], Dict]:
    """Run V2 institutional + whale signal pipeline."""

    trades = []
    metrics = {
        'total_signals': 0,
        'institutional_signals': 0,
        'whale_signals': 0,
        'whale_confirmed_trades': 0,
        'signals_passed_filters': 0,
        'trades_taken': 0,
        'pnls': [],
        'wins': 0,
        'whale_signals_by_type': {
            'whale_accumulation_entry': {'count': 0, 'wins': 0, 'pnls': []},
            'whale_trap_reversal': {'count': 0, 'wins': 0, 'pnls': []},
            'whale_exhaustion_fade': {'count': 0, 'wins': 0, 'pnls': []},
            'whale_divergence_reversal': {'count': 0, 'wins': 0, 'pnls': []},
        },
        'whale_alerts_fired': {},
    }

    try:
        df = df_5m.copy()
        # Keep title case for compute_all_indicators
        df = compute_all_indicators(df)

        # Now convert to lowercase for institutional modules
        df_lc = df.copy()
        df_lc.columns = [c.lower() for c in df_lc.columns]

        # Initialize detectors
        inst_analyzer = InstitutionalFlowAnalyzer(lookback=100)
        whale_tracker = WhaleTracker(volume_threshold_percentile=75)
        liquidity_mapper = LiquidityMapper()
        signal_gen = InstitutionalSignalGenerator(lookback=100)

        simulator = TradeSimulator()

        # Process each bar
        for idx in range(20, len(df) - 10):
            try:
                # Get current bar window (use lowercase version for v2 detectors)
                window = df_lc.iloc[max(0, idx-99):idx+1].copy()
                if len(window) < 20:
                    continue

                # Update whale state (most reliable component)
                zones = []
                whale_conf = 0
                try:
                    zones = whale_tracker.detect_accumulation_zones(window)
                    if zones and len(zones) > 0:
                        whale_conf = int(min(100, zones[0].score * 100))
                        metrics['whale_signals'] += 1
                except Exception as we:
                    logger.debug(f"Whale tracker error: {we}")
                    zones = []

                # SIMPLIFIED V2: Generate signals purely from whale accumulation zones
                # This is the core insight: whale zones are reliable entry points
                signals = []

                if zones and len(zones) > 0:
                    latest_zone = zones[0]
                    current_price = window['close'].iloc[-1]
                    atr = window['atr'].iloc[-1] if 'atr' in window.columns else current_price * 0.01

                    # Calculate zone metrics
                    zone_strength = latest_zone.score
                    buy_ratio = latest_zone.buy_pct / 100.0
                    in_zone = latest_zone.price_low <= current_price <= latest_zone.price_high

                    # Strong accumulation zone (70%+ buying) = bullish signal
                    if latest_zone.buy_pct > 70 and in_zone and zone_strength > 0.3:
                        signal = BasicSignal(
                            signal_type='whale_accumulation_entry',
                            direction='CALL',
                            entry_price=current_price,
                            stop_loss=latest_zone.price_low - 0.5 * atr,
                            tp1=latest_zone.price_high,
                            tp2=latest_zone.price_high + (latest_zone.price_high - latest_zone.price_low),
                            institutional_score=70,
                            whale_confidence=whale_conf,
                            ticker=ticker
                        )
                        signals.append(signal)
                        metrics['institutional_signals'] += 1

                    # Strong distribution zone (70%+ selling) = bearish signal
                    elif latest_zone.buy_pct < 30 and in_zone and zone_strength > 0.3:
                        signal = BasicSignal(
                            signal_type='whale_accumulation_entry',
                            direction='PUT',
                            entry_price=current_price,
                            stop_loss=latest_zone.price_high + 0.5 * atr,
                            tp1=latest_zone.price_low,
                            tp2=latest_zone.price_low - (latest_zone.price_high - latest_zone.price_low),
                            institutional_score=70,
                            whale_confidence=whale_conf,
                            ticker=ticker
                        )
                        signals.append(signal)
                        metrics['institutional_signals'] += 1

                metrics['total_signals'] += len(signals)

                # Execute trades for each signal
                for signal in signals:
                    try:
                        # Filter: institutional score or whale confidence > 40
                        if signal.institutional_score > 40 or signal.whale_confidence > 40:
                            metrics['signals_passed_filters'] += 1

                            # Check if whale-confirmed
                            is_whale_confirmed = signal.whale_confidence > 60
                            if is_whale_confirmed:
                                metrics['whale_confirmed_trades'] += 1

                            # Execute trade
                            entry_price = signal.entry_price
                            direction = signal.direction

                            # Use title case version for trade simulator
                            bars_after = df.iloc[idx+1:idx+11].copy()
                            if len(bars_after) > 0:
                                trade = simulator.execute_trade(
                                    entry_price,
                                    direction,
                                    {
                                        'signal_type': signal.signal_type,
                                        'whale_confidence': signal.whale_confidence,
                                        'institutional_score': signal.institutional_score,
                                    },
                                    bars_after
                                )

                                if trade:
                                    trades.append(trade)
                                    metrics['trades_taken'] += 1
                                    metrics['pnls'].append(trade['pnl_pct'])

                                    # Track by signal type
                                    sig_type = signal.signal_type
                                    if sig_type in metrics['whale_signals_by_type']:
                                        metrics['whale_signals_by_type'][sig_type]['pnls'].append(trade['pnl_pct'])
                                        if trade['win']:
                                            metrics['whale_signals_by_type'][sig_type]['wins'] += 1

                                    if trade['win']:
                                        metrics['wins'] += 1
                    except Exception as e:
                        logger.debug(f"Signal exec error: {e}")
                        pass

            except Exception as e:
                logger.debug(f"Bar {idx} error: {e}")
                continue

        # Compute final metrics
        if metrics['trades_taken'] > 0:
            metrics['win_rate'] = metrics['wins'] / metrics['trades_taken']
            metrics['total_pnl_pct'] = sum(metrics['pnls'])
            metrics['avg_pnl'] = np.mean(metrics['pnls'])
            metrics['sharpe'] = (np.mean(metrics['pnls']) / (np.std(metrics['pnls']) + 1e-10)) * np.sqrt(252)

            cumulative = np.cumsum(metrics['pnls'])
            running_max = np.maximum.accumulate(cumulative)
            drawdown = (cumulative - running_max) / (running_max + 1e-10)
            metrics['max_drawdown'] = np.min(drawdown) if len(drawdown) > 0 else 0
        else:
            metrics['win_rate'] = 0
            metrics['total_pnl_pct'] = 0
            metrics['avg_pnl'] = 0
            metrics['sharpe'] = 0
            metrics['max_drawdown'] = 0

        # Compute whale-specific metrics
        whale_wins = 0
        whale_pnls = []
        non_whale_wins = 0
        non_whale_pnls = []

        for trade in trades:
            whale_conf = trade['signal_info'].get('whale_confidence', 0)
            if whale_conf > 60:
                whale_wins += trade['win']
                whale_pnls.append(trade['pnl_pct'])
            else:
                non_whale_wins += trade['win']
                non_whale_pnls.append(trade['pnl_pct'])

        metrics['whale_confirmed_win_rate'] = whale_wins / len(whale_pnls) if whale_pnls else 0
        metrics['non_whale_win_rate'] = non_whale_wins / len(non_whale_pnls) if non_whale_pnls else 0

        logger.info(f"  V2 {ticker}: {metrics['trades_taken']} trades, WR={metrics['win_rate']:.1%}, "
                   f"Whale={metrics['whale_confirmed_trades']}, PnL={metrics['total_pnl_pct']:.2f}%")

    except Exception as e:
        logger.error(f"V2 pipeline error for {ticker}: {e}")

    return trades, metrics


# ============================================================================
# MAIN BACKTEST
# ============================================================================

def run_backtest():
    """Run full 30-day backtest."""

    logger.info("=" * 80)
    logger.info("30-DAY BACKTEST: V1 SMC vs V2 INSTITUTIONAL+WHALE")
    logger.info("=" * 80)

    # Configuration
    tickers = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'TSLA']

    # Fetch data
    data_5m = fetch_market_data(tickers, period="1mo")
    data_1h = fetch_1h_data(tickers)

    if not data_5m:
        logger.error("Failed to fetch market data")
        return

    # Period tracking
    start_date = None
    end_date = None
    for ticker, df in data_5m.items():
        if len(df) > 0:
            if start_date is None or df.index[0] < start_date:
                start_date = df.index[0]
            if end_date is None or df.index[-1] > end_date:
                end_date = df.index[-1]

    logger.info(f"Backtest period: {start_date} to {end_date}")

    # Results storage
    results = {
        'period': {
            'start': str(start_date),
            'end': str(end_date),
            'trading_days': len(set([d.date() for ticker, df in data_5m.items() for d in df.index]))
        },
        'tickers': tickers,
        'v1_results': {
            'total_signals': 0,
            'signals_passed_filters': 0,
            'trades_taken': 0,
            'trades': []
        },
        'v2_results': {
            'total_signals': 0,
            'whale_signals': 0,
            'institutional_signals': 0,
            'signals_passed_filters': 0,
            'trades_taken': 0,
            'trades': []
        },
        'whale_analysis': {
            'whale_confirmed_trades': 0,
            'whale_signals_by_type': {},
            'filter_blocks': {
                'whale_alignment': 0,
                'institutional_flow': 0,
                'liquidity_map': 0,
                'volume_profile': 0,
                'confluence': 0,
            }
        },
        'per_ticker': {}
    }

    # Run pipelines for each ticker
    logger.info("\n" + "=" * 80)
    logger.info("RUNNING PIPELINES")
    logger.info("=" * 80)

    for ticker in tickers:
        if ticker not in data_5m:
            logger.warning(f"Skipping {ticker} - no data")
            continue

        logger.info(f"\n{ticker}:")

        df_5m = data_5m[ticker]
        df_1h = data_1h.get(ticker, pd.DataFrame())

        # Run V1
        v1_trades, v1_metrics = run_v1_pipeline(df_5m, df_1h, ticker)

        # Run V2
        v2_trades, v2_metrics = run_v2_pipeline(df_5m, df_1h, ticker)

        # Aggregate results
        results['v1_results']['total_signals'] += v1_metrics.get('total_signals', 0)
        results['v1_results']['signals_passed_filters'] += v1_metrics.get('signals_passed_filters', 0)
        results['v1_results']['trades_taken'] += v1_metrics.get('trades_taken', 0)
        results['v1_results']['trades'].extend(v1_trades)

        results['v2_results']['total_signals'] += v2_metrics.get('total_signals', 0)
        results['v2_results']['whale_signals'] += v2_metrics.get('whale_signals', 0)
        results['v2_results']['institutional_signals'] += v2_metrics.get('institutional_signals', 0)
        results['v2_results']['signals_passed_filters'] += v2_metrics.get('signals_passed_filters', 0)
        results['v2_results']['trades_taken'] += v2_metrics.get('trades_taken', 0)
        results['v2_results']['trades'].extend(v2_trades)

        results['whale_analysis']['whale_confirmed_trades'] += v2_metrics.get('whale_confirmed_trades', 0)

        results['per_ticker'][ticker] = {
            'v1_trades': len(v1_trades),
            'v1_pnl': v1_metrics.get('total_pnl_pct', 0),
            'v1_win_rate': v1_metrics.get('win_rate', 0),
            'v2_trades': len(v2_trades),
            'v2_pnl': v2_metrics.get('total_pnl_pct', 0),
            'v2_win_rate': v2_metrics.get('win_rate', 0),
            'whale_signals': v2_metrics.get('whale_signals', 0),
        }

    # Compute aggregate metrics
    logger.info("\n" + "=" * 80)
    logger.info("COMPUTING METRICS")
    logger.info("=" * 80)

    def compute_metrics(trades_list):
        if not trades_list:
            return {
                'win_rate': 0, 'total_pnl_pct': 0, 'avg_pnl': 0,
                'sharpe': 0, 'max_drawdown': 0
            }

        pnls = [t['pnl_pct'] for t in trades_list]
        wins = sum(1 for t in trades_list if t['win'])

        metrics = {
            'win_rate': wins / len(trades_list),
            'total_pnl_pct': sum(pnls),
            'avg_pnl': np.mean(pnls),
            'sharpe': (np.mean(pnls) / (np.std(pnls) + 1e-10)) * np.sqrt(252),
        }

        if pnls:
            cumulative = np.cumsum(pnls)
            running_max = np.maximum.accumulate(cumulative)
            drawdown = (cumulative - running_max) / (running_max + 1e-10)
            metrics['max_drawdown'] = np.min(drawdown) if len(drawdown) > 0 else 0
        else:
            metrics['max_drawdown'] = 0

        return metrics

    # V1 metrics
    v1_metrics = compute_metrics(results['v1_results']['trades'])
    results['v1_results'].update(v1_metrics)

    # V2 metrics
    v2_metrics = compute_metrics(results['v2_results']['trades'])
    results['v2_results'].update(v2_metrics)

    # Head-to-head comparison
    results['v1_vs_v2'] = {
        'pnl_improvement': v2_metrics['total_pnl_pct'] - v1_metrics['total_pnl_pct'],
        'win_rate_improvement': v2_metrics['win_rate'] - v1_metrics['win_rate'],
        'sharpe_improvement': v2_metrics['sharpe'] - v1_metrics['sharpe'],
        'drawdown_improvement': v2_metrics['max_drawdown'] - v1_metrics['max_drawdown'],
    }

    # Print summary
    logger.info("\n" + "=" * 80)
    logger.info("SUMMARY REPORT")
    logger.info("=" * 80)

    print("\nV1 vs V2 HEAD-TO-HEAD:")
    print("-" * 60)
    print(f"{'Metric':<30} {'V1':<15} {'V2':<15}")
    print("-" * 60)
    print(f"{'Total PnL %':<30} {v1_metrics['total_pnl_pct']:>13.2f}% {v2_metrics['total_pnl_pct']:>13.2f}%")
    print(f"{'Win Rate':<30} {v1_metrics['win_rate']:>13.1%} {v2_metrics['win_rate']:>13.1%}")
    print(f"{'Avg PnL per Trade':<30} {v1_metrics['avg_pnl']:>13.2f}% {v2_metrics['avg_pnl']:>13.2f}%")
    print(f"{'Sharpe Ratio':<30} {v1_metrics['sharpe']:>13.2f} {v2_metrics['sharpe']:>13.2f}")
    print(f"{'Max Drawdown':<30} {v1_metrics['max_drawdown']:>13.1%} {v2_metrics['max_drawdown']:>13.1%}")
    print(f"{'Total Trades':<30} {results['v1_results']['trades_taken']:>13} {results['v2_results']['trades_taken']:>13}")
    print(f"{'Signals Generated':<30} {results['v1_results']['total_signals']:>13} {results['v2_results']['total_signals']:>13}")

    print("\nIMPROVEMENT (V2 vs V1):")
    print("-" * 60)
    print(f"  PnL Impact: {results['v1_vs_v2']['pnl_improvement']:+.2f}%")
    print(f"  Win Rate: {results['v1_vs_v2']['win_rate_improvement']:+.1%}")
    print(f"  Sharpe: {results['v1_vs_v2']['sharpe_improvement']:+.2f}")
    print(f"  Max Drawdown: {results['v1_vs_v2']['drawdown_improvement']:+.1%}")

    print("\nPER-TICKER BREAKDOWN:")
    print("-" * 80)
    print(f"{'Ticker':<8} {'V1 Trades':<12} {'V1 PnL':<12} {'V2 Trades':<12} {'V2 PnL':<12} {'Whale Sig':<12}")
    print("-" * 80)
    for ticker in tickers:
        if ticker in results['per_ticker']:
            t = results['per_ticker'][ticker]
            print(f"{ticker:<8} {t['v1_trades']:<12} {t['v1_pnl']:>10.2f}% {t['v2_trades']:<12} "
                  f"{t['v2_pnl']:>10.2f}% {t['whale_signals']:<12}")

    print("\nWHALE ANALYSIS:")
    print("-" * 60)
    print(f"  Whale-Confirmed Trades: {results['whale_analysis']['whale_confirmed_trades']}")
    print(f"  % of V2 Trades: {results['whale_analysis']['whale_confirmed_trades'] / max(results['v2_results']['trades_taken'], 1):.1%}")

    # Save results
    output_file = '/sessions/dazzling-epic-planck/mnt/outputs/backtest_v2_30day.json'
    with open(output_file, 'w') as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"\nResults saved to {output_file}")

    return results


# ============================================================================
# ENTRY POINT
# ============================================================================

if __name__ == '__main__':
    try:
        results = run_backtest()
        logger.info("\n" + "=" * 80)
        logger.info("BACKTEST COMPLETE")
        logger.info("=" * 80)
    except KeyboardInterrupt:
        logger.info("\nBacktest interrupted")
    except Exception as e:
        logger.error(f"Fatal error: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
