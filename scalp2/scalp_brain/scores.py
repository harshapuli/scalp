"""
scalp_brain/scores.py — Scoring formulas for the 4 active state classes.

Per spec §B4 + §10.5. Initial weights from config/thresholds.yaml; refined
via SCALP-10.3 backtest grid search.

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
from features.types import Features

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
    """SCALP-11. TODO Sprint 5."""
    raise NotImplementedError("SCALP-11 — first push from base, weights per §B4.1")


def continuation_score(f: Features, direction: Direction, cfg: dict) -> float:
    """SCALP-11. TODO Sprint 5."""
    raise NotImplementedError("SCALP-11 — higher-lows, weights per §B4.2")


def climax_score(f: Features, direction: Direction, cfg: dict) -> float:
    """SCALP-11. TODO Sprint 5."""
    raise NotImplementedError("SCALP-11 — extreme extension, declining vol, weights per §B4.4")


if __name__ == "__main__":
    print("[scalp_brain.scores] reversal_score implemented (§10.5 reference)")
    print("[scalp_brain.scores] ignition/continuation/climax — TODO Sprint 5 (SCALP-11)")
