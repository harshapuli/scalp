"""infra/redis.py — Redis connection + pubsub helpers. Spec §10.6.

Conventions:
  scalp_states:{ticker}             → {state, score, ts}    TTL 1s
  scalp:price_trigger               → pubsub channel for state changes
  pre_staged_s5:{ticker}            → Candidate JSON         TTL 30 min
  pre_staged_s2,s3,s4 same shape, different TTLs
  opening_range:{ticker}            → {high, low, mid, ts}  TTL 23h
  bias_map:{ticker}                 → {bias, computed_at}   TTL 24h
  gex_snapshot:{ticker}             → GEXSnapshot JSON       TTL 60 min
  scalp_brain:last_seen             → ts (used by SWING-11.3 health monitor)
"""
from __future__ import annotations

import os
from typing import Optional

import redis
import redis.asyncio as redis_async


REDIS_URL_ENV = "REDIS_URL"
DEFAULT_REDIS_URL = "redis://localhost:6379/0"


def get_url() -> str:
    return os.environ.get(REDIS_URL_ENV) or DEFAULT_REDIS_URL


def get_sync_client() -> redis.Redis:
    """Synchronous client for scripts and tests."""
    return redis.Redis.from_url(get_url(), decode_responses=True)


def get_async_client() -> redis_async.Redis:
    """Async client for the live daemon."""
    return redis_async.Redis.from_url(get_url(), decode_responses=True)


# Channel + key conventions (spec §10.6)
CHANNEL_SCALP_PRICE_TRIGGER = "scalp.price_trigger"

KEY_SCALP_STATE_FMT = "scalp_states:{ticker}"
KEY_PRE_STAGED_FMT = "pre_staged_{strategy}:{ticker}"
KEY_OPENING_RANGE_FMT = "opening_range:{ticker}"
KEY_BIAS_MAP_FMT = "bias_map:{ticker}"
KEY_GEX_SNAPSHOT_FMT = "gex_snapshot:{ticker}"
KEY_SCALP_LAST_SEEN = "scalp_brain:last_seen"
KEY_SCANNER_LAST_RUN_FMT = "scanner:{strategy}:last_run"


def is_reachable(client: Optional[redis.Redis] = None) -> bool:
    """For verify_foundation.py — fast check, no exceptions thrown."""
    try:
        c = client or get_sync_client()
        return c.ping()
    except Exception:
        return False


if __name__ == "__main__":
    print(f"[redis] url = {get_url()}")
    print(f"[redis] reachable = {is_reachable()}")
