"""
shadow_logger_swing.py — poll swing engine snapshots and emit fire records.

READ-ONLY on swing_engine_v2/ and swing_trade_strategy/.
Sources polled (every POLL_SECONDS):
  swing_engine_v2/v2_active_portfolio.json     ← God-Mode probe/full layer
  swing_trade_strategy/stream_signals.json     ← Tier-based execution layer

State is kept per (source, ticker) so a transition like HOLD → BUY emits a fire,
while repeated BUYs do not. State persists to data/swing_log/_state.json so a
process restart doesn't replay phantom transitions.

Output: data/swing_log/<DATE>/swing_signals.jsonl  (one fire per line)
        data/swing_log/<DATE>/portfolio_snapshots.jsonl (hourly full snapshot)

Design contract: DESIGN.md §8 (swing capture) and §11 (shadow logger).

Run as a separate daemon — pipeline.py reads the resulting jsonl via loader_swing.py.

Usage:
    python3 shadow_logger_swing.py            # one-shot tick
    python3 shadow_logger_swing.py --daemon   # loop forever
"""
from __future__ import annotations

import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ──────────────────────────────────────────────────────────────────────────────
# Paths — READ-ONLY upstream, write-only into our project
# ──────────────────────────────────────────────────────────────────────────────

GEMINI_ROOT = Path("/Users/harshapuli/Downloads/spy QQQ/claude/gemini ")
V2_PORTFOLIO = GEMINI_ROOT / "swing_engine_v2" / "v2_active_portfolio.json"
STREAM_SIGNALS = GEMINI_ROOT / "swing_trade_strategy" / "stream_signals.json"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG_ROOT = PROJECT_ROOT / "data" / "swing_log"
STATE_FILE = LOG_ROOT / "_state.json"

POLL_SECONDS = 30
SNAPSHOT_EVERY_SECONDS = 3600   # save full portfolio snapshot every hour


# ──────────────────────────────────────────────────────────────────────────────
# Signal-text → kind normalization
# ──────────────────────────────────────────────────────────────────────────────

# v2_active_portfolio: "PROBE PUT (Pre-Positioning Level)" / "FULL LONG" / "HOLD" / etc.
V2_RE = re.compile(r"\b(PROBE|FULL|EXIT|TRIM|FLIP)\b.*?\b(CALL|PUT|LONG|SHORT)\b", re.I)

# swing_trade_strategy: "BUY (Tier 3: Trend Continuation)" / "BUY (Tier 1: Early Accumulation)"
TIER_RE = re.compile(r"BUY\s*\(Tier\s*(\d)[^)]*\)", re.I)


def _norm_v2(signal_text: str) -> Optional[tuple[str, str]]:
    """v2 portfolio signal text → (kind, side) or None for HOLD/AWAITING."""
    if not signal_text:
        return None
    s = signal_text.strip()
    s_upper = s.upper()
    if "HOLD" in s_upper or "AWAITING" in s_upper or "STAND" in s_upper:
        return None
    m = V2_RE.search(s_upper)
    if not m:
        return None
    action, dirn = m.group(1), m.group(2)
    if dirn in ("LONG", "CALL"):
        side = "CALL"
    elif dirn in ("SHORT", "PUT"):
        side = "PUT"
    else:
        return None
    kind = f"V2_{action}_{side}"
    return kind, side


def _norm_strategy(signal_text: str) -> Optional[tuple[str, str]]:
    """stream_signals BUY (Tier N: ...) → (kind, side); HOLD/VETO → None."""
    if not signal_text:
        return None
    s = signal_text.strip()
    s_upper = s.upper()
    if s_upper.startswith("HOLD"):
        return None
    if "VETO" in s_upper:
        return None
    m = TIER_RE.search(s)
    if not m:
        return None
    tier = m.group(1)
    # Tag a few common modifiers so kinds stay specific
    label_bits = []
    if "TREND" in s_upper:
        label_bits.append("TREND")
    if "EARLY" in s_upper or "ACCUMULATION" in s_upper:
        label_bits.append("ACCUM")
    if "BREAKOUT" in s_upper:
        label_bits.append("BREAKOUT")
    if "PRE-EARNINGS" in s_upper or "EARNINGS CATALYST" in s_upper:
        label_bits.append("EARNINGS")
    base = f"STRAT_BUY_T{tier}"
    if label_bits:
        base += "_" + "_".join(label_bits)
    return base, "CALL"   # stream_signals appears to track CALL tier-buys


# ──────────────────────────────────────────────────────────────────────────────
# State
# ──────────────────────────────────────────────────────────────────────────────


def _load_state() -> dict:
    if not STATE_FILE.exists():
        return {"v2_last": {}, "strategy_last": {}, "last_snapshot_epoch": 0.0}
    try:
        return json.loads(STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return {"v2_last": {}, "strategy_last": {}, "last_snapshot_epoch": 0.0}


def _save_state(st: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(st, indent=2))


# ──────────────────────────────────────────────────────────────────────────────
# Reading upstream snapshots
# ──────────────────────────────────────────────────────────────────────────────


def _read_json(path: Path) -> Optional[dict]:
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


# ──────────────────────────────────────────────────────────────────────────────
# Per-source pollers — return list of new fires
# ──────────────────────────────────────────────────────────────────────────────


def poll_v2(state: dict, now_iso: str, now_epoch: float) -> list[dict]:
    fires: list[dict] = []
    snap = _read_json(V2_PORTFOLIO)
    if not snap:
        return fires

    last = state["v2_last"]
    for ticker, row in snap.items():
        if not isinstance(row, dict):
            continue
        signal_text = row.get("signal", "")
        prev = last.get(ticker)

        norm = _norm_v2(signal_text)
        if norm is None:
            # not a fire state — but record so we know last-seen
            last[ticker] = signal_text
            continue
        kind, side = norm

        # Fire only on transition (kind changed) or first time we see this ticker firing
        prev_norm = _norm_v2(prev or "")
        prev_kind = prev_norm[0] if prev_norm else None
        if prev_kind == kind:
            last[ticker] = signal_text
            continue

        fires.append({
            "source": "swing_v2",
            "kind": kind,
            "side": side,
            "ticker": ticker,
            "detected_utc": now_iso,
            "detected_epoch": now_epoch,
            "raw_signal": signal_text,
            "entry_premium": row.get("estimated_premium"),
            "target_strike": row.get("target_strike"),
            "option_type": row.get("option_type"),
            "target_dte": row.get("target_dte"),
            "risk_pct": row.get("risk_pct"),
            "compression_active": row.get("compression_active"),
            "mss_active": row.get("mss_active"),
            "streak_mins": row.get("streak_mins"),
            "raw": row,
        })
        last[ticker] = signal_text

    state["v2_last"] = last
    return fires


def poll_strategy(state: dict, now_iso: str, now_epoch: float) -> list[dict]:
    fires: list[dict] = []
    snap = _read_json(STREAM_SIGNALS)
    if not snap:
        return fires

    last = state["strategy_last"]
    for ticker, row in snap.items():
        if not isinstance(row, dict):
            continue
        signal_text = row.get("signal", "")
        prev = last.get(ticker)

        norm = _norm_strategy(signal_text)
        if norm is None:
            last[ticker] = signal_text
            continue
        kind, side = norm

        prev_norm = _norm_strategy(prev or "")
        prev_kind = prev_norm[0] if prev_norm else None
        if prev_kind == kind:
            last[ticker] = signal_text
            continue

        fires.append({
            "source": "swing_strategy",
            "kind": kind,
            "side": side,
            "ticker": ticker,
            "detected_utc": now_iso,
            "detected_epoch": now_epoch,
            "raw_signal": signal_text,
            "entry_premium": row.get("entry_price"),
            "underlying_close": row.get("close"),
            "stop_loss": row.get("stop_loss"),
            "take_profit": row.get("take_profit"),
            "qty": row.get("qty"),
            "occ_symbol": row.get("occ_symbol"),
            "strike": row.get("strike"),
            "expiration": row.get("expiration"),
            "option_type": row.get("type"),
            "news_sentiment": row.get("news_sentiment"),
            "engine_timestamp": row.get("timestamp"),
            "raw": row,
        })
        last[ticker] = signal_text

    state["strategy_last"] = last
    return fires


# ──────────────────────────────────────────────────────────────────────────────
# Snapshot writers
# ──────────────────────────────────────────────────────────────────────────────


def _date_str_utc(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime("%Y-%m-%d")


def _append_jsonl(path: Path, records: list[dict]) -> None:
    if not records:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        for r in records:
            f.write(json.dumps(r) + "\n")


def write_fires(fires: list[dict]) -> None:
    if not fires:
        return
    by_date: dict[str, list[dict]] = {}
    for f in fires:
        d = _date_str_utc(f["detected_epoch"])
        by_date.setdefault(d, []).append(f)
    for d, rows in by_date.items():
        _append_jsonl(LOG_ROOT / d / "swing_signals.jsonl", rows)


def write_snapshot(now_iso: str, now_epoch: float) -> None:
    """Hourly full-portfolio snapshot — captures HOLD/AWAITING context."""
    v2 = _read_json(V2_PORTFOLIO)
    strat = _read_json(STREAM_SIGNALS)
    rec = {
        "snapshot_utc": now_iso,
        "snapshot_epoch": now_epoch,
        "v2_portfolio": v2,
        "stream_signals": strat,
    }
    d = _date_str_utc(now_epoch)
    _append_jsonl(LOG_ROOT / d / "portfolio_snapshots.jsonl", [rec])


# ──────────────────────────────────────────────────────────────────────────────
# Tick / daemon
# ──────────────────────────────────────────────────────────────────────────────


def tick() -> int:
    """Run one polling cycle. Returns # fires written."""
    state = _load_state()
    now = datetime.now(timezone.utc)
    now_iso = now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond:06d}+00:00"
    now_epoch = now.timestamp()

    fires_v2 = poll_v2(state, now_iso, now_epoch)
    fires_st = poll_strategy(state, now_iso, now_epoch)
    all_fires = fires_v2 + fires_st
    write_fires(all_fires)

    # Hourly snapshot
    last_snap = state.get("last_snapshot_epoch", 0.0) or 0.0
    if now_epoch - last_snap >= SNAPSHOT_EVERY_SECONDS:
        write_snapshot(now_iso, now_epoch)
        state["last_snapshot_epoch"] = now_epoch

    _save_state(state)
    return len(all_fires)


def run_daemon() -> None:
    print(f"[shadow_swing] daemon starting — poll every {POLL_SECONDS}s")
    print(f"[shadow_swing] log root: {LOG_ROOT}")
    while True:
        try:
            n = tick()
            stamp = datetime.now(timezone.utc).strftime("%H:%M:%S")
            if n:
                print(f"[shadow_swing] {stamp}Z — {n} fire(s) written")
        except Exception as e:
            print(f"[shadow_swing] tick error: {e!r}", file=sys.stderr)
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    if "--daemon" in sys.argv:
        run_daemon()
    else:
        n = tick()
        print(f"[shadow_swing] one-shot — {n} fire(s) written")
        # show what we know about both files
        v2 = _read_json(V2_PORTFOLIO)
        st = _read_json(STREAM_SIGNALS)
        print(f"  v2_portfolio: {V2_PORTFOLIO.exists()} ({len(v2 or {})} tickers)")
        print(f"  stream_signals: {STREAM_SIGNALS.exists()} ({len(st or {})} tickers)")
        print(f"  state file: {STATE_FILE}")
