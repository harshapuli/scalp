"""
swing_brain/scanner.py — Per-strategy candidate scanner. Spec SWING-10.

Scans top-100 (S5) or top-200 (S4) names per scanner_interval_min:
  S1: 1440 (daily window-driven)
  S2: 1 (09:35-11:00 ET only)
  S3: 5 (10:00-15:00 ET)
  S4: 5
  S5: 5

For each ticker, runs is_<strategy>_setup(); if True, creates Candidate with TTL.

TODO Sprint 7-8.
"""
from __future__ import annotations


class CandidateScanner:
    """SWING-10. TODO Sprint 7."""

    def __init__(self, strategy: str, universe: list[str], cfg: dict):
        self.strategy = strategy
        self.universe = universe
        self.cfg = cfg

    async def scan_loop(self): raise NotImplementedError("SWING-10.1")
    async def scan_once(self): raise NotImplementedError("SWING-10")
