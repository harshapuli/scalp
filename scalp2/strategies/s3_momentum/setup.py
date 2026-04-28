"""strategies/s3_momentum/setup.py — Intraday momentum gate. Spec S3-1.

ACCEPTANCE CRITERIA (S3-1):
  - session_return_atr = (current_price - day_open) / atr_d1 >= 1.5 (long)
                                                          or <= -1.5 (short)
  - vwap_distance_atr >= 0.75 ATR in trend direction
  - consecutive_higher_lows >= 4 (long) or consecutive_lower_highs >= 4 (short) over last 6 bars
  - aggressor_avg_30m: long >= 0.60, short <= 0.40
  - NOT at_liquidity (opposite of S5; we want clean trend, not zone reversal)
  - distance_to_pos_gex_atr >= 1.0 (avoid trading INTO gamma resistance)

Tests required (S3-1.T1, T2):
  T1 — choppy day with no trend rejected
  T2 — trending into gamma rejected with 'too_close_to_gamma'
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


@dataclass
class S3SetupContext:
    ticker: str
    session_return_atr: float        # (close - day_open) / atr_d1, signed
    vwap_distance_atr: float          # signed
    consecutive_higher_lows: int      # over last 6 bars (long structure)
    consecutive_lower_highs: int      # over last 6 bars (short structure)
    aggressor_avg_30m: float          # [-1, +1]
    near_htf_level_atr: float         # high → at liquidity (not what S3 wants)
    distance_to_pos_gex_atr: float    # high → far from gamma (good for S3)


def is_s3_setup(ctx: S3SetupContext, cfg: dict
                  ) -> tuple[bool, Optional[Literal["long", "short"]], Optional[str]]:
    """S3-1. Returns (eligible, direction, reason_if_not)."""
    s3 = cfg["s3"]

    # Direction by session return
    if ctx.session_return_atr >= s3["session_return_atr_min"]:
        direction = "long"
    elif ctx.session_return_atr <= -s3["session_return_atr_min"]:
        direction = "short"
    else:
        return False, None, "no_session_trend"

    # vwap_distance must be in trend direction
    if direction == "long":
        if ctx.vwap_distance_atr < s3["vwap_distance_atr_min"]:
            return False, direction, "vwap_distance_too_low"
        if ctx.consecutive_higher_lows < s3["consecutive_structure_min"]:
            return False, direction, "structure_too_choppy"
        if ctx.aggressor_avg_30m < s3["aggressor_long_min"]:
            return False, direction, "aggressor_too_weak"
    else:
        if ctx.vwap_distance_atr > -s3["vwap_distance_atr_min"]:
            return False, direction, "vwap_distance_too_low"
        if ctx.consecutive_lower_highs < s3["consecutive_structure_min"]:
            return False, direction, "structure_too_choppy"
        if ctx.aggressor_avg_30m > s3["aggressor_short_max"]:
            return False, direction, "aggressor_too_weak"

    # T2 — NOT at_liquidity (opposite of S5)
    if ctx.near_htf_level_atr <= 0.50:
        return False, direction, "too_close_to_liquidity"

    # T2 — NOT close to gamma resistance
    if abs(ctx.distance_to_pos_gex_atr) < s3["gex_proximity_atr_min"]:
        return False, direction, "too_close_to_gamma"

    return True, direction, None
