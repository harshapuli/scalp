"""swing_brain/lifecycle.py — 7-stage S5 lifecycle state machine. Spec SWING-1.

States: INDUCEMENT → SETUP → TRIGGER → ENTRY → MANAGE → EXIT → JOURNAL

Tests required (SWING-1.T1, T2):
  T1 — state machine refuses invalid transitions (e.g. SETUP → MANAGE)
  T2 — every transition logged to candidate.transition_log
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import Candidate, StateTransition


CandidateState = Literal["INDUCEMENT", "SETUP", "TRIGGER", "ENTRY", "MANAGE", "EXIT", "JOURNAL"]


# Allowed forward transitions (per spec §5.1 SWING-1 acceptance)
ALLOWED_TRANSITIONS = {
    "INDUCEMENT": ["SETUP"],
    "SETUP": ["TRIGGER", "EXIT"],         # EXIT = candidate expired before trigger
    "TRIGGER": ["ENTRY", "EXIT"],         # EXIT = pierce check failed / risk denied
    "ENTRY": ["MANAGE", "EXIT"],          # EXIT = order rejected / unfilled
    "MANAGE": ["EXIT"],                   # always closes via EXIT
    "EXIT": ["JOURNAL"],
    "JOURNAL": [],                         # terminal
}


class InvalidTransitionError(Exception):
    """SWING-1.T1 — raised when a state transition is not allowed."""


def transition(candidate: Candidate, to_state: CandidateState,
               triggered_by: str,
               triggering_data: Optional[dict] = None,
               features_hash: str = "") -> Candidate:
    """SWING-1. Apply a state transition with validation + logging.

    SWING-1.T1: refuses invalid transitions (raises InvalidTransitionError).
    SWING-1.T2: appends StateTransition to candidate.transition_log.
    """
    from_state = candidate.state
    allowed = ALLOWED_TRANSITIONS.get(from_state, [])
    if to_state not in allowed:
        raise InvalidTransitionError(
            f"Invalid transition {from_state} → {to_state} for candidate "
            f"{candidate.id} ({candidate.strategy}/{candidate.ticker}). "
            f"Allowed from {from_state}: {allowed}"
        )

    # Append transition to log
    transition_record = StateTransition(
        from_state=from_state,
        to_state=to_state,
        transitioned_at=datetime.now(tz=timezone.utc),
        triggered_by=triggered_by,
        triggering_data=triggering_data or {},
        features_hash_at_transition=features_hash,
    )
    candidate.transition_log.append(transition_record)
    candidate.predecessor_states.append(from_state)
    candidate.state = to_state
    # Reset age_in_state by setting created reference forward
    from datetime import timedelta
    candidate.age_in_state = timedelta(0)
    return candidate


def can_transition(from_state: CandidateState, to_state: CandidateState) -> bool:
    """Pure predicate — returns True iff the transition is allowed."""
    return to_state in ALLOWED_TRANSITIONS.get(from_state, [])
