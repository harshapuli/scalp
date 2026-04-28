"""
alpaca_bars.py — fetch daily OHLCV bars from Alpaca for swing outcome computation.

Uses the .env file from swing_engine_v2 (same credentials as the upstream engines).
READ-ONLY on the .env (just imports the keys).

Public API:
    fetch_daily_bars_for_swing_tickers(tickers, lookback_days=60) -> dict[ticker, list[DailyBar]]

Caches to data/bars_cache/<ticker>_daily.json so we only hit the API once per ticker per day.
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from outcome_swing import DailyBar


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BARS_CACHE = PROJECT_ROOT / "data" / "bars_cache"
ENV_FILE = Path("/Users/harshapuli/Downloads/spy QQQ/claude/gemini /swing_engine_v2/.env")


# ──────────────────────────────────────────────────────────────────────────────
# Env loading (no python-dotenv dep)
# ──────────────────────────────────────────────────────────────────────────────


def _load_env() -> dict:
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        if "=" not in s:
            continue
        k, _, v = s.partition("=")
        v = v.strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


_ENV = _load_env()
ALPACA_KEY = _ENV.get("ALPACA_API_KEY") or os.environ.get("ALPACA_API_KEY")
ALPACA_SECRET = _ENV.get("ALPACA_API_SECRET") or os.environ.get("ALPACA_API_SECRET")
ALPACA_DATA_URL = _ENV.get("ALPACA_DATA_URL", "https://data.alpaca.markets").rstrip("/")


# ──────────────────────────────────────────────────────────────────────────────
# Cache
# ──────────────────────────────────────────────────────────────────────────────


def _cache_path(ticker: str) -> Path:
    return BARS_CACHE / f"{ticker.upper()}_daily.json"


def _is_fresh(path: Path, max_age_seconds: int = 3600 * 12) -> bool:
    """Cache is fresh if file mtime is within last 12 hours."""
    if not path.exists():
        return False
    return (time.time() - path.stat().st_mtime) < max_age_seconds


def _load_cache(ticker: str) -> Optional[list[DailyBar]]:
    p = _cache_path(ticker)
    if not _is_fresh(p):
        return None
    try:
        rows = json.loads(p.read_text())
        return [DailyBar(**r) for r in rows]
    except (json.JSONDecodeError, OSError, TypeError):
        return None


def _save_cache(ticker: str, bars: list[DailyBar]) -> None:
    BARS_CACHE.mkdir(parents=True, exist_ok=True)
    rows = [{"date": b.date, "epoch": b.epoch, "o": b.o, "h": b.h, "l": b.l,
             "c": b.c, "v": b.v} for b in bars]
    _cache_path(ticker).write_text(json.dumps(rows))


# ──────────────────────────────────────────────────────────────────────────────
# Alpaca call
# ──────────────────────────────────────────────────────────────────────────────


def _fetch_one(ticker: str, lookback_days: int) -> list[DailyBar]:
    """Fetch via Alpaca v2 bars endpoint. Returns [] on error (caller handles)."""
    if not ALPACA_KEY or not ALPACA_SECRET:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_API_SECRET missing in env")

    end = datetime.now(timezone.utc).date()
    start = end - timedelta(days=lookback_days + 5)
    params = {
        "timeframe": "1Day",
        "start": start.isoformat() + "T00:00:00Z",
        "end":   end.isoformat() + "T23:59:59Z",
        "limit": 1000,
        "adjustment": "raw",
        "feed": "iex",   # iex feed available on free + paid plans
    }
    qs = urllib.parse.urlencode(params)
    url = f"{ALPACA_DATA_URL}/v2/stocks/{urllib.parse.quote(ticker)}/bars?{qs}"
    req = urllib.request.Request(url, headers={
        "APCA-API-KEY-ID": ALPACA_KEY,
        "APCA-API-SECRET-KEY": ALPACA_SECRET,
    })
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode())
    except Exception as e:
        # try sip feed as fallback (works on paid)
        try:
            params["feed"] = "sip"
            qs = urllib.parse.urlencode(params)
            url = f"{ALPACA_DATA_URL}/v2/stocks/{urllib.parse.quote(ticker)}/bars?{qs}"
            req = urllib.request.Request(url, headers={
                "APCA-API-KEY-ID": ALPACA_KEY,
                "APCA-API-SECRET-KEY": ALPACA_SECRET,
            })
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read().decode())
        except Exception as e2:
            raise RuntimeError(f"alpaca fetch {ticker}: {e2!r}")

    bars_raw = data.get("bars") or []
    out: list[DailyBar] = []
    for b in bars_raw:
        t = b.get("t")
        if not t:
            continue
        try:
            ep = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
            d = t[:10]
        except Exception:
            continue
        out.append(DailyBar(
            date=d,
            epoch=ep,
            o=float(b.get("o") or 0),
            h=float(b.get("h") or 0),
            l=float(b.get("l") or 0),
            c=float(b.get("c") or 0),
            v=int(b.get("v") or 0),
        ))
    out.sort(key=lambda x: x.epoch)
    return out


# ──────────────────────────────────────────────────────────────────────────────
# Public
# ──────────────────────────────────────────────────────────────────────────────


def fetch_daily_bars_for_swing_tickers(tickers: list[str],
                                       lookback_days: int = 60
                                       ) -> dict[str, list[DailyBar]]:
    """
    Fetch daily bars for a list of tickers, with a 12-hour disk cache.
    Returns {ticker: [DailyBar...]} — empty list for tickers that fail.
    """
    BARS_CACHE.mkdir(parents=True, exist_ok=True)
    out: dict[str, list[DailyBar]] = {}
    for t in tickers:
        cached = _load_cache(t)
        if cached is not None:
            out[t] = cached
            continue
        try:
            bars = _fetch_one(t, lookback_days)
            out[t] = bars
            _save_cache(t, bars)
            time.sleep(0.1)   # gentle pacing
        except Exception as e:
            print(f"[alpaca] {t}: {e}")
            out[t] = []
    return out


# ──────────────────────────────────────────────────────────────────────────────
# CLI sanity
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print(f"ALPACA_KEY: {'set' if ALPACA_KEY else 'MISSING'}")
    print(f"ALPACA_DATA_URL: {ALPACA_DATA_URL}")
    if not ALPACA_KEY:
        print("Cannot fetch — set keys in env first")
        raise SystemExit(1)

    test = ["NVDA", "SPY", "PLTR"]
    bars = fetch_daily_bars_for_swing_tickers(test, lookback_days=30)
    for t, blist in bars.items():
        if blist:
            last = blist[-1]
            print(f"  {t}: {len(blist)} bars · last={last.date} c={last.c:.2f} v={last.v:,}")
        else:
            print(f"  {t}: NO DATA")
