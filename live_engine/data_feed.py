"""
LIVE TRADING ENGINE — DATA FEED (WebSocket + REST)
=====================================================
Two modes:
  1. WebSocket streaming (primary) — real-time bar/trade/quote updates
  2. REST polling (fallback) — if WebSocket disconnects

Alpaca WebSocket streams:
  - wss://stream.data.alpaca.markets/v2/iex   (free tier)
  - wss://stream.data.alpaca.markets/v2/sip   (paid, all exchanges)

Handles:
  - WebSocket connection with auto-reconnect
  - Real-time 1min bar building from trade/quote streams
  - Multi-timeframe aggregation (3M, 5M, 15M, 1H, 4H, daily, weekly)
  - Rolling windows with indicator computation
  - Warm-up from REST API on startup
  - Callbacks to engine when new bars complete
"""
import json
import threading
import time
import logging
import websocket
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
from typing import Dict, Optional, Callable, List
from collections import defaultdict

from indicators import compute_all_indicators, aggregate_bars, incremental_update

logger = logging.getLogger('data_feed')


# ============================================================
# ALPACA WEBSOCKET STREAM
# ============================================================

class AlpacaWebSocket:
    """
    WebSocket connection to Alpaca data stream.
    Subscribes to bars and trades for specified tickers.
    Auto-reconnects on disconnect.
    """

    # Free tier: IEX. Paid: SIP
    WS_URL_IEX = "wss://stream.data.alpaca.markets/v2/iex"
    WS_URL_SIP = "wss://stream.data.alpaca.markets/v2/sip"

    def __init__(self, api_key: str, api_secret: str, tickers: list,
                 on_bar: Callable = None, on_trade: Callable = None,
                 on_quote: Callable = None, use_sip: bool = False):
        self.api_key = api_key
        self.api_secret = api_secret
        self.tickers = tickers
        self.on_bar = on_bar
        self.on_trade = on_trade
        self.on_quote = on_quote
        self.ws_url = self.WS_URL_SIP if use_sip else self.WS_URL_IEX

        self.ws = None
        self._thread = None
        self._connected = False
        self._reconnect_delay = 1
        self._max_reconnect_delay = 60
        self._should_run = True

    def start(self):
        """Start WebSocket connection in background thread."""
        self._should_run = True
        self._thread = threading.Thread(target=self._run_forever, daemon=True)
        self._thread.start()
        logger.info(f"WebSocket thread started → {self.ws_url}")

    def stop(self):
        """Stop WebSocket connection."""
        self._should_run = False
        if self.ws:
            self.ws.close()
        logger.info("WebSocket stopped")

    @property
    def connected(self):
        return self._connected

    def _run_forever(self):
        """Main loop: connect, listen, reconnect on failure."""
        while self._should_run:
            try:
                self.ws = websocket.WebSocketApp(
                    self.ws_url,
                    on_open=self._on_open,
                    on_message=self._on_message,
                    on_error=self._on_error,
                    on_close=self._on_close,
                )
                self.ws.run_forever(ping_interval=30, ping_timeout=10)
            except Exception as e:
                logger.error(f"WebSocket error: {e}")

            if not self._should_run:
                break

            # Reconnect with exponential backoff
            logger.info(f"Reconnecting in {self._reconnect_delay}s...")
            time.sleep(self._reconnect_delay)
            self._reconnect_delay = min(self._reconnect_delay * 2, self._max_reconnect_delay)

    def _on_open(self, ws):
        """Authenticate and subscribe on connect."""
        logger.info("WebSocket connected, authenticating...")

        # Authenticate
        auth_msg = {
            "action": "auth",
            "key": self.api_key,
            "secret": self.api_secret,
        }
        ws.send(json.dumps(auth_msg))

    def _on_message(self, ws, message):
        """Handle incoming messages."""
        try:
            data = json.loads(message)
        except:
            return

        if not isinstance(data, list):
            data = [data]

        for msg in data:
            msg_type = msg.get('T', '')

            # Auth response
            if msg_type == 'success':
                action = msg.get('msg', '')
                if action == 'authenticated':
                    logger.info("WebSocket authenticated, subscribing...")
                    self._connected = True
                    self._reconnect_delay = 1  # Reset backoff
                    self._subscribe()
                elif action == 'connected':
                    pass  # Initial connection ack

            # Error
            elif msg_type == 'error':
                logger.error(f"WebSocket error: {msg}")

            # Subscription confirmation
            elif msg_type == 'subscription':
                bars = msg.get('bars', [])
                trades = msg.get('trades', [])
                logger.info(f"Subscribed: bars={bars}, trades={trades}")

            # Bar update (1min bars)
            elif msg_type == 'b':
                logger.info(f"WS BAR: {msg.get('S')} @ {msg.get('c')} vol={msg.get('v')}")
                if self.on_bar:
                    self.on_bar(msg)

            # Trade update
            elif msg_type == 't':
                if self.on_trade:
                    self.on_trade(msg)

            # Quote update
            elif msg_type == 'q':
                if self.on_quote:
                    self.on_quote(msg)

    def _on_error(self, ws, error):
        logger.error(f"WebSocket error: {error}")
        self._connected = False

    def _on_close(self, ws, close_status, close_msg):
        logger.info(f"WebSocket closed: {close_status} {close_msg}")
        self._connected = False

    def _subscribe(self):
        """Subscribe to bars and trades for all tickers."""
        sub_msg = {
            "action": "subscribe",
            "bars": self.tickers,
            "trades": self.tickers,
        }
        self.ws.send(json.dumps(sub_msg))


# ============================================================
# LIVE DATA FEED — WebSocket + REST
# ============================================================

class LiveDataFeed:
    """
    Manages live bar data for all tickers and timeframes.
    Primary: WebSocket streaming for real-time 1min bars.
    Fallback: REST polling when WebSocket is down.
    """

    ALPACA_TF = {
        '1min': '1Min', '3min': '3Min', '5min': '5Min', '15min': '15Min',
        '1hr': '1Hour', '4hr': '4Hour', 'daily': '1Day', 'weekly': '1Week',
    }

    WARMUP_BARS = {
        '1min': 500, '3min': 300, '5min': 200, '15min': 100,
        '1hr': 100, '4hr': 60, 'daily': 60, 'weekly': 30,
    }

    MAX_BARS = {
        '1min': 2000, '3min': 1000, '5min': 1000, '15min': 500,
        '1hr': 300, '4hr': 200, 'daily': 200, 'weekly': 100,
    }

    # How often to re-aggregate higher TFs (seconds)
    AGG_INTERVALS = {
        '3min': 60, '5min': 60, '15min': 300,
        '1hr': 3600, '4hr': 14400, 'daily': 86400, 'weekly': 604800,
    }

    def __init__(self, client, tickers: list, needed_timeframes: set,
                 use_websocket: bool = True, use_sip: bool = False):
        self.client = client
        self.tickers = tickers
        self.needed_tfs = needed_timeframes

        # Bar storage: {ticker: {timeframe: DataFrame}}
        self.bars: Dict[str, Dict[str, pd.DataFrame]] = defaultdict(dict)
        self.last_update: Dict[str, Dict[str, datetime]] = defaultdict(dict)
        self.htf_trend: Dict[str, Optional[pd.DataFrame]] = {}

        # Current prices from stream
        self._current_prices: Dict[str, float] = {}
        self._price_lock = threading.Lock()

        # Bar callbacks (engine registers to get notified of new bars)
        self._bar_callbacks: List[Callable] = []

        # Last aggregation time per TF
        self._last_agg: Dict[str, float] = {}

        # WebSocket
        self.use_websocket = use_websocket
        self.ws_client = None

        if use_websocket:
            from config import ALPACA_API_KEY, ALPACA_API_SECRET
            self.ws_client = AlpacaWebSocket(
                api_key=ALPACA_API_KEY,
                api_secret=ALPACA_API_SECRET,
                tickers=tickers,
                on_bar=self._handle_ws_bar,
                on_trade=self._handle_ws_trade,
                use_sip=use_sip,
            )

    def register_bar_callback(self, callback: Callable):
        """Register a callback that fires when a new 1min bar completes."""
        self._bar_callbacks.append(callback)

    # ── WebSocket Handlers ──

    def _handle_ws_bar(self, msg):
        """
        Handle incoming 1min bar from WebSocket.
        msg format: {'T': 'b', 'S': 'SPY', 't': '2024-...', 'o': 500.1, 'h': 500.5, ...}
        """
        ticker = msg.get('S', '')
        if ticker not in self.tickers:
            return

        try:
            new_bar = {
                'Date': pd.to_datetime(msg.get('t'), utc=True).tz_localize(None),
                'Open': float(msg.get('o', 0)),
                'High': float(msg.get('h', 0)),
                'Low': float(msg.get('l', 0)),
                'Close': float(msg.get('c', 0)),
                'Volume': int(msg.get('v', 0)),
                'NumTrades': int(msg.get('n', 1)),
            }

            # Update current price
            with self._price_lock:
                self._current_prices[ticker] = new_bar['Close']

            # Append to 1min bars
            new_row = pd.DataFrame([new_bar])
            if '1min' in self.bars[ticker] and len(self.bars[ticker]['1min']) > 30:
                df = pd.concat([self.bars[ticker]['1min'], new_row], ignore_index=True)
                # Ensure Date column stays datetime after concat
                if 'Date' in df.columns:
                    df['Date'] = pd.to_datetime(df['Date'])
                max_bars = self.MAX_BARS['1min']
                if len(df) > max_bars:
                    df = df.tail(max_bars).reset_index(drop=True)
                    # After trim, need full recompute (indices shifted)
                    df = compute_all_indicators(df, include_whale=True)
                else:
                    # Fast path: only update indicators for the new bar
                    df = incremental_update(df, include_whale=True)
                self.bars[ticker]['1min'] = df
            elif '1min' in self.bars[ticker]:
                df = pd.concat([self.bars[ticker]['1min'], new_row], ignore_index=True)
                self.bars[ticker]['1min'] = df
            else:
                self.bars[ticker]['1min'] = new_row

            self.last_update[ticker]['1min'] = datetime.now()

            # Re-aggregate higher timeframes if enough time has passed
            now = time.time()
            for tf in self.needed_tfs:
                if tf == '1min':
                    continue
                agg_interval = self.AGG_INTERVALS.get(tf, 300)
                last_agg = self._last_agg.get(f"{ticker}_{tf}", 0)
                if now - last_agg >= agg_interval:
                    self._reaggregate(ticker, tf)
                    self._last_agg[f"{ticker}_{tf}"] = now

            # Update HTF trend
            self._update_htf_trend(ticker)

            # Notify callbacks
            for cb in self._bar_callbacks:
                try:
                    cb(ticker, new_bar)
                except Exception as e:
                    logger.error(f"Bar callback error: {e}")

        except Exception as e:
            import traceback
            logger.error(f"Error handling WS bar for {ticker}: {e}\n{traceback.format_exc()}")

    def _handle_ws_trade(self, msg):
        """Handle incoming trade from WebSocket. Updates current price."""
        ticker = msg.get('S', '')
        price = msg.get('p')
        if ticker in self.tickers and price:
            with self._price_lock:
                self._current_prices[ticker] = float(price)

    def _reaggregate(self, ticker: str, tf: str):
        """Re-aggregate from 1min to higher timeframe."""
        if '1min' not in self.bars[ticker]:
            return
        df_1min = self.bars[ticker]['1min']
        if len(df_1min) < 30:
            return

        try:
            agg = aggregate_bars(df_1min, tf)
            if len(agg) > 20:
                agg = compute_all_indicators(agg, include_whale=True)
                self.bars[ticker][tf] = agg
                self.last_update[ticker][tf] = datetime.now()
        except Exception as e:
            logger.error(f"Aggregation failed {ticker} {tf}: {e}")

    # ── Warm-up: Load Historical Bars (REST) ──

    def warmup(self):
        """Load historical bars via REST for all tickers. Call once at startup."""
        logger.info("Starting data feed warm-up (REST)...")

        for ticker in self.tickers:
            logger.info(f"  Warming up {ticker}...")

            # Load 1min bars
            self._load_historical(ticker, '1min')

            # Aggregate or load higher TFs
            for tf in self.needed_tfs:
                if tf == '1min':
                    continue
                if tf in ('3min', '5min', '15min', '4hr'):
                    # Aggregate from 1min for intraday
                    if '1min' in self.bars[ticker] and len(self.bars[ticker]['1min']) > 50:
                        self._reaggregate(ticker, tf)
                    else:
                        self._load_historical(ticker, tf)
                else:
                    self._load_historical(ticker, tf)

            self._update_htf_trend(ticker)

        logger.info("Warm-up complete.")

        # Start WebSocket stream
        if self.use_websocket and self.ws_client:
            self.ws_client.start()
            # Wait for connection
            for _ in range(50):  # 5 seconds max
                if self.ws_client.connected:
                    logger.info("WebSocket stream connected and running")
                    break
                time.sleep(0.1)
            else:
                logger.warning("WebSocket connection timed out, falling back to REST polling")

    def _load_historical(self, ticker: str, tf: str):
        """Load historical bars from Alpaca REST API."""
        alpaca_tf = self.ALPACA_TF.get(tf, '1Min')
        warmup_n = self.WARMUP_BARS.get(tf, 200)

        try:
            if tf == '1min':
                start = (datetime.now() - timedelta(days=3)).isoformat() + 'Z'
            elif tf in ('3min', '5min', '15min'):
                start = (datetime.now() - timedelta(days=7)).isoformat() + 'Z'
            elif tf in ('1hr', '4hr'):
                start = (datetime.now() - timedelta(days=30)).isoformat() + 'Z'
            elif tf == 'daily':
                start = (datetime.now() - timedelta(days=120)).isoformat() + 'Z'
            else:
                start = (datetime.now() - timedelta(days=365)).isoformat() + 'Z'

            raw_bars = self.client.get_bars(
                ticker, timeframe=alpaca_tf, start=start, limit=warmup_n
            )

            if not raw_bars:
                logger.warning(f"No bars returned for {ticker} {tf}")
                return

            df = pd.DataFrame(raw_bars)
            df = df.rename(columns={
                't': 'Date', 'o': 'Open', 'h': 'High', 'l': 'Low',
                'c': 'Close', 'v': 'Volume', 'n': 'NumTrades',
            })
            if 'Date' in df.columns:
                df['Date'] = pd.to_datetime(df['Date'], utc=True)
                if df['Date'].dt.tz is not None:
                    df['Date'] = df['Date'].dt.tz_localize(None)

            df = compute_all_indicators(df, include_whale=True)
            self.bars[ticker][tf] = df
            self.last_update[ticker][tf] = datetime.now()

            # Seed current price
            if tf == '1min' and len(df) > 0:
                with self._price_lock:
                    self._current_prices[ticker] = float(df['Close'].iloc[-1])

            logger.info(f"    {tf}: {len(df)} bars loaded")

        except Exception as e:
            logger.error(f"Failed to load {ticker} {tf}: {e}")

    # ── REST Polling Fallback ──

    def update(self, ticker: str):
        """
        REST fallback: fetch latest bar if WebSocket is down.
        Only used when WebSocket is not connected.
        """
        if self.use_websocket and self.ws_client and self.ws_client.connected:
            return  # WebSocket is handling updates

        try:
            latest = self.client.get_latest_bar(ticker)
            if not latest:
                return

            raw_ts = pd.to_datetime(latest.get('t'), utc=True)
            if raw_ts.tzinfo is not None:
                raw_ts = raw_ts.tz_localize(None)
            new_row = pd.DataFrame([{
                'Date': raw_ts,
                'Open': latest.get('o'),
                'High': latest.get('h'),
                'Low': latest.get('l'),
                'Close': latest.get('c'),
                'Volume': latest.get('v', 0),
                'NumTrades': latest.get('n', 1),
            }])

            with self._price_lock:
                self._current_prices[ticker] = float(latest.get('c', 0))

            if '1min' in self.bars[ticker]:
                df = pd.concat([self.bars[ticker]['1min'], new_row], ignore_index=True)
                max_bars = self.MAX_BARS['1min']
                if len(df) > max_bars:
                    df = df.tail(max_bars).reset_index(drop=True)
                df = compute_all_indicators(df, include_whale=True)
                self.bars[ticker]['1min'] = df

            # Re-aggregate
            for tf in self.needed_tfs:
                if tf != '1min':
                    self._reaggregate(ticker, tf)

            self._update_htf_trend(ticker)
            self.last_update[ticker]['1min'] = datetime.now()

        except Exception as e:
            logger.error(f"REST update failed for {ticker}: {e}")

    def update_all(self):
        """Update all tickers (REST fallback only — WebSocket handles itself)."""
        if self.use_websocket and self.ws_client and self.ws_client.connected:
            return  # WebSocket is live, no polling needed
        for ticker in self.tickers:
            self.update(ticker)

    # ── HTF Trend ──

    def _update_htf_trend(self, ticker: str):
        """Update 1hr trend reference for V2 HTF alignment."""
        for tf in ('1hr', '15min'):
            if tf in self.bars[ticker]:
                htf_df = self.bars[ticker][tf]
                if 'Trend_Dir' in htf_df.columns and 'Date' in htf_df.columns:
                    ref = htf_df[['Date', 'Trend_Dir']].copy()
                    ref.set_index('Date', inplace=True)
                    self.htf_trend[ticker] = ref
                    return
        self.htf_trend[ticker] = None

    # ── Getters ──

    def get_bars(self, ticker: str, tf: str) -> Optional[pd.DataFrame]:
        """Get current bars for a ticker/timeframe."""
        return self.bars.get(ticker, {}).get(tf)

    def get_htf_trend(self, ticker: str) -> Optional[pd.DataFrame]:
        """Get HTF trend reference."""
        return self.htf_trend.get(ticker)

    def get_current_price(self, ticker: str) -> Optional[float]:
        """Get latest price (from stream or last bar)."""
        with self._price_lock:
            p = self._current_prices.get(ticker)
        if p:
            return p
        # Fallback to last 1min close
        if '1min' in self.bars.get(ticker, {}):
            df = self.bars[ticker]['1min']
            if len(df) > 0:
                return float(df['Close'].iloc[-1])
        return None

    def get_current_prices(self) -> Dict[str, float]:
        """Get current prices for all tickers."""
        prices = {}
        for ticker in self.tickers:
            p = self.get_current_price(ticker)
            if p:
                prices[ticker] = p
        return prices

    def is_streaming(self) -> bool:
        """Check if WebSocket is connected and streaming."""
        return self.ws_client is not None and self.ws_client.connected

    def shutdown(self):
        """Clean shutdown of WebSocket."""
        if self.ws_client:
            self.ws_client.stop()
