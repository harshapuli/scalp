"""
swing_brain/lifecycle.py — 7-stage S5 lifecycle state machine. Spec SWING-1.

States (SWING-1 acceptance):
  INDUCEMENT → SETUP → TRIGGER → ENTRY → MANAGE → EXIT → JOURNAL

Tests required (SWING-1.T1, T2):
  - state machine refuses invalid transitions (e.g. SETUP → MANAGE skipping TRIGGER+ENTRY)
  - every transition logged to candidate_transitions Postgres table

The 7 stages and their entry conditions (per v2.2):
  INDUCEMENT — Multi-day GEX strike accumulation pattern detected
  SETUP      — All v2.2 §14 conditions met (near_gex, extended, at_liquidity, no_event,
               gex_strike_ok, gex_fresh)
  TRIGGER    — Scalp brain published TANK_REVERSE/SURGE_REVERSE matching pre-staged direction
  ENTRY      — allow_s5_trade returned TRADE; order submitted to Alpaca
  MANAGE     — Position open; monitor for stop/target/IV crush/timeout
  EXIT       — Position closed (any reason)
  JOURNAL    — Trade record persisted to trades table; transition log finalized

TODO Sprint 6-7 (SWING-E1).
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.types import Candidate, StateTransition


CandidateState = Literal["INDUCEMENT", "SETUP", "TRIGGER", "ENTRY", "MANAGE", "EXIT", "JOURNAL"]


# Allowed forward transitions
ALLOWED_TRANSITIONS = {
    "INDUCEMENT": ["SETUP"],
    "SETUP": ["TRIGGER", "EXIT"],         # EXIT = candidate expired
    "TRIGGER": ["ENTRY", "EXIT"],         # EXIT = pierce check failed / risk denied
    "ENTRY": ["MANAGE", "EXIT"],          # EXIT = order rejected / unfilled
    "MANAGE": ["EXIT"],                   # always closes via EXIT
    "EXIT": ["JOURNAL"],
    "JOURNAL": [],                         # terminal
}


class InvalidTransitionError(Exception):
    pass


def transition(candidate: Candidate, to_state: CandidateState,
               triggered_by: str, triggering_data: dict = None) -> Candidate:
    """SWING-1. Apply a state transition with validation + logging.

    SWING-1.T1: refuses invalid transitions (raises InvalidTransitionError).
    SWING-1.T2: appends StateTransition to candidate.transition_log.
    """
    raise NotImplementedError("SWING-1 — TODO Sprint 6")
