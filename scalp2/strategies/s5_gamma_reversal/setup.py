"""
strategies/s5_gamma_reversal/setup.py — is_s5_setup gate. Spec SWING-2 + v2.2 §14.

Six conditions ALL must hold (SWING-2):
  near_gex          : distance_to_major_pos_gex_atr ≤ 0.50
  extended          : extension_from_vwap_atr ≥ 1.50 OR from_prior_close
  at_liquidity      : near_htf_level_atr ≤ 0.50
  no_event          : not earnings_blackout AND not fomc_blackout
  gex_strike_ok     : major_gex_strike_dte ≥ 6   (v2.2 §22b OPEX guard)
  gex_fresh         : gex_snapshot_age_min ≤ 30  (v2.2 §22a freshness)

Tests required (SWING-2.T1, T2, T3):
  - gex_strike_dte rejects near-OPEX (dte=4 → False with reason)
  - stale GEX rejects setup (age=45 → False)
  - all six conditions required (parametrized: each one false → reject with correct reason)

The canonical reference implementation is inlined in spec §10.5 (`is_s5_setup`).
This file is a thin wrapper that delegates to allow_s5_trade.py's gate-1 check
to keep the predicate in one place — but the public API exposes a tuple
(eligible: bool, reason_if_not: Optional[str]) per spec §10.5 contract.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))
from features.datatypes import Features
from strategies.s5_gamma_reversal.allow_s5_trade import _is_s5_setup_check


def is_s5_setup(f: Features, cfg: dict) -> tuple[bool, Optional[str]]:
    """SWING-2 + spec §10.5. Returns (eligible, reason_if_not).

    Reason strings (one per failed condition):
      'not_near_gex' | 'not_extended' | 'not_at_liquidity' | 'event_blackout' |
      'gex_strike_too_close_to_expiry' | 'gex_stale'
    """
    reason = _is_s5_setup_check(f, cfg)
    return (reason is None), reason
