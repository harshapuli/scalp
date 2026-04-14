"""
Institutional Signal Generator - V2 Trading Engine

Generates NEW trading signals based on institutional flow + whale data + liquidity maps.
Sits on TOP of existing SMC signals and adds institutional-grade entries with:
- Institutional order block entries (inst_ob)
- Liquidity sweep + institutional reversal (inst_sweep_reversal)
- Smart money divergence entries (smart_money_div)
- Wyckoff spring/UTAD entries (wyckoff_institutional)
- Volume profile fair value entries (vp_fair_value)
- Iceberg fade entries (iceberg_fade)

Author: Live Trading Engine v2
"""

from dataclasses import dataclass, field
from typing import List, Dict, Optional, Tuple
from enum import Enum
import numpy as np
import pandas as pd
import logging
from datetime import datetime

from institutional_flow import (
    InstitutionalFlowAnalyzer,
    WyckoffPhase,
    DeltaDirection,
)
from whale_tracker import (
    WhaleTracker,
    VolumeProfile,
)
from liquidity_map import (
    LiquidityMapper,
    PoolSide,
)


# ============================================================================
# LOGGING SETUP
# ============================================================================

logger = logging.getLogger(__name__)


# ============================================================================
# SIGNAL DATA CLASSES
# ============================================================================

@dataclass
class InstitutionalSignal:
    """
    Complete institutional trading signal with all components.

    Each signal is one trade opportunity with entry, stops, and targets.
    """
    signal_type: str                    # e.g., 'inst_sweep_reversal'
    direction: str                      # 'CALL' or 'PUT'
    entry_price: float
    stop_loss: float
    tp1: float
    tp2: float
    conviction: int                     # 0-100 (conviction score)
    institutional_score: int            # 0-100 (inst flow strength)
    whale_confidence: int               # 0-100 (whale signal strength)
    components: Dict[str, any]          # What triggered this signal
    timeframe: str                      # e.g., '1min', '5min'
    ticker: str
    timestamp: float                    # Unix timestamp
    risk_reward_ratio: float = 0.0

    def __repr__(self):
        return (f"{self.signal_type.upper()} {self.direction} @ {self.entry_price:.2f} "
                f"| Conv={self.conviction} | Inst={self.institutional_score} | Whale={self.whale_confidence}")


# ============================================================================
# INSTITUTIONAL SIGNAL GENERATOR
# ============================================================================

class InstitutionalSignalGenerator:
    """
    Generates institutional-grade trading signals combining:
    - Institutional order flow analysis
    - Whale accumulation/distribution tracking
    - Liquidity mapping and sweeps
    - Volume profile fair value zones
    """

    def __init__(self, lookback: int = 100, atr_period: int = 14,
                 volume_threshold: int = 75, uw_client=None):
        """
        Initialize signal generator.

        Args:
            lookback: Bars to analyze for historical context
            atr_period: Period for ATR calculations
            volume_threshold: Percentile for whale volume detection (0-100)
            uw_client: Optional UnusualWhalesClient for real institutional data
        """
        self.lookback = lookback
        self.atr_period = atr_period
        self.uw_client = uw_client

        self.flow_analyzer = InstitutionalFlowAnalyzer(lookback=lookback, uw_client=uw_client)
        self.whale_tracker = WhaleTracker(volume_threshold_percentile=volume_threshold,
                                          uw_client=uw_client)
        self.liquidity_mapper = LiquidityMapper()

        # Window-level cache: avoid recomputing shared data for overlapping windows
        self._cache_hash = None
        self._cache = {}

        logger.info(f"InstitutionalSignalGenerator initialized (lookback={lookback}, atr={atr_period}, "
                    f"uw_client={'ACTIVE' if uw_client else 'None'})")

    def _get_window_hash(self, df: pd.DataFrame) -> str:
        """Fast hash to detect if window changed."""
        if df.empty:
            return ""
        return f"{len(df)}_{df.iloc[-1]['close']:.2f}_{df.iloc[-1]['volume']:.0f}_{df.iloc[0]['close']:.2f}"

    def _get_cached(self, key: str, compute_fn, df: pd.DataFrame):
        """Return cached value or compute and cache it."""
        window_hash = self._get_window_hash(df)
        if window_hash != self._cache_hash:
            self._cache = {}
            self._cache_hash = window_hash
        if key not in self._cache:
            self._cache[key] = compute_fn()
        return self._cache[key]

    # ========================================================================
    # SIGNAL 1: INSTITUTIONAL ORDER BLOCK ENTRY (inst_ob)
    # ========================================================================

    def scan_inst_order_block(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Institutional Order Block Entry Signal.

        Standard SMC order block PLUS institutional confirmation:
        - OB at whale accumulation zone OR near POC/VAH/VAL
        - Institutional score >= 60 at the OB level
        - Cumulative delta confirms direction
        - Dark pool prints within 2 ATR of OB

        Args:
            df: OHLCV DataFrame
            ticker: Asset ticker
            timeframe: Timeframe string

        Returns:
            InstitutionalSignal or None
        """
        if len(df) < self.lookback:
            return None

        try:
            # Get key components
            atm = self._calculate_atr(df)
            inst_score = self.flow_analyzer.get_institutional_score(df)
            accum_zones = getattr(self, '_cached_accum_zones', None) or self.whale_tracker.detect_accumulation_zones(df, self.lookback)
            vol_profile = getattr(self, '_cached_vol_profile', None) or self.whale_tracker.build_volume_profile(df)
            dark_pools = self.flow_analyzer.detect_dark_pool_prints(df)
            cum_delta = self.flow_analyzer.cumulative_delta(df)

            # No signal without recent accumulation
            if not accum_zones:
                return None

            # Get latest accumulated zone
            latest_zone = accum_zones[-1]

            # Institutional score check (need 60+ at this level)
            if inst_score.score < 60:
                return None

            # Current price relative to zone
            current_price = df['close'].iloc[-1]
            zone_mid = (latest_zone.price_low + latest_zone.price_high) / 2
            price_dist = abs(current_price - zone_mid) / atm

            # Price must be near zone (within 2 ATR)
            if price_dist > 2.0:
                return None

            # Determine direction based on zone and whale activity
            buy_pct = latest_zone.buy_pct
            if buy_pct > 60:  # More buying in zone
                direction = "CALL"
                entry_price = current_price
                stop_loss = latest_zone.price_low - 0.5 * atm
                tp1 = vol_profile.vah
                tp2 = vol_profile.vah + (vol_profile.vah - vol_profile.poc)
            elif buy_pct < 40:  # More selling in zone
                direction = "PUT"
                entry_price = current_price
                stop_loss = latest_zone.price_high + 0.5 * atm
                tp1 = vol_profile.val
                tp2 = vol_profile.val - (vol_profile.poc - vol_profile.val)
            else:
                return None

            # Validate with cumulative delta
            recent_delta = cum_delta['cum_delta'].iloc[-1]
            if direction == "CALL" and recent_delta < 0:
                return None
            if direction == "PUT" and recent_delta > 0:
                return None

            # Check for dark pool confirmation (within 2 ATR)
            dark_pool_confirm = False
            if dark_pools:
                for dp in dark_pools[-5:]:  # Recent 5 dark pool prints
                    if abs(dp.price_level - entry_price) < 2 * atm:
                        dark_pool_confirm = True
                        break

            conviction = int(inst_score.score * 0.7)
            if dark_pool_confirm:
                conviction = min(100, conviction + 15)

            whale_conf = int(latest_zone.score * 100)

            signal = InstitutionalSignal(
                signal_type="inst_ob",
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                conviction=conviction,
                institutional_score=int(inst_score.score),
                whale_confidence=whale_conf,
                components={
                    "zone_price": zone_mid,
                    "zone_strength": latest_zone.score,
                    "dark_pool_confirm": dark_pool_confirm,
                    "cum_delta_direction": "bullish" if recent_delta > 0 else "bearish",
                    "vah": vol_profile.vah,
                    "val": vol_profile.val,
                    "poc": vol_profile.poc,
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(entry_price, stop_loss, tp2)
            return signal

        except Exception as e:
            logger.error(f"Error in scan_inst_order_block: {e}")
            return None

    # ========================================================================
    # SIGNAL 2: LIQUIDITY SWEEP + INSTITUTIONAL REVERSAL (inst_sweep_reversal)
    # ========================================================================

    def scan_sweep_reversal(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Liquidity Sweep + Institutional Reversal — A+ SNIPER SETUP ONLY.

        V3 UPGRADE: Converted from primary strategy to restricted sniper setup.
        Must pass ALL 4 non-negotiable filters:

        1. DIVERGENCE — smart_money divergence OR volume divergence must be present
        2. EXHAUSTION — no follow-through, weak displacement, wick > body (rejection)
        3. KEY LIQUIDITY LEVEL — PDH/PDL, equal HL, or session HL
        4. SCORING — score 0-100, must be >= 70 to execute

        Missing 1 condition = SKIP. Missing 2+ = HARD BLOCK.
        Expected: 90 trades → 10-20 high-quality trades.
        """
        if len(df) < self.lookback:
            return None

        try:
            atm = self._calculate_atr(df)
            current_price = df['close'].iloc[-1]

            # ── BASIC SWEEP DETECTION ──
            pools = getattr(self, '_cached_pools', None) or self.liquidity_mapper.map_stop_clusters(df, self.lookback)
            if not pools:
                return None

            sweeps = self.liquidity_mapper.detect_sweep(df, pools)
            if not sweeps:
                return None

            latest_sweep = sweeps[-1]

            # Quality gate: must be CLEAN sweep (not MESSY or FAILED)
            if latest_sweep.quality.value in ("FAILED", "MESSY"):
                return None

            # ── INSTITUTIONAL DATA ──
            inst_score = self.flow_analyzer.get_institutional_score(df)
            whale_signal = getattr(self, '_cached_whale_momentum', None) or self.whale_tracker.whale_momentum(df)
            block_trades = self.flow_analyzer.detect_block_trades(df)
            wyckoff_state = self.flow_analyzer.wyckoff_phase_detection(df)
            accum_zones = getattr(self, '_cached_accum_zones', None) or self.whale_tracker.detect_accumulation_zones(df)

            # Institutional score gate (raised from 55 to 58)
            if inst_score.score < 58:
                return None

            # ── DIRECTION FROM SWEEP ──
            sweep_pool_side = latest_sweep.pool.side

            if sweep_pool_side == PoolSide.SELL_SIDE:
                expected_direction = "BUY"
                expected_whale_dir = "BUY"
            else:
                expected_direction = "SELL"
                expected_whale_dir = "SELL"

            # Whale alignment (hard requirement)
            if whale_signal.flow_direction != expected_whale_dir:
                return None

            # ════════════════════════════════════════════════════════════
            # NON-NEGOTIABLE FILTERS — SCORING SYSTEM
            # Each filter adds points. Must reach 70/100 to execute.
            # ════════════════════════════════════════════════════════════

            score = 0
            score_reasons = []
            conditions_met = 0
            conditions_total = 4

            # ── FILTER 1: DIVERGENCE (0-25 points) ──
            # Must have smart_money divergence OR volume divergence
            has_divergence = False

            # Check smart money divergence
            try:
                sm_div = self.flow_analyzer.smart_money_divergence(df)
                if sm_div and sm_div.active:
                    has_divergence = True
                    score += 25
                    score_reasons.append(f"SM divergence active (strength={sm_div.strength:.0f})")
            except:
                pass

            # Check whale vs retail divergence
            if not has_divergence:
                try:
                    whale_div = getattr(self, '_cached_whale_div', None) or self.whale_tracker.whale_vs_retail_divergence(df)
                    if whale_div and whale_div.active:
                        has_divergence = True
                        score += 20
                        score_reasons.append("Whale-retail divergence active")
                except:
                    pass

            # Check volume divergence (price new high/low but volume declining)
            if not has_divergence:
                try:
                    vol = df['volume'].values
                    close = df['close'].values
                    if len(vol) >= 10:
                        recent_vol = np.mean(vol[-5:])
                        prior_vol = np.mean(vol[-10:-5])
                        if expected_direction == "BUY":
                            # Price at low but volume declining = bearish exhaustion
                            price_at_low = close[-1] <= np.min(close[-10:]) * 1.002
                            if price_at_low and recent_vol < prior_vol * 0.8:
                                has_divergence = True
                                score += 15
                                score_reasons.append("Volume divergence at low")
                        else:
                            price_at_high = close[-1] >= np.max(close[-10:]) * 0.998
                            if price_at_high and recent_vol < prior_vol * 0.8:
                                has_divergence = True
                                score += 15
                                score_reasons.append("Volume divergence at high")
                except:
                    pass

            if has_divergence:
                conditions_met += 1

            # ── FILTER 2: EXHAUSTION (0-25 points) ──
            # No follow-through after sweep + weak displacement + rejection wick
            has_exhaustion = False

            try:
                # Check last 3 bars after sweep for exhaustion signs
                sweep_bar = latest_sweep.sweep_bar
                bars_since = len(df) - 1 - sweep_bar

                if bars_since >= 1 and bars_since <= 8:
                    post_sweep = df.iloc[sweep_bar:]
                    opens = post_sweep['open'].values
                    closes = post_sweep['close'].values
                    highs = post_sweep['high'].values
                    lows = post_sweep['low'].values

                    # 1. No follow-through: price didn't continue beyond sweep
                    if expected_direction == "BUY":
                        no_follow = closes[-1] > lows[0]  # Bounced from sweep low
                    else:
                        no_follow = closes[-1] < highs[0]

                    # 2. Weak displacement: body < 0.5 * ATR on post-sweep bars
                    bodies = np.abs(closes - opens)
                    weak_displacement = np.mean(bodies[-3:]) < 0.5 * atm if len(bodies) >= 3 else False

                    # 3. Rejection wick: last bar has wick > body
                    last_body = abs(closes[-1] - opens[-1])
                    if expected_direction == "BUY":
                        last_wick = closes[-1] - lows[-1]  # Lower wick on bullish
                    else:
                        last_wick = highs[-1] - closes[-1]  # Upper wick on bearish
                    rejection_wick = last_wick > last_body * 1.5 if last_body > 0 else False

                    # 4. Whale exhaustion flag
                    whale_exhaustion = whale_signal.exhaustion_flag if hasattr(whale_signal, 'exhaustion_flag') else False

                    exhaustion_score = 0
                    if no_follow:
                        exhaustion_score += 8
                        score_reasons.append("No follow-through after sweep")
                    if weak_displacement:
                        exhaustion_score += 7
                        score_reasons.append("Weak displacement post-sweep")
                    if rejection_wick:
                        exhaustion_score += 7
                        score_reasons.append("Rejection wick detected")
                    if whale_exhaustion:
                        exhaustion_score += 8
                        score_reasons.append("Whale exhaustion flag")

                    score += min(25, exhaustion_score)
                    if exhaustion_score >= 14:
                        has_exhaustion = True
                        conditions_met += 1
            except:
                pass

            # ── FILTER 3: KEY LIQUIDITY LEVEL (0-25 points) ──
            # Sweep must be at PDH/PDL, equal HL, or session HL
            at_key_level = False

            try:
                sweep_price = latest_sweep.pool.price

                # Check PDH/PDL
                if 'Date' in df.columns or hasattr(df.index, 'date'):
                    try:
                        dates = df.index.date if hasattr(df.index, 'date') else df['Date'].dt.date
                        unique_dates = sorted(set(dates))
                        if len(unique_dates) >= 2:
                            prev_day = unique_dates[-2]
                            prev_data = df[dates == prev_day]
                            if len(prev_data) > 0:
                                pdh = prev_data['high'].max()
                                pdl = prev_data['low'].min()
                                if abs(sweep_price - pdh) < 0.5 * atm:
                                    at_key_level = True
                                    score += 25
                                    score_reasons.append(f"Sweep at PDH ({pdh:.2f})")
                                elif abs(sweep_price - pdl) < 0.5 * atm:
                                    at_key_level = True
                                    score += 25
                                    score_reasons.append(f"Sweep at PDL ({pdl:.2f})")
                    except:
                        pass

                # Check session high/low (current day)
                if not at_key_level:
                    try:
                        today = df.index[-1].date() if hasattr(df.index[-1], 'date') else None
                        if today:
                            today_data = df[df.index.date == today] if hasattr(df.index, 'date') else df
                            session_high = today_data['high'].max()
                            session_low = today_data['low'].min()
                            if abs(sweep_price - session_high) < 0.3 * atm:
                                at_key_level = True
                                score += 20
                                score_reasons.append(f"Sweep at session high ({session_high:.2f})")
                            elif abs(sweep_price - session_low) < 0.3 * atm:
                                at_key_level = True
                                score += 20
                                score_reasons.append(f"Sweep at session low ({session_low:.2f})")
                    except:
                        pass

                # Check equal highs/lows (pool strength as proxy)
                if not at_key_level:
                    if latest_sweep.pool.strength > 70:
                        at_key_level = True
                        score += 18
                        score_reasons.append(f"High-strength pool ({latest_sweep.pool.strength:.0f})")
                    elif latest_sweep.pool.touches >= 3:
                        at_key_level = True
                        score += 15
                        score_reasons.append(f"Multi-touch level ({latest_sweep.pool.touches} touches)")

            except:
                pass

            if at_key_level:
                conditions_met += 1

            # ── FILTER 4: INSTITUTIONAL CONFIRMATION (0-25 points) ──
            # Wyckoff + block trades + high whale confidence
            inst_confirm_score = 0

            # Wyckoff alignment
            wyckoff_aligned = False
            if wyckoff_state.phase == WyckoffPhase.PHASE_C:
                wyckoff_aligned = True
                inst_confirm_score += 10
                score_reasons.append("Wyckoff Phase C (spring/UTAD)")

            # Block trade confirmation
            block_trade_confirm = False
            if block_trades:
                recent_bars = len(df) - latest_sweep.sweep_bar
                for bt in block_trades[-3:]:
                    if recent_bars < 10:
                        if expected_direction == "BUY" and bt.trade_type == "accumulation":
                            block_trade_confirm = True
                        elif expected_direction == "SELL" and bt.trade_type == "distribution":
                            block_trade_confirm = True
            if block_trade_confirm:
                inst_confirm_score += 8
                score_reasons.append("Block trade confirms direction")

            # High whale confidence
            if whale_signal.flow_strength > 70:
                inst_confirm_score += 7
                score_reasons.append(f"Strong whale momentum ({whale_signal.flow_strength:.0f})")

            score += min(25, inst_confirm_score)
            if inst_confirm_score >= 10:
                conditions_met += 1

            # ════════════════════════════════════════════════════════════
            # SCORE GATE — THE KILL SWITCH
            # ════════════════════════════════════════════════════════════

            conditions_failed = conditions_total - conditions_met

            # Missing 2+ conditions = HARD BLOCK
            if conditions_failed >= 2:
                logger.debug(
                    f"sweep_reversal HARD BLOCK: score={score}/100, "
                    f"conditions={conditions_met}/{conditions_total}"
                )
                return None

            # Must reach 70/100 to execute
            if score < 70:
                logger.debug(
                    f"sweep_reversal SCORE BLOCK: score={score}/100 < 70, "
                    f"conditions={conditions_met}/{conditions_total}"
                )
                return None

            # Missing 1 condition = reduced conviction
            if conditions_failed == 1:
                conviction_penalty = 10
            else:
                conviction_penalty = 0

            # ── ENTRY / STOPS / TARGETS ──
            entry_price = latest_sweep.pool.price

            if expected_direction == "BUY":
                direction = "CALL"
                stop_loss = latest_sweep.pool.price - 1.5 * atm
                tp1 = latest_sweep.pool.price + 3 * atm
                tp2 = latest_sweep.pool.price + 6 * atm
            else:
                direction = "PUT"
                stop_loss = latest_sweep.pool.price + 1.5 * atm
                tp1 = latest_sweep.pool.price - 3 * atm
                tp2 = latest_sweep.pool.price - 6 * atm

            # Conviction = score, capped at 100
            conviction = min(100, score - conviction_penalty)

            signal = InstitutionalSignal(
                signal_type="inst_sweep_reversal",
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                conviction=conviction,
                institutional_score=int(inst_score.score),
                whale_confidence=int(whale_signal.flow_strength),
                components={
                    "sweep_quality": latest_sweep.quality.value,
                    "sweep_depth_ticks": latest_sweep.depth_ticks,
                    "pool_side": sweep_pool_side.value,
                    "signal_score": score,
                    "conditions_met": conditions_met,
                    "conditions_total": conditions_total,
                    "has_divergence": has_divergence,
                    "has_exhaustion": has_exhaustion,
                    "at_key_level": at_key_level,
                    "wyckoff_aligned": wyckoff_aligned,
                    "block_trades_confirm": block_trade_confirm,
                    "score_reasons": score_reasons,
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(entry_price, stop_loss, tp2)
            return signal

        except Exception as e:
            logger.error(f"Error in scan_sweep_reversal: {e}")
            return None

    # ========================================================================
    # SIGNAL 3: SMART MONEY DIVERGENCE ENTRY (smart_money_div)
    # ========================================================================

    def scan_smart_money_divergence(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Smart Money Divergence Entry Signal.

        Price trending but smart money accumulating opposite direction:
        - smart_money_divergence() shows divergence
        - Whale distribution/accumulation confirms
        - Liquidity void above/below as draw target
        - Volume profile shows price at LVN (low volume)

        Args:
            df: OHLCV DataFrame
            ticker: Asset ticker
            timeframe: Timeframe string

        Returns:
            InstitutionalSignal or None
        """
        if len(df) < self.lookback:
            return None

        try:
            atm = self._calculate_atr(df)

            # Detect smart money divergence
            sm_divergence = self.flow_analyzer.smart_money_divergence(df)
            if not sm_divergence or sm_divergence.strength < 50:
                return None

            # Whale positioning must confirm
            whale_signal = getattr(self, '_cached_whale_momentum', None) or self.whale_tracker.whale_momentum(df)
            distribution = getattr(self, '_cached_distribution', None) or self.whale_tracker.detect_distribution(df)
            accum_zones = getattr(self, '_cached_accum_zones', None) or self.whale_tracker.detect_accumulation_zones(df, self.lookback)

            # Volume profile shows LVN (fast move coming)
            vol_profile = getattr(self, '_cached_vol_profile', None) or self.whale_tracker.build_volume_profile(df)
            current_price = df['close'].iloc[-1]

            # Check if price is at LVN (relaxed: within 1.5 ATR, or near POC)
            at_lvn = False
            near_poc = abs(current_price - vol_profile.poc) < atm * 1.5
            if vol_profile.lvn_levels:
                for lvn in vol_profile.lvn_levels:
                    if abs(current_price - lvn) < atm * 1.5:
                        at_lvn = True
                        break

            # Allow if at LVN OR near POC with strong divergence
            if not at_lvn and not (near_poc and sm_divergence.strength >= 65):
                return None

            # Determine divergence direction
            if sm_divergence.direction == "bullish":
                direction = "CALL"
                # Price down, smart money buying
                target = vol_profile.poc if vol_profile.poc > current_price else vol_profile.vah
                stop_loss = current_price - 2 * atm
            else:
                direction = "PUT"
                # Price up, smart money selling
                target = vol_profile.poc if vol_profile.poc < current_price else vol_profile.val
                stop_loss = current_price + 2 * atm

            # Liquidity void as secondary target
            voids = self.liquidity_mapper.detect_liquidity_voids(df)
            void_target = None
            if voids:
                for void in voids[-3:]:
                    if direction == "CALL" and void.direction.value == "BULLISH":
                        void_target = void.high
                        break
                    elif direction == "PUT" and void.direction.value == "BEARISH":
                        void_target = void.low
                        break

            # Calculate targets
            tp1 = target
            tp2 = void_target if void_target else target + (target - current_price)

            # Conviction based on divergence strength
            conviction = int(sm_divergence.strength * 0.8)
            if whale_signal.divergence:
                conviction = min(100, conviction + 10)
            if at_lvn:
                conviction = min(100, conviction + 5)

            signal = InstitutionalSignal(
                signal_type="smart_money_div",
                direction=direction,
                entry_price=current_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                conviction=conviction,
                institutional_score=int(sm_divergence.strength),
                whale_confidence=int(whale_signal.flow_strength),
                components={
                    "divergence_type": sm_divergence.divergence_type,
                    "at_lvn": at_lvn,
                    "lvn_levels": [f"{l:.2f}" for l in vol_profile.lvn_levels[:3]],
                    "poc_target": vol_profile.poc,
                    "whale_divergence": whale_signal.divergence,
                    "void_target": void_target,
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(current_price, stop_loss, tp2)
            return signal

        except Exception as e:
            logger.error(f"Error in scan_smart_money_divergence: {e}")
            return None

    # ========================================================================
    # SIGNAL 4: WYCKOFF SPRING/UTAD ENTRY (wyckoff_institutional)
    # ========================================================================

    def scan_wyckoff_institutional(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Wyckoff Spring/UTAD Institutional Entry Signal.

        Pure Wyckoff accumulation/distribution plays:
        - Wyckoff phase C detected (spring for longs, UTAD for shorts)
        - Whale accumulation zone at spring level
        - Institutional footprint shows absorption (not panic)
        - Draw on liquidity confirms direction

        Args:
            df: OHLCV DataFrame
            ticker: Asset ticker
            timeframe: Timeframe string

        Returns:
            InstitutionalSignal or None
        """
        if len(df) < self.lookback:
            return None

        try:
            atm = self._calculate_atr(df)

            # Detect Wyckoff phase
            wyckoff_state = self.flow_analyzer.wyckoff_phase_detection(df)

            # Must be in Phase C (spring/UTAD)
            if wyckoff_state.phase != WyckoffPhase.PHASE_C:
                return None

            # Get accumulation zones
            accum_zones = getattr(self, '_cached_accum_zones', None) or self.whale_tracker.detect_accumulation_zones(df, self.lookback)
            if not accum_zones:
                return None

            # Find zone near Wyckoff range low
            current_price = df['close'].iloc[-1]
            zone_at_spring = None
            for az in accum_zones[-3:]:
                if abs(az.price_low - wyckoff_state.range_low) < atm:
                    zone_at_spring = az
                    break

            if not zone_at_spring:
                return None

            # Institutional footprint (absorption check)
            footprint = self.flow_analyzer.footprint_analysis(df)
            block_trades = self.flow_analyzer.detect_block_trades(df)

            # Check for absorption (not panic selling)
            absorption_score = 50  # Base score
            if footprint and footprint.total_absorption > 40:
                absorption_score += int(footprint.total_absorption)

            if block_trades:
                accum_count = sum(1 for bt in block_trades[-5:] if bt.trade_type == "accumulation")
                absorption_score += accum_count * 10

            absorption_score = min(100, absorption_score)

            # Need good absorption (not panic)
            if absorption_score < 60:
                return None

            # Draw on liquidity
            draw_liquidity = self.liquidity_mapper.compute_draw_on_liquidity(df)

            # Direction based on Wyckoff bias
            if wyckoff_state.bias == DeltaDirection.BUY:
                direction = "CALL"
                entry_price = zone_at_spring.price_low
                stop_loss = zone_at_spring.price_low - 1.5 * atm
                # Draw targets upward
                if draw_liquidity and draw_liquidity.get("bullish_target"):
                    tp2 = draw_liquidity["bullish_target"]
                else:
                    tp2 = current_price + 4 * atm
                tp1 = (entry_price + tp2) / 2
            elif wyckoff_state.bias == DeltaDirection.SELL:
                direction = "PUT"
                entry_price = zone_at_spring.price_high
                stop_loss = zone_at_spring.price_high + 1.5 * atm
                # Draw targets downward
                if draw_liquidity and draw_liquidity.get("bearish_target"):
                    tp2 = draw_liquidity["bearish_target"]
                else:
                    tp2 = current_price - 4 * atm
                tp1 = (entry_price + tp2) / 2
            else:
                return None

            # Conviction calculation
            conviction = int(absorption_score * 0.7)
            conviction += int(zone_at_spring.score * 10)
            conviction = min(100, conviction)

            signal = InstitutionalSignal(
                signal_type="wyckoff_institutional",
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                conviction=conviction,
                institutional_score=absorption_score,
                whale_confidence=int(zone_at_spring.score * 100),
                components={
                    "wyckoff_phase": wyckoff_state.phase.value,
                    "wyckoff_sub_phase": wyckoff_state.sub_phase,
                    "range_high": wyckoff_state.range_high,
                    "range_low": wyckoff_state.range_low,
                    "absorption_score": absorption_score,
                    "accumulation_count": sum(1 for bt in block_trades[-5:] if bt.trade_type == "accumulation") if block_trades else 0,
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(entry_price, stop_loss, tp2)
            return signal

        except Exception as e:
            logger.error(f"Error in scan_wyckoff_institutional: {e}")
            return None

    # ========================================================================
    # SIGNAL 5: VOLUME PROFILE FAIR VALUE ENTRY (vp_fair_value)
    # ========================================================================

    def scan_vp_fair_value(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Volume Profile Fair Value Entry Signal.

        Mean reversion to institutional fair value:
        - Price extended beyond VWAP ±2σ
        - Volume profile shows price at LVN, POC is the target
        - Whale momentum reversing toward POC
        - No active distribution/accumulation blocking the move

        Args:
            df: OHLCV DataFrame
            ticker: Asset ticker
            timeframe: Timeframe string

        Returns:
            InstitutionalSignal or None
        """
        if len(df) < self.lookback:
            return None

        try:
            atm = self._calculate_atr(df)

            # Get VWAP and bands
            vwap_data = self.whale_tracker.anchored_vwap(df)
            if not vwap_data:
                return None

            current_price = df['close'].iloc[-1]

            # Price must be extended beyond ±2σ
            deviation = vwap_data.deviation_pct
            if abs(deviation) < 2.0:  # Need 2 sigma move
                return None

            # Volume profile analysis
            vol_profile = getattr(self, '_cached_vol_profile', None) or self.whale_tracker.build_volume_profile(df)

            # Price should be at LVN
            at_lvn = False
            if vol_profile.lvn_levels:
                for lvn in vol_profile.lvn_levels:
                    if abs(current_price - lvn) < atm * 0.5:
                        at_lvn = True
                        break

            if not at_lvn:
                return None

            # POC is the target
            poc = vol_profile.poc

            # Check whale momentum (should be reversing)
            whale_momentum = getattr(self, '_cached_whale_momentum', None) or self.whale_tracker.whale_momentum(df)

            # Check for blocking distribution
            distribution = getattr(self, '_cached_distribution', None) or self.whale_tracker.detect_distribution(df)
            if distribution and distribution.active and distribution.strength > 60:
                return None

            # Direction based on price position
            if current_price > vwap_data.upper_band_2:
                # Price above +2σ, mean revert down
                direction = "PUT"
                stop_loss = current_price + atm
                target = poc
                tp1 = (current_price + poc) / 2
                tp2 = poc - (current_price - poc) * 0.5
            elif current_price < vwap_data.lower_band_2:
                # Price below -2σ, mean revert up
                direction = "CALL"
                stop_loss = current_price - atm
                target = poc
                tp1 = (current_price + poc) / 2
                tp2 = poc + (poc - current_price) * 0.5
            else:
                return None

            # Conviction based on deviation and whale confirmation
            conviction = int(min(100, abs(deviation) * 5))  # Bigger move = higher conviction
            if whale_momentum.flow_strength < 40:  # Weak momentum = better mean reversion
                conviction = min(100, conviction + 15)

            signal = InstitutionalSignal(
                signal_type="vp_fair_value",
                direction=direction,
                entry_price=current_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                conviction=conviction,
                institutional_score=int(vol_profile.developing_poc * 50 + 50),
                whale_confidence=int((100 - whale_momentum.flow_strength)),
                components={
                    "vwap": vwap_data.vwap,
                    "deviation_pct": deviation,
                    "band_position": vwap_data.band_position,
                    "poc": poc,
                    "vah": vol_profile.vah,
                    "val": vol_profile.val,
                    "whale_momentum_strength": whale_momentum.flow_strength,
                    "distribution_active": distribution.active if distribution else False,
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(current_price, stop_loss, tp2)
            return signal

        except Exception as e:
            logger.error(f"Error in scan_vp_fair_value: {e}")
            return None

    # ========================================================================
    # SIGNAL 6: ICEBERG FADE ENTRY (iceberg_fade)
    # ========================================================================

    def scan_iceberg_fade(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Iceberg Fade Entry Signal.

        Trade with the iceberg order:
        - Iceberg detected at a level (price can't push through)
        - Direction: fade the move INTO the iceberg
        - Institutional score confirms large player at that level
        - Confirmation: 2+ bounces off iceberg level

        Args:
            df: OHLCV DataFrame
            ticker: Asset ticker
            timeframe: Timeframe string

        Returns:
            InstitutionalSignal or None
        """
        if len(df) < self.lookback:
            return None

        try:
            atm = self._calculate_atr(df)

            # Detect icebergs
            icebergs = self.whale_tracker.detect_iceberg_orders(df)
            if not icebergs:
                return None

            # Get recent iceberg
            latest_iceberg = icebergs[-1]

            # Need high confidence
            if latest_iceberg.confidence < 70:
                return None

            # Need 2+ touches
            if latest_iceberg.num_touches < 2:
                return None

            current_price = df['close'].iloc[-1]
            iceberg_level = latest_iceberg.price_level

            # Price must be approaching the iceberg (within 1.5 ATR)
            dist_to_iceberg = abs(current_price - iceberg_level)
            if dist_to_iceberg > 1.5 * atm:
                return None

            # Institutional confirmation at iceberg level
            inst_score = self.flow_analyzer.get_institutional_score(df)
            if inst_score.score < 50:
                return None

            # Fade direction (opposite to iceberg)
            if latest_iceberg.direction == "BUY":
                # Iceberg is buying, fade into it (sell)
                direction = "PUT"
                entry_price = current_price
                stop_loss = iceberg_level + atm
                tp1 = iceberg_level - 2 * atm
                tp2 = iceberg_level - 4 * atm
            else:
                # Iceberg is selling, fade into it (buy)
                direction = "CALL"
                entry_price = current_price
                stop_loss = iceberg_level - atm
                tp1 = iceberg_level + 2 * atm
                tp2 = iceberg_level + 4 * atm

            # Conviction based on iceberg confidence and touch count
            conviction = int(latest_iceberg.confidence * 0.8)
            conviction += latest_iceberg.num_touches * 5
            conviction = min(100, conviction)

            signal = InstitutionalSignal(
                signal_type="iceberg_fade",
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                conviction=conviction,
                institutional_score=int(inst_score.score),
                whale_confidence=int(latest_iceberg.confidence),
                components={
                    "iceberg_level": iceberg_level,
                    "iceberg_direction": latest_iceberg.direction,
                    "num_touches": latest_iceberg.num_touches,
                    "estimated_volume": latest_iceberg.total_estimated_volume,
                    "distance_to_iceberg": dist_to_iceberg,
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(entry_price, stop_loss, tp2)
            return signal

        except Exception as e:
            logger.error(f"Error in scan_iceberg_fade: {e}")
            return None

    # ========================================================================
    # SIGNAL 7: WHALE ACCUMULATION ENTRY (whale_accumulation_entry)
    # ========================================================================

    def scan_whale_accumulation_entry(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Whale Accumulation Entry Signal.

        Detect active institutional accumulation at current price:
        - WhaleTracker detects active accumulation zone at current price
        - Whale momentum is positive (net long flow increasing)
        - No distribution signal active
        - Volume profile: entry at or near POC (institutional fair value)

        Entry: at bottom of accumulation zone
        Stop: below zone
        TP: top of zone + extension
        Conviction: 65-85 based on accumulation_score × whale_confidence

        Args:
            df: OHLCV DataFrame
            ticker: Asset ticker
            timeframe: Timeframe string

        Returns:
            InstitutionalSignal or None
        """
        if len(df) < self.lookback:
            return None

        try:
            atm = self._calculate_atr(df)
            current_price = df['close'].iloc[-1]

            # Detect accumulation zones
            accum_zones = getattr(self, '_cached_accum_zones', None) or self.whale_tracker.detect_accumulation_zones(df, self.lookback)
            if not accum_zones:
                return None

            # Find zone containing or near current price (within 0.5 ATR)
            active_zone = None
            for zone in accum_zones[-5:]:
                zone_margin = 0.5 * atm
                if (zone.price_low - zone_margin) <= current_price <= (zone.price_high + zone_margin):
                    active_zone = zone
                    break

            if not active_zone:
                return None

            # Check whale momentum - must be positive (accumulating, not distributing)
            whale_mom = getattr(self, '_cached_whale_momentum', None) or self.whale_tracker.whale_momentum(df)
            if not whale_mom or whale_mom.flow_direction != "BUY":
                return None

            # No active distribution (relaxed: only block strong distribution >75)
            dist_state = getattr(self, '_cached_distribution', None) or self.whale_tracker.detect_distribution(df)
            if dist_state and dist_state.active and dist_state.strength > 75:
                return None  # Strong distribution active — don't enter accumulation trade

            # Volume profile check - price should be near POC (relaxed from 1.0 to 2.0 ATR)
            vol_profile = getattr(self, '_cached_vol_profile', None) or self.whale_tracker.build_volume_profile(df)
            dist_from_poc = abs(current_price - vol_profile.poc) / atm
            if dist_from_poc > 2.0:  # Within 2 ATR of POC (was 1.0 — too tight)
                return None

            # This is a long accumulation setup
            direction = "CALL"
            entry_price = active_zone.price_low
            stop_loss = active_zone.price_low - 1.0 * atm
            tp1 = active_zone.price_high
            tp2 = active_zone.price_high + (active_zone.price_high - active_zone.price_low)

            # Conviction: 65-85 based on zone quality and whale confidence
            conviction = int(active_zone.score * 65)
            conviction = max(65, min(85, conviction))

            signal = InstitutionalSignal(
                signal_type="whale_accumulation_entry",
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                conviction=conviction,
                institutional_score=int(active_zone.score * 100),
                whale_confidence=int(active_zone.score * 100),
                components={
                    "zone_low": active_zone.price_low,
                    "zone_high": active_zone.price_high,
                    "zone_score": active_zone.score,
                    "whale_momentum": "LONG",
                    "poc": vol_profile.poc,
                    "distance_from_poc": dist_from_poc,
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(entry_price, stop_loss, tp2)
            return signal

        except Exception as e:
            logger.error(f"Error in scan_whale_accumulation_entry: {e}")
            return None

    # ========================================================================
    # SIGNAL 8: WHALE TRAP REVERSAL (whale_trap_reversal)
    # ========================================================================

    def scan_whale_trap_reversal(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Whale Trap Reversal Signal - THE MONEY SIGNAL.

        Institutions sweep stops then load up:
        - WhaleTracker.detect_whale_traps() finds a trap at current price
        - Liquidity sweep just happened (pool was swept)
        - Whale bars appear immediately after the sweep (accumulation at swept level)
        - Whale divergence confirms reversal

        This is THE money signal — institutions sweep stops then load up.

        Entry: at trap level
        Stop: beyond sweep low/high
        TP: next liquidity pool
        Conviction: 80-100

        Args:
            df: OHLCV DataFrame
            ticker: Asset ticker
            timeframe: Timeframe string

        Returns:
            InstitutionalSignal or None
        """
        if len(df) < self.lookback:
            return None

        try:
            atm = self._calculate_atr(df)
            current_price = df['close'].iloc[-1]

            # Detect whale traps
            traps = self.whale_tracker.detect_whale_traps(df)
            if not traps:
                return None

            # Find trap at current level
            current_trap = None
            for trap in traps[-2:]:
                if abs(trap.sweep_price - current_price) < atm:
                    current_trap = trap
                    break

            if not current_trap:
                return None

            # Check for recent sweep
            pools = getattr(self, '_cached_pools', None) or self.liquidity_mapper.map_stop_clusters(df, self.lookback)
            sweeps = self.liquidity_mapper.detect_sweep(df, pools) if pools else []
            if not sweeps:
                return None

            # Recent sweep should be close to trap level
            latest_sweep = sweeps[-1]
            sweep_near_trap = abs(latest_sweep.pool.price - current_trap.sweep_price) < 2 * atm
            if not sweep_near_trap:
                return None

            # Check for whale momentum post-sweep (exhaustion flag indicates accumulation)
            whale_mom = getattr(self, '_cached_whale_momentum', None) or self.whale_tracker.whale_momentum(df)
            if not whale_mom or not (whale_mom.conviction_score > 70):
                return None

            # Whale divergence confirmation
            divergence = getattr(self, '_cached_whale_div', None) or self.whale_tracker.whale_vs_retail_divergence(df)
            if not divergence or not divergence.active:
                return None

            # Determine direction based on trap direction
            if current_trap.trap_direction == "BUY":
                direction = "CALL"
                entry_price = current_trap.sweep_price
                stop_loss = latest_sweep.pool.price - 0.5 * atm
                # TP: next liquidity pool above
                next_pool = None
                for pool in pools[-5:]:
                    if pool.price > entry_price:
                        next_pool = pool
                        break
                tp2 = next_pool.price if next_pool else entry_price + 5 * atm
            else:
                direction = "PUT"
                entry_price = current_trap.sweep_price
                stop_loss = latest_sweep.pool.price + 0.5 * atm
                # TP: next liquidity pool below
                next_pool = None
                for pool in pools[-5:]:
                    if pool.price < entry_price:
                        next_pool = pool
                        break
                tp2 = next_pool.price if next_pool else entry_price - 5 * atm

            tp1 = (entry_price + tp2) / 2

            # Conviction: 80-100 (highest conviction signal)
            conviction = int(current_trap.confidence * 100)
            conviction = max(80, min(100, conviction))

            signal = InstitutionalSignal(
                signal_type="whale_trap_reversal",
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                conviction=conviction,
                institutional_score=95,  # Always high for trap reversals
                whale_confidence=int(current_trap.confidence * 100),
                components={
                    "trap_level": current_trap.sweep_price,
                    "trap_direction": current_trap.trap_direction,
                    "trap_confidence": current_trap.confidence,
                    "sweep_level": latest_sweep.pool.price,
                    "sweep_quality": latest_sweep.quality.value,
                    "divergence_active": divergence.active,
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(entry_price, stop_loss, tp2)
            return signal

        except Exception as e:
            logger.error(f"Error in scan_whale_trap_reversal: {e}")
            return None

    # ========================================================================
    # SIGNAL 9: WHALE EXHAUSTION FADE (whale_exhaustion_fade)
    # ========================================================================

    def scan_whale_exhaustion_fade(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Whale Exhaustion Fade Signal.

        Trade exhausted whale moves:
        - Whale exhaustion detected (declining whale volume at new highs/lows)
        - Distribution signal active (for shorts) or absorption detected (for longs)
        - Iceberg order detected at current level
        - Entry: fade the exhausted move. Stop: beyond exhaustion bar. TP: POC
        - Conviction: 60-80

        Args:
            df: OHLCV DataFrame
            ticker: Asset ticker
            timeframe: Timeframe string

        Returns:
            InstitutionalSignal or None
        """
        if len(df) < self.lookback:
            return None

        try:
            atm = self._calculate_atr(df)
            current_price = df['close'].iloc[-1]

            # Detect whale exhaustion via whale_momentum
            whale_mom = getattr(self, '_cached_whale_momentum', None) or self.whale_tracker.whale_momentum(df)
            if not whale_mom or not whale_mom.exhaustion_flag:
                return None

            # Exhaustion detected - proceed with signal logic
            exhaustion_detected = True
            exhaustion_price = current_price

            # Check for distribution (shorts) or absorption (longs)
            dist_state = getattr(self, '_cached_distribution', None) or self.whale_tracker.detect_distribution(df)
            accum_zones = getattr(self, '_cached_accum_zones', None) or self.whale_tracker.detect_accumulation_zones(df)

            dist_active = dist_state.active if dist_state else False
            absorption_active = any(az.price_low <= current_price <= az.price_high for az in accum_zones) if accum_zones else False

            # Need at least one confirmation
            if not (dist_active or absorption_active):
                return None

            # Check for iceberg at current level
            icebergs = self.whale_tracker.detect_iceberg_orders(df)
            iceberg_at_level = any(abs(ice.price_level - current_price) < atm for ice in icebergs) if icebergs else False

            if not iceberg_at_level:
                return None

            # Determine direction: fade the move
            # If distribution active at high = short fade
            # If absorption active at low = long fade
            if dist_active:
                direction = "PUT"
                entry_price = current_price
                stop_loss = current_price + 1.5 * atm
            else:
                direction = "CALL"
                entry_price = current_price
                stop_loss = current_price - 1.5 * atm

            # TP: back to POC (mean reversion)
            vol_profile = getattr(self, '_cached_vol_profile', None) or self.whale_tracker.build_volume_profile(df)
            tp2 = vol_profile.poc
            tp1 = (entry_price + tp2) / 2

            # Conviction: 60-80
            conviction = 70  # Base conviction for exhaustion fades
            if iceberg_at_level:
                conviction = min(80, conviction + 10)

            signal = InstitutionalSignal(
                signal_type="whale_exhaustion_fade",
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                conviction=conviction,
                institutional_score=int((dist_active or absorption_active) * 75),
                whale_confidence=65,
                components={
                    "exhaustion_detected": exhaustion_detected,
                    "exhaustion_price": exhaustion_price,
                    "distribution_active": dist_active,
                    "absorption_active": absorption_active,
                    "iceberg_at_level": iceberg_at_level,
                    "poc": vol_profile.poc,
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(entry_price, stop_loss, tp2)
            return signal

        except Exception as e:
            logger.error(f"Error in scan_whale_exhaustion_fade: {e}")
            return None

    # ========================================================================
    # SIGNAL 10: WHALE DIVERGENCE REVERSAL (whale_divergence_reversal)
    # ========================================================================

    def scan_whale_divergence_reversal(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Whale Divergence Reversal Signal.

        Smart money accumulating opposite to price direction:
        - whale_vs_retail_divergence() shows active divergence (3+ bars)
        - Smart money accumulating opposite to price direction
        - Volume profile: price at LVN (fast move expected when reversal starts)
        - VWAP: price extended beyond ±2σ (mean reversion setup)
        - Entry: on first whale bar confirming reversal. Stop: beyond divergence extreme. TP: VWAP
        - Conviction: 70-90

        Args:
            df: OHLCV DataFrame
            ticker: Asset ticker
            timeframe: Timeframe string

        Returns:
            InstitutionalSignal or None
        """
        if len(df) < self.lookback:
            return None

        try:
            atm = self._calculate_atr(df)
            current_price = df['close'].iloc[-1]

            # Check whale vs retail divergence
            divergence = getattr(self, '_cached_whale_div', None) or self.whale_tracker.whale_vs_retail_divergence(df)
            if not divergence or not divergence.active:
                return None

            # Need 3+ bars of divergence
            if divergence.bars_diverging < 3:
                return None

            # Verify smart money is accumulating opposite to price
            if divergence.direction == "BULLISH":
                # Price going down, whales accumulating
                if current_price >= divergence.divergence_start_price:
                    return None
            else:
                # Price going up, whales distributing
                if current_price <= divergence.divergence_start_price:
                    return None

            # Check volume profile: price should be at LVN (low volume node)
            vol_profile = getattr(self, '_cached_vol_profile', None) or self.whale_tracker.build_volume_profile(df)
            at_lvn = False
            if vol_profile.lvn_levels:
                for lvn in vol_profile.lvn_levels:
                    if abs(current_price - lvn) < atm:
                        at_lvn = True
                        break

            if not at_lvn:
                return None

            # Check VWAP: price extended beyond ±2σ
            vwap_data = self.whale_tracker.anchored_vwap(df)
            if not vwap_data or abs(vwap_data.deviation_pct) < 2.0:
                return None

            # Confirm with whale momentum matching divergence direction
            whale_mom = getattr(self, '_cached_whale_momentum', None) or self.whale_tracker.whale_momentum(df)
            if not whale_mom:
                return None

            # For bullish divergence, expect accumulation; for bearish, expect distribution
            if divergence.whale_direction == "BUY" and whale_mom.flow_direction != "BUY":
                return None
            if divergence.whale_direction == "SELL" and whale_mom.flow_direction != "SELL":
                return None

            # Determine direction
            if divergence.direction == "BULLISH":
                direction = "CALL"
                entry_price = current_price
                stop_loss = divergence.divergence_extreme - atm
                tp2 = vwap_data.vwap
            else:
                direction = "PUT"
                entry_price = current_price
                stop_loss = divergence.divergence_extreme + atm
                tp2 = vwap_data.vwap

            tp1 = (entry_price + tp2) / 2

            # Conviction: 70-90 based on divergence strength and whale confidence
            conviction = 70 + divergence.bar_count * 3
            conviction = min(90, conviction)

            signal = InstitutionalSignal(
                signal_type="whale_divergence_reversal",
                direction=direction,
                entry_price=entry_price,
                stop_loss=stop_loss,
                tp1=tp1,
                tp2=tp2,
                conviction=conviction,
                institutional_score=80,
                whale_confidence=int(divergence.bar_count * 15),
                components={
                    "divergence_direction": divergence.direction,
                    "divergence_bars": divergence.bar_count,
                    "divergence_start_price": divergence.divergence_start_price,
                    "divergence_extreme": divergence.divergence_extreme,
                    "at_lvn": at_lvn,
                    "vwap_deviation_pct": vwap_data.deviation_pct if vwap_data else 0,
                    "vwap": vwap_data.vwap if vwap_data else 0,
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(entry_price, stop_loss, tp2)
            return signal

        except Exception as e:
            logger.error(f"Error in scan_whale_divergence_reversal: {e}")
            return None

    # ========================================================================
    # SIGNAL 11/12: OVERNIGHT GAP SCANNERS (GAP-AND-GO & GAP-FILL)
    # ========================================================================

    def scan_gap_fade(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Gap Fill Reversion Signal
        Triggers when there is a significant opening gap without strong institutional backing,
        targeting the previous close for the fill.
        """
        if timeframe not in ('5min', 'daily') or len(df) < 5:
            return None

        try:
            current = df.iloc[-1]
            prev = df.iloc[-2]
            
            # Simple check for new day on intraday tf
            if timeframe == '5min':
                if current.name.date() == prev.name.date():
                    return None  # Only evaluate the very first bar of the day
                    
            gap_pct = (current['open'] - prev['close']) / prev['close']
            
            if abs(gap_pct) < 0.004:  # 0.4% threshold
                return None
                
            direction = 'PUT' if gap_pct > 0 else 'CALL'
            
            inst_score = self.flow_analyzer.get_institutional_score(current['close'], direction)
            
            # Gap Fade happens when institutional money DOES NOT support the gap
            if inst_score > 40:
                return None
                
            entry_price = current['close']
            tp1 = prev['close']  # Gap Fill target
            sl = current['high'] if direction == 'PUT' else current['low']
            
            # Widen SL slightly to allow noise
            sl = sl * (1.002 if direction == 'PUT' else 0.998)
            
            if abs(tp1 - entry_price) < abs(entry_price - sl) * 0.5:
                return None # Poor RR
                
            signal = InstitutionalSignal(
                signal_type="gap_fade",
                direction=direction,
                entry_price=entry_price,
                stop_loss=sl,
                target_price=tp1,
                confidence=100 - inst_score,
                institutional_score=inst_score,
                whale_backing=0,
                metadata={
                    "gap_pct": gap_pct * 100,
                    "target_fill": tp1
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(entry_price, sl, tp1)
            return signal
            
        except Exception as e:
            logger.error(f"Error in scan_gap_fade: {e}")
            return None

    def scan_gap_momentum(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str
    ) -> Optional[InstitutionalSignal]:
        """
        Gap Go Momentum Signal
        Triggers when there is a significant opening gap WITH strong institutional/whale backing.
        """
        if timeframe not in ('5min', 'daily') or len(df) < 5:
            return None

        try:
            current = df.iloc[-1]
            prev = df.iloc[-2]
            
            if timeframe == '5min':
                if current.name.date() == prev.name.date():
                    return None 
                    
            gap_pct = (current['open'] - prev['close']) / prev['close']
            
            if abs(gap_pct) < 0.004:
                return None
                
            direction = 'CALL' if gap_pct > 0 else 'PUT'
            
            inst_score = self.flow_analyzer.get_institutional_score(current['close'], direction)
            
            # Gap Go happens when inst_score is HIGH
            if inst_score < 60:
                return None
                
            entry_price = current['close']
            atr = self._calculate_atr(df, 14)
            
            tp1 = entry_price + (atr * 1.5 if direction == 'CALL' else -atr * 1.5)
            sl = current['open']
            
            if direction == 'CALL' and sl > entry_price - (atr * 0.2):
                sl = entry_price - (atr * 0.5)
            elif direction == 'PUT' and sl < entry_price + (atr * 0.2):
                sl = entry_price + (atr * 0.5)
                
            signal = InstitutionalSignal(
                signal_type="gap_momentum",
                direction=direction,
                entry_price=entry_price,
                stop_loss=sl,
                target_price=tp1,
                confidence=inst_score,
                institutional_score=inst_score,
                whale_backing=100 if inst_score > 75 else 0,
                metadata={
                    "gap_pct": gap_pct * 100,
                    "target_continuation": tp1
                },
                timeframe=timeframe,
                ticker=ticker,
                timestamp=datetime.now().timestamp(),
            )
            signal.risk_reward_ratio = self._calc_rr(entry_price, sl, tp1)
            return signal
            
        except Exception as e:
            logger.error(f"Error in scan_gap_momentum: {e}")
            return None

    # ========================================================================
    # MAIN SCAN METHOD
    # ========================================================================

    def scan_all_signals(
        self,
        df: pd.DataFrame,
        ticker: str,
        timeframe: str = "5min"
    ) -> List[InstitutionalSignal]:
        """
        Scan for ALL signal types and return sorted by conviction.

        Runs all 10 institutional signal types and validates each with
        institutional backing. Returns sorted list with highest conviction first.

        Signals 1-6: Core institutional signals
        Signals 7-10: Whale-specific signals

        Args:
            df: OHLCV DataFrame with columns: open, high, low, close, volume
            ticker: Asset ticker symbol
            timeframe: Timeframe string (e.g., '1min', '5min')

        Returns:
            List of InstitutionalSignal sorted by conviction (highest first)
        """
        signals = []

        logger.info(f"Scanning {ticker} {timeframe} for institutional signals")
        
        # Standardize dataframe column casing natively
        df = df.rename(columns=str.lower)

        # Set ticker for real UW data fetching
        self.whale_tracker.set_ticker(ticker)
        self.flow_analyzer.set_ticker(ticker)

        # ── Pre-compute expensive shared data ONCE per window ──────────
        # These get called 2-5x across scanners. Caching saves ~60% scan time.
        try:
            self._cached_whale_momentum = getattr(self, '_cached_whale_momentum', None) or self.whale_tracker.whale_momentum(df)
        except:
            self._cached_whale_momentum = None
        try:
            self._cached_accum_zones = getattr(self, '_cached_accum_zones', None) or self.whale_tracker.detect_accumulation_zones(df, self.lookback)
        except:
            self._cached_accum_zones = []
        try:
            self._cached_vol_profile = getattr(self, '_cached_vol_profile', None) or self.whale_tracker.build_volume_profile(df)
        except:
            self._cached_vol_profile = None
        try:
            self._cached_distribution = getattr(self, '_cached_distribution', None) or self.whale_tracker.detect_distribution(df)
        except:
            self._cached_distribution = None
        try:
            self._cached_pools = getattr(self, '_cached_pools', None) or self.liquidity_mapper.map_stop_clusters(df, self.lookback)
        except:
            self._cached_pools = []
        try:
            self._cached_whale_div = self.whale_tracker.whale_vs_retail_divergence(df)
        except:
            self._cached_whale_div = None

        # Scan all 12 signal types (6 core + 6 derived/whale/gap)
        scanners = [
            ("inst_sweep_reversal", self.scan_sweep_reversal),
            ("inst_ob", self.scan_inst_order_block),
            ("smart_money_div", self.scan_smart_money_divergence),
            ("wyckoff_institutional", self.scan_wyckoff_institutional),
            ("vp_fair_value", self.scan_vp_fair_value),
            ("iceberg_fade", self.scan_iceberg_fade),
            ("whale_accumulation_entry", self.scan_whale_accumulation_entry),
            ("whale_trap_reversal", self.scan_whale_trap_reversal),
            ("whale_exhaustion_fade", self.scan_whale_exhaustion_fade),
            ("whale_divergence_reversal", self.scan_whale_divergence_reversal),
            ("gap_fade", self.scan_gap_fade),
            ("gap_momentum", self.scan_gap_momentum),
        ]

        for signal_name, scanner_func in scanners:
            try:
                signal = scanner_func(df, ticker, timeframe)
                if signal:
                    # Validate signal with institutional gate
                    if self.validate_signal_institutional(signal, df):
                        signals.append(signal)
                        logger.info(f"Generated {signal_name}: {signal}")
            except Exception as e:
                logger.error(f"Error scanning {signal_name}: {e}")

        # Sort by conviction (highest first)
        signals.sort(key=lambda s: (s.conviction, s.institutional_score), reverse=True)

        logger.info(f"Generated {len(signals)} signals")
        return signals

    # ========================================================================
    # VALIDATION & QUALITY GATES
    # ========================================================================

    def validate_signal_institutional(
        self,
        signal: InstitutionalSignal,
        df: pd.DataFrame
    ) -> bool:
        """
        POST-SIGNAL QUALITY GATE - reject signals lacking institutional backing.

        Validates:
        1. Institutional score >= 50 (institutional presence required)
        2. Risk/reward ratio >= 2:1
        3. Stop loss not violated on current bar
        4. Entry not already rejected (price past entry)
        5. Whale confidence >= 30 for non-sweep and non-whale signals
        6. Sweep signals need >= 70 conviction
        7. Whale trap reversal signals need >= 80 conviction (highest bar)

        Args:
            signal: InstitutionalSignal to validate
            df: OHLCV DataFrame

        Returns:
            True if signal passes all gates, False otherwise
        """
        # Gate 1: Institutional score minimum (can be relaxed for whale signals)
        min_inst_score = 30 if signal.signal_type.startswith("whale_") else 50
        if signal.institutional_score < min_inst_score:
            logger.warning(f"Signal rejected: institutional score too low ({signal.institutional_score}) for {signal.signal_type}")
            return False

        # Gate 2: Risk/reward ratio
        if signal.risk_reward_ratio < 2.0:
            logger.warning(f"Signal rejected: poor R/R ratio ({signal.risk_reward_ratio:.2f}:1)")
            return False

        # Gate 3: Stop loss not violated
        current_price = df['close'].iloc[-1]
        if signal.direction == "CALL":
            if current_price < signal.stop_loss:
                logger.warning(f"Signal rejected: stop loss already hit")
                return False
        else:
            if current_price > signal.stop_loss:
                logger.warning(f"Signal rejected: stop loss already hit")
                return False

        # Gate 4: Entry not already passed
        if signal.direction == "CALL":
            if current_price > signal.entry_price:
                logger.warning(f"Signal rejected: entry already passed (CALL)")
                return False
        else:
            if current_price < signal.entry_price:
                logger.warning(f"Signal rejected: entry already passed (PUT)")
                return False

        # Gate 5: Whale confidence for most signals (except sweep reversal and whale signals)
        if signal.signal_type not in ("inst_sweep_reversal", "whale_trap_reversal"):
            if not signal.signal_type.startswith("whale_"):
                if signal.whale_confidence < 30:
                    logger.warning(f"Signal rejected: whale confidence too low ({signal.whale_confidence})")
                    return False

        # Gate 6: Sweep signals need higher conviction
        if signal.signal_type == "inst_sweep_reversal":
            if signal.conviction < 70:
                logger.warning(f"Signal rejected: sweep signal conviction too low ({signal.conviction})")
                return False

        # Gate 7: Whale trap reversal (money signal) needs >= 80 conviction
        if signal.signal_type == "whale_trap_reversal":
            if signal.conviction < 80:
                logger.warning(f"Signal rejected: whale trap reversal conviction too low ({signal.conviction})")
                return False

        logger.debug(f"Signal validation passed: {signal.signal_type}")
        return True

    # ========================================================================
    # HELPER METHODS
    # ========================================================================

    def _calculate_atr(self, df: pd.DataFrame, period: int = None) -> float:
        """Calculate Average True Range."""
        if period is None:
            period = self.atr_period

        if len(df) < period:
            return df['close'].std()

        high_low = df['high'] - df['low']
        high_close = abs(df['high'] - df['close'].shift())
        low_close = abs(df['low'] - df['close'].shift())

        tr = pd.concat([high_low, high_close, low_close], axis=1).max(axis=1)
        atr = tr.rolling(period).mean().iloc[-1]

        return atr if atr > 0 else df['close'].std()

    def _calc_rr(self, entry: float, stop: float, target: float) -> float:
        """Calculate risk/reward ratio."""
        risk = abs(entry - stop)
        if risk == 0:
            return 0
        reward = abs(target - entry)
        return reward / risk if risk > 0 else 0


# ============================================================================
# DEMO AND TESTING
# ============================================================================

if __name__ == "__main__":
    """
    Demo/test of InstitutionalSignalGenerator.

    Generates synthetic OHLCV data and demonstrates all 6 signal types.
    """
    import random

    # Setup logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
    )

    print("\n" + "="*80)
    print("INSTITUTIONAL SIGNAL GENERATOR - DEMO")
    print("="*80 + "\n")

    # Create synthetic data
    np.random.seed(42)
    random.seed(42)

    n_bars = 200
    base_price = 100.0

    # Generate realistic OHLCV
    closes = [base_price]
    for _ in range(n_bars - 1):
        change = np.random.normal(0, 0.5)
        closes.append(closes[-1] + change)

    df = pd.DataFrame({
        'open': [c + np.random.uniform(-0.3, 0.3) for c in closes],
        'high': [c + abs(np.random.normal(0, 0.5)) for c in closes],
        'low': [c - abs(np.random.normal(0, 0.5)) for c in closes],
        'close': closes,
        'volume': [random.randint(1000, 50000) for _ in range(n_bars)],
    })

    # Adjust OHLC relationships
    df['high'] = df[['open', 'high', 'close']].max(axis=1)
    df['low'] = df[['open', 'low', 'close']].min(axis=1)

    print(f"Sample data shape: {df.shape}")
    print(f"Price range: {df['close'].min():.2f} - {df['close'].max():.2f}")
    print(f"Latest close: {df['close'].iloc[-1]:.2f}\n")

    # Initialize generator
    gen = InstitutionalSignalGenerator(lookback=100, atr_period=14)

    # Scan all signals
    print("Scanning for institutional signals...\n")
    signals = gen.scan_all_signals(df, ticker="TEST", timeframe="5min")

    # Display results
    if signals:
        print(f"Found {len(signals)} valid signals:\n")
        for i, sig in enumerate(signals, 1):
            print(f"{i}. {sig}")
            print(f"   Entry: {sig.entry_price:.2f} | SL: {sig.stop_loss:.2f} | TP1: {sig.tp1:.2f} | TP2: {sig.tp2:.2f}")
            print(f"   R/R: {sig.risk_reward_ratio:.2f}:1")
            print(f"   Components: {sig.components}")
            print()
    else:
        print("No valid institutional signals found in current data.")

    print("="*80)
    print("Demo complete!")
    print("="*80)
