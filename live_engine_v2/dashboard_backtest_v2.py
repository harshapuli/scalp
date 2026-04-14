#!/usr/bin/env python3
"""
V2 Backtest Results Dashboard Generator
========================================
Generates a self-contained HTML dashboard from backtest results.
"""

import json
import sys
from datetime import datetime
from collections import defaultdict
import numpy as np
from pathlib import Path

# Add the live_engine_v2 to path so we can import TradeAnalyzerV2
sys.path.insert(0, str(Path(__file__).parent))

from trade_analyzer_v2 import TradeAnalyzerV2


class DashboardGenerator:
    def __init__(self, backtest_json_path, output_html_path):
        self.backtest_json_path = backtest_json_path
        self.output_html_path = output_html_path
        self.data = None
        self.analyzer = TradeAnalyzerV2()
        self.analysis = {}

    def load_data(self):
        """Load backtest JSON data."""
        with open(self.backtest_json_path, 'r') as f:
            self.data = json.load(f)
        print(f"Loaded backtest data with {len(self.data['v1_results']['trades'])} V1 trades "
              f"and {len(self.data['v2_results']['trades'])} V2 trades")

    def run_analysis(self):
        """Run TradeAnalyzerV2 on the trades."""
        v1_trades = self.data['v1_results']['trades']
        v2_trades = self.data['v2_results']['trades']

        # Run all analyses
        self.analysis['whale_impact'] = self.analyzer.analyze_whale_impact(v2_trades)
        self.analysis['whale_signals'] = self.analyzer.analyze_whale_signals(v2_trades)
        self.analysis['whale_alerts'] = self.analyzer.analyze_whale_alerts(v2_trades)
        self.analysis['accumulation_zones'] = self.analyzer.analyze_accumulation_zones(v2_trades)
        self.analysis['volume_profile'] = self.analyzer.analyze_volume_profile_impact(v2_trades)
        self.analysis['v1_v2_compare'] = self.analyzer.compare_v1_v2(v1_trades, v2_trades)

        # Generate findings
        self.analysis['whale_findings'] = self.analyzer.generate_whale_findings(v2_trades)

        # Generate comprehensive report
        report_session_data = {
            'v1_results': self.data['v1_results'],
            'v2_results': self.data['v2_results'],
            'whale_analysis': self.data.get('whale_analysis', {}),
        }
        self.analysis['v2_report'] = self.analyzer.generate_v2_report(report_session_data)

        print("Analysis complete")

    def compute_kpis(self):
        """Compute key performance indicators."""
        v1_trades = self.data['v1_results']['trades']
        v2_trades = self.data['v2_results']['trades']

        def get_metrics(trades):
            if not trades:
                return {
                    'total_trades': 0,
                    'wins': 0,
                    'losses': 0,
                    'win_rate': 0,
                    'avg_pnl': 0,
                    'total_pnl': 0,
                    'sharpe': 0,
                    'max_dd': 0,
                    'avg_mfe': 0,
                    'avg_mae': 0,
                }

            wins = sum(1 for t in trades if t.get('win') in ['True', True])
            losses = len(trades) - wins
            pnls = [t.get('pnl_pct', 0) for t in trades]
            mfes = [t.get('mfe', 0) for t in trades]
            maes = [t.get('mae', 0) for t in trades]

            total_pnl = sum(pnls)
            avg_pnl = total_pnl / len(trades) if trades else 0

            # Sharpe ratio
            if len(pnls) > 1:
                std = np.std(pnls)
                sharpe = (np.mean(pnls) / std) * np.sqrt(252) if std > 0 else 0
            else:
                sharpe = 0

            # Max drawdown
            cumsum = np.cumsum(pnls)
            if len(cumsum) > 0:
                running_max = np.maximum.accumulate(cumsum)
                dd = (cumsum - running_max)
                max_dd = np.min(dd) if len(dd) > 0 else 0
            else:
                max_dd = 0

            return {
                'total_trades': len(trades),
                'wins': wins,
                'losses': losses,
                'win_rate': (wins / len(trades) * 100) if trades else 0,
                'avg_pnl': avg_pnl,
                'total_pnl': total_pnl,
                'sharpe': sharpe,
                'max_dd': max_dd,
                'avg_mfe': np.mean(mfes) if mfes else 0,
                'avg_mae': np.mean(maes) if maes else 0,
            }

        return {
            'v1': get_metrics(v1_trades),
            'v2': get_metrics(v2_trades),
        }

    def get_per_ticker_comparison(self):
        """Get per-ticker comparison data."""
        per_ticker = self.data.get('per_ticker', {})
        result = []
        for ticker in self.data.get('tickers', []):
            if ticker in per_ticker:
                pt = per_ticker[ticker]
                result.append({
                    'ticker': ticker,
                    'v1_trades': pt.get('v1_trades', 0),
                    'v1_pnl': pt.get('v1_pnl', 0),
                    'v1_win_rate': pt.get('v1_win_rate', 0),
                    'v2_trades': pt.get('v2_trades', 0),
                    'v2_pnl': pt.get('v2_pnl', 0),
                    'v2_win_rate': pt.get('v2_win_rate', 0),
                })
        return result

    def get_cumulative_pnl_data(self):
        """Get cumulative PnL data for both V1 and V2."""
        v1_trades = self.data['v1_results']['trades']
        v2_trades = self.data['v2_results']['trades']

        def make_cumulative(trades):
            pnls = [t.get('pnl_pct', 0) for t in trades]
            cumulative = np.cumsum(pnls).tolist()
            return cumulative

        return {
            'v1': make_cumulative(v1_trades),
            'v2': make_cumulative(v2_trades),
        }

    def get_whale_signal_table(self):
        """Get whale signal performance table."""
        v2_trades = self.data['v2_results']['trades']

        # Group by signal type
        by_signal = defaultdict(list)
        for t in v2_trades:
            sig_type = t.get('signal_type', 'unknown')
            by_signal[sig_type].append(t)

        result = []
        for sig_type, trades in sorted(by_signal.items()):
            if not trades:
                continue

            wins = sum(1 for t in trades if t.get('win') in ['True', True])
            pnls = [t.get('pnl_pct', 0) for t in trades]
            avg_pnl = sum(pnls) / len(pnls) if pnls else 0

            result.append({
                'signal_type': sig_type,
                'count': len(trades),
                'win_rate': (wins / len(trades) * 100) if trades else 0,
                'avg_pnl': avg_pnl,
                'total_pnl': sum(pnls),
            })

        return sorted(result, key=lambda x: x['count'], reverse=True)

    def get_whale_vs_non_whale(self):
        """Get whale vs non-whale comparison."""
        v2_trades = self.data['v2_results']['trades']

        whale_trades = []
        non_whale_trades = []

        for t in v2_trades:
            sig_info = t.get('signal_info', {})
            whale_conf = sig_info.get('whale_confidence', 0)
            sig_type = t.get('signal_type', '')

            is_whale = (whale_conf > 0 or 'whale' in sig_type.lower())
            if is_whale:
                whale_trades.append(t)
            else:
                non_whale_trades.append(t)

        def get_stats(trades):
            if not trades:
                return {'count': 0, 'win_rate': 0, 'avg_pnl': 0, 'total_pnl': 0}
            wins = sum(1 for t in trades if t.get('win') in ['True', True])
            pnls = [t.get('pnl_pct', 0) for t in trades]
            return {
                'count': len(trades),
                'win_rate': (wins / len(trades) * 100) if trades else 0,
                'avg_pnl': sum(pnls) / len(pnls) if pnls else 0,
                'total_pnl': sum(pnls),
            }

        return {
            'whale': get_stats(whale_trades),
            'non_whale': get_stats(non_whale_trades),
        }

    def get_filter_breakdown(self):
        """Get filter block breakdown."""
        filter_blocks = self.data.get('whale_analysis', {}).get('filter_blocks', {})
        result = []
        for filter_name, count in filter_blocks.items():
            result.append({'filter': filter_name, 'blocked': count})
        return result

    def get_trade_log(self):
        """Get full trade log data."""
        v2_trades = self.data['v2_results']['trades']

        result = []
        for i, t in enumerate(v2_trades):
            sig_info = t.get('signal_info', {})
            whale_conf = sig_info.get('whale_confidence', 0)

            result.append({
                'id': i + 1,
                'direction': t.get('direction', ''),
                'signal_type': t.get('signal_type', ''),
                'entry_price': round(t.get('entry_price', 0), 4),
                'exit_price': round(t.get('exit_price', 0), 4),
                'pnl_pct': round(t.get('pnl_pct', 0) * 100, 2),
                'mfe': round(t.get('mfe', 0) * 100, 2),
                'mae': round(t.get('mae', 0) * 100, 2),
                'whale_conf': whale_conf,
                'inst_score': sig_info.get('institutional_score', 0),
                'outcome': 'WIN' if t.get('win') in ['True', True] else 'LOSS',
            })
        return result

    def get_signal_distribution(self):
        """Get signal type distribution for whale alerts."""
        v2_trades = self.data['v2_results']['trades']
        by_signal = defaultdict(int)

        for t in v2_trades:
            sig_type = t.get('signal_type', 'unknown')
            by_signal[sig_type] += 1

        return [{'label': k, 'value': v} for k, v in sorted(by_signal.items())]

    def generate_html(self):
        """Generate the complete HTML dashboard."""
        kpis = self.compute_kpis()
        per_ticker = self.get_per_ticker_comparison()
        cumulative_pnl = self.get_cumulative_pnl_data()
        whale_signals = self.get_whale_signal_table()
        whale_vs_nw = self.get_whale_vs_non_whale()
        filter_breakdown = self.get_filter_breakdown()
        trade_log = self.get_trade_log()
        signal_dist = self.get_signal_distribution()

        html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>V2 Backtest Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@3.9.1/dist/chart.min.js"></script>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, sans-serif;
            background-color: #0d1117;
            color: #c9d1d9;
            padding: 20px;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}

        header {{
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid #30363d;
        }}

        h1 {{
            font-size: 2.5em;
            margin-bottom: 10px;
            color: #58a6ff;
        }}

        .header-info {{
            font-size: 0.9em;
            color: #8b949e;
        }}

        .tabs {{
            display: flex;
            gap: 10px;
            margin-bottom: 30px;
            border-bottom: 1px solid #30363d;
            flex-wrap: wrap;
        }}

        .tab-button {{
            padding: 12px 24px;
            background: none;
            border: none;
            color: #8b949e;
            cursor: pointer;
            font-size: 1em;
            transition: all 0.3s;
            border-bottom: 2px solid transparent;
            margin-bottom: -1px;
        }}

        .tab-button:hover {{
            color: #58a6ff;
        }}

        .tab-button.active {{
            color: #58a6ff;
            border-bottom-color: #58a6ff;
        }}

        .tab-content {{
            display: none;
        }}

        .tab-content.active {{
            display: block;
        }}

        .kpi-grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}

        .kpi-card {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 20px;
            text-align: center;
        }}

        .kpi-label {{
            color: #8b949e;
            font-size: 0.85em;
            margin-bottom: 10px;
        }}

        .kpi-value {{
            font-size: 2em;
            font-weight: bold;
            color: #58a6ff;
        }}

        .comparison-table {{
            width: 100%;
            border-collapse: collapse;
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            overflow: hidden;
            margin-bottom: 30px;
        }}

        .comparison-table thead {{
            background: #0d1117;
            border-bottom: 1px solid #30363d;
        }}

        .comparison-table th {{
            padding: 12px;
            text-align: left;
            color: #58a6ff;
            font-weight: 600;
        }}

        .comparison-table td {{
            padding: 12px;
            border-bottom: 1px solid #30363d;
        }}

        .comparison-table tr:last-child td {{
            border-bottom: none;
        }}

        .comparison-table tbody tr:hover {{
            background: #0d1117;
        }}

        .chart-container {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 20px;
            margin-bottom: 30px;
            position: relative;
            height: 400px;
        }}

        .chart-container.small {{
            height: 300px;
        }}

        .grid-2 {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 30px;
        }}

        @media (max-width: 768px) {{
            .grid-2 {{
                grid-template-columns: 1fr;
            }}
        }}

        .green-card {{
            background: #1a3a1a;
            border-left: 4px solid #3fb950;
            padding: 15px;
            margin-bottom: 10px;
            border-radius: 4px;
        }}

        .red-card {{
            background: #3a1a1a;
            border-left: 4px solid #f85149;
            padding: 15px;
            margin-bottom: 10px;
            border-radius: 4px;
        }}

        .yellow-card {{
            background: #3a3a1a;
            border-left: 4px solid #d29922;
            padding: 15px;
            margin-bottom: 10px;
            border-radius: 4px;
        }}

        .card-title {{
            font-weight: 600;
            margin-bottom: 5px;
        }}

        .card-content {{
            font-size: 0.9em;
            color: #8b949e;
        }}

        .metric-row {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 20px;
            margin-bottom: 20px;
        }}

        @media (max-width: 768px) {{
            .metric-row {{
                grid-template-columns: 1fr;
            }}
        }}

        .metric-box {{
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            padding: 15px;
        }}

        .metric-label {{
            color: #8b949e;
            font-size: 0.85em;
            margin-bottom: 8px;
        }}

        .metric-value {{
            font-size: 1.5em;
            font-weight: bold;
            color: #58a6ff;
        }}

        table.trade-log {{
            width: 100%;
            border-collapse: collapse;
            background: #161b22;
            border: 1px solid #30363d;
            border-radius: 6px;
            overflow: auto;
            font-size: 0.9em;
        }}

        table.trade-log thead {{
            background: #0d1117;
            border-bottom: 1px solid #30363d;
        }}

        table.trade-log th {{
            padding: 10px;
            text-align: left;
            color: #58a6ff;
            font-weight: 600;
        }}

        table.trade-log td {{
            padding: 10px;
            border-bottom: 1px solid #30363d;
        }}

        table.trade-log tbody tr:hover {{
            background: #0d1117;
        }}

        .win {{
            color: #3fb950;
            font-weight: 600;
        }}

        .loss {{
            color: #f85149;
            font-weight: 600;
        }}

        .positive {{
            color: #3fb950;
        }}

        .negative {{
            color: #f85149;
        }}

        .section-title {{
            font-size: 1.5em;
            margin: 30px 0 20px 0;
            color: #58a6ff;
            border-bottom: 1px solid #30363d;
            padding-bottom: 10px;
        }}

        .table-wrapper {{
            overflow-x: auto;
            margin-bottom: 30px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>V2 Backtest Results Dashboard</h1>
            <div class="header-info">
                <p>Period: {self.data['period']['start']} to {self.data['period']['end']}</p>
                <p>Trading Days: {self.data['period']['trading_days']}</p>
            </div>
        </header>

        <div class="tabs">
            <button class="tab-button active" onclick="switchTab('tab1')">V1 vs V2 Head-to-Head</button>
            <button class="tab-button" onclick="switchTab('tab2')">Whale Intelligence</button>
            <button class="tab-button" onclick="switchTab('tab3')">Institutional Analysis</button>
            <button class="tab-button" onclick="switchTab('tab4')">Trade Log</button>
            <button class="tab-button" onclick="switchTab('tab5')">Findings & Recommendations</button>
        </div>

        <!-- TAB 1: V1 vs V2 Head-to-Head -->
        <div id="tab1" class="tab-content active">
            <div class="kpi-grid">
                <div class="kpi-card">
                    <div class="kpi-label">V1 Win Rate</div>
                    <div class="kpi-value">{kpis['v1']['win_rate']:.1f}%</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">V2 Win Rate</div>
                    <div class="kpi-value">{kpis['v2']['win_rate']:.1f}%</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">V1 Avg PnL</div>
                    <div class="kpi-value {('positive' if kpis['v1']['avg_pnl'] > 0 else 'negative')}">{kpis['v1']['avg_pnl']:.3f}%</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">V2 Avg PnL</div>
                    <div class="kpi-value {('positive' if kpis['v2']['avg_pnl'] > 0 else 'negative')}">{kpis['v2']['avg_pnl']:.3f}%</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">V1 Sharpe</div>
                    <div class="kpi-value">{kpis['v1']['sharpe']:.2f}</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">V2 Sharpe</div>
                    <div class="kpi-value">{kpis['v2']['sharpe']:.2f}</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">V1 Max DD</div>
                    <div class="kpi-value">{kpis['v1']['max_dd']:.3f}%</div>
                </div>
                <div class="kpi-card">
                    <div class="kpi-label">V2 Max DD</div>
                    <div class="kpi-value">{kpis['v2']['max_dd']:.3f}%</div>
                </div>
            </div>

            <h2 class="section-title">Cumulative P&L Comparison</h2>
            <div class="chart-container">
                <canvas id="cumulativePnLChart"></canvas>
            </div>

            <h2 class="section-title">Per-Ticker Comparison</h2>
            <div class="metric-row">
                <div class="chart-container">
                    <canvas id="tickerWinRateChart"></canvas>
                </div>
                <div class="chart-container">
                    <canvas id="tickerPnLChart"></canvas>
                </div>
            </div>

            <h2 class="section-title">Detailed Metrics Comparison</h2>
            <div class="table-wrapper">
                <table class="comparison-table">
                    <thead>
                        <tr>
                            <th>Metric</th>
                            <th>V1</th>
                            <th>V2</th>
                            <th>Improvement</th>
                        </tr>
                    </thead>
                    <tbody>
                        <tr>
                            <td>Total Trades</td>
                            <td>{kpis['v1']['total_trades']}</td>
                            <td>{kpis['v2']['total_trades']}</td>
                            <td class="{('positive' if kpis['v2']['total_trades'] > kpis['v1']['total_trades'] else 'negative')}">{kpis['v2']['total_trades'] - kpis['v1']['total_trades']:+d}</td>
                        </tr>
                        <tr>
                            <td>Wins</td>
                            <td>{kpis['v1']['wins']}</td>
                            <td>{kpis['v2']['wins']}</td>
                            <td class="{('positive' if kpis['v2']['wins'] > kpis['v1']['wins'] else 'negative')}">{kpis['v2']['wins'] - kpis['v1']['wins']:+d}</td>
                        </tr>
                        <tr>
                            <td>Win Rate</td>
                            <td>{kpis['v1']['win_rate']:.1f}%</td>
                            <td>{kpis['v2']['win_rate']:.1f}%</td>
                            <td class="{('positive' if kpis['v2']['win_rate'] > kpis['v1']['win_rate'] else 'negative')}">{kpis['v2']['win_rate'] - kpis['v1']['win_rate']:+.1f}%</td>
                        </tr>
                        <tr>
                            <td>Total P&L</td>
                            <td class="{('positive' if kpis['v1']['total_pnl'] > 0 else 'negative')}">{kpis['v1']['total_pnl']:.2f}%</td>
                            <td class="{('positive' if kpis['v2']['total_pnl'] > 0 else 'negative')}">{kpis['v2']['total_pnl']:.2f}%</td>
                            <td class="{('positive' if kpis['v2']['total_pnl'] > kpis['v1']['total_pnl'] else 'negative')}">{kpis['v2']['total_pnl'] - kpis['v1']['total_pnl']:+.2f}%</td>
                        </tr>
                        <tr>
                            <td>Avg P&L</td>
                            <td class="{('positive' if kpis['v1']['avg_pnl'] > 0 else 'negative')}">{kpis['v1']['avg_pnl']:.4f}%</td>
                            <td class="{('positive' if kpis['v2']['avg_pnl'] > 0 else 'negative')}">{kpis['v2']['avg_pnl']:.4f}%</td>
                            <td class="{('positive' if kpis['v2']['avg_pnl'] > kpis['v1']['avg_pnl'] else 'negative')}">{kpis['v2']['avg_pnl'] - kpis['v1']['avg_pnl']:+.4f}%</td>
                        </tr>
                        <tr>
                            <td>Sharpe Ratio</td>
                            <td>{kpis['v1']['sharpe']:.4f}</td>
                            <td>{kpis['v2']['sharpe']:.4f}</td>
                            <td class="{('positive' if kpis['v2']['sharpe'] > kpis['v1']['sharpe'] else 'negative')}">{kpis['v2']['sharpe'] - kpis['v1']['sharpe']:+.4f}</td>
                        </tr>
                        <tr>
                            <td>Max Drawdown</td>
                            <td>{kpis['v1']['max_dd']:.4f}%</td>
                            <td>{kpis['v2']['max_dd']:.4f}%</td>
                            <td class="{('positive' if kpis['v2']['max_dd'] > kpis['v1']['max_dd'] else 'negative')}">{kpis['v2']['max_dd'] - kpis['v1']['max_dd']:+.4f}%</td>
                        </tr>
                        <tr>
                            <td>Avg MFE</td>
                            <td>{kpis['v1']['avg_mfe']:.4f}%</td>
                            <td>{kpis['v2']['avg_mfe']:.4f}%</td>
                            <td class="{('positive' if kpis['v2']['avg_mfe'] > kpis['v1']['avg_mfe'] else 'negative')}">{kpis['v2']['avg_mfe'] - kpis['v1']['avg_mfe']:+.4f}%</td>
                        </tr>
                        <tr>
                            <td>Avg MAE</td>
                            <td>{kpis['v1']['avg_mae']:.4f}%</td>
                            <td>{kpis['v2']['avg_mae']:.4f}%</td>
                            <td class="{('positive' if kpis['v2']['avg_mae'] > kpis['v1']['avg_mae'] else 'negative')}">{kpis['v2']['avg_mae'] - kpis['v1']['avg_mae']:+.4f}%</td>
                        </tr>
                    </tbody>
                </table>
            </div>
        </div>

        <!-- TAB 2: Whale Intelligence -->
        <div id="tab2" class="tab-content">
            <h2 class="section-title">Whale Signal Performance</h2>
            <div class="table-wrapper">
                <table class="comparison-table">
                    <thead>
                        <tr>
                            <th>Signal Type</th>
                            <th>Count</th>
                            <th>Win Rate</th>
                            <th>Avg P&L</th>
                            <th>Total P&L</th>
                        </tr>
                    </thead>
                    <tbody>
"""
        for sig in whale_signals:
            html_content += f"""                        <tr>
                            <td>{sig['signal_type']}</td>
                            <td>{sig['count']}</td>
                            <td>{sig['win_rate']:.1f}%</td>
                            <td class="{('positive' if sig['avg_pnl'] > 0 else 'negative')}">{sig['avg_pnl']:.4f}%</td>
                            <td class="{('positive' if sig['total_pnl'] > 0 else 'negative')}">{sig['total_pnl']:.2f}%</td>
                        </tr>
"""
        html_content += """                    </tbody>
                </table>
            </div>

            <h2 class="section-title">Whale vs Non-Whale Comparison</h2>
            <div class="metric-row">
                <div class="metric-box">
                    <div class="metric-label">Whale-Confirmed Trades</div>
                    <div class="metric-value">{whale_vs_nw['whale']['count']}</div>
                    <div class="metric-label" style="margin-top: 10px;">Win Rate</div>
                    <div class="metric-value">{whale_vs_nw['whale']['win_rate']:.1f}%</div>
                </div>
                <div class="metric-box">
                    <div class="metric-label">Non-Whale Trades</div>
                    <div class="metric-value">{whale_vs_nw['non_whale']['count']}</div>
                    <div class="metric-label" style="margin-top: 10px;">Win Rate</div>
                    <div class="metric-value">{whale_vs_nw['non_whale']['win_rate']:.1f}%</div>
                </div>
            </div>

            <div class="metric-row">
                <div class="chart-container">
                    <canvas id="whaleComparisonChart"></canvas>
                </div>
                <div class="chart-container">
                    <canvas id="signalDistributionChart"></canvas>
                </div>
            </div>
        </div>

        <!-- TAB 3: Institutional Analysis -->
        <div id="tab3" class="tab-content">
            <h2 class="section-title">Filter Block Breakdown</h2>
            <div class="metric-row">
"""
        for fb in filter_breakdown:
            html_content += f"""                <div class="metric-box">
                    <div class="metric-label">{fb['filter'].replace('_', ' ').title()}</div>
                    <div class="metric-value">{fb['blocked']}</div>
                </div>
"""
        html_content += """            </div>

            <h2 class="section-title">Institutional Score Distribution</h2>
            <div class="chart-container">
                <canvas id="institutionalScoreChart"></canvas>
            </div>

            <h2 class="section-title">Whale Confidence Distribution</h2>
            <div class="chart-container">
                <canvas id="whaleConfidenceChart"></canvas>
            </div>

            <h2 class="section-title">Institutional Score vs P&L Correlation</h2>
            <div class="chart-container">
                <canvas id="instScorePnLChart"></canvas>
            </div>
        </div>

        <!-- TAB 4: Trade Log -->
        <div id="tab4" class="tab-content">
            <h2 class="section-title">Complete Trade Log (V2)</h2>
            <div class="table-wrapper">
                <table class="trade-log">
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Direction</th>
                            <th>Signal Type</th>
                            <th>Entry</th>
                            <th>Exit</th>
                            <th>P&L %</th>
                            <th>MFE %</th>
                            <th>MAE %</th>
                            <th>Whale Conf</th>
                            <th>Inst Score</th>
                            <th>Outcome</th>
                        </tr>
                    </thead>
                    <tbody>
"""
        for trade in trade_log[:100]:  # Limit to first 100 for performance
            outcome_class = 'win' if trade['outcome'] == 'WIN' else 'loss'
            pnl_class = 'positive' if trade['pnl_pct'] > 0 else 'negative'
            html_content += f"""                        <tr>
                            <td>{trade['id']}</td>
                            <td>{trade['direction']}</td>
                            <td>{trade['signal_type']}</td>
                            <td>${trade['entry_price']}</td>
                            <td>${trade['exit_price']}</td>
                            <td class="{pnl_class}">{trade['pnl_pct']:+.2f}%</td>
                            <td class="positive">{trade['mfe']:+.2f}%</td>
                            <td class="negative">{trade['mae']:+.2f}%</td>
                            <td>{trade['whale_conf']}</td>
                            <td>{trade['inst_score']}</td>
                            <td class="{outcome_class}">{trade['outcome']}</td>
                        </tr>
"""
        html_content += f"""                    </tbody>
                </table>
            </div>
            <p style="text-align: center; color: #8b949e; margin-top: 20px;">Showing first 100 trades of {len(trade_log)}</p>
        </div>

        <!-- TAB 5: Findings & Recommendations -->
        <div id="tab5" class="tab-content">
            <h2 class="section-title">What Went Well</h2>
"""

        # Generate findings from the report
        what_went_well = self.analysis['v2_report'].get('what_went_well', [])
        for finding in what_went_well[:5]:
            html_content += f"""            <div class="green-card">
                <div class="card-title">✓ {finding}</div>
            </div>
"""

        html_content += """            <h2 class="section-title">What Went Wrong</h2>
"""

        what_went_wrong = self.analysis['v2_report'].get('what_went_wrong', [])
        for finding in what_went_wrong[:5]:
            html_content += f"""            <div class="red-card">
                <div class="card-title">✗ {finding}</div>
            </div>
"""

        html_content += """            <h2 class="section-title">Improvements & Opportunities</h2>
"""

        improvements = self.analysis['v2_report'].get('improvements', [])
        for imp in improvements[:5]:
            html_content += f"""            <div class="yellow-card">
                <div class="card-title">• {imp}</div>
            </div>
"""

        html_content += """            <h2 class="section-title">Actionable Recommendations</h2>
"""

        recommendations = self.analysis['v2_report'].get('recommendations', [])
        for rec in recommendations[:8]:
            html_content += f"""            <div class="metric-box">
                <div class="card-content">{rec}</div>
            </div>
"""

        html_content += """            <h2 class="section-title">Plain-English Whale Findings</h2>
"""

        whale_findings = self.analysis['whale_findings']
        for finding in whale_findings[:10]:
            html_content += f"""            <div class="metric-box">
                <div class="card-content">{finding}</div>
            </div>
"""

        html_content += """        </div>
    </div>

    <script>
        // Tab switching
        function switchTab(tabName) {
            const tabs = document.querySelectorAll('.tab-content');
            tabs.forEach(tab => tab.classList.remove('active'));
            document.getElementById(tabName).classList.add('active');

            const buttons = document.querySelectorAll('.tab-button');
            buttons.forEach(btn => btn.classList.remove('active'));
            event.target.classList.add('active');
        }

        // Chart.js default options
        const chartOptions = {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
                legend: {
                    labels: { color: '#8b949e' }
                }
            },
            scales: {
                x: { ticks: { color: '#8b949e' }, grid: { color: '#30363d' } },
                y: { ticks: { color: '#8b949e' }, grid: { color: '#30363d' } }
            }
        };

        // Cumulative P&L Chart
        const cumulativeCtx = document.getElementById('cumulativePnLChart').getContext('2d');
        const cumulativePnLData = {v1: JSON.parse('{json.dumps(cumulative_pnl["v1"])}'), v2: JSON.parse('{json.dumps(cumulative_pnl["v2"])}')};
        new Chart(cumulativeCtx, {{
            type: 'line',
            data: {{
                labels: Array.from({{length: Math.max(cumulativePnLData.v1.length, cumulativePnLData.v2.length)}}, (_, i) => i + 1),
                datasets: [
                    {{
                        label: 'V1 Cumulative P&L',
                        data: cumulativePnLData.v1,
                        borderColor: '#f85149',
                        backgroundColor: 'rgba(248, 81, 73, 0.1)',
                        tension: 0.1,
                        borderWidth: 2
                    }},
                    {{
                        label: 'V2 Cumulative P&L',
                        data: cumulativePnLData.v2,
                        borderColor: '#3fb950',
                        backgroundColor: 'rgba(63, 185, 80, 0.1)',
                        tension: 0.1,
                        borderWidth: 2
                    }}
                ]
            }},
            options: {{...chartOptions, plugins: {{...chartOptions.plugins, legend: {{display: true}}}}}}
        }});

        // Ticker Win Rate Chart
        const tickerWinRateCtx = document.getElementById('tickerWinRateChart').getContext('2d');
        const perTickerData = JSON.parse('{json.dumps(per_ticker)}');
        new Chart(tickerWinRateCtx, {{
            type: 'bar',
            data: {{
                labels: perTickerData.map(t => t.ticker),
                datasets: [
                    {{
                        label: 'V1 Win Rate %',
                        data: perTickerData.map(t => t.v1_win_rate),
                        backgroundColor: '#f85149',
                        borderColor: '#f85149'
                    }},
                    {{
                        label: 'V2 Win Rate %',
                        data: perTickerData.map(t => t.v2_win_rate),
                        backgroundColor: '#3fb950',
                        borderColor: '#3fb950'
                    }}
                ]
            }},
            options: {{...chartOptions}}
        }});

        // Ticker P&L Chart
        const tickerPnLCtx = document.getElementById('tickerPnLChart').getContext('2d');
        new Chart(tickerPnLCtx, {{
            type: 'bar',
            data: {{
                labels: perTickerData.map(t => t.ticker),
                datasets: [
                    {{
                        label: 'V1 P&L %',
                        data: perTickerData.map(t => t.v1_pnl),
                        backgroundColor: '#f85149',
                        borderColor: '#f85149'
                    }},
                    {{
                        label: 'V2 P&L %',
                        data: perTickerData.map(t => t.v2_pnl),
                        backgroundColor: '#3fb950',
                        borderColor: '#3fb950'
                    }}
                ]
            }},
            options: {{...chartOptions}}
        }});

        // Whale vs Non-Whale Comparison
        const whaleComparisonCtx = document.getElementById('whaleComparisonChart').getContext('2d');
        const whaleCompData = JSON.parse('{json.dumps(whale_vs_nw)}');
        new Chart(whaleComparisonCtx, {{
            type: 'bar',
            data: {{
                labels: ['Whale-Confirmed', 'Non-Whale'],
                datasets: [
                    {{
                        label: 'Count',
                        data: [whaleCompData.whale.count, whaleCompData.non_whale.count],
                        backgroundColor: '#58a6ff',
                        borderColor: '#58a6ff'
                    }},
                    {{
                        label: 'Win Rate %',
                        data: [whaleCompData.whale.win_rate, whaleCompData.non_whale.win_rate],
                        backgroundColor: '#3fb950',
                        borderColor: '#3fb950'
                    }}
                ]
            }},
            options: {{...chartOptions}}
        }});

        // Signal Distribution Pie Chart
        const signalDistCtx = document.getElementById('signalDistributionChart').getContext('2d');
        const signalDistData = JSON.parse('{json.dumps(signal_dist)}');
        new Chart(signalDistCtx, {{
            type: 'doughnut',
            data: {{
                labels: signalDistData.map(s => s.label),
                datasets: [{{
                    data: signalDistData.map(s => s.value),
                    backgroundColor: ['#58a6ff', '#3fb950', '#f85149', '#d29922', '#a371f7', '#79c0ff']
                }}]
            }},
            options: {{...chartOptions, plugins: {{...chartOptions.plugins, legend: {{position: 'bottom'}}}}}}
        }});

        // Institutional Score Distribution
        const instScoreCtx = document.getElementById('institutionalScoreChart').getContext('2d');
        const trades = JSON.parse('{json.dumps(trade_log)}');
        const instScores = trades.map(t => t.inst_score);
        const instScoreHist = {{}};
        instScores.forEach(score => {{
            const bucket = Math.floor(score / 10) * 10;
            instScoreHist[bucket] = (instScoreHist[bucket] || 0) + 1;
        }});
        new Chart(instScoreCtx, {{
            type: 'bar',
            data: {{
                labels: Object.keys(instScoreHist).sort((a, b) => a - b).map(k => k + '-' + (parseInt(k) + 10)),
                datasets: [{{
                    label: 'Frequency',
                    data: Object.keys(instScoreHist).sort((a, b) => a - b).map(k => instScoreHist[k]),
                    backgroundColor: '#58a6ff',
                    borderColor: '#58a6ff'
                }}]
            }},
            options: {{...chartOptions}}
        }});

        // Whale Confidence Distribution
        const whaleConfCtx = document.getElementById('whaleConfidenceChart').getContext('2d');
        const whaleConfs = trades.map(t => t.whale_conf);
        const whaleConfHist = {{}};
        whaleConfs.forEach(conf => {{
            const bucket = Math.floor(conf / 10) * 10;
            whaleConfHist[bucket] = (whaleConfHist[bucket] || 0) + 1;
        }});
        new Chart(whaleConfCtx, {{
            type: 'bar',
            data: {{
                labels: Object.keys(whaleConfHist).sort((a, b) => a - b).map(k => k + '-' + (parseInt(k) + 10)),
                datasets: [{{
                    label: 'Frequency',
                    data: Object.keys(whaleConfHist).sort((a, b) => a - b).map(k => whaleConfHist[k]),
                    backgroundColor: '#3fb950',
                    borderColor: '#3fb950'
                }}]
            }},
            options: {{...chartOptions}}
        }});

        // Institutional Score vs P&L Scatter
        const instScorePnLCtx = document.getElementById('instScorePnLChart').getContext('2d');
        const instScorePnLData = trades.map(t => ({{x: t.inst_score, y: t.pnl_pct}}));
        new Chart(instScorePnLCtx, {{
            type: 'scatter',
            data: {{
                datasets: [{{
                    label: 'Inst Score vs P&L',
                    data: instScorePnLData,
                    backgroundColor: '#58a6ff',
                    borderColor: '#58a6ff'
                }}]
            }},
            options: {{
                ...chartOptions,
                scales: {{
                    x: {{...chartOptions.scales.x, title: {{display: true, text: 'Institutional Score'}}}},
                    y: {{...chartOptions.scales.y, title: {{display: true, text: 'P&L %'}}}}
                }}
            }}
        }});
    </script>
</body>
</html>
"""
        return html_content

    def run(self):
        """Run the complete dashboard generation."""
        print("Loading backtest data...")
        self.load_data()

        print("Running analysis...")
        self.run_analysis()

        print("Generating HTML...")
        html_content = self.generate_html()

        print(f"Writing to {self.output_html_path}...")
        with open(self.output_html_path, 'w') as f:
            f.write(html_content)

        print(f"Done! Dashboard created at {self.output_html_path}")


if __name__ == '__main__':
    backtest_file = '/sessions/dazzling-epic-planck/mnt/outputs/backtest_v2_30day.json'
    output_file = '/sessions/dazzling-epic-planck/mnt/outputs/v2_backtest_dashboard.html'

    generator = DashboardGenerator(backtest_file, output_file)
    generator.run()
