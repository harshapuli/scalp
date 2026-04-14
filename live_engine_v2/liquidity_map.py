"""
Institutional Liquidity Mapping Engine for ICT/SMC Options Trading

Maps where institutions target liquidity pools (stop loss clusters) to fill large orders.
Detects sweeps, imbalances, institutional levels, and provides draw-on-liquidity targets.

Author: ICT/SMC Trading System
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import List, Tuple, Optional
from datetime import datetime, timedelta
import numpy as np
import pandas as pd


class PoolSide(Enum):
    """Liquidity pool side classification."""
    BUY_SIDE = "BSL"      # Above swing highs, shorts have stops here
    SELL_SIDE = "SSL"     # Below swing lows, longs have stops here


class PoolStatus(Enum):
    """Liquidity pool sweep status."""
    UNTAPPED = "UNTAPPED"
    PARTIALLY_SWEPT = "PARTIALLY_SWEPT"
    FULLY_SWEPT = "FULLY_SWEPT"


class SweepQuality(Enum):
    """Institutional sweep quality classification."""
    CLEAN = "CLEAN"           # Wick only penetration
    MESSY = "MESSY"           # 2+ bars beyond pool
    FAILED = "FAILED"         # No reversal


class LiquidityDirection(Enum):
    """Liquidity void direction bias."""
    BULLISH = "BULLISH"       # Void above current price (upside draw)
    BEARISH = "BEARISH"       # Void below current price (downside draw)


class InstitutionalLevelType(Enum):
    """Types of institutional liquidity levels."""
    PDH = "PDH"               # Previous day high
    PDL = "PDL"               # Previous day low
    PDC = "PDC"               # Previous day close
    PWH = "PWH"               # Previous week high
    PWL = "PWL"               # Previous week low
    PMH = "PMH"               # Previous month high
    PML = "PML"               # Previous month low
    ROUND_NUMBER = "ROUND"    # Psychological level ($100, $150, etc.)
    VWAP_DAILY = "VWAP_D"     # Daily VWAP
    VWAP_WEEKLY = "VWAP_W"    # Weekly VWAP
    NY_OPEN = "NY_OPEN"       # New York session open
    LONDON_OPEN = "LON_OPEN"  # London session open


@dataclass
class LiquidityPool:
    """Represents a cluster of stop losses (institutional liquidity)."""
    price: float
    side: PoolSide
    strength: float                    # 0-100 score
    status: PoolStatus
    formation_bars: int                # Bars since pool formed
    last_test: int                     # Bars since last touched
    estimated_volume: float            # Estimated stops at level
    touches: int = 0                   # Number of times tested
    created_bar: Optional[int] = None  # Bar index when formed

    def __hash__(self):
        return hash((round(self.price, 2), self.side.value))


@dataclass
class LiquiditySweep:
    """Institutional stop hunt sweep event."""
    pool: LiquidityPool
    sweep_bar: int                     # Bar index when sweep occurred
    depth_ticks: float                 # How far beyond pool was reached
    quality: SweepQuality
    displacement_after: float          # Institutional entry displacement (pips)
    reversal_confirmed: bool           # Did price reverse after sweep?
    reversal_bars: int = 0             # Bars to confirm reversal

    def __hash__(self):
        return hash(self.pool)


@dataclass
class LiquidityVoid:
    """Fair Value Gap or imbalance area (liquidity magnet)."""
    high: float
    low: float
    direction: LiquidityDirection
    fill_pct: float                    # 0-100% filled
    age_bars: int                      # Bars since formation
    is_draw_target: bool               # Is price heading here?
    size_ticks: float = field(init=False)

    def __post_init__(self):
        self.size_ticks = abs(self.high - self.low)

    def __hash__(self):
        return hash((round(self.high, 2), round(self.low, 2)))


@dataclass
class InstitutionalLevel:
    """Key institutional liquidity level."""
    price: float
    level_type: InstitutionalLevelType
    strength: float                    # 0-100 score
    times_tested: int
    last_test: int                     # Bars since last tested
    test_volume_avg: float = 0.0       # Average volume at tests


@dataclass
class JudasSwing:
    """ICT Judas Swing pattern (false break + reversal)."""
    fake_direction: str                # "UP" or "DOWN"
    reversal_bar: int
    pool_swept: Optional[LiquidityPool]
    displacement_confirmed: bool
    entry_zone_high: float
    entry_zone_low: float
    confidence: float                  # 0-100


@dataclass
class DrawOnLiquidity:
    """Predicted institutional targets (where price is heading)."""
    upside_targets: List[float]        # Sorted by proximity
    downside_targets: List[float]
    primary_draw: Optional[float]      # Nearest weighted target
    bias: str                          # "BULLISH", "BEARISH", "NEUTRAL"
    confidence: float                  # 0-100 based on alignment


@dataclass
class LiquidityState:
    """Complete real-time liquidity map snapshot."""
    bsl_pools: List[LiquidityPool]
    ssl_pools: List[LiquidityPool]
    voids: List[LiquidityVoid]
    recent_sweeps: List[LiquiditySweep]
    institutional_levels: List[InstitutionalLevel]
    draw_on_liquidity: DrawOnLiquidity
    summary: str
    timestamp: datetime = field(default_factory=datetime.now)


class LiquidityMapper:
    """
    Institutional liquidity mapping engine.

    Maps institutional order clusters, stop hunts, and predicts
    draw-on-liquidity targets using ICT/SMC methodologies.
    """

    def __init__(self, symbol: str = "SPY", tick_size: float = 0.01):
        self.symbol = symbol
        self.tick_size = tick_size
        self.pools: List[LiquidityPool] = []
        self.sweeps: List[LiquiditySweep] = []
        self.voids: List[LiquidityVoid] = []
        self.levels: List[InstitutionalLevel] = []

    def map_stop_clusters(
        self,
        df: pd.DataFrame,
        lookback: int = 100
    ) -> List[LiquidityPool]:
        """
        Detect stop loss clusters (liquidity pools).

        BSL: Above swing highs (shorts have stops above highs)
        SSL: Below swing lows (longs have stops below lows)

        Args:
            df: OHLCV dataframe with columns: open, high, low, close, volume
            lookback: Number of bars to analyze

        Returns:
            List of LiquidityPool objects
        """
        df = df.tail(lookback).reset_index(drop=True)
        pools = []

        # Find swing highs and lows
        swing_highs = self._find_swing_highs(df, window=3)
        swing_lows = self._find_swing_lows(df, window=3)

        # Map buy-side liquidity (above swing highs)
        for idx, high in swing_highs:
            pool_price = high + (2 * self.tick_size)  # Slightly above
            volume_at_level = self._estimate_volume_at_level(df, pool_price, window=5)

            touches = len([h for _, h in swing_highs if abs(h - pool_price) < 0.05])
            bars_since = len(df) - idx - 1

            strength = self._calculate_pool_strength(
                touches, volume_at_level, bars_since
            )

            status = self._classify_pool_status(df, pool_price, PoolSide.BUY_SIDE)

            pool = LiquidityPool(
                price=pool_price,
                side=PoolSide.BUY_SIDE,
                strength=strength,
                status=status,
                formation_bars=bars_since,
                last_test=self._bars_since_last_test(df, pool_price),
                estimated_volume=volume_at_level,
                touches=touches,
                created_bar=idx
            )
            pools.append(pool)

        # Map sell-side liquidity (below swing lows)
        for idx, low in swing_lows:
            pool_price = low - (2 * self.tick_size)  # Slightly below
            volume_at_level = self._estimate_volume_at_level(df, pool_price, window=5)

            touches = len([l for _, l in swing_lows if abs(l - pool_price) < 0.05])
            bars_since = len(df) - idx - 1

            strength = self._calculate_pool_strength(
                touches, volume_at_level, bars_since
            )

            status = self._classify_pool_status(df, pool_price, PoolSide.SELL_SIDE)

            pool = LiquidityPool(
                price=pool_price,
                side=PoolSide.SELL_SIDE,
                strength=strength,
                status=status,
                formation_bars=bars_since,
                last_test=self._bars_since_last_test(df, pool_price),
                estimated_volume=volume_at_level,
                touches=touches,
                created_bar=idx
            )
            pools.append(pool)

        # Merge overlapping pools
        self.pools = self._merge_overlapping_pools(pools, threshold=0.1)
        return self.pools

    def detect_sweep(self, df: pd.DataFrame, pools: List[LiquidityPool]) -> List[LiquiditySweep]:
        """
        Detect institutional stop hunts (sweep events).

        Sweeps occur when price breaks through a liquidity pool
        then reverses, indicating institutions grabbed liquidity.

        Args:
            df: OHLCV dataframe
            pools: List of LiquidityPool objects

        Returns:
            List of LiquiditySweep objects
        """
        sweeps = []
        df = df.reset_index(drop=True)

        for pool in pools:
            # Check recent bars for sweep signature
            recent_idx = max(0, len(df) - 20)
            recent = df.iloc[recent_idx:]

            sweep_idx = self._find_sweep_bar(recent, pool.price, pool.side)
            if sweep_idx is None:
                continue

            abs_idx = recent_idx + sweep_idx
            sweep_bar = df.iloc[abs_idx]

            # Calculate sweep depth
            if pool.side == PoolSide.BUY_SIDE:
                depth_ticks = (sweep_bar['high'] - pool.price) / self.tick_size
                reversal = self._check_reversal_down(df, abs_idx, window=5)
            else:
                depth_ticks = (pool.price - sweep_bar['low']) / self.tick_size
                reversal = self._check_reversal_up(df, abs_idx, window=5)

            # Classify sweep quality
            quality = self._classify_sweep_quality(df, abs_idx, pool.price, depth_ticks)

            # Measure displacement
            displacement = self._measure_displacement(df, abs_idx, pool.side, bars=3)

            sweep = LiquiditySweep(
                pool=pool,
                sweep_bar=abs_idx,
                depth_ticks=depth_ticks,
                quality=quality,
                displacement_after=displacement,
                reversal_confirmed=reversal,
                reversal_bars=self._bars_to_reversal(df, abs_idx, pool.side)
            )
            sweeps.append(sweep)

        self.sweeps = sweeps
        return sweeps

    def detect_liquidity_voids(self, df: pd.DataFrame) -> List[LiquidityVoid]:
        """
        Detect Fair Value Gaps (FVGs) and liquidity voids.

        Unfilled gaps are magnets that price returns to fill.

        Args:
            df: OHLCV dataframe

        Returns:
            List of LiquidityVoid objects
        """
        voids = []
        df = df.reset_index(drop=True)

        for i in range(2, len(df)):
            prev2 = df.iloc[i - 2]
            prev1 = df.iloc[i - 1]
            curr = df.iloc[i]

            # Up FVG: curr low > prev1 high > prev2 high
            if (curr['low'] > prev1['high'] > prev2['high']):
                void = LiquidityVoid(
                    high=curr['low'],
                    low=prev1['high'],
                    direction=LiquidityDirection.BULLISH,
                    fill_pct=self._calculate_void_fill_pct(df, i, curr['low'], prev1['high']),
                    age_bars=len(df) - i - 1,
                    is_draw_target=True
                )
                voids.append(void)

            # Down FVG: curr high < prev1 low < prev2 low
            elif (curr['high'] < prev1['low'] < prev2['low']):
                void = LiquidityVoid(
                    high=prev1['low'],
                    low=curr['high'],
                    direction=LiquidityDirection.BEARISH,
                    fill_pct=self._calculate_void_fill_pct(df, i, prev1['low'], curr['high']),
                    age_bars=len(df) - i - 1,
                    is_draw_target=True
                )
                voids.append(void)

        # Remove filled voids
        self.voids = [v for v in voids if v.fill_pct < 100]
        return self.voids

    def map_institutional_levels(self, df: pd.DataFrame) -> List[InstitutionalLevel]:
        """
        Map institutional liquidity levels.

        Session opens, previous session levels (PDH/PDL/PDC),
        VWAP levels, and round numbers.

        Args:
            df: OHLCV dataframe

        Returns:
            List of InstitutionalLevel objects
        """
        levels = []
        df = df.reset_index(drop=True)
        current_price = df.iloc[-1]['close']

        # Previous Day levels
        if len(df) >= 24:
            prev_day = df.iloc[-24:-1]
            pdh = prev_day['high'].max()
            pdl = prev_day['low'].min()
            pdc = prev_day['close'].iloc[-1]

            for price, ltype in [(pdh, InstitutionalLevelType.PDH),
                                   (pdl, InstitutionalLevelType.PDL),
                                   (pdc, InstitutionalLevelType.PDC)]:
                times_tested = len([1 for h in df['high'] if abs(h - price) < 0.05])
                strength = min(100, 50 + (times_tested * 10))

                level = InstitutionalLevel(
                    price=price,
                    level_type=ltype,
                    strength=strength,
                    times_tested=times_tested,
                    last_test=self._bars_since_last_test(df, price)
                )
                levels.append(level)

        # VWAP levels
        daily_vwap = self._calculate_vwap(df)
        level = InstitutionalLevel(
            price=daily_vwap,
            level_type=InstitutionalLevelType.VWAP_DAILY,
            strength=60,
            times_tested=1,
            last_test=0
        )
        levels.append(level)

        # Round numbers
        round_base = int(current_price / 5) * 5
        for offset in [0, 5, -5, 10, -10]:
            round_price = round_base + offset
            if abs(round_price - current_price) > 0.5:
                level = InstitutionalLevel(
                    price=float(round_price),
                    level_type=InstitutionalLevelType.ROUND_NUMBER,
                    strength=40,
                    times_tested=0,
                    last_test=0
                )
                levels.append(level)

        self.levels = levels
        return levels

    def detect_judas_swing(
        self,
        df: pd.DataFrame,
        session: str = 'NY'
    ) -> Optional[JudasSwing]:
        """
        Detect ICT Judas Swing pattern.

        False break into liquidity, then strong reversal.

        Args:
            df: OHLCV dataframe
            session: Trading session ('NY', 'LON', 'ASIA')

        Returns:
            JudasSwing object or None if not detected
        """
        df = df.tail(50).reset_index(drop=True)

        if len(df) < 10:
            return None

        # Look for recent swing extremes
        recent_high_idx = df['high'].iloc[-20:].idxmax() + (len(df) - 20)
        recent_low_idx = df['low'].iloc[-20:].idxmin() + (len(df) - 20)

        # Check for false break pattern (up then down, or down then up)
        for fake_idx in [recent_high_idx, recent_low_idx]:
            if fake_idx < 5 or fake_idx > len(df) - 3:
                continue

            fake_bar = df.iloc[fake_idx]
            reversal_idx = self._find_reversal_after(df, fake_idx, window=5)

            if reversal_idx is None:
                continue

            fake_direction = "UP" if fake_idx == recent_high_idx else "DOWN"

            # Find if sweep hit a liquidity pool
            sweep_pool = self._find_pool_at_level(
                fake_bar['high'] if fake_direction == "UP" else fake_bar['low']
            )

            # Check displacement
            displacement = self._measure_displacement(
                df, fake_idx,
                PoolSide.BUY_SIDE if fake_direction == "UP" else PoolSide.SELL_SIDE,
                bars=3
            )

            entry_zone_high = max(
                df.iloc[fake_idx:reversal_idx+1]['high']
            )
            entry_zone_low = min(
                df.iloc[fake_idx:reversal_idx+1]['low']
            )

            judas = JudasSwing(
                fake_direction=fake_direction,
                reversal_bar=reversal_idx,
                pool_swept=sweep_pool,
                displacement_confirmed=displacement > 10,
                entry_zone_high=entry_zone_high,
                entry_zone_low=entry_zone_low,
                confidence=min(100, 50 + (displacement / 5))
            )

            return judas

        return None

    def compute_draw_on_liquidity(
        self,
        df: pd.DataFrame,
        pools: List[LiquidityPool],
        voids: List[LiquidityVoid]
    ) -> DrawOnLiquidity:
        """
        Compute draw on liquidity (institutional targets).

        Identifies nearest untapped pools and unfilled voids.
        Weights by proximity, strength, and market bias.

        Args:
            df: OHLCV dataframe
            pools: List of liquidity pools
            voids: List of liquidity voids

        Returns:
            DrawOnLiquidity object with targets
        """
        current_price = df.iloc[-1]['close']

        # Find untapped/partial pools
        untapped_bsl = [p for p in pools
                        if p.side == PoolSide.BUY_SIDE
                        and p.status in [PoolStatus.UNTAPPED, PoolStatus.PARTIALLY_SWEPT]]
        untapped_ssl = [p for p in pools
                        if p.side == PoolSide.SELL_SIDE
                        and p.status in [PoolStatus.UNTAPPED, PoolStatus.PARTIALLY_SWEPT]]

        # Rank by proximity and strength
        upside_targets = sorted(
            [p.price for p in untapped_bsl],
            key=lambda x: (abs(x - current_price), -x)  # Nearest, then higher
        )

        downside_targets = sorted(
            [p.price for p in untapped_ssl],
            key=lambda x: (abs(x - current_price), x)  # Nearest, then lower
        )

        # Add void targets
        void_targets = [(v.high if v.direction == LiquidityDirection.BULLISH else v.low, v.age_bars)
                        for v in voids if v.is_draw_target]

        for void_price, age in void_targets:
            weight = 1.0 / (1 + age / 10)  # Recent voids weighted higher
            if void_price > current_price:
                upside_targets.append(void_price)
            else:
                downside_targets.append(void_price)

        # Determine bias from higher-TF context
        bias = self._determine_market_bias(df)

        # Primary draw (nearest significant target)
        primary_draw = None
        if upside_targets and downside_targets:
            up_dist = upside_targets[0] - current_price
            down_dist = current_price - downside_targets[0]
            primary_draw = upside_targets[0] if up_dist < down_dist else downside_targets[0]
        elif upside_targets:
            primary_draw = upside_targets[0]
        elif downside_targets:
            primary_draw = downside_targets[0]

        # Confidence based on alignment with bias
        confidence = 60
        if bias == "BULLISH" and primary_draw and primary_draw > current_price:
            confidence = 75
        elif bias == "BEARISH" and primary_draw and primary_draw < current_price:
            confidence = 75

        return DrawOnLiquidity(
            upside_targets=upside_targets[:5],  # Top 5
            downside_targets=downside_targets[:5],
            primary_draw=primary_draw,
            bias=bias,
            confidence=confidence
        )

    def get_liquidity_state(self, df: pd.DataFrame) -> LiquidityState:
        """
        Generate complete real-time liquidity map snapshot.

        Args:
            df: OHLCV dataframe

        Returns:
            LiquidityState object with full market liquidity picture
        """
        # Refresh all analyses
        pools = self.map_stop_clusters(df, lookback=100)
        sweeps = self.detect_sweep(df, pools)
        voids = self.detect_liquidity_voids(df)
        levels = self.map_institutional_levels(df)
        dol = self.compute_draw_on_liquidity(df, pools, voids)

        bsl_pools = [p for p in pools if p.side == PoolSide.BUY_SIDE]
        ssl_pools = [p for p in pools if p.side == PoolSide.SELL_SIDE]

        # Generate summary
        summary = self._generate_summary(bsl_pools, ssl_pools, sweeps, dol)

        return LiquidityState(
            bsl_pools=bsl_pools,
            ssl_pools=ssl_pools,
            voids=voids,
            recent_sweeps=sweeps[-5:] if sweeps else [],
            institutional_levels=levels,
            draw_on_liquidity=dol,
            summary=summary
        )

    # ===== HELPER METHODS =====

    def _find_swing_highs(self, df: pd.DataFrame, window: int = 3) -> List[Tuple[int, float]]:
        """Find swing high points."""
        highs = []
        for i in range(window, len(df) - window):
            if df.iloc[i]['high'] == df.iloc[i-window:i+window+1]['high'].max():
                highs.append((i, df.iloc[i]['high']))
        return highs

    def _find_swing_lows(self, df: pd.DataFrame, window: int = 3) -> List[Tuple[int, float]]:
        """Find swing low points."""
        lows = []
        for i in range(window, len(df) - window):
            if df.iloc[i]['low'] == df.iloc[i-window:i+window+1]['low'].min():
                lows.append((i, df.iloc[i]['low']))
        return lows

    def _estimate_volume_at_level(
        self,
        df: pd.DataFrame,
        level: float,
        window: int = 5
    ) -> float:
        """Estimate volume traded at a specific price level."""
        vol_at_level = df[
            (df['high'] >= level - 0.1) & (df['low'] <= level + 0.1)
        ]['volume'].sum()
        return vol_at_level

    def _calculate_pool_strength(
        self,
        touches: int,
        volume: float,
        bars_since: int
    ) -> float:
        """Calculate liquidity pool strength (0-100)."""
        touch_score = min(50, touches * 10)
        volume_score = min(30, (volume / 1_000_000) * 10)
        recency_score = max(0, 20 - (bars_since / 10))
        return min(100, touch_score + volume_score + recency_score)

    def _classify_pool_status(
        self,
        df: pd.DataFrame,
        pool_price: float,
        side: PoolSide
    ) -> PoolStatus:
        """Classify if a pool has been swept."""
        recent = df.tail(10)

        if side == PoolSide.BUY_SIDE:
            if (recent['high'] > pool_price + 0.1).any():
                if (recent['close'] < pool_price).any():
                    return PoolStatus.PARTIALLY_SWEPT
                else:
                    return PoolStatus.FULLY_SWEPT
        else:
            if (recent['low'] < pool_price - 0.1).any():
                if (recent['close'] > pool_price).any():
                    return PoolStatus.PARTIALLY_SWEPT
                else:
                    return PoolStatus.FULLY_SWEPT

        return PoolStatus.UNTAPPED

    def _bars_since_last_test(self, df: pd.DataFrame, level: float) -> int:
        """Count bars since price last touched a level."""
        for i in range(len(df) - 1, -1, -1):
            if (df.iloc[i]['high'] >= level - 0.05) and (df.iloc[i]['low'] <= level + 0.05):
                return len(df) - i - 1
        return len(df)

    def _merge_overlapping_pools(
        self,
        pools: List[LiquidityPool],
        threshold: float = 0.1
    ) -> List[LiquidityPool]:
        """Merge overlapping pools."""
        if not pools:
            return []

        sorted_pools = sorted(pools, key=lambda p: p.price)
        merged = []
        current = sorted_pools[0]

        for pool in sorted_pools[1:]:
            if abs(pool.price - current.price) < threshold:
                # Merge: take average price and sum strength
                current.price = (current.price + pool.price) / 2
                current.strength = min(100, current.strength + pool.strength)
                current.touches += pool.touches
            else:
                merged.append(current)
                current = pool

        merged.append(current)
        return merged

    def _find_sweep_bar(
        self,
        df: pd.DataFrame,
        pool_price: float,
        side: PoolSide
    ) -> Optional[int]:
        """Find if and when a liquidity pool was swept."""
        for i in range(len(df)):
            if side == PoolSide.BUY_SIDE:
                if df.iloc[i]['high'] > pool_price + 0.05:
                    return i
            else:
                if df.iloc[i]['low'] < pool_price - 0.05:
                    return i
        return None

    def _check_reversal_down(self, df: pd.DataFrame, idx: int, window: int = 5) -> bool:
        """Check if price reversed downward."""
        if idx + window >= len(df):
            return False
        after = df.iloc[idx+1:idx+window+1]
        return after['close'].iloc[-1] < df.iloc[idx]['close']

    def _check_reversal_up(self, df: pd.DataFrame, idx: int, window: int = 5) -> bool:
        """Check if price reversed upward."""
        if idx + window >= len(df):
            return False
        after = df.iloc[idx+1:idx+window+1]
        return after['close'].iloc[-1] > df.iloc[idx]['close']

    def _classify_sweep_quality(
        self,
        df: pd.DataFrame,
        sweep_idx: int,
        pool_price: float,
        depth_ticks: float
    ) -> SweepQuality:
        """Classify sweep quality: CLEAN, MESSY, or FAILED."""
        if depth_ticks < 1:
            return SweepQuality.FAILED
        elif depth_ticks <= 5:
            return SweepQuality.CLEAN
        else:
            return SweepQuality.MESSY

    def _measure_displacement(
        self,
        df: pd.DataFrame,
        sweep_idx: int,
        side: PoolSide,
        bars: int = 3
    ) -> float:
        """Measure institutional entry displacement after sweep."""
        if sweep_idx + bars >= len(df):
            return 0.0

        after = df.iloc[sweep_idx+1:sweep_idx+bars+1]
        if side == PoolSide.BUY_SIDE:
            displacement = (after['low'].min() - df.iloc[sweep_idx]['high'])
        else:
            displacement = (df.iloc[sweep_idx]['low'] - after['high'].max())

        return abs(displacement) / self.tick_size

    def _bars_to_reversal(self, df: pd.DataFrame, idx: int, side: PoolSide) -> int:
        """Count bars to reversal after sweep."""
        if side == PoolSide.BUY_SIDE:
            for i in range(idx + 1, len(df)):
                if df.iloc[i]['close'] < df.iloc[idx]['close']:
                    return i - idx
        else:
            for i in range(idx + 1, len(df)):
                if df.iloc[i]['close'] > df.iloc[idx]['close']:
                    return i - idx
        return 0

    def _find_reversal_after(self, df: pd.DataFrame, idx: int, window: int = 5) -> Optional[int]:
        """Find reversal bar after index."""
        if idx + window >= len(df):
            return None

        bar_at_idx = df.iloc[idx]['close']
        for i in range(idx + 1, min(idx + window + 1, len(df))):
            if i > idx and df.iloc[i]['close'] != bar_at_idx:
                return i
        return None

    def _find_pool_at_level(self, price: float) -> Optional[LiquidityPool]:
        """Find if a pool exists at or near a price level."""
        for pool in self.pools:
            if abs(pool.price - price) < 0.2:
                return pool
        return None

    def _calculate_void_fill_pct(
        self,
        df: pd.DataFrame,
        void_idx: int,
        high: float,
        low: float
    ) -> float:
        """Calculate percentage of void that has been filled."""
        void_size = high - low
        if void_size == 0:
            return 100

        future = df.iloc[void_idx+1:void_idx+20] if void_idx + 20 <= len(df) else df.iloc[void_idx+1:]
        candles_in_range = len(future[(future['high'] >= low) & (future['low'] <= high)])

        return min(100, (candles_in_range / max(1, len(future))) * 100)

    def _calculate_vwap(self, df: pd.DataFrame) -> float:
        """Calculate VWAP."""
        typical_price = (df['high'] + df['low'] + df['close']) / 3
        vwap = (typical_price * df['volume']).sum() / df['volume'].sum()
        return vwap

    def _determine_market_bias(self, df: pd.DataFrame) -> str:
        """Determine market bias from price structure."""
        recent = df.tail(20)
        sma20 = recent['close'].mean()
        current = recent.iloc[-1]['close']

        if current > sma20:
            return "BULLISH"
        elif current < sma20:
            return "BEARISH"
        else:
            return "NEUTRAL"

    def _generate_summary(
        self,
        bsl: List[LiquidityPool],
        ssl: List[LiquidityPool],
        sweeps: List[LiquiditySweep],
        dol: DrawOnLiquidity
    ) -> str:
        """Generate text summary of liquidity state."""
        lines = [
            f"Liquidity State Summary:",
            f"  BSL Pools: {len(bsl)} (Buy-side liquidity)",
            f"  SSL Pools: {len(ssl)} (Sell-side liquidity)",
            f"  Recent Sweeps: {len(sweeps)}",
            f"  Market Bias: {dol.bias}",
            f"  Primary Draw: {dol.primary_draw:.2f} (confidence: {dol.confidence:.0f}%)" if dol.primary_draw else "  Primary Draw: None"
        ]
        return "\n".join(lines)


def main():
    """Demo: Institutional liquidity mapping on sample data."""
    print("=" * 80)
    print("ICT/SMC Institutional Liquidity Mapping Engine")
    print("=" * 80)

    # Generate sample OHLCV data
    np.random.seed(42)
    dates = pd.date_range('2024-01-01', periods=200, freq='1H')

    # Realistic price action with liquidity patterns
    prices = np.cumsum(np.random.randn(200) * 0.5) + 350

    df = pd.DataFrame({
        'datetime': dates,
        'open': prices + np.random.randn(200) * 0.1,
        'high': prices + np.abs(np.random.randn(200) * 0.3),
        'low': prices - np.abs(np.random.randn(200) * 0.3),
        'close': prices + np.random.randn(200) * 0.15,
        'volume': np.random.uniform(1_000_000, 10_000_000, 200)
    })

    # Ensure OHLC order
    df['high'] = df[['open', 'high', 'low', 'close']].max(axis=1)
    df['low'] = df[['open', 'high', 'low', 'close']].min(axis=1)

    print(f"\nDataset: {len(df)} bars, {df['datetime'].min()} to {df['datetime'].max()}")
    print(f"Price range: {df['low'].min():.2f} - {df['high'].max():.2f}")

    # Initialize mapper
    mapper = LiquidityMapper(symbol="SPY", tick_size=0.01)

    # Generate full liquidity state
    print("\n" + "=" * 80)
    print("RUNNING FULL LIQUIDITY ANALYSIS")
    print("=" * 80)

    state = mapper.get_liquidity_state(df)

    # Display results
    print("\n" + state.summary)

    print("\n" + "-" * 80)
    print("BUY-SIDE LIQUIDITY (BSL) - Shorts have stops here")
    print("-" * 80)
    for pool in sorted(state.bsl_pools, key=lambda p: -p.strength)[:5]:
        print(f"  Price: ${pool.price:.2f} | Strength: {pool.strength:.0f} | "
              f"Status: {pool.status.value} | Touches: {pool.touches} | "
              f"Volume: {pool.estimated_volume:,.0f}")

    print("\n" + "-" * 80)
    print("SELL-SIDE LIQUIDITY (SSL) - Longs have stops here")
    print("-" * 80)
    for pool in sorted(state.ssl_pools, key=lambda p: -p.strength)[:5]:
        print(f"  Price: ${pool.price:.2f} | Strength: {pool.strength:.0f} | "
              f"Status: {pool.status.value} | Touches: {pool.touches} | "
              f"Volume: {pool.estimated_volume:,.0f}")

    print("\n" + "-" * 80)
    print("LIQUIDITY SWEEPS (Stop Hunts)")
    print("-" * 80)
    if state.recent_sweeps:
        for sweep in state.recent_sweeps:
            print(f"  Bar {sweep.sweep_bar} | Pool: ${sweep.pool.price:.2f} | "
                  f"Depth: {sweep.depth_ticks:.1f} ticks | "
                  f"Quality: {sweep.quality.value} | "
                  f"Reversal: {sweep.reversal_confirmed}")
    else:
        print("  No recent sweeps detected")

    print("\n" + "-" * 80)
    print("LIQUIDITY VOIDS (Fair Value Gaps)")
    print("-" * 80)
    if state.voids:
        for void in state.voids[:5]:
            print(f"  ${void.low:.2f} - ${void.high:.2f} | "
                  f"Direction: {void.direction.value} | "
                  f"Fill: {void.fill_pct:.0f}% | "
                  f"Age: {void.age_bars} bars")
    else:
        print("  No unfilled voids detected")

    print("\n" + "-" * 80)
    print("INSTITUTIONAL LIQUIDITY LEVELS")
    print("-" * 80)
    for level in sorted(state.institutional_levels, key=lambda l: -l.strength)[:8]:
        print(f"  ${level.price:.2f} | Type: {level.level_type.value} | "
              f"Strength: {level.strength:.0f} | "
              f"Tested: {level.times_tested}x | "
              f"Last {level.last_test} bars ago")

    print("\n" + "-" * 80)
    print("DRAW ON LIQUIDITY (Where Price is Heading)")
    print("-" * 80)
    print(f"  Market Bias: {state.draw_on_liquidity.bias}")
    print(f"  Primary Draw: ${state.draw_on_liquidity.primary_draw:.2f}" if state.draw_on_liquidity.primary_draw else "  Primary Draw: None")
    print(f"  Confidence: {state.draw_on_liquidity.confidence:.0f}%")

    if state.draw_on_liquidity.upside_targets:
        print(f"\n  Upside Targets:")
        for target in state.draw_on_liquidity.upside_targets[:3]:
            print(f"    ${target:.2f}")

    if state.draw_on_liquidity.downside_targets:
        print(f"\n  Downside Targets:")
        for target in state.draw_on_liquidity.downside_targets[:3]:
            print(f"    ${target:.2f}")

    print("\n" + "=" * 80)
    print("Liquidity mapping complete.")
    print("=" * 80)


if __name__ == "__main__":
    main()
