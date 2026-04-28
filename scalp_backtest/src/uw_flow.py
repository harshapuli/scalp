"""
uw_flow.py — fetch UW options flow alerts and aggregate per ticker.

Endpoint: GET https://api.unusualwhales.com/api/option-trades/flow-alerts
Auth:     Authorization: Bearer <UW_API_KEY>

Reads keys from swing_engine_v2/.env (same as alpaca_bars.py).
Caches the global feed to data/uw_cache/flow_alerts_global.json with a 30-min TTL.

Public API:
    fetch_global_flow(lookback_hours=24) -> list[dict]
    aggregate_by_ticker(alerts) -> dict[ticker, FlowAgg]
"""
from __future__ import annotations

import json
import os
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional


PROJECT_ROOT = Path(__file__).resolve().parent.parent
UW_CACHE = PROJECT_ROOT / "data" / "uw_cache"
ENV_FILE = Path("/Users/harshapuli/Downloads/spy QQQ/claude/gemini /swing_engine_v2/.env")


def _load_env() -> dict:
    out: dict[str, str] = {}
    if not ENV_FILE.exists():
        return out
    for line in ENV_FILE.read_text().splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, _, v = s.partition("=")
        v = v.strip().strip('"').strip("'")
        out[k.strip()] = v
    return out


_ENV = _load_env()
UW_KEY = _ENV.get("UW_API_KEY") or os.environ.get("UW_API_KEY")
UW_BASE = (_ENV.get("UW_BASE_URL") or "https://api.unusualwhales.com/api").rstrip("/")
URL = f"{UW_BASE}/option-trades/flow-alerts"


# ──────────────────────────────────────────────────────────────────────────────
# Aggregates
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class FlowAgg:
    ticker: str
    n_alerts: int = 0
    sweep_n: int = 0
    call_prem_m: float = 0.0
    put_prem_m: float = 0.0
    net_call_skew_m: float = 0.0
    call_share: float = 0.0
    put_share: float = 0.0
    top3: list[dict] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Cache
# ──────────────────────────────────────────────────────────────────────────────


def _cache_path() -> Path:
    return UW_CACHE / "flow_alerts_global.json"


def _is_fresh(p: Path, max_age_seconds: int = 30 * 60) -> bool:
    if not p.exists():
        return False
    return (time.time() - p.stat().st_mtime) < max_age_seconds


def _load_cache() -> Optional[list[dict]]:
    p = _cache_path()
    if not _is_fresh(p):
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_cache(alerts: list[dict]) -> None:
    UW_CACHE.mkdir(parents=True, exist_ok=True)
    _cache_path().write_text(json.dumps(alerts))


# ──────────────────────────────────────────────────────────────────────────────
# Fetch
# ──────────────────────────────────────────────────────────────────────────────


def _http_get(url: str, headers: dict, timeout: int = 15) -> Optional[dict]:
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except Exception as e:
        print(f"[uw] GET {url[:80]}... failed: {e!r}")
        return None


def fetch_global_flow(lookback_hours: int = 24, max_pages: int = 10,
                      use_cache: bool = True) -> list[dict]:
    """Fetch the global flow-alerts feed via pagination, dedup by id."""
    if use_cache:
        cached = _load_cache()
        if cached is not None:
            return cached

    if not UW_KEY:
        raise RuntimeError("UW_API_KEY missing in env")

    headers = {"Authorization": f"Bearer {UW_KEY}", "Accept": "application/json"}
    cutoff_ms = int((datetime.now(timezone.utc) - timedelta(hours=lookback_hours)).timestamp() * 1000)

    all_items: list[dict] = []
    seen_ids: set = set()
    older_than: Optional[str] = None

    for page in range(max_pages):
        params: dict = {}
        if older_than:
            params["older_than"] = older_than
        url = URL + (f"?{urllib.parse.urlencode(params)}" if params else "")
        data = _http_get(url, headers)
        if not data:
            break
        items = data.get("data") or []
        if not items:
            break

        new_count = 0
        for it in items:
            iid = it.get("id")
            if iid and iid not in seen_ids:
                seen_ids.add(iid)
                all_items.append(it)
                new_count += 1

        if new_count == 0:
            break

        older_than = data.get("older_than")

        try:
            oldest_ts = min(int(it.get("start_time") or 0) for it in items)
            if oldest_ts and oldest_ts < cutoff_ms:
                break
        except (TypeError, ValueError):
            pass

        time.sleep(0.3)

    _save_cache(all_items)
    return all_items


# ──────────────────────────────────────────────────────────────────────────────
# Aggregate
# ──────────────────────────────────────────────────────────────────────────────


def _premium_m(it: dict) -> float:
    """Estimate premium in $M from total_premium / total_size × 100."""
    try:
        tp = it.get("total_premium")
        if tp is not None:
            return float(tp) / 1_000_000.0
        size = float(it.get("total_size") or 0)
        price = float(it.get("price") or 0)
        return (size * price * 100) / 1_000_000.0
    except (TypeError, ValueError):
        return 0.0


def aggregate_by_ticker(alerts: list[dict]) -> dict[str, FlowAgg]:
    """Group alerts by ticker, compute call/put premium splits, sweep count, top3."""
    by_ticker: dict[str, FlowAgg] = {}
    bucket: dict[str, list[dict]] = {}

    for it in alerts:
        tk = (it.get("ticker") or it.get("underlying_symbol") or "").upper()
        if not tk:
            continue
        bucket.setdefault(tk, []).append(it)

    for tk, items in bucket.items():
        agg = FlowAgg(ticker=tk, n_alerts=len(items))
        call_prem = 0.0
        put_prem = 0.0
        sweeps = 0

        scored: list[tuple[float, dict]] = []
        for it in items:
            prem_m = _premium_m(it)
            otype = (it.get("type") or "").lower()
            if otype == "call":
                call_prem += prem_m
            elif otype == "put":
                put_prem += prem_m
            if it.get("has_sweep") or it.get("is_sweep") or "sweep" in (it.get("rule_name") or "").lower():
                sweeps += 1
            scored.append((prem_m, it))

        scored.sort(key=lambda x: -x[0])
        top3 = []
        for prem_m, it in scored[:3]:
            top3.append({
                "type": (it.get("type") or "").lower(),
                "strike": it.get("strike"),
                "expiry": it.get("expiry"),
                "premium_m": round(prem_m, 3),
                "rule": it.get("rule_name") or it.get("rule"),
                "is_sweep": bool(it.get("has_sweep") or it.get("is_sweep")),
                "created_at": it.get("created_at") or it.get("start_time"),
            })

        total = call_prem + put_prem
        agg.call_prem_m = round(call_prem, 3)
        agg.put_prem_m = round(put_prem, 3)
        agg.net_call_skew_m = round(call_prem - put_prem, 3)
        agg.call_share = round(call_prem / total, 3) if total > 0 else 0.0
        agg.put_share = round(put_prem / total, 3) if total > 0 else 0.0
        agg.sweep_n = sweeps
        agg.top3 = top3
        by_ticker[tk] = agg

    return by_ticker


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print(f"UW_KEY: {'set' if UW_KEY else 'MISSING'}")
    print(f"UW_BASE: {UW_BASE}")
    if not UW_KEY:
        raise SystemExit(1)

    t0 = time.time()
    alerts = fetch_global_flow(lookback_hours=24)
    dt = time.time() - t0
    print(f"fetched {len(alerts)} alerts in {dt:.2f}s")
    if not alerts:
        raise SystemExit(0)

    aggs = aggregate_by_ticker(alerts)
    print(f"\nTop 10 tickers by net call skew (most bullish flow):")
    print(f"{'TICKER':6s} {'alerts':>6s} {'sweeps':>6s} {'call$M':>8s} {'put$M':>8s} {'net$M':>8s} {'call%':>6s}")
    print("─" * 60)
    for tk, a in sorted(aggs.items(), key=lambda kv: -kv[1].net_call_skew_m)[:10]:
        print(f"{tk:6s} {a.n_alerts:>6d} {a.sweep_n:>6d} {a.call_prem_m:>8.2f} {a.put_prem_m:>8.2f} "
              f"{a.net_call_skew_m:>+8.2f} {a.call_share*100:>5.1f}%")

    print(f"\nTop 10 tickers by net put skew (most bearish flow):")
    for tk, a in sorted(aggs.items(), key=lambda kv: kv[1].net_call_skew_m)[:10]:
        print(f"{tk:6s} {a.n_alerts:>6d} {a.sweep_n:>6d} {a.call_prem_m:>8.2f} {a.put_prem_m:>8.2f} "
              f"{a.net_call_skew_m:>+8.2f} {a.call_share*100:>5.1f}%")
