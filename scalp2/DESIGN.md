# Scalp 2 — Edge-Centric Trading System v2.2

**Canonical spec:** [`docs/spec/trading_system_lifecycle.docx`](docs/spec/trading_system_lifecycle.docx)
(extracted text: [`docs/spec/trading_system_lifecycle.txt`](docs/spec/trading_system_lifecycle.txt))
**Companion Jira import:** [`docs/spec/trading_system_jira_import.csv`](docs/spec/trading_system_jira_import.csv)

This DESIGN.md is a thin pointer + status snapshot. The full design is in the
spec docs above. If anything in this file conflicts with the spec, the spec wins.

For the reconciliation history (what we inferred before locating the spec, what
got salvaged, what got rebuilt), see [`docs/RECONCILIATION.md`](docs/RECONCILIATION.md).

---

## 1. What this project is

The successor to `scalp_backtest/` (scalp 1, the audit project). Scalp 1 produced
labels + features from the engine_v4 fire archive. Scalp 2 builds the
**production gating + sizing + execution layer** that decides which fires become
live positions and how big.

Per spec §1.1, four layers in dependency order:

| Layer | Modules | Status |
|---|---|---|
| **Foundation** (FND-E1, FND-E2) | `data_clients/{unusual_whales,polygon_ws,polygon_rest,alpaca}.py`, `infra/{secrets,redis,postgres,fixtures,config_loader}.py` | Stubs in place, **Sprint 1 work** |
| **Scalp Brain** (SCALP-E1..E4) | `scalp_brain/{states,classifier,scores,replay,publisher}.py`, `features/{price_structure,volume_features,aggressor,gamma_features,flow_features,execution_costs,builder}.py` | Stubs + canonical reversal_score implemented; rest TODO Sprints 3-6 |
| **Swing Brain** (SWING-E1, E2) | `swing_brain/{lifecycle,candidate,scanner,subscriber,bias_map}.py` | Stubs in place, TODO Sprints 6-8 |
| **S5 Strategy** (S5-E1..E11) | `strategies/s5_gamma_reversal/{setup,trigger,fake_breakout,allow_s5_trade}.py` + `ml/{labeler,trainer,calibrate,threshold,registry,posterior}.py` + `execution/{option_selector,order_builder}.py` + `risk/{manager,kill_switch,position_manager,sizing}.py` + `journal/{decision_log,attribution,alerts}.py` | `allow_s5_trade.py` (S5-104) implemented; `risk/sizing.py` (Kelly with corrected 0.5%/1.0% caps) implemented; `ml/threshold.py` lookup primitives implemented; `ml/posterior.py` Beta-Binomial implemented; rest TODO Sprints 8-15 |

---

## 2. Critical immediate blocker — FND-3.1

**Per spec §3.1, sub-task FND-3.1:**

> **"[BLOCKER] Verify UW GEX endpoint update cadence. Read UW docs and confirm
> /greek-exposure update frequency. Real-time → 30min staleness holds.
> 5min → tighten to 10min. EOD only → S5 redesign required.
> Cannot proceed past Sprint 1 without this answer."**

Run `python3 scripts/verify_foundation.py` to check Sprint 1 readiness. The
script will fail until FND-3.1 is resolved.

---

## 3. Sprint plan (per spec §7)

| Sprint | Layer | Stories | Pts |
|---|---|---|---|
| 1 | Foundation | FND-3 (UW + cadence verify), FND-5 (secrets), FND-10 (API docs), FND-11 (UW change monitor) | 16 |
| 2 | Foundation | FND-1 (Polygon WS), FND-2 (Polygon REST), FND-6 (fixtures) | 13 |
| 3 | Foundation+Scalp | FND-4 (Alpaca), SCALP-1 (Features object) | 13 |
| 4 | Scalp | SCALP-2 (7-state classifier), SCALP-10 (reversal_score) | 13 |
| 5 | Scalp | SCALP-11 (ignition/continuation/climax scores), SCALP-20 (replay), SCALP-21 (parity) | 13 |
| 6 | Scalp+Swing | SCALP-30 (pubsub publisher), SWING-1 (lifecycle state machine) | 13 |
| 7 | Swing | SWING-2 (S5 setup gate), SWING-3 (pierce check), SWING-4 (fake breakout), SWING-10 (scanner) | 13 |
| 8 | Swing+S5-0 | SWING-11 (subscriber), SWING-12 (bias map), S5-1 (gamma proximity validation) | 18 |
| 9 | S5-0 | S5-2 (microstructure validation), S5-3 (composed S5 validation) | 8 |
| 10 | S5-2 | S5-23 (rules-only backtest) | 5 |
| 11 | S5-3 | S5-30 (trade-outcome labeler), S5-31 (dataset), S5-31a (escalation), S5-32 (LR baseline) | 18 |
| 12 | S5-3 | S5-33 (LightGBM), S5-34 (calibration), S5-35 (threshold) | 14 |
| 13 | S5-3+4 | S5-36 (pass gate), S5-37 (registry), S5-40 (holdout comparison), S5-4 (ML sub-edge) | 11 |
| 14 | S5-5 prep | S5-80 (risk manager), S5-81 (decision log), S5-104 (allow_s5_trade), S5-50 (paper wire) | 21 |
| 15 | S5-5 | S5-82 (dashboard), S5-83 (alerts), S5-102 (rollback procs), S5-51 + S5-100 (paper + recalibration) | 12+ |
| 16 | S5-4 re-run | After cost recalibration, re-run Phase 4 if cost delta > 20% | TBD |
| 17+ | S5-6,7 | S5-60, S5-61, S5-70, S5-71 (live ramps) | TBD |

**Total: ~310 story points, 24-26 sprints solo (48-52 weeks calendar).**

---

## 4. Phase gates (no advancement without acceptance gate cleared, per spec §8)

| Phase | Pass criterion | Fail action |
|---|---|---|
| Foundation | All clients reliable, fixtures deterministic, **FND-3.1 UW cadence verified** | Cannot proceed |
| Scalp | Live↔replay parity 0 bars different over 1 trading day | Bug-fix until parity |
| Swing | Scalp publish → swing TRIGGER < 200ms; graceful degradation | Bug-fix integration |
| S5-0 | Gamma adds material lift (≥5pp hit rate or ≥0.30R per trade) | Drop gamma filter; rebrand or kill |
| S5-2 | Net positive EV after costs over 12mo | Audit gates, cost model; if 1 iter still negative, kill |
| S5-3 | AUC ≥ 0.62 OR EV +15%, AND precision ≥ 60% top-decile, AND Brier improvement | Sample-size escalation; if 2 iter fail, ship rules-only |
| S5-4 | ML-gated EV ≥ 1.15× rules-only on identical-cost holdout | Check label leakage; if still failing, ship rules-only |
| S5-5 | Hit rate ±15pp of backtest after 60 days OR 50 candidates | Root-cause, patch, restart timer |
| S5-6 | 30 trades, no drift, slippage <1.5× modeled at 0.25× | Kill switch, halt, decision gate |
| S5-7 | 100 cumulative trades, posterior expectancy > 0 | Fall back to 0.25×, 30 more trades, retire if fails |

---

## 5. What carries forward from scalp 1

These were validated by scalp 1's audit project and remain as ML features for
scalp 2's posterior model (per S5-31 execution-aware feature set):

- `prior_5bar_alignment` — top GBM importance, non-monotonic shape (inverse-U with
  Q2 failure cluster); see scalp_backtest's alignment-quintile probe
- `prior_5bar_volume_z` — top LR coefficient, -0.337 on slow class
- `atr_14_pct` — second-largest LR coef, +0.330 on slow class
- `regime` label (fast/slow per MFE-≥-0.5×ATR-within-30-bars) — durable, +49.6pp
  multi-year T1+ spread validated

Bridge: [`bridges/scalp_backtest_bridge.py`](bridges/scalp_backtest_bridge.py)
loads scalp 1 artifacts (per_signal JSON, predictor baseline, regime sanity).
Run `python3 bridges/scalp_backtest_bridge.py` to verify the bridge works.

---

## 6. Smoke-test status (post-restructure)

- `tests/unit/test_sizing.py` — risk/sizing.py, **passing** (Kelly + per-trade caps + ramp)
- `tests/unit/test_threshold.py` — strategies/s5/ml/threshold.py, **passing** (EV-curve lookup)
- `tests/unit/test_posterior.py` — strategies/s5/ml/posterior.py, **passing** (Beta-Binomial S5-61)
- `tests/unit/test_allow_s5_trade.py` — S5-104 gate function, **passing** (13 tests, all 7 unique pass_reasons covered)
- `bridges/scalp_backtest_bridge.py` CLI — **passing** (loads 4,984 archive fires, predictor baseline, regime sanity)

All other modules are stubs raising `NotImplementedError("<story-id>")` with the
spec's acceptance criteria in docstrings.

---

## 7. Build & run recipe (per spec §10.8)

```bash
# One-time setup
$ python -m venv .venv && source .venv/bin/activate
$ pip install -e '.[dev]'        # pyproject.toml — TODO Sprint 1
$ docker-compose up -d           # Postgres + Redis
$ alembic upgrade head           # tables from §10.3 — TODO Sprint 1
$ cp .env.example /etc/trading/secrets.env && chmod 600 /etc/trading/secrets.env
$ <fill in real keys>

# Sprint 1 acceptance — cannot pass without FND-3.1 cleared
$ python3 scripts/verify_foundation.py
```
