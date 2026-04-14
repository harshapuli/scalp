"""
TRADE ANALYZER V2 — Whale & Institutional Layer Analysis
===========================================================
Extends TradeAnalyzer + DeepAnalyzer with whale-specific capabilities:

1. Whale Performance Analysis — Compare whale vs non-whale trades
2. Whale Signal Breakdown — Per signal type effectiveness
3. Institutional Filter Effectiveness — Which filters work, which don't
4. Whale Alert Analysis — Alert type → trade latency & false rate
5. Accumulation Zone Analysis — Zone strength correlation with wins
6. Volume Profile Analysis — HVN/LVN/POC impact on trades
7. Regime Analysis (Enhanced) — Wyckoff phase + whale momentum
8. V1 vs V2 Comparison — Side-by-side performance
9. Report Generator — Comprehensive report with actionable recommendations
10. Whale Findings — Plain-English insights for traders
"""

import json
import numpy as np
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict
from dataclasses import dataclass, field
from scipy import stats as sp_stats


# ============================================================
# WHALE-SPECIFIC DATA STRUCTURES
# ============================================================

@dataclass
class WhaleSignalAnalysis:
    """Analysis of a single whale signal type."""
    signal_type: str
    count: int = 0
    win_rate: float = 0.0
    avg_pnl: float = 0.0
    avg_mfe: float = 0.0
    avg_mae: float = 0.0
    avg_hold_time: float = 0.0
    sharpe: float = 0.0
    max_dd: float = 0.0
    findings: List[str] = field(default_factory=list)
    optimal_conviction_threshold: float = 0.0


@dataclass
class FilterAnalysis:
    """Analysis of filter effectiveness."""
    filter_name: str
    total_blocks: int = 0
    blocks_that_would_have_lost: int = 0
    precision: float = 0.0  # blocks that would have lost / total blocks
    recall: float = 0.0     # blocks that would have lost / total losers
    recommendation: str = ''


@dataclass
class WhaleReportSection:
    """A section of the whale analysis report."""
    section_name: str
    key_metrics: Dict = field(default_factory=dict)
    findings: List[str] = field(default_factory=list)
    recommendations: List[str] = field(default_factory=list)


# ============================================================
# TRADE ANALYZER V2
# ============================================================

class TradeAnalyzerV2:
    """
    Extended analyzer that wraps TradeAnalyzer and adds whale/institutional
    analysis capabilities.
    """

    def __init__(self):
        """Initialize V2 analyzer."""
        self.trades = []
        self.whale_signals = []
        self.blocked_trades = []

    # ════════════════════════════════════════════════════════
    # 1. WHALE PERFORMANCE ANALYSIS
    # ════════════════════════════════════════════════════════

    def analyze_whale_impact(self, trades: List[Dict]) -> Dict:
        """
        Compare whale-confirmed vs non-whale trades:
        - Win rate split
        - Avg PnL split
        - Sharpe split
        - Max DD split
        - Statistical significance test
        - Whale confidence correlation with P&L
        - Institutional score correlation with P&L
        """
        whale_trades = []
        non_whale_trades = []

        for t in trades:
            sig_info = t.get('signal_info', {})
            whale_conf = sig_info.get('whale_confidence', 0)
            inst_score = sig_info.get('institutional_score', 0)

            # Whale trade = whale_confidence > 0 or whale signal type
            sig_type = t.get('signal_type', '')
            is_whale = (whale_conf > 0 or 'whale' in sig_type.lower())

            if is_whale:
                whale_trades.append(t)
            else:
                non_whale_trades.append(t)

        result = {
            'whale_count': len(whale_trades),
            'non_whale_count': len(non_whale_trades),
            'whale_metrics': self._compute_trade_metrics(whale_trades),
            'non_whale_metrics': self._compute_trade_metrics(non_whale_trades),
            'statistical_test': {},
            'correlations': {},
        }

        # Statistical significance test (t-test if enough samples)
        if len(whale_trades) >= 3 and len(non_whale_trades) >= 3:
            whale_pnls = np.array([t.get('pnl_pct', 0) for t in whale_trades])
            non_whale_pnls = np.array([t.get('pnl_pct', 0) for t in non_whale_trades])

            t_stat, p_val = sp_stats.ttest_ind(whale_pnls, non_whale_pnls)
            result['statistical_test'] = {
                't_statistic': round(float(t_stat), 4),
                'p_value': round(float(p_val), 4),
                'significant_at_95': p_val < 0.05,
                'interpretation': 'Whale trades significantly outperform' if p_val < 0.05
                                else 'No significant difference detected'
            }

        # Correlation: whale_confidence → PnL
        whale_pnls = [t.get('pnl_pct', 0) for t in whale_trades]
        whale_confs = [t.get('signal_info', {}).get('whale_confidence', 0) for t in whale_trades]
        if len(whale_confs) > 2 and len(set(whale_confs)) > 1:
            corr_wc, p_wc = sp_stats.pearsonr(whale_confs, whale_pnls)
            result['correlations']['whale_confidence_to_pnl'] = {
                'correlation': round(float(corr_wc), 4),
                'p_value': round(float(p_wc), 4),
                'interpretation': 'Higher confidence correlates with better PnL' if corr_wc > 0.3
                                else 'Weak or no correlation'
            }

        # Correlation: institutional_score → PnL
        inst_scores = [t.get('signal_info', {}).get('institutional_score', 0) for t in whale_trades]
        if len(inst_scores) > 2 and len(set(inst_scores)) > 1:
            corr_is, p_is = sp_stats.pearsonr(inst_scores, whale_pnls)
            result['correlations']['institutional_score_to_pnl'] = {
                'correlation': round(float(corr_is), 4),
                'p_value': round(float(p_is), 4),
                'interpretation': 'Higher institutional score correlates with better PnL' if corr_is > 0.3
                                else 'Weak or no correlation'
            }

        return result

    # ════════════════════════════════════════════════════════
    # 2. WHALE SIGNAL BREAKDOWN
    # ════════════════════════════════════════════════════════

    def analyze_whale_signals(self, trades: List[Dict]) -> Dict:
        """
        Per whale signal type analysis:
        - Count, win rate, avg PnL, avg MFE, avg MAE, avg hold time
        - Best/worst signal ranking
        - Optimal conviction threshold per signal type
        """
        by_signal = defaultdict(list)

        for t in trades:
            sig_type = t.get('signal_type', 'unknown')
            if 'whale' in sig_type.lower():
                by_signal[sig_type].append(t)

        result = {}
        signal_rankings = []

        for sig_type, sig_trades in by_signal.items():
            if len(sig_trades) == 0:
                continue

            metrics = self._compute_trade_metrics(sig_trades)
            pnls = np.array([t.get('pnl_pct', 0) for t in sig_trades])

            analysis = WhaleSignalAnalysis(
                signal_type=sig_type,
                count=len(sig_trades),
                win_rate=metrics.get('win_rate', 0.0),
                avg_pnl=metrics.get('avg_pnl', 0.0),
                avg_mfe=metrics.get('avg_mfe', 0.0),
                avg_mae=metrics.get('avg_mae', 0.0),
                avg_hold_time=metrics.get('avg_hold_time', 0.0),
                sharpe=metrics.get('sharpe', 0.0),
                max_dd=metrics.get('max_dd', 0.0),
            )

            # Optimal conviction threshold: find confidence percentile that maximizes Sharpe
            whale_confs = np.array([t.get('signal_info', {}).get('whale_confidence', 0)
                                   for t in sig_trades])
            if len(whale_confs) > 0 and len(set(whale_confs)) > 1:
                best_sharpe = -float('inf')
                best_threshold = 0
                for threshold in np.percentile(whale_confs[whale_confs > 0], [25, 50, 75]):
                    subset = pnls[whale_confs >= threshold]
                    if len(subset) > 1:
                        s = (np.mean(subset) / (np.std(subset, ddof=1) + 1e-10)) * np.sqrt(252)
                        if s > best_sharpe:
                            best_sharpe = s
                            best_threshold = threshold
                analysis.optimal_conviction_threshold = float(best_threshold)

            # Generate findings
            findings = []
            if analysis.win_rate > 70:
                findings.append(f'{sig_type} has excellent {analysis.win_rate:.1f}% win rate')
            if analysis.avg_pnl > 0.3:
                findings.append(f'{sig_type} averages +{analysis.avg_pnl:.2f}% per trade')
            if analysis.max_dd > 0.5:
                findings.append(f'{sig_type} max DD of {analysis.max_dd:.2f}% — consider tighter stops')
            analysis.findings = findings

            result[sig_type] = analysis.__dict__
            signal_rankings.append((sig_type, analysis.win_rate, analysis.avg_pnl, analysis.sharpe))

        # Sort by performance
        signal_rankings.sort(key=lambda x: x[2], reverse=True)
        result['ranking'] = [
            {
                'signal_type': s[0],
                'win_rate': s[1],
                'avg_pnl': s[2],
                'sharpe': s[3],
            }
            for s in signal_rankings
        ]

        return result

    # ════════════════════════════════════════════════════════
    # 3. INSTITUTIONAL FILTER EFFECTIVENESS
    # ════════════════════════════════════════════════════════

    def analyze_institutional_filters(self, trades: List[Dict],
                                     blocked_trades: List[Dict] = None) -> Dict:
        """
        Which filters block the most? Of blocked trades, how many WOULD
        have been losers? (counterfactual analysis)

        Filter effectiveness metrics:
        - Precision: blocks that would have lost / total blocks
        - Recall: blocks that would have lost / total losers
        """
        if blocked_trades is None:
            blocked_trades = []

        result = {
            'total_trades': len(trades),
            'total_blocked': len(blocked_trades),
            'filters': {},
            'recommendations': [],
        }

        if len(blocked_trades) == 0:
            return result

        # Count blocks by filter type
        filter_blocks = defaultdict(list)
        for bt in blocked_trades:
            blocked_by = bt.get('blocked_by', 'unknown')
            if isinstance(blocked_by, list):
                for b in blocked_by:
                    filter_blocks[b].append(bt)
            else:
                filter_blocks[blocked_by].append(bt)

        # Analyze each filter
        total_losers = len([t for t in trades if t.get('pnl_pct', 0) < 0])

        for filter_name, blocked in filter_blocks.items():
            blocks_that_would_lose = sum(1 for bt in blocked if bt.get('pnl_pct', 0) < 0)
            total_blocks = len(blocked)

            precision = blocks_that_would_lose / total_blocks if total_blocks > 0 else 0
            recall = blocks_that_would_lose / total_losers if total_losers > 0 else 0

            recommendation = ''
            if precision > 0.75:
                recommendation = 'KEEP: High precision, blocks bad trades effectively'
            elif precision < 0.4:
                recommendation = 'TUNE: Low precision, too many good trades blocked'
            else:
                recommendation = 'MONITOR: Moderate effectiveness, consider edge cases'

            filter_info = FilterAnalysis(
                filter_name=filter_name,
                total_blocks=total_blocks,
                blocks_that_would_have_lost=blocks_that_would_lose,
                precision=round(precision, 3),
                recall=round(recall, 3),
                recommendation=recommendation,
            )

            result['filters'][filter_name] = filter_info.__dict__
            result['recommendations'].append(recommendation)

        return result

    # ════════════════════════════════════════════════════════
    # 4. WHALE ALERT ANALYSIS
    # ════════════════════════════════════════════════════════

    def analyze_whale_alerts(self, trades: List[Dict]) -> Dict:
        """
        Alert type distribution, which alerts preceded winning trades,
        alert latency, false alert rate per type.
        """
        result = {
            'alert_types': defaultdict(int),
            'alert_performance': {},
            'false_alerts': defaultdict(float),
            'latency_stats': {},
        }

        alert_types_to_trades = defaultdict(list)

        for t in trades:
            sig_info = t.get('signal_info', {})
            alert_type = sig_info.get('alert_type', t.get('signal_type', 'UNKNOWN'))

            result['alert_types'][alert_type] += 1
            alert_types_to_trades[alert_type].append(t)

        # Performance by alert type
        for alert_type, alert_trades in alert_types_to_trades.items():
            metrics = self._compute_trade_metrics(alert_trades)
            result['alert_performance'][alert_type] = metrics

            # False alert rate = (losses / total) for this alert type
            losses = sum(1 for t in alert_trades if t.get('pnl_pct', 0) < 0)
            false_rate = losses / len(alert_trades) if len(alert_trades) > 0 else 0
            result['false_alerts'][alert_type] = round(false_rate, 3)

            # Latency: bars between alert and entry (if available)
            latencies = []
            for t in alert_trades:
                alert_time = t.get('signal_info', {}).get('alert_timestamp')
                entry_time = t.get('entry_time')
                if alert_time and entry_time:
                    latency = entry_time - alert_time  # seconds
                    latencies.append(latency)

            if latencies:
                result['latency_stats'][alert_type] = {
                    'mean_seconds': round(float(np.mean(latencies)), 2),
                    'median_seconds': round(float(np.median(latencies)), 2),
                    'min_seconds': round(float(np.min(latencies)), 2),
                    'max_seconds': round(float(np.max(latencies)), 2),
                }

        return result

    # ════════════════════════════════════════════════════════
    # 5. ACCUMULATION ZONE ANALYSIS
    # ════════════════════════════════════════════════════════

    def analyze_accumulation_zones(self, trades: List[Dict]) -> Dict:
        """
        Trades at accumulation zones vs not.
        Zone strength correlation with win rate.
        Zone age vs trade outcome.
        """
        at_zone = []
        not_at_zone = []

        for t in trades:
            at_accum_zone = t.get('signal_info', {}).get('at_accumulation_zone', False)
            zone_strength = t.get('signal_info', {}).get('zone_strength', 0)
            zone_age = t.get('signal_info', {}).get('zone_age_bars', 0)

            t_with_meta = t.copy()
            t_with_meta['zone_strength'] = zone_strength
            t_with_meta['zone_age'] = zone_age

            if at_accum_zone:
                at_zone.append(t_with_meta)
            else:
                not_at_zone.append(t_with_meta)

        result = {
            'at_zone_count': len(at_zone),
            'not_at_zone_count': len(not_at_zone),
            'at_zone_metrics': self._compute_trade_metrics(at_zone),
            'not_at_zone_metrics': self._compute_trade_metrics(not_at_zone),
            'zone_strength_correlation': {},
            'zone_age_analysis': {},
        }

        # Correlation: zone strength → win rate
        if len(at_zone) > 2:
            strengths = [t.get('zone_strength', 0) for t in at_zone]
            outcomes = [1 if t.get('pnl_pct', 0) > 0 else 0 for t in at_zone]
            if len(set(strengths)) > 1:
                try:
                    corr, p_val = sp_stats.pointbiserialr(outcomes, strengths)
                    result['zone_strength_correlation'] = {
                        'correlation': round(float(corr), 4),
                        'p_value': round(float(p_val), 4),
                    }
                except:
                    pass

        # Zone age analysis: fresh zones vs old zones
        if len(at_zone) > 2:
            ages = [t.get('zone_age', 0) for t in at_zone]
            median_age = float(np.median(ages)) if ages else 0

            fresh = [t for t in at_zone if t.get('zone_age', 0) <= median_age]
            old = [t for t in at_zone if t.get('zone_age', 0) > median_age]

            result['zone_age_analysis'] = {
                'fresh_zones_metrics': self._compute_trade_metrics(fresh),
                'old_zones_metrics': self._compute_trade_metrics(old),
            }

        return result

    # ════════════════════════════════════════════════════════
    # 6. VOLUME PROFILE ANALYSIS
    # ════════════════════════════════════════════════════════

    def analyze_volume_profile_impact(self, trades: List[Dict]) -> Dict:
        """
        Trades at HVN vs LVN vs Value Area vs Outside.
        POC proximity correlation with win rate.
        VWAP deviation at entry correlation with P&L.
        """
        hvn_trades = []
        lvn_trades = []
        va_trades = []
        outside_trades = []

        for t in trades:
            vp_location = t.get('signal_info', {}).get('volume_profile_location', 'unknown')

            if vp_location == 'HVN':
                hvn_trades.append(t)
            elif vp_location == 'LVN':
                lvn_trades.append(t)
            elif vp_location == 'VALUE_AREA':
                va_trades.append(t)
            elif vp_location == 'OUTSIDE':
                outside_trades.append(t)

        result = {
            'hvn_metrics': self._compute_trade_metrics(hvn_trades),
            'lvn_metrics': self._compute_trade_metrics(lvn_trades),
            'value_area_metrics': self._compute_trade_metrics(va_trades),
            'outside_metrics': self._compute_trade_metrics(outside_trades),
            'poc_proximity_correlation': {},
            'vwap_deviation_correlation': {},
        }

        # POC proximity correlation
        poc_prox = [t.get('signal_info', {}).get('poc_proximity', 0) for t in trades if 'poc_proximity' in t.get('signal_info', {})]
        pnls = [t.get('pnl_pct', 0) for t in trades if 'poc_proximity' in t.get('signal_info', {})]
        if len(poc_prox) > 2 and len(set(poc_prox)) > 1:
            try:
                corr, p_val = sp_stats.pearsonr(poc_prox, pnls)
                result['poc_proximity_correlation'] = {
                    'correlation': round(float(corr), 4),
                    'p_value': round(float(p_val), 4),
                    'interpretation': 'Closer to POC = better outcomes' if corr > 0.3 else 'Weak correlation'
                }
            except:
                pass

        # VWAP deviation correlation
        vwap_devs = [t.get('signal_info', {}).get('vwap_deviation_pct', 0) for t in trades if 'vwap_deviation_pct' in t.get('signal_info', {})]
        pnls = [t.get('pnl_pct', 0) for t in trades if 'vwap_deviation_pct' in t.get('signal_info', {})]
        if len(vwap_devs) > 2 and len(set(vwap_devs)) > 1:
            try:
                corr, p_val = sp_stats.pearsonr(vwap_devs, pnls)
                result['vwap_deviation_correlation'] = {
                    'correlation': round(float(corr), 4),
                    'p_value': round(float(p_val), 4),
                    'interpretation': 'Smaller VWAP deviation = better outcomes' if corr < -0.3 else 'Weak correlation'
                }
            except:
                pass

        return result

    # ════════════════════════════════════════════════════════
    # 7. REGIME ANALYSIS (ENHANCED)
    # ════════════════════════════════════════════════════════

    def analyze_regime_with_whales(self, trades: List[Dict]) -> Dict:
        """
        Wyckoff phase breakdown → which phases produce winners?
        Whale direction vs market mode alignment.
        Whale momentum regime (loading/reducing/flat) vs trade outcome.
        """
        by_phase = defaultdict(list)
        by_alignment = defaultdict(list)
        by_momentum = defaultdict(list)

        for t in trades:
            sig_info = t.get('signal_info', {})

            # Wyckoff phase
            phase = sig_info.get('wyckoff_phase', 'unknown')
            by_phase[phase].append(t)

            # Whale direction vs market mode alignment
            whale_dir = sig_info.get('whale_direction', 'none')
            market_mode = sig_info.get('market_mode', 'unknown')
            aligned = 'ALIGNED' if whale_dir in market_mode or market_mode in whale_dir else 'MISALIGNED'
            by_alignment[aligned].append(t)

            # Whale momentum regime
            momentum = sig_info.get('whale_momentum_regime', 'flat')
            by_momentum[momentum].append(t)

        result = {
            'wyckoff_phase_breakdown': {},
            'whale_alignment_breakdown': {},
            'whale_momentum_breakdown': {},
        }

        # Phase analysis
        for phase, phase_trades in by_phase.items():
            if len(phase_trades) > 0:
                result['wyckoff_phase_breakdown'][phase] = self._compute_trade_metrics(phase_trades)

        # Alignment analysis
        for align, align_trades in by_alignment.items():
            if len(align_trades) > 0:
                result['whale_alignment_breakdown'][align] = self._compute_trade_metrics(align_trades)

        # Momentum analysis
        for momentum, momentum_trades in by_momentum.items():
            if len(momentum_trades) > 0:
                result['whale_momentum_breakdown'][momentum] = self._compute_trade_metrics(momentum_trades)

        return result

    # ════════════════════════════════════════════════════════
    # 8. V1 vs V2 COMPARISON
    # ════════════════════════════════════════════════════════

    def compare_v1_v2(self, v1_trades: List[Dict], v2_trades: List[Dict]) -> Dict:
        """
        Side-by-side metrics, overlap analysis, unique edges.
        """
        result = {
            'v1_metrics': self._compute_trade_metrics(v1_trades),
            'v2_metrics': self._compute_trade_metrics(v2_trades),
            'overlap_analysis': {},
            'unique_edges': {},
        }

        # Identify overlapping trades (same entry price + direction within 0.1%)
        def trade_key(t):
            return (round(t.get('entry_price', 0), 1), t.get('direction', ''))

        v1_keys = {trade_key(t): t for t in v1_trades}
        v2_keys = {trade_key(t): t for t in v2_trades}

        overlap_keys = set(v1_keys.keys()) & set(v2_keys.keys())
        v1_only_keys = set(v1_keys.keys()) - set(v2_keys.keys())
        v2_only_keys = set(v2_keys.keys()) - set(v1_keys.keys())

        # Overlap: who was right?
        overlap_trades = [v1_keys[k] for k in overlap_keys]
        overlap_v2 = [v2_keys[k] for k in overlap_keys]

        both_right = sum(1 for t1, t2 in zip(overlap_trades, overlap_v2)
                        if (t1.get('pnl_pct', 0) > 0) == (t2.get('pnl_pct', 0) > 0))
        v1_right = sum(1 for t1, t2 in zip(overlap_trades, overlap_v2)
                      if (t1.get('pnl_pct', 0) > 0) and (t2.get('pnl_pct', 0) < 0))
        v2_right = sum(1 for t1, t2 in zip(overlap_trades, overlap_v2)
                      if (t1.get('pnl_pct', 0) < 0) and (t2.get('pnl_pct', 0) > 0))

        result['overlap_analysis'] = {
            'overlap_count': len(overlap_keys),
            'both_right': both_right,
            'v1_better': v1_right,
            'v2_better': v2_right,
            'agreement_rate': round(both_right / len(overlap_keys), 3) if overlap_keys else 0,
        }

        # V1 unique edge
        v1_unique = [v1_keys[k] for k in v1_only_keys]
        # V2 unique edge
        v2_unique = [v2_keys[k] for k in v2_only_keys]

        result['unique_edges'] = {
            'v1_unique_metrics': self._compute_trade_metrics(v1_unique),
            'v2_unique_metrics': self._compute_trade_metrics(v2_unique),
            'v1_unique_count': len(v1_unique),
            'v2_unique_count': len(v2_unique),
        }

        return result

    # ════════════════════════════════════════════════════════
    # 9. REPORT GENERATOR
    # ════════════════════════════════════════════════════════

    def generate_v2_report(self, session_data: Dict) -> Dict:
        """
        Full comprehensive report with all sections.
        Includes what_went_well, what_went_wrong, improvements
        specifically for institutional/whale layer.
        """
        trades = session_data.get('trades', [])
        v1_trades = session_data.get('v1_trades', [])
        blocked_trades = session_data.get('blocked_trades', [])

        if not trades:
            return {'error': 'No trades provided for analysis'}

        report = {
            'generated_at': datetime.now().isoformat(),
            'period': session_data.get('period', {}),

            'whale_impact': self.analyze_whale_impact(trades),
            'whale_signals': self.analyze_whale_signals(trades),
            'institutional_filters': self.analyze_institutional_filters(trades, blocked_trades),
            'whale_alerts': self.analyze_whale_alerts(trades),
            'accumulation_zones': self.analyze_accumulation_zones(trades),
            'volume_profile': self.analyze_volume_profile_impact(trades),
            'regime_whales': self.analyze_regime_with_whales(trades),

            'what_went_well': [],
            'what_went_wrong': [],
            'improvements': [],
            'recommendations': [],
        }

        # Add V1 vs V2 if available
        if v1_trades:
            report['v1_v2_comparison'] = self.compare_v1_v2(v1_trades, trades)

        # Generate insights
        report['what_went_well'] = self._generate_what_went_well(report, trades)
        report['what_went_wrong'] = self._generate_what_went_wrong(report, trades)
        report['improvements'] = self._generate_improvements_list(report, trades)
        report['recommendations'] = self._generate_actionable_recommendations(report, trades)

        return report

    def _generate_what_went_well(self, report: Dict, trades: List[Dict]) -> List[str]:
        """Generate positive findings."""
        findings = []

        # Whale impact
        whale_impact = report.get('whale_impact', {})
        if whale_impact.get('whale_metrics', {}).get('win_rate', 0) > 60:
            wr = whale_impact['whale_metrics']['win_rate']
            findings.append(f"Whale trades have strong {wr:.1f}% win rate — signals are reliable")

        if whale_impact.get('statistical_test', {}).get('significant_at_95'):
            findings.append("Whale trades statistically outperform non-whale trades (p < 0.05)")

        # Best signals
        whale_sigs = report.get('whale_signals', {}).get('ranking', [])
        if whale_sigs:
            best = whale_sigs[0]
            if best.get('win_rate', 0) > 65:
                findings.append(
                    f"{best.get('signal_type', 'Unknown')} has {best.get('win_rate', 0):.1f}% "
                    f"win rate — your best signal"
                )

        # Accumulation zones
        accum = report.get('accumulation_zones', {})
        zone_wr = accum.get('at_zone_metrics', {}).get('win_rate', 0)
        not_zone_wr = accum.get('not_at_zone_metrics', {}).get('win_rate', 0)
        if zone_wr > not_zone_wr + 10:
            findings.append(
                f"Trades at accumulation zones win {zone_wr:.1f}% vs {not_zone_wr:.1f}% "
                f"without — strong setup validation"
            )

        # Regime alignment
        regime = report.get('regime_whales', {}).get('whale_alignment_breakdown', {})
        aligned_wr = regime.get('ALIGNED', {}).get('win_rate', 0)
        if aligned_wr > 60:
            findings.append(
                f"Whale direction aligned with market mode: {aligned_wr:.1f}% win rate"
            )

        return findings

    def _generate_what_went_wrong(self, report: Dict, trades: List[Dict]) -> List[str]:
        """Generate problem areas."""
        findings = []

        # Whale performance issues
        whale_impact = report.get('whale_impact', {})
        whale_wr = whale_impact.get('whale_metrics', {}).get('win_rate', 0)
        if whale_wr < 50:
            findings.append(f"Whale signals underperforming at {whale_wr:.1f}% win rate")

        # Poor filter precision
        filters = report.get('institutional_filters', {}).get('filters', {})
        for fname, finfo in filters.items():
            if finfo.get('precision', 0) < 0.4:
                findings.append(
                    f"Filter '{fname}' has low precision ({finfo['precision']:.1f}) — "
                    f"blocks too many good trades"
                )

        # High false alert rate
        alerts = report.get('whale_alerts', {}).get('false_alerts', {})
        for atype, false_rate in alerts.items():
            if false_rate > 0.6:
                findings.append(
                    f"Alert type '{atype}' has high false rate ({false_rate:.1f}) — "
                    f"needs recalibration"
                )

        # Regime misalignment
        regime = report.get('regime_whales', {}).get('whale_alignment_breakdown', {})
        misaligned_wr = regime.get('MISALIGNED', {}).get('win_rate', 0)
        aligned_wr = regime.get('ALIGNED', {}).get('win_rate', 0)
        if misaligned_wr > 0 and misaligned_wr > aligned_wr:
            findings.append(
                f"Whale-market misalignment actually performs better ({misaligned_wr:.1f}% "
                f"vs {aligned_wr:.1f}%) — consider contrarian opportunities"
            )

        return findings

    def _generate_improvements_list(self, report: Dict, trades: List[Dict]) -> List[str]:
        """Specific improvements for whale/institutional layer."""
        improvements = []

        # Filter tuning
        filters = report.get('institutional_filters', {})
        for fname, finfo in filters.get('filters', {}).items():
            if finfo.get('recommendation'):
                improvements.append(
                    f"{fname}: {finfo['recommendation']}"
                )

        # Signal concentration
        whale_sigs = report.get('whale_signals', {})
        total_sig_trades = sum(
            v.get('count', 0) for k, v in whale_sigs.items() if k != 'ranking'
        )
        if total_sig_trades > 0:
            ranking = whale_sigs.get('ranking', [])
            if ranking:
                top_signal = ranking[0]
                pct_in_top = (top_signal.get('count', 0) / total_sig_trades) * 100
                if pct_in_top > 60:
                    improvements.append(
                        f"Allocate more capital to {top_signal.get('signal_type', '')} — "
                        f"{top_signal.get('win_rate', 0):.1f}% win rate, "
                        f"{pct_in_top:.0f}% of whale trades"
                    )

        # Conviction thresholds
        for sig_type, sig_info in whale_sigs.items():
            if sig_type != 'ranking' and isinstance(sig_info, dict):
                threshold = sig_info.get('optimal_conviction_threshold', 0)
                if threshold > 0:
                    improvements.append(
                        f"{sig_type}: Set minimum whale confidence to {threshold:.0f} "
                        f"for optimal risk-adjusted returns"
                    )

        return improvements

    def _generate_actionable_recommendations(self, report: Dict, trades: List[Dict]) -> List[str]:
        """Specific trader action items."""
        recs = []

        # Best signal: size up
        whale_sigs = report.get('whale_signals', {}).get('ranking', [])
        if whale_sigs:
            best = whale_sigs[0]
            if best.get('win_rate', 0) > 70:
                recs.append(
                    f"INCREASE SIZE: {best.get('signal_type', '')} has "
                    f"{best.get('win_rate', 0):.1f}% win rate. "
                    f"Try 30% larger position on high-conviction setups."
                )

        # Worst signal: disable
        if whale_sigs and len(whale_sigs) > 1:
            worst = whale_sigs[-1]
            if worst.get('win_rate', 0) < 40:
                recs.append(
                    f"DISABLE: {worst.get('signal_type', '')} win rate "
                    f"({worst.get('win_rate', 0):.1f}%) below breakeven. "
                    f"Pause this signal type until recalibrated."
                )

        # Zone priority
        accum = report.get('accumulation_zones', {})
        zone_wr = accum.get('at_zone_metrics', {}).get('win_rate', 0)
        if zone_wr > 65:
            recs.append(
                f"FILTER ENTRY: Always verify accumulation zones before entry. "
                f"Zone-based trades win {zone_wr:.1f}%."
            )

        # Filter management
        filters = report.get('institutional_filters', {})
        for fname, finfo in filters.get('filters', {}).items():
            if finfo.get('precision', 0) > 0.75:
                recs.append(
                    f"TRUST {fname.upper()}: {finfo['precision']:.1%} precision — "
                    f"respect its blocks."
                )

        return recs

    # ════════════════════════════════════════════════════════
    # 10. WHALE FINDINGS (Plain-English Insights)
    # ════════════════════════════════════════════════════════

    def generate_whale_findings(self, trades: List[Dict]) -> List[str]:
        """
        Generate plain-English insights for traders.
        Examples:
        - "whale_trap_reversal has 85% win rate — allocate 30% more size"
        - "institutional_flow filter blocked 23 trades. 18 would have been losers — keep it"
        - "Trades at accumulation zones win 72% vs 48% without — always check"
        """
        findings = []

        # Signal quality findings
        whale_sigs = self.analyze_whale_signals(trades)
        ranking = whale_sigs.get('ranking', [])

        for i, sig in enumerate(ranking):
            sig_type = sig.get('signal_type', '')
            wr = sig.get('win_rate', 0)
            avg_pnl = sig.get('avg_pnl', 0)
            count = sig.get('count', 0) if 'count' in sig else len([t for t in trades if t.get('signal_type') == sig_type])

            if i == 0 and wr > 70:
                findings.append(
                    f"{sig_type} has {wr:.0f}% win rate on {count} trades. "
                    f"Your best signal. Consider allocating 30% more size here."
                )
            elif i == len(ranking) - 1 and wr < 45:
                findings.append(
                    f"{sig_type} has only {wr:.0f}% win rate on {count} trades. "
                    f"Consider pausing or disabling this signal."
                )
            elif avg_pnl > 0.4:
                findings.append(
                    f"{sig_type} averages +{avg_pnl:.2f}% per trade ({wr:.0f}% WR). Strong edge."
                )

        # Filter effectiveness
        filters = self.analyze_institutional_filters(trades)
        filters_info = filters.get('filters', {})

        for fname, finfo in filters_info.items():
            blocks = finfo.get('total_blocks', 0)
            precision = finfo.get('precision', 0)
            blocked_losses = finfo.get('blocks_that_would_have_lost', 0)

            if blocks > 10 and precision > 0.75:
                findings.append(
                    f"{fname} filter blocked {blocks} trades. "
                    f"{blocked_losses} would have been losers ({precision:.0%} precision). Keep it."
                )
            elif blocks > 5 and precision < 0.4:
                findings.append(
                    f"{fname} filter blocked {blocks} trades but only {blocked_losses} "
                    f"would have lost. Consider relaxing this filter."
                )

        # Zone analysis
        accum = self.analyze_accumulation_zones(trades)
        zone_wr = accum.get('at_zone_metrics', {}).get('win_rate', 0)
        non_zone_wr = accum.get('not_at_zone_metrics', {}).get('win_rate', 0)

        if zone_wr > 0 and zone_wr > non_zone_wr + 10:
            findings.append(
                f"Trades at accumulation zones win {zone_wr:.0f}% vs {non_zone_wr:.0f}% "
                f"without. Always check for active zones."
            )

        # Volume profile
        vol = self.analyze_volume_profile_impact(trades)
        hvn_wr = vol.get('hvn_metrics', {}).get('win_rate', 0)
        lvn_wr = vol.get('lvn_metrics', {}).get('win_rate', 0)

        if hvn_wr > 0 and hvn_wr > lvn_wr + 15:
            findings.append(
                f"Trades at HVN (High Volume Nodes) have {hvn_wr:.0f}% win rate "
                f"vs {lvn_wr:.0f}% at LVN. Prefer high volume areas."
            )

        # V1 vs V2
        if len(findings) < 5:  # Add comparison insights if room
            # This would need v1_trades data which may not be available
            pass

        return findings

    # ════════════════════════════════════════════════════════
    # HELPER METHODS
    # ════════════════════════════════════════════════════════

    def _compute_trade_metrics(self, trades: List[Dict]) -> Dict:
        """
        Compute standardized metrics for a list of trades.
        Returns dict with: count, win_rate, avg_pnl, avg_mfe, avg_mae,
                           sharpe, max_dd, avg_hold_time
        """
        if not trades:
            return {
                'count': 0, 'win_rate': 0.0, 'avg_pnl': 0.0, 'avg_mfe': 0.0,
                'avg_mae': 0.0, 'sharpe': 0.0, 'max_dd': 0.0, 'avg_hold_time': 0.0
            }

        pnls = np.array([t.get('pnl_pct', 0) for t in trades])
        mfes = np.array([t.get('mfe', 0) for t in trades])
        maes = np.array([abs(t.get('mae', 0)) for t in trades])

        wins = pnls[pnls > 0]
        losses = pnls[pnls < 0]

        win_rate = len(wins) / len(pnls) * 100 if len(pnls) > 0 else 0

        # Sharpe ratio
        mean_r = float(np.mean(pnls))
        std_r = float(np.std(pnls, ddof=1)) if len(pnls) > 1 else 1e-10
        sharpe = (mean_r / (std_r + 1e-10)) * np.sqrt(252) if std_r > 0 else 0

        # Max drawdown
        cum = np.cumsum(pnls)
        peak = np.maximum.accumulate(cum)
        dd = peak - cum
        max_dd = float(np.max(dd)) if len(dd) > 0 else 0

        # Avg hold time
        hold_times = [t.get('hold_seconds', 0) for t in trades if 'hold_seconds' in t]
        avg_hold = float(np.mean(hold_times)) if hold_times else 0

        return {
            'count': len(trades),
            'win_rate': round(win_rate, 1),
            'avg_pnl': round(float(np.mean(pnls)), 4),
            'avg_win': round(float(np.mean(wins)), 4) if len(wins) > 0 else 0,
            'avg_loss': round(float(np.mean(losses)), 4) if len(losses) > 0 else 0,
            'avg_mfe': round(float(np.mean(mfes)), 4),
            'avg_mae': round(float(np.mean(maes)), 4),
            'sharpe': round(sharpe, 2),
            'max_dd': round(max_dd, 4),
            'avg_hold_time': round(avg_hold, 1),
            'total_pnl': round(float(np.sum(pnls)), 4),
        }


# ============================================================
# MAIN: Load backtest data and run full analysis
# ============================================================

if __name__ == '__main__':
    import json

    # Load backtest results
    with open('/sessions/dazzling-epic-planck/mnt/outputs/backtest_v2_30day.json') as f:
        backtest_data = json.load(f)

    # Extract trades
    v2_trades = backtest_data.get('v2_results', {}).get('trades', [])
    v1_trades = backtest_data.get('v1_results', {}).get('trades', [])
    period = backtest_data.get('period', {})

    print("=" * 80)
    print("WHALE & INSTITUTIONAL ANALYSIS — V2 BACKTEST RESULTS")
    print("=" * 80)
    print(f"Period: {period.get('start')} to {period.get('end')}")
    print(f"V2 Trades: {len(v2_trades)} | V1 Trades: {len(v1_trades)}")
    print()

    # Initialize analyzer
    analyzer = TradeAnalyzerV2()

    # Run analyses
    print("\n1. WHALE PERFORMANCE IMPACT")
    print("-" * 80)
    whale_impact = analyzer.analyze_whale_impact(v2_trades)
    print(f"Whale trades: {whale_impact['whale_count']} | Non-whale: {whale_impact['non_whale_count']}")
    whale_wr = whale_impact.get('whale_metrics', {}).get('win_rate', 0)
    non_whale_wr = whale_impact.get('non_whale_metrics', {}).get('win_rate', 0)
    print(f"Whale win rate: {whale_wr:.1f}% | Non-whale: {non_whale_wr:.1f}%")
    sig_test = whale_impact.get('statistical_test', {})
    if sig_test:
        print(f"Statistical significance (p-value): {sig_test.get('p_value', 'N/A')}")
        print(f"Interpretation: {sig_test.get('interpretation', 'N/A')}")

    print("\n2. WHALE SIGNALS BREAKDOWN")
    print("-" * 80)
    whale_sigs = analyzer.analyze_whale_signals(v2_trades)
    ranking = whale_sigs.get('ranking', [])
    for i, sig in enumerate(ranking[:5], 1):
        print(f"{i}. {sig.get('signal_type', 'Unknown')}: "
              f"{sig.get('win_rate', 0):.1f}% WR, "
              f"+{sig.get('avg_pnl', 0):.3f}% avg PnL, "
              f"Sharpe {sig.get('sharpe', 0):.2f}")

    print("\n3. INSTITUTIONAL FILTER EFFECTIVENESS")
    print("-" * 80)
    filters = analyzer.analyze_institutional_filters(v2_trades)
    print(f"Total blocked: {filters.get('total_blocked', 0)}")
    for fname, finfo in filters.get('filters', {}).items():
        print(f"  {fname}: {finfo.get('precision', 0):.1%} precision, "
              f"blocked {finfo.get('total_blocks', 0)} trades")

    print("\n4. ACCUMULATION ZONE ANALYSIS")
    print("-" * 80)
    accum = analyzer.analyze_accumulation_zones(v2_trades)
    at_zone_wr = accum.get('at_zone_metrics', {}).get('win_rate', 0)
    not_zone_wr = accum.get('not_at_zone_metrics', {}).get('win_rate', 0)
    print(f"At zones: {at_zone_wr:.1f}% WR ({accum.get('at_zone_count', 0)} trades)")
    print(f"Not at zones: {not_zone_wr:.1f}% WR ({accum.get('not_at_zone_count', 0)} trades)")

    print("\n5. WHALE FINDINGS (ACTIONABLE INSIGHTS)")
    print("-" * 80)
    findings = analyzer.generate_whale_findings(v2_trades)
    for finding in findings[:10]:
        print(f"  • {finding}")

    print("\n6. FULL REPORT GENERATION")
    print("-" * 80)
    session_data = {
        'trades': v2_trades,
        'v1_trades': v1_trades,
        'blocked_trades': [],
        'period': period,
    }
    report = analyzer.generate_v2_report(session_data)

    print("Report sections generated:")
    for key in report.keys():
        if not key.startswith('_'):
            print(f"  ✓ {key}")

    print("\nWhat went well:")
    for item in report.get('what_went_well', [])[:5]:
        print(f"  ✓ {item}")

    print("\nRecommendations:")
    for item in report.get('recommendations', [])[:5]:
        print(f"  → {item}")

    print("\n" + "=" * 80)
    print("Analysis complete. Full report available in memory.")
    print("=" * 80)
