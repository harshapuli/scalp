"""risk/position_manager.py — IV-crush watcher, stops, timeouts. Spec S5-80.

vega-weighted IV-crush exit:
  position_vega × delta_iv > 0.5 × current_premium → exit
  Placeholder 35% IV until 30-trade calibration (S5-80 acceptance).

TODO Sprint 14.
"""
from __future__ import annotations


class PositionManager:
    def __init__(self, cfg: dict): self.cfg = cfg
    async def manage_loop(self): raise NotImplementedError("S5-80")
    def should_exit_iv_crush(self, position) -> bool: raise NotImplementedError("S5-80")
