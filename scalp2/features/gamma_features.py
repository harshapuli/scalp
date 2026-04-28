"""features/gamma_features.py — Gamma-related features. SCALP-1.4.

  distance_to_major_pos_gex_atr  — signed; negative = below the +GEX strike
  distance_to_pos_gex_atr        — alias for compat
  gex_magnitude_rank             — 0..1 percentile of |GEX| in current snapshot
  gamma_flip_distance            — ATR units to gamma_flip (zero-gamma) level
  strike_oi_rank                 — OI rank of major strike, 0..1
  major_gex_strike_dte           — DTE of major +GEX strike (v2.2 §22b OPEX guard)
  gex_snapshot_age_min           — v2.2 §22a freshness contract
"""
from __future__ import annotations

from datetime import date, datetime
from typing import Iterable, Optional

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import GEXSnapshot


def distance_to_major_pos_gex_atr(spot: float, gex_strike: float, atr: float) -> float:
    """Signed: spot - strike, divided by ATR. Negative = spot below strike.

    For S5 long setup: we want spot ≈ +GEX strike; |distance| ≤ 0.50 ATR.
    """
    if atr <= 0:
        return 0.0
    return (spot - gex_strike) / atr


def gex_magnitude_rank(snapshot: GEXSnapshot, ticker_strike: float) -> float:
    """Rank of |GEX(strike)| in this snapshot's gex_by_strike, [0, 1]."""
    if not snapshot.gex_by_strike:
        return 0.0
    target = abs(snapshot.gex_by_strike.get(ticker_strike, 0.0))
    if target == 0:
        return 0.0
    abs_vals = sorted(abs(v) for v in snapshot.gex_by_strike.values())
    rank = sum(1 for v in abs_vals if v <= target)
    return rank / len(abs_vals)


def gamma_flip_distance(spot: float, gamma_flip: float, atr: float) -> float:
    """Signed: spot - gamma_flip, divided by ATR.

    > 0 → spot is above zero-gamma (positive-gamma regime, mean-reverting)
    < 0 → spot is below zero-gamma (negative-gamma regime, momentum-amplifying)
    """
    if atr <= 0:
        return 0.0
    return (spot - gamma_flip) / atr


def strike_oi_rank(strike_oi: float, all_strike_ois: Iterable[float]) -> float:
    """Rank of this strike's OI within the chain. [0, 1]."""
    sorted_ois = sorted(all_strike_ois)
    if not sorted_ois:
        return 0.0
    rank = sum(1 for v in sorted_ois if v <= strike_oi)
    return rank / len(sorted_ois)


def major_gex_strike_dte(snapshot: GEXSnapshot,
                          available_expiries: Iterable[date],
                          today: Optional[date] = None) -> int:
    """v2.2 §22b OPEX guard. Returns days-to-expiry of the nearest expiry
    that owns the major +GEX strike.

    Approximation: returns DTE of the NEAREST expiry in available_expiries
    (UW snapshot doesn't always tag which expiry owns each strike; we use
    nearest as conservative — the rule wants ≥ 6 DTE buffer).
    """
    today = today or date.today()
    expiries = sorted(d for d in available_expiries if d >= today)
    if not expiries:
        return 0
    return (expiries[0] - today).days


def gex_snapshot_age_min(snapshot: GEXSnapshot, now: Optional[datetime] = None) -> int:
    """v2.2 §22a freshness contract. snapshot age in minutes."""
    now = now or datetime.now(tz=snapshot.timestamp.tzinfo)
    delta = now - snapshot.timestamp
    return max(0, int(delta.total_seconds() // 60))


if __name__ == "__main__":
    from datetime import timezone
    snap = GEXSnapshot(
        ticker="SPY",
        timestamp=datetime(2026, 4, 28, 14, 0, tzinfo=timezone.utc),
        spot_price=470.0,
        gamma_flip=465.0,
        major_pos_gex_strike=475.0,
        major_neg_gex_strike=460.0,
        gex_by_strike={460.0: -1e9, 465.0: 0, 470.0: 5e8, 475.0: 2e9, 480.0: 1e9},
        age_min=5,
    )
    atr = 2.0
    print(f"[gamma] distance_to_major_pos_gex_atr (spot 470, strike 475, atr 2): "
          f"{distance_to_major_pos_gex_atr(snap.spot_price, snap.major_pos_gex_strike, atr):+.3f}")
    print(f"[gamma] gex_magnitude_rank (strike 475): "
          f"{gex_magnitude_rank(snap, 475.0):.3f}")
    print(f"[gamma] gamma_flip_distance: "
          f"{gamma_flip_distance(snap.spot_price, snap.gamma_flip, atr):+.3f}")
    print("[gamma] OK")
