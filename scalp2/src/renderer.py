"""
renderer.py — scalp 2 dashboard.

Per DESIGN.md §7. Single-page HTML showing:
  - Phase state per signal kind (which kinds are LIVE_*)
  - Posterior scores for today's fires
  - EV curves per kind
  - Kelly ledger rolling alignment
  - Carried-forward attribution from scalp 1's audit findings

M1 scaffold writes a placeholder. M5 implements the full layout.
"""
from __future__ import annotations

from datetime import datetime, timezone


# Anthropic brand colors (matching scalp 1's audit dashboard for visual continuity)
BRAND = {
    "dark": "#141413",
    "light": "#faf9f5",
    "mid_gray": "#b0aea5",
    "light_gray": "#e8e6dc",
    "orange": "#d97757",
    "blue": "#6a9bcc",
    "green": "#788c5d",
}


def render_placeholder(ctx: dict) -> str:
    """M1 placeholder. Replaced by render_dashboard() in M5."""
    n_fires = ctx.get("n_fires", 0)
    n_eligible = ctx.get("n_eligible", 0)
    return f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>scalp 2 dashboard — v0.1 scaffold</title>
  <style>
    body {{ font-family: Georgia, serif; color: {BRAND['dark']};
           background: {BRAND['light']}; margin: 40px; max-width: 880px; }}
    h1 {{ font-family: Arial, sans-serif; color: {BRAND['orange']}; }}
    .note {{ border-left: 4px solid {BRAND['blue']}; padding: 12px 16px;
             background: {BRAND['light_gray']}; margin: 16px 0; }}
    code {{ background: {BRAND['light_gray']}; padding: 2px 6px; border-radius: 3px; }}
  </style>
</head>
<body>
  <h1>scalp 2 — Edge-Centric Trading System v2.2</h1>
  <p>Successor to <code>scalp_backtest/</code> (scalp 1, the audit project).</p>

  <div class="note">
    <b>Status:</b> v0.1 scaffold. M1 (scalp 1 loader) implemented and verified.
    M2-M5 components stubbed. See <code>scalp2/DESIGN.md</code> for the full plan.
  </div>

  <p><b>Scalp 1 inputs loaded:</b></p>
  <ul>
    <li>{n_fires:,} archive fires</li>
    <li>{n_eligible:,} eligible (regime + T1+ hit populated)</li>
  </ul>

  <p><b>Five components to implement:</b></p>
  <ol>
    <li><code>phase_gate.py</code> — state machine (M4)</li>
    <li><code>posterior_model.py</code> — Bayesian fusion (M2)</li>
    <li><code>ev_curve.py</code> — per-kind EV curves (M3)</li>
    <li><code>kelly_progression.py</code> — fractional Kelly + ramp (M4)</li>
    <li><code>position_ledger.py</code> — JSONL writer for approved fires (M1, done)</li>
  </ol>
</body>
</html>"""


def render_dashboard(ctx: dict) -> str:
    """Full dashboard — M5 deliverable."""
    raise NotImplementedError("M5 deliverable")
