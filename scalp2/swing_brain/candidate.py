"""
swing_brain/candidate.py — Candidate dataclass + Redis persistence. Spec SWING-10.2.

Candidate dataclass is canonical (features/types.py::Candidate). This module
adds Redis persistence per spec §10.6:
  pre_staged_s5:{ticker} → Candidate JSON, TTL 30 min

TODO Sprint 7.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import Candidate


REDIS_KEY_FMT = "pre_staged_{strategy}:{ticker}"


def to_redis(candidate: Candidate, redis_client, ttl_seconds: int) -> None:
    """SWING-10.2 — persist candidate JSON with TTL."""
    raise NotImplementedError("SWING-10.2 — TODO Sprint 7")


def from_redis(strategy: str, ticker: str, redis_client) -> Candidate | None:
    """Lookup pre-staged candidate."""
    raise NotImplementedError("SWING-10.2")
