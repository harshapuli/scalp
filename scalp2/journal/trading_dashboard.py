"""journal/trading_dashboard.py — Trader-focused dashboard renderer.

Distinct from the engineer-status dashboard (journal/dashboard.py). This one is
for actually deciding what to trade today:

  1. Strategy edge summary (from backtest)
  2. Per-ticker recommendations (mute / allow per strategy)
  3. Today's signal candidates (live decisions from daemon)
  4. Open positions + P&L
  5. Action queue: ranked tickers to watch by strategy
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import asdict
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


def render_trading_dashboard(ctx: dict) -> str:
    bt = ctx.get("backtest_summary") or {}
    per_strategy = bt.get("per_strategy", {})
    per_ticker = ctx.get("per_ticker_breakdown") or {}
    today_decisions = ctx.get("today_decisions") or []
    positions = ctx.get("alpaca_positions") or []
    account = ctx.get("alpaca_account") or {}
    universe_rules = ctx.get("universe_rules") or {}

    generated = ctx.get("generated_utc",
                          datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z")
    bt_window = bt.get("window", "—")

    # ── 1. Strategy edge cards ────────────────────────────────────────
    cards = []
    for s, m in per_strategy.items():
        win_pct = m.get("win_pct", 0)
        avg_pnl = m.get("avg_pnl_atr", 0)
        sharpe = m.get("sharpe", 0)
        n = m.get("n_trades", 0)

        if n == 0:
            verdict = "NO DATA"
            color_class = "zero"
        elif sharpe > 2 and win_pct > 60:
            verdict = "TRADE"
            color_class = "pos"
        elif sharpe > 0:
            verdict = "MAYBE"
            color_class = "zero"
        else:
            verdict = "MUTE"
            color_class = "neg"

        cards.append(f"""
<div class="strategy-card">
  <div class="strategy-header">
    <span class="strategy-name">{_e(s)}</span>
    <span class="badge {color_class}">{verdict}</span>
  </div>
  <div class="strategy-metrics">
    <div><span class="label">trades</span><span class="value">{n}</span></div>
    <div><span class="label">win %</span><span class="value">{win_pct:.1f}%</span></div>
    <div><span class="label">avg PnL</span><span class="value {color_class}">{avg_pnl:+.2f} ATR</span></div>
    <div><span class="label">Sharpe (ann)</span><span class="value {color_class}">{sharpe:+.2f}</span></div>
  </div>
</div>""")

    # ── 2. Per-ticker recommendations ─────────────────────────────────
    ticker_rows = []
    all_tickers = sorted(per_ticker.keys())
    for tk in all_tickers:
        per = per_ticker[tk]
        s2 = per.get("S2", {"n": 0, "wins": 0, "pnl": 0})
        s3 = per.get("S3", {"n": 0, "wins": 0, "pnl": 0})

        # Determine recommendation per strategy
        s2_allowed = (
            tk in (universe_rules.get("s2") or [])
            or (s2["n"] > 0 and s2["wins"] / s2["n"] >= 0.50)
        )
        s2_label = "✅ TRADE" if s2_allowed else "🔇 MUTE"
        s3_allowed = (
            (universe_rules.get("s3") is None)
            or tk in (universe_rules.get("s3") or [])
        )
        s3_label = "✅ TRADE" if s3_allowed else "🔇 MUTE"

        s2_str = (
            f'{s2["n"]} ({(100*s2["wins"]/s2["n"]) if s2["n"] else 0:.0f}%) '
            f'{s2["pnl"]/max(1,s2["n"]):+.2f}'
            if s2["n"] else "—"
        )
        s3_str = (
            f'{s3["n"]} ({(100*s3["wins"]/s3["n"]) if s3["n"] else 0:.0f}%) '
            f'{s3["pnl"]/max(1,s3["n"]):+.2f}'
            if s3["n"] else "—"
        )

        ticker_rows.append(f"""<tr>
  <td><b>{_e(tk)}</b></td>
  <td>{s2_label}</td>
  <td class="num small">{s2_str}</td>
  <td>{s3_label}</td>
  <td class="num small">{s3_str}</td>
</tr>""")

    # ── 3. Today's signals ────────────────────────────────────────────
    decisions_html = ""
    if today_decisions:
        decision_rows = []
        for d in today_decisions[:30]:
            cls = "pos" if d.get("decision") == "TRADE" else "neg"
            ts = (d.get("timestamp") or "")[:19].replace("T", " ")
            decision_rows.append(f"""<tr>
  <td class="small">{_e(ts)}</td>
  <td>{_e(d.get("strategy"))}</td>
  <td><b>{_e(d.get("ticker"))}</b></td>
  <td><span class="badge {cls}">{_e(d.get("decision"))}</span></td>
  <td>{_e(d.get("pass_reason") or "")}</td>
</tr>""")
        decisions_html = f"""
<table>
  <thead><tr><th>UTC</th><th>Strat</th><th>Ticker</th><th>Decision</th><th>Pass reason</th></tr></thead>
  <tbody>{"".join(decision_rows)}</tbody>
</table>"""
    else:
        decisions_html = f"""
<div class="note">
  <b>No live signals yet today.</b> Start the daemon during market hours:
  <code>python3 scripts/main.py --tickers SPY,QQQ,NVDA,...</code>
  Decisions log to <code>data/dev_journal.db</code> and surface here on next refresh.
</div>"""

    # ── 4. Account + positions ────────────────────────────────────────
    pos_html = ""
    if positions:
        total_pl = sum(p.get("unrealized_pl", 0) for p in positions)
        pl_class = "pos" if total_pl >= 0 else "neg"
        pos_rows = []
        for p in positions[:30]:
            cls = "pos" if p.get("unrealized_pl", 0) >= 0 else "neg"
            pos_rows.append(f"""<tr>
  <td><code>{_e(p.get("symbol"))}</code></td>
  <td class="num">{p.get("qty")}</td>
  <td class="num small">${p.get("avg_entry_price", 0):,.2f}</td>
  <td class="num small">${p.get("current_price", 0):,.2f}</td>
  <td class="num small">${p.get("market_value", 0):,.0f}</td>
  <td class="num {cls}">${p.get("unrealized_pl", 0):+,.0f} ({p.get("unrealized_plpc", 0)*100:+.1f}%)</td>
</tr>""")
        pos_html = f"""
<h2>Open positions ({len(positions)}) — net unrealized: <span class="{pl_class}">${total_pl:+,.0f}</span></h2>
<table>
  <thead><tr><th>Symbol</th><th class="num">Qty</th><th class="num">Avg entry</th><th class="num">Current</th><th class="num">Mkt value</th><th class="num">Unrealized P&L</th></tr></thead>
  <tbody>{"".join(pos_rows)}</tbody>
</table>"""

    # ── CSS + page ─────────────────────────────────────────────────────
    css = f"""
    body {{
      font-family: 'Lora', Georgia, serif;
      color: {BRAND['dark']};
      background: {BRAND['light']};
      margin: 0;
      padding: 28px 36px;
      max-width: 1320px;
      margin-left: auto;
      margin-right: auto;
    }}
    h1, h2, h3 {{ font-family: 'Poppins', Arial, sans-serif; font-weight: 600; }}
    h1 {{ font-size: 26px; color: {BRAND['orange']}; margin-bottom: 0; }}
    h2 {{ font-size: 18px; margin-top: 32px; border-bottom: 1px solid {BRAND['light_gray']};
          padding-bottom: 6px; }}
    .header-sub {{ color: {BRAND['mid_gray']}; font-size: 13px; margin-bottom: 14px; }}
    code {{ background: {BRAND['light_gray']}; padding: 1px 6px; border-radius: 3px;
            font-family: 'SF Mono', Menlo, Monaco, monospace; font-size: 12px; }}

    /* Strategy cards */
    .strategy-row {{ display: flex; gap: 14px; flex-wrap: wrap; margin: 14px 0; }}
    .strategy-card {{
      background: white; border: 1px solid {BRAND['light_gray']};
      border-radius: 8px; padding: 16px 20px; flex: 1; min-width: 240px;
    }}
    .strategy-header {{ display: flex; justify-content: space-between; align-items: center;
                        margin-bottom: 12px; }}
    .strategy-name {{ font-family: 'Poppins', Arial, sans-serif; font-weight: 600;
                      font-size: 18px; color: {BRAND['dark']}; }}
    .strategy-metrics {{ display: grid; grid-template-columns: 1fr 1fr; gap: 8px 14px; }}
    .strategy-metrics .label {{ font-size: 11px; color: {BRAND['mid_gray']};
                                 text-transform: uppercase; letter-spacing: 0.04em;
                                 display: block; }}
    .strategy-metrics .value {{ font-family: 'Poppins', Arial, sans-serif;
                                 font-weight: 600; font-size: 16px; display: block; }}
    .pos {{ color: {BRAND['green']}; }}
    .neg {{ color: {BRAND['red']}; }}
    .zero {{ color: {BRAND['mid_gray']}; }}

    .badge {{ display: inline-block; padding: 3px 10px; border-radius: 3px;
              font-size: 10px; font-family: 'Poppins', Arial, sans-serif;
              font-weight: 600; letter-spacing: 0.06em; }}
    .badge.pos {{ background: {BRAND['green']}; color: white; }}
    .badge.neg {{ background: {BRAND['red']}; color: white; }}
    .badge.zero {{ background: {BRAND['light_gray']}; color: {BRAND['dark']}; }}

    /* Tables */
    table {{ width: 100%; border-collapse: collapse; margin: 12px 0;
              background: white; border: 1px solid {BRAND['light_gray']};
              border-radius: 6px; overflow: hidden; }}
    th, td {{ padding: 7px 11px; text-align: left;
              border-bottom: 1px solid {BRAND['light_gray']}; font-size: 13px; }}
    th {{ background: {BRAND['light_gray']}; font-family: 'Poppins', Arial, sans-serif;
          font-weight: 600; text-transform: uppercase; letter-spacing: 0.03em;
          font-size: 11px; color: {BRAND['dark']}; }}
    td.num {{ text-align: right; font-variant-numeric: tabular-nums; }}
    th.num {{ text-align: right; }}
    .small {{ font-size: 11px; color: {BRAND['mid_gray']}; }}

    .note {{ border-left: 4px solid {BRAND['blue']}; background: {BRAND['light_gray']};
              padding: 12px 16px; border-radius: 0 4px 4px 0; margin: 12px 0; font-size: 13px; }}
    """

    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>scalp 2 — trader dashboard</title>
  <link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;600&family=Lora&display=swap" rel="stylesheet">
  <style>{css}</style>
</head>
<body>
  <h1>scalp 2 — trader dashboard</h1>
  <div class="header-sub">
    Generated {_e(generated)} · backtest window {_e(bt_window)} ·
    branch <code>dev/harsha/slaudesadvstartegy</code>
  </div>

  <h2>Strategy edge — backtest verdict</h2>
  <div class="strategy-row">{"".join(cards)}</div>

  <h2>Account: ${account.get("equity", 0):,.0f} equity, ${account.get("buying_power", 0):,.0f} BP, daytrade count {account.get("daytrade_count", 0)}</h2>

  {pos_html}

  <h2>Per-ticker recommendations ({len(all_tickers)} tickers in backtest)</h2>
  <table>
    <thead>
      <tr>
        <th>Ticker</th>
        <th>S2 status</th>
        <th class="num">S2 history n (win%) avg ATR</th>
        <th>S3 status</th>
        <th class="num">S3 history n (win%) avg ATR</th>
      </tr>
    </thead>
    <tbody>{"".join(ticker_rows)}</tbody>
  </table>

  <h2>Today's live signals</h2>
  {decisions_html}
</body>
</html>"""


def gather_backtest_summary(bt_path: Path) -> tuple[dict, dict]:
    """Returns (per_strategy_summary, per_ticker_breakdown)."""
    if not bt_path.exists():
        return {}, {}
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
    }, dict(per_ticker)
