"""
data_clients/polygon_ws.py — Polygon WebSocket client. Spec FND-1.

Subscribes:
  AM.<ticker>  — 1m aggregate bars
  T.<ticker>   — Trades (with conditions; sweep code 14)
  Q.<ticker>   — NBBO quotes (Lee-Ready aggressor classifier)

Acceptance criteria (FND-1):
  - Connects to wss://socket.polygon.io/stocks
  - Auto-reconnect with exponential backoff (1s, 2s, 4s, 8s, max 60s)
  - Buffer survives 30s reconnects without data loss
  - Heartbeat with alert on > 5min silence

Tests required:
  FND-1.T1 — reconnect after forced disconnect within 5s, resumes stream
  FND-1.T2 — aggressor classification matches Lee-Ready (synthetic trades
             above/at/below midpoint → +1, 0, -1)

Auth: POLYGON_API_KEY env var.
"""
from __future__ import annotations

import os

POLYGON_WS_URL = "wss://socket.polygon.io/stocks"
POLYGON_KEY_ENV = "POLYGON_API_KEY"


class PolygonWS:
    """TODO Sprint 2 — implement per FND-1."""

    def __init__(self, api_key: str = None):
        self.api_key = api_key or os.environ.get(POLYGON_KEY_ENV)
        if not self.api_key:
            raise RuntimeError(f"{POLYGON_KEY_ENV} not set; see FND-5 secrets policy.")

    async def connect(self) -> None: raise NotImplementedError("FND-1.1")
    async def subscribe(self, channels: list[str]) -> None: raise NotImplementedError("FND-1.1")
    async def reconnect_loop(self) -> None: raise NotImplementedError("FND-1.2")
    async def stream(self): raise NotImplementedError("FND-1.3")


if __name__ == "__main__":
    print("[polygon_ws] FND-1 stub. Sprint 2 deliverable.")
