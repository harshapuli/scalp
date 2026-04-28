"""swing_brain/scanner.py — Per-strategy candidate scanner. SWING-10.

Per spec §10.4 swing.scanner_interval_min:
  S1: 1440 (daily)
  S2: 1 (09:35-11:00 ET only)
  S3: 5 (10:00-15:00 ET)
  S4: 5
  S5: 5

For each ticker in universe, runs is_<strategy>_setup(); if True, persists
Candidate to redis pre_staged_<strategy>:{ticker} with TTL.
"""
from __future__ import annotations

import asyncio
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Awaitable, Callable, Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import Candidate, Features
from infra.redis_client import KEY_SCANNER_LAST_RUN_FMT
from swing_brain.candidate import to_redis as candidate_to_redis


SetupGate = Callable[[Features, dict], tuple[bool, Optional[str]]]
"""A setup gate function: (features, cfg) → (eligible, reason_if_not)."""


class CandidateScanner:
    """SWING-10. Async scanner per strategy.

    Caller wires:
      universe          — top-N ticker symbols
      cfg               — full config (uses cfg['swing']['scanner_interval_min'])
      build_features_fn — callable returning Features for a ticker at decision time
      setup_gate        — strategy-specific is_X_setup function
      ttl_seconds       — TTL for pre-staged candidates
    """

    def __init__(self, *,
                  strategy: str,                     # 's1' | 's2' | 's3' | 's4' | 's5'
                  universe: list[str],
                  cfg: dict,
                  redis_async_client,
                  build_features_fn: Callable[[str], Awaitable[Optional[Features]]],
                  setup_gate: SetupGate,
                  candidate_kwargs_fn: Optional[Callable[[Features], dict]] = None):
        self.strategy = strategy
        self.universe = universe
        self.cfg = cfg
        self.redis = redis_async_client
        self.build_features_fn = build_features_fn
        self.setup_gate = setup_gate
        self.candidate_kwargs_fn = candidate_kwargs_fn or (lambda f: {})

        self.interval_min = int(cfg["swing"]["scanner_interval_min"][strategy])
        self.ttl_min = int(cfg["swing"]["candidate_ttl_min"][strategy])

    async def scan_once(self) -> dict:
        """Run one scan over universe; returns summary {n_setups, n_skipped}."""
        n_setups = 0
        n_skipped = 0
        for ticker in self.universe:
            try:
                f = await self.build_features_fn(ticker)
                if f is None:
                    n_skipped += 1
                    continue
                eligible, reason = self.setup_gate(f, self.cfg)
                if not eligible:
                    n_skipped += 1
                    continue
                # Promote to candidate
                kw = self.candidate_kwargs_fn(f)
                cand = Candidate(
                    id=str(uuid.uuid4()),
                    strategy=self.strategy.upper(),
                    ticker=ticker,
                    direction=kw.get("direction", "long"),
                    state="SETUP",
                    created_at=datetime.now(tz=timezone.utc),
                    expires_at=datetime.now(tz=timezone.utc)
                                + timedelta(minutes=self.ttl_min),
                    setup_features=f, setup_features_hash=f.hash(),
                    atr=kw.get("atr", 0.0),
                    gamma_strike=kw.get("gamma_strike"),
                    or_high=kw.get("or_high"),
                    or_low=kw.get("or_low"),
                    flow_score_at_setup=kw.get("flow_score_at_setup"),
                    spread_width=kw.get("spread_width"),
                )
                await candidate_to_redis(cand, self.redis, ttl_seconds=self.ttl_min * 60)
                n_setups += 1
            except Exception:
                n_skipped += 1
                continue

        # Mark last run
        await self.redis.set(
            KEY_SCANNER_LAST_RUN_FMT.format(strategy=self.strategy),
            datetime.now(tz=timezone.utc).isoformat(),
            ex=86400,
        )
        return {"strategy": self.strategy, "n_setups": n_setups,
                "n_skipped": n_skipped, "universe_size": len(self.universe)}

    async def scan_loop(self, *, in_market_hours_fn: Optional[Callable] = None) -> None:
        """SWING-10.1 — async scan loop. Runs every interval_min minutes."""
        in_market_hours = in_market_hours_fn or (lambda: True)
        while True:
            if in_market_hours():
                await self.scan_once()
            await asyncio.sleep(self.interval_min * 60)
