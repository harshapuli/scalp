"""
data_clients/unusual_whales.py — UW API client. Spec FND-3.

Endpoints (per spec §2.1):
  /api/stock/{ticker}/flow-recent           — Live signed flow (Scalp brain)
  /api/stock/{ticker}/flow-historical       — Daily aggregates (Swing brain bias)
  /api/stock/{ticker}/greek-exposure        — GEX by strike, gamma flip (S5 setup gate)
  /api/stock/{ticker}/greek-exposure-history — GEX over time (Phase 0 sub-edge)
  /api/stock/{ticker}/dark-pool-prints      — Off-exchange trades (deferred)
  /api/stock/{ticker}/iv-term-structure     — ATM IV per expiry, slope
  /api/stock/{ticker}/earnings/flow-summary — Pre-earnings (S4 deferred)

Auth: Bearer token via UW_API_TOKEN env var.
Base: https://api.unusualwhales.com

CRITICAL BLOCKER (FND-3.1): Verify /greek-exposure update cadence before any
S5 work proceeds. Real-time → 30min staleness holds. 5min → tighten to 10min.
EOD only → S5 redesign required. Cannot proceed past Sprint 1 without this answer.

Returns typed dataclasses (features.types.GEXSnapshot, FlowRecord), not raw dicts.

TODO Sprint 1: implement per FND-3 acceptance criteria + sub-tasks FND-3.1..FND-3.4.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

# Re-export typed responses
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.types import GEXSnapshot, FlowRecord

UW_BASE_URL = "https://api.unusualwhales.com"
UW_TOKEN_ENV = "UW_API_TOKEN"


class UWClient:
    """Typed UW API client. Spec FND-3.

    Initialization fails (raises) if UW_API_TOKEN is not set —
    we never hardcode credentials per FND-5.
    """

    def __init__(self, token: Optional[str] = None, base_url: str = UW_BASE_URL):
        self.token = token or os.environ.get(UW_TOKEN_ENV)
        if not self.token:
            raise RuntimeError(
                f"UW_API_TOKEN not set. Per FND-5 secrets policy, set it in "
                f"/etc/trading/secrets.env (chmod 600). Never hardcode."
            )
        self.base_url = base_url

    # ─── Sprint 1 deliverables ───────────────────────────────────────────

    def verify_gex_cadence(self) -> dict:
        """FND-3.1 BLOCKER. Read UW docs / probe /greek-exposure repeatedly,
        report observed update interval. Returns:
          {'verified_cadence_min': int, 'evidence': str, 'recommended_max_age_min': int}
        Cannot proceed past Sprint 1 without this answer.
        """
        raise NotImplementedError(
            "FND-3.1 BLOCKER — Sprint 1 must implement this. "
            "Read https://docs.unusualwhales.com/api/greek-exposure and probe "
            "the endpoint at 1-min cadence over a market session to confirm "
            "actual update frequency."
        )

    def flow_recent(self, ticker: str) -> list[FlowRecord]:
        """FND-3.2. Live signed flow records via /flow-recent."""
        raise NotImplementedError("FND-3.2")

    def flow_historical(self, ticker: str, days: int = 30) -> list[dict]:
        """FND-3.2. Daily aggregates via /flow-historical."""
        raise NotImplementedError("FND-3.2")

    def greek_exposure(self, ticker: str) -> GEXSnapshot:
        """FND-3.3. Current GEX snapshot via /greek-exposure."""
        raise NotImplementedError("FND-3.3")

    def greek_exposure_history(self, ticker: str, days: int = 252) -> list[GEXSnapshot]:
        """FND-3.3. Historical GEX snapshots via /greek-exposure-history."""
        raise NotImplementedError("FND-3.3")

    def iv_term_structure(self, ticker: str) -> list[dict]:
        """FND-3.4. ATM IV per expiry + slope."""
        raise NotImplementedError("FND-3.4")

    def earnings_flow_summary(self, ticker: str) -> dict:
        """FND-3.4. Pre-earnings call/put ratio (S4 deferred)."""
        raise NotImplementedError("FND-3.4")


if __name__ == "__main__":
    print("[unusual_whales] FND-3 stub. Sprint 1 BLOCKER: implement verify_gex_cadence().")
    print("[unusual_whales] Endpoints planned:")
    for ep in ["flow-recent", "flow-historical", "greek-exposure",
               "greek-exposure-history", "iv-term-structure", "earnings/flow-summary"]:
        print(f"  - /api/stock/{{ticker}}/{ep}")
