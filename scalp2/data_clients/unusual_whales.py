"""
data_clients/unusual_whales.py — UW API client. Spec FND-3.

Endpoints (per spec §2.1):
  /api/stock/{ticker}/flow-recent           — Live signed flow (Scalp brain)
  /api/stock/{ticker}/flow-historical       — Daily aggregates (Swing brain bias)
  /api/stock/{ticker}/greek-exposure        — GEX by strike, gamma flip (S5 setup gate)
  /api/stock/{ticker}/greek-exposure-history — GEX over time (Phase 0 sub-edge)
  /api/stock/{ticker}/dark-pool-prints      — Off-exchange trades (deferred)
  /api/stock/{ticker}/iv-term-structure     — ATM IV per expiry, slope
  /api/stock/{ticker}/earnings/flow-summary — Pre-earnings (S4 deferred)

Auth: Bearer token via UW_API_TOKEN env var.
Base: https://api.unusualwhales.com (configurable via UW_BASE_URL legacy).

Returns typed dataclasses (features.types.GEXSnapshot, FlowRecord), not raw dicts.
"""
from __future__ import annotations

import os
import time
from datetime import date, datetime, timezone
from typing import Optional

import httpx

import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from features.datatypes import GEXSnapshot, FlowRecord


UW_BASE_URL = os.environ.get("UW_BASE_URL") or "https://api.unusualwhales.com"
UW_TOKEN_ENV = "UW_API_TOKEN"
UW_LEGACY_TOKEN_ENV = "UW_API_KEY"   # what the existing parallel projects use


class UWAuthError(RuntimeError):
    """UW returned 401/403."""


class UWRateLimitError(RuntimeError):
    """UW returned 429."""


class UWClient:
    """Typed UW API client. Spec FND-3.

    Uses httpx for sync + async transport. Initialization fails (raises) if
    UW_API_TOKEN / UW_API_KEY is not set.
    """

    def __init__(self,
                 token: Optional[str] = None,
                 base_url: str = UW_BASE_URL,
                 timeout_s: float = 15.0):
        self.token = (
            token
            or os.environ.get(UW_TOKEN_ENV)
            or os.environ.get(UW_LEGACY_TOKEN_ENV)
        )
        if not self.token:
            raise RuntimeError(
                f"{UW_TOKEN_ENV} (or {UW_LEGACY_TOKEN_ENV}) not set. "
                f"Per FND-5 secrets policy, load via infra.secrets.load_secrets()."
            )
        # UW endpoints all start with /api/. Some env configs already include
        # /api in UW_BASE_URL — strip it so we don't double-prefix.
        cleaned = base_url.rstrip("/")
        if cleaned.endswith("/api"):
            cleaned = cleaned[:-4]
        self.base_url = cleaned
        self.timeout_s = timeout_s
        self._client = httpx.Client(
            base_url=self.base_url,
            headers={
                "Authorization": f"Bearer {self.token}",
                "Accept": "application/json",
                "User-Agent": "scalp2/0.1 (Edge-Centric Trading System v2.2)",
            },
            timeout=self.timeout_s,
        )

    # ─── Internal HTTP helpers ───────────────────────────────────────────

    def _get(self, path: str, params: Optional[dict] = None) -> dict:
        r = self._client.get(path, params=params or {})
        if r.status_code == 401 or r.status_code == 403:
            raise UWAuthError(f"UW auth failed: {r.status_code} {r.text[:120]}")
        if r.status_code == 429:
            raise UWRateLimitError(f"UW rate-limited: {r.text[:120]}")
        r.raise_for_status()
        return r.json()

    def close(self) -> None:
        self._client.close()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()

    # ─── FND-3.1 BLOCKER — verify GEX cadence ─────────────────────────────

    def verify_gex_cadence(self,
                            ticker: str = "SPY",
                            probe_count: int = 1) -> dict:
        """FND-3.1 BLOCKER. Determine UW spot-exposures update frequency.

        UW exposes intraday GEX via /api/stock/{ticker}/spot-exposures, which
        returns the FULL DAY of snapshots (not just the latest). We measure
        cadence directly by computing the median interval between consecutive
        snapshot timestamps in the response.

        This is a single-call verification (probe_count default 1). Optionally
        re-probe to verify cadence stability over time.

        Returns:
          {
            'verified_cadence_min': float,
            'snapshots_per_session': int,
            'evidence': str,
            'recommended_max_age_min': int,
            'session_span_seconds': int,
          }
        """
        evidence_lines: list[str] = []

        # Pull the full intraday spot-exposures array (all snapshots from session start)
        data = self._get(f"/api/stock/{ticker}/spot-exposures")
        rows = data.get("data") if isinstance(data, dict) else data
        if not rows or not isinstance(rows, list):
            return {
                "verified_cadence_min": None,
                "snapshots_per_session": 0,
                "evidence": "spot-exposures returned empty data",
                "recommended_max_age_min": 30,
                "session_span_seconds": 0,
            }

        # Extract all snapshot timestamps
        ts_iso_list = [r.get("time") or r.get("start_time") for r in rows]
        ts_dt_list = []
        for ts_iso in ts_iso_list:
            if not ts_iso:
                continue
            try:
                ts_dt_list.append(datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00")))
            except Exception:
                continue
        ts_dt_list.sort()

        if len(ts_dt_list) < 2:
            return {
                "verified_cadence_min": None,
                "snapshots_per_session": len(ts_dt_list),
                "evidence": "fewer than 2 snapshots; cadence undefined",
                "recommended_max_age_min": 30,
                "session_span_seconds": 0,
            }

        # Inter-snapshot intervals
        intervals = [
            (ts_dt_list[i + 1] - ts_dt_list[i]).total_seconds()
            for i in range(len(ts_dt_list) - 1)
        ]
        intervals_sorted = sorted(intervals)
        median_interval_s = intervals_sorted[len(intervals_sorted) // 2]
        min_interval_s = intervals_sorted[0]
        max_interval_s = intervals_sorted[-1]
        session_span_s = int((ts_dt_list[-1] - ts_dt_list[0]).total_seconds())

        cadence_min = round(median_interval_s / 60.0, 2)
        # Recommend max_age = 3x median cadence, floor 5min
        recommended_max_age_min = max(int(median_interval_s * 3 / 60), 5)

        evidence_lines.append(
            f"Pulled {len(ts_dt_list)} snapshots over {session_span_s}s "
            f"({session_span_s/3600:.1f}h)."
        )
        evidence_lines.append(
            f"Inter-snapshot intervals: min={min_interval_s:.0f}s, "
            f"median={median_interval_s:.0f}s, max={max_interval_s:.0f}s."
        )
        evidence_lines.append(
            f"Median cadence ≈ {cadence_min}min. "
            f"Recommend gex_max_age_min ≥ {recommended_max_age_min}."
        )

        # Verdict
        if median_interval_s <= 120:
            evidence_lines.append(
                "VERDICT: real-time intraday cadence (≤2min). "
                "S5 intraday strategy is fully supported."
            )
        elif median_interval_s <= 600:
            evidence_lines.append(
                "VERDICT: 5-10min cadence. Tighten gex_max_age_min to 10-30min."
            )
        else:
            evidence_lines.append(
                "VERDICT: WARNING — cadence > 10min. Review S5 timing assumptions."
            )

        return {
            "verified_cadence_min": cadence_min,
            "snapshots_per_session": len(ts_dt_list),
            "session_span_seconds": session_span_s,
            "min_interval_seconds": int(min_interval_s),
            "median_interval_seconds": int(median_interval_s),
            "max_interval_seconds": int(max_interval_s),
            "ticker": ticker,
            "evidence": "\n".join(evidence_lines),
            "recommended_max_age_min": recommended_max_age_min,
        }

    # ─── FND-3.2 / FND-3.3 / FND-3.4 — typed endpoint methods ─────────────

    def flow_recent(self, ticker: str) -> list[FlowRecord]:
        """FND-3.2. Live signed flow records.

        Returns: list of FlowRecord. UW endpoint shape may vary; we
        defensively coerce the JSON response into our typed dataclass.
        """
        data = self._get(f"/api/stock/{ticker}/flow-recent")
        # UW flow-recent returns a LIST at the top level (not a dict-wrapped one).
        if isinstance(data, list):
            records = data
        elif isinstance(data, dict):
            records = data.get("data") or data.get("records") or data.get("flow") or []
        else:
            records = []
        out: list[FlowRecord] = []
        for r in records:
            try:
                ts = r.get("executed_at") or r.get("timestamp") or r.get("time")
                if not ts:
                    continue
                ts_dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
                expiry_str = r.get("expiry") or r.get("expiration")
                expiry_d = (
                    date.fromisoformat(str(expiry_str)[:10])
                    if expiry_str else date.today()
                )
                # UW labels: side='call'/'put', action='buy'/'sell' (or sometimes 'B'/'S')
                action_raw = (r.get("action") or r.get("side_action") or "buy").lower()
                action = "buy" if action_raw.startswith(("b", "ask")) else "sell"
                out.append(FlowRecord(
                    ticker=r.get("ticker") or ticker,
                    timestamp=ts_dt,
                    side=str(r.get("side") or r.get("type") or "call").lower(),
                    action=action,
                    premium=float(r.get("premium") or r.get("total_premium") or 0.0),
                    is_sweep=bool(r.get("is_sweep") or r.get("sweep") or False),
                    expiry=expiry_d,
                    strike=float(r.get("strike") or r.get("strike_price") or 0.0),
                    iv_at_trade=float(r.get("iv") or r.get("implied_volatility") or 0.0),
                ))
            except Exception:
                continue
        return out

    def greek_exposure(self, ticker: str) -> GEXSnapshot:
        """FND-3.3. Current intraday GEX snapshot.

        Uses /api/stock/{ticker}/spot-exposures (intraday, ~1-min cadence)
        for real-time gamma + delta exposure aggregates, and merges
        /api/stock/{ticker}/spot-exposures/strike for strike-level breakdown
        of major +/- GEX strikes.

        FND-3.1 resolution: UW spot-exposures runs at ~1-min cadence during
        market hours (520 snapshots per 9.5h session). gex_max_age_min ≥ 5
        is safely above the cadence floor.

        NOTE: /api/stock/{ticker}/greek-exposure (without /strike) returns
        DAILY EOD aggregates — useful for history but NOT intraday gating.
        """
        # Pull latest intraday spot exposure snapshot (last row of array)
        data = self._get(f"/api/stock/{ticker}/spot-exposures")
        rows = data.get("data") if isinstance(data, dict) else data
        if not rows:
            # Fallback to empty snapshot if no data
            now = datetime.now(tz=timezone.utc)
            return GEXSnapshot(
                ticker=ticker, timestamp=now, spot_price=0.0,
                gamma_flip=0.0, major_pos_gex_strike=0.0,
                major_neg_gex_strike=0.0, gex_by_strike={}, age_min=999,
            )
        latest = rows[-1] if isinstance(rows, list) else rows

        ts_iso = latest.get("time") or latest.get("start_time")
        try:
            ts_dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        except Exception:
            ts_dt = datetime.now(tz=timezone.utc)
        age_min = max(0, int((datetime.now(tz=timezone.utc) - ts_dt).total_seconds() // 60))

        spot = float(latest.get("price") or 0.0)

        # Now pull strike-level for major_pos / major_neg / gex_by_strike
        gbs: dict[float, float] = {}
        major_pos = 0.0
        major_neg = 0.0
        try:
            strike_data = self._get(f"/api/stock/{ticker}/spot-exposures/strike")
            srows = strike_data.get("data") if isinstance(strike_data, dict) else strike_data
            if srows:
                # Aggregate net gamma per strike (call_gamma_oi + put_gamma_oi)
                for r in srows:
                    try:
                        strike = float(r.get("strike") or 0)
                        net_gamma = (
                            float(r.get("call_gamma_oi") or 0)
                            + float(r.get("put_gamma_oi") or 0)
                        )
                        gbs[strike] = net_gamma
                    except Exception:
                        continue
                if gbs:
                    major_pos = max(gbs.items(), key=lambda kv: kv[1])[0]
                    major_neg = min(gbs.items(), key=lambda kv: kv[1])[0]
        except Exception:
            pass

        # Gamma flip (zero-gamma) — not directly given, estimate as
        # the strike where cumulative gamma crosses zero from below.
        # Sort strikes ascending, sum cumulative gamma, find first sign flip.
        gamma_flip = 0.0
        if gbs:
            sorted_strikes = sorted(gbs.items())
            cum = 0.0
            prev_strike = sorted_strikes[0][0]
            for strike, gamma in sorted_strikes:
                cum_prev = cum
                cum += gamma
                if cum_prev < 0 and cum >= 0:
                    gamma_flip = strike
                    break
                prev_strike = strike

        return GEXSnapshot(
            ticker=ticker,
            timestamp=ts_dt,
            spot_price=spot,
            gamma_flip=gamma_flip,
            major_pos_gex_strike=major_pos,
            major_neg_gex_strike=major_neg,
            gex_by_strike=gbs,
            age_min=age_min,
        )

    def greek_exposure_history(self, ticker: str, days: int = 252) -> list[GEXSnapshot]:
        """FND-3.3 — GEX history."""
        data = self._get(
            f"/api/stock/{ticker}/greek-exposure-history",
            params={"days": days},
        )
        rows = data.get("data") if isinstance(data, dict) else data
        out: list[GEXSnapshot] = []
        if isinstance(rows, list):
            for r in rows:
                try:
                    out.append(self._gex_row_to_snapshot(ticker, r))
                except Exception:
                    continue
        return out

    def _gex_row_to_snapshot(self, ticker: str, row: dict) -> GEXSnapshot:
        ts_iso = row.get("date") or row.get("timestamp") or row.get("ts")
        ts_dt = datetime.fromisoformat(str(ts_iso).replace("Z", "+00:00"))
        gbs_raw = row.get("gex_by_strike") or {}
        gbs: dict[float, float] = {}
        if isinstance(gbs_raw, dict):
            gbs = {float(k): float(v) for k, v in gbs_raw.items() if v is not None}
        return GEXSnapshot(
            ticker=ticker,
            timestamp=ts_dt,
            spot_price=float(row.get("spot") or 0.0),
            gamma_flip=float(row.get("gamma_flip") or 0.0),
            major_pos_gex_strike=float(row.get("major_pos_gex_strike") or 0.0),
            major_neg_gex_strike=float(row.get("major_neg_gex_strike") or 0.0),
            gex_by_strike=gbs,
            age_min=max(0, int((datetime.now(tz=timezone.utc) - ts_dt).total_seconds() // 60)),
        )

    def iv_term_structure(self, ticker: str) -> list[dict]:
        """FND-3.4. ATM IV per expiry + slope. Returns raw rows (per-expiry IV)."""
        data = self._get(f"/api/stock/{ticker}/iv-term-structure")
        return data.get("data") if isinstance(data, dict) else data or []

    def earnings_flow_summary(self, ticker: str) -> dict:
        """FND-3.4. Pre-earnings call/put ratio (S4 deferred)."""
        data = self._get(f"/api/stock/{ticker}/earnings/flow-summary")
        return data if isinstance(data, dict) else {}


if __name__ == "__main__":
    # Sanity: load secrets, instantiate, hit /greek-exposure once.
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from infra.secrets import load_secrets
    load_secrets()
    print(f"[uw] base_url = {UW_BASE_URL}")
    with UWClient() as c:
        snap = c.greek_exposure("SPY")
        print(f"[uw] SPY GEX: ts={snap.timestamp} spot={snap.spot_price} "
              f"major_pos={snap.major_pos_gex_strike} major_neg={snap.major_neg_gex_strike} "
              f"gamma_flip={snap.gamma_flip} age_min={snap.age_min} "
              f"strikes_count={len(snap.gex_by_strike)}")
