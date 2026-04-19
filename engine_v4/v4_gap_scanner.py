"""
V4 GAP SCANNER — standalone gap-up / gap-down strategy.

Gap plays are a distinct strategy (overnight catalyst driven — earnings,
news, macro surprise). They deserve their own signal pool and dashboard
tab rather than being a scoring ingredient inside flow-based signals.

Output: v4_gap_watchlist.json with gap CALLs (gap-up + hold) and
gap PUTs (gap-down + hold).

Run: python3 engine_v4/v4_gap_scanner.py
Cadence: once per session (intraday hold is checked at run time).
"""
import os
import sys
import json
import tempfile
from datetime import datetime, timedelta, timezone

sys.path.append(os.path.join(os.path.dirname(__file__), '..'))

# Reuse the universe from preposition scanner + the gap helper from meta_engine.
from v4_preposition_scanner import TIER1_WATCHLIST, fetch_tier2_universe
from v4_meta_engine import check_gap_hold

UTC = timezone.utc
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_PATH = os.path.join(BASE_DIR, 'v4_gap_watchlist.json')

# Minimum gap to be worth emitting as a signal (matches check_gap_hold threshold)
MIN_SIGNED_GAP = 1.5


def atomic_write_json(path: str, payload: dict):
    """Atomic write via temp + rename to prevent partial-read corruption."""
    dir_path = os.path.dirname(path) or '.'
    fd, tmp = tempfile.mkstemp(dir=dir_path, prefix='.tmp_', suffix='.json')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(payload, f, indent=2, default=str)
        os.replace(tmp, path)
    except Exception:
        try: os.unlink(tmp)
        except Exception: pass
        raise


def scan():
    print("\n" + "=" * 70)
    print("V4 GAP SCANNER — overnight gap + hold detection (stand-alone strategy)")
    print("=" * 70)

    # Build universe: Tier 1 mega caps + Tier 2 dynamic
    try:
        tier2 = fetch_tier2_universe()
    except Exception as e:
        print(f"tier 2 fetch failed: {e}")
        tier2 = []
    universe = [(t, 'mega_cap') for t in TIER1_WATCHLIST]
    universe += [(r['ticker'], 'dynamic') for r in tier2]
    print(f"Scanning {len(universe)} tickers ({len(TIER1_WATCHLIST)} mega, {len(tier2)} dynamic)\n")

    gap_calls = []
    gap_puts = []
    for t, source in universe:
        # Check both directions — overnight could gap up (CALL) or down (PUT)
        for direction in ('CALL', 'PUT'):
            info = check_gap_hold(t, direction)
            gap_pct = info.get('gap_pct', 0)
            holding = info.get('holding', False)
            bonus = info.get('bonus_pts', 0)

            # Filter: direction-aligned gap meeting threshold and currently holding
            signed_gap = gap_pct if direction == 'CALL' else -gap_pct
            if signed_gap < MIN_SIGNED_GAP or not holding or bonus <= 0:
                continue

            record = {
                'ticker': t,
                'direction': direction,
                'source': source,
                'gap_pct': gap_pct,
                'holding': holding,
                'vs_gap_pct': info.get('vs_gap_pct', 0),  # how far spot is from today's open
                'vol_ratio': info.get('vol_ratio', 1.0),
                'score': bonus,
                'detected_at_utc': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
                'suggested_dte_min': 7,
                'suggested_dte_max': 21,
                'narrative': (
                    f"{direction} gap {gap_pct:+.2f}%, "
                    f"{'holding' if holding else 'faded'} "
                    f"(spot vs open: {info.get('vs_gap_pct', 0):+.2f}%), "
                    f"vol {info.get('vol_ratio', 1.0):.1f}× avg → score {bonus}"
                ),
            }
            if direction == 'CALL':
                gap_calls.append(record)
                print(f"  📈 {t:<6} CALL | gap +{gap_pct:.2f}% | hold {info.get('vs_gap_pct', 0):+.2f}% | "
                      f"vol {info.get('vol_ratio', 1.0):.1f}× | score {bonus} [{source}]")
            else:
                gap_puts.append(record)
                print(f"  📉 {t:<6} PUT  | gap {gap_pct:.2f}% | hold {info.get('vs_gap_pct', 0):+.2f}% | "
                      f"vol {info.get('vol_ratio', 1.0):.1f}× | score {bonus} [{source}]")

    # Rank by score (larger gap + better hold = higher)
    gap_calls.sort(key=lambda x: -x['score'])
    gap_puts.sort(key=lambda x: -x['score'])

    payload = {
        'metadata': {
            'scan_completed_utc': datetime.now(UTC).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'universe_size': len(universe),
            'tier1_count': len(TIER1_WATCHLIST),
            'tier2_count': len(tier2),
            'gap_calls_found': len(gap_calls),
            'gap_puts_found': len(gap_puts),
            '_schema_doc': 'Standalone gap strategy. Overnight gap-up (CALL) or gap-down (PUT) + holding into current session. Score 5-15 based on gap size, hold, volume confirmation.',
        },
        'gap_calls': gap_calls,
        'gap_puts': gap_puts,
    }
    atomic_write_json(OUT_PATH, payload)
    print(f"\n{'='*70}")
    print(f"DONE | gap_calls: {len(gap_calls)} | gap_puts: {len(gap_puts)} | saved to {OUT_PATH}")
    print(f"{'='*70}")
    return payload


if __name__ == "__main__":
    scan()
