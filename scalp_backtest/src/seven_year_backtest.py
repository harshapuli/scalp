"""
seven_year_backtest.py — replay the engine_scalp 4,984 archived signals
against locally cached minute bars (engine_scalp/_bar_cache/<TICKER>_1m.json).

For each signal we:
  - find the entry bar (closest to entry_t)
  - compute ATR(14) on the prior 14 1m bars
  - walk forward up to FORWARD_CAP_MIN minutes
  - apply ATR-primary ladder (stop-first, conservative)
  - fall back to a fixed pct ladder if ATR is unavailable
  - re-derive cost-aware EV at 1× and 5.5× leverage (8% RT cost on options)

Output: data/seven_year_backtest/run_<DATE>.json with:
  meta: { generated_utc, n_signals, n_resimmed, n_bar_misses, ... }
  by_year, by_strategy, by_ticker, by_direction
  per_signal: trimmed list (date, ticker, side, archived_pnl, resim_pnl, drift)

READ-ONLY on engine_scalp/_bar_cache and on engine_scalp/scalp_all_signals.json.
"""
from __future__ import annotations

import json
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from loader_historical import HistoricalSignal, load_historical
from outcome_intraday import (
    _atr_14,
    _signed_pct,
    _fav_extreme,
    _detect_regime,
    _ladder_pass,
    ATR_TARGETS,
    ATR_STOP,
    OPTION_LEVERAGE,
    FLAT_COST_OPTION_5P5X,
)
from loader_scalp import Bar
from stats import wilson_ci


# ──────────────────────────────────────────────────────────────────────────────
# Option-premium-based exit rules (matches engine_scalp behavior)
#   - HARD_SL: option premium drops -20% from entry → stop
#   - TRAIL:   option premium MFE >= +20%, then retraces -20% from peak
#   - TARGET:  PROFIT_50PCT — option premium hits +50%
#   - EOD:     close at session end
# ──────────────────────────────────────────────────────────────────────────────

OPT_STOP_PCT       = -20.0   # HARD_SL
OPT_TRAIL_TRIGGER  = +20.0   # arm trail after MFE crosses this
OPT_TRAIL_GIVEBACK = -20.0   # exit when MFE - current >= this (i.e. peak drops 20pp)
OPT_PROFIT_TARGET  = +50.0   # PROFIT_50PCT


def _premium_ladder_pass(forward: list[Bar], side: str, entry_und_price: float,
                         leverage: float = OPTION_LEVERAGE
                         ) -> tuple[Optional[str], Optional[float], Optional[int]]:
    """
    Walk forward bars, simulate option premium % via leverage * signed underlying %.
    Apply HARD_SL / TRAIL / PROFIT exits in first-touch order:
      - within a bar: STOP triggers first if both stop and trail/target touched (conservative)
    Returns (event_label, option_pnl_pct, bars_to_event).
    """
    peak_opt = 0.0
    armed_trail = False

    for i, b in enumerate(forward):
        # favorable / adverse extremes in option-% space
        und_fav = _signed_pct(side, entry_und_price, b.h if side == "CALL" else b.l)
        und_adv = _signed_pct(side, entry_und_price, b.l if side == "CALL" else b.h)
        opt_fav = und_fav * leverage     # MFE this bar (option %)
        opt_adv = und_adv * leverage     # MAE this bar (option %)

        # 1) stop check — if MAE <= -20%, stop fires (conservative on wide bars)
        if opt_adv <= OPT_STOP_PCT:
            return "HARD_SL", OPT_STOP_PCT, i

        # 2) profit target — if MFE >= +50%, take profit
        if opt_fav >= OPT_PROFIT_TARGET:
            return "PROFIT_50PCT", OPT_PROFIT_TARGET, i

        # 3) update peak
        if opt_fav > peak_opt:
            peak_opt = opt_fav
        if peak_opt >= OPT_TRAIL_TRIGGER:
            armed_trail = True

        # 4) trail check — if MAE this bar dropped 20pp below peak, exit at peak-20
        if armed_trail:
            trail_exit = peak_opt + OPT_TRAIL_GIVEBACK   # e.g. peak=40%, trail_exit=20%
            if opt_adv <= trail_exit:
                return "TRAIL_HIT", max(trail_exit, OPT_STOP_PCT), i

    # EOD — close at last bar's close
    if forward:
        last_pct = _signed_pct(side, entry_und_price, forward[-1].c) * leverage
        return "EOD", last_pct, len(forward) - 1
    return None, None, None


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BAR_CACHE_ROOT = Path("/Users/harshapuli/Downloads/spy QQQ/claude/gemini /engine_scalp/_bar_cache")
OUT_DIR = PROJECT_ROOT / "data" / "seven_year_backtest"

# walk forward at most this many bars looking for target/stop
FORWARD_CAP_MIN = 240          # 4 hours — most scalps resolve in <60 min
PRIOR_BARS_FOR_ATR = 30        # use last 30 prior 1m bars (need 14 for ATR + slop)


# ──────────────────────────────────────────────────────────────────────────────
# Bar loading — read each <TICKER>_1m.json once, sort by epoch, cache in memory
# ──────────────────────────────────────────────────────────────────────────────


_BAR_CACHE: dict[str, list[Bar]] = {}


def load_minute_bars(ticker: str) -> list[Bar]:
    """Load and cache the full 1m bar series for a ticker."""
    if ticker in _BAR_CACHE:
        return _BAR_CACHE[ticker]
    p = BAR_CACHE_ROOT / f"{ticker}_1m.json"
    if not p.exists():
        _BAR_CACHE[ticker] = []
        return []
    try:
        rows = json.loads(p.read_text())
    except Exception:
        _BAR_CACHE[ticker] = []
        return []
    bars: list[Bar] = []
    for r in rows:
        t = r.get("t")
        if not t:
            continue
        try:
            ep = datetime.fromisoformat(t.replace("Z", "+00:00")).timestamp()
        except Exception:
            continue
        bars.append(Bar(
            t=t, epoch=ep,
            o=float(r.get("o") or 0.0),
            h=float(r.get("h") or 0.0),
            l=float(r.get("l") or 0.0),
            c=float(r.get("c") or 0.0),
            v=int(r.get("v") or 0),
            vw=float(r.get("vw") or 0.0),
        ))
    bars.sort(key=lambda b: b.epoch)
    _BAR_CACHE[ticker] = bars
    return bars


def slice_around_entry(bars: list[Bar],
                       entry_epoch: float,
                       prior_n: int = PRIOR_BARS_FOR_ATR,
                       forward_cap: int = FORWARD_CAP_MIN
                       ) -> tuple[list[Bar], list[Bar], Optional[Bar]]:
    """
    Returns (prior_bars, forward_bars, entry_bar).
    entry_bar is the first bar whose epoch >= entry_epoch - 30s slop.
    prior_bars are the last `prior_n` bars before entry_bar.
    forward_bars are the next `forward_cap` bars after entry_bar.
    """
    if not bars:
        return [], [], None

    # binary search for entry index
    import bisect
    epochs = [b.epoch for b in bars]
    idx = bisect.bisect_left(epochs, entry_epoch - 30)
    if idx >= len(bars):
        return [], [], None
    entry_bar = bars[idx]
    # prior = bars before entry (not including entry_bar)
    prior_lo = max(0, idx - prior_n)
    prior = bars[prior_lo:idx]
    # forward = entry_bar + next N
    fwd_hi = min(len(bars), idx + forward_cap)
    # filter forward to same calendar date as entry (avoid spilling into next session)
    entry_date = datetime.fromtimestamp(entry_bar.epoch, tz=timezone.utc).strftime("%Y-%m-%d")
    forward = [b for b in bars[idx:fwd_hi]
               if datetime.fromtimestamp(b.epoch, tz=timezone.utc).strftime("%Y-%m-%d") == entry_date]
    return prior, forward, entry_bar


# ──────────────────────────────────────────────────────────────────────────────
# Per-signal resimulation
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class ResimResult:
    # identity (mirrors HistoricalSignal headers)
    strategy_id: str
    strategy_label: str
    category: str
    ticker: str
    direction: str
    date_str: str
    entry_t: Optional[str]

    # archive-side
    archived_outcome: str
    archived_pnl_pct: Optional[float]
    archived_exit_reason: Optional[str]

    # resim-side
    has_bars: bool = False
    n_prior_bars: int = 0
    n_forward_bars: int = 0
    entry_price: Optional[float] = None
    atr_14: Optional[float] = None
    atr_14_pct: Optional[float] = None

    # ladder result (ATR if available, else pct)
    ladder_used: Optional[str] = None       # "ATR" | "PCT" | None
    event_label: Optional[str] = None       # TARGET_T1/T2/T3 | STOP | EOD
    underlying_pnl_pct: Optional[float] = None     # signed for side
    option_pnl_pct: Optional[float] = None         # 5.5× cost-aware
    bars_to_event: Optional[int] = None
    is_resim_win: Optional[bool] = None     # event_label.startswith("TARGET_")

    # archive-vs-resim drift on the underlying %
    archived_underlying_pnl_pct: Optional[float] = None  # back-derived from entry/exit_px
    drift_underlying_pp: Optional[float] = None          # resim - archived

    # ── Regime sanity test (multi-year) ──────────────────────────────────────
    # Apply the same fast/slow classifier from outcome_intraday._detect_regime
    # to the archive resim. P(T1+ | fast) − P(T1+ | slow) on multi-year data
    # tests whether today's +58.5pp single-day spread holds up cross-regime.
    regime: Optional[str] = None                       # "fast" | "slow"
    bars_to_half_atr: Optional[int] = None             # bar idx where MFE crossed 0.5×ATR
    atr_event_label: Optional[str] = None              # underlying-only ATR ladder result
    atr_t1_plus_hit: Optional[bool] = None             # True iff atr_event_label.startswith("TARGET_")
    atr_bars_to_event: Optional[int] = None            # bars to STOP/T1/T2/T3/EOD on underlying ladder
    mfe_pct: Optional[float] = None                    # signed-favorable max extreme over forward window
    n_forward_bars_atr: Optional[int] = None           # bars actually walked (sanity for cap truncation)

    # ── Pre-trade features (for slow-regime predictor) ───────────────────────
    # All computed from prior_bars or entry_bar — NO information from forward
    # bars. These are the features the pre-trade classifier consumes.
    minute_of_day: Optional[int] = None                # minutes since 09:30 ET
    prior_5bar_return_pct: Optional[float] = None      # signed return on prior 5 1m bars (close-to-close)
    prior_30bar_realized_vol_pct: Optional[float] = None  # stdev of prior 30 1-min returns
    prior_5bar_alignment: Optional[float] = None       # signed prior_5bar return × signal_side (+ = with-trend)
    prior_5bar_volume_z: Optional[float] = None        # last 5 bars vol / 30-bar mean vol

    # bookkeeping
    skip_reason: Optional[str] = None


def resimulate_signal(sig: HistoricalSignal, bars: list[Bar]) -> ResimResult:
    side = (sig.direction or "CALL").upper()
    if side not in ("CALL", "PUT"):
        # FLAT or unknown → no directional resim possible, skip
        return ResimResult(
            strategy_id=sig.strategy_id, strategy_label=sig.strategy_label,
            category=sig.category, ticker=sig.ticker or "—",
            direction=sig.direction or "—", date_str=sig.date_str,
            entry_t=sig.entry_t,
            archived_outcome=sig.outcome,
            archived_pnl_pct=sig.pnl_pct,
            archived_exit_reason=sig.exit_reason,
            skip_reason="non-directional signal",
        )

    if sig.entry_epoch is None:
        return ResimResult(
            strategy_id=sig.strategy_id, strategy_label=sig.strategy_label,
            category=sig.category, ticker=sig.ticker or "—",
            direction=side, date_str=sig.date_str, entry_t=sig.entry_t,
            archived_outcome=sig.outcome,
            archived_pnl_pct=sig.pnl_pct,
            archived_exit_reason=sig.exit_reason,
            skip_reason="no entry_epoch",
        )

    if not bars:
        return ResimResult(
            strategy_id=sig.strategy_id, strategy_label=sig.strategy_label,
            category=sig.category, ticker=sig.ticker or "—",
            direction=side, date_str=sig.date_str, entry_t=sig.entry_t,
            archived_outcome=sig.outcome,
            archived_pnl_pct=sig.pnl_pct,
            archived_exit_reason=sig.exit_reason,
            skip_reason="no bars",
        )

    prior, forward, entry_bar = slice_around_entry(bars, sig.entry_epoch)
    if not entry_bar:
        return ResimResult(
            strategy_id=sig.strategy_id, strategy_label=sig.strategy_label,
            category=sig.category, ticker=sig.ticker or "—",
            direction=side, date_str=sig.date_str, entry_t=sig.entry_t,
            archived_outcome=sig.outcome,
            archived_pnl_pct=sig.pnl_pct,
            archived_exit_reason=sig.exit_reason,
            skip_reason="no entry bar",
        )

    # ALWAYS use entry_bar.o as the underlying entry — sig.entry_px is option
    # premium for the *_real_options strategies (e.g. SMCI $8.50 vs $329 actual).
    # We re-run the ladder against underlying bars, so we need the underlying entry.
    # Sanity: if sig.entry_px is within 5% of entry_bar.o it's the underlying fill;
    # we still prefer entry_bar.o for consistency.
    entry_price = entry_bar.o
    archived_entry_was_underlying = bool(
        sig.entry_px and entry_bar.o > 0
        and 0.95 <= (sig.entry_px / entry_bar.o) <= 1.05
    )
    if entry_price <= 0:
        return ResimResult(
            strategy_id=sig.strategy_id, strategy_label=sig.strategy_label,
            category=sig.category, ticker=sig.ticker or "—",
            direction=side, date_str=sig.date_str, entry_t=sig.entry_t,
            archived_outcome=sig.outcome,
            archived_pnl_pct=sig.pnl_pct,
            archived_exit_reason=sig.exit_reason,
            skip_reason="no entry price",
        )

    atr = _atr_14(prior)
    atr_pct = (atr / entry_price * 100.0) if (atr and entry_price > 0) else None

    # ── Premium-based ladder (matches engine_scalp logic) ──
    # event_label: HARD_SL | TRAIL_HIT | PROFIT_50PCT | EOD
    # opt_pnl: option premium %, signed
    event = opt_pnl = nbars = None
    ladder_used = None
    if forward:
        event, opt_pnl, nbars = _premium_ladder_pass(forward, side, entry_price)
        ladder_used = "PREMIUM"

    # back out the underlying % move at the exit (for drift comparison)
    underlying_pnl = (opt_pnl / OPTION_LEVERAGE) if opt_pnl is not None else None
    # apply the 8% RT cost to give the cost-aware option EV
    option_pnl_costed = (opt_pnl - FLAT_COST_OPTION_5P5X) if opt_pnl is not None else None

    # archived underlying %, back-derived — only valid when the archived entry
    # was actually an underlying price (not an option premium).
    archived_und = None
    if archived_entry_was_underlying and sig.entry_px and sig.exit_px and sig.entry_px > 0:
        raw = (sig.exit_px - sig.entry_px) / sig.entry_px * 100.0
        archived_und = raw if side == "CALL" else -raw

    drift = None
    if underlying_pnl is not None and archived_und is not None:
        drift = underlying_pnl - archived_und

    # win = positive option pnl (after costs)
    is_win = None
    if option_pnl_costed is not None:
        is_win = option_pnl_costed > 0.0

    # ── Regime sanity (multi-year) ──────────────────────────────────────────
    # Apply identical fast/slow classifier used on today's intraday data.
    # Then run an underlying-only ATR ladder (T1=0.75×ATR, T2=1.5×, T3=2.25×,
    # stop=1.0×) to compute T1+ hit independently of the option premium model.
    # This isolates "did the underlying move enough to hit T1" from "did the
    # option premium model report a win" — the latter mixes regime with cost
    # assumptions, the former is pure price-structure.
    regime_label = None
    bars_to_half = None
    atr_event = None
    atr_t1_plus = None
    atr_bars_to_event = None
    mfe_pct = None
    n_forward_bars_atr = None
    if forward and entry_price > 0:
        regime_label, bars_to_half, _lev, _cost = _detect_regime(
            forward, side, entry_price, atr_pct
        )
        # MFE over the entire forward window (not just first 30 bars)
        mfe_running = 0.0
        for b in forward:
            fav = _signed_pct(side, entry_price, _fav_extreme(side, b))
            if fav > mfe_running:
                mfe_running = fav
        mfe_pct = mfe_running
        n_forward_bars_atr = len(forward)
        if atr_pct is not None and atr_pct > 0:
            atr_targets_pct = tuple(m * atr_pct for m in ATR_TARGETS)
            atr_stop_pct = ATR_STOP * atr_pct
            atr_event, _atr_pnl, atr_bars_to_event = _ladder_pass(
                forward, side, entry_price,
                target_levels=atr_targets_pct,
                stop_level=atr_stop_pct,
                stop_first_on_wide=True,
            )
            atr_t1_plus = bool(atr_event and atr_event.startswith("TARGET_"))

    # ── Pre-trade features ──────────────────────────────────────────────────
    # All computed from prior bars and entry-bar timestamp — strict no-leakage
    # from forward bars. These feed the pre-trade slow-regime predictor.
    minute_of_day = None
    if entry_bar is not None:
        try:
            entry_dt = datetime.fromtimestamp(entry_bar.epoch, tz=timezone.utc)
            # Convert UTC → ET (rough offset; market is open 14:30-21:00 UTC during DST,
            # 14:30-21:00 standard. Either way, 09:30 ET = 14:30 UTC summer / 14:30 UTC winter).
            # For minute-of-day relative to NYSE open we just compute (utc_hour*60+min) - 870
            # where 870 = 14h*60+30 = 14:30 UTC. Negative values mean pre-market.
            md_utc = entry_dt.hour * 60 + entry_dt.minute
            minute_of_day = md_utc - 870
        except Exception:
            minute_of_day = None

    prior_5bar_return_pct = None
    prior_30bar_realized_vol_pct = None
    prior_5bar_alignment = None
    prior_5bar_volume_z = None
    if prior:
        # 5-bar return: close[-1] vs close[-6] in prior list (i.e. 5 bars ago)
        if len(prior) >= 6:
            c0 = prior[-6].c
            c1 = prior[-1].c
            if c0 > 0:
                prior_5bar_return_pct = (c1 - c0) / c0 * 100.0
                # Alignment: signed return × +1 if CALL, -1 if PUT
                # Positive means prior 5 bars moved WITH signal direction
                side_sign = 1.0 if side == "CALL" else -1.0
                prior_5bar_alignment = prior_5bar_return_pct * side_sign

        # 30-bar realized vol: stdev of consecutive 1m returns over prior 30 bars
        if len(prior) >= 5:
            tail = prior[-30:] if len(prior) >= 30 else prior
            rets = []
            for i in range(1, len(tail)):
                p0 = tail[i - 1].c
                p1 = tail[i].c
                if p0 > 0:
                    rets.append((p1 - p0) / p0 * 100.0)
            if len(rets) >= 3:
                try:
                    prior_30bar_realized_vol_pct = statistics.stdev(rets)
                except statistics.StatisticsError:
                    prior_30bar_realized_vol_pct = None

        # Volume z-score: last 5 bars mean vol / 30-bar mean vol
        if len(prior) >= 10:
            recent_vols = [b.v for b in prior[-5:] if b.v > 0]
            base_vols = [b.v for b in prior[-30:] if b.v > 0]
            if recent_vols and base_vols:
                base_mean = sum(base_vols) / len(base_vols)
                if base_mean > 0:
                    recent_mean = sum(recent_vols) / len(recent_vols)
                    prior_5bar_volume_z = recent_mean / base_mean

    return ResimResult(
        strategy_id=sig.strategy_id,
        strategy_label=sig.strategy_label,
        category=sig.category,
        ticker=sig.ticker or "—",
        direction=side,
        date_str=sig.date_str,
        entry_t=sig.entry_t,
        archived_outcome=sig.outcome,
        archived_pnl_pct=sig.pnl_pct,
        archived_exit_reason=sig.exit_reason,
        has_bars=True,
        n_prior_bars=len(prior),
        n_forward_bars=len(forward),
        entry_price=entry_price,
        atr_14=atr,
        atr_14_pct=atr_pct,
        ladder_used=ladder_used,
        event_label=event,
        underlying_pnl_pct=underlying_pnl,
        option_pnl_pct=option_pnl_costed,
        bars_to_event=nbars,
        is_resim_win=is_win,
        archived_underlying_pnl_pct=archived_und,
        drift_underlying_pp=drift,
        regime=regime_label,
        bars_to_half_atr=bars_to_half,
        atr_event_label=atr_event,
        atr_t1_plus_hit=atr_t1_plus,
        atr_bars_to_event=atr_bars_to_event,
        mfe_pct=mfe_pct,
        n_forward_bars_atr=n_forward_bars_atr,
        minute_of_day=minute_of_day,
        prior_5bar_return_pct=prior_5bar_return_pct,
        prior_30bar_realized_vol_pct=prior_30bar_realized_vol_pct,
        prior_5bar_alignment=prior_5bar_alignment,
        prior_5bar_volume_z=prior_5bar_volume_z,
    )


# ──────────────────────────────────────────────────────────────────────────────
# Aggregation
# ──────────────────────────────────────────────────────────────────────────────


def aggregate(results: list[ResimResult], key_fn) -> list[dict]:
    """Group results by key_fn and compute n / win% / EV mean / EV median (option)."""
    buckets: dict[str, list[ResimResult]] = defaultdict(list)
    for r in results:
        if r.option_pnl_pct is None or r.is_resim_win is None:
            continue
        k = key_fn(r) or "—"
        buckets[str(k)].append(r)

    rows = []
    for k, rs in buckets.items():
        n = len(rs)
        wins = sum(1 for r in rs if r.is_resim_win)
        win_pct = 100.0 * wins / n if n else None
        lo, hi = wilson_ci(wins, n) if n else (None, None)
        opts = [r.option_pnl_pct for r in rs if r.option_pnl_pct is not None]
        und = [r.underlying_pnl_pct for r in rs if r.underlying_pnl_pct is not None]
        rows.append({
            "key": k,
            "n": n,
            "wins": wins,
            "win_pct": win_pct,
            "win_ci_lo": lo,
            "win_ci_hi": hi,
            "ev_option_mean": statistics.mean(opts) if opts else None,
            "ev_option_median": statistics.median(opts) if opts else None,
            "ev_underlying_mean": statistics.mean(und) if und else None,
            "ev_underlying_median": statistics.median(und) if und else None,
        })
    rows.sort(key=lambda x: -(x["ev_option_mean"] if x["ev_option_mean"] is not None else -1e9))
    return rows


def topline(results: list[ResimResult]) -> dict:
    have = [r for r in results if r.option_pnl_pct is not None and r.is_resim_win is not None]
    n = len(have)
    if not n:
        return {"n_resimmed": 0}
    wins = sum(1 for r in have if r.is_resim_win)
    losses = sum(1 for r in have if r.is_resim_win is False)
    opts = [r.option_pnl_pct for r in have]
    und = [r.underlying_pnl_pct for r in have if r.underlying_pnl_pct is not None]
    lo, hi = wilson_ci(wins, n)
    # archive-vs-resim agreement on win/loss
    archive_aligned = 0; archive_compared = 0
    for r in have:
        if r.archived_outcome in ("WIN", "LOSS"):
            archive_compared += 1
            archived_win = (r.archived_outcome == "WIN")
            if archived_win == r.is_resim_win:
                archive_aligned += 1
    return {
        "n_resimmed": n,
        "n_wins": wins,
        "n_losses": losses,
        "win_pct": 100.0 * wins / n,
        "win_ci_lo": lo,
        "win_ci_hi": hi,
        "ev_option_mean": statistics.mean(opts),
        "ev_option_median": statistics.median(opts),
        "ev_underlying_mean": statistics.mean(und) if und else None,
        "ev_underlying_median": statistics.median(und) if und else None,
        "archive_compared": archive_compared,
        "archive_aligned": archive_aligned,
        "archive_alignment_pct": 100.0 * archive_aligned / archive_compared if archive_compared else None,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Main runner
# ──────────────────────────────────────────────────────────────────────────────


def run_backtest(out_path: Optional[Path] = None) -> dict:
    t0 = time.time()
    sigs = load_historical()
    print(f"[7y] loaded {len(sigs)} archived signals")

    # group signals by ticker so we load each bar file once
    by_ticker: dict[str, list[HistoricalSignal]] = defaultdict(list)
    for s in sigs:
        if s.ticker:
            by_ticker[s.ticker].append(s)
        else:
            by_ticker["__none__"].append(s)

    print(f"[7y] {len(by_ticker)} unique ticker buckets")

    results: list[ResimResult] = []
    n_no_bars = 0
    skip_counts: Counter = Counter()

    t_loop = time.time()
    for ti, (ticker, ticker_sigs) in enumerate(sorted(by_ticker.items()), 1):
        bars = load_minute_bars(ticker) if ticker != "__none__" else []
        if ticker != "__none__" and not bars:
            n_no_bars += len(ticker_sigs)
        for s in ticker_sigs:
            r = resimulate_signal(s, bars)
            results.append(r)
            if r.skip_reason:
                skip_counts[r.skip_reason] += 1
        if ti % 5 == 0 or ti == len(by_ticker):
            elapsed = time.time() - t_loop
            print(f"[7y]   {ti:>3d}/{len(by_ticker)}  {ticker:6s} "
                  f"sigs={len(ticker_sigs):>4d}  bars_loaded={len(bars):>7d}  "
                  f"elapsed={elapsed:5.1f}s")

    print(f"[7y] resim done in {time.time() - t0:.1f}s; "
          f"skipped: {dict(skip_counts)}")

    # aggregate
    tl = topline(results)
    by_year = aggregate(results, lambda r: r.date_str[:4] if r.date_str else "—")
    by_strategy = aggregate(results, lambda r: r.strategy_label or r.strategy_id)
    by_ticker_agg = aggregate(results, lambda r: r.ticker)
    by_direction = aggregate(results, lambda r: r.direction)
    by_category = aggregate(results, lambda r: r.category)

    # also aggregate the ARCHIVED win% per cut (for direct comparison)
    archived_top = _archived_topline(sigs)
    archived_by_year = _archived_aggregate(sigs, lambda s: s.year_str)
    archived_by_strategy = _archived_aggregate(sigs, lambda s: s.strategy_label or s.strategy_id)
    archived_by_ticker = _archived_aggregate(sigs, lambda s: s.ticker)
    archived_by_direction = _archived_aggregate(sigs, lambda s: s.direction)
    archived_by_exit = _archived_aggregate(sigs, lambda s: s.exit_reason)
    # months chronological
    archived_by_month = _archived_aggregate(sigs, lambda s: s.month_str)
    archived_by_month.sort(key=lambda r: r["key"])
    archived_by_year.sort(key=lambda r: r["key"])

    # per-strategy archive-vs-resim alignment (which strategies are gamma-distorted)
    match_by_strategy = match_rate_by_strategy(results)

    # multi-year regime sanity (does today's +58.5pp single-day spread hold?)
    regime_sanity = regime_sanity_aggregate(results)
    print(f"[7y] regime sanity (multi-year, n={regime_sanity['n_total_resimmed']}):")
    pf = regime_sanity['p_fast_t1_plus']
    ps = regime_sanity['p_slow_t1_plus']
    sp = regime_sanity['spread_pp']
    print(f"[7y]   fast: n={regime_sanity['n_fast']:>4d} "
          f"P(T1+)={pf:.1f}%" if pf is not None else "")
    print(f"[7y]   slow: n={regime_sanity['n_slow']:>4d} "
          f"P(T1+)={ps:.1f}%" if ps is not None else "")
    if sp is not None:
        print(f"[7y]   spread = {sp:+.1f}pp → {regime_sanity['verdict']}  "
              f"(years strong/weak/total = "
              f"{regime_sanity['n_years_strong']}/{regime_sanity['n_years_weak']}/"
              f"{regime_sanity['n_years_total']})")

    # hindsight test on the headline MOMENTUM top-10 line
    print("[7y] running walk-forward MOMENTUM top-10 (hindsight test)…")
    wf_momentum = walk_forward_momentum_top10(sigs)
    print(f"[7y]   in-sample n={wf_momentum['in_sample']['n']} "
          f"EV={wf_momentum['in_sample']['pnl_mean']}")
    print(f"[7y]   walk-forward n={wf_momentum['walk_forward']['n']} "
          f"EV={wf_momentum['walk_forward']['pnl_mean']}")

    out = {
        "meta": {
            "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
            "n_signals": len(sigs),
            "n_resimmed": tl.get("n_resimmed", 0),
            "n_no_bars": n_no_bars,
            "ladder_type": "premium_based",
            "opt_stop_pct": OPT_STOP_PCT,
            "opt_trail_trigger": OPT_TRAIL_TRIGGER,
            "opt_trail_giveback": OPT_TRAIL_GIVEBACK,
            "opt_profit_target": OPT_PROFIT_TARGET,
            "option_leverage": OPTION_LEVERAGE,
            "option_rt_cost_pct": FLAT_COST_OPTION_5P5X,
            "forward_cap_minutes": FORWARD_CAP_MIN,
        },
        "topline_resim": tl,
        "topline_archived": archived_top,
        # archive-side cuts (the ground truth)
        "archived_by_year": archived_by_year,
        "archived_by_month": archived_by_month,
        "archived_by_strategy": archived_by_strategy,
        "archived_by_ticker": archived_by_ticker,
        "archived_by_direction": archived_by_direction,
        "archived_by_exit": archived_by_exit,
        # resim-side cuts (linear-leverage sanity)
        "resim_by_year": by_year,
        "resim_by_strategy": by_strategy,
        "resim_by_ticker": by_ticker_agg,
        "resim_by_direction": by_direction,
        "resim_by_category": by_category,
        # archive-vs-resim alignment per strategy
        "resim_match_by_strategy": match_by_strategy,
        # multi-year regime sanity test (priority-0 per design notes)
        "regime_sanity": regime_sanity,
        # hindsight test on MOMENTUM top-10
        "walk_forward_momentum": wf_momentum,
        "skip_counts": dict(skip_counts),
        # trim per_signal to keep file small — drop the 'raw' bar fields
        "per_signal": [
            {
                "date": r.date_str,
                "ticker": r.ticker,
                "side": r.direction,
                "strategy": r.strategy_label or r.strategy_id,
                "archived_outcome": r.archived_outcome,
                "archived_pnl_pct": r.archived_pnl_pct,
                "archived_und_pct": r.archived_underlying_pnl_pct,
                "resim_und_pct": r.underlying_pnl_pct,
                "resim_opt_pct": r.option_pnl_pct,
                "resim_event": r.event_label,
                "resim_bars_to_event": r.bars_to_event,
                "drift_pp": r.drift_underlying_pp,
                "ladder": r.ladder_used,
                "atr_14_pct": r.atr_14_pct,
                "regime": r.regime,
                "atr_event": r.atr_event_label,
                "atr_t1_plus_hit": r.atr_t1_plus_hit,
                "atr_bars_to_event": r.atr_bars_to_event,
                "mfe_pct": r.mfe_pct,
                "n_forward_bars_atr": r.n_forward_bars_atr,
                # pre-trade features (slow-regime predictor input)
                "minute_of_day": r.minute_of_day,
                "prior_5bar_return_pct": r.prior_5bar_return_pct,
                "prior_30bar_realized_vol_pct": r.prior_30bar_realized_vol_pct,
                "prior_5bar_alignment": r.prior_5bar_alignment,
                "prior_5bar_volume_z": r.prior_5bar_volume_z,
                "skip_reason": r.skip_reason,
            }
            for r in results
        ],
    }

    if out_path is None:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        date = datetime.now().strftime("%Y-%m-%d")
        out_path = OUT_DIR / f"run_{date}.json"
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"[7y] wrote {out_path}  ({out_path.stat().st_size/1024:.1f} KB)")
    return out


def _archived_topline(sigs: list[HistoricalSignal]) -> dict:
    decided = [s for s in sigs if s.outcome in ("WIN", "LOSS")]
    pnls = [s.pnl_pct for s in sigs if s.pnl_pct is not None]
    if not decided:
        return {}
    w = sum(1 for s in decided if s.is_win)
    n = len(decided)
    lo, hi = wilson_ci(w, n)
    return {
        "n_decided": n,
        "win_pct": 100.0 * w / n,
        "win_ci_lo": lo,
        "win_ci_hi": hi,
        "pnl_mean": statistics.mean(pnls) if pnls else None,
        "pnl_median": statistics.median(pnls) if pnls else None,
    }


def _archived_aggregate(sigs: list[HistoricalSignal], key_fn) -> list[dict]:
    """Group archived signals by key_fn → per-bucket realized stats."""
    buckets: dict[str, list[HistoricalSignal]] = defaultdict(list)
    for s in sigs:
        if s.outcome not in ("WIN", "LOSS"):
            continue
        k = key_fn(s) or "—"
        buckets[str(k)].append(s)

    rows = []
    for k, ss in buckets.items():
        n = len(ss)
        wins = sum(1 for s in ss if s.is_win)
        pnls = [s.pnl_pct for s in ss if s.pnl_pct is not None]
        lo, hi = wilson_ci(wins, n) if n else (None, None)
        rows.append({
            "key": k,
            "n": n,
            "wins": wins,
            "win_pct": 100.0 * wins / n if n else None,
            "win_ci_lo": lo,
            "win_ci_hi": hi,
            "pnl_mean": statistics.mean(pnls) if pnls else None,
            "pnl_median": statistics.median(pnls) if pnls else None,
        })
    rows.sort(key=lambda x: -(x["pnl_mean"] if x["pnl_mean"] is not None else -1e9))
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# Resim match-rate by strategy
# ──────────────────────────────────────────────────────────────────────────────


def match_rate_by_strategy(results: list[ResimResult]) -> list[dict]:
    """
    Per-strategy archive-vs-resim agreement.

    For each strategy, count signals where:
      - archived_outcome ∈ {WIN, LOSS}
      - is_resim_win is not None
    and report alignment % = (archived_win == resim_win).

    Tells us "which archive numbers can I trust" — strategies with high
    alignment have linear-leverage faithful resims; low alignment means
    gamma/IV crush/theta dominate (i.e. archive numbers reflect option
    behaviour we can't reproduce from underlying bars alone).
    """
    buckets: dict[str, list[ResimResult]] = defaultdict(list)
    for r in results:
        if r.archived_outcome not in ("WIN", "LOSS"):
            continue
        if r.is_resim_win is None:
            continue
        key = r.strategy_label or r.strategy_id
        buckets[key].append(r)

    rows = []
    for key, rs in buckets.items():
        n = len(rs)
        archived_wins = sum(1 for r in rs if r.archived_outcome == "WIN")
        resim_wins = sum(1 for r in rs if r.is_resim_win)
        aligned = sum(
            1 for r in rs
            if (r.archived_outcome == "WIN") == bool(r.is_resim_win)
        )
        # confusion matrix
        true_pos = sum(1 for r in rs if r.archived_outcome == "WIN" and r.is_resim_win)
        true_neg = sum(1 for r in rs if r.archived_outcome == "LOSS" and not r.is_resim_win)
        false_pos = sum(1 for r in rs if r.archived_outcome == "LOSS" and r.is_resim_win)
        false_neg = sum(1 for r in rs if r.archived_outcome == "WIN" and not r.is_resim_win)
        align_pct = 100.0 * aligned / n if n else None
        archived_pnls = [r.archived_pnl_pct for r in rs if r.archived_pnl_pct is not None]
        resim_pnls = [r.option_pnl_pct for r in rs if r.option_pnl_pct is not None]
        rows.append({
            "key": key,
            "n_compared": n,
            "n_archived_wins": archived_wins,
            "n_resim_wins": resim_wins,
            "n_aligned": aligned,
            "alignment_pct": align_pct,
            "true_pos": true_pos,
            "true_neg": true_neg,
            "false_pos": false_pos,
            "false_neg": false_neg,
            "archived_pnl_mean": statistics.mean(archived_pnls) if archived_pnls else None,
            "resim_pnl_mean": statistics.mean(resim_pnls) if resim_pnls else None,
        })
    rows.sort(key=lambda x: -(x["alignment_pct"] if x["alignment_pct"] is not None else -1.0))
    return rows


# ──────────────────────────────────────────────────────────────────────────────
# Regime sanity (multi-year archive replay)
# ──────────────────────────────────────────────────────────────────────────────


def _verdict_for_spread(spread_pp: Optional[float]) -> str:
    if spread_pp is None:
        return "no data"
    if spread_pp >= 30:
        return "strong real edge"
    if spread_pp >= 20:
        return "real edge"
    if spread_pp >= 10:
        return "suspended judgment"
    return "decorative"


def regime_sanity_aggregate(results: list[ResimResult]) -> dict:
    """
    Multi-year version of P(T1+ | regime).

    The single-day intraday version (in pipeline.py) computes P(T1+|fast) vs
    P(T1+|slow) on today's 232 fires. That spread is inflated by single-day
    regime homogeneity and near-coupled outcome.

    This computes the same metric across the whole 7-year archive resim
    (~1,310 fires with bars), with a year-by-year breakdown to test stability.

    If the multi-year spread holds ≥30pp, regime tag is a real production
    filter. If it collapses below +10pp, regime detection is decorative on
    multi-regime data (today's snapshot was an artifact).
    """
    have = [r for r in results
            if r.regime in ("fast", "slow") and r.atr_t1_plus_hit is not None]

    def _bucket(rs):
        n = len(rs)
        n_t1 = sum(1 for r in rs if r.atr_t1_plus_hit)
        p = (100.0 * n_t1 / n) if n else None
        return n, n_t1, p

    n_fast, n_fast_t1, p_fast_t1 = _bucket([r for r in have if r.regime == "fast"])
    n_slow, n_slow_t1, p_slow_t1 = _bucket([r for r in have if r.regime == "slow"])
    spread = (p_fast_t1 - p_slow_t1) if (p_fast_t1 is not None and p_slow_t1 is not None) else None

    # By-year — does spread hold up across regimes?
    by_year_rows = []
    by_year: dict[str, list[ResimResult]] = defaultdict(list)
    for r in have:
        y = (r.date_str or "")[:4] or "—"
        by_year[y].append(r)
    for y in sorted(by_year):
        ys = by_year[y]
        nf, nft, pft = _bucket([r for r in ys if r.regime == "fast"])
        ns, nst, pst = _bucket([r for r in ys if r.regime == "slow"])
        sp = (pft - pst) if (pft is not None and pst is not None) else None
        by_year_rows.append({
            "year": y,
            "n_total": len(ys),
            "n_fast": nf, "n_fast_t1_plus": nft, "p_fast_t1_plus": pft,
            "n_slow": ns, "n_slow_t1_plus": nst, "p_slow_t1_plus": pst,
            "spread_pp": sp,
            "verdict": _verdict_for_spread(sp),
        })

    # Stability check: how many years had spread ≥ 20pp vs collapsed below 10?
    years_with_data = [r for r in by_year_rows if r["spread_pp"] is not None]
    n_years_strong = sum(1 for r in years_with_data if r["spread_pp"] >= 20)
    n_years_weak = sum(1 for r in years_with_data if r["spread_pp"] < 10)
    n_years_total = len(years_with_data)

    # ── Slow-fires probe (mechanism behind 0/305) ────────────────────────────
    # Three competing explanations for "0% T1+ hit on slow regime":
    #   A) died_flat_pre_cap — slow fires really do die quietly within 4h
    #   B) cap_truncated     — slow fires get cut off at FORWARD_CAP_MIN=240
    #   C) stopped_out       — slow fires are heavily stopped out (already-failed
    #                          trades by the time we ask the regime question;
    #                          regime gate would be redundant with stop)
    slow_fires = [r for r in have if r.regime == "slow"]
    CAP_NEAR = FORWARD_CAP_MIN - 5   # treat last 5 bars as "near cap" (~235)
    slow_breakdown = {
        "n_slow_total": len(slow_fires),
        "stopped_out": sum(1 for r in slow_fires if r.atr_event_label == "STOP"),
        "cap_truncated": sum(
            1 for r in slow_fires
            if r.atr_event_label == "EOD"
            and (r.atr_bars_to_event is not None and r.atr_bars_to_event >= CAP_NEAR)
        ),
        "died_flat_pre_cap": sum(
            1 for r in slow_fires
            if r.atr_event_label == "EOD"
            and (r.atr_bars_to_event is not None and r.atr_bars_to_event < CAP_NEAR)
        ),
        "other": sum(
            1 for r in slow_fires
            if r.atr_event_label not in ("STOP", "EOD") or r.atr_event_label is None
        ),
    }
    slow_bars = [r.atr_bars_to_event for r in slow_fires if r.atr_bars_to_event is not None]
    slow_mfes = [r.mfe_pct for r in slow_fires if r.mfe_pct is not None]
    slow_atr14 = [r.atr_14_pct for r in slow_fires if r.atr_14_pct is not None]
    fast_fires = [r for r in have if r.regime == "fast"]
    fast_atr14 = [r.atr_14_pct for r in fast_fires if r.atr_14_pct is not None]
    slow_breakdown.update({
        "n_fast_total": len(fast_fires),
        "mean_bars_to_event": (sum(slow_bars) / len(slow_bars)) if slow_bars else None,
        "median_bars_to_event": statistics.median(slow_bars) if slow_bars else None,
        "mean_mfe_pct": (sum(slow_mfes) / len(slow_mfes)) if slow_mfes else None,
        "mean_atr_14_pct": (sum(slow_atr14) / len(slow_atr14)) if slow_atr14 else None,
        "mean_atr_14_pct_fast": (sum(fast_atr14) / len(fast_atr14)) if fast_atr14 else None,
        "median_atr_14_pct_slow": statistics.median(slow_atr14) if slow_atr14 else None,
        "median_atr_14_pct_fast": statistics.median(fast_atr14) if fast_atr14 else None,
        "cap_threshold_bars": CAP_NEAR,
        "forward_cap_min": FORWARD_CAP_MIN,
    })
    # Fraction MFE reached half-of-T1 (0.375×ATR) — softer probe of whether slow
    # fires get *close* to T1 or fail entirely
    if slow_atr14 and slow_mfes:
        half_t1_threshold_count = sum(
            1 for r in slow_fires
            if r.mfe_pct is not None and r.atr_14_pct is not None
            and r.mfe_pct >= 0.375 * r.atr_14_pct
        )
        slow_breakdown["n_mfe_above_half_t1"] = half_t1_threshold_count
        slow_breakdown["pct_mfe_above_half_t1"] = (
            100.0 * half_t1_threshold_count / len(slow_fires)
        )

    # Verdict on which explanation dominates
    n_slow_with_event = sum(slow_breakdown.get(k, 0) for k in
                            ("stopped_out", "cap_truncated", "died_flat_pre_cap"))
    if n_slow_with_event > 0:
        pct_stop = 100.0 * slow_breakdown["stopped_out"] / n_slow_with_event
        pct_cap = 100.0 * slow_breakdown["cap_truncated"] / n_slow_with_event
        pct_flat = 100.0 * slow_breakdown["died_flat_pre_cap"] / n_slow_with_event
        if pct_stop >= 70:
            mech_verdict = "C — stop-dominated (gate partly redundant with stop)"
        elif pct_cap >= 30:
            mech_verdict = "B — cap-truncated (re-run with FORWARD_CAP_MIN=480)"
        elif pct_flat >= 50:
            mech_verdict = "A — slow fires die quietly (gate is real edge)"
        else:
            mech_verdict = "mixed — no single mechanism dominates"
        slow_breakdown["pct_stop"] = pct_stop
        slow_breakdown["pct_cap"] = pct_cap
        slow_breakdown["pct_flat"] = pct_flat
        slow_breakdown["mechanism_verdict"] = mech_verdict
    else:
        slow_breakdown["mechanism_verdict"] = "no event data"

    # ── Alignment-quintile probe ────────────────────────────────────────────
    # The pre-trade predictor showed the LR coefficient on prior_5bar_alignment
    # is positive on the slow class (with-trend → more slow). The GBM ranks
    # alignment as its #1 feature but unsigned. This probe bins fires by
    # prior_5bar_alignment and reports P(slow) and P(T1+) per quintile.
    #
    # Interpretation:
    #   - Monotonic decreasing P(slow) Q1→Q5: counter-aligned has more slow,
    #     LR sign was wrong, fire-timing is fine.
    #   - Monotonic increasing P(slow) Q1→Q5: with-trend has more slow,
    #     LR sign was right, engine_v4 fires at move-exhaustion.
    #   - U-shaped: both extremes are bad; sweet spot is neutral alignment.
    aligned = [r for r in have
               if r.prior_5bar_alignment is not None]
    aligned.sort(key=lambda r: r.prior_5bar_alignment)
    align_quintile_rows = []
    align_shape = "no data"
    if len(aligned) >= 25:
        n_a = len(aligned)
        qsize = n_a // 5
        for q in range(5):
            lo = q * qsize
            hi = (q + 1) * qsize if q < 4 else n_a
            bucket = aligned[lo:hi]
            n_b = len(bucket)
            if n_b == 0:
                continue
            n_b_slow = sum(1 for r in bucket if r.regime == "slow")
            n_b_t1plus = sum(
                1 for r in bucket
                if r.atr_event_label and r.atr_event_label.startswith("TARGET_")
            )
            align_lo = bucket[0].prior_5bar_alignment
            align_hi = bucket[-1].prior_5bar_alignment
            align_med = bucket[n_b // 2].prior_5bar_alignment
            align_quintile_rows.append({
                "quintile": q + 1,
                "n": n_b,
                "alignment_lo": align_lo,
                "alignment_hi": align_hi,
                "alignment_median": align_med,
                "n_slow": n_b_slow,
                "n_t1_plus": n_b_t1plus,
                "p_slow": 100.0 * n_b_slow / n_b,
                "p_t1_plus": 100.0 * n_b_t1plus / n_b,
            })

        # Classify shape: monotonic incr / decr / U-shape / inverse-U / flat
        slow_pcts = [r["p_slow"] for r in align_quintile_rows]
        if len(slow_pcts) >= 5:
            # Monotonicity check (allow small wiggle)
            diffs = [slow_pcts[i+1] - slow_pcts[i] for i in range(4)]
            n_pos = sum(1 for d in diffs if d > 1.0)
            n_neg = sum(1 for d in diffs if d < -1.0)
            spread_5 = max(slow_pcts) - min(slow_pcts)
            if spread_5 < 5.0:
                align_shape = "flat — alignment doesn't materially affect P(slow)"
            elif n_pos >= 3 and n_neg <= 1:
                align_shape = ("monotonic increasing — with-trend entries are more slow "
                               "(engine_v4 fires at move-exhaustion; LR sign was right)")
            elif n_neg >= 3 and n_pos <= 1:
                align_shape = ("monotonic decreasing — counter-aligned entries are more slow "
                               "(naive intuition was right; LR sign was misleading)")
            elif slow_pcts[0] > slow_pcts[2] and slow_pcts[4] > slow_pcts[2]:
                align_shape = ("U-shaped — both extremes have more slow; neutral alignment "
                               "is the sweet spot (LR was averaging an interaction)")
            elif slow_pcts[0] < slow_pcts[2] and slow_pcts[4] < slow_pcts[2]:
                align_shape = ("inverse-U — neutral alignment has more slow; both extremes "
                               "are safer (rare, would imply specific timing dynamics)")
            else:
                align_shape = "non-monotonic, non-U — mixed pattern"

    alignment_probe = {
        "quintiles": align_quintile_rows,
        "shape": align_shape,
        "n_eligible": len(aligned),
    }

    return {
        "n_total_resimmed": len(have),
        "n_fast": n_fast,
        "n_slow": n_slow,
        "pct_fast": (100.0 * n_fast / len(have)) if have else None,
        "pct_slow": (100.0 * n_slow / len(have)) if have else None,
        "n_fast_t1_plus": n_fast_t1,
        "n_slow_t1_plus": n_slow_t1,
        "p_fast_t1_plus": p_fast_t1,
        "p_slow_t1_plus": p_slow_t1,
        "spread_pp": spread,
        "verdict": _verdict_for_spread(spread),
        "by_year": by_year_rows,
        "n_years_total": n_years_total,
        "n_years_strong": n_years_strong,    # spread ≥ 20pp
        "n_years_weak": n_years_weak,        # spread < 10pp
        "slow_breakdown": slow_breakdown,
        "alignment_quintile_probe": alignment_probe,
    }


# ──────────────────────────────────────────────────────────────────────────────
# Walk-forward MOMENTUM top-10 (hindsight test)
# ──────────────────────────────────────────────────────────────────────────────


MOMENTUM_STRATEGY_HINTS = ("MOMENTUM", "momentum")


def _is_momentum(sig: HistoricalSignal) -> bool:
    sid = (sig.strategy_id or "").upper()
    lbl = (sig.strategy_label or "").upper()
    return ("MOMENTUM" in sid) or ("MOMENTUM" in lbl)


def _date_to_epoch(date_str: str) -> Optional[float]:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str[:10], "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    except Exception:
        return None


def walk_forward_momentum_top10(
    sigs: list[HistoricalSignal],
    window_days: int = 365,
    min_n_per_ticker: int = 5,
    top_k: int = 10,
) -> dict:
    """
    Hindsight-free MOMENTUM top-10 ticker EV.

    For each MOMENTUM signal at date D:
      - lookback set L = MOMENTUM signals with date in [D - window_days, D)
        (strict <, never includes D itself)
      - per ticker: mean pnl_pct over L (require n >= min_n_per_ticker)
      - rank tickers by mean desc → top_k tickers
      - if this signal's ticker is in the top_k: include in walk-forward set

    Aggregate the walk-forward signals (n / win% / EV / median).
    Compare against in-sample MOMENTUM top-10 (computed once over the full
    archive) to quantify hindsight bias.
    """
    momentum = [s for s in sigs if _is_momentum(s) and s.outcome in ("WIN", "LOSS")]
    momentum.sort(key=lambda s: _date_to_epoch(s.date_str) or 0.0)

    # in-sample top-10 by ticker (the headline +162% comes from this sort of cut)
    by_ticker: dict[str, list[HistoricalSignal]] = defaultdict(list)
    for s in momentum:
        if s.ticker:
            by_ticker[s.ticker].append(s)
    in_sample_rows = []
    for tk, tk_sigs in by_ticker.items():
        n = len(tk_sigs)
        if n < min_n_per_ticker:
            continue
        wins = sum(1 for s in tk_sigs if s.is_win)
        pnls = [s.pnl_pct for s in tk_sigs if s.pnl_pct is not None]
        in_sample_rows.append({
            "ticker": tk,
            "n": n,
            "win_pct": 100.0 * wins / n,
            "pnl_mean": statistics.mean(pnls) if pnls else None,
        })
    in_sample_rows.sort(key=lambda r: -(r["pnl_mean"] if r["pnl_mean"] is not None else -1e9))
    in_sample_top = in_sample_rows[:top_k]
    in_sample_top_tickers = [r["ticker"] for r in in_sample_top]
    # in-sample headline (the line under suspicion)
    in_sample_signals = [
        s for s in momentum
        if s.ticker in set(in_sample_top_tickers)
    ]
    in_n = len(in_sample_signals)
    in_w = sum(1 for s in in_sample_signals if s.is_win)
    in_pnls = [s.pnl_pct for s in in_sample_signals if s.pnl_pct is not None]
    in_ci = wilson_ci(in_w, in_n) if in_n else (None, None)
    in_sample_summary = {
        "n": in_n,
        "win_pct": 100.0 * in_w / in_n if in_n else None,
        "win_ci_lo": in_ci[0],
        "win_ci_hi": in_ci[1],
        "pnl_mean": statistics.mean(in_pnls) if in_pnls else None,
        "pnl_median": statistics.median(in_pnls) if in_pnls else None,
        "top_tickers": in_sample_top_tickers,
    }

    # walk-forward: re-rank at each signal's date using only prior data
    wf_signals: list[HistoricalSignal] = []
    skipped_no_history = 0
    secs_per_day = 86400.0
    for sig in momentum:
        sig_epoch = _date_to_epoch(sig.date_str)
        if sig_epoch is None:
            continue
        lo_epoch = sig_epoch - window_days * secs_per_day
        # build per-ticker stats from strictly prior signals
        prior_by_ticker: dict[str, list[HistoricalSignal]] = defaultdict(list)
        for s in momentum:
            ep = _date_to_epoch(s.date_str)
            if ep is None or ep >= sig_epoch or ep < lo_epoch:
                continue
            if not s.ticker:
                continue
            prior_by_ticker[s.ticker].append(s)
        scored: list[tuple[str, float]] = []
        for tk, tk_sigs in prior_by_ticker.items():
            if len(tk_sigs) < min_n_per_ticker:
                continue
            pnls = [s.pnl_pct for s in tk_sigs if s.pnl_pct is not None]
            if not pnls:
                continue
            scored.append((tk, statistics.mean(pnls)))
        if not scored:
            skipped_no_history += 1
            continue
        scored.sort(key=lambda kv: -kv[1])
        top_tickers = {tk for tk, _ in scored[:top_k]}
        if sig.ticker in top_tickers:
            wf_signals.append(sig)

    wf_n = len(wf_signals)
    wf_w = sum(1 for s in wf_signals if s.is_win)
    wf_pnls = [s.pnl_pct for s in wf_signals if s.pnl_pct is not None]
    wf_ci = wilson_ci(wf_w, wf_n) if wf_n else (None, None)
    wf_summary = {
        "n": wf_n,
        "win_pct": 100.0 * wf_w / wf_n if wf_n else None,
        "win_ci_lo": wf_ci[0],
        "win_ci_hi": wf_ci[1],
        "pnl_mean": statistics.mean(wf_pnls) if wf_pnls else None,
        "pnl_median": statistics.median(wf_pnls) if wf_pnls else None,
        "skipped_no_history": skipped_no_history,
    }

    # hindsight bias = in-sample EV - walk-forward EV
    hindsight_bias = None
    if (in_sample_summary["pnl_mean"] is not None
            and wf_summary["pnl_mean"] is not None):
        hindsight_bias = in_sample_summary["pnl_mean"] - wf_summary["pnl_mean"]

    return {
        "params": {
            "window_days": window_days,
            "min_n_per_ticker": min_n_per_ticker,
            "top_k": top_k,
        },
        "n_momentum_signals": len(momentum),
        "in_sample": in_sample_summary,
        "walk_forward": wf_summary,
        "hindsight_bias_pp": hindsight_bias,   # in-sample EV minus WF EV
        "in_sample_top_table": in_sample_top,
    }


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    out = run_backtest()

    print()
    print("─" * 80)
    print("ARCHIVED TOP-LINE (engine_scalp recorded outcomes)")
    a = out["topline_archived"]
    if a:
        print(f"  n_decided={a['n_decided']}  win%={a['win_pct']:.1f}  "
              f"CI=[{a['win_ci_lo']:.1f},{a['win_ci_hi']:.1f}]  "
              f"pnl_mean={a['pnl_mean']:+.2f}  pnl_median={a['pnl_median']:+.2f}")

    print()
    print("RESIM TOP-LINE (re-derived from minute bars + ATR ladder, 5.5× option)")
    t = out["topline_resim"]
    if t:
        print(f"  n_resimmed={t['n_resimmed']}  win%={t['win_pct']:.1f}  "
              f"CI=[{t['win_ci_lo']:.1f},{t['win_ci_hi']:.1f}]")
        print(f"  EV option mean={t['ev_option_mean']:+.2f}  median={t['ev_option_median']:+.2f}")
        if t.get("ev_underlying_mean") is not None:
            print(f"  EV underlying mean={t['ev_underlying_mean']:+.2f}  median={t['ev_underlying_median']:+.2f}")
        print(f"  Archive vs resim alignment: {t['archive_aligned']}/{t['archive_compared']} "
              f"({t['archive_alignment_pct']:.1f}%)")

    print()
    print("ARCHIVED — BY YEAR (engine_scalp realized):")
    for r in out["archived_by_year"]:
        wp = f"{r['win_pct']:.1f}%" if r['win_pct'] is not None else "—"
        pm = f"{r['pnl_mean']:+.2f}%" if r['pnl_mean'] is not None else "—"
        print(f"  {r['key']:6s}  n={r['n']:>5d}  win={wp:>6s}  pnl_mean={pm:>9s}")

    print()
    print("ARCHIVED — BY STRATEGY:")
    for r in out["archived_by_strategy"]:
        wp = f"{r['win_pct']:.1f}%" if r['win_pct'] is not None else "—"
        pm = f"{r['pnl_mean']:+.2f}%" if r['pnl_mean'] is not None else "—"
        print(f"  {r['key']:45s}  n={r['n']:>5d}  win={wp:>6s}  pnl_mean={pm:>9s}")

    print()
    print("ARCHIVED — BY TICKER (top 15, n>=20):")
    for r in [r for r in out["archived_by_ticker"] if r["n"] >= 20][:15]:
        wp = f"{r['win_pct']:.1f}%" if r['win_pct'] is not None else "—"
        pm = f"{r['pnl_mean']:+.2f}%" if r['pnl_mean'] is not None else "—"
        print(f"  {r['key']:8s}  n={r['n']:>5d}  win={wp:>6s}  pnl_mean={pm:>9s}")

    print()
    print("ARCHIVED — BY DIRECTION:")
    for r in out["archived_by_direction"]:
        wp = f"{r['win_pct']:.1f}%" if r['win_pct'] is not None else "—"
        pm = f"{r['pnl_mean']:+.2f}%" if r['pnl_mean'] is not None else "—"
        print(f"  {r['key']:6s}  n={r['n']:>5d}  win={wp:>6s}  pnl_mean={pm:>9s}")

    print()
    print("ARCHIVED — BY EXIT REASON:")
    for r in out["archived_by_exit"]:
        wp = f"{r['win_pct']:.1f}%" if r['win_pct'] is not None else "—"
        pm = f"{r['pnl_mean']:+.2f}%" if r['pnl_mean'] is not None else "—"
        print(f"  {r['key']:20s}  n={r['n']:>5d}  win={wp:>6s}  pnl_mean={pm:>9s}")

    print()
    print("RESIM — BY YEAR (linear-leverage sanity check):")
    for r in out["resim_by_year"]:
        wp = f"{r['win_pct']:.1f}%"
        ev = f"{r['ev_option_mean']:+.2f}%"
        print(f"  {r['key']:6s}  n={r['n']:>5d}  win={wp:>6s}  ev_opt={ev:>9s}")

    print()
    print("RESIM MATCH-RATE BY STRATEGY (which archive numbers can I trust):")
    print(f"  {'strategy':45s} {'n':>5s} {'arc_w':>5s} {'res_w':>5s} {'align%':>7s} "
          f"{'arc_pnl':>8s} {'res_pnl':>8s}")
    for r in out["resim_match_by_strategy"]:
        ap = f"{r['archived_pnl_mean']:+.1f}" if r['archived_pnl_mean'] is not None else "—"
        rp = f"{r['resim_pnl_mean']:+.1f}" if r['resim_pnl_mean'] is not None else "—"
        align = f"{r['alignment_pct']:.1f}%" if r['alignment_pct'] is not None else "—"
        print(f"  {r['key']:45s} {r['n_compared']:>5d} {r['n_archived_wins']:>5d} "
              f"{r['n_resim_wins']:>5d} {align:>7s} {ap:>8s} {rp:>8s}")

    print()
    print("WALK-FORWARD MOMENTUM TOP-10 (hindsight test):")
    wf = out["walk_forward_momentum"]
    p = wf["params"]
    print(f"  params: window_days={p['window_days']}  min_n_per_ticker={p['min_n_per_ticker']}  top_k={p['top_k']}")
    print(f"  total MOMENTUM signals: {wf['n_momentum_signals']}")
    ins = wf["in_sample"]
    wfs = wf["walk_forward"]
    if ins.get("pnl_mean") is not None:
        print(f"  in-sample top-10:  n={ins['n']:>4d}  win={ins['win_pct']:.1f}%  "
              f"pnl_mean={ins['pnl_mean']:+.2f}  pnl_median={ins['pnl_median']:+.2f}")
        print(f"    top tickers: {', '.join(ins['top_tickers'])}")
    if wfs.get("pnl_mean") is not None:
        print(f"  walk-forward:      n={wfs['n']:>4d}  win={wfs['win_pct']:.1f}%  "
              f"pnl_mean={wfs['pnl_mean']:+.2f}  pnl_median={wfs['pnl_median']:+.2f}")
        print(f"    skipped (no prior history): {wfs['skipped_no_history']}")
    if wf.get("hindsight_bias_pp") is not None:
        print(f"  hindsight bias (in-sample - walk-forward): {wf['hindsight_bias_pp']:+.2f}pp")
