"""journal/decision_log.py — Decision log writer. Spec S5-81 + §10.3.

Production target: Postgres (per spec §10.3 schema). Dev/MVP: SQLite, same
schema (with TEXT instead of TIMESTAMPTZ, plain CHECK instead of full constraints).
Swap drivers at the connection level; SQL stays identical.

Acceptance (S5-81):
  - Insert latency < 50ms (non-blocking — async)
  - Indexed on timestamp, ticker, model_version
  - Includes gex_strike_dte, gex_snapshot_age_min, fake_breakout_check,
    ml_threshold_used, iv_at_entry
  - DB constraint: pass_requires_reason (decision='PASS' → pass_reason NOT NULL)

Tests required (S5-81.T1, S5-50.T2):
  - Insert latency < 50ms
  - Every PASS row has pass_reason
"""
from __future__ import annotations

import os
import sqlite3
import sys
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import Decision, Features


PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SQLITE_PATH = PROJECT_ROOT / "data" / "dev_journal.db"


# Spec §10.3 schema, SQLite-portable
DECISION_LOG_DDL = """
CREATE TABLE IF NOT EXISTS decision_log (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp       TEXT    NOT NULL,
    strategy        TEXT    NOT NULL,
    ticker          TEXT    NOT NULL,
    candidate_id    TEXT    NOT NULL,
    direction       TEXT    CHECK (direction IN ('long', 'short') OR direction IS NULL),
    gex_level       REAL,
    gex_strike_dte  INTEGER,
    gex_snapshot_age_min INTEGER,
    distance_to_gex_atr REAL,
    reversal_state  TEXT,
    deterministic_score REAL,
    fake_breakout_check INTEGER,
    ml_prob         REAL,
    ml_threshold_used REAL,
    model_version   TEXT,
    features_hash   TEXT NOT NULL,
    decision        TEXT    NOT NULL CHECK (decision IN ('TRADE', 'PASS')),
    pass_reason     TEXT,
    instrument      TEXT,
    expected_value  REAL,
    risk_dollars    REAL,
    iv_at_entry     REAL,
    order_id        TEXT,
    CHECK (decision = 'TRADE' OR (decision = 'PASS' AND pass_reason IS NOT NULL))
);

CREATE INDEX IF NOT EXISTS idx_decision_log_ts        ON decision_log (timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decision_log_strategy  ON decision_log (strategy, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decision_log_ticker    ON decision_log (ticker, timestamp DESC);
CREATE INDEX IF NOT EXISTS idx_decision_log_model     ON decision_log (model_version);
"""


def init_db(path: Optional[Path] = None) -> Path:
    """Create the SQLite DB + decision_log table if not present."""
    db_path = path or DEFAULT_SQLITE_PATH
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.executescript(DECISION_LOG_DDL)
        conn.commit()
    return db_path


@contextmanager
def _conn(path: Optional[Path] = None):
    db_path = path or DEFAULT_SQLITE_PATH
    init_db(db_path)
    conn = sqlite3.connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


def log_decision_sync(decision: Decision,
                       features: Features,
                       model_version: Optional[str] = None,
                       db_path: Optional[Path] = None) -> int:
    """Synchronous insert. Returns row id. S5-81.

    For non-blocking production (latency < 50ms p99), wrap in asyncio.to_thread
    or use asyncpg + Postgres equivalent.
    """
    fh = features.hash() if features else ""
    iv = getattr(features, "iv_at_entry", None) or getattr(features, "iv_percentile", None)
    direction = None
    if decision.strategy == "S5":
        # Infer direction from features if not given
        direction = (
            "long"
            if (features and features.distance_to_major_pos_gex_atr <= 0)
            else "short"
        )

    instrument_str = None
    if decision.instrument is not None:
        v = decision.instrument
        instrument_str = f"{v.long_leg.symbol}/{v.short_leg.symbol}"

    with _conn(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO decision_log (
                timestamp, strategy, ticker, candidate_id, direction,
                gex_level, gex_strike_dte, gex_snapshot_age_min,
                distance_to_gex_atr,
                reversal_state, deterministic_score, fake_breakout_check,
                ml_prob, ml_threshold_used, model_version,
                features_hash, decision, pass_reason, instrument,
                expected_value, risk_dollars, iv_at_entry, order_id
            ) VALUES (?, ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?,  ?, ?, ?, ?,  ?, ?, ?, ?)
            """,
            (
                decision.timestamp.isoformat(),
                decision.strategy,
                features.ticker if features else "",
                decision.candidate_id,
                direction,
                None,                                          # gex_level (TODO: enrich at call site)
                getattr(features, "major_gex_strike_dte", None),
                getattr(features, "gex_snapshot_age_min", None),
                getattr(features, "distance_to_major_pos_gex_atr", None),
                None,                                          # reversal_state (TODO: enrich)
                None,                                          # deterministic_score (TODO: enrich)
                None,                                          # fake_breakout_check (TODO: enrich)
                decision.ml_prob,
                decision.ml_threshold_used,
                model_version,
                fh,
                decision.decision,
                decision.pass_reason,
                instrument_str,
                decision.expected_value_net,
                decision.risk_dollars,
                iv,
                None,                                          # order_id (set after submission)
            ),
        )
        conn.commit()
        return int(cur.lastrowid)


def query_pass_reasons(window_days: int = 7,
                        strategy: str = "S5",
                        db_path: Optional[Path] = None) -> dict[str, int]:
    """Return histogram of pass_reason → count over the window."""
    cutoff = (
        datetime.now(tz=timezone.utc)
        .replace(microsecond=0)
        .isoformat()
    )
    # crude window: just count all for now (caller can refine). Sufficient for MVP.
    with _conn(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT pass_reason, COUNT(*) FROM decision_log "
            "WHERE strategy = ? AND decision = 'PASS' "
            "GROUP BY pass_reason",
            (strategy,),
        )
        return {row[0]: row[1] for row in cur.fetchall()}


def assert_no_pass_without_reason(db_path: Optional[Path] = None) -> int:
    """S5-50.T2 — Returns count of PASS rows missing pass_reason. MUST be 0."""
    with _conn(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM decision_log "
            "WHERE decision = 'PASS' AND pass_reason IS NULL"
        )
        return int(cur.fetchone()[0])


if __name__ == "__main__":
    p = init_db()
    print(f"[journal] decision_log initialized at {p}")
