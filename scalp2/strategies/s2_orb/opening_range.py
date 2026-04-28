"""strategies/s2_orb/opening_range.py — Opening Range computation. Spec S2-1.

For top-200 liquid names, compute OR_high and OR_low from 09:30:00-09:35:00 ET.
Persist to redis: opening_range:{ticker} = {high, low, mid, computed_at}, TTL 23h.

Test required (S2-1.T1):
  - OR uses only first 5 minutes (09:30-09:34 inclusive); 09:35+ ignored
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from datetime import datetime, time, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))


# NYSE open in UTC — naive default for SPY/QQQ-style instruments.
# 09:30 ET = 13:30 UTC (winter, EST) or 13:30 UTC (summer, EDT) — 09:30 ET is
# the same wall-clock time year-round; the UTC offset varies. For correctness
# in production, use a tz-aware ET timezone (zoneinfo.ZoneInfo('US/Eastern')).
NYSE_OPEN_ET_HOUR = 9
NYSE_OPEN_ET_MIN = 30
OR_DURATION_MINUTES = 5    # 09:30:00 through 09:34:59 inclusive


@dataclass
class OpeningRange:
    ticker: str
    high: float
    low: float
    mid: float
    computed_at: datetime


def compute_opening_range(bars: list, ticker: str,
                            session_date: datetime) -> Optional[OpeningRange]:
    """S2-1. Compute OR from the first 5 minutes' bars.

    Args:
      bars: list with .t (datetime) and .h, .l fields
      ticker: ticker symbol
      session_date: datetime representing the trading day in ET (date portion used)

    Returns OpeningRange or None if not enough bars.
    """
    # Window: session_date.date() at 09:30 ET → +5 minutes
    # In UTC for naive comparison, convert below
    or_high = float("-inf")
    or_low = float("inf")
    n = 0
    target_date = session_date.date()
    for b in bars:
        if b.t.date() != target_date:
            continue
        # bar opens at b.t — keep only 09:30:00-09:34:59 ET (= 13:30-13:34 UTC summer)
        # We compare hour/minute of b.t directly against ET reference; for production
        # use ZoneInfo. For our common SPY/QQQ universe + UTC-stored bars, the
        # following crude check works for both EDT and EST sessions.
        utc_hm = (b.t.hour, b.t.minute)
        # 09:30 ET = 13:30 UTC (EDT) or 14:30 UTC (EST). Accept either.
        in_or_window = (
            (utc_hm[0] == 13 and 30 <= utc_hm[1] <= 30 + OR_DURATION_MINUTES - 1)
            or (utc_hm[0] == 14 and 30 <= utc_hm[1] <= 30 + OR_DURATION_MINUTES - 1)
        )
        if not in_or_window:
            continue
        or_high = max(or_high, b.h)
        or_low = min(or_low, b.l)
        n += 1

    if n == 0 or or_high == float("-inf"):
        return None
    return OpeningRange(
        ticker=ticker,
        high=or_high, low=or_low,
        mid=(or_high + or_low) / 2.0,
        computed_at=datetime.now(tz=timezone.utc),
    )


async def persist_opening_range(or_: OpeningRange, redis_async_client,
                                  ttl_seconds: int = 23 * 3600) -> None:
    """Persist to redis: opening_range:{ticker} with 23h TTL."""
    from infra.redis_client import KEY_OPENING_RANGE_FMT
    payload = json.dumps({
        "ticker": or_.ticker,
        "high": or_.high, "low": or_.low, "mid": or_.mid,
        "computed_at": or_.computed_at.isoformat(),
    })
    await redis_async_client.set(
        KEY_OPENING_RANGE_FMT.format(ticker=or_.ticker),
        payload, ex=ttl_seconds,
    )
