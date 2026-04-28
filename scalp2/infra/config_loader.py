"""infra/config_loader.py — YAML config loader for thresholds.yaml + universe.yaml.

Hot-reload OK; never edit in code.

TODO Sprint 1 — implement with watchdog for hot-reload.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional

# yaml lib intentionally not imported here — ensure pyproject.toml lists pyyaml

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"
DEFAULT_THRESHOLDS = CONFIG_DIR / "thresholds.yaml"


def load_thresholds(path: Optional[Path] = None) -> dict:
    """Load config/thresholds.yaml. Reads YAML lazily so this stub doesn't
    require pyyaml at import time.
    """
    p = path or DEFAULT_THRESHOLDS
    if not p.exists():
        raise FileNotFoundError(f"thresholds.yaml not found at {p}")
    try:
        import yaml
    except ImportError:
        raise RuntimeError(
            "pyyaml not installed. Sprint 1: add to pyproject.toml dependencies."
        )
    with p.open() as f:
        return yaml.safe_load(f)


if __name__ == "__main__":
    print(f"[config_loader] DEFAULT_THRESHOLDS = {DEFAULT_THRESHOLDS}")
    print(f"[config_loader]   exists: {DEFAULT_THRESHOLDS.exists()}")
