"""
TRADE LOGGER — Captures Every Decision Point in a Trade's Lifecycle
=====================================================================
Records the full story of every signal: from detection → filter pipeline
→ entry → position management → exit. Also captures signals that were
BLOCKED and why, which is often more valuable than the winners.

Log structure per trade:
  1. SIGNAL_DETECTED  — raw signal fired (strategy, ticker, TF, direction)
  2. FILTER_RESULT    — each filter in the V3 pipeline (pass/fail + values)
  3. ENTRY_DECISION   — final go/no-go, refined price, sizing
  4. POSITION_UPDATE  — every tick: price, PnL, MFE, MAE, trailing stop
  5. SCALE_EVENT      — TP1 hit, TP2 hit, trail adjustment
  6. EXIT             — reason, final PnL, hold time, MFE/MAE
  7. CONTEXT_SNAPSHOT — market state at entry and exit (regime, bias, ADX, RSI, etc.)

Also captures:
  - BLOCKED signals (why they were rejected — critical for finding filter bugs)
  - MISSED signals (signal fired, filter passed, but no trade due to risk limits)
  - MARKET_STATE changes (mode transitions, bias flips)
"""

import json
import time
import os
import threading
import numpy as np
import pandas as pd
from datetime import datetime
from typing import Dict, List, Optional, Any
from collections import defaultdict
from dataclasses import dataclass, field, asdict


# ============================================================
# EVENT TYPES
# ============================================================

class EventType:
    SIGNAL_DETECTED = 'SIGNAL_DETECTED'
    FILTER_PASS = 'FILTER_PASS'
    FILTER_BLOCK = 'FILTER_BLOCK'
    ENTRY_EXECUTED = 'ENTRY_EXECUTED'
    ENTRY_SKIPPED = 'ENTRY_SKIPPED'
    POSITION_UPDATE = 'POSITION_UPDATE'
    SCALE_EVENT = 'SCALE_EVENT'
    EXIT = 'EXIT'
    CONTEXT_SNAPSHOT = 'CONTEXT_SNAPSHOT'
    MODE_CHANGE = 'MODE_CHANGE'
    BIAS_CHANGE = 'BIAS_CHANGE'
    RISK_LIMIT = 'RISK_LIMIT'
    ERROR = 'ERROR'


# ============================================================
# TRADE LOG ENTRY
# ============================================================

@dataclass
class TradeLogEntry:
    """Single event in a trade's lifecycle."""
    timestamp: float
    event_type: str
    trade_id: Optional[str]
    ticker: str
    strategy: str
    timeframe: str
    direction: str
    data: Dict[str, Any]

    def to_dict(self):
        d = asdict(self)
        d['timestamp_iso'] = datetime.fromtimestamp(self.timestamp).isoformat()
        return d


# ============================================================
# TRADE JOURNAL — Full lifecycle of one trade
# ============================================================

@dataclass
class TradeJournal:
    """Complete lifecycle record for one trade."""
    trade_id: str
    ticker: str
    strategy: str
    strategy_config: Dict
    timeframe: str
    direction: str
    module: str
    category: str

    # Signal
    signal_time: float = 0
    signal_bar_idx: int = 0

    # Filter pipeline results (ordered)
    filter_results: List[Dict] = field(default_factory=list)
    filters_passed: int = 0
    filters_total: int = 0
    blocked_by: Optional[str] = None

    # Entry
    entry_time: float = 0
    entry_price: float = 0
    raw_entry_price: float = 0
    refined_entry_price: Optional[float] = None
    position_size: float = 0

    # Context at entry
    entry_context: Dict = field(default_factory=dict)

    # Position tracking
    ticks: List[Dict] = field(default_factory=list)
    mfe: float = 0  # Max Favorable Excursion (%)
    mae: float = 0  # Max Adverse Excursion (%)
    mfe_time: float = 0
    mae_time: float = 0

    # Scale events
    scale_events: List[Dict] = field(default_factory=list)
    tp1_hit: bool = False
    tp1_time: float = 0

    # Exit
    exit_time: float = 0
    exit_price: float = 0
    exit_reason: str = ''
    pnl: float = 0
    pnl_pct: float = 0
    hold_bars: int = 0
    hold_seconds: float = 0

    # Context at exit
    exit_context: Dict = field(default_factory=dict)

    # Outcome classification
    outcome: str = ''  # WIN, LOSS, BREAKEVEN, STOPPED, TP1_ONLY, FULL_RUNNER

    def to_dict(self):
        d = asdict(self)
        # Trim ticks to summary (keep first, last, MFE point, MAE point)
        if len(self.ticks) > 10:
            key_ticks = [
                self.ticks[0],
                self.ticks[-1],
            ]
            # Find MFE and MAE ticks
            if self.ticks:
                mfe_tick = max(self.ticks, key=lambda t: t.get('unrealized_pnl_pct', 0))
                mae_tick = min(self.ticks, key=lambda t: t.get('unrealized_pnl_pct', 0))
                key_ticks.extend([mfe_tick, mae_tick])
            # Sample every Nth tick
            step = max(1, len(self.ticks) // 10)
            sampled = self.ticks[::step]
            d['ticks_summary'] = key_ticks
            d['ticks_sampled'] = sampled
            d['total_ticks'] = len(self.ticks)
            del d['ticks']
        return d


# ============================================================
# TRADE LOGGER — Main logging engine
# ============================================================

class TradeLogger:
    """
    Captures every decision point in the trading pipeline.
    Thread-safe for use with WebSocket callbacks.
    """

    def __init__(self, log_dir: str = 'trade_logs', max_blocked_per_strategy: int = 100):
        self.log_dir = log_dir
        os.makedirs(log_dir, exist_ok=True)

        self._lock = threading.Lock()

        # Active trade journals (trade_id → TradeJournal)
        self.active_trades: Dict[str, TradeJournal] = {}

        # Completed trade journals
        self.completed_trades: List[TradeJournal] = []

        # Blocked signal log (strategy → list of block events)
        self.blocked_signals: Dict[str, List[Dict]] = defaultdict(list)
        self.max_blocked = max_blocked_per_strategy

        # Missed signals (passed filters but couldn't trade)
        self.missed_signals: List[Dict] = []

        # Market state transitions
        self.state_transitions: List[Dict] = []

        # Error log
        self.errors: List[Dict] = []

        # Session stats
        self.session_start = time.time()
        self.stats = defaultdict(int)

        # Daily file
        self._today = datetime.now().strftime('%Y-%m-%d')
        self._events_file = os.path.join(log_dir, f'events_{self._today}.jsonl')
        self._trades_file = os.path.join(log_dir, f'trades_{self._today}.json')

    # ── Signal Detection ──

    def log_signal_detected(self, ticker: str, strategy: Dict, direction: str,
                            bar_idx: int, timeframe: str, raw_values: Dict = None):
        """Log when a raw signal fires (before any filtering)."""
        with self._lock:
            self.stats['signals_detected'] += 1
            event = TradeLogEntry(
                timestamp=time.time(),
                event_type=EventType.SIGNAL_DETECTED,
                trade_id=None,
                ticker=ticker,
                strategy=strategy.get('name', 'unknown'),
                timeframe=timeframe,
                direction=direction,
                data={
                    'bar_idx': bar_idx,
                    'signal_func': strategy.get('signal_func', ''),
                    'module': strategy.get('module', ''),
                    'category': strategy.get('category', ''),
                    'raw_values': raw_values or {},
                },
            )
            self._write_event(event)
            return event

    # ── Filter Pipeline ──

    def log_filter_result(self, ticker: str, strategy: Dict, direction: str,
                          filter_name: str, passed: bool, values: Dict = None,
                          trade_id: str = None):
        """Log each individual filter result in the V3 pipeline."""
        with self._lock:
            event_type = EventType.FILTER_PASS if passed else EventType.FILTER_BLOCK
            self.stats[f'filter_{filter_name}_{"pass" if passed else "block"}'] += 1

            event = TradeLogEntry(
                timestamp=time.time(),
                event_type=event_type,
                trade_id=trade_id,
                ticker=ticker,
                strategy=strategy.get('name', 'unknown'),
                timeframe=strategy.get('timeframe', ''),
                direction=direction,
                data={
                    'filter': filter_name,
                    'passed': passed,
                    'values': values or {},
                },
            )
            self._write_event(event)
            return event

    def log_filter_pipeline(self, ticker: str, strategy: Dict, direction: str,
                            pipeline_results: List[Dict], final_passed: bool,
                            blocked_by: str = None):
        """
        Log the COMPLETE filter pipeline for one signal.
        pipeline_results: [{'filter': 'kill_zone', 'passed': True, 'values': {...}}, ...]
        """
        with self._lock:
            if final_passed:
                self.stats['signals_passed_all_filters'] += 1
            else:
                self.stats['signals_blocked'] += 1
                # Track which filter blocks the most
                if blocked_by:
                    self.stats[f'blocked_by_{blocked_by}'] += 1
                    # Keep blocked signal detail
                    key = strategy.get('name', 'unknown')
                    block_record = {
                        'time': time.time(),
                        'ticker': ticker,
                        'direction': direction,
                        'blocked_by': blocked_by,
                        'pipeline': pipeline_results,
                    }
                    self.blocked_signals[key].append(block_record)
                    if len(self.blocked_signals[key]) > self.max_blocked:
                        self.blocked_signals[key] = self.blocked_signals[key][-self.max_blocked:]

    # ── Entry ──

    def log_entry(self, trade_id: str, ticker: str, strategy: Dict, direction: str,
                  entry_price: float, raw_price: float, refined_price: float = None,
                  position_size: float = 0, context: Dict = None,
                  filter_pipeline: List[Dict] = None):
        """Log trade entry — creates the TradeJournal."""
        with self._lock:
            self.stats['trades_entered'] += 1

            journal = TradeJournal(
                trade_id=trade_id,
                ticker=ticker,
                strategy=strategy.get('name', 'unknown'),
                strategy_config=_safe_strategy_dict(strategy),
                timeframe=strategy.get('timeframe', ''),
                direction=direction,
                module=strategy.get('module', ''),
                category=strategy.get('category', ''),
                signal_time=time.time(),
                entry_time=time.time(),
                entry_price=entry_price,
                raw_entry_price=raw_price,
                refined_entry_price=refined_price,
                position_size=position_size,
                entry_context=context or {},
                filter_results=filter_pipeline or [],
                filters_passed=sum(1 for f in (filter_pipeline or []) if f.get('passed')),
                filters_total=len(filter_pipeline or []),
            )

            self.active_trades[trade_id] = journal

            event = TradeLogEntry(
                timestamp=time.time(),
                event_type=EventType.ENTRY_EXECUTED,
                trade_id=trade_id,
                ticker=ticker,
                strategy=strategy.get('name', 'unknown'),
                timeframe=strategy.get('timeframe', ''),
                direction=direction,
                data={
                    'entry_price': entry_price,
                    'raw_price': raw_price,
                    'refined_price': refined_price,
                    'position_size': position_size,
                    'stop_pct': strategy.get('stop_pct', 0),
                    'target_pct': strategy.get('target_pct', 0),
                    'context': context or {},
                },
            )
            self._write_event(event)
            return journal

    def log_entry_skipped(self, ticker: str, strategy: Dict, direction: str,
                          reason: str, context: Dict = None):
        """Log when a signal passed all filters but couldn't be traded."""
        with self._lock:
            self.stats['entries_skipped'] += 1
            self.stats[f'skip_reason_{reason}'] += 1
            self.missed_signals.append({
                'time': time.time(),
                'ticker': ticker,
                'strategy': strategy.get('name', 'unknown'),
                'direction': direction,
                'reason': reason,
                'context': context or {},
            })

    # ── Position Updates ──

    def log_position_update(self, trade_id: str, current_price: float,
                            unrealized_pnl: float = 0, unrealized_pnl_pct: float = 0):
        """Log a position tick (called on every price update)."""
        with self._lock:
            journal = self.active_trades.get(trade_id)
            if not journal:
                return

            tick = {
                'time': time.time(),
                'price': current_price,
                'unrealized_pnl': unrealized_pnl,
                'unrealized_pnl_pct': unrealized_pnl_pct,
            }
            journal.ticks.append(tick)

            # Update MFE / MAE
            if unrealized_pnl_pct > journal.mfe:
                journal.mfe = unrealized_pnl_pct
                journal.mfe_time = time.time()
            if unrealized_pnl_pct < journal.mae:
                journal.mae = unrealized_pnl_pct
                journal.mae_time = time.time()

    # ── Scale Events ──

    def log_scale_event(self, trade_id: str, action: str, reason: str,
                        price: float, pnl_pct: float = 0):
        """Log TP1, TP2, trailing stop adjustments."""
        with self._lock:
            journal = self.active_trades.get(trade_id)
            if not journal:
                return

            event_data = {
                'time': time.time(),
                'action': action,
                'reason': reason,
                'price': price,
                'pnl_pct': pnl_pct,
            }
            journal.scale_events.append(event_data)

            if reason == 'TP1':
                journal.tp1_hit = True
                journal.tp1_time = time.time()
                self.stats['tp1_hits'] += 1

            self.stats[f'scale_{action}'] += 1

            event = TradeLogEntry(
                timestamp=time.time(),
                event_type=EventType.SCALE_EVENT,
                trade_id=trade_id,
                ticker=journal.ticker,
                strategy=journal.strategy,
                timeframe=journal.timeframe,
                direction=journal.direction,
                data=event_data,
            )
            self._write_event(event)

    # ── Exit ──

    def log_exit(self, trade_id: str, exit_price: float, exit_reason: str,
                 pnl: float, pnl_pct: float, hold_bars: int = 0,
                 exit_context: Dict = None):
        """Log trade exit — finalizes the TradeJournal."""
        with self._lock:
            journal = self.active_trades.get(trade_id)
            if not journal:
                return None

            journal.exit_time = time.time()
            journal.exit_price = exit_price
            journal.exit_reason = exit_reason
            journal.pnl = pnl
            journal.pnl_pct = pnl_pct
            journal.hold_bars = hold_bars
            journal.hold_seconds = journal.exit_time - journal.entry_time
            journal.exit_context = exit_context or {}

            # Classify outcome
            if pnl_pct > 0:
                if journal.tp1_hit and exit_reason in ('TP2', 'TRAIL'):
                    journal.outcome = 'FULL_RUNNER'
                elif journal.tp1_hit:
                    journal.outcome = 'TP1_ONLY'
                else:
                    journal.outcome = 'WIN'
            elif pnl_pct < -0.02:
                journal.outcome = 'LOSS'
            else:
                journal.outcome = 'BREAKEVEN'

            # Additional classification
            if exit_reason == 'STOP':
                journal.outcome = 'STOPPED'

            self.stats[f'outcome_{journal.outcome.lower()}'] += 1

            # Move to completed
            self.completed_trades.append(journal)
            del self.active_trades[trade_id]

            event = TradeLogEntry(
                timestamp=time.time(),
                event_type=EventType.EXIT,
                trade_id=trade_id,
                ticker=journal.ticker,
                strategy=journal.strategy,
                timeframe=journal.timeframe,
                direction=journal.direction,
                data={
                    'exit_price': exit_price,
                    'exit_reason': exit_reason,
                    'pnl': pnl,
                    'pnl_pct': pnl_pct,
                    'mfe': journal.mfe,
                    'mae': journal.mae,
                    'hold_seconds': journal.hold_seconds,
                    'hold_bars': hold_bars,
                    'outcome': journal.outcome,
                    'tp1_hit': journal.tp1_hit,
                    'entry_price': journal.entry_price,
                    'context_at_exit': exit_context or {},
                },
            )
            self._write_event(event)
            return journal

    # ── Market State Changes ──

    def log_mode_change(self, old_mode: str, new_mode: str, ticker: str,
                        adx: float = 0, has_sweep: bool = False):
        """Log market mode transition."""
        with self._lock:
            if old_mode == new_mode:
                return
            self.stats['mode_changes'] += 1
            record = {
                'time': time.time(),
                'old_mode': old_mode,
                'new_mode': new_mode,
                'ticker': ticker,
                'adx': adx,
                'has_sweep': has_sweep,
            }
            self.state_transitions.append(record)

            event = TradeLogEntry(
                timestamp=time.time(),
                event_type=EventType.MODE_CHANGE,
                trade_id=None,
                ticker=ticker,
                strategy='',
                timeframe='',
                direction='',
                data=record,
            )
            self._write_event(event)

    def log_bias_change(self, old_bias: str, new_bias: str, ema_val: float = 0,
                        close: float = 0):
        """Log EMA bias flip."""
        with self._lock:
            if old_bias == new_bias:
                return
            self.stats['bias_changes'] += 1
            record = {
                'time': time.time(),
                'old_bias': old_bias,
                'new_bias': new_bias,
                'ema_val': ema_val,
                'close': close,
            }
            self.state_transitions.append(record)

    # ── Error Logging ──

    def log_error(self, context: str, error: str, ticker: str = '',
                  strategy: str = '', trade_id: str = None):
        """Log an error in the pipeline."""
        with self._lock:
            self.stats['errors'] += 1
            record = {
                'time': time.time(),
                'context': context,
                'error': error,
                'ticker': ticker,
                'strategy': strategy,
                'trade_id': trade_id,
            }
            self.errors.append(record)

            event = TradeLogEntry(
                timestamp=time.time(),
                event_type=EventType.ERROR,
                trade_id=trade_id,
                ticker=ticker,
                strategy=strategy,
                timeframe='',
                direction='',
                data=record,
            )
            self._write_event(event)

    # ── Context Snapshot ──

    def capture_context(self, df: pd.DataFrame, idx: int, tf_data: Dict = None,
                        extra: Dict = None) -> Dict:
        """Capture market context at a specific moment."""
        ctx = {}
        try:
            if idx >= len(df):
                idx = len(df) - 1

            ctx['close'] = float(df['Close'].iloc[idx])
            ctx['high'] = float(df['High'].iloc[idx])
            ctx['low'] = float(df['Low'].iloc[idx])

            for col in ['RSI_14', 'ADX_14', 'ATR_14', 'EMA_9', 'EMA_21', 'EMA_50',
                         'SMA_20', 'BB_Upper', 'BB_Lower', 'VWAP']:
                if col in df.columns:
                    val = df[col].iloc[idx]
                    if pd.notna(val):
                        ctx[col] = round(float(val), 4)

            # SMC state
            for col in ['Bull_OB', 'Bear_OB', 'Bull_FVG', 'Bear_FVG', 'BOS_Bull',
                         'BOS_Bear', 'CHoCH_Bull', 'CHoCH_Bear', 'Displacement',
                         'Bull_Sweep', 'Bear_Sweep']:
                if col in df.columns:
                    ctx[col] = bool(df[col].iloc[idx]) if pd.notna(df[col].iloc[idx]) else False

            # Volume context
            if 'Volume' in df.columns and idx >= 20:
                vol = df['Volume'].values
                ctx['volume'] = float(vol[idx])
                ctx['volume_ratio'] = round(float(vol[idx]) / (np.mean(vol[max(0, idx-20):idx]) + 1e-10), 2)

            # Spread / volatility
            if 'ATR_14' in df.columns and pd.notna(df['ATR_14'].iloc[idx]):
                atr = df['ATR_14'].values
                if idx >= 20:
                    avg_atr = np.mean(atr[max(0, idx-20):idx])
                    ctx['atr_ratio'] = round(float(atr[idx]) / (avg_atr + 1e-10), 3)

            if extra:
                ctx.update(extra)

        except Exception as e:
            ctx['error'] = str(e)

        return ctx

    # ── Persistence ──

    def _write_event(self, event: TradeLogEntry):
        """Append event to daily JSONL file."""
        try:
            with open(self._events_file, 'a') as f:
                f.write(json.dumps(event.to_dict(), default=str) + '\n')
        except Exception:
            pass  # Don't let logging errors kill the engine

    def save_session(self):
        """Save all completed trades and stats to disk."""
        with self._lock:
            data = {
                'session_start': self.session_start,
                'session_end': time.time(),
                'stats': dict(self.stats),
                'completed_trades': [t.to_dict() for t in self.completed_trades],
                'active_trades': [t.to_dict() for t in self.active_trades.values()],
                'blocked_signals_summary': {
                    k: len(v) for k, v in self.blocked_signals.items()
                },
                'missed_signals': self.missed_signals[-50:],
                'state_transitions': self.state_transitions[-100:],
                'errors': self.errors[-50:],
            }

            with open(self._trades_file, 'w') as f:
                json.dump(data, f, indent=2, default=str)

            return self._trades_file

    def get_session_stats(self) -> Dict:
        """Get current session statistics."""
        with self._lock:
            return {
                'session_duration': time.time() - self.session_start,
                'signals_detected': self.stats.get('signals_detected', 0),
                'signals_blocked': self.stats.get('signals_blocked', 0),
                'signals_passed': self.stats.get('signals_passed_all_filters', 0),
                'trades_entered': self.stats.get('trades_entered', 0),
                'entries_skipped': self.stats.get('entries_skipped', 0),
                'active_trades': len(self.active_trades),
                'completed_trades': len(self.completed_trades),
                'errors': self.stats.get('errors', 0),
                'top_blockers': _top_blockers(self.stats),
            }


# ============================================================
# LOGGED V3 FILTER — Wraps v3_master_filter with per-filter logging
# ============================================================

def v3_master_filter_logged(df, idx, direction, strategy, entry_price,
                             sweep_bull=None, sweep_bear=None,
                             vol_regime=None, df_5m=None,
                             logger_inst: TradeLogger = None,
                             ticker: str = '') -> tuple:
    """
    Drop-in replacement for v3_master_filter that logs each filter step.
    Returns (allowed, meta_dict, pipeline_log).
    """
    from v3_filters import (
        kill_zone_check, displacement_confirmation, risk_gate_check,
        volume_confirmation_check, check_ltf_choch, compute_refined_entry,
        V3_CONFIG,
    )

    pipeline = []
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

    def _log_step(name, passed, values=None):
        step = {'filter': name, 'passed': passed, 'values': values or {}}
        pipeline.append(step)
        if logger_inst:
            logger_inst.log_filter_result(ticker, strategy, direction, name, passed, values)

    # 1. Kill Zone
    kz_ok = kill_zone_check(df, idx, cat)
    _log_step('kill_zone', kz_ok, {'category': cat})
    if not kz_ok:
        meta['blocked_by'] = 'kill_zone'
        if logger_inst:
            logger_inst.log_filter_pipeline(ticker, strategy, direction, pipeline, False, 'kill_zone')
        return False, meta, pipeline

    # 2. Volatility Regime
    if vol_regime:
        vol_ok, regime = vol_regime.check(df, idx, cat)
        meta['regime'] = regime
        _log_step('volatility', vol_ok, {'regime': regime, 'category': cat})
        if not vol_ok:
            meta['blocked_by'] = f'volatility_{regime}'
            if logger_inst:
                logger_inst.log_filter_pipeline(ticker, strategy, direction, pipeline, False, f'volatility_{regime}')
            return False, meta, pipeline
    else:
        _log_step('volatility', True, {'note': 'no_vol_regime_provided'})

    # 3. Liquidity Sweep
    sweep_ok = True
    sweep_vals = {}
    if sweep_bull is not None and sweep_bear is not None and idx < len(sweep_bull):
        if direction == 'CALL':
            sweep_ok = bool(sweep_bull[idx])
            sweep_vals = {'sweep_bull': bool(sweep_bull[idx]), 'sweep_bear': bool(sweep_bear[idx])}
        elif direction == 'PUT' and idx < len(sweep_bear):
            sweep_ok = bool(sweep_bear[idx])
            sweep_vals = {'sweep_bull': bool(sweep_bull[idx]), 'sweep_bear': bool(sweep_bear[idx])}
    _log_step('liquidity_sweep', sweep_ok, sweep_vals)
    if not sweep_ok:
        meta['blocked_by'] = 'sweep_required'
        if logger_inst:
            logger_inst.log_filter_pipeline(ticker, strategy, direction, pipeline, False, 'sweep_required')
        return False, meta, pipeline

    # 4. Displacement
    needs_disp = module in ('sweep', 'displacement') or strategy.get('needs_displacement', False)
    if needs_disp:
        disp_ok = displacement_confirmation(df, idx, direction)
        _log_step('displacement', disp_ok, {'module': module})
        if not disp_ok:
            meta['blocked_by'] = 'displacement'
            if logger_inst:
                logger_inst.log_filter_pipeline(ticker, strategy, direction, pipeline, False, 'displacement')
            return False, meta, pipeline
    else:
        _log_step('displacement', True, {'note': 'not_required', 'module': module})

    # 5. RSI Gate
    rsi_ok = risk_gate_check(df, idx, direction)
    rsi_val = float(df['RSI_14'].iloc[idx]) if 'RSI_14' in df.columns else None
    _log_step('rsi_gate', rsi_ok, {'rsi': rsi_val, 'direction': direction})
    if not rsi_ok:
        meta['blocked_by'] = 'rsi_gate'
        if logger_inst:
            logger_inst.log_filter_pipeline(ticker, strategy, direction, pipeline, False, 'rsi_gate')
        return False, meta, pipeline

    # 6. Volume Confirmation
    if strategy.get('needs_volume_confirm', False):
        vol_ok = volume_confirmation_check(df, idx)
        _log_step('volume_confirm', vol_ok)
        if not vol_ok:
            meta['blocked_by'] = 'volume_confirm'
            if logger_inst:
                logger_inst.log_filter_pipeline(ticker, strategy, direction, pipeline, False, 'volume_confirm')
            return False, meta, pipeline
    else:
        _log_step('volume_confirm', True, {'note': 'not_required'})

    # 7. LTF CHoCH
    if strategy.get('needs_ltf_choch', False) and df_5m is not None and 'Date' in df.columns:
        sig_time = df['Date'].iloc[idx]
        choch_ok = check_ltf_choch(df_5m, sig_time, direction)
        _log_step('ltf_choch', choch_ok, {'signal_time': str(sig_time)})
        if not choch_ok:
            meta['blocked_by'] = 'ltf_choch'
            if logger_inst:
                logger_inst.log_filter_pipeline(ticker, strategy, direction, pipeline, False, 'ltf_choch')
            return False, meta, pipeline
    else:
        _log_step('ltf_choch', True, {'note': 'not_required'})

    # 8. Entry Refinement
    max_dev = V3_CONFIG.get('entry_refinement', {}).get('max_deviation_pct', 0.005)
    refined = compute_refined_entry(df, idx, direction, sig)
    refinement_applied = False
    if refined is not None and refined > 0:
        diff = abs(refined - entry_price) / entry_price
        if diff < max_dev:
            meta['refined_entry'] = refined
            refinement_applied = True
    _log_step('entry_refinement', True, {
        'refined_price': refined,
        'raw_price': entry_price,
        'applied': refinement_applied,
    })

    # ALL PASSED
    if logger_inst:
        logger_inst.log_filter_pipeline(ticker, strategy, direction, pipeline, True)

    return True, meta, pipeline


# ============================================================
# HELPERS
# ============================================================

def _safe_strategy_dict(strategy: Dict) -> Dict:
    """Make strategy config JSON-serializable."""
    safe = {}
    for k, v in strategy.items():
        try:
            json.dumps(v)
            safe[k] = v
        except (TypeError, ValueError):
            safe[k] = str(v)
    return safe


def _top_blockers(stats: Dict, top_n: int = 5) -> List[Dict]:
    """Extract top N blocking filters from stats."""
    blockers = [(k.replace('blocked_by_', ''), v) for k, v in stats.items()
                if k.startswith('blocked_by_')]
    blockers.sort(key=lambda x: x[1], reverse=True)
    return [{'filter': k, 'count': v} for k, v in blockers[:top_n]]
