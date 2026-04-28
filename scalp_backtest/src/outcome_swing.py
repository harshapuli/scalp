"""
outcome_swing.py — for every SwingSignal, compute underlying-relative outcomes
at fixed horizons (1d, 3d, 5d, 10d, 20d, EOD-on-expiration).

Design contract: DESIGN.md §8 (swing outcome) and §14 Q17.

Inputs:
  - SwingSignal records (from loader_swing.load_all)
  - daily_bars: dict[ticker -> list[DailyBar]] (optional; if missing, outcome fields stay None)

We do NOT have option-quote history — option premium outcomes are inferred from
underlying via a simple delta proxy:
    target_underlying_move_pct  = +5.0% for CALL, −5.0% for PUT
    stop_underlying_move_pct    = −2.5% for CALL, +2.5% for PUT
This is an approximation — real option PnL depends on IV/DTE/delta. It's
clearly labeled in the dashboard as a proxy until we wire option quotes.

Cost-aware labels (mirror outcome_intraday §8f):
  label_underlying     : flat_cost = 0%
  label_option_proxy   : flat_cost = 8% RT × 5.5× leverage on underlying move
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loader_swing import SwingSignal


# ──────────────────────────────────────────────────────────────────────────────
# Constants
# ──────────────────────────────────────────────────────────────────────────────

HORIZONS_DAYS = (1, 3, 5, 10, 20)
TARGET_UNDERLYING_PCT = 5.0    # +5% for CALL, -5% for PUT (proxy for ~2× option)
STOP_UNDERLYING_PCT   = 2.5    # -2.5% for CALL, +2.5% for PUT (proxy for ~0.5× option)
FLAT_COST_OPTION_5P5X = 8.0    # %
OPTION_LEVERAGE = 5.5
MARGINAL_BAND_PCT = 2.0


@dataclass
class DailyBar:
    """Daily OHLCV bar — populated from yfinance/alpaca when available."""
    date: str           # "YYYY-MM-DD"
    epoch: float
    o: float
    h: float
    l: float
    c: float
    v: int


# ──────────────────────────────────────────────────────────────────────────────
# Outcome record
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class SwingOutcome:
    # identity
    source: str
    kind: str
    side: str
    ticker: str
    detected_utc: str
    detected_epoch: float
    date_str: str

    # context (mirrored from fire)
    entry_premium: Optional[float] = None
    underlying_close: Optional[float] = None
    stop_loss_premium: Optional[float] = None
    take_profit_premium: Optional[float] = None
    risk_pct: Optional[float] = None
    compression_active: Optional[bool] = None
    mss_active: Optional[bool] = None
    news_sentiment: Optional[str] = None
    target_dte: Optional[str] = None
    expiration: Optional[str] = None

    # outcomes — populated only when daily_bars is supplied
    has_daily_bars: bool = False
    bars_walked: int = 0
    ret_1d_pct: Optional[float] = None
    ret_3d_pct: Optional[float] = None
    ret_5d_pct: Optional[float] = None
    ret_10d_pct: Optional[float] = None
    ret_20d_pct: Optional[float] = None
    ret_to_expiration_pct: Optional[float] = None
    mfe_pct: Optional[float] = None
    mae_pct: Optional[float] = None

    # ladder events (underlying-equivalent thresholds)
    target_hit_day: Optional[int] = None       # 1-indexed bar where target first touched
    stop_hit_day: Optional[int] = None         # 1-indexed bar where stop first touched
    first_event: Optional[str] = None          # "TARGET" | "STOP" | "EXPIRE_NO_HIT" | None

    # cost-aware labels
    label_underlying: Optional[int] = None     # +1/0/-1
    label_option_proxy: Optional[int] = None   # +1/0/-1 (5.5×, 8% RT)

    # bookkeeping
    notes: Optional[str] = None
    raw: dict = field(default_factory=dict, repr=False)


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _signed_pct(side: str, base: float, price: float) -> float:
    """Return % move signed FOR THE SIDE (CALL up positive, PUT down positive)."""
    if not base:
        return 0.0
    raw = (price - base) / base * 100.0
    return raw if side == "CALL" else -raw


def _label_from_pnl(event_label: str | None, pnl_pct: float | None,
                    leverage: float, flat_cost: float) -> int:
    """+1 if target hit and net positive; 0 if within ±MARGINAL_BAND_PCT; -1 otherwise."""
    if pnl_pct is None:
        return 0
    net = pnl_pct * leverage - flat_cost
    if event_label and event_label.startswith("TARGET") and net > 0:
        return 1
    if abs(net) <= MARGINAL_BAND_PCT:
        return 0
    return -1


def _bars_after_fire(bars: list[DailyBar], fire_epoch: float) -> list[DailyBar]:
    """Bars strictly after fire_epoch (next session onward)."""
    return [b for b in bars if b.epoch > fire_epoch]


def _ret_at_index(forward: list[DailyBar], side: str, entry: float, idx: int) -> Optional[float]:
    if idx >= len(forward) or entry == 0:
        return None
    return _signed_pct(side, entry, forward[idx].c)


# ──────────────────────────────────────────────────────────────────────────────
# Compute
# ──────────────────────────────────────────────────────────────────────────────


def compute_outcome(sig: SwingSignal,
                    daily_bars: Optional[list[DailyBar]] = None) -> SwingOutcome:
    out = SwingOutcome(
        source=sig.source,
        kind=sig.kind,
        side=sig.side,
        ticker=sig.ticker,
        detected_utc=sig.detected_utc,
        detected_epoch=sig.detected_epoch,
        date_str=sig.date_str,
        entry_premium=sig.entry_premium,
        underlying_close=sig.underlying_close,
        stop_loss_premium=sig.stop_loss,
        take_profit_premium=sig.take_profit,
        risk_pct=sig.risk_pct,
        compression_active=sig.compression_active,
        mss_active=sig.mss_active,
        news_sentiment=sig.news_sentiment,
        target_dte=sig.target_dte,
        expiration=sig.expiration,
        raw=sig.raw,
    )

    if not daily_bars:
        out.notes = "outcome_pending_no_daily_bars"
        return out

    forward = _bars_after_fire(daily_bars, sig.detected_epoch)
    if not forward:
        out.notes = "outcome_pending_no_forward_bars"
        return out

    entry = sig.underlying_close
    if not entry:
        # try entry from first forward open
        entry = forward[0].o
    if not entry:
        out.notes = "no_entry_price"
        return out

    out.has_daily_bars = True
    out.bars_walked = len(forward)

    # Horizon returns at close-of-day
    out.ret_1d_pct  = _ret_at_index(forward, sig.side, entry, 0)
    out.ret_3d_pct  = _ret_at_index(forward, sig.side, entry, 2)
    out.ret_5d_pct  = _ret_at_index(forward, sig.side, entry, 4)
    out.ret_10d_pct = _ret_at_index(forward, sig.side, entry, 9)
    out.ret_20d_pct = _ret_at_index(forward, sig.side, entry, 19)

    # Ret to expiration (if expiration date present)
    if sig.expiration:
        try:
            exp_dt = datetime.strptime(sig.expiration, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            exp_ep = exp_dt.timestamp()
            up_to = [b for b in forward if b.epoch <= exp_ep]
            if up_to:
                out.ret_to_expiration_pct = _signed_pct(sig.side, entry, up_to[-1].c)
        except ValueError:
            pass

    # Walk bars to find target/stop event
    mfe = float("-inf")
    mae = float("inf")
    target_day = None
    stop_day = None
    for i, b in enumerate(forward, start=1):
        # signed extremes for the side
        if sig.side == "CALL":
            fav_extreme = (b.h - entry) / entry * 100.0
            adv_extreme = (b.l - entry) / entry * 100.0
        else:  # PUT — favorable is downside
            fav_extreme = (entry - b.l) / entry * 100.0
            adv_extreme = (entry - b.h) / entry * 100.0

        if fav_extreme > mfe:
            mfe = fav_extreme
        if adv_extreme < mae:
            mae = adv_extreme

        if target_day is None and fav_extreme >= TARGET_UNDERLYING_PCT:
            target_day = i
        if stop_day is None and adv_extreme <= -STOP_UNDERLYING_PCT:
            stop_day = i
        if target_day or stop_day:
            # if BOTH same day, the underlying daily bar can't disambiguate —
            # mark conservatively: STOP-first
            break

    out.mfe_pct = mfe if mfe != float("-inf") else None
    out.mae_pct = mae if mae != float("inf") else None
    out.target_hit_day = target_day
    out.stop_hit_day = stop_day

    if target_day and stop_day:
        # same-bar ambiguity → conservative STOP-first
        out.first_event = "STOP_AMBIG"
        pnl_pct = -STOP_UNDERLYING_PCT
        event_label = "STOP"
    elif target_day:
        out.first_event = "TARGET"
        pnl_pct = TARGET_UNDERLYING_PCT
        event_label = "TARGET"
    elif stop_day:
        out.first_event = "STOP"
        pnl_pct = -STOP_UNDERLYING_PCT
        event_label = "STOP"
    else:
        out.first_event = "EXPIRE_NO_HIT"
        pnl_pct = out.ret_to_expiration_pct or out.ret_20d_pct or out.ret_10d_pct or out.ret_5d_pct or 0.0
        event_label = "EXPIRE"

    out.label_underlying  = _label_from_pnl(event_label, pnl_pct, leverage=1.0, flat_cost=0.0)
    out.label_option_proxy = _label_from_pnl(event_label, pnl_pct,
                                             leverage=OPTION_LEVERAGE,
                                             flat_cost=FLAT_COST_OPTION_5P5X)

    return out


def compute_all(signals: list[SwingSignal],
                daily_bars: Optional[dict[str, list[DailyBar]]] = None
                ) -> list[SwingOutcome]:
    out: list[SwingOutcome] = []
    bars = daily_bars or {}
    for s in signals:
        out.append(compute_outcome(s, bars.get(s.ticker)))
    return out


def dedup_signals(signals: list[SwingSignal]) -> list[SwingSignal]:
    """Keep first fire per (date, ticker, kind, side) — design §14 Q4."""
    seen: set[tuple] = set()
    keep: list[SwingSignal] = []
    for s in sorted(signals, key=lambda x: x.detected_epoch):
        k = (s.date_str, s.ticker, s.kind, s.side)
        if k in seen:
            continue
        seen.add(k)
        keep.append(s)
    return keep


# ──────────────────────────────────────────────────────────────────────────────
# CLI sanity
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    from loader_swing import load_all

    sigs = load_all()
    print(f"raw swing fires: {len(sigs)}")

    sigs_dedup = dedup_signals(sigs)
    print(f"deduped (1 per ticker/session/kind): {len(sigs_dedup)}")

    outs = compute_all(sigs_dedup)   # no daily_bars yet
    print(f"computed {len(outs)} outcomes (daily_bars=None)\n")

    pending = sum(1 for o in outs if not o.has_daily_bars)
    matured = sum(1 for o in outs if o.has_daily_bars)
    print(f"matured: {matured}    pending: {pending}\n")

    # Headline kind table — even without outcomes we can show fires per kind
    by_kind: dict[str, list[SwingOutcome]] = {}
    for o in outs:
        by_kind.setdefault(o.kind, []).append(o)

    print(f"\n{'KIND':35s} {'n':>4s} {'side':>5s} {'avg risk%':>10s} {'comp':>5s}")
    print("─" * 70)
    for kind in sorted(by_kind, key=lambda k: -len(by_kind[k])):
        rows = by_kind[kind]
        n = len(rows)
        sides = {r.side for r in rows}
        side_str = "/".join(sorted(sides))
        risks = [r.risk_pct for r in rows if r.risk_pct is not None]
        avg_risk = sum(risks) / len(risks) if risks else 0.0
        compr = sum(1 for r in rows if r.compression_active)
        print(f"{kind:35s} {n:>4d} {side_str:>5s} {avg_risk:>9.1f}% {compr:>4d}")
