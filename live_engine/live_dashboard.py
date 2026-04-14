"""
LIVE ENGINE MONITORING DASHBOARD
==================================
Reads JSONL event logs + engine log to show real-time engine state.
Generates self-contained HTML with trades, signals, PnL, and pipeline view.

Usage:
    python live_dashboard.py                    # Generate from today's events
    python live_dashboard.py --watch            # Regenerate every 30s
"""

import json
import os
import re
import time
import argparse
from datetime import datetime
from collections import defaultdict, Counter


def parse_events(filepath):
    events = []
    if not os.path.exists(filepath):
        return events
    with open(filepath) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                events.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return events


def parse_engine_log(filepath='/tmp/v1_engine.log'):
    data = {
        'status_lines': [], 'signal_debugs': [], 'mode_blocks': [],
        'ws_bars': [], 'errors': [], 'tf_data': {},
        'processing': [], 'ema_blocks': [], 'v2_filters': [],
        'v3_blocks': [], 'executions': [],
    }
    if not os.path.exists(filepath):
        return data
    with open(filepath) as f:
        for line in f:
            if 'STATUS [' in line:
                data['status_lines'].append(line.strip())
            elif 'SIGNAL DEBUG:' in line:
                data['signal_debugs'].append(line.strip())
            elif 'MODE BLOCK:' in line:
                data['mode_blocks'].append(line.strip())
            elif 'WS BAR:' in line:
                data['ws_bars'].append(line.strip())
            elif 'ERROR' in line or 'FATAL' in line:
                data['errors'].append(line.strip())
            elif 'PROCESSING ' in line:
                data['processing'].append(line.strip())
            elif 'EMA BLOCK:' in line:
                data['ema_blocks'].append(line.strip())
            elif 'V2 FILTER:' in line:
                data['v2_filters'].append(line.strip())
            elif 'V3 blocked:' in line:
                data['v3_blocks'].append(line.strip())
            elif 'TF DATA' in line:
                m = re.search(r'TF DATA \[(\w+)\]: ({.*})', line)
                if m:
                    try:
                        data['tf_data'][m.group(1)] = json.loads(m.group(2).replace("'", '"'))
                    except:
                        pass
    return data


def generate_html(events, engine_data):
    now = datetime.now()

    # Categorize events
    mode_changes = [e for e in events if e.get('event_type') == 'MODE_CHANGE']
    filter_blocks = [e for e in events if e.get('event_type') == 'FILTER_BLOCK']
    signals = [e for e in events if e.get('event_type') == 'SIGNAL_DETECTED']
    entries = [e for e in events if e.get('event_type') == 'ENTRY_EXECUTED']
    exits = [e for e in events if e.get('event_type') == 'EXIT']
    skipped = [e for e in events if e.get('event_type') == 'ENTRY_SKIPPED']
    filter_pipelines = [e for e in events if e.get('event_type') == 'FILTER_PIPELINE']

    # Current modes
    current_modes = {}
    for mc in mode_changes:
        t = mc.get('ticker', '')
        m = mc.get('data', {}).get('new_mode', '')
        if t and m:
            current_modes[t] = m

    # Block reasons
    block_reasons = Counter()
    for fb in filter_blocks:
        reason = fb.get('data', {}).get('reason', fb.get('data', {}).get('filter_name', 'unknown'))
        block_reasons[reason] += 1

    # Build open/closed trades from events
    open_trades = {}
    closed_trades = []
    for e in entries:
        tid = e.get('trade_id', '')
        d = e.get('data', {})
        open_trades[tid] = {
            'trade_id': tid,
            'ticker': e.get('ticker', ''),
            'strategy': e.get('strategy', ''),
            'direction': e.get('direction', ''),
            'entry_price': d.get('entry_price', 0),
            'entry_time': e.get('timestamp_iso', '')[:19],
            'stop_pct': d.get('stop_pct', 0),
            'target_pct': d.get('target_pct', 0),
        }
    for e in exits:
        tid = e.get('trade_id', '')
        d = e.get('data', {})
        trade = open_trades.pop(tid, {})
        trade.update({
            'exit_price': d.get('exit_price', 0),
            'exit_reason': d.get('exit_reason', ''),
            'pnl': d.get('pnl', 0),
            'pnl_pct': d.get('pnl_pct', 0),
            'outcome': d.get('outcome', ''),
            'hold_seconds': d.get('hold_seconds', 0),
            'exit_time': e.get('timestamp_iso', '')[:19],
            'mfe': d.get('mfe', 0),
            'mae': d.get('mae', 0),
        })
        closed_trades.append(trade)

    # PnL stats
    total_pnl = sum(t.get('pnl', 0) for t in closed_trades)
    total_trades = len(closed_trades)
    wins = [t for t in closed_trades if t.get('pnl', 0) > 0]
    losses = [t for t in closed_trades if t.get('pnl', 0) <= 0]
    win_rate = (len(wins) / total_trades * 100) if total_trades > 0 else 0

    # Engine status
    engine_alive = False
    if engine_data['ws_bars']:
        try:
            ts = engine_data['ws_bars'][-1][:19]
            last_time = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')
            engine_alive = (now - last_time).total_seconds() < 120
        except:
            pass

    # Parse signal debug for latest per ticker/strategy
    signal_table = []
    seen = set()
    for line in reversed(engine_data.get('signal_debugs', [])[-100:]):
        m = re.search(r'SIGNAL DEBUG: (\w+)/(\w+)/(\w+) — raw=(\d+)C/(\d+)P, recent\(>=\d+\)=(\d+)C/(\d+)P', line)
        if m:
            key = f"{m.group(1)}/{m.group(2)}/{m.group(3)}"
            if key not in seen:
                seen.add(key)
                signal_table.append({
                    'ticker': m.group(1), 'tf': m.group(2), 'strategy': m.group(3),
                    'raw_c': int(m.group(4)), 'raw_p': int(m.group(5)),
                    'rec_c': int(m.group(6)), 'rec_p': int(m.group(7)),
                })

    # Parse processing/EMA/V2 pipeline
    pipeline_events = []
    for line in engine_data.get('processing', [])[-20:]:
        m = re.search(r'PROCESSING (\w+): (\w+)/(\w+)/(\w+) idx=(\d+) bias=(\w+) cat=(\w+)', line)
        if m:
            pipeline_events.append({
                'time': line[:19], 'dir': m.group(1), 'ticker': m.group(2),
                'tf': m.group(3), 'strategy': m.group(4), 'idx': m.group(5),
                'bias': m.group(6), 'cat': m.group(7), 'result': 'processing',
            })
    for line in engine_data.get('ema_blocks', [])[-20:]:
        m = re.search(r'EMA BLOCK: (\w+) (\w+) — bias=(\w+) cat=(\w+)', line)
        if m:
            pipeline_events.append({
                'time': line[:19], 'dir': m.group(2), 'ticker': m.group(1),
                'bias': m.group(3), 'cat': m.group(4), 'result': 'ema_blocked',
            })
    for line in engine_data.get('v2_filters', [])[-20:]:
        m = re.search(r'V2 FILTER: (\w+) (\w+) — (\d+) passed', line)
        if m:
            pipeline_events.append({
                'time': line[:19], 'ticker': m.group(1), 'dir': m.group(2),
                'v2_passed': int(m.group(3)), 'result': 'v2_filter',
            })

    # TF data
    tf_data = engine_data.get('tf_data', {})

    # ── BUILD HTML ──
    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<meta http-equiv="refresh" content="60">
<title>V1 SMC Engine — Live Dashboard</title>
<style>
* {{ margin:0; padding:0; box-sizing:border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background:#0a0e17; color:#e2e8f0; padding:16px; }}
.header {{ display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; border-bottom:1px solid #1e293b; padding-bottom:16px; }}
.header h1 {{ font-size:22px; color:#f8fafc; }}
.header .status {{ font-size:14px; padding:6px 14px; border-radius:20px; font-weight:600; }}
.alive {{ background:#065f46; color:#6ee7b7; }}
.dead {{ background:#7f1d1d; color:#fca5a5; }}
.updated {{ font-size:12px; color:#64748b; margin-top:4px; }}

/* KPI Cards */
.kpi-row {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(160px, 1fr)); gap:12px; margin-bottom:20px; }}
.kpi {{ background:#111827; border:1px solid #1e293b; border-radius:10px; padding:14px 16px; text-align:center; }}
.kpi .kpi-val {{ font-size:28px; font-weight:800; color:#f8fafc; }}
.kpi .kpi-label {{ font-size:11px; color:#64748b; text-transform:uppercase; letter-spacing:0.5px; margin-top:4px; }}
.kpi .kpi-val.green {{ color:#34d399; }}
.kpi .kpi-val.red {{ color:#f87171; }}
.kpi .kpi-val.yellow {{ color:#fbbf24; }}
.kpi .kpi-val.blue {{ color:#60a5fa; }}
.kpi .kpi-val.purple {{ color:#a78bfa; }}

/* Grid / Cards */
.grid {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(320px, 1fr)); gap:16px; margin-bottom:20px; }}
.card {{ background:#111827; border:1px solid #1e293b; border-radius:10px; padding:16px; }}
.card.highlight {{ border-color:#fbbf24; }}
.card h2 {{ font-size:13px; color:#94a3b8; text-transform:uppercase; letter-spacing:0.5px; margin-bottom:12px; }}
.full-width {{ grid-column: 1 / -1; }}

/* Tables */
table {{ width:100%; border-collapse:collapse; font-size:13px; }}
th {{ text-align:left; padding:8px 10px; background:#1e293b; color:#94a3b8; font-weight:600; font-size:11px; text-transform:uppercase; }}
td {{ padding:7px 10px; border-bottom:1px solid #1e293b; }}
tr:hover {{ background:#1e293b33; }}

/* Badges */
.badge {{ display:inline-block; padding:2px 8px; border-radius:4px; font-size:11px; font-weight:700; }}
.b-TREND {{ background:#065f46; color:#6ee7b7; }}
.b-REVERSAL {{ background:#7c2d12; color:#fdba74; }}
.b-NO_TRADE {{ background:#1e293b; color:#64748b; }}
.b-CALL {{ background:#065f46; color:#6ee7b7; }}
.b-PUT {{ background:#7f1d1d; color:#fca5a5; }}
.b-LONG {{ background:#065f46; color:#6ee7b7; }}
.b-SHORT {{ background:#7f1d1d; color:#fca5a5; }}
.b-NEUTRAL {{ background:#1e293b; color:#94a3b8; }}
.b-WIN {{ background:#065f46; color:#6ee7b7; }}
.b-LOSS {{ background:#7f1d1d; color:#fca5a5; }}
.b-STOPPED {{ background:#7f1d1d; color:#fca5a5; }}
.b-BREAKEVEN {{ background:#1e293b; color:#94a3b8; }}
.b-FULL_RUNNER {{ background:#1e3a5f; color:#93c5fd; }}
.b-TP1_ONLY {{ background:#065f46; color:#6ee7b7; }}
.b-blocked {{ background:#7f1d1d; color:#fca5a5; }}
.b-passed {{ background:#065f46; color:#6ee7b7; }}
.b-processing {{ background:#1e293b; color:#fbbf24; }}

/* Signal bars */
.sig-bar {{ display:inline-block; height:12px; border-radius:2px; margin-right:1px; min-width:2px; vertical-align:middle; }}
.sig-c {{ background:#3b82f6; }}
.sig-p {{ background:#ef4444; }}

/* TF chips */
.tf-grid {{ display:grid; grid-template-columns:repeat(auto-fill, minmax(90px, 1fr)); gap:6px; }}
.tf-chip {{ background:#1e293b; padding:5px 8px; border-radius:5px; text-align:center; font-size:11px; }}
.tf-chip b {{ color:#60a5fa; }}

.empty {{ text-align:center; color:#475569; padding:30px; font-style:italic; font-size:13px; }}
.pnl-pos {{ color:#34d399; font-weight:700; }}
.pnl-neg {{ color:#f87171; font-weight:700; }}
.section {{ margin-bottom:24px; }}
.section-title {{ font-size:16px; font-weight:700; margin-bottom:12px; color:#f8fafc; display:flex; align-items:center; gap:8px; }}
.section-title .count {{ font-size:12px; background:#1e293b; padding:2px 8px; border-radius:10px; color:#94a3b8; }}
</style>
</head>
<body>

<div class="header">
    <div>
        <h1>V1 SMC Engine — Live Dashboard</h1>
        <div class="updated">Updated: {now.strftime('%Y-%m-%d %H:%M:%S')} &bull; Auto-refresh 60s &bull; Paper Mode</div>
    </div>
    <div class="status {'alive' if engine_alive else 'dead'}">{'LIVE' if engine_alive else 'OFFLINE'}</div>
</div>

<!-- KPI Row -->
<div class="kpi-row">
    <div class="kpi"><div class="kpi-val {'green' if total_pnl >= 0 else 'red'}">${total_pnl:+.2f}</div><div class="kpi-label">Total PnL</div></div>
    <div class="kpi"><div class="kpi-val blue">{total_trades}</div><div class="kpi-label">Trades Closed</div></div>
    <div class="kpi"><div class="kpi-val purple">{len(open_trades)}</div><div class="kpi-label">Open Positions</div></div>
    <div class="kpi"><div class="kpi-val {'green' if win_rate >= 50 else 'yellow'}">{win_rate:.0f}%</div><div class="kpi-label">Win Rate</div></div>
    <div class="kpi"><div class="kpi-val yellow">{len(signals)}</div><div class="kpi-label">Signals Found</div></div>
    <div class="kpi"><div class="kpi-val red">{len(filter_blocks)}</div><div class="kpi-label">Blocks</div></div>
    <div class="kpi"><div class="kpi-val">{len(engine_data['ws_bars'])}</div><div class="kpi-label">WS Bars</div></div>
</div>
"""

    # ══════════════════════════════════════════════════
    # OPEN TRADES
    # ══════════════════════════════════════════════════
    html += '<div class="section"><div class="section-title">Open Positions <span class="count">' + str(len(open_trades)) + '</span></div>'
    html += '<div class="card highlight full-width">'
    if open_trades:
        html += """<table><thead><tr>
            <th>Trade ID</th><th>Ticker</th><th>Dir</th><th>Strategy</th>
            <th>Entry</th><th>Stop %</th><th>Target %</th><th>Time</th>
        </tr></thead><tbody>"""
        for tid, t in open_trades.items():
            dir_cls = t.get('direction', '')
            html += f"""<tr>
                <td style="font-family:monospace;font-size:11px;">{tid[:12]}</td>
                <td><strong>{t.get('ticker','')}</strong></td>
                <td><span class="badge b-{dir_cls}">{dir_cls}</span></td>
                <td>{t.get('strategy','')}</td>
                <td>${t.get('entry_price',0):.2f}</td>
                <td>{t.get('stop_pct',0):.2f}%</td>
                <td>{t.get('target_pct',0):.2f}%</td>
                <td>{t.get('entry_time','')}</td>
            </tr>"""
        html += "</tbody></table>"
    else:
        html += '<div class="empty">No open positions — engine is scanning for setups</div>'
    html += '</div></div>'

    # ══════════════════════════════════════════════════
    # CLOSED TRADES
    # ══════════════════════════════════════════════════
    html += '<div class="section"><div class="section-title">Completed Trades <span class="count">' + str(len(closed_trades)) + '</span></div>'
    html += '<div class="card full-width">'
    if closed_trades:
        html += """<table><thead><tr>
            <th>Ticker</th><th>Dir</th><th>Strategy</th><th>Entry</th><th>Exit</th>
            <th>PnL</th><th>PnL %</th><th>Outcome</th><th>Hold</th><th>MFE</th><th>MAE</th>
        </tr></thead><tbody>"""
        for t in reversed(closed_trades):
            pnl = t.get('pnl', 0)
            pnl_cls = 'pnl-pos' if pnl > 0 else 'pnl-neg'
            outcome = t.get('outcome', '')
            hold_s = t.get('hold_seconds', 0)
            hold_str = f"{int(hold_s//60)}m" if hold_s < 3600 else f"{hold_s/3600:.1f}h"
            html += f"""<tr>
                <td><strong>{t.get('ticker','')}</strong></td>
                <td><span class="badge b-{t.get('direction','')}">{t.get('direction','')}</span></td>
                <td>{t.get('strategy','')}</td>
                <td>${t.get('entry_price',0):.2f}</td>
                <td>${t.get('exit_price',0):.2f}</td>
                <td class="{pnl_cls}">${pnl:+.2f}</td>
                <td class="{pnl_cls}">{t.get('pnl_pct',0):+.2f}%</td>
                <td><span class="badge b-{outcome}">{outcome}</span></td>
                <td>{hold_str}</td>
                <td>{t.get('mfe',0):.2f}%</td>
                <td>{t.get('mae',0):.2f}%</td>
            </tr>"""
        html += "</tbody></table>"
    else:
        html += '<div class="empty">No completed trades yet — waiting for signals to align with EMA bias</div>'
    html += '</div></div>'

    # ══════════════════════════════════════════════════
    # SIGNAL PROCESSING PIPELINE
    # ══════════════════════════════════════════════════
    html += '<div class="section"><div class="section-title">Signal Pipeline (recent) <span class="count">' + str(len(pipeline_events)) + '</span></div>'
    html += '<div class="card full-width">'
    if pipeline_events:
        html += """<table><thead><tr>
            <th>Time</th><th>Ticker</th><th>Dir</th><th>Bias</th><th>Category</th><th>Stage</th>
        </tr></thead><tbody>"""
        for pe in reversed(pipeline_events[-15:]):
            result = pe.get('result', '')
            if result == 'ema_blocked':
                stage = '<span class="badge b-blocked">EMA BLOCKED</span>'
            elif result == 'v2_filter':
                passed = pe.get('v2_passed', 0)
                stage = f'<span class="badge b-{"passed" if passed > 0 else "blocked"}">V2: {passed} passed</span>'
            else:
                stage = '<span class="badge b-processing">Processing</span>'
            bias = pe.get('bias', '')
            html += f"""<tr>
                <td>{pe.get('time','')}</td>
                <td><strong>{pe.get('ticker','')}</strong></td>
                <td><span class="badge b-{pe.get('dir','')}">{pe.get('dir','')}</span></td>
                <td><span class="badge b-{bias}">{bias}</span></td>
                <td>{pe.get('cat','')}</td>
                <td>{stage}</td>
            </tr>"""
        html += "</tbody></table>"
    else:
        html += '<div class="empty">No signals have reached the filter pipeline yet</div>'
    html += '</div></div>'

    # ══════════════════════════════════════════════════
    # CURRENT MODES + SIGNAL ACTIVITY SIDE BY SIDE
    # ══════════════════════════════════════════════════
    html += '<div class="grid">'

    # Current modes
    html += '<div class="card"><h2>Current Modes</h2>'
    if current_modes:
        for ticker, mode in sorted(current_modes.items()):
            html += f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:6px;"><span>{ticker}</span><span class="badge b-{mode}">{mode}</span></div>'
    else:
        html += '<div class="empty">No mode data</div>'
    html += '</div>'

    # Block summary
    html += '<div class="card"><h2>Block Reasons</h2>'
    if block_reasons:
        for reason, count in block_reasons.most_common(8):
            html += f'<div style="display:flex;justify-content:space-between;margin-bottom:6px;"><span style="color:#94a3b8;font-size:13px;">{reason}</span><span style="color:#f87171;font-weight:700;">{count}</span></div>'
    else:
        html += '<div class="empty">No blocks</div>'
    html += '</div>'

    html += '</div>'

    # ══════════════════════════════════════════════════
    # SIGNAL ACTIVITY TABLE
    # ══════════════════════════════════════════════════
    html += '<div class="section"><div class="section-title">Signal Detection <span class="count">' + str(len(signal_table)) + ' strategies active</span></div>'
    html += '<div class="card full-width">'
    if signal_table:
        html += """<table><thead><tr>
            <th>Ticker</th><th>TF</th><th>Strategy</th><th>Raw Signals</th><th>Recent (tradeable)</th>
        </tr></thead><tbody>"""
        for sig in signal_table:
            rec_total = sig['rec_c'] + sig['rec_p']
            rec_cls = 'style="color:#34d399;font-weight:700;"' if rec_total > 0 else ''
            html += f"""<tr>
                <td><strong>{sig['ticker']}</strong></td>
                <td>{sig['tf']}</td>
                <td>{sig['strategy']}</td>
                <td>
                    <span class="sig-bar sig-c" style="width:{min(sig['raw_c']*8, 60)}px"></span>
                    <span class="sig-bar sig-p" style="width:{min(sig['raw_p']*8, 60)}px"></span>
                    &nbsp;{sig['raw_c']}C / {sig['raw_p']}P
                </td>
                <td {rec_cls}>{sig['rec_c']}C / {sig['rec_p']}P</td>
            </tr>"""
        html += "</tbody></table>"
    else:
        html += '<div class="empty">Waiting for first scan cycle...</div>'
    html += '</div></div>'

    # ══════════════════════════════════════════════════
    # TF DATA PER TICKER
    # ══════════════════════════════════════════════════
    tf_order = ['1min','3min','5min','15min','1hr','4hr','daily','weekly']
    html += '<div class="section"><div class="section-title">Timeframe Data</div><div class="grid">'
    for ticker, tfs in sorted(tf_data.items()):
        html += f'<div class="card"><h2>{ticker}</h2><div class="tf-grid">'
        for tf in tf_order:
            if tf in tfs:
                html += f'<div class="tf-chip"><b>{tf}</b><br>{tfs[tf]}</div>'
        html += '</div></div>'
    if not tf_data:
        html += '<div class="card"><div class="empty">Waiting for first scan...</div></div>'
    html += '</div></div>'

    # ══════════════════════════════════════════════════
    # MODE TIMELINE
    # ══════════════════════════════════════════════════
    html += '<div class="section"><div class="section-title">Mode Timeline (last 15)</div><div class="card full-width">'
    recent_modes = mode_changes[-15:]
    if recent_modes:
        html += '<table><thead><tr><th>Time</th><th>Ticker</th><th>From</th><th>To</th><th>ADX</th><th>Sweep</th></tr></thead><tbody>'
        for mc in reversed(recent_modes):
            d = mc.get('data', {})
            adx = d.get('adx', 0)
            adx_str = f'{adx:.1f}' if adx and adx == adx else 'n/a'
            html += f"""<tr>
                <td>{mc.get('timestamp_iso','')[:19]}</td>
                <td><strong>{mc.get('ticker','')}</strong></td>
                <td><span class="badge b-{d.get('old_mode','')}">{d.get('old_mode','')}</span></td>
                <td><span class="badge b-{d.get('new_mode','')}">{d.get('new_mode','')}</span></td>
                <td>{adx_str}</td>
                <td>{'Yes' if d.get('has_sweep') else ''}</td>
            </tr>"""
        html += '</tbody></table>'
    else:
        html += '<div class="empty">No mode changes</div>'
    html += '</div></div>'

    # Errors
    if engine_data['errors']:
        html += '<div class="section"><div class="section-title">Errors</div><div class="card full-width" style="border-color:#7f1d1d;">'
        for err in engine_data['errors'][-8:]:
            html += f'<div style="color:#fca5a5;font-size:11px;margin-bottom:4px;font-family:monospace;">{err[:250]}</div>'
        html += '</div></div>'

    html += '</body></html>'
    return html


def main():
    parser = argparse.ArgumentParser(description='Live Engine Dashboard')
    parser.add_argument('--watch', action='store_true', help='Auto-regenerate every 30s')
    parser.add_argument('--output', '-o', default='live_dashboard.html', help='Output file')
    args = parser.parse_args()

    log_dir = 'trade_logs'
    today = datetime.now().strftime('%Y-%m-%d')
    events_file = os.path.join(log_dir, f'events_{today}.jsonl')

    while True:
        events = parse_events(events_file)
        engine_data = parse_engine_log()
        html = generate_html(events, engine_data)
        with open(args.output, 'w') as f:
            f.write(html)
        print(f"[{datetime.now().strftime('%H:%M:%S')}] Dashboard updated: {args.output} ({len(events)} events)")
        if not args.watch:
            break
        time.sleep(30)


if __name__ == '__main__':
    main()
