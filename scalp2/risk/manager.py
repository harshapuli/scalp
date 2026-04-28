"""risk/manager.py — Pre-trade gates. Spec S5-80.

Risk caps (spec §10.4 s5.risk):
  per-trade:        0.50% (paper/ramp), 1.00% (full Phase 7) → enforced in risk/sizing.py
  concurrent S5:    max 2
  same ticker:      max 1
  sector:           max 1
  daily kill:       -2.0% → enforced in risk/kill_switch.py
  weekly soft halt: -4.0%

Filters (S5-80):
  IV-crush exit:        empirically calibrated; vega-weighted (handled in position_manager)
  Model-stale kill:     model_version_age_hours > 48 OR drift_alert active → disable ML
  GEX freshness:        stale > gex_max_age_min, pass
  Execution-liquidity:  bid_ask_spread_pct > 0.05 OR option_volume_5d_avg < min_option_volume

Tests required (S5-80.T1, T2, T3, T4):
  T1 — family-correlation cap blocks 4th S5
  T2 — IV crush exit calibrated empirically (in position_manager)
  T3 — model-stale disables ML gate
  T4 — execution-liquidity reject fires
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


@dataclass
class RiskState:
    """Snapshot of current risk position. Constructed from journal + Alpaca positions."""
    open_s5_count: int = 0
    same_ticker_open: dict[str, int] = field(default_factory=dict)
    sector_open: dict[str, int] = field(default_factory=dict)
    daily_pnl_pct: float = 0.0
    weekly_pnl_pct: float = 0.0
    model_version_loaded_at: Optional[datetime] = None
    last_drift_alert_at: Optional[datetime] = None


class RiskManager:
    """S5-80 pre-trade gate."""

    def __init__(self, cfg: dict):
        self.cfg = cfg
        r = cfg["s5"]["risk"]
        self.concurrent_max = int(r["concurrent_max"])
        self.same_ticker_max = int(r["same_ticker_max"])
        self.sector_max = int(r["sector_max"])
        self.daily_kill_pct = float(r["daily_kill_pct"])
        self.weekly_soft_pct = float(r["weekly_soft_pct"])
        self.bid_ask_spread_pct_max = float(r["bid_ask_spread_pct_max"])
        self.option_volume_5d_min = float(r["option_volume_5d_min"])
        self.model_max_age_hours = int(r["model_max_age_hours"])

    def reject_reason(self, *,
                       ticker: str,
                       sector: Optional[str],
                       bid_ask_spread_pct: float,
                       option_volume_5d_avg: float,
                       gex_snapshot_age_min: int,
                       state: RiskState,
                       gex_max_age_min: Optional[int] = None,
                       now: Optional[datetime] = None) -> Optional[str]:
        """Returns pass_reason if any check fails, None if all pass."""
        gex_max = gex_max_age_min or int(self.cfg["uw"]["gex_max_age_min"])

        if state.open_s5_count >= self.concurrent_max:
            return "family_correlation_cap"
        if state.same_ticker_open.get(ticker, 0) >= self.same_ticker_max:
            return "same_ticker_cap"
        if sector and state.sector_open.get(sector, 0) >= self.sector_max:
            return "sector_cap"

        if bid_ask_spread_pct > self.bid_ask_spread_pct_max:
            return "option_spread_too_wide"
        if option_volume_5d_avg < self.option_volume_5d_min:
            return "option_liquidity_too_low"

        if gex_snapshot_age_min > gex_max:
            return "gex_stale"

        if state.daily_pnl_pct <= self.daily_kill_pct:
            return "daily_kill_triggered"
        if state.weekly_pnl_pct <= self.weekly_soft_pct:
            return "weekly_soft_halt"

        return None

    def allows(self, **kwargs) -> bool:
        return self.reject_reason(**kwargs) is None

    def model_is_stale(self, state: RiskState, now: Optional[datetime] = None) -> bool:
        """S5-80.T3 — model_version_age_hours > 48 → disable ML."""
        now = now or datetime.now(tz=timezone.utc)
        if state.model_version_loaded_at is None:
            return True
        age_h = (now - state.model_version_loaded_at).total_seconds() / 3600.0
        return age_h > self.model_max_age_hours
