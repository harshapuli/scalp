"""features/gamma_features.py — SCALP-1.4.

distance_to_pos_gex_atr, gex_magnitude_rank, gamma_flip_distance,
strike_oi_rank, gex_strike_dte (v2.2 §22b OPEX guard).

TODO Sprint 3.
"""
from __future__ import annotations

def distance_to_major_pos_gex_atr(spot: float, gex_strike: float, atr: float) -> float:
    raise NotImplementedError("SCALP-1.4")

def gex_strike_dte(major_pos_strike, expiries: list) -> int:
    """Days-to-expiry of the major positive GEX strike. v2.2 §22b OPEX guard."""
    raise NotImplementedError("SCALP-1.4")
