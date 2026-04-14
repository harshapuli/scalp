"""
MASTER TRADING ENGINE — ONE SYSTEM, ONE PROCESS
==================================================
This is NOT 84 separate strategies running independently.
This is ONE decision tree that routes signals through:

  1. Data Update   → Pull live bars for all tickers + all needed TFs
  2. Context Build  → Compute sweep levels, EMA bias, volatility regime
  3. Mode Detect    → TREND / REVERSAL / NO_TRADE
  4. Signal Scan    → Detect signals from 20 strategies
  5. Filter Stack   → Mode routing → EMA bias → V2 → V3 full pipeline
  6. Execute        → Limit entry at refined price, 2-stage exit management
  7. Monitor        → ScalingManager handles TP1/TP2/trailing stops

Usage:
  python engine.py              # Paper trading (default)
  python engine.py --live       # Live trading (be careful!)
"""
import sys
import time
import json
import logging
import argparse
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, List
from collections import defaultdict

from config import (
    ALPACA_API_KEY, ALPACA_API_SECRET, ALPACA_BASE_URL, ALPACA_DATA_URL,
    TICKERS, RISK, STRATEGIES, V2_FILTERS, V3_CONFIG, SCAN_INTERVALS, MODULES,
)
from execution import AlpacaClient, PositionManager, OptionsHelper
from data_feed import LiveDataFeed
from signals import detect_signals, apply_v2_filters
from v3_filters import (
    MarketModeDetector, EMABiasFilter, VolatilityRegime, ScalingManager,
    detect_liquidity_levels, v3_master_filter,
)
from trade_logger import TradeLogger, v3_master_filter_logged
from trade_analyzer import TradeAnalyzer


# ============================================================
# LOGGING
# ============================================================

def setup_logging(level='INFO'):
    fmt = '%(asctime)s | %(name)-12s | %(levelname)-5s | %(message)s'
    logging.basicConfig(level=level, format=fmt)
    fh = logging.FileHandler('trading_engine.log')
    fh.setFormatter(logging.Formatter(fmt))
    logging.getLogger().addHandler(fh)

logger = logging.getLogger('engine')


# ============================================================
# MASTER TRADING ENGINE — ONE PROCESS
# ============================================================

class TradingEngine:
    """
    The ONE trading engine. Not 84 processes. One decision tree.
    """

    def __init__(self, paper: bool = True, strategies: list = None):
        self.paper = paper
        self.strategies = strategies or STRATEGIES

        # Alpaca client
        self.client = AlpacaClient(
            api_key=ALPACA_API_KEY,
            api_secret=ALPACA_API_SECRET,
            base_url=ALPACA_BASE_URL,
            data_url=ALPACA_DATA_URL,
        )

        # Options helper (chain lookup, strike selection)
        self.options_helper = OptionsHelper(ALPACA_API_KEY, ALPACA_API_SECRET, paper=True)
        logger.info("OPTIONS: Initialized options helper (Level 3 approved)")

        # Position manager (with options support)
        self.pos_mgr = PositionManager(self.client, RISK, options_helper=self.options_helper)

        # ══════ MASTER SYSTEM COMPONENTS ══════
        self.mode_detector = MarketModeDetector()      # Decision tree
        self.ema_bias = EMABiasFilter()                 # Strict 1H EMA bias
        self.vol_regime = VolatilityRegime()             # ATR + ADX regime
        self.scaling_mgr = ScalingManager()              # 2-stage exits

        # ══════ TRADE LOGGING & ANALYSIS ══════
        self.trade_logger = TradeLogger(log_dir='trade_logs')
        self.trade_analyzer = TradeAnalyzer()
        self._prev_mode = 'TREND'
        self._prev_bias = 'NEUTRAL'

        # Determine needed timeframes (always include 1hr, 4hr, 5min, daily for context)
        self.needed_tfs = set(s['timeframe'] for s in self.strategies)
        self.needed_tfs.update(['1hr', '4hr', '5min', 'daily'])

        # Data feed (WebSocket primary, REST fallback)
        self.data_feed = LiveDataFeed(
            client=self.client,
            tickers=list(TICKERS.keys()),
            needed_timeframes=self.needed_tfs,
            use_sip=True,  # Paid Alpaca — use SIP for real-time data
        )

        # Register for bar callbacks → event-driven scanning
        self.data_feed.register_bar_callback(self._on_new_bar)

        # Queue of tickers that got new bars (thread-safe)
        import queue
        self._scan_queue: queue.Queue = queue.Queue()

        # Per-category scan timing
        self.last_scan: Dict[str, float] = {}
        # Cooldown per strategy x ticker
        self.last_signal_bar: Dict[str, int] = {}

        # Stats
        self.stats = defaultdict(int)

        logger.info(f"MASTER ENGINE initialized | Paper={paper} | "
                    f"Strategies={len(self.strategies)} | "
                    f"Modules={len(set(s['module'] for s in self.strategies))} | "
                    f"Tickers={len(TICKERS)} | TFs={self.needed_tfs}")

    # ── Startup ──

    def start(self):
        """Start the engine."""
        logger.info("=" * 70)
        logger.info("MASTER TRADING ENGINE — STARTING")
        logger.info(f"Mode: {'PAPER' if self.paper else '*** LIVE ***'}")
        logger.info(f"Strategies: {len(self.strategies)} across 7 modules")
        logger.info(f"Decision tree: TREND / REVERSAL / NO_TRADE")
        logger.info(f"Tickers: {list(TICKERS.keys())}")
        logger.info("=" * 70)

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

    # ── Bar Callback (fired by WebSocket) ──

    def _on_new_bar(self, ticker: str, bar: dict):
        """Called by LiveDataFeed when a new 1min bar arrives via WebSocket."""
        logger.info(f"BAR CALLBACK: {ticker} → queued for scan")
        self._scan_queue.put(ticker)

    # ── Main Loop (event-driven + fallback polling) ──

    def _run_loop(self):
        """
        Event-driven loop:
          - When WebSocket is streaming, bar callbacks push tickers to _scan_queue.
            Each new bar triggers a context build + strategy scan for that ticker.
          - When WebSocket is down, falls back to REST polling every 10s.
          - Position management (scaling, TP1/TP2, trailing) runs every tick.
          - Status logged every 5 minutes.
        """
        import queue
        logger.info("Entering main loop (event-driven + REST fallback) — Ctrl+C to stop")
        last_status_log = 0
        last_rest_poll = 0
        current_prices = {}

        try:
            while True:
                loop_start = time.time()

                # Market hours check (every loop)
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
                            unrealized_pnl=pnl_pct * trade.position_size / 100 if hasattr(trade, 'position_size') else 0,
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
                        # Log exit with context
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
                # Process all tickers that received new bars from WebSocket
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
                if tickers_to_scan:
                    logger.info(f"SCANNING {len(tickers_to_scan)} tickers: {tickers_to_scan}")
                for ticker in tickers_to_scan:
                    self._scan_ticker(ticker, current_prices, now)

                # ══════ RISK & LOGGING ══════
                equity = RISK.get('account_size', 25000)
                max_loss = equity * RISK['max_daily_loss_pct'] / 100
                if self.pos_mgr.daily_stats.total_pnl < -max_loss:
                    logger.warning(f"DAILY LOSS LIMIT: ${self.pos_mgr.daily_stats.total_pnl:.2f}")

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
                        f"TP1={self.stats.get('tp1_hits', 0)} "
                        f"TP2={self.stats.get('exit_tp2', 0)} "
                        f"Trail={self.stats.get('exit_trail', 0)} | "
                        f"AvgScan={avg_scan:.1f}ms ({scan_count} scans)"
                    )
                    last_status_log = now

                # Sleep: shorter when streaming (just checking positions),
                # longer when REST polling (data comes in 10s intervals anyway)
                elapsed = time.time() - loop_start
                if self.data_feed.is_streaming():
                    time.sleep(max(0.5, 1 - elapsed))
                else:
                    time.sleep(max(1, 10 - elapsed))

        except KeyboardInterrupt:
            logger.info("\nShutdown requested...")
            self._shutdown(current_prices)
        except Exception as e:
            logger.error(f"FATAL ERROR in main loop: {e}", exc_info=True)
            logger.error(f"FATAL ERROR in main loop: {e}", exc_info=True)
            self._shutdown(current_prices)

    # ── Scan One Ticker (extracted for event-driven use) ──

    def _scan_ticker(self, ticker: str, current_prices: dict, now: float):
        """Build context and scan all strategies for one ticker."""
        scan_start = time.perf_counter()

        tf_data = {}
        for tf in self.needed_tfs:
            bars = self.data_feed.get_bars(ticker, tf)
            if bars is not None and len(bars) >= 20:
                tf_data[tf] = bars

        if '1min' not in tf_data:
            return

        # One-shot diagnostic: log available TFs on first scan
        if not hasattr(self, '_diag_done'):
            self._diag_done = set()
        if ticker not in self._diag_done:
            self._diag_done.add(ticker)
            tf_summary = {tf: len(df) for tf, df in tf_data.items()}
            logger.info(f"TF DATA [{ticker}]: {tf_summary}")

        df_1min = tf_data['1min']
        df_5m = tf_data.get('5min', None)

        # Compute context ONCE per ticker (not per strategy)
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

        # Scan all strategies
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

        scan_ms = (time.perf_counter() - scan_start) * 1000
        if scan_ms > 50:
            logger.warning(f"SLOW SCAN: {ticker} took {scan_ms:.0f}ms")
        self.stats['scan_ms_total'] = self.stats.get('scan_ms_total', 0) + scan_ms
        self.stats['scan_count'] = self.stats.get('scan_count', 0) + 1

    # ── Strategy Scanner (with full context) ──

    def _scan_strategy_with_context(self, strategy, ticker, tf_data, df_5m,
                                     sweep_bull, sweep_bear, htf_bias, current_mode,
                                     htf_trend, current_prices):
        """Scan ONE strategy for ONE ticker with pre-computed context."""
        tf = strategy['timeframe']
        if tf not in tf_data:
            return

        df = tf_data[tf]
        min_bars = 30 if tf in ('15min', '4hr', 'weekly') else 50
        if len(df) < min_bars:
            return

        # ══════ DECISION TREE: MODE ROUTING ══════
        strategy_mode = strategy.get('mode', 'TREND')

        # Check sweep for override
        n = len(df)
        has_sweep_here = False
        if n > 0 and n <= len(sweep_bull):
            has_sweep_here = bool(sweep_bull[n-1]) or bool(sweep_bear[n-1])
        elif '5min' in tf_data:
            # Compute sweep on strategy TF
            sb, se = detect_liquidity_levels(df)
            has_sweep_here = bool(sb[-1]) or bool(se[-1]) if len(sb) > 0 else False

        mode_ok, mode_reason = self.mode_detector.check_mode_alignment(
            strategy_mode, current_mode, has_sweep_here
        )
        if not mode_ok:
            self.stats[f'blocked_{mode_reason}'] += 1
            logger.info(f"MODE BLOCK: {ticker}/{tf}/{strategy.get('name','?')} — strat={strategy_mode} mkt={current_mode} reason={mode_reason}")
            return

        # ══════ STRICT EMA BIAS CHECK ══════
        cat = strategy.get('category', 'scalp')
        # (applied per-signal in execute, but block entire scan if strongly misaligned)

        # Detect raw signals
        signal_call, signal_put = detect_signals(df, strategy['signal_func'])
        call_idx = np.where(signal_call)[0]
        put_idx = np.where(signal_put)[0]

        raw_calls = len(call_idx)
        raw_puts = len(put_idx)
        if raw_calls == 0 and raw_puts == 0:
            return

        # Only recent signals — window depends on timeframe
        # 1min: last 5 bars (5 min), 5min: last 3 bars (15 min),
        # 15min+: last 2 bars
        recency_map = {'1min': 15, '3min': 10, '5min': 6, '15min': 4, '1hr': 3}
        recency = recency_map.get(tf, 3)
        recent = n - recency
        call_idx = call_idx[call_idx >= recent]
        put_idx = put_idx[put_idx >= recent]

        logger.info(f"SIGNAL DEBUG: {ticker}/{tf}/{strategy.get('name','?')} — raw={raw_calls}C/{raw_puts}P, recent(>={recent})={len(call_idx)}C/{len(put_idx)}P, n={n}, recency={recency}")

        if len(call_idx) == 0 and len(put_idx) == 0:
            return

        # HTF trend for V2
        htf_trend_ref = htf_trend

        # Process CALL signals
        for idx in call_idx:
            self.trade_logger.log_signal_detected(
                ticker, strategy, 'CALL', idx, tf,
                raw_values={'close': float(df['Close'].iloc[idx])},
            )
            logger.info(f"PROCESSING CALL: {ticker}/{tf}/{strategy.get('name','?')} idx={idx} bias={self.ema_bias.current_bias} cat={cat}")
            if not self.ema_bias.check_alignment('CALL', cat):
                self.stats['blocked_ema_bias'] += 1
                logger.info(f"EMA BLOCK: {ticker} CALL — bias={self.ema_bias.current_bias} cat={cat}")
                continue
            filtered = apply_v2_filters(df, np.array([idx]), 'CALL', strategy,
                                        htf_trend=htf_trend_ref, v2_config=V2_FILTERS)
            logger.info(f"V2 FILTER: {ticker} CALL — {len(filtered)} passed out of 1")
            for entry_idx, stop_pct, target_pct in filtered:
                if entry_idx >= n - recency:
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
            logger.info(f"PROCESSING PUT: {ticker}/{tf}/{strategy.get('name','?')} idx={idx} bias={self.ema_bias.current_bias} cat={cat}")
            if not self.ema_bias.check_alignment('PUT', cat):
                self.stats['blocked_ema_bias'] += 1
                logger.info(f"EMA BLOCK: {ticker} PUT — bias={self.ema_bias.current_bias} cat={cat}")
                continue
            filtered = apply_v2_filters(df, np.array([idx]), 'PUT', strategy,
                                        htf_trend=htf_trend_ref, v2_config=V2_FILTERS)
            logger.info(f"V2 FILTER: {ticker} PUT — {len(filtered)} passed out of 1")
            for entry_idx, stop_pct, target_pct in filtered:
                if entry_idx >= n - recency:
                    self._execute_signal(
                        ticker, 'PUT', df, entry_idx, strategy,
                        stop_pct, target_pct, current_prices,
                        sweep_bull, sweep_bear, df_5m,
                    )

    # ── Execute Signal ──

    def _execute_signal(self, ticker, direction, df, entry_idx, strategy,
                        stop_pct, target_pct, current_prices,
                        sweep_bull, sweep_bear, df_5m):
        """Execute a signal through the full V3 master filter pipeline."""
        # Dedup: don't process same signal twice
        sig_key = f"{ticker}_{strategy.get('name','')}_{direction}_{entry_idx}"
        if not hasattr(self, '_processed_signals'):
            self._processed_signals = set()
        if sig_key in self._processed_signals:
            return
        self._processed_signals.add(sig_key)
        # Trim old entries to prevent memory leak
        if len(self._processed_signals) > 500:
            self._processed_signals = set(list(self._processed_signals)[-200:])

        entry_price = current_prices.get(ticker)
        if entry_price is None:
            return

        # V3 MASTER FILTER — LOGGED version (captures every filter step)
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

            opt_info = ""
            if trade.option_symbol:
                opt_info = (f" | OPT: {trade.contracts}x {trade.option_symbol} "
                           f"@ ${trade.option_entry_premium:.2f}")
            logger.info(
                f"NEW TRADE: {trade.trade_id} | {direction} {ticker} @ ${entry_price:.2f} | "
                f"SL=${trade.stop_price:.2f} TP=${trade.target_price:.2f}{opt_info} | "
                f"Module={strategy['module']} | Mode={strategy['mode']} | "
                f"Strategy={strategy['name']} ({strategy['backtest_wr']:.1f}% WR)"
            )
        else:
            # Signal passed all filters but couldn't be traded (risk limits, etc.)
            self.trade_logger.log_entry_skipped(
                ticker, strategy, direction, 'position_manager_rejected',
                context=entry_context,
            )

    # ── Shutdown ──

    def _shutdown(self, current_prices):
        """Graceful shutdown with full trade analysis."""
        logger.info("Shutting down...")

        # Stop WebSocket first
        self.data_feed.shutdown()

        if self.pos_mgr.open_trades:
            logger.info(f"Closing {len(self.pos_mgr.open_trades)} open positions...")
            self.pos_mgr.force_close_all(current_prices, reason='SHUTDOWN')

        self.pos_mgr.save_trades('trades.json')

        # ══════ SAVE TRADE LOGS ══════
        log_file = self.trade_logger.save_session()
        logger.info(f"Trade logs saved: {log_file}")

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
            logger.info("=" * 70)
            logger.info("POST-SESSION ANALYSIS")
            logger.info("=" * 70)
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

        summary = self.pos_mgr.get_summary()
        logger.info("=" * 70)
        logger.info("SESSION SUMMARY")
        logger.info(f"  Trades: {summary['trades_today']}")
        logger.info(f"  Win Rate: {summary['win_rate']}%")
        logger.info(f"  Daily PnL: ${summary['daily_pnl']:.2f}")
        logger.info(f"  Stats: {dict(self.stats)}")
        logger.info(f"  Logger stats: {self.trade_logger.get_session_stats()}")
        logger.info("=" * 70)


# ============================================================
# ENTRY POINT
# ============================================================

if __name__ == '__main__':
    parser = argparse.ArgumentParser(description='Master Trading Engine')
    parser.add_argument('--live', action='store_true', help='Live trading mode')
    parser.add_argument('--log-level', default='INFO', help='Log level')
    args = parser.parse_args()

    setup_logging(args.log_level)

    engine = TradingEngine(paper=not args.live)
    engine.start()
