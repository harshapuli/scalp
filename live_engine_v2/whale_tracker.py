"""
Whale Tracker Module - ICT/SMC Options Trading System
Detects large player accumulation, distribution, volume profiles, and institutional positioning
"""

from dataclasses import dataclass
from typing import List, Optional, Tuple
from enum import Enum
import numpy as np
import pandas as pd
from collections import defaultdict
import logging

logger = logging.getLogger(__name__)

# Optional UW client import
try:
    from unusual_whales import UnusualWhalesClient
except ImportError:
    UnusualWhalesClient = None


class ZoneStatus(Enum):
    """Accumulation zone lifecycle"""
    ACTIVE = "ACTIVE"
    COMPLETED = "COMPLETED"
    BROKEN = "BROKEN"


class WhaleDirection(Enum):
    """Whale trading direction"""
    BUY = "BUY"
    SELL = "SELL"
    NEUTRAL = "NEUTRAL"


class AlertType(Enum):
    """Real-time whale alert types"""
    WHALE_ENTRY = "WHALE_ENTRY"
    WHALE_EXIT = "WHALE_EXIT"
    WHALE_EXHAUSTION = "WHALE_EXHAUSTION"
    WHALE_ABSORPTION = "WHALE_ABSORPTION"
    WHALE_DIVERGENCE = "WHALE_DIVERGENCE"
    WHALE_TRAP = "WHALE_TRAP"


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class AccumulationZone:
    """Whale accumulation zone marker"""
    price_low: float
    price_high: float
    price_mid: float
    total_volume: float
    buy_pct: float  # % of zone volume that was bullish
    status: str  # ACTIVE, COMPLETED, BROKEN
    first_seen: int  # bar index
    last_seen: int
    score: float  # composite strength score
    consistency: float  # how regularly whales hit this zone
    recency_weight: float  # recent activity bonus

    def __repr__(self):
        return (f"AccumZone({self.price_low:.2f}-{self.price_high:.2f}, "
                f"vol={self.total_volume:.0f}, buy={self.buy_pct:.0f}%, "
                f"score={self.score:.2f}, {self.status})")


@dataclass
class DistributionSignal:
    """Large player distribution/unload detection"""
    active: bool
    strength: float  # 0-100
    price_ceiling: float
    bars_in_distribution: int
    exit_urgency: str  # LOW, MEDIUM, HIGH
    evidence: List[str]  # What triggered this


@dataclass
class VolumeProfile:
    """Volume-at-price analysis"""
    poc: float  # Point of Control - fair value
    vah: float  # Value Area High - 70% range top
    val: float  # Value Area Low - 70% range bottom
    hvn_levels: List[float]  # High volume nodes
    lvn_levels: List[float]  # Low volume nodes
    profile_data: dict  # {price_bin: volume}
    developing_poc: bool  # POC still forming

    def __repr__(self):
        return (f"VolumeProfile(POC={self.poc:.2f}, VAH={self.vah:.2f}, "
                f"VAL={self.val:.2f}, HVN={len(self.hvn_levels)}, LVN={len(self.lvn_levels)})")


@dataclass
class VWAPData:
    """VWAP and band analysis"""
    vwap: float
    upper_band_1: float
    lower_band_1: float
    upper_band_2: float
    lower_band_2: float
    deviation_pct: float  # How far from VWAP
    band_position: str  # "above_2", "between_1_2", "between_vwap_1", "below_vwap", etc
    band_position_numeric: float  # -2 to +2 scale


@dataclass
class IcebergOrder:
    """Hidden/iceberg order detection"""
    price_level: float
    direction: str  # BUY or SELL
    total_estimated_volume: float
    num_touches: int
    confidence: float  # 0-100
    still_active: bool
    bars_active: int


@dataclass
class WhaleMomentum:
    """Whale flow and momentum tracking"""
    net_flow: float  # Cumulative whale buy - whale sell
    flow_direction: str  # BUY or SELL
    flow_strength: float  # 0-100
    divergence: bool  # Whale flow diverging from price
    exhaustion_flag: bool  # Declining volume at extremes
    conviction_score: float  # 0-100, consistency of whale
    bars_consistent: int


@dataclass
class WhaleSignal:
    """Composite actionable whale signal"""
    direction: str  # WHALE_BUY, WHALE_SELL, WHALE_NEUTRAL
    confidence: float  # 0-100
    nearby_zones: List[AccumulationZone]
    momentum: WhaleMomentum
    distribution_signal: Optional[DistributionSignal]
    volume_profile: VolumeProfile
    key_summary: str


@dataclass
class WhaleBarEvent:
    """Per-bar whale activity event"""
    bar_index: int
    is_whale_bar: bool
    whale_volume: float
    direction: str  # BUY or SELL
    at_accumulation_zone: bool
    zone_reference: Optional[AccumulationZone]
    confirms_whale_direction: bool
    is_exhaustion_bar: bool
    exhaustion_at: str  # "high", "low", or None
    cumulative_delta: float
    timestamp: str  # For live tracking


@dataclass
class WhaleAlert:
    """Active whale alert for real-time trading"""
    alert_type: str  # AlertType enum value
    direction: str  # BUY or SELL
    confidence: float  # 0-100
    price_level: float
    bars_active: int
    description: str
    trade_signal: str  # Strong/Moderate/Weak
    timestamp: str


@dataclass
class WhalePosition:
    """Estimated large player positioning"""
    direction: str  # LONG, SHORT, FLAT
    estimated_size_pct: float  # % of total volume
    loading_rate: float  # bars/unit (how fast accumulating)
    inventory_change: str  # "loading", "reducing", "flat", "flipping"
    confidence: float  # 0-100
    bars_in_position: int


@dataclass
class WhaleTrap:
    """Stop hunt + whale entry detection"""
    sweep_price: float
    trap_direction: str  # BUY or SELL
    whale_bar_idx: int
    volume: float
    trap_quality: float  # 0-100, how obvious the trap
    bars_after_sweep: int


@dataclass
class DivergenceState:
    """Whale vs retail volume divergence"""
    active: bool
    whale_direction: str  # BUY or SELL
    retail_direction: str  # BUY or SELL
    bars_diverging: int
    strength: float  # 0-100
    trade_signal: str  # "Strong bearish divergence", etc


# ============================================================================
# WHALE TRACKER CLASS
# ============================================================================

class WhaleTracker:
    """Institutional whale detection and tracking - enhanced with real UW data"""

    def __init__(self, volume_threshold_percentile=75, uw_client=None, ticker=None):
        """
        Args:
            volume_threshold_percentile: Volume above this percentile = whale volume (OHLCV fallback)
            uw_client: Optional UnusualWhalesClient instance for REAL institutional data
            ticker: Ticker symbol for UW API calls (required if uw_client is set)
        """
        self.volume_threshold_percentile = volume_threshold_percentile
        self.uw_client = uw_client
        self.ticker = ticker
        self._last_accumulation_zones = []
        self._last_whale_signal = None

        # Cached UW data (refreshed per scan cycle)
        self._uw_cache = {
            'dark_pool_levels': None,
            'whale_signal_live': None,
            'institutional_sentiment': None,
            'flow_alerts': None,
            'dark_pool_prints': None,
            'net_prem_ticks': None,
            'last_refresh': 0,
        }

        # Live tracking state
        self._live_state = {
            'whale_bar_count': 0,
            'consecutive_whale_bars': 0,
            'last_whale_direction': None,
            'cumulative_whale_delta': 0.0,
            'alerts': [],
            'last_update_idx': -1,
            'whale_bars_sequence': [],
            'divergence_counter': 0,
            'absorption_counter': 0,
        }

    def set_ticker(self, ticker: str):
        """Update ticker and clear UW cache."""
        self.ticker = ticker
        self._uw_cache = {k: None for k in self._uw_cache}
        self._uw_cache['last_refresh'] = 0

    def _refresh_uw_data(self):
        """Fetch fresh UW data for current ticker. Called once per scan cycle."""
        import time
        if not self.uw_client or not self.ticker:
            return

        now = time.time()
        if now - self._uw_cache.get('last_refresh', 0) < 25:
            return  # Use cache for 25 seconds

        try:
            self._uw_cache['dark_pool_prints'] = self.uw_client.get_dark_pool(self.ticker)
            self._uw_cache['dark_pool_levels'] = self.uw_client.get_dark_pool_levels(self.ticker)
            self._uw_cache['whale_signal_live'] = self.uw_client.get_whale_signal_live(self.ticker)
            self._uw_cache['institutional_sentiment'] = self.uw_client.get_institutional_sentiment(self.ticker)
            self._uw_cache['flow_alerts'] = self.uw_client.get_flow_alerts(self.ticker)
            self._uw_cache['net_prem_ticks'] = self.uw_client.get_net_premium_ticks(self.ticker)
            self._uw_cache['last_refresh'] = now
            logger.debug(f"UW data refreshed for {self.ticker}")
        except Exception as e:
            logger.warning(f"Failed to refresh UW data for {self.ticker}: {e}")

    @property
    def has_real_data(self) -> bool:
        """Check if real UW data is available."""
        return (self.uw_client is not None and self.ticker is not None)

    def _calculate_atr(self, df: pd.DataFrame, period: int = 14) -> pd.Series:
        """Calculate Average True Range"""
        if len(df) < period:
            return pd.Series(0, index=df.index)

        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift())
        low_close = abs(df['low'] - df['close'].shift())

        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(window=period).mean()
        return atr

    def _is_whale_volume(self, df: pd.DataFrame) -> pd.Series:
        """Identify whale-sized bars by volume"""
        threshold = df['volume'].quantile(self.volume_threshold_percentile / 100)
        return df['volume'] > threshold

    def _bar_direction(self, df: pd.DataFrame) -> pd.Series:
        """1 if bullish, -1 if bearish, 0 if doji"""
        direction = np.where(df['close'] > df['open'], 1,
                            np.where(df['close'] < df['open'], -1, 0))
        return pd.Series(direction, index=df.index)

    # ========================================================================
    # 1. ACCUMULATION ZONES
    # ========================================================================

    def detect_accumulation_zones(self, df: pd.DataFrame, lookback: int = 100) -> List[AccumulationZone]:
        """
        Detect price levels where large players are accumulating.
        Enhanced: when UW client is available, overlays REAL dark pool levels
        onto OHLCV-detected zones for much higher accuracy.

        Args:
            df: OHLCV dataframe
            lookback: bars to analyze

        Returns:
            List of AccumulationZone objects
        """
        if len(df) < lookback:
            lookback = len(df)

        df = df.tail(lookback).copy()
        df = df.reset_index(drop=True)

        atr = self._calculate_atr(df)
        is_whale = self._is_whale_volume(df)
        direction = self._bar_direction(df)

        # === REAL DATA OVERLAY: inject dark pool levels as confirmed zones ===
        if self.has_real_data:
            self._refresh_uw_data()
            dp_levels = self._uw_cache.get('dark_pool_levels') or []
            if dp_levels:
                dp_zones = self._dark_pool_to_accumulation_zones(dp_levels, df)
                if dp_zones:
                    # DP zones are high-confidence; OHLCV zones supplement
                    self._last_accumulation_zones = dp_zones
                    # Still run OHLCV analysis below to find additional zones
                    # but DP zones take priority

        # Price clustering: group whale bars by similar prices
        whale_indices = np.where(is_whale)[0]
        if len(whale_indices) < 2:
            return []

        whale_bars = df.loc[whale_indices]
        clustering_distance = atr.iloc[-1] * 0.5 if not np.isnan(atr.iloc[-1]) else 0.5

        # Cluster whale bars by price (midpoint)
        clusters = defaultdict(list)
        price_midpoints = (whale_bars['high'] + whale_bars['low']) / 2

        for idx, bar_idx in enumerate(whale_indices):
            price_mid = price_midpoints.iloc[idx]

            # Find closest existing cluster
            closest_cluster = None
            min_distance = clustering_distance

            for cluster_key in clusters.keys():
                if abs(price_mid - cluster_key) < min_distance:
                    min_distance = abs(price_mid - cluster_key)
                    closest_cluster = cluster_key

            if closest_cluster is not None:
                clusters[closest_cluster].append(bar_idx)
            else:
                clusters[price_mid].append(bar_idx)

        # Score and analyze each cluster
        accumulation_zones = []
        current_bar = len(df) - 1

        for cluster_price, bar_indices in clusters.items():
            if len(bar_indices) < 2:
                continue

            cluster_df = df.loc[bar_indices]
            price_low = cluster_df['low'].min()
            price_high = cluster_df['high'].max()
            total_volume = cluster_df['volume'].sum()

            # Buy/sell breakdown
            bullish_volume = cluster_df[direction.loc[bar_indices] == 1]['volume'].sum()
            buy_pct = (bullish_volume / total_volume * 100) if total_volume > 0 else 50

            # Consistency: how many times whales came back to this zone
            consistency = len(bar_indices) / lookback

            # Recency: more recent activity = higher weight
            first_seen = bar_indices[0]
            last_seen = bar_indices[-1]
            bars_since_last = current_bar - last_seen
            recency_weight = max(0, 1 - (bars_since_last / lookback))

            # Status: is zone still active or broken through?
            current_price = df['close'].iloc[-1]
            if current_price > price_high:
                status = ZoneStatus.BROKEN.value
            elif current_price > price_low and current_price < price_high:
                status = ZoneStatus.ACTIVE.value
            else:
                status = ZoneStatus.COMPLETED.value

            # Composite score
            score = (total_volume / df['volume'].max()) * consistency * (1 + recency_weight)

            zone = AccumulationZone(
                price_low=price_low,
                price_high=price_high,
                price_mid=cluster_price,
                total_volume=total_volume,
                buy_pct=buy_pct,
                status=status,
                first_seen=first_seen,
                last_seen=last_seen,
                score=score,
                consistency=consistency,
                recency_weight=recency_weight
            )

            accumulation_zones.append(zone)

        # Sort by score
        accumulation_zones.sort(key=lambda z: z.score, reverse=True)

        # === MERGE with dark pool zones if available ===
        if self.has_real_data and self._last_accumulation_zones:
            dp_zones = self._last_accumulation_zones  # Set earlier from DP data
            # Merge: boost OHLCV zones near DP levels, add DP zones not covered
            merged = self._merge_zones(dp_zones, accumulation_zones)
            self._last_accumulation_zones = merged
            return merged

        self._last_accumulation_zones = accumulation_zones
        return accumulation_zones

    def _dark_pool_to_accumulation_zones(self, dp_levels, df: pd.DataFrame) -> List[AccumulationZone]:
        """Convert real dark pool levels from UW API into AccumulationZone objects."""
        zones = []
        current_price = df['close'].iloc[-1] if len(df) > 0 else 0
        atr = self._calculate_atr(df)
        atr_val = atr.iloc[-1] if len(atr) > 0 and not np.isnan(atr.iloc[-1]) else 1.0

        for dp in dp_levels:
            if dp.total_volume < 10000:  # Filter noise
                continue

            price = dp.price
            zone_width = atr_val * 0.3  # Tight zone around DP price

            # Status based on current price vs zone
            if current_price > price + zone_width:
                status = ZoneStatus.BROKEN.value
            elif current_price < price - zone_width:
                status = ZoneStatus.COMPLETED.value
            else:
                status = ZoneStatus.ACTIVE.value

            # Score from real volume data (normalized)
            score = min(5.0, dp.total_volume / 500000)  # High score for big volume

            # Buy percentage from direction bias
            if dp.direction_bias == 'ACCUMULATION':
                buy_pct = 75.0
            elif dp.direction_bias == 'DISTRIBUTION':
                buy_pct = 25.0
            else:
                buy_pct = 50.0

            zone = AccumulationZone(
                price_low=price - zone_width,
                price_high=price + zone_width,
                price_mid=price,
                total_volume=float(dp.total_volume),
                buy_pct=buy_pct,
                status=status,
                first_seen=0,
                last_seen=len(df) - 1,
                score=score * 2.0,  # Boost: real DP data = 2x confidence vs OHLCV
                consistency=dp.trade_count / 20.0,
                recency_weight=1.0  # Real data is always "recent"
            )
            zones.append(zone)

        zones.sort(key=lambda z: z.score, reverse=True)
        return zones[:10]  # Top 10 DP zones

    def _merge_zones(self, dp_zones: List[AccumulationZone],
                     ohlcv_zones: List[AccumulationZone]) -> List[AccumulationZone]:
        """Merge dark pool zones with OHLCV zones. DP zones get priority."""
        merged = list(dp_zones)  # Start with DP zones

        for oz in ohlcv_zones:
            # Check if OHLCV zone overlaps any DP zone
            overlaps = False
            for dz in dp_zones:
                if abs(oz.price_mid - dz.price_mid) / (dz.price_mid + 1e-8) < 0.005:
                    # Boost the DP zone with OHLCV confirmation
                    dz.score *= 1.3  # 30% boost for confluence
                    dz.consistency = max(dz.consistency, oz.consistency)
                    overlaps = True
                    break

            if not overlaps:
                # Add OHLCV zone at reduced score (no DP confirmation)
                oz.score *= 0.6
                merged.append(oz)

        merged.sort(key=lambda z: z.score, reverse=True)
        return merged[:15]

    # ========================================================================
    # 2. WHALE DISTRIBUTION DETECTION
    # ========================================================================

    def detect_distribution(self, df: pd.DataFrame) -> DistributionSignal:
        """
        Detect when large players are unloading/distributing.
        Enhanced: uses real UW net premium and dark pool sentiment when available.

        Returns:
            DistributionSignal with activity and urgency assessment
        """
        if len(df) < 20:
            return DistributionSignal(False, 0, 0, 0, "LOW", [])

        df = df.tail(50).copy()
        df = df.reset_index(drop=True)

        is_whale = self._is_whale_volume(df)
        direction = self._bar_direction(df)

        evidence = []
        distribution_strength = 0
        bars_in_dist = 0

        # === REAL DATA: Check UW institutional sentiment for distribution ===
        if self.has_real_data:
            self._refresh_uw_data()
            sentiment = self._uw_cache.get('institutional_sentiment')
            if sentiment:
                # Large negative net premium = institutional selling
                if sentiment.net_premium < -5_000_000:  # >$5M net put premium
                    evidence.append(f"UW: Heavy put premium ${sentiment.net_premium:,.0f}")
                    distribution_strength += 35
                elif sentiment.net_premium < -1_000_000:
                    evidence.append(f"UW: Moderate put premium ${sentiment.net_premium:,.0f}")
                    distribution_strength += 20

                # Dark pool distribution signal
                if sentiment.dark_pool_sentiment == "DISTRIBUTION":
                    evidence.append(f"UW: Dark pool DISTRIBUTION ({sentiment.dark_pool_volume:,} shares)")
                    distribution_strength += 25

                # High put/call ratio
                if sentiment.put_call_ratio > 1.5:
                    evidence.append(f"UW: Elevated P/C ratio {sentiment.put_call_ratio:.2f}")
                    distribution_strength += 15

        # Signal 1: Rising price + declining large bar volume
        rising_price = df['close'].iloc[-1] > df['close'].iloc[-10]
        recent_whale_volume = df.loc[is_whale & (df.index >= len(df) - 10), 'volume'].mean()
        earlier_whale_volume = df.loc[is_whale & (df.index < len(df) - 10), 'volume'].mean()

        if rising_price and earlier_whale_volume > 0:
            vol_decline = (1 - recent_whale_volume / earlier_whale_volume) * 100
            if vol_decline > 30:
                evidence.append("Rising price with declining whale volume")
                distribution_strength += 25

        # Signal 2: Repeated tests of highs with decreasing volume
        recent_highs = df['high'].tail(10).values
        recent_high = recent_highs.max()
        touch_count = 0
        touch_volumes = []

        for i in range(len(df) - 10, len(df)):
            if df['high'].iloc[i] >= recent_high * 0.99:
                touch_count += 1
                touch_volumes.append(df['volume'].iloc[i])

        if touch_count > 2 and len(touch_volumes) > 1:
            vol_trend = touch_volumes[-1] / (touch_volumes[0] + 1e-8)
            if vol_trend < 0.7:
                evidence.append(f"Supply exhaustion: {touch_count} tests of high with declining volume")
                distribution_strength += 30
                bars_in_dist = touch_count

        # Signal 3: Large bearish bars after rally
        recent_df = df.tail(5)
        for i in recent_df.index:
            if is_whale.iloc[i] and direction.iloc[i] == -1:
                if df['close'].iloc[i] < df['open'].iloc[i]:
                    body_pct = ((df['open'].iloc[i] - df['close'].iloc[i]) /
                               (df['high'].iloc[i] - df['low'].iloc[i] + 1e-8))
                    if body_pct > 0.6:
                        evidence.append("Large bearish whale candle detected")
                        distribution_strength += 20

        # Determine urgency
        if distribution_strength >= 60:
            exit_urgency = "HIGH"
        elif distribution_strength >= 35:
            exit_urgency = "MEDIUM"
        else:
            exit_urgency = "LOW"

        ceiling = df['high'].tail(10).max()
        is_active = distribution_strength >= 25

        return DistributionSignal(
            active=is_active,
            strength=min(100, distribution_strength),
            price_ceiling=ceiling,
            bars_in_distribution=bars_in_dist,
            exit_urgency=exit_urgency,
            evidence=evidence
        )

    # ========================================================================
    # 3. VOLUME PROFILE & POINT OF CONTROL
    # ========================================================================

    def build_volume_profile(self, df: pd.DataFrame, num_bins: int = 50) -> VolumeProfile:
        """
        Build volume-at-price profile for institutional fair value detection

        Returns:
            VolumeProfile with POC, VAH, VAL, and volume nodes
        """
        if len(df) < 10:
            poc = df['close'].iloc[-1]
            return VolumeProfile(poc, poc, poc, [], [], {}, True)

        df = df.tail(len(df)).copy()

        # Create price bins
        price_min = df['low'].min()
        price_max = df['high'].max()
        bins = np.linspace(price_min, price_max, num_bins)

        # Distribute volume to price bins based on OHLC
        profile_data = defaultdict(float)

        for idx, row in df.iterrows():
            bar_volume = row['volume']
            bar_range = row['high'] - row['low']

            if bar_range > 0:
                # Distribute volume proportionally across touched prices
                for i in range(len(bins) - 1):
                    bin_low, bin_high = bins[i], bins[i + 1]

                    # Calculate overlap between bar range and bin
                    overlap_low = max(row['low'], bin_low)
                    overlap_high = min(row['high'], bin_high)

                    if overlap_high > overlap_low:
                        overlap_pct = (overlap_high - overlap_low) / bar_range
                        profile_data[round((bin_low + bin_high) / 2, 2)] += bar_volume * overlap_pct
            else:
                # Doji - put all volume at close price
                bin_idx = np.searchsorted(bins, row['close'])
                bin_price = bins[min(bin_idx, len(bins) - 1)]
                profile_data[bin_price] += bar_volume

        # Find POC (price with highest volume)
        if profile_data:
            poc = max(profile_data, key=profile_data.get)
            total_profile_volume = sum(profile_data.values())
        else:
            poc = df['close'].iloc[-1]
            total_profile_volume = 1

        # Find VAH/VAL (70% of volume range)
        sorted_prices = sorted(profile_data.items(), key=lambda x: x[1], reverse=True)
        cumulative_volume = 0
        value_area_prices = []

        for price, vol in sorted_prices:
            cumulative_volume += vol
            value_area_prices.append(price)
            if cumulative_volume >= total_profile_volume * 0.7:
                break

        if value_area_prices:
            vah = max(value_area_prices)
            val = min(value_area_prices)
        else:
            vah = poc
            val = poc

        # Find HVN (high volume nodes) and LVN (low volume nodes)
        threshold_high = np.percentile(list(profile_data.values()), 75)
        threshold_low = np.percentile(list(profile_data.values()), 25)

        hvn_levels = sorted([p for p, v in profile_data.items() if v >= threshold_high])
        lvn_levels = sorted([p for p, v in profile_data.items() if v <= threshold_low])

        # Check if POC is still developing (near recent bars)
        recent_highs = df['high'].tail(5).max()
        recent_lows = df['low'].tail(5).min()
        developing = val <= recent_lows or vah >= recent_highs

        return VolumeProfile(
            poc=poc,
            vah=vah,
            val=val,
            hvn_levels=hvn_levels[:5],  # Top 5
            lvn_levels=lvn_levels[:5],
            profile_data=dict(profile_data),
            developing_poc=developing
        )

    # ========================================================================
    # 4. VWAP ANCHORED ANALYSIS
    # ========================================================================

    def anchored_vwap(self, df: pd.DataFrame, anchor_idx: int = 0) -> VWAPData:
        """
        Calculate anchored VWAP from a specific point

        Args:
            df: OHLCV dataframe
            anchor_idx: bar index to anchor from (0 = today)

        Returns:
            VWAPData with VWAP and band analysis
        """
        if len(df) < 2:
            price = df['close'].iloc[-1]
            return VWAPData(price, price, price, price, price, 0, "middle", 0)

        # Anchor from specified index (0 = current bar)
        if anchor_idx >= len(df):
            anchor_idx = 0

        anchor_idx = len(df) - 1 - anchor_idx

        # Calculate VWAP from anchor
        df_anchored = df.iloc[anchor_idx:].copy()
        df_anchored = df_anchored.reset_index(drop=True)

        typical_price = (df_anchored['high'] + df_anchored['low'] + df_anchored['close']) / 3
        cumulative_tp_volume = (typical_price * df_anchored['volume']).cumsum()
        cumulative_volume = df_anchored['volume'].cumsum()

        vwap = cumulative_tp_volume / cumulative_volume

        # Calculate standard deviation for bands
        squared_diff = ((typical_price - vwap) ** 2 * df_anchored['volume']).cumsum()
        variance = squared_diff / cumulative_volume
        std_dev = np.sqrt(variance.clip(lower=0))

        # Current VWAP and bands
        current_vwap = vwap.iloc[-1]
        current_std = std_dev.iloc[-1]
        current_price = df['close'].iloc[-1]

        upper_band_1 = current_vwap + current_std
        lower_band_1 = current_vwap - current_std
        upper_band_2 = current_vwap + (2 * current_std)
        lower_band_2 = current_vwap - (2 * current_std)

        # Deviation from VWAP
        deviation_pct = ((current_price - current_vwap) / current_vwap) * 100

        # Band position
        if current_price > upper_band_2:
            band_position = "above_2sigma"
            band_numeric = 2.0
        elif current_price > upper_band_1:
            band_position = "between_1_2_upper"
            band_numeric = 1.5
        elif current_price > current_vwap:
            band_position = "between_vwap_1_upper"
            band_numeric = 0.5
        elif current_price > lower_band_1:
            band_position = "between_vwap_1_lower"
            band_numeric = -0.5
        elif current_price > lower_band_2:
            band_position = "between_1_2_lower"
            band_numeric = -1.5
        else:
            band_position = "below_2sigma"
            band_numeric = -2.0

        return VWAPData(
            vwap=current_vwap,
            upper_band_1=upper_band_1,
            lower_band_1=lower_band_1,
            upper_band_2=upper_band_2,
            lower_band_2=lower_band_2,
            deviation_pct=deviation_pct,
            band_position=band_position,
            band_position_numeric=band_numeric
        )

    # ========================================================================
    # 5. ICEBERG ORDER DETECTION
    # ========================================================================

    def detect_iceberg_orders(self, df: pd.DataFrame) -> List[IcebergOrder]:
        """
        Detect hidden/iceberg orders by price clustering.
        Enhanced: uses real dark pool prints from UW for genuine hidden order detection.

        Returns:
            List of IcebergOrder detections
        """
        if len(df) < 10:
            return []

        # === REAL DATA: Convert dark pool prints to iceberg detections ===
        if self.has_real_data:
            self._refresh_uw_data()
            dp_prints = self._uw_cache.get('dark_pool_prints') or []
            if dp_prints:
                return self._dark_pool_to_icebergs(dp_prints, df)

        # === FALLBACK: OHLCV approximation ===
        df = df.tail(50).copy()
        df = df.reset_index(drop=True)

        is_whale = self._is_whale_volume(df)
        direction = self._bar_direction(df)

        # Price clustering with ±0.1% tolerance
        whale_indices = np.where(is_whale)[0]
        if len(whale_indices) < 2:
            return []

        whale_bars = df.loc[whale_indices]
        icebergs = []

        # Group whale bars by price (±0.1% clustering)
        clusters = defaultdict(list)

        for idx, bar_idx in enumerate(whale_indices):
            price_level = whale_bars['close'].iloc[idx]

            # Find if similar price exists
            found = False
            for cluster_key in list(clusters.keys()):
                pct_diff = abs(price_level - cluster_key) / cluster_key
                if pct_diff < 0.001:  # ±0.1%
                    clusters[cluster_key].append(bar_idx)
                    found = True
                    break

            if not found:
                clusters[price_level].append(bar_idx)

        # Analyze clusters for iceberg characteristics
        for cluster_price, bar_indices in clusters.items():
            if len(bar_indices) < 3:  # Need at least 3 touches
                continue

            cluster_df = df.loc[bar_indices]

            # Direction consistency (all buying or all selling)
            cluster_directions = direction.loc[bar_indices]
            buy_count = (cluster_directions == 1).sum()
            sell_count = (cluster_directions == -1).sum()

            if buy_count > sell_count:
                iceberg_direction = "BUY"
            else:
                iceberg_direction = "SELL"

            # Consistency check: should have similar volumes
            volumes = cluster_df['volume'].values
            vol_std = np.std(volumes)
            vol_mean = np.mean(volumes)
            consistency = 1 - (vol_std / vol_mean) if vol_mean > 0 else 0
            consistency = max(0, consistency)

            # Total estimated volume
            total_estimated = volumes.sum()

            # How many bars since last touch
            bars_since_last = len(df) - 1 - bar_indices[-1]
            still_active = bars_since_last <= 5

            # Confidence based on: consistency, number of touches, directional conviction
            confidence = (len(bar_indices) / 10 * 30 +  # Touches
                         consistency * 40 +  # Volume consistency
                         max(buy_count, sell_count) / len(bar_indices) * 30)  # Direction
            confidence = min(100, confidence)

            iceberg = IcebergOrder(
                price_level=cluster_price,
                direction=iceberg_direction,
                total_estimated_volume=total_estimated,
                num_touches=len(bar_indices),
                confidence=confidence,
                still_active=still_active,
                bars_active=bars_since_last
            )

            icebergs.append(iceberg)

        # Sort by confidence
        icebergs.sort(key=lambda x: x.confidence, reverse=True)

        return icebergs

    def _dark_pool_to_icebergs(self, dp_prints, df: pd.DataFrame) -> List[IcebergOrder]:
        """Convert real dark pool prints into iceberg order detections."""
        from collections import defaultdict

        # Cluster prints by price (±0.1%)
        clusters = defaultdict(list)
        for p in dp_prints:
            rounded = round(p.price, 1)
            found = False
            for key in list(clusters.keys()):
                if abs(rounded - key) / (key + 1e-8) < 0.001:
                    clusters[key].append(p)
                    found = True
                    break
            if not found:
                clusters[rounded].append(p)

        icebergs = []
        for price_level, prints in clusters.items():
            if len(prints) < 2:
                continue

            total_vol = sum(p.volume for p in prints)

            # Direction: compare to NBBO midpoint
            buy_prints = sum(1 for p in prints if p.price >= (p.nbbo_bid + p.nbbo_ask) / 2)
            sell_prints = len(prints) - buy_prints
            iceberg_direction = "BUY" if buy_prints > sell_prints else "SELL"

            # Confidence from real data
            confidence = min(100, 40 + len(prints) * 8 + (total_vol / 100000) * 5)

            iceberg = IcebergOrder(
                price_level=price_level,
                direction=iceberg_direction,
                total_estimated_volume=total_vol,
                num_touches=len(prints),
                confidence=confidence,
                still_active=True,  # Real DP prints = recent activity
                bars_active=0
            )
            icebergs.append(iceberg)

        icebergs.sort(key=lambda x: x.confidence, reverse=True)
        return icebergs[:10]

    # ========================================================================
    # 6. WHALE MOMENTUM TRACKING
    # ========================================================================

    def whale_momentum(self, df: pd.DataFrame, lookback: int = 20) -> WhaleMomentum:
        """
        Track whale positioning momentum and divergence.
        Enhanced: uses real UW options flow and net premium when available.

        Returns:
            WhaleMomentum with flow direction and conviction
        """
        if len(df) < lookback:
            lookback = len(df)

        # === REAL DATA PATH: Use UW net premium + flow alerts ===
        if self.has_real_data:
            self._refresh_uw_data()
            whale_live = self._uw_cache.get('whale_signal_live')
            sentiment = self._uw_cache.get('institutional_sentiment')

            if whale_live and sentiment:
                # Direction from real net premium
                if whale_live.direction == "BULLISH":
                    flow_direction = "BUY"
                elif whale_live.direction == "BEARISH":
                    flow_direction = "SELL"
                else:
                    flow_direction = "NEUTRAL"

                # Net flow = net premium (real dollar flow)
                net_flow = whale_live.premium_flow

                # Flow strength from UW confidence
                flow_strength = whale_live.confidence

                # Divergence: options flow vs price direction
                price_change = df['close'].iloc[-1] - df['close'].iloc[-min(lookback, len(df))]
                divergence = False
                if price_change > 0 and flow_direction == "SELL":
                    divergence = True
                elif price_change < 0 and flow_direction == "BUY":
                    divergence = True

                # Exhaustion: declining sweep counts or premium
                exhaustion_flag = (whale_live.sweep_count == 0 and
                                   abs(whale_live.premium_flow) < 500_000)

                # Conviction from overall score
                conviction_score = sentiment.overall_score

                # Count sweep + block as "consistent bars"
                bars_consistent = whale_live.sweep_count + whale_live.block_count

                return WhaleMomentum(
                    net_flow=net_flow,
                    flow_direction=flow_direction,
                    flow_strength=min(100, flow_strength),
                    divergence=divergence,
                    exhaustion_flag=exhaustion_flag,
                    conviction_score=min(100, conviction_score),
                    bars_consistent=bars_consistent
                )

        # === FALLBACK: OHLCV approximation ===
        df = df.tail(lookback).copy()
        df = df.reset_index(drop=True)

        is_whale = self._is_whale_volume(df)
        direction = self._bar_direction(df)

        # Calculate whale buy/sell volumes
        whale_buy = np.where((is_whale) & (direction == 1), df['volume'], 0)
        whale_sell = np.where((is_whale) & (direction == -1), df['volume'], 0)

        # Net whale flow
        net_flow = whale_buy.sum() - whale_sell.sum()

        if whale_buy.sum() > whale_sell.sum():
            flow_direction = "BUY"
        elif whale_sell.sum() > whale_buy.sum():
            flow_direction = "SELL"
        else:
            flow_direction = "NEUTRAL"

        # Flow strength (0-100)
        total_whale_volume = whale_buy.sum() + whale_sell.sum()
        if total_whale_volume > 0:
            flow_strength = abs(net_flow) / total_whale_volume * 100
        else:
            flow_strength = 0

        # Divergence: whale flow vs price direction
        price_change = df['close'].iloc[-1] - df['close'].iloc[0]
        divergence = False

        if price_change > 0 and flow_direction == "SELL":
            divergence = True  # Price up but whales selling
        elif price_change < 0 and flow_direction == "BUY":
            divergence = True  # Price down but whales buying

        # Exhaustion: declining whale volume at new highs/lows
        exhaustion_flag = False

        recent_high = df['high'].tail(5).max()
        earlier_high = df['high'].iloc[:-5].max() if len(df) > 5 else recent_high

        if recent_high > earlier_high:
            recent_whale_vol = is_whale.tail(5).sum()
            earlier_whale_vol = is_whale.iloc[:-5].sum() if len(df) > 5 else recent_whale_vol

            if earlier_whale_vol > 0 and recent_whale_vol < earlier_whale_vol * 0.7:
                exhaustion_flag = True

        # Conviction: consistency of whale direction
        whale_bar_count = is_whale.sum()
        if whale_bar_count > 0:
            directional_bars = max((direction[is_whale] == 1).sum(),
                                  (direction[is_whale] == -1).sum())
            conviction_score = (directional_bars / whale_bar_count) * 100
        else:
            conviction_score = 0

        # Bars with consistent direction
        bars_consistent = 0
        if flow_direction == "BUY":
            bars_consistent = (direction == 1).sum()
        elif flow_direction == "SELL":
            bars_consistent = (direction == -1).sum()

        return WhaleMomentum(
            net_flow=net_flow,
            flow_direction=flow_direction,
            flow_strength=min(100, flow_strength),
            divergence=divergence,
            exhaustion_flag=exhaustion_flag,
            conviction_score=min(100, conviction_score),
            bars_consistent=bars_consistent
        )

    # ========================================================================
    # 7. COMPOSITE WHALE SIGNAL
    # ========================================================================

    def get_whale_signal(self, df: pd.DataFrame,
                        price_level: Optional[float] = None) -> WhaleSignal:
        """
        Generate composite whale signal combining all indicators

        Args:
            df: OHLCV dataframe
            price_level: Optional specific price to evaluate for institutional support

        Returns:
            WhaleSignal with direction, confidence, and reasoning
        """
        if len(df) < 20:
            return WhaleSignal(
                direction="WHALE_NEUTRAL",
                confidence=0,
                nearby_zones=[],
                momentum=WhaleMomentum(0, "NEUTRAL", 0, False, False, 0, 0),
                distribution_signal=DistributionSignal(False, 0, 0, 0, "LOW", []),
                volume_profile=VolumeProfile(df['close'].iloc[-1],
                                            df['close'].iloc[-1],
                                            df['close'].iloc[-1],
                                            [], [], {}, True),
                key_summary="Insufficient data"
            )

        # Run all whale analysis
        zones = self.detect_accumulation_zones(df)
        distribution = self.detect_distribution(df)
        whale_momentum_result = self.whale_momentum(df)
        volume_profile = self.build_volume_profile(df)
        icebergs = self.detect_iceberg_orders(df)

        # Calculate signal
        signal_score = 0
        signal_direction = WhaleDirection.NEUTRAL
        reasoning = []

        current_price = df['close'].iloc[-1]
        nearby_zones = [z for z in zones if abs(z.price_mid - current_price) / current_price < 0.02]

        # === REAL DATA BOOST: incorporate UW live signal directly ===
        if self.has_real_data:
            self._refresh_uw_data()
            whale_live = self._uw_cache.get('whale_signal_live')
            sentiment = self._uw_cache.get('institutional_sentiment')

            if whale_live:
                if whale_live.direction == "BULLISH":
                    signal_score += 30 * (whale_live.confidence / 100)
                    reasoning.append(f"UW: BULLISH flow (${whale_live.premium_flow:,.0f} net, "
                                    f"{whale_live.sweep_count} sweeps, conf={whale_live.confidence:.0f}%)")
                elif whale_live.direction == "BEARISH":
                    signal_score -= 30 * (whale_live.confidence / 100)
                    reasoning.append(f"UW: BEARISH flow (${whale_live.premium_flow:,.0f} net, "
                                    f"{whale_live.sweep_count} sweeps, conf={whale_live.confidence:.0f}%)")

                # Dark pool volume as supporting evidence
                if whale_live.dark_pool_flow > 1_000_000:
                    dp_direction = "supporting" if (
                        (whale_live.direction == "BULLISH" and signal_score > 0) or
                        (whale_live.direction == "BEARISH" and signal_score < 0)
                    ) else "conflicting"
                    reasoning.append(f"UW: {whale_live.dark_pool_flow:,} DP shares ({dp_direction})")

            if sentiment:
                # Overlay institutional conviction
                if sentiment.overall_score > 70:
                    score_boost = 15
                elif sentiment.overall_score > 55:
                    score_boost = 8
                else:
                    score_boost = 0

                if sentiment.net_premium_direction == "BULLISH":
                    signal_score += score_boost
                elif sentiment.net_premium_direction == "BEARISH":
                    signal_score -= score_boost

                if score_boost > 0:
                    reasoning.append(f"UW: Inst conviction {sentiment.overall_score:.0f}/100 "
                                    f"({sentiment.net_premium_direction})")

        # Signal from accumulation zones
        active_zones = [z for z in zones if z.status == ZoneStatus.ACTIVE.value]
        if active_zones:
            avg_zone_score = np.mean([z.score for z in active_zones])
            zone_strength = min(40, avg_zone_score * 50)

            # Are we at a zone?
            price_in_zone = any(z.price_low <= current_price <= z.price_high
                               for z in active_zones)

            if price_in_zone:
                signal_score += zone_strength
                signal_direction = WhaleDirection.BUY
                reasoning.append(f"Price in active accumulation zone (score: {zone_strength:.0f})")

        # Signal from whale momentum
        if whale_momentum_result.flow_direction == "BUY":
            signal_score += whale_momentum_result.flow_strength * 0.4
            if whale_momentum_result.flow_strength > 50:
                signal_direction = WhaleDirection.BUY
                reasoning.append(f"Strong whale buying momentum ({whale_momentum_result.flow_strength:.0f}%)")
        elif whale_momentum_result.flow_direction == "SELL":
            signal_score -= whale_momentum_result.flow_strength * 0.3
            reasoning.append(f"Whale selling pressure ({whale_momentum_result.flow_strength:.0f}%)")

        # Signal from divergence (contrary indicator)
        if whale_momentum_result.divergence:
            if whale_momentum_result.flow_direction == "BUY":
                signal_score += 15
                reasoning.append("Whale buying on price weakness (potential reversal)")
            else:
                signal_score -= 20
                reasoning.append("Whale distribution despite price strength (warning)")

        # Signal from distribution
        if distribution.active:
            signal_score -= distribution.strength * 0.3
            if distribution.exit_urgency == "HIGH":
                signal_score -= 30
            reasoning.append(f"Distribution detected (urgency: {distribution.exit_urgency})")

        # Signal from icebergs (support/resistance)
        if icebergs:
            strongest_iceberg = icebergs[0]
            if strongest_iceberg.direction == "BUY":
                signal_score += strongest_iceberg.confidence * 0.2
                reasoning.append(f"Buy-side iceberg at {strongest_iceberg.price_level:.2f}")
            else:
                signal_score -= strongest_iceberg.confidence * 0.2
                reasoning.append(f"Sell-side iceberg at {strongest_iceberg.price_level:.2f}")

        # Volume profile signal
        price_vs_poc = current_price - volume_profile.poc
        if price_vs_poc < 0 and abs(price_vs_poc) / volume_profile.poc > 0.01:
            signal_score += 10
            reasoning.append(f"Price below POC ({volume_profile.poc:.2f}) - mean reversion potential")

        # Finalize signal
        signal_score = max(-100, min(100, signal_score))

        if signal_score > 30:
            final_direction = "WHALE_BUY"
            confidence = min(100, signal_score + 20)
        elif signal_score < -30:
            final_direction = "WHALE_SELL"
            confidence = min(100, abs(signal_score) + 20)
        else:
            final_direction = "WHALE_NEUTRAL"
            confidence = min(100, 50 - abs(signal_score))

        # Check specific price level if provided
        nearby_for_level = []
        if price_level is not None:
            nearby_for_level = [z for z in zones
                               if abs(z.price_mid - price_level) / price_level < 0.01]

        summary = " | ".join(reasoning) if reasoning else "No whale signals detected"

        result = WhaleSignal(
            direction=final_direction,
            confidence=confidence,
            nearby_zones=nearby_zones,
            momentum=whale_momentum_result,
            distribution_signal=distribution,
            volume_profile=volume_profile,
            key_summary=summary
        )
        # Cache for whale intent classifier
        self._last_whale_signal = result
        self._last_distribution = distribution
        self._last_volume_profile = volume_profile
        return result

    # ========================================================================
    # 8. LIVE TRACKING - PER-BAR UPDATES
    # ========================================================================

    def update_bar(self, bar: pd.Series, bar_index: int = 0, df: pd.DataFrame = None) -> Optional[WhaleBarEvent]:
        """
        Called every new bar to update live tracking state.
        Enhanced: checks real UW flow alerts for whale confirmation.

        Args:
            bar: Single bar series (open, high, low, close, volume)
            bar_index: Index of this bar in the full dataframe
            df: Optional full dataframe for context analysis

        Returns:
            WhaleBarEvent if this bar is whale-significant, else None
        """
        # Skip if this is a duplicate update
        if bar_index <= self._live_state['last_update_idx']:
            return None

        # Determine if this is a whale bar
        whale_threshold = 75  # percentile
        is_whale = False

        # === REAL DATA: check if UW flow confirms whale activity ===
        uw_confirms_whale = False
        if self.has_real_data:
            self._refresh_uw_data()
            flow_alerts = self._uw_cache.get('flow_alerts') or []
            whale_live = self._uw_cache.get('whale_signal_live')

            # Any recent sweep or block = confirmed whale activity
            if flow_alerts:
                sweep_count = len([a for a in flow_alerts if a.has_sweep])
                block_count = len([a for a in flow_alerts if a.total_size > 1000])
                uw_confirms_whale = (sweep_count > 0 or block_count > 0)

        if df is not None and len(df) > 0:
            threshold = df['volume'].quantile(whale_threshold / 100)
            is_whale = bar['volume'] > threshold or uw_confirms_whale
        else:
            is_whale = bar['volume'] > np.percentile([1000, 2000], whale_threshold) or uw_confirms_whale

        # Bar direction
        if bar['close'] > bar['open']:
            direction = "BUY"
            whale_direction = 1
        elif bar['close'] < bar['open']:
            direction = "SELL"
            whale_direction = -1
        else:
            direction = "NEUTRAL"
            whale_direction = 0

        # Update cumulative delta
        if is_whale:
            if whale_direction == 1:
                self._live_state['cumulative_whale_delta'] += bar['volume']
            elif whale_direction == -1:
                self._live_state['cumulative_whale_delta'] -= bar['volume']

            # Track consecutive whale bars
            if self._live_state['last_whale_direction'] == whale_direction:
                self._live_state['consecutive_whale_bars'] += 1
            else:
                self._live_state['consecutive_whale_bars'] = 1
                self._live_state['last_whale_direction'] = whale_direction

            self._live_state['whale_bar_count'] += 1
            self._live_state['whale_bars_sequence'].append({
                'idx': bar_index,
                'direction': direction,
                'volume': bar['volume']
            })
            # Keep only last 10
            if len(self._live_state['whale_bars_sequence']) > 10:
                self._live_state['whale_bars_sequence'].pop(0)

        # Check if at accumulation zone
        at_zone = False
        zone_ref = None
        if self._last_accumulation_zones:
            for zone in self._last_accumulation_zones:
                if zone.price_low <= bar['close'] <= zone.price_high:
                    at_zone = True
                    zone_ref = zone
                    break

        # Check exhaustion (whale volume at new highs/lows)
        is_exhaustion = False
        exhaustion_at = None
        if is_whale and df is not None and len(df) > 5:
            recent_high = df['high'].tail(10).max() if len(df) >= 10 else df['high'].max()
            recent_low = df['low'].tail(10).min() if len(df) >= 10 else df['low'].min()

            if bar['high'] > recent_high and direction == "BUY":
                is_exhaustion = True
                exhaustion_at = "high"
            elif bar['low'] < recent_low and direction == "SELL":
                is_exhaustion = True
                exhaustion_at = "low"

        # Determine if confirms whale direction
        confirms = (self._live_state['last_whale_direction'] == whale_direction) and is_whale

        # Create event
        event = WhaleBarEvent(
            bar_index=bar_index,
            is_whale_bar=is_whale,
            whale_volume=bar['volume'] if is_whale else 0.0,
            direction=direction,
            at_accumulation_zone=at_zone,
            zone_reference=zone_ref,
            confirms_whale_direction=confirms,
            is_exhaustion_bar=is_exhaustion,
            exhaustion_at=exhaustion_at,
            cumulative_delta=self._live_state['cumulative_whale_delta'],
            timestamp=str(bar_index)
        )

        self._live_state['last_update_idx'] = bar_index

        return event if is_whale else None

    # ========================================================================
    # 9. LIVE ALERTS - DETECT REAL-TIME SIGNALS
    # ========================================================================

    def get_live_alerts(self) -> List[WhaleAlert]:
        """
        Returns list of currently active whale alerts.
        Enhanced: includes real-time UW flow alerts (sweeps, blocks).

        Alert types:
        - WHALE_ENTRY: 3+ consecutive whale bars same direction OR UW sweep cluster
        - WHALE_EXIT: Distribution after accumulation
        - WHALE_EXHAUSTION: Volume declining at extremes
        - WHALE_ABSORPTION: Price flat despite massive volume
        - WHALE_DIVERGENCE: Whale flow vs price divergence
        - WHALE_TRAP: Sweep + immediate whale reversal

        Returns:
            List of WhaleAlert objects
        """
        alerts = []

        # === REAL DATA: Generate alerts from UW flow ===
        if self.has_real_data:
            self._refresh_uw_data()
            flow_alerts = self._uw_cache.get('flow_alerts') or []
            whale_live = self._uw_cache.get('whale_signal_live')

            # Sweep cluster = WHALE_ENTRY
            sweeps = [a for a in flow_alerts if a.has_sweep]
            if len(sweeps) >= 3:
                total_premium = sum(a.total_premium for a in sweeps)
                call_sweeps = len([s for s in sweeps if s.option_type == 'call'])
                put_sweeps = len(sweeps) - call_sweeps
                direction = "BUY" if call_sweeps > put_sweeps else "SELL"

                alerts.append(WhaleAlert(
                    alert_type=AlertType.WHALE_ENTRY.value,
                    direction=direction,
                    confidence=min(100, 70 + len(sweeps) * 5),
                    price_level=sweeps[0].underlying_price if sweeps else 0,
                    bars_active=len(sweeps),
                    description=f"UW: {len(sweeps)} sweeps detected (${total_premium:,.0f} premium, "
                               f"{call_sweeps}C/{put_sweeps}P)",
                    trade_signal="Strong" if len(sweeps) >= 5 else "Moderate",
                    timestamp=str(self._live_state['last_update_idx'])
                ))

            # Divergence: UW direction vs price
            if whale_live and whale_live.confidence > 60:
                price_dir = self._live_state.get('last_whale_direction', 0)
                uw_dir = 1 if whale_live.direction == "BULLISH" else (-1 if whale_live.direction == "BEARISH" else 0)
                if price_dir != 0 and uw_dir != 0 and price_dir != uw_dir:
                    alerts.append(WhaleAlert(
                        alert_type=AlertType.WHALE_DIVERGENCE.value,
                        direction="BUY" if uw_dir == 1 else "SELL",
                        confidence=whale_live.confidence,
                        price_level=0.0,
                        bars_active=1,
                        description=f"UW flow ({whale_live.direction}) diverges from price action",
                        trade_signal="Strong",
                        timestamp=str(self._live_state['last_update_idx'])
                    ))

        # Alert 1: WHALE_ENTRY
        if self._live_state['consecutive_whale_bars'] >= 3:
            direction = self._live_state['last_whale_direction']
            direction_str = "BUY" if direction == 1 else "SELL"

            entry_alert = WhaleAlert(
                alert_type=AlertType.WHALE_ENTRY.value,
                direction=direction_str,
                confidence=min(100, 60 + self._live_state['consecutive_whale_bars'] * 10),
                price_level=0.0,  # Would be set from current price
                bars_active=self._live_state['consecutive_whale_bars'],
                description=f"Large player opening {direction_str} position ({self._live_state['consecutive_whale_bars']} consecutive whale bars)",
                trade_signal="Strong",
                timestamp=str(self._live_state['last_update_idx'])
            )
            alerts.append(entry_alert)

        # Alert 2: WHALE_DIVERGENCE
        if self._live_state['divergence_counter'] > 0:
            div_alert = WhaleAlert(
                alert_type=AlertType.WHALE_DIVERGENCE.value,
                direction="NEUTRAL",
                confidence=min(100, 50 + self._live_state['divergence_counter'] * 5),
                price_level=0.0,
                bars_active=self._live_state['divergence_counter'],
                description=f"Whale activity diverging from price direction for {self._live_state['divergence_counter']} bars",
                trade_signal="Moderate",
                timestamp=str(self._live_state['last_update_idx'])
            )
            alerts.append(div_alert)

        # Alert 3: WHALE_ABSORPTION
        if self._live_state['absorption_counter'] > 0:
            abs_alert = WhaleAlert(
                alert_type=AlertType.WHALE_ABSORPTION.value,
                direction="NEUTRAL",
                confidence=min(100, 45 + self._live_state['absorption_counter'] * 8),
                price_level=0.0,
                bars_active=self._live_state['absorption_counter'],
                description=f"Whale absorbing all volume - price not moving despite massive activity",
                trade_signal="Moderate",
                timestamp=str(self._live_state['last_update_idx'])
            )
            alerts.append(abs_alert)

        return alerts

    # ========================================================================
    # 10. WHALE HEATMAP - PRICE-VOLUME INTENSITY
    # ========================================================================

    def get_whale_heatmap(self, df: pd.DataFrame, atr_period: int = 14) -> dict:
        """
        Returns price-volume heatmap showing whale activity intensity by price level.

        Returns:
            dict mapping price levels to whale metrics:
            {
                price_level: {
                    'whale_volume': total_volume,
                    'direction_bias': buy_pct - sell_pct,
                    'recency_weight': 0-1,
                    'intensity': 0-100
                }
            }
        """
        if len(df) < 10:
            return {}

        df_recent = df.tail(50).copy()
        df_recent = df_recent.reset_index(drop=True)
        atr = self._calculate_atr(df_recent)
        is_whale = self._is_whale_volume(df_recent)
        direction = self._bar_direction(df_recent)

        # Create price buckets (0.25 ATR width)
        atr_val = atr.iloc[-1] if not np.isnan(atr.iloc[-1]) else 0.5
        bucket_width = atr_val * 0.25

        heatmap = {}

        whale_indices = np.where(is_whale)[0]
        for i in whale_indices:
            row = df_recent.iloc[i]
            # Get bar midpoint
            mid = (row['high'] + row['low']) / 2

            # Find bucket
            bucket_level = round(mid / bucket_width) * bucket_width

            if bucket_level not in heatmap:
                heatmap[bucket_level] = {
                    'whale_volume': 0.0,
                    'buy_volume': 0.0,
                    'sell_volume': 0.0,
                    'touches': 0,
                    'last_idx': i
                }

            heatmap[bucket_level]['whale_volume'] += row['volume']
            heatmap[bucket_level]['touches'] += 1
            heatmap[bucket_level]['last_idx'] = i

            # Direction
            if direction.iloc[i] == 1:
                heatmap[bucket_level]['buy_volume'] += row['volume']
            else:
                heatmap[bucket_level]['sell_volume'] += row['volume']

        # Add recency and intensity
        max_vol = max([h['whale_volume'] for h in heatmap.values()]) if heatmap else 1
        current_idx = len(df_recent) - 1

        for level, data in heatmap.items():
            recency = 1 - ((current_idx - data['last_idx']) / len(df_recent))
            data['recency_weight'] = max(0, recency)

            total_vol = data['buy_volume'] + data['sell_volume']
            if total_vol > 0:
                data['direction_bias'] = (data['buy_volume'] - data['sell_volume']) / total_vol
            else:
                data['direction_bias'] = 0.0

            # Intensity score: volume * recency * touches
            intensity = (data['whale_volume'] / max_vol) * 50
            intensity += data['recency_weight'] * 30
            intensity += min(data['touches'] / 5, 1) * 20
            data['intensity'] = min(100, intensity)

        return heatmap

    # ========================================================================
    # 11. WHALE POSITION TRACKING
    # ========================================================================

    def track_whale_positions(self, df: pd.DataFrame, lookback: int = 50) -> WhalePosition:
        """
        Estimate large player net position using whale delta accumulation.

        Returns:
            WhalePosition with direction, size estimate, and loading rate
        """
        if len(df) < lookback:
            lookback = len(df)

        df_analysis = df.tail(lookback).copy()
        df_analysis = df_analysis.reset_index(drop=True)

        is_whale = self._is_whale_volume(df_analysis)
        direction = self._bar_direction(df_analysis)

        # Calculate whale delta
        whale_buy = np.where((is_whale) & (direction == 1), df_analysis['volume'], 0)
        whale_sell = np.where((is_whale) & (direction == -1), df_analysis['volume'], 0)

        cumulative_delta = whale_buy.sum() - whale_sell.sum()

        # Direction
        if cumulative_delta > 0:
            pos_direction = "LONG"
        elif cumulative_delta < 0:
            pos_direction = "SHORT"
        else:
            pos_direction = "FLAT"

        # Size estimate (% of total volume)
        total_vol = df_analysis['volume'].sum()
        whale_vol = (whale_buy.sum() + whale_sell.sum())
        size_pct = (whale_vol / total_vol * 100) if total_vol > 0 else 0

        # Loading rate
        first_half_delta = whale_buy[:len(whale_buy)//2].sum() - whale_sell[:len(whale_sell)//2].sum()
        second_half_delta = whale_buy[len(whale_buy)//2:].sum() - whale_sell[len(whale_sell)//2:].sum()

        if first_half_delta != 0:
            loading_rate = abs(second_half_delta - first_half_delta) / abs(first_half_delta)
        else:
            loading_rate = 0

        # Inventory change
        if abs(cumulative_delta) > total_vol * 0.1 and second_half_delta > first_half_delta:
            inv_change = "loading"
        elif abs(cumulative_delta) > total_vol * 0.1 and second_half_delta < first_half_delta:
            inv_change = "reducing"
        elif abs(second_half_delta - first_half_delta) < total_vol * 0.05:
            inv_change = "flat"
        else:
            inv_change = "flipping"

        # Confidence
        whale_bar_count = is_whale.sum()
        if whale_bar_count >= lookback * 0.15:
            confidence = min(100, 50 + whale_bar_count * 2)
        else:
            confidence = whale_bar_count * 5

        return WhalePosition(
            direction=pos_direction,
            estimated_size_pct=min(100, size_pct),
            loading_rate=loading_rate,
            inventory_change=inv_change,
            confidence=confidence,
            bars_in_position=lookback
        )

    # ========================================================================
    # 12. WHALE TRAP DETECTION
    # ========================================================================

    def detect_whale_traps(self, df: pd.DataFrame, lookback: int = 30) -> List[WhaleTrap]:
        """
        Detect "sweep and trade" patterns - liquidity sweep followed by whale entry.
        Classic institutional stop hunt then trade.

        Returns:
            List of WhaleTrap detections
        """
        if len(df) < lookback:
            lookback = len(df)

        df_analysis = df.tail(lookback).copy()
        df_analysis = df_analysis.reset_index(drop=True)

        is_whale = self._is_whale_volume(df_analysis)
        direction = self._bar_direction(df_analysis)

        traps = []

        # Find potential sweep points (new high/low on volume)
        for i in range(1, len(df_analysis) - 1):
            recent_high = df_analysis['high'].iloc[:i].max()
            recent_low = df_analysis['low'].iloc[:i].min()

            current_high = df_analysis['high'].iloc[i]
            current_low = df_analysis['low'].iloc[i]
            current_vol = df_analysis['volume'].iloc[i]

            # Is this a potential sweep? (new extreme)
            is_high_sweep = current_high > recent_high
            is_low_sweep = current_low < recent_low

            if not (is_high_sweep or is_low_sweep):
                continue

            # Look ahead for whale bar in opposite direction (trap reversal)
            for j in range(i + 1, min(i + 4, len(df_analysis))):
                if is_whale.iloc[j]:
                    whale_direction = direction.iloc[j]

                    # Should be reversal from sweep
                    if is_high_sweep and whale_direction == -1:
                        # Swept high, whale came in to sell
                        trap = WhaleTrap(
                            sweep_price=current_high,
                            trap_direction="SELL",
                            whale_bar_idx=j,
                            volume=df_analysis['volume'].iloc[j],
                            trap_quality=min(100, 60 + (j - i) * 10),
                            bars_after_sweep=j - i
                        )
                        traps.append(trap)
                        break

                    elif is_low_sweep and whale_direction == 1:
                        # Swept low, whale came in to buy
                        trap = WhaleTrap(
                            sweep_price=current_low,
                            trap_direction="BUY",
                            whale_bar_idx=j,
                            volume=df_analysis['volume'].iloc[j],
                            trap_quality=min(100, 60 + (j - i) * 10),
                            bars_after_sweep=j - i
                        )
                        traps.append(trap)
                        break

        # Sort by quality
        traps.sort(key=lambda x: x.trap_quality, reverse=True)

        return traps

    # ========================================================================
    # 13. WHALE VS RETAIL DIVERGENCE
    # ========================================================================

    def whale_vs_retail_divergence(self, df: pd.DataFrame, lookback: int = 20) -> DivergenceState:
        """
        Compare whale volume direction vs overall price direction.
        Detects when whales and retail are in disagreement.

        Returns:
            DivergenceState showing direction and strength of divergence
        """
        if len(df) < lookback:
            lookback = len(df)

        df_analysis = df.tail(lookback).copy()
        df_analysis = df_analysis.reset_index(drop=True)

        is_whale = self._is_whale_volume(df_analysis)
        direction = self._bar_direction(df_analysis)

        # Whale direction
        whale_buy = np.where((is_whale) & (direction == 1), df_analysis['volume'], 0).sum()
        whale_sell = np.where((is_whale) & (direction == -1), df_analysis['volume'], 0).sum()

        if whale_buy > whale_sell:
            whale_dir = "BUY"
        elif whale_sell > whale_buy:
            whale_dir = "SELL"
        else:
            whale_dir = "NEUTRAL"

        # Retail direction (total volume minus whale)
        retail_buy = np.where((~is_whale) & (direction == 1), df_analysis['volume'], 0).sum()
        retail_sell = np.where((~is_whale) & (direction == -1), df_analysis['volume'], 0).sum()

        if retail_buy > retail_sell:
            retail_dir = "BUY"
        elif retail_sell > retail_buy:
            retail_dir = "SELL"
        else:
            retail_dir = "NEUTRAL"

        # Count bars of divergence
        div_count = 0
        for i in range(len(df_analysis)):
            w_dir = 1 if (is_whale.iloc[i] and direction.iloc[i] == 1) else (-1 if (is_whale.iloc[i] and direction.iloc[i] == -1) else 0)
            r_dir = 1 if ((~is_whale).iloc[i] and direction.iloc[i] == 1) else (-1 if ((~is_whale).iloc[i] and direction.iloc[i] == -1) else 0)

            if w_dir != 0 and r_dir != 0 and w_dir != r_dir:
                div_count += 1

        # Divergence strength
        if whale_dir != retail_dir and whale_dir != "NEUTRAL" and retail_dir != "NEUTRAL":
            is_active = True
            strength = min(100, 50 + div_count * 3)

            # Trade signal
            if whale_dir == "BUY" and retail_dir == "SELL":
                signal = "Strong bullish divergence - whales buying panic (BUY)"
            elif whale_dir == "SELL" and retail_dir == "BUY":
                signal = "Strong bearish divergence - whales distributing into retail (SELL)"
            else:
                signal = f"Divergence: Whales {whale_dir} vs Retail {retail_dir}"
        else:
            is_active = False
            strength = 0
            signal = "No significant divergence"

        return DivergenceState(
            active=is_active,
            whale_direction=whale_dir,
            retail_direction=retail_dir,
            bars_diverging=div_count,
            strength=strength,
            trade_signal=signal
        )

    # ========================================================================
    # 14. ENHANCED WHALE SIGNAL WITH LIVE STATE
    # ========================================================================

    def get_whale_signal_live(self, df: pd.DataFrame,
                              price_level: Optional[float] = None) -> WhaleSignal:
        """
        Enhanced get_whale_signal that incorporates live state tracking.

        Returns:
            WhaleSignal with improved accuracy from live tracking
        """
        # Get base signal
        signal = self.get_whale_signal(df, price_level)

        # Incorporate live state
        if self._live_state['consecutive_whale_bars'] >= 3:
            # Strong entry signal
            if self._live_state['last_whale_direction'] == 1:
                signal.direction = "WHALE_BUY"
            else:
                signal.direction = "WHALE_SELL"
            signal.confidence = min(100, signal.confidence + 15)

        # Adjust for divergence
        if self._live_state['divergence_counter'] > 2:
            signal.confidence = max(0, signal.confidence - 10)

        return signal


# ============================================================================
# DEMO
# ============================================================================

if __name__ == "__main__":
    # Generate sample OHLCV data
    np.random.seed(42)
    n_bars = 200

    base_price = 100
    prices = [base_price]
    volumes = []

    for i in range(n_bars):
        # Add some trend and volatility
        if i < 50:
            trend = 0.3
        elif i < 100:
            trend = 0.1
        elif i < 150:
            trend = -0.2
        else:
            trend = 0.15

        change = np.random.normal(trend, 1.0)
        prices.append(prices[-1] + change)

        # Volume with whale activity
        base_vol = np.random.uniform(1000, 2000)
        if i % 15 == 0:  # Whale bars
            base_vol *= np.random.uniform(3, 5)
        volumes.append(base_vol)

    # Create OHLCV dataframe
    closes = np.array(prices[1:])
    highs = closes + np.abs(np.random.normal(0.5, 0.3, n_bars))
    lows = closes - np.abs(np.random.normal(0.5, 0.3, n_bars))
    opens = closes + np.random.normal(0, 0.5, n_bars)

    df = pd.DataFrame({
        'open': opens,
        'high': np.maximum(highs, np.maximum(opens, closes)),
        'low': np.minimum(lows, np.minimum(opens, closes)),
        'close': closes,
        'volume': volumes
    })

    # Run whale tracker
    tracker = WhaleTracker(volume_threshold_percentile=75)

    print("\n" + "="*70)
    print("WHALE TRACKER ANALYSIS")
    print("="*70)

    # 1. Accumulation Zones
    print("\n1. ACCUMULATION ZONES")
    print("-" * 70)
    zones = tracker.detect_accumulation_zones(df)
    for i, zone in enumerate(zones[:3]):
        print(f"Zone {i+1}: {zone}")

    # 2. Distribution
    print("\n2. DISTRIBUTION SIGNAL")
    print("-" * 70)
    dist = tracker.detect_distribution(df)
    print(f"Active: {dist.active} | Strength: {dist.strength:.0f} | Urgency: {dist.exit_urgency}")
    if dist.evidence:
        for ev in dist.evidence:
            print(f"  - {ev}")

    # 3. Volume Profile
    print("\n3. VOLUME PROFILE")
    print("-" * 70)
    vp = tracker.build_volume_profile(df)
    print(f"POC: {vp.poc:.2f} | VAH: {vp.vah:.2f} | VAL: {vp.val:.2f}")
    print(f"HVN Levels: {[f'{x:.2f}' for x in vp.hvn_levels]}")
    print(f"LVN Levels: {[f'{x:.2f}' for x in vp.lvn_levels]}")

    # 4. VWAP
    print("\n4. VWAP ANALYSIS")
    print("-" * 70)
    vwap = tracker.anchored_vwap(df)
    print(f"VWAP: {vwap.vwap:.2f} | Deviation: {vwap.deviation_pct:.2f}%")
    print(f"Bands: [{vwap.lower_band_2:.2f}, {vwap.lower_band_1:.2f}] - "
          f"[{vwap.upper_band_1:.2f}, {vwap.upper_band_2:.2f}]")
    print(f"Position: {vwap.band_position}")

    # 5. Iceberg Orders
    print("\n5. ICEBERG ORDERS")
    print("-" * 70)
    icebergs = tracker.detect_iceberg_orders(df)
    for i, iceberg in enumerate(icebergs[:2]):
        print(f"Iceberg {i+1}: {iceberg.direction} @ {iceberg.price_level:.2f} | "
              f"Est Vol: {iceberg.total_estimated_volume:.0f} | "
              f"Touches: {iceberg.num_touches} | Confidence: {iceberg.confidence:.0f}%")

    # 6. Whale Momentum
    print("\n6. WHALE MOMENTUM")
    print("-" * 70)
    momentum = tracker.whale_momentum(df)
    print(f"Net Flow: {momentum.net_flow:.0f} | Direction: {momentum.flow_direction}")
    print(f"Flow Strength: {momentum.flow_strength:.0f}% | Conviction: {momentum.conviction_score:.0f}%")
    print(f"Divergence: {momentum.divergence} | Exhaustion: {momentum.exhaustion_flag}")

    # 7. Composite Signal
    print("\n7. COMPOSITE WHALE SIGNAL")
    print("-" * 70)
    signal = tracker.get_whale_signal(df)
    print(f"Direction: {signal.direction} | Confidence: {signal.confidence:.0f}%")
    print(f"Summary: {signal.key_summary}")
    if signal.nearby_zones:
        print(f"Nearby Zones: {len(signal.nearby_zones)}")
        for z in signal.nearby_zones[:2]:
            print(f"  {z}")

    # ========================================================================
    # LIVE TRACKING DEMO
    # ========================================================================

    print("\n" + "="*70)
    print("LIVE TRACKING - PER-BAR UPDATES")
    print("="*70)

    # Simulate live bar updates
    print("\n8. LIVE BAR UPDATES (simulating last 10 bars)")
    print("-" * 70)
    event_count = 0
    for i in range(max(0, len(df) - 10), len(df)):
        bar = df.iloc[i]
        event = tracker.update_bar(bar, bar_index=i, df=df)
        if event:
            print(f"Bar {i}: WHALE EVENT")
            print(f"  Direction: {event.direction} | Volume: {event.whale_volume:.0f}")
            print(f"  At Zone: {event.at_accumulation_zone} | Exhaustion: {event.is_exhaustion_bar}")
            print(f"  Cumulative Delta: {event.cumulative_delta:.0f}")
            event_count += 1
    if event_count == 0:
        print("(No whale events in last 10 bars)")

    # 9. Live Alerts
    print("\n9. LIVE ALERTS")
    print("-" * 70)
    alerts = tracker.get_live_alerts()
    if alerts:
        for alert in alerts:
            print(f"[{alert.alert_type}] {alert.direction}")
            print(f"  Confidence: {alert.confidence:.0f}% | Bars Active: {alert.bars_active}")
            print(f"  {alert.description}")
            print(f"  Signal: {alert.trade_signal}")
    else:
        print("(No active alerts)")

    # 10. Whale Heatmap
    print("\n10. WHALE HEATMAP (price-volume intensity)")
    print("-" * 70)
    heatmap = tracker.get_whale_heatmap(df)
    if heatmap:
        # Sort by intensity
        sorted_levels = sorted(heatmap.items(), key=lambda x: x[1]['intensity'], reverse=True)
        for level, data in sorted_levels[:5]:
            print(f"Price {level:.2f}: Volume={data['whale_volume']:.0f} | "
                  f"Intensity={data['intensity']:.0f} | Bias={data['direction_bias']:.2f}")
    else:
        print("(No heatmap data)")

    # 11. Whale Position Tracking
    print("\n11. WHALE POSITION TRACKING")
    print("-" * 70)
    position = tracker.track_whale_positions(df)
    print(f"Direction: {position.direction} | Size: {position.estimated_size_pct:.1f}%")
    print(f"Loading Rate: {position.loading_rate:.2f} | Inventory: {position.inventory_change}")
    print(f"Confidence: {position.confidence:.0f}%")

    # 12. Whale Traps
    print("\n12. WHALE TRAP DETECTION (stop hunts)")
    print("-" * 70)
    traps = tracker.detect_whale_traps(df)
    if traps:
        for i, trap in enumerate(traps[:3]):
            print(f"Trap {i+1}: {trap.trap_direction} at {trap.sweep_price:.2f}")
            print(f"  Quality: {trap.trap_quality:.0f}% | Bars After Sweep: {trap.bars_after_sweep}")
            print(f"  Whale Volume: {trap.volume:.0f}")
    else:
        print("(No traps detected)")

    # 13. Whale vs Retail Divergence
    print("\n13. WHALE VS RETAIL DIVERGENCE")
    print("-" * 70)
    divergence = tracker.whale_vs_retail_divergence(df)
    print(f"Active: {divergence.active}")
    print(f"Whale: {divergence.whale_direction} | Retail: {divergence.retail_direction}")
    print(f"Strength: {divergence.strength:.0f}% | Bars Diverging: {divergence.bars_diverging}")
    print(f"Signal: {divergence.trade_signal}")

    # 14. Enhanced Live Signal
    print("\n14. ENHANCED WHALE SIGNAL (with live state)")
    print("-" * 70)
    live_signal = tracker.get_whale_signal_live(df)
    print(f"Direction: {live_signal.direction} | Confidence: {live_signal.confidence:.0f}%")
    print(f"Summary: {live_signal.key_summary}")

    print("\n" + "="*70)
