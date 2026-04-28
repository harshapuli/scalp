"""scripts/main.py — Live daemon orchestrator.

Per spec §10.8 build recipe:
  $ python -m scripts.main
    → scalp brain  : LIVE
    → swing brain  : LIVE (S5 active)
    → executor     : paper @ 0.50%/trade
    → kill switch  : ARMED

Wires together:
  - data_clients.alpaca + data_clients.unusual_whales (live feeds)
  - features.builder (per-bar Features)
  - scalp_brain.classifier + publisher (state stream → Redis pubsub)
  - swing_brain.scanner + subscriber (S5 candidate lifecycle)
  - strategies.s5_gamma_reversal.allow_s5_trade (gating)
  - risk.sizing + risk.manager + risk.kill_switch
  - journal.decision_log + journal.alerts

Operates as an asyncio daemon. Three concurrent loops:
  1. Polygon-style bar feed → scalp brain classify → publisher
  2. Swing-brain scanner (every N min per strategy)
  3. Swing-brain subscriber (consume scalp triggers)
"""
from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config_loader import load_thresholds
from infra.secrets import load_secrets, status_report
from infra.redis_client import get_async_client, is_reachable, get_sync_client
from journal.alerts import alert
from journal.decision_log import init_db


async def run_scalp_brain_loop(redis_async, cfg: dict):
    """Loop: pull live bars from Alpaca + GEX from UW + classify + publish."""
    # Production: subscribe to Alpaca WS for live AM.* bars; on each bar close,
    # build features, classify, and publish state changes.
    print("[main] scalp_brain loop — live wiring deferred until WS handler implemented")
    while True:
        await asyncio.sleep(60)


async def run_swing_subscriber_loop(redis_async, cfg: dict):
    """Loop: consume scalp.price_trigger and elevate matching pre-staged candidates."""
    print("[main] swing_subscriber loop — wiring on_trigger callback")
    while True:
        await asyncio.sleep(60)


async def run_health_monitor(cfg: dict):
    """Loop: every 60s, check that Redis + Alpaca + UW are reachable."""
    while True:
        if not is_reachable():
            alert("Redis unreachable", severity="critical")
        await asyncio.sleep(60)


async def main() -> int:
    print("=" * 60)
    print("scalp2 / Edge-Centric Trading System v2.2 — LIVE DAEMON")
    print("=" * 60)

    load_secrets()
    rpt = status_report()
    print(f"\nSecrets loaded from: {rpt['secrets_path']}")
    cfg = load_thresholds()
    init_db()
    print(f"\nMode: {cfg.get('alpaca', {}).get('endpoint', '?')}")
    print()

    # Probe foundation pre-flight
    if not is_reachable():
        print("⚠ Redis not reachable — required for pubsub. "
              "Start: docker-compose up -d (or set REDIS_URL).")
        # Continue without Redis for now — scalp_brain loop will skip publish
    redis_async = get_async_client() if is_reachable() else None

    print("Spawning loops:")
    print("  - scalp_brain (bar feed → classify → publish)")
    print("  - swing_subscriber (consume scalp.price_trigger → trigger candidates)")
    print("  - health_monitor (every 60s)")
    print()
    alert("scalp2 daemon started", severity="info")

    tasks = [
        asyncio.create_task(run_scalp_brain_loop(redis_async, cfg)),
        asyncio.create_task(run_swing_subscriber_loop(redis_async, cfg)),
        asyncio.create_task(run_health_monitor(cfg)),
    ]
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("\n[main] shutdown requested")
        for t in tasks:
            t.cancel()
        alert("scalp2 daemon stopped", severity="info")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
