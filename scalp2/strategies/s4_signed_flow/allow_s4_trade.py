"""strategies/s4_signed_flow/allow_s4_trade.py — S4 canonical gate.

S4 fires on either IGNITION or CONTINUATION matching flow direction.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from features.datatypes import Decision, Features, ScalpState, TRADE, PASS
from strategies.s4_signed_flow.setup import S4SetupContext, is_s4_setup
from strategies.s5_gamma_reversal.fake_breakout import fake_breakout_reject


PASS_REASONS = (
    "earnings_blackout",
    "flow_score_too_weak",
    "flow_price_mismatch",
    "iv_too_extreme",
    "too_close_to_gamma",
    "rvol_too_low",
    "scalp_state_contradicts_flow",
    "wick_excessive",
    "aggressor_collapse",
    "flow_contradicts",
    "risk_denied",
)


# Allowed scalp states by direction
_LONG_STATES = ("SURGE_IGNITION", "SURGE_CONTINUATION")
_SHORT_STATES = ("TANK_IGNITION", "TANK_CONTINUATION")


def allow_s4_trade(*,
                    setup_ctx: S4SetupContext,
                    features: Features,
                    scalp_state: ScalpState,
                    cfg: dict,
                    risk_manager_allows: bool,
                    candidate_id: str = "") -> Decision:
    """S4 canonical gate — flow + scalp confirmation."""
    eligible, direction, reason = is_s4_setup(setup_ctx, cfg)
    if not eligible:
        return PASS(reason or "setup_failed", strategy="S4", candidate_id=candidate_id)

    expected_states = _LONG_STATES if direction == "long" else _SHORT_STATES
    if scalp_state.name not in expected_states:
        return PASS("scalp_state_contradicts_flow",
                     strategy="S4", candidate_id=candidate_id)

    # Reuse S5 fake_breakout filter
    rejected, fb_reason = fake_breakout_reject(features, direction, cfg)
    if rejected:
        return PASS(fb_reason or "fake_breakout",
                     strategy="S4", candidate_id=candidate_id)

    if not risk_manager_allows:
        return PASS("risk_denied", strategy="S4", candidate_id=candidate_id)

    return TRADE("S4", candidate_id=candidate_id)
