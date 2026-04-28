"""
consume_scalp1.py — read scalp 1 artifacts as typed input for scalp 2.

Strict contract per DESIGN.md §5. We never modify scalp 1 outputs; we read them
as ground truth. Three sources:
  - data/seven_year_backtest/run_*.json       — per-fire resim w/ regime + features
  - data/pretrade_slow_predictor/run_*.json   — sklearn baseline (AUC, importances)
  - data/slices/outcomes_scalp.pkl            — pickled Outcome records (per-fire)

This module is the ONLY place scalp 2 touches scalp 1 paths. Any future move of
the audit project (e.g. to a separate repo) only requires updating SCALP1_ROOT.
"""
from __future__ import annotations

import json
import pickle
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

# Resolve scalp 1 root by climbing one level then over to scalp_backtest/
# /claude/gemini /scalp2/src/consume_scalp1.py → /claude/gemini /scalp_backtest/
SCALP1_ROOT = (Path(__file__).resolve().parent.parent.parent / "scalp_backtest").resolve()
SEVEN_YEAR_DIR = SCALP1_ROOT / "data" / "seven_year_backtest"
PREDICTOR_DIR = SCALP1_ROOT / "data" / "pretrade_slow_predictor"
SLICES_DIR = SCALP1_ROOT / "data" / "slices"


# ──────────────────────────────────────────────────────────────────────────────
# Typed records
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class ArchiveFire:
    """One per-signal record from scalp 1's seven_year_backtest run JSON."""
    date: Optional[str]
    ticker: Optional[str]
    side: Optional[str]                  # CALL | PUT
    strategy: Optional[str]
    archived_outcome: Optional[str]      # WIN | LOSS | TIME_STOP | etc
    archived_pnl_pct: Optional[float]
    resim_und_pct: Optional[float]
    resim_opt_pct: Optional[float]
    resim_event: Optional[str]
    atr_14_pct: Optional[float]
    # Regime-related
    regime: Optional[str]                # fast | slow
    atr_event: Optional[str]             # TARGET_T1 | TARGET_T2 | TARGET_T3 | STOP | EOD
    atr_t1_plus_hit: Optional[bool]
    atr_bars_to_event: Optional[int]
    mfe_pct: Optional[float]
    # Pre-trade features
    minute_of_day: Optional[int]
    prior_5bar_return_pct: Optional[float]
    prior_30bar_realized_vol_pct: Optional[float]
    prior_5bar_alignment: Optional[float]
    prior_5bar_volume_z: Optional[float]
    skip_reason: Optional[str] = None

    @classmethod
    def from_dict(cls, d: dict) -> "ArchiveFire":
        return cls(
            date=d.get("date"),
            ticker=d.get("ticker"),
            side=d.get("side"),
            strategy=d.get("strategy"),
            archived_outcome=d.get("archived_outcome"),
            archived_pnl_pct=d.get("archived_pnl_pct"),
            resim_und_pct=d.get("resim_und_pct"),
            resim_opt_pct=d.get("resim_opt_pct"),
            resim_event=d.get("resim_event"),
            atr_14_pct=d.get("atr_14_pct"),
            regime=d.get("regime"),
            atr_event=d.get("atr_event"),
            atr_t1_plus_hit=d.get("atr_t1_plus_hit"),
            atr_bars_to_event=d.get("atr_bars_to_event"),
            mfe_pct=d.get("mfe_pct"),
            minute_of_day=d.get("minute_of_day"),
            prior_5bar_return_pct=d.get("prior_5bar_return_pct"),
            prior_30bar_realized_vol_pct=d.get("prior_30bar_realized_vol_pct"),
            prior_5bar_alignment=d.get("prior_5bar_alignment"),
            prior_5bar_volume_z=d.get("prior_5bar_volume_z"),
            skip_reason=d.get("skip_reason"),
        )


@dataclass
class PredictorBaseline:
    """sklearn baseline metrics from scalp 1's pretrade_slow_predictor."""
    headline_auc: Optional[float]
    headline_source: Optional[str]
    headline_verdict: Optional[str]
    lr_cv_mean_auc: Optional[float]
    gbm_cv_mean_auc: Optional[float]
    yh_lr_test_auc: Optional[float]
    yh_gbm_test_auc: Optional[float]
    feature_keys: list[str]
    lr_coefficients: dict[str, float]
    gbm_perm_importance: dict[str, float]
    median_imputed_values: dict[str, float]
    n_total: int
    n_slow: int
    n_fast: int
    base_rate_slow: float


@dataclass
class Scalp1Bundle:
    """Everything scalp 2 reads from scalp 1, in one container."""
    archive_fires: list[ArchiveFire] = field(default_factory=list)
    predictor_baseline: Optional[PredictorBaseline] = None
    regime_sanity: dict = field(default_factory=dict)
    walk_forward_momentum: dict = field(default_factory=dict)
    seven_year_run_path: Optional[Path] = None
    predictor_run_path: Optional[Path] = None
    loaded_utc: str = ""

    @property
    def n_fires(self) -> int:
        return len(self.archive_fires)

    @property
    def n_eligible_fires(self) -> int:
        """Fires with regime + features populated (suitable for posterior model)."""
        return sum(
            1 for f in self.archive_fires
            if f.regime in ("fast", "slow") and f.atr_t1_plus_hit is not None
        )


# ──────────────────────────────────────────────────────────────────────────────
# Loaders
# ──────────────────────────────────────────────────────────────────────────────


def _latest_run(dir_: Path) -> Optional[Path]:
    if not dir_.exists():
        return None
    runs = sorted(dir_.glob("run_*.json"))
    return runs[-1] if runs else None


def load_seven_year_run() -> tuple[Optional[Path], dict]:
    """Returns (path, parsed_dict). Empty dict if no run found."""
    p = _latest_run(SEVEN_YEAR_DIR)
    if p is None:
        return None, {}
    return p, json.loads(p.read_text())


def load_predictor_run() -> tuple[Optional[Path], dict]:
    p = _latest_run(PREDICTOR_DIR)
    if p is None:
        return None, {}
    return p, json.loads(p.read_text())


def parse_predictor_baseline(raw: dict) -> Optional[PredictorBaseline]:
    if not raw:
        return None
    headline = raw.get("headline") or {}
    lr_cv = raw.get("lr_cv") or {}
    gbm_cv = raw.get("gbm_cv") or {}
    yh = raw.get("year_holdout") or {}
    fi = raw.get("feature_importances") or {}
    ds = raw.get("dataset") or {}
    return PredictorBaseline(
        headline_auc=headline.get("auc"),
        headline_source=headline.get("source"),
        headline_verdict=headline.get("verdict"),
        lr_cv_mean_auc=lr_cv.get("mean_auc"),
        gbm_cv_mean_auc=gbm_cv.get("mean_auc"),
        yh_lr_test_auc=(yh.get("lr_test") or {}).get("auc"),
        yh_gbm_test_auc=(yh.get("gbm_test") or {}).get("auc"),
        feature_keys=ds.get("feature_keys") or list(((fi.get("lr") or {}).get("coefficients") or {}).keys()),
        lr_coefficients=(fi.get("lr") or {}).get("coefficients") or {},
        gbm_perm_importance=(fi.get("gbm") or {}).get("permutation_importance_mean") or {},
        median_imputed_values=ds.get("median_imputed_values") or {},
        n_total=ds.get("n_total", 0),
        n_slow=ds.get("n_slow", 0),
        n_fast=ds.get("n_fast", 0),
        base_rate_slow=ds.get("base_rate_slow", 0.0),
    )


def load_outcomes_pickle() -> Optional[list]:
    """Load scalp 1's outcomes_scalp.pkl. Returns None if file missing.

    Note: depends on scalp_backtest.src being importable for unpickling
    (the pickle references its Outcome dataclass). If scalp 2 needs to run
    standalone, prefer the per_signal JSON path instead.
    """
    p = SLICES_DIR / "outcomes_scalp.pkl"
    if not p.exists():
        return None
    try:
        with p.open("rb") as f:
            return pickle.load(f)
    except Exception:
        # If unpickling fails (likely missing scalp_backtest src on path),
        # fall back to None — caller should use load_seven_year_run() per_signal.
        return None


def load_bundle() -> Scalp1Bundle:
    """One-shot loader — returns everything scalp 2 needs from scalp 1."""
    sy_path, sy_raw = load_seven_year_run()
    pred_path, pred_raw = load_predictor_run()
    fires = [ArchiveFire.from_dict(d) for d in (sy_raw.get("per_signal") or [])]
    return Scalp1Bundle(
        archive_fires=fires,
        predictor_baseline=parse_predictor_baseline(pred_raw),
        regime_sanity=sy_raw.get("regime_sanity") or {},
        walk_forward_momentum=sy_raw.get("walk_forward_momentum") or {},
        seven_year_run_path=sy_path,
        predictor_run_path=pred_path,
        loaded_utc=datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
    )


# ──────────────────────────────────────────────────────────────────────────────
# CLI sanity
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    print(f"[consume_scalp1] SCALP1_ROOT = {SCALP1_ROOT}")
    print(f"[consume_scalp1]   exists: {SCALP1_ROOT.exists()}")

    bundle = load_bundle()
    print(f"[consume_scalp1] loaded at {bundle.loaded_utc}")
    print(f"[consume_scalp1]   seven_year run: {bundle.seven_year_run_path}")
    print(f"[consume_scalp1]   predictor run:  {bundle.predictor_run_path}")
    print(f"[consume_scalp1]   archive fires: {bundle.n_fires:,}")
    print(f"[consume_scalp1]   eligible fires (regime+t1+hit populated): {bundle.n_eligible_fires:,}")

    if bundle.predictor_baseline:
        pb = bundle.predictor_baseline
        print(f"[consume_scalp1] predictor baseline:")
        print(f"  headline AUC: {pb.headline_auc:.4f} (source={pb.headline_source})")
        print(f"  LR CV mean:   {pb.lr_cv_mean_auc:.4f}")
        print(f"  GBM CV mean:  {pb.gbm_cv_mean_auc:.4f}")
        print(f"  dataset: n={pb.n_total:,} (slow={pb.n_slow:,} fast={pb.n_fast:,}, base rate {pb.base_rate_slow*100:.1f}%)")
        print(f"  features: {pb.feature_keys}")

    if bundle.regime_sanity:
        rs = bundle.regime_sanity
        sp = rs.get("spread_pp")
        verd = rs.get("verdict")
        print(f"[consume_scalp1] regime sanity (multi-year): spread={sp:+.1f}pp → {verd}")
        sb = rs.get("slow_breakdown") or {}
        if sb:
            mech = sb.get("mechanism_verdict")
            print(f"[consume_scalp1]   slow mechanism: {mech}")
        aq = rs.get("alignment_quintile_probe") or {}
        if aq:
            print(f"[consume_scalp1]   alignment quintile shape: {aq.get('shape')}")
