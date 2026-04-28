"""scripts/verify_foundation.py — Sprint 1 Foundation Layer acceptance.

Per spec §10.8 build recipe + FND-3.1 BLOCKER.

Note: Polygon is OPTIONAL (deferred per user direction; Alpaca Pro covers data).
The verification checks Alpaca + UW only. If Polygon keys appear later, add the
Polygon WS/REST checks here.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def check(name: str, fn) -> bool:
    try:
        fn()
        print(f"  ✓ {name}")
        return True
    except NotImplementedError as e:
        print(f"  ✗ {name}  (not yet implemented: {e})")
        return False
    except Exception as e:
        print(f"  ✗ {name}  ({type(e).__name__}: {str(e)[:120]})")
        return False


def main() -> int:
    from infra.secrets import load_secrets, status_report
    print("=" * 60)
    print("Sprint 1 Foundation Layer Verification")
    print("=" * 60)
    load_secrets()
    rpt = status_report()
    print()
    print(f"Secrets loaded from: {rpt['secrets_path']}")
    for k, info in rpt["set_vars"].items():
        mark = "✓" if info["set"] else "✗"
        print(f"  {mark} {k} (length={info['length']})")
    print()

    if not (os.environ.get("UW_API_TOKEN") and os.environ.get("ALPACA_KEY_ID")):
        print("Critical secrets missing. Cannot proceed with API checks.")
        return 1

    from data_clients.unusual_whales import UWClient
    from data_clients.alpaca import AlpacaClient

    results = []

    # UW core
    print("UW (FND-3):")
    with UWClient() as uw:
        results.append(check("UW client returns intraday GEX for SPY",
                              lambda: uw.greek_exposure("SPY")))
        results.append(check("UW flow_recent for SPY",
                              lambda: uw.flow_recent("SPY")))

        # FND-3.1 BLOCKER — the load-bearing test
        print()
        print("Critical blocker (FND-3.1):")
        try:
            cad = uw.verify_gex_cadence("SPY")
            cm = cad.get("verified_cadence_min")
            if cm is not None and cm <= 30:
                print(f"  ✓ FND-3.1 RESOLVED — UW spot-exposures cadence ≈ {cm}min")
                print(f"    snapshots/session={cad.get('snapshots_per_session')}, "
                      f"recommended max_age={cad.get('recommended_max_age_min')}min")
                blocker = True
            else:
                print(f"  ✗ FND-3.1 NOT RESOLVED — cadence={cm}min")
                print(f"    {cad.get('evidence')}")
                blocker = False
        except Exception as e:
            print(f"  ✗ FND-3.1 FAILED ({type(e).__name__}: {e})")
            blocker = False

    print()
    print("Alpaca (FND-4 + data substitute for FND-1/2):")
    with AlpacaClient() as a:
        results.append(check("Alpaca account access",
                              lambda: a.get_account()))
        results.append(check("Alpaca positions list",
                              lambda: a.get_positions()))
        results.append(check("Alpaca SIP bars (1mo SPY 1m)",
                              lambda: a.get_bars(
                                  "SPY", "2026-04-01T00:00:00Z",
                                  "2026-04-28T00:00:00Z",
                                  timeframe="1Min", limit=100,
                              )))

    print()
    print("=" * 60)
    if blocker and all(results):
        print("✓ Foundation Layer ready — Sprint 2 may begin")
        return 0
    print("✗ Foundation checks failed")
    if not blocker:
        print("  FND-3.1 BLOCKER unresolved — Sprint 1 cannot complete")
    return 1


if __name__ == "__main__":
    sys.exit(main())
