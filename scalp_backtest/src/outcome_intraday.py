"""
outcome_intraday.py — for every scalp Signal, walk forward through 1-min bars
and compute MFE/MAE, ATR-primary ladder, pct-secondary ladder, dual STOP/TARGET-
first passes, and cost-aware labels.

Design contract: DESIGN.md §8.

Sign convention: returns are signed FOR THE SIDE.
  CALL → up = positive
  PUT  → down = positive (we negate underlying %)

Wide-bar pass (must-fix item 11):
  STOP-first  — assume stop fires before targets when both touched same bar
  TARGET-first— assume targets fire first (optimistic upper bound)

Cost-aware labels (must-fix item 6):
  label_underlying    : flat_cost = 0%
  label_option_5p5x   : flat_cost = 8% RT × 5.5× leverage
  label = +1 if target hit AND net_pnl > 0
  label =  0 if abs(net_pnl) <= MARGINAL_BAND_PCT
  label = -1 otherwise
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from loader_scalp import Signal, Bar


# ──────────────────────────────────────────────────────────────────────────────
# Constants — design §14 defaults
# ──────────────────────────────────────────────────────────────────────────────

# ATR ladder (multipliers on ATR(14))
ATR_TARGETS = (0.75, 1.5, 2.25)   # T1, T2, T3
ATR_STOP    = 1.0                 # adverse multiplier (negative on signed pct)

# Pct ladder (secondary, parallel)
PCT_TARGETS = (0.5, 1.0, 1.5)
PCT_STOP    = 0.40

# Cost basis (round-trip) — design §8f
FLAT_COST_UNDERLYING = 0.0   # %
FLAT_COST_OPTION_5P5X = 8.0  # %
OPTION_LEVERAGE = 5.5

# ─── Two-regime option model (replaces flat 5.5× linear, which is broken at T1) ─
# Slow regime: theta + spread bleed dominate, no gamma kicker → 4× leverage, 8% cost
# Fast regime: underlying moves ≥0.5 ATR within 30 min → premium spikes via gamma,
#              spread tighter on momentum → 8× leverage, 6% cost
# Detection: walk forward, time-to-half-ATR. ≤FAST_REGIME_BARS minutes ⇒ fast.
OPTION_LEVERAGE_SLOW = 4.0
OPTION_LEVERAGE_FAST = 8.0
COST_OPTION_SLOW_PCT = 8.0
COST_OPTION_FAST_PCT = 6.0
FAST_REGIME_ATR_MULT = 0.5    # MFE must cross this fraction of ATR
FAST_REGIME_BARS     = 30     # …within this many forward 1-min bars

# Marginal label band — within ±this is label=0 — design §14 Q16
MARGINAL_BAND_PCT = 2.0      # post-cost pp

# Confidence band cuts — design §14 Q7
CONF_LOW_HI = 30.0
CONF_MED_HI = 60.0


# ──────────────────────────────────────────────────────────────────────────────
# Outcome record
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Outcome:
    # identity
    source: str
    kind: str
    side: str
    ticker: str
    detected_utc: str
    detected_epoch: float
    date_str: str
    entry_price: float

    # context features (the ML feature vector)
    confidence: Optional[float] = None
    rvol: Optional[float] = None
    day_pct: Optional[float] = None
    coil_range_pct: Optional[float] = None
    dist_above_lod_pct: Optional[float] = None
    time_of_day_bucket: str = "?"   # "HH:30" UTC bucket
    day_of_week: str = "?"

    # confidence filter band — design §14 Q7
    conf_band: str = "unknown"      # low | med | high | unknown

    # macro overlay (stored, not surfaced v1) — design §14 Q17
    vix_bucket: Optional[str] = None
    spx_regime: Optional[str] = None
    near_miss_eligible: bool = False

    # ATR(14) at signal time — design §8h
    atr_14: Optional[float] = None
    atr_14_pct: Optional[float] = None

    # bar coverage
    n_bars_after: int = 0
    minutes_to_eod: Optional[float] = None

    # horizon returns (signed, %)
    ret_5m_pct: Optional[float] = None
    ret_15m_pct: Optional[float] = None
    ret_30m_pct: Optional[float] = None
    ret_60m_pct: Optional[float] = None
    ret_eod_pct: Optional[float] = None

    # MFE / MAE (signed for side)
    mfe_pct: float = 0.0
    mae_pct: float = 0.0
    bars_to_mfe: int = 0
    bars_to_mae: int = 0

    # ── ATR-primary ladder, both passes ──
    atr_first_event_stop_first: Optional[str] = None     # TARGET_T1|T2|T3|STOP|EOD
    atr_first_event_target_first: Optional[str] = None
    atr_pnl_stop_first_pct: Optional[float] = None
    atr_pnl_target_first_pct: Optional[float] = None
    bars_to_atr_first_event_stop: Optional[int] = None
    bars_to_atr_first_event_target: Optional[int] = None

    # ── Pct-ladder secondary, both passes ──
    pct_first_event_stop_first: Optional[str] = None
    pct_first_event_target_first: Optional[str] = None
    pct_pnl_stop_first_pct: Optional[float] = None
    pct_pnl_target_first_pct: Optional[float] = None
    bars_to_pct_first_event_stop: Optional[int] = None
    bars_to_pct_first_event_target: Optional[int] = None

    # ── Cost-aware labels ──
    label_underlying: int = 0      # +1 / 0 / -1
    label_option_5p5x: int = 0     # +1 / 0 / -1 (legacy linear 5.5× — kept for ml_readiness/back-compat)
    label_option_2regime: int = 0  # +1 / 0 / -1 (regime-aware option label, the one to read)

    # ── Realized EV per fire (the headline number) ──
    ev_underlying_stop_first: Optional[float] = None
    ev_underlying_target_first: Optional[float] = None
    ev_option_5p5x_stop_first: Optional[float] = None
    ev_option_5p5x_target_first: Optional[float] = None
    ev_option_2regime_stop_first: Optional[float] = None
    ev_option_2regime_target_first: Optional[float] = None

    # ── Two-regime detector outputs ──
    regime_used: str = "slow"                      # "slow" | "fast"
    bars_to_half_atr: Optional[int] = None         # bars before MFE crossed 0.5×ATR
    regime_leverage_used: Optional[float] = None   # 4.0 or 8.0
    regime_cost_used: Optional[float] = None       # 8.0 or 6.0

    # ── Fire-time enrichment from lifecycle snapshot — design §15 ──
    # Joined from engine_v4 lifecycle/<TICKER>_*.json closest to detected_epoch.
    at_fire_has_whale_flow: Optional[bool] = None
    at_fire_call_share: Optional[float] = None
    at_fire_net_call_skew_m: Optional[float] = None
    at_fire_sweep_n: Optional[int] = None
    at_fire_whale_n_alerts: Optional[int] = None
    at_fire_has_news: Optional[bool] = None
    at_fire_news_count: Optional[int] = None
    at_fire_has_conviction: Optional[bool] = None
    at_fire_rvol: Optional[float] = None
    at_fire_day_pct: Optional[float] = None
    at_fire_mode: Optional[str] = None
    at_fire_snapshot_lag_sec: Optional[float] = None  # how close snap was to fire


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────


def _signed_pct(side: str, entry: float, price: float) -> float:
    if entry <= 0:
        return 0.0
    raw = (price - entry) / entry * 100.0
    return raw if side == "CALL" else -raw


def _fav_extreme(side: str, bar: Bar) -> float:
    """Bar extreme that's favorable for our side."""
    return bar.h if side == "CALL" else bar.l


def _adv_extreme(side: str, bar: Bar) -> float:
    """Bar extreme that's adverse for our side."""
    return bar.l if side == "CALL" else bar.h


def _atr_14(bars_before: list[Bar]) -> Optional[float]:
    """
    True ATR(14) computed on the LAST 14 bars BEFORE entry.
    TR_i = max(H_i - L_i, |H_i - C_{i-1}|, |L_i - C_{i-1}|).
    """
    if len(bars_before) < 14:
        return None
    last14 = bars_before[-14:]
    # need previous close for first TR; if we have it, use it
    if len(bars_before) >= 15:
        prev_close = bars_before[-15].c
    else:
        prev_close = last14[0].c  # fall back to first bar's open as approximate
    trs = []
    for b in last14:
        tr = max(
            b.h - b.l,
            abs(b.h - prev_close),
            abs(b.l - prev_close),
        )
        trs.append(tr)
        prev_close = b.c
    return sum(trs) / len(trs)


def _conf_band(confidence: Optional[float]) -> str:
    if confidence is None:
        return "unknown"
    if confidence < CONF_LOW_HI:
        return "low"
    if confidence < CONF_MED_HI:
        return "med"
    return "high"


def _tod_bucket(epoch: float) -> str:
    """30-min UTC bucket, e.g. '17:00' or '17:30'."""
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    half = "30" if dt.minute >= 30 else "00"
    return f"{dt.hour:02d}:{half}"


def _dow(epoch: float) -> str:
    dt = datetime.fromtimestamp(epoch, tz=timezone.utc)
    return ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"][dt.weekday()]


# ──────────────────────────────────────────────────────────────────────────────
# Ladder pass — generic over (target_levels, stop_level) in pct
# ──────────────────────────────────────────────────────────────────────────────


def _ladder_pass(
    forward: list[Bar],
    side: str,
    entry: float,
    target_levels: tuple[float, float, float],   # in pct (signed positive favorable)
    stop_level: float,                            # in pct (positive number, will be negated)
    stop_first_on_wide: bool,
) -> tuple[Optional[str], Optional[float], Optional[int]]:
    """
    Walk forward bars. For each bar, evaluate adverse extreme and favorable extreme.
    If both are touched in the same bar:
      - stop_first_on_wide=True   → STOP fires (conservative)
      - stop_first_on_wide=False  → highest target fires (optimistic)

    Returns (event_label, pnl_pct, bars_to_event).
    Event labels: TARGET_T1, TARGET_T2, TARGET_T3, STOP, EOD
    """
    t1, t2, t3 = target_levels
    stop = -abs(stop_level)

    for i, b in enumerate(forward):
        fav_pct = _signed_pct(side, entry, _fav_extreme(side, b))
        adv_pct = _signed_pct(side, entry, _adv_extreme(side, b))

        hit_stop = adv_pct <= stop
        hit_t3 = fav_pct >= t3
        hit_t2 = fav_pct >= t2
        hit_t1 = fav_pct >= t1

        if not hit_stop and not (hit_t1 or hit_t2 or hit_t3):
            continue

        # both sides touched this bar → wide-bar
        wide = hit_stop and (hit_t1 or hit_t2 or hit_t3)
        if wide:
            if stop_first_on_wide:
                return "STOP", stop, i
            else:
                # optimistic: highest target available
                if hit_t3:
                    return "TARGET_T3", t3, i
                if hit_t2:
                    return "TARGET_T2", t2, i
                return "TARGET_T1", t1, i

        if hit_stop:
            return "STOP", stop, i
        # only targets touched
        if hit_t3:
            return "TARGET_T3", t3, i
        if hit_t2:
            return "TARGET_T2", t2, i
        if hit_t1:
            return "TARGET_T1", t1, i

    # nothing hit — settle at EOD (last bar close)
    if forward:
        last_pct = _signed_pct(side, entry, forward[-1].c)
        return "EOD", last_pct, len(forward) - 1
    return None, None, None


# ──────────────────────────────────────────────────────────────────────────────
# Cost-aware label
# ──────────────────────────────────────────────────────────────────────────────


def _label_from_pnl(event_label: Optional[str], pnl_pct: Optional[float], leverage: float, flat_cost: float) -> int:
    if event_label is None or pnl_pct is None:
        return 0
    net = pnl_pct * leverage - flat_cost
    is_target_hit = event_label.startswith("TARGET_")
    if is_target_hit and net > 0:
        return 1
    if abs(net) <= MARGINAL_BAND_PCT:
        return 0
    return -1


# ──────────────────────────────────────────────────────────────────────────────
# Two-regime detector
# ──────────────────────────────────────────────────────────────────────────────


def _detect_regime(after: list[Bar], side: str, entry_price: float,
                   atr_pct: Optional[float]
                   ) -> tuple[str, Optional[int], float, float]:
    """
    Walk forward bars; return (regime, bars_to_half_atr, leverage, cost_pct).

    "fast" if the favourable extreme reaches FAST_REGIME_ATR_MULT × ATR within
    FAST_REGIME_BARS minutes — those are the moves where premium spikes via gamma
    and we can lift before spread widens. Otherwise "slow" — theta + spread bleed
    dominate, leverage is realistically lower and cost is the full RT.

    If ATR is unavailable, fall back to a 0.4% absolute threshold (typical 1m ATR
    of a liquid intraday name).
    """
    if not after:
        return ("slow", None, OPTION_LEVERAGE_SLOW, COST_OPTION_SLOW_PCT)
    threshold_pct = (atr_pct * FAST_REGIME_ATR_MULT) if (atr_pct and atr_pct > 0) else 0.4
    cap = min(len(after), FAST_REGIME_BARS)
    for i in range(cap):
        b = after[i]
        fav = _signed_pct(side, entry_price, _fav_extreme(side, b))
        if fav >= threshold_pct:
            return ("fast", i, OPTION_LEVERAGE_FAST, COST_OPTION_FAST_PCT)
    return ("slow", None, OPTION_LEVERAGE_SLOW, COST_OPTION_SLOW_PCT)


# ──────────────────────────────────────────────────────────────────────────────
# Main: per-signal outcome
# ──────────────────────────────────────────────────────────────────────────────


def compute_outcome(sig: Signal, ticker_bars: list[Bar]) -> Outcome:
    out = Outcome(
        source=sig.source,
        kind=sig.kind,
        side=sig.side,
        ticker=sig.ticker,
        detected_utc=sig.detected_utc,
        detected_epoch=sig.detected_epoch,
        date_str=sig.date_str,
        entry_price=sig.entry_price,
        confidence=sig.confidence,
        rvol=sig.rvol,
        day_pct=sig.day_pct,
        coil_range_pct=sig.coil_range_pct,
        dist_above_lod_pct=sig.dist_above_lod_pct,
        time_of_day_bucket=_tod_bucket(sig.detected_epoch),
        day_of_week=_dow(sig.detected_epoch),
        conf_band=_conf_band(sig.confidence),
    )

    if not ticker_bars or sig.entry_price <= 0:
        return out

    # split bars into BEFORE entry (for ATR) and AFTER (for outcome walk)
    # entry bar boundary: bars whose epoch >= detected_epoch - 30s slop
    before: list[Bar] = []
    after: list[Bar] = []
    for b in ticker_bars:
        if b.epoch < sig.detected_epoch - 30:
            before.append(b)
        else:
            after.append(b)

    # ATR(14)
    atr = _atr_14(before)
    if atr is not None:
        out.atr_14 = atr
        out.atr_14_pct = (atr / sig.entry_price) * 100.0

    if not after:
        return out

    out.n_bars_after = len(after)
    out.minutes_to_eod = round((after[-1].epoch - sig.detected_epoch) / 60.0, 1)

    # horizon returns — close at +N minutes (index-based since 1-min bars)
    def _close_at(n: int) -> Optional[float]:
        return after[n].c if n < len(after) else None

    c5, c15, c30, c60 = _close_at(5), _close_at(15), _close_at(30), _close_at(60)
    if c5  is not None: out.ret_5m_pct  = _signed_pct(sig.side, sig.entry_price, c5)
    if c15 is not None: out.ret_15m_pct = _signed_pct(sig.side, sig.entry_price, c15)
    if c30 is not None: out.ret_30m_pct = _signed_pct(sig.side, sig.entry_price, c30)
    if c60 is not None: out.ret_60m_pct = _signed_pct(sig.side, sig.entry_price, c60)
    out.ret_eod_pct = _signed_pct(sig.side, sig.entry_price, after[-1].c)

    # MFE / MAE (over full forward window)
    mfe = mae = 0.0
    bars_to_mfe = bars_to_mae = 0
    for i, b in enumerate(after):
        f = _signed_pct(sig.side, sig.entry_price, _fav_extreme(sig.side, b))
        a = _signed_pct(sig.side, sig.entry_price, _adv_extreme(sig.side, b))
        if f > mfe:
            mfe = f; bars_to_mfe = i
        if a < mae:
            mae = a; bars_to_mae = i
    out.mfe_pct = mfe
    out.mae_pct = mae
    out.bars_to_mfe = bars_to_mfe
    out.bars_to_mae = bars_to_mae

    # ── ATR ladder (primary) — both passes ──
    if atr is not None and atr > 0:
        atr_pct = (atr / sig.entry_price) * 100.0
        atr_targets_pct = tuple(m * atr_pct for m in ATR_TARGETS)  # type: ignore[assignment]
        atr_stop_pct = ATR_STOP * atr_pct

        ev, p, n = _ladder_pass(after, sig.side, sig.entry_price,
                                 atr_targets_pct, atr_stop_pct, stop_first_on_wide=True)
        out.atr_first_event_stop_first = ev
        out.atr_pnl_stop_first_pct = p
        out.bars_to_atr_first_event_stop = n

        ev, p, n = _ladder_pass(after, sig.side, sig.entry_price,
                                 atr_targets_pct, atr_stop_pct, stop_first_on_wide=False)
        out.atr_first_event_target_first = ev
        out.atr_pnl_target_first_pct = p
        out.bars_to_atr_first_event_target = n

    # ── Pct ladder (secondary) — both passes ──
    ev, p, n = _ladder_pass(after, sig.side, sig.entry_price,
                             PCT_TARGETS, PCT_STOP, stop_first_on_wide=True)
    out.pct_first_event_stop_first = ev
    out.pct_pnl_stop_first_pct = p
    out.bars_to_pct_first_event_stop = n

    ev, p, n = _ladder_pass(after, sig.side, sig.entry_price,
                             PCT_TARGETS, PCT_STOP, stop_first_on_wide=False)
    out.pct_first_event_target_first = ev
    out.pct_pnl_target_first_pct = p
    out.bars_to_pct_first_event_target = n

    # ── Cost-aware labels — use the ATR pass if present, else pct ──
    if out.atr_first_event_stop_first is not None:
        ev_label = out.atr_first_event_stop_first
        ev_pnl   = out.atr_pnl_stop_first_pct
    else:
        ev_label = out.pct_first_event_stop_first
        ev_pnl   = out.pct_pnl_stop_first_pct

    out.label_underlying  = _label_from_pnl(ev_label, ev_pnl, leverage=1.0, flat_cost=FLAT_COST_UNDERLYING)
    out.label_option_5p5x = _label_from_pnl(ev_label, ev_pnl, leverage=OPTION_LEVERAGE, flat_cost=FLAT_COST_OPTION_5P5X)

    # ── Two-regime option label & EV ──
    regime, bars_to_half, lev_used, cost_used = _detect_regime(
        after, sig.side, sig.entry_price, out.atr_14_pct
    )
    out.regime_used = regime
    out.bars_to_half_atr = bars_to_half
    out.regime_leverage_used = lev_used
    out.regime_cost_used = cost_used
    out.label_option_2regime = _label_from_pnl(ev_label, ev_pnl, leverage=lev_used, flat_cost=cost_used)

    # ── Realized EV per fire ──
    # Underlying — leverage 1, cost 0
    if out.atr_pnl_stop_first_pct is not None:
        out.ev_underlying_stop_first = out.atr_pnl_stop_first_pct
        out.ev_option_5p5x_stop_first = out.atr_pnl_stop_first_pct * OPTION_LEVERAGE - FLAT_COST_OPTION_5P5X
        out.ev_option_2regime_stop_first = out.atr_pnl_stop_first_pct * lev_used - cost_used
    elif out.pct_pnl_stop_first_pct is not None:
        out.ev_underlying_stop_first = out.pct_pnl_stop_first_pct
        out.ev_option_5p5x_stop_first = out.pct_pnl_stop_first_pct * OPTION_LEVERAGE - FLAT_COST_OPTION_5P5X
        out.ev_option_2regime_stop_first = out.pct_pnl_stop_first_pct * lev_used - cost_used

    if out.atr_pnl_target_first_pct is not None:
        out.ev_underlying_target_first = out.atr_pnl_target_first_pct
        out.ev_option_5p5x_target_first = out.atr_pnl_target_first_pct * OPTION_LEVERAGE - FLAT_COST_OPTION_5P5X
        out.ev_option_2regime_target_first = out.atr_pnl_target_first_pct * lev_used - cost_used
    elif out.pct_pnl_target_first_pct is not None:
        out.ev_underlying_target_first = out.pct_pnl_target_first_pct
        out.ev_option_5p5x_target_first = out.pct_pnl_target_first_pct * OPTION_LEVERAGE - FLAT_COST_OPTION_5P5X
        out.ev_option_2regime_target_first = out.pct_pnl_target_first_pct * lev_used - cost_used

    return out


def compute_all(signals: list[Signal], bars: dict[tuple[str, str], list[Bar]]) -> list[Outcome]:
    outcomes: list[Outcome] = []
    for s in signals:
        ticker_bars = bars.get((s.date_str, s.ticker), [])
        outcomes.append(compute_outcome(s, ticker_bars))
    return outcomes


# ──────────────────────────────────────────────────────────────────────────────
# Dedup — design §14 Q6 default ON
# ──────────────────────────────────────────────────────────────────────────────


def dedup_signals(signals: list[Signal]) -> list[Signal]:
    """
    Keep first fire per (date, ticker, kind, side) tuple.
    Per design §14 Q6: dedup default ON.
    """
    seen = set()
    keep: list[Signal] = []
    for s in signals:
        key = (s.date_str, s.ticker, s.kind, s.side)
        if key in seen:
            continue
        seen.add(key)
        keep.append(s)
    return keep


# ──────────────────────────────────────────────────────────────────────────────
# CLI sanity
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    import statistics
    from loader_scalp import load_all

    sigs, bars = load_all()
    print(f"raw signals: {len(sigs)}")

    sigs_dedup = dedup_signals(sigs)
    print(f"deduped (1 per ticker/session/kind): {len(sigs_dedup)}")

    outs = compute_all(sigs_dedup, bars)
    print(f"computed {len(outs)} outcomes\n")

    # NVDA spot check
    nvda = [o for o in outs if o.ticker == "NVDA" and o.kind == "CALL_MORNING"]
    if nvda:
        o = nvda[0]
        atr14_pct_str = f"{o.atr_14_pct:.3f}" if o.atr_14_pct is not None else "—"
        atr14_str = f"{o.atr_14:.3f}" if o.atr_14 is not None else "—"
        mfe_str = f"{o.mfe_pct:.2f}%" if o.mfe_pct is not None else "—"
        mae_str = f"{o.mae_pct:.2f}%" if o.mae_pct is not None else "—"
        print(f"NVDA CALL_MORNING @ {o.detected_utc}")
        print(f"  entry={o.entry_price}  atr14={atr14_str}  atr14%={atr14_pct_str}")
        print(f"  conf={o.confidence}  band={o.conf_band}")
        print(f"  ret 5m={o.ret_5m_pct} 15m={o.ret_15m_pct} 30m={o.ret_30m_pct}")
        print(f"  MFE={mfe_str}  MAE={mae_str}")
        print(f"  ATR stop-first: {o.atr_first_event_stop_first} @ {o.atr_pnl_stop_first_pct}")
        print(f"  ATR tgt-first : {o.atr_first_event_target_first} @ {o.atr_pnl_target_first_pct}")
        print(f"  Pct stop-first: {o.pct_first_event_stop_first} @ {o.pct_pnl_stop_first_pct}")
        print(f"  label_u={o.label_underlying} label_opt={o.label_option_5p5x}")
        print(f"  EV opt5.5× stop-first: {o.ev_option_5p5x_stop_first}\n")

    # Headline kind table — dedup view
    by_kind = {}
    for o in outs:
        by_kind.setdefault(o.kind, []).append(o)

    print(f"\n{'KIND':22s} {'n':>4s} {'win%':>6s} {'EV/fire opt5.5×':>16s} {'STOP/TGT range':>22s}")
    print("─" * 90)
    for kind in sorted(by_kind, key=lambda k: -len(by_kind[k])):
        rows = by_kind[kind]
        n = len(rows)
        wins = sum(1 for r in rows if r.label_option_5p5x == 1)
        evs_stop = [r.ev_option_5p5x_stop_first for r in rows if r.ev_option_5p5x_stop_first is not None]
        evs_tgt  = [r.ev_option_5p5x_target_first for r in rows if r.ev_option_5p5x_target_first is not None]
        ev_stop = statistics.mean(evs_stop) if evs_stop else None
        ev_tgt  = statistics.mean(evs_tgt)  if evs_tgt  else None
        win_pct = wins / n * 100.0 if n else 0.0
        ev_str  = f"{ev_stop:+.2f}%" if ev_stop is not None else "—"
        rng = f"{ev_stop:+.2f} → {ev_tgt:+.2f}" if (ev_stop is not None and ev_tgt is not None) else "—"
        print(f"{kind:22s} {n:>4d} {win_pct:>5.1f}% {ev_str:>16s} {rng:>22s}")
