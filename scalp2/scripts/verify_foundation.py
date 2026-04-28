"""
scripts/verify_foundation.py — Sprint 1 acceptance script.

Per spec §10.8 build recipe:
  $ python -m scripts.verify_foundation
    ✓ Polygon WS connects
    ✓ Polygon REST returns 1mo SPY bars
    ✓ UW client returns GEX for SPY
    ✗ FND-3.1: UW GEX cadence verified ?  ← BLOCKER
    ✓ Alpaca paper account balance

Cannot proceed past Sprint 1 without FND-3.1 cleared.

Run after `infra/secrets.py` loads /etc/trading/secrets.env.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def check(name: str, fn) -> bool:
    """Run one check, print ✓/✗, return True iff passed."""
    try:
        fn()
        print(f"  ✓ {name}")
        return True
    except NotImplementedError as e:
        print(f"  ✗ {name}  (not yet implemented: {e})")
        return False
    except Exception as e:
        print(f"  ✗ {name}  ({type(e).__name__}: {e})")
        return False


def main() -> int:
    print("=" * 60)
    print("Sprint 1 Foundation Layer Verification")
    print("=" * 60)
    print()

    # Required env vars (FND-5)
    print("Secrets (FND-5):")
    secrets_ok = True
    for v in ("POLYGON_API_KEY", "UW_API_TOKEN", "ALPACA_KEY_ID",
              "ALPACA_SECRET", "ALPACA_ENDPOINT"):
        if os.environ.get(v):
            print(f"  ✓ {v} set")
        else:
            print(f"  ✗ {v} not set")
            secrets_ok = False
    print()

    if not secrets_ok:
        print("Secrets missing — populate /etc/trading/secrets.env (chmod 600)")
        print("Cannot proceed with API checks.")
        return 1

    # Foundation API checks
    print("API connectivity:")
    from data_clients.polygon_ws import PolygonWS
    from data_clients.polygon_rest import PolygonREST
    from data_clients.unusual_whales import UWClient
    from data_clients.alpaca import AlpacaClient

    results = []
    results.append(check("Polygon WS connects",
                         lambda: PolygonWS().connect()))
    results.append(check("Polygon REST returns 1mo SPY bars",
                         lambda: PolygonREST().historical_bars(
                             "SPY", "2025-12-01", "2026-01-01")))
    results.append(check("UW client returns GEX for SPY",
                         lambda: UWClient().greek_exposure("SPY")))

    # THE BLOCKER — Sprint 1 cannot complete without this
    print()
    print("Critical blocker (FND-3.1):")
    blocker = check("UW GEX cadence verified (FND-3.1 BLOCKER)",
                     lambda: UWClient().verify_gex_cadence())

    print()
    print("Alpaca:")
    results.append(check("Alpaca paper account balance",
                         lambda: AlpacaClient().get_account()))

    print()
    print("=" * 60)
    if blocker and all(results):
        print("✓ Foundation Layer ready — Sprint 2 may begin")
        return 0
    else:
        if not blocker:
            print("✗ FND-3.1 BLOCKER unresolved — Sprint 1 cannot complete")
            print("  Read https://docs.unusualwhales.com/api/greek-exposure")
            print("  + probe endpoint at 1-min cadence over a market session.")
            print("  If EOD-only → S5 redesign required.")
        else:
            print("✗ Some Foundation checks failed — fix before Sprint 2")
        return 1


if __name__ == "__main__":
    sys.exit(main())
