"""swing_brain/subscriber.py — Subscribe to scalp.price_trigger. SWING-11.

Acceptance criteria (SWING-11):
  - Async subscriber consuming scalp.price_trigger
  - On message: lookup pre-staged candidate for {ticker}
  - If candidate exists AND direction matches state:
    - TANK_REVERSE → long candidate elevated to TRIGGER
    - SURGE_REVERSE → short candidate elevated to TRIGGER
  - If no candidate or direction mismatch: ignore
  - Failure mode: scalp brain unreachable >60s → alert; pre-staged expire normally
  - Latency target: subscriber receive → TRIGGER state transition < 200ms

Tests required (SWING-11.T1, T2, T3):
  - End-to-end handoff fires TRIGGER within 200ms
  - Direction mismatch does not trigger
  - Graceful degradation when scalp down
"""
from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from infra.redis_client import (
    CHANNEL_SCALP_PRICE_TRIGGER, KEY_SCALP_LAST_SEEN, KEY_PRE_STAGED_FMT,
)


HEALTH_TIMEOUT_SECONDS = 60


class ScalpSubscriber:
    """SWING-11. Async subscriber for scalp.price_trigger."""

    def __init__(self, *,
                  redis_async_client,
                  on_trigger: Callable[[dict, dict], Awaitable[None]],
                  alert_fn: Optional[Callable[[str], None]] = None):
        """
        Args:
          redis_async_client — async Redis
          on_trigger(scalp_msg, candidate_dict) → coroutine that elevates the
            pre-staged candidate to TRIGGER state via swing_brain.lifecycle
          alert_fn — fired on health-monitor timeout
        """
        self.redis = redis_async_client
        self.on_trigger = on_trigger
        self.alert_fn = alert_fn or (lambda msg: print(f"[subscriber] ALERT: {msg}"))

    @staticmethod
    def _direction_for_scalp_state(state_name: str) -> Optional[str]:
        """TANK_REVERSE → long candidate; SURGE_REVERSE → short candidate."""
        if state_name == "TANK_REVERSE":
            return "long"
        if state_name == "SURGE_REVERSE":
            return "short"
        return None

    async def _handle_message(self, raw_payload: str) -> None:
        try:
            msg = json.loads(raw_payload)
        except Exception:
            return
        ticker = msg.get("ticker")
        state = msg.get("state")
        if not ticker or not state:
            return
        wanted_dir = self._direction_for_scalp_state(state)
        if wanted_dir is None:
            return    # not a reversal — ignore

        # Look up pre-staged S5 candidate
        key = KEY_PRE_STAGED_FMT.format(strategy="s5", ticker=ticker)
        raw_cand = await self.redis.get(key)
        if not raw_cand:
            return    # no candidate
        try:
            cand = json.loads(raw_cand)
        except Exception:
            return
        if cand.get("direction") != wanted_dir:
            return    # SWING-11.T2 direction mismatch — ignore

        # SWING-11.T1 — elevate to TRIGGER
        await self.on_trigger(msg, cand)

    async def subscribe_loop(self) -> None:
        """SWING-11.1 — main consumer loop."""
        pubsub = self.redis.pubsub()
        await pubsub.subscribe(CHANNEL_SCALP_PRICE_TRIGGER)
        try:
            async for raw in pubsub.listen():
                if raw.get("type") != "message":
                    continue
                payload = raw.get("data")
                if isinstance(payload, bytes):
                    payload = payload.decode()
                await self._handle_message(payload)
        finally:
            await pubsub.unsubscribe(CHANNEL_SCALP_PRICE_TRIGGER)
            await pubsub.aclose()

    async def health_monitor(self) -> None:
        """SWING-11.3 + SWING-11.T3 — alert if scalp brain unreachable >60s."""
        while True:
            await asyncio.sleep(HEALTH_TIMEOUT_SECONDS)
            try:
                last_seen = await self.redis.get(KEY_SCALP_LAST_SEEN)
                if not last_seen:
                    self.alert_fn("scalp brain has never reported (no last_seen)")
                    continue
                ts = datetime.fromisoformat(last_seen.replace("Z", "+00:00"))
                age_s = (datetime.now(tz=timezone.utc) - ts).total_seconds()
                if age_s > HEALTH_TIMEOUT_SECONDS:
                    self.alert_fn(
                        f"scalp brain silent for {age_s:.0f}s "
                        f"(timeout {HEALTH_TIMEOUT_SECONDS}s)"
                    )
            except Exception:
                self.alert_fn("redis health check failed")
