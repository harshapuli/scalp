"""features/price_structure.py — SCALP-1.1.

Computes per-bar price-structure features:
  extension_from_vwap_atr, extension_from_prior_close_atr,
  failed_extension_atr, pullback_break,
  wick_pct, upper_wick_pct, lower_wick_pct.

TODO Sprint 3.
"""
from __future__ import annotations

def extension_from_vwap_atr(bar, vwap: float, atr: float) -> float: raise NotImplementedError("SCALP-1.1")
def wick_pct(bar) -> tuple[float, float, float]: raise NotImplementedError("SCALP-1.1")
def failed_extension_atr(prior_high: float, current_close: float, atr: float) -> float: raise NotImplementedError("SCALP-1.1")
