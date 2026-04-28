"""
pipeline.py — scalp 2 orchestrator.

Wires the five components together for batch-mode runs. Per DESIGN.md §7.

Batch mode:
  python3 src/pipeline.py
  - reads scalp 1 artifacts via consume_scalp1
  - fits posterior model lambdas (M2)
  - fits per-kind EV curves (M3)
  - runs phase-gate evaluation (M4)
  - simulates today's engine_v4 fires through gating + sizing (M4)
  - writes phase_state, posterior scores, ledger rows
  - renders dashboard.html

Live mode (separate file): live_loop.py polls engine_v4 every 60s.
"""
from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from consume_scalp1 import load_bundle

PROJECT_ROOT = Path(__file__).resolve().parent.parent
OUTPUT_ROOT = PROJECT_ROOT / "output"


def run() -> dict:
    t0 = time.time()
    print("[pipeline] start scalp 2 batch")

    # ── M1: load scalp 1 artifacts ────────────────────────────────────────
    bundle = load_bundle()
    print(f"[pipeline] scalp 1 bundle: {bundle.n_fires:,} fires "
          f"({bundle.n_eligible_fires:,} eligible)")
    if bundle.predictor_baseline:
        print(f"[pipeline]   scalp 1 predictor AUC = "
              f"{bundle.predictor_baseline.headline_auc:.3f} "
              f"({bundle.predictor_baseline.headline_source})")

    # ── M2: fit posterior model lambdas ───────────────────────────────────
    # TODO: from posterior_model import fit_lambdas
    # lambdas = fit_lambdas(bundle.archive_fires)
    print("[pipeline] M2 posterior_model.fit_lambdas — STUB (not yet implemented)")

    # ── M3: fit per-kind EV curves ────────────────────────────────────────
    # TODO: from ev_curve import fit_curve, save_curve
    # for kind in unique_kinds:
    #     curve = fit_curve(kind, [(post, pnl) for ...])
    #     save_curve(curve)
    print("[pipeline] M3 ev_curve.fit_curve per kind — STUB")

    # ── M4: phase gate + Kelly evaluation ─────────────────────────────────
    print("[pipeline] M4 phase_gate.evaluate + kelly_progression — STUB")

    # ── M5: render dashboard ──────────────────────────────────────────────
    OUTPUT_ROOT.mkdir(parents=True, exist_ok=True)
    placeholder = OUTPUT_ROOT / "dashboard.html"
    placeholder.write_text(
        "<!doctype html><html><body>"
        "<h1>scalp 2 dashboard — placeholder</h1>"
        "<p>M1 scaffold landed. M2-M5 not yet implemented.</p>"
        f"<p>Loaded scalp 1 bundle: {bundle.n_fires:,} fires, "
        f"{bundle.n_eligible_fires:,} eligible.</p>"
        "</body></html>"
    )
    print(f"[pipeline] wrote placeholder dashboard {placeholder}")

    print(f"[pipeline] done in {time.time() - t0:.2f}s")
    return {
        "n_fires": bundle.n_fires,
        "n_eligible": bundle.n_eligible_fires,
        "stage": "M1_scaffold",
    }


if __name__ == "__main__":
    run()
