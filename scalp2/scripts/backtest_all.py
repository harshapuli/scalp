"""scripts/backtest_all.py — Replay-based backtest for all 5 strategies.

Pulls real Alpaca SIP bars per ticker, classifies through scalp brain, dispatches
to all 5 strategies, simulates trade outcomes (target/stop within forward window),
computes hit rate + EV + Sharpe per strategy.

Usage:
  $ python3 scripts/backtest_all.py --start 2026-04-01 --end 2026-04-25
  $ python3 scripts/backtest_all.py --tickers SPY,QQQ,NVDA --strategies S2,S3,S5

Output: data/backtest/run_<DATE>.json with per-strategy metrics.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config_loader import load_thresholds
from infra.secrets import load_secrets


PROJECT_ROOT = Path(__file__).resolve().parent.parent
BACKTEST_DIR = PROJECT_ROOT / "data" / "backtest"

# Forward window in 1m bars to check target / stop after each TRADE decision
FORWARD_BARS = 60                # 60 minutes
ATR_TARGET_MULT = 1.0
ATR_STOP_MULT = 1.0


@dataclass
class TradeOutcome:
    strategy: str
    ticker: str
    timestamp_iso: str
    direction: str                  # 'long' | 'short'
    entry_price: float
    target_price: float
    stop_price: float
    exit_reason: str                # 'target' | 'stop' | 'eod' | 'timeout'
    exit_price: float
    bars_to_event: int
    pnl_atr: float                  # signed pnl in ATR units
    is_win: bool


@dataclass
class StrategyMetrics:
    strategy: str
    n_trades: int
    n_wins: int
    n_losses: int
    win_pct: float
    avg_pnl_atr: float
    median_pnl_atr: float
    sharpe: float
    max_drawdown_atr: float
    pass_reasons: dict[str, int] = field(default_factory=dict)


def _simulate_first_touch(future_bars, target, stop, direction, entry_price):
    """Returns (exit_reason, exit_price, bars_to_event, pnl_atr)."""
    is_long = direction == "long"
    for i, b in enumerate(future_bars):
        if is_long:
            hit_target = b.h >= target
            hit_stop = b.l <= stop
        else:
            hit_target = b.l <= target
            hit_stop = b.h >= stop
        # Stop-first on wide bars (conservative)
        if hit_stop and hit_target:
            return "stop", stop, i, _signed_pnl_atr(stop, entry_price, direction, target, stop)
        if hit_stop:
            return "stop", stop, i, _signed_pnl_atr(stop, entry_price, direction, target, stop)
        if hit_target:
            return "target", target, i, _signed_pnl_atr(target, entry_price, direction, target, stop)
    if future_bars:
        last_close = future_bars[-1].c
        return "timeout", last_close, len(future_bars) - 1, _signed_pnl_atr(last_close, entry_price, direction, target, stop)
    return "no_data", entry_price, 0, 0.0


def _signed_pnl_atr(exit_price, entry_price, direction, target, stop):
    """PnL in ATR units (target - entry = +1 ATR for long; flipped for short)."""
    atr = abs(target - entry_price)
    if atr <= 0:
        return 0.0
    raw = exit_price - entry_price
    return (raw / atr) if direction == "long" else (-raw / atr)


def backtest_one_ticker(*,
                         ticker: str,
                         alpaca,
                         uw,
                         cfg: dict,
                         start_iso: str,
                         end_iso: str,
                         strategies: list[str]) -> tuple[list[TradeOutcome], dict[str, dict[str, int]]]:
    """Replays one ticker over [start, end], returns trade outcomes + per-strategy
    pass-reason histogram.
    """
    from features.builder import build_features
    from features.price_structure import BarOHLC, atr_from_bars
    from scalp_brain.classifier import classify
    from features.datatypes import ScalpState

    print(f"  [{ticker}] fetching bars...")
    bars_alp = alpaca.get_bars(
        ticker, start=start_iso, end=end_iso,
        timeframe="1Min", feed="sip", limit=10000,
    )
    if len(bars_alp) < 60:
        print(f"  [{ticker}] insufficient bars ({len(bars_alp)})")
        return [], {}
    print(f"  [{ticker}] got {len(bars_alp)} bars; replaying...")

    # Pull GEX history once (daily aggregates)
    try:
        gex_history = uw.greek_exposure_history(ticker, days=60)
    except Exception:
        gex_history = []

    ohlc_bars = [BarOHLC(o=b.o, h=b.h, l=b.l, c=b.c, v=b.v) for b in bars_alp]
    timestamps = [b.t for b in bars_alp]

    outcomes: list[TradeOutcome] = []
    pass_hist: dict[str, dict[str, int]] = {s: {} for s in strategies}
    prior_state: Optional[ScalpState] = None

    # Cache one flow snapshot up front (intraday flow doesn't help over 30-day backtest)
    try:
        flow = uw.flow_recent(ticker)[:50]
    except Exception:
        flow = []

    for i, (b, ts) in enumerate(zip(ohlc_bars, timestamps)):
        if i < 30:
            continue   # warm up bars

        # Find latest GEX snapshot ≤ ts
        gex_at_t = None
        for snap in reversed(gex_history):
            if snap.timestamp <= ts:
                gex_at_t = snap
                break

        try:
            f = build_features(
                ticker=ticker, bar_idx=i,
                bars=ohlc_bars[:i + 1],
                trades=[], quotes=[],
                gex_snapshot=gex_at_t,
                flow_records=flow,
                timestamp=ts,
            )
            new_state = classify(prior_state=prior_state, features=f, cfg=cfg)
        except Exception:
            continue

        state_changed = (prior_state is None) or (prior_state.name != new_state.name)
        prior_state = new_state

        if not state_changed:
            continue

        # Dispatch decisions per strategy (rules-only, no risk_manager_allows simulation)
        atr = atr_from_bars(ohlc_bars[:i + 1])
        if atr <= 0:
            continue

        future_bars = ohlc_bars[i + 1:i + 1 + FORWARD_BARS]
        if not future_bars:
            continue

        # ── S5 — Reversal ────────────────────────────────────────
        if "S5" in strategies and new_state.name in ("SURGE_REVERSE", "TANK_REVERSE"):
            from strategies.s5_gamma_reversal.allow_s5_trade import allow_s5_trade
            d = allow_s5_trade(
                features=f, model=None, threshold=0.50, cfg=cfg,
                risk_manager_allows=True, expected_value_net=0.05,
                reversal_score=new_state.score,
            )
            if d.decision == "TRADE":
                direction = "short" if new_state.name == "SURGE_REVERSE" else "long"
                target = b.c + (atr * ATR_TARGET_MULT if direction == "long" else -atr * ATR_TARGET_MULT)
                stop = b.c - (atr * ATR_STOP_MULT if direction == "long" else -atr * ATR_STOP_MULT)
                er, ep, nb, pnl = _simulate_first_touch(future_bars, target, stop, direction, b.c)
                outcomes.append(TradeOutcome(
                    strategy="S5", ticker=ticker, timestamp_iso=ts.isoformat(),
                    direction=direction, entry_price=b.c, target_price=target,
                    stop_price=stop, exit_reason=er, exit_price=ep,
                    bars_to_event=nb, pnl_atr=pnl, is_win=(pnl > 0),
                ))
            else:
                pass_hist["S5"][d.pass_reason] = pass_hist["S5"].get(d.pass_reason, 0) + 1

        # ── S3 — Continuation ────────────────────────────────────
        if "S3" in strategies and new_state.name in ("SURGE_CONTINUATION", "TANK_CONTINUATION"):
            from strategies.s3_momentum.allow_s3_trade import allow_s3_trade
            from strategies.s3_momentum.setup import S3SetupContext
            from scalp_brain.scores import reversal_score as rev_score
            direction = "long" if new_state.name == "SURGE_CONTINUATION" else "short"
            ctx = S3SetupContext(
                ticker=ticker,
                session_return_atr=f.extension_from_prior_close_atr,
                vwap_distance_atr=f.extension_from_vwap_atr,
                consecutive_higher_lows=4 if direction == "long" and not f.pullback_break else 0,
                consecutive_lower_highs=4 if direction == "short" and not f.pullback_break else 0,
                aggressor_avg_30m=f.aggressor_recent,
                near_htf_level_atr=f.near_htf_level_atr,
                distance_to_pos_gex_atr=abs(f.distance_to_major_pos_gex_atr),
            )
            rs = rev_score(f, "short" if direction == "long" else "long", cfg)
            d = allow_s3_trade(
                setup_ctx=ctx, features=f, scalp_state=new_state,
                reversal_score=rs, cfg=cfg, risk_manager_allows=True,
            )
            if d.decision == "TRADE":
                target = b.c + (atr * ATR_TARGET_MULT if direction == "long" else -atr * ATR_TARGET_MULT)
                stop = b.c - (atr * ATR_STOP_MULT if direction == "long" else -atr * ATR_STOP_MULT)
                er, ep, nb, pnl = _simulate_first_touch(future_bars, target, stop, direction, b.c)
                outcomes.append(TradeOutcome(
                    strategy="S3", ticker=ticker, timestamp_iso=ts.isoformat(),
                    direction=direction, entry_price=b.c, target_price=target,
                    stop_price=stop, exit_reason=er, exit_price=ep,
                    bars_to_event=nb, pnl_atr=pnl, is_win=(pnl > 0),
                ))
            else:
                pass_hist["S3"][d.pass_reason] = pass_hist["S3"].get(d.pass_reason, 0) + 1

        # ── S2 — ORB Ignition ─────────────────────────────────────
        if "S2" in strategies and new_state.name in ("SURGE_IGNITION", "TANK_IGNITION"):
            from strategies.s2_orb.allow_s2_trade import allow_s2_trade
            from strategies.s2_orb.setup import S2SetupContext
            from strategies.s2_orb.opening_range import OpeningRange

            # Minimal stub OR (open of session = first bar's open)
            session_open = ohlc_bars[max(0, i - 30)].o
            or_ = OpeningRange(
                ticker=ticker, high=session_open * 1.005, low=session_open * 0.995,
                mid=session_open, computed_at=ts,
            )
            ctx = S2SetupContext(
                ticker=ticker, last_close=b.c,
                last_bar_volume=b.v, avg_minute_volume_30bar=sum(x.v for x in ohlc_bars[max(0, i-30):i])/30,
                gap_pct=0.025,                     # placeholder (need prior close)
                pre_market_volume_ratio=5.5,       # placeholder
                opening_range=or_, earnings_blackout=False,
            )
            d = allow_s2_trade(
                setup_ctx=ctx, features=f, scalp_state=new_state, cfg=cfg,
                risk_manager_allows=True,
            )
            if d.decision == "TRADE":
                direction = "long" if new_state.name == "SURGE_IGNITION" else "short"
                target = b.c + (atr * ATR_TARGET_MULT if direction == "long" else -atr * ATR_TARGET_MULT)
                stop = b.c - (atr * ATR_STOP_MULT if direction == "long" else -atr * ATR_STOP_MULT)
                er, ep, nb, pnl = _simulate_first_touch(future_bars, target, stop, direction, b.c)
                outcomes.append(TradeOutcome(
                    strategy="S2", ticker=ticker, timestamp_iso=ts.isoformat(),
                    direction=direction, entry_price=b.c, target_price=target,
                    stop_price=stop, exit_reason=er, exit_price=ep,
                    bars_to_event=nb, pnl_atr=pnl, is_win=(pnl > 0),
                ))
            else:
                pass_hist["S2"][d.pass_reason] = pass_hist["S2"].get(d.pass_reason, 0) + 1

    return outcomes, pass_hist


def aggregate_metrics(outcomes: list[TradeOutcome],
                       pass_hist: dict[str, int]) -> StrategyMetrics:
    if not outcomes:
        return StrategyMetrics(
            strategy=pass_hist.get("__strategy__", "?"),
            n_trades=0, n_wins=0, n_losses=0,
            win_pct=0.0, avg_pnl_atr=0.0, median_pnl_atr=0.0,
            sharpe=0.0, max_drawdown_atr=0.0,
            pass_reasons=pass_hist,
        )
    pnls = [o.pnl_atr for o in outcomes]
    wins = [o for o in outcomes if o.is_win]
    losses = [o for o in outcomes if not o.is_win]
    avg = statistics.mean(pnls)
    sd = statistics.stdev(pnls) if len(pnls) >= 2 else 0
    sharpe = (avg / sd * (252 ** 0.5)) if sd > 0 else 0
    # Max drawdown: cumulative pnl peak-to-trough
    cum = 0; peak = 0; dd = 0
    for p in pnls:
        cum += p
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return StrategyMetrics(
        strategy=outcomes[0].strategy,
        n_trades=len(outcomes), n_wins=len(wins), n_losses=len(losses),
        win_pct=100.0 * len(wins) / len(outcomes),
        avg_pnl_atr=avg, median_pnl_atr=statistics.median(pnls),
        sharpe=sharpe, max_drawdown_atr=dd,
        pass_reasons=pass_hist,
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--tickers", default="SPY,QQQ,NVDA,TSLA,AAPL,META,GOOGL,AMZN,MSFT,IWM")
    parser.add_argument("--strategies", default="S2,S3,S5")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()

    load_secrets()
    cfg = load_thresholds()
    BACKTEST_DIR.mkdir(parents=True, exist_ok=True)

    from data_clients.alpaca import AlpacaClient
    from data_clients.unusual_whales import UWClient

    tickers = [t.strip() for t in args.tickers.split(",") if t.strip()]
    strategies = [s.strip() for s in args.strategies.split(",") if s.strip()]
    start_iso = f"{args.start}T00:00:00Z"
    end_iso = f"{args.end}T23:59:59Z"

    print(f"[backtest] tickers={tickers} strategies={strategies}")
    print(f"[backtest] window: {args.start} → {args.end}")

    t0 = time.time()
    all_outcomes: list[TradeOutcome] = []
    all_pass: dict[str, dict[str, int]] = {s: {} for s in strategies}

    with AlpacaClient() as alpaca, UWClient() as uw:
        for ticker in tickers:
            outcomes, pass_hist = backtest_one_ticker(
                ticker=ticker, alpaca=alpaca, uw=uw, cfg=cfg,
                start_iso=start_iso, end_iso=end_iso, strategies=strategies,
            )
            all_outcomes.extend(outcomes)
            for s in strategies:
                for r, n in pass_hist.get(s, {}).items():
                    all_pass[s][r] = all_pass[s].get(r, 0) + n
            print(f"  [{ticker}] +{len(outcomes)} trades, "
                    f"pass_hist S5={sum(pass_hist.get('S5',{}).values())} "
                    f"S3={sum(pass_hist.get('S3',{}).values())} "
                    f"S2={sum(pass_hist.get('S2',{}).values())}")

    print()
    print("=" * 60)
    print("BACKTEST RESULTS")
    print("=" * 60)
    metrics_per_strategy = {}
    for s in strategies:
        outcomes_s = [o for o in all_outcomes if o.strategy == s]
        m = aggregate_metrics(outcomes_s, all_pass[s])
        metrics_per_strategy[s] = m
        print(f"\n{s}:")
        print(f"  trades:       {m.n_trades}")
        print(f"  wins/losses:  {m.n_wins}/{m.n_losses}")
        print(f"  win pct:      {m.win_pct:.1f}%")
        print(f"  avg pnl:      {m.avg_pnl_atr:+.3f} ATR")
        print(f"  median pnl:   {m.median_pnl_atr:+.3f} ATR")
        print(f"  sharpe (ann): {m.sharpe:+.2f}")
        print(f"  max DD:       {m.max_drawdown_atr:.3f} ATR")
        if m.pass_reasons:
            top_pass = sorted(m.pass_reasons.items(), key=lambda kv: -kv[1])[:5]
            print(f"  top passes:   {top_pass}")

    out_path = Path(args.output or (BACKTEST_DIR / f"run_{date.today().isoformat()}.json"))
    out = {
        "meta": {
            "generated_utc": datetime.now(tz=timezone.utc).isoformat(timespec="seconds") + "Z",
            "tickers": tickers, "strategies": strategies,
            "start": args.start, "end": args.end,
            "elapsed_seconds": time.time() - t0,
            "forward_bars": FORWARD_BARS,
            "atr_target_mult": ATR_TARGET_MULT, "atr_stop_mult": ATR_STOP_MULT,
        },
        "per_strategy": {s: m.__dict__ for s, m in metrics_per_strategy.items()},
        "trades": [o.__dict__ for o in all_outcomes],
    }
    out_path.write_text(json.dumps(out, indent=2, default=str))
    print(f"\n[backtest] wrote {out_path} ({out_path.stat().st_size / 1024:.1f} KB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
