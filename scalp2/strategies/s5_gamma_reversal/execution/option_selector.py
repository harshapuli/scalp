"""
strategies/s5_gamma_reversal/execution/option_selector.py — Vertical leg picker.

Per spec §10.4 s5.execution + §10.5 build_s5_order:
  dte_min: 7
  dte_max: 14
  delta_min: 0.35
  delta_max: 0.45
  debit_to_width_max: 0.40
  option_spread_pct_max: 0.12
  limit_buffer: 1.02         (2% above mid)

TODO Sprint 14.
"""
from __future__ import annotations


def choose_expiry(chain, min_dte: int, max_dte: int): raise NotImplementedError("Sprint 14")
def choose_delta_leg(chain, expiry, delta_min: float, delta_max: float, side: str):
    raise NotImplementedError("Sprint 14")
def choose_short_leg(long_leg, target_width: float): raise NotImplementedError("Sprint 14")
