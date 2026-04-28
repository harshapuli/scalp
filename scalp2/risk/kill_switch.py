"""risk/kill_switch.py — Daily loss kill switch. Spec S5-80.

Per spec §10.4 s5.risk.daily_kill_pct = -2.0% triggers immediate close + halt.

Tests required (S5-60.T1):
  - Simulate -2.1% daily P&L; kill closes positions, blocks new entries.

TODO Sprint 14.
"""
from __future__ import annotations


class KillSwitch:
    def __init__(self, daily_kill_pct: float = -0.020):
        self.daily_kill_pct = daily_kill_pct
        self.armed = True

    def check(self, current_daily_pnl_pct: float) -> bool:
        return self.armed and current_daily_pnl_pct <= self.daily_kill_pct

    async def trigger(self) -> None:
        """Close all positions, block new entries, alert."""
        raise NotImplementedError("S5-60.T1 — TODO Sprint 14")
