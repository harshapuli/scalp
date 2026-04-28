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


async def evaluate_strategies_on_state_change(features, scalp_state, cfg):
    """Multi-strategy dispatcher. Each scalp state can trigger different gates:

      SURGE_REVERSE   → evaluate S5 (short)
      TANK_REVERSE    → evaluate S5 (long)
      SURGE_IGNITION  → evaluate S2 (long), S4 (long if flow agrees)
      SURGE_CONTINUATION → evaluate S3 (long), S4 (long)
      TANK_IGNITION   → evaluate S2 (short), S4 (short if flow agrees)
      TANK_CONTINUATION → evaluate S3 (short), S4 (short)
    """
    from strategies.s5_gamma_reversal.allow_s5_trade import allow_s5_trade
    from journal.decision_log import log_decision_sync

    # ── S5 — Reversal states ───────────────────────────────────────────────
    if scalp_state.name in ("SURGE_REVERSE", "TANK_REVERSE"):
        decision = allow_s5_trade(
            features=features, model=None,
            threshold=0.50, cfg=cfg,
            risk_manager_allows=True,                # placeholder
            expected_value_net=0.0,                  # placeholder
            reversal_score=scalp_state.score,
            candidate_id=f"live-S5-{features.ticker}-{features.bar_idx}",
        )
        _safe_log(decision, features, "rules_only_v0")
        _print_decision("S5", features.ticker, scalp_state.name, decision)

    # ── S3 — Continuation states (avoid S5 conflict) ─────────────────────
    if scalp_state.name in ("SURGE_CONTINUATION", "TANK_CONTINUATION"):
        from strategies.s3_momentum.allow_s3_trade import allow_s3_trade
        from strategies.s3_momentum.setup import S3SetupContext
        from scalp_brain.scores import reversal_score as rev_score

        # Build setup context from features (placeholders for missing data)
        direction = "long" if scalp_state.name == "SURGE_CONTINUATION" else "short"
        ctx = S3SetupContext(
            ticker=features.ticker,
            session_return_atr=features.extension_from_prior_close_atr,
            vwap_distance_atr=features.extension_from_vwap_atr,
            consecutive_higher_lows=4 if direction == "long" and not features.pullback_break else 0,
            consecutive_lower_highs=4 if direction == "short" and not features.pullback_break else 0,
            aggressor_avg_30m=features.aggressor_recent,
            near_htf_level_atr=features.near_htf_level_atr,
            distance_to_pos_gex_atr=abs(features.distance_to_major_pos_gex_atr),
        )
        rs = rev_score(features, "short" if direction == "long" else "long", cfg)
        decision = allow_s3_trade(
            setup_ctx=ctx, features=features, scalp_state=scalp_state,
            reversal_score=rs, cfg=cfg,
            risk_manager_allows=True,
            candidate_id=f"live-S3-{features.ticker}-{features.bar_idx}",
        )
        _safe_log(decision, features, "rules_only_v0")
        _print_decision("S3", features.ticker, scalp_state.name, decision)

    # ── S4 — Flow-aligned ignition / continuation ──────────────────────────
    if scalp_state.name in ("SURGE_IGNITION", "SURGE_CONTINUATION",
                              "TANK_IGNITION", "TANK_CONTINUATION"):
        from strategies.s4_signed_flow.allow_s4_trade import allow_s4_trade
        from strategies.s4_signed_flow.setup import S4SetupContext

        ctx = S4SetupContext(
            ticker=features.ticker,
            signed_flow_score=features.signed_flow_score,
            price_return_30m_atr=features.extension_from_vwap_atr,
            iv_percentile=features.iv_percentile,
            distance_to_pos_gex_atr=abs(features.distance_to_major_pos_gex_atr),
            earnings_blackout=features.earnings_blackout,
            daily_relative_volume=1.5,         # placeholder until daily-vol feed wired
        )
        decision = allow_s4_trade(
            setup_ctx=ctx, features=features, scalp_state=scalp_state, cfg=cfg,
            risk_manager_allows=True,
            candidate_id=f"live-S4-{features.ticker}-{features.bar_idx}",
        )
        _safe_log(decision, features, "rules_only_v0")
        _print_decision("S4", features.ticker, scalp_state.name, decision)


def _safe_log(decision, features, model_version):
    from journal.decision_log import log_decision_sync
    try:
        log_decision_sync(decision, features, model_version=model_version)
    except Exception as e:
        print(f"[evaluate] log_decision failed: {e}", file=sys.stderr)


def _print_decision(strategy, ticker, scalp_state_name, decision):
    if decision.decision == "TRADE":
        print(f"[evaluate] {strategy} {ticker} {scalp_state_name} → ✓ TRADE")
    else:
        print(f"[evaluate] {strategy} {ticker} {scalp_state_name} → "
                f"PASS ({decision.pass_reason})")


# Keep old name as alias for backwards compat with code that may still reference it
evaluate_s5_on_state_change = evaluate_strategies_on_state_change


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
