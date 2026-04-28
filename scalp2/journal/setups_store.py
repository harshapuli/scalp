"""journal/setups_store.py — SQLite persistence for daemon setups.

Replaces the spec's Redis pre_staged_<strategy>:{ticker} layer with a single
sqlite table in dev_journal.db. Same purpose: setups survive a daemon restart
mid-session so the dashboard doesn't lose in-flight cards.

Schema:
  setups (
    id              TEXT PRIMARY KEY,    -- "{strategy}-{ticker}-{direction}"
    ticker          TEXT NOT NULL,
    strategy        TEXT NOT NULL,
    direction       TEXT NOT NULL,
    side            TEXT NOT NULL,
    state           TEXT,
    score           REAL,
    stage           TEXT NOT NULL,       -- 'FORMING' | 'TRADE'
    decision        TEXT,
    pass_reason     TEXT,
    entry           REAL,
    stop            REAL,
    target          REAL,
    qty             INTEGER,
    atr             REAL,
    created_at      TEXT NOT NULL,
    last_evaluated_at TEXT NOT NULL,
    taken           INTEGER NOT NULL DEFAULT 0,   -- 1 once user TAKES (or auto-submit fires)
    session_date    TEXT NOT NULL                  -- YYYY-MM-DD UTC for daily cleanup
  )

API:
  init_setups_table(db_path)
  persist_setup(db_path, setup_row)         # INSERT OR REPLACE on id
  delete_setup(db_path, setup_id)
  delete_setups_for_ticker(db_path, ticker, keep_ids)
  mark_setup_taken(db_path, setup_id)
  load_active_setups(db_path, session_date) -> list[dict]
  load_taken_ids(db_path, session_date) -> set[str]
  delete_stale_setups(db_path, before_iso)
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS setups (
    id                TEXT PRIMARY KEY,
    ticker            TEXT NOT NULL,
    strategy          TEXT NOT NULL,
    direction         TEXT NOT NULL,
    side              TEXT NOT NULL,
    state             TEXT,
    score             REAL,
    stage             TEXT NOT NULL,
    decision          TEXT,
    pass_reason       TEXT,
    entry             REAL,
    stop              REAL,
    target            REAL,
    qty               INTEGER,
    atr               REAL,
    created_at        TEXT NOT NULL,
    last_evaluated_at TEXT NOT NULL,
    taken             INTEGER NOT NULL DEFAULT 0,
    session_date      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_setups_active
  ON setups (session_date, taken);
CREATE INDEX IF NOT EXISTS idx_setups_ticker
  ON setups (ticker, taken);
"""


@contextmanager
def _conn(db_path: Path):
    """Short-lived connection. Caller responsible for path validity."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_setups_table(db_path: Path) -> None:
    """Run on daemon boot. Idempotent (CREATE IF NOT EXISTS)."""
    with _conn(db_path) as c:
        c.executescript(_SCHEMA)


def persist_setup(db_path: Path, row: dict) -> None:
    """INSERT OR REPLACE on id. Caller passes the same dict shape used in
    PaperTrader.status.setups (plus a 'session_date' field if not present)."""
    sd = row.get("session_date") or (row.get("created_at") or "")[:10]
    with _conn(db_path) as c:
        c.execute(
            """INSERT OR REPLACE INTO setups
              (id, ticker, strategy, direction, side, state, score, stage,
               decision, pass_reason, entry, stop, target, qty, atr,
               created_at, last_evaluated_at, taken, session_date)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                row["id"], row["ticker"], row["strategy"], row["direction"],
                row.get("side") or "buy", row.get("state"), row.get("score"),
                row.get("stage", "FORMING"), row.get("decision"),
                row.get("pass_reason"),
                row.get("entry"), row.get("stop"), row.get("target"),
                row.get("qty"), row.get("atr"),
                row["created_at"], row["last_evaluated_at"],
                int(row.get("taken", 0)), sd,
            ),
        )


def delete_setup(db_path: Path, setup_id: str) -> None:
    with _conn(db_path) as c:
        c.execute("DELETE FROM setups WHERE id = ?", (setup_id,))


def delete_setups_for_ticker(db_path: Path, ticker: str,
                              keep_ids: Iterable[str]) -> None:
    keep = list(keep_ids) or [""]
    placeholders = ",".join("?" * len(keep))
    with _conn(db_path) as c:
        c.execute(
            f"DELETE FROM setups WHERE ticker = ? AND id NOT IN ({placeholders})",
            (ticker, *keep),
        )


def mark_setup_taken(db_path: Path, setup_id: str) -> None:
    with _conn(db_path) as c:
        c.execute("UPDATE setups SET taken = 1 WHERE id = ?", (setup_id,))


def load_active_setups(db_path: Path, session_date: str) -> list[dict]:
    """Return all setups for `session_date` that haven't been taken.
    Used on daemon boot to repopulate `PaperTrader.status.setups`."""
    if not db_path.exists():
        return []
    with _conn(db_path) as c:
        rows = c.execute(
            """SELECT * FROM setups WHERE session_date = ? AND taken = 0
               ORDER BY created_at""",
            (session_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def load_taken_ids(db_path: Path, session_date: str) -> set:
    """Return ids of setups already marked taken — used to repopulate the
    `taken_signal_ids` set so the dashboard hides them after a restart."""
    if not db_path.exists():
        return set()
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT id FROM setups WHERE session_date = ? AND taken = 1",
            (session_date,),
        ).fetchall()
    return {r["id"] for r in rows}


def delete_stale_setups(db_path: Path, before_iso: str) -> int:
    """Drop setups whose last_evaluated_at < before_iso. Returns row count."""
    if not db_path.exists():
        return 0
    with _conn(db_path) as c:
        cur = c.execute(
            "DELETE FROM setups WHERE last_evaluated_at < ? AND taken = 0",
            (before_iso,),
        )
        return cur.rowcount or 0
