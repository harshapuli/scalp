"""prebreakout/scanner.py — Pre-breakout setup scanner for the 37-ticker universe.

Ports engine_v4's v4_prebreak_scanner gate logic into scalp2 (separate file —
does NOT modify the parallel project). For each ticker in the backtest universe,
checks whether price is coiling beneath a resistance (SURGE candidate) or above
support (TANK candidate) with all 6 structural gates plus UW flow alignment.

Gates (all must pass for a fire):
  G1. Compressed       — last 30 bars' (high-low) ≤ 1.5% of avg close
  G2. Level defined    — resistance/support tagged ≥ 2 times in window
  G3. Structure        — higher-lows (SURGE) or lower-highs (TANK)
  G4. Proximity        — current close within 0.25% of level
  G5. No break yet     — last bar hasn't pierced level by more than tolerance
  G6. Volume drying    — last 5-bar avg vol < 85% of trailing 20-bar avg
  G7. Flow aligned     — qualifying UW flow alert in last 10 min, same direction

Inputs: AlpacaClient (1m bars), UWClient (flow_alerts_historical / flow_recent).
Outputs: list[dict] sorted by composite confidence, separated into SURGE / TANK.

This is the v1 ship — gates and thresholds match engine_v4's tuned values. v2
will layer the design-doc 8-factor stack ON TOP for additional probability
shifting (factors are independent of gates).
"""
from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ─── Tuning constants (mirror engine_v4 v4_prebreak_scanner.py) ────────────

COMPRESSION_BARS     = 30
COMPRESSION_MAX_PCT  = 0.015
LEVEL_TOLERANCE_PCT  = 0.0015
MIN_LEVEL_TESTS      = 2
PROXIMITY_PCT        = 0.0025
VOL_DRY_RATIO        = 0.85
FLOW_RECENCY_MIN     = 10
BARS_LOOKBACK        = 40

# Per scalp2 spec — pre-break should also avoid earnings binary risk
EARNINGS_BLOCK_DAYS  = 5

# Engine_v4 universe is restricted to MIN_MARKETCAP $5B. Our 37-ticker
# backtest universe is already filtered to liquid names — no extra check needed.

# Default universe — load from latest backtest run
def _load_universe() -> list[str]:
    import json as _json
    bt_dir = PROJECT_ROOT / "data" / "backtest"
    runs = sorted(bt_dir.glob("run_37tickers_v*_*.json")) or sorted(bt_dir.glob("run_*.json"))
    if not runs:
        return list(("SPY", "QQQ", "IWM", "NVDA", "AAPL", "TSLA", "META",
                      "MSFT", "AMD", "AMZN"))
    try:
        d = _json.loads(runs[-1].read_text())
        return list(d.get("meta", {}).get("tickers") or [])
    except Exception:
        return list(("SPY", "QQQ", "IWM", "NVDA", "AAPL", "TSLA", "META",
                      "MSFT", "AMD", "AMZN"))


DEFAULT_UNIVERSE = tuple(_load_universe())


# ─── Sector mapping (per design doc §6 F2) ────────────────────────────────

TECH_TICKERS = {"AAPL", "AMZN", "GOOGL", "META", "MSFT", "NVDA", "AMD",
                  "AVGO", "CRWD", "ARM", "MU", "MSTR", "PLTR", "SMCI",
                  "ROKU", "SHOP", "ABNB", "CRWV", "RDDT", "SNDK", "HOOD",
                  "COIN", "CVNA", "TSLA", "NFLX"}

SECTOR_ETF_MAP = {
    # Tech
    "AAPL": "XLK", "AMZN": "XLY", "GOOGL": "XLC", "META": "XLC",
    "MSFT": "XLK", "NVDA": "XLK", "AMD": "XLK", "AVGO": "XLK",
    "CRWD": "XLK", "ARM": "XLK", "MU": "XLK", "MSTR": "XLK",
    "PLTR": "XLK", "SMCI": "XLK", "TSLA": "XLY", "NFLX": "XLC",
    "ROKU": "XLY", "SHOP": "XLY", "ABNB": "XLY", "CRWV": "XLK",
    "RDDT": "XLC", "SNDK": "XLK", "HOOD": "XLF", "COIN": "XLF",
    "CVNA": "XLY",
    # Indexes / ETFs (no sector mapping — F2 inactive)
}


# ─── F1-F6 factors (per PREBREAKOUT_DESIGN.md §6) ─────────────────────────


def f1_index_align(side: str, ticker: str, alpaca, now: datetime) -> float:
    """F1. SPY/QQQ direction in prior 5 min vs signal side. Score [-1, +1].
    Tech names use QQQ; everything else uses SPY."""
    idx = "QQQ" if ticker in TECH_TICKERS else "SPY"
    end = now.isoformat(timespec="seconds")
    start = (now - timedelta(minutes=12)).isoformat(timespec="seconds")
    try:
        bars = alpaca.get_bars(idx, start=start, end=end,
                                 timeframe="1Min", limit=12)
    except Exception:
        return 0.0
    if len(bars) < 6:
        return 0.0
    ret_5m = (bars[-1].c - bars[-6].c) / bars[-6].c
    direction = 1 if side == "long" else -1
    NORM = 0.003   # 0.3% in 5min in your direction → +1.0
    return max(-1.0, min(1.0, (ret_5m * direction) / NORM))


def f2_sector_align(side: str, ticker: str, alpaca, now: datetime) -> float:
    """F2. Sector ETF direction in prior 5 min. Score [-1, +1]. Inactive
    (returns 0) for tickers without a sector mapping."""
    etf = SECTOR_ETF_MAP.get(ticker)
    if not etf:
        return 0.0
    end = now.isoformat(timespec="seconds")
    start = (now - timedelta(minutes=12)).isoformat(timespec="seconds")
    try:
        bars = alpaca.get_bars(etf, start=start, end=end,
                                 timeframe="1Min", limit=12)
    except Exception:
        return 0.0
    if len(bars) < 6:
        return 0.0
    ret_5m = (bars[-1].c - bars[-6].c) / bars[-6].c
    direction = 1 if side == "long" else -1
    NORM = 0.005  # sectors move more than indices
    return max(-1.0, min(1.0, (ret_5m * direction) / NORM))


def f3_volume_buildup(bars: list[dict]) -> float:
    """F3. Last 5 bars vol vs trailing 30-bar avg. Score [-1, +1].

    Independent from gate G6 (volume_drying) — that gate fires when recent
    volume is BELOW baseline (coil compression). F3 here is symmetric: ratio
    above 1.0 = positive score, below = negative. The two coexist because
    coiling beneath resistance with falling pre-breakout volume CAN itself
    score above neutral if the very last bars tick up — F3 captures the
    micro-buildup at the tail end."""
    if len(bars) < 35:
        return 0.0
    last_5_avg = sum(b["v"] for b in bars[-5:]) / 5
    trailing_30_avg = sum(b["v"] for b in bars[-35:-5]) / 30
    if trailing_30_avg <= 0:
        return 0.0
    ratio = last_5_avg / trailing_30_avg
    return max(-1.0, min(1.0, ratio - 1.0))


def f4_tape_strength(side: str, ticker: str, alpaca, now: datetime) -> float:
    """F4. Lee-Ready aggressor over prior 30 min. Score [-1, +1]."""
    end = now.isoformat(timespec="seconds")
    start = (now - timedelta(minutes=30)).isoformat(timespec="seconds")
    try:
        trades = alpaca.get_trades(ticker, start=start, end=end, limit=10000)
        quotes = alpaca.get_quotes(ticker, start=start, end=end, limit=10000)
    except Exception:
        return 0.0
    if not trades:
        return 0.0
    try:
        from features.aggressor import classify_trades
        classified = classify_trades(trades, quotes)
    except Exception:
        return 0.0
    ask_premium = sum(t.size * t.price for t in classified if t.sign == 1)
    bid_premium = sum(t.size * t.price for t in classified if t.sign == -1)
    total = ask_premium + bid_premium
    if total <= 0:
        return 0.0
    ask_pct = ask_premium / total
    direction = 1 if side == "long" else -1
    return max(-1.0, min(1.0, (ask_pct - 0.5) * 2 * direction))


def f5_aligned_flow(side: str, flow_records: list, now: datetime) -> float:
    """F5. Net signed premium over last 30 min, ticker-baseline-normalized.
    Score [-1, +1]. We use a fixed $1M baseline for v1 (per-ticker baseline
    from rolling 30d UW history is a v2 add)."""
    if not flow_records:
        return 0.0
    cutoff = now - timedelta(minutes=30)
    flow_30m = [r for r in flow_records
                  if getattr(r, "timestamp", now) >= cutoff]
    call_buy = sum(getattr(r, "premium", 0) or 0 for r in flow_30m
                    if getattr(r, "side", None) == "call"
                    and getattr(r, "action", None) == "buy")
    put_buy = sum(getattr(r, "premium", 0) or 0 for r in flow_30m
                   if getattr(r, "side", None) == "put"
                   and getattr(r, "action", None) == "buy")
    call_sell = sum(getattr(r, "premium", 0) or 0 for r in flow_30m
                     if getattr(r, "side", None) == "call"
                     and getattr(r, "action", None) == "sell")
    put_sell = sum(getattr(r, "premium", 0) or 0 for r in flow_30m
                    if getattr(r, "side", None) == "put"
                    and getattr(r, "action", None) == "sell")
    net_signed = (call_buy - put_buy - call_sell + put_sell)
    BASELINE = 1_000_000.0
    raw = net_signed / BASELINE
    direction = 1 if side == "long" else -1
    aligned = raw * direction
    return max(-1.0, min(1.0, aligned / 2.0))


def f6_sweep_concentration(side: str, flow_records: list, now: datetime) -> float:
    """F6. Aligned sweep count in last 15 min. Score [-1, +1]."""
    if not flow_records:
        return 0.0
    cutoff = now - timedelta(minutes=15)
    sweeps = [r for r in flow_records
                if getattr(r, "is_sweep", False)
                and getattr(r, "timestamp", now) >= cutoff]
    expected_side = "call" if side == "long" else "put"
    aligned = [s for s in sweeps
                 if getattr(s, "side", None) == expected_side
                 and getattr(s, "action", None) == "buy"]
    if not aligned:
        return 0.0
    count = len(aligned)
    return max(-1.0, min(1.0, (count - 1.0) / 5.0))


def f7_vix_bucket(alpaca, now: datetime) -> str:
    """F7. low / normal / elevated. Uses VXX as paper-account-friendly VIX proxy."""
    end = now.isoformat(timespec="seconds")
    start = (now - timedelta(minutes=10)).isoformat(timespec="seconds")
    try:
        # Try VIX directly first; fall back to VXX (VIX tracker ETN)
        for sym in ("VIX", "VIXY", "VXX"):
            try:
                bars = alpaca.get_bars(sym, start=start, end=end,
                                          timeframe="1Min", limit=2)
                if bars:
                    vix = bars[-1].c
                    # VXX/VIXY are 1m-bar proxies — apply rough scale
                    if sym in ("VXX", "VIXY"):
                        # VXX trades around 30-50 in low VIX; map roughly.
                        vix_estimate = vix * 0.7
                    else:
                        vix_estimate = vix
                    if vix_estimate < 14:
                        return "low"
                    if vix_estimate < 22:
                        return "normal"
                    return "elevated"
            except Exception:
                continue
    except Exception:
        pass
    return "normal"


def f8_time_of_day(now_utc: datetime) -> str:
    """F8. Maps UTC time to design-doc time-of-day bucket (ET-derived)."""
    # EDT = UTC-4 (Apr 2026 is DST). 9:30 ET = 13:30 UTC.
    et_h = (now_utc.hour - 4) % 24
    et_m = now_utc.minute
    et = et_h + et_m / 60.0
    if 9.5 <= et < 10.0:
        return "opening_drive"
    if 10.0 <= et < 11.5:
        return "morning_trend"
    if 11.5 <= et < 14.0:
        return "midday_chop"
    if 14.0 <= et < 15.5:
        return "afternoon_trend"
    if 15.5 <= et < 16.0:
        return "close_window"
    return "off_hours"


# Per design doc §5b
WEIGHTS = {
    "f1_index_align": 0.18,
    "f2_sector_align": 0.12,
    "f3_volume_buildup": 0.15,
    "f4_tape_strength": 0.13,
    "f5_aligned_flow": 0.20,
    "f6_sweep_conc": 0.07,
}

VIX_MOD = {"low": 0.7, "normal": 1.0, "elevated": 1.2}
TOD_MOD = {
    "opening_drive": 1.1, "morning_trend": 1.0,
    "midday_chop": 0.7, "afternoon_trend": 1.0,
    "close_window": 0.6, "off_hours": 0.5,
}


def composite_score(factors: dict, vix_bucket: str, tod_bucket: str) -> float:
    raw = sum(WEIGHTS.get(k, 0) * factors.get(k, 0) for k in WEIGHTS)
    vix_m = VIX_MOD.get(vix_bucket, 1.0)
    tod_m = TOD_MOD.get(tod_bucket, 1.0)
    score = raw * vix_m * tod_m
    return max(-1.0, min(1.0, score))


def score_to_verdict(comp: float) -> str:
    """Per design doc §5c thresholds."""
    if comp >= 0.50:
        return "STRONG_ALIGN"
    if comp >= 0.20:
        return "ALIGN"
    if comp >= -0.20:
        return "NEUTRAL"
    if comp >= -0.50:
        return "COUNTER"
    return "STRONG_COUNTER"


# ─── Pattern check primitives ─────────────────────────────────────────────


def _bars_to_dict_list(bars) -> list[dict]:
    """Normalize Alpaca Bar dataclass → engine_v4-shaped dict so the gate
    functions stay close to the proven implementation."""
    return [
        {"o": b.o, "h": b.h, "l": b.l, "c": b.c, "v": b.v,
         "t": b.t.isoformat() if hasattr(b.t, "isoformat") else b.t}
        for b in bars
    ]


def compressed(bars: list[dict]) -> tuple[bool, Optional[float], Optional[float]]:
    """Range of compression window ≤ 1.5% of avg close. Returns (ok, hi, lo)."""
    if len(bars) < COMPRESSION_BARS:
        return (False, None, None)
    win = bars[-COMPRESSION_BARS:-1]
    hi = max(b["h"] for b in win)
    lo = min(b["l"] for b in win)
    avg = sum(b["c"] for b in win) / max(1, len(win))
    if avg <= 0:
        return (False, None, None)
    pct = (hi - lo) / avg
    return (pct <= COMPRESSION_MAX_PCT, hi, lo)


def level_tests(bars: list[dict], level: float, kind: str = "high") -> int:
    """Count bars whose high (or low) sits within tolerance of level."""
    tol = level * LEVEL_TOLERANCE_PCT
    if kind == "high":
        return sum(1 for b in bars if (level - tol) <= b["h"] <= (level + tol))
    return sum(1 for b in bars if (level - tol) <= b["l"] <= (level + tol))


def higher_lows(bars: list[dict]) -> bool:
    """Lows progressing up across last 20 bars (4 windows of 5)."""
    if len(bars) < 20:
        return False
    last20 = bars[-20:]
    lows = [min(b["l"] for b in last20[i*5:(i+1)*5]) for i in range(4)]
    up = sum(1 for i in range(3) if lows[i+1] > lows[i])
    return up >= 2 and lows[-1] > lows[0]


def lower_highs(bars: list[dict]) -> bool:
    if len(bars) < 20:
        return False
    last20 = bars[-20:]
    highs = [max(b["h"] for b in last20[i*5:(i+1)*5]) for i in range(4)]
    dn = sum(1 for i in range(3) if highs[i+1] < highs[i])
    return dn >= 2 and highs[-1] < highs[0]


def volume_drying(bars: list[dict]) -> bool:
    """Recent 5-bar avg vol < 85% of trailing 20-bar avg. Coiling, not distributing."""
    if len(bars) < 25:
        return False
    recent = sum(b["v"] for b in bars[-5:]) / 5
    baseline = sum(b["v"] for b in bars[-25:-5]) / 20
    if baseline <= 0:
        return False
    return recent / baseline < VOL_DRY_RATIO


# ─── Direction-specific gate runners ─────────────────────────────────────


def check_prebreak_long(bars: list[dict]) -> tuple[bool, dict]:
    """SURGE candidate. All 6 structural gates must pass."""
    ok, hi, lo = compressed(bars)
    if not ok:
        return (False, {"reason": "not_compressed"})

    win = bars[-COMPRESSION_BARS:-1]
    level = hi
    tests = level_tests(win, level, "high")
    if tests < MIN_LEVEL_TESTS:
        return (False, {"reason": "few_level_tests", "tests": tests})

    if not higher_lows(bars):
        return (False, {"reason": "no_higher_lows"})

    last_c = bars[-1]["c"]
    dist = (level - last_c) / level if level > 0 else 1
    if dist < 0 or dist > PROXIMITY_PCT:
        return (False, {"reason": "not_near_level",
                          "dist_pct": round(dist * 100, 3)})

    tol = level * LEVEL_TOLERANCE_PCT
    if bars[-1]["h"] > level + tol:
        return (False, {"reason": "already_broke"})

    if not volume_drying(bars):
        return (False, {"reason": "volume_not_drying"})

    return (True, {
        "level": level, "dist_pct": round(dist * 100, 3),
        "level_tests": tests,
        "compression_pct": round((hi - lo) / level * 100, 3) if level else None,
    })


def check_prebreak_short(bars: list[dict]) -> tuple[bool, dict]:
    """TANK candidate."""
    ok, hi, lo = compressed(bars)
    if not ok:
        return (False, {"reason": "not_compressed"})

    win = bars[-COMPRESSION_BARS:-1]
    level = lo
    tests = level_tests(win, level, "low")
    if tests < MIN_LEVEL_TESTS:
        return (False, {"reason": "few_level_tests", "tests": tests})

    if not lower_highs(bars):
        return (False, {"reason": "no_lower_highs"})

    last_c = bars[-1]["c"]
    dist = (last_c - level) / level if level > 0 else 1
    if dist < 0 or dist > PROXIMITY_PCT:
        return (False, {"reason": "not_near_level",
                          "dist_pct": round(dist * 100, 3)})

    tol = level * LEVEL_TOLERANCE_PCT
    if bars[-1]["l"] < level - tol:
        return (False, {"reason": "already_broke"})

    if not volume_drying(bars):
        return (False, {"reason": "volume_not_drying"})

    return (True, {
        "level": level, "dist_pct": round(dist * 100, 3),
        "level_tests": tests,
        "compression_pct": round((hi - lo) / level * 100, 3) if level else None,
    })


# ─── UW flow alignment ────────────────────────────────────────────────────


def has_aligned_flow(uw, ticker: str, side: str, target_dt: date) -> tuple[bool, dict]:
    """Per engine_v4 logic: a qualifying flow alert in the last FLOW_RECENCY_MIN
    minutes, same side as our directional bet.

    side: 'long' (CALL flow) or 'short' (PUT flow).
    """
    if uw is None:
        return (False, {"reason": "uw_unavailable"})
    try:
        # Use flow_recent for today (UW returns recent records, we filter by age)
        records = uw.flow_recent(ticker)
    except Exception as e:
        return (False, {"reason": f"uw_error: {type(e).__name__}: {e}"})

    if not records:
        return (False, {"reason": "no_flow"})

    expected_side = "call" if side == "long" else "put"
    now = datetime.now(tz=timezone.utc)
    recent_aligned = []
    for r in records:
        if getattr(r, "side", None) != expected_side:
            continue
        if getattr(r, "action", None) != "buy":
            continue
        ts = getattr(r, "timestamp", None)
        if ts is None:
            continue
        try:
            age_min = (now - ts).total_seconds() / 60.0
        except Exception:
            continue
        if age_min <= FLOW_RECENCY_MIN:
            recent_aligned.append(r)

    if not recent_aligned:
        return (False, {"reason": "no_recent_aligned_flow",
                          "n_total_flow": len(records)})

    total_premium = sum(float(getattr(r, "premium", 0) or 0) for r in recent_aligned)
    return (True, {
        "n_aligned_flow": len(recent_aligned),
        "total_aligned_premium": total_premium,
        "newest_age_min": min(
            (now - getattr(r, "timestamp", now)).total_seconds() / 60.0
            for r in recent_aligned
        ),
    })


# ─── Composite confidence (combines structural + flow signals) ───────────


def compute_confidence(structural: dict, flow: dict) -> float:
    """Returns [0, 100]. Higher = stronger pre-breakout setup.

    Inputs:
      structural: dict from check_prebreak_long/short on success
      flow: dict from has_aligned_flow on success

    Components:
      · base 50 (gates passed)
      · proximity bonus (closer to level = more imminent break)
      · level_tests bonus (more tests = stronger level)
      · compression tightness (tighter = more energy)
      · flow premium scaling (more premium = stronger conviction)
      · flow recency bonus (fresher = better)
    """
    score = 50.0

    # Proximity (closer = better) — at 0% dist score adds 10, at PROXIMITY_PCT adds 0
    dist_pct = structural.get("dist_pct", PROXIMITY_PCT * 100)
    score += 10.0 * (1.0 - dist_pct / (PROXIMITY_PCT * 100))

    # Level tests — 2 = base, 4+ = max bonus
    tests = structural.get("level_tests", 2)
    score += min(10.0, (tests - 2) * 5.0)

    # Compression tightness — at 1.5% (max allowed) adds 0, at 0.5% adds 10
    comp = structural.get("compression_pct", 1.5)
    score += max(0.0, 10.0 * (1.0 - comp / 1.5))

    # Flow premium ($) — log-scaled. $1M = +5, $10M = +10
    prem = flow.get("total_aligned_premium", 0)
    if prem > 0:
        import math
        score += min(15.0, 5.0 * math.log10(max(1.0, prem / 100_000)))

    # Flow recency — newest_age_min near 0 adds 5, at FLOW_RECENCY_MIN adds 0
    age = flow.get("newest_age_min", FLOW_RECENCY_MIN)
    score += 5.0 * (1.0 - age / FLOW_RECENCY_MIN)

    return max(0.0, min(100.0, score))


# ─── Universe scan ────────────────────────────────────────────────────────


def compute_factors(side: str, ticker: str, bars: list[dict],
                     flow_records: list, alpaca, now: datetime,
                     vix_bucket: str, tod_bucket: str) -> dict:
    """Compute F1-F6 + composite for one (ticker, side). Per design doc §6."""
    factors = {
        "f1_index_align": f1_index_align(side, ticker, alpaca, now),
        "f2_sector_align": f2_sector_align(side, ticker, alpaca, now),
        "f3_volume_buildup": f3_volume_buildup(bars),
        "f4_tape_strength": f4_tape_strength(side, ticker, alpaca, now),
        "f5_aligned_flow": f5_aligned_flow(side, flow_records, now),
        "f6_sweep_conc": f6_sweep_concentration(side, flow_records, now),
    }
    composite = composite_score(factors, vix_bucket, tod_bucket)
    return {
        "factors": {k: round(v, 3) for k, v in factors.items()},
        "composite": round(composite, 3),
        "verdict": score_to_verdict(composite),
        "vix_bucket": vix_bucket,
        "tod_bucket": tod_bucket,
    }


def scan_one(alpaca, uw, ticker: str, now: Optional[datetime] = None,
              vix_bucket: Optional[str] = None,
              tod_bucket: Optional[str] = None) -> dict:
    """Pull bars + flow for one ticker, run gates AND factors, return result."""
    if now is None:
        now = datetime.now(tz=timezone.utc)
    today = now.date()
    if vix_bucket is None:
        vix_bucket = f7_vix_bucket(alpaca, now)
    if tod_bucket is None:
        tod_bucket = f8_time_of_day(now)

    end = now.isoformat(timespec="seconds")
    start = (now - timedelta(minutes=BARS_LOOKBACK + 5)).isoformat(timespec="seconds")
    try:
        bars_raw = alpaca.get_bars(ticker, start=start, end=end,
                                     timeframe="1Min", limit=BARS_LOOKBACK + 5)
    except Exception as e:
        return {"ticker": ticker, "ok": False,
                 "error": f"alpaca: {type(e).__name__}: {e}"}

    if len(bars_raw) < COMPRESSION_BARS:
        return {"ticker": ticker, "ok": False,
                 "reason": f"insufficient_bars ({len(bars_raw)} < {COMPRESSION_BARS})",
                 "n_bars": len(bars_raw)}

    bars = _bars_to_dict_list(bars_raw)

    out = {"ticker": ticker, "ok": True, "n_bars": len(bars),
            "last_close": bars[-1]["c"], "candidates": []}

    # Pre-fetch flow once per ticker (we'll use for both sides + factor scoring)
    flow_records = []
    if uw is not None:
        try:
            flow_records = uw.flow_recent(ticker)
        except Exception:
            flow_records = []

    # SURGE — gates first (filter), then factors (rank)
    surge_ok, surge_meta = check_prebreak_long(bars)
    if surge_ok:
        flow_ok, flow_meta = has_aligned_flow(uw, ticker, "long", today)
        if flow_ok:
            confidence = compute_confidence(surge_meta, flow_meta)
            factor_pkg = compute_factors(
                "long", ticker, bars, flow_records, alpaca, now,
                vix_bucket, tod_bucket,
            )
            out["candidates"].append({
                "ticker": ticker, "side": "SURGE", "direction": "long",
                "level": surge_meta["level"],
                "level_tests": surge_meta["level_tests"],
                "dist_pct": surge_meta["dist_pct"],
                "compression_pct": surge_meta["compression_pct"],
                "n_aligned_flow": flow_meta["n_aligned_flow"],
                "total_aligned_premium": flow_meta["total_aligned_premium"],
                "newest_age_min": round(flow_meta["newest_age_min"], 1),
                "confidence": round(confidence, 1),
                "last_close": bars[-1]["c"],
                "computed_at": now.isoformat(timespec="seconds") + "Z",
                # 8-factor scoring layer (per design doc §5)
                **factor_pkg,
            })
        else:
            out["surge_pass_reason"] = "no_aligned_flow_for_surge: " + flow_meta.get("reason", "")
    else:
        out["surge_pass_reason"] = surge_meta.get("reason")

    # TANK
    tank_ok, tank_meta = check_prebreak_short(bars)
    if tank_ok:
        flow_ok, flow_meta = has_aligned_flow(uw, ticker, "short", today)
        if flow_ok:
            confidence = compute_confidence(tank_meta, flow_meta)
            factor_pkg = compute_factors(
                "short", ticker, bars, flow_records, alpaca, now,
                vix_bucket, tod_bucket,
            )
            out["candidates"].append({
                "ticker": ticker, "side": "TANK", "direction": "short",
                "level": tank_meta["level"],
                "level_tests": tank_meta["level_tests"],
                "dist_pct": tank_meta["dist_pct"],
                "compression_pct": tank_meta["compression_pct"],
                "n_aligned_flow": flow_meta["n_aligned_flow"],
                "total_aligned_premium": flow_meta["total_aligned_premium"],
                "newest_age_min": round(flow_meta["newest_age_min"], 1),
                "confidence": round(confidence, 1),
                "last_close": bars[-1]["c"],
                "computed_at": now.isoformat(timespec="seconds") + "Z",
                **factor_pkg,
            })
        else:
            out["tank_pass_reason"] = "no_aligned_flow_for_tank: " + flow_meta.get("reason", "")
    else:
        out["tank_pass_reason"] = tank_meta.get("reason")

    return out


def scan_universe(universe: Optional[list[str]] = None,
                   now: Optional[datetime] = None) -> dict:
    """Scan every ticker in `universe`, return ranked SURGE + TANK lists."""
    if universe is None:
        universe = list(DEFAULT_UNIVERSE)
    if now is None:
        now = datetime.now(tz=timezone.utc)

    from infra.secrets import load_secrets
    load_secrets()
    from data_clients.alpaca import AlpacaClient
    try:
        from data_clients.unusual_whales import UWClient
        uw = UWClient()
    except Exception as e:
        print(f"[prebreakout] UW unavailable: {e}", flush=True)
        uw = None

    surge: list[dict] = []
    tank: list[dict] = []
    raw_results: list[dict] = []

    with AlpacaClient() as alp:
        # Compute regime + time-of-day once per scan (shared by all tickers)
        vix_bucket = f7_vix_bucket(alp, now)
        tod_bucket = f8_time_of_day(now)

        for ticker in universe:
            r = scan_one(alp, uw, ticker, now=now,
                          vix_bucket=vix_bucket, tod_bucket=tod_bucket)
            raw_results.append(r)
            for c in r.get("candidates") or []:
                if c["side"] == "SURGE":
                    surge.append(c)
                else:
                    tank.append(c)

    # Final ranking: composite score (factors) × confidence (gates)
    # — both sources of edge contribute, neither alone overrides the other
    def _rank_key(c):
        return -(c.get("composite", 0) * 0.5 + c.get("confidence", 0) / 200.0)
    surge.sort(key=_rank_key)
    tank.sort(key=_rank_key)

    return {
        "scanned_at": now.isoformat(timespec="seconds") + "Z",
        "universe_size": len(universe),
        "vix_bucket": vix_bucket,
        "tod_bucket": tod_bucket,
        "surge": surge,
        "tank": tank,
        "summary": {
            "n_surge": len(surge),
            "n_tank": len(tank),
            "n_universe": len(universe),
            "vix_bucket": vix_bucket,
            "tod_bucket": tod_bucket,
        },
        "raw": raw_results,
    }


if __name__ == "__main__":
    import json as _json
    r = scan_universe()
    print(_json.dumps(r["summary"], indent=2))
    print("\nSURGE candidates (top 5):")
    for c in r["surge"][:5]:
        print(f"  {c['ticker']:5s} conf={c['confidence']:5.1f} "
              f"level=${c['level']:.2f} dist={c['dist_pct']}%  "
              f"flow=${c['total_aligned_premium']:,.0f}")
    print("\nTANK candidates (top 5):")
    for c in r["tank"][:5]:
        print(f"  {c['ticker']:5s} conf={c['confidence']:5.1f} "
              f"level=${c['level']:.2f} dist={c['dist_pct']}%  "
              f"flow=${c['total_aligned_premium']:,.0f}")
