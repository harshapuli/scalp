"""
TRADE ANALYZER — Post-Trade Analysis + Improvement Engine
===========================================================
Reads TradeJournal data and produces actionable insights:

  1. WHAT WENT WELL — Winning pattern detection
     - Which strategies/TFs/sessions have edge
     - Filter pipeline effectiveness (which filters save the most money)
     - Optimal entry refinement impact
     - MFE analysis: are targets too conservative?

  2. WHAT WENT WRONG — Loss forensics
     - Stop-outs: too tight? too wide? (MAE vs stop distance)
     - Filter leaks: which filter SHOULD have blocked but didn't?
     - Signal quality: was the signal itself bad, or did execution fail?
     - Timing: entered too early/late relative to OB zone?
     - Regime mismatch: traded against the dominant trend?

  3. IMPROVEMENTS — Concrete parameter suggestions
     - Stop/target recalibration from MFE/MAE distributions
     - Filter threshold tuning (RSI gate, ADX thresholds, etc.)
     - Strategy-specific diagnostics
     - Session/kill zone refinement

  4. PATTERN RECOGNITION — Cross-trade analysis
     - Losing streaks: what caused them?
     - Correlation with VIX/regime/time-of-day
     - Strategy interaction: do certain combos conflict?
"""

import json
import time
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional, Tuple
from collections import defaultdict
from dataclasses import dataclass, field


# ============================================================
# ANALYSIS RESULT STRUCTURES
# ============================================================

@dataclass
class TradeAnalysis:
    """Analysis of a single completed trade."""
    trade_id: str
    ticker: str
    strategy: str
    direction: str
    outcome: str
    pnl_pct: float

    # Verdicts
    signal_quality: str = ''     # GOOD, WEAK, FALSE
    entry_quality: str = ''      # OPTIMAL, EARLY, LATE, CHASED
    exit_quality: str = ''       # OPTIMAL, PREMATURE, LATE, STOPPED_TIGHT, STOPPED_WIDE
    filter_quality: str = ''     # CORRECT, MISSED_BLOCK, OVER_FILTERED

    # Key findings
    findings: List[str] = field(default_factory=list)
    improvements: List[str] = field(default_factory=list)
    severity: str = 'INFO'       # INFO, WARNING, CRITICAL

    def to_dict(self):
        return {
            'trade_id': self.trade_id,
            'ticker': self.ticker,
            'strategy': self.strategy,
            'direction': self.direction,
            'outcome': self.outcome,
            'pnl_pct': self.pnl_pct,
            'signal_quality': self.signal_quality,
            'entry_quality': self.entry_quality,
            'exit_quality': self.exit_quality,
            'filter_quality': self.filter_quality,
            'findings': self.findings,
            'improvements': self.improvements,
            'severity': self.severity,
        }


@dataclass
class SessionAnalysis:
    """Analysis of an entire trading session."""
    session_date: str
    total_trades: int = 0
    wins: int = 0
    losses: int = 0
    breakevens: int = 0
    win_rate: float = 0
    total_pnl: float = 0
    avg_win: float = 0
    avg_loss: float = 0
    profit_factor: float = 0
    max_drawdown: float = 0
    best_trade: Optional[Dict] = None
    worst_trade: Optional[Dict] = None

    # By strategy
    strategy_breakdown: Dict = field(default_factory=dict)
    # By timeframe
    timeframe_breakdown: Dict = field(default_factory=dict)
    # By hour
    hourly_breakdown: Dict = field(default_factory=dict)

    # Aggregate findings
    top_issues: List[str] = field(default_factory=list)
    top_wins: List[str] = field(default_factory=list)
    improvements: List[str] = field(default_factory=list)

    # Filter effectiveness
    filter_stats: Dict = field(default_factory=dict)


# ============================================================
# TRADE ANALYZER
# ============================================================

class TradeAnalyzer:
    """
    Analyzes completed trades to find patterns, diagnose losses,
    and suggest concrete improvements.
    """

    def __init__(self):
        self.analyses: List[TradeAnalysis] = []

    # ── Single Trade Analysis ──

    def analyze_trade(self, journal: Dict) -> TradeAnalysis:
        """Deep analysis of one completed trade."""
        analysis = TradeAnalysis(
            trade_id=journal.get('trade_id', 'unknown'),
            ticker=journal.get('ticker', ''),
            strategy=journal.get('strategy', ''),
            direction=journal.get('direction', ''),
            outcome=journal.get('outcome', ''),
            pnl_pct=journal.get('pnl_pct', 0),
        )

        # ── Signal Quality ──
        analysis.signal_quality = self._assess_signal_quality(journal)

        # ── Entry Quality ──
        analysis.entry_quality = self._assess_entry_quality(journal)

        # ── Exit Quality ──
        analysis.exit_quality = self._assess_exit_quality(journal)

        # ── Filter Quality ──
        analysis.filter_quality = self._assess_filter_quality(journal)

        # ── Generate Findings ──
        self._generate_findings(journal, analysis)

        # ── Generate Improvements ──
        self._generate_improvements(journal, analysis)

        # ── Severity ──
        if analysis.pnl_pct < -0.5:
            analysis.severity = 'CRITICAL'
        elif analysis.pnl_pct < -0.1:
            analysis.severity = 'WARNING'
        elif analysis.outcome == 'FULL_RUNNER':
            analysis.severity = 'SUCCESS'

        self.analyses.append(analysis)
        return analysis

    def _assess_signal_quality(self, j: Dict) -> str:
        """Was the signal itself valid?"""
        mfe = j.get('mfe', 0)
        mae = j.get('mae', 0)
        target = j.get('strategy_config', {}).get('target_pct', 0.3)

        # If MFE reached target, signal was good regardless of outcome
        if mfe >= target:
            return 'GOOD'
        # If MFE reached at least 50% of target
        if mfe >= target * 0.5:
            return 'WEAK'
        # Price never moved in our direction meaningfully
        if mfe < target * 0.2:
            return 'FALSE'
        return 'WEAK'

    def _assess_entry_quality(self, j: Dict) -> str:
        """Was the entry well-timed?"""
        mae = abs(j.get('mae', 0))
        stop = j.get('strategy_config', {}).get('stop_pct', 0.2)
        entry_price = j.get('entry_price', 0)
        refined = j.get('refined_entry_price')

        # If MAE was very small, entry timing was great
        if mae < stop * 0.3:
            return 'OPTIMAL'

        # If we used refined entry and MAE was still big, we entered too early
        if refined and mae > stop * 0.7:
            return 'EARLY'

        # If MAE nearly hit stop, entry was late (chased)
        if mae > stop * 0.9:
            return 'CHASED'

        if mae > stop * 0.5:
            return 'LATE'

        return 'ACCEPTABLE'

    def _assess_exit_quality(self, j: Dict) -> str:
        """Was the exit optimal?"""
        mfe = j.get('mfe', 0)
        pnl = j.get('pnl_pct', 0)
        exit_reason = j.get('exit_reason', '')
        target = j.get('strategy_config', {}).get('target_pct', 0.3)
        stop = j.get('strategy_config', {}).get('stop_pct', 0.2)

        # Left too much on the table
        if mfe > target * 2 and pnl < mfe * 0.5:
            return 'PREMATURE'

        # Caught full runner
        if j.get('outcome') == 'FULL_RUNNER':
            return 'OPTIMAL'

        # Stopped out but MFE was well past target
        if exit_reason == 'STOP' and mfe > target:
            return 'LATE'  # Should have taken profits

        # Stop was too tight
        if exit_reason == 'STOP' and mfe > stop * 0.5 and pnl < 0:
            return 'STOPPED_TIGHT'

        # Stop was too wide (big loss, MFE was negligible)
        if exit_reason == 'STOP' and mfe < target * 0.2:
            return 'STOPPED_WIDE'

        if pnl > 0:
            return 'ACCEPTABLE'

        return 'SUBOPTIMAL'

    def _assess_filter_quality(self, j: Dict) -> str:
        """Did the filters do their job?"""
        outcome = j.get('outcome', '')
        signal_quality = j.get('signal_quality', self._assess_signal_quality(j))
        filters = j.get('filter_results', [])

        # If signal was FALSE and filters let it through → filter leak
        if signal_quality == 'FALSE' and outcome in ('LOSS', 'STOPPED'):
            return 'MISSED_BLOCK'

        # If trade was profitable → filters correctly allowed it
        if outcome in ('WIN', 'FULL_RUNNER', 'TP1_ONLY'):
            return 'CORRECT'

        return 'CORRECT'  # Losses happen even with good filtering

    def _generate_findings(self, j: Dict, analysis: TradeAnalysis):
        """Generate human-readable findings for this trade."""
        findings = []
        mfe = j.get('mfe', 0)
        mae = abs(j.get('mae', 0))
        pnl = j.get('pnl_pct', 0)
        hold = j.get('hold_seconds', 0)
        target = j.get('strategy_config', {}).get('target_pct', 0.3)
        stop = j.get('strategy_config', {}).get('stop_pct', 0.2)
        exit_reason = j.get('exit_reason', '')

        # MFE vs actual PnL
        if mfe > 0 and pnl < mfe * 0.3 and mfe > target * 0.5:
            left_on_table = mfe - pnl
            findings.append(
                f"Left {left_on_table:.2f}% on the table. MFE reached {mfe:.2f}% "
                f"but exited at {pnl:.2f}%. Consider tighter trailing stop."
            )

        # Stop-out analysis
        if exit_reason == 'STOP':
            if mfe > target * 0.8:
                findings.append(
                    f"STOP-OUT after hitting {mfe:.2f}% MFE (target was {target:.2f}%). "
                    f"TP1 should have triggered. Check scaling manager."
                )
            elif mfe < stop * 0.3:
                findings.append(
                    f"Immediate reversal — price never moved favorably (MFE={mfe:.2f}%). "
                    f"Signal quality was poor. Consider additional confirmation."
                )
            else:
                findings.append(
                    f"Stopped at {mae:.2f}% adverse with stop at {stop:.2f}%. "
                    f"MFE was {mfe:.2f}%. {'Stop too tight.' if mae < stop * 1.1 and mfe > stop else 'Normal stop-out.'}"
                )

        # Entry timing
        if analysis.entry_quality == 'CHASED':
            findings.append(
                f"Entry was chased — MAE of {mae:.2f}% nearly hit {stop:.2f}% stop. "
                f"Price pulled back significantly after entry. Wait for OB 50% retrace."
            )

        # Refinement impact
        if j.get('refined_entry_price') and j.get('raw_entry_price'):
            raw = j['raw_entry_price']
            refined = j['refined_entry_price']
            saving = abs(refined - raw) / raw * 100
            if saving > 0.05:
                findings.append(
                    f"Entry refinement saved {saving:.2f}% "
                    f"(raw={raw:.2f} → refined={refined:.2f})."
                )

        # Hold time
        if hold > 0:
            max_hold = j.get('strategy_config', {}).get('hold_bars', 15)
            if exit_reason == 'MAX_HOLD':
                findings.append(
                    f"Hit max hold ({max_hold} bars). "
                    f"PnL at exit: {pnl:.2f}%. Consider extending hold for this setup."
                )

        # TP1 analysis
        if j.get('tp1_hit') and exit_reason != 'TP2':
            findings.append(
                f"TP1 hit but runner didn't reach TP2. "
                f"MFE after TP1: {mfe:.2f}%. "
                f"{'Runner strategy working.' if pnl > 0 else 'Trail stop caught it — tighten trail?'}"
            )

        # Context analysis
        ctx = j.get('entry_context', {})
        if ctx.get('RSI_14'):
            rsi = ctx['RSI_14']
            if j.get('direction') == 'CALL' and rsi > 65:
                findings.append(f"Entered CALL with RSI at {rsi:.1f} — close to overbought territory.")
            elif j.get('direction') == 'PUT' and rsi < 35:
                findings.append(f"Entered PUT with RSI at {rsi:.1f} — close to oversold territory.")

        if ctx.get('atr_ratio'):
            atr_r = ctx['atr_ratio']
            if atr_r > 1.8:
                findings.append(f"ATR ratio was {atr_r:.2f}x — high volatility. Wider stops may be needed.")
            elif atr_r < 0.6:
                findings.append(f"ATR ratio was {atr_r:.2f}x — low volatility. Tighter targets may work better.")

        analysis.findings = findings

    def _generate_improvements(self, j: Dict, analysis: TradeAnalysis):
        """Generate concrete improvement suggestions."""
        improvements = []
        mfe = j.get('mfe', 0)
        mae = abs(j.get('mae', 0))
        pnl = j.get('pnl_pct', 0)
        target = j.get('strategy_config', {}).get('target_pct', 0.3)
        stop = j.get('strategy_config', {}).get('stop_pct', 0.2)
        strategy = j.get('strategy', '')

        # Stop calibration
        if analysis.exit_quality == 'STOPPED_TIGHT':
            new_stop = round(mae * 1.2, 3)
            improvements.append({
                'type': 'STOP_ADJUSTMENT',
                'strategy': strategy,
                'current': stop,
                'suggested': new_stop,
                'reason': f'MAE of {mae:.2f}% frequently exceeds stop of {stop:.2f}%. '
                          f'Widen to {new_stop:.2f}% to survive normal pullbacks.',
                'confidence': 'MEDIUM',
            })

        # Target calibration
        if analysis.exit_quality == 'PREMATURE' and mfe > target * 1.5:
            new_target = round(mfe * 0.7, 3)
            improvements.append({
                'type': 'TARGET_ADJUSTMENT',
                'strategy': strategy,
                'current': target,
                'suggested': new_target,
                'reason': f'MFE reached {mfe:.2f}% but target was only {target:.2f}%. '
                          f'Increase TP1 to {new_target:.2f}%.',
                'confidence': 'HIGH',
            })

        # Signal quality fix
        if analysis.signal_quality == 'FALSE':
            improvements.append({
                'type': 'SIGNAL_FILTER',
                'strategy': strategy,
                'reason': f'False signal — price never moved favorably. '
                          f'Add confirmation: require volume > 1.5x avg at signal bar, '
                          f'or require displacement on prior bar.',
                'confidence': 'HIGH',
            })

        # Entry timing fix
        if analysis.entry_quality in ('CHASED', 'LATE'):
            improvements.append({
                'type': 'ENTRY_REFINEMENT',
                'strategy': strategy,
                'reason': f'Entry was {analysis.entry_quality.lower()} — '
                          f'MAE of {mae:.2f}% suggests chasing. '
                          f'Use limit order at OB 50% retrace instead of market entry.',
                'confidence': 'HIGH',
            })

        # Filter leak
        if analysis.filter_quality == 'MISSED_BLOCK':
            ctx = j.get('entry_context', {})
            improvements.append({
                'type': 'FILTER_ADD',
                'strategy': strategy,
                'reason': f'Signal was false but passed all filters. '
                          f'Context: RSI={ctx.get("RSI_14", "?")}, '
                          f'ATR_ratio={ctx.get("atr_ratio", "?")}, '
                          f'Volume_ratio={ctx.get("volume_ratio", "?")}. '
                          f'Consider tightening sweep requirement or adding BOS confirmation.',
                'confidence': 'MEDIUM',
            })

        analysis.improvements = improvements

    # ── Session-Level Analysis ──

    def analyze_session(self, trades: List[Dict], blocked_signals: Dict = None,
                        stats: Dict = None) -> SessionAnalysis:
        """Analyze an entire trading session."""
        session = SessionAnalysis(
            session_date=datetime.now().strftime('%Y-%m-%d'),
            total_trades=len(trades),
        )

        if not trades:
            session.top_issues = ['No trades executed this session.']
            return session

        # Basic metrics
        pnls = [t.get('pnl_pct', 0) for t in trades]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]

        session.wins = len(wins)
        session.losses = len(losses)
        session.breakevens = len(trades) - session.wins - session.losses
        session.win_rate = round(session.wins / len(trades) * 100, 1) if trades else 0
        session.total_pnl = round(sum(pnls), 4)
        session.avg_win = round(np.mean(wins), 4) if wins else 0
        session.avg_loss = round(np.mean(losses), 4) if losses else 0
        session.profit_factor = round(
            abs(sum(wins)) / (abs(sum(losses)) + 1e-10), 2
        ) if losses else float('inf')

        # Drawdown
        cumulative = np.cumsum(pnls)
        peak = np.maximum.accumulate(cumulative)
        dd = peak - cumulative
        session.max_drawdown = round(float(np.max(dd)) if len(dd) > 0 else 0, 4)

        # Best / worst
        if trades:
            best = max(trades, key=lambda t: t.get('pnl_pct', 0))
            worst = min(trades, key=lambda t: t.get('pnl_pct', 0))
            session.best_trade = {
                'trade_id': best.get('trade_id'), 'strategy': best.get('strategy'),
                'pnl_pct': best.get('pnl_pct'), 'ticker': best.get('ticker'),
            }
            session.worst_trade = {
                'trade_id': worst.get('trade_id'), 'strategy': worst.get('strategy'),
                'pnl_pct': worst.get('pnl_pct'), 'ticker': worst.get('ticker'),
            }

        # Strategy breakdown
        by_strat = defaultdict(list)
        for t in trades:
            by_strat[t.get('strategy', 'unknown')].append(t.get('pnl_pct', 0))
        session.strategy_breakdown = {
            k: {
                'trades': len(v),
                'win_rate': round(sum(1 for p in v if p > 0) / len(v) * 100, 1),
                'total_pnl': round(sum(v), 4),
                'avg_pnl': round(np.mean(v), 4),
            }
            for k, v in by_strat.items()
        }

        # Timeframe breakdown
        by_tf = defaultdict(list)
        for t in trades:
            by_tf[t.get('timeframe', 'unknown')].append(t.get('pnl_pct', 0))
        session.timeframe_breakdown = {
            k: {
                'trades': len(v),
                'win_rate': round(sum(1 for p in v if p > 0) / len(v) * 100, 1),
                'total_pnl': round(sum(v), 4),
            }
            for k, v in by_tf.items()
        }

        # Hourly breakdown (from entry_time)
        by_hour = defaultdict(list)
        for t in trades:
            et = t.get('entry_time', 0)
            if et > 0:
                hr = datetime.fromtimestamp(et).hour
                by_hour[hr].append(t.get('pnl_pct', 0))
        session.hourly_breakdown = {
            k: {
                'trades': len(v),
                'win_rate': round(sum(1 for p in v if p > 0) / len(v) * 100, 1),
                'total_pnl': round(sum(v), 4),
            }
            for k, v in sorted(by_hour.items())
        }

        # Filter effectiveness (from blocked signals)
        if stats:
            filter_counts = {}
            for k, v in stats.items():
                if k.startswith('filter_') and k.endswith('_block'):
                    fname = k.replace('filter_', '').replace('_block', '')
                    filter_counts[fname] = v
            session.filter_stats = filter_counts

        # ── Aggregate Findings ──
        session.top_issues = self._find_session_issues(trades, session)
        session.top_wins = self._find_session_wins(trades, session)
        session.improvements = self._find_session_improvements(trades, session, blocked_signals)

        return session

    def _find_session_issues(self, trades: List[Dict], session: SessionAnalysis) -> List[str]:
        """Find the biggest problems in this session."""
        issues = []

        # Win rate below threshold
        if session.win_rate < 60:
            issues.append(
                f"Win rate {session.win_rate}% is below 60% threshold. "
                f"Possible causes: signal quality degradation, regime mismatch, or over-trading."
            )

        # Profit factor
        if session.profit_factor < 1.5 and session.total_trades > 5:
            issues.append(
                f"Profit factor {session.profit_factor} is below 1.5. "
                f"Average win ({session.avg_win:.2f}%) vs average loss ({session.avg_loss:.2f}%) "
                f"needs improvement. Consider widening targets or tightening stops."
            )

        # Drawdown
        if session.max_drawdown > 1.0:
            issues.append(
                f"Max drawdown of {session.max_drawdown:.2f}% exceeded 1% threshold. "
                f"Review position sizing and concurrent trade limits."
            )

        # Strategy-specific issues
        for strat, data in session.strategy_breakdown.items():
            if data['trades'] >= 3 and data['win_rate'] < 40:
                issues.append(
                    f"Strategy '{strat}' has {data['win_rate']}% win rate over {data['trades']} trades. "
                    f"Consider pausing this strategy or reviewing its filter configuration."
                )

        # Consecutive losses
        pnls = [t.get('pnl_pct', 0) for t in trades]
        max_streak = 0
        current_streak = 0
        for p in pnls:
            if p < 0:
                current_streak += 1
                max_streak = max(max_streak, current_streak)
            else:
                current_streak = 0
        if max_streak >= 3:
            issues.append(
                f"Had {max_streak} consecutive losses. "
                f"Consider adding a circuit breaker that pauses trading after 3 straight losses."
            )

        # MFE waste
        mfe_waste = []
        for t in trades:
            mfe = t.get('mfe', 0)
            pnl = t.get('pnl_pct', 0)
            if mfe > 0 and pnl < mfe * 0.3:
                mfe_waste.append(mfe - pnl)
        if mfe_waste and np.mean(mfe_waste) > 0.1:
            issues.append(
                f"Average of {np.mean(mfe_waste):.2f}% left on table per trade. "
                f"Trail stop may be too loose. Consider trailing at 40% MFE instead of 50%."
            )

        return issues

    def _find_session_wins(self, trades: List[Dict], session: SessionAnalysis) -> List[str]:
        """Find what worked well this session."""
        wins = []

        if session.win_rate >= 70:
            wins.append(f"Excellent win rate of {session.win_rate}%.")

        if session.profit_factor >= 2.0:
            wins.append(f"Strong profit factor of {session.profit_factor}.")

        # Best performing strategy
        best_strat = None
        best_pnl = -float('inf')
        for strat, data in session.strategy_breakdown.items():
            if data['total_pnl'] > best_pnl and data['trades'] >= 2:
                best_strat = strat
                best_pnl = data['total_pnl']
        if best_strat:
            data = session.strategy_breakdown[best_strat]
            wins.append(
                f"'{best_strat}' was the top performer: {data['win_rate']}% WR, "
                f"+{data['total_pnl']:.2f}% total across {data['trades']} trades."
            )

        # Best timeframe
        for tf, data in session.timeframe_breakdown.items():
            if data['trades'] >= 2 and data['win_rate'] >= 80:
                wins.append(f"{tf} timeframe had {data['win_rate']}% win rate.")

        # Full runners
        runners = [t for t in trades if t.get('outcome') == 'FULL_RUNNER']
        if runners:
            wins.append(
                f"{len(runners)} full runner(s) captured. "
                f"Scaling manager is working — TP1 + trail strategy is effective."
            )

        return wins

    def _find_session_improvements(self, trades: List[Dict],
                                    session: SessionAnalysis,
                                    blocked_signals: Dict = None) -> List[str]:
        """Generate concrete session-level improvements."""
        improvements = []

        # Analyze all trades
        for trade in trades:
            analysis = self.analyze_trade(trade)
            for imp in analysis.improvements:
                improvements.append(
                    f"[{imp.get('type', 'GENERAL')}] {imp.get('strategy', '')}: "
                    f"{imp.get('reason', '')} (confidence: {imp.get('confidence', 'LOW')})"
                )

        # Blocked signal analysis
        if blocked_signals:
            total_blocked = sum(len(v) for v in blocked_signals.values())
            if total_blocked > 0 and session.total_trades < 5:
                improvements.append(
                    f"Only {session.total_trades} trades but {total_blocked} signals blocked. "
                    f"Filters may be too aggressive. Review kill_zone and sweep requirements."
                )

            # Which filters block the most
            blocker_counts = defaultdict(int)
            for strat, blocks in blocked_signals.items():
                for b in blocks:
                    blocker_counts[b.get('blocked_by', 'unknown')] += 1
            top_blocker = max(blocker_counts.items(), key=lambda x: x[1]) if blocker_counts else None
            if top_blocker and top_blocker[1] > total_blocked * 0.4:
                improvements.append(
                    f"'{top_blocker[0]}' filter blocked {top_blocker[1]}/{total_blocked} signals "
                    f"({top_blocker[1]/total_blocked*100:.0f}%). "
                    f"Verify this filter's thresholds aren't too restrictive."
                )

        # Deduplicate
        seen = set()
        unique = []
        for imp in improvements:
            key = imp[:80]
            if key not in seen:
                seen.add(key)
                unique.append(imp)

        return unique[:15]  # Top 15 improvements

    # ── Multi-Session Pattern Recognition ──

    def analyze_patterns(self, all_sessions: List[Dict]) -> Dict:
        """Cross-session pattern analysis."""
        patterns = {
            'losing_streaks': [],
            'regime_correlation': {},
            'time_of_day': {},
            'strategy_evolution': {},
            'recommendations': [],
        }

        all_trades = []
        for sess in all_sessions:
            trades = sess.get('completed_trades', [])
            all_trades.extend(trades)

        if len(all_trades) < 10:
            patterns['recommendations'].append(
                'Need at least 10 trades for meaningful pattern analysis.'
            )
            return patterns

        # ── Losing streak analysis ──
        pnls = [t.get('pnl_pct', 0) for t in all_trades]
        streak = 0
        streak_start = 0
        for i, p in enumerate(pnls):
            if p < 0:
                if streak == 0:
                    streak_start = i
                streak += 1
            else:
                if streak >= 3:
                    streak_trades = all_trades[streak_start:streak_start + streak]
                    strategies = [t.get('strategy', '') for t in streak_trades]
                    patterns['losing_streaks'].append({
                        'length': streak,
                        'start_idx': streak_start,
                        'strategies': strategies,
                        'total_loss': sum(t.get('pnl_pct', 0) for t in streak_trades),
                    })
                streak = 0

        # ── Strategy evolution (is WR declining?) ──
        by_strat = defaultdict(list)
        for i, t in enumerate(all_trades):
            by_strat[t.get('strategy', 'unknown')].append({
                'idx': i, 'pnl': t.get('pnl_pct', 0),
            })

        for strat, trades_list in by_strat.items():
            if len(trades_list) < 5:
                continue
            # Compare first half vs second half
            mid = len(trades_list) // 2
            first_wr = sum(1 for t in trades_list[:mid] if t['pnl'] > 0) / mid * 100
            second_wr = sum(1 for t in trades_list[mid:] if t['pnl'] > 0) / (len(trades_list) - mid) * 100
            if second_wr < first_wr - 15:
                patterns['strategy_evolution'][strat] = {
                    'first_half_wr': round(first_wr, 1),
                    'second_half_wr': round(second_wr, 1),
                    'declining': True,
                }
                patterns['recommendations'].append(
                    f"'{strat}' win rate declining: {first_wr:.0f}% → {second_wr:.0f}%. "
                    f"Possible regime shift or parameter decay. Re-optimize or pause."
                )

        # ── MFE/MAE distribution recommendations ──
        mfes = [t.get('mfe', 0) for t in all_trades if t.get('mfe', 0) > 0]
        maes = [abs(t.get('mae', 0)) for t in all_trades if t.get('mae', 0) < 0]
        if mfes:
            p75_mfe = np.percentile(mfes, 75)
            p25_mfe = np.percentile(mfes, 25)
            patterns['recommendations'].append(
                f"MFE distribution: 25th={p25_mfe:.2f}%, 75th={p75_mfe:.2f}%. "
                f"Consider setting TP1 near {p25_mfe:.2f}% for consistency."
            )
        if maes:
            p90_mae = np.percentile(maes, 90)
            patterns['recommendations'].append(
                f"90th percentile MAE: {p90_mae:.2f}%. "
                f"Stops wider than {p90_mae:.2f}% survive 90% of drawdowns."
            )

        return patterns

    # ── Report Generation ──

    def generate_report(self, session_data: Dict) -> Dict:
        """Generate a complete analysis report from saved session data."""
        trades = session_data.get('completed_trades', [])
        blocked = session_data.get('blocked_signals_summary', {})
        stats = session_data.get('stats', {})

        # Full blocked signals with details
        blocked_detail = {}
        # Session analysis
        session = self.analyze_session(trades, blocked_detail, stats)

        # Individual trade analyses
        trade_analyses = []
        for t in trades:
            analysis = self.analyze_trade(t)
            trade_analyses.append(analysis.to_dict())

        report = {
            'generated_at': datetime.now().isoformat(),
            'session_summary': {
                'date': session.session_date,
                'total_trades': session.total_trades,
                'wins': session.wins,
                'losses': session.losses,
                'win_rate': session.win_rate,
                'total_pnl': session.total_pnl,
                'profit_factor': session.profit_factor,
                'max_drawdown': session.max_drawdown,
                'best_trade': session.best_trade,
                'worst_trade': session.worst_trade,
            },
            'strategy_breakdown': session.strategy_breakdown,
            'timeframe_breakdown': session.timeframe_breakdown,
            'hourly_breakdown': session.hourly_breakdown,
            'filter_effectiveness': session.filter_stats,
            'what_went_well': session.top_wins,
            'what_went_wrong': session.top_issues,
            'improvements': session.improvements,
            'trade_analyses': trade_analyses,
        }

        # ══════ DEEP ANALYSIS ══════
        deep = DeepAnalyzer()
        report['deep'] = deep.full_analysis(trades, stats)

        return report


# ============================================================
# DEEP ANALYZER — Quantitative post-trade forensics
# ============================================================

class DeepAnalyzer:
    """
    Quantitative analysis that the basic analyzer misses:
      - MFE/MAE distributions per strategy with optimal stop/target
      - Entry context correlation (RSI/ADX/ATR vs outcome)
      - Risk-adjusted metrics (Sharpe, Sortino, Calmar, expectancy)
      - Regime-specific performance
      - Time-of-day edge map
      - Counterfactual analysis (what-if different stops/targets)
      - Filter effectiveness in $ terms
      - Runner capture efficiency
      - Streak analysis with root cause
    """

    def full_analysis(self, trades: List[Dict], stats: Dict = None) -> Dict:
        if not trades:
            return {'error': 'No trades to analyze'}

        return {
            'mfe_mae': self.mfe_mae_analysis(trades),
            'risk_metrics': self.risk_adjusted_metrics(trades),
            'context_correlation': self.context_correlation(trades),
            'regime_performance': self.regime_performance(trades),
            'time_edge': self.time_of_day_edge(trades),
            'counterfactual': self.counterfactual_analysis(trades),
            'filter_effectiveness': self.filter_dollar_effectiveness(trades, stats),
            'runner_efficiency': self.runner_capture_analysis(trades),
            'streak_analysis': self.streak_analysis(trades),
            'strategy_deep': self.per_strategy_deep(trades),
        }

    # ── 1. MFE/MAE DISTRIBUTIONS ──

    def mfe_mae_analysis(self, trades: List[Dict]) -> Dict:
        """MFE/MAE distributions per strategy → optimal stop/target calibration."""
        by_strat = defaultdict(lambda: {'mfe': [], 'mae': [], 'pnl': [], 'stops': [], 'targets': []})
        for t in trades:
            s = t.get('strategy', 'all')
            by_strat[s]['mfe'].append(t.get('mfe', 0))
            by_strat[s]['mae'].append(abs(t.get('mae', 0)))
            by_strat[s]['pnl'].append(t.get('pnl_pct', 0))
            by_strat[s]['stops'].append(t.get('strategy_config', {}).get('stop_pct', 0))
            by_strat[s]['targets'].append(t.get('strategy_config', {}).get('target_pct', 0))
            # Also accumulate to 'all'
            by_strat['_all']['mfe'].append(t.get('mfe', 0))
            by_strat['_all']['mae'].append(abs(t.get('mae', 0)))
            by_strat['_all']['pnl'].append(t.get('pnl_pct', 0))

        result = {}
        for strat, d in by_strat.items():
            mfe = np.array(d['mfe'])
            mae = np.array(d['mae'])
            n = len(mfe)
            if n < 3:
                continue

            # MFE percentiles
            mfe_pos = mfe[mfe > 0]
            mae_pos = mae[mae > 0]

            entry = {
                'n': n,
                'mfe_mean': round(float(np.mean(mfe)), 4),
                'mfe_median': round(float(np.median(mfe)), 4),
                'mfe_p25': round(float(np.percentile(mfe_pos, 25)), 4) if len(mfe_pos) > 0 else 0,
                'mfe_p50': round(float(np.percentile(mfe_pos, 50)), 4) if len(mfe_pos) > 0 else 0,
                'mfe_p75': round(float(np.percentile(mfe_pos, 75)), 4) if len(mfe_pos) > 0 else 0,
                'mfe_p90': round(float(np.percentile(mfe_pos, 90)), 4) if len(mfe_pos) > 0 else 0,
                'mae_mean': round(float(np.mean(mae)), 4),
                'mae_median': round(float(np.median(mae)), 4),
                'mae_p75': round(float(np.percentile(mae_pos, 75)), 4) if len(mae_pos) > 0 else 0,
                'mae_p90': round(float(np.percentile(mae_pos, 90)), 4) if len(mae_pos) > 0 else 0,
                'mae_p95': round(float(np.percentile(mae_pos, 95)), 4) if len(mae_pos) > 0 else 0,
            }

            # Optimal stop: p90 MAE (survives 90% of drawdowns)
            # Optimal target: p25 MFE (hit by 75% of trades)
            if len(mae_pos) > 0 and len(mfe_pos) > 0:
                opt_stop = round(float(np.percentile(mae_pos, 90)), 4)
                opt_target = round(float(np.percentile(mfe_pos, 25)), 4)
                cur_stop = round(float(np.mean(d['stops'])), 4) if d['stops'] else 0
                cur_target = round(float(np.mean(d['targets'])), 4) if d['targets'] else 0

                entry['optimal_stop'] = opt_stop
                entry['optimal_target'] = opt_target
                entry['current_stop'] = cur_stop
                entry['current_target'] = cur_target
                entry['stop_delta'] = round(opt_stop - cur_stop, 4)
                entry['target_delta'] = round(opt_target - cur_target, 4)

                # Capture ratio: what % of MFE do we actually keep?
                pnl_arr = np.array(d['pnl'])
                capture = np.where(mfe > 0, pnl_arr / (mfe + 1e-10), 0)
                entry['capture_ratio'] = round(float(np.mean(capture)), 3)

                # Diagnosis
                if entry['stop_delta'] > 0.05:
                    entry['stop_diagnosis'] = f'Stop too tight by {entry["stop_delta"]:.2f}%. Widen to {opt_stop:.2f}% to survive 90% of drawdowns.'
                elif entry['stop_delta'] < -0.05:
                    entry['stop_diagnosis'] = f'Stop too wide by {abs(entry["stop_delta"]):.2f}%. Tighten to {opt_stop:.2f}% for better R:R.'
                else:
                    entry['stop_diagnosis'] = 'Stop is well calibrated.'

                if entry['target_delta'] > 0.05:
                    entry['target_diagnosis'] = f'Target too aggressive by {entry["target_delta"]:.2f}%. Lower to {opt_target:.2f}% (hit by 75% of winners).'
                elif entry['target_delta'] < -0.05:
                    entry['target_diagnosis'] = f'Target too conservative by {abs(entry["target_delta"]):.2f}%. Raise to {opt_target:.2f}%.'
                else:
                    entry['target_diagnosis'] = 'Target is well calibrated.'

            result[strat] = entry

        return result

    # ── 2. RISK-ADJUSTED METRICS ──

    def risk_adjusted_metrics(self, trades: List[Dict]) -> Dict:
        """Sharpe, Sortino, Calmar, expectancy, Kelly criterion."""
        pnls = np.array([t.get('pnl_pct', 0) for t in trades])
        if len(pnls) < 2:
            return {}

        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]
        wr = len(wins) / len(pnls) if len(pnls) > 0 else 0
        avg_win = float(np.mean(wins)) if len(wins) > 0 else 0
        avg_loss = float(np.mean(losses)) if len(losses) > 0 else 0

        mean_r = float(np.mean(pnls))
        std_r = float(np.std(pnls, ddof=1))
        downside = pnls[pnls < 0]
        downside_std = float(np.std(downside, ddof=1)) if len(downside) > 1 else 1e-10

        # Cumulative for drawdown
        cum = np.cumsum(pnls)
        peak = np.maximum.accumulate(cum)
        dd = peak - cum
        max_dd = float(np.max(dd)) if len(dd) > 0 else 1e-10

        # Expectancy per trade
        expectancy = wr * avg_win + (1 - wr) * avg_loss

        # Kelly criterion: f* = (bp - q) / b where b = avg_win/|avg_loss|, p = wr, q = 1-wr
        b = avg_win / (abs(avg_loss) + 1e-10)
        kelly = (b * wr - (1 - wr)) / (b + 1e-10)

        # Consecutive stats
        max_consec_wins = max_consec_losses = cur_w = cur_l = 0
        for p in pnls:
            if p > 0:
                cur_w += 1; cur_l = 0
                max_consec_wins = max(max_consec_wins, cur_w)
            else:
                cur_l += 1; cur_w = 0
                max_consec_losses = max(max_consec_losses, cur_l)

        return {
            'sharpe': round(mean_r / (std_r + 1e-10), 3),
            'sortino': round(mean_r / (downside_std + 1e-10), 3),
            'calmar': round(float(np.sum(pnls)) / (max_dd + 1e-10), 3),
            'expectancy_per_trade': round(expectancy, 4),
            'kelly_fraction': round(kelly, 4),
            'kelly_suggestion': f'Optimal bet size: {max(0, kelly)*100:.1f}% of capital per trade' if kelly > 0 else 'Negative edge — do not trade this system as-is',
            'payoff_ratio': round(avg_win / (abs(avg_loss) + 1e-10), 3),
            'max_consecutive_wins': max_consec_wins,
            'max_consecutive_losses': max_consec_losses,
            'max_drawdown_pct': round(max_dd, 4),
            'recovery_factor': round(float(np.sum(pnls)) / (max_dd + 1e-10), 3),
            'trade_std': round(std_r, 4),
            'win_rate': round(wr * 100, 1),
        }

    # ── 3. CONTEXT CORRELATION ──

    def context_correlation(self, trades: List[Dict]) -> Dict:
        """Correlate entry context (RSI, ADX, ATR ratio) with outcomes."""
        fields = ['RSI_14', 'ADX_14', 'atr_ratio']
        result = {}

        for field in fields:
            vals = []
            pnls = []
            for t in trades:
                ctx = t.get('entry_context', {})
                v = ctx.get(field)
                if v is not None:
                    vals.append(float(v))
                    pnls.append(t.get('pnl_pct', 0))

            if len(vals) < 10:
                continue

            vals = np.array(vals)
            pnls_arr = np.array(pnls)

            # Correlation
            corr = float(np.corrcoef(vals, pnls_arr)[0, 1]) if np.std(vals) > 0 else 0

            # Bucket analysis: split into quartiles
            quartiles = np.percentile(vals, [25, 50, 75])
            buckets = {}
            labels = ['Q1 (low)', 'Q2', 'Q3', 'Q4 (high)']
            edges = [-np.inf, quartiles[0], quartiles[1], quartiles[2], np.inf]
            for i in range(4):
                mask = (vals >= edges[i]) & (vals < edges[i+1])
                if np.sum(mask) > 0:
                    bucket_pnls = pnls_arr[mask]
                    buckets[labels[i]] = {
                        'range': f'{edges[i]:.1f}–{edges[i+1]:.1f}' if np.isfinite(edges[i]) and np.isfinite(edges[i+1]) else f'{"<" if i==0 else ">"}{quartiles[min(i,2)]:.1f}',
                        'trades': int(np.sum(mask)),
                        'win_rate': round(float(np.sum(bucket_pnls > 0) / np.sum(mask) * 100), 1),
                        'avg_pnl': round(float(np.mean(bucket_pnls)), 4),
                        'total_pnl': round(float(np.sum(bucket_pnls)), 4),
                    }

            # Best/worst bucket
            best_bucket = max(buckets.items(), key=lambda x: x[1]['avg_pnl']) if buckets else None
            worst_bucket = min(buckets.items(), key=lambda x: x[1]['avg_pnl']) if buckets else None

            diagnosis = ''
            if abs(corr) > 0.2:
                direction = 'positive' if corr > 0 else 'negative'
                diagnosis = f'{field} has {direction} correlation ({corr:.2f}) with P&L. '
            if best_bucket and worst_bucket:
                diagnosis += (
                    f'Best bucket: {best_bucket[0]} ({best_bucket[1]["avg_pnl"]:+.3f}% avg), '
                    f'Worst: {worst_bucket[0]} ({worst_bucket[1]["avg_pnl"]:+.3f}% avg).'
                )

            result[field] = {
                'correlation': round(corr, 4),
                'buckets': buckets,
                'diagnosis': diagnosis,
                'mean': round(float(np.mean(vals)), 2),
                'std': round(float(np.std(vals)), 2),
            }

        # Regime (market_mode) vs outcome
        mode_perf = defaultdict(list)
        for t in trades:
            mode = t.get('entry_context', {}).get('market_mode', 'UNKNOWN')
            mode_perf[mode].append(t.get('pnl_pct', 0))
        result['market_mode'] = {
            mode: {
                'trades': len(pnls),
                'win_rate': round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
                'avg_pnl': round(float(np.mean(pnls)), 4),
                'total_pnl': round(float(np.sum(pnls)), 4),
            }
            for mode, pnls in mode_perf.items()
        }

        # EMA bias vs outcome
        bias_perf = defaultdict(list)
        for t in trades:
            bias = t.get('entry_context', {}).get('ema_bias', 'UNKNOWN')
            bias_perf[bias].append(t.get('pnl_pct', 0))
        result['ema_bias'] = {
            bias: {
                'trades': len(pnls),
                'win_rate': round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
                'avg_pnl': round(float(np.mean(pnls)), 4),
            }
            for bias, pnls in bias_perf.items()
        }

        return result

    # ── 4. REGIME-SPECIFIC PERFORMANCE ──

    def regime_performance(self, trades: List[Dict]) -> Dict:
        """Performance by market regime, direction, and category."""
        dims = ['market_mode', 'ema_bias']
        result = {}

        for dim in dims:
            by_dim = defaultdict(lambda: {'trades': [], 'pnls': []})
            for t in trades:
                key = t.get('entry_context', {}).get(dim, 'UNKNOWN')
                by_dim[key]['trades'].append(t)
                by_dim[key]['pnls'].append(t.get('pnl_pct', 0))

            result[dim] = {}
            for key, data in by_dim.items():
                pnls = np.array(data['pnls'])
                result[dim][key] = {
                    'trades': len(pnls),
                    'win_rate': round(float(np.sum(pnls > 0) / len(pnls) * 100), 1),
                    'avg_pnl': round(float(np.mean(pnls)), 4),
                    'total_pnl': round(float(np.sum(pnls)), 4),
                    'avg_mfe': round(float(np.mean([t.get('mfe', 0) for t in data['trades']])), 4),
                    'avg_mae': round(float(np.mean([abs(t.get('mae', 0)) for t in data['trades']])), 4),
                }

        # Direction (CALL vs PUT)
        by_dir = defaultdict(list)
        for t in trades:
            by_dir[t.get('direction', 'UNK')].append(t.get('pnl_pct', 0))
        result['direction'] = {
            d: {
                'trades': len(pnls),
                'win_rate': round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
                'avg_pnl': round(float(np.mean(pnls)), 4),
                'total_pnl': round(float(np.sum(pnls)), 4),
            }
            for d, pnls in by_dir.items()
        }

        return result

    # ── 5. TIME-OF-DAY EDGE ──

    def time_of_day_edge(self, trades: List[Dict]) -> Dict:
        """Find which hours/sessions have the strongest edge."""
        by_hour = defaultdict(list)
        for t in trades:
            et = t.get('entry_time', 0)
            if et > 0:
                hr = datetime.fromtimestamp(et).hour
                by_hour[hr].append(t.get('pnl_pct', 0))

        hourly = {}
        for hr in sorted(by_hour.keys()):
            pnls = by_hour[hr]
            hourly[hr] = {
                'trades': len(pnls),
                'win_rate': round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
                'avg_pnl': round(float(np.mean(pnls)), 4),
                'total_pnl': round(float(np.sum(pnls)), 4),
            }

        # Session classification
        sessions = {
            'pre_market (4-9:30)': [], 'ny_open (9:30-11:30)': [],
            'midday (11:30-14:00)': [], 'afternoon (14:00-15:00)': [],
            'power_hour (15:00-16:00)': [], 'after_hours (16+)': [],
        }
        for t in trades:
            et = t.get('entry_time', 0)
            if et <= 0: continue
            dt = datetime.fromtimestamp(et)
            mins = dt.hour * 60 + dt.minute
            pnl = t.get('pnl_pct', 0)
            if mins < 570: sessions['pre_market (4-9:30)'].append(pnl)
            elif mins < 690: sessions['ny_open (9:30-11:30)'].append(pnl)
            elif mins < 840: sessions['midday (11:30-14:00)'].append(pnl)
            elif mins < 900: sessions['afternoon (14:00-15:00)'].append(pnl)
            elif mins < 960: sessions['power_hour (15:00-16:00)'].append(pnl)
            else: sessions['after_hours (16+)'].append(pnl)

        session_perf = {}
        for name, pnls in sessions.items():
            if not pnls: continue
            session_perf[name] = {
                'trades': len(pnls),
                'win_rate': round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
                'avg_pnl': round(float(np.mean(pnls)), 4),
                'total_pnl': round(float(np.sum(pnls)), 4),
            }

        # Best/worst hour
        best_hr = max(hourly.items(), key=lambda x: x[1]['avg_pnl']) if hourly else None
        worst_hr = min(hourly.items(), key=lambda x: x[1]['avg_pnl']) if hourly else None

        diagnosis = ''
        if best_hr:
            diagnosis += f'Best hour: {best_hr[0]}:00 ({best_hr[1]["avg_pnl"]:+.3f}% avg, {best_hr[1]["trades"]} trades). '
        if worst_hr and worst_hr[1]['avg_pnl'] < 0:
            diagnosis += f'Worst: {worst_hr[0]}:00 ({worst_hr[1]["avg_pnl"]:+.3f}% avg). Consider blocking.'

        return {
            'hourly': hourly,
            'sessions': session_perf,
            'diagnosis': diagnosis,
        }

    # ── 6. COUNTERFACTUAL ANALYSIS ──

    def counterfactual_analysis(self, trades: List[Dict]) -> Dict:
        """What-if analysis: how would results change with different stop/target?"""
        results = {}

        for mult_name, stop_mult, target_mult in [
            ('tighter_stops', 0.7, 1.0),
            ('wider_stops', 1.3, 1.0),
            ('higher_targets', 1.0, 1.3),
            ('lower_targets', 1.0, 0.7),
            ('tighter_both', 0.8, 0.8),
            ('wider_both', 1.2, 1.2),
        ]:
            sim_pnls = []
            for t in trades:
                mfe = t.get('mfe', 0)
                mae = abs(t.get('mae', 0))
                cur_stop = t.get('strategy_config', {}).get('stop_pct', 0.2)
                cur_target = t.get('strategy_config', {}).get('target_pct', 0.4)

                new_stop = cur_stop * stop_mult
                new_target = cur_target * target_mult

                # Simulate: if MAE > new_stop, stopped out. If MFE > new_target, hit target.
                if mae >= new_stop:
                    sim_pnls.append(-new_stop)
                elif mfe >= new_target:
                    sim_pnls.append(new_target)
                else:
                    sim_pnls.append(t.get('pnl_pct', 0))  # same outcome

            sim = np.array(sim_pnls)
            actual_pnl = sum(t.get('pnl_pct', 0) for t in trades)
            sim_total = float(np.sum(sim))

            results[mult_name] = {
                'stop_multiplier': stop_mult,
                'target_multiplier': target_mult,
                'simulated_pnl': round(sim_total, 4),
                'actual_pnl': round(actual_pnl, 4),
                'improvement': round(sim_total - actual_pnl, 4),
                'win_rate': round(float(np.sum(sim > 0) / len(sim) * 100), 1),
                'avg_pnl': round(float(np.mean(sim)), 4),
            }

        # Find best scenario
        best = max(results.items(), key=lambda x: x[1]['improvement'])
        results['_best_scenario'] = {
            'name': best[0],
            'improvement': best[1]['improvement'],
            'recommendation': f"'{best[0]}' would improve total P&L by {best[1]['improvement']:+.3f}% "
                              f"(stop ×{best[1]['stop_multiplier']}, target ×{best[1]['target_multiplier']})"
        }

        return results

    # ── 7. FILTER $ EFFECTIVENESS ──

    def filter_dollar_effectiveness(self, trades: List[Dict], stats: Dict = None) -> Dict:
        """How much $ does each filter save? Based on blocked signals that would have lost."""
        # For traded signals: look at losses and what filter SHOULD have caught them
        false_signals = [t for t in trades if t.get('pnl_pct', 0) < 0]
        correct_blocks = {}

        for t in false_signals:
            # Check each filter that passed — which one should have blocked?
            ctx = t.get('entry_context', {})
            pnl = t.get('pnl_pct', 0)

            # RSI gate: would tighter RSI have caught this?
            rsi = ctx.get('RSI_14', 50)
            if t.get('direction') == 'CALL' and rsi > 65:
                correct_blocks.setdefault('rsi_gate_65', {'count': 0, 'saved': 0})
                correct_blocks['rsi_gate_65']['count'] += 1
                correct_blocks['rsi_gate_65']['saved'] += abs(pnl)
            elif t.get('direction') == 'PUT' and rsi < 35:
                correct_blocks.setdefault('rsi_gate_35', {'count': 0, 'saved': 0})
                correct_blocks['rsi_gate_35']['count'] += 1
                correct_blocks['rsi_gate_35']['saved'] += abs(pnl)

            # ATR ratio: low vol losses
            atr_ratio = ctx.get('atr_ratio', 1.0)
            if atr_ratio < 0.8:
                correct_blocks.setdefault('vol_filter_0.8', {'count': 0, 'saved': 0})
                correct_blocks['vol_filter_0.8']['count'] += 1
                correct_blocks['vol_filter_0.8']['saved'] += abs(pnl)

            # ADX: weak trend
            adx = ctx.get('ADX_14', 25)
            if adx < 20:
                correct_blocks.setdefault('adx_filter_20', {'count': 0, 'saved': 0})
                correct_blocks['adx_filter_20']['count'] += 1
                correct_blocks['adx_filter_20']['saved'] += abs(pnl)

        # Stats-based filter effectiveness
        filter_blocks = {}
        if stats:
            for k, v in stats.items():
                if k.startswith('blocked_by_'):
                    name = k.replace('blocked_by_', '')
                    filter_blocks[name] = v

        return {
            'would_have_saved': {k: {'count': v['count'], 'pnl_saved': round(v['saved'], 4)}
                                  for k, v in sorted(correct_blocks.items(), key=lambda x: -x[1]['saved'])},
            'total_losses': len(false_signals),
            'total_loss_pnl': round(sum(abs(t.get('pnl_pct', 0)) for t in false_signals), 4),
            'preventable_losses': sum(v['count'] for v in correct_blocks.values()),
            'preventable_pnl': round(sum(v['saved'] for v in correct_blocks.values()), 4),
            'current_filter_blocks': filter_blocks,
        }

    # ── 8. RUNNER CAPTURE ANALYSIS ──

    def runner_capture_analysis(self, trades: List[Dict]) -> Dict:
        """How efficiently do we capture the available MFE?"""
        captures = []
        by_exit = defaultdict(list)
        tp1_runners = {'reached_tp2': 0, 'trailed_out': 0, 'total_tp1': 0}

        for t in trades:
            mfe = t.get('mfe', 0)
            pnl = t.get('pnl_pct', 0)
            exit_r = t.get('exit_reason', '')

            if mfe > 0:
                ratio = pnl / mfe
                captures.append(ratio)
                by_exit[exit_r].append(ratio)

            if t.get('tp1_hit'):
                tp1_runners['total_tp1'] += 1
                if exit_r == 'TP2':
                    tp1_runners['reached_tp2'] += 1
                elif exit_r == 'TRAIL':
                    tp1_runners['trailed_out'] += 1

        overall_capture = float(np.mean(captures)) if captures else 0
        winners = [c for c in captures if c > 0]
        losers = [c for c in captures if c <= 0]

        exit_efficiency = {}
        for exit_r, ratios in by_exit.items():
            exit_efficiency[exit_r] = {
                'count': len(ratios),
                'avg_capture': round(float(np.mean(ratios)), 3),
                'median_capture': round(float(np.median(ratios)), 3),
            }

        diagnosis = []
        if overall_capture < 0.3:
            diagnosis.append(f'Only capturing {overall_capture*100:.0f}% of available MFE. Trail stop is too loose or exits are too early.')
        elif overall_capture < 0.5:
            diagnosis.append(f'Capturing {overall_capture*100:.0f}% of MFE. Decent but room to improve trail logic.')
        else:
            diagnosis.append(f'Capturing {overall_capture*100:.0f}% of MFE. Excellent execution.')

        if tp1_runners['total_tp1'] > 0:
            runner_rate = tp1_runners['reached_tp2'] / tp1_runners['total_tp1']
            diagnosis.append(
                f'Of {tp1_runners["total_tp1"]} TP1 hits, {tp1_runners["reached_tp2"]} reached TP2 '
                f'({runner_rate*100:.0f}% runner conversion).'
            )

        return {
            'overall_capture_ratio': round(overall_capture, 3),
            'winner_capture': round(float(np.mean(winners)), 3) if winners else 0,
            'loser_capture': round(float(np.mean(losers)), 3) if losers else 0,
            'by_exit_reason': exit_efficiency,
            'tp1_stats': tp1_runners,
            'diagnosis': diagnosis,
        }

    # ── 9. STREAK ANALYSIS ──

    def streak_analysis(self, trades: List[Dict]) -> Dict:
        """Find winning/losing streaks and diagnose root causes."""
        pnls = [t.get('pnl_pct', 0) for t in trades]
        streaks = []
        cur_type = None
        cur_start = 0
        cur_len = 0

        for i, p in enumerate(pnls):
            t = 'W' if p > 0 else 'L'
            if t == cur_type:
                cur_len += 1
            else:
                if cur_len >= 2:
                    streak_trades = trades[cur_start:cur_start + cur_len]
                    streaks.append({
                        'type': cur_type,
                        'length': cur_len,
                        'start_idx': cur_start,
                        'total_pnl': round(sum(x.get('pnl_pct', 0) for x in streak_trades), 4),
                        'strategies': list(set(x.get('strategy', '') for x in streak_trades)),
                        'tickers': list(set(x.get('ticker', '') for x in streak_trades)),
                        'modes': list(set(x.get('entry_context', {}).get('market_mode', '') for x in streak_trades)),
                    })
                cur_type = t
                cur_start = i
                cur_len = 1

        # Final streak
        if cur_len >= 2:
            streak_trades = trades[cur_start:cur_start + cur_len]
            streaks.append({
                'type': cur_type, 'length': cur_len, 'start_idx': cur_start,
                'total_pnl': round(sum(x.get('pnl_pct', 0) for x in streak_trades), 4),
                'strategies': list(set(x.get('strategy', '') for x in streak_trades)),
                'tickers': list(set(x.get('ticker', '') for x in streak_trades)),
                'modes': list(set(x.get('entry_context', {}).get('market_mode', '') for x in streak_trades)),
            })

        losing_streaks = [s for s in streaks if s['type'] == 'L' and s['length'] >= 3]
        winning_streaks = [s for s in streaks if s['type'] == 'W' and s['length'] >= 3]

        diagnosis = []
        for ls in losing_streaks:
            if len(ls['strategies']) == 1:
                diagnosis.append(f'{ls["length"]}-loss streak all from {ls["strategies"][0]} — strategy-specific issue.')
            if len(ls['modes']) == 1 and ls['modes'][0]:
                diagnosis.append(f'{ls["length"]}-loss streak all in {ls["modes"][0]} mode — regime mismatch likely.')

        return {
            'all_streaks': streaks,
            'worst_losing_streak': max(losing_streaks, key=lambda x: x['length']) if losing_streaks else None,
            'best_winning_streak': max(winning_streaks, key=lambda x: x['length']) if winning_streaks else None,
            'total_losing_streaks_3plus': len(losing_streaks),
            'total_winning_streaks_3plus': len(winning_streaks),
            'diagnosis': diagnosis,
        }

    # ── 10. PER-STRATEGY DEEP DIVE ──

    def per_strategy_deep(self, trades: List[Dict]) -> Dict:
        """Deep metrics per strategy."""
        by_strat = defaultdict(list)
        for t in trades:
            by_strat[t.get('strategy', 'unknown')].append(t)

        result = {}
        for strat, strat_trades in by_strat.items():
            pnls = [t.get('pnl_pct', 0) for t in strat_trades]
            mfes = [t.get('mfe', 0) for t in strat_trades]
            maes = [abs(t.get('mae', 0)) for t in strat_trades]
            holds = [t.get('hold_seconds', 0) for t in strat_trades]
            n = len(pnls)

            wins = [p for p in pnls if p > 0]
            losses = [p for p in pnls if p < 0]

            entry = {
                'trades': n,
                'win_rate': round(len(wins) / n * 100, 1) if n > 0 else 0,
                'total_pnl': round(sum(pnls), 4),
                'avg_pnl': round(float(np.mean(pnls)), 4),
                'avg_win': round(float(np.mean(wins)), 4) if wins else 0,
                'avg_loss': round(float(np.mean(losses)), 4) if losses else 0,
                'profit_factor': round(abs(sum(wins)) / (abs(sum(losses)) + 1e-10), 2) if losses else float('inf'),
                'avg_mfe': round(float(np.mean(mfes)), 4),
                'avg_mae': round(float(np.mean(maes)), 4),
                'avg_hold_min': round(float(np.mean(holds)) / 60, 1) if holds else 0,
                'outcomes': {},
            }

            # Outcome distribution
            for t in strat_trades:
                o = t.get('outcome', 'UNKNOWN')
                entry['outcomes'][o] = entry['outcomes'].get(o, 0) + 1

            # Direction split
            calls = [t.get('pnl_pct', 0) for t in strat_trades if t.get('direction') == 'CALL']
            puts = [t.get('pnl_pct', 0) for t in strat_trades if t.get('direction') == 'PUT']
            if calls and puts:
                entry['call_wr'] = round(sum(1 for p in calls if p > 0) / len(calls) * 100, 1)
                entry['put_wr'] = round(sum(1 for p in puts if p > 0) / len(puts) * 100, 1)
                entry['direction_bias'] = 'CALL' if entry['call_wr'] > entry['put_wr'] + 10 else ('PUT' if entry['put_wr'] > entry['call_wr'] + 10 else 'NEUTRAL')

            result[strat] = entry

        return result
