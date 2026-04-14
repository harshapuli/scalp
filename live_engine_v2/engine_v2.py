"""
INSTITUTIONAL TRADING ENGINE V2 — TWO-LAYER SIGNAL PIPELINE
============================================================
This extends the V1 engine with institutional intelligence.

V1 Pipeline (unchanged):
  Data Feed → Indicators → SMC Signals → V3 Filters → Execution

V2 Pipeline (new two-layer approach):
  Data Feed → Indicators → [SMC Signals + Institutional Signals]
                               ↓
                      V3 Filters + Institutional Filters
                               ↓
                          Execution

The institutional layer includes:
  - Institutional flow analysis (large order tracking)
  - Whale position tracking
  - Liquidity mapping (pools, voids, sweeps)
  - Volume profile + Wyckoff phase detection
  - Enhanced risk management with institutional context

Usage:
  python engine_v2.py              # Paper trading, hybrid mode (default)
  python engine_v2.py --live       # Live trading
  python engine_v2.py --mode institutional_only  # Institutional signals only
  python engine_v2.py --mode hybrid --tickers BTC,ETH  # Selective tickers
"""
import sys
import time
import json
import logging
import argparse
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from collections import defaultdict

from config_v2 import (
    ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_BASE_URL, ALPACA_DATA_URL,
    TICKERS, RISK, STRATEGIES, V2_FILTERS, V3_CONFIG, SCAN_INTERVALS, MODULES,
    INSTITUTIONAL_CONFIG,
)
from execution import AlpacaClient, PositionManager
from data_feed import LiveDataFeed
from signals import detect_signals, apply_v2_filters
from v3_filters import (
    MarketModeDetector, EMABiasFilter, VolatilityRegime, ScalingManager,
    detect_liquidity_levels, v3_master_filter,
)
from trade_logger import TradeLogger, v3_master_filter_logged
from trade_analyzer import TradeAnalyzer
from institutional_flow import InstitutionalFlowAnalyzer
from whale_tracker import WhaleTracker
from liquidity_map import LiquidityMapper
from institutional_signals import InstitutionalSignalGenerator
from v3_filters_institutional import v3_master_filter_institutional_logged
from unusual_whales import UnusualWhalesClient
from whale_intent import WhaleIntentClassifier, ExecutionPlanner

try:
    from config_v2 import UW_API_KEY
except ImportError:
    UW_API_KEY = ""


# ============================================================
# LOGGING
# ============================================================

def setup_logging(level='INFO'):
    fmt = '%(asctime)s | %(name)-12s | %(levelname)-5s | %(message)s'
    logging.basicConfig(level=level, format=fmt)
    fh = logging.FileHandler('trading_engine_v2.log')
    fh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(fh)

logger = logging.getLogger('engine_v2')


# ============================================================
# INSTITUTIONAL TRADING ENGINE — TWO-LAYER PIPELINE
# ============================================================

class InstitutionalTradingEngine:
    """
    Enhanced trading engine with institutional intelligence.
    Maintains full V1 capability while adding institutional layer.
    """

    def __init__(self, paper: bool = True, strategies: list = None, mode: str = 'hybrid'):
        self.paper = paper
        self.strategies = strategies or STRATEGIES
        self.mode = mode  # 'hybrid' or 'institutional_only'

        if mode not in ('hybrid', 'institutional_only'):
            raise ValueError(f"Invalid mode: {mode}. Must be 'hybrid' or 'institutional_only'.")

        # Alpaca client
        self.client = AlpacaClient(
            api_key=ALPACA_API_KEY,
            api_secret=ALPACA_API_SECRET,
            base_url=ALPACA_BASE_URL,
            data_url=ALPACA_DATA_URL,
        )

        # Position manager
        self.pos_mgr = PositionManager(self.client, RISK)

        # ══════ V1 SYSTEM COMPONENTS (unchanged) ══════
        self.mode_detector = MarketModeDetector()      # Decision tree
        self.ema_bias = EMABiasFilter()                 # Strict 1H EMA bias
        self.vol_regime = VolatilityRegime()             # ATR + ADX regime
        self.scaling_mgr = ScalingManager()              # 2-stage exits

        # ══════ V2 INSTITUTIONAL COMPONENTS ══════
        # Initialize Unusual Whales API client (PAID subscription)
        self.uw_client = None
        if UW_API_KEY:
            self.uw_client = UnusualWhalesClient(api_key=UW_API_KEY)
            logger.info("Unusual Whales API client initialized (PAID data)")
        else:
            logger.warning("No UW_API_KEY — whale detection will use OHLCV fallback")

        self.institutional_flow = InstitutionalFlowAnalyzer(uw_client=self.uw_client)
        self.whale_tracker = WhaleTracker(uw_client=self.uw_client)
        self.liquidity_mapper = LiquidityMapper()
        self.inst_signal_gen = InstitutionalSignalGenerator(uw_client=self.uw_client)
        self.whale_intent_classifier = WhaleIntentClassifier(uw_client=self.uw_client)
        self.execution_planner = ExecutionPlanner()
        self.institutional_state = {}  # per-ticker cache
        self.latest_signals = []       # store recent signals for dashboard

        # ══════ TRADE LOGGING & ANALYSIS ══════
        self.trade_logger = TradeLogger(log_dir='trade_logs_v2')
        self.trade_analyzer = TradeAnalyzer()
        self._prev_mode = 'TREND'
        self._prev_bias = 'NEUTRAL'

        # Determine needed timeframes
        self.needed_tfs = set(s['timeframe'] for s in self.strategies)
        self.needed_tfs.update(['1min', '5min', '1hr', '4hr', 'daily'])

        # Data feed (WebSocket primary, REST fallback)
        self.data_feed = LiveDataFeed(
            client=self.client,
            tickers=list(TICKERS.keys()),
            needed_timeframes=self.needed_tfs,
        )

        # Register for bar callbacks → event-driven scanning
        self.data_feed.register_bar_callback(self._on_new_bar)

        # Queue of tickers that got new bars
        import queue
        self._scan_queue: queue.Queue = queue.Queue()

        # Per-category scan timing
        self.last_scan: Dict[str, float] = {}
        self.last_institutional_scan: Dict[str, float] = {}

        # Stats
        self.stats = defaultdict(int)
        self.inst_stats = defaultdict(int)

        logger.info(f"INSTITUTIONAL ENGINE V2 initialized | Paper={paper} | "
                    f"Mode={mode} | "
                    f"Strategies={len(self.strategies)} | "
                    f"Tickers={len(TICKERS)} | TFs={self.needed_tfs}")

    # ── Startup ──

    def start(self):
        """Start the engine."""
        logger.info("=" * 80)
        logger.info("INSTITUTIONAL TRADING ENGINE V2 — STARTING")
        logger.info(f"Mode: {'PAPER' if self.paper else '*** LIVE ***'} | Pipeline: {self.mode.upper()}")
        logger.info(f"Strategies: {len(self.strategies)} across modules")
        logger.info(f"Institutional layer: {'ENABLED' if self.mode in ('hybrid', 'institutional_only') else 'DISABLED'}")
        logger.info(f"Tickers: {list(TICKERS.keys())}")
        logger.info("=" * 80)

        # Market check
        try:
            clock = self.client.get_clock()
            is_open = clock.get('is_open', False)
            logger.info(f"Market is {'OPEN' if is_open else 'CLOSED'}")
            if not is_open:
                logger.info(f"Next open: {clock.get('next_open', 'unknown')}")
        except Exception as e:
            logger.warning(f"Clock check failed: {e}")

        # Warm up data
        self.data_feed.warmup()

        # Account
        try:
            equity = self.client.get_equity()
            logger.info(f"Account equity: ${equity:,.2f}")
            RISK['account_size'] = equity
        except:
            logger.warning("Using default account size")

        self._run_loop()

    # ── Bar Callback ──

    def _on_new_bar(self, ticker: str, bar: dict):
        """Called by LiveDataFeed when a new 1min bar arrives via WebSocket."""
        self._scan_queue.put(ticker)

    # ── Main Loop ──

    def _run_loop(self):
        """
        Event-driven loop with institutional state updates.
        - When WebSocket streams, bar callbacks push tickers to queue
        - Each new bar triggers context build + SMC + institutional scan
        - Position management runs every tick
        - Institutional dashboard logged every N seconds
        """
        import queue
        logger.info("Entering main loop (event-driven + REST fallback) — Ctrl+C to stop")
        last_status_log = 0
        last_inst_dashboard = 0
        last_rest_poll = 0
        current_prices = {}

        try:
            while True:
                loop_start = time.time()

                # Market hours check
                try:
                    if not self.client.is_market_open():
                        logger.info("Market closed. Waiting 60s...")
                        time.sleep(60)
                        continue
                except:
                    pass

                # ══════ POSITION MANAGEMENT (every tick) ══════
                current_prices = self.data_feed.get_current_prices()

                if V3_CONFIG.get('scaling_enabled', True):
                    for trade_id, trade in list(self.pos_mgr.open_trades.items()):
                        price = current_prices.get(trade.ticker)
                        if price is None:
                            continue

                        # Log position tick
                        pnl_pct = ((price - trade.entry_price) / trade.entry_price * 100
                                   if trade.direction == 'CALL'
                                   else (trade.entry_price - price) / trade.entry_price * 100)
                        self.trade_logger.log_position_update(
                            trade_id, price,
                            unrealized_pnl=pnl_pct * getattr(trade, 'position_size', 1) / 100,
                            unrealized_pnl_pct=pnl_pct,
                        )

                        action = self.scaling_mgr.update(trade_id, price)
                        if action['action'] == 'close_half':
                            self.stats['tp1_hits'] += 1
                            self.trade_logger.log_scale_event(trade_id, 'close_half', 'TP1', price, pnl_pct)
                            logger.info(f"  SCALE TP1: {trade_id} — close 50%, trail runner")
                        elif action['action'] == 'close_all':
                            reason = action.get('reason', 'TRAIL')
                            self.stats[f'exit_{reason.lower()}'] += 1
                            self.trade_logger.log_scale_event(trade_id, 'close_all', reason, price, pnl_pct)
                            logger.info(f"  SCALE EXIT: {trade_id} | {reason}")

                closed = self.pos_mgr.check_exits(current_prices)
                if closed:
                    for t in closed:
                        self.scaling_mgr.remove_trade(t.trade_id)
                        exit_pnl_pct = t.pnl / (t.entry_price * getattr(t, 'position_size', 1) / 100) if t.entry_price else 0
                        self.trade_logger.log_exit(
                            trade_id=t.trade_id,
                            exit_price=getattr(t, 'exit_price', 0),
                            exit_reason=t.exit_reason,
                            pnl=t.pnl,
                            pnl_pct=exit_pnl_pct,
                            hold_bars=getattr(t, 'hold_bars', 0),
                        )
                        logger.info(f"  Closed: {t.trade_id} | {t.exit_reason} | PnL=${t.pnl:.2f}")

                # ══════ EVENT-DRIVEN: DRAIN SCAN QUEUE ══════
                tickers_to_scan = set()
                while True:
                    try:
                        ticker = self._scan_queue.get_nowait()
                        tickers_to_scan.add(ticker)
                    except queue.Empty:
                        break

                # ══════ REST FALLBACK: POLL IF NO WEBSOCKET ══════
                now = time.time()
                if not self.data_feed.is_streaming():
                    if now - last_rest_poll >= 10:
                        self.data_feed.update_all()
                        tickers_to_scan = set(TICKERS.keys())
                        last_rest_poll = now

                # ══════ SCAN EACH TICKER THAT GOT NEW DATA ══════
                for ticker in tickers_to_scan:
                    self._scan_ticker(ticker, current_prices, now)

                # ══════ INSTITUTIONAL STATE UPDATES ══════
                for ticker in TICKERS.keys():
                    bars_1min = self.data_feed.get_bars(ticker, '1min')
                    if bars_1min is not None and len(bars_1min) > 30:
                        self._update_institutional_state(ticker, bars_1min)

                # ══════ INSTITUTIONAL DASHBOARD (every 30s) ══════
                if now - last_inst_dashboard >= 30:
                    self._log_institutional_dashboard()
                    last_inst_dashboard = now

                # ══════ LIVE STATE EXPORTER ══════
                if now - getattr(self, '_last_live_export', 0) >= 2:
                    try:
                        import os
                        live_payload = {
                            "timestamp": now,
                            "summary": self.pos_mgr.get_summary(),
                            "institutional_state": self.institutional_state,
                            "active_trades": [vars(t) for t in getattr(self.pos_mgr, 'open_trades', {}).values()] if hasattr(self.pos_mgr, 'open_trades') else [],
                            "stats": self.stats,
                            "latest_signals": self.latest_signals
                        }
                        with open("live_state_tmp.json", "w") as f:
                            json.dump(live_payload, f, default=str)
                        os.rename("live_state_tmp.json", "live_state.json")
                        self._last_live_export = now
                    except Exception as e:
                        logger.error(f"State export failed: {e}")
                        import traceback
                        logger.error(traceback.format_exc())

                # ══════ RISK & LOGGING ══════
                equity = RISK.get('account_size', 25000)
                max_loss = equity * RISK['max_daily_loss_pct'] / 100
                if self.pos_mgr.daily_stats.total_pnl < -max_loss:
                    logger.warning(f"DAILY LOSS LIMIT EXCEEDED: ${self.pos_mgr.daily_stats.total_pnl:.2f}")

                if now - last_status_log >= 300:
                    summary = self.pos_mgr.get_summary()
                    streaming = "WS" if self.data_feed.is_streaming() else "REST"
                    scan_count = self.stats.get('scan_count', 1)
                    avg_scan = self.stats.get('scan_ms_total', 0) / max(scan_count, 1)
                    logger.info(
                        f"STATUS [{streaming}] | Mode={self.mode_detector.current_mode} | "
                        f"Bias={self.ema_bias.current_bias} | "
                        f"Open={summary['open_positions']} | "
                        f"Trades={summary['trades_today']} | "
                        f"WR={summary['win_rate']}% | "
                        f"PnL=${summary['daily_pnl']:.2f} | "
                        f"Inst={self.inst_stats.get('signals_detected', 0)} | "
                        f"AvgScan={avg_scan:.1f}ms"
                    )
                    last_status_log = now

                # Sleep
                elapsed = time.time() - loop_start
                if self.data_feed.is_streaming():
                    time.sleep(max(0.5, 1 - elapsed))
                else:
                    time.sleep(max(1, 10 - elapsed))

        except KeyboardInterrupt:
            logger.info("\nShutdown requested...")
            self._shutdown(current_prices)

    # ── Institutional State Update (NEW) ──

    def _update_institutional_state(self, ticker: str, df):
        """
        Update institutional state cache for ticker.
        Called every scan cycle per ticker.
        Stores rich whale data, liquidity state, and Wyckoff phase detection.
        """
        try:
            # Set ticker for real UW data fetching
            self.whale_tracker.set_ticker(ticker)
            self.institutional_flow.set_ticker(ticker)

            # Get institutional flow score
            flow_obj = self.institutional_flow.get_institutional_score(df)
            flow_score = flow_obj.score if hasattr(flow_obj, 'score') else flow_obj

            # Whale tracking with live state update
            last_bar = df.iloc[-1] if len(df) > 0 else None
            if last_bar is not None:
                self.whale_tracker.update_bar(last_bar)

            # Get whale signal (primary direction/confidence)
            whale_signal = self.whale_tracker.get_whale_signal(df)
            whale_direction = whale_signal.direction if hasattr(whale_signal, 'direction') else 'NEUTRAL'
            whale_confidence = whale_signal.confidence if hasattr(whale_signal, 'confidence') else 0

            # Try to get live whale signal if available
            try:
                whale_signal_live = self.whale_tracker.get_whale_signal_live(df)
                if whale_signal_live:
                    whale_direction = whale_signal_live.direction if hasattr(whale_signal_live, 'direction') else whale_direction
                    whale_confidence = whale_signal_live.confidence if hasattr(whale_signal_live, 'confidence') else whale_confidence
            except:
                pass

            # Rich whale data extraction
            accumulation_zones = self.whale_tracker.detect_accumulation_zones(df)
            distribution_signal = self.whale_tracker.detect_distribution(df)
            volume_profile = self.whale_tracker.build_volume_profile(df)
            vwap_data = self.whale_tracker.anchored_vwap(df)
            iceberg_orders = self.whale_tracker.detect_iceberg_orders(df)
            whale_momentum = self.whale_tracker.whale_momentum(df)
            whale_position = self.whale_tracker.track_whale_positions(df)

            # Get liquidity state for whale trap detection
            liquidity_state = self.liquidity_mapper.get_liquidity_state(df)
            liquidity_pools = liquidity_state.bsl_pools if hasattr(liquidity_state, 'bsl_pools') else []

            # Whale traps (cross-reference with liquidity pools)
            whale_traps = self.whale_tracker.detect_whale_traps(df, liquidity_pools=liquidity_pools)

            # Divergence state
            divergence_state = self.whale_tracker.whale_vs_retail_divergence(df)

            # Whale heatmap
            whale_heatmap = self.whale_tracker.get_whale_heatmap(df)

            # Get live alerts
            live_alerts = self.whale_tracker.get_live_alerts()

            # Liquidity mapping
            liquidity_voids = liquidity_state.voids if hasattr(liquidity_state, 'voids') else []
            recent_sweeps = liquidity_state.recent_sweeps if hasattr(liquidity_state, 'recent_sweeps') else []

            # Wyckoff phase detection (on institutional_flow, not liquidity_mapper)
            wyckoff_obj = self.institutional_flow.wyckoff_phase_detection(df)
            wyckoff_phase = wyckoff_obj.phase if hasattr(wyckoff_obj, 'phase') else 'UNKNOWN'

            # V3: Classify whale intent (3-state model)
            try:
                whale_intent = self.whale_intent_classifier.classify(
                    ticker,
                    whale_tracker=self.whale_tracker,
                    institutional_flow=self.institutional_flow,
                )
            except Exception:
                whale_intent = None

            # Build cache with rich whale data
            self.institutional_state[ticker] = {
                'timestamp': datetime.now().isoformat(),
                'flow_score': flow_score,
                'whale_direction': whale_direction,
                'whale_confidence': whale_confidence,
                'whale_position_estimate': whale_position,
                'accumulation_zones': accumulation_zones,
                'distribution_signal': distribution_signal,
                'volume_profile': volume_profile,
                'vwap_data': vwap_data,
                'iceberg_orders': iceberg_orders,
                'whale_momentum': whale_momentum,
                'whale_traps': whale_traps,
                'divergence_state': divergence_state,
                'whale_heatmap': whale_heatmap,
                'live_alerts': live_alerts,
                'liquidity_pools': liquidity_pools,
                'liquidity_voids': liquidity_voids,
                'recent_sweeps': recent_sweeps,
                'wyckoff_phase': wyckoff_phase,
                'whale_intent': whale_intent,
                '_whale_tracker': self.whale_tracker,
            }

            # Log whale alerts when they trigger
            if live_alerts:
                for alert in live_alerts:
                    alert_desc = str(alert) if hasattr(alert, '__str__') else str(alert)
                    logger.info(f"WHALE ALERT {ticker}: {alert_desc}")

            # Log transitions
            prev_state = self.institutional_state.get(ticker, {})
            if whale_direction != prev_state.get('whale_direction'):
                logger.info(f"WHALE FLIP: {ticker} — {whale_direction} "
                           f"(conf={whale_confidence:.2f})")

        except Exception as e:
            logger.debug(f"Institutional state update failed for {ticker}: {e}")

    # ── Scan One Ticker (extended for institutional) ──

    def _scan_ticker(self, ticker: str, current_prices: dict, now: float):
        """Build context and scan all strategies for one ticker."""
        scan_start = time.perf_counter()

        tf_data = {}
        for tf in self.needed_tfs:
            bars = self.data_feed.get_bars(ticker, tf)
            if bars is not None and len(bars) > 30:
                tf_data[tf] = bars

        if '1min' not in tf_data:
            return

        df_1min = tf_data['1min']
        df_5m = tf_data.get('5min', None)

        # Compute context ONCE per ticker
        sweep_bull, sweep_bear = detect_liquidity_levels(df_1min)
        htf_bias = self.ema_bias.compute_bias(tf_data)

        # Determine market mode
        current_mode = self.mode_detector.determine_mode(
            df_1min, len(df_1min) - 1, sweep_bull, sweep_bear
        )
        self.stats[f'mode_{current_mode.lower()}'] += 1

        # Log mode transitions
        if current_mode != self._prev_mode:
            adx_val = float(df_1min['ADX_14'].iloc[-1]) if 'ADX_14' in df_1min.columns else 0
            self.trade_logger.log_mode_change(
                self._prev_mode, current_mode, ticker,
                adx=adx_val, has_sweep=bool(sweep_bull[-1]) or bool(sweep_bear[-1]),
            )
            self._prev_mode = current_mode

        # HTF trend reference
        htf_trend = None
        if '1hr' in tf_data and 'Trend_Dir' in tf_data['1hr'].columns:
            htf_trend = tf_data['1hr'][['Date', 'Trend_Dir']].copy()
            htf_trend.set_index('Date', inplace=True)

        # ══════ V1 SMC SIGNAL SCAN ══════
        if self.mode in ('hybrid', 'institutional_only'):
            for strategy in self.strategies:
                cat = strategy['category']
                interval = SCAN_INTERVALS.get(cat, 60)
                scan_key = f"{cat}_{ticker}"
                last = self.last_scan.get(scan_key, 0)
                if now - last < interval:
                    continue

                self._scan_strategy_with_context(
                    strategy, ticker, tf_data, df_5m,
                    sweep_bull, sweep_bear, htf_bias, current_mode,
                    htf_trend, current_prices,
                )

                self.last_scan[scan_key] = now

        # ══════ V2 INSTITUTIONAL SIGNAL SCAN (NEW) ══════
        if self.mode in ('hybrid', 'institutional_only'):
            inst_scan_key = f"inst_{ticker}"
            inst_last = self.last_institutional_scan.get(inst_scan_key, 0)
            inst_interval = INSTITUTIONAL_CONFIG.get('scan_interval', 60)

            if now - inst_last >= inst_interval:
                self._scan_institutional_signals(ticker, tf_data, current_prices, now)
                self.last_institutional_scan[inst_scan_key] = now

        scan_ms = (time.perf_counter() - scan_start) * 1000
        if scan_ms > 50:
            logger.warning(f"SLOW SCAN: {ticker} took {scan_ms:.0f}ms")
        self.stats['scan_ms_total'] = self.stats.get('scan_ms_total', 0) + scan_ms
        self.stats['scan_count'] = self.stats.get('scan_count', 0) + 1

    # ── Scan Institutional Signals (NEW) ──

    def _scan_institutional_signals(self, ticker: str, tf_data: dict, current_prices: dict, now: float):
        """Scan for institutional signals using InstitutionalSignalGenerator."""
        try:
            for timeframe in ['5min', '1hr', '4hr']:
                if timeframe not in tf_data:
                    continue

                df = tf_data[timeframe]
                if len(df) < 50:
                    continue

                # Generate institutional signals
                signals = self.inst_signal_gen.scan_all_signals(df, ticker, timeframe)

                for signal_obj in signals:
                    signal = vars(signal_obj) if not isinstance(signal_obj, dict) else signal_obj
                    
                    # Log signal
                    self.trade_logger.log_signal_detected(
                        ticker, {'name': f"inst_{signal.get('signal_type', 'unknown')}", 'module': 'institutional'},
                        signal.get('direction', 'CALL'), len(df) - 1, timeframe,
                        raw_values={'inst_score': signal.get('confidence', 0)},
                    )

                    # Execute institutional signal
                    self._execute_institutional_signal(
                        ticker, signal, df, current_prices, now
                    )

                    self.inst_stats['signals_detected'] += 1

        except Exception as e:
            logger.error(f"Institutional signal scan failed for {ticker}: {e}")
            import traceback
            logger.error(traceback.format_exc())

    # ── Execute Institutional Signal (NEW) ──

    def _execute_institutional_signal(self, ticker: str, signal: dict, df, current_prices: dict, now: float):
        """Execute an institutional signal through institutional filter pipeline."""
        entry_price = current_prices.get(ticker)
        if entry_price is None:
            return

        direction = signal.get('direction', 'CALL')
        confidence = signal.get('confidence', 0.5)
        entry_idx = min(len(df) - 1, len(df) - 1)

        # Get full institutional state for this ticker
        inst_state = self.institutional_state.get(ticker, {})

        signal['entry_price'] = entry_price
        signal['ticker'] = ticker
        
        # V3 Institutional Filter with full state
        try:
            inst_ok, inst_meta, filter_pipeline = v3_master_filter_institutional_logged(
                signal=signal,
                row=df.iloc[entry_idx],
                df=df,
                context={'idx': entry_idx, 'inst_state': inst_state},
                config=V3_CONFIG,
                logger=logger
            )
        except Exception as e:
            logger.error(f"Institutional filter error: {e}")
            import traceback
            logger.error(traceback.format_exc())
            return

        if not inst_ok:
            blocker = inst_meta.get('blocked_by', 'unknown')
            self.inst_stats[f'blocked_{blocker}'] += 1
            
            # Extract target OCC to show what we *would* have traded on the dashboard
            target_occ = self.pos_mgr._find_target_contract(ticker, direction, entry_price, "0DTE")
            out_strike = entry_price
            out_expiry = "N/A"
            if target_occ:
                import re
                m = re.match(r'^([A-Z]+)(\d{6})([CP])(\d{8})$', target_occ)
                if m:
                    out_expiry = f"20{m.group(2)[0:2]}-{m.group(2)[2:4]}-{m.group(2)[4:6]}"
                    out_strike = float(m.group(4)) / 1000.0

            sig = {
                "ticker": ticker,
                "direction": direction,
                "strike": out_strike,
                "expiry": out_expiry,
                "occ": target_occ or "None Found",
                "status": f"BLOCKED ({blocker})" if not inst_ok else "PLACED",
                "signal_type": signal.get('signal_type', 'unknown'),
                "time": time.time()
            }
            self.latest_signals.insert(0, sig)
            self.latest_signals = self.latest_signals[:10]
            
            return

        # Position size modifier from institutional filters
        size_modifier = inst_meta.get('position_size_modifier', 1.0)

        # Capture market context with rich whale data
        entry_context = self.trade_logger.capture_context(
            df, entry_idx,
            extra={
                'institutional': True,
                'inst_score': inst_state.get('flow_score', 0),
                'whale_direction': inst_state.get('whale_direction'),
                'whale_confidence': inst_state.get('whale_confidence', 0),
                'whale_position': inst_state.get('whale_position_estimate'),
                'accumulation_zones': inst_state.get('accumulation_zones', []),
                'distribution_signal': inst_state.get('distribution_signal'),
                'divergence_state': inst_state.get('divergence_state'),
                'whale_momentum': inst_state.get('whale_momentum'),
                'whale_traps': inst_state.get('whale_traps', []),
                'liquidity_draw': len(inst_state.get('recent_sweeps', [])),
                'volume_profile': inst_state.get('volume_profile'),
                'wyckoff_phase': inst_state.get('wyckoff_phase'),
            },
        )

        # Place the trade
        trade = self.pos_mgr.open_trade(
            ticker=ticker,
            direction=direction,
            entry_price=entry_price,
            strategy={'name': signal.get('signal_type'), 'module': 'institutional', 'backtest_wr': 0},
            stop_pct=signal.get('stop_pct', 2),
            target_pct=signal.get('target_pct', 4),
            paper=self.paper,
            size_modifier=size_modifier,
        )

        if trade:
            self.scaling_mgr.init_trade(
                trade.trade_id, entry_price, direction,
                signal.get('stop_pct', 2), signal.get('target_pct', 4),
            )
            self.inst_stats['trades_placed'] += 1

            # Log entry with full institutional context
            self.trade_logger.log_entry(
                trade_id=trade.trade_id,
                ticker=ticker,
                strategy={'name': signal.get('signal_type'), 'module': 'institutional'},
                direction=direction,
                entry_price=entry_price,
                context=entry_context,
                filter_pipeline=filter_pipeline,
            )

            logger.info(
                f"INST TRADE: {trade.trade_id} | {direction} {ticker} @ ${entry_price:.2f} | "
                f"SL=${trade.stop_price:.2f} TP=${trade.target_price:.2f} | "
                f"Confidence={confidence:.2f} | Size={size_modifier:.2f}x | "
                f"WhaleDir={inst_state.get('whale_direction', 'N/A')} | "
                f"Wyckoff={inst_state.get('wyckoff_phase', 'N/A')}"
            )

    # ── Strategy Scan (V1, unchanged) ──

    def _scan_strategy_with_context(self, strategy, ticker, tf_data, df_5m,
                                     sweep_bull, sweep_bear, htf_bias, current_mode,
                                     htf_trend, current_prices):
        """Scan ONE strategy for ONE ticker with pre-computed context."""
        tf = strategy['timeframe']
        if tf not in tf_data:
            return

        df = tf_data[tf]
        if len(df) < 50:
            return

        # ══════ DECISION TREE: MODE ROUTING ══════
        strategy_mode = strategy.get('mode', 'TREND')

        # Check sweep for override
        n = len(df)
        has_sweep_here = False
        if n > 0 and n <= len(sweep_bull):
            has_sweep_here = bool(sweep_bull[n-1]) or bool(sweep_bear[n-1])
        elif '5min' in tf_data:
            sb, se = detect_liquidity_levels(df)
            has_sweep_here = bool(sb[-1]) or bool(se[-1]) if len(sb) > 0 else False

        mode_ok, mode_reason = self.mode_detector.check_mode_alignment(
            strategy_mode, current_mode, has_sweep_here
        )
        if not mode_ok:
            self.stats[f'blocked_{mode_reason}'] += 1
            self.trade_logger.log_filter_result(
                ticker, strategy, '', 'mode_alignment', False,
                {'strategy_mode': strategy_mode, 'market_mode': current_mode, 'reason': mode_reason},
            )
            return

        # Detect raw signals
        signal_call, signal_put = detect_signals(df, strategy['signal_func'])
        call_idx = np.where(signal_call)[0]
        put_idx = np.where(signal_put)[0]

        if len(call_idx) == 0 and len(put_idx) == 0:
            return

        # Only recent signals (last 3 bars)
        recent = n - 3
        call_idx = call_idx[call_idx >= recent]
        put_idx = put_idx[put_idx >= recent]

        if len(call_idx) == 0 and len(put_idx) == 0:
            return

        htf_trend_ref = htf_trend

        # Process CALL signals
        for idx in call_idx:
            self.trade_logger.log_signal_detected(
                ticker, strategy, 'CALL', idx, tf,
                raw_values={'close': float(df['Close'].iloc[idx])},
            )
            if not self.ema_bias.check_alignment('CALL', strategy.get('category', 'scalp')):
                self.stats['blocked_ema_bias'] += 1
                self.trade_logger.log_filter_result(
                    ticker, strategy, 'CALL', 'ema_bias', False,
                    {'bias': self.ema_bias.current_bias, 'direction': 'CALL'},
                )
                continue
            filtered = apply_v2_filters(df, np.array([idx]), 'CALL', strategy,
                                        htf_trend=htf_trend_ref, v2_config=V2_FILTERS)
            for entry_idx, stop_pct, target_pct in filtered:
                if entry_idx >= n - 2:
                    self._execute_signal(
                        ticker, 'CALL', df, entry_idx, strategy,
                        stop_pct, target_pct, current_prices,
                        sweep_bull, sweep_bear, df_5m,
                    )

        # Process PUT signals
        for idx in put_idx:
            self.trade_logger.log_signal_detected(
                ticker, strategy, 'PUT', idx, tf,
                raw_values={'close': float(df['Close'].iloc[idx])},
            )
            if not self.ema_bias.check_alignment('PUT', strategy.get('category', 'scalp')):
                self.stats['blocked_ema_bias'] += 1
                self.trade_logger.log_filter_result(
                    ticker, strategy, 'PUT', 'ema_bias', False,
                    {'bias': self.ema_bias.current_bias, 'direction': 'PUT'},
                )
                continue
            filtered = apply_v2_filters(df, np.array([idx]), 'PUT', strategy,
                                        htf_trend=htf_trend_ref, v2_config=V2_FILTERS)
            for entry_idx, stop_pct, target_pct in filtered:
                if entry_idx >= n - 2:
                    self._execute_signal(
                        ticker, 'PUT', df, entry_idx, strategy,
                        stop_pct, target_pct, current_prices,
                        sweep_bull, sweep_bear, df_5m,
                    )

    # ── Execute V1 Signal ──

    def _execute_signal(self, ticker, direction, df, entry_idx, strategy,
                        stop_pct, target_pct, current_prices,
                        sweep_bull, sweep_bear, df_5m):
        """Execute a V1 SMC signal through the full V3 master filter pipeline."""
        entry_price = current_prices.get(ticker)
        if entry_price is None:
            return

        # V3 MASTER FILTER — LOGGED version
        v3_ok, v3_meta, filter_pipeline = v3_master_filter_logged(
            df, min(entry_idx, len(df) - 1), direction, strategy, entry_price,
            sweep_bull=sweep_bull, sweep_bear=sweep_bear,
            vol_regime=self.vol_regime, df_5m=df_5m,
            logger_inst=self.trade_logger, ticker=ticker,
        )

        if not v3_ok:
            blocker = v3_meta.get('blocked_by', 'unknown')
            self.stats[f'blocked_{blocker}'] += 1
            logger.debug(f"V3 blocked: {strategy['name']} {ticker} {direction} — {blocker}")
            return

        # Use refined entry if available
        raw_price = entry_price
        if v3_meta.get('refined_entry') is not None:
            entry_price = v3_meta['refined_entry']
            stop_pct = v3_meta['new_stop_pct']
            target_pct = v3_meta['new_target_pct']
            self.stats['refined_entries'] += 1

        # Capture market context at entry
        entry_context = self.trade_logger.capture_context(
            df, min(entry_idx, len(df) - 1),
            extra={
                'market_mode': self.mode_detector.current_mode,
                'ema_bias': self.ema_bias.current_bias,
                'vol_regime': v3_meta.get('regime', 'unknown'),
                'institutional': False,
            },
        )

        # Place the trade
        trade = self.pos_mgr.open_trade(
            ticker=ticker,
            direction=direction,
            entry_price=entry_price,
            strategy=strategy,
            stop_pct=stop_pct,
            target_pct=target_pct,
            paper=self.paper,
        )

        if trade:
            # Register with scaling manager
            self.scaling_mgr.init_trade(
                trade.trade_id, entry_price, direction, stop_pct, target_pct,
            )
            self.stats['trades_placed'] += 1

            # ══════ LOG THE ENTRY ══════
            self.trade_logger.log_entry(
                trade_id=trade.trade_id,
                ticker=ticker,
                strategy=strategy,
                direction=direction,
                entry_price=entry_price,
                raw_price=raw_price,
                refined_price=v3_meta.get('refined_entry'),
                context=entry_context,
                filter_pipeline=filter_pipeline,
            )

            logger.info(
                f"SMC TRADE: {trade.trade_id} | {direction} {ticker} @ ${entry_price:.2f} | "
                f"SL=${trade.stop_price:.2f} TP=${trade.target_price:.2f} | "
                f"Module={strategy['module']} | Strategy={strategy['name']} ({strategy['backtest_wr']:.1f}% WR)"
            )
        else:
            # Signal passed all filters but couldn't be traded
            self.trade_logger.log_entry_skipped(
                ticker, strategy, direction, 'position_manager_rejected',
                context=entry_context,
            )

    # ── Institutional Dashboard (NEW) ──

    def _log_institutional_dashboard(self):
        """Log live institutional status dashboard with rich whale data."""
        logger.info("=" * 80)
        logger.info("INSTITUTIONAL DASHBOARD")
        logger.info("=" * 80)

        for ticker, state in self.institutional_state.items():
            # Core metrics
            flow = state.get('flow_score', 0)
            whale_dir = state.get('whale_direction', 'NEUTRAL')
            whale_conf = state.get('whale_confidence', 0)
            wyckoff = state.get('wyckoff_phase', 'UNKNOWN')

            # Whale position and momentum
            whale_pos = state.get('whale_position_estimate', {})
            whale_pos_str = str(whale_pos) if whale_pos else "N/A"
            momentum = state.get('whale_momentum', {})
            momentum_str = str(momentum) if momentum else "N/A"

            # Accumulation zones
            acc_zones = state.get('accumulation_zones', [])
            acc_count = len(acc_zones) if acc_zones else 0

            # Distribution and divergence
            dist = state.get('distribution_signal', {})
            div = state.get('divergence_state', {})
            div_str = str(div) if div else "N/A"

            # Liquidity and sweeps
            sweeps = len(state.get('recent_sweeps', []))
            voids = len(state.get('liquidity_voids', []))
            traps = len(state.get('whale_traps', []))

            # Volume profile
            vol_prof = state.get('volume_profile', {})
            poc = vol_prof.poc if hasattr(vol_prof, 'poc') else 'N/A'
            vah = vol_prof.vah if hasattr(vol_prof, 'vah') else 'N/A'

            # Log main ticker line
            logger.info(
                f"{ticker:8} | Flow={flow:6.2f} | Whale={whale_dir:8} ({whale_conf:.2f}) | "
                f"Wyckoff={wyckoff:12} | Sweeps={sweeps:2} | Traps={traps:2}"
            )

            # Log accumulation zones if present
            if acc_zones:
                zone_strs = [str(z) for z in acc_zones[:3]]
                logger.info(f"         | Accumulation zones: {', '.join(zone_strs)}")

            # Log whale position estimate
            if whale_pos:
                logger.info(f"         | Whale position: {whale_pos_str}")

            # Log distribution state
            if dist:
                dist_str = str(dist)
                logger.info(f"         | Distribution: {dist_str}")

            # Log divergence state
            if div:
                logger.info(f"         | Retail divergence: {div_str}")

            # Log volume profile POC/VAH/VAL
            if vol_prof and (poc != 'N/A' or vah != 'N/A'):
                val = vol_prof.val if hasattr(vol_prof, 'val') else 'N/A'
                logger.info(f"         | Volume profile - POC={poc}, VAH={vah}, VAL={val}")

            # Log active alerts
            alerts = state.get('live_alerts', [])
            if alerts:
                for alert in alerts[:3]:
                    alert_desc = str(alert) if hasattr(alert, '__str__') else str(alert)
                    logger.info(f"         | ALERT: {alert_desc}")

        logger.info("-" * 80)
        logger.info("Active institutional signals:")
        # This would be populated from signal queue tracking
        logger.info(f"Total tickers monitored: {len(self.institutional_state)}")

        logger.info("=" * 80)

    # ── Shutdown (extended) ──

    def _shutdown(self, current_prices):
        """Graceful shutdown with institutional analysis."""
        logger.info("Shutting down institutional engine...")

        # Stop WebSocket
        self.data_feed.shutdown()

        if self.pos_mgr.open_trades:
            logger.info(f"Closing {len(self.pos_mgr.open_trades)} open positions...")
            self.pos_mgr.force_close_all(current_prices, reason='SHUTDOWN')

        self.pos_mgr.save_trades('trades_v2.json')

        # ══════ SAVE TRADE LOGS ══════
        log_file = self.trade_logger.save_session()
        logger.info(f"Trade logs saved: {log_file}")

        # ══════ SAVE INSTITUTIONAL STATE ══════
        inst_state_file = log_file.replace('.json', '_institutional_state.json')
        try:
            with open(inst_state_file, 'w') as f:
                json.dump(self.institutional_state, f, indent=2, default=str)
            logger.info(f"Institutional state saved: {inst_state_file}")
        except Exception as e:
            logger.warning(f"Failed to save institutional state: {e}")

        # ══════ RUN POST-SESSION ANALYSIS ══════
        try:
            import json as _json
            with open(log_file, 'r') as f:
                session_data = _json.load(f)
            report = self.trade_analyzer.generate_report(session_data)

            report_file = log_file.replace('.json', '_analysis.json')
            with open(report_file, 'w') as f:
                _json.dump(report, f, indent=2, default=str)
            logger.info(f"Analysis report saved: {report_file}")

            # Print key findings
            logger.info("=" * 80)
            logger.info("POST-SESSION ANALYSIS")
            logger.info("=" * 80)
            if report.get('what_went_well'):
                logger.info("WHAT WENT WELL:")
                for w in report['what_went_well']:
                    logger.info(f"  + {w}")
            if report.get('what_went_wrong'):
                logger.info("WHAT WENT WRONG:")
                for w in report['what_went_wrong']:
                    logger.info(f"  - {w}")
            if report.get('improvements'):
                logger.info("IMPROVEMENTS:")
                for imp in report['improvements'][:5]:
                    logger.info(f"  > {imp}")
        except Exception as e:
            logger.warning(f"Analysis failed: {e}")

        # ══════ INSTITUTIONAL VS RETAIL COMPARISON ══════
        try:
            logger.info("=" * 80)
            logger.info("INSTITUTIONAL vs RETAIL SIGNAL PERFORMANCE")
            logger.info("=" * 80)

            smc_trades = self.stats.get('trades_placed', 0) - self.inst_stats.get('trades_placed', 0)
            inst_trades = self.inst_stats.get('trades_placed', 0)

            logger.info(f"SMC Signals (Retail):        {smc_trades} trades")
            logger.info(f"Institutional Signals:       {inst_trades} trades")
            logger.info(f"SMC Blocked:                 {sum(v for k, v in self.stats.items() if k.startswith('blocked_'))}")
            logger.info(f"Institutional Blocked:       {sum(v for k, v in self.inst_stats.items() if k.startswith('blocked_'))}")
            logger.info("=" * 80)
        except Exception as e:
            logger.debug(f"Performance comparison failed: {e}")

        # ══════ SESSION SUMMARY ══════
        summary = self.pos_mgr.get_summary()
        logger.info("=" * 80)
        logger.info("SESSION SUMMARY")
        logger.info(f"  Mode: {self.mode.upper()}")
        logger.info(f"  Trades: {summary['trades_today']}")
        logger.info(f"  Win Rate: {summary['win_rate']}%")
        logger.info(f"  Daily PnL: ${summary['daily_pnl']:.2f}")
        logger.info(f"  V1 Stats: {dict(self.stats)}")
        logger.info(f"  V2 Stats: {dict(self.inst_stats)}")
        logger.info("=" * 80)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Institutional Trading Engine V2')
    parser.add_argument('--live', action='store_true', help='Live trading mode')
    parser.add_argument('--mode', default='hybrid', choices=['hybrid', 'institutional_only'],
                        help='Trading mode: hybrid (SMC+Institutional) or institutional_only')
    parser.add_argument('--tickers', default=None, help='Comma-separated tickers (override config)')
    parser.add_argument('--log-level', default='INFO', help='Log level')
    args = parser.parse_args()

    setup_logging(args.log_level)

    # Override tickers if specified
    if args.tickers:
        ticker_list = args.tickers.split(',')
        TICKERS.clear()
        for t in ticker_list:
            TICKERS[t.strip()] = {}

    engine = InstitutionalTradingEngine(paper=not args.live, mode=args.mode)
    engine.start()
