"""
pipeline.py — end-to-end orchestrator.

Runs:
  1. Scalp loader → outcomes (cached to data/slices/outcomes_scalp.pkl)
  2. Swing loader → outcomes (no daily-bars yet, fields may be None)
  3. Stats: kind tables (underlying & option) + conf-band slice
  4. ML readiness ledger
  5. Archive copy (engine HTML → output/archive/)
  6. Render output/index.html

Idempotent. Safe to run anytime.
"""
from __future__ import annotations

import json
import pickle
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from loader_scalp import load_all as load_scalp_all
from outcome_intraday import compute_all as compute_scalp_all, dedup_signals as dedup_scalp
from loader_swing import load_all as load_swing_all
from outcome_swing import compute_all as compute_swing_all, dedup_signals as dedup_swing
from loader_lifecycle import load_all_lifecycles
from loader_historical import load_historical, archive_meta
from fire_time_enrich import enrich as enrich_fire_time
from stats import kind_table, conf_band_table, summarize_outcomes
from stats_historical import (
    top_line as hist_top_line,
    by_strategy as hist_by_strategy,
    by_ticker as hist_by_ticker,
    by_trigger as hist_by_trigger,
    by_direction as hist_by_direction,
    by_category as hist_by_category,
    by_year as hist_by_year,
    by_month as hist_by_month,
    by_exit_reason as hist_by_exit_reason,
    top_n_signals as hist_top_n,
    worst_n_signals as hist_worst_n,
)
from ml_readiness import readiness_ledger
from renderer import render_page, copy_archives, write_lifecycle_drilldowns

try:
    from alpaca_bars import fetch_daily_bars_for_swing_tickers
    HAS_ALPACA = True
except ImportError:
    HAS_ALPACA = False

try:
    from uw_flow import fetch_global_flow, aggregate_by_ticker
    HAS_UW = True
except ImportError:
    HAS_UW = False


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SLICES_ROOT = PROJECT_ROOT / "data" / "slices"
OUTPUT_ROOT = PROJECT_ROOT / "output"
SNAPSHOTS_ROOT = OUTPUT_ROOT / "snapshots"


def _cache_pickle(obj, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("wb") as f:
        pickle.dump(obj, f)


def run() -> None:
    t0 = time.time()
    print(f"[pipeline] start")

    # ── Scalp ────────────────────────────────────────────────────────────────
    scalp_sigs, bars = load_scalp_all()
    n_raw_scalp = len(scalp_sigs)
    n_dates_scalp = len({s.date_str for s in scalp_sigs})
    scalp_dedup = dedup_scalp(scalp_sigs)
    scalp_outs = compute_scalp_all(scalp_dedup, bars)
    n_enriched = enrich_fire_time(scalp_outs)
    _cache_pickle(scalp_outs, SLICES_ROOT / "outcomes_scalp.pkl")
    print(f"[pipeline] scalp: raw={n_raw_scalp} dedup={len(scalp_dedup)} outcomes={len(scalp_outs)} enriched={n_enriched}")

    # ── Swing ────────────────────────────────────────────────────────────────
    swing_sigs = load_swing_all()
    swing_dedup = dedup_swing(swing_sigs)

    daily_bars = None
    if HAS_ALPACA and swing_dedup:
        try:
            tickers = sorted({s.ticker for s in swing_dedup})
            daily_bars = fetch_daily_bars_for_swing_tickers(tickers)
            n_with_bars = sum(1 for t in tickers if daily_bars and daily_bars.get(t))
            print(f"[pipeline] alpaca: daily-bars fetched for {n_with_bars}/{len(tickers)} swing tickers")
        except Exception as e:
            print(f"[pipeline] alpaca daily-bars fetch failed: {e!r} — outcomes will be pending")

    swing_outs = compute_swing_all(swing_dedup, daily_bars)
    _cache_pickle(swing_outs, SLICES_ROOT / "outcomes_swing.pkl")
    n_matured = sum(1 for o in swing_outs if o.has_daily_bars)
    print(f"[pipeline] swing: raw={len(swing_sigs)} dedup={len(swing_dedup)} outcomes={len(swing_outs)} (matured={n_matured})")

    # ── Lifecycle ────────────────────────────────────────────────────────────
    print(f"[pipeline] loading stock lifecycles (this can take ~10s for ~200 tickers)...")
    lifecycles = load_all_lifecycles()
    _cache_pickle(lifecycles, SLICES_ROOT / "lifecycles.pkl")
    print(f"[pipeline] lifecycle: {len(lifecycles)} ticker(s)")

    # ── Stats ────────────────────────────────────────────────────────────────
    # Switched from label_option_5p5x → label_option_2regime: linear 5.5× makes T1
    # un-winnable post-cost (cost dominates leveraged gain), producing 0% wins by
    # construction. The 2-regime model uses 4×/8× leverage with 8%/6% cost depending
    # on whether MFE crosses 0.5×ATR within 30 min — closer to real option behaviour.
    kt_under = kind_table(scalp_outs,
                          label_field="label_underlying",
                          ev_field="atr_pnl_stop_first_pct")
    kt_option = kind_table(scalp_outs,
                           label_field="label_option_2regime",
                           ev_field="ev_option_2regime_stop_first")
    cbt = conf_band_table(scalp_outs,
                          label_field="label_option_2regime",
                          ev_field="ev_option_2regime_stop_first")

    overall_under = summarize_outcomes(scalp_outs,
                                       label_field="label_underlying",
                                       ev_field="atr_pnl_stop_first_pct",
                                       min_n=1)
    overall_option = summarize_outcomes(scalp_outs,
                                        label_field="label_option_2regime",
                                        ev_field="ev_option_2regime_stop_first",
                                        min_n=1)

    # Regime split (fast = MFE crosses 0.5×ATR within 30 bars; slow = otherwise)
    n_fast = sum(1 for o in scalp_outs if o.regime_used == "fast")
    n_slow = sum(1 for o in scalp_outs if o.regime_used == "slow")
    n_total_reg = n_fast + n_slow

    # Regime sanity: P(T1+ hit | regime). If fast >> slow, regime detection is real
    # signal. If gap is tiny, regime is decorative noise — 0.5×ATR within 30 bars
    # might just mean "moved at all" not "moved with conviction".
    def _hit_t1_plus(o) -> bool:
        # Use stop-first (conservative) ATR ladder. "TARGET_T1/T2/T3" all count.
        ev = o.atr_first_event_stop_first
        return bool(ev and ev.startswith("TARGET_"))
    n_fast_t1 = sum(1 for o in scalp_outs if o.regime_used == "fast" and _hit_t1_plus(o))
    n_slow_t1 = sum(1 for o in scalp_outs if o.regime_used == "slow" and _hit_t1_plus(o))
    p_fast_t1 = (100.0 * n_fast_t1 / n_fast) if n_fast else None
    p_slow_t1 = (100.0 * n_slow_t1 / n_slow) if n_slow else None
    spread_pp = None
    if p_fast_t1 is not None and p_slow_t1 is not None:
        spread_pp = p_fast_t1 - p_slow_t1
    if spread_pp is None:
        verdict = "no data"
    elif spread_pp >= 20:
        verdict = "real edge"
    elif spread_pp < 10:
        verdict = "decorative"
    else:
        verdict = "suspended judgment"

    regime_split = {
        "n_fast": n_fast,
        "n_slow": n_slow,
        "pct_fast": (100.0 * n_fast / n_total_reg) if n_total_reg else None,
        "pct_slow": (100.0 * n_slow / n_total_reg) if n_total_reg else None,
        "leverage_fast": 8.0,
        "leverage_slow": 4.0,
        "cost_fast_pct": 6.0,
        "cost_slow_pct": 8.0,
        # Regime sanity — does fast-regime tag correlate with actually hitting T1?
        "n_fast_t1_plus": n_fast_t1,
        "n_slow_t1_plus": n_slow_t1,
        "p_fast_t1_plus": p_fast_t1,
        "p_slow_t1_plus": p_slow_t1,
        "regime_t1_spread_pp": spread_pp,
        "regime_verdict": verdict,
    }

    # Best/worst kind by underlying win% (n ≥ 5)
    eligible = [(k, s) for k, s in kt_under.items() if not s.suppressed and s.win_pct is not None]
    best = None
    worst = None
    if eligible:
        eligible.sort(key=lambda kv: -kv[1].win_pct)
        best = {"kind": eligible[0][0], "win": eligible[0][1].win_pct, "n": eligible[0][1].n}
        worst = {"kind": eligible[-1][0], "win": eligible[-1][1].win_pct, "n": eligible[-1][1].n}

    # Fire-time context slices — does flow/news/conviction at fire time matter?
    def _slice(pred):
        subset = [o for o in scalp_outs if pred(o)]
        if not subset:
            return None
        return {
            "n": len(subset),
            "under": summarize_outcomes(subset, label_field="label_underlying",
                                        ev_field="atr_pnl_stop_first_pct", min_n=1),
            "opt": summarize_outcomes(subset, label_field="label_option_2regime",
                                      ev_field="ev_option_2regime_stop_first", min_n=1),
        }
    ctx_slices = {
        "all": _slice(lambda o: True),
        "with_flow": _slice(lambda o: o.at_fire_has_whale_flow is True),
        "no_flow": _slice(lambda o: o.at_fire_has_whale_flow is False),
        "with_news": _slice(lambda o: o.at_fire_has_news is True),
        "no_news": _slice(lambda o: o.at_fire_has_news is False),
        "with_conv": _slice(lambda o: o.at_fire_has_conviction is True),
        "no_conv": _slice(lambda o: o.at_fire_has_conviction is False),
        "flow_and_conv": _slice(lambda o: o.at_fire_has_whale_flow and o.at_fire_has_conviction),
    }

    # ── ML readiness ─────────────────────────────────────────────────────────
    ledger = readiness_ledger(scalp_outs)
    ml_ready = sum(1 for r in ledger if r.ready)

    # ── Archive copy ─────────────────────────────────────────────────────────
    archive_items = copy_archives()
    print(f"[pipeline] archive: {len(archive_items)} HTML page(s) mirrored")

    # ── Live UW options flow ─────────────────────────────────────────────────
    uw_aggs: dict = {}
    n_uw_alerts = 0
    if HAS_UW:
        try:
            alerts = fetch_global_flow(lookback_hours=24)
            n_uw_alerts = len(alerts)
            uw_aggs = aggregate_by_ticker(alerts)
            print(f"[pipeline] uw flow: {n_uw_alerts} alerts → {len(uw_aggs)} ticker(s)")
        except Exception as e:
            print(f"[pipeline] uw flow fetch failed: {e!r}")

    # ── Per-ticker lifecycle drilldown pages ─────────────────────────────────
    # Generate one page for every ticker the engine tracked today (≥5 snaps),
    # ordered TRADE-mode first by snapshot density. ~30KB each, ~5s for 200.
    drilldown_set = write_lifecycle_drilldowns(lifecycles, max_pages=400, uw_aggs=uw_aggs)
    print(f"[pipeline] lifecycle drilldowns: {len(drilldown_set)} page(s) written")

    # ── Historical archive (engine_scalp/scalp_all_signals.json) ─────────────
    # Multi-year backtest archive, ~5K signals across 7 strategies and 38 tickers.
    # Aggregated cuts feed the Historical tab.
    t_hist = time.time()
    hist_meta = archive_meta()
    hist_sigs = load_historical()
    hist_tl = hist_top_line(hist_sigs)
    hist_strategy = hist_by_strategy(hist_sigs)
    hist_trigger = hist_by_trigger(hist_sigs)
    hist_ticker = [r for r in hist_by_ticker(hist_sigs) if r.n >= 5]   # n<5 hidden
    hist_direction = hist_by_direction(hist_sigs)
    hist_category = hist_by_category(hist_sigs)
    hist_year = hist_by_year(hist_sigs)
    hist_month = hist_by_month(hist_sigs)
    hist_month_recent = hist_month[-36:] if len(hist_month) > 36 else hist_month
    hist_exit_reason = hist_by_exit_reason(hist_sigs)
    hist_top = hist_top_n(hist_sigs, n=25)
    hist_worst = hist_worst_n(hist_sigs, n=25)
    print(f"[pipeline] historical: {len(hist_sigs)} signals, "
          f"{hist_tl.get('n_strategies', 0)} strategies, "
          f"{hist_tl.get('n_tickers', 0)} tickers, "
          f"{hist_tl.get('n_dates', 0)} dates, "
          f"win {hist_tl.get('win_pct') or 0:.1f}% in {time.time()-t_hist:.2f}s")

    # ── 7-Year Backtest run (engine_scalp archive + bar-based resim) ─────────
    # Loads the most recent run JSON written by seven_year_backtest.py.
    # Build it once with: python3 src/seven_year_backtest.py
    seven_year = {}
    seven_year_dir = PROJECT_ROOT / "data" / "seven_year_backtest"
    if seven_year_dir.exists():
        runs = sorted(seven_year_dir.glob("run_*.json"))
        if runs:
            try:
                seven_year = json.loads(runs[-1].read_text())
                print(f"[pipeline] 7y backtest: loaded {runs[-1].name} "
                      f"({seven_year.get('topline_archived', {}).get('n_decided', 0)} decided, "
                      f"{seven_year.get('topline_resim', {}).get('n_resimmed', 0)} resimmed)")
            except Exception as e:
                print(f"[pipeline] 7y backtest load failed: {e!r}")
                seven_year = {}
        else:
            print(f"[pipeline] 7y backtest: no run_*.json in {seven_year_dir}")

    # ── Alignment-quintile fallback (computed from per_signal if 7y JSON
    # doesn't have it yet — graceful degradation while a re-run is pending) ───
    if seven_year:
        rs = seven_year.get("regime_sanity") or {}
        if not (rs.get("alignment_quintile_probe") or {}).get("quintiles"):
            ps = seven_year.get("per_signal") or []
            aligned = [s for s in ps
                       if s.get("regime") in ("fast", "slow")
                       and s.get("prior_5bar_alignment") is not None]
            aligned.sort(key=lambda s: s["prior_5bar_alignment"])
            quintile_rows = []
            shape = "no data"
            if len(aligned) >= 25:
                n_a = len(aligned)
                qsize = n_a // 5
                for q in range(5):
                    lo = q * qsize
                    hi = (q + 1) * qsize if q < 4 else n_a
                    bucket = aligned[lo:hi]
                    n_b = len(bucket)
                    if not n_b:
                        continue
                    n_b_slow = sum(1 for r in bucket if r.get("regime") == "slow")
                    n_b_t1 = sum(1 for r in bucket
                                 if (r.get("atr_event") or "").startswith("TARGET_"))
                    quintile_rows.append({
                        "quintile": q + 1,
                        "n": n_b,
                        "alignment_lo": bucket[0]["prior_5bar_alignment"],
                        "alignment_hi": bucket[-1]["prior_5bar_alignment"],
                        "alignment_median": bucket[n_b // 2]["prior_5bar_alignment"],
                        "n_slow": n_b_slow,
                        "n_t1_plus": n_b_t1,
                        "p_slow": 100.0 * n_b_slow / n_b,
                        "p_t1_plus": 100.0 * n_b_t1 / n_b,
                    })
                slow_pcts = [r["p_slow"] for r in quintile_rows]
                if len(slow_pcts) >= 5:
                    diffs = [slow_pcts[i+1] - slow_pcts[i] for i in range(4)]
                    n_pos = sum(1 for d in diffs if d > 1.0)
                    n_neg = sum(1 for d in diffs if d < -1.0)
                    spread_5 = max(slow_pcts) - min(slow_pcts)
                    if spread_5 < 5.0:
                        shape = "flat — alignment doesn't materially affect P(slow)"
                    elif n_pos >= 3 and n_neg <= 1:
                        shape = ("monotonic increasing — with-trend entries are more slow "
                                 "(engine_v4 fires at move-exhaustion; LR sign was right)")
                    elif n_neg >= 3 and n_pos <= 1:
                        shape = ("monotonic decreasing — counter-aligned entries are more slow "
                                 "(naive intuition was right; LR sign was misleading)")
                    elif slow_pcts[0] > slow_pcts[2] and slow_pcts[4] > slow_pcts[2]:
                        shape = ("U-shaped — both extremes have more slow; neutral alignment "
                                 "is the sweet spot (LR was averaging an interaction)")
                    elif slow_pcts[0] < slow_pcts[2] and slow_pcts[4] < slow_pcts[2]:
                        shape = "inverse-U — neutral alignment has more slow; both extremes are safer"
                    else:
                        shape = "non-monotonic, non-U — mixed pattern"
            rs["alignment_quintile_probe"] = {
                "quintiles": quintile_rows,
                "shape": shape,
                "n_eligible": len(aligned),
                "_source": "computed_from_per_signal_in_pipeline",
            }
            seven_year["regime_sanity"] = rs
            print(f"[pipeline] alignment-quintile probe: {len(quintile_rows)} quintiles, shape='{shape[:60]}...'")

    # ── Pre-trade slow-regime predictor (sklearn) ────────────────────────────
    # Loads the most recent run JSON from pretrade_slow_predictor.py.
    # Build with: python3 src/pretrade_slow_predictor.py
    pretrade_predictor = {}
    predictor_dir = PROJECT_ROOT / "data" / "pretrade_slow_predictor"
    if predictor_dir.exists():
        runs = sorted(predictor_dir.glob("run_*.json"))
        if runs:
            try:
                pretrade_predictor = json.loads(runs[-1].read_text())
                hl = pretrade_predictor.get("headline", {})
                auc = hl.get("auc")
                src = hl.get("source")
                verd = hl.get("verdict", "—")
                if auc is not None:
                    print(f"[pipeline] pretrade predictor: loaded {runs[-1].name} "
                          f"(AUC={auc:.3f} from {src} → {verd})")
                else:
                    print(f"[pipeline] pretrade predictor: loaded {runs[-1].name} (no AUC)")
            except Exception as e:
                print(f"[pipeline] pretrade predictor load failed: {e!r}")
                pretrade_predictor = {}
        else:
            print(f"[pipeline] pretrade predictor: no run_*.json in {predictor_dir}")

    # ── Build context dict ───────────────────────────────────────────────────
    swing_outs_for_render = swing_outs   # SwingOutcome
    swing_fires_for_render = swing_dedup  # SwingSignal (richer for ledger view)

    ctx = {
        "n_scalp_raw": n_raw_scalp,
        "n_scalp_outcomes": len(scalp_outs),
        "n_dates_scalp": n_dates_scalp,
        "n_swing": len(swing_outs_for_render),
        "swing_fires": swing_fires_for_render,

        "scalp_kind_underlying": list(kt_under.items()),
        "scalp_kind_option": list(kt_option.items()),
        "scalp_conf_band": cbt,

        "overall_win_underlying": overall_under.win_pct,
        "overall_win_option": overall_option.win_pct,
        "overall_ev_under": overall_under.ev_mean_pct,
        "overall_ev_opt": overall_option.ev_mean_pct,

        "ml_ledger": ledger,
        "ml_ready_kinds": ml_ready,
        "ml_total_kinds": len(ledger),

        "best_kind": best,
        "worst_kind": worst,

        "archive_items": archive_items,
        "lifecycles": lifecycles,
        "lifecycle_drilldown_set": drilldown_set,
        "uw_aggs": uw_aggs,
        "uw_n_alerts": n_uw_alerts,

        # Fire-time enrichment summary
        "n_fire_with_flow": sum(1 for o in scalp_outs if o.at_fire_has_whale_flow),
        "n_fire_with_news": sum(1 for o in scalp_outs if o.at_fire_has_news),
        "n_fire_with_conviction": sum(1 for o in scalp_outs if o.at_fire_has_conviction),
        "n_fire_enriched": n_enriched,
        "ctx_slices": ctx_slices,
        "regime_split": regime_split,

        # Historical archive (engine_scalp)
        "hist_meta": hist_meta,
        "hist_top_line": hist_tl,
        "hist_by_strategy": hist_strategy,
        "hist_by_trigger": hist_trigger,
        "hist_by_ticker": hist_ticker,
        "hist_by_direction": hist_direction,
        "hist_by_category": hist_category,
        "hist_by_year": hist_year,
        "hist_by_month": hist_month_recent,
        "hist_by_exit_reason": hist_exit_reason,
        "hist_top_winners": hist_top,
        "hist_worst_losers": hist_worst,

        # 7-year backtest payload (whole JSON; renderer pulls what it needs)
        "seven_year": seven_year,
        # Pre-trade slow-regime predictor (sklearn JSON)
        "pretrade_predictor": pretrade_predictor,
    }

    # ── Render ───────────────────────────────────────────────────────────────
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    page = render_page(ctx)
    out_path = OUTPUT_ROOT / "index.html"
    out_path.write_text(page)
    print(f"[pipeline] wrote {out_path} ({len(page):,} bytes)")

    # ── Snapshot mode ────────────────────────────────────────────────────────
    if "--snapshot" in sys.argv:
        write_snapshot(ctx, page)

    print(f"[pipeline] done in {time.time() - t0:.2f}s")
    print(f"\n  open file://{out_path}\n")


# ──────────────────────────────────────────────────────────────────────────────
# Snapshot writer + diff-vs-prior helper (for lunchtime/EOD checkpoints)
# ──────────────────────────────────────────────────────────────────────────────


def _ctx_summary(ctx: dict) -> dict:
    """Tiny JSON-serializable digest used for diffing snapshots."""
    return {
        "n_scalp_outcomes": ctx["n_scalp_outcomes"],
        "n_scalp_raw": ctx["n_scalp_raw"],
        "n_swing": ctx["n_swing"],
        "n_dates_scalp": ctx["n_dates_scalp"],
        "overall_win_underlying": ctx["overall_win_underlying"],
        "overall_win_option": ctx["overall_win_option"],
        "overall_ev_under": ctx["overall_ev_under"],
        "overall_ev_opt": ctx["overall_ev_opt"],
        "ml_ready_kinds": ctx["ml_ready_kinds"],
        "ml_total_kinds": ctx["ml_total_kinds"],
        "best_kind": ctx["best_kind"],
        "worst_kind": ctx["worst_kind"],
        "kind_table_underlying": {
            k: {"n": s.n, "win_pct": s.win_pct, "ev_mean_pct": s.ev_mean_pct}
            for k, s in ctx["scalp_kind_underlying"]
        },
        "kind_table_option": {
            k: {"n": s.n, "win_pct": s.win_pct, "ev_mean_pct": s.ev_mean_pct}
            for k, s in ctx["scalp_kind_option"]
        },
    }


def write_snapshot(ctx: dict, html_page: str) -> None:
    """Save a per-checkpoint copy of the dashboard + a JSON digest, then diff against prior."""
    now = datetime.now(timezone.utc)
    date_str = now.strftime("%Y-%m-%d")
    time_str = now.strftime("%H%M")

    snap_dir = SNAPSHOTS_ROOT / date_str / time_str
    snap_dir.mkdir(parents=True, exist_ok=True)

    (snap_dir / "index.html").write_text(html_page)
    digest = _ctx_summary(ctx)
    (snap_dir / "digest.json").write_text(json.dumps(digest, default=str, indent=2))

    print(f"[pipeline] snapshot → {snap_dir.relative_to(PROJECT_ROOT)}")

    # Find prior snapshot (most recent before this one) for diff
    prior_dirs = sorted(
        (p for p in SNAPSHOTS_ROOT.glob("*/*") if p.is_dir() and p != snap_dir),
        key=lambda p: p.stat().st_mtime, reverse=True
    )
    if prior_dirs:
        prior = prior_dirs[0]
        prior_digest_path = prior / "digest.json"
        if prior_digest_path.exists():
            try:
                prior_d = json.loads(prior_digest_path.read_text())
                _print_diff(prior_d, digest, prior, snap_dir)
            except Exception as e:
                print(f"[pipeline] diff skipped: {e}")


def _print_diff(prev: dict, curr: dict, prev_dir: Path, curr_dir: Path) -> None:
    """Print a short delta between two snapshot digests."""
    print(f"\n[pipeline] DIFF vs {prev_dir.relative_to(PROJECT_ROOT)}")
    print("─" * 70)

    def _delta(a, b):
        if a is None or b is None:
            return ""
        return f"  Δ {b - a:+.2f}"

    def _row(label, prev_v, curr_v, fmt="{:.2f}", suffix=""):
        a = prev_v if prev_v is not None else 0
        b = curr_v if curr_v is not None else 0
        a_s = (fmt.format(prev_v) + suffix) if prev_v is not None else "—"
        b_s = (fmt.format(curr_v) + suffix) if curr_v is not None else "—"
        d = _delta(prev_v, curr_v)
        print(f"  {label:30s} {a_s:>10s} → {b_s:>10s}{d}")

    _row("scalp fires (raw)",      prev.get("n_scalp_raw"),       curr.get("n_scalp_raw"),       "{:d}",   "")
    _row("scalp fires (deduped)",  prev.get("n_scalp_outcomes"),  curr.get("n_scalp_outcomes"),  "{:d}",   "")
    _row("swing fires",            prev.get("n_swing"),           curr.get("n_swing"),           "{:d}",   "")
    _row("trading days",           prev.get("n_dates_scalp"),     curr.get("n_dates_scalp"),     "{:d}",   "")
    _row("win % (underlying)",     prev.get("overall_win_underlying"), curr.get("overall_win_underlying"), "{:.1f}", "%")
    _row("win % (option 5.5×)",    prev.get("overall_win_option"),     curr.get("overall_win_option"),     "{:.1f}", "%")
    _row("EV/fire (underlying)",   prev.get("overall_ev_under"), curr.get("overall_ev_under"), "{:+.2f}", "%")
    _row("EV/fire (option 5.5×)",  prev.get("overall_ev_opt"),   curr.get("overall_ev_opt"),   "{:+.2f}", "%")
    _row("ML-ready kinds",         prev.get("ml_ready_kinds"),   curr.get("ml_ready_kinds"),   "{:d}",   "")

    # New kinds appearing
    prev_kinds = set((prev.get("kind_table_underlying") or {}).keys())
    curr_kinds = set((curr.get("kind_table_underlying") or {}).keys())
    added = curr_kinds - prev_kinds
    removed = prev_kinds - curr_kinds
    if added:
        print(f"\n  + new kinds appeared: {', '.join(sorted(added))}")
    if removed:
        print(f"  - kinds disappeared:  {', '.join(sorted(removed))}")
    print()


if __name__ == "__main__":
    run()
