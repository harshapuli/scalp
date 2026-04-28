"""strategies/s1_pre_fomc/allow_s1_trade.py — S1 canonical gate.

S1 is unique: NO scalp brain trigger. Pure time-window gate + risk check.
Time-trigger fires at 09:35 ET on FOMC day for pre-staged candidates.
Forced exit at 13:55 ET (5 min before announcement) regardless of P&L.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from features.datatypes import Decision, TRADE, PASS
from strategies.s1_pre_fomc.setup import S1SetupContext, is_s1_setup


PASS_REASONS = (
    "ticker_not_in_universe",
    "holiday_day",
    "prior_emergency_announcement",
    "vix_regime_override",
    "outside_window",
    "risk_denied",
)


def allow_s1_trade(*,
                    setup_ctx: S1SetupContext,
                    cfg: dict,
                    risk_manager_allows: bool,
                    candidate_id: str = "") -> Decision:
    """S1 canonical gate."""
    eligible, reason = is_s1_setup(setup_ctx, cfg)
    if not eligible:
        return PASS(reason or "setup_failed",
                     strategy="S1", candidate_id=candidate_id)

    if not risk_manager_allows:
        return PASS("risk_denied", strategy="S1", candidate_id=candidate_id)

    return TRADE("S1", candidate_id=candidate_id)
