"""
strategies/s5_gamma_reversal/allow_s5_trade.py — S5-104 canonical gate.

Per spec §6.9 (S5-E11) and §10.5 pseudocode. The ONLY path to S5 order
submission. Sequential gate evaluation; first failure short-circuits with
a typed Decision(decision='PASS', pass_reason=...).

Gate ordering (locked per spec §6.9 acceptance criteria):
  1. is_s5_setup
  2. reversal_score >= required
  3. gamma_distance_atr <= 0.50
  4. event_blackout
  5. gex_stale
  6. bid_ask_spread_pct <= 0.05
  7. option_volume >= min_option_volume
  8. ml_prob >= threshold
  9. expected_value_net > 0
  10. risk_manager_allows

Tests required (S5-104.T1, S5-104.T2):
  - Each gate has unique pass_reason
  - This is the ONLY path to alpaca.submit_order() for S5

NOTE: replaces inferred scaffold formerly at scalp2/src/phase_gate.py.
The earlier 4-state machine (OBSERVE/PAPER/LIVE_SMALL/LIVE_FULL) was wrong —
that's the *trading mode* (in risk/sizing.py::TradingMode). The phase-gate
concept in the spec is the gate-chain below, not a state machine.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


# Import canonical types
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from features.datatypes import Features, Decision, TRADE, PASS


# ──────────────────────────────────────────────────────────────────────────────
# ML model protocol — what allow_s5_trade expects from the ML layer
# ──────────────────────────────────────────────────────────────────────────────


class MLModel(Protocol):
    """Minimal interface for the ML model that scores ml_prob.

    Any concrete model (sklearn LR/LightGBM, etc.) must implement this.
    """

    def predict_proba(self, ml_vector) -> list[list[float]]:
        """Returns [[prob_loss, prob_win]] for one-row input."""
        ...


# ──────────────────────────────────────────────────────────────────────────────
# Helper: extract ml_vector from Features (currently a stub — M2 wires this)
# ──────────────────────────────────────────────────────────────────────────────


def features_to_ml_vector(f: Features):
    """Convert a Features dataclass into the model's input vector.

    TODO M2: define exact column order matching trainer.py.
    """
    raise NotImplementedError("M2 — wire features → ml_vector")


# ──────────────────────────────────────────────────────────────────────────────
# Per-gate predicates — each named for its pass_reason
# ──────────────────────────────────────────────────────────────────────────────


def _is_s5_setup_check(f: Features, cfg: dict) -> Optional[str]:
    """Returns pass_reason if setup fails, None if passes (gate 1)."""
    s = cfg["s5"]["setup"]
    if f.distance_to_major_pos_gex_atr > s["near_gex_atr"]:
        return "not_near_gex"
    extended = (f.extension_from_vwap_atr >= s["extended_atr_min"]
                or f.extension_from_prior_close_atr >= s["extended_atr_min"])
    if not extended:
        return "not_extended"
    if f.near_htf_level_atr > s["at_liquidity_atr"]:
        return "not_at_liquidity"
    if f.earnings_blackout or f.fomc_blackout:
        return "event_blackout"
    if f.major_gex_strike_dte < s["gex_strike_dte_min"]:
        return "gex_strike_too_close_to_expiry"
    if f.gex_snapshot_age_min > s["gex_snapshot_max_age_min"]:
        return "gex_stale"
    return None


# ──────────────────────────────────────────────────────────────────────────────
# Public entry point — S5-104
# ──────────────────────────────────────────────────────────────────────────────


def allow_s5_trade(features: Features,
                   model: Optional[MLModel],
                   threshold: float,
                   cfg: dict,
                   risk_manager_allows: bool,
                   expected_value_net: Optional[float] = None,
                   required_reversal_score: float = 0.50,
                   reversal_score: float = 0.0,
                   candidate_id: str = "") -> Decision:
    """Sequential gate evaluation. First failure short-circuits with PASS.

    Per spec §10.5 pseudocode. Gate ordering is LOCKED.

    Returns:
        Decision(decision='TRADE', strategy='S5', ...) iff every gate passes
        Decision(decision='PASS', pass_reason='<unique-per-gate>', ...) otherwise
    """
    # Gate 1: is_s5_setup
    setup_fail = _is_s5_setup_check(features, cfg)
    if setup_fail is not None:
        return PASS(setup_fail, candidate_id=candidate_id)

    # Gate 2: reversal_score >= required
    if reversal_score < required_reversal_score:
        return PASS("reversal_score_too_low", candidate_id=candidate_id)

    # Gate 3: gamma_distance_atr <= 0.50  (already partly checked in setup; redundant safety)
    if features.distance_to_major_pos_gex_atr > cfg["s5"]["setup"]["near_gex_atr"]:
        return PASS("not_near_gamma_strike", candidate_id=candidate_id)

    # Gate 4: event_blackout (also in setup; redundant safety)
    if features.earnings_blackout or features.fomc_blackout:
        return PASS("event_blackout", candidate_id=candidate_id)

    # Gate 5: gex_stale (also in setup; redundant safety)
    if features.gex_snapshot_age_min > cfg["s5"]["setup"]["gex_snapshot_max_age_min"]:
        return PASS("gex_stale", candidate_id=candidate_id)

    # Gate 6: bid_ask_spread_pct <= 0.05
    if features.bid_ask_spread_pct > cfg["s5"]["risk"]["bid_ask_spread_pct_max"]:
        return PASS("option_spread_too_wide", candidate_id=candidate_id)

    # Gate 7: option_volume >= min_option_volume
    if features.option_volume_5d_avg < cfg["s5"]["risk"]["option_volume_5d_min"]:
        return PASS("option_liquidity_too_low", candidate_id=candidate_id)

    # Gate 8: ml_prob >= threshold
    ml_prob = None
    if model is not None:
        try:
            vec = features_to_ml_vector(features)
            ml_prob = model.predict_proba(vec)[0][1]
            if ml_prob < threshold:
                return PASS("ml_probability_below_threshold",
                            ml_prob=ml_prob, ml_threshold_used=threshold,
                            candidate_id=candidate_id)
        except NotImplementedError:
            # M2 not yet wired — degrade gracefully to rules-only
            ml_prob = None

    # Gate 9: expected_value_net > 0
    if expected_value_net is not None and expected_value_net <= 0:
        return PASS("negative_ev",
                    expected_value_net=expected_value_net,
                    ml_prob=ml_prob, ml_threshold_used=threshold,
                    candidate_id=candidate_id)

    # Gate 10: risk_manager_allows
    if not risk_manager_allows:
        return PASS("risk_denied",
                    ml_prob=ml_prob, ml_threshold_used=threshold,
                    expected_value_net=expected_value_net,
                    candidate_id=candidate_id)

    # All gates passed
    return TRADE(
        "S5",
        ml_prob=ml_prob,
        ml_threshold_used=threshold,
        expected_value_net=expected_value_net,
        candidate_id=candidate_id,
    )


# ──────────────────────────────────────────────────────────────────────────────
# All pass_reasons — used for static-analysis test (S5-104.T1)
# ──────────────────────────────────────────────────────────────────────────────


PASS_REASONS = (
    # Setup-failure reasons (Gate 1 sub-checks)
    "not_near_gex",
    "not_extended",
    "not_at_liquidity",
    "event_blackout",
    "gex_strike_too_close_to_expiry",
    "gex_stale",
    # Standalone gate reasons (2-10)
    "reversal_score_too_low",
    "not_near_gamma_strike",
    "option_spread_too_wide",
    "option_liquidity_too_low",
    "ml_probability_below_threshold",
    "negative_ev",
    "risk_denied",
)


if __name__ == "__main__":
    print(f"[allow_s5_trade] gate ordering locked per spec §10.5 pseudocode")
    print(f"[allow_s5_trade] {len(PASS_REASONS)} unique pass_reasons declared:")
    for r in PASS_REASONS:
        print(f"  · {r}")
