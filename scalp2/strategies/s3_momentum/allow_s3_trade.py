"""strategies/s3_momentum/allow_s3_trade.py — S3 canonical gate.

Mirrors S5/S2 pattern. S3 fires on continuation states (not reversals).

Tests required (S3-3.T1):
  - High reversal_score rejects continuation entry (mixed signals = pass)
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from features.datatypes import Decision, Features, ScalpState, TRADE, PASS
from strategies.s3_momentum.setup import S3SetupContext, is_s3_setup


PASS_REASONS = (
    "no_session_trend",
    "vwap_distance_too_low",
    "structure_too_choppy",
    "aggressor_too_weak",
    "too_close_to_liquidity",
    "too_close_to_gamma",
    "scalp_state_not_continuation",
    "continuation_score_too_low",
    "mixed_signals",
    "risk_denied",
)


def allow_s3_trade(*,
                    setup_ctx: S3SetupContext,
                    features: Features,
                    scalp_state: ScalpState,
                    reversal_score: float,
                    cfg: dict,
                    risk_manager_allows: bool,
                    candidate_id: str = "") -> Decision:
    """S3 canonical gate."""
    s3 = cfg["s3"]

    # Gate 1: setup
    eligible, direction, reason = is_s3_setup(setup_ctx, cfg)
    if not eligible:
        return PASS(reason or "setup_failed", strategy="S3", candidate_id=candidate_id)

    # Gate 2: scalp state must be CONTINUATION in matching direction
    expected_state = "SURGE_CONTINUATION" if direction == "long" else "TANK_CONTINUATION"
    if scalp_state.name != expected_state:
        return PASS("scalp_state_not_continuation",
                     strategy="S3", candidate_id=candidate_id)

    # Gate 3: continuation_score >= threshold
    if scalp_state.score < s3["continuation_score_min"]:
        return PASS("continuation_score_too_low",
                     strategy="S3", candidate_id=candidate_id)

    # Gate 4: S3-3.T1 — high reversal_score = mixed signals, pass
    if reversal_score > 0.45:
        return PASS("mixed_signals", strategy="S3", candidate_id=candidate_id)

    # Gate 5: risk
    if not risk_manager_allows:
        return PASS("risk_denied", strategy="S3", candidate_id=candidate_id)

    return TRADE("S3", candidate_id=candidate_id)
