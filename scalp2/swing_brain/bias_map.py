"""
swing_brain/bias_map.py — Daily Conviction-style bias map. Spec SWING-12.

Per-ticker daily bias: BULLISH / BEARISH / NEUTRAL. Computed once per day at
market open from previous-day GEX, flow, term structure (logistic combine).

Stored in Redis: bias_map:{ticker} with 1-day TTL (per spec §10.6).

S5 candidates check bias as SOFT filter (allow but flag anti-bias trades).

TODO Sprint 8.
"""
from __future__ import annotations

from typing import Literal

Bias = Literal["BULLISH", "BEARISH", "NEUTRAL"]


def compute_daily_bias(ticker: str, prior_day_data: dict) -> Bias:
    """SWING-12.1. From prior-day net call/put flow, GEX direction, term contango."""
    raise NotImplementedError("SWING-12 — TODO Sprint 8")
