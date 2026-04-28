"""journal/intraday_snapshot.py — periodic state snapshots into dev_journal.db.

Every 5 minutes during regular session, dump the daemon's current view
(per-ticker GEX, IV, scalp state, current setups, account equity) so the
EOD analyzer can replay how the day evolved instead of only seeing the
final state.

Two tables:
  intraday_account_snapshot
    (id PK, ts, equity, buying_power, daytrade_count, n_positions,
     unrealized_pl, session_date)

  intraday_ticker_snapshot
    (id PK, ts, ticker, session_date,
     scalp_state, scalp_score,
     gex_spot, gex_gamma_flip, gex_pos_strike, gex_neg_strike,
     iv_percentile, earnings_blackout,
     n_active_setups, n_flow_records_5min,
     setups_json)

At EOD the analyzer pulls these tables, joins per-ticker by session_date,
and produces timeseries: GEX-strike-over-day, IV-rank-over-day,
state-transitions, setup-promotions FORMING→TRADE, account equity curve.

This is the data we mine to decide whether UW WebSockets are worth the
subscription — REST polling captured here, side-by-side comparison
becomes possible once WS data lands.

API:
  init_intraday_tables(db_path)
  snapshot_account(db_path, account_dict)
  snapshot_ticker(db_path, ts, ticker, payload_dict)
  load_account_timeseries(db_path, session_date) -> list[dict]
  load_ticker_timeseries(db_path, ticker, session_date) -> list[dict]
  load_all_ticker_timeseries(db_path, session_date) -> dict[ticker, list[dict]]
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


_SCHEMA = """
CREATE TABLE IF NOT EXISTS intraday_account_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    session_date    TEXT NOT NULL,
    equity          REAL,
    buying_power    REAL,
    daytrade_count  INTEGER,
    n_positions     INTEGER,
    n_open_orders   INTEGER,
    unrealized_pl   REAL,
    market_value    REAL
);
CREATE INDEX IF NOT EXISTS idx_acct_snap_session
  ON intraday_account_snapshot (session_date, ts);

CREATE TABLE IF NOT EXISTS intraday_ticker_snapshot (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              TEXT NOT NULL,
    session_date    TEXT NOT NULL,
    ticker          TEXT NOT NULL,
    scalp_state     TEXT,
    scalp_score     REAL,
    gex_spot        REAL,
    gex_gamma_flip  REAL,
    gex_pos_strike  REAL,
    gex_neg_strike  REAL,
    gex_age_min     INTEGER,
    iv_percentile   REAL,
    earnings_blackout INTEGER,
    n_active_setups INTEGER,
    n_flow_records_5min INTEGER,
    setups_json     TEXT
);
CREATE INDEX IF NOT EXISTS idx_tick_snap_session
  ON intraday_ticker_snapshot (session_date, ticker, ts);
CREATE INDEX IF NOT EXISTS idx_tick_snap_ticker
  ON intraday_ticker_snapshot (ticker, ts);

-- Raw UW data dumps — every endpoint snapshotted as JSON so we can mine
-- flow records, full GEX strike maps, IV term structure later. The user's
-- explicit ask: UW data is what we can't recover later (Alpaca bars are
-- queryable historically anytime).
CREATE TABLE IF NOT EXISTS intraday_uw_snapshot (
    id                      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                      TEXT NOT NULL,
    session_date            TEXT NOT NULL,
    ticker                  TEXT NOT NULL,
    -- Full UW /spot-exposures payload as JSON (gex_by_strike map + spot + flip)
    gex_full_json           TEXT,
    -- All flow records seen in this 5-min cache window as JSON list
    flow_records_json       TEXT,
    n_flow_records          INTEGER,
    -- IV-rank latest entry as JSON
    iv_rank_latest_json     TEXT,
    -- IV term structure as JSON (heavier — only every N snapshots)
    iv_term_json            TEXT,
    -- Earnings flow summary as JSON
    earnings_summary_json   TEXT
);
CREATE INDEX IF NOT EXISTS idx_uw_snap_session_tk
  ON intraday_uw_snapshot (session_date, ticker, ts);
"""


@contextmanager
def _conn(db_path: Path):
    db_path.parent.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(str(db_path))
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


def init_intraday_tables(db_path: Path) -> None:
    with _conn(db_path) as c:
        c.executescript(_SCHEMA)


# ─── Writers ────────────────────────────────────────────────────────────────


def snapshot_account(db_path: Path, payload: dict) -> None:
    """Insert one account-snapshot row. Caller passes:
    {ts, session_date, equity, buying_power, daytrade_count,
     n_positions, n_open_orders, unrealized_pl, market_value}"""
    with _conn(db_path) as c:
        c.execute(
            """INSERT INTO intraday_account_snapshot
              (ts, session_date, equity, buying_power, daytrade_count,
               n_positions, n_open_orders, unrealized_pl, market_value)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload["ts"], payload["session_date"],
                payload.get("equity"), payload.get("buying_power"),
                payload.get("daytrade_count"), payload.get("n_positions"),
                payload.get("n_open_orders"), payload.get("unrealized_pl"),
                payload.get("market_value"),
            ),
        )


def snapshot_uw(db_path: Path, payload: dict) -> None:
    """Insert one raw-UW-dump row per ticker per snapshot time."""
    with _conn(db_path) as c:
        c.execute(
            """INSERT INTO intraday_uw_snapshot
              (ts, session_date, ticker,
               gex_full_json, flow_records_json, n_flow_records,
               iv_rank_latest_json, iv_term_json, earnings_summary_json)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload["ts"], payload["session_date"], payload["ticker"],
                json.dumps(payload.get("gex_full"), default=str) if payload.get("gex_full") else None,
                json.dumps(payload.get("flow_records") or [], default=str),
                payload.get("n_flow_records", 0),
                json.dumps(payload.get("iv_rank_latest"), default=str) if payload.get("iv_rank_latest") else None,
                json.dumps(payload.get("iv_term"), default=str) if payload.get("iv_term") else None,
                json.dumps(payload.get("earnings_summary"), default=str) if payload.get("earnings_summary") else None,
            ),
        )


def load_uw_timeseries(db_path: Path, ticker: str, session_date: str) -> list[dict]:
    if not db_path.exists():
        return []
    with _conn(db_path) as c:
        rows = c.execute(
            """SELECT * FROM intraday_uw_snapshot
               WHERE ticker = ? AND session_date = ?
               ORDER BY ts""",
            (ticker, session_date),
        ).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        # Parse JSON columns back into objects for the analyzer
        for k in ("gex_full_json", "flow_records_json", "iv_rank_latest_json",
                   "iv_term_json", "earnings_summary_json"):
            if d.get(k):
                try:
                    d[k.replace("_json", "")] = json.loads(d[k])
                except Exception:
                    pass
        out.append(d)
    return out


def load_all_uw_timeseries(db_path: Path, session_date: str) -> dict[str, list[dict]]:
    if not db_path.exists():
        return {}
    with _conn(db_path) as c:
        rows = c.execute(
            "SELECT * FROM intraday_uw_snapshot WHERE session_date = ? ORDER BY ticker, ts",
            (session_date,),
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        for k in ("gex_full_json", "flow_records_json", "iv_rank_latest_json",
                   "iv_term_json", "earnings_summary_json"):
            if d.get(k):
                try:
                    d[k.replace("_json", "")] = json.loads(d[k])
                except Exception:
                    pass
        out.setdefault(d["ticker"], []).append(d)
    return out


def snapshot_ticker(db_path: Path, payload: dict) -> None:
    """Insert one per-ticker snapshot row."""
    setups_json = json.dumps(payload.get("setups") or [], default=str)
    with _conn(db_path) as c:
        c.execute(
            """INSERT INTO intraday_ticker_snapshot
              (ts, session_date, ticker, scalp_state, scalp_score,
               gex_spot, gex_gamma_flip, gex_pos_strike, gex_neg_strike,
               gex_age_min, iv_percentile, earnings_blackout,
               n_active_setups, n_flow_records_5min, setups_json)
              VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                payload["ts"], payload["session_date"], payload["ticker"],
                payload.get("scalp_state"), payload.get("scalp_score"),
                payload.get("gex_spot"), payload.get("gex_gamma_flip"),
                payload.get("gex_pos_strike"), payload.get("gex_neg_strike"),
                payload.get("gex_age_min"),
                payload.get("iv_percentile"),
                int(bool(payload.get("earnings_blackout"))),
                payload.get("n_active_setups", 0),
                payload.get("n_flow_records_5min", 0),
                setups_json,
            ),
        )


# ─── Readers ────────────────────────────────────────────────────────────────


def load_account_timeseries(db_path: Path, session_date: str) -> list[dict]:
    if not db_path.exists():
        return []
    with _conn(db_path) as c:
        rows = c.execute(
            """SELECT * FROM intraday_account_snapshot
               WHERE session_date = ? ORDER BY ts""",
            (session_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def load_ticker_timeseries(db_path: Path, ticker: str,
                              session_date: str) -> list[dict]:
    if not db_path.exists():
        return []
    with _conn(db_path) as c:
        rows = c.execute(
            """SELECT * FROM intraday_ticker_snapshot
               WHERE ticker = ? AND session_date = ?
               ORDER BY ts""",
            (ticker, session_date),
        ).fetchall()
    return [dict(r) for r in rows]


def load_all_ticker_timeseries(db_path: Path, session_date: str) -> dict[str, list[dict]]:
    if not db_path.exists():
        return {}
    with _conn(db_path) as c:
        rows = c.execute(
            """SELECT * FROM intraday_ticker_snapshot
               WHERE session_date = ? ORDER BY ticker, ts""",
            (session_date,),
        ).fetchall()
    out: dict[str, list[dict]] = {}
    for r in rows:
        d = dict(r)
        out.setdefault(d["ticker"], []).append(d)
    return out


# ─── Daemon entry point — called every N minutes ────────────────────────────


_SNAPSHOT_TICK_COUNTER = {"count": 0}


def run_snapshot(db_path: Path, trader, alpaca_account: Optional[dict] = None,
                  positions: Optional[list[dict]] = None,
                  uw_client = None) -> dict:
    """Capture the current daemon view + Alpaca account + raw UW dumps.

    Heavy UW endpoints (iv_term_structure, earnings_flow_summary) are pulled
    only every 5th call (~25 min) to stay rate-limit safe."""
    from dataclasses import asdict

    now = datetime.now(tz=timezone.utc)
    session_date = now.date().isoformat()
    ts = now.isoformat(timespec="seconds") + "Z"

    init_intraday_tables(db_path)

    _SNAPSHOT_TICK_COUNTER["count"] += 1
    is_heavy_tick = _SNAPSHOT_TICK_COUNTER["count"] % 5 == 1

    # Account snapshot
    if alpaca_account:
        positions = positions or []
        snapshot_account(db_path, {
            "ts": ts, "session_date": session_date,
            "equity": alpaca_account.get("equity"),
            "buying_power": alpaca_account.get("buying_power"),
            "daytrade_count": alpaca_account.get("daytrade_count"),
            "n_positions": len(positions),
            "n_open_orders": alpaca_account.get("n_open_orders", 0),
            "unrealized_pl": sum(p.get("unrealized_pl", 0) for p in positions),
            "market_value": sum(abs(p.get("market_value", 0)) for p in positions),
        })

    n_tickers_written = 0
    n_uw_written = 0
    for ticker in trader.status.tickers:
        scalp = trader._prior_state.get(ticker)
        gex_cached = trader._gex_cache.get(ticker)
        gex = gex_cached[0] if gex_cached else None
        iv_cached = trader._iv_cache.get(ticker)
        iv = iv_cached[0] if iv_cached else None
        earn_cached = trader._earnings_cache.get(ticker)
        earnings_blackout = earn_cached[0] if earn_cached else False
        flow_cached = trader._flow_cache.get(ticker)
        flow_records = flow_cached[0] if flow_cached else []
        active_setups = [s for s in trader.status.setups.values()
                          if s.get("ticker") == ticker]

        if scalp is None and gex is None and iv is None and not active_setups:
            continue

        # Compact ticker snapshot (lightweight columns)
        snapshot_ticker(db_path, {
            "ts": ts, "session_date": session_date, "ticker": ticker,
            "scalp_state": getattr(scalp, "name", None) if scalp else None,
            "scalp_score": float(getattr(scalp, "score", 0)) if scalp else None,
            "gex_spot": getattr(gex, "spot_price", None) if gex else None,
            "gex_gamma_flip": getattr(gex, "gamma_flip", None) if gex else None,
            "gex_pos_strike": getattr(gex, "major_pos_gex_strike", None) if gex else None,
            "gex_neg_strike": getattr(gex, "major_neg_gex_strike", None) if gex else None,
            "gex_age_min": getattr(gex, "age_min", None) if gex else None,
            "iv_percentile": iv,
            "earnings_blackout": earnings_blackout,
            "n_active_setups": len(active_setups),
            "n_flow_records_5min": len(flow_records),
            "setups": active_setups,
        })
        n_tickers_written += 1

        # Raw UW dump — full GEX strike map + flow records as JSON
        # Skip if neither GEX nor flow has data
        if gex is None and not flow_records:
            continue

        gex_full = None
        if gex is not None:
            try:
                gex_full = asdict(gex) if hasattr(gex, "__dataclass_fields__") else dict(gex.__dict__)
            except Exception:
                gex_full = {
                    "ticker": getattr(gex, "ticker", ticker),
                    "timestamp": str(getattr(gex, "timestamp", "")),
                    "spot_price": getattr(gex, "spot_price", None),
                    "gamma_flip": getattr(gex, "gamma_flip", None),
                    "major_pos_gex_strike": getattr(gex, "major_pos_gex_strike", None),
                    "major_neg_gex_strike": getattr(gex, "major_neg_gex_strike", None),
                    "gex_by_strike": getattr(gex, "gex_by_strike", {}),
                    "age_min": getattr(gex, "age_min", None),
                }

        flow_dumps = []
        for r in flow_records:
            try:
                flow_dumps.append(asdict(r) if hasattr(r, "__dataclass_fields__") else dict(r.__dict__))
            except Exception:
                pass

        # Heavy UW endpoints — pull only every 5th tick to respect rate limit
        iv_term = None
        earnings_summary = None
        iv_rank_latest = None
        if is_heavy_tick and uw_client is not None:
            try:
                iv_hist = uw_client.iv_rank_history(ticker)
                if iv_hist:
                    iv_rank_latest = iv_hist[-1] if isinstance(iv_hist, list) else iv_hist
            except Exception:
                pass
            try:
                iv_term = uw_client.iv_term_structure(ticker)
            except Exception:
                pass
            try:
                earnings_summary = uw_client.earnings_flow_summary(ticker)
            except Exception:
                pass

        snapshot_uw(db_path, {
            "ts": ts, "session_date": session_date, "ticker": ticker,
            "gex_full": gex_full,
            "flow_records": flow_dumps,
            "n_flow_records": len(flow_dumps),
            "iv_rank_latest": iv_rank_latest,
            "iv_term": iv_term,
            "earnings_summary": earnings_summary,
        })
        n_uw_written += 1

    return {
        "ts": ts, "session_date": session_date,
        "n_tickers": n_tickers_written,
        "n_uw_dumps": n_uw_written,
        "heavy_tick": is_heavy_tick,
        "account_written": bool(alpaca_account),
    }
