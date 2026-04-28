"""strategies/s2_orb/allow_s2_trade.py — S2 canonical gate.

Mirrors S5-104's pattern. Sequential gate evaluation; first failure short-circuits.

Gate ordering:
  1. is_s2_setup
  2. scalp_state must be SURGE_IGNITION (long) or TANK_IGNITION (short)
  3. ignition_score >= 0.50
  4. fake_breakout_reject does not trigger
  5. risk_manager_allows
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from features.datatypes import Decision, Features, ScalpState, TRADE, PASS
from strategies.s2_orb.setup import S2SetupContext, is_s2_setup
from strategies.s5_gamma_reversal.fake_breakout import fake_breakout_reject


PASS_REASONS = (
    "earnings_blackout",
    "no_gap_or_volume",
    "no_or_break_yet",
    "breakout_volume_too_low",
    "scalp_state_not_ignition",
    "ignition_score_too_low",
    "wick_excessive",
    "aggressor_collapse",
    "flow_contradicts",
    "risk_denied",
)


def allow_s2_trade(*,
                    setup_ctx: S2SetupContext,
                    features: Features,
                    scalp_state: ScalpState,
                    cfg: dict,
                    risk_manager_allows: bool,
                    candidate_id: str = "") -> Decision:
    """S2 canonical gate — same shape as S5-104."""
    s2 = cfg["s2"]

    # Gate 1: setup
    eligible, direction, reason = is_s2_setup(setup_ctx, cfg)
    if not eligible:
        return PASS(reason or "setup_failed", strategy="S2", candidate_id=candidate_id)

    # Gate 2: scalp state must be IGNITION in matching direction
    expected_state = "SURGE_IGNITION" if direction == "long" else "TANK_IGNITION"
    if scalp_state.name != expected_state:
        return PASS("scalp_state_not_ignition", strategy="S2", candidate_id=candidate_id)

    # Gate 3: ignition_score >= threshold
    if scalp_state.score < s2["ignition_score_min"]:
        return PASS("ignition_score_too_low", strategy="S2", candidate_id=candidate_id)

    # Gate 4: fake-breakout reject (reuses S5's logic — also valid for S2 ignition)
    rejected, fb_reason = fake_breakout_reject(features, direction, cfg)
    if rejected:
        return PASS(fb_reason or "fake_breakout", strategy="S2", candidate_id=candidate_id)

    # Gate 5: risk manager
    if not risk_manager_allows:
        return PASS("risk_denied", strategy="S2", candidate_id=candidate_id)

    return TRADE("S2", candidate_id=candidate_id)
