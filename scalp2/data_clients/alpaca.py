"""
data_clients/alpaca.py — Alpaca client for paper + live execution. Spec FND-4.

Endpoints:
  POST /v2/orders            — Equity bracket orders (TP/SL)
  POST /v2/orders (mleg)     — Multi-leg vertical option spreads
  WS   /stream trade_updates — Position fills
  GET  /v2/positions         — Open positions
  GET  /v2/account           — Equity, BP, daytrade count

Acceptance criteria (FND-4):
  - POST /v2/orders for equity brackets
  - POST /v2/orders with order_class='mleg' for verticals
  - WS subscription to trade_updates
  - Paper/live switching via ALPACA_ENDPOINT env var
  - Idempotency via client_order_id (prefix 'edge')

Tests required:
  FND-4.T1 — vertical leg construction with ratio_qty='1' and position_intent
  FND-4.T2 — idempotent submission via client_order_id (same id twice → no duplicate)

Auth: ALPACA_KEY_ID + ALPACA_SECRET. Endpoint: ALPACA_ENDPOINT.
"""
from __future__ import annotations

import os
from typing import Optional

ALPACA_KEY_ID_ENV = "ALPACA_KEY_ID"
ALPACA_SECRET_ENV = "ALPACA_SECRET"
ALPACA_ENDPOINT_ENV = "ALPACA_ENDPOINT"

DEFAULT_PAPER_URL = "https://paper-api.alpaca.markets"
DEFAULT_LIVE_URL = "https://api.alpaca.markets"


class AlpacaClient:
    """TODO Sprint 3 — implement per FND-4."""

    def __init__(self,
                 key_id: Optional[str] = None,
                 secret: Optional[str] = None,
                 endpoint: Optional[str] = None,
                 client_order_id_prefix: str = "edge"):
        self.key_id = key_id or os.environ.get(ALPACA_KEY_ID_ENV)
        self.secret = secret or os.environ.get(ALPACA_SECRET_ENV)
        self.endpoint = endpoint or os.environ.get(ALPACA_ENDPOINT_ENV) or DEFAULT_PAPER_URL
        if not self.key_id or not self.secret:
            raise RuntimeError(
                f"{ALPACA_KEY_ID_ENV}/{ALPACA_SECRET_ENV} not set; see FND-5."
            )
        self.client_order_id_prefix = client_order_id_prefix

    @property
    def is_live(self) -> bool:
        return "paper" not in self.endpoint.lower()

    def submit_equity_bracket(self, ticker: str, qty: int, take_profit: float,
                              stop_loss: float, client_order_id: str):
        raise NotImplementedError("FND-4")

    def submit_vertical(self, vertical_order, client_order_id: str):
        """Multi-leg order with order_class='mleg'."""
        raise NotImplementedError("FND-4")

    async def stream_trade_updates(self):
        """WebSocket trade fill events."""
        raise NotImplementedError("FND-4")

    def get_positions(self):
        raise NotImplementedError("FND-4")

    def get_account(self):
        raise NotImplementedError("FND-4")


if __name__ == "__main__":
    print("[alpaca] FND-4 stub. Sprint 3 deliverable.")
