"""journal/conviction_dashboard.py — scalp 2 conviction page.

Mirrors the engine_v4 /conviction visual language (Anthropic palette, Poppins/
Lora, card-per-ticker, action stages, gradient direction pills) but is wired
to scalp 2's strategies + data:

  Stages:
    ⚡ TRADE NOW  — decision_log fired TRADE for this ticker today
    🌿 FORMING    — backtest edge exists; setup likely tomorrow
    ⏸ WAIT       — recent loss or just exited; let it rest
    — PASS       — no edge in backtest

  Per-card:
    ticker · best-strategy badge (S2/S3/S5) · direction (LONG/SHORT) · stage
    confidence bar (0-100, derived from total ATR pnl + win rate)
    backtest stats: trades · win % · total ATR · sharpe
    fired-signals chips (which gates the strategy passed historically)
    buy/plan line: per-strategy entry recipe

Pure HTML + inline SVG — no JS framework. (Filter chips use a tiny inline
script for show/hide; data is baked at render time.)
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# Anthropic palette — copied verbatim from engine_v4/conviction.html so the
# pages share visual identity if the user opens both.
BRAND = {
    "ink":         "#141413",
    "bg":          "#faf9f5",
    "panel":       "#ffffff",
    "hair":        "#e8e6dc",
    "dim":         "#6b6a65",
    "mid":         "#b0aea5",
    "accent":      "#d97757",
    "accent_soft": "#fceee7",
    "blue":        "#6a9bcc",
    "blue_soft":   "#e4edf6",
    "green":       "#788c5d",
    "green_soft":  "#e7ece0",
    "bull":        "#3d7c5e",
    "bear":        "#b03528",
    "warn":        "#a16207",
    "warn_soft":   "#fef3c7",
}


def _e(s) -> str:
    if s is None:
        return ""
    s = str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
              .replace('"', "&quot;").replace("'", "&#39;"))


# ─── Per-strategy buy-line recipe ─────────────────────────────────────────────


def _entry_plan_for(strategy: str, ticker: str, direction: str,
                    last_close: Optional[float], atr: Optional[float]) -> str:
    """Per-strategy human-readable entry plan for the buy box.

    These are deterministic from spec §B4 + per-strategy gates. The conviction
    page presents them as the "what to do" if/when the setup triggers tomorrow.
    """
    px = f"${last_close:,.2f}" if last_close else "—"
    a = f"${atr:.2f}" if atr else "—"
    if strategy == "S2":
        side = "long" if direction.lower() == "long" else "short"
        side_word = "above OR high" if side == "long" else "below OR low"
        return (f"S2 ORB {side.upper()} · enter on first 5m close {side_word} "
                f"with vol≥1.5× · stop = OR midpoint, target = +1 ATR ({a}) "
                f"· last close {px}")
    if strategy == "S3":
        side = "long" if direction.lower() == "long" else "short"
        cond = ("higher-lows + aggressor>0.60" if side == "long"
                else "lower-highs + aggressor<0.40")
        return (f"S3 momentum {side.upper()} · enter mid-session if "
                f"session-return≥1.5 ATR + VWAP-distance≥0.75 ATR + {cond} "
                f"· stop = VWAP, target = +1 ATR ({a}) · last close {px}")
    if strategy == "S5":
        side = "long" if direction.lower() == "long" else "short"
        wall = "put wall (below)" if side == "long" else "call wall (above)"
        opt_side = "CALL debit spread" if side == "long" else "PUT debit spread"
        return (f"S5 gamma reversal {side.upper()} · price extended into "
                f"{wall} + reversal-score ≥ 0.50 · buy 7-14 DTE {opt_side} "
                f"(35-45Δ long) · risk 0.5% NAV · last close {px}")
    return f"{strategy} {direction.upper()} · last close {px}"


# ─── Per-ticker conviction scoring ────────────────────────────────────────────


def _score_ticker(by_strategy: dict, today_decisions: list[dict],
                   recent_alpaca_position: bool = False) -> dict:
    """Computes the per-ticker conviction record for the dashboard.

    Returns a dict with:
      best_strategy, best_direction, confidence (0-100), stage, signals_fired,
      total_atr, total_trades, total_wins, sharpe, recipe
    """
    # Pick the strategy with the highest total ATR pnl on this ticker
    best = None
    best_pnl = -1e9
    for strat, d in by_strategy.items():
        if d.get("n", 0) > 0 and d.get("pnl", 0.0) > best_pnl:
            best = strat
            best_pnl = d["pnl"]

    if best is None:
        return {
            "best_strategy": None,
            "best_direction": None,
            "confidence": 0,
            "stage": "PASS",
            "total_atr": 0.0,
            "total_trades": 0,
            "total_wins": 0,
            "win_pct": 0.0,
        }

    d = by_strategy[best]
    n = d.get("n", 0)
    wins = d.get("wins", 0)
    pnl = d.get("pnl", 0.0)
    win_pct = 100.0 * wins / max(1, n)
    direction = d.get("last_direction", "long")
    signals = d.get("signals_fired", [])

    # Confidence: weighted blend of (1) total ATR ranked, (2) win rate above 50%,
    # (3) trade count (sample size confidence). Capped 0-100.
    pnl_score = max(0.0, min(40.0, pnl * 8.0))         # +5 ATR ⇒ 40 pts
    wr_score = max(0.0, min(40.0, (win_pct - 50.0)))   # 70% ⇒ 20 pts (·2 = 40)
    n_score = max(0.0, min(20.0, n * 2.0))             # 10 trades ⇒ 20 pts
    confidence = int(round(pnl_score * 1.0 + wr_score * 1.0 + n_score * 1.0))
    confidence = max(0, min(100, confidence))

    # Stage
    fired_today = any(
        (dec.get("ticker") == d.get("ticker_for_decisions") and dec.get("decision") == "TRADE")
        for dec in today_decisions
    )
    if fired_today:
        stage = "TRADE_NOW"
    elif recent_alpaca_position:
        stage = "WAIT"
    elif confidence >= 30:
        stage = "FORMING"
    else:
        stage = "PASS"

    return {
        "best_strategy": best,
        "best_direction": direction,
        "confidence": confidence,
        "stage": stage,
        "total_atr": pnl,
        "total_trades": n,
        "total_wins": wins,
        "win_pct": win_pct,
        "signals_fired": signals,
    }


# ─── Cap bucket (visual only) ────────────────────────────────────────────────


def _cap_bucket(ticker: str) -> str:
    etfs = {"SPY", "QQQ", "IWM", "DIA", "SOXX", "SOXS", "SQQQ", "TQQQ", "XLE",
            "XLF", "XLK", "SMH"}
    mega = {"AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "TSLA", "AVGO",
            "NFLX"}
    mid = {"AMD", "ABNB", "ARM", "COIN", "CRWD", "CVNA", "HOOD", "MSTR", "MU",
           "PLTR", "RDDT", "ROKU", "SHOP", "SMCI", "SNDK"}
    if ticker in etfs:
        return "ETF"
    if ticker in mega:
        return "MEGA"
    if ticker in mid:
        return "MID"
    return "SMALL"


# ─── Card renderer ────────────────────────────────────────────────────────────


def _render_card(ticker: str, score: dict, last_close: Optional[float],
                 atr: Optional[float], position: Optional[dict] = None) -> str:
    """One ticker card. Mirrors engine_v4 conviction card structure."""
    strat = score.get("best_strategy") or "—"
    direction = (score.get("best_direction") or "long").upper()
    dir_long = direction == "LONG"
    # For options-traded S5 we map LONG → CALL, SHORT → PUT for visual parity
    if strat == "S5":
        dir_label = "CALL" if dir_long else "PUT"
    else:
        dir_label = "LONG" if dir_long else "SHORT"

    stage = score.get("stage", "PASS")
    conf = score.get("confidence", 0)
    n = score.get("total_trades", 0)
    wins = score.get("total_wins", 0)
    win_pct = score.get("win_pct", 0)
    total_atr = score.get("total_atr", 0)
    cap = _cap_bucket(ticker)

    # Action badge styling per stage
    action_styles = {
        "TRADE_NOW": ("⚡", "background:#fff7ed;color:#9a3412;border:1px solid #fb923c;font-weight:800;"),
        "FORMING":   ("🌿", "background:#dbeafe;color:#1e3a8a;border:1px solid #93c5fd;"),
        "WAIT":      ("⏸",  "background:#fef3c7;color:#854d0e;border:1px solid #fbbf24;"),
        "PASS":      ("",   "background:#e8e6dc;color:#6b6a65;"),
    }
    aemoji, astyle = action_styles.get(stage, ("", ""))
    action_label = stage.replace("_", " ")

    # Direction pill (gradient — bull/bear)
    if dir_long:
        dir_grad = (f"background:linear-gradient(135deg,{BRAND['bull']} 0%,#4d9b75 50%,#5fad84 100%);"
                    f"color:white;border:1px solid #2d6249;")
        dir_emoji = "🐂"
    else:
        dir_grad = (f"background:linear-gradient(135deg,{BRAND['bear']} 0%,#d04638 50%,#e35344 100%);"
                    f"color:white;border:1px solid #8d2920;")
        dir_emoji = "🐻"

    # Cap bucket pill colors
    bucket_colors = {
        "MEGA": ("#fef3c7", "#78350f"),
        "MID":  ("#e0e7ff", "#3730a3"),
        "SMALL": ("#dcfce7", "#14532d"),
        "ETF":  (BRAND["blue_soft"], "#2b5b8c"),
    }
    bcol_bg, bcol_fg = bucket_colors.get(cap, (BRAND["hair"], BRAND["ink"]))

    # Held badge if Alpaca position exists for this ticker
    held_badge = ""
    if position:
        plpc = position.get("unrealized_plpc", 0) * 100
        cls = ("background:#e7ece0;color:#3d7c5e" if plpc >= 0
               else "background:#f9e0dd;color:#b03528")
        sign = "+" if plpc >= 0 else ""
        held_badge = (f'<span style="margin-left:auto;font-family:Poppins,Arial,sans-serif;'
                      f'font-size:10px;font-weight:600;padding:3px 8px;border-radius:999px;'
                      f'{cls}">held {sign}{plpc:.1f}%</span>')

    # Confidence bar
    bar_color = BRAND["accent"] if conf >= 50 else BRAND["mid"]
    conf_html = f"""
<div class="conf-row">
  <span class="conf-label">Conviction</span>
  <div class="conf-bar"><span style="width:{conf}%;background:{bar_color}"></span></div>
  <span class="conf-val">{conf}</span>
</div>"""

    # Backtest signals fired
    signals = score.get("signals_fired") or []
    signals_html = ""
    if signals:
        chips = "".join(
            f'<span class="sig" title="{_e(s)}">✓ {_e(s)}</span>'
            for s in signals[:6]
        )
        signals_html = f'<div class="signals">{chips}</div>'

    # Stats meta line
    px_str = f"${last_close:,.2f}" if last_close else "—"
    atr_str = f"${atr:.2f}" if atr else "—"
    sharpe_str = ""  # per-strategy sharpe handled at strategy_summary level
    meta = (f'<div class="meta-line">{px_str}'
            f' · ATR {atr_str}'
            f' · backtest <b>{n}</b> trades · '
            f'<b>{wins}/{n}</b> wins ({win_pct:.0f}%)'
            f' · cumulative <b>{total_atr:+.2f}</b> ATR'
            f'</div>')

    # Buy / plan line
    if strat in ("S2", "S3", "S5") and stage != "PASS":
        plan = _entry_plan_for(strat, ticker, direction, last_close, atr)
        buy_line = f'<div class="buy">{_e(plan)}</div>'
    else:
        buy_line = ""

    # Strategy badge
    strat_colors = {
        "S2": ("#fceee7", "#9a3412"),
        "S3": ("#dbeafe", "#1e3a8a"),
        "S5": ("#dcfce7", "#14532d"),
    }
    sc_bg, sc_fg = strat_colors.get(strat, (BRAND["hair"], BRAND["dim"]))

    held_class = ' card-held' if position else ''
    return f"""
<div class="card{held_class}" data-ticker="{_e(ticker)}" data-stage="{stage}"
     data-strategy="{_e(strat)}" data-direction="{dir_label}" data-cap="{cap}">
  <div class="card-head">
    <span class="tick">{_e(ticker)}</span>
    {f'<span style="font-family:Poppins,Arial,sans-serif;font-size:9px;font-weight:700;letter-spacing:0.06em;padding:3px 8px;border-radius:4px;{astyle}">{aemoji} {action_label}</span>' if astyle else ''}
    <span class="strat-pill" style="background:{sc_bg};color:{sc_fg}">{_e(strat)}</span>
    <span class="dir" style="{dir_grad}">{dir_emoji} {dir_label}</span>
    <span class="bucket" style="background:{bcol_bg};color:{bcol_fg}">{_e(cap)}</span>
    {held_badge}
  </div>
  {conf_html}
  {meta}
  {signals_html}
  {buy_line}
</div>"""


# ─── Top-level renderer ───────────────────────────────────────────────────────


def render_conviction_dashboard(ctx: dict) -> str:
    """ctx keys:
        per_ticker        — dict[ticker, dict[strategy, {n, wins, pnl, ...}]]
        meta              — backtest meta dict (start/end/strategies/tickers)
        today_decisions   — list[dict] from decision_log filtered to today
        positions         — list[dict] of Alpaca open positions
        last_closes       — dict[ticker, float] last close from a recent bar (optional)
        atrs              — dict[ticker, float] daily ATR (optional)
        per_strategy      — dict[strategy, {n_trades, win_pct, sharpe, ...}]
        generated_utc     — string timestamp
        trading_mode      — 'paper' / 'live'
    """
    per_ticker = ctx.get("per_ticker") or {}
    meta = ctx.get("meta") or {}
    today_decisions = ctx.get("today_decisions") or []
    positions_list = ctx.get("positions") or []
    last_closes = ctx.get("last_closes") or {}
    atrs = ctx.get("atrs") or {}
    per_strategy = ctx.get("per_strategy") or {}
    generated = ctx.get("generated_utc") or (
        datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z"
    )
    mode = ctx.get("trading_mode") or "paper"

    # Map positions list → ticker lookup. Alpaca returns OCC option symbols
    # like "AAPL260508C00277500"; extract the underlying ticker.
    import re as _re
    def _underlying(sym: str) -> str:
        # OCC: <ROOT><YYMMDD><C|P><STRIKE_8>
        m = _re.match(r"^([A-Z]{1,6})\d{6}[CP]\d{8}$", sym or "")
        return m.group(1) if m else (sym or "")

    pos_by_ticker: dict[str, dict] = {}
    for p in positions_list:
        u = _underlying(p["symbol"])
        # Aggregate multiple legs on same underlying — sum unrealized P&L
        if u in pos_by_ticker:
            existing = pos_by_ticker[u]
            existing["unrealized_pl"] = existing.get("unrealized_pl", 0) + p.get("unrealized_pl", 0)
            existing["market_value"]  = existing.get("market_value", 0) + p.get("market_value", 0)
            mv = existing["market_value"]
            if mv:
                existing["unrealized_plpc"] = existing["unrealized_pl"] / abs(mv)
            existing["leg_count"] = existing.get("leg_count", 1) + 1
        else:
            pos_by_ticker[u] = {**p, "leg_count": 1}
    # Map today's TRADE decisions → ticker lookup for stage assignment
    decision_tickers_with_trade = {
        d.get("ticker") for d in today_decisions if d.get("decision") == "TRADE"
    }

    # Score every ticker the meta says is in the universe (so empty-edge tickers
    # still appear as PASS for completeness).
    universe = sorted(set(meta.get("tickers", []) or list(per_ticker.keys())))

    cards = []
    rollup_counts = {"TRADE_NOW": 0, "FORMING": 0, "WAIT": 0, "PASS": 0}
    rollup_dirs = {"LONG": 0, "SHORT": 0}

    scored = []
    for ticker in universe:
        by_strat = per_ticker.get(ticker, {})
        # Inject last_direction per strategy from raw trades if available.
        # Caller pre-attaches "last_direction" if they have it; otherwise default 'long'.
        score = _score_ticker(
            by_strategy=by_strat,
            today_decisions=[
                {**d, "ticker_for_decisions": ticker} for d in today_decisions
                if d.get("ticker") == ticker
            ],
            recent_alpaca_position=ticker in pos_by_ticker,
        )
        if ticker in decision_tickers_with_trade:
            score["stage"] = "TRADE_NOW"
        scored.append((ticker, score))

    # Sort: TRADE_NOW > FORMING > WAIT > PASS, then by confidence desc
    stage_order = {"TRADE_NOW": 0, "FORMING": 1, "WAIT": 2, "PASS": 3}
    scored.sort(key=lambda r: (stage_order.get(r[1]["stage"], 9),
                                 -r[1]["confidence"]))

    for ticker, score in scored:
        rollup_counts[score["stage"]] = rollup_counts.get(score["stage"], 0) + 1
        if score.get("best_direction"):
            d = "LONG" if score["best_direction"].lower() == "long" else "SHORT"
            rollup_dirs[d] = rollup_dirs.get(d, 0) + 1
        cards.append(_render_card(
            ticker=ticker, score=score,
            last_close=last_closes.get(ticker),
            atr=atrs.get(ticker),
            position=pos_by_ticker.get(ticker),
        ))

    # Strategy stat pills (top of page)
    strat_pills = []
    for s in ("S2", "S3", "S5"):
        m = per_strategy.get(s) or {}
        n = m.get("n_trades", 0)
        wp = m.get("win_pct", 0)
        sh = m.get("sharpe", 0)
        if n == 0:
            label = f"{s}: no trades"
            cls = "off"
        elif wp >= 60 and sh > 1.0:
            label = f"{s}: {wp:.0f}% on {n} · Sharpe {sh:+.1f}"
            cls = "on"
        else:
            label = f"{s}: {wp:.0f}% on {n}"
            cls = "off"
        strat_pills.append(f'<span class="pill {cls}">{label}</span>')

    window = f'{meta.get("start","?")} → {meta.get("end","?")}'

    # CSS — copied/adapted from engine_v4 conviction.html, scoped to scalp 2.
    css = f"""
:root {{
  --ink: {BRAND['ink']}; --bg: {BRAND['bg']}; --panel: {BRAND['panel']};
  --hair: {BRAND['hair']}; --dim: {BRAND['dim']}; --mid: {BRAND['mid']};
  --accent: {BRAND['accent']}; --accent-soft: {BRAND['accent_soft']};
  --blue: {BRAND['blue']}; --blue-soft: {BRAND['blue_soft']};
  --green: {BRAND['green']}; --green-soft: {BRAND['green_soft']};
  --bull: {BRAND['bull']}; --bear: {BRAND['bear']};
}}
* {{ box-sizing: border-box; -webkit-tap-highlight-color: transparent; }}
html, body {{ margin: 0; padding: 0; }}
body {{
  background: var(--bg); color: var(--ink);
  font: 15px/1.5 'Lora', Georgia, Cambria, 'Times New Roman', serif;
  -webkit-font-smoothing: antialiased;
}}
h1, h2, h3, .poppins {{
  font-family: 'Poppins', -apple-system, BlinkMacSystemFont, Arial, sans-serif;
  letter-spacing: -0.01em;
}}
header {{ padding: 18px 16px 12px; max-width: 960px; margin: 0 auto; }}
h1 {{ margin: 0 0 6px; font-size: 22px; font-weight: 600; letter-spacing: -0.02em; }}
.sub-head {{ color: var(--dim); font-size: 13px; }}
.statusline {{ display: flex; gap: 6px; margin-top: 10px; flex-wrap: wrap; }}

.topnav {{
  position: sticky; top: 0; z-index: 20;
  display: flex; gap: 4px; padding: 10px 14px;
  background: var(--bg); border-bottom: 1px solid var(--hair);
  overflow-x: auto; white-space: nowrap;
}}
.topnav a {{
  font-family: 'Poppins', Arial, sans-serif;
  display: inline-flex; align-items: center; gap: 6px;
  padding: 8px 14px; font-size: 13px; font-weight: 500;
  color: var(--dim); text-decoration: none;
  border-radius: 999px; border: 1px solid transparent;
}}
.topnav a:hover {{ color: var(--ink); background: var(--hair); }}
.topnav a.active {{
  color: var(--ink); background: var(--accent-soft);
  border-color: rgba(217,119,87,0.3);
}}
.topnav a .sub {{ font-size: 10px; color: var(--mid); font-weight: 400; }}
.topnav a.active .sub {{ color: var(--accent); }}

.pill {{
  font-family: 'Poppins', Arial, sans-serif;
  padding: 4px 10px; border-radius: 999px;
  background: var(--panel); border: 1px solid var(--hair);
  font-size: 11px; font-weight: 500; color: var(--dim);
}}
.pill.on {{ color: var(--bull); background: var(--green-soft); border-color: rgba(120,140,93,0.3); }}
.pill.off {{ color: var(--mid); }}

main {{ padding: 0 14px 24px; max-width: 960px; margin: 0 auto; }}

/* Filter chips */
.filterbar {{ display: flex; gap: 6px; flex-wrap: wrap; margin: 10px 0 14px; }}
.filterbar .group {{
  display: flex; gap: 4px; align-items: center; padding: 4px 10px;
  background: var(--panel); border: 1px solid var(--hair); border-radius: 999px;
}}
.filterbar .group .lbl {{
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 10px; color: var(--mid); text-transform: uppercase;
  letter-spacing: 0.08em; margin-right: 4px;
}}
.fchip {{
  font-family: 'Poppins', Arial, sans-serif;
  padding: 5px 11px; border-radius: 999px; cursor: pointer; user-select: none;
  background: transparent; border: none; font-size: 12px; font-weight: 500;
  color: var(--dim); display: inline-flex; align-items: center; gap: 5px;
}}
.fchip:hover {{ color: var(--ink); }}
.fchip.active {{ background: var(--accent-soft); color: var(--accent); }}
.fchip .n {{
  font-variant-numeric: tabular-nums; font-weight: 600;
  color: var(--ink); background: var(--hair);
  padding: 1px 7px; border-radius: 999px; font-size: 10px;
}}
.fchip.active .n {{ color: var(--accent); background: #fff; }}

/* Cards */
.feed {{ display: flex; flex-direction: column; gap: 10px; }}
.card {{
  background: var(--panel); border: 1px solid var(--hair);
  border-radius: 14px; padding: 14px 16px;
  transition: border-color .15s;
}}
.card:hover {{ border-color: var(--mid); }}
.card-held {{
  border-color: rgba(217,119,87,0.35);
  background: linear-gradient(180deg, #fffaf6 0%, var(--panel) 100%);
}}
.card.hidden {{ display: none; }}

.card-head {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
.card-head .tick {{
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 22px; font-weight: 700; color: var(--ink);
}}
.dir {{
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 13px; font-weight: 800; padding: 5px 12px; border-radius: 6px;
  letter-spacing: 0.04em;
  box-shadow: 0 2px 6px rgba(0,0,0,0.08);
  text-shadow: 0 1px 1px rgba(0,0,0,0.15);
  position: relative; overflow: hidden;
}}
.dir::after {{
  content: ''; position: absolute; top: 0; left: -100%; width: 50%; height: 100%;
  background: linear-gradient(110deg, transparent 0%, rgba(255,255,255,0.30) 50%, transparent 100%);
  animation: pill-shimmer 4.5s ease-in-out infinite;
  pointer-events: none;
}}
@keyframes pill-shimmer {{ 0%, 60% {{ left: -100%; }} 100% {{ left: 200%; }} }}

.strat-pill {{
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 10px; font-weight: 700; letter-spacing: 0.06em;
  padding: 3px 9px; border-radius: 4px;
}}
.bucket {{
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 10px; font-weight: 600; letter-spacing: 0.06em;
  padding: 3px 8px; border-radius: 4px;
}}

.conf-row {{ display: flex; align-items: center; gap: 10px; margin-top: 10px; }}
.conf-label {{
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 11px; color: var(--dim); min-width: 72px;
}}
.conf-bar {{
  flex: 1; height: 6px; background: var(--hair);
  border-radius: 999px; overflow: hidden;
}}
.conf-bar > span {{ display: block; height: 100%; border-radius: 999px; }}
.conf-val {{
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 12px; font-weight: 600; color: var(--ink);
  font-variant-numeric: tabular-nums; min-width: 40px; text-align: right;
}}

.signals {{ margin-top: 10px; display: flex; gap: 6px; flex-wrap: wrap; }}
.sig {{
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 11px; padding: 3px 8px; border-radius: 4px;
  background: var(--green-soft); color: var(--bull);
}}

.meta-line {{
  margin-top: 6px; font-family: 'Poppins', Arial, sans-serif;
  color: var(--dim); font-size: 11px; font-variant-numeric: tabular-nums;
}}
.meta-line b {{ color: var(--ink); font-weight: 600; }}

.buy {{
  margin-top: 12px; padding: 10px 12px;
  background: var(--accent-soft);
  border: 1px solid rgba(217,119,87,0.25);
  border-radius: 8px;
  font-family: 'Poppins', Arial, sans-serif;
  color: var(--accent); font-size: 13px; font-weight: 500;
  font-variant-numeric: tabular-nums;
}}

footer {{
  color: var(--mid); font-size: 11px; text-align: center;
  padding: 24px 16px; max-width: 960px; margin: 0 auto;
}}

@media (max-width: 480px) {{
  body {{ font-size: 14px; }}
  header {{ padding: 14px 12px 10px; }}
  h1 {{ font-size: 19px; }}
  main {{ padding: 0 10px 20px; }}
  .card {{ padding: 12px 14px; border-radius: 12px; }}
  .card-head {{ gap: 8px; }}
  .card-head .tick {{ font-size: 20px; }}
  .buy {{ font-size: 12px; padding: 9px 11px; }}
}}
"""

    # Filter chip groups (counts come from rollup_counts)
    filterbar_html = f"""
<div class="filterbar">
  <div class="group">
    <span class="lbl">stage</span>
    <span class="fchip active" data-filter-stage="ALL">All <span class="n">{len(scored)}</span></span>
    <span class="fchip" data-filter-stage="TRADE_NOW">⚡ Trade now <span class="n">{rollup_counts['TRADE_NOW']}</span></span>
    <span class="fchip" data-filter-stage="FORMING">🌿 Forming <span class="n">{rollup_counts['FORMING']}</span></span>
    <span class="fchip" data-filter-stage="WAIT">⏸ Wait <span class="n">{rollup_counts['WAIT']}</span></span>
    <span class="fchip" data-filter-stage="PASS">— Pass <span class="n">{rollup_counts['PASS']}</span></span>
  </div>
  <div class="group">
    <span class="lbl">side</span>
    <span class="fchip active" data-filter-side="ALL">All</span>
    <span class="fchip" data-filter-side="LONG">🐂 Long <span class="n">{rollup_dirs.get('LONG',0)}</span></span>
    <span class="fchip" data-filter-side="SHORT">🐻 Short <span class="n">{rollup_dirs.get('SHORT',0)}</span></span>
    <span class="fchip" data-filter-side="CALL">🐂 Call</span>
    <span class="fchip" data-filter-side="PUT">🐻 Put</span>
  </div>
  <div class="group">
    <span class="lbl">strategy</span>
    <span class="fchip active" data-filter-strategy="ALL">All</span>
    <span class="fchip" data-filter-strategy="S2">S2 ORB</span>
    <span class="fchip" data-filter-strategy="S3">S3 momentum</span>
    <span class="fchip" data-filter-strategy="S5">S5 gamma</span>
  </div>
</div>
"""

    # Tiny inline JS for filter chips (no framework)
    filter_script = """
<script>
(function() {
  const state = { stage: 'ALL', side: 'ALL', strategy: 'ALL' };
  const cards = Array.from(document.querySelectorAll('.card'));
  const groups = {
    stage: document.querySelectorAll('[data-filter-stage]'),
    side: document.querySelectorAll('[data-filter-side]'),
    strategy: document.querySelectorAll('[data-filter-strategy]'),
  };

  function apply() {
    cards.forEach(c => {
      const stage = c.dataset.stage || 'PASS';
      const dir = c.dataset.direction || '';
      const strat = c.dataset.strategy || '';
      let show = true;
      if (state.stage !== 'ALL' && stage !== state.stage) show = false;
      if (state.side !== 'ALL') {
        // 'CALL' filter shows only S5 LONG cards (which display CALL pill).
        // 'PUT' filter shows only S5 SHORT cards (PUT pill).
        // 'LONG' / 'SHORT' apply across S2/S3/S5.
        if (state.side === 'LONG' && dir !== 'LONG' && dir !== 'CALL') show = false;
        else if (state.side === 'SHORT' && dir !== 'SHORT' && dir !== 'PUT') show = false;
        else if (state.side === 'CALL' && dir !== 'CALL') show = false;
        else if (state.side === 'PUT' && dir !== 'PUT') show = false;
      }
      if (state.strategy !== 'ALL' && strat !== state.strategy) show = false;
      c.classList.toggle('hidden', !show);
    });
  }

  function wire(group, key) {
    group.forEach(el => el.addEventListener('click', () => {
      group.forEach(x => x.classList.remove('active'));
      el.classList.add('active');
      state[key] = el.dataset['filter' + key.charAt(0).toUpperCase() + key.slice(1)];
      apply();
    }));
  }
  wire(groups.stage, 'stage');
  wire(groups.side, 'side');
  wire(groups.strategy, 'strategy');
})();
</script>
"""

    # Compose
    body = f"""
<nav class="topnav">
  <a href="trading_dashboard.html"><span>📊 Dashboard</span><span class="sub">positions + backtest</span></a>
  <a href="conviction.html" class="active"><span>👁 Conviction</span><span class="sub">tomorrow's plan</span></a>
</nav>

<header>
  <h1>Conviction · {_e(window)}</h1>
  <div class="sub-head">
    Per-ticker conviction for tomorrow's session. Ranked by backtest edge,
    filtered by strategy. Click a strategy chip below to focus.
  </div>
  <div class="statusline">
    <span class="pill">{_e(generated[:19])}</span>
    <span class="pill {('on' if mode == 'paper' else 'off')}">{_e(mode.upper())} mode</span>
    {''.join(strat_pills)}
  </div>
</header>

<main>
  {filterbar_html}
  <div class="feed">
    {''.join(cards)}
  </div>
</main>

<footer>
  scalp 2 · conviction · last 12 backtest days · stage = TRADE_NOW &gt; FORMING &gt; WAIT &gt; PASS
</footer>

{filter_script}
"""

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="{BRAND['bg']}">
  <title>scalp 2 — Conviction</title>
  <link rel="preconnect" href="https://fonts.googleapis.com">
  <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
  <link href="https://fonts.googleapis.com/css2?family=Lora:wght@400;500;600&family=Poppins:wght@400;500;600;700&display=swap" rel="stylesheet">
  <style>{css}</style>
</head>
<body>{body}</body>
</html>"""


# ─── Helper: load latest backtest with per-direction info ────────────────────


def gather_conviction_data(bt_path: Path) -> tuple[dict, dict, dict]:
    """Returns (per_ticker_with_directions, per_strategy, meta).

    per_ticker_with_directions: {ticker: {strategy: {n, wins, pnl, last_direction, signals_fired}}}
    """
    if not bt_path.exists():
        return {}, {}, {}
    raw = json.loads(bt_path.read_text())
    per_strategy = raw.get("per_strategy", {})
    trades = raw.get("trades", [])
    meta = raw.get("meta", {})

    by_ticker: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(
        lambda: {"n": 0, "wins": 0, "pnl": 0.0, "last_direction": "long",
                  "last_timestamp": "", "signals_fired": set()}
    ))
    for t in trades:
        d = by_ticker[t["ticker"]][t["strategy"]]
        d["n"] += 1
        d["wins"] += int(t.get("is_win", False))
        d["pnl"] += t.get("pnl_atr", 0.0)
        # Most-recent direction wins (sort by timestamp_iso later)
        ts = t.get("timestamp_iso", "")
        if ts > d.get("last_timestamp", ""):
            d["last_direction"] = t.get("direction", "long")
            d["last_timestamp"] = ts

    # Add per-strategy "signals fired" — synthesize from gates that passed.
    # For strategies that traded, the gates by name are the strategy's setup
    # criteria. This is a static set per strategy (no per-trade granularity in
    # the JSON yet); keep it short + descriptive.
    static_signals = {
        "S2": ["OR break", "vol≥1.5×", "ignition", "passed allowlist"],
        "S3": ["session-trend", "VWAP-dist≥0.75 ATR", "structure-clean"],
        "S5": ["near GEX wall", "extended ≥1.5 ATR", "reversal score"],
    }
    out_by_ticker: dict[str, dict[str, dict]] = {}
    for tk, sd in by_ticker.items():
        out_by_ticker[tk] = {}
        for s, d in sd.items():
            out_by_ticker[tk][s] = {
                "n": d["n"], "wins": d["wins"], "pnl": d["pnl"],
                "last_direction": d["last_direction"],
                "signals_fired": static_signals.get(s, []),
            }

    return out_by_ticker, per_strategy, meta
