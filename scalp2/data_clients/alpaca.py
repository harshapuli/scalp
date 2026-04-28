"""
data_clients/alpaca.py — Alpaca client for paper + live execution + market data.

Replaces Polygon for MVP per user direction (Alpaca Pro provides SIP-aligned
bars + trades + quotes + historical option chains). Spec FND-4 + (substituted)
FND-1 / FND-2 functionality.

Two API roots:
  TRADING:      https://paper-api.alpaca.markets   /  https://api.alpaca.markets
  MARKET DATA:  https://data.alpaca.markets

Endpoints used:
  Trading:
    POST /v2/orders                — equity bracket (FND-4)
    POST /v2/orders (mleg)         — multi-leg vertical (FND-4)
    GET  /v2/positions             — open positions (S5 risk manager)
    GET  /v2/account               — equity, BP, daytrade count (S5 sizing)

  Data:
    GET  /v2/stocks/{ticker}/bars              — historical 1m bars (replaces FND-2)
    GET  /v2/stocks/{ticker}/trades            — trades (replaces FND-1 T.)
    GET  /v2/stocks/{ticker}/quotes            — NBBO (replaces FND-1 Q.)
    WS   /v1beta3/crypto/us OR /v2/iex|sip     — realtime (replaces FND-1 WS)
    GET  /v2/options/{symbol}/snapshot         — option chain snapshot (Pro tier)
    GET  /v1beta1/options/snapshots            — bulk option snapshots (Pro tier)

Auth:
  Trading: APCA-API-KEY-ID + APCA-API-SECRET-KEY headers
  Data:    same headers (one set of credentials for both APIs)

Idempotency: client_order_id with 'edge' prefix per spec §10.4.
"""
from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Iterable, Literal, Optional

import httpx


# Env var names — supports both spec canonical and existing parallel-project legacy
ALPACA_KEY_ID_ENVS = ("ALPACA_KEY_ID", "ALPACA_API_KEY")
ALPACA_SECRET_ENVS = ("ALPACA_SECRET", "ALPACA_API_SECRET")
ALPACA_ENDPOINT_ENVS = ("ALPACA_ENDPOINT", "ALPACA_BASE_URL")
ALPACA_DATA_URL_ENVS = ("ALPACA_DATA_URL",)

DEFAULT_PAPER_TRADING = "https://paper-api.alpaca.markets"
DEFAULT_LIVE_TRADING = "https://api.alpaca.markets"
DEFAULT_DATA_URL = "https://data.alpaca.markets"


def _first_env(envs: Iterable[str]) -> Optional[str]:
    for e in envs:
        v = os.environ.get(e)
        if v:
            return v
    return None


@dataclass
class Bar:
    """1-minute bar from Alpaca data API."""
    t: datetime         # bar open time UTC
    o: float
    h: float
    l: float
    c: float
    v: int
    vw: float           # VWAP for the bar
    n: int = 0          # trade count (Pro tier)


@dataclass
class Trade:
    """One trade from Alpaca data API."""
    t: datetime
    price: float
    size: int
    conditions: list[str]


@dataclass
class Quote:
    """One NBBO quote."""
    t: datetime
    bid: float
    bid_size: int
    ask: float
    ask_size: int

    @property
    def midpoint(self) -> float:
        return (self.bid + self.ask) / 2.0

    @property
    def spread_pct(self) -> float:
        if self.midpoint <= 0:
            return 0.0
        return (self.ask - self.bid) / self.midpoint


@dataclass
class Account:
    equity: float
    buying_power: float
    cash: float
    daytrade_count: int
    pattern_day_trader: bool


@dataclass
class Position:
    symbol: str
    qty: int
    avg_entry_price: float
    current_price: float
    market_value: float
    unrealized_pl: float
    unrealized_plpc: float
    side: Literal["long", "short"]


# ──────────────────────────────────────────────────────────────────────────────
# Client
# ──────────────────────────────────────────────────────────────────────────────


class AlpacaClient:
    """Combined trading + market data client. FND-4 + (substituted) FND-1/FND-2."""

    def __init__(self,
                 key_id: Optional[str] = None,
                 secret: Optional[str] = None,
                 trading_url: Optional[str] = None,
                 data_url: Optional[str] = None,
                 client_order_id_prefix: str = "edge",
                 timeout_s: float = 15.0):
        self.key_id = key_id or _first_env(ALPACA_KEY_ID_ENVS)
        self.secret = secret or _first_env(ALPACA_SECRET_ENVS)
        if not self.key_id or not self.secret:
            raise RuntimeError(
                f"Alpaca credentials not set. Looked for "
                f"{ALPACA_KEY_ID_ENVS} + {ALPACA_SECRET_ENVS}. "
                f"Per FND-5: load via infra.secrets.load_secrets()."
            )
        self.trading_url = (
            trading_url or _first_env(ALPACA_ENDPOINT_ENVS) or DEFAULT_PAPER_TRADING
        )
        self.data_url = (
            data_url or _first_env(ALPACA_DATA_URL_ENVS) or DEFAULT_DATA_URL
        )
        self.client_order_id_prefix = client_order_id_prefix
        self.timeout_s = timeout_s
        self._headers = {
            "APCA-API-KEY-ID": self.key_id,
            "APCA-API-SECRET-KEY": self.secret,
            "Accept": "application/json",
            "User-Agent": "scalp2/0.1",
        }
        self._trading = httpx.Client(
            base_url=self.trading_url, headers=self._headers, timeout=self.timeout_s
        )
        self._data = httpx.Client(
            base_url=self.data_url, headers=self._headers, timeout=self.timeout_s
        )

    @property
    def is_live(self) -> bool:
        return "paper" not in self.trading_url.lower()

    def close(self) -> None:
        self._trading.close()
        self._data.close()

    def __enter__(self): return self
    def __exit__(self, *args): self.close()

    def _make_client_order_id(self) -> str:
        return f"{self.client_order_id_prefix}-{uuid.uuid4().hex[:16]}"

    # ─── Account / Positions (FND-4) ─────────────────────────────────────

    def get_account(self) -> Account:
        r = self._trading.get("/v2/account")
        r.raise_for_status()
        d = r.json()
        return Account(
            equity=float(d.get("equity") or 0.0),
            buying_power=float(d.get("buying_power") or 0.0),
            cash=float(d.get("cash") or 0.0),
            daytrade_count=int(d.get("daytrade_count") or 0),
            pattern_day_trader=bool(d.get("pattern_day_trader") or False),
        )

    def get_positions(self) -> list[Position]:
        r = self._trading.get("/v2/positions")
        r.raise_for_status()
        out = []
        for p in r.json():
            try:
                out.append(Position(
                    symbol=p.get("symbol"),
                    qty=int(float(p.get("qty") or 0)),
                    avg_entry_price=float(p.get("avg_entry_price") or 0.0),
                    current_price=float(p.get("current_price") or 0.0),
                    market_value=float(p.get("market_value") or 0.0),
                    unrealized_pl=float(p.get("unrealized_pl") or 0.0),
                    unrealized_plpc=float(p.get("unrealized_plpc") or 0.0),
                    side=p.get("side") or "long",
                ))
            except Exception:
                continue
        return out

    # ─── Orders (FND-4) ──────────────────────────────────────────────────

    def submit_equity_bracket(self,
                               symbol: str,
                               qty: int,
                               side: Literal["buy", "sell"],
                               take_profit_price: float,
                               stop_loss_price: float,
                               client_order_id: Optional[str] = None) -> dict:
        """FND-4. Equity bracket order. Returns Alpaca order JSON."""
        payload = {
            "symbol": symbol,
            "qty": str(qty),
            "side": side,
            "type": "market",
            "time_in_force": "day",
            "order_class": "bracket",
            "take_profit": {"limit_price": str(take_profit_price)},
            "stop_loss": {"stop_price": str(stop_loss_price)},
            "client_order_id": client_order_id or self._make_client_order_id(),
        }
        r = self._trading.post("/v2/orders", json=payload)
        r.raise_for_status()
        return r.json()

    def submit_vertical(self,
                         underlying: str,
                         long_leg_symbol: str,    # OCC formatted
                         short_leg_symbol: str,
                         qty: int,
                         limit_price: float,
                         side: Literal["buy", "sell"] = "buy",
                         client_order_id: Optional[str] = None) -> dict:
        """FND-4 + FND-4.T1. Multi-leg vertical with order_class='mleg'.

        Per spec test FND-4.T1 — legs payload uses ratio_qty='1' and explicit
        position_intent ('buy_to_open' / 'sell_to_open').
        """
        payload = {
            "order_class": "mleg",
            "qty": str(qty),
            "type": "limit",
            "limit_price": str(limit_price),
            "time_in_force": "day",
            "client_order_id": client_order_id or self._make_client_order_id(),
            "legs": [
                {
                    "symbol": long_leg_symbol,
                    "ratio_qty": "1",
                    "side": side,
                    "position_intent": "buy_to_open",
                },
                {
                    "symbol": short_leg_symbol,
                    "ratio_qty": "1",
                    "side": "sell" if side == "buy" else "buy",
                    "position_intent": "sell_to_open",
                },
            ],
        }
        r = self._trading.post("/v2/orders", json=payload)
        r.raise_for_status()
        return r.json()

    def cancel_order(self, order_id: str) -> None:
        r = self._trading.delete(f"/v2/orders/{order_id}")
        r.raise_for_status()

    def close_all_positions(self) -> list[dict]:
        """For S5-60.T1 kill switch — close everything."""
        r = self._trading.delete("/v2/positions", params={"cancel_orders": "true"})
        r.raise_for_status()
        return r.json()

    # ─── Market data: bars / trades / quotes (substitutes FND-1, FND-2) ───

    def get_bars(self,
                  symbol: str,
                  start: str,         # ISO 'YYYY-MM-DDTHH:MM:SSZ' or 'YYYY-MM-DD'
                  end: str,
                  timeframe: str = "1Min",
                  feed: str = "sip",  # 'sip' for Pro tier; 'iex' for free tier
                  limit: int = 10000,
                  ) -> list[Bar]:
        """Replaces Polygon FND-2 historical bars. Pagination handled."""
        out: list[Bar] = []
        page_token: Optional[str] = None
        while True:
            params = {
                "symbols": symbol,
                "start": start,
                "end": end,
                "timeframe": timeframe,
                "feed": feed,
                "limit": str(limit),
            }
            if page_token:
                params["page_token"] = page_token
            r = self._data.get("/v2/stocks/bars", params=params)
            r.raise_for_status()
            d = r.json()
            bars = (d.get("bars") or {}).get(symbol) or []
            for b in bars:
                try:
                    out.append(Bar(
                        t=datetime.fromisoformat(b["t"].replace("Z", "+00:00")),
                        o=float(b["o"]), h=float(b["h"]), l=float(b["l"]),
                        c=float(b["c"]), v=int(b["v"]),
                        vw=float(b.get("vw") or b["c"]),
                        n=int(b.get("n") or 0),
                    ))
                except Exception:
                    continue
            page_token = d.get("next_page_token")
            if not page_token:
                break
        return out

    def get_trades(self,
                    symbol: str,
                    start: str,
                    end: str,
                    feed: str = "sip",
                    limit: int = 10000) -> list[Trade]:
        """Replaces Polygon FND-1 T. trades. Used for Lee-Ready aggressor."""
        out: list[Trade] = []
        page_token = None
        while True:
            params = {
                "symbols": symbol, "start": start, "end": end,
                "feed": feed, "limit": str(limit),
            }
            if page_token:
                params["page_token"] = page_token
            r = self._data.get("/v2/stocks/trades", params=params)
            r.raise_for_status()
            d = r.json()
            trades = (d.get("trades") or {}).get(symbol) or []
            for tr in trades:
                try:
                    out.append(Trade(
                        t=datetime.fromisoformat(tr["t"].replace("Z", "+00:00")),
                        price=float(tr["p"]),
                        size=int(tr["s"]),
                        conditions=tr.get("c") or [],
                    ))
                except Exception:
                    continue
            page_token = d.get("next_page_token")
            if not page_token:
                break
        return out

    def get_quotes(self,
                    symbol: str,
                    start: str,
                    end: str,
                    feed: str = "sip",
                    limit: int = 10000) -> list[Quote]:
        """Replaces Polygon FND-1 Q. NBBO. Used for Lee-Ready midpoint."""
        out: list[Quote] = []
        page_token = None
        while True:
            params = {
                "symbols": symbol, "start": start, "end": end,
                "feed": feed, "limit": str(limit),
            }
            if page_token:
                params["page_token"] = page_token
            r = self._data.get("/v2/stocks/quotes", params=params)
            r.raise_for_status()
            d = r.json()
            quotes = (d.get("quotes") or {}).get(symbol) or []
            for q in quotes:
                try:
                    out.append(Quote(
                        t=datetime.fromisoformat(q["t"].replace("Z", "+00:00")),
                        bid=float(q["bp"]), bid_size=int(q["bs"]),
                        ask=float(q["ap"]), ask_size=int(q["as"]),
                    ))
                except Exception:
                    continue
            page_token = d.get("next_page_token")
            if not page_token:
                break
        return out

    # ─── Options data (Pro tier) ─────────────────────────────────────────

    def get_option_chain(self, underlying: str, expiry: Optional[date] = None) -> dict:
        """Pro tier. Returns option chain snapshot."""
        params = {}
        if expiry:
            params["expiration_date"] = expiry.isoformat()
        r = self._data.get(f"/v1beta1/options/snapshots/{underlying}", params=params)
        r.raise_for_status()
        return r.json()

    def get_option_contracts_with_oi(self,
                                       underlying: str,
                                       expiration_min: date,
                                       expiration_max: date,
                                       strike_pct_band: float = 0.10,
                                       spot_price: Optional[float] = None) -> list[dict]:
        """Pull active option contracts via TRADING API (which returns OI).

        Returns list of {symbol, strike, type, expiration, open_interest, close_price}.
        Used to synthesize proxy GEX strikes from highest-OI call/put walls.

        spot_price: if provided, filter strikes to ±strike_pct_band of spot. If None,
                     no strike filter (returns full chain — slow).
        """
        params = {
            "underlying_symbols": underlying,
            "expiration_date_gte": expiration_min.isoformat(),
            "expiration_date_lte": expiration_max.isoformat(),
            "status": "active",
            "limit": "1000",
        }
        if spot_price:
            params["strike_price_gte"] = str(spot_price * (1 - strike_pct_band))
            params["strike_price_lte"] = str(spot_price * (1 + strike_pct_band))

        out = []
        page_token = None
        while True:
            if page_token:
                params["page_token"] = page_token
            r = self._trading.get("/v2/options/contracts", params=params)
            r.raise_for_status()
            d = r.json()
            for c in d.get("option_contracts", []):
                try:
                    out.append({
                        "symbol": c.get("symbol"),
                        "strike": float(c.get("strike_price")),
                        "type": c.get("type"),    # 'call' | 'put'
                        "expiration": c.get("expiration_date"),
                        "open_interest": int(c.get("open_interest") or 0),
                        "close_price": float(c.get("close_price") or 0),
                    })
                except Exception:
                    continue
            page_token = d.get("next_page_token")
            if not page_token:
                break
        return out


def infer_proxy_gex(chain: list[dict], spot_price: float
                      ) -> tuple[float, float, dict[float, float]]:
    """Synthesize proxy +GEX / -GEX strikes from option chain OI.

    Method: aggregate OI across expirations per (strike, type). Highest call-OI
    strike near spot ≈ major +GEX (call wall); highest put-OI strike ≈ major -GEX.
    Net OI per strike (call_oi - put_oi) approximates per-strike net gamma sign.

    Returns (major_pos_gex_strike, major_neg_gex_strike, net_gamma_by_strike).
    """
    if not chain:
        return 0.0, 0.0, {}

    call_oi_per_strike: dict[float, int] = {}
    put_oi_per_strike: dict[float, int] = {}
    for c in chain:
        s = c["strike"]
        oi = c.get("open_interest", 0)
        if c["type"] == "call":
            call_oi_per_strike[s] = call_oi_per_strike.get(s, 0) + oi
        elif c["type"] == "put":
            put_oi_per_strike[s] = put_oi_per_strike.get(s, 0) + oi

    if not call_oi_per_strike and not put_oi_per_strike:
        return 0.0, 0.0, {}

    # Major call wall = highest call OI strike (proxy +GEX)
    major_pos = (
        max(call_oi_per_strike.items(), key=lambda kv: kv[1])[0]
        if call_oi_per_strike else 0.0
    )
    # Major put wall = highest put OI strike (proxy -GEX)
    major_neg = (
        max(put_oi_per_strike.items(), key=lambda kv: kv[1])[0]
        if put_oi_per_strike else 0.0
    )
    # Net gamma by strike = call OI - put OI (sign approximates)
    all_strikes = set(call_oi_per_strike) | set(put_oi_per_strike)
    net_gamma = {
        s: call_oi_per_strike.get(s, 0) - put_oi_per_strike.get(s, 0)
        for s in all_strikes
    }
    return float(major_pos), float(major_neg), net_gamma


if __name__ == "__main__":
    import sys
    from pathlib import Path
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from infra.secrets import load_secrets
    load_secrets()

    with AlpacaClient() as c:
        print(f"[alpaca] trading_url = {c.trading_url}  (live={c.is_live})")
        print(f"[alpaca] data_url    = {c.data_url}")
        acct = c.get_account()
        print(f"[alpaca] account: equity=${acct.equity:.2f} BP=${acct.buying_power:.2f} cash=${acct.cash:.2f}")
        print(f"[alpaca] daytrade_count={acct.daytrade_count} PDT={acct.pattern_day_trader}")
        positions = c.get_positions()
        print(f"[alpaca] positions: {len(positions)}")
        for p in positions[:3]:
            print(f"  · {p.symbol} qty={p.qty} avg={p.avg_entry_price} mv={p.market_value}")

        # 1mo SPY bars sanity (FND-2 acceptance proxy)
        from datetime import datetime, timedelta, timezone
        end = datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        start = (datetime.now(tz=timezone.utc) - timedelta(days=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
        try:
            bars = c.get_bars("SPY", start, end, timeframe="1Min", feed="sip", limit=1000)
            print(f"[alpaca] SPY 1m bars (last 5d): {len(bars)} bars (first ts={bars[0].t if bars else None})")
        except httpx.HTTPStatusError as e:
            # SIP feed requires Pro; fall back to IEX
            print(f"[alpaca] SIP failed ({e.response.status_code}), trying IEX...")
            bars = c.get_bars("SPY", start, end, timeframe="1Min", feed="iex", limit=1000)
            print(f"[alpaca] SPY 1m bars (IEX, last 5d): {len(bars)} bars")
