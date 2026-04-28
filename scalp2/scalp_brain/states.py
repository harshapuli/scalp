"""
scalp_brain/states.py — 7-state classifier enum + transition rules. Spec SCALP-2.

States per v2.2 §B4:
  NEUTRAL
  SURGE_IGNITION    — long ignition into resistance
  SURGE_CONTINUATION — long continuation up
  SURGE_CLIMAX      — exhaustion at top
  SURGE_REVERSE     — top reversal forming  (← S5 short trigger)
  TANK_IGNITION     — short ignition into support
  TANK_CONTINUATION — short continuation down
  TANK_CLIMAX       — exhaustion at bottom
  TANK_REVERSE      — bottom reversal forming  (← S5 long trigger)

Transition rules (SCALP-2.1, SCALP-2.2):
  - NEUTRAL → IGNITION when extension threshold crossed
  - IGNITION → CONTINUATION when sustained
  - CONTINUATION → CLIMAX when volume declines on extension
  - CLIMAX → REVERSE when reversal_score crosses threshold
  - SURGE_REVERSE requires prior CONTINUATION or CLIMAX (cannot jump from NEUTRAL)
  - States persist for state_min_age_bars (hysteresis, default 2)
"""
from __future__ import annotations

from enum import Enum


class ScalpStateName(str, Enum):
    NEUTRAL = "NEUTRAL"
    SURGE_IGNITION = "SURGE_IGNITION"
    SURGE_CONTINUATION = "SURGE_CONTINUATION"
    SURGE_CLIMAX = "SURGE_CLIMAX"
    SURGE_REVERSE = "SURGE_REVERSE"
    TANK_IGNITION = "TANK_IGNITION"
    TANK_CONTINUATION = "TANK_CONTINUATION"
    TANK_CLIMAX = "TANK_CLIMAX"
    TANK_REVERSE = "TANK_REVERSE"


# Allowed transitions per SCALP-2 acceptance criteria
ALLOWED_TRANSITIONS = {
    ScalpStateName.NEUTRAL: [ScalpStateName.SURGE_IGNITION, ScalpStateName.TANK_IGNITION],
    ScalpStateName.SURGE_IGNITION: [ScalpStateName.SURGE_CONTINUATION, ScalpStateName.NEUTRAL],
    ScalpStateName.SURGE_CONTINUATION: [ScalpStateName.SURGE_CLIMAX, ScalpStateName.SURGE_REVERSE, ScalpStateName.NEUTRAL],
    ScalpStateName.SURGE_CLIMAX: [ScalpStateName.SURGE_REVERSE, ScalpStateName.NEUTRAL],
    ScalpStateName.SURGE_REVERSE: [ScalpStateName.NEUTRAL, ScalpStateName.TANK_IGNITION],
    ScalpStateName.TANK_IGNITION: [ScalpStateName.TANK_CONTINUATION, ScalpStateName.NEUTRAL],
    ScalpStateName.TANK_CONTINUATION: [ScalpStateName.TANK_CLIMAX, ScalpStateName.TANK_REVERSE, ScalpStateName.NEUTRAL],
    ScalpStateName.TANK_CLIMAX: [ScalpStateName.TANK_REVERSE, ScalpStateName.NEUTRAL],
    ScalpStateName.TANK_REVERSE: [ScalpStateName.NEUTRAL, ScalpStateName.SURGE_IGNITION],
}


def is_valid_transition(from_state: ScalpStateName, to_state: ScalpStateName) -> bool:
    """SCALP-2.T2 — SURGE_REVERSE requires CONTINUATION or CLIMAX prior."""
    if from_state == to_state:
        return True
    return to_state in ALLOWED_TRANSITIONS.get(from_state, [])


if __name__ == "__main__":
    print(f"[scalp_brain.states] {len(ScalpStateName)} states declared per v2.2 §B4")
    for s in ScalpStateName:
        print(f"  · {s.value}")
