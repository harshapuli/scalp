"""
scalp_brain/classifier.py — Per-bar 7-state classifier with hysteresis.

Spec SCALP-2. Inputs: prior_state + Features. Output: new_state.

Tests required (SCALP-2.T1, T2, T3):
  - NEUTRAL → SURGE_IGNITION when extension_from_vwap_atr crosses threshold
  - SURGE_REVERSE requires CONTINUATION or CLIMAX prior (not direct from NEUTRAL)
  - State persists for state_min_age_bars (hysteresis prevents flip-flop)

TODO Sprint 4: implement transition rules per §B4.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.types import Features, ScalpState
from scalp_brain.states import ScalpStateName, is_valid_transition


def classify(prior_state: Optional[ScalpState],
             features: Features,
             cfg: dict) -> ScalpState:
    """SCALP-2. Returns new ScalpState given prior state + bar features."""
    raise NotImplementedError("SCALP-2 — TODO Sprint 4")
