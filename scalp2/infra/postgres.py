"""infra/postgres.py — Postgres connection + ORM. Schemas in §10.3.

5 tables: decision_log, candidate_transitions, trades, ml_models, slippage_log.
Migrations via alembic per spec §10.8 build recipe.

TODO Sprint 1.
"""
from __future__ import annotations

import os

POSTGRES_URL_ENV = "POSTGRES_URL"


def get_pool():
    """Return asyncpg pool connected via POSTGRES_URL."""
    raise NotImplementedError("Sprint 1")


# DDL strings reproduced from spec §10.3 — for alembic / setup scripts
DECISION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS decision_log (
    id              BIGSERIAL PRIMARY KEY,
    timestamp       TIMESTAMPTZ NOT NULL,
    strategy        TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    candidate_id    UUID NOT NULL,
    direction       TEXT CHECK (direction IN ('long','short')),
    gex_level       DOUBLE PRECISION,
    gex_strike_dte  INTEGER,
    gex_snapshot_age_min INTEGER,
    distance_to_gex_atr DOUBLE PRECISION,
    reversal_state  TEXT,
    deterministic_score DOUBLE PRECISION,
    fake_breakout_check BOOLEAN,
    ml_prob         DOUBLE PRECISION,
    ml_threshold_used DOUBLE PRECISION,
    model_version   TEXT,
    features_hash   TEXT NOT NULL,
    decision        TEXT CHECK (decision IN ('TRADE','PASS')),
    pass_reason     TEXT,
    instrument      TEXT,
    expected_value  DOUBLE PRECISION,
    risk_dollars    DOUBLE PRECISION,
    iv_at_entry     DOUBLE PRECISION,
    order_id        TEXT,
    CONSTRAINT pass_requires_reason CHECK (
        decision = 'TRADE' OR (decision = 'PASS' AND pass_reason IS NOT NULL)
    )
);
CREATE INDEX IF NOT EXISTS idx_decision_log_ts ON decision_log (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decision_log_strategy ON decision_log (strategy, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decision_log_ticker ON decision_log (ticker, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decision_log_model ON decision_log (model_version);
"""
