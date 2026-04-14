"""
Whale Intent Classifier — PRO-LEVEL Institutional Execution Layer
=================================================================

Replaces simple confirm/deny whale logic with a 3-state intent model:

  ACCUMULATION — Smart money building position BEFORE move
  DISTRIBUTION — Smart money exiting, move likely ending
  AGGRESSION   — Move happening NOW, directional conviction

Each state maps to different execution behavior per ICT setup type.

Uses real Unusual Whales data when available, OHLCV fallback when not.
"""

from dataclasses import dataclass, field
from typing import Optional, List, Dict, Tuple
from enum import Enum
import logging
import time

logger = logging.getLogger(__name__)


# ============================================================================
# ENUMS
# ============================================================================

class WhaleState(str, Enum):
    """Three whale intent states."""
    ACCUMULATION = "ACCUMULATION"   # Positioning before move
    DISTRIBUTION = "DISTRIBUTION"   # Exiting, move ending
    AGGRESSION = "AGGRESSION"       # Move in progress NOW
    NEUTRAL = "NEUTRAL"             # No clear intent


class ExecutionGrade(str, Enum):
    """Trade execution quality grade."""
    A_PLUS = "A+"    # Structure + whale alignment → aggressive entry
    A = "A"          # Structure only → standard entry
    B = "B"          # Conflict → reduced or skip


# ============================================================================
# DATA CLASSES
# ============================================================================

@dataclass
class WhaleIntent:
    """Complete whale intent classification for a ticker."""
    state: WhaleState
    direction: str              # BUY or SELL (what whales are doing)
    confidence: float           # 0-100
    evidence: List[str]         # What triggered this classification

    # Component scores (0-100 each)
    accumulation_score: float = 0.0
    distribution_score: float = 0.0
    aggression_score: float = 0.0

    # Raw data summary
    net_premium: float = 0.0          # $ from options flow
    dark_pool_volume: int = 0         # Total DP shares
    sweep_count: int = 0              # Aggressive sweeps
    block_count: int = 0              # Large blocks
    put_call_ratio: float = 1.0       # P/C ratio
    dp_sentiment: str = "NEUTRAL"     # ACCUMULATION/DISTRIBUTION/NEUTRAL

    def __repr__(self):
        return (f"WhaleIntent({self.state.value} {self.direction} "
                f"conf={self.confidence:.0f}% | "
                f"A={self.accumulation_score:.0f} D={self.distribution_score:.0f} "
                f"G={self.aggression_score:.0f})")


@dataclass
class ExecutionPlan:
    """How to execute a trade based on whale intent + ICT setup."""
    grade: ExecutionGrade
    entry_mode: str             # "limit_inside_ob", "confirmation_candle", "market", "skip"
    size_multiplier: float      # 0.0 (skip) to 1.5
    tp_mode: str                # "extended_runner", "standard_scalp", "quick_tp"
    stop_mode: str              # "tight", "standard", "wide"
    reason: str                 # Why this execution plan

    @property
    def should_trade(self) -> bool:
        return self.size_multiplier > 0


# ============================================================================
# STRATEGY TYPES (ICT setups)
# ============================================================================

class ICTSetupType(str, Enum):
    """ICT/SMC setup categories for whale mapping."""
    MOMENTUM_OB = "momentum_ob"          # Order block with trend
    BREAKER_REVERSAL = "breaker_reversal" # Breaker block reversal
    FVG_REENTRY = "fvg_reentry"          # Fair value gap re-entry
    SWEEP_REVERSAL = "sweep_reversal"    # Liquidity sweep + reversal
    CONTINUATION = "continuation"         # Trend continuation
    JUDAS_SWING = "judas_swing"          # Judas swing (fake move)


# ============================================================================
# WHALE STATE → STRATEGY BEHAVIOR MAP
# ============================================================================

# Key insight: whale state changes HOW you trade the ICT setup
# Not just whether you trade it

STRATEGY_BEHAVIOR_MAP = {
    # ── MOMENTUM ORDER BLOCK ──
    # Whales accumulating = they're loading before the move, enter early
    # Whales aggressive = move is happening, enter breakout
    # Whales distributing = OB is a trap, block trade
    ICTSetupType.MOMENTUM_OB: {
        WhaleState.ACCUMULATION: ExecutionPlan(
            grade=ExecutionGrade.A_PLUS,
            entry_mode="limit_inside_ob",
            size_multiplier=1.4,
            tp_mode="extended_runner",
            stop_mode="tight",
            reason="Whales loading before move — enter early inside OB"
        ),
        WhaleState.AGGRESSION: ExecutionPlan(
            grade=ExecutionGrade.A_PLUS,
            entry_mode="market",
            size_multiplier=1.25,
            tp_mode="standard_scalp",
            stop_mode="standard",
            reason="Whales aggressive — breakout entry, don't wait"
        ),
        WhaleState.DISTRIBUTION: ExecutionPlan(
            grade=ExecutionGrade.B,
            entry_mode="skip",
            size_multiplier=0.0,
            tp_mode="quick_tp",
            stop_mode="tight",
            reason="Whales distributing — OB is likely a trap, BLOCK"
        ),
        WhaleState.NEUTRAL: ExecutionPlan(
            grade=ExecutionGrade.A,
            entry_mode="confirmation_candle",
            size_multiplier=1.0,
            tp_mode="standard_scalp",
            stop_mode="standard",
            reason="No whale signal — standard ICT execution"
        ),
    },

    # ── BREAKER BLOCK REVERSAL ──
    # Whales distributing = STRONG reversal confirmation (they're exiting)
    # Whales accumulating = trend continues, avoid fading it
    # Whales aggressive = don't fade active momentum
    ICTSetupType.BREAKER_REVERSAL: {
        WhaleState.DISTRIBUTION: ExecutionPlan(
            grade=ExecutionGrade.A_PLUS,
            entry_mode="limit_inside_ob",
            size_multiplier=1.5,
            tp_mode="extended_runner",
            stop_mode="tight",
            reason="Whales distributing — strong reversal signal, enter aggressive"
        ),
        WhaleState.ACCUMULATION: ExecutionPlan(
            grade=ExecutionGrade.B,
            entry_mode="skip",
            size_multiplier=0.0,
            tp_mode="quick_tp",
            stop_mode="tight",
            reason="Whales still accumulating — trend likely continues, don't fade"
        ),
        WhaleState.AGGRESSION: ExecutionPlan(
            grade=ExecutionGrade.B,
            entry_mode="skip",
            size_multiplier=0.0,
            tp_mode="quick_tp",
            stop_mode="tight",
            reason="Whales aggressive in current direction — don't fade strength"
        ),
        WhaleState.NEUTRAL: ExecutionPlan(
            grade=ExecutionGrade.A,
            entry_mode="confirmation_candle",
            size_multiplier=0.8,
            tp_mode="standard_scalp",
            stop_mode="standard",
            reason="No whale signal — standard breaker execution, reduced size"
        ),
    },

    # ── FVG RE-ENTRY ──
    # Accumulation at FVG level = institutional fair value, enter
    # Distribution = FVG will likely fail
    # Aggression = price may gap through, enter on retest
    ICTSetupType.FVG_REENTRY: {
        WhaleState.ACCUMULATION: ExecutionPlan(
            grade=ExecutionGrade.A_PLUS,
            entry_mode="limit_inside_ob",
            size_multiplier=1.3,
            tp_mode="extended_runner",
            stop_mode="tight",
            reason="Whales accumulating at FVG — institutional fair value confirmed"
        ),
        WhaleState.AGGRESSION: ExecutionPlan(
            grade=ExecutionGrade.A,
            entry_mode="confirmation_candle",
            size_multiplier=1.0,
            tp_mode="standard_scalp",
            stop_mode="standard",
            reason="Whales aggressive — enter FVG retest on confirmation"
        ),
        WhaleState.DISTRIBUTION: ExecutionPlan(
            grade=ExecutionGrade.B,
            entry_mode="skip",
            size_multiplier=0.0,
            tp_mode="quick_tp",
            stop_mode="tight",
            reason="Whales distributing — FVG will likely fail"
        ),
        WhaleState.NEUTRAL: ExecutionPlan(
            grade=ExecutionGrade.A,
            entry_mode="confirmation_candle",
            size_multiplier=1.0,
            tp_mode="standard_scalp",
            stop_mode="standard",
            reason="No whale signal — standard FVG execution"
        ),
    },

    # ── SWEEP REVERSAL ──
    # Distribution after sweep = possible reversal BUT needs confirmation
    # Accumulation at sweep = they're buying the sweep, strong reversal
    # Aggression = sweep is real breakout, don't fade
    ICTSetupType.SWEEP_REVERSAL: {
        WhaleState.DISTRIBUTION: ExecutionPlan(
            grade=ExecutionGrade.A,
            entry_mode="confirmation_candle",
            size_multiplier=0.7,
            tp_mode="standard_scalp",
            stop_mode="tight",
            reason="Whales distributing post-sweep — confirmation required, reduced size"
        ),
        WhaleState.ACCUMULATION: ExecutionPlan(
            grade=ExecutionGrade.A_PLUS,
            entry_mode="limit_inside_ob",
            size_multiplier=1.4,
            tp_mode="extended_runner",
            stop_mode="tight",
            reason="Whales accumulating at sweep level — strong reversal"
        ),
        WhaleState.AGGRESSION: ExecutionPlan(
            grade=ExecutionGrade.B,
            entry_mode="skip",
            size_multiplier=0.0,
            tp_mode="quick_tp",
            stop_mode="tight",
            reason="Whales aggressive through sweep — not a fake, real breakout"
        ),
        WhaleState.NEUTRAL: ExecutionPlan(
            grade=ExecutionGrade.A,
            entry_mode="confirmation_candle",
            size_multiplier=1.0,
            tp_mode="standard_scalp",
            stop_mode="standard",
            reason="No whale signal — standard sweep reversal execution"
        ),
    },

    # ── CONTINUATION ──
    # Accumulation + trend = loading for next leg
    # Aggression = trend accelerating
    # Distribution = trend ending, don't chase
    ICTSetupType.CONTINUATION: {
        WhaleState.ACCUMULATION: ExecutionPlan(
            grade=ExecutionGrade.A_PLUS,
            entry_mode="limit_inside_ob",
            size_multiplier=1.3,
            tp_mode="extended_runner",
            stop_mode="standard",
            reason="Whales accumulating in trend — next leg loading"
        ),
        WhaleState.AGGRESSION: ExecutionPlan(
            grade=ExecutionGrade.A,
            entry_mode="market",
            size_multiplier=1.1,
            tp_mode="standard_scalp",
            stop_mode="standard",
            reason="Whales aggressive — trend accelerating, ride it"
        ),
        WhaleState.DISTRIBUTION: ExecutionPlan(
            grade=ExecutionGrade.B,
            entry_mode="skip",
            size_multiplier=0.0,
            tp_mode="quick_tp",
            stop_mode="tight",
            reason="Whales distributing — trend ending, don't chase"
        ),
        WhaleState.NEUTRAL: ExecutionPlan(
            grade=ExecutionGrade.A,
            entry_mode="confirmation_candle",
            size_multiplier=1.0,
            tp_mode="standard_scalp",
            stop_mode="standard",
            reason="No whale signal — standard continuation execution"
        ),
    },

    # ── JUDAS SWING ──
    # Distribution into the fake move = classic Judas
    # Accumulation opposite to swing = smart money fading it
    # Aggression = might not be a Judas, real move
    ICTSetupType.JUDAS_SWING: {
        WhaleState.DISTRIBUTION: ExecutionPlan(
            grade=ExecutionGrade.A_PLUS,
            entry_mode="limit_inside_ob",
            size_multiplier=1.5,
            tp_mode="extended_runner",
            stop_mode="tight",
            reason="Whales distributing into Judas — confirmed fake move"
        ),
        WhaleState.ACCUMULATION: ExecutionPlan(
            grade=ExecutionGrade.A,
            entry_mode="confirmation_candle",
            size_multiplier=1.2,
            tp_mode="standard_scalp",
            stop_mode="standard",
            reason="Whales accumulating opposite — likely Judas"
        ),
        WhaleState.AGGRESSION: ExecutionPlan(
            grade=ExecutionGrade.B,
            entry_mode="skip",
            size_multiplier=0.0,
            tp_mode="quick_tp",
            stop_mode="tight",
            reason="Whales aggressive in swing direction — might be real, not Judas"
        ),
        WhaleState.NEUTRAL: ExecutionPlan(
            grade=ExecutionGrade.A,
            entry_mode="confirmation_candle",
            size_multiplier=1.0,
            tp_mode="standard_scalp",
            stop_mode="standard",
            reason="No whale signal — standard Judas execution"
        ),
    },
}


# ============================================================================
# WHALE INTENT CLASSIFIER
# ============================================================================

class WhaleIntentClassifier:
    """
    Classifies whale activity into ACCUMULATION / DISTRIBUTION / AGGRESSION.

    Uses real UW data when available:
    - Dark pool prints + NBBO positioning → accumulation/distribution
    - Options flow sweeps + blocks → aggression
    - Net premium + put/call ratio → direction

    Falls back to OHLCV whale tracker data when UW not available.
    """

    def __init__(self, uw_client=None):
        self.uw_client = uw_client
        self._cache = {}
        self._cache_time = 0

    def classify(self, ticker: str, whale_tracker=None,
                 institutional_flow=None) -> WhaleIntent:
        """
        Classify whale intent for a ticker.

        Args:
            ticker: Stock symbol
            whale_tracker: WhaleTracker instance (for cached UW data)
            institutional_flow: InstitutionalFlowAnalyzer (for cached UW data)

        Returns:
            WhaleIntent with state, direction, confidence, and evidence
        """
        evidence = []
        accum_score = 0.0
        distrib_score = 0.0
        aggression_score = 0.0
        direction = "NEUTRAL"
        net_premium = 0.0
        dp_volume = 0
        sweep_count = 0
        block_count = 0
        put_call_ratio = 1.0
        dp_sentiment = "NEUTRAL"

        # === Try real UW data first ===
        has_uw = False

        if whale_tracker and whale_tracker.has_real_data:
            whale_tracker._refresh_uw_data()
            cache = whale_tracker._uw_cache

            whale_live = cache.get('whale_signal_live')
            sentiment = cache.get('institutional_sentiment')
            dp_prints = cache.get('dark_pool_prints') or []
            flow_alerts = cache.get('flow_alerts') or []

            if whale_live or sentiment:
                has_uw = True

            # ── ACCUMULATION signals ──

            # 1. Dark pool prints at level (passive buying/selling)
            if dp_prints:
                dp_volume = sum(p.volume for p in dp_prints if not p.canceled)

                # Accumulation: DP prints above NBBO mid = buying
                above_mid = sum(1 for p in dp_prints
                               if p.price >= (p.nbbo_bid + p.nbbo_ask) / 2)
                below_mid = len(dp_prints) - above_mid

                if above_mid > below_mid * 1.3:
                    accum_score += 35
                    evidence.append(f"DP prints above NBBO mid ({above_mid}/{len(dp_prints)})")
                    dp_sentiment = "ACCUMULATION"
                elif below_mid > above_mid * 1.3:
                    distrib_score += 35
                    evidence.append(f"DP prints below NBBO mid ({below_mid}/{len(dp_prints)})")
                    dp_sentiment = "DISTRIBUTION"

                # Large DP volume = institutional positioning (accumulation)
                if dp_volume > 5_000_000:
                    accum_score += 15
                    evidence.append(f"Heavy DP volume: {dp_volume:,} shares")

            # 2. Iceberg absorption (from whale tracker iceberg detection)
            if whale_tracker._uw_cache.get('dark_pool_levels'):
                dp_levels = whale_tracker._uw_cache['dark_pool_levels']
                accum_levels = [l for l in dp_levels
                               if l.direction_bias == 'ACCUMULATION']
                if len(accum_levels) >= 3:
                    accum_score += 20
                    evidence.append(f"{len(accum_levels)} accumulation levels detected")

            # ── DISTRIBUTION signals ──

            if sentiment:
                net_premium = sentiment.net_premium
                put_call_ratio = sentiment.put_call_ratio

                # 3. Opposite side premium (large put premium = distribution)
                if net_premium < -5_000_000:
                    distrib_score += 40
                    evidence.append(f"Heavy put premium: ${net_premium:,.0f}")
                elif net_premium < -1_000_000:
                    distrib_score += 20
                    evidence.append(f"Moderate put premium: ${net_premium:,.0f}")
                elif net_premium > 5_000_000:
                    accum_score += 25
                    evidence.append(f"Heavy call premium: ${net_premium:,.0f}")
                elif net_premium > 1_000_000:
                    accum_score += 15
                    evidence.append(f"Moderate call premium: ${net_premium:,.0f}")

                # 4. High P/C ratio = distribution
                if put_call_ratio > 1.5:
                    distrib_score += 20
                    evidence.append(f"Elevated P/C ratio: {put_call_ratio:.2f}")
                elif put_call_ratio < 0.6:
                    accum_score += 15
                    evidence.append(f"Low P/C ratio: {put_call_ratio:.2f} (call-heavy)")

                # 5. DP sentiment from UW
                if sentiment.dark_pool_sentiment == "DISTRIBUTION":
                    distrib_score += 25
                    evidence.append("UW dark pool sentiment: DISTRIBUTION")
                elif sentiment.dark_pool_sentiment == "ACCUMULATION":
                    accum_score += 25
                    evidence.append("UW dark pool sentiment: ACCUMULATION")

            # ── AGGRESSION signals ──

            if flow_alerts:
                # 6. Sweep orders = aggressive institutional flow
                sweeps = [a for a in flow_alerts if a.has_sweep]
                sweep_count = len(sweeps)
                if sweep_count >= 5:
                    aggression_score += 45
                    evidence.append(f"{sweep_count} sweep orders (highly aggressive)")
                elif sweep_count >= 3:
                    aggression_score += 30
                    evidence.append(f"{sweep_count} sweep orders (aggressive)")
                elif sweep_count >= 1:
                    aggression_score += 15
                    evidence.append(f"{sweep_count} sweep order(s)")

                # 7. Large blocks = directional conviction
                blocks = [a for a in flow_alerts if a.total_size > 1000]
                block_count = len(blocks)
                if block_count >= 3:
                    aggression_score += 25
                    evidence.append(f"{block_count} large block trades")
                elif block_count >= 1:
                    aggression_score += 10
                    evidence.append(f"{block_count} block trade(s)")

                # 8. Total premium momentum
                total_premium = sum(a.total_premium for a in flow_alerts)
                if total_premium > 2_000_000:
                    aggression_score += 15
                    evidence.append(f"Total alert premium: ${total_premium:,.0f}")

            # Direction from whale live signal
            if whale_live:
                if whale_live.direction == "BULLISH":
                    direction = "BUY"
                elif whale_live.direction == "BEARISH":
                    direction = "SELL"

        # === OHLCV FALLBACK ===
        if not has_uw and whale_tracker:
            # Compute fresh whale data from OHLCV
            try:
                import pandas as pd
                # Need a DataFrame — check if tracker has cached data
                # or get the composite signal which calls all sub-methods
                signal = whale_tracker._last_whale_signal
                if not signal:
                    # Try to get from internal caches
                    pass

                if signal:
                    if 'BUY' in str(signal.direction):
                        direction = "BUY"
                        accum_score += 20
                    elif 'SELL' in str(signal.direction):
                        direction = "SELL"
                        distrib_score += 20

                    if hasattr(signal, 'momentum') and signal.momentum:
                        mom = signal.momentum
                        if hasattr(mom, 'exhaustion_flag') and mom.exhaustion_flag:
                            distrib_score += 15
                            evidence.append("OHLCV: whale exhaustion detected")
                        if hasattr(mom, 'flow_strength') and mom.flow_strength > 60:
                            aggression_score += 20
                            evidence.append(f"OHLCV: strong momentum ({mom.flow_strength:.0f})")

                    if hasattr(signal, 'distribution_signal') and signal.distribution_signal:
                        ds = signal.distribution_signal
                        if hasattr(ds, 'active') and ds.active:
                            distrib_score += 25
                            evidence.append(f"OHLCV: distribution active (strength={ds.strength:.0f})")

                    if hasattr(signal, 'confidence') and signal.confidence > 60:
                        # High confidence whale signal = aggression
                        aggression_score += 15
                        evidence.append(f"OHLCV: whale confidence {signal.confidence:.0f}%")

                # Also check volume profile position
                try:
                    vp = whale_tracker._last_volume_profile
                    if vp and hasattr(vp, 'poc'):
                        evidence.append(f"OHLCV: POC={vp.poc:.2f}")
                except:
                    pass

                # Check accumulation zones
                try:
                    accum_zones = getattr(whale_tracker, '_last_accumulation_zones', [])
                    if accum_zones and len(accum_zones) >= 3:
                        accum_score += 15
                        evidence.append(f"OHLCV: {len(accum_zones)} accumulation zones")
                except:
                    pass

            except Exception as e:
                evidence.append(f"OHLCV fallback error: {e}")

            evidence.append("(OHLCV fallback — no UW data)")

        # === CLASSIFY STATE ===
        # Highest score wins, with thresholds
        max_score = max(accum_score, distrib_score, aggression_score)

        if max_score < 15:
            state = WhaleState.NEUTRAL
            confidence = 30.0
        elif aggression_score >= accum_score and aggression_score >= distrib_score:
            state = WhaleState.AGGRESSION
            confidence = min(100, aggression_score)
        elif distrib_score >= accum_score:
            state = WhaleState.DISTRIBUTION
            confidence = min(100, distrib_score)
        else:
            state = WhaleState.ACCUMULATION
            confidence = min(100, accum_score)

        # Boost confidence if multiple signals agree
        agreement_count = sum(1 for s in [accum_score, distrib_score, aggression_score] if s > 20)
        if agreement_count == 1:
            confidence = min(100, confidence * 1.1)  # Single strong signal

        return WhaleIntent(
            state=state,
            direction=direction,
            confidence=confidence,
            evidence=evidence,
            accumulation_score=accum_score,
            distribution_score=distrib_score,
            aggression_score=aggression_score,
            net_premium=net_premium,
            dark_pool_volume=dp_volume,
            sweep_count=sweep_count,
            block_count=block_count,
            put_call_ratio=put_call_ratio,
            dp_sentiment=dp_sentiment,
        )


# ============================================================================
# EXECUTION PLANNER
# ============================================================================

class ExecutionPlanner:
    """
    Maps (ICT setup type × whale intent) → execution plan.

    This is where the edge lives. Whale data doesn't just confirm/deny —
    it changes HOW you trade the setup.
    """

    def get_plan(self, setup_type: ICTSetupType,
                 whale_intent: WhaleIntent,
                 signal_direction: str = None) -> ExecutionPlan:
        """
        Get execution plan for an ICT setup given whale intent.

        Args:
            setup_type: Type of ICT/SMC setup
            whale_intent: Classified whale intent
            signal_direction: CALL/PUT or BUY/SELL from the ICT signal

        Returns:
            ExecutionPlan with grade, entry mode, size, TP, stop
        """
        # Check for directional conflict
        is_long = signal_direction in ('CALL', 'LONG', 'BUY') if signal_direction else True

        # If whale direction conflicts with signal direction AND confidence is high,
        # downgrade regardless of state
        if whale_intent.confidence > 60:
            whale_bullish = whale_intent.direction == "BUY"
            if is_long and not whale_bullish and whale_intent.direction != "NEUTRAL":
                return ExecutionPlan(
                    grade=ExecutionGrade.B,
                    entry_mode="skip" if whale_intent.confidence > 80 else "confirmation_candle",
                    size_multiplier=0.0 if whale_intent.confidence > 80 else 0.5,
                    tp_mode="quick_tp",
                    stop_mode="tight",
                    reason=f"Whale {whale_intent.direction} conflicts with LONG signal "
                           f"(conf={whale_intent.confidence:.0f}%)"
                )
            elif not is_long and whale_bullish and whale_intent.direction != "NEUTRAL":
                return ExecutionPlan(
                    grade=ExecutionGrade.B,
                    entry_mode="skip" if whale_intent.confidence > 80 else "confirmation_candle",
                    size_multiplier=0.0 if whale_intent.confidence > 80 else 0.5,
                    tp_mode="quick_tp",
                    stop_mode="tight",
                    reason=f"Whale {whale_intent.direction} conflicts with SHORT signal "
                           f"(conf={whale_intent.confidence:.0f}%)"
                )

        # Look up behavior map
        behavior_map = STRATEGY_BEHAVIOR_MAP.get(setup_type)
        if not behavior_map:
            # Unknown setup type — default to neutral plan
            return ExecutionPlan(
                grade=ExecutionGrade.A,
                entry_mode="confirmation_candle",
                size_multiplier=1.0,
                tp_mode="standard_scalp",
                stop_mode="standard",
                reason=f"Unknown setup type {setup_type} — standard execution"
            )

        plan = behavior_map.get(whale_intent.state)
        if not plan:
            plan = behavior_map[WhaleState.NEUTRAL]

        # === CONFIDENCE GATE ===
        # DISTRIBUTION with low confidence = unreliable classification
        # Very low confidence: block entirely
        # Medium-low confidence: reduce size significantly
        if whale_intent.state == WhaleState.DISTRIBUTION:
            if whale_intent.confidence < 35 and plan.should_trade:
                return ExecutionPlan(
                    grade=ExecutionGrade.B,
                    entry_mode="skip",
                    size_multiplier=0.0,
                    tp_mode="quick_tp",
                    stop_mode="tight",
                    reason=f"Very low confidence DISTRIBUTION ({whale_intent.confidence:.0f}%) — BLOCK"
                )
            elif whale_intent.confidence < 50 and plan.should_trade:
                return ExecutionPlan(
                    grade=ExecutionGrade.A,
                    entry_mode="confirmation_candle",
                    size_multiplier=min(plan.size_multiplier, 0.5),
                    tp_mode="quick_tp",
                    stop_mode="tight",
                    reason=f"Low-confidence DISTRIBUTION ({whale_intent.confidence:.0f}%) — reduced size, tight exit"
                )

        return plan

    def classify_ict_setup(self, signal_type: str) -> ICTSetupType:
        """
        Map V2 signal types to ICT setup categories.

        Args:
            signal_type: Raw signal type from the signal generator

        Returns:
            ICTSetupType enum
        """
        mapping = {
            # Core institutional signals
            'inst_ob': ICTSetupType.MOMENTUM_OB,
            'inst_sweep_reversal': ICTSetupType.SWEEP_REVERSAL,
            'smart_money_div': ICTSetupType.BREAKER_REVERSAL,
            'wyckoff_institutional': ICTSetupType.CONTINUATION,
            'vp_fair_value': ICTSetupType.FVG_REENTRY,
            'iceberg_fade': ICTSetupType.MOMENTUM_OB,

            # Whale-specific signals
            'whale_accumulation_entry': ICTSetupType.CONTINUATION,
            'whale_trap_reversal': ICTSetupType.SWEEP_REVERSAL,
            'whale_exhaustion_fade': ICTSetupType.BREAKER_REVERSAL,
            'whale_divergence_reversal': ICTSetupType.BREAKER_REVERSAL,

            # Continuation flow (new)
            'continuation_flow': ICTSetupType.CONTINUATION,

            # V1 signal types
            'smc_ob': ICTSetupType.MOMENTUM_OB,
            'smc_fvg': ICTSetupType.FVG_REENTRY,
            'smc_choch': ICTSetupType.BREAKER_REVERSAL,
            'smc_bos': ICTSetupType.CONTINUATION,
            'smc_breaker': ICTSetupType.BREAKER_REVERSAL,
            'smc_sweep': ICTSetupType.SWEEP_REVERSAL,
        }

        return mapping.get(signal_type, ICTSetupType.MOMENTUM_OB)


# ============================================================================
# CONTINUATION FLOW SIGNAL (NEW — Step 5 from user spec)
# ============================================================================

@dataclass
class ContinuationFlowSignal:
    """
    Detects persistent directional flow confirming trend continuation.

    Triggers:
    - Repeated call/put buying (same direction, multiple prints)
    - Persistent bid/ask lifting
    - No absorption (no whale resistance)

    This is the MISSING signal the user identified.
    """
    active: bool
    direction: str              # BUY or SELL
    strength: float             # 0-100
    evidence: List[str]
    consecutive_flow_bars: int  # How many bars of consistent flow
    premium_momentum: float     # $ acceleration of flow


def detect_continuation_flow(whale_tracker=None, flow_alerts=None,
                             net_prem_ticks=None) -> ContinuationFlowSignal:
    """
    Detect continuation flow from UW data.

    Args:
        whale_tracker: WhaleTracker instance
        flow_alerts: List of UW flow alerts
        net_prem_ticks: List of UW net premium ticks

    Returns:
        ContinuationFlowSignal
    """
    evidence = []
    strength = 0.0
    direction = "NEUTRAL"
    consecutive = 0
    premium_momentum = 0.0

    if not flow_alerts and not net_prem_ticks:
        return ContinuationFlowSignal(
            active=False, direction="NEUTRAL", strength=0,
            evidence=["No flow data"], consecutive_flow_bars=0,
            premium_momentum=0
        )

    # 1. Repeated directional buying
    if flow_alerts:
        call_alerts = [a for a in flow_alerts if a.option_type == 'call']
        put_alerts = [a for a in flow_alerts if a.option_type == 'put']

        call_premium = sum(a.total_premium for a in call_alerts)
        put_premium = sum(a.total_premium for a in put_alerts)

        if call_premium > put_premium * 2:
            direction = "BUY"
            strength += 30
            evidence.append(f"Repeated call buying: ${call_premium:,.0f} vs ${put_premium:,.0f} puts")
        elif put_premium > call_premium * 2:
            direction = "SELL"
            strength += 30
            evidence.append(f"Repeated put buying: ${put_premium:,.0f} vs ${call_premium:,.0f} calls")

        # Check for sweep persistence (same direction sweeps)
        call_sweeps = len([a for a in call_alerts if a.has_sweep])
        put_sweeps = len([a for a in put_alerts if a.has_sweep])

        if call_sweeps >= 3 and direction == "BUY":
            strength += 20
            evidence.append(f"{call_sweeps} persistent call sweeps")
        elif put_sweeps >= 3 and direction == "SELL":
            strength += 20
            evidence.append(f"{put_sweeps} persistent put sweeps")

    # 2. Net premium momentum (consecutive same-direction ticks)
    if net_prem_ticks and len(net_prem_ticks) >= 3:
        # Check last N ticks for consistent direction
        recent_ticks = net_prem_ticks[:10]  # Most recent first
        bullish_ticks = sum(1 for t in recent_ticks
                          if (t.net_call_premium - t.net_put_premium) > 0)
        bearish_ticks = len(recent_ticks) - bullish_ticks

        if bullish_ticks >= 7:
            consecutive = bullish_ticks
            if direction != "SELL":
                direction = "BUY"
            strength += 25
            evidence.append(f"{bullish_ticks}/{len(recent_ticks)} bullish premium ticks")
        elif bearish_ticks >= 7:
            consecutive = bearish_ticks
            if direction != "BUY":
                direction = "SELL"
            strength += 25
            evidence.append(f"{bearish_ticks}/{len(recent_ticks)} bearish premium ticks")

        # Premium acceleration
        if len(recent_ticks) >= 2:
            latest_net = recent_ticks[0].net_call_premium - recent_ticks[0].net_put_premium
            prev_net = recent_ticks[1].net_call_premium - recent_ticks[1].net_put_premium
            premium_momentum = latest_net - prev_net

            if abs(premium_momentum) > 1_000_000:
                strength += 15
                evidence.append(f"Premium accelerating: ${premium_momentum:,.0f}")

    # 3. No absorption (whale tracker doesn't show resistance)
    if whale_tracker and hasattr(whale_tracker, '_last_distribution'):
        dist = whale_tracker._last_distribution
        # If no distribution detected, flow is clear
        if not dist or (hasattr(dist, 'active') and not dist.active):
            strength += 10
            evidence.append("No whale absorption/resistance detected")

    active = strength >= 40 and direction != "NEUTRAL"

    return ContinuationFlowSignal(
        active=active,
        direction=direction,
        strength=min(100, strength),
        evidence=evidence,
        consecutive_flow_bars=consecutive,
        premium_momentum=premium_momentum,
    )


# ============================================================================
# DEMO / TEST
# ============================================================================

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)

    print("=" * 80)
    print("WHALE INTENT CLASSIFIER — TEST")
    print("=" * 80)

    # Test with real UW data
    try:
        from unusual_whales import UnusualWhalesClient
        from config_v2 import UW_API_KEY
        from whale_tracker import WhaleTracker
        import pandas as pd
        import numpy as np

        if UW_API_KEY:
            client = UnusualWhalesClient(api_key=UW_API_KEY)
            tracker = WhaleTracker(uw_client=client, ticker="SPY")

            # Generate dummy df for tracker init
            n = 200
            np.random.seed(42)
            df = pd.DataFrame({
                'open': np.random.uniform(540, 545, n),
                'high': np.random.uniform(545, 550, n),
                'low': np.random.uniform(535, 540, n),
                'close': np.random.uniform(540, 545, n),
                'volume': np.random.uniform(1000, 5000, n),
            }, index=pd.date_range('2024-01-01', periods=n, freq='5min'))

            # Prime the tracker
            tracker.get_whale_signal(df)

            # Classify
            classifier = WhaleIntentClassifier(uw_client=client)
            intent = classifier.classify("SPY", whale_tracker=tracker)

            print(f"\nSPY Whale Intent: {intent}")
            print(f"  State: {intent.state.value}")
            print(f"  Direction: {intent.direction}")
            print(f"  Confidence: {intent.confidence:.0f}%")
            print(f"  Scores: A={intent.accumulation_score:.0f} "
                  f"D={intent.distribution_score:.0f} "
                  f"G={intent.aggression_score:.0f}")
            print(f"  Evidence:")
            for ev in intent.evidence:
                print(f"    - {ev}")

            # Test execution planner
            planner = ExecutionPlanner()
            print(f"\n{'─' * 60}")
            print("EXECUTION PLANS PER SETUP TYPE:")
            print(f"{'─' * 60}")

            # Test both directions — use CALL and PUT
            for sig_dir in ["CALL", "PUT"]:
                print(f"\n  === Signal Direction: {sig_dir} ===")
                for setup_type in ICTSetupType:
                    plan = planner.get_plan(setup_type, intent, sig_dir)
                    if plan.should_trade:
                        marker = "✅"
                    else:
                        marker = "❌"
                    print(f"  {marker} {setup_type.value:20s} | {plan.grade.value:3s} | "
                          f"{plan.entry_mode:20s} | {plan.size_multiplier:.2f}x | {plan.reason}")

            # Test continuation flow
            print(f"\n{'─' * 60}")
            print("CONTINUATION FLOW SIGNAL:")
            print(f"{'─' * 60}")
            tracker._refresh_uw_data()
            flow_alerts = tracker._uw_cache.get('flow_alerts') or []
            net_prem = tracker._uw_cache.get('net_prem_ticks') or []
            cont = detect_continuation_flow(tracker, flow_alerts, net_prem)
            print(f"  Active: {cont.active}")
            print(f"  Direction: {cont.direction}")
            print(f"  Strength: {cont.strength:.0f}")
            for ev in cont.evidence:
                print(f"    - {ev}")

        else:
            print("No UW_API_KEY — skipping live test")

    except ImportError as e:
        print(f"Import error: {e}")

    print("\n" + "=" * 80)
