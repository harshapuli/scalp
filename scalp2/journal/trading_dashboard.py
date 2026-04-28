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

import sys
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


def _fmt_signed_money(n: float) -> str:
    """+$1,234 / -$1,234 / $0 — used everywhere money is shown with a sign."""
    if not n:
        return "$0"
    return ("+$" if n > 0 else "-$") + f"{abs(n):,.0f}"


def _header_bar(account: dict, daily_pnl: float, generated: str, mode: str) -> str:
    eq = account.get("equity", 0)
    bp = account.get("buying_power", 0)
    dt_count = account.get("daytrade_count", 0)
    pnl_class = "pos" if daily_pnl >= 0 else "neg"
    return f"""
<div class="header-bar">
  <div class="header-left">
    <h1>scalp 2 — trader dashboard</h1>
    <div class="header-sub">live polling 3s · last reload {_e(generated[:19])}</div>
  </div>
  <div class="header-right">
    <div class="hb-stat"><span class="hb-label">Mode</span><span class="hb-value {('neg' if 'live' in mode.lower() else 'pos')}">{_e(mode.upper())}</span></div>
    <div class="hb-stat"><span class="hb-label">Equity</span><span class="hb-value" data-live="equity">${eq:,.0f}</span></div>
    <div class="hb-stat"><span class="hb-label">Buying Power</span><span class="hb-value" data-live="bp">${bp:,.0f}</span></div>
    <div class="hb-stat"><span class="hb-label">Today P&L</span><span class="hb-value {pnl_class}" data-live="pnl">{_fmt_signed_money(daily_pnl)}</span></div>
    <div class="hb-stat"><span class="hb-label">Day Trades</span><span class="hb-value" data-live="daytrade">{dt_count}/3</span></div>
  </div>
</div>"""


def _can_i_trade_panel(today_decisions: list[dict],
                         account: dict,
                         positions: list[dict],
                         daily_pnl: float,
                         cfg_caps: dict) -> str:
    """The dominant element on the page: GO / NO-GO answer.

    Evaluates account-level risk gates first. If any is blown, the page tells
    you flat-out NO before listing any signals. If risk is OK, surfaces every
    today TRADE decision with explicit "TAKE IT" call-to-action.
    """
    eq = account.get("equity", 0) or 1
    daytrade = account.get("daytrade_count", 0) or 0
    daily_kill_pct = cfg_caps.get("daily_kill_pct", -0.020) * 100
    weekly_pct = cfg_caps.get("weekly_soft_pct", -0.040) * 100
    concurrent_max = cfg_caps.get("concurrent_max", 2)
    pnl_pct = (daily_pnl / eq * 100) if eq > 0 else 0
    n_open = len(positions)

    # Evaluate gates
    gates = []
    # Daily kill
    if pnl_pct <= daily_kill_pct:
        gates.append(("FAIL", f"Daily kill hit · {pnl_pct:+.2f}% vs {daily_kill_pct:.1f}%",
                       "stop trading until tomorrow"))
    elif pnl_pct <= daily_kill_pct * 0.5:
        gates.append(("WARN", f"Daily P&L approaching kill · {pnl_pct:+.2f}% (kill at {daily_kill_pct:.1f}%)",
                       "size down, one more loss = halt"))
    else:
        gates.append(("OK", f"Daily P&L · {pnl_pct:+.2f}% (kill at {daily_kill_pct:.1f}%)", ""))

    # Day trade count
    if daytrade >= 3:
        gates.append(("FAIL", f"Day trades · {daytrade}/3 PDT cap reached",
                       "no more round-trips today"))
    elif daytrade >= 2:
        gates.append(("WARN", f"Day trades · {daytrade}/3", "1 left before PDT block"))
    else:
        gates.append(("OK", f"Day trades · {daytrade}/3", ""))

    # Concurrent positions (S5 cap is on options spreads — count option positions)
    n_option_positions = sum(1 for p in positions
                              if len(p.get("symbol", "")) > 6 and any(c.isdigit() for c in p.get("symbol", "")))
    if n_option_positions >= concurrent_max:
        gates.append(("WARN", f"S5 concurrent · {n_option_positions}/{concurrent_max}",
                       "at cap — close one before opening another"))
    else:
        gates.append(("OK", f"S5 concurrent · {n_option_positions}/{concurrent_max}", ""))

    blocked = any(g[0] == "FAIL" for g in gates)

    today_trade_decisions = [d for d in today_decisions if d.get("decision") == "TRADE"]
    n_trade = len(today_trade_decisions)

    # ─── Top banner: GO / NO-GO ─────────────────────────────────────────────
    if blocked:
        banner_class = "banner-blocked"
        banner_emoji = "⛔"
        banner_text = "DON'T TRADE"
        banner_sub = next((g[2] for g in gates if g[0] == "FAIL"), "risk gate hit")
    elif n_trade > 0:
        banner_class = "banner-go"
        banner_emoji = "⚡"
        banner_text = f"TAKE IT — {n_trade} ACTIVE SIGNAL{'S' if n_trade > 1 else ''}"
        banner_sub = "see cards below for entry / stop / target"
    else:
        banner_class = "banner-wait"
        banner_emoji = "⏸"
        banner_text = "WAIT — no signals firing"
        banner_sub = "daemon is watching; no strategy gates have triggered"

    # ─── Risk gate strip ─────────────────────────────────────────────────────
    gate_chips = []
    for status, label, _hint in gates:
        cls = {"OK": "gate-ok", "WARN": "gate-warn", "FAIL": "gate-fail"}[status]
        emoji = {"OK": "✓", "WARN": "⚠", "FAIL": "✗"}[status]
        gate_chips.append(f'<span class="gate-chip {cls}">{emoji} {_e(label)}</span>')

    # ─── Trade-decision cards (one per TRADE row from decision_log) ───────
    trade_cards_html = ""
    if today_trade_decisions:
        cards = []
        for t in today_trade_decisions[:8]:
            ts = (t.get("timestamp") or "")[11:19]
            ml_str = (f"{t.get('ml_prob'):.3f}" if t.get('ml_prob') is not None else "—")
            ev_str = (f"${t.get('expected_value'):,.2f}" if t.get('expected_value') is not None else "—")
            direction = (t.get("direction") or "").upper()
            dir_class = "go-long" if direction == "LONG" else "go-short" if direction == "SHORT" else ""
            cards.append(f"""
<div class="trade-card {('disabled' if blocked else 'active')}">
  <div class="tc-head">
    <span class="tc-ticker">{_e(t.get("ticker", "?"))}</span>
    <span class="tc-strategy">{_e(t.get("strategy", "?"))}</span>
    <span class="tc-dir {dir_class}">{_e(direction or "—")}</span>
  </div>
  <div class="tc-meta">
    <span>{_e(ts)} UTC</span>
    <span>ML <b>{ml_str}</b></span>
    <span>EV <b>{ev_str}</b></span>
  </div>
  <div class="tc-cta">{('⛔ BLOCKED — risk gate' if blocked else '⚡ TAKE THIS TRADE')}</div>
</div>""")
        trade_cards_html = f'<div class="trade-cards">{"".join(cards)}</div>'

    return f"""
<div class="action-panel">
  <div class="banner {banner_class}">
    <div class="banner-main">
      <span class="banner-emoji">{banner_emoji}</span>
      <span class="banner-text">{banner_text}</span>
    </div>
    <div class="banner-sub">{_e(banner_sub)}</div>
  </div>
  <div class="gate-strip">{"".join(gate_chips)}</div>
  {trade_cards_html}
</div>"""


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
  <td class="num {cls}">{_fmt_signed_money(p.get("unrealized_pl", 0))}</td>
  <td class="num {cls}">{plpc:+.1f}%</td>
</tr>""")
    return f"""
<div class="section">
  <h2>Open positions ({len(positions)}) · ${total_mv:,.0f} mkt value · <span class="{pl_class}">{_fmt_signed_money(total_pl)}</span> unrealized</h2>
  <table>
    <thead><tr><th>Symbol</th><th class="num">Qty</th><th class="num">Avg entry</th>
      <th class="num">Current</th><th class="num">Mkt value</th>
      <th class="num">Unrealized $</th><th class="num">%</th></tr></thead>
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
    today_decisions = ctx.get("today_decisions") or []
    positions = ctx.get("alpaca_positions") or []
    account = ctx.get("alpaca_account") or {}
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

    /* Action panel — Can I take this trade? */
    .action-panel {{ margin: 18px 0; }}
    .banner {{
      border-radius: 12px; padding: 22px 28px; margin-bottom: 12px;
      display: flex; flex-direction: column; gap: 4px;
    }}
    .banner-main {{ display: flex; align-items: center; gap: 12px; }}
    .banner-emoji {{ font-size: 32px; }}
    .banner-text {{
      font-family: 'Poppins', Arial, sans-serif; font-weight: 700;
      font-size: 26px; letter-spacing: 0.02em;
    }}
    .banner-sub {{
      font-family: 'Poppins', Arial, sans-serif; font-size: 13px;
      opacity: 0.85;
    }}
    .banner-go {{
      background: linear-gradient(135deg, #788c5d 0%, #3d7c5e 100%);
      color: white; box-shadow: 0 4px 14px rgba(61,124,94,0.35);
    }}
    .banner-blocked {{
      background: linear-gradient(135deg, #b85436 0%, #8d2920 100%);
      color: white; box-shadow: 0 4px 14px rgba(176,53,40,0.35);
    }}
    .banner-wait {{
      background: {BRAND['light_gray']}; color: {BRAND['dark']};
      border: 1px solid {BRAND['mid_gray']};
    }}
    .gate-strip {{
      display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px;
    }}
    .gate-chip {{
      font-family: 'Poppins', Arial, sans-serif; font-size: 11px; font-weight: 500;
      padding: 6px 12px; border-radius: 999px;
      display: inline-flex; align-items: center; gap: 6px;
    }}
    .gate-ok   {{ background: #e7ece0; color: #3d7c5e; border: 1px solid rgba(61,124,94,0.2); }}
    .gate-warn {{ background: #fef3c7; color: #854d0e; border: 1px solid rgba(161,98,7,0.3); }}
    .gate-fail {{ background: #f9e0dd; color: #8d2920; border: 1px solid rgba(176,53,40,0.3); }}

    .trade-cards {{ display: flex; gap: 10px; flex-wrap: wrap; }}
    .trade-card {{
      flex: 1; min-width: 240px;
      background: white; border: 2px solid {BRAND['orange']};
      border-radius: 10px; padding: 14px 18px;
    }}
    .trade-card.disabled {{
      border-color: {BRAND['mid_gray']}; opacity: 0.6;
    }}
    .tc-head {{ display: flex; align-items: center; gap: 10px; margin-bottom: 6px; }}
    .tc-ticker {{
      font-family: 'Poppins', Arial, sans-serif; font-weight: 700;
      font-size: 22px; color: {BRAND['dark']};
    }}
    .tc-strategy {{
      font-family: 'Poppins', Arial, sans-serif; font-size: 10px; font-weight: 600;
      letter-spacing: 0.06em; padding: 3px 8px; border-radius: 4px;
      background: #fceee7; color: #9a3412;
    }}
    .tc-dir {{
      font-family: 'Poppins', Arial, sans-serif; font-size: 10px; font-weight: 700;
      letter-spacing: 0.06em; padding: 3px 9px; border-radius: 4px;
      color: white;
    }}
    .tc-dir.go-long {{ background: linear-gradient(135deg, #3d7c5e 0%, #4d9b75 100%); }}
    .tc-dir.go-short {{ background: linear-gradient(135deg, #b03528 0%, #d04638 100%); }}
    .tc-meta {{
      display: flex; gap: 14px; font-size: 11px; color: {BRAND['mid_gray']};
      font-family: 'Poppins', Arial, sans-serif; font-variant-numeric: tabular-nums;
      margin-bottom: 8px;
    }}
    .tc-meta b {{ color: {BRAND['dark']}; font-weight: 600; }}
    .tc-cta {{
      font-family: 'Poppins', Arial, sans-serif; font-weight: 700;
      font-size: 13px; padding: 8px 12px; border-radius: 6px;
      text-align: center; letter-spacing: 0.04em;
      background: #fceee7; color: {BRAND['orange']};
    }}
    .trade-card.disabled .tc-cta {{
      background: {BRAND['light_gray']}; color: {BRAND['mid_gray']};
    }}

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

    /* Polling failure indicator */
    body.poll-error {{ box-shadow: inset 0 0 0 4px {BRAND['red']}; }}
    body.poll-error::before {{
      content: "⚠ live polling stalled — check server"; position: fixed;
      top: 0; right: 12px; background: {BRAND['red']}; color: white;
      padding: 4px 12px; font-family: 'Poppins', Arial, sans-serif;
      font-size: 11px; z-index: 100; border-radius: 0 0 4px 4px;
    }}

    /* Live trades — extra prominent block */
    .live-trades h2 {{ color: {BRAND['orange']}; border-bottom-color: {BRAND['orange']}; }}
    .live-trades h3 {{ font-size: 13px; margin: 14px 0 6px; }}
    .live-trades table {{ box-shadow: 0 1px 3px rgba(20,20,19,0.06); }}

    """

    # Embed the cfg_caps so the client-side JS can re-evaluate gates.
    import json as _json
    caps_json = _json.dumps({
        "daily_kill_pct": cfg_caps.get("daily_kill_pct", -0.020),
        "weekly_soft_pct": cfg_caps.get("weekly_soft_pct", -0.040),
        "concurrent_max": cfg_caps.get("concurrent_max", 2),
    })

    body = f"""
{_header_bar(account, daily_pnl, generated, mode)}

<div class="container">
  <div id="action-panel-host">
    {_can_i_trade_panel(today_decisions, account, positions, daily_pnl, cfg_caps)}
  </div>

  <div id="live-trades-host">
    {_live_trades_section(live_trades, today_decisions)}
  </div>

  <div id="positions-host">
    {_positions_section(positions)}
  </div>

  {_risk_gauges(account, daily_pnl, len(positions), cfg_caps)}
</div>

<script>
  // ── Live polling layer for scalping ────────────────────────────────────
  // Polls /api/live every 3s. Updates banner state, positions P&L, and live
  // trades feed without a page reload. The full-page HTML renders only every
  // 5 min server-side (for structure / signals); the live JSON drives the
  // numbers that move tick-by-tick.
  const CFG_CAPS = {caps_json};
  const LIVE_POLL_MS = 3000;

  function fmtMoney(n) {{
    // Unsigned magnitude: "$1,234" — caller adds the sign.
    if (n == null) return "—";
    return "$" + Math.abs(n).toLocaleString(undefined, {{minimumFractionDigits: 0, maximumFractionDigits: 0}});
  }}
  function fmtSignedMoney(n) {{
    // Always shows sign: "+$1,234" / "-$1,234" / "$0".
    if (n == null) return "—";
    if (n === 0) return "$0";
    return (n > 0 ? "+" : "-") + "$" + Math.abs(n).toLocaleString(undefined, {{minimumFractionDigits: 0, maximumFractionDigits: 0}});
  }}
  function fmtPct(n, digits=2) {{
    if (n == null) return "—";
    const sign = n >= 0 ? "+" : "";
    return sign + n.toFixed(digits) + "%";
  }}

  function classifyGate(label, value, danger) {{
    // Returns ['OK'|'WARN'|'FAIL', cls, emoji]
    if (danger < 0) {{
      if (value <= danger) return ["FAIL", "gate-fail", "✗"];
      if (value <= danger * 0.5) return ["WARN", "gate-warn", "⚠"];
      return ["OK", "gate-ok", "✓"];
    }}
    if (value >= danger) return ["FAIL", "gate-fail", "✗"];
    if (value >= danger * 0.66) return ["WARN", "gate-warn", "⚠"];
    return ["OK", "gate-ok", "✓"];
  }}

  function isOptionSymbol(sym) {{
    return sym && sym.length > 6 && /\\d/.test(sym);
  }}

  function updateBanner(state) {{
    const acct = state.account || {{}};
    const positions = state.positions || [];
    const decisions = state.decisions || [];
    const eq = acct.equity || 1;
    const dailyPnl = positions.reduce((s, p) => s + (p.unrealized_pl || 0), 0);
    const pnlPct = (dailyPnl / eq) * 100;
    const dt = acct.daytrade_count || 0;
    const nOptions = positions.filter(p => isOptionSymbol(p.symbol)).length;
    const tradeDecisions = decisions.filter(d => d.decision === "TRADE");

    const dailyKillPct = (CFG_CAPS.daily_kill_pct || -0.020) * 100;
    const concurrentMax = CFG_CAPS.concurrent_max || 2;

    const gates = [];
    const [s1, c1, e1] = classifyGate("Daily P&L", pnlPct, dailyKillPct);
    gates.push({{status: s1, label: `Daily P&L · ${{pnlPct.toFixed(2)}}% (kill at ${{dailyKillPct.toFixed(1)}}%)`, cls: c1, emoji: e1}});
    if (dt >= 3)        gates.push({{status: "FAIL", label: `Day trades · ${{dt}}/3 PDT cap reached`, cls: "gate-fail", emoji: "✗"}});
    else if (dt >= 2)   gates.push({{status: "WARN", label: `Day trades · ${{dt}}/3`, cls: "gate-warn", emoji: "⚠"}});
    else                gates.push({{status: "OK",   label: `Day trades · ${{dt}}/3`, cls: "gate-ok",   emoji: "✓"}});
    if (nOptions >= concurrentMax) gates.push({{status: "WARN", label: `S5 concurrent · ${{nOptions}}/${{concurrentMax}}`, cls: "gate-warn", emoji: "⚠"}});
    else                            gates.push({{status: "OK",   label: `S5 concurrent · ${{nOptions}}/${{concurrentMax}}`, cls: "gate-ok",   emoji: "✓"}});

    const blocked = gates.some(g => g.status === "FAIL");
    let bannerClass, bannerEmoji, bannerText, bannerSub;
    if (blocked) {{
      bannerClass = "banner-blocked"; bannerEmoji = "⛔";
      bannerText = "DON'T TRADE";
      bannerSub = "risk gate hit — see below";
    }} else if (tradeDecisions.length > 0) {{
      bannerClass = "banner-go"; bannerEmoji = "⚡";
      bannerText = `TAKE IT — ${{tradeDecisions.length}} ACTIVE SIGNAL${{tradeDecisions.length > 1 ? "S" : ""}}`;
      bannerSub = "see cards below for entry / stop / target";
    }} else {{
      bannerClass = "banner-wait"; bannerEmoji = "⏸";
      bannerText = "WAIT — no signals firing";
      bannerSub = "daemon is watching; no strategy gates have triggered";
    }}

    const tradeCardsHtml = tradeDecisions.slice(0, 8).map(t => {{
      const ts = (t.timestamp || "").substring(11, 19);
      const ml = t.ml_prob != null ? Number(t.ml_prob).toFixed(3) : "—";
      const ev = t.expected_value != null ? "$" + Number(t.expected_value).toFixed(2) : "—";
      const dir = (t.direction || "").toUpperCase();
      const dirClass = dir === "LONG" ? "go-long" : dir === "SHORT" ? "go-short" : "";
      return `<div class="trade-card ${{blocked ? "disabled" : "active"}}">
        <div class="tc-head">
          <span class="tc-ticker">${{t.ticker || "?"}}</span>
          <span class="tc-strategy">${{t.strategy || "?"}}</span>
          <span class="tc-dir ${{dirClass}}">${{dir || "—"}}</span>
        </div>
        <div class="tc-meta"><span>${{ts}} UTC</span><span>ML <b>${{ml}}</b></span><span>EV <b>${{ev}}</b></span></div>
        <div class="tc-cta">${{blocked ? "⛔ BLOCKED — risk gate" : "⚡ TAKE THIS TRADE"}}</div>
      </div>`;
    }}).join("");

    const gatesHtml = gates.map(g =>
      `<span class="gate-chip ${{g.cls}}">${{g.emoji}} ${{g.label}}</span>`
    ).join("");

    const html = `<div class="action-panel">
      <div class="banner ${{bannerClass}}">
        <div class="banner-main">
          <span class="banner-emoji">${{bannerEmoji}}</span>
          <span class="banner-text">${{bannerText}}</span>
        </div>
        <div class="banner-sub">${{bannerSub}}</div>
      </div>
      <div class="gate-strip">${{gatesHtml}}</div>
      ${{tradeDecisions.length > 0 ? `<div class="trade-cards">${{tradeCardsHtml}}</div>` : ""}}
    </div>`;
    document.getElementById("action-panel-host").innerHTML = html;
  }}

  function updateHeader(state) {{
    const acct = state.account || {{}};
    const positions = state.positions || [];
    const dailyPnl = positions.reduce((s, p) => s + (p.unrealized_pl || 0), 0);
    const eqEl = document.querySelector("[data-live=equity]");
    const bpEl = document.querySelector("[data-live=bp]");
    const pnlEl = document.querySelector("[data-live=pnl]");
    const dtEl = document.querySelector("[data-live=daytrade]");
    if (eqEl) eqEl.textContent = fmtMoney(acct.equity || 0);
    if (bpEl) bpEl.textContent = fmtMoney(acct.buying_power || 0);
    if (pnlEl) {{
      pnlEl.textContent = fmtSignedMoney(dailyPnl);
      pnlEl.className = "hb-value " + (dailyPnl >= 0 ? "pos" : "neg");
    }}
    if (dtEl) dtEl.textContent = (acct.daytrade_count || 0) + "/3";
  }}

  function updatePositions(state) {{
    const positions = state.positions || [];
    const host = document.getElementById("positions-host");
    if (!positions.length) {{
      host.innerHTML = "";
      return;
    }}
    const totalPl = positions.reduce((s, p) => s + (p.unrealized_pl || 0), 0);
    const totalMv = positions.reduce((s, p) => s + Math.abs(p.market_value || 0), 0);
    const plClass = totalPl >= 0 ? "pos" : "neg";

    const rows = positions.map(p => {{
      const cls = (p.unrealized_pl || 0) >= 0 ? "pos" : "neg";
      const plpc = (p.unrealized_plpc || 0) * 100;
      return `<tr>
        <td><code>${{p.symbol}}</code></td>
        <td class="num">${{p.qty}}</td>
        <td class="num small">$${{Number(p.avg_entry_price || 0).toFixed(2)}}</td>
        <td class="num small">$${{Number(p.current_price || 0).toFixed(2)}}</td>
        <td class="num small">${{fmtMoney(p.market_value || 0)}}</td>
        <td class="num ${{cls}}">${{fmtSignedMoney(p.unrealized_pl || 0)}}</td>
        <td class="num ${{cls}}">${{fmtPct(plpc, 1)}}</td>
      </tr>`;
    }}).join("");

    host.innerHTML = `<div class="section">
      <h2>Open positions (${{positions.length}}) · ${{fmtMoney(totalMv)}} mkt value · <span class="${{plClass}}">${{fmtSignedMoney(totalPl)}}</span> unrealized</h2>
      <table>
        <thead><tr><th>Symbol</th><th class="num">Qty</th><th class="num">Avg entry</th>
          <th class="num">Current</th><th class="num">Mkt value</th>
          <th class="num">Unrealized $</th><th class="num">%</th></tr></thead>
        <tbody>${{rows}}</tbody>
      </table>
    </div>`;
  }}

  function updateLiveTrades(state) {{
    const orders = state.live_orders || [];
    const decisions = state.decisions || [];
    const tradeDecisions = decisions.filter(d => d.decision === "TRADE");
    const filled = orders.filter(o => o.status === "filled");
    const open = orders.filter(o => ["new","accepted","partially_filled","pending_new"].includes(o.status));

    const host = document.getElementById("live-trades-host");
    if (!filled.length && !open.length && !tradeDecisions.length) {{
      host.innerHTML = `<div class="section"><h2>Live trades today · 0 fills · 0 open · 0 daemon TRADE</h2><div class="note">No live activity yet. Daemon decisions + Alpaca fills appear here in real time (3s poll).</div></div>`;
      return;
    }}

    const decisionsBlock = tradeDecisions.length > 0 ? `
      <h3 class="pos">Daemon TRADE decisions today (${{tradeDecisions.length}})</h3>
      <table>
        <thead><tr><th>UTC</th><th>Strategy</th><th>Ticker</th><th>Decision</th><th class="num">ML</th><th class="num">EV</th></tr></thead>
        <tbody>${{tradeDecisions.slice(0, 50).map(d => {{
          const ts = (d.timestamp || "").substring(11, 19);
          const ml = d.ml_prob != null ? Number(d.ml_prob).toFixed(3) : "—";
          const ev = d.expected_value != null ? "$" + Number(d.expected_value).toFixed(2) : "—";
          return `<tr><td class="small">${{ts}}</td><td>${{d.strategy || ""}}</td><td><b>${{d.ticker || ""}}</b></td><td><span class="badge-sm pos">TRADE</span></td><td class="num small">${{ml}}</td><td class="num small">${{ev}}</td></tr>`;
        }}).join("")}}</tbody>
      </table>` : "";

    const filledBlock = filled.length > 0 ? `
      <h3 class="pos">Filled today (${{filled.length}})</h3>
      <table>
        <thead><tr><th>UTC</th><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Avg fill</th><th class="num">Notional</th></tr></thead>
        <tbody>${{filled.slice(0, 50).map(o => {{
          const ts = ((o.filled_at || o.submitted_at) || "").substring(11, 19);
          const side = (o.side || "").toUpperCase();
          const qty = o.filled_qty || o.qty || 0;
          const px = Number(o.filled_avg_price || 0);
          const notional = px * Number(qty);
          return `<tr><td class="small">${{ts}}</td><td><b>${{o.symbol}}</b></td><td><span class="dir-${{side.toLowerCase()}}">${{side}}</span></td><td class="num">${{qty}}</td><td class="num small">$${{px.toFixed(2)}}</td><td class="num small">${{fmtMoney(notional)}}</td></tr>`;
        }}).join("")}}</tbody>
      </table>` : "";

    const openBlock = open.length > 0 ? `
      <h3>Open orders (${{open.length}})</h3>
      <table>
        <thead><tr><th>UTC</th><th>Symbol</th><th>Side</th><th class="num">Qty</th><th class="num">Limit</th><th>Status</th></tr></thead>
        <tbody>${{open.slice(0, 50).map(o => `<tr>
          <td class="small">${{((o.submitted_at || o.created_at) || "").substring(11, 19)}}</td>
          <td><b>${{o.symbol}}</b></td>
          <td><span class="dir-${{(o.side || "").toLowerCase()}}">${{(o.side || "").toUpperCase()}}</span></td>
          <td class="num">${{o.qty || 0}}</td>
          <td class="num small">${{o.limit_price ? "$" + o.limit_price : "—"}}</td>
          <td><span class="badge-sm zero">${{o.status || ""}}</span></td>
        </tr>`).join("")}}</tbody>
      </table>` : "";

    host.innerHTML = `<div class="section live-trades">
      <h2>Live trades today · ${{filled.length}} filled · ${{open.length}} open · ${{tradeDecisions.length}} daemon TRADE</h2>
      ${{decisionsBlock}}${{filledBlock}}${{openBlock}}
    </div>`;
  }}

  let lastFetchOk = true;
  async function pollLive() {{
    try {{
      const r = await fetch("/api/live", {{ cache: "no-store" }});
      if (!r.ok) throw new Error("status " + r.status);
      const state = await r.json();
      if (state.stale) return;
      updateHeader(state);
      updateBanner(state);
      updatePositions(state);
      updateLiveTrades(state);
      // Indicator: clear any error border
      document.body.classList.remove("poll-error");
      lastFetchOk = true;
    }} catch (e) {{
      if (lastFetchOk) {{
        console.warn("poll failed:", e);
      }}
      document.body.classList.add("poll-error");
      lastFetchOk = false;
    }}
  }}
  pollLive();
  setInterval(pollLive, LIVE_POLL_MS);
</script>
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


