"""journal/trading_dashboard.py — Proper trader-focused dashboard.

Layout:
  HEADER BAR     — date, mode (paper/live), account equity, daily P&L
  ACTION PANEL   — what to do RIGHT NOW (latest signals + recommendations)
  LIVE POSITIONS — open positions with current P&L
  STRATEGY CARDS — per-strategy performance + equity curve (SVG)
  TRADES TABLE   — every backtest trade with full detail
  TOP/BOTTOM     — best winners + worst losers
  PER-TICKER     — leaderboard with win % bar chart
  RISK GAUGES    — daily kill, weekly halt, concurrent caps

Pure HTML + CSS + inline SVG — no JS framework dependency.
Anthropic brand styling: Poppins/Lora, #d97757/#6a9bcc/#788c5d.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


BRAND = {
    "dark": "#141413",
    "light": "#faf9f5",
    "mid_gray": "#b0aea5",
    "light_gray": "#e8e6dc",
    "orange": "#d97757",
    "blue": "#6a9bcc",
    "green": "#788c5d",
    "red": "#b85436",
}


def _e(s) -> str:
    if s is None:
        return ""
    s = str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
              .replace('"', "&quot;").replace("'", "&#39;"))


# ──────────────────────────────────────────────────────────────────────────────
# SVG widgets
# ──────────────────────────────────────────────────────────────────────────────


def _svg_equity_curve(pnls: list[float], width: int = 320, height: int = 80,
                       color: str = None) -> str:
    """Inline SVG equity curve (cumulative PnL polyline). Pure float values."""
    if not pnls:
        return f'<svg width="{width}" height="{height}"></svg>'
    cum = []; running = 0.0
    for p in pnls:
        running += p
        cum.append(running)
    pmin = min(0, min(cum))
    pmax = max(0, max(cum))
    span = max(0.001, pmax - pmin)

    pad = 6
    points = []
    for i, v in enumerate(cum):
        x = pad + (i / max(1, len(cum) - 1)) * (width - 2 * pad)
        y = height - pad - ((v - pmin) / span) * (height - 2 * pad)
        points.append(f"{x:.1f},{y:.1f}")

    color = color or (BRAND["green"] if cum[-1] >= 0 else BRAND["red"])
    fill_color = color
    # Build a closed polygon for fill
    fill_pts = points + [
        f"{pad + (width - 2*pad):.1f},{height - pad}",
        f"{pad}.0,{height - pad}",
    ]
    # Zero line
    if pmin < 0 < pmax:
        zero_y = height - pad - ((0 - pmin) / span) * (height - 2 * pad)
    else:
        zero_y = None

    zero_line = (f'<line x1="{pad}" y1="{zero_y:.1f}" x2="{width - pad}" '
                  f'y2="{zero_y:.1f}" stroke="{BRAND["mid_gray"]}" '
                  f'stroke-dasharray="2,2" opacity="0.5"/>'
                  if zero_y is not None else '')

    return f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
  {zero_line}
  <polygon points="{' '.join(fill_pts)}" fill="{fill_color}" opacity="0.15"/>
  <polyline points="{' '.join(points)}" fill="none" stroke="{color}" stroke-width="2"/>
</svg>"""


def _svg_horizontal_bar(value: float, max_value: float, color: str,
                         width: int = 140, height: int = 14) -> str:
    """A small horizontal bar widget."""
    pct = (value / max_value) if max_value > 0 else 0
    bar_w = max(0, min(width, int(width * pct)))
    return f"""<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" xmlns="http://www.w3.org/2000/svg">
  <rect x="0" y="2" width="{width}" height="{height-4}" rx="2" fill="{BRAND['light_gray']}"/>
  <rect x="0" y="2" width="{bar_w}" height="{height-4}" rx="2" fill="{color}"/>
</svg>"""


def _svg_gauge(value: float, danger_at: float, label: str,
                 width: int = 180, height: int = 50) -> str:
    """Risk gauge — shows value position relative to a danger threshold."""
    # value normalized to [-1, 1] where -1 = at danger, 0 = neutral, 1 = safe positive
    if danger_at == 0:
        pct = 0.5
    else:
        pct = max(0, min(1, (1 - value / danger_at) if danger_at < 0 else (value / danger_at)))
    # Color based on proximity to danger
    if (danger_at < 0 and value <= danger_at * 0.5) or (danger_at > 0 and value >= danger_at * 0.5):
        bar_color = BRAND["red"]
    elif (danger_at < 0 and value <= danger_at * 0.25) or (danger_at > 0 and value >= danger_at * 0.25):
        bar_color = BRAND["orange"]
    else:
        bar_color = BRAND["green"]

    bar_w = max(2, int(width * pct))
    return f"""<div style="font-size:11px;color:{BRAND['mid_gray']};text-transform:uppercase;letter-spacing:0.04em;margin-bottom:2px;">{label}</div>
<svg width="{width}" height="14" viewBox="0 0 {width} 14" xmlns="http://www.w3.org/2000/svg">
  <rect x="0" y="2" width="{width}" height="10" rx="2" fill="{BRAND['light_gray']}"/>
  <rect x="0" y="2" width="{bar_w}" height="10" rx="2" fill="{bar_color}"/>
</svg>"""


# ──────────────────────────────────────────────────────────────────────────────
# Section renderers
# ──────────────────────────────────────────────────────────────────────────────


def _header_bar(account: dict, daily_pnl: float, generated: str, mode: str) -> str:
    eq = account.get("equity", 0)
    bp = account.get("buying_power", 0)
    dt_count = account.get("daytrade_count", 0)
    pnl_class = "pos" if daily_pnl >= 0 else "neg"
    pnl_sign = "+" if daily_pnl >= 0 else ""
    return f"""
<div class="header-bar">
  <div class="header-left">
    <h1>scalp 2 — trader dashboard</h1>
    <div class="header-sub">{_e(generated[:19])} · branch <code>dev/harsha/slaudesadvstartegy</code></div>
  </div>
  <div class="header-right">
    <div class="hb-stat"><span class="hb-label">Mode</span><span class="hb-value {('neg' if 'live' in mode.lower() else 'pos')}">{_e(mode.upper())}</span></div>
    <div class="hb-stat"><span class="hb-label">Equity</span><span class="hb-value">${eq:,.0f}</span></div>
    <div class="hb-stat"><span class="hb-label">Buying Power</span><span class="hb-value">${bp:,.0f}</span></div>
    <div class="hb-stat"><span class="hb-label">Today P&L</span><span class="hb-value {pnl_class}">{pnl_sign}${daily_pnl:,.0f}</span></div>
    <div class="hb-stat"><span class="hb-label">Day Trades</span><span class="hb-value">{dt_count}/3</span></div>
  </div>
</div>"""


def _action_panel(today_decisions: list[dict], universe_rules: dict) -> str:
    today_trades = [d for d in today_decisions if d.get("decision") == "TRADE"]
    today_passes = [d for d in today_decisions if d.get("decision") == "PASS"]

    if today_trades:
        first = today_trades[0]
        cards = []
        for t in today_trades[:5]:
            ts = (t.get("timestamp") or "")[11:19]
            cards.append(f"""
<div class="action-card">
  <div class="action-time">{_e(ts)}</div>
  <div class="action-strategy">{_e(t.get("strategy"))}</div>
  <div class="action-ticker">{_e(t.get("ticker"))}</div>
  <div class="action-cta">▶ ACTIVE SIGNAL</div>
</div>""")
        return f"""
<div class="action-panel">
  <div class="action-header">
    <h2>Active signals — {len(today_trades)} TRADE(s) today</h2>
    <span class="badge pos">LIVE</span>
  </div>
  <div class="action-cards">{"".join(cards)}</div>
</div>"""
    else:
        s2_uni = (universe_rules.get("s2") or [])[:6]
        s3_uni = "ALL 37 tickers" if universe_rules.get("s3") is None else "selected"
        return f"""
<div class="action-panel quiet">
  <div class="action-header">
    <h2>No active signals · waiting for setups</h2>
    <span class="badge zero">WAIT</span>
  </div>
  <div class="action-body">
    Daemon is watching for state changes. When a strategy fires, it'll appear here.<br>
    <span class="muted">S3 universe: {_e(s3_uni)} · S2 universe: {_e(', '.join(s2_uni))} ... · S5: low-frequency, paper-only</span>
  </div>
</div>"""


def _strategy_cards(per_strategy: dict, trades: list[dict]) -> str:
    cards = []
    for s in ["S2", "S3", "S5"]:
        m = per_strategy.get(s) or {}
        n = m.get("n_trades", 0)
        win_pct = m.get("win_pct", 0)
        avg_pnl = m.get("avg_pnl_atr", 0)
        sharpe = m.get("sharpe", 0)
        max_dd = m.get("max_drawdown_atr", 0)

        if n == 0:
            verdict = "NO DATA"
            verdict_class = "zero"
        elif sharpe > 4 and win_pct > 60:
            verdict = "STRONG"
            verdict_class = "pos"
        elif sharpe > 0:
            verdict = "OK"
            verdict_class = "zero"
        else:
            verdict = "MUTE"
            verdict_class = "neg"

        # Equity curve from the trade pnls (in order of timestamp)
        s_trades = sorted([t for t in trades if t.get("strategy") == s],
                           key=lambda t: t.get("timestamp_iso") or "")
        pnls = [t.get("pnl_atr", 0) for t in s_trades]
        equity_svg = _svg_equity_curve(pnls, width=300, height=70)
        running = 0; peak = 0; dd_running = 0
        for p in pnls:
            running += p; peak = max(peak, running)
            dd_running = min(dd_running, running - peak)

        win_bar = _svg_horizontal_bar(win_pct, 100, BRAND["green"] if win_pct >= 50 else BRAND["red"], width=110)

        cards.append(f"""
<div class="strat-card">
  <div class="strat-header">
    <span class="strat-name">{_e(s)}</span>
    <span class="badge {verdict_class}">{verdict}</span>
  </div>
  <div class="strat-stats">
    <div class="strat-stat"><span class="label">Trades</span><span class="value">{n}</span></div>
    <div class="strat-stat"><span class="label">Win %</span>
      <span class="value">{win_pct:.1f}%</span>
      {win_bar}
    </div>
    <div class="strat-stat"><span class="label">Avg PnL</span>
      <span class="value {verdict_class}">{avg_pnl:+.2f} ATR</span></div>
    <div class="strat-stat"><span class="label">Sharpe (ann)</span>
      <span class="value {verdict_class}">{sharpe:+.2f}</span></div>
    <div class="strat-stat"><span class="label">Max DD</span>
      <span class="value neg">{max_dd:.2f} ATR</span></div>
  </div>
  <div class="strat-equity">
    <div class="strat-equity-label">Cumulative ATR</div>
    {equity_svg}
  </div>
</div>""")
    return f'<div class="strat-cards-row">{"".join(cards)}</div>'


def _live_trades_section(live_trades: list[dict], today_decisions: list[dict]) -> str:
    """The "live trades today" panel — what actually executed on Alpaca + what
    the daemon decided. This is the section the trader stares at all day.

    `live_trades` are Alpaca order dicts (any status from today). `today_decisions`
    are decision_log rows from dev_journal.db (PASS/TRADE).
    """
    today_trade_decisions = [d for d in today_decisions if d.get("decision") == "TRADE"]

    # Bucket Alpaca orders
    filled = [o for o in live_trades if o.get("status") == "filled"]
    open_orders = [o for o in live_trades if o.get("status") in ("new", "accepted", "partially_filled", "pending_new")]
    other = [o for o in live_trades if o not in filled and o not in open_orders]

    # Header counts
    n_filled = len(filled)
    n_open = len(open_orders)
    n_decisions = len(today_trade_decisions)

    # Determine the session date being displayed (most recent activity day).
    session_date = ""
    for o in live_trades:
        if o.get("__session_date"):
            session_date = o["__session_date"]
            break
    today_str = datetime.now(tz=timezone.utc).date().isoformat()
    if session_date and session_date != today_str:
        session_label = f"last session · {session_date}"
    else:
        session_label = "today"

    if n_filled == 0 and n_open == 0 and n_decisions == 0:
        return f"""
<div class="section">
  <h2>Live trades {session_label} · 0 fills · 0 open orders · 0 daemon TRADE decisions</h2>
  <div class="note">
    No live activity yet. When the daemon fires a TRADE decision or you
    place an order via Alpaca, it'll show here in real time.
    <br><span class="muted">Source: Alpaca <code>/v2/orders?status=all&after=today</code> + dev_journal.db decision_log filtered to <code>decision='TRADE'</code> for today.</span>
  </div>
</div>"""

    # Filled rows
    def _fill_row(o):
        sym = o.get("symbol", "")
        side = (o.get("side") or "").upper()
        side_cls = "pos" if side == "BUY" else "neg"
        qty = o.get("filled_qty") or o.get("qty") or 0
        price = float(o.get("filled_avg_price") or 0)
        notional = float(price) * float(qty or 0)
        ts_filled = (o.get("filled_at") or o.get("submitted_at") or "")[11:19]
        cls_label = (o.get("order_class") or o.get("type") or "")
        order_id = (o.get("client_order_id") or o.get("id") or "")[:14]
        return f"""<tr>
  <td class="small">{_e(ts_filled)}</td>
  <td><b>{_e(sym)}</b></td>
  <td><span class="dir-{side.lower()}">{_e(side)}</span></td>
  <td class="num">{qty}</td>
  <td class="num small">${price:,.2f}</td>
  <td class="num small">${notional:,.0f}</td>
  <td class="small"><code>{_e(cls_label)}</code></td>
  <td class="small muted">{_e(order_id)}</td>
</tr>"""

    def _open_row(o):
        sym = o.get("symbol", "")
        side = (o.get("side") or "").upper()
        qty = o.get("qty") or 0
        limit_px = o.get("limit_price")
        stop_px = o.get("stop_price")
        ts_sub = (o.get("submitted_at") or o.get("created_at") or "")[11:19]
        status = o.get("status", "")
        return f"""<tr>
  <td class="small">{_e(ts_sub)}</td>
  <td><b>{_e(sym)}</b></td>
  <td><span class="dir-{side.lower()}">{_e(side)}</span></td>
  <td class="num">{qty}</td>
  <td class="num small">{('$' + str(limit_px)) if limit_px else '—'}</td>
  <td class="num small">{('$' + str(stop_px)) if stop_px else '—'}</td>
  <td><span class="badge-sm zero">{_e(status)}</span></td>
</tr>"""

    def _decision_row(d):
        ts = (d.get("timestamp") or "")[11:19]
        return f"""<tr>
  <td class="small">{_e(ts)}</td>
  <td>{_e(d.get("strategy"))}</td>
  <td><b>{_e(d.get("ticker"))}</b></td>
  <td><span class="badge-sm pos">TRADE</span></td>
  <td class="num small">{(f"{d.get('ml_prob'):.3f}" if d.get('ml_prob') is not None else '—')}</td>
  <td class="num small">{(f"${d.get('expected_value'):,.2f}" if d.get('expected_value') is not None else '—')}</td>
</tr>"""

    fills_block = ""
    if filled:
        fills_block = f"""
<h3 class="pos">Filled today ({n_filled})</h3>
<table>
  <thead><tr>
    <th>Filled (UTC)</th><th>Symbol</th><th>Side</th><th class="num">Qty</th>
    <th class="num">Avg fill</th><th class="num">Notional</th>
    <th>Class</th><th>Order ID</th>
  </tr></thead>
  <tbody>{"".join(_fill_row(o) for o in filled[:50])}</tbody>
</table>"""

    open_block = ""
    if open_orders:
        open_block = f"""
<h3>Open orders waiting fill ({n_open})</h3>
<table>
  <thead><tr>
    <th>Submitted</th><th>Symbol</th><th>Side</th><th class="num">Qty</th>
    <th class="num">Limit</th><th class="num">Stop</th><th>Status</th>
  </tr></thead>
  <tbody>{"".join(_open_row(o) for o in open_orders[:50])}</tbody>
</table>"""

    decisions_block = ""
    if today_trade_decisions:
        decisions_block = f"""
<h3 class="pos">Daemon TRADE decisions today ({n_decisions})</h3>
<table>
  <thead><tr>
    <th>UTC</th><th>Strategy</th><th>Ticker</th><th>Decision</th>
    <th class="num">ML prob</th><th class="num">Expected $</th>
  </tr></thead>
  <tbody>{"".join(_decision_row(d) for d in today_trade_decisions[:50])}</tbody>
</table>"""

    other_block = ""
    if other:
        other_block = f"""
<div class="muted small">+ {len(other)} other order(s) (canceled / rejected / expired) — not shown</div>"""

    return f"""
<div class="section live-trades">
  <h2>Live trades {session_label} · {n_filled} filled · {n_open} open · {n_decisions} daemon TRADE</h2>
  {decisions_block}
  {fills_block}
  {open_block}
  {other_block}
</div>"""


def _positions_section(positions: list[dict]) -> str:
    if not positions:
        return ""
    total_pl = sum(p.get("unrealized_pl", 0) for p in positions)
    total_mv = sum(abs(p.get("market_value", 0)) for p in positions)
    pl_class = "pos" if total_pl >= 0 else "neg"
    rows = []
    for p in positions:
        cls = "pos" if p.get("unrealized_pl", 0) >= 0 else "neg"
        plpc = p.get("unrealized_plpc", 0) * 100
        rows.append(f"""<tr>
  <td><code>{_e(p.get("symbol"))}</code></td>
  <td class="num">{p.get("qty")}</td>
  <td class="num small">${p.get("avg_entry_price", 0):,.2f}</td>
  <td class="num small">${p.get("current_price", 0):,.2f}</td>
  <td class="num small">${p.get("market_value", 0):,.0f}</td>
  <td class="num {cls}">${p.get("unrealized_pl", 0):+,.0f}</td>
  <td class="num {cls}">{plpc:+.1f}%</td>
</tr>""")
    return f"""
<div class="section">
  <h2>Open positions ({len(positions)}) · ${total_mv:,.0f} mkt value · <span class="{pl_class}">${total_pl:+,.0f}</span> unrealized</h2>
  <table>
    <thead><tr><th>Symbol</th><th class="num">Qty</th><th class="num">Avg entry</th>
      <th class="num">Current</th><th class="num">Mkt value</th>
      <th class="num">Unrealized $</th><th class="num">%</th></tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>"""


def _trades_table(trades: list[dict]) -> str:
    if not trades:
        return f"""
<div class="section">
  <h2>Backtest trades</h2>
  <div class="note">No backtest trades yet. Run <code>python3 scripts/backtest_all.py</code>.</div>
</div>"""
    sorted_trades = sorted(trades, key=lambda t: t.get("timestamp_iso") or "", reverse=True)

    def _row(t):
        cls = "pos" if t.get("is_win") else "neg"
        ts = (t.get("timestamp_iso") or "")[:16].replace("T", " ")
        dirn = t.get("direction", "")
        return f"""<tr>
  <td class="small">{_e(ts)}</td>
  <td>{_e(t.get("strategy"))}</td>
  <td><b>{_e(t.get("ticker"))}</b></td>
  <td><span class="dir-{dirn}">{_e(dirn.upper())}</span></td>
  <td class="num small">${t.get("entry_price", 0):,.2f}</td>
  <td class="num small">${t.get("target_price", 0):,.2f}</td>
  <td class="num small">${t.get("stop_price", 0):,.2f}</td>
  <td class="num small">${t.get("exit_price", 0):,.2f}</td>
  <td><span class="badge-sm {('pos' if t.get('exit_reason') == 'target' else 'neg')}">{_e(t.get("exit_reason"))}</span></td>
  <td class="num small">{t.get("bars_to_event")}</td>
  <td class="num {cls}"><b>{t.get("pnl_atr", 0):+.2f}</b></td>
</tr>"""

    rows_html = "".join(_row(t) for t in sorted_trades[:100])
    return f"""
<div class="section">
  <h2>Backtest trades · {len(trades)} total · showing latest {min(100, len(trades))}</h2>
  <p class="muted">Each trade simulated forward 60 bars after signal fire. Stop-first on wide bars (conservative). PnL in ATR units.</p>
  <table>
    <thead><tr>
      <th>UTC</th><th>Strat</th><th>Ticker</th><th>Dir</th>
      <th class="num">Entry</th><th class="num">Target</th><th class="num">Stop</th>
      <th class="num">Exit</th><th>Exit</th><th class="num">Bars</th>
      <th class="num">PnL ATR</th>
    </tr></thead>
    <tbody>{rows_html}</tbody>
  </table>
</div>"""


def _winners_losers(trades: list[dict]) -> str:
    if not trades:
        return ""
    wins = sorted([t for t in trades if t.get("is_win")],
                   key=lambda t: -t.get("pnl_atr", 0))[:10]
    losses = sorted([t for t in trades if not t.get("is_win")],
                     key=lambda t: t.get("pnl_atr", 0))[:10]

    def _row(t, cls):
        ts = (t.get("timestamp_iso") or "")[:10]
        dirn = t.get("direction", "")
        return f"""<tr>
  <td class="small">{_e(ts)}</td>
  <td>{_e(t.get("strategy"))}</td>
  <td><b>{_e(t.get("ticker"))}</b></td>
  <td><span class="dir-{dirn}">{_e(dirn.upper())}</span></td>
  <td class="num small">${t.get("entry_price", 0):,.2f} → ${t.get("exit_price", 0):,.2f}</td>
  <td class="num {cls}"><b>{t.get("pnl_atr", 0):+.2f}</b></td>
</tr>"""

    wins_html = "".join(_row(t, "pos") for t in wins)
    losses_html = "".join(_row(t, "neg") for t in losses)
    return f"""
<div class="section">
  <h2>Top winners + losers</h2>
  <div style="display:flex;gap:18px;flex-wrap:wrap">
    <div style="flex:1;min-width:380px">
      <h3 class="pos">Top {len(wins)} winners</h3>
      <table>
        <thead><tr><th>Date</th><th>Strat</th><th>Ticker</th><th>Dir</th>
          <th>Trade</th><th class="num">PnL</th></tr></thead>
        <tbody>{wins_html}</tbody>
      </table>
    </div>
    <div style="flex:1;min-width:380px">
      <h3 class="neg">Top {len(losses)} losers</h3>
      <table>
        <thead><tr><th>Date</th><th>Strat</th><th>Ticker</th><th>Dir</th>
          <th>Trade</th><th class="num">PnL</th></tr></thead>
        <tbody>{losses_html}</tbody>
      </table>
    </div>
  </div>
</div>"""


def _per_ticker_leaderboard(per_ticker: dict, trades: list[dict]) -> str:
    if not per_ticker:
        return ""
    leaderboard = []
    for tk, by_strat in per_ticker.items():
        total_n = sum(d.get("n", 0) for d in by_strat.values())
        total_w = sum(d.get("wins", 0) for d in by_strat.values())
        total_pnl = sum(d.get("pnl", 0.0) for d in by_strat.values())
        if total_n > 0:
            leaderboard.append({
                "ticker": tk, "n": total_n,
                "win_pct": 100 * total_w / total_n,
                "avg_pnl": total_pnl / total_n,
                "total_pnl": total_pnl,
                "by_strat": {s: d for s, d in by_strat.items() if d.get("n", 0) > 0},
            })
    leaderboard.sort(key=lambda r: -r["total_pnl"])

    rows = []
    max_pnl = max((abs(r["total_pnl"]) for r in leaderboard), default=1)
    for r in leaderboard:
        cls = "pos" if r["total_pnl"] >= 0 else "neg"
        bar = _svg_horizontal_bar(abs(r["total_pnl"]), max_pnl,
                                    BRAND["green"] if r["total_pnl"] >= 0 else BRAND["red"],
                                    width=120)
        strat_str = " · ".join(f"{s}: {d['n']}({100*d['wins']/d['n']:.0f}%)"
                                 for s, d in r["by_strat"].items())
        rows.append(f"""<tr>
  <td><b>{_e(r["ticker"])}</b></td>
  <td class="num">{r["n"]}</td>
  <td class="num">{r["win_pct"]:.1f}%</td>
  <td class="num {cls}">{r["avg_pnl"]:+.2f}</td>
  <td class="num {cls}"><b>{r["total_pnl"]:+.2f}</b></td>
  <td>{bar}</td>
  <td class="small">{_e(strat_str)}</td>
</tr>""")

    return f"""
<div class="section">
  <h2>Per-ticker leaderboard ({len(leaderboard)} tickers ranked by cumulative ATR)</h2>
  <table>
    <thead><tr>
      <th>Ticker</th><th class="num">Trades</th><th class="num">Win %</th>
      <th class="num">Avg PnL</th><th class="num">Total PnL</th>
      <th>Magnitude</th><th>Per-strategy</th>
    </tr></thead>
    <tbody>{"".join(rows)}</tbody>
  </table>
</div>"""


def _risk_gauges(account: dict, daily_pnl: float, n_open: int, cfg_caps: dict) -> str:
    eq = account.get("equity", 1)
    daily_pnl_pct = (daily_pnl / eq * 100) if eq > 0 else 0
    daily_kill_pct = cfg_caps.get("daily_kill_pct", -2.0)
    weekly_pct = cfg_caps.get("weekly_soft_pct", -4.0)
    concurrent_max = cfg_caps.get("concurrent_max", 2)

    return f"""
<div class="section">
  <h2>Risk monitor</h2>
  <div class="gauge-row">
    <div class="gauge-card">
      {_svg_gauge(daily_pnl_pct, danger_at=daily_kill_pct, label=f"Daily P&L vs kill ({daily_kill_pct:.1f}%)")}
      <div class="gauge-value {('neg' if daily_pnl_pct <= daily_kill_pct/2 else 'pos')}">{daily_pnl_pct:+.2f}%</div>
    </div>
    <div class="gauge-card">
      {_svg_gauge(0, danger_at=weekly_pct, label=f"Weekly P&L vs halt ({weekly_pct:.1f}%)")}
      <div class="gauge-value zero">— (track over week)</div>
    </div>
    <div class="gauge-card">
      <div style="font-size:11px;color:{BRAND['mid_gray']};text-transform:uppercase;letter-spacing:0.04em;margin-bottom:4px;">Concurrent S5 vs cap</div>
      <div class="gauge-value">{n_open} / {concurrent_max}</div>
      <div class="muted small">positions can grow until cap</div>
    </div>
  </div>
</div>"""


# ──────────────────────────────────────────────────────────────────────────────
# Main page renderer
# ──────────────────────────────────────────────────────────────────────────────


def render_trading_dashboard(ctx: dict) -> str:
    bt = ctx.get("backtest_summary") or {}
    per_strategy = bt.get("per_strategy", {})
    per_ticker = ctx.get("per_ticker_breakdown") or {}
    today_decisions = ctx.get("today_decisions") or []
    positions = ctx.get("alpaca_positions") or []
    account = ctx.get("alpaca_account") or {}
    universe_rules = ctx.get("universe_rules") or {}
    trades = ctx.get("backtest_trades") or []
    cfg_caps = ctx.get("cfg_caps") or {}
    mode = ctx.get("trading_mode") or "paper"
    live_trades = ctx.get("live_trades") or []
    generated = ctx.get("generated_utc",
                          datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z")

    # Daily P&L from positions
    daily_pnl = sum(p.get("unrealized_pl", 0) for p in positions)

    css = f"""
    body {{
      font-family: 'Lora', Georgia, serif;
      color: {BRAND['dark']};
      background: {BRAND['light']};
      margin: 0; padding: 0;
    }}
    .container {{ max-width: 1500px; margin: 0 auto; padding: 16px 28px 36px; }}
    h1, h2, h3 {{ font-family: 'Poppins', Arial, sans-serif; font-weight: 600; }}
    h1 {{ font-size: 24px; color: {BRAND['orange']}; margin: 0; }}
    h2 {{ font-size: 17px; margin: 24px 0 8px; border-bottom: 1px solid {BRAND['light_gray']};
          padding-bottom: 4px; }}
    h3 {{ font-size: 14px; margin: 14px 0 8px; }}
    .muted {{ color: {BRAND['mid_gray']}; font-size: 12px; }}
    .small {{ font-size: 11px; }}
    code {{ background: {BRAND['light_gray']}; padding: 1px 6px; border-radius: 3px;
            font-family: 'SF Mono', Menlo, Monaco, monospace; font-size: 12px; }}
    .pos {{ color: {BRAND['green']}; }}
    .neg {{ color: {BRAND['red']}; }}
    .zero {{ color: {BRAND['mid_gray']}; }}

    /* Header bar */
    .header-bar {{
      background: white; border-bottom: 2px solid {BRAND['orange']};
      padding: 14px 28px; display: flex; align-items: center; justify-content: space-between;
      flex-wrap: wrap; gap: 16px;
    }}
    .header-left h1 {{ margin: 0; }}
    .header-sub {{ color: {BRAND['mid_gray']}; font-size: 12px; margin-top: 2px; }}
    .header-right {{ display: flex; gap: 24px; flex-wrap: wrap; }}
    .hb-stat {{ display: flex; flex-direction: column; align-items: flex-end; }}
    .hb-label {{ font-size: 10px; color: {BRAND['mid_gray']};
                 text-transform: uppercase; letter-spacing: 0.06em; }}
    .hb-value {{ font-family: 'Poppins', Arial, sans-serif; font-weight: 600;
                 font-size: 16px; color: {BRAND['dark']}; }}

    /* Action panel */
    .action-panel {{
      background: white; border: 2px solid {BRAND['orange']}; border-radius: 8px;
      padding: 16px 20px; margin: 18px 0;
    }}
    .action-panel.quiet {{ border-color: {BRAND['light_gray']}; background: {BRAND['light_gray']}; }}
    .action-header {{ display: flex; justify-content: space-between; align-items: center;
                       margin-bottom: 10px; }}
    .action-header h2 {{ margin: 0; border-bottom: none; padding: 0; font-size: 16px; }}
    .action-cards {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .action-card {{ background: {BRAND['light']}; border: 1px solid {BRAND['orange']};
                     padding: 10px 14px; border-radius: 6px; min-width: 140px; }}
    .action-time {{ font-size: 10px; color: {BRAND['mid_gray']}; }}
    .action-strategy {{ font-family: 'Poppins', Arial, sans-serif; font-weight: 600;
                         font-size: 11px; color: {BRAND['mid_gray']}; }}
    .action-ticker {{ font-family: 'Poppins', Arial, sans-serif; font-weight: 600;
                       font-size: 18px; color: {BRAND['dark']}; }}
    .action-cta {{ font-family: 'Poppins', Arial, sans-serif; font-weight: 600;
                    font-size: 11px; color: {BRAND['orange']}; margin-top: 4px; }}

    /* Strategy cards */
    .strat-cards-row {{ display: flex; gap: 14px; margin: 14px 0; flex-wrap: wrap; }}
    .strat-card {{ background: white; border: 1px solid {BRAND['light_gray']};
                    border-radius: 8px; padding: 14px 18px; flex: 1; min-width: 320px; }}
    .strat-header {{ display: flex; justify-content: space-between; align-items: center;
                      margin-bottom: 10px; }}
    .strat-name {{ font-family: 'Poppins', Arial, sans-serif; font-weight: 600;
                    font-size: 17px; color: {BRAND['dark']}; }}
    .strat-stats {{ display: grid; grid-template-columns: 1fr 1fr; gap: 6px 16px; margin-bottom: 12px; }}
    .strat-stat {{ display: flex; flex-direction: column; }}
    .strat-stat .label {{ font-size: 10px; color: {BRAND['mid_gray']};
                           text-transform: uppercase; letter-spacing: 0.04em; }}
    .strat-stat .value {{ font-family: 'Poppins', Arial, sans-serif; font-weight: 600;
                           font-size: 14px; }}
    .strat-equity {{ margin-top: 8px; }}
    .strat-equity-label {{ font-size: 10px; color: {BRAND['mid_gray']};
                            text-transform: uppercase; letter-spacing: 0.04em; margin-bottom: 4px; }}

    /* Tables */
    .section {{ margin: 24px 0; }}
    table {{ width: 100%; border-collapse: collapse; background: white;
              border: 1px solid {BRAND['light_gray']}; border-radius: 6px; overflow: hidden; }}
    th, td {{ padding: 6px 10px; text-align: left;
              border-bottom: 1px solid {BRAND['light_gray']}; font-size: 12px; }}
    th {{ background: {BRAND['light_gray']}; font-family: 'Poppins', Arial, sans-serif;
          font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em;
          font-size: 10px; color: {BRAND['dark']}; }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    th.num {{ text-align: right; }}
    tr:hover {{ background: {BRAND['light']}; }}

    /* Badges + dir labels */
    .badge {{ display: inline-block; padding: 3px 9px; border-radius: 3px;
              font-size: 10px; font-family: 'Poppins', Arial, sans-serif;
              font-weight: 600; letter-spacing: 0.06em; }}
    .badge.pos {{ background: {BRAND['green']}; color: white; }}
    .badge.neg {{ background: {BRAND['red']}; color: white; }}
    .badge.zero {{ background: {BRAND['mid_gray']}; color: white; }}
    .badge-sm {{ display: inline-block; padding: 1px 6px; border-radius: 2px;
                 font-size: 10px; font-family: 'Poppins', Arial, sans-serif;
                 font-weight: 600; }}
    .badge-sm.pos {{ background: {BRAND['green']}; color: white; }}
    .badge-sm.neg {{ background: {BRAND['red']}; color: white; }}
    .dir-long {{ color: {BRAND['green']}; font-weight: 600; font-size: 10px; }}
    .dir-short {{ color: {BRAND['red']}; font-weight: 600; font-size: 10px; }}

    /* Risk gauges */
    .gauge-row {{ display: flex; gap: 14px; flex-wrap: wrap; }}
    .gauge-card {{ background: white; border: 1px solid {BRAND['light_gray']};
                    padding: 12px 18px; border-radius: 6px; min-width: 220px; }}
    .gauge-value {{ font-family: 'Poppins', Arial, sans-serif; font-weight: 600;
                     font-size: 18px; margin-top: 4px; }}

    /* Note */
    .note {{ border-left: 4px solid {BRAND['blue']}; background: {BRAND['light_gray']};
              padding: 12px 16px; border-radius: 0 4px 4px 0; font-size: 13px; }}

    /* Live trades — extra prominent block */
    .live-trades h2 {{ color: {BRAND['orange']}; border-bottom-color: {BRAND['orange']}; }}
    .live-trades h3 {{ font-size: 13px; margin: 14px 0 6px; }}
    .live-trades table {{ box-shadow: 0 1px 3px rgba(20,20,19,0.06); }}
    """

    body = f"""
{_header_bar(account, daily_pnl, generated, mode)}

<div class="container">
  {_action_panel(today_decisions, universe_rules)}

  {_live_trades_section(live_trades, today_decisions)}

  <h2>Strategy performance · backtest window {_e(bt.get("window", "—"))}</h2>
  {_strategy_cards(per_strategy, trades)}

  {_positions_section(positions)}

  {_trades_table(trades)}

  {_winners_losers(trades)}

  {_per_ticker_leaderboard(per_ticker, trades)}

  {_risk_gauges(account, daily_pnl, len(positions), cfg_caps)}
</div>
"""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>scalp 2 — trader dashboard</title>
  <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600&family=Lora&display=swap" rel="stylesheet">
  <style>{css}</style>
</head>
<body>{body}</body>
</html>"""


# ──────────────────────────────────────────────────────────────────────────────
# Backtest loader (called by scripts/render_trading_dashboard.py)
# ──────────────────────────────────────────────────────────────────────────────


def gather_backtest_summary(bt_path: Path) -> tuple[dict, dict, list]:
    """Returns (per_strategy_summary, per_ticker_breakdown, raw_trades)."""
    if not bt_path.exists():
        return {}, {}, []
    raw = json.loads(bt_path.read_text())
    per_strategy = raw.get("per_strategy", {})
    trades = raw.get("trades", [])

    per_ticker: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0}
    ))
    for t in trades:
        d = per_ticker[t["ticker"]][t["strategy"]]
        d["n"] += 1
        d["wins"] += int(t["is_win"])
        d["pnl"] += t["pnl_atr"]

    return {
        "per_strategy": per_strategy,
        "window": f'{raw.get("meta",{}).get("start","?")} → {raw.get("meta",{}).get("end","?")}',
    }, dict(per_ticker), trades
