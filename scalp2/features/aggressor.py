"""features/aggressor.py — Lee-Ready aggressor classifier. SCALP-1.3.

Lee-Ready (1991) classifies trade direction by comparing trade price to the
prevailing NBBO midpoint:
  trade_price > midpoint  → buyer-initiated   (+1)
  trade_price < midpoint  → seller-initiated  (-1)
  trade_price == midpoint → tick test (use prior trade direction)
                            For simplicity here, returns 0 on exact equality.

Also computes:
  aggressor_recent — signed [-1, +1] aggregate over last N bars (default 5)
  aggressor_prior  — signed over the N bars before that
  aggressor_velocity — d(aggressor)/dt
  flip_strength   — |aggressor_prior - aggressor_recent| (for SURGE/TANK_REVERSE)

Tests: FND-1.T2 — synthetic trades above/at/below midpoint → +1, 0, -1.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


@dataclass
class ClassifiedTrade:
    """A trade with its NBBO midpoint and Lee-Ready sign."""
    ts_epoch: float
    price: float
    size: int
    midpoint: float
    sign: int                   # +1, 0, or -1


def lee_ready_classify(trade_price: float, midpoint: float, eps: float = 1e-6) -> int:
    """Returns +1 if trade > mid+eps, -1 if < mid-eps, 0 otherwise. FND-1.T2."""
    if trade_price > midpoint + eps:
        return 1
    if trade_price < midpoint - eps:
        return -1
    return 0


def classify_trades(trades: Iterable, quotes: Iterable) -> list[ClassifiedTrade]:
    """Pair each trade to its prevailing NBBO quote (last quote ≤ trade ts).

    Inputs:
      trades — sequence with .t (datetime), .price, .size
      quotes — sequence with .t (datetime), .midpoint
    """
    quote_list = sorted(quotes, key=lambda q: q.t)
    trade_list = sorted(trades, key=lambda t: t.t)
    if not quote_list:
        return [
            ClassifiedTrade(
                ts_epoch=tr.t.timestamp(),
                price=tr.price, size=tr.size,
                midpoint=tr.price, sign=0,
            )
            for tr in trade_list
        ]
    out: list[ClassifiedTrade] = []
    qi = 0
    n_q = len(quote_list)
    for tr in trade_list:
        while qi + 1 < n_q and quote_list[qi + 1].t <= tr.t:
            qi += 1
        q = quote_list[qi] if quote_list[qi].t <= tr.t else None
        mid = q.midpoint if q else tr.price
        out.append(ClassifiedTrade(
            ts_epoch=tr.t.timestamp(),
            price=tr.price,
            size=tr.size,
            midpoint=mid,
            sign=lee_ready_classify(tr.price, mid),
        ))
    return out


def aggressor_signed_mean(classified: Iterable[ClassifiedTrade]) -> float:
    """Volume-weighted signed aggressor in [-1, +1]."""
    total_v = 0
    weighted = 0.0
    for t in classified:
        total_v += t.size
        weighted += t.sign * t.size
    if total_v == 0:
        return 0.0
    return weighted / total_v


def aggressor_recent_prior(classified: list[ClassifiedTrade],
                            cutoff_ts_epoch: float,
                            window_seconds: int = 300) -> tuple[float, float]:
    """Returns (aggressor_recent, aggressor_prior) over [cutoff-W, cutoff] and
    [cutoff-2W, cutoff-W]. Default W=300s (5 min)."""
    recent_lo = cutoff_ts_epoch - window_seconds
    prior_lo = cutoff_ts_epoch - 2 * window_seconds
    recent = [t for t in classified if recent_lo <= t.ts_epoch < cutoff_ts_epoch]
    prior = [t for t in classified if prior_lo <= t.ts_epoch < recent_lo]
    return aggressor_signed_mean(recent), aggressor_signed_mean(prior)


def aggressor_velocity(now: float, prior: float, dt_seconds: float = 60.0) -> float:
    """Rate of change of aggressor. Units: aggressor-units per minute."""
    if dt_seconds <= 0:
        return 0.0
    return (now - prior) / (dt_seconds / 60.0)


def flip_strength(aggressor_recent_val: float, aggressor_prior_val: float) -> float:
    """How much did aggressor flip? For SURGE_REVERSE / TANK_REVERSE."""
    return abs(aggressor_prior_val - aggressor_recent_val)


if __name__ == "__main__":
    assert lee_ready_classify(100.05, 100.0) == 1, "above mid → +1"
    assert lee_ready_classify(99.95, 100.0) == -1, "below mid → -1"
    assert lee_ready_classify(100.0, 100.0) == 0, "at mid → 0"
    print("[aggressor] FND-1.T2 PASS — Lee-Ready synthetic trades classify correctly")
