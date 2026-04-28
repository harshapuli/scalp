"""
ml_readiness.py — ML training-readiness ledger.

Per DESIGN.md §14 Q13 default: a kind is "ML-ready" when it has:
  • ≥ MIN_DAYS unique trading days  AND
  • ≥ MIN_FIRES deduped fires

No model training in v1 — just the readiness ledger. The dashboard uses this
to flag which kinds are ready to graduate from rules-only → ML-trainable.

Public API:
    readiness_ledger(outcomes, min_days=30, min_fires=100) -> list[ReadinessRow]
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

MIN_DAYS_DEFAULT = 30
MIN_FIRES_DEFAULT = 100


@dataclass
class ReadinessRow:
    kind: str
    n_fires: int                    # deduped fire count
    n_days: int                     # unique trading days seen
    n_wins_underlying: int = 0
    n_wins_option: int = 0
    days_to_min: int = 0            # days needed to reach MIN_DAYS
    fires_to_min: int = 0           # fires needed to reach MIN_FIRES
    ready: bool = False
    blocker: Optional[str] = None   # "days" | "fires" | "both" | None


def readiness_ledger(outcomes: list,
                     min_days: int = MIN_DAYS_DEFAULT,
                     min_fires: int = MIN_FIRES_DEFAULT) -> list[ReadinessRow]:
    by_kind: dict[str, list] = {}
    for o in outcomes:
        by_kind.setdefault(o.kind, []).append(o)

    out: list[ReadinessRow] = []
    for kind, rows in by_kind.items():
        n = len(rows)
        days = {getattr(r, "date_str", None) for r in rows}
        days.discard(None)
        n_days = len(days)
        wins_u = sum(1 for r in rows if getattr(r, "label_underlying", None) == 1)
        wins_o = sum(1 for r in rows if getattr(r, "label_option_5p5x", None) == 1)

        days_short = max(0, min_days - n_days)
        fires_short = max(0, min_fires - n)

        days_ok = n_days >= min_days
        fires_ok = n >= min_fires

        if days_ok and fires_ok:
            blocker = None
        elif not days_ok and not fires_ok:
            blocker = "both"
        elif not days_ok:
            blocker = "days"
        else:
            blocker = "fires"

        out.append(ReadinessRow(
            kind=kind,
            n_fires=n,
            n_days=n_days,
            n_wins_underlying=wins_u,
            n_wins_option=wins_o,
            days_to_min=days_short,
            fires_to_min=fires_short,
            ready=(blocker is None),
            blocker=blocker,
        ))

    out.sort(key=lambda r: (-r.n_fires, r.kind))
    return out


# ──────────────────────────────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────────────────────────────


if __name__ == "__main__":
    from loader_scalp import load_all
    from outcome_intraday import compute_all, dedup_signals

    sigs, bars = load_all()
    sigs_d = dedup_signals(sigs)
    outs = compute_all(sigs_d, bars)
    print(f"deduped outcomes: {len(outs)}")

    ledger = readiness_ledger(outs)
    print(f"\n{'KIND':22s} {'n_fires':>8s} {'days':>5s} {'win_u':>6s} {'win_o':>6s} {'days→min':>10s} {'fires→min':>10s} {'status':>10s}")
    print("─" * 90)
    for r in ledger:
        status = "READY" if r.ready else f"need {r.blocker}"
        print(f"{r.kind:22s} {r.n_fires:>8d} {r.n_days:>5d} {r.n_wins_underlying:>6d} {r.n_wins_option:>6d} "
              f"{r.days_to_min:>10d} {r.fires_to_min:>10d} {status:>10s}")
