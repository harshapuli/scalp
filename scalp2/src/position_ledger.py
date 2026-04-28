"""
position_ledger.py — component (5) of scalp 2.

JSONL writer for APPROVED+SIZED fires. Per DESIGN.md §3e + §6d.

One row per fire that passed components (1)+(2)+(3)+(4). The dashboard reads
this file for live phase status and rolling alignment metrics.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LEDGER_DIR = PROJECT_ROOT / "data" / "kelly_ledger"


@dataclass
class LedgerRow:
    ts_utc: str
    kind: str
    ticker: str
    phase_state: str            # OBSERVE | PAPER | LIVE_SMALL | LIVE_FULL
    posterior: float
    ev_expected: float
    ev_p25: float
    kelly_full: float
    kelly_used: float
    approved: bool
    skip_reason: Optional[str] = None
    scalp1_features: dict = field(default_factory=dict)
    live_features: dict = field(default_factory=dict)


def _date_dir(ts_utc: str) -> Path:
    """Bucket by UTC date — one ledger file per session."""
    d = ts_utc[:10]   # YYYY-MM-DD
    p = LEDGER_DIR / d
    p.mkdir(parents=True, exist_ok=True)
    return p


def append(row: LedgerRow) -> Path:
    """Append one ledger row to today's positions.jsonl. Returns the path."""
    p = _date_dir(row.ts_utc) / "positions.jsonl"
    with p.open("a") as f:
        f.write(json.dumps(asdict(row)) + "\n")
    return p


def load_day(date: str) -> list[LedgerRow]:
    """Load all rows for a UTC date (YYYY-MM-DD)."""
    p = LEDGER_DIR / date / "positions.jsonl"
    if not p.exists():
        return []
    rows = []
    with p.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            d = json.loads(line)
            rows.append(LedgerRow(**d))
    return rows


def load_recent(n_days: int = 30) -> list[LedgerRow]:
    """Load N most recent UTC dates' rows. For rolling-window metrics."""
    if not LEDGER_DIR.exists():
        return []
    dates = sorted([p.name for p in LEDGER_DIR.iterdir() if p.is_dir()])[-n_days:]
    out = []
    for d in dates:
        out.extend(load_day(d))
    return out


def _set_ledger_dir(p: Path) -> None:
    """Test hook — override LEDGER_DIR. Production code never calls this."""
    global LEDGER_DIR
    LEDGER_DIR = p


if __name__ == "__main__":
    # Smoke test — write a synthetic row to a temp dir then read back
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        original = LEDGER_DIR
        _set_ledger_dir(Path(td))
        try:
            row = LedgerRow(
                ts_utc=datetime.now(timezone.utc).isoformat(timespec="seconds") + "Z",
                kind="CALL_CONVICTION",
                ticker="NVDA",
                phase_state="LIVE_SMALL",
                posterior=0.67,
                ev_expected=0.082,
                ev_p25=-0.005,
                kelly_full=0.18,
                kelly_used=0.018,
                approved=True,
                scalp1_features={"prior_5bar_alignment": 0.42, "atr_14_pct": 0.35},
                live_features={"uw_flow_score": 0.4, "news_llm_score": 0.1},
            )
            p = append(row)
            print(f"[ledger] wrote {p}")
            rows = load_day(row.ts_utc[:10])
            print(f"[ledger] read back {len(rows)} row(s)")
            assert len(rows) == 1
            assert rows[0].ticker == "NVDA"
            print("[ledger] M1 scaffold OK — append + load roundtrip works")
        finally:
            _set_ledger_dir(original)
