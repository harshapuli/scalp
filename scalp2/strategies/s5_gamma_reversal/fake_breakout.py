"""strategies/s5_gamma_reversal/fake_breakout.py — Fake-breakout reject layer. SWING-4.

Filters wick-driven false signals. Three checks:
  Wick:               upper_wick/range > 0.40 (long) or lower_wick/range > 0.40 (short)
  Aggressor collapse: aggressor_pct < 0.45 (long) or > 0.55 (short)
  Flow contradicts:   net_signed_premium 5m > $300K opposite

Tests required (SWING-4.T1, T2):
  - Wick rule rejects upper-heavy bar on long
  - Flow-contradicts rejects opposing flow
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Literal, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from features.datatypes import Features


def fake_breakout_reject(f: Features,
                         direction: Literal["long", "short"],
                         cfg: dict) -> tuple[bool, Optional[str]]:
    """SWING-4. Returns (should_reject, reason_if_yes).

    Reasons: 'wick_excessive' | 'aggressor_collapse' | 'flow_contradicts'
    """
    fb = cfg["s5"]["fake_breakout"]

    # Wick check
    if direction == "long":
        if f.upper_wick_pct > fb["wick_pct_max"]:
            return True, "wick_excessive"
        if f.aggressor_recent < fb["aggressor_collapse_long"]:
            return True, "aggressor_collapse"
        if f.net_signed_premium_5m < -fb["flow_contradicts_dollars"]:
            return True, "flow_contradicts"
    else:  # short
        if f.lower_wick_pct > fb["wick_pct_max"]:
            return True, "wick_excessive"
        if f.aggressor_recent > fb["aggressor_collapse_short"]:
            return True, "aggressor_collapse"
        if f.net_signed_premium_5m > fb["flow_contradicts_dollars"]:
            return True, "flow_contradicts"

    return False, None
