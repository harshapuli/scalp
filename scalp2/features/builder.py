"""features/builder.py — Features object factory + hash.

Computes the per-bar Features dataclass from raw bars + trades + UW data.
Hash stored in any decision logs (SCALP-1).

Deterministic across live/replay (SCALP-1.T1).

TODO Sprint 3.
"""
from __future__ import annotations

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.types import Features


def build_features(bar, prior_bars, trades, gex_snapshot, flow_records, cfg) -> Features:
    """Spec SCALP-1. Compose all sub-feature modules into a single Features object."""
    raise NotImplementedError("SCALP-1 — TODO Sprint 3")
