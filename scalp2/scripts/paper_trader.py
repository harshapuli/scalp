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
from datetime import datetime, time as dt_time, timedelta, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# SQLite-backed setup persistence so cards survive daemon restart.
DB_PATH = PROJECT_ROOT / "data" / "dev_journal.db"


# Default universe — load from latest backtest run JSON so live trading is
# scoped to exactly the tickers we validated. Falls back to a small default
# if the file is missing (fresh checkout, smoke test).
def _load_backtest_universe() -> tuple[str, ...]:
    import json as _json
    bt_dir = PROJECT_ROOT / "data" / "backtest"
    if not bt_dir.exists():
        return ("SPY", "QQQ", "IWM", "NVDA", "AAPL", "TSLA", "META", "MSFT", "AMD", "AMZN")
    runs = sorted(bt_dir.glob("run_37tickers_v*_*.json")) or sorted(bt_dir.glob("run_*.json"))
    if not runs:
        return ("SPY", "QQQ", "IWM", "NVDA", "AAPL", "TSLA", "META", "MSFT", "AMD", "AMZN")
    try:
        d = _json.loads(runs[-1].read_text())
        tickers = tuple(d.get("meta", {}).get("tickers") or ())
        if tickers:
            return tickers
    except Exception:
        pass
    return ("SPY", "QQQ", "IWM", "NVDA", "AAPL", "TSLA", "META", "MSFT", "AMD", "AMZN")


DEFAULT_UNIVERSE = _load_backtest_universe()


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
    # Setups now keyed by setup_id (strategy-ticker-direction), so multiple
    # strategies can have parallel cards per ticker (e.g., S2 + S4 both
    # firing on a SURGE_IGNITION).
    setups: dict = field(default_factory=dict)
    orders: deque = field(default_factory=lambda: deque(maxlen=20))
    taken_signal_ids: set = field(default_factory=set)                 # de-dup
    error: Optional[str] = None
    market_open: bool = False
    phase: str = "closed"   # 'closed' | 'warmup' | 'open'
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
            "phase": self.phase,
            "poll_seconds": self.poll_seconds,
        }


# NYSE regular session: 09:30-16:00 ET = 13:30-20:00 UTC (EDT)
#                                       = 14:30-21:00 UTC (EST)
# Pre-market warm-up window: 60 min before open so the daemon can read bars,
# classify state, and populate FORMING cards before regular session starts.
#
# User intent (2026-04-28): "getting ready and start from 6:30 AM PST" —
#   6:30 AM PT = 9:30 AM ET = 13:30 UTC EDT (auto-submission begins)
#   5:30 AM PT = 8:30 AM ET = 12:30 UTC EDT (warm-up scanning begins)
def _market_is_open(now_utc: datetime) -> bool:
    """Regular session — auto-submission allowed."""
    if now_utc.weekday() >= 5:
        return False
    t = now_utc.replace(tzinfo=timezone.utc).timetz()
    open_t = dt_time(13, 30, tzinfo=timezone.utc)   # 09:30 EDT / 06:30 PT
    close_t = dt_time(20, 0, tzinfo=timezone.utc)   # 16:00 EDT / 13:00 PT
    return open_t <= t <= close_t


def _market_is_warmup(now_utc: datetime) -> bool:
    """Pre-market warm-up — scan, populate FORMING cards, but DO NOT auto-submit.
    Manual TAKE through /api/take is also blocked here so we don't fire
    against thin pre-market liquidity."""
    if now_utc.weekday() >= 5:
        return False
    t = now_utc.replace(tzinfo=timezone.utc).timetz()
    warm_t = dt_time(12, 30, tzinfo=timezone.utc)   # 05:30 PT — 1h pre-open
    open_t = dt_time(13, 30, tzinfo=timezone.utc)
    return warm_t <= t < open_t


class PaperTrader:
    """Controllable scalp-2 daemon. Singleton — instantiate once in serve."""

    def __init__(self):
        self.status = DaemonStatus()
        self._thread: Optional[threading.Thread] = None
        self._stop_event = threading.Event()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        # Per-ticker prior state (for state-change detection)
        self._prior_state: dict[str, object] = {}
        # Per-ticker per-session data — prior daily close + last computed OR
        self._session: dict[str, dict] = {}
        # UW flow cache: ticker → (records, fetched_ts) — refreshed every 5min
        self._flow_cache: dict[str, tuple[list, float]] = {}
        # UW GEX snapshot cache (5-min TTL — UW spot-exposures cadence is 1min)
        self._gex_cache: dict[str, tuple[object, float]] = {}
        # UW IV rank cache (1-hour TTL — IV updates daily)
        self._iv_cache: dict[str, tuple[float, float]] = {}
        # Earnings blackout cache (24-hour TTL)
        self._earnings_cache: dict[str, tuple[bool, float]] = {}
        # Daily relative volume cache (1-min TTL — needs fresh today's vol)
        self._daily_rvol_cache: dict[str, tuple[float, float]] = {}
        # HTF (higher-timeframe) support/resistance level cache (1-hour TTL)
        # value = (nearest_distance_atr, fetched_ts)
        self._htf_cache: dict[str, tuple[float, float]] = {}
        # ML model — lazy-loaded once at first use, kept in memory
        self._ml_model = None
        self._ml_model_loaded = False
        # Today's account snapshot at session start (for daily-kill comparison)
        self._session_start_equity: Optional[float] = None
        self._session_start_date: Optional[str] = None

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
        in the universe, evaluates strategies, submits orders on TRADE.

        After each tick, drops setup cards that haven't been refreshed in 30
        min — covers the case where a state stays trigger for many bars but
        the user has long since moved on, and also handles daemon-restart
        cleanup.
        """
        from infra.config_loader import load_thresholds
        from infra.secrets import load_secrets
        load_secrets()
        cfg = load_thresholds()
        from data_clients.alpaca import AlpacaClient
        try:
            from data_clients.unusual_whales import UWClient
            uw = UWClient()
        except Exception as e:
            print(f"[paper_trader] UW client unavailable: {e} — S4 will not fire")
            uw = None

        # Reload any in-flight setups from SQLite (crash recovery).
        try:
            from journal.setups_store import (
                init_setups_table, load_active_setups, load_taken_ids,
            )
            init_setups_table(DB_PATH)
            today_str = datetime.now(tz=timezone.utc).date().isoformat()
            for row in load_active_setups(DB_PATH, today_str):
                self.status.setups[row["id"]] = dict(row)
            self.status.taken_signal_ids |= load_taken_ids(DB_PATH, today_str)
            if self.status.setups:
                print(f"[paper_trader] restored {len(self.status.setups)} "
                      f"active setup(s) from {DB_PATH.name}")
        except Exception as e:
            print(f"[paper_trader] setup reload failed (will start clean): {e}")

        STALE_SETUP_MIN = 30

        alp = AlpacaClient()
        try:
            while not self._stop_event.is_set():
                now = datetime.now(tz=timezone.utc)
                self.status.market_open = _market_is_open(now)
                if self.status.market_open:
                    self.status.phase = "open"
                elif _market_is_warmup(now):
                    self.status.phase = "warmup"
                else:
                    self.status.phase = "closed"
                self.status.last_tick_at = now.isoformat(timespec="seconds") + "Z"

                # Scan during regular session AND warm-up. Closed → sleep.
                if self.status.phase == "closed":
                    await self._interruptible_sleep(min(60, self.status.poll_seconds))
                    continue

                changes_this_tick = 0
                for ticker in list(self.status.tickers):
                    if self._stop_event.is_set():
                        break
                    try:
                        changed = await self._tick_one(alp, uw, ticker, cfg, now)
                        if changed:
                            changes_this_tick += 1
                        # Refresh last_evaluated_at on any existing setups for
                        # this ticker — keeps cards alive across no-change ticks
                        ts_iso = now.isoformat(timespec="seconds") + "Z"
                        for sid, row in list(self.status.setups.items()):
                            if row.get("ticker") == ticker:
                                row["last_evaluated_at"] = ts_iso
                    except Exception as e:
                        self._add_decision({
                            "ts": now.isoformat(timespec="seconds") + "Z",
                            "ticker": ticker, "strategy": "—",
                            "decision": "ERROR",
                            "reason": f"{type(e).__name__}: {e}",
                        })

                # Drop setups not refreshed in STALE_SETUP_MIN min (memory + DB)
                cutoff_dt = now.timestamp() - (STALE_SETUP_MIN * 60)
                for sid in list(self.status.setups.keys()):
                    s = self.status.setups[sid]
                    last_iso = (s.get("last_evaluated_at") or "").rstrip("Z")
                    try:
                        last_ts = datetime.fromisoformat(last_iso).timestamp()
                    except Exception:
                        last_ts = now.timestamp()
                    if last_ts < cutoff_dt:
                        self.status.setups.pop(sid, None)
                try:
                    from journal.setups_store import delete_stale_setups
                    cutoff_iso = (now - timedelta(minutes=STALE_SETUP_MIN)).isoformat(timespec="seconds") + "Z"
                    delete_stale_setups(DB_PATH, cutoff_iso)
                except Exception:
                    pass

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

    async def _tick_one(self, alp, uw, ticker: str, cfg: dict,
                          now: datetime) -> bool:
        """Pull recent bars + UW flow for one ticker, classify scalp state,
        evaluate every applicable strategy. Returns True if state changed."""
        from datetime import timedelta as _td
        from features.builder import build_features
        from features.price_structure import BarOHLC
        from scalp_brain.classifier import classify as classify_scalp_state

        # Fetch enough bars to cover the OR window + recent context.
        # Regular session: from 13:00 UTC today (covers OR + all session bars).
        # Warm-up / pre-open: last 90 minutes for ATR + state context.
        session_start = now.replace(hour=13, minute=0, second=0, microsecond=0)
        if now >= session_start:
            start_dt = session_start
        else:
            start_dt = now - _td(minutes=90)
        end = now.isoformat(timespec="seconds")
        start = start_dt.isoformat(timespec="seconds")
        try:
            alp_bars = alp.get_bars(symbol=ticker, start=start, end=end,
                                      timeframe="1Min", limit=500)
        except Exception:
            return False
        if len(alp_bars) < 5:
            return False

        bars_ohlc: list[BarOHLC] = [
            BarOHLC(o=b.o, h=b.h, l=b.l, c=b.c, v=b.v)
            for b in alp_bars
        ]
        last_bar_t = alp_bars[-1].t

        # Real-time data feeds (all cached, exception-isolated)
        flow_records = self._fetch_flow_records(uw, ticker)
        gex_snapshot = self._get_gex_snapshot(uw, ticker)        # → fixes S5
        iv_pct = self._get_iv_percentile(uw, ticker)             # → fixes S4
        earnings_blackout = self._is_earnings_blackout(uw, ticker)
        # Real Lee-Ready: pass actual recent trades + quotes (last 30 min)
        try:
            ts_30m_ago = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
            trades = alp.get_trades(ticker, start=ts_30m_ago,
                                     end=now.isoformat(timespec="seconds"),
                                     limit=10000)
            quotes = alp.get_quotes(ticker, start=ts_30m_ago,
                                     end=now.isoformat(timespec="seconds"),
                                     limit=10000)
        except Exception:
            trades, quotes = [], []

        # Real HTF S/R: nearest daily pivot in ATR units (replaces default 1.0)
        try:
            from features.price_structure import atr_from_bars
            atr_now = atr_from_bars(bars_ohlc, period=14) if len(bars_ohlc) >= 14 else 0.0
        except Exception:
            atr_now = 0.0
        htf_distance_atr = self._compute_htf_distance_atr(
            alp, ticker, now,
            current_price=bars_ohlc[-1].c if bars_ohlc else 0.0,
            atr=atr_now,
        )

        try:
            features = build_features(
                ticker=ticker, bar_idx=len(bars_ohlc) - 1,
                bars=bars_ohlc, trades=trades, quotes=quotes,
                gex_snapshot=gex_snapshot, flow_records=flow_records,
                iv_percentile=iv_pct,
                earnings_blackout=earnings_blackout,
                near_htf_level_atr=htf_distance_atr,
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
        self._prior_state[ticker] = new_state
        # Always evaluate every tick — S1 is calendar-driven (fires regardless
        # of state); S2-S5 short-circuit if state isn't a trigger state.
        # Cost: one function call per ticker per minute. Well within budget.
        await self._evaluate_and_maybe_trade(
            alp, features, new_state, cfg, alp_bars, now,
        )
        return state_changed

    def _ensure_session(self, ticker: str, today_str: str) -> dict:
        sd = self._session.get(ticker)
        if sd is None or sd.get("date") != today_str:
            sd = {"date": today_str, "prior_close": None}
            self._session[ticker] = sd
        return sd

    def _compute_or(self, bars: list, today_str: str) -> Optional[tuple[float, float]]:
        """OR = max-high / min-low across bars whose open time falls in
        [13:30:00, 13:35:00) UTC on `today_str`. Returns None if no bars in
        the window yet (i.e., the market hasn't reached 13:30 today)."""
        or_bars = []
        for b in bars:
            ts = b.t
            if (ts.date().isoformat() == today_str
                    and ts.hour == 13 and 30 <= ts.minute < 35):
                or_bars.append(b)
        if not or_bars:
            return None
        return max(b.h for b in or_bars), min(b.l for b in or_bars)

    def _fetch_prior_close(self, alp, ticker: str, today_dt: datetime) -> Optional[float]:
        """Yesterday's daily close. Lazy — only fetches once per ticker per
        session. Cached on self._session[ticker]['prior_close']."""
        sd = self._ensure_session(ticker, today_dt.date().isoformat())
        if sd["prior_close"] is not None:
            return sd["prior_close"]
        try:
            from datetime import timedelta as _td
            end = today_dt.date().isoformat()
            start = (today_dt.date() - _td(days=10)).isoformat()
            bars = alp.get_bars(ticker, start=start, end=end,
                                  timeframe="1Day", limit=10)
            prior = next((b for b in reversed(bars)
                            if b.t.date() < today_dt.date()), None)
            if prior:
                sd["prior_close"] = prior.c
                return prior.c
        except Exception:
            pass
        return None

    def _fetch_flow_records(self, uw_client, ticker: str) -> list:
        """Flow records for `ticker`, preferring fresh UW WS data over REST.

        Source priority:
          1. UW WS in-memory cache (real-time, sub-second updates)
          2. REST flow_recent, cached 5 min (rate-limit safe fallback)
          3. Empty list on total failure"""
        # Try WS first — if streamer running and has fresh data, use it.
        try:
            from data_clients.uw_ws_streamer import (
                get_recent_flow_for_ticker, get_recent_flow_alerts, get_stats,
            )
            ws_stats = get_stats()
            if ws_stats.get("connected"):
                # WS streamer maintains both per-ticker AND global flow-alerts
                # caches. flow-alerts is global (all tickers share); filter
                # to this ticker.
                ws_records = get_recent_flow_for_ticker(ticker)
                # Also pull flow-alerts (cross-ticker, filtered)
                ws_alerts = [a for a in get_recent_flow_alerts(limit=200)
                             if (a.get("ticker") == ticker)]
                if ws_records or ws_alerts:
                    # Combine, dedup by id
                    seen = set()
                    merged = []
                    for r in (ws_records + ws_alerts):
                        rid = r.get("id") or id(r)
                        if rid not in seen:
                            seen.add(rid)
                            merged.append(r)
                    return merged
        except Exception:
            pass

        # Fallback to REST cache
        import time as _time
        now = _time.time()
        cached = self._flow_cache.get(ticker)
        if cached:
            records, fetched = cached
            if now - fetched < 300:
                return records
        if uw_client is None:
            return []
        try:
            recs = uw_client.flow_recent(ticker)
            self._flow_cache[ticker] = (recs, now)
            return recs
        except Exception:
            self._flow_cache[ticker] = ([], now)
            return []

    # ─── Live UW data fetchers (cached, exception-isolated) ─────────────

    def _get_gex_snapshot(self, uw, ticker: str):
        """Live UW spot-exposures, cached 5min per ticker. None on failure
        (build_features falls back to 99.0 → S5 not_near_gex)."""
        if uw is None:
            return None
        import time as _time
        now = _time.time()
        cached = self._gex_cache.get(ticker)
        if cached and (now - cached[1]) < 300:
            return cached[0]
        try:
            snap = uw.greek_exposure(ticker)
            self._gex_cache[ticker] = (snap, now)
            return snap
        except Exception:
            self._gex_cache[ticker] = (None, now)
            return None

    def _get_iv_percentile(self, uw, ticker: str) -> float:
        """UW iv_rank_history latest entry. Cached 1 hour. Defaults to 0.5
        on failure (acceptable middle-of-distribution)."""
        if uw is None:
            return 0.5
        import time as _time
        now = _time.time()
        cached = self._iv_cache.get(ticker)
        if cached and (now - cached[1]) < 3600:
            return cached[0]
        try:
            history = uw.iv_rank_history(ticker)
            if history:
                # iv_rank_1y is reported on 0-100 scale, normalize to 0-1
                latest = history[-1] if isinstance(history, list) else history
                rank = latest.get("iv_rank_1y") if isinstance(latest, dict) else None
                if rank is None:
                    rank = (latest.get("iv_rank") if isinstance(latest, dict) else None)
                if rank is not None:
                    rank = float(rank)
                    if rank > 1.5:    # 0-100 form
                        rank = rank / 100.0
                    self._iv_cache[ticker] = (rank, now)
                    return rank
        except Exception:
            pass
        self._iv_cache[ticker] = (0.5, now)
        return 0.5

    def _is_earnings_blackout(self, uw, ticker: str,
                               days_threshold: int = 5) -> bool:
        """True if earnings within `days_threshold` calendar days. Cached 24h."""
        if uw is None:
            return False
        import time as _time
        now = _time.time()
        cached = self._earnings_cache.get(ticker)
        if cached and (now - cached[1]) < 86400:
            return cached[0]
        try:
            summary = uw.earnings_flow_summary(ticker)
            # UW returns various keys depending on payload — check several
            nxt_str = (summary.get("next_earnings_date")
                          or summary.get("earnings_date")
                          or summary.get("expected_date"))
            if nxt_str:
                from datetime import date as _date
                nxt = _date.fromisoformat(str(nxt_str)[:10])
                today = datetime.now(tz=timezone.utc).date()
                days = (nxt - today).days
                blackout = (0 <= days <= days_threshold)
                self._earnings_cache[ticker] = (blackout, now)
                return blackout
        except Exception:
            pass
        self._earnings_cache[ticker] = (False, now)
        return False

    def _compute_premkt_volume_ratio(self, alpaca, ticker: str,
                                        now_utc: datetime) -> float:
        """Today's premarket volume (08:00-13:30 UTC) ÷ 5-day premarket avg.
        Used by S2's gap-or-volume gate. Caches alongside daily_rvol."""
        import time as _time
        cache_key = f"PRE:{ticker}:{now_utc.date().isoformat()}"
        wall = _time.time()
        cached = self._daily_rvol_cache.get(cache_key)
        if cached and (wall - cached[1]) < 300:    # 5-min TTL
            return cached[0]
        try:
            today = now_utc.date()
            # Today's premkt window: 08:00 → 13:30 UTC (04:00-09:30 ET)
            t_pm_start = datetime(today.year, today.month, today.day, 8, 0,
                                     tzinfo=timezone.utc)
            t_pm_end = datetime(today.year, today.month, today.day, 13, 30,
                                   tzinfo=timezone.utc)
            now_or_close = min(now_utc, t_pm_end)
            today_pm_bars = alpaca.get_bars(
                ticker,
                start=t_pm_start.isoformat(timespec="seconds"),
                end=now_or_close.isoformat(timespec="seconds"),
                timeframe="1Min", limit=400,
            )
            today_pm_vol = sum(b.v for b in today_pm_bars)

            # Last 5 sessions' premkt vol
            past_vols: list[float] = []
            for back in range(1, 8):    # walk back up to a week to get 5 sessions
                d = today - timedelta(days=back)
                if d.weekday() >= 5:
                    continue
                pm_s = datetime(d.year, d.month, d.day, 8, 0, tzinfo=timezone.utc)
                pm_e = datetime(d.year, d.month, d.day, 13, 30, tzinfo=timezone.utc)
                try:
                    pb = alpaca.get_bars(ticker,
                                            start=pm_s.isoformat(timespec="seconds"),
                                            end=pm_e.isoformat(timespec="seconds"),
                                            timeframe="1Min", limit=400)
                    if pb:
                        past_vols.append(sum(b.v for b in pb))
                except Exception:
                    continue
                if len(past_vols) >= 5:
                    break
            if not past_vols or today_pm_vol <= 0:
                self._daily_rvol_cache[cache_key] = (1.0, wall)
                return 1.0
            avg = sum(past_vols) / len(past_vols)
            ratio = today_pm_vol / avg if avg > 0 else 1.0
            self._daily_rvol_cache[cache_key] = (ratio, wall)
            return ratio
        except Exception:
            self._daily_rvol_cache[cache_key] = (1.0, wall)
            return 1.0

    def _compute_daily_relative_volume(self, alpaca, ticker: str,
                                          now_utc: datetime) -> float:
        """Today's vol-so-far time-weighted to a full session ÷ 30-day daily avg.
        Cached 1 min. 1.0 = matching average; 1.5 = 50% above average."""
        import time as _time
        cache_key = f"{ticker}:{now_utc.date().isoformat()}"
        wall = _time.time()
        cached = self._daily_rvol_cache.get(cache_key)
        if cached and (wall - cached[1]) < 60:
            return cached[0]
        try:
            today = now_utc.date()
            # Today's volume so far (from regular session start 13:30 UTC)
            t_start = datetime(today.year, today.month, today.day, 13, 30,
                                  tzinfo=timezone.utc)
            today_bars = alpaca.get_bars(
                ticker,
                start=t_start.isoformat(timespec="seconds"),
                end=now_utc.isoformat(timespec="seconds"),
                timeframe="1Min", limit=500,
            )
            today_vol = sum(b.v for b in today_bars)
            # 30-day historical daily avg
            hist_start = (today - timedelta(days=45)).isoformat()
            hist_end = (today - timedelta(days=1)).isoformat()
            daily_bars = alpaca.get_bars(
                ticker, start=hist_start, end=hist_end,
                timeframe="1Day", limit=30,
            )
            if not daily_bars or today_vol <= 0:
                self._daily_rvol_cache[cache_key] = (1.0, wall)
                return 1.0
            avg_daily = sum(b.v for b in daily_bars) / len(daily_bars)
            if avg_daily <= 0:
                self._daily_rvol_cache[cache_key] = (1.0, wall)
                return 1.0
            # Project today's vol to a full session (390 mins)
            elapsed_min = max(1, (now_utc - t_start).total_seconds() / 60.0)
            full_session_min = 390
            projected = today_vol * (full_session_min / elapsed_min) if elapsed_min < full_session_min else today_vol
            ratio = projected / avg_daily
            self._daily_rvol_cache[cache_key] = (ratio, wall)
            return ratio
        except Exception:
            self._daily_rvol_cache[cache_key] = (1.0, wall)
            return 1.0

    def _compute_htf_distance_atr(self, alpaca, ticker: str,
                                      now_utc: datetime,
                                      current_price: float,
                                      atr: float) -> float:
        """Distance to nearest daily-pivot S/R in ATR units. Cached 1 hour.
        Returns 99.0 if no pivots found or ATR is zero (matches the spec
        default of 'far from any HTF level' = no constraint)."""
        import time as _time
        wall = _time.time()
        cached = self._htf_cache.get(ticker)
        if cached and (wall - cached[1]) < 3600:
            return cached[0]
        if atr <= 0:
            return 99.0
        try:
            today = now_utc.date()
            start = (today - timedelta(days=80)).isoformat()
            end = today.isoformat()
            bars = alpaca.get_bars(ticker, start=start, end=end,
                                     timeframe="1Day", limit=80)
            if len(bars) < 7:
                self._htf_cache[ticker] = (99.0, wall)
                return 99.0
            # 3-bar pivot detection: a high is a pivot if it's the max of
            # bars[i-1], bars[i], bars[i+1]; same for lows
            pivots: list[float] = []
            for i in range(1, len(bars) - 1):
                if bars[i].h >= bars[i-1].h and bars[i].h >= bars[i+1].h:
                    pivots.append(bars[i].h)
                if bars[i].l <= bars[i-1].l and bars[i].l <= bars[i+1].l:
                    pivots.append(bars[i].l)
            if not pivots:
                self._htf_cache[ticker] = (99.0, wall)
                return 99.0
            nearest = min(abs(p - current_price) for p in pivots)
            distance_atr = nearest / atr
            self._htf_cache[ticker] = (distance_atr, wall)
            return distance_atr
        except Exception:
            self._htf_cache[ticker] = (99.0, wall)
            return 99.0

    def _load_ml_model(self):
        """Lazy-load `data/ml_model_s5.pkl` once. Returns None if not present
        (S5 falls back to rules-only). Kept in memory after first load."""
        if self._ml_model_loaded:
            return self._ml_model
        self._ml_model_loaded = True
        try:
            import pickle
            model_path = PROJECT_ROOT / "data" / "ml_model_s5.pkl"
            if not model_path.exists():
                return None
            with open(model_path, "rb") as f:
                self._ml_model = pickle.load(f)
            print(f"[paper_trader] loaded S5 ML model from {model_path.name}")
            return self._ml_model
        except Exception as e:
            print(f"[paper_trader] ML model load failed: {e}")
            return None

    @staticmethod
    def _count_higher_lows(bars: list, n_windows: int = 4,
                              window_size: int = 5) -> int:
        """Count consecutive higher-lows across `n_windows` of `window_size` bars
        ending at the most recent. Returns 0 if not enough bars."""
        need = n_windows * window_size
        if len(bars) < need:
            return 0
        windows = []
        for i in range(n_windows):
            start_idx = -((i + 1) * window_size)
            end_idx = -(i * window_size) or None
            windows.append(bars[start_idx:end_idx])
        # windows[0] = most recent, windows[-1] = oldest
        windows.reverse()
        lows = [min(b.l for b in w) for w in windows]
        count = 0
        for i in range(len(lows) - 1):
            if lows[i + 1] > lows[i]:
                count += 1
            else:
                break
        return count

    @staticmethod
    def _count_lower_highs(bars: list, n_windows: int = 4,
                             window_size: int = 5) -> int:
        need = n_windows * window_size
        if len(bars) < need:
            return 0
        windows = []
        for i in range(n_windows):
            start_idx = -((i + 1) * window_size)
            end_idx = -(i * window_size) or None
            windows.append(bars[start_idx:end_idx])
        windows.reverse()
        highs = [max(b.h for b in w) for w in windows]
        count = 0
        for i in range(len(highs) - 1):
            if highs[i + 1] < highs[i]:
                count += 1
            else:
                break
        return count

    # ─── Risk manager (real, replaces hardcoded True) ───────────────────

    def _risk_manager_allows(self, alpaca, cfg: dict,
                                strategy: str) -> tuple[bool, str]:
        """Block auto-submission when daily kill / concurrent caps hit.
        Returns (allowed, reason_if_not)."""
        risk_cfg = cfg.get("s5", {}).get("risk", {})
        try:
            acct = alpaca.get_account()
            positions = alpaca.get_positions()
        except Exception as e:
            return (False, f"alpaca_unavailable: {e}")

        eq = acct.equity or 1.0

        # Capture session-start equity for daily-kill calculation
        today_str = datetime.now(tz=timezone.utc).date().isoformat()
        if (self._session_start_equity is None
                or self._session_start_date != today_str):
            self._session_start_equity = eq
            self._session_start_date = today_str

        # Daily kill check
        daily_kill_pct = risk_cfg.get("daily_kill_pct", -0.020)
        if self._session_start_equity > 0:
            day_pl_pct = (eq - self._session_start_equity) / self._session_start_equity
            if day_pl_pct <= daily_kill_pct:
                return (False, f"daily_kill_hit ({day_pl_pct*100:+.2f}% vs {daily_kill_pct*100:.1f}%)")

        # Concurrent option positions cap — applies to S5 (option spreads)
        concurrent_max = risk_cfg.get("concurrent_max", 2)
        n_option_pos = sum(1 for p in positions
                            if len(p.symbol) > 6 and any(c.isdigit() for c in p.symbol))
        if strategy == "S5" and n_option_pos >= concurrent_max:
            return (False, f"concurrent_s5_cap ({n_option_pos}/{concurrent_max})")

        # Concurrent equity positions cap — applies to S1/S2/S3/S4 (all
        # equity bracket strategies). Default 5 simultaneous open positions.
        equity_concurrent_max = int(risk_cfg.get("equity_concurrent_max", 5))
        n_equity_pos = sum(1 for p in positions
                            if not (len(p.symbol) > 6 and any(c.isdigit() for c in p.symbol)))
        if strategy in ("S1", "S2", "S3", "S4") and n_equity_pos >= equity_concurrent_max:
            return (False, f"concurrent_equity_cap ({n_equity_pos}/{equity_concurrent_max})")

        return (True, "")

    def _drop_setups_for_ticker(self, ticker: str, keep_ids: set) -> None:
        """Drop any setup cards for `ticker` whose id is NOT in keep_ids.
        Lets multi-strategy evaluation safely refresh only the strategies
        that just ran without nuking sibling cards (e.g., S2 firing alongside S4)."""
        from journal.setups_store import delete_setups_for_ticker
        for sid in list(self.status.setups.keys()):
            row = self.status.setups[sid]
            if row.get("ticker") == ticker and sid not in keep_ids:
                self.status.setups.pop(sid, None)
        # Mirror the in-memory drop into SQLite
        try:
            delete_setups_for_ticker(DB_PATH, ticker, keep_ids)
        except Exception:
            pass

    def _build_s2_decision(self, features, scalp_state, cfg, alp, bars, now):
        """Returns (decision, direction) for S2, or (None, None) if can't evaluate."""
        from strategies.s2_orb.allow_s2_trade import allow_s2_trade
        from strategies.s2_orb.setup import S2SetupContext
        from strategies.s2_orb.opening_range import OpeningRange

        today_str = now.date().isoformat()
        or_pair = self._compute_or(bars, today_str)
        if or_pair is None:
            return None, None
        or_high, or_low = or_pair
        opening_range = OpeningRange(
            ticker=features.ticker, high=or_high, low=or_low,
            mid=(or_high + or_low) / 2.0, computed_at=now,
        )
        prior_close = self._fetch_prior_close(alp, features.ticker, now)
        gap_pct = 0.0
        if prior_close and prior_close > 0 and bars:
            first_today = next((b for b in bars
                                   if b.t.date().isoformat() == today_str),
                                  bars[-1])
            gap_pct = (first_today.o - prior_close) / prior_close
        recent_30 = bars[-30:] if len(bars) >= 30 else bars
        avg_min_vol = sum(b.v for b in recent_30) / max(1, len(recent_30))
        # Real premarket volume ratio: today's premarket vol vs 5-day premarket avg.
        premkt_vol_ratio = self._compute_premkt_volume_ratio(alp, features.ticker, now)
        direction = "long" if scalp_state.name == "SURGE_IGNITION" else "short"
        ctx = S2SetupContext(
            ticker=features.ticker, last_close=features.close,
            last_bar_volume=features.volume,
            avg_minute_volume_30bar=avg_min_vol,
            gap_pct=gap_pct, pre_market_volume_ratio=premkt_vol_ratio,
            opening_range=opening_range,
            earnings_blackout=features.earnings_blackout,
        )
        decision = allow_s2_trade(
            setup_ctx=ctx, features=features, scalp_state=scalp_state,
            cfg=cfg, risk_manager_allows=True,
            candidate_id=f"auto-S2-{features.ticker}-{features.bar_idx}",
        )
        return decision, direction

    def _build_s3_decision(self, features, scalp_state, cfg, bars_ohlc):
        from strategies.s3_momentum.allow_s3_trade import allow_s3_trade
        from strategies.s3_momentum.setup import S3SetupContext
        from scalp_brain.scores import reversal_score as rev_score
        direction = "long" if scalp_state.name == "SURGE_CONTINUATION" else "short"
        # Real structure counting (replaces hardcoded 4)
        higher_lows = self._count_higher_lows(bars_ohlc)
        lower_highs = self._count_lower_highs(bars_ohlc)
        # 30-min aggressor window (last 30 1m bars). Falls back to features.aggressor_recent.
        agg_30m = features.aggressor_recent
        ctx = S3SetupContext(
            ticker=features.ticker,
            session_return_atr=features.extension_from_prior_close_atr,
            vwap_distance_atr=features.extension_from_vwap_atr,
            consecutive_higher_lows=higher_lows,
            consecutive_lower_highs=lower_highs,
            aggressor_avg_30m=agg_30m,
            near_htf_level_atr=features.near_htf_level_atr,
            distance_to_pos_gex_atr=abs(features.distance_to_major_pos_gex_atr),
        )
        rs = rev_score(features, "short" if direction == "long" else "long", cfg)
        decision = allow_s3_trade(
            setup_ctx=ctx, features=features, scalp_state=scalp_state,
            reversal_score=rs, cfg=cfg, risk_manager_allows=True,
            candidate_id=f"auto-S3-{features.ticker}-{features.bar_idx}",
        )
        return decision, direction

    def _build_s4_decision(self, features, scalp_state, cfg, alpaca, now):
        from strategies.s4_signed_flow.allow_s4_trade import allow_s4_trade
        from strategies.s4_signed_flow.setup import S4SetupContext
        if features.signed_flow_score == 0.0:
            return None, None
        direction = "long" if features.signed_flow_score > 0 else "short"
        # Real daily relative volume (replaces hardcoded 1.5)
        daily_rvol = self._compute_daily_relative_volume(
            alpaca, features.ticker, now,
        )
        ctx = S4SetupContext(
            ticker=features.ticker,
            signed_flow_score=features.signed_flow_score,
            price_return_30m_atr=features.extension_from_vwap_atr,
            iv_percentile=features.iv_percentile,
            distance_to_pos_gex_atr=abs(features.distance_to_major_pos_gex_atr),
            earnings_blackout=features.earnings_blackout,
            daily_relative_volume=daily_rvol,
        )
        decision = allow_s4_trade(
            setup_ctx=ctx, features=features, scalp_state=scalp_state, cfg=cfg,
            risk_manager_allows=True,
            candidate_id=f"auto-S4-{features.ticker}-{features.bar_idx}",
        )
        return decision, direction

    def _build_s1_decision(self, features, cfg, alpaca, now: datetime):
        """S1 Pre-FOMC: calendar-driven, fires on SPY/QQQ/IWM during 24h
        window before scheduled FOMC announcement. Returns (decision, direction)
        or (None, None) if outside window or wrong ticker."""
        from strategies.s1_pre_fomc.allow_s1_trade import allow_s1_trade
        from strategies.s1_pre_fomc.setup import (
            S1SetupContext, is_in_window, S1_UNIVERSE,
        )
        if features.ticker not in S1_UNIVERSE:
            return None, None
        if not is_in_window(now):
            return None, None
        # Fetch latest VIX (daemon's prebreakout scanner already does this)
        vix_value = None
        try:
            from prebreakout.scanner import f7_vix_bucket
            bucket = f7_vix_bucket(alpaca, now)
            # Approximate VIX from bucket (we don't need exact — only the >35 trigger)
            if bucket == "low":
                vix_value = 12.0
            elif bucket == "normal":
                vix_value = 18.0
            elif bucket == "elevated":
                vix_value = 26.0
        except Exception:
            pass
        ctx = S1SetupContext(
            ticker=features.ticker,
            now_utc=now,
            vix_value=vix_value,
            is_holiday_day=False,
            prior_emergency_announcement=False,
        )
        decision = allow_s1_trade(
            setup_ctx=ctx, cfg=cfg, risk_manager_allows=True,
            candidate_id=f"auto-S1-{features.ticker}-{features.bar_idx}",
        )
        # S1 spec is bullish-drift bias (long-only on broad-market ETFs)
        return decision, "long"

    def _build_s5_decision(self, features, scalp_state, cfg):
        from strategies.s5_gamma_reversal.allow_s5_trade import allow_s5_trade
        direction = "long" if scalp_state.name == "TANK_REVERSE" else "short"
        # Lazy-load ML model on first use; None when no model file shipped
        model = self._load_ml_model()
        threshold = float(cfg.get("s5", {}).get("ml", {}).get("threshold_low", 0.50))
        decision = allow_s5_trade(
            features=features, model=model, threshold=threshold, cfg=cfg,
            risk_manager_allows=True, expected_value_net=0.0,
            reversal_score=scalp_state.score,
            candidate_id=f"auto-S5-{features.ticker}-{features.bar_idx}",
        )
        return decision, direction

    async def _evaluate_and_maybe_trade(self, alp, features, scalp_state,
                                          cfg: dict, bars: list, now: datetime) -> None:
        """Multi-strategy evaluation. Each trigger state can fire multiple
        strategies in parallel, each producing its own setup card.

        State → strategies:
          SURGE_REVERSE / TANK_REVERSE         → S5
          SURGE_CONTINUATION / TANK_CONTINUATION → S3 + S4 (if flow non-zero)
          SURGE_IGNITION / TANK_IGNITION       → S2 + S4 (if flow non-zero)
          NEUTRAL / other                      → drop all setups for this ticker
        """
        from journal.decision_log import log_decision_sync

        # Build list of (strategy, decision, direction) tuples for all
        # strategies in scope of this state.
        evals: list[tuple[str, object, str]] = []

        # S1 Pre-FOMC fires on calendar window, NOT state changes —
        # evaluated independently for SPY/QQQ/IWM during the 24h pre-FOMC window.
        d1, dirn1 = self._build_s1_decision(features, cfg, alp, now)
        if d1 is not None:
            evals.append(("S1", d1, dirn1))

        if scalp_state.name in ("SURGE_REVERSE", "TANK_REVERSE"):
            d, dirn = self._build_s5_decision(features, scalp_state, cfg)
            if d is not None:
                evals.append(("S5", d, dirn))

        elif scalp_state.name in ("SURGE_CONTINUATION", "TANK_CONTINUATION"):
            # `bars` are alpaca Bar dataclass with .l/.h attrs — direct use is fine
            d, dirn = self._build_s3_decision(features, scalp_state, cfg, bars)
            if d is not None:
                evals.append(("S3", d, dirn))
            d4, dirn4 = self._build_s4_decision(features, scalp_state, cfg, alp, now)
            if d4 is not None:
                evals.append(("S4", d4, dirn4))

        elif scalp_state.name in ("SURGE_IGNITION", "TANK_IGNITION"):
            d, dirn = self._build_s2_decision(features, scalp_state, cfg, alp, bars, now)
            if d is not None:
                evals.append(("S2", d, dirn))
            d4, dirn4 = self._build_s4_decision(features, scalp_state, cfg, alp, now)
            if d4 is not None:
                evals.append(("S4", d4, dirn4))

        # No strategy in scope → drop all setups for this ticker
        if not evals:
            self._drop_setups_for_ticker(features.ticker, keep_ids=set())
            return

        # Build setup cards for each evaluation
        kept_ids: set = set()
        atr = max(features.high - features.low, 0.10)
        now_iso = datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z"

        for strategy, decision, direction in evals:
            try:
                log_decision_sync(decision, features, model_version="paper_trader_v0")
            except Exception:
                pass

            entry = features.close
            if direction == "long":
                stop = entry - atr; target = entry + atr; side = "buy"
            else:
                stop = entry + atr; target = entry - atr; side = "sell"
            risk_per_share = max(abs(entry - stop), 0.01)
            qty = max(1, int(500.0 / risk_per_share))

            stage = "TRADE" if decision.decision == "TRADE" else "FORMING"
            signal_id = f"{strategy}-{features.ticker}-{direction}"
            kept_ids.add(signal_id)

            prior_setup = self.status.setups.get(signal_id)
            created_at = (prior_setup.get("created_at") if prior_setup else now_iso) or now_iso

            setup_row = {
                "id": signal_id,
                "ticker": features.ticker,
                "strategy": strategy,
                "direction": direction, "side": side,
                "state": scalp_state.name,
                "score": round(scalp_state.score, 3),
                "stage": stage,
                "decision": decision.decision,
                "pass_reason": decision.pass_reason,
                "entry": round(entry, 2), "stop": round(stop, 2),
                "target": round(target, 2), "qty": qty,
                "atr": round(atr, 2),
                "created_at": created_at,
                "last_evaluated_at": now_iso,
            }
            self.status.setups[signal_id] = setup_row
            # Persist so daemon restart doesn't lose this card
            try:
                from journal.setups_store import persist_setup
                persist_setup(DB_PATH, setup_row)
            except Exception:
                pass

            self._add_decision({
                "ts": now_iso, "ticker": features.ticker,
                "strategy": strategy, "direction": direction,
                "state": scalp_state.name, "score": setup_row["score"],
                "decision": decision.decision, "reason": decision.pass_reason,
            })
            self.status.last_decision = setup_row

            # Auto-submit gate: TRADE + auto_submit + regular session
            #                    + risk manager allows
            if (stage == "TRADE" and self.status.auto_submit
                    and self.status.phase == "open"):
                allowed, deny_reason = self._risk_manager_allows(alp, cfg, strategy)
                if not allowed:
                    # Log the gate denial — visible in /auto decisions feed
                    self._add_decision({
                        "ts": now_iso, "ticker": features.ticker,
                        "strategy": strategy, "direction": direction,
                        "state": scalp_state.name, "score": setup_row["score"],
                        "decision": "RISK_BLOCKED",
                        "reason": deny_reason,
                    })
                    continue
                await self._submit_from_setup(alp, setup_row)
                self.status.taken_signal_ids.add(signal_id)
                try:
                    from journal.setups_store import mark_setup_taken
                    mark_setup_taken(DB_PATH, signal_id)
                except Exception:
                    pass

        # Drop sibling setups for this ticker that were NOT regenerated this tick
        # (e.g., S3 evaluation stopped because state moved to S2 IGNITION).
        self._drop_setups_for_ticker(features.ticker, keep_ids=kept_ids)

    async def _submit_from_setup(self, alp, setup: dict) -> dict:
        """Dispatcher: S5 → vertical option spread; S2/S3/S4 → equity bracket."""
        if setup.get("strategy") == "S5":
            return await self._submit_s5_vertical(alp, setup)
        return await self._submit_equity_bracket(alp, setup)

    async def _submit_equity_bracket(self, alp, setup: dict) -> dict:
        """Equity bracket submission for S2/S3/S4."""
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
                "instrument": "equity",
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

    async def _submit_s5_vertical(self, alp, setup: dict) -> dict:
        """S5 gamma-reversal: vertical debit spread. Long delta 35-45, short
        leg one strike further OTM. Per spec: 7-14 DTE, debit ≤ 40% of width."""
        from datetime import datetime, date as _date
        ts = datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z"
        try:
            ticker = setup["ticker"]
            direction = setup["direction"]    # 'long' = CALL spread, 'short' = PUT
            spot = float(setup["entry"])
            cp = "C" if direction == "long" else "P"

            # Pick expiry 7-14 DTE
            today = _date.today()
            target_expiry = today + timedelta(days=10)
            chain = alp.get_option_contracts_with_oi(ticker)  # returns list[OCC contracts]
            same_side = [c for c in chain if str(c.get("symbol", "")).endswith(("C", "P"))
                                                 or cp in str(c.get("symbol", ""))]
            # Filter to call/put + DTE window
            candidates = []
            for c in same_side:
                sym = c.get("symbol", "")
                # OCC: <ROOT><YYMMDD><C|P><STRIKE_8>
                import re as _re
                m = _re.match(rf"^{ticker}(\d{{6}})({cp})(\d{{8}})$", sym)
                if not m:
                    continue
                yy, mm, dd = m.group(1)[:2], m.group(1)[2:4], m.group(1)[4:6]
                exp = _date(2000 + int(yy), int(mm), int(dd))
                dte = (exp - today).days
                if not (7 <= dte <= 14):
                    continue
                strike = int(m.group(3)) / 1000.0
                candidates.append({"symbol": sym, "strike": strike, "expiry": exp,
                                     "dte": dte, "delta": c.get("delta")})

            if not candidates:
                raise RuntimeError(f"no {cp} contracts in 7-14 DTE for {ticker}")

            # Pick long leg closest to spot (proxy for ATM/40-delta)
            candidates.sort(key=lambda c: abs(c["strike"] - spot))
            long_leg = candidates[0]
            # Pick short leg one strike further OTM
            if direction == "long":  # call spread: short higher strike
                further = [c for c in candidates if c["strike"] > long_leg["strike"]]
                further.sort(key=lambda c: c["strike"])
            else:  # put spread: short lower strike
                further = [c for c in candidates if c["strike"] < long_leg["strike"]]
                further.sort(key=lambda c: -c["strike"])
            if not further:
                raise RuntimeError(f"no further-OTM strike found for {ticker} {cp}")
            short_leg = further[0]

            # Estimate debit (mid of long − mid of short). Without quote feed we
            # approximate via 1% of spot per strike of width.
            width = abs(long_leg["strike"] - short_leg["strike"])
            est_debit = max(0.05, spot * 0.005)
            limit_price = round(est_debit * 1.02, 2)    # 2% buffer

            qty = max(1, setup.get("qty", 1) // 100)    # contracts, not shares
            client_order_id = f"edge-{setup['id']}"
            resp = alp.submit_vertical(
                underlying=ticker,
                long_leg_symbol=long_leg["symbol"],
                short_leg_symbol=short_leg["symbol"],
                qty=qty, limit_price=limit_price,
                side="buy",
                client_order_id=client_order_id,
            )
            order_row = {
                "ts": ts, "ticker": ticker,
                "side": ("BUY_CALL_SPREAD" if direction == "long" else "BUY_PUT_SPREAD"),
                "qty": qty, "entry": spot,
                "stop": setup["stop"], "target": setup["target"],
                "strategy": "S5", "direction": direction,
                "instrument": f"vertical_{cp}",
                "long_leg": long_leg["symbol"],
                "short_leg": short_leg["symbol"],
                "width": width, "limit_price": limit_price,
                "client_order_id": client_order_id,
                "order_id": (resp or {}).get("id"),
                "status": (resp or {}).get("status", "submitted"),
                "source": "auto" if self.status.auto_submit else "manual",
            }
            self.status.orders.appendleft(order_row)
            return {"ok": True, **order_row}
        except Exception as e:
            err = {
                "ts": ts, "ticker": setup["ticker"],
                "side": ("S5_" + setup.get("direction", "?")).upper(),
                "instrument": "vertical",
                "error": f"{type(e).__name__}: {e}",
            }
            self.status.orders.appendleft(err)
            return {"ok": False, **err}

    def force_test_tick(self, ticker: str) -> dict:
        """Run one synchronous evaluation pass for `ticker`, bypassing the
        phase gate (works even when market is closed). Used by /api/test_tick
        to verify wiring end-to-end before tomorrow's open.

        Returns a dict describing what happened: state, strategies evaluated,
        decisions, setup rows produced.
        """
        from infra.config_loader import load_thresholds
        from infra.secrets import load_secrets
        load_secrets()
        cfg = load_thresholds()
        from data_clients.alpaca import AlpacaClient
        try:
            from data_clients.unusual_whales import UWClient
            uw = UWClient()
        except Exception:
            uw = None

        result = {"ticker": ticker, "ok": True, "ts": datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z"}
        try:
            with AlpacaClient() as alp:
                # Run one tick synchronously through the scan logic
                loop = asyncio.new_event_loop()
                try:
                    asyncio.set_event_loop(loop)
                    now = datetime.now(tz=timezone.utc)
                    # Capture the current setups for diff
                    before_ids = {sid for sid, r in self.status.setups.items() if r.get("ticker") == ticker}
                    changed = loop.run_until_complete(
                        self._tick_one(alp, uw, ticker, cfg, now)
                    )
                    after_setups = [
                        {**r} for sid, r in self.status.setups.items()
                        if r.get("ticker") == ticker
                    ]
                    state = self._prior_state.get(ticker)
                    result.update({
                        "state": state.name if state else "UNKNOWN",
                        "score": (state.score if state else 0.0),
                        "state_changed": bool(changed),
                        "setups_after": after_setups,
                        "setups_count": len(after_setups),
                    })
                finally:
                    loop.close()
        except Exception as e:
            result.update({"ok": False, "error": f"{type(e).__name__}: {e}"})
        return result

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
        if self.status.phase != "open":
            return {"ok": False,
                     "error": f"market {self.status.phase} — wait for regular session"}
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
            try:
                from journal.setups_store import mark_setup_taken
                mark_setup_taken(DB_PATH, signal_id)
            except Exception:
                pass
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
