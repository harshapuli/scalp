"""infra/secrets.py — Env var loader. Spec FND-5.

Acceptance criteria:
  - Keys in /etc/trading/secrets.env (chmod 600)
  - Never logged, never echoed
  - Pre-commit hook prevents commits containing key patterns (FND-5.T1)
  - Documented rotation procedure for each provider

This implementation supports BOTH the spec's canonical names AND the legacy
names already present in the user's existing parallel projects (live_engine_v2,
swing_trade_strategy, etc. all use ALPACA_API_KEY / UW_API_KEY rather than
the spec's ALPACA_KEY_ID / UW_API_TOKEN). Reading from any one of them
populates BOTH legacy and canonical env-var names so downstream code can use
either.

Lookup order for the secrets file:
  1. Path passed explicitly to load_secrets()
  2. SCALP2_SECRETS_PATH env var
  3. /etc/trading/secrets.env (spec default; chmod 600 expected)
  4. ~/Downloads/spy QQQ/gemini /global_data_hub/.env (existing parallel project)
  5. ~/Downloads/spy QQQ/gemini /live_engine_v2/.env (fallback)
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Optional


# Spec canonical names (per docs/spec/trading_system_lifecycle.docx §10.7)
CANONICAL_VARS = (
    "POLYGON_API_KEY", "UW_API_TOKEN", "ALPACA_KEY_ID", "ALPACA_SECRET",
    "ALPACA_ENDPOINT", "POSTGRES_URL", "REDIS_URL",
)

# Legacy names used by existing sibling projects
LEGACY_TO_CANONICAL = {
    "ALPACA_API_KEY": "ALPACA_KEY_ID",
    "ALPACA_API_SECRET": "ALPACA_SECRET",
    "ALPACA_BASE_URL": "ALPACA_ENDPOINT",
    "UW_API_KEY": "UW_API_TOKEN",
}
CANONICAL_TO_LEGACY = {v: k for k, v in LEGACY_TO_CANONICAL.items()}

# What the spec really requires for MVP (Polygon dropped per user — Alpaca data sufficient)
MVP_REQUIRED_CANONICAL = (
    "UW_API_TOKEN", "ALPACA_KEY_ID", "ALPACA_SECRET", "ALPACA_ENDPOINT",
)

# Search paths for the secrets file
DEFAULT_SECRETS_PATHS = (
    Path("/etc/trading/secrets.env"),
    Path("/Users/harshapuli/Downloads/spy QQQ/gemini /global_data_hub/.env"),
    Path("/Users/harshapuli/Downloads/spy QQQ/gemini /live_engine_v2/.env"),
    Path("/Users/harshapuli/Downloads/spy QQQ/gemini /swing_trade_strategy/.env"),
)


def _parse_env_file(path: Path) -> dict[str, str]:
    """Parse a `KEY=value` env file. Skips comments + blank lines.

    Strips matching leading/trailing single or double quotes from values.
    Does NOT do shell expansion.
    """
    out: dict[str, str] = {}
    if not path.exists():
        return out
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        k, v = line.split("=", 1)
        k = k.strip()
        v = v.strip()
        # Strip matching quotes
        if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
            v = v[1:-1]
        out[k] = v
    return out


def find_secrets_path(extra_candidates: Iterable[Path] = ()) -> Optional[Path]:
    """Return the first existing secrets path. None if no candidate found."""
    explicit = os.environ.get("SCALP2_SECRETS_PATH")
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.extend(extra_candidates)
    candidates.extend(DEFAULT_SECRETS_PATHS)
    for p in candidates:
        if p.exists():
            return p
    return None


def load_secrets(path: Optional[Path] = None,
                 strict: bool = False) -> dict[str, str]:
    """Read secrets.env into os.environ. Returns the raw dict for diagnostics
    (keys only; values not logged).

    If `strict=True`, raises if no secrets file is found OR if any
    MVP-required canonical var is missing after load.

    Populates BOTH legacy and canonical env names so downstream code is
    naming-agnostic.
    """
    if path is None:
        path = find_secrets_path()
    if path is None:
        if strict:
            raise FileNotFoundError(
                "No secrets file found. Set SCALP2_SECRETS_PATH or place "
                "secrets at /etc/trading/secrets.env (chmod 600)."
            )
        return {}

    raw = _parse_env_file(path)

    # Mirror legacy → canonical and canonical → legacy
    for k, v in list(raw.items()):
        # Set the file value
        os.environ.setdefault(k, v)
        # If this is a legacy name, also set the canonical
        canonical = LEGACY_TO_CANONICAL.get(k)
        if canonical:
            os.environ.setdefault(canonical, v)
        # If this is a canonical name, also set the legacy alias
        legacy = CANONICAL_TO_LEGACY.get(k)
        if legacy:
            os.environ.setdefault(legacy, v)

    if strict:
        assert_required(MVP_REQUIRED_CANONICAL)

    return raw


def assert_required(vars_: tuple[str, ...] = MVP_REQUIRED_CANONICAL) -> None:
    """Raise if any var is missing from os.environ."""
    missing = [v for v in vars_ if not os.environ.get(v)]
    if missing:
        raise RuntimeError(
            f"Missing required env vars: {missing}. "
            f"Per FND-5: ensure secrets.env contains them and is loaded via "
            f"infra.secrets.load_secrets()."
        )


def status_report() -> dict:
    """Diagnostic — name + length only, never values. For verify_foundation.py."""
    return {
        "secrets_path": str(find_secrets_path() or "<none-found>"),
        "set_vars": {
            v: {"set": bool(os.environ.get(v)), "length": len(os.environ.get(v) or "")}
            for v in CANONICAL_VARS
        },
    }


if __name__ == "__main__":
    p = find_secrets_path()
    print(f"[secrets] secrets_path = {p}")
    if p is None:
        print("[secrets] no secrets file found; set SCALP2_SECRETS_PATH or use defaults")
    else:
        loaded = load_secrets()
        print(f"[secrets] loaded {len(loaded)} keys from {p.name}")
        rpt = status_report()
        for k, info in rpt["set_vars"].items():
            mark = "✓" if info["set"] else "✗"
            print(f"  {mark} {k} (length={info['length']})")
