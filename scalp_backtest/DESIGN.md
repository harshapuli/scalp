# Trading Backtest Page — Master Design v4

> ## SCOPE
> **This project audits `engine_v4` signals only.** It is a post-hoc, read-only analysis of every fire the live system has produced. The Edge-Centric Trading System v2.2 (described in `trading_system_lifecycle.docx` / `trading_system_jira_import.csv`) is a **separate, future build**. v2.2 phase gates (`allow_s5_trade`, ≥1.15× lift, posterior models, EV-curve picker, full Kelly progression) are NOT in this project. We **produce labels and features** that a future v2.2 build could consume — that is the only contact point.

> **Status:** DRAFT v4 for review. Includes 14 must-fix items from the consolidated review.
> **Boundary:** `engine_v4/`, `swing_engine_v2/`, `swing_trade_strategy/` are **READ-ONLY**. We do **not** patch any existing engine. The swing data path is a **shadow logger** that polls existing snapshot files — no engine modification.
> **No code beyond this doc until sign-off** on §14 questions.

---

## 0. Table of contents

1. The questions we're answering
2. What we're NOT doing
3. Current system inventory — scalp side
4. Current system inventory — swing side
5. ML pipeline — current state (engine_v4 only)
6. Backtest data inventory
7. Pipeline architecture
8. Outcome semantics — ATR-primary ladders, cost-aware labels
9. Slices we'll surface
10. Statistics — rigor floor
11. UI / page layout — five tabs
12. Project layout & dependencies
13. Edge cases & display rules
14. Open questions for you
15. Build order — milestones M1–M8
16. Pause points & review gates
17. Non-goals
18. What this unlocks downstream
19. Changes from v3

---

## 1. The questions we're answering

> **Q-scalp:** *Of every intraday scalp signal we've ever shown, was it actually good — after costs?*
> **Q-swing:** *Of every multi-day swing signal we've ever shown, was it actually good — after costs?*
> **Q-ml:** *For each signal class, do we have enough labeled history (days × fires) to train a real model on it yet?*
> **Q-archive:** *How do the existing dashboards and prior backtest pages slot in?*

**One HTML page, five tabs:**

```
┌──────────────────────────────────────────────────────────────────┐
│  Trading Backtest — every signal we've shown                     │
│  [ OVERVIEW ] [ SCALP (n_dedup/n_raw) ] [ SWING (n_dedup/n_raw) ]│
│  [ ML READINESS ] [ ARCHIVE ]                                    │
└──────────────────────────────────────────────────────────────────┘
```

---

## 2. What we're NOT doing

- Not building any new signal logic. Read-only on the existing engines.
- Not patching any live engine. Swing data is collected via shadow polling, not insertion.
- Not training a real ML model. The backtest *produces the labels* a model would need; the ML tab is a readiness ledger only.
- Not implementing v2.2 phase gates (`allow_s5_trade`, posterior models, EV-curve picker). That's a future, separate project.
- Not modeling option chain / IV / theta. Underlying % is honest; option-leveraged numbers carry an explicit multiplier label.
- Not modeling slippage / spread. We DO model **flat round-trip cost (8% options, 0% underlying)** in the label definition.
- Not running anything live. Post-hoc only.

---

## 3. Current system inventory — SCALP (`engine_v4/`)

### 3a. DETECTORS

| file | what it fires | status |
|---|---|---|
| `v4_live_breakout_scanner.py` | `PRE_*_COIL` / `PRE_*_RAMP` | running PID 20480 @ 10s |
| `v4_validated_paper_trader.py` | `CALL_MORNING`, `*_CONVICTION`, `CALL_CONT_CALL`, `CALL_V4SIGNALS` | running PID 20481 @ 10s |
| `v4_breakout_scanner.py` | breakout core | scanner-of-record |
| `v4_continuation_scanner.py` / `_put.py` | continuation fires | called by main scanner |
| `v4_0dte_flow_scanner.py` | 0DTE option-flow | running 90s loop |
| `v4_bcs_pcs_scanner.py` | bull-call-spread / put-credit-spread | structural |
| `v4_prebreak_scanner.py` | pre-breakout watches | running 60s loop |

### 3b. PATROL (live read-only watchman)

| file | what it does |
|---|---|
| `v4_patrol_engine.py` | running PID 7804 (30s, launchd) — structure invalidation, fast SMC retest on 1-min bars, engine staleness alarm; logs to `v4_patrol_log.jsonl`. **Read-only by design** — never mutates state files. |
| `v4_intraday_rescore.py` | re-scores live watches every cycle |

### 3c. DOCTOR (post-mortem + live audit)

| file | when | role |
|---|---|---|
| `v4_breakout_lifecycle.py` | one-shot post-mortem | walks back 60 bars + whale flow + conviction + news + DP + V7 for each fresh break. Forensic recogniser. |
| `v4_lifecycle_view.py --loop` | running PID 20482 @ 30s | audits open positions every cycle |
| `v4_breakout_followthrough.py` | post-fire | tracks whether breakouts followed through |

### 3d. STREAMER + RANKER + EXEC

| file | role |
|---|---|
| `v4_ws_streamer.py` | running PID 14074 — WS tick pump |
| `v4_top_picks.py --loop` | running PID 20483 @ 60s — top-5 refresh |
| `v4_paper_trader.py` | running PID 26431 — paper exec |
| `v4_armada.py` | orchestration umbrella |

### 3e. SCORING (rules + LLM, not classical ML)

| file | role |
|---|---|
| `v4_premarket_scorer.py` | running launchd loop |
| `v4_intraday_rescore.py` | running launchd loop |
| `v4_bcs_pcs_scorer.py` | spread scoring |
| `v4_news_llm_scorer.py` | news LLM scoring |
| `v4_multileg_classifier.py` | multileg classifier |
| `v4_meta_engine.py` | running 5-min loop — meta orchestration |

**No `sklearn` / `xgboost` anywhere in `engine_v4/`.** Scalp scoring today is rules + LLM only.

### 3f. Data outputs

```
engine_v4/data/intraday/<DATE>/
├── breakouts_validated.jsonl     ← validated bucket (100 today)
├── breakouts_coil.jsonl          ← coil/ramp bucket (496 today)
├── bars/<TICKER>.jsonl           ← 1-min OHLCV (305 tickers today)
├── lifecycle/<TICKER>_*.json     ← DP/CALL_BUYING context
├── breakout_lifecycles.jsonl     ← Doctor's forensic output
├── events.jsonl                  ← 1,282 lifecycle events
├── breakouts_validated_open.json ← open-positions snapshot
└── top_picks_*.json              ← ranker snapshots (~150 today)
```

**Today's totals (2026-04-27 only):**

| source | n raw | breakdown |
|---|---|---|
| validated | 100 | PUT_CONVICTION 36 · CALL_MORNING 30 · CALL_CONT_CALL 16 · CALL_CONVICTION 12 · CALL_V4SIGNALS 6 |
| coil | 496 | PRE_CALL_COIL 259 · PRE_PUT_COIL 189 · PRE_CALL_RAMP 32 · PRE_PUT_RAMP 16 |
| **TOTAL** | **596 raw** | dedup TBD per §14 Q5 — likely ~**38 unique** (one fire per ticker per session per kind) |

---

## 4. Current system inventory — SWING

### 4a. `swing_engine_v2/`

| file | role |
|---|---|
| `v2_core.py` | per-ticker state machine — PROBE PUT / GOD-MODE / HOLD |
| `v2_smc_matrix.py` | ATR compression + volume exhaustion + range overlap |
| `v2_data_feed.py` | swing data feed |
| `v2_active_portfolio.json` | **snapshot of current state** — PLTR/SPY/AAPL/MSFT/QQQ/TSLA/META |

### 4b. `swing_trade_strategy/`

| role | file |
|---|---|
| **DETECTOR** | `screener_engine.py`, `smc_engine.py`, `institutional_scanner.py`, `standalone_screener.py` |
| **PATROL** | `auto_analysis_daemon.py` — schedule-based 5 AM PT |
| **DOCTOR (EOD)** | `analysis_engine.py` — writes `EOD_ANALYSIS.md` |
| **DOCTOR (deep)** | `deep_analysis.py`, `analyze_ticker.py`, `diag_losers.py` |
| **STREAMER** | `live_stream_engine.py`, `data_feed.py` |
| **EXECUTION** | `alpaca_execution.py`, `cancel_redundant.py` |
| **NEWS** | `news_sentiment_engine.py`, `scrape_zenscans.py` |
| **OPTIONS** | `options_engine.py` |
| **NOTIFICATIONS** | `notification_engine.py`, `secure_mobile_server.py` |
| **ML** | `ml_engine.py` — sklearn `RandomForestClassifier` (synthetic data, see §5) |
| **DASHBOARD** | `dashboard.py`, `screener_ui.py` |
| **BACKTEST** | `backtest.py` — limited |

### 4c. Swing data path — shadow logger (NOT a patch)

**There is no clean structured swing-fire history on disk today.** Per the must-fix review (item 8), we do **not** modify the swing engine to add a logger.

Instead the backtest project runs a **shadow logger** in its own process:

```
┌──────────────────────────────────────────────────────────────────┐
│  scalp_backtest/src/swing_shadow_logger.py                       │
│                                                                  │
│  loop (every 60s):                                               │
│    snap_v2  = json.load(swing_engine_v2/v2_active_portfolio.json)│
│    snap_str = json.load(swing_trade_strategy/stream_signals.json)│
│    diff vs prev snapshot → emit transition lines                 │
│    append to scalp_backtest/data/swing_signals.jsonl             │
│                                                                  │
│  emits one line per ticker state transition:                     │
│    {ts, ticker, prev_state, new_state, kind, side, ...}          │
└──────────────────────────────────────────────────────────────────┘
```

**No engine code modified. No restart of swing engine required. No diff approval needed for M5** because we don't touch their code.

The shadow logger's accuracy depends on poll cadence — at 60s we may miss state-transitions that happen and revert within 60s. Documented caveat. Cadence tunable.

---

## 5. ML pipeline — current state (engine_v4 only)

### 5a. What exists today (engine_v4 scope only)

- **No classical ML model** in engine_v4. The scoring stack is rules + LLM:
  - rules-based composite confidence number (e.g., `confidence: 53.0` in validated jsonl)
  - LLM news scoring (`v4_news_llm_scorer.py`)
  - rule-based scorers across `v4_premarket_scorer.py`, `v4_bcs_pcs_scorer.py`, `v4_multileg_classifier.py`

### 5b. What this backtest produces (engine_v4-scoped, NOT v2.2)

```
┌──────────────────────────────────────────────────────────────────┐
│  scalp_backtest produces, for each engine_v4 signal:             │
│                                                                  │
│   1. cost-aware label                                            │
│      label = +1 if target-hit-before-stop AND                    │
│                 (pnl_at_first_event - flat_cost) > 0            │
│      label =  0 if marginal (within ±2% post-cost break-even)   │
│      label = -1 otherwise                                        │
│      flat_cost: 0% (underlying), 8% RT (options at 5.5× lev)    │
│                                                                  │
│   2. feature vector per fire                                     │
│      kind, side, time_of_day, day_of_week,                       │
│      rvol, day_pct, coil_range, dist_above_lod,                  │
│      confidence, vix_bucket, spx_regime,                         │
│      atr_14, near_miss_eligible                                  │
│                                                                  │
│   3. readiness ledger per kind                                   │
│      n_deduped_fires, n_unique_trading_days,                     │
│      ready_to_train = (days >= 30 AND fires >= 100)              │
└──────────────────────────────────────────────────────────────────┘
```

That's it. **No v2.2 phase gates, no posterior models, no EV-curve picker, no `allow_s5_trade`** in this project. Those live in the separate v2.2 build, which would *consume* the labels and features above.

---

## 6. Backtest data inventory — what we read

```
engine_v4/data/intraday/<DATE>/
├── breakouts_validated.jsonl       ← scalp signals (validated)
├── breakouts_coil.jsonl            ← scalp signals (coil/ramp)
├── bars/<TICKER>.jsonl             ← 1-min OHLCV
└── lifecycle/<TICKER>_*.json       ← DP/flow context

swing_engine_v2/v2_active_portfolio.json    ← polled by shadow logger
swing_trade_strategy/stream_signals.json    ← polled by shadow logger
swing_trade_strategy/v3_stream_signals.json ← polled by shadow logger

scalp_backtest/data/swing_signals.jsonl     ← OUR shadow log output (M5)

(optional macro overlay)
yfinance / polygon → ^VIX daily              ← for vix_bucket
yfinance / polygon → SPY daily               ← for spx_regime (50/200 MA)
```

---

## 7. Pipeline architecture

```
┌──────────┐   ┌───────────────┐   ┌─────────────┐   ┌──────────────┐   ┌────────────┐
│  loader  │ → │ outcome eng.  │ → │  slicer     │ → │  stats       │ → │  renderer  │
│ jsonl→DC │   │ walk bars +   │   │ groupby     │   │ Wilson CI    │   │  HTML page │
│          │   │ ATR ladder +  │   │ + n<5       │   │ + EV/fire    │   │  5 tabs    │
│          │   │ cost label    │   │ suppression │   │              │   │            │
└──────────┘   └───────────────┘   └─────────────┘   └──────────────┘   └────────────┘
       ↑              ↑                  ↑                ↑                   ↑
   scalp+swing    intraday +          shared          shared             OVERVIEW /
   loaders        daily               per slice       framework          SCALP /
                                                                         SWING /
                                                                         ML-READY /
                                                                         ARCHIVE
```

---

## 8. Outcome semantics — ATR-primary ladders, cost-aware labels

### 8a. Sign convention

Returns are signed for the side. CALL up = positive, PUT down = positive.

### 8b. Horizons

- **Scalp** (1-min bars): 5m / 15m / 30m / 60m / EOD
- **Swing** (daily bars): +1d / +3d / +5d / +10d / +20d

### 8c. ATR-primary ladder (PRIMARY) — must-fix item 3

ATR(14) is computed at signal time from the 14 most recent session bars BEFORE detected_minute_utc. Stored in Outcome.

| level | scalp (1-min ATR) | swing (daily ATR) |
|---|---|---|
| Target T1 | +0.75 × ATR | +0.75 × ATR |
| Target T2 | +1.50 × ATR | +1.50 × ATR |
| Target T3 | +2.25 × ATR | +2.25 × ATR |
| Stop      | −1.00 × ATR | −1.00 × ATR |

ATR ladder is the **primary** semantic. It adapts to per-name volatility — a 0.5% target on PYPL is meaningless if PYPL has 1.2% ATR.

### 8d. Pct ladder (SECONDARY, stored alongside) — must-fix item 3

Both stored in Outcome for cross-reference.

| level | scalp pct | swing pct |
|---|---|---|
| T1 | +0.50 % | +3 % |
| T2 | +1.00 % | +6 % |
| T3 | +1.50 % | +10 % |
| Stop | −0.40 % | −5 % |

### 8e. Wide-bar pass — must-fix item 11

A wide-range bar can touch BOTH target and stop. Default is conservative ("STOP-first"). We run **both passes** and surface the range:

- `first_event_stop_first` — stop assumed to fire first on wide bars (pessimistic)
- `first_event_target_first` — target assumed to fire first on wide bars (optimistic)
- `pnl_first_event_stop_first` and `_target_first` — both stored

The kind table headlines **STOP-first** (conservative) and shows TARGET-first as upper bound.

### 8f. Cost-aware label — must-fix item 6

```python
# costs (round-trip)
flat_cost_underlying = 0.0   # %  — pure stock backtest
flat_cost_option_5p5x = 8.0  # %  — typical 0DTE/short-dated round-trip friction (mid-quote spread x2 + slippage)

# net pnl at first event, after cost
net_pnl_pct = pnl_first_event_pct - flat_cost

# label
if first_event in {TARGET_T1, TARGET_T2, TARGET_T3} AND net_pnl_pct > 0:
    label = +1
elif abs(net_pnl_pct) <= 2.0:        # marginal band (within ±2% of post-cost break-even)
    label = 0
else:
    label = -1
```

Two label fields stored:
- `label_underlying` (flat_cost = 0)
- `label_option_5p5x` (flat_cost = 8%)

Headline number on the kind table is `label_option_5p5x` because that's what we'd actually trade.

### 8g. EV per fire is the headline — must-fix item 10

The headline number on every kind/band cell is `realized_ev_per_fire`:

```python
realized_ev_per_fire_underlying  = mean(pnl_first_event_stop_first - 0)
realized_ev_per_fire_option_5p5x = mean(pnl_first_event_stop_first * 5.5 - 8.0)
```

**Win rate is shown** but is secondary. EV per fire is what determines position sizing and which signals to keep.

### 8h. Outcome dataclass — must-fix items 5, 14

```python
@dataclass
class Outcome:
    # identity
    source: str                 # "validated" | "coil" | "swing"
    kind: str
    side: str                   # CALL | PUT
    ticker: str
    detected_utc: str
    entry_price: float

    # context features (the ML feature vector)
    confidence: Optional[float]
    rvol: Optional[float]
    day_pct: Optional[float]
    coil_range_pct: Optional[float]
    dist_above_lod_pct: Optional[float]
    time_of_day_bucket: str
    day_of_week: str

    # NEW per must-fix item 5 — stored now, surfaced later
    vix_bucket: Optional[str]   # "low" (<15) | "mid" (15-25) | "high" (25+) — daily VIX
    spx_regime: Optional[str]   # "uptrend" | "range" | "downtrend" — SPY 50/200 MA position
    near_miss_eligible: bool    # True if signal almost fired but didn't (defined per kind)

    # NEW per must-fix item 14 — ATR(14) at signal time
    atr_14: Optional[float]     # in price units (entry currency)
    atr_14_pct: Optional[float] # ATR / entry_price * 100, for cross-name comparison

    # horizon returns (signed, %)
    ret_5m_pct / ret_15m_pct / ret_30m_pct / ret_60m_pct / ret_eod_pct  # scalp
    ret_1d_pct / ret_3d_pct / ret_5d_pct / ret_10d_pct / ret_20d_pct    # swing

    # MFE / MAE
    mfe_pct: float
    mae_pct: float
    bars_to_mfe: int
    bars_to_mae: int

    # ATR-primary ladder (the primary semantic)
    atr_first_event_stop_first: str   # TARGET_T1 | TARGET_T2 | TARGET_T3 | STOP | EOD
    atr_first_event_target_first: str
    atr_pnl_stop_first_pct: float
    atr_pnl_target_first_pct: float
    bars_to_atr_first_event: int

    # Pct-ladder (secondary, parallel)
    pct_first_event_stop_first: str
    pct_first_event_target_first: str
    pct_pnl_stop_first_pct: float
    pct_pnl_target_first_pct: float

    # Cost-aware labels (must-fix item 6)
    label_underlying: int                # +1 / 0 / -1, flat_cost = 0
    label_option_5p5x: int               # +1 / 0 / -1, flat_cost = 8.0

    # Realized EV per fire (the headline — must-fix item 10)
    ev_underlying_stop_first: float      # = pct_pnl_stop_first - 0
    ev_underlying_target_first: float
    ev_option_5p5x_stop_first: float     # = pct_pnl_stop_first * 5.5 - 8.0
    ev_option_5p5x_target_first: float
```

The **schema is source-agnostic** — slicer/stats/renderer don't care if a row came from scalp or swing.

---

## 9. Slices

| dim | scalp bins | swing bins | surfaced v1? |
|---|---|---|---|
| **kind** | every distinct kind (9) | every distinct kind | ✅ headline |
| **side** | CALL / PUT | CALL / PUT | ✅ |
| **filter band (confidence)** | low (0-30) / med (30-60) / high (60+) | low / med / high (risk_pct) | ✅ in kind table — must-fix item 4 |
| **time-of-day** | 30-min UTC buckets | n/a | ✅ heatmap |
| **day-of-week** | n/a | Mon-Fri | ✅ swing tab |
| **ticker** | top-20 by signal count | full universe | ✅ leaderboard |
| **rvol bucket** | <1.2 / 1.2-1.5 / 1.5-2 / 2-3 / 3+ | n/a | ✅ slice |
| **day_pct bucket** | <-2 / -2 to -1 / ... / +2+ | n/a | ✅ slice |
| **vix_bucket** | low / mid / high | low / mid / high | ❌ stored, not surfaced v1 — must-fix item 5 |
| **spx_regime** | uptrend / range / downtrend | uptrend / range / downtrend | ❌ stored, not surfaced v1 — must-fix item 5 |
| **near_miss_eligible** | True / False | True / False | ❌ stored, not surfaced v1 |

Cross-tabs surfaced v1: **kind × time-of-day** (scalp), **kind × day-of-week** (swing), **kind × filter-band** (the headline kind table).

---

## 10. Statistics — rigor floor

- Wilson 95% CI on every win rate
- Sample size shown next to every metric
- `n < 10` flagged "too few to trust" (⚠️)
- **`n < 5` cells in cross-tab heatmaps suppressed** — render as gray "·", not colored — must-fix item 12
- Continuous returns: mean, median, p10, p90 (tail asymmetry)
- "Open" signals scored from bars; engine's own exit decisions shown separately

---

## 11. UI / page layout — five tabs

**Tone**: Anthropic brand. Dark text `#141413` on light `#faf9f5`. Accents orange `#d97757` (primary), blue `#6a9bcc` (secondary), green `#788c5d` (tertiary). Headings Poppins (Arial fallback), body Lora (Georgia fallback).

**Stack**: single self-contained `output/index.html`. Plotly via CDN. No build step.

**Sticky header on every tab — must-fix item 13:**

```
┌─────────────────────────────────────────────────────────────────────┐
│  Trading Backtest — every signal we've shown                        │
│  date range: 2026-04-27 → 2026-04-27       last refresh: HH:MM PT   │
│  ───────────────────────────────────────────────────────────────    │
│  [ OVERVIEW ] [ SCALP (38 unique / 596 raw) ] [ SWING (?/?) ]       │
│  [ ML READINESS ] [ ARCHIVE ]                                       │
│  Caveat: 8% RT cost on options · 0% on underlying · STOP-first      │
└─────────────────────────────────────────────────────────────────────┘
```

### 11a. OVERVIEW tab

```
┌─ KPI tiles row (5-up) ─────────────────────────────────────────────┐
│ total signals · scalp EV/fire (u + opt5.5×) · swing EV/fire ·      │
│ best class today (by EV) · cost basis (0% / 8%)                    │
└────────────────────────────────────────────────────────────────────┘
┌─ side-by-side panels ──────────────────────────────────────────────┐
│ [ scalp summary ]  [ swing summary ]                               │
└────────────────────────────────────────────────────────────────────┘
┌─ time series ──────────────────────────────────────────────────────┐
│ cumulative EV under STOP-first ladder, one line per source         │
└────────────────────────────────────────────────────────────────────┘
```

### 11b. SCALP tab — three filter bands, no recommendation column

Per must-fix items 4 and 9: **drop the ✅⚠️❌ column**. Show three filter bands per kind row.

```
┌─ KPI tiles ────────────────────────────────────────────────────────┐
│ unique fires · raw fires · win% (CI) · avg MFE · EV/fire           │
└────────────────────────────────────────────────────────────────────┘
┌─ equity curve ─────────────────────────────────────────────────────┐
│ cumulative EV over time-of-day, lines per source                   │
└────────────────────────────────────────────────────────────────────┘
┌─ KIND TABLE — the headline (per must-fix items 4, 9, 10, 11) ─────┐
│                                                                    │
│  kind          | filter | n  | win% (CI) | EV/fire (u, opt5.5×)   │
│                |        |    |           | STOP-first → TGT-first │
│ ──────────────┼────────┼────┼───────────┼────────────────────────│
│  CALL_MORNING  | low    | 11 | 45% (..) | -0.05% / +0.30%         │
│                | med    | 14 | 64% (..) | +0.18% / +0.55%         │
│                | high   |  5 | 80% (..) | +0.32% / +0.78%  ⚠ n<10│
│ ──────────────┼────────┼────┼───────────┼────────────────────────│
│  PRE_CALL_COIL | low    | 88 | ...      | ...                    │
│                | med    |134 | ...      | ...                    │
│                | high   | 37 | ...      | ...                    │
│  ...                                                              │
│                                                                    │
│  EV columns show range "STOP-first / TARGET-first" (item 11)      │
│  No ✅⚠️❌ column in v1 (item 9)                                   │
└────────────────────────────────────────────────────────────────────┘
┌─ time-of-day heatmap ──────────────────────────────────────────────┐
│ rows=kind, cols=30-min PT buckets, cell=avg EV                     │
│ n<5 cells show as "·" gray (item 12)                               │
└────────────────────────────────────────────────────────────────────┘
┌─ feature slices (collapsible) ─────────────────────────────────────┐
│ rvol×kind, day_pct×kind, atr%×kind                                 │
│ same n<5 suppression                                               │
└────────────────────────────────────────────────────────────────────┘
┌─ ticker leaderboard ───────────────────────────────────────────────┐
│ top 20 by count, then top 20 by EV                                 │
└────────────────────────────────────────────────────────────────────┘
┌─ distributions ────────────────────────────────────────────────────┐
│ histogram of pnl per major kind                                    │
│ scatter MFE vs MAE colored by side                                 │
└────────────────────────────────────────────────────────────────────┘
┌─ SIGNAL TABLE — every fire ────────────────────────────────────────┐
│ search · kind/side/ticker filter                                   │
│ time(PT) | tkr | kind | side | entry | atr% | conf-band |          │
│ atr-event(stop-first) | bars | net-pnl% | mfe% | mae%              │
└────────────────────────────────────────────────────────────────────┘
```

**Toggle on table**: dedup-default-ON (one fire per ticker per session per kind) vs raw-view (every fire). Default = dedup ON per must-fix item 2.

### 11c. SWING tab

Same structure as SCALP but daily horizons.

```
┌─ KPI tiles ────────────────────────────────────────────────────────┐
│ unique fires · raw fires · win% · EV/fire (u + opt2.0×)            │
└────────────────────────────────────────────────────────────────────┘
┌─ universe + current state ─────────────────────────────────────────┐
│ from v2_active_portfolio.json (live snapshot)                      │
└────────────────────────────────────────────────────────────────────┘
┌─ if shadow log has history: ───────────────────────────────────────┐
│ KIND TABLE with three filter bands                                 │
│ daily horizon equity curve · day-of-week heatmap                   │
│ per-ticker breakdown · swing signal table                          │
└────────────────────────────────────────────────────────────────────┘
┌─ if shadow log thin: ──────────────────────────────────────────────┐
│ "Shadow log accumulating — N fires logged, page becomes meaningful │
│  around 30 unique trading days × 100 deduped fires (~8 weeks)"    │
└────────────────────────────────────────────────────────────────────┘
```

### 11d. ML READINESS tab — must-fix items 1, 7

**Strip all v2.2 phase-gate content.** This tab is engine_v4-scoped only.

Threshold per kind: **`ready_to_train = (n_unique_trading_days >= 30) AND (n_deduped_fires >= 100)`** — must-fix item 7.

```
┌─ KPI tiles ────────────────────────────────────────────────────────┐
│ classes ready to train · classes accumulating ·                    │
│ total deduped labels · total unique trading days                   │
└────────────────────────────────────────────────────────────────────┘
┌─ per-class readiness ledger (must-fix items 1, 7) ─────────────────┐
│                                                                    │
│  kind          | days | fires (dedup) | label dist (+/0/-) | ready │
│ ──────────────┼──────┼───────────────┼────────────────────┼───────│
│  PRE_CALL_COIL | 1/30 | 47/100        | 22 / 8 / 17        | no    │
│                |  ███░░░░░░░░         | progress bars       |       │
│  CALL_MORNING  | 1/30 |  6/100        |  3 / 1 / 2         | no    │
│                |  ░░░░░░░░░░░░         |                    |       │
│  ...                                                              │
│                                                                    │
│  Two progress bars per row: days, fires.                           │
│  ready = both >= threshold.                                        │
└────────────────────────────────────────────────────────────────────┘
┌─ feature coverage matrix ──────────────────────────────────────────┐
│ rows=class, cols=feature                                           │
│ cell=% non-null in labeled set                                     │
│ green ≥95%, amber 70-95%, red <70%                                 │
│ n<5 cells suppressed                                               │
└────────────────────────────────────────────────────────────────────┘
┌─ label distribution per class ─────────────────────────────────────┐
│ stacked bar: wins (green) / pushes (gray) / losses (red)           │
│ uses label_option_5p5x (cost-aware) as default                     │
│ toggle to label_underlying for cost-free comparison                │
└────────────────────────────────────────────────────────────────────┘

(NO v2.2 phase checklist — item 1)
```

### 11e. ARCHIVE tab (NEW)

Surfaces existing dashboards so we can integrate them — must-fix-implicit (user request).

```
┌─ Existing dashboards (engine_v4) ──────────────────────────────────┐
│  v4_backtest_page.html          Apr 26  conviction + breakout      │
│  v4_scalp_backtest_page.html    Apr 27  scalp backtest             │
│                                                                    │
│  Click → opens in new tab from output/archive/ (copied at build)   │
└────────────────────────────────────────────────────────────────────┘
┌─ Other archives ───────────────────────────────────────────────────┐
│  engine_scalp/scalp_backtest_dashboard.html                        │
│  live_engine/trade_dashboard.html                                  │
│  live_engine/live_dashboard.html                                   │
└────────────────────────────────────────────────────────────────────┘
┌─ Methodology divergence notes ─────────────────────────────────────┐
│ Brief writeup of how the new page differs from each archive page   │
│ (cost model, ATR ladder, dedup, etc.)                              │
└────────────────────────────────────────────────────────────────────┘
```

### 11f. Common chrome

- Caveat strip: "8% RT cost on options · 0% on underlying · STOP-first headline · TARGET-first as upper bound"
- Footer: data sources, methodology, version, last cache refresh
- Numbers click-through to underlying signal table filtered to that bucket

---

## 12. Project layout & dependencies

```
scalp_backtest/
├── DESIGN.md                          ← this file (v4)
├── README.md                          ← run instructions (later)
├── src/
│   ├── __init__.py
│   ├── loader_scalp.py                ← engine_v4 jsonl → Signal/Bar
│   ├── loader_swing.py                ← read swing shadow log
│   ├── swing_shadow_logger.py         ← polls swing snapshots, writes JSONL (no engine touch)
│   ├── outcome_intraday.py            ← Signal+1m-bars → Outcome (ATR + pct ladders)
│   ├── outcome_daily.py               ← Signal+daily-bars → Outcome
│   ├── macro_overlay.py               ← VIX bucket + SPX regime fetch
│   ├── slicer.py                      ← shared (Outcome → slice dicts)
│   ├── stats.py                       ← Wilson CI, EV/fire, distributions
│   ├── ml_readiness.py                ← per-class days × fires ledger
│   ├── render.py                      ← unified HTML renderer (5 tabs)
│   └── pipeline.py                    ← orchestration entrypoint
├── data/                              ← cached intermediates
│   ├── signals_scalp.pkl
│   ├── signals_swing.pkl
│   ├── swing_signals.jsonl            ← shadow logger output
│   ├── bars_intraday.pkl
│   ├── bars_daily.pkl
│   ├── outcomes_scalp.pkl
│   ├── outcomes_swing.pkl
│   ├── ml_readiness.json
│   └── slices/*.json
└── output/
    ├── index.html                     ← THE PAGE
    └── archive/                       ← copied existing dashboards
        ├── v4_backtest_page.html
        ├── v4_scalp_backtest_page.html
        └── ...
```

**Dependencies:** stdlib + Plotly via CDN. `pandas` optional in slicer (still TBD per Q13). For VIX/SPX overlay, `yfinance` (cheap, free) — but only if you OK adding it. Without it, `vix_bucket` and `spx_regime` are `None` and the fields just sit dormant.

**External code touched:** NONE. Engine_v4, swing_engine_v2, swing_trade_strategy are read-only. Shadow logger reads snapshot JSON files those processes already write.

---

## 13. Edge cases & display rules

| case | handling |
|---|---|
| Signal but no bars | record `n_bars=0`, exclude from stats, surface in data-quality panel |
| Signal too late for horizon | shorter horizons present, longer = `None`, sample size shown |
| Same ticker re-fires within session | dedup ON by default (1 fire per ticker per session per kind, per item 2). Raw view as toggle. |
| Recorded entry_price 0/null | dropped, counted in data-quality |
| Bar gap > 1 unit | use whatever bars exist; degrades gracefully |
| Side ambiguity | use recorded `kind` field's CALL/PUT marker |
| Detected_utc inside 1-min bar boundary | floor to bar start; lose ≤59s of in-bar move |
| Wide-range bar touches both target and stop | run BOTH passes — STOP-first (headline) and TARGET-first (upper bound), per item 11 |
| Heatmap cell with `n < 5` | render as gray "·", not colored — per item 12 |
| `n_unique_trading_days < 30` for any kind in ML tab | not ready, regardless of fire count — per item 7 |
| Shadow logger misses sub-poll-cadence transitions | documented caveat; cadence tunable |
| ATR(14) undefined (fewer than 14 bars before signal) | atr_14 = `None`, signal still scored on pct ladder, ATR ladder skipped for that row |
| VIX/SPX overlay unavailable | vix_bucket/spx_regime = `None`, fields sit dormant |

---

## 14. Open questions for you

Defaults shown — reply with overrides only.

### A. Naming
1. **Folder name** — keep `scalp_backtest/` (default) or rename?

### B. Scalp specifics
2. **ATR ladder multipliers** — sketched **0.75 / 1.5 / 2.25 (targets), 1.0 (stop)** per item 3. Different?
3. **Pct ladder (secondary)** — sketched **−0.40 / +0.50 / +1.00 / +1.50** per item 3. Different?
4. **Option flat cost** — sketched **8% RT** per item 6. Different? (Sensitivity panel optional.)
5. **Option leverage multiplier** — sketched **5.5×** scalp. Different?
6. **Dedup default** — confirmed **ON** (1/ticker/session/kind) per item 2.
7. **Confidence band cuts** — sketched **0–30 / 30–60 / 60+** per item 4. Different?
8. **Liquidity filter** — drop <500k ADV, or include all? (Default: include all.)

### C. Swing specifics
9. **Swing data source** — confirmed **shadow logger** per item 8. Locked.
10. **Swing ATR ladder** — same multipliers as scalp (0.75/1.5/2.25/-1.0) on **daily** ATR. Different?
11. **Swing pct ladder (secondary)** — sketched **−5 / +3 / +6 / +10** per item 3. Different?
12. **Swing option leverage** — sketched **2.0×**. Different?
13. **Shadow logger poll cadence** — sketched **60s**. Different?

### D. ML readiness — must-fix items 1, 7
14. **Min unique trading days** — sketched **30**. Different?
15. **Min deduped fires** — sketched **100**. Different?
16. **Marginal-label band** — sketched **±2%** post-cost break-even. Different?

### E. Macro overlay — must-fix item 5
17. **Add `yfinance` dependency** for VIX + SPY daily? (Without it, fields are `None`.)
18. **VIX bucket cuts** — sketched **<15 / 15–25 / 25+**. Different?
19. **SPX regime def** — sketched **uptrend = SPY > 50d MA > 200d MA · downtrend = inverse · range = otherwise**. Different?

### F. Headline
20. **Headline EV column** — sketched **option-5.5× STOP-first** (Q-trader's actual experience). Different?

### G. Technical
21. **Pandas allowed?** Default: stdlib only.

---

## 15. Build order — milestones

```
┌──────────────────────────────────────────────────────────────────────┐
│  M1  Clean slate     → skeleton modules                             │
│  M2  Scalp loader    → 596 fires + 305 bar series                   │
│  M3  Scalp outcomes  → ATR ladder + pct ladder + cost-aware label   │
│  M4  Swing shadow    → swing_shadow_logger.py + initial historic    │
│       logger            scrape from JSON snapshots                   │
│  M5  Macro overlay   → vix_bucket + spx_regime (yfinance OPTIONAL)  │
│  M6  Stats + slicer  → Wilson CIs + EV/fire + n<5 suppression       │
│  M7  ML readiness    → days × fires ledger (engine_v4-scoped only)  │
│  M8  HTML page       → 5 tabs (Overview/Scalp/Swing/ML/Archive),   │
│                        archive copies, brand-styled                  │
└──────────────────────────────────────────────────────────────────────┘
```

| ms | est | output | verify |
|---|---|---|---|
| M1 | 5 min | skeleton modules | `python3 src/pipeline.py --help` runs |
| M2 | 30 min | `signals_scalp.pkl` (596) + `bars_intraday.pkl` (305) | print summary matches §3f totals |
| M3 | 60 min | `outcomes_scalp.pkl` with ATR ladder, pct ladder, cost-aware labels | spot-check NVDA CALL_MORNING — atr_14, both first_event passes, label_option_5p5x |
| M4 | 60 min | `swing_shadow_logger.py` + initial JSONL backfill | run logger 5 min, verify state-transitions captured |
| M5 | 30 min | `macro_overlay.py` filling vix/spx fields | (skip if yfinance not approved) |
| M6 | 60 min | `stats.json` + `slices/*.json` | print kind table with EV/fire to stdout |
| M7 | 30 min | `ml_readiness.json` | print per-class days × fires ledger |
| M8 | 150 min | `output/index.html` + archive copies | open in browser, scroll all 5 tabs |

**Total: ~7 hours focused build.**

---

## 16. Pause points & review gates

1. **Before any code** — sign off on this v4 doc (§14 questions answered)
2. **After M3** — show kind-table numbers in stdout BEFORE rendering. Lets us tune ATR multipliers / cost model before committing to HTML.
3. **After M4** — show shadow logger output sample (5 minutes of polling) BEFORE building swing tab.
4. **After M8** — page in browser, sign off, ship it.

---

## 17. Non-goals (explicit, prevent scope creep)

- Not modifying `engine_v4/` in any way (read-only)
- Not modifying `swing_engine_v2/` or `swing_trade_strategy/` (shadow logger reads snapshots only — item 8)
- Not training a real ML model (readiness ledger only)
- Not implementing v2.2 phase gates / posterior models / EV-curve picker / `allow_s5_trade` (item 1 — separate project)
- Not modeling option chain / IV / theta
- Not modeling slippage / spread (only flat round-trip cost)
- Not running anything live or in real time
- Not auto-deploying / scheduling — manual `python3 src/pipeline.py` for now
- Not storing in Postgres / Redis (local pickle/JSON sufficient)
- Not surfacing vix_bucket / spx_regime / near_miss_eligible in v1 UI (stored only — items 5)
- No ✅⚠️❌ recommendation column in v1 (item 9)

---

## 18. What this unlocks downstream

1. **Decide which signals to keep** with EV/fire + filter-band evidence (cost-aware)
2. **Tune live engine** — gate fires from kinds with EV ≤ 0 in their target band
3. **Train a real ML model later** — `outcomes_*.pkl` files become training data; v2.2 build (separate project) consumes
4. **Track edge decay** — re-run weekly, watch EV/fire per kind drift over time
5. **Promote signals** from rules → ML when the readiness ledger says (≥30 days × ≥100 deduped fires)
6. **Cross-reference** with archived backtest pages (v4_backtest_page, v4_scalp_backtest_page) to see methodology divergence

---

## 19. Changes from v3

The 14 must-fix items applied:

| # | item | section(s) updated |
|---|---|---|
| 1 | Scope statement: engine_v4 only, v2.2 separate | top scope box, §2, §5, §11d, §17 |
| 2 | Default dedup ON (1/ticker/session/kind) | §11b table toggle, §13, §14 Q6 |
| 3 | ATR-based ladders primary, pct secondary | §8c, §8d, §8h Outcome dataclass, §14 Q2-3 |
| 4 | Three filter bands per kind row | §9, §11b kind table layout |
| 5 | Add vix_bucket, spx_regime, near_miss_eligible | §8h Outcome, §9 (stored only), §17 |
| 6 | Cost-aware label (8% RT options, 0% underlying) | §8f, §8h, §14 Q4 |
| 7 | Readiness threshold = days × fires | §11d, §14 Q14-15 |
| 8 | Swing data via shadow logger (no engine patch) | §4c, §15 M4, §17 |
| 9 | No ✅⚠️❌ recommendation column in v1 | §11b, §17 |
| 10 | Realized EV/fire as headline (not win%) | §8g, §11b kind table, §11a KPIs |
| 11 | Show STOP-first AND TARGET-first wide-bar passes | §8e, §8h, §11b table format |
| 12 | n<5 cell suppression on heatmaps | §10, §11b heatmap, §13 |
| 13 | n_deduped/n_raw in tab counts | §11 sticky header |
| 14 | ATR(14) computed at signal time, stored | §8c, §8h Outcome dataclass |

Plus added: ARCHIVE tab (§11e) for integrating existing dashboards.


---

## §20 — Swing-Setup Architecture (multi-day, daily bars) — outline only

**Status**: design-only. NOT for engine_v4 modification (this project is read-only on
engine_v4/swing_engine_v2/swing_trade_strategy). This section captures what a future
swing-aware engine would look like, so when an engine owner picks it up they have the
shape ready.

### Purpose

Engine_v4 is intraday-only — every signal lives and dies inside the session. Swing
setups need a multi-day state machine that survives overnight, tracks daily-bar
context, and triggers on confluence over 3-20 trading days.

### Inputs

- **Daily OHLCV** for the universe (Polygon/Alpaca/yfinance) — 252-day rolling window
- **Daily-relative-volume** vs 20-day median
- **Pivot/level history** — 3-month, 6-month, 1-year highs/lows
- **News-sentiment + earnings calendar** (already wired in swing_trade_strategy)
- **Regime overlay** — VIX bucket + SPX 5d/20d trend (deps-free stub at macro.py)

### State machine (per ticker)

```
DORMANT
   │
   │ daily-bar pivot break (close > 20d high, vol > 1.5× avg)
   ▼
WATCH (1-3 days)
   │
   │ 3-day consolidation above pivot (no fail-back)
   ▼
ARMED
   │
   │ next-day breakout bar opens above ARMED level
   ▼
TRIGGER → emit SwingSignal (kind: SWING_BREAKOUT_<TIER>)
   │
   ▼
ACTIVE (1-30 days, monitored daily)
   │   target: 5-15% underlying move
   │   stop:   close below ARMED level
   │   time:   exit after target_dte expires
   ▼
EXIT (TARGET / STOP / TIME)
```

### Why a state machine

Probe-style entries (current swing_engine_v2 PROBE_PUT/PROBE_CALL) emit on a single
snapshot signal — no waiting for confirmation. That's fine for compression detection
but produces noise: half of PROBEs fail same-day. A WATCH→ARMED→TRIGGER state machine
forces confluence:
1. Break must hold for 3 days before ARMED — kills failed-breakouts
2. TRIGGER requires next-day open > ARMED — kills overnight reversals
3. ACTIVE has monitored daily exit gates — no infinite hold

### Kinds we'd add

| Kind | Tier | Trigger | Target horizon |
|---|---|---|---|
| SWING_BREAKOUT_T1 | high-conviction | 20d high + earnings catalyst + bullish news | 5-15 days |
| SWING_BREAKOUT_T2 | medium | 20d high + RVOL ≥ 1.5× | 5-10 days |
| SWING_BREAKOUT_T3 | exploratory | 10d high only | 3-7 days |
| SWING_FAIL_T1 | high-conviction short | 20d low + bearish news | 5-15 days |
| SWING_REVERSAL | counter-trend | RSI<25 + capitulation volume | 3-7 days |

### Scalp ↔ swing handoff

A scalp engine_v4 fire on the same ticker as an ACTIVE swing creates a **stack** —
double-position risk. The swing-aware engine should:
1. On scalp fire: check if ticker has ACTIVE swing → if yes, mark scalp as "stacked"
   and route to a tighter risk bucket
2. On swing TRIGGER: flag any open scalp as "merged" — extend stop to swing stop

This is just data plumbing — no engine logic change for engine_v4 if the swing
engine emits to a known location and engine_v4 reads it (currently engine_v4 doesn't
read v2_active_portfolio.json — it could, no modification needed to start).

### Outcome model

Mirrors `outcome_swing.py` (already built, currently no daily-bar source):
- horizon returns: 1d, 3d, 5d, 10d, 20d, to-expiration
- target/stop in underlying-equivalent (proxy until option-quote history exists)
- cost-aware labels: `label_underlying` (0% RT), `label_option_5p5x` (8% RT × 5.5×)
- realized EV per fire as headline

### What's missing for this to ship

1. **Daily-bar feed**: Polygon/Alpaca daily endpoint OR yfinance (deps approval)
2. **State persistence**: DORMANT/WATCH/ARMED/ACTIVE state in JSON per ticker
3. **Daily polling cron**: 16:00 ET evaluation against close
4. **Universe selection**: ~50-200 tickers, refreshed weekly

The shadow logger handles ingestion; outcomes_swing handles math; what's left is a
producer (the actual swing-aware engine) — but that's outside this read-only project.

### Why this lives in DESIGN.md and not in code

This project's contract is read-only on engine_v4/swing_engine_v2/swing_trade_strategy.
Building the producer here would either (a) duplicate the producer (drift risk) or
(b) modify the upstream engines (constraint violation). Documenting the architecture
here lets the engine owner pick it up cleanly — the consumer side (loader_swing,
outcome_swing, ML readiness) is already wired and will populate as fires arrive.

