"""
scalp_brain/publisher.py — Redis pubsub publisher. Spec SCALP-30.

Channel: scalp.price_trigger
Schema: {ticker, state, score, timestamp, sequence_number}

Acceptance criteria (SCALP-30):
  - Publish on EVERY state change (not every bar)
  - Sequence number per ticker for downstream re-ordering
  - Async publish — must NOT block scalp classifier on Redis latency
  - Latency target: pubsub round-trip < 100ms p99 (SCALP-30.T2)
  - Sequence numbers monotonic per ticker (SCALP-30.T3)

TODO Sprint 6.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import ScalpState

CHANNEL = "scalp.price_trigger"


class ScalpPublisher:
    """TODO Sprint 6 — implement per SCALP-30."""

    def __init__(self, redis_client):
        self.redis = redis_client
        self._sequence_per_ticker: dict[str, int] = {}

    async def publish_state_change(self, prior: ScalpState, current: ScalpState) -> None:
        """SCALP-30.1 + SCALP-30.2 + SCALP-30.3."""
        raise NotImplementedError("SCALP-30")
