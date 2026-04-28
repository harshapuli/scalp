"""scripts/main.py — Live daemon orchestrator.

Per spec §10.8 build recipe. Wires the four loops:

  1. bar_feed_loop       — Alpaca WS 1m bars → Features → classifier → publisher
  2. swing_subscriber    — consume scalp.price_trigger → elevate S5/S2 candidates
  3. health_monitor      — every 60s, ping Redis + check scalp_brain:last_seen
  4. periodic_log_flush  — periodic stats summary to stdout

Run:
  $ python3 scripts/main.py
  $ python3 scripts/main.py --tickers SPY,QQQ,NVDA   # custom universe
  $ python3 scripts/main.py --no-publish              # smoke (no Redis)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config_loader import load_thresholds
from infra.secrets import load_secrets, status_report
from journal.alerts import alert
from journal.decision_log import init_db


DEFAULT_UNIVERSE = "SPY,QQQ,IWM,NVDA,AAPL,TSLA,META,GOOGL,AMZN,MSFT"


async def _wait_redis(verbose: bool = True):
    """Tries Redis once. Returns async client or None."""
    try:
        from infra.redis_client import get_async_client, is_reachable
        if is_reachable():
            return get_async_client()
    except Exception:
        pass
    if verbose:
        print("[main] Redis not reachable — running in NO-PUBLISH mode "
              "(daemon still classifies + logs decisions to SQLite).")
    return None


async def bar_feed_loop(*,
                        tickers: list[str],
                        cfg: dict,
                        publisher: Optional[object] = None,
                        on_state_change=None,
                        poll_seconds: int = 60):
    """Pull 1m bars per ticker via Alpaca REST (WS would be faster but REST is
    sufficient for MVP — we poll for the latest closed bar each loop).

    For each ticker:
      - get last 30 bars (need history for ATR/VWAP)
      - get latest GEX snapshot from UW
      - get latest flow records from UW
      - build Features
      - classify
      - if state changed: publish + invoke on_state_change
    """
    from data_clients.alpaca import AlpacaClient
    from data_clients.unusual_whales import UWClient
    from features.builder import build_features
    from features.price_structure import BarOHLC
    from scalp_brain.classifier import classify
    from features.datatypes import ScalpState

    prior_state_per_ticker: dict[str, Optional[ScalpState]] = {t: None for t in tickers}
    bar_count_per_ticker: dict[str, int] = defaultdict(int)

    # Single long-lived clients
    alp = AlpacaClient()
    uw = UWClient()

    print(f"[bar_feed] starting for {len(tickers)} tickers, poll={poll_seconds}s")

    try:
        while True:
            now = datetime.now(tz=timezone.utc)
            # Window: last 60 minutes
            from datetime import timedelta
            start = (now - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%SZ")
            end = now.strftime("%Y-%m-%dT%H:%M:%SZ")

            for ticker in tickers:
                try:
                    # Fetch bars
                    bars_alp = alp.get_bars(
                        ticker, start=start, end=end,
                        timeframe="1Min", feed="sip", limit=60,
                    )
                    if len(bars_alp) < 2:
                        continue

                    ohlc_bars = [BarOHLC(o=b.o, h=b.h, l=b.l, c=b.c, v=b.v)
                                   for b in bars_alp]
                    last_bar_t = bars_alp[-1].t

                    # Fetch GEX (cached at the UW client level — could optimize)
                    try:
                        gex = uw.greek_exposure(ticker)
                    except Exception:
                        gex = None
                    try:
                        flow = uw.flow_recent(ticker)
                    except Exception:
                        flow = []

                    # Build features
                    f = build_features(
                        ticker=ticker,
                        bar_idx=bar_count_per_ticker[ticker],
                        bars=ohlc_bars,
                        trades=[], quotes=[],     # MVP: skip trade/quote feed
                        gex_snapshot=gex,
                        flow_records=flow,
                        timestamp=last_bar_t,
                    )
                    bar_count_per_ticker[ticker] += 1

                    # Classify
                    new_state = classify(
                        prior_state=prior_state_per_ticker[ticker],
                        features=f, cfg=cfg,
                    )

                    state_changed = (
                        prior_state_per_ticker[ticker] is None
                        or prior_state_per_ticker[ticker].name != new_state.name
                    )

                    if state_changed:
                        print(f"[bar_feed] {ticker} {last_bar_t.strftime('%H:%M')} "
                              f"state {prior_state_per_ticker[ticker].name if prior_state_per_ticker[ticker] else 'None'} → "
                              f"{new_state.name} score={new_state.score:.3f}")

                        if publisher is not None:
                            await publisher.publish_state_change(
                                prior_state_per_ticker[ticker], new_state,
                            )

                        if on_state_change is not None:
                            await on_state_change(f, new_state)

                    prior_state_per_ticker[ticker] = new_state

                except Exception as e:
                    print(f"[bar_feed] {ticker} error: {type(e).__name__}: {e}",
                            file=sys.stderr)
                    continue

            await asyncio.sleep(poll_seconds)
    finally:
        alp.close()
        uw.close()


async def evaluate_s5_on_state_change(features, scalp_state, cfg):
    """Default on_state_change: if state is REVERSE, evaluate S5 gate.

    For now we don't have pre-staged candidates wired (no scanner running yet);
    we just log what the gate would say if it were triggered.
    """
    from strategies.s5_gamma_reversal.allow_s5_trade import allow_s5_trade
    from journal.decision_log import log_decision_sync

    if scalp_state.name not in ("SURGE_REVERSE", "TANK_REVERSE"):
        return

    # Quick rules-only gate (no ML model loaded)
    decision = allow_s5_trade(
        features=features, model=None,
        threshold=0.50, cfg=cfg,
        risk_manager_allows=True,                # placeholder until risk_manager wired
        expected_value_net=0.0,                  # placeholder
        reversal_score=scalp_state.score,
        candidate_id=f"live-{features.ticker}-{features.bar_idx}",
    )
    try:
        log_decision_sync(decision, features, model_version="rules_only_v0")
    except Exception as e:
        print(f"[evaluate] log_decision failed: {e}", file=sys.stderr)
    print(f"[evaluate] {features.ticker} {scalp_state.name} → "
          f"{decision.decision} ({decision.pass_reason or 'TRADE'})")


async def health_monitor_loop(redis_async, cfg):
    """Every 60s, check Redis reachability + alert on failures."""
    while True:
        try:
            if redis_async is not None:
                ok = await redis_async.ping()
                if not ok:
                    alert("Redis ping returned falsy", severity="warning")
        except Exception as e:
            alert(f"Redis ping failed: {e}", severity="warning")
        await asyncio.sleep(60)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tickers", default=DEFAULT_UNIVERSE,
                         help="comma-separated tickers")
    parser.add_argument("--no-publish", action="store_true",
                         help="don't publish to Redis (for smoke testing)")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()

    print("=" * 60)
    print("scalp2 / Edge-Centric Trading System v2.2 — LIVE DAEMON")
    print("=" * 60)

    load_secrets()
    rpt = status_report()
    print(f"\nSecrets loaded from: {rpt['secrets_path']}")
    cfg = load_thresholds()
    init_db()
    print(f"\nMode: {cfg.get('alpaca', {}).get('endpoint', '?')}")

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    print(f"Universe: {tickers}")

    # Redis (optional)
    redis_async = None
    publisher = None
    if not args.no_publish:
        redis_async = await _wait_redis()
        if redis_async is not None:
            from scalp_brain.publisher import ScalpPublisher
            publisher = ScalpPublisher(redis_async)
            print("[main] publisher wired to scalp.price_trigger")

    print()
    print("Spawning loops:")
    print(f"  - bar_feed_loop ({len(tickers)} tickers, poll {args.poll_seconds}s)")
    print(f"  - health_monitor (every 60s)")
    print()
    alert(f"scalp2 daemon started ({len(tickers)} tickers)", severity="info")

    tasks = [
        asyncio.create_task(bar_feed_loop(
            tickers=tickers, cfg=cfg, publisher=publisher,
            on_state_change=lambda f, s: evaluate_s5_on_state_change(f, s, cfg),
            poll_seconds=args.poll_seconds,
        )),
        asyncio.create_task(health_monitor_loop(redis_async, cfg)),
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
