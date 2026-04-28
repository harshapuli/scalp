"""risk/manager.py — Pre-trade gates. Spec S5-80.

Risk caps (spec §10.4 s5.risk):
  per-trade:        0.50% (paper/ramp), 1.00% (full Phase 7)
  concurrent S5:    max 2
  same ticker:      max 1
  sector:           max 1
  daily kill:       -2.0%
  weekly soft halt: -4.0%

Filters:
  IV-crush exit:        empirically calibrated over first 30 paper trades; vega-weighted
  Model-stale kill:     model_version_age_hours > 48 OR drift_alert active → disable ML
  GEX freshness:        stale > 30min, pass
  Execution-liquidity:  bid_ask_spread_pct > 0.05 OR option_volume_5d_avg < min_option_volume

Tests required (S5-80.T1, T2, T3, T4):
  T1 — family-correlation cap blocks 4th S5
  T2 — IV crush exit calibrated empirically
  T3 — model-stale disables ML gate
  T4 — execution-liquidity reject fires

TODO Sprint 14.
"""
from __future__ import annotations


class RiskManager:
    def __init__(self, cfg: dict): self.cfg = cfg
    def allows(self, candidate, current_positions: list) -> bool: raise NotImplementedError("S5-80")
    def reject_reason(self, candidate, current_positions: list) -> str | None: raise NotImplementedError("S5-80")
