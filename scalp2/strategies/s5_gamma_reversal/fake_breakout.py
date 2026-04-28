"""
strategies/s5_gamma_reversal/fake_breakout.py — Fake-breakout reject layer. Spec SWING-4.

Filters wick-driven false signals. Three checks:
  Wick:               upper_wick/range > 0.40 (long) or lower_wick/range > 0.40 (short)
  Aggressor collapse: aggressor_pct < 0.45 (long) or > 0.55 (short)
  Flow contradicts:   net_signed_premium 5m > $300K opposite

Tests required (SWING-4.T1, T2):
  - Wick rule rejects upper-heavy bar on long
  - Flow-contradicts rejects opposing flow

TODO Sprint 7.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from features.types import Features


def fake_breakout_reject(f: Features,
                         direction: Literal["long", "short"],
                         cfg: dict) -> tuple[bool, Optional[str]]:
    """SWING-4. Returns (should_reject, reason_if_yes).

    Reason strings: 'wick_excessive' | 'aggressor_collapse' | 'flow_contradicts'
    """
    raise NotImplementedError("SWING-4 — TODO Sprint 7")
