"""
journal/decision_log.py — Postgres writer for every TRADE/PASS decision. Spec S5-81.

Acceptance criteria (S5-81):
  - Insert latency < 50ms (non-blocking)
  - Indexed on timestamp, ticker, model_version
  - Includes gex_strike_dte, gex_snapshot_age_min, fake_breakout_check,
    ml_threshold_used, iv_at_entry

Schema: spec §10.3 decision_log table (see scalp2/infra/postgres.py for DDL).

Constraint: pass_requires_reason — DB-level check that decision='PASS' implies
pass_reason IS NOT NULL.

Tests required (S5-81.T1):
  - decision log non-blocking — diff(latency_with_log, latency_without_log) < 50ms
  - Test S5-50.T2: every PASS has logged reason (decision_log query
    'pass_reason IS NULL AND decision=PASS' returns zero)

NOTE: replaces inferred-scaffold journal/position_ledger.py JSONL writer.
JSONL is too informal for production audit; spec requires Postgres.
position_ledger.py is kept for backwards compat with scalp1 bridge but
new code MUST use this module.

TODO Sprint 14.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.types import Decision, Features


async def log_decision(decision: Decision,
                        features: Features,
                        model_version: Optional[str] = None) -> int:
    """S5-81. Returns row id. Insert latency must stay < 50ms."""
    raise NotImplementedError("S5-81 — TODO Sprint 14")
