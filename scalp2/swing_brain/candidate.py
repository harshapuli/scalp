"""swing_brain/candidate.py — Candidate Redis persistence. SWING-10.2.

Per spec §10.6:
  pre_staged_<strategy>:{ticker} → Candidate JSON, TTL per strategy
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import Candidate
from infra.redis_client import KEY_PRE_STAGED_FMT


def _serialize(c: Candidate) -> dict:
    """Convert Candidate to JSON-serializable dict (handles datetime/date/dataclass)."""
    def _conv(o):
        if isinstance(o, (datetime, date)):
            return o.isoformat()
        if hasattr(o, "__dict__"):
            return o.__dict__
        return o

    d = {
        "id": c.id,
        "strategy": c.strategy,
        "ticker": c.ticker,
        "direction": c.direction,
        "state": c.state,
        "created_at": c.created_at.isoformat(),
        "expires_at": c.expires_at.isoformat(),
        "atr": c.atr,
        "gamma_strike": c.gamma_strike,
        "or_high": c.or_high,
        "or_low": c.or_low,
        "flow_score_at_setup": c.flow_score_at_setup,
        "spread_width": c.spread_width,
        "predecessor_states": list(c.predecessor_states),
        "setup_features_hash": c.setup_features_hash,
    }
    return d


async def to_redis(candidate: Candidate, redis_async_client, ttl_seconds: int) -> None:
    """SWING-10.2 — persist candidate JSON with TTL."""
    key = KEY_PRE_STAGED_FMT.format(
        strategy=candidate.strategy.lower(), ticker=candidate.ticker,
    )
    await redis_async_client.set(key, json.dumps(_serialize(candidate)), ex=ttl_seconds)


async def from_redis(strategy: str, ticker: str, redis_async_client) -> Optional[dict]:
    """SWING-10.2 — lookup pre-staged candidate. Returns dict or None."""
    key = KEY_PRE_STAGED_FMT.format(strategy=strategy.lower(), ticker=ticker)
    raw = await redis_async_client.get(key)
    if not raw:
        return None
    return json.loads(raw)


async def list_pre_staged(strategy: str, redis_async_client) -> list[dict]:
    """List all pre-staged candidates for a strategy."""
    pattern = KEY_PRE_STAGED_FMT.format(strategy=strategy.lower(), ticker="*")
    keys = []
    async for k in redis_async_client.scan_iter(match=pattern):
        keys.append(k)
    out = []
    for k in keys:
        raw = await redis_async_client.get(k)
        if raw:
            try:
                out.append(json.loads(raw))
            except Exception:
                continue
    return out
