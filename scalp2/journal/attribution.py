"""journal/attribution.py — Attribution dashboard backend. Spec S5-82.

Per-strategy hit rate, R, Sharpe over rolling 30/90/365 day windows.
Built on decision_log + closed trades (Postgres §10.3 / SQLite for dev).

Per-edge breakdown. Per-pass-reason histogram. Drift alerts (>10pp degradation).
"""
from __future__ import annotations

import sqlite3
import statistics
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from journal.decision_log import _conn, DEFAULT_SQLITE_PATH


@dataclass
class WindowMetrics:
    window_days: int
    n_decisions: int
    n_trades: int
    n_passes: int
    pass_reasons: dict[str, int]
    hit_rate: Optional[float]
    avg_r: Optional[float]
    sharpe: Optional[float]


def rolling_metrics(strategy: str, window_days: int,
                     db_path: Optional[Path] = None) -> WindowMetrics:
    """S5-82. Compute attribution metrics over rolling window.

    Note: hit_rate / avg_r / sharpe require a closed-trades table; for MVP we
    return decision-log-only metrics (counts + pass_reasons). Closed-trades
    are populated when journal/trades.py is wired (Sprint 14).
    """
    cutoff = (datetime.now(tz=timezone.utc) - timedelta(days=window_days)).isoformat()
    with _conn(db_path) as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT decision, pass_reason FROM decision_log "
            "WHERE strategy = ? AND timestamp >= ?",
            (strategy, cutoff),
        )
        rows = cur.fetchall()

    n_decisions = len(rows)
    n_trades = sum(1 for r in rows if r[0] == "TRADE")
    n_passes = sum(1 for r in rows if r[0] == "PASS")
    pass_reasons: dict[str, int] = {}
    for r in rows:
        if r[0] == "PASS" and r[1]:
            pass_reasons[r[1]] = pass_reasons.get(r[1], 0) + 1

    return WindowMetrics(
        window_days=window_days,
        n_decisions=n_decisions,
        n_trades=n_trades,
        n_passes=n_passes,
        pass_reasons=pass_reasons,
        hit_rate=None,    # filled when trades table is wired
        avg_r=None,
        sharpe=None,
    )


def per_pass_reason_histogram(strategy: str, window_days: int = 7,
                                db_path: Optional[Path] = None) -> dict[str, int]:
    """S5-82 — pass_reason → count over window."""
    return rolling_metrics(strategy, window_days, db_path).pass_reasons


def detect_drift(strategy: str,
                  baseline_hit_rate: float,
                  window_days: int = 7,
                  alert_pp: float = 10.0,
                  db_path: Optional[Path] = None) -> Optional[str]:
    """S5-82 — fire if observed hit_rate dropped > alert_pp from baseline.

    Returns alert message if drift detected, None if within tolerance.
    """
    m = rolling_metrics(strategy, window_days, db_path)
    if m.hit_rate is None:
        return None
    drop_pp = (baseline_hit_rate - m.hit_rate) * 100.0
    if drop_pp > alert_pp:
        return (f"{strategy} hit_rate dropped {drop_pp:.1f}pp "
                f"(observed {m.hit_rate:.2%} vs baseline {baseline_hit_rate:.2%})")
    return None


if __name__ == "__main__":
    m = rolling_metrics("S5", window_days=30)
    print(f"[attribution] S5 30d: n_decisions={m.n_decisions} "
          f"n_trades={m.n_trades} n_passes={m.n_passes}")
    if m.pass_reasons:
        print(f"[attribution]   top pass reasons: {sorted(m.pass_reasons.items(), key=lambda x: -x[1])[:5]}")
