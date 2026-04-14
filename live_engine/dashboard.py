"""
TRADE DASHBOARD GENERATOR
===========================
Generates a self-contained interactive HTML dashboard from trade logs.
Opens in any browser — no server needed.

Usage:
    python dashboard.py                          # Uses latest trade log
    python dashboard.py trade_logs/trades_2026-04-12.json
    python dashboard.py --demo                   # Generate with demo data
"""

import json
import os
import sys
import time
import numpy as np
from datetime import datetime
from collections import defaultdict
from trade_analyzer import TradeAnalyzer


def generate_demo_data():
    """Generate realistic demo trade data for testing the dashboard."""
    np.random.seed(42)
    trades = []
    strategies = [
        ('smc_order_block_1min', '1min', 'scalp', 'core_ob'),
        ('smc_order_block_5min', '5min', '0dte', 'core_ob'),
        ('smc_order_block_15min', '15min', 'weekly', 'core_ob'),
        ('smc_order_block_1hr', '1hr', 'weekly', 'core_ob'),
        ('judas_swing_5min', '5min', '0dte', 'sweep'),
        ('breaker_block_15min', '15min', 'weekly', 'sweep'),
        ('displacement_engine_5min', '5min', '0dte', 'displacement'),
        ('ict_reversal_15min', '15min', 'weekly', 'confluence'),
    ]
    tickers = ['SPY', 'QQQ', 'AAPL', 'MSFT', 'NVDA', 'AMZN', 'META', 'TSLA']
    exit_reasons = ['TP1', 'TP2', 'TRAIL', 'STOP', 'MAX_HOLD']

    base_time = time.time() - 86400 * 7  # 7 days ago

    for i in range(80):
        strat_name, tf, cat, module = strategies[np.random.randint(0, len(strategies))]
        ticker = tickers[np.random.randint(0, len(tickers))]
        direction = 'CALL' if np.random.random() > 0.45 else 'PUT'
        entry_price = 100 + np.random.uniform(-20, 80)
        win = np.random.random() < 0.72  # 72% win rate

        if win:
            pnl_pct = np.random.uniform(0.05, 1.2)
            exit_reason = np.random.choice(['TP1', 'TP2', 'TRAIL'], p=[0.4, 0.3, 0.3])
        else:
            pnl_pct = -np.random.uniform(0.05, 0.5)
            exit_reason = np.random.choice(['STOP', 'MAX_HOLD', 'TRAIL'], p=[0.6, 0.2, 0.2])

        mfe = max(pnl_pct, np.random.uniform(0, 0.8))
        mae = min(pnl_pct, -np.random.uniform(0, 0.4))
        entry_time = base_time + i * 3600 + np.random.uniform(0, 1800)
        hold_seconds = np.random.uniform(60, 7200)
        tp1_hit = exit_reason in ('TP2', 'TRAIL') or (exit_reason == 'TP1')
        outcome = 'FULL_RUNNER' if exit_reason == 'TP2' else ('TP1_ONLY' if exit_reason == 'TP1' else ('STOPPED' if exit_reason == 'STOP' else ('WIN' if pnl_pct > 0 else 'LOSS')))

        rsi = np.random.uniform(30, 70)
        adx = np.random.uniform(15, 40)
        atr_ratio = np.random.uniform(0.6, 2.0)

        trade = {
            'trade_id': f'T{i+1:04d}',
            'ticker': ticker,
            'strategy': strat_name,
            'timeframe': tf,
            'direction': direction,
            'module': module,
            'category': cat,
            'entry_time': entry_time,
            'entry_price': round(entry_price, 2),
            'exit_price': round(entry_price * (1 + pnl_pct / 100), 2),
            'exit_time': entry_time + hold_seconds,
            'exit_reason': exit_reason,
            'pnl': round(pnl_pct * 10, 2),  # simulated $ PnL
            'pnl_pct': round(pnl_pct, 4),
            'mfe': round(mfe, 4),
            'mae': round(mae, 4),
            'hold_seconds': round(hold_seconds),
            'tp1_hit': tp1_hit,
            'outcome': outcome,
            'entry_context': {
                'RSI_14': round(rsi, 2),
                'ADX_14': round(adx, 2),
                'atr_ratio': round(atr_ratio, 3),
                'market_mode': np.random.choice(['TREND', 'REVERSAL', 'NO_TRADE'], p=[0.6, 0.3, 0.1]),
                'ema_bias': np.random.choice(['LONG', 'SHORT', 'NEUTRAL'], p=[0.5, 0.35, 0.15]),
            },
            'filter_results': [
                {'filter': 'kill_zone', 'passed': True},
                {'filter': 'volatility', 'passed': True},
                {'filter': 'liquidity_sweep', 'passed': True},
                {'filter': 'displacement', 'passed': True},
                {'filter': 'rsi_gate', 'passed': np.random.random() > 0.1},
                {'filter': 'entry_refinement', 'passed': True},
            ],
            'strategy_config': {
                'stop_pct': round(np.random.uniform(0.1, 0.5), 2),
                'target_pct': round(np.random.uniform(0.15, 0.8), 2),
                'hold_bars': np.random.randint(8, 20),
            },
        }
        trades.append(trade)

    blocked_summary = {
        'smc_order_block_1min': 145,
        'smc_order_block_5min': 89,
        'judas_swing_5min': 67,
        'breaker_block_15min': 34,
        'displacement_engine_5min': 56,
    }

    stats = {
        'signals_detected': 520,
        'signals_blocked': 390,
        'signals_passed_all_filters': 130,
        'trades_entered': 80,
        'entries_skipped': 50,
        'filter_kill_zone_block': 78,
        'filter_volatility_block': 45,
        'filter_liquidity_sweep_block': 112,
        'filter_rsi_gate_block': 34,
        'filter_displacement_block': 28,
        'filter_ema_bias_block': 55,
        'blocked_by_sweep_required': 112,
        'blocked_by_kill_zone': 78,
        'blocked_by_volatility_ranging': 45,
        'blocked_by_rsi_gate': 34,
        'blocked_by_ema_bias': 55,
    }

    return {
        'session_start': base_time,
        'session_end': time.time(),
        'stats': stats,
        'completed_trades': trades,
        'active_trades': [],
        'blocked_signals_summary': blocked_summary,
        'missed_signals': [],
        'state_transitions': [],
        'errors': [],
    }


def generate_dashboard_html(session_data: dict, analysis_report: dict = None) -> str:
    """Generate a self-contained HTML dashboard."""

    trades = session_data.get('completed_trades', [])
    stats = session_data.get('stats', {})
    blocked = session_data.get('blocked_signals_summary', {})

    # Run analysis if not provided
    if analysis_report is None:
        analyzer = TradeAnalyzer()
        analysis_report = analyzer.generate_report(session_data)

    # Prepare data for JS
    trades_json = json.dumps(trades, default=str)
    analysis_json = json.dumps(analysis_report, default=str)
    stats_json = json.dumps(stats, default=str)
    deep_json = json.dumps(analysis_report.get('deep', {}), default=str)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>ICT/SMC Trade Analysis Dashboard</title>
<script src="https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.4.1/chart.umd.min.js"></script>
<style>
:root {{
    --bg: #0f1117;
    --card: #1a1d29;
    --border: #2a2d3a;
    --text: #e4e4e7;
    --text-dim: #8b8d97;
    --accent: #6366f1;
    --green: #22c55e;
    --red: #ef4444;
    --yellow: #eab308;
    --blue: #3b82f6;
    --purple: #a855f7;
    --orange: #f97316;
}}
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ background: var(--bg); color: var(--text); font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', system-ui, sans-serif; padding: 20px; }}
.header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 24px; padding-bottom: 16px; border-bottom: 1px solid var(--border); }}
.header h1 {{ font-size: 22px; font-weight: 600; }}
.header .date {{ color: var(--text-dim); font-size: 14px; }}
.filters {{ display: flex; gap: 12px; margin-bottom: 20px; flex-wrap: wrap; }}
.filters select {{ background: var(--card); color: var(--text); border: 1px solid var(--border); padding: 8px 12px; border-radius: 8px; font-size: 13px; cursor: pointer; }}
.kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 16px; margin-bottom: 24px; }}
.kpi {{ background: var(--card); border-radius: 12px; padding: 20px; border: 1px solid var(--border); }}
.kpi .label {{ font-size: 12px; color: var(--text-dim); text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 8px; }}
.kpi .value {{ font-size: 28px; font-weight: 700; }}
.kpi .sub {{ font-size: 12px; color: var(--text-dim); margin-top: 4px; }}
.kpi .value.green {{ color: var(--green); }}
.kpi .value.red {{ color: var(--red); }}
.kpi .value.blue {{ color: var(--blue); }}
.kpi .value.yellow {{ color: var(--yellow); }}
.charts-grid {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }}
@media (max-width: 900px) {{ .charts-grid {{ grid-template-columns: 1fr; }} }}
.chart-card {{ background: var(--card); border-radius: 12px; padding: 20px; border: 1px solid var(--border); }}
.chart-card h3 {{ font-size: 14px; font-weight: 600; margin-bottom: 16px; color: var(--text-dim); }}
.chart-card canvas {{ max-height: 280px; }}
.findings {{ display: grid; grid-template-columns: 1fr 1fr; gap: 20px; margin-bottom: 24px; }}
@media (max-width: 900px) {{ .findings {{ grid-template-columns: 1fr; }} }}
.finding-card {{ background: var(--card); border-radius: 12px; padding: 20px; border: 1px solid var(--border); }}
.finding-card h3 {{ font-size: 14px; font-weight: 600; margin-bottom: 12px; }}
.finding-card h3.green {{ color: var(--green); }}
.finding-card h3.red {{ color: var(--red); }}
.finding-card h3.blue {{ color: var(--blue); }}
.finding-item {{ font-size: 13px; line-height: 1.6; padding: 8px 0; border-bottom: 1px solid var(--border); }}
.finding-item:last-child {{ border-bottom: none; }}
.finding-item .icon {{ margin-right: 6px; }}
.table-card {{ background: var(--card); border-radius: 12px; padding: 20px; border: 1px solid var(--border); margin-bottom: 24px; overflow-x: auto; }}
.table-card h3 {{ font-size: 14px; font-weight: 600; margin-bottom: 16px; color: var(--text-dim); }}
table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
th {{ text-align: left; padding: 10px 12px; background: rgba(255,255,255,0.03); color: var(--text-dim); font-weight: 500; cursor: pointer; border-bottom: 1px solid var(--border); white-space: nowrap; }}
th:hover {{ color: var(--accent); }}
td {{ padding: 10px 12px; border-bottom: 1px solid var(--border); white-space: nowrap; }}
tr:hover td {{ background: rgba(99,102,241,0.05); }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 4px; font-size: 11px; font-weight: 600; }}
.badge-win {{ background: rgba(34,197,94,0.15); color: var(--green); }}
.badge-loss {{ background: rgba(239,68,68,0.15); color: var(--red); }}
.badge-call {{ background: rgba(59,130,246,0.15); color: var(--blue); }}
.badge-put {{ background: rgba(168,85,247,0.15); color: var(--purple); }}
.tabs {{ display: flex; gap: 4px; margin-bottom: 20px; }}
.tab {{ padding: 8px 16px; background: var(--card); border: 1px solid var(--border); border-radius: 8px; cursor: pointer; font-size: 13px; color: var(--text-dim); }}
.tab.active {{ background: var(--accent); color: white; border-color: var(--accent); }}
.tab-content {{ display: none; }}
.tab-content.active {{ display: block; }}
.full-width {{ grid-column: 1 / -1; }}
</style>
</head>
<body>

<div class="header">
    <h1>ICT/SMC Trade Analysis</h1>
    <div class="date" id="dateRange"></div>
</div>

<div class="filters">
    <select id="filterStrategy" onchange="applyFilters()"><option value="all">All Strategies</option></select>
    <select id="filterTicker" onchange="applyFilters()"><option value="all">All Tickers</option></select>
    <select id="filterTF" onchange="applyFilters()"><option value="all">All Timeframes</option></select>
    <select id="filterDirection" onchange="applyFilters()">
        <option value="all">All Directions</option>
        <option value="CALL">CALL Only</option>
        <option value="PUT">PUT Only</option>
    </select>
    <select id="filterOutcome" onchange="applyFilters()">
        <option value="all">All Outcomes</option>
        <option value="winners">Winners</option>
        <option value="losers">Losers</option>
    </select>
</div>

<div class="kpi-grid" id="kpiGrid"></div>

<div class="tabs">
    <div class="tab active" onclick="switchTab('overview')">Overview</div>
    <div class="tab" onclick="switchTab('analysis')">Analysis</div>
    <div class="tab" onclick="switchTab('filters')">Filter Pipeline</div>
    <div class="tab" onclick="switchTab('trades')">Trade Log</div>
    <div class="tab" onclick="switchTab('deep')">Deep Analysis</div>
</div>

<!-- OVERVIEW TAB -->
<div id="tab-overview" class="tab-content active">
    <div class="charts-grid">
        <div class="chart-card"><h3>Cumulative P&L (%)</h3><canvas id="pnlChart"></canvas></div>
        <div class="chart-card"><h3>Win Rate by Strategy</h3><canvas id="strategyChart"></canvas></div>
        <div class="chart-card"><h3>P&L by Timeframe</h3><canvas id="tfChart"></canvas></div>
        <div class="chart-card"><h3>Outcome Distribution</h3><canvas id="outcomeChart"></canvas></div>
        <div class="chart-card"><h3>MFE vs MAE Scatter</h3><canvas id="mfeChart"></canvas></div>
        <div class="chart-card"><h3>P&L by Hour</h3><canvas id="hourChart"></canvas></div>
    </div>
</div>

<!-- ANALYSIS TAB -->
<div id="tab-analysis" class="tab-content">
    <div class="findings">
        <div class="finding-card"><h3 class="green">What Went Well</h3><div id="wentWell"></div></div>
        <div class="finding-card"><h3 class="red">What Went Wrong</h3><div id="wentWrong"></div></div>
        <div class="finding-card full-width"><h3 class="blue">Improvements</h3><div id="improvements"></div></div>
    </div>
    <div class="table-card">
        <h3>Individual Trade Analyses</h3>
        <table id="analysisTable">
            <thead><tr>
                <th onclick="sortTable('analysisTable',0)">Trade</th>
                <th onclick="sortTable('analysisTable',1)">Strategy</th>
                <th onclick="sortTable('analysisTable',2)">Signal</th>
                <th onclick="sortTable('analysisTable',3)">Entry</th>
                <th onclick="sortTable('analysisTable',4)">Exit</th>
                <th onclick="sortTable('analysisTable',5)">P&L</th>
                <th onclick="sortTable('analysisTable',6)">Key Finding</th>
            </tr></thead>
            <tbody id="analysisBody"></tbody>
        </table>
    </div>
</div>

<!-- FILTERS TAB -->
<div id="tab-filters" class="tab-content">
    <div class="charts-grid">
        <div class="chart-card"><h3>Signals: Detected vs Blocked vs Traded</h3><canvas id="funnelChart"></canvas></div>
        <div class="chart-card"><h3>Block Reasons</h3><canvas id="blockChart"></canvas></div>
    </div>
    <div class="table-card">
        <h3>Filter Pipeline Effectiveness</h3>
        <table>
            <thead><tr><th>Filter</th><th>Blocks</th><th>% of Total Blocks</th><th>Assessment</th></tr></thead>
            <tbody id="filterBody"></tbody>
        </table>
    </div>
</div>

<!-- DEEP ANALYSIS TAB -->
<div id="tab-deep" class="tab-content">
    <div class="kpi-grid" id="riskKpis"></div>
    <div class="charts-grid">
        <div class="chart-card"><h3>Context Correlation: RSI vs P&L</h3><canvas id="rsiCorr"></canvas></div>
        <div class="chart-card"><h3>Context Correlation: ADX vs P&L</h3><canvas id="adxCorr"></canvas></div>
        <div class="chart-card"><h3>Counterfactual: What-If Stop/Target</h3><canvas id="counterfactualChart"></canvas></div>
        <div class="chart-card"><h3>Runner Capture by Exit Reason</h3><canvas id="runnerChart"></canvas></div>
    </div>
    <div class="findings">
        <div class="finding-card"><h3 class="blue">MFE/MAE Calibration</h3><div id="mfeMaeFindings"></div></div>
        <div class="finding-card"><h3 class="green">Context Edge Map</h3><div id="contextFindings"></div></div>
        <div class="finding-card"><h3 class="red">Filter $ Effectiveness</h3><div id="filterDollarFindings"></div></div>
        <div class="finding-card"><h3 style="color:var(--purple)">Runner & Streak Analysis</h3><div id="runnerFindings"></div></div>
    </div>
    <div class="table-card">
        <h3>Per-Strategy Deep Metrics</h3>
        <table>
            <thead><tr><th>Strategy</th><th>Trades</th><th>WR%</th><th>Total P&L</th><th>Avg Win</th><th>Avg Loss</th><th>PF</th><th>Avg MFE</th><th>Avg MAE</th><th>Avg Hold</th><th>Dir Bias</th></tr></thead>
            <tbody id="stratDeepBody"></tbody>
        </table>
    </div>
</div>

<!-- TRADES TAB -->
<div id="tab-trades" class="tab-content">
    <div class="table-card">
        <h3>All Trades</h3>
        <table id="tradesTable">
            <thead><tr>
                <th onclick="sortTable('tradesTable',0)">ID</th>
                <th onclick="sortTable('tradesTable',1)">Time</th>
                <th onclick="sortTable('tradesTable',2)">Ticker</th>
                <th onclick="sortTable('tradesTable',3)">Dir</th>
                <th onclick="sortTable('tradesTable',4)">Strategy</th>
                <th onclick="sortTable('tradesTable',5)">TF</th>
                <th onclick="sortTable('tradesTable',6)">Entry</th>
                <th onclick="sortTable('tradesTable',7)">Exit</th>
                <th onclick="sortTable('tradesTable',8)">P&L %</th>
                <th onclick="sortTable('tradesTable',9)">MFE</th>
                <th onclick="sortTable('tradesTable',10)">MAE</th>
                <th onclick="sortTable('tradesTable',11)">Outcome</th>
                <th onclick="sortTable('tradesTable',12)">Exit Reason</th>
                <th onclick="sortTable('tradesTable',13)">Hold</th>
            </tr></thead>
            <tbody id="tradesBody"></tbody>
        </table>
    </div>
</div>

<script>
const ALL_TRADES = {trades_json};
const ANALYSIS = {analysis_json};
const STATS = {stats_json};
const DEEP = {deep_json};
let filteredTrades = [...ALL_TRADES];

// Populate filters
function populateFilters() {{
    const strats = [...new Set(ALL_TRADES.map(t => t.strategy))].sort();
    const tickers = [...new Set(ALL_TRADES.map(t => t.ticker))].sort();
    const tfs = [...new Set(ALL_TRADES.map(t => t.timeframe))].sort();
    const sel = (id, vals) => {{
        const el = document.getElementById(id);
        vals.forEach(v => {{ const o = document.createElement('option'); o.value = v; o.textContent = v; el.appendChild(o); }});
    }};
    sel('filterStrategy', strats);
    sel('filterTicker', tickers);
    sel('filterTF', tfs);

    // Date range
    if (ALL_TRADES.length) {{
        const times = ALL_TRADES.map(t => t.entry_time).filter(Boolean);
        if (times.length) {{
            const d1 = new Date(Math.min(...times) * 1000).toLocaleDateString();
            const d2 = new Date(Math.max(...times) * 1000).toLocaleDateString();
            document.getElementById('dateRange').textContent = d1 + ' — ' + d2 + ' | ' + ALL_TRADES.length + ' trades';
        }}
    }}
}}

function applyFilters() {{
    const s = document.getElementById('filterStrategy').value;
    const tk = document.getElementById('filterTicker').value;
    const tf = document.getElementById('filterTF').value;
    const dir = document.getElementById('filterDirection').value;
    const out = document.getElementById('filterOutcome').value;
    filteredTrades = ALL_TRADES.filter(t => {{
        if (s !== 'all' && t.strategy !== s) return false;
        if (tk !== 'all' && t.ticker !== tk) return false;
        if (tf !== 'all' && t.timeframe !== tf) return false;
        if (dir !== 'all' && t.direction !== dir) return false;
        if (out === 'winners' && t.pnl_pct <= 0) return false;
        if (out === 'losers' && t.pnl_pct >= 0) return false;
        return true;
    }});
    renderAll();
}}

function renderKPIs() {{
    const t = filteredTrades;
    const wins = t.filter(x => x.pnl_pct > 0);
    const losses = t.filter(x => x.pnl_pct < 0);
    const totalPnl = t.reduce((s, x) => s + x.pnl_pct, 0);
    const wr = t.length ? (wins.length / t.length * 100).toFixed(1) : '0';
    const avgWin = wins.length ? (wins.reduce((s, x) => s + x.pnl_pct, 0) / wins.length).toFixed(3) : '0';
    const avgLoss = losses.length ? (losses.reduce((s, x) => s + x.pnl_pct, 0) / losses.length).toFixed(3) : '0';
    const pf = losses.length ? (Math.abs(wins.reduce((s, x) => s + x.pnl_pct, 0)) / Math.abs(losses.reduce((s, x) => s + x.pnl_pct, 0))).toFixed(2) : '∞';
    const avgMFE = t.length ? (t.reduce((s, x) => s + (x.mfe || 0), 0) / t.length).toFixed(3) : '0';
    const avgMAE = t.length ? (t.reduce((s, x) => s + Math.abs(x.mae || 0), 0) / t.length).toFixed(3) : '0';

    document.getElementById('kpiGrid').innerHTML = `
        <div class="kpi"><div class="label">Total Trades</div><div class="value blue">${{t.length}}</div><div class="sub">${{wins.length}}W / ${{losses.length}}L</div></div>
        <div class="kpi"><div class="label">Win Rate</div><div class="value ${{parseFloat(wr) >= 60 ? 'green' : parseFloat(wr) >= 45 ? 'yellow' : 'red'}}">${{wr}}%</div><div class="sub">Target: 65%+</div></div>
        <div class="kpi"><div class="label">Total P&L</div><div class="value ${{totalPnl >= 0 ? 'green' : 'red'}}">${{totalPnl >= 0 ? '+' : ''}}${{totalPnl.toFixed(3)}}%</div><div class="sub">Cumulative</div></div>
        <div class="kpi"><div class="label">Profit Factor</div><div class="value ${{parseFloat(pf) >= 1.5 ? 'green' : parseFloat(pf) >= 1 ? 'yellow' : 'red'}}">${{pf}}</div><div class="sub">Target: 1.5+</div></div>
        <div class="kpi"><div class="label">Avg Win</div><div class="value green">+${{avgWin}}%</div><div class="sub">per winner</div></div>
        <div class="kpi"><div class="label">Avg Loss</div><div class="value red">${{avgLoss}}%</div><div class="sub">per loser</div></div>
        <div class="kpi"><div class="label">Avg MFE</div><div class="value blue">${{avgMFE}}%</div><div class="sub">max favorable</div></div>
        <div class="kpi"><div class="label">Avg MAE</div><div class="value yellow">${{avgMAE}}%</div><div class="sub">max adverse</div></div>
    `;
}}

// Charts
let charts = {{}};
function destroyCharts() {{ Object.values(charts).forEach(c => c.destroy()); charts = {{}}; }}

function renderCharts() {{
    destroyCharts();
    const t = filteredTrades;
    const chartOpts = {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ labels: {{ color: '#8b8d97', font: {{ size: 11 }} }} }} }}, scales: {{ x: {{ ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }} }}, y: {{ ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }} }} }} }};

    // Cumulative PnL
    const cumPnl = []; let running = 0;
    t.forEach((x, i) => {{ running += x.pnl_pct; cumPnl.push(running); }});
    charts.pnl = new Chart(document.getElementById('pnlChart'), {{
        type: 'line',
        data: {{ labels: t.map((_, i) => i + 1), datasets: [{{ label: 'Cumulative P&L %', data: cumPnl, borderColor: '#6366f1', backgroundColor: 'rgba(99,102,241,0.1)', fill: true, tension: 0.3, pointRadius: 2 }}] }},
        options: {{ ...chartOpts, plugins: {{ ...chartOpts.plugins, legend: {{ display: false }} }} }}
    }});

    // Win rate by strategy
    const byStrat = {{}};
    t.forEach(x => {{
        if (!byStrat[x.strategy]) byStrat[x.strategy] = {{ w: 0, l: 0, pnl: 0 }};
        if (x.pnl_pct > 0) byStrat[x.strategy].w++; else byStrat[x.strategy].l++;
        byStrat[x.strategy].pnl += x.pnl_pct;
    }});
    const stratNames = Object.keys(byStrat).sort((a, b) => (byStrat[b].w / (byStrat[b].w + byStrat[b].l)) - (byStrat[a].w / (byStrat[a].w + byStrat[a].l)));
    charts.strategy = new Chart(document.getElementById('strategyChart'), {{
        type: 'bar',
        data: {{ labels: stratNames.map(s => s.replace('smc_order_block_', 'OB_').replace('_engine', '').replace('_block', '')), datasets: [
            {{ label: 'Win Rate %', data: stratNames.map(s => ((byStrat[s].w / (byStrat[s].w + byStrat[s].l)) * 100).toFixed(1)), backgroundColor: stratNames.map(s => (byStrat[s].w / (byStrat[s].w + byStrat[s].l)) >= 0.6 ? '#22c55e88' : '#ef444488'), borderColor: stratNames.map(s => (byStrat[s].w / (byStrat[s].w + byStrat[s].l)) >= 0.6 ? '#22c55e' : '#ef4444'), borderWidth: 1 }}
        ] }},
        options: {{ ...chartOpts, indexAxis: 'y' }}
    }});

    // PnL by timeframe
    const byTF = {{}};
    t.forEach(x => {{
        if (!byTF[x.timeframe]) byTF[x.timeframe] = 0;
        byTF[x.timeframe] += x.pnl_pct;
    }});
    const tfOrder = ['1min', '5min', '15min', '1hr', '4hr', 'daily'];
    const tfs = Object.keys(byTF).sort((a, b) => tfOrder.indexOf(a) - tfOrder.indexOf(b));
    charts.tf = new Chart(document.getElementById('tfChart'), {{
        type: 'bar',
        data: {{ labels: tfs, datasets: [{{ label: 'Total P&L %', data: tfs.map(tf => byTF[tf].toFixed(3)), backgroundColor: tfs.map(tf => byTF[tf] >= 0 ? '#22c55e88' : '#ef444488'), borderColor: tfs.map(tf => byTF[tf] >= 0 ? '#22c55e' : '#ef4444'), borderWidth: 1 }}] }},
        options: chartOpts
    }});

    // Outcome distribution
    const outcomes = {{}};
    t.forEach(x => {{ outcomes[x.outcome] = (outcomes[x.outcome] || 0) + 1; }});
    const oc = Object.entries(outcomes).sort((a, b) => b[1] - a[1]);
    const oColors = {{ FULL_RUNNER: '#22c55e', WIN: '#86efac', TP1_ONLY: '#a7f3d0', BREAKEVEN: '#eab308', LOSS: '#fca5a5', STOPPED: '#ef4444' }};
    charts.outcome = new Chart(document.getElementById('outcomeChart'), {{
        type: 'doughnut',
        data: {{ labels: oc.map(x => x[0]), datasets: [{{ data: oc.map(x => x[1]), backgroundColor: oc.map(x => oColors[x[0]] || '#6366f1') }}] }},
        options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ position: 'right', labels: {{ color: '#8b8d97', font: {{ size: 11 }} }} }} }} }}
    }});

    // MFE vs MAE scatter
    charts.mfe = new Chart(document.getElementById('mfeChart'), {{
        type: 'scatter',
        data: {{ datasets: [
            {{ label: 'Winners', data: t.filter(x => x.pnl_pct > 0).map(x => ({{ x: Math.abs(x.mae || 0), y: x.mfe || 0 }})), backgroundColor: '#22c55e88', pointRadius: 4 }},
            {{ label: 'Losers', data: t.filter(x => x.pnl_pct <= 0).map(x => ({{ x: Math.abs(x.mae || 0), y: x.mfe || 0 }})), backgroundColor: '#ef444488', pointRadius: 4 }},
        ] }},
        options: {{ ...chartOpts, scales: {{ x: {{ ...chartOpts.scales.x, title: {{ display: true, text: 'MAE (adverse) %', color: '#8b8d97' }} }}, y: {{ ...chartOpts.scales.y, title: {{ display: true, text: 'MFE (favorable) %', color: '#8b8d97' }} }} }} }}
    }});

    // PnL by hour
    const byHour = {{}};
    t.forEach(x => {{
        if (x.entry_time) {{
            const h = new Date(x.entry_time * 1000).getHours();
            if (!byHour[h]) byHour[h] = {{ pnl: 0, count: 0 }};
            byHour[h].pnl += x.pnl_pct;
            byHour[h].count++;
        }}
    }});
    const hours = Object.keys(byHour).map(Number).sort((a, b) => a - b);
    charts.hour = new Chart(document.getElementById('hourChart'), {{
        type: 'bar',
        data: {{ labels: hours.map(h => h + ':00'), datasets: [{{ label: 'P&L %', data: hours.map(h => byHour[h].pnl.toFixed(3)), backgroundColor: hours.map(h => byHour[h].pnl >= 0 ? '#22c55e88' : '#ef444488'), borderColor: hours.map(h => byHour[h].pnl >= 0 ? '#22c55e' : '#ef4444'), borderWidth: 1 }}] }},
        options: chartOpts
    }});
}}

function renderFilterPipeline() {{
    // Funnel chart
    const detected = STATS.signals_detected || 0;
    const blocked = STATS.signals_blocked || 0;
    const passed = STATS.signals_passed_all_filters || 0;
    const traded = STATS.trades_entered || 0;

    charts.funnel = new Chart(document.getElementById('funnelChart'), {{
        type: 'bar',
        data: {{ labels: ['Detected', 'Blocked', 'Passed Filters', 'Traded'], datasets: [{{ data: [detected, blocked, passed, traded], backgroundColor: ['#6366f188', '#ef444488', '#eab30888', '#22c55e88'], borderColor: ['#6366f1', '#ef4444', '#eab308', '#22c55e'], borderWidth: 1 }}] }},
        options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }} }}, y: {{ ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }} }} }} }}
    }});

    // Block reasons
    const blockReasons = {{}};
    Object.entries(STATS).forEach(([k, v]) => {{
        if (k.startsWith('blocked_by_')) blockReasons[k.replace('blocked_by_', '')] = v;
    }});
    const br = Object.entries(blockReasons).sort((a, b) => b[1] - a[1]);
    const totalBlocks = br.reduce((s, x) => s + x[1], 0) || 1;

    charts.block = new Chart(document.getElementById('blockChart'), {{
        type: 'bar',
        data: {{ labels: br.map(x => x[0]), datasets: [{{ label: 'Blocks', data: br.map(x => x[1]), backgroundColor: '#f9731688', borderColor: '#f97316', borderWidth: 1 }}] }},
        options: {{ responsive: true, maintainAspectRatio: false, indexAxis: 'y', plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }} }}, y: {{ ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }} }} }} }}
    }});

    // Filter table
    const body = document.getElementById('filterBody');
    body.innerHTML = br.map(([name, count]) => `
        <tr>
            <td>${{name}}</td>
            <td>${{count}}</td>
            <td>${{(count / totalBlocks * 100).toFixed(1)}}%</td>
            <td>${{count > totalBlocks * 0.3 ? '<span style="color:var(--yellow)">High block rate — verify threshold</span>' : '<span style="color:var(--green)">Normal</span>'}}</td>
        </tr>
    `).join('');
}}

function renderFindings() {{
    const well = ANALYSIS.what_went_well || [];
    const wrong = ANALYSIS.what_went_wrong || [];
    const imps = ANALYSIS.improvements || [];

    document.getElementById('wentWell').innerHTML = well.length
        ? well.map(w => `<div class="finding-item"><span class="icon">+</span>${{w}}</div>`).join('')
        : '<div class="finding-item" style="color:var(--text-dim)">No notable wins detected.</div>';

    document.getElementById('wentWrong').innerHTML = wrong.length
        ? wrong.map(w => `<div class="finding-item"><span class="icon">-</span>${{w}}</div>`).join('')
        : '<div class="finding-item" style="color:var(--text-dim)">No major issues detected.</div>';

    document.getElementById('improvements').innerHTML = imps.length
        ? imps.map(w => `<div class="finding-item"><span class="icon">&gt;</span>${{w}}</div>`).join('')
        : '<div class="finding-item" style="color:var(--text-dim)">No improvements suggested yet.</div>';

    // Analysis table
    const analyses = ANALYSIS.trade_analyses || [];
    document.getElementById('analysisBody').innerHTML = analyses.map(a => `
        <tr>
            <td>${{a.trade_id}}</td>
            <td>${{a.strategy.replace('smc_order_block_', 'OB_')}}</td>
            <td><span class="badge ${{a.signal_quality === 'GOOD' ? 'badge-win' : 'badge-loss'}}">${{a.signal_quality}}</span></td>
            <td><span class="badge ${{a.entry_quality === 'OPTIMAL' ? 'badge-win' : 'badge-loss'}}">${{a.entry_quality}}</span></td>
            <td><span class="badge ${{a.exit_quality === 'OPTIMAL' ? 'badge-win' : 'badge-loss'}}">${{a.exit_quality}}</span></td>
            <td style="color: ${{a.pnl_pct >= 0 ? 'var(--green)' : 'var(--red)'}}">${{a.pnl_pct >= 0 ? '+' : ''}}${{a.pnl_pct.toFixed(3)}}%</td>
            <td style="max-width:300px;white-space:normal;font-size:12px">${{(a.findings || []).slice(0, 2).join(' | ') || '—'}}</td>
        </tr>
    `).join('');
}}

function renderTradesTable() {{
    const t = filteredTrades;
    document.getElementById('tradesBody').innerHTML = t.map(x => `
        <tr>
            <td>${{x.trade_id}}</td>
            <td>${{x.entry_time ? new Date(x.entry_time * 1000).toLocaleString() : '—'}}</td>
            <td>${{x.ticker}}</td>
            <td><span class="badge ${{x.direction === 'CALL' ? 'badge-call' : 'badge-put'}}">${{x.direction}}</span></td>
            <td>${{x.strategy.replace('smc_order_block_', 'OB_').replace('_engine', '')}}</td>
            <td>${{x.timeframe}}</td>
            <td>$${{x.entry_price.toFixed(2)}}</td>
            <td>$${{(x.exit_price || 0).toFixed(2)}}</td>
            <td style="color: ${{x.pnl_pct >= 0 ? 'var(--green)' : 'var(--red)}};font-weight:600">${{x.pnl_pct >= 0 ? '+' : ''}}${{x.pnl_pct.toFixed(3)}}%</td>
            <td style="color:var(--green)">${{(x.mfe || 0).toFixed(3)}}%</td>
            <td style="color:var(--red)">${{(x.mae || 0).toFixed(3)}}%</td>
            <td><span class="badge ${{['WIN','FULL_RUNNER','TP1_ONLY'].includes(x.outcome) ? 'badge-win' : 'badge-loss'}}">${{x.outcome}}</span></td>
            <td>${{x.exit_reason}}</td>
            <td>${{x.hold_seconds ? Math.round(x.hold_seconds / 60) + 'm' : '—'}}</td>
        </tr>
    `).join('');
}}

function renderDeepAnalysis() {{
    if (!DEEP || Object.keys(DEEP).length === 0) return;

    // Risk KPIs
    const rm = DEEP.risk_metrics || {{}};
    document.getElementById('riskKpis').innerHTML = `
        <div class="kpi"><div class="label">Sharpe Ratio</div><div class="value ${{rm.sharpe >= 1 ? 'green' : rm.sharpe >= 0.5 ? 'yellow' : 'red'}}">${{rm.sharpe || '—'}}</div><div class="sub">Risk-adj return</div></div>
        <div class="kpi"><div class="label">Sortino Ratio</div><div class="value ${{rm.sortino >= 2 ? 'green' : 'yellow'}}">${{rm.sortino || '—'}}</div><div class="sub">Downside-adj</div></div>
        <div class="kpi"><div class="label">Expectancy</div><div class="value ${{rm.expectancy_per_trade > 0 ? 'green' : 'red'}}">${{rm.expectancy_per_trade ? (rm.expectancy_per_trade > 0 ? '+' : '') + rm.expectancy_per_trade + '%' : '—'}}</div><div class="sub">Per trade</div></div>
        <div class="kpi"><div class="label">Kelly %</div><div class="value blue">${{rm.kelly_fraction ? (rm.kelly_fraction * 100).toFixed(1) + '%' : '—'}}</div><div class="sub">${{rm.kelly_suggestion ? rm.kelly_suggestion.substring(0, 40) : ''}}</div></div>
        <div class="kpi"><div class="label">Payoff Ratio</div><div class="value ${{rm.payoff_ratio >= 1.5 ? 'green' : 'yellow'}}">${{rm.payoff_ratio || '—'}}x</div><div class="sub">Avg win / avg loss</div></div>
        <div class="kpi"><div class="label">Max DD</div><div class="value red">${{rm.max_drawdown_pct || '—'}}%</div><div class="sub">Recovery: ${{rm.recovery_factor || '—'}}x</div></div>
        <div class="kpi"><div class="label">Max Win Streak</div><div class="value green">${{rm.max_consecutive_wins || 0}}</div><div class="sub">consecutive</div></div>
        <div class="kpi"><div class="label">Max Loss Streak</div><div class="value red">${{rm.max_consecutive_losses || 0}}</div><div class="sub">consecutive</div></div>
    `;

    // Context correlation charts
    const cc = DEEP.context_correlation || {{}};
    const rsiData = cc.RSI_14 || {{}};
    const adxData = cc.ADX_14 || {{}};

    if (rsiData.buckets) {{
        const labels = Object.keys(rsiData.buckets);
        const wrs = labels.map(l => rsiData.buckets[l].win_rate);
        const pnls = labels.map(l => rsiData.buckets[l].avg_pnl);
        charts.rsiCorr = new Chart(document.getElementById('rsiCorr'), {{
            type: 'bar',
            data: {{ labels, datasets: [
                {{ label: 'Win Rate %', data: wrs, backgroundColor: '#3b82f688', borderColor: '#3b82f6', borderWidth: 1, yAxisID: 'y' }},
                {{ label: 'Avg P&L %', data: pnls, type: 'line', borderColor: '#22c55e', backgroundColor: '#22c55e', pointRadius: 5, yAxisID: 'y1' }},
            ] }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ labels: {{ color: '#8b8d97' }} }} }}, scales: {{ x: {{ ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }} }}, y: {{ type: 'linear', position: 'left', ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }}, title: {{ display: true, text: 'Win Rate %', color: '#8b8d97' }} }}, y1: {{ type: 'linear', position: 'right', ticks: {{ color: '#8b8d97' }}, grid: {{ drawOnChartArea: false }}, title: {{ display: true, text: 'Avg P&L %', color: '#8b8d97' }} }} }} }}
        }});
    }}

    if (adxData.buckets) {{
        const labels = Object.keys(adxData.buckets);
        const wrs = labels.map(l => adxData.buckets[l].win_rate);
        const pnls = labels.map(l => adxData.buckets[l].avg_pnl);
        charts.adxCorr = new Chart(document.getElementById('adxCorr'), {{
            type: 'bar',
            data: {{ labels, datasets: [
                {{ label: 'Win Rate %', data: wrs, backgroundColor: '#a855f788', borderColor: '#a855f7', borderWidth: 1, yAxisID: 'y' }},
                {{ label: 'Avg P&L %', data: pnls, type: 'line', borderColor: '#22c55e', backgroundColor: '#22c55e', pointRadius: 5, yAxisID: 'y1' }},
            ] }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ labels: {{ color: '#8b8d97' }} }} }}, scales: {{ x: {{ ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }} }}, y: {{ type: 'linear', position: 'left', ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }} }}, y1: {{ type: 'linear', position: 'right', ticks: {{ color: '#8b8d97' }}, grid: {{ drawOnChartArea: false }} }} }} }}
        }});
    }}

    // Counterfactual chart
    const cf = DEEP.counterfactual || {{}};
    const cfNames = Object.keys(cf).filter(k => !k.startsWith('_'));
    if (cfNames.length) {{
        const actual = cf[cfNames[0]]?.actual_pnl || 0;
        charts.cf = new Chart(document.getElementById('counterfactualChart'), {{
            type: 'bar',
            data: {{ labels: ['Actual', ...cfNames], datasets: [{{ label: 'Total P&L %', data: [actual, ...cfNames.map(n => cf[n].simulated_pnl)], backgroundColor: ['#6366f188', ...cfNames.map(n => cf[n].improvement > 0 ? '#22c55e88' : '#ef444488')], borderColor: ['#6366f1', ...cfNames.map(n => cf[n].improvement > 0 ? '#22c55e' : '#ef4444')], borderWidth: 1 }}] }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ ticks: {{ color: '#8b8d97', font: {{ size: 10 }} }}, grid: {{ color: '#2a2d3a' }} }}, y: {{ ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }} }} }} }}
        }});
    }}

    // Runner capture chart
    const rc = DEEP.runner_efficiency?.by_exit_reason || {{}};
    const rcNames = Object.keys(rc);
    if (rcNames.length) {{
        charts.runner = new Chart(document.getElementById('runnerChart'), {{
            type: 'bar',
            data: {{ labels: rcNames, datasets: [
                {{ label: 'Avg Capture %', data: rcNames.map(n => (rc[n].avg_capture * 100).toFixed(0)), backgroundColor: rcNames.map(n => rc[n].avg_capture > 0 ? '#22c55e88' : '#ef444488'), borderColor: rcNames.map(n => rc[n].avg_capture > 0 ? '#22c55e' : '#ef4444'), borderWidth: 1 }},
            ] }},
            options: {{ responsive: true, maintainAspectRatio: false, plugins: {{ legend: {{ display: false }} }}, scales: {{ x: {{ ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }} }}, y: {{ ticks: {{ color: '#8b8d97' }}, grid: {{ color: '#2a2d3a' }}, title: {{ display: true, text: 'MFE Capture %', color: '#8b8d97' }} }} }} }}
        }});
    }}

    // MFE/MAE findings
    const mm = DEEP.mfe_mae || {{}};
    let mmHtml = '';
    Object.entries(mm).filter(([k]) => k !== '_all').slice(0, 6).forEach(([strat, data]) => {{
        mmHtml += `<div class="finding-item"><strong>${{strat.replace('smc_order_block_', 'OB_')}}</strong><br>`;
        if (data.stop_diagnosis) mmHtml += `Stop: ${{data.stop_diagnosis}}<br>`;
        if (data.target_diagnosis) mmHtml += `Target: ${{data.target_diagnosis}}<br>`;
        if (data.capture_ratio !== undefined) mmHtml += `Capture ratio: ${{(data.capture_ratio * 100).toFixed(0)}}% of MFE kept`;
        mmHtml += `</div>`;
    }});
    document.getElementById('mfeMaeFindings').innerHTML = mmHtml || '<div class="finding-item" style="color:var(--text-dim)">No data</div>';

    // Context findings
    let ctxHtml = '';
    ['RSI_14', 'ADX_14', 'atr_ratio'].forEach(field => {{
        const data = cc[field];
        if (data?.diagnosis) {{
            ctxHtml += `<div class="finding-item"><strong>${{field}}</strong> (corr: ${{data.correlation}})<br>${{data.diagnosis}}</div>`;
        }}
    }});
    ['market_mode', 'ema_bias'].forEach(field => {{
        const data = cc[field];
        if (data) {{
            ctxHtml += `<div class="finding-item"><strong>${{field}}</strong><br>`;
            Object.entries(data).forEach(([k, v]) => {{
                ctxHtml += `${{k}}: ${{v.trades}} trades, ${{v.win_rate}}% WR, ${{v.avg_pnl > 0 ? '+' : ''}}${{v.avg_pnl}}% avg | `;
            }});
            ctxHtml += `</div>`;
        }}
    }});
    document.getElementById('contextFindings').innerHTML = ctxHtml || '<div class="finding-item">No data</div>';

    // Filter $ findings
    const fe = DEEP.filter_effectiveness || {{}};
    let feHtml = `<div class="finding-item"><strong>Total losses:</strong> ${{fe.total_losses || 0}} trades (${{fe.total_loss_pnl || 0}}% total)</div>`;
    feHtml += `<div class="finding-item"><strong>Preventable:</strong> ${{fe.preventable_losses || 0}} trades (${{fe.preventable_pnl || 0}}%) could have been blocked</div>`;
    Object.entries(fe.would_have_saved || {{}}).forEach(([filter, data]) => {{
        feHtml += `<div class="finding-item">${{filter}}: would save ${{data.count}} trades / ${{data.pnl_saved}}%</div>`;
    }});
    document.getElementById('filterDollarFindings').innerHTML = feHtml;

    // Runner + Streak findings
    const re = DEEP.runner_efficiency || {{}};
    const sa = DEEP.streak_analysis || {{}};
    let rsHtml = '';
    (re.diagnosis || []).forEach(d => {{ rsHtml += `<div class="finding-item">${{d}}</div>`; }});
    (sa.diagnosis || []).forEach(d => {{ rsHtml += `<div class="finding-item">${{d}}</div>`; }});
    if (sa.worst_losing_streak) {{
        const wls = sa.worst_losing_streak;
        rsHtml += `<div class="finding-item"><strong>Worst losing streak:</strong> ${{wls.length}} trades (${{wls.total_pnl}}%) — ${{wls.strategies.join(', ')}}</div>`;
    }}
    document.getElementById('runnerFindings').innerHTML = rsHtml || '<div class="finding-item">No streaks detected</div>';

    // Per-strategy deep table
    const sd = DEEP.strategy_deep || {{}};
    const sorted = Object.entries(sd).sort((a, b) => b[1].total_pnl - a[1].total_pnl);
    document.getElementById('stratDeepBody').innerHTML = sorted.map(([strat, d]) => `
        <tr>
            <td>${{strat.replace('smc_order_block_', 'OB_').replace('_engine', '')}}</td>
            <td>${{d.trades}}</td>
            <td style="color:${{d.win_rate >= 70 ? 'var(--green)' : d.win_rate >= 50 ? 'var(--yellow)' : 'var(--red)'}}">${{d.win_rate}}%</td>
            <td style="color:${{d.total_pnl >= 0 ? 'var(--green)' : 'var(--red)'}}">${{d.total_pnl >= 0 ? '+' : ''}}${{d.total_pnl}}%</td>
            <td style="color:var(--green)">+${{d.avg_win}}%</td>
            <td style="color:var(--red)">${{d.avg_loss}}%</td>
            <td>${{d.profit_factor === Infinity ? '∞' : d.profit_factor}}</td>
            <td>${{d.avg_mfe}}%</td>
            <td>${{d.avg_mae}}%</td>
            <td>${{d.avg_hold_min}}m</td>
            <td>${{d.direction_bias || '—'}}</td>
        </tr>
    `).join('');
}}

function switchTab(name) {{
    document.querySelectorAll('.tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.tab-content').forEach(t => t.classList.remove('active'));
    event.target.classList.add('active');
    document.getElementById('tab-' + name).classList.add('active');
    if (name === 'filters') renderFilterPipeline();
    if (name === 'deep') renderDeepAnalysis();
}}

function sortTable(tableId, col) {{
    const table = document.getElementById(tableId);
    const rows = Array.from(table.tBodies[0].rows);
    const dir = table.dataset.sortDir === 'asc' ? 'desc' : 'asc';
    table.dataset.sortDir = dir;
    rows.sort((a, b) => {{
        let va = a.cells[col].textContent.trim().replace(/[^\\d.\\-]/g, '');
        let vb = b.cells[col].textContent.trim().replace(/[^\\d.\\-]/g, '');
        if (!isNaN(va) && !isNaN(vb)) return dir === 'asc' ? va - vb : vb - va;
        return dir === 'asc' ? a.cells[col].textContent.localeCompare(b.cells[col].textContent) : b.cells[col].textContent.localeCompare(a.cells[col].textContent);
    }});
    rows.forEach(r => table.tBodies[0].appendChild(r));
}}

function renderAll() {{
    renderKPIs();
    renderCharts();
    renderFindings();
    renderTradesTable();
    renderDeepAnalysis();
}}

populateFilters();
renderAll();
</script>
</body>
</html>"""

    return html


def main():
    """CLI entry point."""
    import argparse
    parser = argparse.ArgumentParser(description='Trade Dashboard Generator')
    parser.add_argument('input_file', nargs='?', help='Trade log JSON file')
    parser.add_argument('--demo', action='store_true', help='Generate with demo data')
    parser.add_argument('--output', '-o', default=None, help='Output HTML file')
    args = parser.parse_args()

    if args.demo:
        print("Generating demo dashboard...")
        session_data = generate_demo_data()
    elif args.input_file:
        with open(args.input_file, 'r') as f:
            session_data = json.load(f)
    else:
        # Find latest trade log
        log_dir = 'trade_logs'
        if os.path.isdir(log_dir):
            files = sorted([f for f in os.listdir(log_dir) if f.startswith('trades_') and f.endswith('.json')])
            if files:
                with open(os.path.join(log_dir, files[-1]), 'r') as f:
                    session_data = json.load(f)
                print(f"Using: {files[-1]}")
            else:
                print("No trade logs found. Use --demo for demo data.")
                return
        else:
            print("No trade_logs directory. Use --demo for demo data.")
            return

    # Generate
    analyzer = TradeAnalyzer()
    report = analyzer.generate_report(session_data)
    html = generate_dashboard_html(session_data, report)

    output = args.output or 'trade_dashboard.html'
    with open(output, 'w') as f:
        f.write(html)
    print(f"Dashboard saved: {output}")
    print(f"  Trades: {len(session_data.get('completed_trades', []))}")
    print(f"  Analysis: {len(report.get('trade_analyses', []))} trade analyses")
    print(f"  Findings: {len(report.get('what_went_well', []))} wins, {len(report.get('what_went_wrong', []))} issues")


if __name__ == '__main__':
    main()
