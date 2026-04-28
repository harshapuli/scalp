"""scripts/paper_trader.py — Controllable autonomous paper trader.

Wraps the bar_feed_loop in scripts/main.py with:
  · start/stop control (HTTP-driven from the dashboard)
  · actual Alpaca order submission when allow_*_trade returns TRADE
  · status snapshot for the /api/daemon/status endpoint

Runs in a background thread (so the HTTP server stays responsive). The
thread spins up its own asyncio event loop and tears it down cleanly on stop.

The dashboard's /auto page surfaces:
  · running/stopped state
  · last tick timestamp + universe size
  · recent decisions feed (TRADE rows + PASS rows with reason)
  · daemon-placed orders (filtered by client_order_id prefix 'edge-auto-')
"""
from __future__ import annotations

import asyncio
import sys
import threading
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, time as dt_time, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# Default universe — small focused set for live paper trading. Bigger lists
# blow API quota and make the decision feed unreadable.
DEFAULT_UNIVERSE = ("SPY", "QQQ", "IWM", "NVDA", "AAPL", "TSLA", "META",
                     "MSFT", "AMD", "AMZN")


@dataclass
class DaemonStatus:
    running: bool = False
    auto_submit: bool = False         # /auto sets this; /trade reads as scan-only
    started_at: Optional[str] = None
    stopped_at: Optional[str] = None
    last_tick_at: Optional[str] = None
    last_state_changes: int = 0
    last_decision: Optional[dict] = None
    tickers: list[str] = field(default_factory=lambda: list(DEFAULT_UNIVERSE))
    decisions: deque = field(default_factory=lambda: deque(maxlen=80))
    # Current per-ticker setup view, keyed by ticker. Each entry:
    #   { id, ticker, strategy, direction, state, score,
    #     stage: 'FORMING' | 'TRADE',
    #     entry, stop, target, qty, pass_reason, last_evaluated_at }
    # Cards on /trade.html stack-render this dict; empty when state is NEUTRAL
    # or no strategy gate is in scope.
    setups: dict = field(default_factory=dict)
    orders: deque = field(default_factory=lambda: deque(maxlen=20))
    taken_signal_ids: set = field(default_factory=set)                 # de-dup
    error: Optional[str] = None
    market_open: bool = False
    poll_seconds: int = 60

    def to_dict(self) -> dict:
        # Active setups (sorted: TRADE first, then FORMING by score desc)
        setups = [s for s in self.setups.values()
                   if s.get("id") not in self.taken_signal_ids]
        setups.sort(key=lambda s: (
            0 if s.get("stage") == "TRADE" else 1,
            -float(s.get("score") or 0),
        ))
        return {
            "running": self.running,
            "auto_submit": self.auto_submit,
            "started_at": self.started_at,
            "stopped_at": self.stopped_at,
            "last_tick_at": self.last_tick_at,
            "last_state_changes": self.last_state_changes,
            "last_decision": self.last_decision,
            "tickers": list(self.tickers),
            "decisions": list(self.decisions),
            "setups": setups,
            "orders": list(self.orders),
            "error": self.error,
            "market_open": self.market_open,
            "poll_seconds": self.poll_seconds,
        }


# NYSE regular session: 09:30-16:00 ET = 13:30-20:00 UTC (EDT) / 14:30-21:00 (EST).
# We use EDT for the user's current period (Apr 2026 → DST in effect).
def _market_is_open(now_utc: datetime) -> bool:
    t = now_utc.timetz()
    if now_utc.weekday() >= 5:
        return False
    open_t = dt_time(13, 30, tzinfo=timezone.utc)   # 09:30 EDT
    close_t = dt_time(20, 0, tzinfo=timezone.utc)   # 16:00 EDT
    return open_t <= t.replace(tzinfo=timezone.utc) <= close_t


class PaperTrader:
    """Controllable scalp-2 daemon. Singleton — instantiate once in serve."""

    def __init__(self):
        self.status = DaemonStatus()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Per-ticker prior state (for state-change detection)
        self._prior_state: dict[str, object] = {}

    # ─── Control ─────────────────────────────────────────────────────────

    def start(self, tickers: Optional[list[str]] = None,
              poll_seconds: int = 60,
              auto_submit: bool = False) -> dict:
        if self.status.running:
            # Already running — just toggle the auto_submit flag if the call asked.
            self.status.auto_submit = auto_submit
            return {"ok": True, "already_running": True,
                     "auto_submit": auto_submit}
        if tickers:
            self.status.tickers = tickers
        self.status.poll_seconds = poll_seconds
        self.status.auto_submit = auto_submit
        self.status.error = None
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, daemon=True,
                                          name="scalp2-paper-trader")
        self._thread.start()
        self.status.running = True
        self.status.started_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z"
        return {"ok": True, "tickers": list(self.status.tickers),
                 "poll_seconds": poll_seconds,
                 "auto_submit": auto_submit}

    def set_auto_submit(self, auto_submit: bool) -> dict:
        """Toggle auto-submission without restarting the loop."""
        self.status.auto_submit = bool(auto_submit)
        return {"ok": True, "auto_submit": self.status.auto_submit}

    def stop(self) -> dict:
        if not self.status.running:
            return {"ok": False, "error": "not running"}
        self._stop_event.set()
        # Cancel any pending asyncio tasks
        if self._loop is not None and self._loop.is_running():
            self._loop.call_soon_threadsafe(self._loop.stop)
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.status.running = False
        self.status.stopped_at = datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z"
        return {"ok": True}

    # ─── Internals ───────────────────────────────────────────────────────

    def _run(self) -> None:
        """Thread entry point — spins up an asyncio event loop and runs the
        scan loop until _stop_event is set."""
        try:
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            self._loop.run_until_complete(self._scan_loop())
        except Exception as e:
            self.status.error = f"{type(e).__name__}: {e}"
            traceback.print_exc()
        finally:
            try:
                if self._loop is not None and not self._loop.is_closed():
                    self._loop.close()
            except Exception:
                pass
            self.status.running = False

    async def _scan_loop(self) -> None:
        """Main daemon loop. Polls Alpaca every poll_seconds for each ticker
        in the universe, evaluates strategies, submits orders on TRADE."""
        from infra.config_loader import load_thresholds
        from infra.secrets import load_secrets
        load_secrets()
        cfg = load_thresholds()
        from data_clients.alpaca import AlpacaClient

        alp = AlpacaClient()
        try:
            while not self._stop_event.is_set():
                now = datetime.now(tz=timezone.utc)
                self.status.market_open = _market_is_open(now)
                self.status.last_tick_at = now.isoformat(timespec="seconds") + "Z"

                if not self.status.market_open:
                    # Sleep faster outside hours; nothing to do until 13:30 UTC
                    await self._interruptible_sleep(min(60, self.status.poll_seconds))
                    continue

                changes_this_tick = 0
                for ticker in list(self.status.tickers):
                    if self._stop_event.is_set():
                        break
                    try:
                        changed = await self._tick_one(alp, ticker, cfg, now)
                        if changed:
                            changes_this_tick += 1
                    except Exception as e:
                        self._add_decision({
                            "ts": now.isoformat(timespec="seconds") + "Z",
                            "ticker": ticker, "strategy": "—",
                            "decision": "ERROR",
                            "reason": f"{type(e).__name__}: {e}",
                        })

                self.status.last_state_changes = changes_this_tick
                await self._interruptible_sleep(self.status.poll_seconds)
        finally:
            alp.close()

    async def _interruptible_sleep(self, seconds: int) -> None:
        """Sleep `seconds` but wake up early if _stop_event is set."""
        for _ in range(seconds):
            if self._stop_event.is_set():
                return
            await asyncio.sleep(1)

    async def _tick_one(self, alp, ticker: str, cfg: dict,
                          now: datetime) -> bool:
        """Pull recent bars for one ticker, classify scalp state, evaluate
        strategies, fire order on TRADE. Returns True if state changed."""
        from features.builder import build_features
        from features.price_structure import BarOHLC
        from scalp_brain.classifier import classify as classify_scalp_state

        # Fetch last ~50 1-min bars (about 50 minutes of data)
        end = now.isoformat(timespec="seconds")
        start = (now.replace(hour=now.hour - (now.hour % 1)).isoformat(timespec="seconds"))
        try:
            alp_bars = alp.get_bars(symbol=ticker, start=start, end=end,
                                      timeframe="1Min", limit=50)
        except Exception:
            return False
        if len(alp_bars) < 5:
            return False

        bars: list[BarOHLC] = [
            BarOHLC(o=b.o, h=b.h, l=b.l, c=b.c, v=b.v)
            for b in alp_bars
        ]
        last_bar_t = alp_bars[-1].t

        try:
            features = build_features(
                ticker=ticker, bar_idx=len(bars) - 1,
                bars=bars, trades=[], quotes=[],
                gex_snapshot=None, flow_records=[],
                timestamp=last_bar_t,
            )
        except Exception:
            return False

        prior = self._prior_state.get(ticker)
        try:
            new_state = classify_scalp_state(prior, features, cfg)
        except Exception:
            return False

        state_changed = (prior is None or prior.name != new_state.name)
        if state_changed:
            self._prior_state[ticker] = new_state
            await self._evaluate_and_maybe_trade(alp, features, new_state, cfg)
            return True
        # Update age even when no state change
        self._prior_state[ticker] = new_state
        return False

    async def _evaluate_and_maybe_trade(self, alp, features, scalp_state,
                                          cfg: dict) -> None:
        """Always emit a setup card for any trigger state (FORMING or TRADE).
        On TRADE + auto_submit=True, also fire an Alpaca bracket order.

        State → strategy mapping (mirrors scripts/main.py):
          SURGE_REVERSE        → S5 short
          TANK_REVERSE         → S5 long
          SURGE_CONTINUATION   → S3 long
          TANK_CONTINUATION    → S3 short
          SURGE_IGNITION/TANK_IGNITION → S2 (placeholder — needs OR levels)
        """
        from journal.decision_log import log_decision_sync

        decision = None
        strategy = None
        direction = None

        if scalp_state.name in ("SURGE_REVERSE", "TANK_REVERSE"):
            from strategies.s5_gamma_reversal.allow_s5_trade import allow_s5_trade
            strategy = "S5"
            direction = "long" if scalp_state.name == "TANK_REVERSE" else "short"
            decision = allow_s5_trade(
                features=features, model=None,
                threshold=0.50, cfg=cfg,
                risk_manager_allows=True,
                expected_value_net=0.0,
                reversal_score=scalp_state.score,
                candidate_id=f"auto-S5-{features.ticker}-{features.bar_idx}",
            )

        elif scalp_state.name in ("SURGE_CONTINUATION", "TANK_CONTINUATION"):
            from strategies.s3_momentum.allow_s3_trade import allow_s3_trade
            from strategies.s3_momentum.setup import S3SetupContext
            from scalp_brain.scores import reversal_score as rev_score
            strategy = "S3"
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
                candidate_id=f"auto-S3-{features.ticker}-{features.bar_idx}",
            )

        # No strategy in scope for this state → drop any stale setup card and exit
        if decision is None:
            self.status.setups.pop(features.ticker, None)
            return

        # sqlite log (best-effort)
        try:
            log_decision_sync(decision, features, model_version="paper_trader_v0")
        except Exception:
            pass

        # Compute executable plan (entry / stop / target / qty)
        atr = max(features.high - features.low, 0.10)
        entry = features.close
        if direction == "long":
            stop = entry - atr
            target = entry + atr
            side = "buy"
        else:
            stop = entry + atr
            target = entry - atr
            side = "sell"
        risk_per_share = max(abs(entry - stop), 0.01)
        risk_dollars = 500.0
        qty = max(1, int(risk_dollars / risk_per_share))

        stage = "TRADE" if decision.decision == "TRADE" else "FORMING"
        signal_id = f"{strategy}-{features.ticker}-{features.bar_idx}"
        now_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z"

        setup_row = {
            "id": signal_id,
            "ticker": features.ticker,
            "strategy": strategy,
            "direction": direction,
            "side": side,
            "state": scalp_state.name,
            "score": round(scalp_state.score, 3),
            "stage": stage,
            "decision": decision.decision,
            "pass_reason": decision.pass_reason,
            "entry": round(entry, 2),
            "stop": round(stop, 2),
            "target": round(target, 2),
            "qty": qty,
            "atr": round(atr, 2),
            "last_evaluated_at": now_iso,
        }
        # Card persists until state goes back to NEUTRAL (or another state replaces it)
        self.status.setups[features.ticker] = setup_row

        # Decisions feed (audit log)
        self._add_decision({
            "ts": now_iso, "ticker": features.ticker,
            "strategy": strategy, "direction": direction,
            "state": scalp_state.name, "score": setup_row["score"],
            "decision": decision.decision, "reason": decision.pass_reason,
        })
        self.status.last_decision = setup_row

        # Auto-submit only if /auto turned the toggle on
        if stage == "TRADE" and self.status.auto_submit:
            await self._submit_from_setup(alp, setup_row)
            # Mark taken so /trade hides the card
            self.status.taken_signal_ids.add(signal_id)

    async def _submit_from_setup(self, alp, setup: dict) -> dict:
        """Submit an Alpaca bracket from a setup row. Used by both auto-submit
        and the /api/take manual path (via take_setup() below)."""
        from datetime import datetime
        try:
            client_order_id = f"edge-{setup['id']}"
            resp = alp.submit_equity_bracket(
                symbol=setup["ticker"], qty=setup["qty"], side=setup["side"],
                take_profit_price=setup["target"],
                stop_loss_price=setup["stop"],
                client_order_id=client_order_id,
            )
            order_row = {
                "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z",
                "ticker": setup["ticker"], "side": setup["side"].upper(),
                "qty": setup["qty"], "entry": setup["entry"],
                "stop": setup["stop"], "target": setup["target"],
                "strategy": setup["strategy"], "direction": setup["direction"],
                "client_order_id": client_order_id,
                "order_id": (resp or {}).get("id"),
                "status": (resp or {}).get("status", "submitted"),
                "source": "auto" if self.status.auto_submit else "manual",
            }
            self.status.orders.appendleft(order_row)
            return {"ok": True, **order_row}
        except Exception as e:
            err = {
                "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z",
                "ticker": setup["ticker"], "side": setup["side"].upper(),
                "error": f"{type(e).__name__}: {e}",
            }
            self.status.orders.appendleft(err)
            return {"ok": False, **err}

    def take_setup(self, signal_id: str) -> dict:
        """Public entry point for the /api/take HTTP handler. Submits the
        setup card with the given id. Synchronous (run from request thread)."""
        setup = None
        for s in self.status.setups.values():
            if s.get("id") == signal_id:
                setup = s
                break
        if setup is None:
            return {"ok": False, "error": "signal not found (may have expired)"}
        if signal_id in self.status.taken_signal_ids:
            return {"ok": False, "error": "already taken"}
        if setup.get("stage") != "TRADE":
            return {"ok": False, "error": f"signal is {setup.get('stage')}; not TRADE"}
        # Submit synchronously (no asyncio in this path — Alpaca client is sync)
        from data_clients.alpaca import AlpacaClient
        try:
            with AlpacaClient() as alp:
                # Reuse the async submit logic by inlining the sync parts
                client_order_id = f"edge-{setup['id']}"
                resp = alp.submit_equity_bracket(
                    symbol=setup["ticker"], qty=setup["qty"], side=setup["side"],
                    take_profit_price=setup["target"],
                    stop_loss_price=setup["stop"],
                    client_order_id=client_order_id,
                )
            order_row = {
                "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z",
                "ticker": setup["ticker"], "side": setup["side"].upper(),
                "qty": setup["qty"], "entry": setup["entry"],
                "stop": setup["stop"], "target": setup["target"],
                "strategy": setup["strategy"], "direction": setup["direction"],
                "client_order_id": client_order_id,
                "order_id": (resp or {}).get("id"),
                "status": (resp or {}).get("status", "submitted"),
                "source": "manual",
            }
            self.status.orders.appendleft(order_row)
            self.status.taken_signal_ids.add(signal_id)
            return {"ok": True, **order_row}
        except Exception as e:
            return {"ok": False, "error": f"{type(e).__name__}: {e}"}

    def _add_decision(self, row: dict) -> None:
        self.status.decisions.appendleft(row)


# ─── module-level singleton (the HTTP server holds a reference) ─────────────

TRADER = PaperTrader()


if __name__ == "__main__":
    # Manual smoke test
    print("[paper_trader] starting…")
    TRADER.start(tickers=list(DEFAULT_UNIVERSE), poll_seconds=30)
    try:
        import time
        while True:
            time.sleep(10)
            s = TRADER.status
            print(f"[paper_trader] running={s.running} "
                  f"market_open={s.market_open} "
                  f"last_tick={s.last_tick_at} "
                  f"decisions={len(s.decisions)} orders={len(s.orders)}")
    except KeyboardInterrupt:
        TRADER.stop()
        print("[paper_trader] stopped")
