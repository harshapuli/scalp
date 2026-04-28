"""swing_brain/bias_map.py — Daily Conviction-style bias map. SWING-12.

Per-ticker daily bias: BULLISH / BEARISH / NEUTRAL. Computed once per day at
market open from previous-day GEX, flow, term structure (logistic combine).

Stored in Redis: bias_map:{ticker} with 1-day TTL (per spec §10.6).

S5 candidates check bias as SOFT filter (allow but flag anti-bias trades).
"""
from __future__ import annotations

import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from data_clients.unusual_whales import UWClient
from infra.redis_client import KEY_BIAS_MAP_FMT


Bias = Literal["BULLISH", "BEARISH", "NEUTRAL"]


def _logistic_score(net_call_ratio: float, gex_direction: float, term_slope: float) -> float:
    """Combine three indicators into a [0, 1] bullish probability via logistic."""
    # Standardize-ish weights (calibrated post-MVP)
    z = (
        2.0 * net_call_ratio          # +1 = call buying dominant
        + 1.5 * gex_direction          # +1 = positive GEX direction
        + 1.0 * term_slope             # +1 = contango (bullish for IV/skew)
    )
    return 1.0 / (1.0 + math.exp(-z))


def compute_daily_bias(net_call_ratio: float,
                        gex_direction: float,
                        term_slope: float,
                        bullish_threshold: float = 0.55,
                        bearish_threshold: float = 0.45) -> Bias:
    """SWING-12.1. Combine signals into BULLISH/BEARISH/NEUTRAL.

    Inputs all normalized to [-1, +1].
    """
    p = _logistic_score(net_call_ratio, gex_direction, term_slope)
    if p >= bullish_threshold:
        return "BULLISH"
    if p <= bearish_threshold:
        return "BEARISH"
    return "NEUTRAL"


def _net_call_ratio_from_flow(records: list) -> float:
    """Net call premium share, scaled to [-1, +1]."""
    if not records:
        return 0.0
    call_buy = sum(r.premium for r in records if r.side == "call" and r.action == "buy")
    put_buy = sum(r.premium for r in records if r.side == "put" and r.action == "buy")
    total = call_buy + put_buy
    if total <= 0:
        return 0.0
    return (call_buy - put_buy) / total


def _gex_direction_from_snapshot(snap) -> float:
    """spot vs gamma_flip → +1 if above (positive-gamma regime), -1 if below."""
    if not snap or snap.gamma_flip == 0:
        return 0.0
    return 1.0 if snap.spot_price >= snap.gamma_flip else -1.0


async def compute_and_store(ticker: str,
                              redis_async_client,
                              uw_client: UWClient,
                              ttl_seconds: int = 86400) -> Bias:
    """Pull yesterday's UW data, compute bias, persist to Redis with 1-day TTL."""
    snap = uw_client.greek_exposure(ticker)
    flow = uw_client.flow_recent(ticker)
    net_calls = _net_call_ratio_from_flow(flow)
    gex_dir = _gex_direction_from_snapshot(snap)
    # Term slope — would need iv_term_structure call; placeholder 0 for MVP
    term_slope = 0.0

    bias = compute_daily_bias(net_calls, gex_dir, term_slope)
    payload = json.dumps({
        "bias": bias,
        "net_call_ratio": net_calls,
        "gex_direction": gex_dir,
        "term_slope": term_slope,
        "computed_at": datetime.now(tz=timezone.utc).isoformat(),
    })
    await redis_async_client.set(
        KEY_BIAS_MAP_FMT.format(ticker=ticker),
        payload, ex=ttl_seconds,
    )
    return bias


if __name__ == "__main__":
    # Smoke
    bias = compute_daily_bias(net_call_ratio=0.4, gex_direction=1.0, term_slope=0.2)
    print(f"[bias] strong call buying + positive GEX → {bias}")
    assert bias == "BULLISH"

    bias2 = compute_daily_bias(net_call_ratio=-0.4, gex_direction=-1.0, term_slope=-0.2)
    print(f"[bias] strong put buying + negative GEX → {bias2}")
    assert bias2 == "BEARISH"

    bias3 = compute_daily_bias(net_call_ratio=0.05, gex_direction=0, term_slope=0)
    print(f"[bias] balanced → {bias3}")
    assert bias3 == "NEUTRAL"
    print("[bias] OK")
