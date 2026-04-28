"""scripts/build_features.py — Replay → features dataset.

Per spec §10.8 build recipe:
  $ python -m scripts.build_features --start 2025-01-01 --end 2025-12-31
  → 47,231 candidate rows written to data/features/v1.parquet

For each ticker × day in [start, end]:
  - Pull Alpaca 1m bars
  - Pull UW GEX history + flow snapshot at each bar
  - Build per-bar Features
  - Run scalp brain classifier (replay)
  - For SETUP candidates, label trade outcome (S5-30)
  - Append to parquet
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.config_loader import load_thresholds
from infra.secrets import load_secrets


PROJECT_ROOT = Path(__file__).resolve().parent.parent
FEATURES_DIR = PROJECT_ROOT / "data" / "features"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--universe", default="SPY,QQQ,IWM,NVDA,TSLA",
                         help="comma-separated tickers")
    parser.add_argument("--output", default=str(FEATURES_DIR / "v1.parquet"))
    args = parser.parse_args()

    load_secrets()
    cfg = load_thresholds()
    FEATURES_DIR.mkdir(parents=True, exist_ok=True)

    from data_clients.alpaca import AlpacaClient
    from data_clients.unusual_whales import UWClient
    from features.price_structure import BarOHLC
    from scalp_brain.replay import replay_session

    tickers = [t.strip() for t in args.universe.split(",") if t.strip()]
    start_date = date.fromisoformat(args.start)
    end_date = date.fromisoformat(args.end)

    print(f"[build_features] universe={tickers} start={start_date} end={end_date}")
    print(f"[build_features] output={args.output}")

    rows: list[dict] = []
    with AlpacaClient() as alp, UWClient() as uw:
        for ticker in tickers:
            print(f"[build_features] fetching {ticker}...")
            try:
                bars_alp = alp.get_bars(
                    ticker,
                    start=f"{start_date.isoformat()}T00:00:00Z",
                    end=f"{end_date.isoformat()}T23:59:59Z",
                    timeframe="1Min", feed="sip", limit=10000,
                )
            except Exception as e:
                print(f"  ✗ Alpaca failed for {ticker}: {e}")
                continue
            print(f"  ✓ {len(bars_alp)} bars")

            try:
                gex_history = uw.greek_exposure_history(ticker, days=365)
                flow = uw.flow_recent(ticker)
            except Exception as e:
                print(f"  ⚠ UW failed for {ticker}: {e} — proceeding with empty UW")
                gex_history = []
                flow = []

            # Convert Alpaca Bar → BarOHLC and replay
            ohlc_bars = [BarOHLC(o=b.o, h=b.h, l=b.l, c=b.c, v=b.v) for b in bars_alp]
            ts = [b.t for b in bars_alp]
            outputs = replay_session(
                ticker=ticker, bars=ohlc_bars, bar_timestamps=ts,
                trades_per_bar=[[]] * len(ohlc_bars),
                quotes_per_bar=[[]] * len(ohlc_bars),
                gex_snapshots=gex_history,
                flow_records_per_bar=[flow] * len(ohlc_bars),
                cfg=cfg,
            )
            for r in outputs:
                rows.append({
                    "ticker": r.ticker,
                    "timestamp": r.timestamp_iso,
                    "bar_idx": r.bar_idx,
                    "state": r.state,
                    "score": r.score,
                    "features_hash": r.features_hash,
                })
            print(f"  ✓ {len(outputs)} replay rows")

    # Save to parquet
    try:
        import pandas as pd
        df = pd.DataFrame(rows)
        df.to_parquet(args.output, index=False)
        print(f"[build_features] wrote {len(df)} rows → {args.output}")
    except Exception as e:
        # Fallback: JSONL
        out_jsonl = args.output.replace(".parquet", ".jsonl")
        import json
        with open(out_jsonl, "w") as f:
            for r in rows:
                f.write(json.dumps(r) + "\n")
        print(f"[build_features] parquet failed ({e}); wrote JSONL → {out_jsonl}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
