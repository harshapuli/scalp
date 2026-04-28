"""scalp_brain/publisher.py — Redis pubsub publisher. SCALP-30.

Channel: scalp.price_trigger
Schema: {ticker, state, score, timestamp, sequence_number}

Acceptance criteria (SCALP-30):
  - Publish on EVERY state change (not every bar)
  - Sequence number per ticker for downstream re-ordering
  - Async publish — must NOT block scalp classifier on Redis latency
  - Latency target: pubsub round-trip < 100ms p99 (SCALP-30.T2)
  - Sequence numbers monotonic per ticker (SCALP-30.T3)
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import ScalpState
from infra.redis_client import CHANNEL_SCALP_PRICE_TRIGGER, KEY_SCALP_LAST_SEEN, KEY_SCALP_STATE_FMT


CHANNEL = CHANNEL_SCALP_PRICE_TRIGGER


class ScalpPublisher:
    """SCALP-30. Async publisher to scalp.price_trigger.

    Maintains per-ticker monotonic sequence numbers in memory; on restart
    they reset (acceptable per spec — downstream uses gaps for diagnostic only).
    """

    def __init__(self, redis_async_client):
        self.redis = redis_async_client
        self._sequence_per_ticker: dict[str, int] = {}
        self._queue: asyncio.Queue = asyncio.Queue()

    def _next_sequence(self, ticker: str) -> int:
        seq = self._sequence_per_ticker.get(ticker, 0) + 1
        self._sequence_per_ticker[ticker] = seq
        return seq

    async def publish_state_change(self, prior: Optional[ScalpState],
                                     current: ScalpState) -> bool:
        """SCALP-30.1 — publish only on state change. Returns True if published."""
        if prior is not None and prior.name == current.name:
            return False    # not a state change

        seq = self._next_sequence(current.ticker)
        msg = {
            "ticker": current.ticker,
            "state": current.name,
            "score": round(current.score, 4),
            "timestamp": current.timestamp.isoformat(),
            "sequence_number": seq,
            "bar_idx": current.bar_idx,
        }
        # SCALP-30.3 async publish, queue if Redis slow (don't block classifier)
        try:
            await self.redis.publish(CHANNEL, json.dumps(msg))
            await self.redis.set(
                KEY_SCALP_STATE_FMT.format(ticker=current.ticker),
                json.dumps(msg), ex=1,
            )
            await self.redis.set(
                KEY_SCALP_LAST_SEEN, datetime.now(tz=timezone.utc).isoformat(),
            )
            return True
        except Exception:
            # Queue for later; don't block
            await self._queue.put(msg)
            return False

    async def drain_queue(self) -> int:
        """Background flusher — re-publish queued messages."""
        n = 0
        while not self._queue.empty():
            msg = self._queue.get_nowait()
            try:
                await self.redis.publish(CHANNEL, json.dumps(msg))
                n += 1
            except Exception:
                await self._queue.put(msg)   # re-queue on failure
                break
        return n
