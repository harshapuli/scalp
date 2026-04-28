# Scalp 2 — Edge-Centric Trading System v2.2

> **Successor to scalp 1 (`scalp_backtest/`).** Scalp 1 was a post-hoc audit of every
> engine_v4 fire and produced labels + features. Scalp 2 *consumes* those artifacts
> and adds the v2.2 phase-gate state machine, posterior models, EV-curve picker,
> and Kelly progression that scalp 1 explicitly excluded.

## 0. Status

**v0.1 scaffold.** This document fixes the scope and the inputs from scalp 1.
Implementation is staged across milestones M1–M5 below. No code shipped yet
beyond the scaffold modules.

This DESIGN.md is **inferred from scalp 1's references to v2.2** in
`scalp_backtest/DESIGN.md` (lines 4, 60, 231, 784) plus the validated findings
from scalp 1's pretrade predictor + regime sanity work. The original
`trading_system_lifecycle.docx` referenced by scalp 1's design doc is not on
disk in the search paths; if it surfaces, this DESIGN.md should be reconciled
against it and any drift documented in §10.

---

## 1. Scope statement

> **Scalp 2 builds the production gating + sizing layer that decides which
> engine_v4 fires become live positions and how big.** Scalp 1 told us *which
> labels and features matter*; scalp 2 turns them into a phase-gate that
> approves trades, a posterior model that scores them, an EV-curve picker
> that sizes them, and a Kelly progression that scales risk over time.

**Read-only inputs** (no writes back to engine_v4 or scalp_backtest):
- `scalp_backtest/data/seven_year_backtest/run_*.json` — per-fire resim with regime + pre-trade features
- `scalp_backtest/data/pretrade_slow_predictor/run_*.json` — sklearn baseline AUC + feature importances
- `scalp_backtest/data/slices/outcomes_scalp.pkl` — per-fire outcome records
- `engine_v4/data/intraday/<DATE>/breakouts_validated.jsonl` — live fires (read-only mirror)
- `engine_v4/data/intraday/<DATE>/breakouts_coil.jsonl` — coil/ramp fires (read-only mirror)

**Outputs** (scalp 2 owns these):
- `data/phase_state/<DATE>/state.json` — current phase per signal kind (`allow_s5_trade`)
- `data/posterior/<DATE>/scores.jsonl` — per-fire posterior probability of T1+ hit
- `data/ev_curves/<KIND>.json` — fitted EV curves per signal kind for sizing
- `data/kelly_ledger/<DATE>/positions.jsonl` — sized positions with Kelly fractions
- `output/dashboard.html` — phase-gate + sizing dashboard (separate from scalp 1's audit page)

**Boundary** (same as scalp 1):
- `engine_v4/`, `swing_engine_v2/`, `scalp_backtest/` are READ-ONLY.
- We do NOT patch any existing engine.
- We do NOT execute live trades. Scalp 2 produces *gating decisions* and *sizing recommendations*; an external execution layer would consume them.

---

## 2. What we're NOT doing

- **Not re-deriving labels.** Scalp 1's per-fire labels (regime, T1+ hit, atr_event) are taken as ground truth. If labels need refining, that's a scalp 1 change pushed back through the audit project, not a scalp 2 modification.
- **Not building a new ML model from scratch.** Scalp 2's posterior model consumes scalp 1's pre-trade features (alignment, vol-z, ATR, regime) plus richer context features added here (UW flow at fire, news LLM score, GEX proximity). The architecture is a thin Bayesian update on top of scalp 1's GBM baseline.
- **Not touching engine_v4.** All gating happens at the *consumer* layer. Engine_v4 keeps firing as it does today; scalp 2 decides whether to act on each fire.
- **Not running live execution.** Output is `kelly_ledger/positions.jsonl`. An execution layer (Alpaca/IBKR/whatever) is a separate concern.
- **Not implementing the `S5 gamma reversal` signal as a new detector.** That's an engine_v4 concern. Scalp 2 *gates* engine_v4's existing fires; it does not produce new ones. (If S5 gamma reversal turns out to mean "the gating state where we allow gamma-reversal-style scalps," that lives in §4 phase-gate definitions.)

---

## 3. Architecture — five components

```
┌─────────────────────────────────────────────────────────────────────┐
│  SCALP 2 — Edge-Centric Trading System v2.2                         │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  [SCALP 1 OUTPUTS]──────┐                                           │
│  per_signal JSONs       │                                           │
│  outcomes pickles       ▼                                           │
│  pretrade_predictor   (1) Phase Gate State Machine                  │
│                         │   - allow_s5_trade per kind               │
│                         │   - lift threshold ≥1.15× over baseline   │
│                         │   - daily/weekly aggregation              │
│                         ▼                                           │
│  [ENGINE_V4 LIVE]─────► (2) Posterior Model                         │
│  breakouts_*.jsonl      │   - prior: scalp 1 GBM score              │
│                         │   - likelihood: real-time UW flow / news  │
│                         │   - posterior P(T1+ | features)           │
│                         ▼                                           │
│                       (3) EV Curve Picker                           │
│                         │   - per-kind EV curves from scalp 1       │
│                         │   - select expected payoff at posterior   │
│                         ▼                                           │
│                       (4) Kelly Progression                         │
│                         │   - fractional Kelly = (p-q)/(b)*scale    │
│                         │   - scale ramps with rolling out-of-sample│
│                         │     win rate (full Kelly only at maturity)│
│                         ▼                                           │
│                       (5) Position Ledger                           │
│                         │   - one row per APPROVED+SIZED fire       │
│                         │   - dashboard reads this for live status  │
│                                                                     │
└─────────────────────────────────────────────────────────────────────┘
```

### 3a. Component (1) — Phase Gate State Machine

A state machine per signal kind. States: `OBSERVE` → `PAPER` → `LIVE_SMALL` → `LIVE_FULL`.

Transitions controlled by:
- **Lift over baseline.** Rolling 30-day live win% must beat scalp 1's archive baseline by ≥1.15× for the kind to advance.
- **Sample size.** N ≥ 30 fires per kind in the rolling window before any advancement.
- **Drawdown brake.** Max drawdown over rolling 7 days > 2× expected EV → state regresses by one step.

Output: `phase_state/<DATE>/state.json` keyed by signal kind.

`allow_s5_trade(kind)` returns True iff state ∈ {`LIVE_SMALL`, `LIVE_FULL`}.

### 3b. Component (2) — Posterior Model

Bayesian update on top of scalp 1's GBM baseline.

```
prior = GBM(scalp1_pretrade_features)        # ~0.50 base rate, AUC 0.61
likelihood_uw = UW_flow_score_at_fire        # +/- bumps based on bullish/bearish flow
likelihood_news = news_LLM_score_at_fire     # +/- bumps based on sentiment + recency
likelihood_gex = GEX_proximity_score         # gamma exposure within 1 ATR of strike

posterior = sigmoid(logit(prior) + λ_uw·likelihood_uw + λ_news·... + λ_gex·...)
```

Hyperparameters λ_* fit by maximum likelihood on scalp 1's archive resim
(treating recorded `recorded_exit_reason` as labels for in-sample fits, then
out-of-sample-validating against the 2026 holdout used by scalp 1's predictor).

Output: `posterior/<DATE>/scores.jsonl` — one row per live fire with `posterior` field.

### 3c. Component (3) — EV Curve Picker

Per signal kind, fit an EV curve from scalp 1's archive: expected pnl% as a
function of *posterior score percentile*. Curves typically rise monotonically
(higher posterior → higher EV) and may have a knee (the band where EV justifies
trading).

Curves stored per kind: `data/ev_curves/<KIND>.json` with `(percentile, ev_mean, ev_p25, ev_p75, n)` rows.

At fire time:
```
posterior_pct = percentile_rank(posterior, kind)
ev_expected = lookup(ev_curve[kind], posterior_pct)
ev_p25 = lookup_p25(ev_curve[kind], posterior_pct)
```

If `ev_expected ≤ 0` or `ev_p25 ≤ -2×expected_loss` → SKIP regardless of phase gate.

### 3d. Component (4) — Kelly Progression

Fractional Kelly with an explicit ramp:

```
b = (ev_expected_at_win) / (ev_expected_at_loss)    # odds
p = posterior                                         # prob of win
q = 1 - p
kelly_full = (b·p - q) / b

# Scale ramps with rolling 60-day live win-rate alignment to predicted
scale_oos = clip(rolling_60d_realized_wr / rolling_60d_predicted_wr, 0.0, 1.0)

# Phase-state bumper
scale_phase = {OBSERVE: 0.0, PAPER: 0.0, LIVE_SMALL: 0.10, LIVE_FULL: 0.25}[state]

kelly_used = kelly_full · scale_oos · scale_phase
```

Default cap: `kelly_used ≤ 0.05` (5% per position) regardless of math. Hard floor.

### 3e. Component (5) — Position Ledger

One row per fire that passed (1)+(2)+(3)+(4):

```json
{
  "ts_utc": "2026-04-28T13:42:01Z",
  "kind": "CALL_CONVICTION",
  "ticker": "NVDA",
  "phase_state": "LIVE_SMALL",
  "posterior": 0.67,
  "ev_expected": 0.082,
  "ev_p25": -0.005,
  "kelly_full": 0.18,
  "kelly_used": 0.018,
  "approved": true,
  "scalp1_features": {...},
  "live_features": {"uw_flow_score": 0.4, "news_llm_score": 0.1, "gex_proximity": 0.2}
}
```

Dashboard tab pulls from this ledger to show live phase status, today's approved fires,
and rolling out-of-sample alignment.

---

## 4. Phase-gate definitions per signal kind

Source: scalp 1's `kind_table` ranks each engine_v4 signal kind by win% and EV.
Scalp 2 starts every kind at `OBSERVE` and progresses based on rolling lift.

| Initial state | Criterion |
|---|---|
| `OBSERVE` | Default — no live trading. Just log fires + posterior + would-have-been-ev. |
| `PAPER` | Promote when rolling 30-day archive lift ≥1.10×. Paper trade with full sizing math. |
| `LIVE_SMALL` | Promote when paper rolling 30-day lift ≥1.15× AND n ≥ 30. Live with 0.10× Kelly. |
| `LIVE_FULL` | Promote when LIVE_SMALL rolling 30-day lift ≥1.20× AND n ≥ 60. Live with 0.25× Kelly. |

Demotion: any state regresses one step on rolling 7-day drawdown > 2× expected EV.

The "S5" terminology from scalp 1's DESIGN.md (`allow_s5_trade`) refers to this
phase-gate's binary output for the trading layer. `S` = state, `5` = the fifth
phase tier (matching scalp 1's `s5_regime_gate` naming). The function signature
is `allow_s5_trade(kind, ts) -> bool`.

---

## 5. Inputs from scalp 1 — exact contract

Scalp 2 reads these without modification:

### 5a. `scalp_backtest/data/seven_year_backtest/run_*.json`
- `per_signal[]` — list of resimmed fires with:
  - `regime` (fast / slow), `atr_event` (TARGET_T1/T2/T3/STOP/EOD), `atr_t1_plus_hit`
  - Pre-trade features: `prior_5bar_alignment`, `prior_5bar_volume_z`,
    `prior_30bar_realized_vol_pct`, `atr_14_pct`, `minute_of_day`
  - Outcome features: `option_pnl_pct`, `underlying_pnl_pct`, `bars_to_event`
- `regime_sanity` — multi-year P(T1+|fast) vs P(T1+|slow) breakdown + by_year stability
- `regime_sanity.alignment_quintile_probe` — Q1–Q5 P(slow) and P(T1+) shape
- `resim_match_by_strategy` — which kinds have alignable archive vs resim outcomes
- `walk_forward_momentum` — hindsight-bias-corrected MOMENTUM top-10 EV

### 5b. `scalp_backtest/data/pretrade_slow_predictor/run_*.json`
- `feature_importances.lr.coefficients` — signed LR coefficients per feature
- `feature_importances.gbm.permutation_importance_mean` — unsigned GBM importance
- `headline.auc` — the baseline AUC scalp 2's posterior model must beat to be useful
- `dataset.median_imputed_values` — per-feature defaults for missing data

### 5c. `scalp_backtest/data/slices/outcomes_scalp.pkl`
Pickled list of `Outcome` records. Schema documented in
`scalp_backtest/src/outcome_intraday.py::Outcome`. Loaded for the EV-curve
fits per signal kind.

### 5d. Live read of `engine_v4/data/intraday/<DATE>/breakouts_*.jsonl`
Reuses scalp 1's `loader_scalp.py::load_validated/load_coil` functions imported
as a library. No code duplication.

---

## 6. Outputs — exact schemas

### 6a. `phase_state/<DATE>/state.json`
```json
{
  "generated_utc": "2026-04-28T18:00:00Z",
  "states": {
    "CALL_CONVICTION": {
      "state": "LIVE_SMALL",
      "since_utc": "2026-04-15T00:00:00Z",
      "rolling_30d_lift": 1.18,
      "rolling_30d_n": 47,
      "rolling_7d_drawdown_pct": -3.2,
      "next_review_utc": "2026-04-29T00:00:00Z"
    },
    "CALL_MORNING": {"state": "PAPER", "..."},
    "PRE_BREAKOUT_COIL": {"state": "OBSERVE", "..."}
  }
}
```

### 6b. `posterior/<DATE>/scores.jsonl`
One JSON per line, one line per fire:
```json
{"ts_utc":"...","ticker":"NVDA","kind":"CALL_CONVICTION","prior":0.52,"posterior":0.67,"likelihoods":{"uw_flow":0.13,"news":0.05,"gex":-0.03}}
```

### 6c. `ev_curves/<KIND>.json`
```json
{
  "kind": "CALL_CONVICTION",
  "fitted_at_utc": "2026-04-28T00:00:00Z",
  "n_archive_fires": 442,
  "curve": [
    {"percentile": 10, "ev_mean": -0.05, "ev_p25": -0.18, "ev_p75": +0.02, "n": 44},
    {"percentile": 50, "ev_mean": +0.02, "ev_p25": -0.10, "ev_p75": +0.18, "n": 44},
    {"percentile": 90, "ev_mean": +0.15, "ev_p25": +0.03, "ev_p75": +0.32, "n": 44}
  ]
}
```

### 6d. `kelly_ledger/<DATE>/positions.jsonl`
One JSON per line, one line per APPROVED fire (component 5 schema above).

---

## 7. Pipeline architecture

```
src/
├── __init__.py
├── consume_scalp1.py          # loaders for scalp 1 artifacts
├── phase_gate.py               # component (1)
├── posterior_model.py          # component (2)
├── ev_curve.py                 # component (3) — fits + lookups
├── kelly_progression.py        # component (4)
├── position_ledger.py          # component (5)
├── pipeline.py                 # orchestrator
├── renderer.py                 # dashboard HTML
└── live_loop.py                # daemon mode (poll engine_v4 fires, write ledger)

tests/
├── test_phase_gate.py
├── test_posterior_model.py
├── test_ev_curve.py
└── test_kelly_progression.py
```

Two execution modes:
- **Batch mode**: `python3 src/pipeline.py` — reads scalp 1 artifacts + today's engine_v4
  fires, produces all output files, renders dashboard. One-shot.
- **Live mode**: `python3 src/live_loop.py` — polls engine_v4 every 60s, writes
  posterior + ledger rows in real time. Dashboard auto-refreshes.

---

## 8. Validation gates — what proves scalp 2 is working

Before any phase advances past `PAPER`:
- Posterior model AUC on out-of-sample 2026 ≥0.62 (must beat scalp 1's GBM baseline of 0.584)
- EV curve monotonicity: top-decile EV > median EV by ≥0.5× ATR cost
- Phase gate state-machine integration test (synthetic fixture: confirm advancement + demotion logic)
- Kelly cap sanity: no fire ever sized > 5% (hard floor enforced)

Before LIVE_SMALL → LIVE_FULL on any kind:
- 60+ fires in LIVE_SMALL with rolling 30-day lift ≥1.20×
- No drawdown event ≥2× expected EV in the prior 14 days

---

## 9. Build order — milestones M1–M5

| M | Deliverable | Acceptance |
|---|---|---|
| M1 | `consume_scalp1.py` + tests | Loads run_*.json, returns typed dataclasses for per-fire records |
| M2 | `posterior_model.py` baseline | Beats scalp 1 GBM AUC (0.584) on 2026 holdout |
| M3 | `ev_curve.py` per-kind fits | Curves persisted, monotonicity sanity check passes |
| M4 | `phase_gate.py` + `kelly_progression.py` | State transitions integration-tested |
| M5 | `pipeline.py` + `renderer.py` + dashboard | Single-page dashboard renders all 5 components |

Live mode (`live_loop.py`) is a stretch goal — out of scope until M5 is validated.

---

## 10. Drift control — when the original docx surfaces

If `trading_system_lifecycle.docx` is found, this DESIGN.md should be
diff'd against it and any structural drift documented in this section.
Substantive disagreements (component count, phase definitions, sizing math)
trigger a §0 status update + a re-scoping review before further code changes.

Inferred-from-references-only items in this DESIGN.md (most likely to
need reconciliation):
- The 4-state phase machine (OBSERVE/PAPER/LIVE_SMALL/LIVE_FULL) — only the
  output `allow_s5_trade` bool is firmly anchored in scalp 1's references.
- The 1.15× / 1.20× lift thresholds — scalp 1 references "≥1.15× lift" once
  but doesn't elaborate on per-state thresholds.
- The Kelly progression specifics (0.10× / 0.25× scales) — fully inferred.
- The component breakdown (5 numbered components) — inferred from the
  enumeration `allow_s5_trade, posterior models, EV-curve picker, full Kelly
  progression` in scalp 1's DESIGN.md line 4.

---

## 11. What this unlocks downstream

- **Live phase-gating layer for engine_v4.** First time the audit findings turn into a production decision boundary.
- **Bayesian fusion of scalp 1's offline features with live UW flow / news.** The pretrade predictor's AUC ceiling (0.584) gets bumped by the live likelihoods.
- **Per-kind EV curves persistence.** Every signal class becomes a calibrated, monotone EV → size mapping.
- **Risk-bounded Kelly with ramp.** No "I lost half my account on one trade" failure mode.
- **Foundation for stage 3 ML upgrade in engine_v4.** The carried-forward features
  from scalp 1 (`prior_5bar_alignment`, `prior_5bar_volume_z`, `atr_14_pct`,
  regime label) plus the posterior model become the starting point for
  whatever ML upgrade the engine team builds next.
