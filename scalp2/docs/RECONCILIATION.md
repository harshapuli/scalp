# Reconciliation — inferred scalp2/DESIGN.md vs canonical spec

**Spec docs found at** `docs/spec/trading_system_lifecycle.docx` and `docs/spec/trading_system_jira_import.csv` (originally `~/Downloads/Swing and scalp/`). These are the canonical documents referenced in `scalp_backtest/DESIGN.md` line 4. They were located via Spotlight (`mdfind`) after initial filesystem search missed them.

**Status:** scalp2/DESIGN.md was inferred from references — the inference captured the *spirit* (gating + sizing + posterior + EV-curve + Kelly) but missed the *scope* (4-layer architecture spanning Foundation → Scalp Brain → Swing Brain → S5) and the *sequencing* (24-26 sprints, 17 epics, 189 Jira rows).

---

## Scope drift — what I inferred vs what the spec defines

### My inferred scaffold (5 components, all S5-specific)

```
scalp2/
├── DESIGN.md                  ← inferred, 5-component view
├── src/
│   ├── consume_scalp1.py      ← M1 done, loads scalp_backtest artifacts
│   ├── phase_gate.py          ← OBSERVE/PAPER/LIVE_SMALL/LIVE_FULL state machine
│   ├── posterior_model.py     ← Bayesian update on scalp 1 GBM
│   ├── ev_curve.py            ← Per-kind EV curves
│   ├── kelly_progression.py   ← Fractional Kelly + 5% cap
│   ├── position_ledger.py     ← JSONL writer
│   ├── pipeline.py            ← Orchestrator
│   └── renderer.py            ← Dashboard
└── tests/                     ← 4 test files, 29 passing
```

### What the spec actually defines (4 layers, 17 epics, ~70 modules)

```
trading_system/
├── data_clients/              ← Foundation Layer (FND-E1)
│   ├── unusual_whales.py      ← UW: GEX, flow, dark pool, sweeps, IV term
│   ├── polygon_ws.py          ← Polygon WebSocket: 1m bars, trades, NBBO
│   ├── polygon_rest.py        ← Polygon REST: historical bars, option chains
│   └── alpaca.py              ← Alpaca: equity brackets, mleg verticals
├── features/                  ← Per-bar Features object (SCALP-1)
│   ├── price_structure.py     ← extension_from_vwap_atr, wick_pct, etc.
│   ├── volume_features.py
│   ├── aggressor.py           ← Lee-Ready classifier, aggressor_velocity
│   ├── gamma_features.py      ← distance_to_pos_gex_atr, gex_strike_dte
│   ├── flow_features.py       ← net_signed_premium_5m, signed_flow_score
│   ├── execution_costs.py     ← bid_ask_spread_pct, option_volume
│   └── builder.py             ← Features object factory + hash
├── scalp_brain/               ← Scalp Brain Layer (SCALP-E1..E4)
│   ├── states.py              ← 7 states: NEUTRAL + SURGE_*/TANK_*
│   ├── classifier.py          ← Per-bar classifier with hysteresis
│   ├── scores.py              ← reversal/ignition/continuation/climax
│   ├── replay.py              ← Historical replay harness
│   └── publisher.py           ← Redis pubsub publisher
├── swing_brain/               ← Swing Brain Layer (SWING-E1..E2)
│   ├── lifecycle.py           ← 7-stage state machine (Inducement→Setup→Trigger→Entry→Manage→Exit→Journal)
│   ├── candidate.py           ← Candidate dataclass + Redis persistence
│   ├── scanner.py             ← 5-min candidate scanner
│   ├── subscriber.py          ← scalp.price_trigger consumer
│   └── bias_map.py            ← Daily Conviction-style bias
├── strategies/
│   ├── s5_gamma_reversal/     ← S5 Strategy Layer (S5-E1..E11)
│   │   ├── setup.py           ← is_s5_setup (§14)
│   │   ├── trigger.py         ← s5_trigger with pierce check (§15)
│   │   ├── fake_breakout.py   ← fake_breakout_reject layer
│   │   ├── ml/
│   │   │   ├── labeler.py     ← label_s5_trade_outcome (§17 + ChatGPT review)
│   │   │   ├── trainer.py     ← LR baseline + LightGBM
│   │   │   ├── calibrate.py   ← Isotonic + reliability bins
│   │   │   ├── threshold.py   ← EV-curve picker (§19)
│   │   │   └── registry.py    ← Model versioning (Postgres)
│   │   ├── allow_s5_trade.py  ← Canonical gate (S5-104)
│   │   └── execution/
│   │       ├── option_selector.py
│   │       └── order_builder.py
│   ├── s1_pre_fomc/           ← Future
│   ├── s2_orb/                ← Future
│   ├── s3_momentum/           ← Future
│   └── s4_signed_flow/        ← Future
├── risk/
│   ├── manager.py             ← Pre-trade gates
│   ├── position_manager.py    ← IV-crush watcher, stops, timeouts
│   └── kill_switch.py         ← Daily loss kill
├── journal/
│   ├── decision_log.py        ← Postgres writer (5 tables in §10.3)
│   ├── attribution.py         ← Dashboard backend
│   └── alerts.py              ← Pushover/Discord
├── infra/
│   ├── redis.py               ← Connection + pubsub helpers
│   ├── postgres.py
│   ├── secrets.py             ← /etc/trading/secrets.env
│   └── fixtures.py            ← S3-cached replay fixtures
├── config/
│   ├── thresholds.yaml        ← ALL strategy thresholds (canonical, §10.4)
│   ├── universe.yaml
│   ├── fomc_calendar.yaml
│   └── earnings_calendar.yaml
└── tests/{unit,integration,replay,critical}/
```

### Total scope per spec

- **17 epics** (2 Foundation + 4 Scalp + 2 Swing + 9 S5)
- **50 stories**
- **47 sub-tasks**
- **75 test cases**
- **189 total Jira rows**
- **~310 story points**
- **~24-26 sprints solo cadence (48-52 weeks calendar)**
- **~16-18 sprints with one helper**

---

## Mapping — what I built that's salvageable

| My module | Maps to spec | Salvage notes |
|---|---|---|
| `scalp2/src/consume_scalp1.py` | NOT in spec — but useful | Keep as `bridges/scalp_backtest_bridge.py`. Lets v2.2 system consume scalp 1's audit data (regime sanity, predictor baseline, alignment quintiles). |
| `scalp2/src/phase_gate.py` (allow_s5_trade signature) | Maps directly to **S5-104** (`strategies/s5_gamma_reversal/allow_s5_trade.py`) | The 4-state machine I built (OBSERVE/PAPER/LIVE_SMALL/LIVE_FULL) is NOT what the spec wants — spec uses S5 phase 0-7 with explicit acceptance gates per phase. My state machine concept is wrong; the gate function signature `allow_s5_trade(features, model, threshold) -> Decision` is correct. |
| `scalp2/src/posterior_model.py` (logit/sigmoid Bayesian update) | Maps to **S5-61** (`strategies/s5_gamma_reversal/ml/`) — per-direction posterior tracker | Spec uses TWO Beta-Binomial trackers (long_after_tank, short_after_surge), not a generic logit-space update. My math is reusable but the wrapping is wrong. |
| `scalp2/src/ev_curve.py` (lookup interpolation) | Maps directly to **S5-35** (`strategies/s5_gamma_reversal/ml/threshold.py`) | The EV-curve picker pseudocode in spec §10.5 is more rigorous than my version (iterates threshold 0.50→0.85, requires min trade count, warns on extremes). My interpolation logic still useful. |
| `scalp2/src/kelly_progression.py` (full Kelly + ramp + 5% cap) | Maps to **S5-E7/E8** (Phase 6/7 ramps) | Spec uses sizing multipliers 0.25× → 0.50× → 1.00× (full Kelly), per-trade cap 0.50% (paper/ramp) → 1.00% (full). My 5% hard cap is too high — spec's per-trade is 0.50%/1.00%. Concept right, numbers wrong. |
| `scalp2/src/position_ledger.py` (JSONL writer) | Doesn't match — spec uses **Postgres `decision_log`** (5 tables in §10.3) | Replace with `journal/decision_log.py` writing to Postgres per §10.3 schema. My JSONL approach is too informal. |
| `scalp2/src/pipeline.py` (batch orchestrator) | Doesn't directly map — spec runs as live daemon (`trading_system.main`) with replay capability separate (`scripts/build_features.py`, `scripts/train_s5_ml.py`) | My batch-mode pipeline is too monolithic. Spec wants live daemon + replay scripts. |
| `scalp2/src/renderer.py` (HTML dashboard) | Maps to **S5-82** (`journal/attribution.py`) | Spec dashboard shows per-strategy hit rate, R, Sharpe over rolling 30/90/365d windows, drift alerts. My placeholder is off-mission. |

---

## Critical immediate blocker — FND-3.1

> Per spec §3.1 (FND-E1, STORY FND-3, SUB-TASK FND-3.1):
>
> **"[BLOCKER] Verify UW GEX endpoint update cadence. Read UW docs and confirm /greek-exposure update frequency. Real-time → 30min staleness holds. 5min → tighten to 10min. EOD only → S5 redesign required. Cannot proceed past Sprint 1 without this answer."**

This is the single most important pre-anything task. If UW GEX is EOD-only, the entire S5 strategy needs redesign because S5 cannot run intraday on stale GEX. The current scalp 1 audit data uses no live GEX (UW API not in scalp_backtest), so this question is genuinely unanswered in our codebase right now.

---

## Proposed restructure path

### Option A — Wholesale restructure NOW

1. Rename `scalp2/` → `trading_system/` (matches spec §10.1 layout)
2. Create the canonical directory tree (data_clients/ features/ scalp_brain/ swing_brain/ strategies/s5_gamma_reversal/ risk/ journal/ infra/ config/ tests/ scripts/ docs/)
3. Migrate salvageable code into new locations
4. Replace inferred scalp2/DESIGN.md with a thin pointer → docs/spec/trading_system_lifecycle.docx
5. Add docker-compose.yml for Postgres + Redis
6. Stub the Foundation Layer: data_clients/{unusual_whales,polygon_ws,polygon_rest,alpaca}.py with FND-1..FND-6 acceptance criteria
7. Mark FND-3.1 as the CRITICAL BLOCKER explicitly

### Option B — Keep scalp2/ name, restructure internals only

1. Keep `scalp2/` as the directory name (user's chosen)
2. Internal layout matches spec §10.1 exactly
3. Migrate salvage as in Option A
4. DESIGN.md reflects the actual spec scope

### Option C — Treat current scalp2/ as deprecated, fresh start

1. Move `scalp2/` to `scalp2_inferred_v0/` (preserves history)
2. Create `trading_system/` fresh per spec
3. Cherry-pick from scalp2_inferred_v0/ as needed

---

## What I'd recommend (for the record, not asking permission)

**Option B**, with the user's chosen `scalp2/` directory name preserved (it's a project nickname, not a structural concern), but internal layout matching spec §10.1 exactly. Rationale:

- The user explicitly named the project "scalp 2" in their lock-in instruction
- The internal restructure is what matters for AI-coder consumption per spec §10.1
- `scalp2/docs/spec/trading_system_lifecycle.docx` becomes the source of truth
- Salvage path is clear (mapping table above)
- FND-3.1 BLOCKER gets surfaced as Sprint 1 pre-requisite

This is a Sprint 0 reconciliation. After this commit lands, Sprint 1 work is FND-3 (UW client + cadence verification), FND-5 (secrets), FND-10 (API docs), FND-11 (UW change monitor) — 16 story points per spec §7.
