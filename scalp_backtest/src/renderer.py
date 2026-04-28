"""
renderer.py — emit the unified HTML dashboard.

Design contract: DESIGN.md §13 (UI) and brand-guidelines skill.

Page layout: 5 tabs
  OVERVIEW       — top-line numbers + ML readiness summary
  SCALP          — kind table (3-band conf), label_underlying vs label_option_5p5x,
                   STOP-first / TARGET-first dual columns, n<5 suppressed
  SWING          — fire ledger from shadow logger; outcomes pending until daily-bars
  ML READINESS   — per-kind days × fires gate, READY / need days / need fires
  ARCHIVE        — links into output/archive/* (engine_v4 + swing_engine_v2 dashboards)

Brand:
  dark #141413 • light #faf9f5 • orange #d97757 • blue #6a9bcc • green #788c5d
  Poppins headings (Arial fallback), Lora body (Georgia fallback)
"""
from __future__ import annotations

import html
import json
import shutil
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Paths
# ──────────────────────────────────────────────────────────────────────────────

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_ROOT / "output"
ARCHIVE_ROOT = OUTPUT_ROOT / "archive"

ENGINE_V4_DASH = Path("/Users/harshapuli/Downloads/spy QQQ/claude/gemini /engine_v4/dashboard")
SWING_V2_TEMPLATES = Path("/Users/harshapuli/Downloads/spy QQQ/claude/gemini /swing_engine_v2/templates")


# ──────────────────────────────────────────────────────────────────────────────
# CSS
# ──────────────────────────────────────────────────────────────────────────────

CSS = """
:root {
  --dark: #141413;
  --light: #faf9f5;
  --mid-gray: #b0aea5;
  --light-gray: #e8e6dc;
  --orange: #d97757;
  --blue: #6a9bcc;
  --green: #788c5d;
  --red: #b85c4a;
}
* { box-sizing: border-box; }
html, body {
  margin: 0;
  padding: 0;
  background: var(--light);
  color: var(--dark);
  font-family: 'Lora', Georgia, serif;
  font-size: 15px;
  line-height: 1.55;
}
h1, h2, h3, h4, .tab-bar, .stat-card .label, .badge {
  font-family: 'Poppins', Arial, sans-serif;
  letter-spacing: 0.01em;
}
h1 { font-size: 32px; margin: 0 0 8px 0; font-weight: 600; }
h2 { font-size: 22px; margin: 32px 0 12px 0; font-weight: 600; border-bottom: 1px solid var(--light-gray); padding-bottom: 4px; }
h3 { font-size: 17px; margin: 20px 0 8px 0; font-weight: 500; color: #2a2a28; }

.container {
  max-width: 1280px;
  margin: 0 auto;
  padding: 24px 32px;
}

.header {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  border-bottom: 2px solid var(--dark);
  padding-bottom: 12px;
  margin-bottom: 24px;
}
.header .meta { font-size: 13px; color: #4a4a45; text-align: right; }
.header .meta div { margin-top: 2px; }

.tab-bar {
  display: flex;
  gap: 4px;
  border-bottom: 1px solid var(--light-gray);
  margin-bottom: 24px;
}
.tab-bar a {
  padding: 10px 18px;
  text-decoration: none;
  color: #4a4a45;
  font-weight: 500;
  font-size: 14px;
  border-bottom: 3px solid transparent;
  cursor: pointer;
  transition: color 0.1s, border-bottom-color 0.1s;
}
.tab-bar a:hover { color: var(--orange); }
.tab-bar a.active { color: var(--dark); border-bottom-color: var(--orange); }

.tab { display: none; }
.tab.active { display: block; }

.stat-row {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
  gap: 12px;
  margin-bottom: 16px;
}
.stat-card {
  background: white;
  border: 1px solid var(--light-gray);
  border-radius: 4px;
  padding: 14px 16px;
}
.stat-card .label {
  font-size: 11px;
  text-transform: uppercase;
  color: #6a6a62;
  letter-spacing: 0.06em;
  margin-bottom: 4px;
}
.stat-card .value { font-size: 24px; font-weight: 600; }
.stat-card .sub { font-size: 12px; color: #6a6a62; margin-top: 2px; }

.value.pos { color: var(--green); }
.value.neg { color: var(--red); }
.value.warn { color: var(--orange); }

table {
  width: 100%;
  border-collapse: collapse;
  background: white;
  border: 1px solid var(--light-gray);
  border-radius: 4px;
  overflow: hidden;
  font-size: 13px;
}
th {
  text-align: left;
  padding: 8px 10px;
  background: #f4f2ea;
  font-family: 'Poppins', Arial, sans-serif;
  font-size: 11px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  color: #4a4a45;
  border-bottom: 1px solid var(--light-gray);
}
td {
  padding: 7px 10px;
  border-bottom: 1px solid #f4f2ea;
}
tr:last-child td { border-bottom: none; }
tr:hover { background: #fbfaf3; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
td.kind { font-family: 'Poppins', Arial, sans-serif; font-weight: 500; }
td.suppressed { color: var(--mid-gray); font-style: italic; }
td.pos { color: var(--green); }
td.neg { color: var(--red); }
td.zero { color: #4a4a45; }

.badge {
  display: inline-block;
  padding: 2px 7px;
  border-radius: 3px;
  font-size: 10px;
  text-transform: uppercase;
  letter-spacing: 0.05em;
  font-weight: 500;
}
.badge.ready { background: var(--green); color: white; }
.badge.need-days { background: var(--blue); color: white; }
.badge.need-fires { background: var(--orange); color: white; }
.badge.need-both { background: var(--mid-gray); color: white; }
.badge.pos { background: var(--green); color: white; }
.badge.neg { background: var(--red); color: white; }
.badge.zero { background: var(--light-gray); color: var(--dark); }

.note {
  background: #fef9f4;
  border-left: 3px solid var(--orange);
  padding: 10px 14px;
  margin: 12px 0;
  font-size: 13px;
  color: #4a4a45;
}
.note .title {
  font-family: 'Poppins', Arial, sans-serif;
  font-weight: 600;
  color: var(--dark);
  margin-bottom: 4px;
}

.archive-grid {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 12px;
}
.archive-card {
  background: white;
  border: 1px solid var(--light-gray);
  border-radius: 4px;
  padding: 14px;
  text-decoration: none;
  color: var(--dark);
  transition: border-color 0.1s, transform 0.1s;
}
.archive-card:hover {
  border-color: var(--orange);
  transform: translateY(-1px);
}
.archive-card .src {
  font-size: 11px;
  color: #6a6a62;
  text-transform: uppercase;
  letter-spacing: 0.05em;
}
.archive-card .name {
  font-family: 'Poppins', Arial, sans-serif;
  font-weight: 500;
  font-size: 15px;
  margin-top: 4px;
}

.footer {
  margin-top: 48px;
  padding-top: 12px;
  border-top: 1px solid var(--light-gray);
  font-size: 11px;
  color: #6a6a62;
  text-align: center;
}
"""


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _e(s) -> str:
    if s is None:
        return "—"
    return html.escape(str(s))


def _fmt_pct(v: Optional[float], digits: int = 2) -> str:
    if v is None:
        return "—"
    return f"{v:+.{digits}f}%"


def _fmt_num(v: Optional[float], digits: int = 1) -> str:
    if v is None:
        return "—"
    return f"{v:.{digits}f}"


def _fmt_int(v: Optional[int]) -> str:
    if v is None:
        return "—"
    return str(int(v))


def _ev_class(v: Optional[float]) -> str:
    if v is None:
        return ""
    if v > 0.5:
        return "pos"
    if v < -0.5:
        return "neg"
    return "zero"


def _to_dict(o):
    if is_dataclass(o):
        return asdict(o)
    if hasattr(o, "__dict__"):
        return dict(o.__dict__)
    return o


# ──────────────────────────────────────────────────────────────────────────────
# Section renderers
# ──────────────────────────────────────────────────────────────────────────────


def render_overview(ctx: dict) -> str:
    n_scalp = ctx["n_scalp_outcomes"]
    n_scalp_raw = ctx["n_scalp_raw"]
    n_swing = ctx["n_swing"]
    n_dates = ctx["n_dates_scalp"]
    win_underlying = ctx["overall_win_underlying"]
    win_option = ctx["overall_win_option"]
    ev_under = ctx["overall_ev_under"]
    ev_opt = ctx["overall_ev_opt"]
    ml_ready = ctx["ml_ready_kinds"]
    ml_total = ctx["ml_total_kinds"]
    best = ctx["best_kind"]
    worst = ctx["worst_kind"]

    # Fire-time enrichment counts
    n_flow = ctx.get("n_fire_with_flow", 0)
    n_news = ctx.get("n_fire_with_news", 0)
    n_conv = ctx.get("n_fire_with_conviction", 0)
    n_enriched = ctx.get("n_fire_enriched", 0)
    pct = lambda c: f"{100*c/max(n_scalp,1):.1f}%"

    # Historical archive top-line
    htl = ctx.get("hist_top_line") or {}
    h_n = htl.get("n", 0)
    h_win = htl.get("win_pct")
    h_win_str = "—" if h_win is None else f"{h_win:.1f}%"
    h_ev = htl.get("ev_mean_pct")
    h_ev_str = "—" if h_ev is None else f"{h_ev:+.1f}%"
    h_med = htl.get("ev_median_pct")
    h_med_str = "—" if h_med is None else f"{h_med:+.1f}%"
    h_dates = htl.get("n_dates", 0)
    h_first = htl.get("first_date") or "—"
    h_last = htl.get("last_date") or "—"
    h_strats = htl.get("n_strategies", 0)
    h_tickers = htl.get("n_tickers", 0)

    return f"""
<div class="tab" id="tab-overview">
  <h2>Overview</h2>
  <div class="stat-row">
    <div class="stat-card"><div class="label">Scalp fires (deduped)</div>
      <div class="value">{n_scalp}</div>
      <div class="sub">from {n_scalp_raw} raw signals across {n_dates} day(s)</div>
    </div>
    <div class="stat-card"><div class="label">Swing fires (logged)</div>
      <div class="value">{n_swing}</div>
      <div class="sub">shadow logger snapshot</div>
    </div>
    <div class="stat-card"><div class="label">Win % — underlying</div>
      <div class="value {_ev_class(win_underlying - 50.0 if win_underlying else None)}">{_fmt_num(win_underlying)}%</div>
      <div class="sub">no costs, ATR ladder STOP-first</div>
    </div>
    <div class="stat-card"><div class="label">Win % — option 5.5×</div>
      <div class="value {_ev_class(win_option - 50.0 if win_option else None)}">{_fmt_num(win_option)}%</div>
      <div class="sub">8% RT cost × 5.5× leverage</div>
    </div>
    <div class="stat-card"><div class="label">EV/fire — underlying</div>
      <div class="value {_ev_class(ev_under)}">{_fmt_pct(ev_under)}</div>
      <div class="sub">realized mean</div>
    </div>
    <div class="stat-card"><div class="label">EV/fire — option 5.5×</div>
      <div class="value {_ev_class(ev_opt)}">{_fmt_pct(ev_opt)}</div>
      <div class="sub">realized mean (cost-aware)</div>
    </div>
    <div class="stat-card"><div class="label">ML-ready kinds</div>
      <div class="value">{ml_ready} / {ml_total}</div>
      <div class="sub">≥30 days AND ≥100 fires</div>
    </div>
    <div class="stat-card"><div class="label">Best kind (underlying)</div>
      <div class="value pos">{_e(best['kind']) if best else '—'}</div>
      <div class="sub">{_fmt_num(best['win']) + '% • n=' + str(best['n']) if best else 'n/a'}</div>
    </div>
  </div>

  <h3>Fire-time context (joined to lifecycle snapshot at signal time)</h3>
  <div class="stat-row">
    <div class="stat-card"><div class="label">Enriched</div>
      <div class="value">{n_enriched} / {n_scalp}</div>
      <div class="sub">matched to ≥1 lifecycle snap</div>
    </div>
    <div class="stat-card"><div class="label">With whale flow</div>
      <div class="value">{n_flow}</div>
      <div class="sub">{pct(n_flow)} of fires</div>
    </div>
    <div class="stat-card"><div class="label">With news</div>
      <div class="value">{n_news}</div>
      <div class="sub">{pct(n_news)} of fires</div>
    </div>
    <div class="stat-card"><div class="label">With conviction</div>
      <div class="value">{n_conv}</div>
      <div class="sub">{pct(n_conv)} of fires</div>
    </div>
  </div>

  <h3>Historical archive (engine_scalp · multi-year)</h3>
  <div class="stat-row">
    <div class="stat-card"><div class="label">Total historical fires</div>
      <div class="value">{h_n}</div>
      <div class="sub">{h_strats} strategies · {h_tickers} tickers</div>
    </div>
    <div class="stat-card"><div class="label">Date span</div>
      <div class="value" style="font-size:18px">{_e(h_first)} → {_e(h_last)}</div>
      <div class="sub">{h_dates} trading days</div>
    </div>
    <div class="stat-card"><div class="label">Win % (decided)</div>
      <div class="value">{h_win_str}</div>
      <div class="sub">across all strategies</div>
    </div>
    <div class="stat-card"><div class="label">EV mean / fire (option)</div>
      <div class="value {_ev_class(h_ev)}">{h_ev_str}</div>
      <div class="sub">median {h_med_str} — heavy right tail</div>
    </div>
  </div>

  <div class="note">
    <div class="title">Reading this dashboard</div>
    The headline number is <b>EV per fire</b>, not win%. Win% can be 60% but if the wins are small
    and losses are large, EV is negative — costs (8% RT options × 5.5× leverage) eat thin edges.
    Slices with n &lt; 5 are suppressed. Wilson 95% CI shown alongside win% to prevent over-reading
    small samples. Fire-time context features (whale / news / conviction) are joined from the
    engine_v4 lifecycle snapshot closest to detection time — the same context the engine had at fire.
    The Historical tab adds multi-year coverage from the engine_scalp archive — same kinds of
    momentum / washout / ULTRO triggers we run live, but with realized PnL.
  </div>
</div>
"""


def render_scalp_table(ctx: dict) -> str:
    """Two side-by-side views — underlying (no cost) vs option (5.5×, 8% RT)."""
    rows_u = ctx["scalp_kind_underlying"]    # list of (kind, SliceStat)
    rows_o = ctx["scalp_kind_option"]
    by_kind_under = {k: s for k, s in rows_u}
    by_kind_opt = {k: s for k, s in rows_o}

    all_kinds = sorted(set(by_kind_under) | set(by_kind_opt),
                       key=lambda k: -((by_kind_under.get(k) or by_kind_opt.get(k)).n))

    def cell(stat, field, fmt):
        if stat is None or stat.suppressed:
            return '<td class="num suppressed">—</td>'
        v = getattr(stat, field, None)
        return f'<td class="num">{fmt(v)}</td>'

    def cell_pct(stat, field):
        if stat is None or stat.suppressed:
            return '<td class="num suppressed">—</td>'
        v = getattr(stat, field, None)
        if v is None:
            return '<td class="num">—</td>'
        cls = _ev_class(v)
        return f'<td class="num {cls}">{_fmt_pct(v)}</td>'

    rows_html = []
    for k in all_kinds:
        u = by_kind_under.get(k)
        o = by_kind_opt.get(k)
        n = (u or o).n
        if (u and u.suppressed) and (o and o.suppressed):
            rows_html.append(f"""
<tr>
  <td class="kind">{_e(k)}</td>
  <td class="num">{n}</td>
  <td class="num suppressed" colspan="6">n &lt; 5 — suppressed</td>
</tr>""")
            continue
        ci_u = (f"{u.win_ci_lo:.0f}–{u.win_ci_hi:.0f}"
                if u and not u.suppressed and u.win_ci_lo is not None else "—")
        ci_o = (f"{o.win_ci_lo:.0f}–{o.win_ci_hi:.0f}"
                if o and not o.suppressed and o.win_ci_lo is not None else "—")

        rows_html.append(f"""
<tr>
  <td class="kind">{_e(k)}</td>
  <td class="num">{n}</td>
  <td class="num">{_fmt_num(u.win_pct) if u and not u.suppressed else '—'}%</td>
  <td class="num" style="font-size:11px;color:#6a6a62">{ci_u}</td>
  {cell_pct(u, 'ev_mean_pct')}
  <td class="num">{_fmt_num(o.win_pct) if o and not o.suppressed else '—'}%</td>
  <td class="num" style="font-size:11px;color:#6a6a62">{ci_o}</td>
  {cell_pct(o, 'ev_mean_pct')}
</tr>""")

    return f"""
<div class="tab" id="tab-scalp">
  <h2>Scalp — every signal kind</h2>
  <div class="note">
    <div class="title">Headline read</div>
    <b>label_underlying</b> = pure ATR-ladder hit/miss (0% costs).
    <b>label_option_2regime</b> = same hit/miss but with a regime-aware option model:
    <b>fast</b> regime (MFE ≥ 0.5×ATR within 30 min, gamma kicks in) gets 8× leverage / 6% RT cost;
    <b>slow</b> regime (theta + spread bleed dominate) gets 4× leverage / 8% RT cost.
    Replaces flat 5.5× linear, which made T1 hits un-winnable post-cost
    (5.5 × 0.75% ATR ≈ 4.1% gross − 8% cost = always negative). All numbers use the
    deduped view (1 fire per ticker per session per kind). EV is realized mean per fire.
    Win% is wins / decided. CI is Wilson 95%.
  </div>

  {_regime_split_card(ctx.get("regime_split") or {})}
  {_regime_sanity_multiyear_card((ctx.get("seven_year") or {}).get("regime_sanity") or {})}
  {_alignment_quintile_card(((ctx.get("seven_year") or {}).get("regime_sanity") or {}).get("alignment_quintile_probe") or {})}
  {_pretrade_predictor_card(ctx.get("pretrade_predictor") or {})}

  <table>
    <thead>
      <tr>
        <th>Kind</th>
        <th class="num">n</th>
        <th class="num">Win % (under)</th>
        <th class="num">CI</th>
        <th class="num">EV/fire (under)</th>
        <th class="num" title="T1+ hit AND survived costs in the chosen regime (fast: 8× lev / 6% cost, slow: 4× lev / 8% cost). Not a raw option win rate.">Win % (opt, post-cost)</th>
        <th class="num">CI</th>
        <th class="num" title="Mean realized option pnl per fire after regime-specific costs (post-cost, regime-aware leverage).">EV/fire (opt, post-cost)</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows_html)}
    </tbody>
  </table>

  {render_scalp_conf_band(ctx)}
  {render_scalp_context_slice(ctx)}
</div>
"""


def _regime_split_card(rs: dict) -> str:
    """Show how many fires landed in fast vs slow regime + sanity check.

    The sanity check is the load-bearing question for this whole tab:
    if P(T1+ | fast) is not materially higher than P(T1+ | slow), the
    regime tag is just decorative noise — ≥0.5×ATR within 30 bars might
    just mean "moved at all" rather than "moved with conviction".
    """
    if not rs:
        return ""
    n_fast = rs.get("n_fast", 0); n_slow = rs.get("n_slow", 0)
    pf = rs.get("pct_fast"); ps = rs.get("pct_slow")
    pf_str = "—" if pf is None else f"{pf:.1f}%"
    ps_str = "—" if ps is None else f"{ps:.1f}%"

    # Sanity-check fields
    pft = rs.get("p_fast_t1_plus"); pst = rs.get("p_slow_t1_plus")
    nft = rs.get("n_fast_t1_plus", 0); nst = rs.get("n_slow_t1_plus", 0)
    spread = rs.get("regime_t1_spread_pp")
    verdict = rs.get("regime_verdict") or "no data"
    pft_str = "—" if pft is None else f"{pft:.1f}%"
    pst_str = "—" if pst is None else f"{pst:.1f}%"
    spread_str = "—" if spread is None else f"{spread:+.1f}pp"

    # Verdict color: green = real edge, orange = suspended, red/grey = decorative/no data
    verdict_class = {
        "real edge": "pos",
        "suspended judgment": "zero",
        "decorative": "neg",
        "no data": "zero",
    }.get(verdict, "zero")

    # Spread cell uses same coloring as verdict
    spread_class = verdict_class

    return f"""
<div class="stat-row">
  <div class="stat-card">
    <div class="label">Fast regime fires</div>
    <div class="value">{n_fast}</div>
    <div style="font-size:12px;color:#4a4a45">{pf_str} of fires · 8× lev / 6% cost</div>
  </div>
  <div class="stat-card">
    <div class="label">Slow regime fires</div>
    <div class="value">{n_slow}</div>
    <div style="font-size:12px;color:#4a4a45">{ps_str} of fires · 4× lev / 8% cost</div>
  </div>
  <div class="stat-card">
    <div class="label">Detection rule</div>
    <div class="value" style="font-size:14px">≥0.5×ATR / 30min</div>
    <div style="font-size:12px;color:#4a4a45">MFE crosses half-ATR before 30 bars elapse</div>
  </div>
</div>

<div class="note" style="border-left-color:#6a9bcc;margin-top:12px">
  <div class="title">Regime sanity check — does the fast tag actually predict T1+ hits?</div>
  This is the load-bearing question. If P(T1+ | fast) is not meaningfully higher than
  P(T1+ | slow), then the regime detection is decorative — 0.5×ATR within 30 bars just
  means "moved at all" rather than "moved with conviction", and the entire two-regime
  model is a relabeling exercise. T1+ = stop-first ATR ladder hit any TARGET tier.
  Thresholds: <b>≥20pp</b> = real edge, <b>10–20pp</b> = suspended judgment,
  <b>&lt;10pp</b> = decorative.
</div>

<div class="stat-row">
  <div class="stat-card">
    <div class="label">P(T1+ | fast)</div>
    <div class="value">{pft_str}</div>
    <div style="font-size:12px;color:#4a4a45">{nft} / {n_fast} fast fires hit T1+</div>
  </div>
  <div class="stat-card">
    <div class="label">P(T1+ | slow)</div>
    <div class="value">{pst_str}</div>
    <div style="font-size:12px;color:#4a4a45">{nst} / {n_slow} slow fires hit T1+</div>
  </div>
  <div class="stat-card">
    <div class="label">Spread (fast − slow)</div>
    <div class="value {spread_class}">{spread_str}</div>
    <div style="font-size:12px;color:#4a4a45">verdict: <b class="{verdict_class}">{verdict}</b></div>
  </div>
</div>

<div class="note" style="border-left-color:#788c5d;margin-top:12px">
  <div class="title">Multi-year confirmation</div>
  This single-day result was tested across <b>1,310 archive fires (2019–2026, 4 yearly
  buckets with both fast/slow data)</b>. Multi-year P(T1+|fast) − P(T1+|slow) =
  <b>+49.6pp</b>, with all 4 yearly buckets individually ≥+42pp. The single-regime
  homogeneity / near-coupled-outcome concerns compressed the spread by ~9pp, not the
  10–20pp predicted. <b>The fast tag is a real production-grade discriminator</b>.
  See the multi-year regime card below for the year-by-year breakdown, and the
  slow-fires breakdown for what mechanism produces the 0% slow-side T1+ rate
  (it's not what we expected).
</div>"""


def _regime_sanity_multiyear_card(rs: dict) -> str:
    """Multi-year archive replay of the regime sanity test.

    Identical fast/slow classifier (MFE ≥ 0.5×ATR within 30 bars) applied to the
    7-year engine_scalp archive, with underlying-only ATR ladder (T1=0.75×ATR,
    T2=1.5×, T3=2.25×, stop=1.0×). Tests whether the single-day +58.5pp spread
    holds up under multi-regime conditions across multiple years.
    """
    if not rs:
        return """
<div class="note" style="border-left-color:#b0aea5;margin-top:12px">
  <div class="title">Multi-year regime sanity — not yet computed</div>
  Run <code>python3 src/seven_year_backtest.py</code> to populate this. The archive
  replay applies the same fast/slow classifier to ~1,300 multi-year fires and
  compares the spread against today's single-day result.
</div>"""

    n_total = rs.get("n_total_resimmed", 0)
    n_fast = rs.get("n_fast", 0); n_slow = rs.get("n_slow", 0)
    pf_total = rs.get("pct_fast"); ps_total = rs.get("pct_slow")
    pf = rs.get("p_fast_t1_plus"); ps = rs.get("p_slow_t1_plus")
    nft = rs.get("n_fast_t1_plus", 0); nst = rs.get("n_slow_t1_plus", 0)
    spread = rs.get("spread_pp")
    verdict = rs.get("verdict") or "no data"
    n_years_total = rs.get("n_years_total", 0)
    n_years_strong = rs.get("n_years_strong", 0)
    n_years_weak = rs.get("n_years_weak", 0)
    by_year = rs.get("by_year") or []

    pf_str = "—" if pf is None else f"{pf:.1f}%"
    ps_str = "—" if ps is None else f"{ps:.1f}%"
    spread_str = "—" if spread is None else f"{spread:+.1f}pp"
    pft_str = "—" if pf_total is None else f"{pf_total:.1f}%"
    pst_str = "—" if ps_total is None else f"{ps_total:.1f}%"

    verdict_class = {
        "strong real edge": "pos",
        "real edge": "pos",
        "suspended judgment": "zero",
        "decorative": "neg",
        "no data": "zero",
    }.get(verdict, "zero")

    # By-year rows (skip years with no slow data — undefined spread)
    yr_rows = []
    for y in by_year:
        sp = y.get("spread_pp")
        sp_cell_class = {
            "strong real edge": "pos",
            "real edge": "pos",
            "suspended judgment": "zero",
            "decorative": "neg",
            "no data": "zero",
        }.get(y.get("verdict") or "no data", "zero")
        sp_str = "—" if sp is None else f"{sp:+.1f}pp"
        pf_y = y.get("p_fast_t1_plus")
        ps_y = y.get("p_slow_t1_plus")
        pf_y_str = "—" if pf_y is None else f"{pf_y:.1f}%"
        ps_y_str = "—" if ps_y is None else f"{ps_y:.1f}%"
        yr_rows.append(f"""<tr>
  <td>{_e(y.get('year') or '—')}</td>
  <td class="num">{y.get('n_total', 0)}</td>
  <td class="num">{y.get('n_fast', 0)}</td>
  <td class="num">{pf_y_str}</td>
  <td class="num">{y.get('n_slow', 0)}</td>
  <td class="num">{ps_y_str}</td>
  <td class="num {sp_cell_class}">{sp_str}</td>
  <td><b class="{sp_cell_class}">{_e(y.get('verdict') or '—')}</b></td>
</tr>""")
    yr_html = "".join(yr_rows) if yr_rows else '<tr><td colspan="8" style="text-align:center;color:#6a6a62">no by-year data</td></tr>'

    return f"""
<div class="note" style="border-left-color:#788c5d;margin-top:24px">
  <div class="title">Multi-year regime sanity check (7-year archive replay, n={n_total:,} resimmed fires)</div>
  Identical fast/slow classifier applied to the engine_scalp historical archive
  (2019-2026), with an underlying-only ATR ladder (T1=0.75×ATR, T2=1.5×, T3=2.25×,
  stop=1.0×). Tests whether today's single-day +58.5pp spread holds up
  cross-regime. Each year is computed independently — if spread persists across
  years, regime tag is a real production filter. If it collapses on volatile
  years, the result is decorative.
</div>

<div class="stat-row">
  <div class="stat-card">
    <div class="label">P(T1+ | fast) — multi-year</div>
    <div class="value">{pf_str}</div>
    <div style="font-size:12px;color:#4a4a45">{nft:,} / {n_fast:,} fast fires hit T1+ ({pft_str} of total)</div>
  </div>
  <div class="stat-card">
    <div class="label">P(T1+ | slow) — multi-year</div>
    <div class="value">{ps_str}</div>
    <div style="font-size:12px;color:#4a4a45">{nst:,} / {n_slow:,} slow fires hit T1+ ({pst_str} of total)</div>
  </div>
  <div class="stat-card">
    <div class="label">Spread (multi-year)</div>
    <div class="value {verdict_class}">{spread_str}</div>
    <div style="font-size:12px;color:#4a4a45">verdict: <b class="{verdict_class}">{verdict}</b></div>
  </div>
  <div class="stat-card">
    <div class="label">Years with strong edge</div>
    <div class="value">{n_years_strong} / {n_years_total}</div>
    <div style="font-size:12px;color:#4a4a45">spread ≥+20pp · weak (&lt;+10pp): {n_years_weak}</div>
  </div>
</div>

<h4 style="margin-top:18px">Year-by-year regime sanity — does the spread hold up cross-regime?</h4>
<table>
  <thead>
    <tr>
      <th>Year</th>
      <th class="num">n total</th>
      <th class="num">n fast</th>
      <th class="num">P(T1+ | fast)</th>
      <th class="num">n slow</th>
      <th class="num">P(T1+ | slow)</th>
      <th class="num">Spread</th>
      <th>Verdict</th>
    </tr>
  </thead>
  <tbody>{yr_html}</tbody>
</table>

{_slow_breakdown_card(rs.get("slow_breakdown") or {})}"""


def _slow_breakdown_card(sb: dict) -> str:
    """Slow-fires probe: which mechanism produces the 0/305 slow-side T1+ rate?

    Three competing explanations:
      A) died_flat_pre_cap — slow fires really do die quietly within 4h
      B) cap_truncated     — slow fires get cut off at FORWARD_CAP_MIN=240
      C) stopped_out       — slow fires already stopped out (gate redundant w/ stop)
    """
    if not sb:
        return ""

    n_slow = sb.get("n_slow_total", 0)
    pct_stop = sb.get("pct_stop", 0)
    pct_cap = sb.get("pct_cap", 0)
    pct_flat = sb.get("pct_flat", 0)
    mech_verdict = sb.get("mechanism_verdict", "—")

    # Verdict color
    mech_class = "neg" if "C" in mech_verdict else ("pos" if "A" in mech_verdict else "zero")

    mean_bars = sb.get("mean_bars_to_event")
    median_bars = sb.get("median_bars_to_event")
    mean_mfe = sb.get("mean_mfe_pct")
    mean_atr = sb.get("mean_atr_14_pct")
    n_above_half_t1 = sb.get("n_mfe_above_half_t1")
    pct_above_half_t1 = sb.get("pct_mfe_above_half_t1")

    bars_str = "—"
    if mean_bars is not None and median_bars is not None:
        bars_str = f"mean {mean_bars:.1f} · median {median_bars:.1f}"

    half_t1_str = "—"
    if n_above_half_t1 is not None and pct_above_half_t1 is not None:
        half_t1_str = f"{n_above_half_t1} ({pct_above_half_t1:.1f}%)"

    # The production-implication note depends on the mechanism
    if "C" in mech_verdict:
        prod_color = "#d97757"  # orange
        prod_title = "Production implication — slow fires are stop-dominated"
        prod_body = """
          <b>The regime gate as a pre-trade filter is impossible</b>. Median 1 bar to
          stop means the trade is dead before the 30-bar regime classification window
          completes. The current 1×ATR stop already does the suppression work.
          The regime tag is therefore a <b>post-trade descriptor</b>: useful for
          ML feature engineering and ex-post diagnostics, not for live trading
          gates. <b>Recommendation: skip s5_regime_gate</b>. Instead, find pre-trade
          features that correlate with slow regime (time-of-day, RVOL, distance from
          LOD, etc.) — those are the production filters that actually prevent the
          trade rather than just stopping it after the fact."""
    elif "B" in mech_verdict:
        prod_color = "#6a9bcc"  # blue
        prod_title = "Production implication — re-run with extended cap"
        prod_body = """
          Slow fires are getting cut off at FORWARD_CAP_MIN=240 (4 hours). Re-run with
          FORWARD_CAP_MIN=480 (full session) to see if the 0% slow rate lifts. If it
          does, the +49.6pp spread is overstated and the gate would be less valuable
          than it appears."""
    else:  # A or mixed
        prod_color = "#788c5d"  # green
        prod_title = "Production implication — slow fires die quietly"
        prod_body = """
          Slow fires really do die within the trading window without hitting T1+.
          The regime classifier is identifying a structural property of these signals.
          <b>s5_regime_gate is justified</b>: build it as a soft suppression first
          (log "would have suppressed" without acting) for 2 weeks, then hard-suppress."""

    # Fast vs slow ATR comparison (rules out small-ATR-floor hypothesis)
    mean_atr_fast = sb.get("mean_atr_14_pct_fast")
    median_atr_fast = sb.get("median_atr_14_pct_fast")
    median_atr_slow = sb.get("median_atr_14_pct_slow")
    atr_fast_str = "—"
    if mean_atr_fast is not None and median_atr_fast is not None:
        atr_fast_str = f"mean {mean_atr_fast:.3f}% · median {median_atr_fast:.3f}%"
    atr_slow_str = "—"
    if mean_atr is not None and median_atr_slow is not None:
        atr_slow_str = f"mean {mean_atr:.3f}% · median {median_atr_slow:.3f}%"

    # Inversion note — surprising direction of ATR comparison
    atr_inversion_note = ""
    if mean_atr is not None and mean_atr_fast is not None:
        if mean_atr > mean_atr_fast:
            atr_inversion_note = (
                f"<b>Inverted from intuition</b> — slow fires have <i>higher</i> ATR than fast "
                f"(slow {mean_atr:.3f}% &gt; fast {mean_atr_fast:.3f}%). Rules out the "
                f"\"slow-class is just trades with ATR too small to survive intra-bar noise\" "
                f"hypothesis. Mechanism is direction, not volatility."
            )
        else:
            atr_inversion_note = (
                f"Slow fires have lower ATR than fast (slow {mean_atr:.3f}% &lt; fast "
                f"{mean_atr_fast:.3f}%). Calibration-floor hypothesis remains plausible."
            )

    return f"""
<h4 style="margin-top:24px">Slow-fires breakdown — what mechanism produces the 0% slow-side T1+ rate?</h4>

<div class="note" style="border-left-color:#b0aea5;margin-top:12px">
  <div class="title">Diagnostic stats first — what does the slow population look like?</div>
  Before drawing the mechanism conclusion, check the diagnostic features that would
  rule out alternative explanations. The most plausible alternative ("slow-class is
  just trades with ATR too small to survive intra-bar noise") is testable directly:
  if slow fires had systematically smaller ATR than fast, the mechanism would be a
  calibration-floor artifact rather than a real structural property.
</div>

<div class="stat-row">
  <div class="stat-card">
    <div class="label">ATR(14)% — fast fires (n={sb.get("n_fast_total", 0):,})</div>
    <div class="value" style="font-size:14px">{atr_fast_str}</div>
    <div style="font-size:12px;color:#4a4a45">baseline for comparison</div>
  </div>
  <div class="stat-card">
    <div class="label">ATR(14)% — slow fires (n={n_slow})</div>
    <div class="value" style="font-size:14px">{atr_slow_str}</div>
    <div style="font-size:12px;color:#4a4a45">test set for the calibration hypothesis</div>
  </div>
  <div class="stat-card">
    <div class="label">Bars to first event (slow)</div>
    <div class="value" style="font-size:14px">{bars_str}</div>
    <div style="font-size:12px;color:#4a4a45">how quickly slow fires resolve on the ATR ladder</div>
  </div>
  <div class="stat-card">
    <div class="label">Slow MFE ≥ 0.375×ATR (half-T1)</div>
    <div class="value" style="font-size:14px">{half_t1_str}</div>
    <div style="font-size:12px;color:#4a4a45">did the underlying get close to T1 ever?</div>
  </div>
</div>

<div class="note" style="border-left-color:#d97757;margin-top:12px">
  <div class="title">ATR-distribution diagnostic — what the comparison rules out</div>
  {atr_inversion_note}
</div>

<h4 style="margin-top:24px">Mechanism breakdown — three competing explanations, mutually exclusive</h4>
<div class="note" style="border-left-color:#b0aea5;margin-top:12px">
  <div class="title">Three competing explanations</div>
  <b>A) died_flat_pre_cap</b> — slow fires die quietly within 4h
  &nbsp;·&nbsp; <b>B) cap_truncated</b> — fires cut off at FORWARD_CAP_MIN=240
  &nbsp;·&nbsp; <b>C) stopped_out</b> — fires already stopped on underlying ATR ladder.
  Threshold: ≥70% C → stop-dominated; ≥30% B → cap-truncated; ≥50% A → die-quietly.
</div>

<div class="stat-row">
  <div class="stat-card">
    <div class="label">Stopped out (C)</div>
    <div class="value {('neg' if pct_stop >= 70 else 'zero')}">{sb.get("stopped_out", 0)}</div>
    <div style="font-size:12px;color:#4a4a45">{pct_stop:.1f}% of slow fires</div>
  </div>
  <div class="stat-card">
    <div class="label">Cap truncated (B)</div>
    <div class="value {('zero' if pct_cap < 30 else 'neg')}">{sb.get("cap_truncated", 0)}</div>
    <div style="font-size:12px;color:#4a4a45">{pct_cap:.1f}% of slow fires</div>
  </div>
  <div class="stat-card">
    <div class="label">Died flat pre-cap (A)</div>
    <div class="value {('pos' if pct_flat >= 50 else 'zero')}">{sb.get("died_flat_pre_cap", 0)}</div>
    <div style="font-size:12px;color:#4a4a45">{pct_flat:.1f}% of slow fires</div>
  </div>
  <div class="stat-card">
    <div class="label">Mechanism verdict</div>
    <div class="value {mech_class}" style="font-size:14px">{_e(mech_verdict)}</div>
    <div style="font-size:12px;color:#4a4a45">n_slow = {n_slow}</div>
  </div>
</div>

<div class="note" style="border-left-color:{prod_color};margin-top:12px">
  <div class="title">{prod_title}</div>
  {prod_body}
</div>

<div class="note" style="border-left-color:#b0aea5;margin-top:12px">
  <div class="title">Status of next-step builds — explicit deferred / blocked / not-affected</div>
  <b>Deferred:</b> <code>s5_regime_gate</code> — would be redundant with the existing 1×ATR stop;
  no edge captured. Do not build.<br>
  <b>Blocked on data:</b> Pre-trade slow-regime classifier — needs feature table built from
  prior bars + signal context. Target: <code>regime == "slow"</code> (not stopped-by-bar-5,
  per discussion — regime label is more durable than stop-event label). See pre-trade
  predictor card below.<br>
  <b>Not affected:</b> The regime tag remains valuable as (1) an ML feature for future
  models, (2) ex-post attribution / diagnostics, (3) a target variable for the pre-trade
  classifier. The 49.6pp multi-year spread is real signal regardless of how we exploit it.
</div>"""


def _alignment_quintile_card(probe: dict) -> str:
    """P(slow) and P(T1+) by prior_5bar_alignment quintile.

    Resolves the LR-vs-GBM ambiguity: did the LR coefficient on alignment
    correctly identify a fire-timing flaw in engine_v4 (with-trend signals fire
    at move-exhaustion), or was it a misspecification artifact?

    The quintile shape is the answer:
      - Monotonic increasing P(slow): with-trend → more slow, LR sign was right,
        engine_v4 has a fire-timing problem
      - Monotonic decreasing: counter-aligned → more slow, naive intuition right,
        LR sign was an artifact
      - U-shaped: both extremes are bad, neutral is the sweet spot
    """
    if not probe or not probe.get("quintiles"):
        return """
<div class="note" style="border-left-color:#b0aea5;margin-top:24px">
  <div class="title">Alignment-quintile probe — not yet computed</div>
  Re-run <code>python3 src/seven_year_backtest.py</code> to populate this. Bins
  fires by <code>prior_5bar_alignment</code> quintile and reports P(slow) and
  P(T1+) per bucket — resolves whether the LR sign was real (engine_v4 fire-timing
  flaw) or an artifact.
</div>"""

    quintiles = probe.get("quintiles") or []
    shape = probe.get("shape") or "—"
    n_eligible = probe.get("n_eligible", 0)

    # Color the shape verdict
    if "increasing" in shape and "with-trend" in shape:
        shape_color = "#d97757"   # orange — engine timing flaw
        shape_class = "neg"
    elif "decreasing" in shape:
        shape_color = "#788c5d"   # green — naive intuition right
        shape_class = "pos"
    elif "U-shaped" in shape or "inverse-U" in shape:
        shape_color = "#6a9bcc"   # blue — interaction
        shape_class = "zero"
    elif "flat" in shape:
        shape_color = "#b0aea5"   # gray — no signal
        shape_class = "zero"
    else:
        shape_color = "#b0aea5"
        shape_class = "zero"

    # Build the quintile rows
    qrows = ""
    p_slow_vals = [q["p_slow"] for q in quintiles]
    p_slow_min = min(p_slow_vals) if p_slow_vals else 0
    p_slow_max = max(p_slow_vals) if p_slow_vals else 0
    for q in quintiles:
        # Color P(slow) by relative position (lighter = lower, darker = higher)
        p_slow = q["p_slow"]
        # Inline bar chart cell — width proportional to value
        bar_width_pct = (p_slow / max(p_slow_max, 1.0)) * 100
        slow_cell = (
            f'<td class="num" style="position:relative;">'
            f'<div style="position:absolute;left:0;top:0;bottom:0;'
            f'width:{bar_width_pct:.0f}%;background:rgba(217,119,87,0.15);"></div>'
            f'<span style="position:relative;">{p_slow:.1f}%</span>'
            f'</td>'
        )
        align_med = q.get("alignment_median")
        align_med_str = "—" if align_med is None else f"{align_med:+.3f}%"
        align_lo = q.get("alignment_lo")
        align_hi = q.get("alignment_hi")
        align_range_str = "—"
        if align_lo is not None and align_hi is not None:
            align_range_str = f"[{align_lo:+.3f}%, {align_hi:+.3f}%]"
        qrows += f"""<tr>
  <td>Q{q['quintile']}</td>
  <td class="num">{q['n']:,}</td>
  <td>{align_range_str}</td>
  <td class="num">{align_med_str}</td>
  <td class="num">{q['n_slow']}</td>
  {slow_cell}
  <td class="num">{q['n_t1_plus']}</td>
  <td class="num">{q['p_t1_plus']:.1f}%</td>
</tr>"""

    return f"""
<h4 style="margin-top:24px">Alignment-quintile probe — does the LR sign reversal mean engine_v4 fires too late?</h4>
<div class="note" style="border-left-color:#6a9bcc;margin-top:12px">
  <div class="title">What this resolves</div>
  The pre-trade predictor showed <code>prior_5bar_alignment</code> as the GBM's
  top feature (unsigned) AND a positive LR coefficient on the slow class. The
  positive sign would mean <i>with-trend entries are MORE slow</i> — i.e.
  engine_v4 fires at move-exhaustion, not ignition. That's a structural critique
  of the engine, not just a feature curiosity. The quintile shape resolves it:
  monotonic increasing P(slow) → LR was right, fire-timing flaw exists.
  Monotonic decreasing → naive intuition was right, LR sign was a misspecification
  artifact. U-shape → interaction the linear model can't see.
</div>

<table>
  <thead>
    <tr>
      <th>Quintile</th>
      <th class="num">n</th>
      <th>Alignment range (signed %)</th>
      <th class="num">Median</th>
      <th class="num">n slow</th>
      <th class="num">P(slow)</th>
      <th class="num">n T1+</th>
      <th class="num">P(T1+)</th>
    </tr>
  </thead>
  <tbody>{qrows}</tbody>
</table>

<div class="note" style="border-left-color:{shape_color};margin-top:12px">
  <div class="title">Quintile shape verdict — <span class="{shape_class}">{_e(shape)}</span></div>
  Computed on n={n_eligible:,} fires with non-null <code>prior_5bar_alignment</code>.
  Q1 = most counter-aligned (prior 5 bars moved against signal side).
  Q5 = most with-trend (prior 5 bars moved with signal side).
  P(slow) trajectory across Q1→Q5 tells you the relationship direction; P(T1+) trajectory
  is the inverse signal (since T1+ is mostly fast-class).
</div>

<div class="note" style="border-left-color:#788c5d;margin-top:12px">
  <div class="title">What this resolves about the alignment hypothesis</div>
  <b>Both prior hypotheses are wrong.</b> The naive intuition (counter-trend → more slow)
  predicted monotonically decreasing P(slow); the LR sign (with-trend → more slow)
  predicted monotonically increasing. Neither: <b>strong moves in either direction are
  the most reliable</b> (Q1 P(slow)=20.6%, Q5 P(slow)=20.2%), while <b>moderate
  counter-trend pullbacks (Q2, ~-0.5% prior 5-bar) are the failure cluster</b>
  (P(slow)=29.0%). The LR's positive coefficient was averaging the inverse-U's right-side
  rise (P(T1+) climbing Q3→Q5) which the linear model couldn't disentangle from the
  left-side rise (P(slow) climbing Q1→Q2). GBM picked up the non-monotonicity — that's
  why it ranked alignment as its top feature while LR underweighted it.<br><br>
  <b>For engine_v4:</b> not a fire-timing flaw on with-trend setups. The actionable
  finding is that <b>fires with a small counter-trend pullback in the prior 5 bars
  (-1% to -0.3% alignment) are 1.4× more likely to be slow</b>. If pre-trade filtering
  is added at stage 3, this is the band to deprioritize.
</div>"""


def _pretrade_predictor_card(pp: dict) -> str:
    """Pre-trade slow-regime predictor results (sklearn LR + GBM, time-series CV).

    Tests whether we can predict regime=='slow' from features observable at signal
    time. If AUC ≥0.70 → real production filter. If 0.60≤AUC<0.70 → directional but
    weak (use as ML feature). If AUC <0.60 → no pre-trade signal, slow regime is
    irreducible noise from a pre-trade view.
    """
    if not pp:
        return """
<div class="note" style="border-left-color:#b0aea5;margin-top:24px">
  <div class="title">Pre-trade slow-regime predictor — not yet computed</div>
  Run <code>python3 src/pretrade_slow_predictor.py</code> after the 7y backtest to
  populate this. Tests whether the slow regime label can be predicted from pre-trade
  features (ATR, prior 5-bar return, alignment with signal side, recent volume z-score,
  realized vol, time-of-day). AUC ≥0.70 → real production filter; 0.60-0.70 →
  directional but weak; &lt;0.60 → irreducible noise.
</div>"""

    hl = pp.get("headline") or {}
    auc = hl.get("auc")
    src = hl.get("source") or "—"
    verdict = hl.get("verdict") or "—"
    auc_str = "—" if auc is None else f"{auc:.3f}"

    # Color the verdict
    if verdict.startswith("real pre-trade filter"):
        verdict_class = "pos"
        verdict_color = "#788c5d"
    elif verdict.startswith("directional but weak"):
        verdict_class = "zero"
        verdict_color = "#d97757"
    else:
        verdict_class = "neg"
        verdict_color = "#b0aea5"

    ds = pp.get("dataset") or {}
    n_total = ds.get("n_total", 0)
    n_slow = ds.get("n_slow", 0)
    n_fast = ds.get("n_fast", 0)
    base_rate = ds.get("base_rate_slow")

    lr_cv = pp.get("lr_cv") or {}
    gbm_cv = pp.get("gbm_cv") or {}
    lr_mean_auc = lr_cv.get("mean_auc")
    gbm_mean_auc = gbm_cv.get("mean_auc")

    yh = pp.get("year_holdout") or {}
    lr_val = yh.get("lr_val") or {}
    lr_test = yh.get("lr_test") or {}
    gbm_val = yh.get("gbm_val") or {}
    gbm_test = yh.get("gbm_test") or {}

    # Year-holdout table
    def _yh_row(name, val, test):
        def _fmt_auc(d):
            v = d.get("auc") if d else None
            return "—" if v is None else f"{v:.3f}"
        def _fmt_tdp(d):
            v = d.get("top_decile_precision") if d else None
            return "—" if v is None else f"{v*100:.1f}%"
        return f"""<tr>
  <td>{name}</td>
  <td class="num">{yh.get('n_train', 0):,}</td>
  <td class="num">{yh.get('n_val', 0):,}</td>
  <td class="num">{yh.get('n_test', 0):,}</td>
  <td class="num">{_fmt_auc(val)}</td>
  <td class="num">{_fmt_tdp(val)}</td>
  <td class="num">{_fmt_auc(test)}</td>
  <td class="num">{_fmt_tdp(test)}</td>
</tr>"""

    yh_rows = ""
    if yh.get("status") == "ok":
        yh_rows = (
            _yh_row("Logistic regression", lr_val, lr_test) +
            _yh_row("HistGradientBoosting", gbm_val, gbm_test)
        )
    yh_table = ""
    if yh_rows:
        yh_table = f"""
<h4 style="margin-top:18px">Year-holdout evaluation — train ≤2024, validate 2025, test 2026</h4>
<table>
  <thead>
    <tr>
      <th>Model</th>
      <th class="num">n train</th>
      <th class="num">n val</th>
      <th class="num">n test</th>
      <th class="num">Val AUC</th>
      <th class="num">Val top-10% prec</th>
      <th class="num">Test AUC</th>
      <th class="num">Test top-10% prec</th>
    </tr>
  </thead>
  <tbody>{yh_rows}</tbody>
</table>"""

    # Feature importances side-by-side
    fi = pp.get("feature_importances") or {}
    lr_ranked = (fi.get("lr") or {}).get("ranked_by_abs_coef") or []
    gbm_ranked = (fi.get("gbm") or {}).get("ranked_by_perm_importance") or []

    def _fi_rows(items, fmt_val):
        rows = ""
        for k, v in items[:7]:
            sign_class = "pos" if v > 0 else ("neg" if v < 0 else "zero")
            rows += f"""<tr>
  <td><code>{_e(k)}</code></td>
  <td class="num {sign_class}">{fmt_val(v)}</td>
</tr>"""
        return rows

    lr_fi_rows = _fi_rows(lr_ranked, lambda v: f"{v:+.3f}")
    gbm_fi_rows = _fi_rows(gbm_ranked, lambda v: f"{v:+.4f}")

    # Pull alignment-LR coefficient for the headline
    align_lr_coef = (fi.get("lr") or {}).get("coefficients", {}).get("prior_5bar_alignment", 0.0)
    align_gbm_rank = None
    for i, (k, _v) in enumerate((fi.get("gbm") or {}).get("ranked_by_perm_importance") or []):
        if k == "prior_5bar_alignment":
            align_gbm_rank = i + 1
            break
    align_lr_rank = None
    for i, (k, _v) in enumerate((fi.get("lr") or {}).get("ranked_by_abs_coef") or []):
        if k == "prior_5bar_alignment":
            align_lr_rank = i + 1
            break

    return f"""
<h4 style="margin-top:24px">Pre-trade slow-regime predictor — can we identify slow fires before entry?</h4>

<div class="note" style="border-left-color:#d97757;margin-top:12px">
  <div class="title">Headline finding — alignment is the dominant feature, with non-monotonic shape</div>
  <code>prior_5bar_alignment</code> ranks <b>#{align_gbm_rank or '?'}</b> on the GBM
  by permutation importance and <b>#{align_lr_rank or '?'}</b> on the LR by |coefficient|.
  The LR coefficient is <b class="pos">{align_lr_coef:+.3f}</b> (positive on the slow class) —
  initially read as "with-trend entries are more slow," which would have been a
  fire-timing critique of engine_v4. The <b>alignment-quintile probe above</b> kills
  that interpretation: P(slow) is inverse-U with peak at Q2 (moderate counter-trend
  pullback). Strong moves either direction are the most reliable; the LR was averaging
  a non-monotonic relationship the linear model couldn't represent. <b>Engine_v4 fire
  timing is not the problem</b> — moderate counter-trend pullback fires are. The GBM
  ranked alignment top because it captured this non-linearity; LR ranked it third
  because the averaged coefficient is small.
</div>

<div class="note" style="border-left-color:#6a9bcc;margin-top:12px">
  <div class="title">Question and acceptance bands</div>
  Multi-year archive showed slow-regime fires never hit T1+ (0/305) and are 100%
  stop-dominated. Can we predict <code>regime=='slow'</code> from features observable
  <i>at signal time</i> (no forward-bar information)? If yes, we have a real
  production gate that prevents the trade rather than just stopping it after.
  <b>Acceptance:</b> AUC ≥0.70 → real filter, build production gate. 0.60–0.70 →
  directional but weak, use as ML feature only. &lt;0.60 → these features don't
  get there (different from "irreducible noise" — richer features may push higher).
  <b>Validation:</b> 5-fold TimeSeriesSplit (no random shuffles, chronological folds)
  + explicit year-holdout (train ≤2024, val 2025, test 2026).
</div>

<div class="stat-row">
  <div class="stat-card">
    <div class="label">Headline AUC</div>
    <div class="value {verdict_class}">{auc_str}</div>
    <div style="font-size:12px;color:#4a4a45">source: <code>{_e(src)}</code></div>
  </div>
  <div class="stat-card">
    <div class="label">5-fold CV mean — LR</div>
    <div class="value" style="font-size:14px">{('—' if lr_mean_auc is None else f'{lr_mean_auc:.3f}')}</div>
    <div style="font-size:12px;color:#4a4a45">logistic regression baseline</div>
  </div>
  <div class="stat-card">
    <div class="label">5-fold CV mean — GBM</div>
    <div class="value" style="font-size:14px">{('—' if gbm_mean_auc is None else f'{gbm_mean_auc:.3f}')}</div>
    <div style="font-size:12px;color:#4a4a45">gradient-boosted comparator</div>
  </div>
  <div class="stat-card">
    <div class="label">Dataset</div>
    <div class="value" style="font-size:14px">n={n_total:,}</div>
    <div style="font-size:12px;color:#4a4a45">{n_slow:,} slow / {n_fast:,} fast (base rate {base_rate*100 if base_rate else 0:.1f}%)</div>
  </div>
</div>

<div class="note" style="border-left-color:{verdict_color};margin-top:12px">
  <div class="title">Verdict — {_e(verdict)}</div>
  At AUC {auc_str} (year-holdout test on 2026, n={yh.get('n_test', 0)}), the predictor
  has measurable pre-trade signal. Top-decile precision of
  {(gbm_test.get('top_decile_precision') or 0)*100:.1f}% beats the {(base_rate or 0)*100:.1f}% base rate by
  ~{((gbm_test.get('top_decile_precision') or 0)*100 - (base_rate or 0)*100):.1f}pp —
  directional, but well below the ~60% top-decile precision needed for a production gate.
  <b>Recommendation: do not build a hard suppression gate</b> with this feature set.
  Adding richer pre-trade context (UW flow at fire time, news LLM score, multi-timeframe
  alignment, GEX proximity) could push AUC into the 0.65–0.70 range and reopen the gate
  question — what's been proven is feature-set insufficiency, not irreducibility.
</div>

<div class="note" style="border-left-color:#788c5d;margin-top:12px">
  <div class="title">Carried forward — ML features for engine_v4 stage 3</div>
  These three features have non-trivial signal individually and should be wired into the
  engine_v4 stage-3 ML scoring model when it's built. The composite score will benefit
  even though no single feature is gate-worthy:
  <ul style="margin:6px 0 0 16px;padding:0">
    <li><code>prior_5bar_alignment</code> — top GBM feature (perm importance), top-3 LR
        coefficient. Signed direction TBD on quintile shape (above).</li>
    <li><code>prior_5bar_volume_z</code> — top LR coefficient (-0.337 on slow). High recent
        volume vs 30-bar baseline confirms breakout.</li>
    <li><code>atr_14_pct</code> — second-largest LR coefficient (+0.330). Slow fires have
        systematically higher ATR; useful as a calibration feature for stop sizing.</li>
  </ul>
</div>

{yh_table}

<h4 style="margin-top:18px">Feature importances — what's actually moving the needle</h4>
<div class="note" style="border-left-color:#b0aea5;margin-top:12px">
  <div class="title">How to read</div>
  LR coefficients are signed (positive = pushes prediction toward "slow"; negative
  = pushes toward "fast"). GBM permutation importance is unsigned magnitude (how much
  AUC drops when this feature is shuffled). Sign disagreements between the two
  models indicate non-linear interactions; magnitude disagreements indicate
  features that matter to one model class but not the other.
</div>

<div style="display:flex;gap:16px;flex-wrap:wrap;align-items:flex-start">
  <table style="flex:1;min-width:300px">
    <thead><tr><th colspan="2">LR — by |coefficient|</th></tr><tr><th>Feature</th><th class="num">Coef (signed)</th></tr></thead>
    <tbody>{lr_fi_rows}</tbody>
  </table>
  <table style="flex:1;min-width:300px">
    <thead><tr><th colspan="2">GBM — by permutation importance</th></tr><tr><th>Feature</th><th class="num">ΔAUC when shuffled</th></tr></thead>
    <tbody>{gbm_fi_rows}</tbody>
  </table>
</div>

<div class="note" style="border-left-color:#d97757;margin-top:12px">
  <div class="title">Surprising sign — alignment hypothesis partially refuted</div>
  The directional-alignment hypothesis predicted that slow fires would be systematically
  counter-trend (signal-side opposes prior 5-bar move). The data partially refutes this:
  <code>prior_5bar_alignment</code> IS the most important feature for the GBM model
  (highest permutation importance), but the LR coefficient is <b>positive</b>
  ({(fi.get("lr") or {}).get("coefficients", {}).get("prior_5bar_alignment", 0):+.3f}) —
  meaning <i>with-trend entries are MORE likely to be slow</i>, not less. Plausible
  interpretation: with-trend signals fire at move-exhaustion and immediately mean-revert,
  while genuine counter-trend signals (rare in this archive — the strategies are mostly
  momentum/breakout) catch reversals before they're crowded. Worth flagging that this is
  <b>a weak signal</b> (CV AUC ~0.6) and the sign could be partly model artifact —
  GBM's unsigned importance just says alignment <i>matters</i>, not whether
  with-trend or counter-trend is worse. Single-feature univariate plot would resolve
  the ambiguity.
</div>"""


def render_scalp_context_slice(ctx: dict) -> str:
    """Fire-time context slice — win%/EV by whale/news/conviction presence."""
    slices: dict = ctx.get("ctx_slices") or {}
    if not slices or not slices.get("all"):
        return ""

    def _row(label, slc):
        if slc is None:
            return f"""<tr>
  <td>{_e(label)}</td>
  <td class="num">0</td>
  <td class="num">—</td><td class="num">—</td>
  <td class="num">—</td><td class="num">—</td>
</tr>"""
        u = slc["under"]
        o = slc["opt"]
        u_win = u.win_pct if u and not u.suppressed else None
        u_ev  = u.ev_mean_pct if u and not u.suppressed else None
        o_win = o.win_pct if o and not o.suppressed else None
        o_ev  = o.ev_mean_pct if o and not o.suppressed else None
        return f"""<tr>
  <td>{_e(label)}</td>
  <td class="num">{slc['n']}</td>
  <td class="num">{_fmt_num(u_win)}%</td>
  <td class="num {_ev_class(u_ev)}">{_fmt_pct(u_ev)}</td>
  <td class="num">{_fmt_num(o_win)}%</td>
  <td class="num {_ev_class(o_ev)}">{_fmt_pct(o_ev)}</td>
</tr>"""

    rows = [
        ("All fires (baseline)",    slices.get("all")),
        ("With whale flow",         slices.get("with_flow")),
        ("No whale flow",           slices.get("no_flow")),
        ("With news",               slices.get("with_news")),
        ("No news",                 slices.get("no_news")),
        ("With conviction",         slices.get("with_conv")),
        ("No conviction",           slices.get("no_conv")),
        ("Flow + conviction (high quality)", slices.get("flow_and_conv")),
    ]
    rows_html = "".join(_row(lbl, slc) for lbl, slc in rows)

    return f"""
  <h3>Fire-time context slice (compare buckets to find what matters)</h3>
  <div class="note">
    <div class="title">How to read</div>
    Each row is the same scalp universe filtered by whether the fire-time lifecycle snapshot
    showed whale flow / news / conviction. If "with whale flow" beats "no whale flow" on EV,
    that's a real signal. <b>Flow + conviction</b> isolates the highest-quality fires.
  </div>
  <table>
    <thead>
      <tr>
        <th>Bucket</th>
        <th class="num">n</th>
        <th class="num">Win % (under)</th>
        <th class="num">EV/fire (under)</th>
        <th class="num" title="T1+ hit AND survived costs in the chosen regime — not a raw option win rate.">Win % (opt, post-cost)</th>
        <th class="num" title="Mean realized option pnl per fire after regime-specific costs.">EV/fire (opt, post-cost)</th>
      </tr>
    </thead>
    <tbody>{rows_html}</tbody>
  </table>
"""


def render_scalp_conf_band(ctx: dict) -> str:
    """Confidence-band slice — kind × low/med/high."""
    cbt = ctx["scalp_conf_band"]   # dict {(kind, band): SliceStat}
    if not cbt:
        return ""
    # Pick a band ordering
    band_order = ["low", "med", "high", "unknown"]
    by_kind: dict[str, dict[str, object]] = {}
    for (k, b), s in cbt.items():
        by_kind.setdefault(k, {})[b] = s

    rows = []
    for k in sorted(by_kind, key=lambda x: -sum((by_kind[x].get(b).n if by_kind[x].get(b) else 0) for b in band_order)):
        cells = [f'<td class="kind">{_e(k)}</td>']
        for b in band_order:
            s = by_kind[k].get(b)
            if s is None:
                cells.append('<td class="num">—</td>')
                continue
            if s.suppressed:
                cells.append(f'<td class="num suppressed">n={s.n}</td>')
                continue
            ev = s.ev_mean_pct
            cls = _ev_class(ev)
            cells.append(f'<td class="num {cls}">n={s.n} • EV {_fmt_pct(ev)}</td>')
        rows.append("<tr>" + "".join(cells) + "</tr>")

    return f"""
  <h3>Confidence-band slice (option 5.5×, n &lt; 5 suppressed)</h3>
  <table>
    <thead>
      <tr>
        <th>Kind</th>
        <th class="num">low conf (&lt;30)</th>
        <th class="num">med (30–60)</th>
        <th class="num">high (60+)</th>
        <th class="num">unknown</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
"""


def render_swing(ctx: dict) -> str:
    fires = ctx["swing_fires"]
    if not fires:
        body = '<div class="note"><div class="title">No swing fires logged yet</div>The shadow logger writes a JSONL line whenever a swing engine snapshot transitions into a fire state. It runs every 30 seconds — fires will accumulate as the swing engines emit BUY/PROBE signals.</div>'
    else:
        # group by source
        by_source: dict[str, list] = {}
        for f in fires:
            by_source.setdefault(f.source, []).append(f)

        sec_html = []
        for src in sorted(by_source):
            rows = by_source[src]
            tr = []
            for r in rows[-100:]:   # last 100 per source
                tr.append(f"""
<tr>
  <td>{_e(r.detected_utc[:19])}</td>
  <td class="kind">{_e(r.ticker)}</td>
  <td>{_e(r.kind)}</td>
  <td>{_e(r.side)}</td>
  <td class="num">{_e(r.entry_premium)}</td>
  <td class="num">{_e(r.underlying_close)}</td>
  <td class="num">{_e(r.stop_loss)}</td>
  <td class="num">{_e(r.take_profit)}</td>
  <td>{_e(r.news_sentiment or '—')}</td>
</tr>""")

            sec_html.append(f"""
<h3>Source: {src} ({len(rows)} fire(s))</h3>
<table>
  <thead>
    <tr>
      <th>Time (UTC)</th>
      <th>Ticker</th>
      <th>Kind</th>
      <th>Side</th>
      <th class="num">Entry premium</th>
      <th class="num">Underlying</th>
      <th class="num">Stop</th>
      <th class="num">Target</th>
      <th>News</th>
    </tr>
  </thead>
  <tbody>{''.join(tr)}</tbody>
</table>
""")
        body = "".join(sec_html)

    return f"""
<div class="tab" id="tab-swing">
  <h2>Swing — fires from shadow logger</h2>
  <div class="note">
    <div class="title">Outcomes pending</div>
    Swing outcomes need daily-bar history to compute. The shadow logger captures fires now —
    once daily bars are wired (yfinance / Alpaca), each fire gets MFE/MAE, target/stop hit-day,
    and cost-aware labels at 1d/3d/5d/10d/20d horizons.
  </div>
  {body}
</div>
"""


def render_ml(ctx: dict) -> str:
    ledger = ctx["ml_ledger"]
    rows = []
    for r in ledger:
        if r.ready:
            badge = '<span class="badge ready">READY</span>'
        elif r.blocker == "days":
            badge = '<span class="badge need-days">need days</span>'
        elif r.blocker == "fires":
            badge = '<span class="badge need-fires">need fires</span>'
        else:
            badge = '<span class="badge need-both">need both</span>'
        rows.append(f"""
<tr>
  <td class="kind">{_e(r.kind)}</td>
  <td class="num">{r.n_fires}</td>
  <td class="num">{r.n_days}</td>
  <td class="num">{r.n_wins_underlying}</td>
  <td class="num">{r.n_wins_option}</td>
  <td class="num">{r.days_to_min}</td>
  <td class="num">{r.fires_to_min}</td>
  <td>{badge}</td>
</tr>""")

    return f"""
<div class="tab" id="tab-ml">
  <h2>ML readiness ledger</h2>
  <div class="note">
    <div class="title">Graduation rule</div>
    A kind becomes ML-trainable when it has <b>≥ 30 unique trading days</b> AND
    <b>≥ 100 deduped fires</b>. Below that, sample noise &gt; signal — rules-only is the right tool.
    No model training in v1; this is the ledger that tells us when each class is ready to graduate.
  </div>
  <table>
    <thead>
      <tr>
        <th>Kind</th>
        <th class="num">Fires</th>
        <th class="num">Days</th>
        <th class="num">Wins (under)</th>
        <th class="num">Wins (opt 5.5×)</th>
        <th class="num">Days→min</th>
        <th class="num">Fires→min</th>
        <th>Status</th>
      </tr>
    </thead>
    <tbody>{''.join(rows)}</tbody>
  </table>
</div>
"""


def render_lifecycle(ctx: dict) -> str:
    recs = ctx.get("lifecycles") or []
    if not recs:
        body = '<div class="note">No lifecycle data found.</div>'
    else:
        # Stats summary
        n = len(recs)
        n_trade = sum(1 for r in recs if r.final_mode == "TRADE")
        n_open = sum(1 for r in recs if r.has_open_trade)
        n_news = sum(1 for r in recs if r.had_news)
        n_flow = sum(1 for r in recs if r.had_flow)
        n_conv = sum(1 for r in recs if r.had_conviction)

        # peak/trough
        peaks = [r.day_pct_peak for r in recs if r.day_pct_peak is not None]
        troughs = [r.day_pct_trough for r in recs if r.day_pct_trough is not None]
        peak_max = max(peaks) if peaks else 0
        trough_min = min(troughs) if troughs else 0

        # split by mode
        trade_recs = [r for r in recs if r.final_mode == "TRADE"]
        watch_recs = [r for r in recs if r.final_mode == "WATCH"]
        other_recs = [r for r in recs if r.final_mode not in ("TRADE", "WATCH")]

        def _pnl_class(v):
            if v is None:
                return ""
            if v > 0.5:
                return "pos"
            if v < -0.5:
                return "neg"
            return "zero"

        drilldown_set = ctx.get("lifecycle_drilldown_set") or set()

        def _row(r):
            kinds = ", ".join(r.kinds_fired) if r.kinds_fired else "—"
            dpf = _fmt_pct(r.day_pct_final)
            peak = _fmt_pct(r.day_pct_peak)
            trough = _fmt_pct(r.day_pct_trough)
            pnl = _fmt_pct(r.open_trade_pnl_pct)
            entry = _fmt_num(r.open_trade_entry, 2)
            close = _fmt_num(r.last_close, 2)
            rvol = _fmt_num(r.rvol_peak, 2)
            exit_r = _e(r.open_trade_exit_reason or r.final_state or '—')
            badges = []
            if r.had_news:
                badges.append('<span class="badge" style="background:var(--blue);color:white">news</span>')
            if r.had_flow:
                badges.append('<span class="badge" style="background:var(--orange);color:white">flow</span>')
            if r.had_conviction:
                badges.append('<span class="badge" style="background:var(--green);color:white">conv</span>')
            badge_str = " ".join(badges) or '<span style="color:var(--mid-gray)">—</span>'

            # Ticker cell — link to drilldown if generated
            key = (r.ticker, r.date_str)
            if key in drilldown_set:
                ticker_cell = f'<a href="lifecycle/{_e(r.date_str)}/{_e(r.ticker)}.html" style="color:var(--orange);font-weight:500;text-decoration:none">{_e(r.ticker)}</a>'
            else:
                ticker_cell = _e(r.ticker)

            return f"""
<tr>
  <td class="kind">{ticker_cell}</td>
  <td class="num">{r.n_snapshots}</td>
  <td>{_e(r.final_mode)}</td>
  <td>{_e(kinds)}</td>
  <td class="num">{entry}</td>
  <td class="num">{close}</td>
  <td class="num {_pnl_class(r.day_pct_final)}">{dpf}</td>
  <td class="num pos">{peak}</td>
  <td class="num neg">{trough}</td>
  <td class="num">{rvol}</td>
  <td class="num {_pnl_class(r.open_trade_pnl_pct)}">{pnl}</td>
  <td>{exit_r}</td>
  <td>{badge_str}</td>
</tr>"""

        def _table(rows, title):
            if not rows:
                return ""
            cap = 200
            tr = "".join(_row(r) for r in rows[:cap])
            extra = f" (showing first {cap} of {len(rows)})" if len(rows) > cap else ""
            return f"""
<h3>{_e(title)}{extra}</h3>
<table>
  <thead>
    <tr>
      <th>Ticker</th>
      <th class="num">Snaps</th>
      <th>Mode</th>
      <th>Kinds fired</th>
      <th class="num">Entry</th>
      <th class="num">Last close</th>
      <th class="num">Day %</th>
      <th class="num">Peak</th>
      <th class="num">Trough</th>
      <th class="num">RVOL peak</th>
      <th class="num">PnL %</th>
      <th>State / Exit</th>
      <th>Context</th>
    </tr>
  </thead>
  <tbody>{tr}</tbody>
</table>
"""

        body = f"""
<div class="stat-row">
  <div class="stat-card"><div class="label">Tickers tracked</div><div class="value">{n}</div></div>
  <div class="stat-card"><div class="label">In TRADE mode</div><div class="value">{n_trade}</div></div>
  <div class="stat-card"><div class="label">With open trade</div><div class="value">{n_open}</div></div>
  <div class="stat-card"><div class="label">Had news</div><div class="value">{n_news}</div></div>
  <div class="stat-card"><div class="label">Had whale flow</div><div class="value">{n_flow}</div></div>
  <div class="stat-card"><div class="label">Had conviction</div><div class="value">{n_conv}</div></div>
  <div class="stat-card"><div class="label">Best day move</div><div class="value pos">{_fmt_pct(peak_max)}</div></div>
  <div class="stat-card"><div class="label">Worst day move</div><div class="value neg">{_fmt_pct(trough_min)}</div></div>
</div>
{_table(trade_recs, f'TRADE mode (paper trader opened) — {len(trade_recs)} tickers')}
{_table(watch_recs, f'WATCH mode (signal but no trade) — {len(watch_recs)} tickers')}
{_table(other_recs, f'Other modes — {len(other_recs)} tickers') if other_recs else ''}
"""

    return f"""
<div class="tab" id="tab-lifecycle">
  <h2>Stock Lifecycle — every ticker, every signal, every state</h2>
  <div class="note">
    <div class="title">What this tab shows</div>
    Each ticker the engine watched today, collapsed from its 30-second snapshot stream.
    <b>Mode</b> = TRADE (paper trader opened a position) / WATCH (signal but no trade).
    <b>Kinds fired</b> = every distinct signal kind that opened a trade for this ticker today.
    <b>Peak/Trough</b> = best and worst intraday underlying %.
    <b>PnL %</b> = current underlying % from entry on the most recent open trade.
    Context badges: <span class="badge" style="background:var(--blue);color:white">news</span>
    <span class="badge" style="background:var(--orange);color:white">flow</span>
    <span class="badge" style="background:var(--green);color:white">conv</span> = had news / had whale flow / had conviction.
  </div>
  {body}
</div>
"""


# ──────────────────────────────────────────────────────────────────────────────
# UW Live Flow tab
# ──────────────────────────────────────────────────────────────────────────────


def render_uw_flow(ctx: dict) -> str:
    aggs: dict = ctx.get("uw_aggs") or {}
    n_alerts = ctx.get("uw_n_alerts") or 0
    drilldown_set = ctx.get("lifecycle_drilldown_set") or set()
    # We use today's date for the drilldown links — pick from any record
    lifecycles = ctx.get("lifecycles") or []
    today_date = lifecycles[0].date_str if lifecycles else None

    if not aggs:
        body = '<div class="note">UW flow not fetched (key missing or fetch failed). Skipping.</div>'
    else:
        # Sort by abs net call skew (loudest signals)
        sorted_aggs = sorted(aggs.values(), key=lambda a: -abs(a.net_call_skew_m))
        bullish = [a for a in sorted_aggs if a.net_call_skew_m > 0][:25]
        bearish = [a for a in sorted_aggs if a.net_call_skew_m < 0][:25]

        def _ticker_cell(tk):
            if today_date and (tk, today_date) in drilldown_set:
                return f'<a href="lifecycle/{_e(today_date)}/{_e(tk)}.html" style="color:var(--orange);font-weight:500;text-decoration:none">{_e(tk)}</a>'
            return _e(tk)

        def _row(a):
            sweep_badge = (
                f'<span class="badge" style="background:var(--orange);color:white">{a.sweep_n} sweep</span>'
                if a.sweep_n > 0 else
                '<span style="color:var(--mid-gray)">—</span>'
            )
            net_class = "pos" if a.net_call_skew_m > 0 else ("neg" if a.net_call_skew_m < 0 else "zero")
            return f"""
<tr>
  <td class="kind">{_ticker_cell(a.ticker)}</td>
  <td class="num">{a.n_alerts}</td>
  <td>{sweep_badge}</td>
  <td class="num pos">{_fmt_num(a.call_prem_m, 2)}</td>
  <td class="num neg">{_fmt_num(a.put_prem_m, 2)}</td>
  <td class="num {net_class}">{a.net_call_skew_m:+.2f}</td>
  <td class="num">{(a.call_share or 0) * 100:.1f}%</td>
</tr>"""

        def _table(rows, title):
            tr = "".join(_row(a) for a in rows)
            return f"""
<h3>{_e(title)}</h3>
<table>
  <thead><tr>
    <th>Ticker</th><th class="num">Alerts</th><th>Sweeps</th>
    <th class="num">Call $M</th><th class="num">Put $M</th>
    <th class="num">Net call skew $M</th><th class="num">Call share</th>
  </tr></thead>
  <tbody>{tr}</tbody>
</table>"""

        # Coverage stat: how many of our tracked tickers got matched flow
        n_tracked = len({r.ticker for r in lifecycles})
        n_matched = len({r.ticker for r in lifecycles if r.ticker in aggs})

        body = f"""
<div class="stat-row">
  <div class="stat-card"><div class="label">Alerts (24h)</div><div class="value">{n_alerts}</div></div>
  <div class="stat-card"><div class="label">Tickers w/ flow</div><div class="value">{len(aggs)}</div></div>
  <div class="stat-card"><div class="label">Tracked tickers w/ flow</div><div class="value">{n_matched} / {n_tracked}</div></div>
  <div class="stat-card"><div class="label">Bullish (net call > 0)</div><div class="value pos">{len(bullish)}</div></div>
  <div class="stat-card"><div class="label">Bearish (net put > 0)</div><div class="value neg">{len(bearish)}</div></div>
</div>
{_table(bullish, f'Top bullish flow — net call skew (top {len(bullish)})')}
{_table(bearish, f'Top bearish flow — net put skew (top {len(bearish)})')}
"""

    return f"""
<div class="tab" id="tab-uwflow">
  <h2>UW Live Flow — last 24 hours</h2>
  <div class="note">
    <div class="title">What this tab shows</div>
    Live options flow from <b>Unusual Whales /api/option-trades/flow-alerts</b>, fetched at pipeline render time
    and cached for 30 minutes. Each row aggregates all alerts for a ticker over the last 24h.
    <b>Net call skew $M</b> = call premium − put premium (positive = bullish; negative = bearish).
    <b>Sweeps</b> = aggressive multi-exchange fills (high conviction). Tickers with a drilldown page
    are linked — click for full per-ticker context.
  </div>
  {body}
</div>
"""


# ──────────────────────────────────────────────────────────────────────────────
# Per-ticker lifecycle drilldown pages
# ──────────────────────────────────────────────────────────────────────────────


DRILLDOWN_CSS = """
:root {
  --dark: #141413;
  --light: #faf9f5;
  --mid-gray: #b0aea5;
  --light-gray: #e8e6dc;
  --orange: #d97757;
  --blue: #6a9bcc;
  --green: #788c5d;
  --red: #b85c4a;
}
* { box-sizing: border-box; }
html, body {
  margin: 0; padding: 0;
  background: var(--light); color: var(--dark);
  font-family: 'Lora', Georgia, serif; font-size: 15px; line-height: 1.55;
}
h1, h2, h3, h4 { font-family: 'Poppins', Arial, sans-serif; letter-spacing: 0.01em; }
h1 { font-size: 28px; margin: 0 0 6px 0; font-weight: 600; }
h2 { font-size: 19px; margin: 28px 0 10px 0; font-weight: 600; border-bottom: 1px solid var(--light-gray); padding-bottom: 4px; }
h3 { font-size: 15px; margin: 16px 0 6px 0; font-weight: 500; color: #2a2a28; }
.container { max-width: 1100px; margin: 0 auto; padding: 24px 32px; }
.back { font-size: 13px; color: var(--orange); text-decoration: none; }
.back:hover { text-decoration: underline; }
.meta { color: #6a6a62; font-size: 12px; }
.stat-row { display: flex; flex-wrap: wrap; gap: 10px; margin: 12px 0; }
.stat-card { background: white; border: 1px solid var(--light-gray); border-radius: 4px; padding: 10px 14px; flex: 1; min-width: 110px; }
.stat-card .label { font-size: 11px; color: #6a6a62; text-transform: uppercase; letter-spacing: 0.05em; font-family: 'Poppins', Arial, sans-serif; }
.stat-card .value { font-size: 18px; font-weight: 600; margin-top: 2px; font-family: 'Poppins', Arial, sans-serif; }
.pos { color: var(--green); }
.neg { color: var(--red); }
.zero { color: var(--mid-gray); }
table { width: 100%; border-collapse: collapse; font-size: 13px; background: white; }
th, td { padding: 6px 10px; border-bottom: 1px solid var(--light-gray); }
th { background: var(--light-gray); font-family: 'Poppins', Arial, sans-serif; font-weight: 500; text-align: left; font-size: 11px; text-transform: uppercase; letter-spacing: 0.04em; }
td.num, th.num { text-align: right; font-variant-numeric: tabular-nums; }
.badge { display: inline-block; padding: 1px 6px; border-radius: 8px; font-size: 10px; font-family: 'Poppins', Arial, sans-serif; font-weight: 500; }
.spark-wrap { background: white; border: 1px solid var(--light-gray); border-radius: 4px; padding: 14px; margin: 12px 0; }
.note { background: var(--light-gray); border-left: 3px solid var(--orange); padding: 10px 14px; margin: 12px 0; font-size: 13px; color: #4a4a45; border-radius: 2px; }
.note .title { font-family: 'Poppins', Arial, sans-serif; font-weight: 500; margin-bottom: 4px; color: var(--dark); }
.kv { font-family: 'Poppins', Arial, sans-serif; font-size: 12px; color: #4a4a45; }
.kv b { color: var(--dark); font-weight: 500; }
.footer { margin-top: 32px; padding-top: 12px; border-top: 1px solid var(--light-gray); font-size: 11px; color: #6a6a62; text-align: center; }
"""


def _hhmm_from_iso(s: Optional[str]) -> str:
    if not s:
        return "—"
    s = str(s)
    # 2026-04-27T18:23:50.337... → 18:23
    if "T" in s and len(s) >= 16:
        return s[11:16]
    return s[:5]


def _sparkline_svg(prices: list[float], width: int = 760, height: int = 100) -> str:
    """Simple SVG sparkline of close prices."""
    if not prices or len(prices) < 2:
        return '<div class="meta">No price path available.</div>'
    lo = min(prices)
    hi = max(prices)
    rng = max(hi - lo, 0.0001)
    pad = 4
    pts = []
    for i, p in enumerate(prices):
        x = pad + (i / max(len(prices) - 1, 1)) * (width - 2 * pad)
        y = height - pad - ((p - lo) / rng) * (height - 2 * pad)
        pts.append(f"{x:.1f},{y:.1f}")
    end_color = "var(--green)" if prices[-1] >= prices[0] else "var(--red)"
    return f"""
<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" style="display:block;width:100%;height:auto">
  <polyline fill="none" stroke="{end_color}" stroke-width="2" points="{' '.join(pts)}"/>
  <text x="4" y="14" font-family="Poppins" font-size="11" fill="#6a6a62">low {lo:.2f}</text>
  <text x="{width - 90}" y="14" font-family="Poppins" font-size="11" fill="#6a6a62">high {hi:.2f}</text>
  <text x="4" y="{height - 4}" font-family="Poppins" font-size="11" fill="#6a6a62">first {prices[0]:.2f}</text>
  <text x="{width - 90}" y="{height - 4}" font-family="Poppins" font-size="11" fill="#6a6a62">last {prices[-1]:.2f}</text>
</svg>"""


def render_lifecycle_drilldown(tl, uw_agg=None) -> str:
    """Render single-ticker drilldown HTML page from a TickerTimeline.
    `uw_agg` is an optional FlowAgg from uw_flow.aggregate_by_ticker.
    """
    ticker = tl.ticker
    date_str = tl.date_str

    # Aggregates from points
    points = tl.points
    closes = [p.last_close for p in points if p.last_close is not None]
    day_pcts = [p.day_pct for p in points if p.day_pct is not None]
    rvols = [p.rvol for p in points if p.rvol is not None]

    n_pts = len(points)
    n_trade = sum(1 for p in points if p.mode == "TRADE")
    n_watch = sum(1 for p in points if p.mode == "WATCH")
    pnls = [p.open_trade_pnl_pct for p in points if p.open_trade_pnl_pct is not None]
    pnl_peak = max(pnls) if pnls else None
    pnl_trough = min(pnls) if pnls else None

    sparkline = _sparkline_svg(closes)

    # Kinds fired chronological
    if tl.kinds_fired_chronological:
        kf_rows = "".join(
            f'<tr><td>{_e(_hhmm_from_iso(t))}</td><td class="kind">{_e(k)}</td></tr>'
            for t, k in tl.kinds_fired_chronological
        )
        kf_table = f"""
<h2>Signal kinds fired</h2>
<table>
  <thead><tr><th>Time UTC</th><th>Kind</th></tr></thead>
  <tbody>{kf_rows}</tbody>
</table>"""
    else:
        kf_table = '<h2>Signal kinds fired</h2><div class="note">No paper-trade signals fired today.</div>'

    # News
    if tl.news_seen:
        news_rows = "".join(
            f"""<tr>
  <td>{_e(ni.created_at[:16] if ni.created_at else '—')}</td>
  <td>{_e(ni.source)}</td>
  <td>{_e(ni.headline)}</td>
  <td>{_e(ni.sentiment or '—')}</td>
</tr>""" for ni in tl.news_seen
        )
        news_table = f"""
<h2>News today ({len(tl.news_seen)})</h2>
<table>
  <thead><tr><th>Created</th><th>Source</th><th>Headline</th><th>Sentiment</th></tr></thead>
  <tbody>{news_rows}</tbody>
</table>"""
    else:
        news_table = ""

    # Flow
    if tl.flow_seen:
        agg = tl.final_flow_agg
        agg_str = ""
        if agg:
            agg_str = (
                f'<div class="kv">'
                f'<b>Alerts:</b> {agg.n_alerts or "—"} · '
                f'<b>Sweeps:</b> {agg.sweep_n or "—"} · '
                f'<b>Call $M:</b> {_fmt_num(agg.call_prem_m, 2)} · '
                f'<b>Put $M:</b> {_fmt_num(agg.put_prem_m, 2)} · '
                f'<b>Net call skew:</b> {_fmt_num(agg.net_call_skew_m, 2)} · '
                f'<b>Call share:</b> {_fmt_num((agg.call_share or 0) * 100, 1)}%'
                f'</div>'
            )
        flow_rows = "".join(
            f"""<tr>
  <td>{_e(fi.type_)}</td>
  <td>{_e(fi.strike)}</td>
  <td>{_e(fi.expiry)}</td>
  <td class="num">{_fmt_num(fi.premium_m, 2)}</td>
  <td>{'<span class="badge" style="background:var(--orange);color:white">SWEEP</span>' if fi.is_sweep else _e(fi.rule or '—')}</td>
</tr>""" for fi in tl.flow_seen
        )
        flow_table = f"""
<h2>Whale flow ({len(tl.flow_seen)})</h2>
{agg_str}
<table>
  <thead><tr><th>Type</th><th>Strike</th><th>Expiry</th><th class="num">Premium $M</th><th>Rule</th></tr></thead>
  <tbody>{flow_rows}</tbody>
</table>"""
    else:
        flow_table = ""

    # Final open trade
    ft = tl.final_open_trade or {}
    if ft:
        kind = ft.get("kind") or "—"
        side = ft.get("side") or "—"
        entry = ft.get("entry_price")
        last_pnl = ft.get("last_underlying_pct")
        best = ft.get("best_option_pnl_pct")
        worst = ft.get("worst_option_pnl_pct")
        state = ft.get("state") or "—"
        exit_r = ft.get("exit_reason") or "—"
        ot_block = f"""
<h2>Final open trade</h2>
<div class="stat-row">
  <div class="stat-card"><div class="label">Kind</div><div class="value">{_e(kind)}</div></div>
  <div class="stat-card"><div class="label">Side</div><div class="value">{_e(side)}</div></div>
  <div class="stat-card"><div class="label">Entry</div><div class="value">{_fmt_num(entry, 2)}</div></div>
  <div class="stat-card"><div class="label">Underlying %</div><div class="value {_ev_class(last_pnl)}">{_fmt_pct(last_pnl)}</div></div>
  <div class="stat-card"><div class="label">Best option %</div><div class="value pos">{_fmt_pct(best)}</div></div>
  <div class="stat-card"><div class="label">Worst option %</div><div class="value neg">{_fmt_pct(worst)}</div></div>
  <div class="stat-card"><div class="label">State</div><div class="value">{_e(state)}</div></div>
  <div class="stat-card"><div class="label">Exit reason</div><div class="value">{_e(exit_r)}</div></div>
</div>"""
    else:
        ot_block = ""

    # Snapshot timeline (sample down if too many)
    sample = points
    if len(sample) > 80:
        step = max(len(sample) // 80, 1)
        sample = sample[::step][:80]
    tl_rows = []
    for p in sample:
        flags = []
        if p.has_news: flags.append('<span class="badge" style="background:var(--blue);color:white">N</span>')
        if p.has_flow: flags.append('<span class="badge" style="background:var(--orange);color:white">F</span>')
        if p.has_conviction: flags.append('<span class="badge" style="background:var(--green);color:white">C</span>')
        flag_str = " ".join(flags) or "—"
        tl_rows.append(f"""
<tr>
  <td>{_e(_hhmm_from_iso(p.as_of_utc))}</td>
  <td>{_e(p.mode)}</td>
  <td class="num">{_fmt_num(p.last_close, 2)}</td>
  <td class="num {_pnl_class_local(p.day_pct)}">{_fmt_pct(p.day_pct)}</td>
  <td class="num">{_fmt_num(p.rvol, 2)}</td>
  <td>{_e(p.open_trade_kind or '—')}</td>
  <td>{_e(p.open_trade_state or '—')}</td>
  <td class="num {_pnl_class_local(p.open_trade_pnl_pct)}">{_fmt_pct(p.open_trade_pnl_pct)}</td>
  <td>{flag_str}</td>
</tr>""")
    # Live UW flow (NOW) section
    uw_block = ""
    if uw_agg and uw_agg.n_alerts > 0:
        skew_color = "var(--green)" if uw_agg.net_call_skew_m >= 0 else "var(--red)"
        top_rows = "".join(
            f"""<tr>
  <td>{_e(t.get('type'))}</td>
  <td>{_e(t.get('strike'))}</td>
  <td>{_e(t.get('expiry'))}</td>
  <td class="num">{_fmt_num(t.get('premium_m'), 2)}</td>
  <td>{'<span class="badge" style="background:var(--orange);color:white">SWEEP</span>' if t.get('is_sweep') else _e(t.get('rule') or '—')}</td>
</tr>""" for t in (uw_agg.top3 or [])
        )
        uw_block = f"""
<h2>Live UW flow (last 24h, fetched at render)</h2>
<div class="stat-row">
  <div class="stat-card"><div class="label">Alerts (24h)</div><div class="value">{uw_agg.n_alerts}</div></div>
  <div class="stat-card"><div class="label">Sweeps</div><div class="value">{uw_agg.sweep_n}</div></div>
  <div class="stat-card"><div class="label">Call $M</div><div class="value">{_fmt_num(uw_agg.call_prem_m, 2)}</div></div>
  <div class="stat-card"><div class="label">Put $M</div><div class="value">{_fmt_num(uw_agg.put_prem_m, 2)}</div></div>
  <div class="stat-card"><div class="label">Net call skew $M</div><div class="value" style="color:{skew_color}">{uw_agg.net_call_skew_m:+.2f}</div></div>
  <div class="stat-card"><div class="label">Call share</div><div class="value">{(uw_agg.call_share or 0) * 100:.1f}%</div></div>
</div>
<table>
  <thead><tr><th>Type</th><th>Strike</th><th>Expiry</th><th class="num">Premium $M</th><th>Rule</th></tr></thead>
  <tbody>{top_rows}</tbody>
</table>"""

    timeline_table = f"""
<h2>Snapshot timeline ({len(sample)} of {len(points)})</h2>
<table>
  <thead><tr>
    <th>Time UTC</th><th>Mode</th><th class="num">Close</th><th class="num">Day %</th>
    <th class="num">RVOL</th><th>Kind</th><th>State</th><th class="num">PnL %</th><th>Flags</th>
  </tr></thead>
  <tbody>{''.join(tl_rows)}</tbody>
</table>"""

    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{_e(ticker)} · {_e(date_str)} · scalp backtest lifecycle</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600&family=Lora:wght@400;500&display=swap" rel="stylesheet">
<style>{DRILLDOWN_CSS}</style>
</head>
<body>
<div class="container">
  <a class="back" href="../../index.html">← Back to dashboard</a>
  <h1>{_e(ticker)} <span class="meta" style="font-size:14px;color:#6a6a62">· {_e(date_str)} · {n_pts} snapshots</span></h1>
  <div class="meta">Generated {now}</div>

  <div class="stat-row">
    <div class="stat-card"><div class="label">Snapshots</div><div class="value">{n_pts}</div></div>
    <div class="stat-card"><div class="label">In TRADE</div><div class="value">{n_trade}</div></div>
    <div class="stat-card"><div class="label">In WATCH</div><div class="value">{n_watch}</div></div>
    <div class="stat-card"><div class="label">Day % range</div><div class="value">{_fmt_pct(min(day_pcts) if day_pcts else None)} → {_fmt_pct(max(day_pcts) if day_pcts else None)}</div></div>
    <div class="stat-card"><div class="label">RVOL peak</div><div class="value">{_fmt_num(max(rvols) if rvols else None, 2)}</div></div>
    <div class="stat-card"><div class="label">Trade PnL peak</div><div class="value pos">{_fmt_pct(pnl_peak)}</div></div>
    <div class="stat-card"><div class="label">Trade PnL trough</div><div class="value neg">{_fmt_pct(pnl_trough)}</div></div>
  </div>

  <h2>Underlying price path ({len(closes)} points)</h2>
  <div class="spark-wrap">{sparkline}</div>

  {kf_table}
  {ot_block}
  {news_table}
  {flow_table}
  {uw_block}
  {timeline_table}

  <div class="footer">scalp_backtest lifecycle drilldown · {_e(ticker)} · {_e(date_str)}</div>
</div>
</body>
</html>
"""


def _pnl_class_local(v):
    if v is None: return ""
    if v > 0.5: return "pos"
    if v < -0.5: return "neg"
    return "zero"


def write_lifecycle_drilldowns(recs, max_pages: int = 100,
                               uw_aggs: Optional[dict] = None) -> set[tuple[str, str]]:
    """
    Generate per-ticker drilldown pages for the most-active tickers.
    Returns the set of (ticker, date_str) keys that have drilldown pages.

    `recs` is a list of LifecycleRecord (from loader_lifecycle).
    `uw_aggs` is an optional dict[ticker → FlowAgg] from uw_flow.
    """
    from lifecycle_drilldown import load_timeline

    # Pick top N by snapshot count, prioritizing TRADE-mode
    active = [r for r in recs if r.n_snapshots >= 5]
    active.sort(key=lambda r: (0 if r.final_mode == "TRADE" else 1, -r.n_snapshots))
    chosen = active[:max_pages]
    uw_aggs = uw_aggs or {}

    written: set[tuple[str, str]] = set()
    for r in chosen:
        try:
            tl = load_timeline(r.ticker, r.date_str)
            if not tl or not tl.points:
                continue
            uw_agg = uw_aggs.get(r.ticker)
            html_str = render_lifecycle_drilldown(tl, uw_agg=uw_agg)
            out_dir = OUTPUT_ROOT / "lifecycle" / r.date_str
            out_dir.mkdir(parents=True, exist_ok=True)
            out_path = out_dir / f"{r.ticker}.html"
            out_path.write_text(html_str)
            written.add((r.ticker, r.date_str))
        except Exception as e:
            print(f"[renderer] drilldown failed {r.ticker} {r.date_str}: {e!r}")
    return written


# ──────────────────────────────────────────────────────────────────────────────
# Historical (engine_scalp archive)
# ──────────────────────────────────────────────────────────────────────────────


def _hist_pnl_class(v: Optional[float]) -> str:
    """Color a pnl% cell — historical signals can hit +40000% so we widen bands."""
    if v is None:
        return ""
    if v > 5.0:
        return "pos"
    if v < -5.0:
        return "neg"
    return "zero"


def _hist_row(r) -> str:
    """One row in a historical-cuts table."""
    suppressed = r.suppressed
    win_pct  = "—" if suppressed or r.win_pct is None else f"{r.win_pct:.1f}%"
    win_ci   = "—" if suppressed or r.win_ci_lo is None else f"[{r.win_ci_lo:.1f}, {r.win_ci_hi:.1f}]"
    ev_mean  = "—" if suppressed or r.ev_mean_pct is None else f"{r.ev_mean_pct:+.2f}%"
    ev_med   = "—" if suppressed or r.ev_median_pct is None else f"{r.ev_median_pct:+.2f}%"
    ev_p25   = "—" if suppressed or r.ev_p25_pct is None else f"{r.ev_p25_pct:+.2f}%"
    ev_p75   = "—" if suppressed or r.ev_p75_pct is None else f"{r.ev_p75_pct:+.2f}%"
    avg_min  = "—" if suppressed or r.avg_minutes is None else f"{r.avg_minutes:.0f}m"
    side_mix = "—" if (r.n_call + r.n_put) == 0 else f"{r.n_call}C / {r.n_put}P"

    return f"""<tr>
  <td>{_e(r.key)}</td>
  <td class="num">{r.n}</td>
  <td class="num">{r.n_wins}</td>
  <td class="num">{r.n_losses}</td>
  <td class="num">{win_pct}</td>
  <td class="num" style="font-size:12px;color:#4a4a45">{win_ci}</td>
  <td class="num {_hist_pnl_class(r.ev_mean_pct)}">{ev_mean}</td>
  <td class="num {_hist_pnl_class(r.ev_median_pct)}">{ev_med}</td>
  <td class="num" style="font-size:12px;color:#4a4a45">{ev_p25} … {ev_p75}</td>
  <td class="num">{avg_min}</td>
  <td class="num" style="font-size:12px;color:#4a4a45">{side_mix}</td>
</tr>"""


def _hist_table(rows: list, title: str, cap: int = 60) -> str:
    if not rows:
        return ""
    head = "".join(_hist_row(r) for r in rows[:cap])
    extra = f" (showing {cap} of {len(rows)})" if len(rows) > cap else ""
    return f"""
<h3>{_e(title)}{extra}</h3>
<table>
  <thead>
    <tr>
      <th>Bucket</th>
      <th class="num">N</th>
      <th class="num">Wins</th>
      <th class="num">Losses</th>
      <th class="num">Win %</th>
      <th class="num">95% CI</th>
      <th class="num">EV mean</th>
      <th class="num">EV median</th>
      <th class="num">EV p25 … p75</th>
      <th class="num">Avg hold</th>
      <th class="num">CALL/PUT</th>
    </tr>
  </thead>
  <tbody>{head}</tbody>
</table>"""


def render_historical(ctx: dict) -> str:
    """Historical (engine_scalp archive) tab."""
    meta = ctx.get("hist_meta") or {}
    if not meta.get("available"):
        return f"""
<div class="tab" id="tab-historical">
  <h2>Historical archive — engine_scalp</h2>
  <div class="note">
    <div class="title">Archive not found</div>
    Expected at <code>engine_scalp/scalp_all_signals.json</code>. Add it and re-run the pipeline.
  </div>
</div>"""

    tl = ctx.get("hist_top_line") or {}
    n = tl.get("n", 0)
    if n == 0:
        return f"""
<div class="tab" id="tab-historical">
  <h2>Historical archive — engine_scalp</h2>
  <div class="note">No signals in archive.</div>
</div>"""

    win_pct = tl.get("win_pct")
    win_str = "—" if win_pct is None else f"{win_pct:.1f}%"
    ev_mean = tl.get("ev_mean_pct")
    ev_str = "—" if ev_mean is None else f"{ev_mean:+.2f}%"
    ev_med = tl.get("ev_median_pct")
    ev_med_str = "—" if ev_med is None else f"{ev_med:+.2f}%"
    n_call = tl.get("n_call", 0)
    n_put = tl.get("n_put", 0)
    n_dates = tl.get("n_dates", 0)
    n_tickers = tl.get("n_tickers", 0)
    n_strats = tl.get("n_strategies", 0)
    first = tl.get("first_date") or "—"
    last = tl.get("last_date") or "—"

    by_strat = ctx.get("hist_by_strategy") or []
    by_trig  = ctx.get("hist_by_trigger") or []
    by_tick  = ctx.get("hist_by_ticker") or []
    by_dir   = ctx.get("hist_by_direction") or []
    by_cat   = ctx.get("hist_by_category") or []
    by_year_ = ctx.get("hist_by_year") or []
    by_mon   = ctx.get("hist_by_month") or []
    by_exit  = ctx.get("hist_by_exit_reason") or []

    top_winners = ctx.get("hist_top_winners") or []
    worst_losers = ctx.get("hist_worst_losers") or []

    def _signal_row(s) -> str:
        pnl = "—" if s.pnl_pct is None else f"{s.pnl_pct:+.1f}%"
        cls = _hist_pnl_class(s.pnl_pct)
        held = "—" if s.minutes_held is None else f"{s.minutes_held}m"
        return f"""<tr>
  <td>{_e(s.date_str)}</td>
  <td>{_e(s.ticker or '—')}</td>
  <td>{_e(s.direction or '—')}</td>
  <td>{_e(s.strategy_label or s.strategy_id)}</td>
  <td>{_e(s.trigger or '—')}</td>
  <td class="num {cls}">{pnl}</td>
  <td class="num">{held}</td>
  <td>{_e(s.exit_reason or '—')}</td>
</tr>"""

    def _signal_table(sigs, title) -> str:
        if not sigs:
            return ""
        rows = "".join(_signal_row(s) for s in sigs)
        return f"""
<h3>{_e(title)}</h3>
<table>
  <thead>
    <tr>
      <th>Date</th>
      <th>Ticker</th>
      <th>Side</th>
      <th>Strategy</th>
      <th>Trigger</th>
      <th class="num">PnL %</th>
      <th class="num">Held</th>
      <th>Exit</th>
    </tr>
  </thead>
  <tbody>{rows}</tbody>
</table>"""

    return f"""
<div class="tab" id="tab-historical">
  <h2>Historical archive — engine_scalp</h2>
  <div class="note">
    <div class="title">What this tab shows</div>
    Every signal the legacy <code>engine_scalp</code> backtest engine has ever generated —
    rolled up from <code>engine_scalp/scalp_all_signals.json</code>. This is multi-year
    coverage (2019–2026) across {n_strats} strategies and {n_tickers} tickers, mostly
    standardized momentum / washout / ULTRO patterns with realized option PnL.
    Use these tables to see <b>which strategies, tickers, and triggers actually paid out</b>
    over a long horizon — the live engine_v4 dashboard tabs only have today's data.
  </div>

  <div class="note" style="border-left:4px solid var(--orange);background:#fff5ee">
    <div class="title" style="color:var(--orange)">⚠ Different exit rules — not directly comparable to the live Scalp tab</div>
    These archive numbers use <b>premium-based exits</b>: HARD_SL = −20% option premium,
    TRAIL_HIT = arm at +20% / exit on −20pp giveback from peak, PROFIT_50PCT = +50% take.
    The live <a data-tab="scalp" href="#" onclick="document.querySelector('[data-tab=scalp]').click();return false;">Scalp tab</a>
    uses an <b>ATR-ladder + 8% RT cost</b> model on a 2-regime option estimate.
    The two EV/win% numbers are produced by different cost+exit logic — do not compare
    them side-by-side. To compare apples to apples, run the archive through the live
    simulator (or vice-versa). Bridging is on the roadmap.
  </div>

  <div class="stat-row">
    <div class="stat-card"><div class="label">Total signals</div><div class="value">{n}</div></div>
    <div class="stat-card"><div class="label">Win % (decided)</div><div class="value">{win_str}</div></div>
    <div class="stat-card"><div class="label">EV mean / fire</div><div class="value {_hist_pnl_class(ev_mean)}">{ev_str}</div></div>
    <div class="stat-card"><div class="label">EV median</div><div class="value {_hist_pnl_class(ev_med)}">{ev_med_str}</div></div>
    <div class="stat-card"><div class="label">CALL / PUT</div><div class="value">{n_call} / {n_put}</div></div>
    <div class="stat-card"><div class="label">Trading days</div><div class="value">{n_dates}</div></div>
    <div class="stat-card"><div class="label">Tickers</div><div class="value">{n_tickers}</div></div>
    <div class="stat-card"><div class="label">Strategies</div><div class="value">{n_strats}</div></div>
  </div>
  <div class="note" style="font-size:12px;color:#4a4a45">
    Date range: {_e(first)} → {_e(last)}.
    EV mean is per-fire realized option PnL %. Heavy right tail — momentum strategies
    have +1000%+ trail-hit winners that pull the mean far from the median.
  </div>

  {_hist_table(by_strat, 'By strategy', cap=20)}
  {_hist_table(by_trig, 'By trigger', cap=20)}
  {_hist_table(by_dir, 'By direction (CALL vs PUT)', cap=10)}
  {_hist_table(by_cat, 'By category (engine label)', cap=10)}
  {_hist_table(by_tick, 'By ticker (top 30 by EV, n>=5)', cap=30)}
  {_hist_table(by_year_, 'By year', cap=20)}
  {_hist_table(by_mon, 'By month (last 36)', cap=36)}
  {_hist_table(by_exit, 'By exit reason', cap=20)}

  {_signal_table(top_winners, 'Top 25 winners (by realized PnL %)')}
  {_signal_table(worst_losers, 'Worst 25 losers (by realized PnL %)')}
</div>"""


# ──────────────────────────────────────────────────────────────────────────────
# 7-Year Backtest (archive + bar-based resim validation)
# ──────────────────────────────────────────────────────────────────────────────


def _sev_row(r: dict, kind: str = "archived") -> str:
    """One row in a 7y aggregation table.

    kind="archived"  → archive realized columns (win%, pnl_mean, pnl_median)
    kind="resim"     → resim columns (win%, ev_option_mean, ev_option_median, ev_underlying_mean)
    """
    n = r.get("n", 0)
    wins = r.get("wins", 0)
    losses = n - wins
    win_pct = r.get("win_pct")
    ci_lo = r.get("win_ci_lo"); ci_hi = r.get("win_ci_hi")

    win_str = "—" if win_pct is None else f"{win_pct:.1f}%"
    ci_str = "—" if ci_lo is None or ci_hi is None else f"[{ci_lo:.1f}, {ci_hi:.1f}]"

    if kind == "archived":
        m1 = r.get("pnl_mean"); m2 = r.get("pnl_median")
        m1_lbl = "PnL mean"; m2_lbl = "PnL median"
    else:
        m1 = r.get("ev_option_mean"); m2 = r.get("ev_option_median")
        m1_lbl = "EV opt mean"; m2_lbl = "EV opt median"

    m1_str = "—" if m1 is None else f"{m1:+.2f}%"
    m2_str = "—" if m2 is None else f"{m2:+.2f}%"

    und = r.get("ev_underlying_mean") if kind == "resim" else None
    und_str = "—" if und is None else f"{und:+.2f}%"

    return f"""<tr>
  <td>{_e(r.get('key', '—'))}</td>
  <td class="num">{n}</td>
  <td class="num">{wins}</td>
  <td class="num">{losses}</td>
  <td class="num">{win_str}</td>
  <td class="num" style="font-size:12px;color:#4a4a45">{ci_str}</td>
  <td class="num {_hist_pnl_class(m1)}">{m1_str}</td>
  <td class="num {_hist_pnl_class(m2)}">{m2_str}</td>
  <td class="num" style="font-size:12px;color:#4a4a45">{und_str}</td>
</tr>"""


def _sev_table(rows: list[dict], title: str, kind: str = "archived", cap: int = 30) -> str:
    if not rows:
        return ""
    head = "".join(_sev_row(r, kind=kind) for r in rows[:cap])
    extra = f" (showing {cap} of {len(rows)})" if len(rows) > cap else ""
    if kind == "archived":
        cols = ("PnL mean", "PnL median", "—")
    else:
        cols = ("EV opt mean", "EV opt median", "EV und mean")
    return f"""
<h3>{_e(title)}{extra}</h3>
<table>
  <thead>
    <tr>
      <th>Bucket</th>
      <th class="num">N</th>
      <th class="num">Wins</th>
      <th class="num">Losses</th>
      <th class="num">Win %</th>
      <th class="num">95% CI</th>
      <th class="num">{cols[0]}</th>
      <th class="num">{cols[1]}</th>
      <th class="num">{cols[2]}</th>
    </tr>
  </thead>
  <tbody>{head}</tbody>
</table>"""


# ──────────────────────────────────────────────────────────────────────────────
# Strategy table with UW-aware column (archive has no options-flow data, ever)
# ──────────────────────────────────────────────────────────────────────────────


def _sev_strategy_row_with_uw(r: dict) -> str:
    """Strategy row that explicitly tags every archive row 'No' for UW data."""
    n = r.get("n", 0); wins = r.get("wins", 0); losses = n - wins
    win_pct = r.get("win_pct"); ci_lo = r.get("win_ci_lo"); ci_hi = r.get("win_ci_hi")
    pnl_mean = r.get("pnl_mean"); pnl_med = r.get("pnl_median")
    win_str = "—" if win_pct is None else f"{win_pct:.1f}%"
    ci_str = "—" if ci_lo is None else f"[{ci_lo:.1f}, {ci_hi:.1f}]"
    pm_str = "—" if pnl_mean is None else f"{pnl_mean:+.2f}%"
    pmd_str = "—" if pnl_med is None else f"{pnl_med:+.2f}%"
    return f"""<tr>
  <td>{_e(r.get('key', '—'))}</td>
  <td class="num">{n}</td>
  <td class="num">{wins}</td>
  <td class="num">{losses}</td>
  <td class="num">{win_str}</td>
  <td class="num" style="font-size:12px;color:#4a4a45">{ci_str}</td>
  <td class="num {_hist_pnl_class(pnl_mean)}">{pm_str}</td>
  <td class="num {_hist_pnl_class(pnl_med)}">{pmd_str}</td>
  <td class="num" style="color:#b0aea5">No</td>
</tr>"""


def _sev_strategy_table_with_uw(rows: list[dict], title: str, cap: int = 20) -> str:
    if not rows:
        return ""
    body = "".join(_sev_strategy_row_with_uw(r) for r in rows[:cap])
    extra = f" (showing {cap} of {len(rows)})" if len(rows) > cap else ""
    return f"""
<h3>{_e(title)}{extra}</h3>
<table>
  <thead>
    <tr>
      <th>Strategy</th>
      <th class="num">N</th>
      <th class="num">Wins</th>
      <th class="num">Losses</th>
      <th class="num">Win %</th>
      <th class="num">95% CI</th>
      <th class="num">PnL mean</th>
      <th class="num">PnL median</th>
      <th class="num" title="Was UW (Unusual Whales) flow data used at signal generation? Archive predates the UW integration.">UW-aware?</th>
    </tr>
  </thead>
  <tbody>{body}</tbody>
</table>"""


# ──────────────────────────────────────────────────────────────────────────────
# Resim match-rate by strategy table
# ──────────────────────────────────────────────────────────────────────────────


def _sev_match_table(rows: list[dict], title: str, cap: int = 20) -> str:
    if not rows:
        return ""
    body_parts = []
    for r in rows[:cap]:
        align = r.get("alignment_pct")
        align_str = "—" if align is None else f"{align:.1f}%"
        # color-code alignment: ≥70 green (trust), 50-70 gray (caution), <50 red (don't trust)
        if align is None:
            cls = ""
        elif align >= 70:
            cls = "pos"
        elif align >= 50:
            cls = "zero"
        else:
            cls = "neg"
        ap = r.get("archived_pnl_mean")
        rp = r.get("resim_pnl_mean")
        ap_str = "—" if ap is None else f"{ap:+.2f}%"
        rp_str = "—" if rp is None else f"{rp:+.2f}%"
        gap = (ap - rp) if (ap is not None and rp is not None) else None
        gap_str = "—" if gap is None else f"{gap:+.2f}pp"
        body_parts.append(f"""<tr>
  <td>{_e(r.get('key','—'))}</td>
  <td class="num">{r.get('n_compared', 0)}</td>
  <td class="num">{r.get('n_archived_wins', 0)}</td>
  <td class="num">{r.get('n_resim_wins', 0)}</td>
  <td class="num">{r.get('n_aligned', 0)}</td>
  <td class="num {cls}">{align_str}</td>
  <td class="num {_hist_pnl_class(ap)}">{ap_str}</td>
  <td class="num {_hist_pnl_class(rp)}">{rp_str}</td>
  <td class="num" style="font-size:12px;color:#4a4a45">{gap_str}</td>
</tr>""")
    body = "".join(body_parts)
    extra = f" (showing {cap} of {len(rows)})" if len(rows) > cap else ""
    return f"""
<h3>{_e(title)}{extra}</h3>
<table>
  <thead>
    <tr>
      <th>Strategy</th>
      <th class="num" title="Signals where archive had WIN/LOSS and resim had a verdict">Compared</th>
      <th class="num">Archive wins</th>
      <th class="num">Resim wins</th>
      <th class="num" title="Resim verdict matched archive verdict (both win or both loss)">Aligned</th>
      <th class="num" title="≥70% trustworthy, 50-70 cautious, <50 the resim is essentially noise — archive numbers reflect option behaviour we can't reproduce from underlying bars">Alignment %</th>
      <th class="num">Archive PnL mean</th>
      <th class="num">Resim PnL mean</th>
      <th class="num">Gap (archive - resim)</th>
    </tr>
  </thead>
  <tbody>{body}</tbody>
</table>"""


# ──────────────────────────────────────────────────────────────────────────────
# Walk-forward MOMENTUM card
# ──────────────────────────────────────────────────────────────────────────────


def _sev_walk_forward_card(wf: dict) -> str:
    if not wf:
        return ""
    p = wf.get("params", {})
    ins = wf.get("in_sample", {}) or {}
    wfs = wf.get("walk_forward", {}) or {}
    bias = wf.get("hindsight_bias_pp")
    bias_str = "—" if bias is None else f"{bias:+.2f}pp"
    # bias is "in_sample - walk_forward" — large positive = headline overstates EV
    bias_cls = ""
    if bias is not None:
        bias_cls = "neg" if bias >= 30 else ("zero" if bias >= 10 else "pos")

    def _fmt_pct(v):
        return "—" if v is None else f"{v:+.2f}%"
    def _fmt_win(v):
        return "—" if v is None else f"{v:.1f}%"
    def _fmt_ci(lo, hi):
        return "—" if lo is None else f"[{lo:.1f}, {hi:.1f}]"

    top_tickers = ins.get("top_tickers") or []
    top_str = ", ".join(top_tickers) if top_tickers else "—"

    interpretation = ""
    if bias is not None:
        if bias >= 30:
            interpretation = (
                "<b style='color:#b00020'>Heavy hindsight bias.</b> The +"
                f"{ins.get('pnl_mean', 0):.0f}% headline shrinks to +"
                f"{wfs.get('pnl_mean', 0):.0f}% when you can only use prior data to pick the top 10. "
                "The 'top 10' MOMENTUM tickers were cherry-picked ex-post — "
                "ranking by realised PnL at decision time would have given you essentially the "
                "full-universe MOMENTUM EV."
            )
        elif bias >= 10:
            interpretation = (
                "<b style='color:#996600'>Some hindsight bias.</b> The headline "
                "overstates forward EV by a meaningful amount, but the walk-forward "
                "result is still positive — there's a real edge underneath the cherry-picking."
            )
        else:
            interpretation = (
                "<b style='color:#2d6a3f'>Robust to hindsight.</b> Walk-forward EV is "
                "close to the in-sample headline — the top-10 selection holds up out-of-sample."
            )

    return f"""
<h3 style="margin-top:24px">Walk-forward hindsight test — MOMENTUM top-10</h3>
<div class="note">
  <div class="title">Why this exists</div>
  The headline <b>MOMENTUM — top 10 (real options)</b> line shows +
  {ins.get('pnl_mean', 0):.0f}% mean PnL across {ins.get('n', 0):,} fires. But the 'top 10'
  was picked using <i>all</i> the data we have (including data from after each signal).
  This walk-forward re-ranks per signal using only the prior {p.get('window_days', '?')} days
  with n≥{p.get('min_n_per_ticker', '?')} per ticker, then keeps signals whose ticker would
  have been in the top {p.get('top_k', '?')} <i>at decision time</i>.
</div>

<div class="stat-row">
  <div class="stat-card">
    <div class="label">In-sample top-10 (all data)</div>
    <div class="value">+{ins.get('pnl_mean') or 0:.0f}%</div>
    <div style="font-size:12px;color:#4a4a45">n={ins.get('n', 0):,} · win {_fmt_win(ins.get('win_pct'))}</div>
  </div>
  <div class="stat-card">
    <div class="label">Walk-forward (no peeking)</div>
    <div class="value">{_fmt_pct(wfs.get('pnl_mean'))}</div>
    <div style="font-size:12px;color:#4a4a45">n={wfs.get('n', 0):,} · win {_fmt_win(wfs.get('win_pct'))}</div>
  </div>
  <div class="stat-card">
    <div class="label">Hindsight bias</div>
    <div class="value {bias_cls}">{bias_str}</div>
    <div style="font-size:12px;color:#4a4a45">in-sample minus walk-forward</div>
  </div>
  <div class="stat-card">
    <div class="label">CI walk-forward</div>
    <div class="value" style="font-size:14px">{_fmt_ci(wfs.get('win_ci_lo'), wfs.get('win_ci_hi'))}</div>
    <div style="font-size:12px;color:#4a4a45">95% Wilson on win%</div>
  </div>
</div>

<div class="note" style="font-size:13px">
  {interpretation}<br><br>
  <b>In-sample top-10 tickers (with hindsight):</b> <code>{_e(top_str)}</code><br>
  <b>Median in-sample:</b> {_fmt_pct(ins.get('pnl_median'))} ·
  <b>Median walk-forward:</b> {_fmt_pct(wfs.get('pnl_median'))} — the median is at the stop in
  both cases (-20%); the difference is entirely in the right tail.<br>
  <b>Skipped (no prior history yet):</b> {wfs.get('skipped_no_history', 0)}
</div>"""


def render_seven_year(ctx: dict) -> str:
    """7-year backtest tab: archive truth + bar-based resim validation."""
    sev = ctx.get("seven_year") or {}
    if not sev:
        return f"""
<div class="tab" id="tab-sevenyear">
  <h2>7-Year Backtest</h2>
  <div class="note">
    <div class="title">Run not found</div>
    Build it with:
    <pre>python3 src/seven_year_backtest.py</pre>
    Then re-run the pipeline.
  </div>
</div>"""

    meta = sev.get("meta", {})
    arch = sev.get("topline_archived", {})
    resim = sev.get("topline_resim", {})

    # archived headline
    arch_n = arch.get("n_decided", 0)
    arch_win = arch.get("win_pct")
    arch_ci_lo = arch.get("win_ci_lo"); arch_ci_hi = arch.get("win_ci_hi")
    arch_pnl_mean = arch.get("pnl_mean")
    arch_pnl_med = arch.get("pnl_median")

    arch_win_str = "—" if arch_win is None else f"{arch_win:.1f}%"
    arch_ci_str = "—" if arch_ci_lo is None else f"[{arch_ci_lo:.1f}, {arch_ci_hi:.1f}]"
    arch_pm_str = "—" if arch_pnl_mean is None else f"{arch_pnl_mean:+.2f}%"
    arch_pmd_str = "—" if arch_pnl_med is None else f"{arch_pnl_med:+.2f}%"

    # resim sanity
    resim_n = resim.get("n_resimmed", 0)
    resim_win = resim.get("win_pct")
    resim_ev = resim.get("ev_option_mean")
    align_pct = resim.get("archive_alignment_pct")
    align_str = "—" if align_pct is None else f"{align_pct:.1f}%"
    resim_win_str = "—" if resim_win is None else f"{resim_win:.1f}%"
    resim_ev_str = "—" if resim_ev is None else f"{resim_ev:+.2f}%"

    n_total = meta.get("n_signals", 0)
    n_no_bars = meta.get("n_no_bars", 0)

    wf = sev.get("walk_forward_momentum") or {}
    match_rows = sev.get("resim_match_by_strategy") or []

    return f"""
<div class="tab" id="tab-sevenyear">
  <h2>7-Year Backtest — every fire we have on disk</h2>

  <div class="note" style="border-left:4px solid var(--orange);background:#fff5ee">
    <div class="title" style="color:var(--orange)">⚠ Price-structure baseline only — no options-flow / UW data in this archive</div>
    Every signal in this 7-year archive was generated from <b>price-structure rules</b>
    (momentum, ORB, washouts, OB) on minute bars. The engine_scalp build that produced
    these fires <b>predates the Unusual Whales (UW) integration</b>, so there is
    <b>no whale-flow signal</b>, no DPI/GEX gating, no flow-confirmation filter applied.<br><br>
    Read the headline numbers as a <b>technical baseline</b>: this is what the strategies
    do without UW. Live engine_v4 layers UW on top — its win-rate / EV will not match these
    archive numbers, and shouldn't.
  </div>

  <div class="note">
    <div class="title">What this tab is</div>
    <b>The truth:</b> {arch_n:,} decided fires recorded by <code>engine_scalp</code> across
    7 years (2019–2026), 37 tickers, 7 strategies. Each row has a real entry/exit and a
    realized option PnL using the engine's actual rules (HARD_SL = -20% premium, TRAIL_HIT =
    ride winners then exit on -20% retrace from peak, PROFIT_50PCT = +50% take). This is
    not a model — it's what the engine actually did.<br><br>
    <b>The validation:</b> {resim_n:,} of those signals had entry timestamps + cached minute
    bars, so we re-ran the same exit rules against the bars (linear leverage approximation;
    8% RT cost). Resim agreement with archive: <b>{align_str}</b> on win/loss outcome.
    Resim is a conservative <i>floor</i> — it under-counts the gamma-driven option spikes
    that drive the archive's right-tail winners.
  </div>

  <h3>Archive headline (the real numbers)</h3>
  <div class="stat-row">
    <div class="stat-card"><div class="label">Decided fires</div><div class="value">{arch_n:,}</div></div>
    <div class="stat-card"><div class="label">Win %</div><div class="value">{arch_win_str}</div></div>
    <div class="stat-card"><div class="label">95% CI</div><div class="value" style="font-size:14px">{arch_ci_str}</div></div>
    <div class="stat-card"><div class="label">PnL mean / fire</div><div class="value {_hist_pnl_class(arch_pnl_mean)}">{arch_pm_str}</div></div>
    <div class="stat-card"><div class="label">PnL median / fire</div><div class="value {_hist_pnl_class(arch_pnl_med)}">{arch_pmd_str}</div></div>
  </div>
  <div class="note" style="font-size:12px;color:#4a4a45">
    Median is at the stop (-20%) because most fires lose. Mean is positive because winners
    average +118% (mean TRAIL_HIT). The right tail carries the strategy. Don't chase win%.
  </div>

  <h3>Resim sanity check (linear-leverage floor)</h3>
  <div class="stat-row">
    <div class="stat-card"><div class="label">Resimmed</div><div class="value">{resim_n:,}</div></div>
    <div class="stat-card"><div class="label">Resim win %</div><div class="value">{resim_win_str}</div></div>
    <div class="stat-card"><div class="label">Resim EV (5.5×, 8% cost)</div><div class="value {_hist_pnl_class(resim_ev)}">{resim_ev_str}</div></div>
    <div class="stat-card"><div class="label">Archive ↔ resim alignment</div><div class="value">{align_str}</div></div>
    <div class="stat-card"><div class="label">No bars on disk</div><div class="value">{n_no_bars}</div></div>
  </div>
  <div class="note" style="font-size:12px;color:#4a4a45">
    Resim uses a linear option model (option% ≈ underlying% × 5.5). Real options have gamma —
    on a fast underlying move, premium can spike 100-500% nonlinearly, which the linear model
    can't see. So resim under-counts winners. Treat the resim numbers as a <i>lower bound</i>
    and the archive numbers as the truth.
  </div>

  {_sev_table(sev.get("archived_by_year", []), "Archive — by year (is the edge stable?)", kind="archived", cap=20)}
  {_sev_strategy_table_with_uw(sev.get("archived_by_strategy", []), "Archive — by strategy (which one pays?)", cap=20)}
  {_sev_table(sev.get("archived_by_direction", []), "Archive — CALL vs PUT", kind="archived", cap=10)}
  {_sev_table(sev.get("archived_by_exit", []), "Archive — by exit reason (where did the money come from?)", kind="archived", cap=20)}
  {_sev_table(sev.get("archived_by_ticker", []), "Archive — by ticker (top 30 by mean PnL)", kind="archived", cap=30)}
  {_sev_table(sev.get("archived_by_month", []), "Archive — by month (chronological)", kind="archived", cap=120)}

  {_sev_walk_forward_card(wf)}

  <h3 style="margin-top:24px">Resim sanity — which archive numbers can I trust?</h3>
  <div class="note" style="font-size:13px">
    For each strategy: how often does the bar-based resim agree with the archive on
    win/loss? Strategies with <b>≥70% alignment</b> have linear-leverage faithful resims —
    treat those archive numbers as roughly reproducible. Strategies with <b>&lt;60%</b>
    are gamma-dominated — the archive numbers reflect option-premium dynamics
    (gamma, IV crush, theta) that we can't recover from underlying bars alone, so the
    archive PnL is the truth and the resim is noise.
  </div>
  {_sev_match_table(match_rows, "Resim ↔ archive alignment by strategy", cap=20)}

  <h3 style="margin-top:24px">Resim cuts (linear-leverage floor — for sanity, not betting)</h3>
  {_sev_table(sev.get("resim_by_year", []), "Resim — by year", kind="resim", cap=20)}
  {_sev_table(sev.get("resim_by_strategy", []), "Resim — by strategy", kind="resim", cap=20)}
  {_sev_table(sev.get("resim_by_ticker", []), "Resim — by ticker", kind="resim", cap=30)}

  <div class="note" style="font-size:12px;color:#4a4a45;margin-top:18px">
    Resim parameters: stop=-20% premium, trail-arm=+20%, trail-giveback=-20pp from peak,
    take-profit=+50%, leverage=5.5×, RT cost=8%, max forward window={meta.get("forward_cap_minutes", "?")} min.
    Run produced {n_total:,} signal records ({arch_n:,} decided + 137 unresolved/credit-spread).
    Generated {meta.get("generated_utc", "—")}.
  </div>
</div>"""


# ──────────────────────────────────────────────────────────────────────────────
# Archive (engine HTML mirrors)
# ──────────────────────────────────────────────────────────────────────────────


def render_archive(ctx: dict) -> str:
    items = ctx["archive_items"]   # list of (group, name, rel_path)
    by_group: dict[str, list] = {}
    for g, n, p in items:
        by_group.setdefault(g, []).append((n, p))

    sections = []
    for g in sorted(by_group):
        cards = []
        for n, p in sorted(by_group[g]):
            cards.append(f"""
<a class="archive-card" href="{_e(p)}" target="_blank">
  <div class="src">{_e(g)}</div>
  <div class="name">{_e(n)}</div>
</a>""")
        sections.append(f"<h3>{_e(g)}</h3><div class=\"archive-grid\">{''.join(cards)}</div>")

    body = "".join(sections) if sections else '<div class="note">No archive pages copied yet.</div>'

    return f"""
<div class="tab" id="tab-archive">
  <h2>Archived dashboards</h2>
  <div class="note">
    <div class="title">Read-only mirrors</div>
    Snapshots of every HTML dashboard from engine_v4 and swing_engine_v2 — copied into
    <code>output/archive/</code> so this page is self-contained. Originals untouched.
  </div>
  {body}
</div>
"""


# ──────────────────────────────────────────────────────────────────────────────
# Archive copy
# ──────────────────────────────────────────────────────────────────────────────


def copy_archives() -> list[tuple[str, str, str]]:
    """Copy engine HTML pages into output/archive/<group>/. Returns list of (group, name, rel_path)."""
    ARCHIVE_ROOT.mkdir(parents=True, exist_ok=True)
    items: list[tuple[str, str, str]] = []

    sources = [
        ("engine_v4_dashboard", ENGINE_V4_DASH),
        ("swing_engine_v2_templates", SWING_V2_TEMPLATES),
    ]
    for group, src_dir in sources:
        if not src_dir.exists():
            continue
        dest_dir = ARCHIVE_ROOT / group
        dest_dir.mkdir(parents=True, exist_ok=True)
        for fp in src_dir.iterdir():
            if fp.suffix.lower() != ".html":
                continue
            dest = dest_dir / fp.name
            try:
                shutil.copy2(fp, dest)
                rel = dest.relative_to(OUTPUT_ROOT)
                items.append((group, fp.stem, str(rel)))
            except Exception as e:
                print(f"[renderer] archive copy skipped {fp}: {e}")
    return items


# ──────────────────────────────────────────────────────────────────────────────
# Top-level page
# ──────────────────────────────────────────────────────────────────────────────


def render_page(ctx: dict) -> str:
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    overview = render_overview(ctx)
    scalp = render_scalp_table(ctx)
    swing = render_swing(ctx)
    lifecycle = render_lifecycle(ctx)
    uwflow = render_uw_flow(ctx)
    historical = render_historical(ctx)
    sevenyear = render_seven_year(ctx)
    ml = render_ml(ctx)
    archive = render_archive(ctx)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>Scalp Backtest — every signal we've shown, audited</title>
<link href="https://fonts.googleapis.com/css2?family=Poppins:wght@400;500;600&family=Lora:wght@400;500&display=swap" rel="stylesheet">
<style>{CSS}</style>
</head>
<body>
<div class="container">

<div class="header">
  <div>
    <h1>Scalp Backtest — every signal, audited</h1>
    <div style="font-size:13px;color:#4a4a45">
      ATR-primary ladder · pct-secondary ladder · cost-aware labels (5.5× / 8% RT) · Wilson 95% CI · n&lt;5 suppressed
    </div>
  </div>
  <div class="meta">
    <div>Generated {now}</div>
    <div>{ctx['n_scalp_outcomes']} scalp · {ctx['n_swing']} swing · {ctx['n_dates_scalp']} day(s)</div>
  </div>
</div>

<div class="tab-bar">
  <a data-tab="overview" class="active">Overview</a>
  <a data-tab="lifecycle">Lifecycle</a>
  <a data-tab="scalp">Scalp</a>
  <a data-tab="swing">Swing</a>
  <a data-tab="uwflow">UW Flow</a>
  <a data-tab="historical">Historical</a>
  <a data-tab="sevenyear">7-Year Backtest</a>
  <a data-tab="ml">ML readiness</a>
  <a data-tab="archive">Archive</a>
</div>

{overview}
{lifecycle}
{scalp}
{swing}
{uwflow}
{historical}
{sevenyear}
{ml}
{archive}

<div class="footer">
  scalp_backtest v0.1.0 · DESIGN.md v4 · brand-styled · read-only on engine_v4/swing_engine_v2/swing_trade_strategy
</div>

</div>
<script>
(function() {{
  const tabs = document.querySelectorAll('.tab-bar a');
  const panes = document.querySelectorAll('.tab');
  // show first by default
  document.querySelector('#tab-overview').classList.add('active');
  tabs.forEach(t => t.addEventListener('click', () => {{
    tabs.forEach(x => x.classList.remove('active'));
    panes.forEach(p => p.classList.remove('active'));
    t.classList.add('active');
    document.querySelector('#tab-' + t.dataset.tab).classList.add('active');
  }}));
}})();
</script>
</body>
</html>
"""
