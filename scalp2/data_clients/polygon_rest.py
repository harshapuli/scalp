"""
data_clients/polygon_rest.py — Polygon REST client. Spec FND-2.

Endpoints:
  /v2/aggs/ticker/{symbol}/range/1/minute/{from}/{to}  — Historical bars w/ pagination
  /v3/reference/options/contracts                       — Chain snapshot at timestamp
  /v3/snapshot/options/{underlying}                     — Live option pricing

Acceptance criteria (FND-2):
  - Pagination handled
  - Options chain snapshot at any historical timestamp
  - Rate-limit aware (5 req/min default per spec §10.4 polygon.rest_rate_limit_per_min)
  - Results cached to S3/parquet

Tests required:
  FND-2.T1 — pagination retrieves all bars (1 yr SPY 1m bars ~98,280 ±1%)
  FND-2.T2 — rate limit respected (100 req in 10s with rate=5/min throttles, no 429)
"""
from __future__ import annotations

import os

POLYGON_REST_URL = "https://api.polygon.io"
POLYGON_KEY_ENV = "POLYGON_API_KEY"


class PolygonREST:
    """TODO Sprint 2 — implement per FND-2."""

    def __init__(self, api_key: str = None, rate_limit_per_min: int = 5):
        self.api_key = api_key or os.environ.get(POLYGON_KEY_ENV)
        if not self.api_key:
            raise RuntimeError(f"{POLYGON_KEY_ENV} not set; see FND-5.")
        self.rate_limit_per_min = rate_limit_per_min

    def historical_bars(self, ticker: str, start: str, end: str, timespan: str = "minute"):
        """FND-2 — paginated historical bars. Cached to S3."""
        raise NotImplementedError("FND-2")

    def options_chain_snapshot(self, ticker: str, ts: str = None):
        """FND-2 — option chain at historical timestamp."""
        raise NotImplementedError("FND-2")

    def options_live_snapshot(self, underlying: str):
        """FND-2 — live option Greeks/bid/ask/OI."""
        raise NotImplementedError("FND-2")


if __name__ == "__main__":
    print("[polygon_rest] FND-2 stub. Sprint 2 deliverable.")
