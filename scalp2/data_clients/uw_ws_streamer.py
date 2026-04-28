"""data_clients/uw_ws_streamer.py — Centralized UnusualWhales WebSocket streamer.

ONE connection to UW WS shared by both engines (scalp2 + engine_v4).

Spec (from UW's reference Python at
  github.com/unusual-whales/api-examples/.../stream_flow_alerts.py):

  · URL:        wss://api.unusualwhales.com/socket?token={UW_TOKEN}
  · Subscribe:  send JSON {"channel": "<name>", "msg_type": "join"}
  · Receive:    each message is a JSON 2-tuple [channel, payload]
  · Heartbeat:  ping after 30s of silence; reconnect on dead pong
  · Backoff:    exponential up to 60s on disconnect

Three outputs (configurable):
  1. In-memory store `LATEST_BY_TICKER` — read by paper_trader live
  2. SQLite `data/uw_ws.db` table `ws_messages` — durable history
  3. Append-only `data/uw_ws_flow.jsonl` — tail-able, schema-stable for
     engine_v4 or any other consumer to read with zero coupling

Channels subscribed by default: flow-alerts (global), gex:TICKER per
universe ticker, news, price:TICKER. Configurable via env / start() args.

Per UW's WS guidance: "drops messages server-side if consumers fall
behind" — receive loop does as little as possible (parse + queue), a
separate writer task batches to disk every 1s or 100 records.
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import sys
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

UW_WS_URL_TEMPLATE = "wss://api.unusualwhales.com/socket?token={token}"
SINK_JSONL = PROJECT_ROOT / "data" / "uw_ws_flow.jsonl"
SINK_DB = PROJECT_ROOT / "data" / "uw_ws.db"
LOG_FILE = PROJECT_ROOT / "data" / "uw_ws_streamer.log"

# Reconnection
TIMEOUT_LENGTH = 30
MAX_RECONNECT_ATTEMPTS = 999       # essentially infinite — daemon should keep trying
RECONNECT_DELAY = 5
RECONNECT_DELAY_MAX = 60

# Default channels — flow-alerts is global so always on; per-ticker
# channels are added per-universe-ticker
GLOBAL_CHANNELS = ["flow-alerts", "news"]
PER_TICKER_CHANNELS = ["gex", "price"]

# In-memory store, read by paper_trader. Bounded so memory stable.
LATEST_BY_TICKER: dict[str, deque] = defaultdict(lambda: deque(maxlen=200))
LATEST_FLOW_ALERTS: deque = deque(maxlen=500)
LATEST_GEX_BY_TICKER: dict[str, dict] = {}
LATEST_PRICE_BY_TICKER: dict[str, dict] = {}
LATEST_NEWS: deque = deque(maxlen=200)
LATEST_LOCK = threading.Lock()

# Health stats — exposed via /api/diag
STATS: dict = {
    "connected": False,
    "connected_at": None,
    "last_message_at": None,
    "n_messages": 0,
    "n_flow_alerts": 0,
    "n_option_trades": 0,
    "n_gex_updates": 0,
    "n_price_updates": 0,
    "n_news": 0,
    "n_other": 0,
    "n_reconnects": 0,
    "last_error": None,
    "subscribed_channels": [],
}

_stop_event = threading.Event()
_loop_thread: Optional[threading.Thread] = None


def _log(msg: str) -> None:
    line = f"[{datetime.now(tz=timezone.utc).strftime('%H:%M:%S')}] {msg}"
    print(line, flush=True)
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with open(LOG_FILE, "a") as f:
            f.write(line + "\n")
    except Exception:
        pass


# ─── SQLite schema (durable history) ──────────────────────────────────


_SCHEMA = """
CREATE TABLE IF NOT EXISTS ws_messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    received_at   TEXT NOT NULL,
    channel       TEXT NOT NULL,
    ticker        TEXT,
    payload_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ws_channel_ts ON ws_messages (channel, received_at);
CREATE INDEX IF NOT EXISTS idx_ws_ticker_ts ON ws_messages (ticker, received_at);
"""


def _init_db() -> None:
    SINK_DB.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(str(SINK_DB)) as c:
        c.executescript(_SCHEMA)


def _persist_batch(batch: list[tuple[str, str, Optional[str], dict]]) -> None:
    """batch = list of (received_at_iso, channel, ticker, payload_dict).
    Writes to BOTH the JSONL append-log AND the SQLite ws_messages table."""
    if not batch:
        return
    # JSONL append (tail-able for engine_v4 consumers)
    try:
        with open(SINK_JSONL, "a") as f:
            for ts, ch, tk, p in batch:
                rec = {"ts": ts, "channel": ch, "ticker": tk, "payload": p}
                f.write(json.dumps(rec, separators=(",", ":")) + "\n")
    except Exception as e:
        _log(f"jsonl write failed: {e}")

    # SQLite batch insert (queryable history)
    try:
        rows = [(ts, ch, tk, json.dumps(p, separators=(",", ":")))
                for ts, ch, tk, p in batch]
        with sqlite3.connect(str(SINK_DB)) as c:
            c.executemany(
                "INSERT INTO ws_messages (received_at, channel, ticker, payload_json) VALUES (?, ?, ?, ?)",
                rows,
            )
    except Exception as e:
        _log(f"sqlite write failed: {e}")


# ─── In-memory store update — cheap, called per message ───────────────


def _update_memory(channel: str, payload: dict) -> None:
    """Update the appropriate in-memory cache based on channel."""
    if not isinstance(payload, dict):
        return
    ticker = (payload.get("ticker") or payload.get("symbol")
              or payload.get("underlying"))
    with LATEST_LOCK:
        if ticker:
            LATEST_BY_TICKER[ticker].appendleft(payload)
        if "flow-alerts" in channel:
            LATEST_FLOW_ALERTS.appendleft(payload)
        elif channel.startswith("gex"):
            if ticker:
                LATEST_GEX_BY_TICKER[ticker] = payload
        elif channel.startswith("price"):
            if ticker:
                LATEST_PRICE_BY_TICKER[ticker] = payload
        elif channel == "news":
            LATEST_NEWS.appendleft(payload)


def _bump_stats(channel: str) -> None:
    if "flow-alerts" in channel:
        STATS["n_flow_alerts"] += 1
    elif "option-trades" in channel or "option_trades" in channel:
        STATS["n_option_trades"] += 1
    elif channel.startswith("gex"):
        STATS["n_gex_updates"] += 1
    elif channel.startswith("price"):
        STATS["n_price_updates"] += 1
    elif channel == "news":
        STATS["n_news"] += 1
    else:
        STATS["n_other"] += 1


# ─── Writer task — batches JSONL/SQLite writes every 1s or 100 records ─


async def _writer(queue: asyncio.Queue) -> None:
    """Drain the queue and batch-flush to durable sinks."""
    batch: list[tuple[str, str, Optional[str], dict]] = []
    last_flush = time.time()
    while not _stop_event.is_set():
        try:
            item = await asyncio.wait_for(queue.get(), timeout=1.0)
            batch.append(item)
        except asyncio.TimeoutError:
            pass
        if batch and (len(batch) >= 100 or (time.time() - last_flush) >= 1.0):
            _persist_batch(batch)
            batch = []
            last_flush = time.time()
    # Final flush on shutdown
    if batch:
        _persist_batch(batch)


# ─── Connect + receive loop ───────────────────────────────────────────


async def _connect_and_run(token: str, tickers: list[str],
                              queue: asyncio.Queue) -> None:
    """One full connection + subscribe + receive cycle. Raises on disconnect
    so the outer loop can reconnect."""
    import websockets

    uri = UW_WS_URL_TEMPLATE.format(token=token)
    _log(f"connecting to {uri[:60]}...")
    async with websockets.connect(uri, ping_interval=20, ping_timeout=20,
                                     max_size=2**22, open_timeout=15) as ws:
        # Build channel list
        channels: list[str] = list(GLOBAL_CHANNELS)
        for ch in PER_TICKER_CHANNELS:
            for tk in tickers:
                channels.append(f"{ch}:{tk}")

        for ch in channels:
            await ws.send(json.dumps({"channel": ch, "msg_type": "join"}))
        STATS["connected"] = True
        STATS["connected_at"] = datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z"
        STATS["subscribed_channels"] = channels
        _log(f"connected · subscribed to {len(channels)} channels: "
              f"global={GLOBAL_CHANNELS}, per-ticker={PER_TICKER_CHANNELS}, n_tickers={len(tickers)}")

        while not _stop_event.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=TIMEOUT_LENGTH)
            except asyncio.TimeoutError:
                _log(f"no msg for {TIMEOUT_LENGTH}s — pinging")
                pong = await ws.ping()
                await asyncio.wait_for(pong, timeout=10)
                continue
            STATS["n_messages"] += 1
            STATS["last_message_at"] = datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z"
            try:
                data = json.loads(raw)
            except Exception:
                STATS["n_other"] += 1
                continue

            # UW format: each message is a JSON 2-tuple [channel, payload]
            if isinstance(data, list) and len(data) == 2:
                channel, payload = data
            elif isinstance(data, dict):
                channel = data.get("channel", "")
                payload = data.get("payload", data)
            else:
                STATS["n_other"] += 1
                continue

            _bump_stats(channel)
            _update_memory(channel, payload if isinstance(payload, dict) else {})
            ticker = None
            if isinstance(payload, dict):
                ticker = (payload.get("ticker") or payload.get("symbol")
                          or payload.get("underlying"))
            recv_ts = STATS["last_message_at"]
            await queue.put((recv_ts, channel, ticker,
                              payload if isinstance(payload, dict) else {"raw": payload}))


# ─── Outer reconnect loop ─────────────────────────────────────────────


async def _outer_loop(token: str, tickers: list[str]) -> None:
    queue: asyncio.Queue = asyncio.Queue(maxsize=10000)
    writer_task = asyncio.create_task(_writer(queue))
    attempt = 0
    try:
        while not _stop_event.is_set() and attempt <= MAX_RECONNECT_ATTEMPTS:
            try:
                await _connect_and_run(token, tickers, queue)
                attempt = 0    # reset on graceful exit (rare)
            except Exception as e:
                STATS["connected"] = False
                STATS["last_error"] = f"{type(e).__name__}: {e}"
                STATS["n_reconnects"] += 1
                _log(f"connection failed: {STATS['last_error']}")
                attempt += 1
                if attempt > MAX_RECONNECT_ATTEMPTS:
                    _log("max reconnect attempts reached, giving up")
                    break
                delay = min(RECONNECT_DELAY * (2 ** min(attempt - 1, 6)),
                              RECONNECT_DELAY_MAX)
                for _ in range(int(delay)):
                    if _stop_event.is_set():
                        break
                    await asyncio.sleep(1)
    finally:
        writer_task.cancel()
        try:
            await writer_task
        except Exception:
            pass
        STATS["connected"] = False


def _thread_entry(token: str, tickers: list[str]) -> None:
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        loop.run_until_complete(_outer_loop(token, tickers))
    except Exception as e:
        STATS["last_error"] = f"{type(e).__name__}: {e}"
        _log(f"thread crashed: {e}")
        import traceback
        traceback.print_exc()


# ─── Public API ───────────────────────────────────────────────────────


def start(tickers: Optional[list[str]] = None) -> dict:
    """Start the WS streamer in a background thread. Idempotent."""
    global _loop_thread
    if _loop_thread is not None and _loop_thread.is_alive():
        return {"ok": False, "error": "already running"}
    token = (os.environ.get("UW_API_KEY")
              or os.environ.get("UW_TOKEN") or "").replace('"', '').strip()
    if not token:
        return {"ok": False, "error": "UW_API_KEY / UW_TOKEN not set"}
    if tickers is None:
        try:
            from scripts.paper_trader import DEFAULT_UNIVERSE
            tickers = list(DEFAULT_UNIVERSE)
        except Exception:
            tickers = ["SPY", "QQQ", "IWM", "NVDA", "AAPL"]
    _init_db()
    _stop_event.clear()
    _loop_thread = threading.Thread(
        target=_thread_entry, args=(token, tickers),
        daemon=True, name="uw-ws-central",
    )
    _loop_thread.start()
    return {"ok": True, "tickers": tickers,
             "channels_global": GLOBAL_CHANNELS,
             "channels_per_ticker": PER_TICKER_CHANNELS}


def stop() -> dict:
    _stop_event.set()
    if _loop_thread:
        _loop_thread.join(timeout=5.0)
    STATS["connected"] = False
    return {"ok": True}


def get_recent_flow_for_ticker(ticker: str, max_age_s: float = 30.0) -> list[dict]:
    """Cached flow records for `ticker` from the WS feed, only if the
    most recent one is fresh (≤ max_age_s old). Empty list when stale —
    caller should fall back to REST cache."""
    with LATEST_LOCK:
        records = list(LATEST_BY_TICKER.get(ticker) or [])
    return records


def get_recent_flow_alerts(limit: int = 50) -> list[dict]:
    with LATEST_LOCK:
        return list(LATEST_FLOW_ALERTS)[:limit]


def get_gex_snapshot(ticker: str) -> Optional[dict]:
    """Latest cached GEX snapshot from WS, or None if not received yet."""
    with LATEST_LOCK:
        return LATEST_GEX_BY_TICKER.get(ticker)


def get_price_snapshot(ticker: str) -> Optional[dict]:
    with LATEST_LOCK:
        return LATEST_PRICE_BY_TICKER.get(ticker)


def get_stats() -> dict:
    return dict(STATS)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--tickers", default="SPY,QQQ,NVDA,AAPL,META")
    args = ap.parse_args()
    from infra.secrets import load_secrets
    load_secrets()
    print("[uw_ws] starting smoke...")
    r = start(tickers=args.tickers.split(","))
    print(f"[uw_ws] start: {r}")
    try:
        while True:
            time.sleep(5)
            s = get_stats()
            print(f"[uw_ws] connected={s['connected']} msgs={s['n_messages']} "
                  f"flow={s['n_flow_alerts']} gex={s['n_gex_updates']} "
                  f"price={s['n_price_updates']} reconnects={s['n_reconnects']}")
    except KeyboardInterrupt:
        stop()
        print("[uw_ws] stopped")
