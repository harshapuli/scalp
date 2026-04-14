"""
Institutional Order Flow Detection Module for ICT/SMC Options Trading System

This module provides institutional-grade flow analysis for identifying:
- Dark pool prints and off-exchange volume patterns
- Block trades and large institutional orders
- Wyckoff accumulation/distribution phases
- Cumulative delta and true volume analysis
- Smart money divergences and footprint analysis

Author: Live Trading Engine v2
"""

from dataclasses import dataclass, field
from typing import List, Tuple, Optional, Dict
from enum import Enum
import numpy as np
import pandas as pd
import logging
import time

logger = logging.getLogger(__name__)

# Optional UW client import
try:
    from unusual_whales import UnusualWhalesClient
except ImportError:
    UnusualWhalesClient = None


# ============================================================================
# ENUMS AND DATA CLASSES
# ============================================================================

class DeltaDirection(Enum):
    """Direction of institutional flow bias."""
    BUY = 1
    SELL = -1
    NEUTRAL = 0


class WyckoffPhase(Enum):
    """Wyckoff accumulation/distribution phases."""
    PHASE_A = "A"  # Selling climax, AR, ST
    PHASE_B = "B"  # Accumulation range / tests
    PHASE_C = "C"  # Spring (shakeout) / ENTRY
    PHASE_D = "D"  # Sign of strength / breakout
    PHASE_E = "E"  # Markup/Markdown
    UNKNOWN = "UNKNOWN"


class ActivityType(Enum):
    """Classification of institutional activity."""
    INITIATIVE = "initiative"  # Institutional aggressive buying/selling
    RESPONSIVE = "responsive"  # Reaction to retail activity
    ABSORPTION = "absorption"  # Soaking up volume
    EXHAUSTION = "exhaustion"  # End of move confirmation
    NEUTRAL = "neutral"


@dataclass
class DarkPoolPrint:
    """Represents a likely dark pool print event."""
    timestamp: pd.Timestamp
    price_level: float
    volume: float
    direction_bias: DeltaDirection
    confidence: float  # 0-100
    absorption_score: float = 0.0


@dataclass
class BlockTrade:
    """Represents an institutional block trade."""
    timestamp: pd.Timestamp
    price: float
    volume: float
    trade_type: str  # "accumulation" or "distribution"
    z_score: float
    bars_in_cluster: int = 1


@dataclass
class WyckoffState:
    """Current Wyckoff phase state."""
    phase: WyckoffPhase
    sub_phase: str  # e.g., "selling_climax", "automatic_rally", "secondary_test"
    range_high: float
    range_low: float
    bias: DeltaDirection
    confidence: float  # 0-100
    bars_in_phase: int = 0


@dataclass
class SmartMoneySignal:
    """Smart money vs retail divergence detection."""
    direction: str  # "bullish" or "bearish"
    divergence_type: str  # "obv", "mfi", "price_action"
    strength: float  # 0-100
    bars_active: int


@dataclass
class InstitutionalScore:
    """Aggregated institutional activity score."""
    score: float  # 0-100
    components: Dict[str, float]  # Sub-scores for each detector
    bias: DeltaDirection  # Overall bias
    summary: str  # Interpretation
    confidence: float


@dataclass
class FootprintBar:
    """Bar-level institutional footprint analysis."""
    timestamp: pd.Timestamp
    absorption_score: float  # 0-100
    exhaustion_flag: bool
    imbalance_dir: DeltaDirection
    activity_type: ActivityType
    delta_skew: float  # Standard deviations from neutral


# ============================================================================
# INSTITUTIONAL FLOW ANALYZER
# ============================================================================

class InstitutionalFlowAnalyzer:
    """
    Professional-grade institutional flow detection.
    Enhanced: uses real Unusual Whales API data when available,
    falls back to OHLCV approximation when not.

    Combines multiple institutional-level signals:
    - Dark pool print detection (REAL from UW API when available)
    - Block trade identification (REAL from UW flow alerts when available)
    - Wyckoff phase analysis
    - Cumulative delta and true volume
    - Smart money divergences
    - Institutional footprint profiling
    """

    def __init__(self, lookback: int = 20, sensitivity: float = 1.0, uw_client=None):
        """
        Initialize the analyzer.

        Args:
            lookback: Number of bars for rolling statistics (default 20)
            sensitivity: Multiplier for thresholds (>1 = more sensitive)
            uw_client: Optional UnusualWhalesClient for REAL institutional data
        """
        self.lookback = lookback
        self.sensitivity = sensitivity
        self.uw_client = uw_client
        self.ticker = None

        # Cached UW data
        self._uw_cache = {
            'dark_pool_prints': None,
            'flow_alerts': None,
            'institutional_sentiment': None,
            'last_refresh': 0,
        }

    def set_ticker(self, ticker: str):
        """Update ticker and clear UW cache."""
        if ticker != self.ticker:
            self.ticker = ticker
            self._uw_cache = {k: None for k in self._uw_cache}
            self._uw_cache['last_refresh'] = 0

    def _refresh_uw_data(self):
        """Fetch fresh UW data for current ticker."""
        if not self.uw_client or not self.ticker:
            return

        now = time.time()
        if now - self._uw_cache.get('last_refresh', 0) < 25:
            return

        try:
            self._uw_cache['dark_pool_prints'] = self.uw_client.get_dark_pool(self.ticker)
            self._uw_cache['flow_alerts'] = self.uw_client.get_flow_alerts(self.ticker)
            self._uw_cache['institutional_sentiment'] = self.uw_client.get_institutional_sentiment(self.ticker)
            self._uw_cache['last_refresh'] = now
        except Exception as e:
            logger.warning(f"Failed to refresh UW data for {self.ticker}: {e}")

    @property
    def has_real_data(self) -> bool:
        return self.uw_client is not None and self.ticker is not None

    # ========================================================================
    # 1. DARK POOL / OFF-EXCHANGE VOLUME DETECTION
    # ========================================================================

    def detect_dark_pool_prints(self, df: pd.DataFrame) -> List[DarkPoolPrint]:
        """
        Identify dark pool prints.
        Enhanced: uses REAL dark pool data from UW API when available.

        Args:
            df: DataFrame with OHLCV columns [open, high, low, close, volume]

        Returns:
            List of DarkPoolPrint objects with confidence scores
        """
        if len(df) < self.lookback:
            return []

        # === REAL DATA: Convert UW dark pool prints to our DarkPoolPrint format ===
        if self.has_real_data:
            self._refresh_uw_data()
            uw_prints = self._uw_cache.get('dark_pool_prints') or []
            if uw_prints:
                real_prints = []
                for uw_p in uw_prints:
                    if uw_p.canceled:
                        continue

                    # Determine direction from NBBO positioning
                    mid_price = (uw_p.nbbo_bid + uw_p.nbbo_ask) / 2.0
                    if uw_p.price > mid_price:
                        direction = DeltaDirection.BUY
                    elif uw_p.price < mid_price:
                        direction = DeltaDirection.SELL
                    else:
                        direction = DeltaDirection.NEUTRAL

                    # Use real volume as confidence proxy
                    confidence = min(100, 50 + (uw_p.volume / 50000) * 10)

                    # Use closest bar timestamp
                    ts = df.index[-1] if len(df) > 0 else pd.Timestamp.now()

                    real_prints.append(DarkPoolPrint(
                        timestamp=ts,
                        price_level=uw_p.price,
                        volume=float(uw_p.volume),
                        direction_bias=direction,
                        confidence=confidence,
                        absorption_score=min(100, uw_p.volume / 10000)
                    ))

                # Cluster and return
                if real_prints:
                    return self._cluster_dark_pools(real_prints, df)

        # === FALLBACK: OHLCV approximation ===
        prints = []
        df = df.copy()

        # Calculate volume rolling statistics
        df['vol_sma'] = df['volume'].rolling(self.lookback).mean()
        df['vol_std'] = df['volume'].rolling(self.lookback).std()

        # Calculate price impact (body size as % of range)
        df['range'] = df['high'] - df['low']
        df['body'] = abs(df['close'] - df['open'])
        df['body_pct'] = df['body'] / (df['range'] + 1e-8)

        # Calculate volatility (ATR-based)
        df['atr'] = self._calculate_atr(df, period=14)
        df['atr_sma'] = df['atr'].rolling(self.lookback).mean()
        df['vol_ratio'] = df['atr'] / (df['atr_sma'] + 1e-8)

        for i in range(self.lookback, len(df)):
            vol = df.iloc[i]['volume']
            vol_sma = df.iloc[i]['vol_sma']
            vol_std = df.iloc[i]['vol_std']
            body_pct = df.iloc[i]['body_pct']
            vol_ratio = df.iloc[i]['vol_ratio']

            # Signal 1: High volume with low body ratio (absorption)
            absorption_score = 0.0
            if vol_std > 0:
                z_vol = (vol - vol_sma) / vol_std
                if z_vol > 1.5 * self.sensitivity and body_pct < 0.3:
                    absorption_score = min(100, (z_vol * 20 + (1 - body_pct) * 50))

            # Signal 2: Volume spike during low volatility (stealth)
            stealth_score = 0.0
            if vol > vol_sma * 1.5 * self.sensitivity and vol_ratio < 0.8:
                stealth_score = min(100, (z_vol * 15 + (0.8 - vol_ratio) * 60))

            combined_score = max(absorption_score, stealth_score)

            if combined_score > 40:
                # Determine direction bias from close position
                close_pct = (df.iloc[i]['close'] - df.iloc[i]['low']) / (df.iloc[i]['range'] + 1e-8)
                if close_pct > 0.6:
                    direction = DeltaDirection.BUY
                elif close_pct < 0.4:
                    direction = DeltaDirection.SELL
                else:
                    direction = DeltaDirection.NEUTRAL

                confidence = min(100, combined_score)

                prints.append(DarkPoolPrint(
                    timestamp=df.index[i],
                    price_level=df.iloc[i]['close'],
                    volume=vol,
                    direction_bias=direction,
                    confidence=confidence,
                    absorption_score=absorption_score
                ))

        # Cluster detection: multiple prints in same zone
        if prints:
            prints = self._cluster_dark_pools(prints, df)

        return prints

    def _cluster_dark_pools(self, prints: List[DarkPoolPrint], df: pd.DataFrame) -> List[DarkPoolPrint]:
        """
        Enhance confidence of dark pool prints that cluster together.
        """
        if len(prints) < 2:
            return prints

        cluster_zone = df['close'].rolling(self.lookback).std().mean() * 0.5

        for i, print1 in enumerate(prints):
            cluster_count = 1
            for print2 in prints[i + 1:]:
                if abs(print1.price_level - print2.price_level) < cluster_zone:
                    cluster_count += 1

            # Boost confidence for clustered prints (institutional behavior)
            if cluster_count > 1:
                print1.confidence = min(100, print1.confidence + (cluster_count - 1) * 10)

        return prints

    # ========================================================================
    # 2. BLOCK TRADE DETECTION
    # ========================================================================

    def detect_block_trades(self, df: pd.DataFrame, threshold_multiplier: float = 5.0) -> List[BlockTrade]:
        """
        Identify institutional block trades.
        Enhanced: uses real UW flow alerts (sweeps, blocks) when available.

        Args:
            df: DataFrame with OHLCV
            threshold_multiplier: Sensitivity multiplier (default 5.0)

        Returns:
            List of BlockTrade objects
        """
        if len(df) < self.lookback:
            return []

        # === REAL DATA: Convert UW flow alerts to BlockTrade objects ===
        if self.has_real_data:
            self._refresh_uw_data()
            flow_alerts = self._uw_cache.get('flow_alerts') or []
            if flow_alerts:
                real_blocks = []
                for fa in flow_alerts:
                    # Only consider large trades and sweeps
                    if fa.total_size < 500 and not fa.has_sweep:
                        continue

                    # Determine accumulation vs distribution
                    if fa.option_type == 'call':
                        trade_type = "accumulation"
                    elif fa.option_type == 'put':
                        trade_type = "distribution"
                    else:
                        trade_type = "ambiguous"

                    # Z-score proxy from trade size
                    z_score = min(10, fa.total_size / 500)
                    if fa.has_sweep:
                        z_score += 2.0  # Sweep = more aggressive

                    ts = df.index[-1] if len(df) > 0 else pd.Timestamp.now()

                    real_blocks.append(BlockTrade(
                        timestamp=ts,
                        price=fa.underlying_price,
                        volume=float(fa.total_premium),  # Use premium as volume proxy
                        trade_type=trade_type,
                        z_score=z_score,
                        bars_in_cluster=1
                    ))

                if real_blocks:
                    return self._cluster_block_trades(real_blocks, df)

        # === FALLBACK: OHLCV approximation ===
        trades = []
        df = df.copy()

        df['vol_sma'] = df['volume'].rolling(self.lookback).mean()
        df['vol_std'] = df['volume'].rolling(self.lookback).std()
        df['atr'] = self._calculate_atr(df, 14)

        threshold = threshold_multiplier * self.sensitivity

        for i in range(self.lookback, len(df)):
            vol = df.iloc[i]['volume']
            vol_sma = df.iloc[i]['vol_sma']
            vol_std = df.iloc[i]['vol_std']

            if vol_std == 0:
                continue

            z_score = (vol - vol_sma) / vol_std

            # Block trade detection
            if z_score > threshold:
                # Classify: buying into weakness vs selling into strength
                prev_close = df.iloc[i - 1]['close']
                curr_close = df.iloc[i]['close']
                low = df.iloc[i]['low']
                high = df.iloc[i]['high']

                # Buying into weakness (accumulation)
                if curr_close > prev_close and low < prev_close:
                    trade_type = "accumulation"
                # Selling into strength (distribution)
                elif curr_close < prev_close and high > prev_close:
                    trade_type = "distribution"
                else:
                    trade_type = "ambiguous"

                trades.append(BlockTrade(
                    timestamp=df.index[i],
                    price=df.iloc[i]['close'],
                    volume=vol,
                    trade_type=trade_type,
                    z_score=z_score,
                    bars_in_cluster=1
                ))

        # Cluster block trades
        if trades:
            trades = self._cluster_block_trades(trades, df)

        return trades

    def _cluster_block_trades(self, trades: List[BlockTrade], df: pd.DataFrame) -> List[BlockTrade]:
        """
        Identify block trade clusters forming support/resistance.
        """
        if len(trades) < 2:
            return trades

        zone_width = df['close'].rolling(self.lookback).std().mean() * 0.75

        for i, trade1 in enumerate(trades):
            cluster_count = 1
            for trade2 in trades[i + 1:]:
                if abs(trade1.price - trade2.price) < zone_width:
                    cluster_count += 1

            trade1.bars_in_cluster = cluster_count
            if cluster_count > 1:
                trade1.z_score += (cluster_count - 1) * 0.5

        return trades

    # ========================================================================
    # 3. WYCKOFF ACCUMULATION/DISTRIBUTION PHASE DETECTION
    # ========================================================================

    def wyckoff_phase_detection(self, df: pd.DataFrame) -> WyckoffState:
        """
        Detect Wyckoff accumulation/distribution phases.

        Identifies:
        - Phase A: Selling climax (SC), Automatic Rally (AR), Secondary Test (ST)
        - Phase B: Accumulation range with tests of supply/demand
        - Phase C: Spring (shakeout below range) — THE ENTRY
        - Phase D: Sign of Strength (SOS) breakout above range
        - Phase E: Markup/Markdown

        Args:
            df: DataFrame with OHLCV

        Returns:
            WyckoffState with current phase, range levels, and bias
        """
        if len(df) < self.lookback * 2:
            return WyckoffState(
                phase=WyckoffPhase.UNKNOWN,
                sub_phase="insufficient_data",
                range_high=0,
                range_low=0,
                bias=DeltaDirection.NEUTRAL,
                confidence=0,
                bars_in_phase=0
            )

        df = df.copy()
        df['atr'] = self._calculate_atr(df, 14)
        df['vol_sma'] = df['volume'].rolling(self.lookback).mean()

        # Identify potential range (Phase B)
        look_period = min(60, len(df) - 1)
        range_high = df.iloc[-look_period:]['high'].max()
        range_low = df.iloc[-look_period:]['low'].min()
        range_size = range_high - range_low

        curr_price = df.iloc[-1]['close']
        curr_high = df.iloc[-1]['high']
        curr_low = df.iloc[-1]['low']
        curr_vol = df.iloc[-1]['volume']
        vol_sma = df.iloc[-1]['vol_sma']

        # Detect volume climax (extreme volume at extremes)
        vol_std = df['volume'].rolling(self.lookback).std().iloc[-1]
        recent_vol_z = (curr_vol - vol_sma) / (vol_std + 1e-8)

        # Phase detection logic
        if curr_price < range_low - range_size * 0.1:
            # Below range = Spring/Shakeout
            if recent_vol_z > 2.0:
                phase = WyckoffPhase.PHASE_C
                sub_phase = "spring"
            else:
                phase = WyckoffPhase.PHASE_C
                sub_phase = "spring_quiet"

        elif curr_price > range_high + range_size * 0.1:
            # Above range = Sign of Strength
            phase = WyckoffPhase.PHASE_D
            sub_phase = "sos_breakout"

        elif range_low < curr_price < range_high:
            # Inside range = Accumulation
            phase = WyckoffPhase.PHASE_B

            # Detect secondary tests
            if recent_vol_z > 1.5 and (curr_low < range_low + range_size * 0.05):
                sub_phase = "testing_demand"
            elif recent_vol_z > 1.5 and (curr_high > range_high - range_size * 0.05):
                sub_phase = "testing_supply"
            else:
                sub_phase = "consolidation"

        else:
            # Markup/Markdown phase
            phase = WyckoffPhase.PHASE_E
            sub_phase = "trend"

        # Determine bias from recent action
        recent_close_avg = df.iloc[-5:]['close'].mean()
        recent_open_avg = df.iloc[-5:]['open'].mean()

        if recent_close_avg > recent_open_avg:
            bias = DeltaDirection.BUY
        elif recent_close_avg < recent_open_avg:
            bias = DeltaDirection.SELL
        else:
            bias = DeltaDirection.NEUTRAL

        # Confidence based on volume and structure clarity
        confidence = min(100, abs(recent_vol_z) * 15 + 50)

        return WyckoffState(
            phase=phase,
            sub_phase=sub_phase,
            range_high=range_high,
            range_low=range_low,
            bias=bias,
            confidence=confidence,
            bars_in_phase=len(df) - max(0, len(df) - self.lookback * 2)
        )

    # ========================================================================
    # 4. CUMULATIVE DELTA ANALYSIS
    # ========================================================================

    def cumulative_delta(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Calculate true cumulative delta from tick approximation.

        Uses Bulk Volume Classification:
        - Close in upper half of bar = buy volume
        - Close in lower half of bar = sell volume

        Detects divergences (price vs delta) and momentum extremes.

        Args:
            df: DataFrame with OHLCV

        Returns:
            DataFrame with [buy_vol, sell_vol, delta, cum_delta, divergence, momentum]
        """
        if len(df) < 2:
            return pd.DataFrame()

        result = df.copy()
        result['range'] = result['high'] - result['low']

        # Bulk Volume Classification
        result['close_pct'] = (result['close'] - result['low']) / (result['range'] + 1e-8)
        result['buy_vol'] = result['volume'] * result['close_pct'].clip(0, 1)
        result['sell_vol'] = result['volume'] * (1 - result['close_pct'].clip(0, 1))

        # Cumulative delta
        result['delta'] = result['buy_vol'] - result['sell_vol']
        result['cum_delta'] = result['delta'].cumsum()

        # Delta divergence (price vs delta)
        result['price_direction'] = np.sign(result['close'].diff())
        result['delta_direction'] = np.sign(result['delta'].diff())

        # Divergence flag: price making highs but delta making lows
        result['new_high'] = result['close'] > result['close'].rolling(self.lookback).max().shift(1)
        result['new_low'] = result['close'] < result['close'].rolling(self.lookback).min().shift(1)

        cum_delta_max = result['cum_delta'].rolling(self.lookback).max()
        cum_delta_min = result['cum_delta'].rolling(self.lookback).min()

        result['delta_new_high'] = result['cum_delta'] > cum_delta_max.shift(1)
        result['delta_new_low'] = result['cum_delta'] < cum_delta_min.shift(1)

        # Bearish divergence: price new high, delta new low
        result['divergence'] = ((result['new_high'] & ~result['delta_new_high']) |
                                (result['new_low'] & ~result['delta_new_low'])).astype(int)

        # Delta momentum: rate of change
        result['delta_momentum'] = result['cum_delta'].diff().rolling(5).mean()

        return result[['buy_vol', 'sell_vol', 'delta', 'cum_delta', 'divergence', 'delta_momentum']]

    # ========================================================================
    # 5. INSTITUTIONAL FOOTPRINT ANALYSIS
    # ========================================================================

    def footprint_analysis(self, df: pd.DataFrame) -> pd.DataFrame:
        """
        Perform bar-by-bar institutional activity profiling.

        Detects:
        - Absorption: high volume + small body = institutional buying/selling
        - Exhaustion: extreme volume at key levels with reversal
        - Imbalance: delta skew > 2 std = one-sided institutional flow
        - Activity type: initiative vs responsive

        Args:
            df: DataFrame with OHLCV

        Returns:
            DataFrame with footprint analysis per bar
        """
        if len(df) < self.lookback:
            return pd.DataFrame()

        result = df.copy()

        # Calculate deltas
        delta_df = self.cumulative_delta(df)
        result['delta'] = delta_df['delta']
        result['buy_vol'] = delta_df['buy_vol']
        result['sell_vol'] = delta_df['sell_vol']

        # Absorption ratio: volume × small body ratio
        result['range'] = result['high'] - result['low']
        result['body'] = abs(result['close'] - result['open'])
        result['body_pct'] = result['body'] / (result['range'] + 1e-8)

        result['vol_sma'] = result['volume'].rolling(self.lookback).mean()
        result['vol_std'] = result['volume'].rolling(self.lookback).std()

        vol_z = (result['volume'] - result['vol_sma']) / (result['vol_std'] + 1e-8)
        absorption = (vol_z.clip(0, None) * (1 - result['body_pct'])) * 25
        result['absorption_score'] = absorption.clip(0, 100)

        # Exhaustion: extreme volume at key level with reversal
        result['is_exhaustion'] = False
        result['exhaustion_level'] = ""

        for i in range(1, len(result)):
            if vol_z.iloc[i] > 2.5:
                # Check if reversal follows
                if i + 1 < len(result):
                    if (result.iloc[i]['close'] > result.iloc[i]['open'] and
                        result.iloc[i + 1]['close'] < result.iloc[i + 1]['open']):
                        result.loc[result.index[i], 'is_exhaustion'] = True
                        result.loc[result.index[i], 'exhaustion_level'] = result.iloc[i]['high']

        # Imbalance detection: delta skew
        result['delta_abs'] = result['delta'].abs()
        result['delta_sma'] = result['delta_abs'].rolling(self.lookback).mean()
        result['delta_std'] = result['delta_abs'].rolling(self.lookback).std()

        delta_z = (result['delta_abs'] - result['delta_sma']) / (result['delta_std'] + 1e-8)
        result['imbalance_dir'] = result['delta'].apply(
            lambda x: DeltaDirection.BUY if x > 0 else (DeltaDirection.SELL if x < 0 else DeltaDirection.NEUTRAL)
        )

        # Activity type classification
        result['activity_type'] = ActivityType.NEUTRAL

        for i in range(self.lookback, len(result)):
            # Initiative: large directional volume with momentum
            if vol_z.iloc[i] > 2.0 and abs(delta_z.iloc[i]) > 1.5:
                result.loc[result.index[i], 'activity_type'] = ActivityType.INITIATIVE
            # Absorption: high volume with small body
            elif result.iloc[i]['absorption_score'] > 60:
                result.loc[result.index[i], 'activity_type'] = ActivityType.ABSORPTION
            # Exhaustion: detected above
            elif result.iloc[i]['is_exhaustion']:
                result.loc[result.index[i], 'activity_type'] = ActivityType.EXHAUSTION
            # Responsive: volume on continuation
            elif vol_z.iloc[i] > 1.0 and (result.iloc[i]['close'] > result.iloc[i]['open']) == (
                result.iloc[i - 1]['close'] > result.iloc[i - 1]['open']):
                result.loc[result.index[i], 'activity_type'] = ActivityType.RESPONSIVE

        result['delta_skew'] = delta_z

        return result[['absorption_score', 'is_exhaustion', 'imbalance_dir', 'activity_type', 'delta_skew']]

    # ========================================================================
    # 6. SMART MONEY DIVERGENCE
    # ========================================================================

    def smart_money_divergence(self, df: pd.DataFrame) -> Optional[SmartMoneySignal]:
        """
        Detect smart money vs retail divergence.

        Patterns:
        - Price trending one way, large volume accumulating the other
        - OBV divergence with volume weighting
        - Money Flow Index divergence filtered for institutional volume

        Args:
            df: DataFrame with OHLCV

        Returns:
            SmartMoneySignal or None if insufficient data
        """
        if len(df) < self.lookback * 2:
            return None

        df = df.copy()

        # OBV Divergence
        df['obv'] = (np.sign(df['close'].diff()) * df['volume']).fillna(0).cumsum()
        df['price_high'] = df['close'].rolling(self.lookback).max()
        df['price_low'] = df['close'].rolling(self.lookback).min()
        df['obv_high'] = df['obv'].rolling(self.lookback).max()
        df['obv_low'] = df['obv'].rolling(self.lookback).min()

        # Bearish divergence: price makes new high, OBV doesn't
        bearish_div = (
            (df['close'] > df['price_high'].shift(1)) &
            (df['obv'] < df['obv_high'].shift(1))
        )

        # Bullish divergence: price makes new low, OBV doesn't
        bullish_div = (
            (df['close'] < df['price_low'].shift(1)) &
            (df['obv'] > df['obv_low'].shift(1))
        )

        # Money Flow Index (MFI)
        typical_price = (df['high'] + df['low'] + df['close']) / 3
        money_flow = typical_price * df['volume']

        positive_flow = money_flow.where(typical_price > typical_price.shift(1), 0)
        negative_flow = money_flow.where(typical_price < typical_price.shift(1), 0)

        pos_mf_sum = positive_flow.rolling(self.lookback).sum()
        neg_mf_sum = negative_flow.rolling(self.lookback).sum()

        mfi = 100 - (100 / (1 + (pos_mf_sum / (neg_mf_sum + 1e-8))))

        # MFI divergence
        mfi_bearish = (df['close'] > df['price_high'].shift(1)) & (mfi < mfi.shift(self.lookback))
        mfi_bullish = (df['close'] < df['price_low'].shift(1)) & (mfi > mfi.shift(self.lookback))

        # Current signal
        curr_bearish = bearish_div.iloc[-1] or mfi_bearish.iloc[-1]
        curr_bullish = bullish_div.iloc[-1] or mfi_bullish.iloc[-1]

        bars_active = 0
        if curr_bearish:
            direction = "bearish"
            bars_active = bearish_div.iloc[-self.lookback:].sum()
        elif curr_bullish:
            direction = "bullish"
            bars_active = bullish_div.iloc[-self.lookback:].sum()
        else:
            return None

        # Strength based on OBV divergence magnitude
        obv_range = df['obv'].rolling(self.lookback).max() - df['obv'].rolling(self.lookback).min()
        obv_div_size = abs(df['obv'].iloc[-1] - df['obv'].rolling(self.lookback).max().iloc[-1])
        strength = min(100, (obv_div_size / (obv_range.iloc[-1] + 1e-8)) * 100)

        div_type = "obv" if (bearish_div.iloc[-1] or bullish_div.iloc[-1]) else "mfi"

        return SmartMoneySignal(
            direction=direction,
            divergence_type=div_type,
            strength=strength,
            bars_active=int(bars_active)
        )

    # ========================================================================
    # 7. AGGREGATED INSTITUTIONAL SCORE
    # ========================================================================

    def get_institutional_score(
        self,
        df: pd.DataFrame,
        price_level: Optional[float] = None
    ) -> InstitutionalScore:
        """
        Aggregate all institutional signals into single 0-100 score.

        Weighted combination:
        - Dark pool prints (20%)
        - Block trades (20%)
        - Wyckoff phase (15%)
        - Cumulative delta (15%)
        - Footprint analysis (15%)
        - Smart money divergence (15%)

        Args:
            df: DataFrame with OHLCV
            price_level: Optional price to score institutional activity AT that level

        Returns:
            InstitutionalScore with aggregated score and components
        """
        components = {}

        # 1. Dark pool prints (20%)
        dark_pools = self.detect_dark_pool_prints(df)
        if dark_pools:
            recent_prints = [p for p in dark_pools if len(df) - df.index.get_loc(p.timestamp) < self.lookback * 2]
            if recent_prints:
                dark_pool_score = np.mean([p.confidence for p in recent_prints])
            else:
                dark_pool_score = 0
        else:
            dark_pool_score = 0
        components['dark_pool'] = dark_pool_score

        # 2. Block trades (20%)
        block_trades = self.detect_block_trades(df)
        if block_trades:
            recent_blocks = [t for t in block_trades if len(df) - df.index.get_loc(t.timestamp) < self.lookback * 2]
            if recent_blocks:
                block_score = min(100, np.mean([min(t.z_score, 5) * 20 for t in recent_blocks]))
            else:
                block_score = 0
        else:
            block_score = 0
        components['block_trade'] = block_score

        # 3. Wyckoff phase (15%)
        wyckoff = self.wyckoff_phase_detection(df)
        wyckoff_score = wyckoff.confidence if wyckoff.phase != WyckoffPhase.UNKNOWN else 0
        components['wyckoff'] = wyckoff_score

        # 4. Cumulative delta (15%)
        delta_df = self.cumulative_delta(df)
        if not delta_df.empty and len(delta_df) > 0:
            # Score based on divergence presence and momentum
            div_score = delta_df['divergence'].iloc[-self.lookback:].sum() * 10
            momentum = abs(delta_df['delta_momentum'].iloc[-1])
            delta_score = min(100, div_score + momentum * 5)
        else:
            delta_score = 0
        components['cumulative_delta'] = delta_score

        # 5. Footprint analysis (15%)
        footprint = self.footprint_analysis(df)
        if not footprint.empty and len(footprint) > 0:
            absorption_avg = footprint['absorption_score'].iloc[-self.lookback:].mean()
            exhaustion_count = footprint['is_exhaustion'].iloc[-self.lookback:].sum() * 15
            footprint_score = min(100, (absorption_avg + exhaustion_count) / 2)
        else:
            footprint_score = 0
        components['footprint'] = footprint_score

        # 6. Smart money divergence (15%)
        divergence = self.smart_money_divergence(df)
        if divergence:
            divergence_score = divergence.strength
        else:
            divergence_score = 0
        components['divergence'] = divergence_score

        # === REAL DATA: Add UW institutional sentiment as a component ===
        uw_sentiment_score = 0
        if self.has_real_data:
            self._refresh_uw_data()
            sentiment = self._uw_cache.get('institutional_sentiment')
            if sentiment:
                uw_sentiment_score = sentiment.overall_score
                components['uw_sentiment'] = uw_sentiment_score

        # Weighted aggregate
        if uw_sentiment_score > 0:
            # When real UW data available, give it significant weight
            weights = {
                'dark_pool': 0.15,
                'block_trade': 0.15,
                'wyckoff': 0.12,
                'cumulative_delta': 0.12,
                'footprint': 0.10,
                'divergence': 0.10,
                'uw_sentiment': 0.26,  # UW real data gets 26% weight
            }
        else:
            weights = {
                'dark_pool': 0.20,
                'block_trade': 0.20,
                'wyckoff': 0.15,
                'cumulative_delta': 0.15,
                'footprint': 0.15,
                'divergence': 0.15,
            }

        total_score = sum(components.get(k, 0) * weights.get(k, 0) for k in weights)

        # Determine bias — prefer real UW data
        if self.has_real_data and self._uw_cache.get('institutional_sentiment'):
            sentiment = self._uw_cache['institutional_sentiment']
            if sentiment.net_premium_direction == "BULLISH":
                bias = DeltaDirection.BUY
            elif sentiment.net_premium_direction == "BEARISH":
                bias = DeltaDirection.SELL
            else:
                bias = DeltaDirection.NEUTRAL
        elif divergence and divergence.direction == "bullish":
            bias = DeltaDirection.BUY
        elif divergence and divergence.direction == "bearish":
            bias = DeltaDirection.SELL
        elif wyckoff.bias != DeltaDirection.NEUTRAL:
            bias = wyckoff.bias
        else:
            bias = DeltaDirection.NEUTRAL

        # Summary interpretation
        data_source = " (UW+OHLCV)" if uw_sentiment_score > 0 else " (OHLCV only)"
        if total_score >= 70:
            summary = "INSTITUTIONAL CONFIRMED" + data_source
        elif 40 <= total_score < 70:
            summary = "MIXED (institutional + retail)" + data_source
        else:
            summary = "RETAIL-DOMINATED" + data_source

        return InstitutionalScore(
            score=total_score,
            components=components,
            bias=bias,
            summary=summary,
            confidence=min(100, wyckoff.confidence)
        )

    # ========================================================================
    # UTILITY METHODS
    # ========================================================================

    @staticmethod
    def _calculate_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Average True Range."""
        df = df.copy()
        df['tr'] = np.maximum(
            df['high'] - df['low'],
            np.maximum(
                abs(df['high'] - df['close'].shift(1)),
                abs(df['low'] - df['close'].shift(1))
            )
        )
        return df['tr'].rolling(period).mean()


# ============================================================================
# DEMO / TESTING
# ============================================================================

if __name__ == '__main__':
    """
    Demo: Generate sample OHLCV data and run all institutional flow detectors.
    """
    import warnings
    warnings.filterwarnings('ignore')

    print("=" * 80)
    print("INSTITUTIONAL FLOW DETECTION - DEMO")
    print("=" * 80)

    # Generate realistic sample data
    np.random.seed(42)
    n_bars = 200

    prices = [100.0]
    opens = [100.0]
    closes = [100.0]
    highs = [100.0]
    lows = [100.0]
    volumes = []

    for i in range(n_bars):
        # Random walk with drift
        change = np.random.normal(0.05, 1.0)
        price = prices[-1] + change
        prices.append(price)

        # OHLC construction
        open_p = price + np.random.uniform(-0.5, 0.5)
        close_p = price + np.random.uniform(-0.5, 0.5)
        high_p = max(open_p, close_p) + abs(np.random.normal(0, 0.3))
        low_p = min(open_p, close_p) - abs(np.random.normal(0, 0.3))

        opens.append(open_p)
        closes.append(close_p)
        highs.append(high_p)
        lows.append(low_p)

        # Volume with occasional spikes (dark pools / blocks)
        base_vol = np.random.uniform(1000, 2000)
        if i % 30 == 0:  # Dark pool print
            vol = base_vol * np.random.uniform(3, 6)
        elif i % 40 == 0:  # Block trade
            vol = base_vol * np.random.uniform(5, 8)
        else:
            vol = base_vol
        volumes.append(vol)

    # Create DataFrame
    dates = pd.date_range(start='2024-01-01', periods=n_bars, freq='1H')
    df = pd.DataFrame({
        'open': opens[1:],
        'high': highs[1:],
        'low': lows[1:],
        'close': closes[1:],
        'volume': volumes
    }, index=dates)

    print(f"\nGenerated sample data: {len(df)} bars from {df.index[0]} to {df.index[-1]}")
    print(f"Price range: {df['close'].min():.2f} - {df['close'].max():.2f}")
    print(f"Volume range: {df['volume'].min():.0f} - {df['volume'].max():.0f}")

    # Initialize analyzer
    analyzer = InstitutionalFlowAnalyzer(lookback=20, sensitivity=1.0)

    # 1. Dark Pool Prints
    print("\n" + "-" * 80)
    print("1. DARK POOL PRINT DETECTION")
    print("-" * 80)
    dark_pools = analyzer.detect_dark_pool_prints(df)
    if dark_pools:
        print(f"Found {len(dark_pools)} dark pool prints:")
        for i, dp in enumerate(dark_pools[:5], 1):
            print(f"  {i}. {dp.timestamp.strftime('%Y-%m-%d %H:%M')} @ {dp.price_level:.2f} | "
                  f"Vol: {dp.volume:.0f} | Confidence: {dp.confidence:.1f}% | Dir: {dp.direction_bias.name}")
    else:
        print("No significant dark pool prints detected.")

    # 2. Block Trades
    print("\n" + "-" * 80)
    print("2. BLOCK TRADE DETECTION")
    print("-" * 80)
    block_trades = analyzer.detect_block_trades(df, threshold_multiplier=4.0)
    if block_trades:
        print(f"Found {len(block_trades)} block trades:")
        for i, bt in enumerate(block_trades[:5], 1):
            print(f"  {i}. {bt.timestamp.strftime('%Y-%m-%d %H:%M')} @ {bt.price:.2f} | "
                  f"Type: {bt.trade_type.upper()} | Z-Score: {bt.z_score:.2f} | Cluster: {bt.bars_in_cluster}")
    else:
        print("No significant block trades detected.")

    # 3. Wyckoff Phase
    print("\n" + "-" * 80)
    print("3. WYCKOFF PHASE DETECTION")
    print("-" * 80)
    wyckoff = analyzer.wyckoff_phase_detection(df)
    print(f"Phase: {wyckoff.phase.value}")
    print(f"Sub-phase: {wyckoff.sub_phase}")
    print(f"Range: {wyckoff.range_low:.2f} - {wyckoff.range_high:.2f}")
    print(f"Bias: {wyckoff.bias.name}")
    print(f"Confidence: {wyckoff.confidence:.1f}%")

    # 4. Cumulative Delta
    print("\n" + "-" * 80)
    print("4. CUMULATIVE DELTA ANALYSIS")
    print("-" * 80)
    delta_df = analyzer.cumulative_delta(df)
    if not delta_df.empty:
        latest = delta_df.iloc[-1]
        print(f"Buy Volume (last bar): {latest['buy_vol']:.0f}")
        print(f"Sell Volume (last bar): {latest['sell_vol']:.0f}")
        print(f"Delta (last bar): {latest['delta']:.0f}")
        print(f"Cumulative Delta: {latest['cum_delta']:.0f}")
        print(f"Delta Momentum: {latest['delta_momentum']:.0f}")

        div_count = delta_df['divergence'].sum()
        print(f"Divergence count (last 20 bars): {int(div_count)}")
    else:
        print("Insufficient data for delta analysis.")

    # 5. Footprint Analysis
    print("\n" + "-" * 80)
    print("5. INSTITUTIONAL FOOTPRINT ANALYSIS")
    print("-" * 80)
    footprint = analyzer.footprint_analysis(df)
    if not footprint.empty:
        recent = footprint.iloc[-10:]
        print(f"Recent footprint scores (last 10 bars):")
        for i, (idx, row) in enumerate(recent.iterrows(), 1):
            print(f"  Bar {i}: Absorption={row['absorption_score']:.1f} | "
                  f"Exhaustion={row['is_exhaustion']} | "
                  f"Imbalance={row['imbalance_dir'].name} | "
                  f"Activity={row['activity_type'].value}")
    else:
        print("Insufficient data for footprint analysis.")

    # 6. Smart Money Divergence
    print("\n" + "-" * 80)
    print("6. SMART MONEY DIVERGENCE")
    print("-" * 80)
    divergence = analyzer.smart_money_divergence(df)
    if divergence:
        print(f"Direction: {divergence.direction.upper()}")
        print(f"Type: {divergence.divergence_type.upper()}")
        print(f"Strength: {divergence.strength:.1f}%")
        print(f"Bars Active: {divergence.bars_active}")
    else:
        print("No significant smart money divergence detected.")

    # 7. Aggregated Score
    print("\n" + "-" * 80)
    print("7. AGGREGATED INSTITUTIONAL SCORE")
    print("-" * 80)
    inst_score = analyzer.get_institutional_score(df)
    print(f"Overall Score: {inst_score.score:.1f}/100")
    print(f"Summary: {inst_score.summary}")
    print(f"Bias: {inst_score.bias.name}")
    print(f"\nComponent Scores:")
    for comp, score in inst_score.components.items():
        print(f"  {comp.upper():20s}: {score:6.1f}")

    print("\n" + "=" * 80)
    print("DEMO COMPLETE")
    print("=" * 80)
