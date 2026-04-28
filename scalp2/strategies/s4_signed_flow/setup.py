"""strategies/s4_signed_flow/setup.py — Signed flow setup gate. Spec S4-2.

ACCEPTANCE CRITERIA (S4-2):
  - abs(signed_flow_score) >= 0.65
  - price_aligned_with_flow: price up if flow > 0, price down if flow < 0 (last 30m)
  - not_at_extreme_iv: iv_percentile < 0.80 (avoid IV-crush traps)
  - gex_proximity_check: NOT within 0.5 ATR of major +GEX strike (don't fight gamma)
  - earnings_blackout = False
  - daily_relative_volume >= 1.2

Tests required (S4-2.T1, T2):
  T1 — extreme IV rejects S4 (signed_flow=0.80 BUT iv_percentile=0.85 → False)
  T2 — flow opposite of price rejects ('flow_price_mismatch')
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


@dataclass
class S4SetupContext:
    ticker: str
    signed_flow_score: float                 # [-1, +1]
    price_return_30m_atr: float              # signed (long-direction return)
    iv_percentile: float                     # [0, 1]
    distance_to_pos_gex_atr: float           # absolute distance
    earnings_blackout: bool
    daily_relative_volume: float


def is_s4_setup(ctx: S4SetupContext, cfg: dict
                  ) -> tuple[bool, Optional[Literal["long", "short"]], Optional[str]]:
    """S4-2. Returns (eligible, direction, reason_if_not)."""
    s4 = cfg["s4"]

    if ctx.earnings_blackout:
        return False, None, "earnings_blackout"

    abs_score = abs(ctx.signed_flow_score)
    if abs_score < s4["signed_flow_score_min_abs"]:
        return False, None, "flow_score_too_weak"

    direction = "long" if ctx.signed_flow_score > 0 else "short"

    # T2 — flow / price alignment
    if direction == "long" and ctx.price_return_30m_atr <= 0:
        return False, direction, "flow_price_mismatch"
    if direction == "short" and ctx.price_return_30m_atr >= 0:
        return False, direction, "flow_price_mismatch"

    # T1 — extreme IV
    if ctx.iv_percentile >= s4["iv_percentile_max"]:
        return False, direction, "iv_too_extreme"

    # GEX proximity
    if abs(ctx.distance_to_pos_gex_atr) < s4["gex_proximity_atr_min"]:
        return False, direction, "too_close_to_gamma"

    if ctx.daily_relative_volume < s4["daily_relative_volume_min"]:
        return False, direction, "rvol_too_low"

    return True, direction, None
