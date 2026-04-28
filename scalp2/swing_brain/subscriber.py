"""
swing_brain/subscriber.py — Subscribe to scalp.price_trigger. Spec SWING-11.

Acceptance criteria (SWING-11):
  - Async subscriber consuming scalp.price_trigger
  - On message: lookup pre-staged candidate for {ticker}
  - If candidate exists AND direction matches state:
    - TANK_REVERSE → long candidate elevated to TRIGGER
    - SURGE_REVERSE → short candidate elevated to TRIGGER
  - If no candidate or direction mismatch: ignore message
  - Failure mode: scalp brain unreachable >60s during market hours → alert; pre-staged
    candidates expire normally
  - Latency target: subscriber receive → TRIGGER state transition < 200ms

Tests required (SWING-11.T1, T2, T3):
  - End-to-end handoff fires TRIGGER within 200ms
  - Direction mismatch does not trigger
  - Graceful degradation when scalp down

TODO Sprint 7.
"""
from __future__ import annotations


class ScalpSubscriber:
    """SWING-11. TODO Sprint 7."""

    def __init__(self, redis_client, lifecycle_module):
        self.redis = redis_client
        self.lifecycle = lifecycle_module

    async def subscribe_loop(self): raise NotImplementedError("SWING-11.1")
    async def health_monitor(self): raise NotImplementedError("SWING-11.3")
