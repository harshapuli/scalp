"""scalp2 — Edge-Centric Trading System v2.2

Successor to scalp_backtest (scalp 1). See ../DESIGN.md for the full scope.

Five components:
  (1) phase_gate         — state machine (OBSERVE → PAPER → LIVE_SMALL → LIVE_FULL)
  (2) posterior_model    — Bayesian update on scalp 1's GBM baseline
  (3) ev_curve           — per-kind EV curves from archive resim
  (4) kelly_progression  — fractional Kelly with ramp + cap
  (5) position_ledger    — APPROVED+SIZED position records for the dashboard
"""
__version__ = "0.1.0-scaffold"
