"""strategies/s5_gamma_reversal/trigger.py — S5 trigger with pierce check. SWING-3 + v2.2 §15.

After scalp.price_trigger arrives, check if price already pierced the gamma strike
in recent_bars. If pierced, invalidate the candidate (gamma sign already flipped
locally — too late to fade).

  Long setup:  invalidate if recent_low  < gamma_strike - 0.25×ATR
  Short setup: invalidate if recent_high > gamma_strike + 0.25×ATR

Confirmation candle must be CLOSED (not forming).

Tests required (SWING-3.T1):
  - pierce check invalidates short setup (gamma_strike=$890, ATR=$2,
    recent_high=$890.60 → False with 'gamma_pierced')

Canonical reference: spec §10.5 `s5_trigger` pseudocode.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from features.datatypes import Candidate, ScalpState


def s5_trigger(pre_staged: Candidate,
               scalp_state: ScalpState,
               recent_bars: list,                  # list of bar-shaped objects with .high/.low
               cfg: dict) -> tuple[bool, Optional[str]]:
    """SWING-3 + spec §10.5. Returns (should_trigger, reason_if_not).

    Args:
      pre_staged: Candidate currently in SETUP, with gamma_strike + atr + direction
      scalp_state: most recent ScalpState from scalp_brain.classifier
      recent_bars: bars between SETUP creation and now
      cfg: project config (uses cfg['s5']['trigger'])
    """
    if pre_staged.gamma_strike is None or pre_staged.atr <= 0:
        return False, "no_gamma_strike_or_atr"

    buf = cfg["s5"]["trigger"]["pierce_buffer_atr"] * pre_staged.atr

    # Pierce check
    if pre_staged.direction == "short":
        # SHORT setup invalidated if any recent high pushed above gamma + buffer
        if recent_bars:
            highest = max(b.high for b in recent_bars)
            if highest > pre_staged.gamma_strike + buf:
                return False, "gamma_pierced"
        # Trigger condition: SURGE_REVERSE with sufficient score
        if scalp_state.name != "SURGE_REVERSE":
            return False, "scalp_state_not_reversing"
        if scalp_state.score < cfg["s5"]["trigger"]["surge_reverse_score_min"]:
            return False, "reversal_score_too_low"
    else:
        # LONG setup invalidated if any recent low pushed below gamma - buffer
        if recent_bars:
            lowest = min(b.low for b in recent_bars)
            if lowest < pre_staged.gamma_strike - buf:
                return False, "gamma_pierced"
        if scalp_state.name != "TANK_REVERSE":
            return False, "scalp_state_not_reversing"
        if scalp_state.score < cfg["s5"]["trigger"]["tank_reverse_score_min"]:
            return False, "reversal_score_too_low"

    return True, None
