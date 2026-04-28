"""strategies/s2_orb/setup.py — is_s2_setup gate. Spec S2-2.

ALL conditions must hold:
  - gap_pct >= cfg.s2.gap_pct_min (default 2%) OR pre_market_volume_ratio >= 5
  - After 09:35 ET: price closes above OR_high (long) or below OR_low (short)
  - Volume_at_breakout >= 1.5× avg minute volume
  - No earnings within 24h

Tests required (S2-2.T1, T2):
  T1 — pure gap without volume rejected
  T2 — ORB break requires CLOSE above/below, not just touch
"""
from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from strategies.s2_orb.opening_range import OpeningRange


@dataclass
class S2SetupContext:
    """All inputs the S2 setup gate needs."""
    ticker: str
    last_close: float
    last_bar_volume: float
    avg_minute_volume_30bar: float
    gap_pct: float                    # (open - prior_close) / prior_close
    pre_market_volume_ratio: float    # premkt vol / 5d avg premkt vol
    opening_range: OpeningRange
    earnings_blackout: bool


def is_s2_setup(ctx: S2SetupContext, cfg: dict) -> tuple[bool, Optional[str], Optional[str]]:
    """S2-2. Returns (eligible, direction, reason_if_not).

    direction is 'long' if break above OR_high, 'short' if below OR_low.
    """
    s2 = cfg["s2"]

    if ctx.earnings_blackout:
        return False, None, "earnings_blackout"

    # T1: pure gap without volume rejected
    if ctx.gap_pct < s2["gap_pct_min"] and ctx.pre_market_volume_ratio < s2["pre_market_volume_ratio_min"]:
        return False, None, "no_gap_or_volume"

    # T2: ORB break requires CLOSE not just touch
    if ctx.last_close > ctx.opening_range.high:
        direction = "long"
    elif ctx.last_close < ctx.opening_range.low:
        direction = "short"
    else:
        return False, None, "no_or_break_yet"

    # Volume confirmation at breakout
    vol_ratio = (
        ctx.last_bar_volume / ctx.avg_minute_volume_30bar
        if ctx.avg_minute_volume_30bar > 0 else 0
    )
    if vol_ratio < s2["breakout_volume_ratio_min"]:
        return False, direction, "breakout_volume_too_low"

    return True, direction, None
