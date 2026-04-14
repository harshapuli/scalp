"""
LIVE TRADING ENGINE — EXECUTION & POSITION MANAGEMENT
=======================================================
Alpaca API integration for:
  - Market data streaming (bars, quotes)
  - Order placement (market, limit, bracket)
  - Position tracking and management
  - Risk management (max positions, daily loss, sizing)
"""
import os
import json
import time
import logging
import requests
from datetime import datetime, timedelta
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
    entry_price: float
    entry_time: str
    stop_pct: float
    target_pct: float
    hold_bars: int
    bars_held: int = 0
    status: str = 'OPEN'     # OPEN, CLOSED
    exit_price: float = 0.0
    exit_time: str = ''
    exit_reason: str = ''    # TARGET, STOP, TIME, MANUAL
    pnl: float = 0.0
    order_id: str = ''
    whale_confirmed: bool = False
    occ_symbol: str = ''

    @property
    def stop_price(self) -> float:
        if self.direction == 'CALL':
            return self.entry_price * (1 - self.stop_pct / 100)
        return self.entry_price * (1 + self.stop_pct / 100)

    @property
    def target_price(self) -> float:
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

    # ── Options Data ──

    def get_option_contracts(self, underlying: str, contract_type: str = None) -> list:
        """Fetch active OCC contracts for an underlying ticker from Alpaca."""
        params = {'underlying_symbols': underlying, 'status': 'active'}
        if contract_type:
            params['type'] = contract_type.lower() # 'call' or 'put'
        r = self._session.get(f'{self.base_url}/v2/options/contracts', params=params)
        r.raise_for_status()
        return r.json().get('option_contracts', [])

    def get_option_snapshot(self, underlying: str, occ_symbol: str) -> dict:
        """Fetch the physical bid/ask/trade limit quote of the exact option contract."""
        r = self._session.get(f'{self.data_url}/v1beta1/options/snapshots/{underlying}', params={'feed': 'indicative'})
        try:
            r.raise_for_status()
            data = r.json().get('snapshots', {})
            return data.get(occ_symbol, {})
        except:
            return {}

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
# POSITION MANAGER
# ============================================================

class PositionManager:
    """
    Manages all open trades, risk limits, and position sizing.
    Works with the Alpaca client for real execution.
    """

    def __init__(self, client: AlpacaClient, risk_config: dict):
        self.client = client
        self.risk = risk_config
        self.open_trades: Dict[str, Trade] = {}
        self.closed_trades: List[Trade] = []
        self.daily_stats = DailyStats(date=datetime.now().strftime('%Y-%m-%d'))
        self._trade_counter = 0

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

        return True, "OK"

    # ── Options Logic ──

    def _find_target_contract(self, ticker: str, direction: str, entry_price: float, instrument: str) -> Optional[str]:
        try:
            contracts = self.client.get_option_contracts(ticker, direction)
            if not contracts:
                return None
            
            # Filter by strike distance
            contracts = sorted(contracts, key=lambda c: abs(float(c.get('strike_price', 0)) - entry_price))
            
            # For 0DTE, find expiration today or nearest future
            # For Weekly, find expiration ~7 days out
            today = datetime.now().date()
            valid = []
            for c in contracts:
                exp = datetime.strptime(c.get('expiration_date', ''), '%Y-%m-%d').date()
                days_out = (exp - today).days
                if days_out < 0: continue
                if '0DTE' in instrument and days_out <= 2:
                    valid.append(c)
                elif 'Weekly' in instrument and 4 <= days_out <= 10:
                    valid.append(c)
            
            if valid:
                return valid[0].get('symbol')
        except Exception as e:
            logger.error(f"Failed to fetch OCC contract for {ticker}: {e}")
        return None

    # ── Position Sizing ──

    def calculate_position_size(self, ticker: str, entry_price: float,
                                 stop_pct: float, strategy: dict, multiplier: int = 1) -> int:
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
        risk_per_unit = (entry_price * stop_pct / 100) * multiplier
        shares = int(risk_amount / max(risk_per_unit, 0.01))

        # Ensure at least 1 share, max based on equity
        max_shares = int(equity * 0.25 / max(entry_price * multiplier, 1))  # Max 25% in one trade
        return max(1, min(shares, max_shares))

    # ── Trade Execution ──

    def open_trade(self, ticker: str, direction: str, entry_price: float,
                   strategy: dict, stop_pct: float, target_pct: float,
                   whale_confirmed: bool = False, paper: bool = True) -> Optional[Trade]:
        """
        Open a new trade with bracket order (entry + SL + TP).
        """
        can_trade, reason = self.can_open_trade(ticker, strategy)
        if not can_trade:
            logger.info(f"SKIP {strategy['name']} {ticker} {direction}: {reason}")
            return None

        occ_symbol = self._find_target_contract(ticker, direction, entry_price, strategy['instrument'])
        if not occ_symbol:
            logger.error(f"Failed to resolve Options Chain OCC string for {ticker}. Aborting execution.")
            return None

        # Fetch option premium from orderbook natively
        try:
            opt_snapshot = self.client.get_option_snapshot(ticker, occ_symbol)
            option_price = float(opt_snapshot.get('latestQuote', {}).get('ap', 0)) # Ask Price
            if option_price <= 0:
                option_price = float(opt_snapshot.get('latestTrade', {}).get('p', 0)) # Trade Price fallback
            if option_price <= 0:
                raise ValueError("Premium pricing resolved to 0")
        except Exception as e:
            logger.error(f"Failed to quote premium on {occ_symbol}: {e}")
            return None

        qty = self.calculate_position_size(ticker, option_price, stop_pct, strategy, multiplier=100)

        # Apply target mathematically directly to the physical premium string
        side = 'buy' # Options execute natively as 'buy to open' for calls AND puts
        stop_price = round(option_price * (1 - stop_pct / 100), 2)
        target_price = round(option_price * (1 + target_pct / 100), 2)

        # Create trade record
        self._trade_counter += 1
        trade_id = f"{strategy['name']}_{ticker}_{self._trade_counter}"

        trade = Trade(
            trade_id=trade_id,
            strategy=strategy['name'],
            ticker=ticker,
            occ_symbol=occ_symbol,
            direction=direction,
            instrument=strategy['instrument'],
            entry_price=option_price,
            entry_time=datetime.now().isoformat(),
            stop_pct=stop_pct,
            target_pct=target_pct,
            hold_bars=strategy['hold_bars'],
            whale_confirmed=whale_confirmed,
        )

        # Place bracket order via Alpaca on OCC String
        if not paper:
            try:
                order = self.client.place_bracket_order(
                    ticker=occ_symbol, qty=qty, side=side,
                    take_profit=target_price, stop_loss=stop_price,
                )
                trade.order_id = order.get('id', '')
                logger.info(f"ORDER PLACED: {trade_id} | {side} {qty} {occ_symbol} Options @ ${option_price:.2f} (x100) | "
                           f"SL=${stop_price:.2f} TP=${target_price:.2f} | Order: {trade.order_id}")
            except Exception as e:
                logger.error(f"ORDER FAILED: {trade_id} | {e}")
                return None
        else:
            logger.info(f"PAPER TRADE: {trade_id} | {side} {qty} {occ_symbol} Options @ ${option_price:.2f} (x100) | "
                       f"SL=${stop_price:.2f} TP=${target_price:.2f}")

        self.open_trades[trade_id] = trade
        self.daily_stats.trades_taken += 1
        return trade

    # ── Trade Monitoring ──

    def check_exits(self, current_prices: Dict[str, float]) -> List[Trade]:
        """
        Check all open trades for exit conditions.
        Returns list of trades that were closed.
        """
        closed = []
        to_close = []

        for trade_id, trade in self.open_trades.items():
            # In live options mode, we must retrieve the actual physical premium bid matching the OCC contract
            try:
                opt_snapshot = self.client.get_option_snapshot(trade.ticker, trade.occ_symbol)
                price = float(opt_snapshot.get('latestQuote', {}).get('bp', 0))
                if price <= 0:
                    price = float(opt_snapshot.get('latestTrade', {}).get('p', 0))
                if price <= 0:
                    continue
            except:
                continue

            trade.bars_held += 1
            exit_reason = None

            # Stop calculations are universal to option premiums since calls & puts both buy-to-open logic
            if price <= trade.stop_price:
                exit_reason = 'STOP'
            elif price >= trade.target_price:
                exit_reason = 'TARGET'
            elif trade.bars_held >= trade.hold_bars:
                exit_reason = 'TIME'

            if exit_reason:
                trade.exit_price = price
                trade.exit_time = datetime.now().isoformat()
                trade.exit_reason = exit_reason
                trade.status = 'CLOSED'

                # Calculate PnL
                trade.pnl = self._calculate_pnl(trade)

                # Update daily stats
                self.daily_stats.total_pnl += trade.pnl
                if trade.pnl > 0:
                    self.daily_stats.wins += 1
                else:
                    self.daily_stats.losses += 1

                to_close.append(trade_id)
                closed.append(trade)

                logger.info(
                    f"EXIT {trade_id}: {exit_reason} | "
                    f"Entry={trade.entry_price:.2f} Exit={price:.2f} | "
                    f"PnL=${trade.pnl:.2f} | Held {trade.bars_held} bars"
                )

        for tid in to_close:
            t = self.open_trades.pop(tid)
            self.closed_trades.append(t)

        return closed

    def force_close_all(self, current_prices: Dict[str, float], reason: str = 'MANUAL'):
        """Emergency close all positions."""
        for trade_id, trade in list(self.open_trades.items()):
            price = current_prices.get(trade.ticker, trade.entry_price)
            trade.exit_price = price
            trade.exit_time = datetime.now().isoformat()
            trade.exit_reason = reason
            trade.status = 'CLOSED'
            trade.pnl = self._calculate_pnl(trade)
            self.daily_stats.total_pnl += trade.pnl
            self.closed_trades.append(trade)
            logger.info(f"FORCE CLOSE {trade_id}: {reason} | PnL=${trade.pnl:.2f}")

        self.open_trades.clear()

        # Also close via Alpaca
        try:
            self.client.close_all_positions()
        except:
            pass

    # ── PnL Calculation ──

    def _calculate_pnl(self, trade: Trade) -> float:
        """Calculate physical PnL natively using extreme realism on options premium diff"""
        # Always mapping option premiums (buy to open, sell to close)
        move_pct = (trade.exit_price - trade.entry_price) / trade.entry_price * 100
        qty = self.calculate_position_size(trade.ticker, trade.entry_price, trade.stop_pct, {'backtest_wr': 50, 'instrument': trade.instrument}, multiplier=100)

        raw_pnl = (trade.exit_price - trade.entry_price) * 100 * qty
        spread_cost = qty * 0.02 * 100 # Approx $2 per contract
        
        pnl = raw_pnl - spread_cost
        return round(pnl, 2)

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
