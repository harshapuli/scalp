"""
LIVE TRADING ENGINE — EXECUTION & POSITION MANAGEMENT
=======================================================
Alpaca API integration for:
  - Market data streaming (bars, quotes)
  - Options chain lookup & ATM strike selection
  - Order placement (options buy-to-open / sell-to-close)
  - Position tracking and management
  - Risk management (max positions, daily loss, sizing)
"""
import os
import json
import time
import logging
import requests
import math
from datetime import datetime, date, timedelta
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field, asdict

logger = logging.getLogger('execution')


# ============================================================
# DATA CLASSES
# ============================================================

@dataclass
class Trade:
    """Represents an active or completed trade."""
    trade_id: str
    strategy: str
    ticker: str
    direction: str           # 'CALL' or 'PUT'
    instrument: str          # '0DTE_ATM', 'Weekly_ATM', etc.
    entry_price: float       # underlying price at entry
    entry_time: str
    stop_pct: float
    target_pct: float
    hold_bars: int
    bars_held: int = 0
    status: str = 'OPEN'     # OPEN, CLOSED
    exit_price: float = 0.0  # underlying price at exit
    exit_time: str = ''
    exit_reason: str = ''    # TARGET, STOP, TIME, MANUAL
    pnl: float = 0.0
    order_id: str = ''
    close_order_id: str = ''
    whale_confirmed: bool = False
    # --- Options fields ---
    option_symbol: str = ''          # OCC symbol e.g. QQQ260413C00612000
    option_entry_premium: float = 0  # premium paid per share (contract = 100x)
    option_exit_premium: float = 0   # premium received per share
    contracts: int = 0               # number of option contracts
    strike: float = 0.0              # strike price

    @property
    def stop_price(self) -> float:
        """Stop based on UNDERLYING price."""
        if self.direction == 'CALL':
            return self.entry_price * (1 - self.stop_pct / 100)
        return self.entry_price * (1 + self.stop_pct / 100)

    @property
    def target_price(self) -> float:
        """Target based on UNDERLYING price."""
        if self.direction == 'CALL':
            return self.entry_price * (1 + self.target_pct / 100)
        return self.entry_price * (1 - self.target_pct / 100)


@dataclass
class DailyStats:
    """Track daily P&L and trade counts."""
    date: str = ''
    trades_taken: int = 0
    wins: int = 0
    losses: int = 0
    total_pnl: float = 0.0
    max_drawdown: float = 0.0
    peak_pnl: float = 0.0


# ============================================================
# ALPACA API CLIENT
# ============================================================

class AlpacaClient:
    """Alpaca API client for trading and data."""

    def __init__(self, api_key: str, api_secret: str, base_url: str, data_url: str):
        self.api_key = api_key
        self.api_secret = api_secret
        self.base_url = base_url.rstrip('/')
        self.data_url = data_url.rstrip('/')
        self.headers = {
            'APCA-API-KEY-ID': api_key,
            'APCA-API-SECRET-KEY': api_secret,
            'Content-Type': 'application/json',
        }
        self._session = requests.Session()
        self._session.headers.update(self.headers)

    # ── Account ──

    def get_account(self) -> dict:
        """Get account info (equity, buying power, etc.)."""
        r = self._session.get(f'{self.base_url}/v2/account')
        r.raise_for_status()
        return r.json()

    def get_equity(self) -> float:
        """Get current account equity."""
        acct = self.get_account()
        return float(acct.get('equity', 0))

    # ── Market Data ──

    def get_bars(self, ticker: str, timeframe: str = '1Min',
                 start: str = None, limit: int = 1000) -> list:
        """
        Get historical bars from Alpaca data API.
        timeframe: '1Min', '5Min', '15Min', '1Hour', '1Day', '1Week'
        """
        params = {'timeframe': timeframe, 'limit': limit, 'feed': 'iex'}
        if start:
            params['start'] = start
        r = self._session.get(
            f'{self.data_url}/v2/stocks/{ticker}/bars', params=params
        )
        r.raise_for_status()
        data = r.json()
        return data.get('bars', [])

    def get_latest_bar(self, ticker: str) -> dict:
        """Get the most recent bar."""
        r = self._session.get(
            f'{self.data_url}/v2/stocks/{ticker}/bars/latest',
            params={'feed': 'iex'}
        )
        r.raise_for_status()
        return r.json().get('bar', {})

    def get_latest_quote(self, ticker: str) -> dict:
        """Get latest bid/ask quote."""
        r = self._session.get(
            f'{self.data_url}/v2/stocks/{ticker}/quotes/latest',
            params={'feed': 'iex'}
        )
        r.raise_for_status()
        return r.json().get('quote', {})

    def get_snapshot(self, ticker: str) -> dict:
        """Get snapshot (latest trade, quote, bar)."""
        r = self._session.get(
            f'{self.data_url}/v2/stocks/{ticker}/snapshot',
            params={'feed': 'iex'}
        )
        r.raise_for_status()
        return r.json()

    # ── Orders ──

    def place_market_order(self, ticker: str, qty: int, side: str,
                           time_in_force: str = 'day') -> dict:
        """Place a market order. side: 'buy' or 'sell'."""
        payload = {
            'symbol': ticker,
            'qty': str(qty),
            'side': side,
            'type': 'market',
            'time_in_force': time_in_force,
        }
        r = self._session.post(f'{self.base_url}/v2/orders', json=payload)
        r.raise_for_status()
        return r.json()

    def place_bracket_order(self, ticker: str, qty: int, side: str,
                            take_profit: float, stop_loss: float,
                            time_in_force: str = 'day') -> dict:
        """
        Place a bracket order (entry + take profit + stop loss).
        This is the primary order type for our strategies.
        """
        payload = {
            'symbol': ticker,
            'qty': str(qty),
            'side': side,
            'type': 'market',
            'time_in_force': time_in_force,
            'order_class': 'bracket',
            'take_profit': {'limit_price': str(round(take_profit, 2))},
            'stop_loss': {'stop_price': str(round(stop_loss, 2))},
        }
        r = self._session.post(f'{self.base_url}/v2/orders', json=payload)
        r.raise_for_status()
        return r.json()

    def place_oto_order(self, ticker: str, qty: int, side: str,
                        limit_price: float, stop_price: float) -> dict:
        """One-triggers-other: limit entry → stop loss."""
        payload = {
            'symbol': ticker,
            'qty': str(qty),
            'side': side,
            'type': 'limit',
            'limit_price': str(round(limit_price, 2)),
            'time_in_force': 'day',
            'order_class': 'oto',
            'stop_loss': {'stop_price': str(round(stop_price, 2))},
        }
        r = self._session.post(f'{self.base_url}/v2/orders', json=payload)
        r.raise_for_status()
        return r.json()

    def cancel_order(self, order_id: str) -> bool:
        """Cancel an open order."""
        try:
            r = self._session.delete(f'{self.base_url}/v2/orders/{order_id}')
            return r.status_code == 204 or r.status_code == 200
        except:
            return False

    def get_order(self, order_id: str) -> dict:
        """Get order status."""
        r = self._session.get(f'{self.base_url}/v2/orders/{order_id}')
        r.raise_for_status()
        return r.json()

    def list_orders(self, status: str = 'open') -> list:
        """List orders by status."""
        r = self._session.get(
            f'{self.base_url}/v2/orders', params={'status': status}
        )
        r.raise_for_status()
        return r.json()

    # ── Positions ──

    def get_positions(self) -> list:
        """Get all open positions."""
        r = self._session.get(f'{self.base_url}/v2/positions')
        r.raise_for_status()
        return r.json()

    def get_position(self, ticker: str) -> Optional[dict]:
        """Get position for a specific ticker."""
        try:
            r = self._session.get(f'{self.base_url}/v2/positions/{ticker}')
            r.raise_for_status()
            return r.json()
        except:
            return None

    def close_position(self, ticker: str) -> dict:
        """Close entire position for a ticker."""
        r = self._session.delete(f'{self.base_url}/v2/positions/{ticker}')
        r.raise_for_status()
        return r.json()

    def close_all_positions(self) -> list:
        """Close all positions (emergency)."""
        r = self._session.delete(f'{self.base_url}/v2/positions')
        r.raise_for_status()
        return r.json()

    # ── Market Status ──

    def is_market_open(self) -> bool:
        """Check if market is currently open."""
        try:
            r = self._session.get(f'{self.base_url}/v2/clock')
            r.raise_for_status()
            return r.json().get('is_open', False)
        except:
            return False

    def get_clock(self) -> dict:
        """Get market clock (open/close times)."""
        r = self._session.get(f'{self.base_url}/v2/clock')
        r.raise_for_status()
        return r.json()


# ============================================================
# OPTIONS HELPER — CHAIN LOOKUP & STRIKE SELECTION
# ============================================================

class OptionsHelper:
    """
    Handles option chain lookup, ATM strike selection, and
    building OCC symbols for Alpaca options orders.
    """

    def __init__(self, api_key: str, api_secret: str, paper: bool = True):
        from alpaca.trading.client import TradingClient
        self._api_key = api_key
        self._api_secret = api_secret
        self.client = TradingClient(api_key, api_secret, paper=paper)
        self._chain_cache: Dict[str, dict] = {}  # ticker_date → contracts
        self._cache_time: Dict[str, float] = {}

    def get_atm_option(self, ticker: str, underlying_price: float,
                       direction: str, instrument: str = '0DTE_ATM') -> Optional[dict]:
        """
        Find the ATM option contract for the given ticker/direction.

        instrument types:
          '0DTE_ATM'     → today's expiry, ATM strike
          'Weekly_ATM'   → this Friday's expiry, ATM strike
          'Monthly_ATM'  → monthly expiry, ATM strike

        Returns: {symbol, strike, expiry, type} or None
        """
        try:
            expiry = self._get_expiry(instrument)
            opt_type = 'call' if direction == 'CALL' else 'put'

            # Check cache (refresh every 60s)
            cache_key = f"{ticker}_{expiry}_{opt_type}"
            now = time.time()
            if cache_key in self._chain_cache and now - self._cache_time.get(cache_key, 0) < 60:
                contracts = self._chain_cache[cache_key]
            else:
                contracts = self._fetch_chain(ticker, expiry, opt_type)
                self._chain_cache[cache_key] = contracts
                self._cache_time[cache_key] = now

            if not contracts:
                logger.warning(f"OPTIONS: No {opt_type} contracts for {ticker} exp={expiry}")
                return None

            # Find ATM: closest strike to underlying price
            best = min(contracts, key=lambda c: abs(c['strike'] - underlying_price))
            logger.info(f"OPTIONS: {ticker} ATM {opt_type.upper()} → {best['symbol']} "
                       f"strike={best['strike']} (underlying={underlying_price:.2f})")
            return best

        except Exception as e:
            logger.error(f"OPTIONS LOOKUP FAILED: {ticker} {direction} — {e}")
            return None

    def _get_expiry(self, instrument: str) -> date:
        """Determine expiry date from instrument type."""
        today = date.today()
        if '0DTE' in instrument:
            return today
        elif 'Weekly' in instrument:
            # Next Friday (or today if Friday)
            days_ahead = 4 - today.weekday()  # Friday = 4
            if days_ahead <= 0:
                days_ahead += 7
            return today + timedelta(days=days_ahead)
        elif 'Monthly' in instrument:
            # Third Friday of current/next month
            return self._third_friday(today)
        return today  # default to 0DTE

    def _third_friday(self, dt: date) -> date:
        """Find third Friday of the month."""
        first = dt.replace(day=1)
        # Find first Friday
        day_offset = (4 - first.weekday()) % 7
        first_friday = first + timedelta(days=day_offset)
        third_friday = first_friday + timedelta(weeks=2)
        if third_friday <= dt:
            # Move to next month
            if dt.month == 12:
                next_month = dt.replace(year=dt.year + 1, month=1, day=1)
            else:
                next_month = dt.replace(month=dt.month + 1, day=1)
            return self._third_friday(next_month)
        return third_friday

    def _fetch_chain(self, ticker: str, expiry: date, opt_type: str) -> list:
        """Fetch option chain from Alpaca."""
        from alpaca.trading.requests import GetOptionContractsRequest
        req = GetOptionContractsRequest(
            underlying_symbols=[ticker],
            expiration_date=expiry,
            status='active',
            type=opt_type,
        )
        result = self.client.get_option_contracts(req)
        contracts = result.option_contracts or []
        return [
            {
                'symbol': c.symbol,
                'strike': float(c.strike_price),
                'expiry': str(c.expiration_date),
                'type': opt_type,
            }
            for c in contracts
        ]

    def get_option_quote(self, option_symbol: str) -> Optional[float]:
        """
        Get current mid-price for an option contract.
        Uses Alpaca's latest option quote endpoint with auth.
        """
        try:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            from alpaca.data.requests import OptionLatestQuoteRequest
            data_client = OptionHistoricalDataClient(self._api_key, self._api_secret)
            req = OptionLatestQuoteRequest(symbol_or_symbols=[option_symbol])
            quotes = data_client.get_option_latest_quote(req)
            if option_symbol in quotes:
                q = quotes[option_symbol]
                bid = float(q.bid_price) if q.bid_price else 0
                ask = float(q.ask_price) if q.ask_price else 0
                if bid > 0 and ask > 0:
                    return round((bid + ask) / 2, 2)
                elif ask > 0:
                    return round(ask, 2)
            return None
        except Exception as e:
            logger.warning(f"OPTIONS QUOTE FAILED: {option_symbol} — {e}")
            return None


# ============================================================
# POSITION MANAGER
# ============================================================

class PositionManager:
    """
    Manages all open trades, risk limits, and position sizing.
    Works with the Alpaca client for real execution.
    """

    TRADES_FILE = 'trade_logs/open_trades.json'

    def __init__(self, client: AlpacaClient, risk_config: dict,
                 options_helper: Optional[OptionsHelper] = None):
        self.client = client
        self.risk = risk_config
        self.options = options_helper
        self.open_trades: Dict[str, Trade] = {}
        self.closed_trades: List[Trade] = []
        self.daily_stats = DailyStats(date=datetime.now().strftime('%Y-%m-%d'))
        self._trade_counter = 0
        self._ticker_cooldown: Dict[str, float] = {}  # ticker → timestamp of last loss
        self._load_persisted_trades()

    def _load_persisted_trades(self):
        """Load open trades from disk (survives restarts)."""
        import json, os
        if not os.path.exists(self.TRADES_FILE):
            return
        try:
            with open(self.TRADES_FILE) as f:
                data = json.load(f)
            for td in data.get('open', []):
                trade = Trade(
                    trade_id=td['trade_id'], strategy=td['strategy'],
                    ticker=td['ticker'], direction=td['direction'],
                    instrument=td.get('instrument', 'stock'),
                    entry_price=td['entry_price'], entry_time=td['entry_time'],
                    stop_pct=td.get('stop_pct', 1), target_pct=td.get('target_pct', 1),
                    hold_bars=td.get('hold_bars', 60),
                )
                trade.bars_held = td.get('bars_held', 0)
                trade.option_symbol = td.get('option_symbol', '')
                trade.option_entry_premium = td.get('option_entry_premium', 0)
                trade.contracts = td.get('contracts', 0)
                trade.strike = td.get('strike', 0)
                trade.order_id = td.get('order_id', '')
                self.open_trades[trade.trade_id] = trade
            self._trade_counter = data.get('counter', 0)
            stats = data.get('daily_stats', {})
            self.daily_stats.total_pnl = stats.get('total_pnl', 0)
            self.daily_stats.wins = stats.get('wins', 0)
            self.daily_stats.losses = stats.get('losses', 0)
            self.daily_stats.trades_taken = stats.get('trades_taken', 0)
            logger.info(f"RESTORED {len(self.open_trades)} open trades, PnL=${self.daily_stats.total_pnl:.2f}")
        except Exception as e:
            logger.error(f"Failed to load trades: {e}")

    def _persist_trades(self):
        """Save open trades + stats to disk."""
        import json, os
        os.makedirs(os.path.dirname(self.TRADES_FILE), exist_ok=True)
        data = {
            'open': [{
                'trade_id': t.trade_id, 'strategy': t.strategy,
                'ticker': t.ticker, 'direction': t.direction,
                'instrument': t.instrument, 'entry_price': t.entry_price,
                'entry_time': t.entry_time, 'stop_pct': t.stop_pct,
                'target_pct': t.target_pct, 'hold_bars': t.hold_bars,
                'bars_held': t.bars_held,
                'stop_price_calc': t.stop_price, 'target_price_calc': t.target_price,
                'option_symbol': t.option_symbol, 'option_entry_premium': t.option_entry_premium,
                'contracts': t.contracts, 'strike': t.strike, 'order_id': t.order_id,
            } for t in self.open_trades.values()],
            'counter': self._trade_counter,
            'daily_stats': {
                'total_pnl': self.daily_stats.total_pnl,
                'wins': self.daily_stats.wins,
                'losses': self.daily_stats.losses,
                'trades_taken': self.daily_stats.trades_taken,
            },
        }
        with open(self.TRADES_FILE, 'w') as f:
            json.dump(data, f, indent=2)


    # ── Risk Checks ──

    def can_open_trade(self, ticker: str, strategy: dict) -> Tuple[bool, str]:
        """Check all risk limits before opening a trade."""
        # Max positions
        if len(self.open_trades) >= self.risk['max_positions']:
            return False, f"Max positions reached ({self.risk['max_positions']})"

        # Max per ticker
        ticker_count = sum(1 for t in self.open_trades.values() if t.ticker == ticker)
        if ticker_count >= self.risk['max_per_ticker']:
            return False, f"Max positions for {ticker} ({self.risk['max_per_ticker']})"

        # Max daily trades
        if self.daily_stats.trades_taken >= self.risk['max_daily_trades']:
            return False, f"Max daily trades reached ({self.risk['max_daily_trades']})"

        # Daily loss limit
        equity = self.risk.get('account_size', 25000)
        max_loss = equity * self.risk['max_daily_loss_pct'] / 100
        if self.daily_stats.total_pnl < -max_loss:
            return False, f"Daily loss limit hit (${max_loss:.0f})"

        # Don't allow duplicate strategy+ticker+direction
        for t in self.open_trades.values():
            if t.strategy == strategy['name'] and t.ticker == ticker:
                return False, f"Already in {strategy['name']} on {ticker}"

        # Per-ticker cooldown after a loss (5 minutes)
        cooldown_until = self._ticker_cooldown.get(ticker, 0)
        if time.time() < cooldown_until:
            remaining = int(cooldown_until - time.time())
            return False, f"Cooldown on {ticker} ({remaining}s remaining after loss)"

        return True, "OK"

    # ── Position Sizing ──

    def calculate_position_size(self, ticker: str, entry_price: float,
                                 stop_pct: float, strategy: dict) -> int:
        """
        Calculate number of shares based on risk.
        Uses fixed % of account with WR scaling.
        """
        equity = self.risk.get('account_size', 25000)
        base_pct = self.risk['position_size_pct'] / 100

        # Scale by win rate if enabled
        if self.risk.get('scale_by_wr', False):
            wr = strategy.get('backtest_wr', 50)
            if wr >= 75:
                size_mult = 1.5   # High conviction
            elif wr >= 60:
                size_mult = 1.2
            elif wr >= 50:
                size_mult = 1.0
            else:
                size_mult = 0.7   # Lower conviction = smaller size
        else:
            size_mult = 1.0

        risk_amount = equity * base_pct * size_mult
        risk_per_share = entry_price * stop_pct / 100
        shares = int(risk_amount / max(risk_per_share, 0.01))

        # Ensure at least 1 share, max based on equity
        max_shares = int(equity * 0.25 / max(entry_price, 1))  # Max 25% in one trade
        return max(1, min(shares, max_shares))

    # ── Trade Execution ──

    def calculate_contracts(self, premium: float, strategy: dict) -> int:
        """
        Calculate number of option contracts based on risk budget.
        Risk amount = account_size * position_size_pct * wr_mult
        Contracts = risk_amount / (premium * 100)
        """
        equity = self.risk.get('account_size', 25000)
        base_pct = self.risk['position_size_pct'] / 100

        if self.risk.get('scale_by_wr', False):
            wr = strategy.get('backtest_wr', 50)
            if wr >= 75:
                size_mult = 1.5
            elif wr >= 60:
                size_mult = 1.2
            elif wr >= 50:
                size_mult = 1.0
            else:
                size_mult = 0.7
        else:
            size_mult = 1.0

        risk_amount = equity * base_pct * size_mult
        cost_per_contract = premium * 100  # 1 contract = 100 shares
        if cost_per_contract <= 0:
            return 1
        contracts = int(risk_amount / cost_per_contract)
        # Min 1, max based on equity (never more than 10% in one trade)
        max_contracts = int(equity * 0.10 / max(cost_per_contract, 1))
        return max(1, min(contracts, max(max_contracts, 1)))

    def open_trade(self, ticker: str, direction: str, entry_price: float,
                   strategy: dict, stop_pct: float, target_pct: float,
                   whale_confirmed: bool = False, paper: bool = True) -> Optional[Trade]:
        """
        Open a new OPTIONS trade:
          1. Look up ATM option contract
          2. Get current premium quote
          3. Buy-to-open via Alpaca
          4. Track underlying price for stop/target/time exits
        """
        can_trade, reason = self.can_open_trade(ticker, strategy)
        if not can_trade:
            logger.info(f"SKIP {strategy['name']} {ticker} {direction}: {reason}")
            return None

        # ── Step 1: Find the ATM option ──
        if not self.options:
            logger.error(f"OPTIONS HELPER NOT INITIALIZED — cannot trade options")
            return None

        instrument = strategy.get('instrument', '0DTE_ATM')
        opt_info = self.options.get_atm_option(ticker, entry_price, direction, instrument)
        if not opt_info:
            logger.warning(f"NO OPTION FOUND: {ticker} {direction} {instrument}")
            return None

        option_symbol = opt_info['symbol']
        strike = opt_info['strike']

        # ── Step 2: Get premium quote ──
        premium = self.options.get_option_quote(option_symbol)
        if premium is None or premium <= 0:
            # Fallback: estimate premium from intrinsic + rough time value
            # ATM 0DTE → mostly time value, rough $1-3 for QQQ/SPY, $0.50-2 for stocks
            premium = max(round(entry_price * 0.003, 2), 0.10)  # ~0.3% of underlying
            logger.warning(f"OPTIONS QUOTE UNAVAILABLE for {option_symbol}, estimating premium=${premium}")

        # ── Step 3: Size the trade ──
        contracts = self.calculate_contracts(premium, strategy)

        # Calculate underlying stop/target
        if direction == 'CALL':
            stop_ul = round(entry_price * (1 - stop_pct / 100), 2)
            target_ul = round(entry_price * (1 + target_pct / 100), 2)
        else:
            stop_ul = round(entry_price * (1 + stop_pct / 100), 2)
            target_ul = round(entry_price * (1 - target_pct / 100), 2)

        # Create trade record
        self._trade_counter += 1
        trade_id = f"{strategy['name']}_{ticker}_{self._trade_counter}"

        trade = Trade(
            trade_id=trade_id,
            strategy=strategy['name'],
            ticker=ticker,
            direction=direction,
            instrument=instrument,
            entry_price=entry_price,
            entry_time=datetime.now().isoformat(),
            stop_pct=stop_pct,
            target_pct=target_pct,
            hold_bars=strategy['hold_bars'],
            whale_confirmed=whale_confirmed,
            option_symbol=option_symbol,
            option_entry_premium=premium,
            contracts=contracts,
            strike=strike,
        )

        # ── Step 4: Submit order to Alpaca ──
        try:
            payload = {
                'symbol': option_symbol,
                'qty': str(contracts),
                'side': 'buy',
                'type': 'market',
                'time_in_force': 'day',
            }
            r = self.client._session.post(f'{self.client.base_url}/v2/orders', json=payload)
            r.raise_for_status()
            order_data = r.json()
            trade.order_id = order_data.get('id', '')
            # Try to get fill price
            fill_price = order_data.get('filled_avg_price')
            if fill_price:
                trade.option_entry_premium = float(fill_price)
                premium = float(fill_price)

            total_cost = premium * contracts * 100
            logger.info(
                f"OPTIONS ORDER: {trade_id} | BUY {contracts}x {option_symbol} "
                f"@ ${premium:.2f} (${total_cost:.0f} total) | "
                f"UL={entry_price:.2f} SL={stop_ul:.2f} TP={target_ul:.2f} | "
                f"Order: {trade.order_id}"
            )
        except Exception as e:
            logger.error(f"OPTIONS ORDER FAILED: {trade_id} | {option_symbol} | {e}")
            return None

        self.open_trades[trade_id] = trade
        self.daily_stats.trades_taken += 1
        self._persist_trades()
        return trade

    # ── Trade Monitoring ──

    def check_exits(self, current_prices: Dict[str, float]) -> List[Trade]:
        """
        Check all open trades for exit conditions based on UNDERLYING price.
        When exit triggers, sell-to-close the option position via Alpaca.
        Returns list of trades that were closed.
        """
        closed = []
        to_close = []

        for trade_id, trade in self.open_trades.items():
            price = current_prices.get(trade.ticker)
            if price is None:
                continue

            trade.bars_held += 1
            exit_reason = None

            if trade.direction == 'CALL':
                if price <= trade.stop_price:
                    exit_reason = 'STOP'
                elif price >= trade.target_price:
                    exit_reason = 'TARGET'
                elif trade.bars_held >= trade.hold_bars:
                    exit_reason = 'TIME'
            else:  # PUT
                if price >= trade.stop_price:
                    exit_reason = 'STOP'
                elif price <= trade.target_price:
                    exit_reason = 'TARGET'
                elif trade.bars_held >= trade.hold_bars:
                    exit_reason = 'TIME'

            if exit_reason:
                trade.exit_price = price
                trade.exit_time = datetime.now().isoformat()
                trade.exit_reason = exit_reason
                trade.status = 'CLOSED'

                # ── Close the option position via Alpaca ──
                exit_premium = None
                if trade.option_symbol and trade.contracts > 0:
                    try:
                        payload = {
                            'symbol': trade.option_symbol,
                            'qty': str(trade.contracts),
                            'side': 'sell',
                            'type': 'market',
                            'time_in_force': 'day',
                        }
                        r = self.client._session.post(
                            f'{self.client.base_url}/v2/orders', json=payload
                        )
                        r.raise_for_status()
                        close_data = r.json()
                        trade.close_order_id = close_data.get('id', '')
                        fill = close_data.get('filled_avg_price')
                        if fill:
                            exit_premium = float(fill)
                        logger.info(f"OPTIONS CLOSE: {trade_id} | SELL {trade.contracts}x "
                                   f"{trade.option_symbol} | Order: {trade.close_order_id}")
                    except Exception as e:
                        logger.error(f"OPTIONS CLOSE FAILED: {trade_id} | {trade.option_symbol} | {e}")
                        # Try to close via position endpoint as fallback
                        try:
                            self.client._session.delete(
                                f'{self.client.base_url}/v2/positions/{trade.option_symbol}'
                            )
                        except:
                            pass

                # Get exit premium from quote if not from fill
                if exit_premium is None and trade.option_symbol and self.options:
                    exit_premium = self.options.get_option_quote(trade.option_symbol)
                trade.option_exit_premium = exit_premium or 0

                # Calculate PnL
                trade.pnl = self._calculate_pnl(trade)

                # Update daily stats
                self.daily_stats.total_pnl += trade.pnl
                if trade.pnl > 0:
                    self.daily_stats.wins += 1
                else:
                    self.daily_stats.losses += 1
                    # Set cooldown on this ticker (5 minutes)
                    self._ticker_cooldown[trade.ticker] = time.time() + 300

                to_close.append(trade_id)
                closed.append(trade)

                logger.info(
                    f"EXIT {trade_id}: {exit_reason} | "
                    f"UL Entry={trade.entry_price:.2f} Exit={price:.2f} | "
                    f"Option: {trade.contracts}x @ ${trade.option_entry_premium:.2f}→"
                    f"${trade.option_exit_premium:.2f} | "
                    f"PnL=${trade.pnl:.2f} | Held {trade.bars_held} bars"
                )

        for tid in to_close:
            t = self.open_trades.pop(tid)
            self.closed_trades.append(t)

        if to_close:
            self._persist_trades()

        return closed

    def force_close_all(self, current_prices: Dict[str, float], reason: str = 'MANUAL'):
        """Emergency close all positions (options + stock)."""
        for trade_id, trade in list(self.open_trades.items()):
            price = current_prices.get(trade.ticker, trade.entry_price)
            trade.exit_price = price
            trade.exit_time = datetime.now().isoformat()
            trade.exit_reason = reason
            trade.status = 'CLOSED'

            # Close option position
            if trade.option_symbol and trade.contracts > 0:
                try:
                    self.client._session.delete(
                        f'{self.client.base_url}/v2/positions/{trade.option_symbol}'
                    )
                except:
                    pass

            trade.pnl = self._calculate_pnl(trade)
            self.daily_stats.total_pnl += trade.pnl
            self.closed_trades.append(trade)
            logger.info(f"FORCE CLOSE {trade_id}: {reason} | PnL=${trade.pnl:.2f}")

        self.open_trades.clear()

        # Also close all via Alpaca as safety net
        try:
            self.client.close_all_positions()
        except:
            pass

    # ── PnL Calculation ──

    def _calculate_pnl(self, trade: Trade) -> float:
        """
        Calculate trade PnL from option premiums.
        PnL = (exit_premium - entry_premium) * contracts * 100
        If exit premium unknown, estimate from underlying move + delta.
        """
        if trade.option_entry_premium > 0 and trade.option_exit_premium > 0:
            # Real premium-based PnL
            raw_pnl = (trade.option_exit_premium - trade.option_entry_premium) * trade.contracts * 100
        elif trade.option_entry_premium > 0 and trade.contracts > 0:
            # Estimate exit premium from underlying move
            ul_move = trade.exit_price - trade.entry_price
            if trade.direction == 'PUT':
                ul_move = -ul_move  # PUT gains when underlying drops
            # ATM delta ~0.50, theta decay ~10-20% for 0DTE over hold period
            delta = 0.50
            # Rough theta: lose ~15% of premium over 14 bars (14min) on 0DTE
            bars_frac = min(trade.bars_held / max(trade.hold_bars, 1), 1.0)
            theta_decay = trade.option_entry_premium * 0.15 * bars_frac
            est_exit = max(trade.option_entry_premium + (ul_move * delta) - theta_decay, 0.01)
            trade.option_exit_premium = round(est_exit, 2)
            raw_pnl = (est_exit - trade.option_entry_premium) * trade.contracts * 100
        else:
            # Fallback: basic underlying-based calc
            qty = self.calculate_position_size(trade.ticker, trade.entry_price,
                                                trade.stop_pct, {'category': 'scalp', 'name': trade.strategy})
            if trade.direction == 'CALL':
                raw_pnl = (trade.exit_price - trade.entry_price) * qty
            else:
                raw_pnl = (trade.entry_price - trade.exit_price) * qty
        return round(raw_pnl, 2)

    # ── Reporting ──

    def get_summary(self) -> dict:
        """Get current session summary."""
        return {
            'open_positions': len(self.open_trades),
            'trades_today': self.daily_stats.trades_taken,
            'wins': self.daily_stats.wins,
            'losses': self.daily_stats.losses,
            'win_rate': round(self.daily_stats.wins / max(self.daily_stats.trades_taken, 1) * 100, 1),
            'daily_pnl': round(self.daily_stats.total_pnl, 2),
            'open_trades': {tid: asdict(t) for tid, t in self.open_trades.items()},
        }

    def save_trades(self, filepath: str = 'trades.json'):
        """Save all trades to JSON."""
        all_trades = [asdict(t) for t in self.closed_trades]
        all_trades += [asdict(t) for t in self.open_trades.values()]
        with open(filepath, 'w') as f:
            json.dump({
                'trades': all_trades,
                'daily_stats': asdict(self.daily_stats),
                'summary': self.get_summary(),
            }, f, indent=2)

    def reset_daily(self):
        """Reset daily stats (call at market open)."""
        self.daily_stats = DailyStats(date=datetime.now().strftime('%Y-%m-%d'))
