"""
Unusual Whales API Integration Module

Wraps the Unusual Whales API to provide structured data for the trading engine.
Provides institutional sentiment analysis, whale trade detection, and dark pool insights.

API Base: https://api.unusualwhales.com
Auth: Bearer token via UW_API_KEY config
"""

import requests
import logging
from typing import Optional, List, Dict, Any
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta
from functools import wraps
import time
from enum import Enum

# Configuration
try:
    from config_v2 import UW_API_KEY
except ImportError:
    UW_API_KEY = ""

logger = logging.getLogger(__name__)

# Constants
UW_BASE_URL = "https://api.unusualwhales.com"
RATE_LIMIT_DELAY = 0.5  # 2 calls/sec max
CACHE_TTL = 30  # 30-second cache


# ============================================================================
# ENUMS
# ============================================================================

class SentimentEnum(str, Enum):
    BULLISH = "BULLISH"
    BEARISH = "BEARISH"
    NEUTRAL = "NEUTRAL"


class AlertRuleEnum(str, Enum):
    """Flow alert rule types."""
    REPEATED_HITS_DESCENDING = "RepeatedHitsDescending"
    UNUSUAL_SWEEP = "UnusualSweep"
    BLOCK_TRADE = "BlockTrade"
    REPEATED_HITS_ASCENDING = "RepeatedHitsAscending"


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class DarkPoolPrint:
    """Single dark pool print execution."""
    ticker: str
    size: int
    price: float
    volume: int  # Total volume (shares) for this print
    executed_at: datetime
    premium: float  # Dollar amount (price * size)
    canceled: bool
    nbbo_bid: float
    nbbo_ask: float
    market_center: str  # L = ArcaX, etc.
    nbbo_bid_quantity: int
    nbbo_ask_quantity: int
    tracking_id: int


@dataclass
class FlowAlert:
    """Unusual options activity alert."""
    ticker: str
    option_type: str  # 'put', 'call'
    created_at: datetime
    price: float  # Strike price
    volume: int  # Volume traded
    open_interest: int
    expiry: str  # YYYY-MM-DD
    strike: float
    underlying_price: float
    total_premium: float
    trade_count: int
    iv_start: float  # Implied vol start
    iv_end: float    # Implied vol end
    has_floor: bool
    has_multileg: bool
    has_sweep: bool
    total_size: int
    alert_rule: str  # AlertRule type


@dataclass
class NetPremiumTick:
    """5-minute net premium data (intraday)."""
    date: str  # YYYY-MM-DD
    tape_time: datetime
    call_volume: int
    put_volume: int
    call_volume_ask_side: int
    call_volume_bid_side: int
    put_volume_ask_side: int
    put_volume_bid_side: int
    net_call_volume: int
    net_call_premium: float
    net_put_volume: int
    net_put_premium: float
    net_delta: float


@dataclass
class OptionsVolumeSummary:
    """Daily options volume summary."""
    date: str  # YYYY-MM-DD
    call_volume: int
    put_volume: int
    call_volume_ask_side: int
    call_volume_bid_side: int
    put_volume_ask_side: int
    put_volume_bid_side: int
    net_call_premium: float
    net_put_premium: float
    put_premium: float
    call_premium: float
    avg_30_day_call_volume: float
    avg_30_day_put_volume: float


@dataclass
class MarketTide:
    """5-minute market-wide sentiment."""
    timestamp: datetime
    date: str  # YYYY-MM-DD
    net_call_premium: float
    net_put_premium: float
    net_volume: int


@dataclass
class OptionTrade:
    """Individual option trade (tick-level)."""
    executed_at: datetime
    option_chain_id: str
    price: float
    theta: float
    option_type: str  # 'put', 'call'
    size: int
    nbbo_ask: float
    gamma: float
    volume: int
    open_interest: int
    delta: float
    # Extended fields
    multi_vol: int = 0
    bid_vol: int = 0


@dataclass
class DarkPoolLevel:
    """Clustered dark pool accumulation/distribution level."""
    price: float
    total_volume: int
    trade_count: int
    direction_bias: str  # 'ACCUMULATION', 'DISTRIBUTION', 'NEUTRAL'
    last_activity: datetime
    avg_print_size: float = 0.0


@dataclass
class WhaleSignalLive:
    """Real-time whale signal from latest options flow."""
    ticker: str
    direction: str  # BULLISH, BEARISH, NEUTRAL
    confidence: float  # 0-100
    timestamp: datetime
    premium_flow: float  # Net $ from options (calls - puts)
    dark_pool_flow: int  # Total DP volume
    key_trades: List[Dict[str, Any]] = field(default_factory=list)
    sweep_count: int = 0
    block_count: int = 0
    alert_count: int = 0


@dataclass
class InstitutionalSentiment:
    """Aggregated institutional sentiment for a ticker."""
    ticker: str
    timestamp: datetime
    net_premium: float  # $ (calls - puts)
    net_premium_direction: str  # BULLISH, BEARISH, NEUTRAL
    dark_pool_volume: int  # Total DP volume
    dark_pool_sentiment: str  # ACCUMULATION, DISTRIBUTION, NEUTRAL
    net_delta: float
    sweep_count: int
    alert_count: int
    put_call_ratio: float
    overall_score: float  # 0-100 institutional conviction
    summary: str


# ============================================================================
# RATE LIMITING & CACHING
# ============================================================================

class SimpleCache:
    """Simple in-memory cache with TTL."""

    def __init__(self, ttl_seconds: int = 30):
        self.ttl = ttl_seconds
        self.cache = {}

    def get(self, key: str) -> Optional[Any]:
        if key in self.cache:
            value, expiry = self.cache[key]
            if datetime.now() < expiry:
                return value
            else:
                del self.cache[key]
        return None

    def set(self, key: str, value: Any):
        self.cache[key] = (value, datetime.now() + timedelta(seconds=self.ttl))

    def clear(self):
        self.cache.clear()


# ============================================================================
# UNUSUAL WHALES API CLIENT
# ============================================================================

class UnusualWhalesClient:
    """
    Client for Unusual Whales API.

    Uses verified working endpoints to fetch:
    - Dark pool prints (/api/darkpool/{ticker})
    - Flow alerts - unusual options (/api/stock/{ticker}/flow-alerts)
    - Net premium ticks - intraday 5min (/api/stock/{ticker}/net-prem-ticks)
    - Options volume - daily (/api/stock/{ticker}/options-volume)
    - Market sentiment - 5min (/api/market/market-tide)
    - Individual trades - tick level (/api/stock/{ticker}/flow-recent)
    """

    def __init__(self, api_key: str = UW_API_KEY):
        """
        Initialize the UW API client.

        Args:
            api_key: Bearer token for API authentication

        Raises:
            ValueError: If API key is not provided
        """
        self.api_key = api_key
        self.base_url = UW_BASE_URL
        self.cache = SimpleCache(ttl_seconds=CACHE_TTL)
        self.last_request_time = 0

        if not self.api_key:
            logger.warning("No UW_API_KEY provided. API calls will fail.")

    def _get_headers(self) -> Dict[str, str]:
        """Return request headers with authentication."""
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "User-Agent": "TradingEngine/2.0"
        }

    def _rate_limit(self):
        """Enforce 2 requests/sec rate limit."""
        elapsed = time.time() - self.last_request_time
        if elapsed < RATE_LIMIT_DELAY:
            time.sleep(RATE_LIMIT_DELAY - elapsed)
        self.last_request_time = time.time()

    def _get(self, endpoint: str, params: Optional[Dict] = None) -> Optional[Dict]:
        """
        Make GET request to UW API.

        Args:
            endpoint: API endpoint (with /api prefix)
            params: Query parameters

        Returns:
            Parsed JSON response or None on error
        """
        self._rate_limit()

        url = f"{self.base_url}{endpoint}"

        try:
            response = requests.get(
                url,
                headers=self._get_headers(),
                params=params,
                timeout=10
            )
            response.raise_for_status()
            return response.json()
        except requests.exceptions.RequestException as e:
            logger.error(f"UW API request failed ({endpoint}): {e}")
            return None

    # ========================================================================
    # CORE DATA METHODS
    # ========================================================================

    def get_dark_pool(self, ticker: str) -> List[DarkPoolPrint]:
        """
        Get actual dark pool prints for a ticker.

        Endpoint: /api/darkpool/{ticker}

        Args:
            ticker: Stock ticker symbol

        Returns:
            List of DarkPoolPrint objects
        """
        cache_key = f"dark_pool:{ticker}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get(f"/api/darkpool/{ticker}")
        if not data:
            return []

        prints = []
        for item in data.get("data", []):
            try:
                print_obj = DarkPoolPrint(
                    ticker=item.get("ticker", ticker),
                    size=int(item.get("size", 0)),
                    price=float(item.get("price", 0)),
                    volume=int(item.get("volume", 0)),
                    executed_at=self._parse_timestamp(item.get("executed_at")),
                    premium=float(item.get("premium", 0)),
                    canceled=item.get("canceled", False),
                    nbbo_bid=float(item.get("nbbo_bid", 0)),
                    nbbo_ask=float(item.get("nbbo_ask", 0)),
                    market_center=item.get("market_center", ""),
                    nbbo_bid_quantity=int(item.get("nbbo_bid_quantity", 0)),
                    nbbo_ask_quantity=int(item.get("nbbo_ask_quantity", 0)),
                    tracking_id=int(item.get("tracking_id", 0))
                )
                prints.append(print_obj)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Error parsing dark pool print: {e}")
                continue

        self.cache.set(cache_key, prints)
        return prints

    def get_dark_pool_recent(self) -> List[DarkPoolPrint]:
        """
        Get recent dark pool prints across all tickers.

        Endpoint: /api/darkpool/recent

        Returns:
            List of DarkPoolPrint objects
        """
        cache_key = "dark_pool:recent"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get("/api/darkpool/recent")
        if not data:
            return []

        prints = []
        for item in data.get("data", []):
            try:
                print_obj = DarkPoolPrint(
                    ticker=item.get("ticker", ""),
                    size=int(item.get("size", 0)),
                    price=float(item.get("price", 0)),
                    volume=int(item.get("volume", 0)),
                    executed_at=self._parse_timestamp(item.get("executed_at")),
                    premium=float(item.get("premium", 0)),
                    canceled=item.get("canceled", False),
                    nbbo_bid=float(item.get("nbbo_bid", 0)),
                    nbbo_ask=float(item.get("nbbo_ask", 0)),
                    market_center=item.get("market_center", ""),
                    nbbo_bid_quantity=int(item.get("nbbo_bid_quantity", 0)),
                    nbbo_ask_quantity=int(item.get("nbbo_ask_quantity", 0)),
                    tracking_id=int(item.get("tracking_id", 0))
                )
                prints.append(print_obj)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Error parsing dark pool print: {e}")
                continue

        self.cache.set(cache_key, prints)
        return prints

    def get_flow_alerts(self, ticker: str) -> List[FlowAlert]:
        """
        Get unusual options activity alerts (unusual flow).

        Endpoint: /api/stock/{ticker}/flow-alerts

        Args:
            ticker: Stock ticker symbol

        Returns:
            List of FlowAlert objects
        """
        cache_key = f"flow_alerts:{ticker}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get(f"/api/stock/{ticker}/flow-alerts")
        if not data:
            return []

        alerts = []
        for item in data.get("data", []):
            try:
                alert = FlowAlert(
                    ticker=item.get("ticker", ticker),
                    option_type=item.get("type", "").lower(),
                    created_at=self._parse_timestamp(item.get("created_at")),
                    price=float(item.get("price", 0)),
                    volume=int(item.get("volume", 0)),
                    open_interest=int(item.get("open_interest", 0)),
                    expiry=item.get("expiry", ""),
                    strike=float(item.get("strike", 0)),
                    underlying_price=float(item.get("underlying_price", 0)),
                    total_premium=float(item.get("total_premium", 0)),
                    trade_count=int(item.get("trade_count", 0)),
                    iv_start=float(item.get("iv_start", 0)),
                    iv_end=float(item.get("iv_end", 0)),
                    has_floor=item.get("has_floor", False),
                    has_multileg=item.get("has_multileg", False),
                    has_sweep=item.get("has_sweep", False),
                    total_size=int(item.get("total_size", 0)),
                    alert_rule=item.get("alert_rule", "")
                )
                alerts.append(alert)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Error parsing flow alert: {e}")
                continue

        self.cache.set(cache_key, alerts)
        return alerts

    def get_market_flow_alerts(self) -> List[FlowAlert]:
        """
        Get market-wide unusual options activity alerts.

        Endpoint: /api/option-trades/flow-alerts

        Returns:
            List of FlowAlert objects across all tickers
        """
        cache_key = "flow_alerts:market"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get("/api/option-trades/flow-alerts")
        if not data:
            return []

        alerts = []
        for item in data.get("data", []):
            try:
                alert = FlowAlert(
                    ticker=item.get("ticker", ""),
                    option_type=item.get("type", "").lower(),
                    created_at=self._parse_timestamp(item.get("created_at")),
                    price=float(item.get("price", 0)),
                    volume=int(item.get("volume", 0)),
                    open_interest=int(item.get("open_interest", 0)),
                    expiry=item.get("expiry", ""),
                    strike=float(item.get("strike", 0)),
                    underlying_price=float(item.get("underlying_price", 0)),
                    total_premium=float(item.get("total_premium", 0)),
                    trade_count=int(item.get("trade_count", 0)),
                    iv_start=float(item.get("iv_start", 0)),
                    iv_end=float(item.get("iv_end", 0)),
                    has_floor=item.get("has_floor", False),
                    has_multileg=item.get("has_multileg", False),
                    has_sweep=item.get("has_sweep", False),
                    total_size=int(item.get("total_size", 0)),
                    alert_rule=item.get("alert_rule", "")
                )
                alerts.append(alert)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Error parsing market flow alert: {e}")
                continue

        self.cache.set(cache_key, alerts)
        return alerts

    def get_net_premium_ticks(self, ticker: str) -> List[NetPremiumTick]:
        """
        Get 5-minute intraday net premium data.

        Endpoint: /api/stock/{ticker}/net-prem-ticks

        Args:
            ticker: Stock ticker symbol

        Returns:
            List of NetPremiumTick objects
        """
        cache_key = f"net_prem_ticks:{ticker}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get(f"/api/stock/{ticker}/net-prem-ticks")
        if not data:
            return []

        ticks = []
        for item in data.get("data", []):
            try:
                tick = NetPremiumTick(
                    date=item.get("date", ""),
                    tape_time=self._parse_timestamp(item.get("tape_time")),
                    call_volume=int(item.get("call_volume", 0)),
                    put_volume=int(item.get("put_volume", 0)),
                    call_volume_ask_side=int(item.get("call_volume_ask_side", 0)),
                    call_volume_bid_side=int(item.get("call_volume_bid_side", 0)),
                    put_volume_ask_side=int(item.get("put_volume_ask_side", 0)),
                    put_volume_bid_side=int(item.get("put_volume_bid_side", 0)),
                    net_call_volume=int(item.get("net_call_volume", 0)),
                    net_call_premium=float(item.get("net_call_premium", 0)),
                    net_put_volume=int(item.get("net_put_volume", 0)),
                    net_put_premium=float(item.get("net_put_premium", 0)),
                    net_delta=float(item.get("net_delta", 0))
                )
                ticks.append(tick)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Error parsing net premium tick: {e}")
                continue

        self.cache.set(cache_key, ticks)
        return ticks

    def get_options_volume(self, ticker: str) -> Optional[OptionsVolumeSummary]:
        """
        Get daily options volume summary.

        Endpoint: /api/stock/{ticker}/options-volume

        Args:
            ticker: Stock ticker symbol

        Returns:
            OptionsVolumeSummary object or None
        """
        cache_key = f"options_volume:{ticker}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get(f"/api/stock/{ticker}/options-volume")
        if not data:
            return None

        # Get most recent entry
        items = data.get("data", [])
        if not items:
            return None

        item = items[0]  # Typically most recent is first
        try:
            summary = OptionsVolumeSummary(
                date=item.get("date", ""),
                call_volume=int(item.get("call_volume", 0)),
                put_volume=int(item.get("put_volume", 0)),
                call_volume_ask_side=int(item.get("call_volume_ask_side", 0)),
                call_volume_bid_side=int(item.get("call_volume_bid_side", 0)),
                put_volume_ask_side=int(item.get("put_volume_ask_side", 0)),
                put_volume_bid_side=int(item.get("put_volume_bid_side", 0)),
                net_call_premium=float(item.get("net_call_premium", 0)),
                net_put_premium=float(item.get("net_put_premium", 0)),
                put_premium=float(item.get("put_premium", 0)),
                call_premium=float(item.get("call_premium", 0)),
                avg_30_day_call_volume=float(item.get("avg_30_day_call_volume", 0)),
                avg_30_day_put_volume=float(item.get("avg_30_day_put_volume", 0))
            )
            self.cache.set(cache_key, summary)
            return summary
        except (KeyError, ValueError, TypeError) as e:
            logger.warning(f"Error parsing options volume: {e}")
            return None

    def get_market_tide(self) -> List[MarketTide]:
        """
        Get 5-minute market-wide sentiment.

        Endpoint: /api/market/market-tide

        Returns:
            List of MarketTide objects
        """
        cache_key = "market_tide"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get("/api/market/market-tide")
        if not data:
            return []

        tides = []
        for item in data.get("data", []):
            try:
                tide = MarketTide(
                    timestamp=self._parse_timestamp(item.get("timestamp")),
                    date=item.get("date", ""),
                    net_call_premium=float(item.get("net_call_premium", 0)),
                    net_put_premium=float(item.get("net_put_premium", 0)),
                    net_volume=int(item.get("net_volume", 0))
                )
                tides.append(tide)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Error parsing market tide: {e}")
                continue

        self.cache.set(cache_key, tides)
        return tides

    def get_recent_flow(self, ticker: str) -> List[OptionTrade]:
        """
        Get individual tick-level option trades.

        Endpoint: /api/stock/{ticker}/flow-recent

        Args:
            ticker: Stock ticker symbol

        Returns:
            List of OptionTrade objects
        """
        cache_key = f"flow_recent:{ticker}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get(f"/api/stock/{ticker}/flow-recent")
        if not data:
            return []

        trades = []
        # Response may be array directly or wrapped in {"data": [...]}
        items = data if isinstance(data, list) else data.get("data", [])

        for item in items:
            try:
                trade = OptionTrade(
                    executed_at=self._parse_timestamp(item.get("executed_at")),
                    option_chain_id=item.get("option_chain_id", ""),
                    price=float(item.get("price", 0)),
                    theta=float(item.get("theta", 0)),
                    option_type=item.get("option_type", "").lower(),
                    size=int(item.get("size", 0)),
                    nbbo_ask=float(item.get("nbbo_ask", 0)),
                    gamma=float(item.get("gamma", 0)),
                    volume=int(item.get("volume", 0)),
                    open_interest=int(item.get("open_interest", 0)),
                    delta=float(item.get("delta", 0)),
                    multi_vol=int(item.get("multi_vol", 0)),
                    bid_vol=int(item.get("bid_vol", 0))
                )
                trades.append(trade)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Error parsing flow trade: {e}")
                continue

        self.cache.set(cache_key, trades)
        return trades

    def get_greek_flow(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Get Greek flow data (gamma, vega, theta exposure).

        Endpoint: /api/stock/{ticker}/greek-flow

        Args:
            ticker: Stock ticker symbol

        Returns:
            Dict with greek flow data or None
        """
        cache_key = f"greek_flow:{ticker}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get(f"/api/stock/{ticker}/greek-flow")
        if data:
            self.cache.set(cache_key, data)
        return data

    def get_oi_change(self, ticker: str) -> Optional[Dict[str, Any]]:
        """
        Get open interest change data.

        Endpoint: /api/stock/{ticker}/oi-change

        Args:
            ticker: Stock ticker symbol

        Returns:
            Dict with OI change data or None
        """
        cache_key = f"oi_change:{ticker}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get(f"/api/stock/{ticker}/oi-change")
        if data:
            self.cache.set(cache_key, data)
        return data

    def get_congress_trades(self) -> List[Dict[str, Any]]:
        """
        Get congress trading activity.

        Endpoint: /api/congress/recent-trades

        Returns:
            List of congress trade records
        """
        cache_key = "congress_trades"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get("/api/congress/recent-trades")
        if not data:
            return []

        trades = data.get("data", [])
        self.cache.set(cache_key, trades)
        return trades

    def get_insider_transactions(self) -> List[Dict[str, Any]]:
        """
        Get insider transactions.

        Endpoint: /api/insider/transactions

        Returns:
            List of insider transaction records
        """
        cache_key = "insider_transactions"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        data = self._get("/api/insider/transactions")
        if not data:
            return []

        transactions = data.get("data", [])
        self.cache.set(cache_key, transactions)
        return transactions

    # ========================================================================
    # AGGREGATED ENGINE METHODS
    # ========================================================================

    def get_whale_signal_live(self, ticker: str) -> Optional[WhaleSignalLive]:
        """
        Get real-time whale signal from latest options flow and dark pool.

        Uses flow_alerts + dark_pool + net_premium to determine:
        - Direction: BULLISH/BEARISH/NEUTRAL based on net premium
        - Confidence: 0-100 based on sweep concentration
        - Key trades: largest/most unusual trades

        Args:
            ticker: Stock ticker symbol

        Returns:
            WhaleSignalLive object or None on error
        """
        flow_alerts = self.get_flow_alerts(ticker)
        dark_pool = self.get_dark_pool(ticker)
        net_prem_ticks = self.get_net_premium_ticks(ticker)

        if not flow_alerts and not dark_pool and not net_prem_ticks:
            return None

        # Net premium direction from most recent tick
        net_call_prem = 0.0
        net_put_prem = 0.0
        net_delta = 0.0

        if net_prem_ticks:
            latest_tick = net_prem_ticks[0]
            net_call_prem = latest_tick.net_call_premium
            net_put_prem = latest_tick.net_put_premium
            net_delta = latest_tick.net_delta

        net_premium = net_call_prem - net_put_prem

        if net_premium > 0:
            direction = "BULLISH"
        elif net_premium < 0:
            direction = "BEARISH"
        else:
            direction = "NEUTRAL"

        # Confidence based on sweep activity and size
        sweep_count = len([a for a in flow_alerts if a.has_sweep])
        block_count = len([a for a in flow_alerts if a.total_size > 1000])
        alert_count = len(flow_alerts)

        # Confidence: higher if many sweeps and large size
        confidence = min(100.0, (sweep_count + block_count) * 5.0)

        # Dark pool volume
        dp_volume = sum(p.volume for p in dark_pool)

        # Key trades: top 5 by premium
        key_trades = [
            {
                "strike": a.strike,
                "option_type": a.option_type,
                "premium": a.total_premium,
                "volume": a.volume,
                "has_sweep": a.has_sweep,
                "timestamp": a.created_at.isoformat() if a.created_at else None
            }
            for a in sorted(flow_alerts, key=lambda x: x.total_premium, reverse=True)[:5]
        ]

        return WhaleSignalLive(
            ticker=ticker,
            direction=direction,
            confidence=confidence,
            timestamp=datetime.now(),
            premium_flow=net_premium,
            dark_pool_flow=dp_volume,
            key_trades=key_trades,
            sweep_count=sweep_count,
            block_count=block_count,
            alert_count=alert_count
        )

    def get_institutional_sentiment(self, ticker: str) -> Optional[InstitutionalSentiment]:
        """
        Get comprehensive institutional sentiment analysis.

        Combines:
        - Net premium and direction
        - Dark pool accumulation/distribution
        - Sweep and alert counts
        - Put/call ratio
        - Net delta from intraday data

        Args:
            ticker: Stock ticker symbol

        Returns:
            InstitutionalSentiment object or None on error
        """
        flow_alerts = self.get_flow_alerts(ticker)
        dark_pool = self.get_dark_pool(ticker)
        options_vol = self.get_options_volume(ticker)
        net_prem_ticks = self.get_net_premium_ticks(ticker)

        if not flow_alerts and not dark_pool and not options_vol and not net_prem_ticks:
            return None

        # Net premium metrics
        net_call_prem = 0.0
        net_put_prem = 0.0
        net_delta = 0.0

        if options_vol:
            net_call_prem = options_vol.net_call_premium
            net_put_prem = options_vol.net_put_premium

        if net_prem_ticks:
            latest_tick = net_prem_ticks[0]
            net_delta = latest_tick.net_delta

        net_premium = net_call_prem - net_put_prem

        if net_premium > 0:
            prem_direction = "BULLISH"
        elif net_premium < 0:
            prem_direction = "BEARISH"
        else:
            prem_direction = "NEUTRAL"

        # Dark pool sentiment (above/below NBBO midpoint)
        dp_above_mid = 0
        dp_below_mid = 0
        dp_volume = 0

        for p in dark_pool:
            mid_price = (p.nbbo_bid + p.nbbo_ask) / 2.0
            dp_volume += p.volume

            if p.price > mid_price:
                dp_above_mid += p.volume
            else:
                dp_below_mid += p.volume

        if dp_above_mid > dp_below_mid * 1.3:
            dp_sentiment = "ACCUMULATION"
        elif dp_below_mid > dp_above_mid * 1.3:
            dp_sentiment = "DISTRIBUTION"
        else:
            dp_sentiment = "NEUTRAL"

        # Flow alert metrics
        sweep_count = len([a for a in flow_alerts if a.has_sweep])
        alert_count = len(flow_alerts)

        # Put/call ratio
        put_call_ratio = 1.0
        if options_vol and options_vol.call_volume > 0:
            put_call_ratio = options_vol.put_volume / options_vol.call_volume

        # Overall conviction score (0-100)
        conviction = 50.0  # Start neutral

        if prem_direction == "BULLISH":
            conviction += 20
        elif prem_direction == "BEARISH":
            conviction -= 20

        if dp_sentiment == "ACCUMULATION":
            conviction += 15
        elif dp_sentiment == "DISTRIBUTION":
            conviction -= 15

        if sweep_count > 10:
            conviction += 15
        elif sweep_count > 5:
            conviction += 10

        if put_call_ratio > 1.0:
            conviction -= 5  # More puts = more downside
        elif put_call_ratio < 0.7:
            conviction += 5  # More calls = more upside

        conviction = max(0, min(100, conviction))

        # Summary
        summary_parts = []
        if prem_direction != "NEUTRAL":
            summary_parts.append(f"Net premium: {prem_direction}")
        if dp_sentiment != "NEUTRAL":
            summary_parts.append(f"Dark pool: {dp_sentiment} ({dp_volume:,} shares)")
        if sweep_count > 0:
            summary_parts.append(f"{sweep_count} sweeps detected")
        if put_call_ratio > 1.2:
            summary_parts.append(f"High put/call ratio: {put_call_ratio:.2f}")

        summary = " | ".join(summary_parts) if summary_parts else "Neutral institutional activity"

        return InstitutionalSentiment(
            ticker=ticker,
            timestamp=datetime.now(),
            net_premium=net_premium,
            net_premium_direction=prem_direction,
            dark_pool_volume=dp_volume,
            dark_pool_sentiment=dp_sentiment,
            net_delta=net_delta,
            sweep_count=sweep_count,
            alert_count=alert_count,
            put_call_ratio=put_call_ratio,
            overall_score=conviction,
            summary=summary
        )

    def get_dark_pool_levels(self, ticker: str) -> List[DarkPoolLevel]:
        """
        Cluster dark pool prints by price to find institutional accumulation/distribution levels.

        Args:
            ticker: Stock ticker symbol

        Returns:
            List of DarkPoolLevel objects sorted by price
        """
        cache_key = f"dark_pool_levels:{ticker}"
        cached = self.cache.get(cache_key)
        if cached is not None:
            return cached

        prints = self.get_dark_pool(ticker)
        if not prints:
            return []

        # Cluster by price (rounded to nearest 0.10)
        price_clusters = {}

        for p in prints:
            rounded_price = round(p.price, 1)

            if rounded_price not in price_clusters:
                price_clusters[rounded_price] = {
                    "volume": 0,
                    "count": 0,
                    "sizes": [],
                    "last_activity": p.executed_at
                }

            price_clusters[rounded_price]["volume"] += p.volume
            price_clusters[rounded_price]["count"] += 1
            price_clusters[rounded_price]["sizes"].append(p.size)
            price_clusters[rounded_price]["last_activity"] = max(
                price_clusters[rounded_price]["last_activity"],
                p.executed_at
            )

        levels = []
        for price, cluster in sorted(price_clusters.items()):
            avg_size = sum(cluster["sizes"]) / len(cluster["sizes"]) if cluster["sizes"] else 0

            # Determine if accumulation or distribution based on print positioning
            # (simplified: would benefit from comparing to VWAP in production)
            if cluster["volume"] > 1000000:
                bias = "ACCUMULATION"
            else:
                bias = "NEUTRAL"

            level = DarkPoolLevel(
                price=price,
                total_volume=cluster["volume"],
                trade_count=cluster["count"],
                direction_bias=bias,
                last_activity=cluster["last_activity"],
                avg_print_size=avg_size
            )
            levels.append(level)

        self.cache.set(cache_key, levels)
        return levels

    # ========================================================================
    # HELPER METHODS
    # ========================================================================

    @staticmethod
    def _parse_timestamp(ts_str: Optional[str]) -> Optional[datetime]:
        """Parse ISO timestamp to datetime."""
        if not ts_str:
            return None

        try:
            # Try ISO format with Z or timezone offset
            return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
        except (ValueError, AttributeError):
            try:
                # Try common formats
                return datetime.strptime(ts_str, "%Y-%m-%d %H:%M:%S")
            except (ValueError, TypeError):
                return None


# ============================================================================
# TESTING & DEMO
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("\n" + "="*80)
    print("UNUSUAL WHALES API CLIENT - LIVE ENGINE V2")
    print("="*80 + "\n")

    if not UW_API_KEY:
        print("ERROR: No UW_API_KEY found in config_v2.py")
        print("Set UW_API_KEY to your Unusual Whales API token to enable live data.\n")
        exit(1)

    client = UnusualWhalesClient()

    # Test ticker
    ticker = "SPY"

    print(f"Testing UnusualWhalesClient with ticker: {ticker}\n")
    print("-" * 80)

    # Test 1: Dark Pool
    print("\n[1] Dark Pool Prints (get_dark_pool)")
    dp = client.get_dark_pool(ticker)
    print(f"    Found {len(dp)} dark pool prints")
    if dp:
        sample = dp[0]
        print(f"    Sample: ${sample.price} x {sample.volume} shares | Premium: ${sample.premium:,.0f}")

    # Test 2: Flow Alerts
    print("\n[2] Flow Alerts (get_flow_alerts)")
    alerts = client.get_flow_alerts(ticker)
    print(f"    Found {len(alerts)} flow alerts")
    if alerts:
        sample = alerts[0]
        print(f"    Sample: {sample.option_type.upper()} ${sample.strike} | Premium: ${sample.total_premium:,.0f} | Sweep: {sample.has_sweep}")

    # Test 3: Net Premium Ticks
    print("\n[3] Net Premium Ticks (get_net_premium_ticks)")
    ticks = client.get_net_premium_ticks(ticker)
    print(f"    Found {len(ticks)} 5-min ticks")
    if ticks:
        sample = ticks[0]
        print(f"    Sample: Call Vol: {sample.call_volume:,} | Put Vol: {sample.put_volume:,} | Net Call Prem: ${sample.net_call_premium:,.0f}")

    # Test 4: Options Volume
    print("\n[4] Options Volume Summary (get_options_volume)")
    vol = client.get_options_volume(ticker)
    if vol:
        print(f"    Date: {vol.date}")
        print(f"    Call Volume: {vol.call_volume:,} | Put Volume: {vol.put_volume:,}")
        print(f"    Net Call Premium: ${vol.net_call_premium:,.0f}")

    # Test 5: Market Tide
    print("\n[5] Market Tide (get_market_tide)")
    tide = client.get_market_tide()
    print(f"    Found {len(tide)} market tide entries")
    if tide:
        sample = tide[0]
        print(f"    Sample: Net Call Prem: ${sample.net_call_premium:,.0f} | Net Vol: {sample.net_volume:,}")

    # Test 6: Recent Flow
    print("\n[6] Recent Flow Trades (get_recent_flow)")
    trades = client.get_recent_flow(ticker)
    print(f"    Found {len(trades)} flow trades")
    if trades:
        sample = trades[0]
        print(f"    Sample: {sample.option_type.upper()} | Price: ${sample.price:.2f} | Size: {sample.size} | Delta: {sample.delta:.3f}")

    # Test 7: Whale Signal Live
    print("\n[7] Whale Signal Live (get_whale_signal_live)")
    whale = client.get_whale_signal_live(ticker)
    if whale:
        print(f"    Direction: {whale.direction}")
        print(f"    Confidence: {whale.confidence:.1f}%")
        print(f"    Premium Flow: ${whale.premium_flow:,.0f}")
        print(f"    Dark Pool Flow: {whale.dark_pool_flow:,} shares")
        print(f"    Sweeps: {whale.sweep_count} | Blocks: {whale.block_count}")

    # Test 8: Institutional Sentiment
    print("\n[8] Institutional Sentiment (get_institutional_sentiment)")
    sentiment = client.get_institutional_sentiment(ticker)
    if sentiment:
        print(f"    Net Premium Direction: {sentiment.net_premium_direction}")
        print(f"    Dark Pool Sentiment: {sentiment.dark_pool_sentiment}")
        print(f"    Net Delta: {sentiment.net_delta:,.0f}")
        print(f"    Put/Call Ratio: {sentiment.put_call_ratio:.2f}")
        print(f"    Overall Score: {sentiment.overall_score:.1f}/100")
        print(f"    Summary: {sentiment.summary}")

    # Test 9: Dark Pool Levels
    print("\n[9] Dark Pool Levels (get_dark_pool_levels)")
    levels = client.get_dark_pool_levels(ticker)
    print(f"    Found {len(levels)} price clusters")
    if levels:
        for level in levels[:3]:
            print(f"    ${level.price:.2f}: {level.total_volume:,} vol | {level.trade_count} trades | {level.direction_bias}")

    print("\n" + "="*80)
    print("Testing complete!")
    print("="*80 + "\n")
