"""journal/alerts.py — Pushover or Discord webhook alerts. Spec S5-83.

Acceptance criteria:
  - Fires on: trade fill, kill switch, drift, GEX stale rejection rate >20%
  - Rate-limited: max 5 alerts/hour
  - Pushover or Discord webhook configurable via env (PUSHOVER_USER/TOKEN, DISCORD_WEBHOOK)

TODO Sprint 15.
"""
from __future__ import annotations


def alert(message: str, severity: str = "info") -> None:
    """S5-83. Send via Pushover or Discord. Rate-limited."""
    raise NotImplementedError("S5-83 — TODO Sprint 15")
