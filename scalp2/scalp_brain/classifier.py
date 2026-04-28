"""scalp_brain/classifier.py — Per-bar 7-state classifier with hysteresis.

Spec SCALP-2. Inputs: prior_state + Features + cfg. Output: new_state.

Tests required (SCALP-2.T1, T2, T3):
  - NEUTRAL → SURGE_IGNITION when extension crosses threshold
  - SURGE_REVERSE requires CONTINUATION or CLIMAX prior (not direct from NEUTRAL)
  - State persists for state_min_age_bars (hysteresis prevents flip-flop)
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import Features, ScalpState
from scalp_brain.states import ScalpStateName, is_valid_transition
from scalp_brain.scores import (
    ignition_score, continuation_score, climax_score, reversal_score,
)


def _candidate_states(prior: ScalpStateName) -> list[ScalpStateName]:
    """Return target states reachable from prior, ordered by classification priority.

    Reversals first (most specific), then continuation, then ignition, then climax.
    """
    # Reversals are the most decision-relevant for S5
    rev_first: list[ScalpStateName] = []
    if prior in (ScalpStateName.SURGE_CONTINUATION, ScalpStateName.SURGE_CLIMAX):
        rev_first.append(ScalpStateName.SURGE_REVERSE)
    if prior in (ScalpStateName.TANK_CONTINUATION, ScalpStateName.TANK_CLIMAX):
        rev_first.append(ScalpStateName.TANK_REVERSE)
    # Then climax / continuation / ignition / neutral within trajectory
    if prior in (ScalpStateName.SURGE_CONTINUATION,):
        rev_first.append(ScalpStateName.SURGE_CLIMAX)
    if prior in (ScalpStateName.TANK_CONTINUATION,):
        rev_first.append(ScalpStateName.TANK_CLIMAX)
    if prior in (ScalpStateName.SURGE_IGNITION,):
        rev_first.append(ScalpStateName.SURGE_CONTINUATION)
    if prior in (ScalpStateName.TANK_IGNITION,):
        rev_first.append(ScalpStateName.TANK_CONTINUATION)
    if prior == ScalpStateName.NEUTRAL:
        rev_first.extend([ScalpStateName.SURGE_IGNITION, ScalpStateName.TANK_IGNITION])
    return rev_first


def _score_for_state(state: ScalpStateName, features: Features, cfg: dict) -> float:
    """Pick the right score for the candidate state."""
    if state in (ScalpStateName.SURGE_IGNITION,):
        return ignition_score(features, "long", cfg)
    if state in (ScalpStateName.TANK_IGNITION,):
        return ignition_score(features, "short", cfg)
    if state in (ScalpStateName.SURGE_CONTINUATION,):
        return continuation_score(features, "long", cfg)
    if state in (ScalpStateName.TANK_CONTINUATION,):
        return continuation_score(features, "short", cfg)
    if state in (ScalpStateName.SURGE_CLIMAX,):
        return climax_score(features, "long", cfg)
    if state in (ScalpStateName.TANK_CLIMAX,):
        return climax_score(features, "short", cfg)
    if state == ScalpStateName.SURGE_REVERSE:
        return reversal_score(features, "short", cfg)
    if state == ScalpStateName.TANK_REVERSE:
        return reversal_score(features, "long", cfg)
    return 0.0


def _threshold_for_state(state: ScalpStateName, cfg: dict) -> float:
    """Threshold above which the candidate state is accepted."""
    if state in (ScalpStateName.SURGE_IGNITION, ScalpStateName.TANK_IGNITION):
        return cfg["scalp"]["ignition_score"]["threshold"]
    if state in (ScalpStateName.SURGE_CONTINUATION, ScalpStateName.TANK_CONTINUATION):
        return cfg["scalp"]["continuation_score"]["threshold"]
    if state in (ScalpStateName.SURGE_CLIMAX, ScalpStateName.TANK_CLIMAX):
        return cfg["scalp"]["climax_score"]["threshold"]
    if state == ScalpStateName.SURGE_REVERSE:
        return cfg["scalp"]["reversal_score"]["threshold_short"]
    if state == ScalpStateName.TANK_REVERSE:
        return cfg["scalp"]["reversal_score"]["threshold_long"]
    return 1.0   # never crosses


def classify(prior_state: Optional[ScalpState],
             features: Features,
             cfg: dict) -> ScalpState:
    """SCALP-2. Returns new ScalpState given prior state + bar features.

    Hysteresis: holds prior state for at least cfg.scalp.state_min_age_bars
    bars unless a strictly higher-priority state's score crosses its threshold.
    """
    min_age = cfg.get("scalp", {}).get("state_min_age_bars", 2)
    prior_name = (
        prior_state.name if prior_state else ScalpStateName.NEUTRAL.value
    )
    try:
        prior_enum = ScalpStateName(prior_name)
    except ValueError:
        prior_enum = ScalpStateName.NEUTRAL

    # Build candidate list (only valid transitions)
    candidates = []
    for cand in _candidate_states(prior_enum):
        if is_valid_transition(prior_enum, cand):
            candidates.append(cand)

    # Score each candidate; pick the highest score that crosses its threshold
    best_state = prior_enum
    best_score = _score_for_state(prior_enum, features, cfg) if prior_state else 0.0
    for cand in candidates:
        s = _score_for_state(cand, features, cfg)
        thr = _threshold_for_state(cand, features and cfg)
        if s >= thr and s > best_score:
            best_state = cand
            best_score = s

    # Hysteresis: don't flip away from prior state unless minimum age reached
    if (prior_state is not None
            and best_state != prior_enum
            and prior_state.age_bars < min_age):
        # Only allow flips before min_age if it's a high-conviction reversal
        # (reversals from continuation/climax are the load-bearing transitions)
        if best_state not in (ScalpStateName.SURGE_REVERSE, ScalpStateName.TANK_REVERSE):
            best_state = prior_enum

    is_new = best_state != prior_enum
    return ScalpState(
        name=best_state.value,
        score=best_score,
        timestamp=features.timestamp,
        ticker=features.ticker,
        bar_idx=features.bar_idx,
        entry_bar=features.bar_idx if is_new else (
            prior_state.entry_bar if prior_state else features.bar_idx
        ),
        age_bars=0 if is_new else (prior_state.age_bars + 1 if prior_state else 0),
        score_at_entry=best_score if is_new else (
            prior_state.score_at_entry if prior_state else best_score
        ),
        sequence_number=(
            (prior_state.sequence_number + 1) if (prior_state and is_new)
            else (prior_state.sequence_number if prior_state else 0)
        ),
    )
