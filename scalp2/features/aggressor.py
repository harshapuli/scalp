"""features/aggressor.py — SCALP-1.3 + Lee-Ready classifier.

aggressor_recent, aggressor_prior, aggressor_velocity, flip_strength.

Tests required (FND-1.T2):
  - aggressor classification matches Lee-Ready (above midpoint=+1, at=0, below=-1)

TODO Sprint 3.
"""
from __future__ import annotations

def lee_ready_classify(trade_price: float, midpoint: float, eps: float = 1e-6) -> int:
    """Returns +1 if trade > mid+eps (buyer-initiated), -1 if < mid-eps, 0 otherwise."""
    if trade_price > midpoint + eps:
        return 1
    if trade_price < midpoint - eps:
        return -1
    return 0

def aggressor_recent(trades: list, n_bars: int = 5) -> float:
    """Signed [-1, +1] aggregate over last n_bars."""
    raise NotImplementedError("SCALP-1.3")
def aggressor_velocity(now: float, prior: float, dt: float) -> float:
    raise NotImplementedError("SCALP-1.3")
