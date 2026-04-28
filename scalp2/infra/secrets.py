"""infra/secrets.py — Env var loader. Spec FND-5.

Acceptance:
  - Keys in /etc/trading/secrets.env (chmod 600)
  - Never logged, never echoed
  - Pre-commit hook prevents commits containing key patterns (FND-5.T1)

TODO Sprint 1.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_SECRETS_PATH = Path("/etc/trading/secrets.env")

REQUIRED_VARS = (
    "POLYGON_API_KEY", "UW_API_TOKEN", "ALPACA_KEY_ID", "ALPACA_SECRET",
    "ALPACA_ENDPOINT", "POSTGRES_URL", "REDIS_URL",
)


def load_secrets(path: Path = DEFAULT_SECRETS_PATH) -> None:
    """Read secrets.env into os.environ. Raises on missing required vars."""
    raise NotImplementedError("FND-5 — TODO Sprint 1")


def assert_required(vars: tuple[str, ...] = REQUIRED_VARS) -> None:
    missing = [v for v in vars if not os.environ.get(v)]
    if missing:
        raise RuntimeError(f"Missing required env vars: {missing}")
