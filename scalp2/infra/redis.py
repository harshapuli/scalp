"""infra/redis.py — Redis connection + pubsub helpers. Spec §10.6 conventions.

TODO Sprint 1.
"""
from __future__ import annotations

import os

REDIS_URL_ENV = "REDIS_URL"


def get_client():
    """Return a redis.asyncio.Redis client connected via REDIS_URL."""
    raise NotImplementedError("Sprint 1")
