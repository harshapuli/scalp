"""scalp_brain/scores.py — Scoring formulas for the 4 active state classes.

Per spec §B4 + §10.5. Initial weights from config/thresholds.yaml.

  reversal_score(f, direction)      — SCALP-10  (TANK_REVERSE / SURGE_REVERSE)
  ignition_score(f, direction)      — SCALP-11  (TANK_IGNITION / SURGE_IGNITION)
  continuation_score(f, direction)  — SCALP-11  (TANK_CONTINUATION / SURGE_CONTINUATION)
  climax_score(f, direction)        — SCALP-11  (TANK_CLIMAX / SURGE_CLIMAX)

All scores in [0, 1]. Computed at bar close only (SCALP-1.T2 — no look-ahead).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import Features

Direction = Literal["long", "short"]


def clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def reversal_score(f: Features, direction: Direction, cfg: dict) -> float:
    """Spec §10.5 reference implementation.

    Long direction = TANK_REVERSE (bottom reversal forming).
    Short direction = SURGE_REVERSE (top reversal forming).
    """
    w = cfg["scalp"]["reversal_score"]
    if direction == "short":  # SURGE_REVERSE
        failed_ext = clamp01(-f.failed_extension_atr / 1.0)
        wick = clamp01(f.upper_wick_pct / 0.5)
        agg_flip = clamp01((f.aggressor_prior - f.aggressor_recent) / 1.0)
        vol_div = clamp01(1 - f.volume_divergence_ratio)
        flow_flip = 1.0 if f.flow_flip and f.net_signed_premium_5m < 0 else 0.0
    else:  # long = TANK_REVERSE
        failed_ext = clamp01(f.failed_extension_atr / 1.0)
        wick = clamp01(f.lower_wick_pct / 0.5)
        agg_flip = clamp01((f.aggressor_recent - f.aggressor_prior) / 1.0)
        vol_div = clamp01(1 - f.volume_divergence_ratio)
        flow_flip = 1.0 if f.flow_flip and f.net_signed_premium_5m > 0 else 0.0
    score = (
        w["weight_failed_extension"] * failed_ext
        + w["weight_wick"] * wick
        + w["weight_aggressor_flip"] * agg_flip
        + w["weight_volume_divergence"] * vol_div
        + w["weight_flow_flip"] * flow_flip
    )
    return clamp01(score)


def ignition_score(f: Features, direction: Direction, cfg: dict) -> float:
    """SCALP-11. Per §B4.1: first push from base.

    Components: extension speed, volume surge, aggressor strength, flow alignment.
    Long = SURGE_IGNITION (extension upward); short = TANK_IGNITION (downward).
    """
    w = cfg["scalp"]["ignition_score"]
    if direction == "long":
        ext = clamp01(f.extension_from_prior_close_atr / 1.5)
        agg = clamp01(f.aggressor_recent / 1.0)            # +1 = aggressive buying
        flow = clamp01(f.net_signed_premium_5m / 500_000)
    else:
        ext = clamp01(-f.extension_from_prior_close_atr / 1.5)
        agg = clamp01(-f.aggressor_recent / 1.0)
        flow = clamp01(-f.net_signed_premium_5m / 500_000)
    vol = clamp01(f.climax_vol_ratio / 2.0)
    score = (
        w["weight_extension"] * ext
        + w["weight_volume"] * vol
        + w["weight_aggressor"] * agg
        + w["weight_flow"] * flow
    )
    return clamp01(score)


def continuation_score(f: Features, direction: Direction, cfg: dict) -> float:
    """SCALP-11. Per §B4.2: trend persists.

    Long = SURGE_CONTINUATION (sustained higher highs / steady volume / aligned aggressor).
    """
    w = cfg["scalp"]["continuation_score"]
    if direction == "long":
        # higher_lows proxied by pullback NOT broken + extension positive
        higher_lows = 1.0 if not f.pullback_break and f.extension_from_vwap_atr > 0 else 0.0
        agg_persist = clamp01(f.aggressor_recent / 1.0)
        vwap_dist = clamp01(f.extension_from_vwap_atr / 2.0)
    else:
        higher_lows = 1.0 if not f.pullback_break and f.extension_from_vwap_atr < 0 else 0.0
        agg_persist = clamp01(-f.aggressor_recent / 1.0)
        vwap_dist = clamp01(-f.extension_from_vwap_atr / 2.0)
    vol_steady = clamp01(min(1.5, f.volume_divergence_ratio) / 1.5)
    score = (
        w["weight_higher_lows"] * higher_lows
        + w["weight_aggressor_persist"] * agg_persist
        + w["weight_volume_steady"] * vol_steady
        + w["weight_vwap_distance"] * vwap_dist
    )
    return clamp01(score)


def climax_score(f: Features, direction: Direction, cfg: dict) -> float:
    """SCALP-11. Per §B4.4: parabolic move with declining volume + wick."""
    w = cfg["scalp"]["climax_score"]
    if direction == "long":
        ext = clamp01(f.extension_from_vwap_atr / 3.0)
        agg_decay = clamp01((f.aggressor_prior - f.aggressor_recent) / 1.0)
    else:
        ext = clamp01(-f.extension_from_vwap_atr / 3.0)
        agg_decay = clamp01((f.aggressor_recent - f.aggressor_prior) / 1.0)
    vol_decline = clamp01(1.0 - f.volume_divergence_ratio)
    wick = clamp01(f.wick_pct / 0.5)
    score = (
        w["weight_extreme_extension"] * ext
        + w["weight_volume_decline"] * vol_decline
        + w["weight_wick_pct"] * wick
        + w["weight_aggressor_decay"] * agg_decay
    )
    return clamp01(score)
