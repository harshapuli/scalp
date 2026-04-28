"""
strategies/s5_gamma_reversal/trigger.py — S5 trigger with pierce check. Spec SWING-3 + v2.2 §15.

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

TODO Sprint 7.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from features.types import Candidate, ScalpState


def s5_trigger(pre_staged: Candidate,
               scalp_state: ScalpState,
               recent_bars: list,            # list[Bar]
               cfg: dict) -> tuple[bool, Optional[str]]:
    """SWING-3 + spec §10.5. Returns (should_trigger, reason_if_not)."""
    raise NotImplementedError("SWING-3 — TODO Sprint 7")
