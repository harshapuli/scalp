"""
Canonical dataclasses per spec §10.2.

Every cross-module boundary uses these typed dataclasses. No dicts, no kwargs,
no positional surprises. Reference: docs/spec/trading_system_lifecycle.docx §10.2.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Literal, Optional


# ──────────────────────────────────────────────────────────────────────────────
# Features — per-bar feature snapshot (SCALP-1)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Features:
    """Per-bar Features computed deterministically from bars+trades+UW data.

    Spec §10.2: Computed at bar close only (no intra-bar peeking). Hashable
    via .hash(); hash stored in any decision logs. Live and replay paths
    produce identical Features for same bar (SCALP-1.T1, SCALP-1.T2).
    """
    # Identity
    ticker: str
    timestamp: datetime               # bar close time, ET
    bar_idx: int                      # for deterministic referencing
    # Price structure (v2.2 §B4 + §18)
    open: float
    high: float
    low: float
    close: float
    extension_from_vwap_atr: float    # (close - vwap) / atr, signed
    extension_from_prior_close_atr: float
    failed_extension_atr: float       # how far retraced from extension peak
    pullback_break: bool              # last pullback's low broken?
    wick_pct: float                   # max(upper_wick, lower_wick) / range
    upper_wick_pct: float
    lower_wick_pct: float
    # Volume
    volume: float
    volume_divergence_ratio: float    # current / prior_push
    current_push_vol: float
    prior_push_vol: float
    climax_vol_ratio: float
    # Aggressor (Lee-Ready)
    aggressor_recent: float           # signed [-1, +1] over last 5 bars
    aggressor_prior: float            # signed over prior 5 bars
    aggressor_velocity: float         # d(aggressor)/dt
    flip_strength: float              # magnitude of recent flip
    # Gamma (UW)
    distance_to_major_pos_gex_atr: float  # signed; negative = below strike
    distance_to_pos_gex_atr: float        # alias for compat
    gex_magnitude_rank: float             # 0..1 percentile
    gamma_flip_distance: float            # ATR units to gamma_flip level
    strike_oi_rank: float                 # OI rank of major strike, 0..1
    major_gex_strike_dte: int             # DTE of major +GEX strike (v2.2 §22b)
    gex_snapshot_age_min: int             # v2.2 §22a freshness
    # Options flow (UW)
    net_signed_premium_5m: float          # dollars
    net_signed_premium_30m: float
    call_ask_pct: float                   # 0..1
    put_ask_pct: float
    sweep_count: int
    flow_flip: bool                       # 5m flow opposite of 30m?
    signed_flow_score: float              # [-1, +1] z-score combo
    # Regime / context
    near_htf_level_atr: float             # daily/weekly S/R proximity
    earnings_blackout: bool
    fomc_blackout: bool
    iv_percentile: float                  # 0..1
    front_iv_change: float                # day's ATM IV change
    skew_slope: float                     # 25Δ put - 25Δ call
    # Execution
    bid_ask_spread_pct: float             # underlying NBBO
    option_volume_5d_avg: float           # ATM weekly avg daily volume
    option_open_interest: float
    option_spread_pct: float              # ATM weekly bid/ask spread

    def hash(self) -> str:
        """SHA256 of canonical-ordered field tuple. Stable across runs (SCALP-1.T1)."""
        # Use sorted dict serialization for determinism (no random key order)
        d = {f.name: getattr(self, f.name) for f in self.__dataclass_fields__.values()}
        # datetime → ISO string for stable hashing
        for k, v in list(d.items()):
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        canonical = json.dumps(d, sort_keys=True, separators=(",", ":"), default=str)
        return hashlib.sha256(canonical.encode()).hexdigest()


# ──────────────────────────────────────────────────────────────────────────────
# Candidate — pre-staged trade waiting on trigger (SWING-1, SWING-10)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class StateTransition:
    """One row in candidate_transitions Postgres table (§10.3)."""
    from_state: str
    to_state: str
    transitioned_at: datetime
    triggered_by: str                       # 'scanner' | 'scalp.price_trigger' | 'manual'
    triggering_data: dict = field(default_factory=dict)
    features_hash_at_transition: str = ""


@dataclass
class Candidate:
    """Per-strategy pre-staged candidate. Lives in redis pre_staged_<strategy>:{ticker}."""
    id: str                           # uuid4
    strategy: Literal['S1', 'S2', 'S3', 'S4', 'S5']
    ticker: str
    direction: Literal['long', 'short']
    state: Literal['INDUCEMENT', 'SETUP', 'TRIGGER', 'ENTRY', 'MANAGE', 'EXIT', 'JOURNAL']
    created_at: datetime
    expires_at: datetime              # TTL: S1 announcement-time; S2/S3/S5 +30m; S4 +45m
    setup_features: Features          # snapshot at SETUP
    setup_features_hash: str
    atr: float                                 # decision-time ATR
    gamma_strike: Optional[float] = None       # S5 only
    or_high: Optional[float] = None            # S2 only
    or_low: Optional[float] = None             # S2 only
    flow_score_at_setup: Optional[float] = None  # S4 only
    spread_width: Optional[float] = None       # S5 vertical width target
    age_in_state: timedelta = field(default_factory=lambda: timedelta(0))
    predecessor_states: list[str] = field(default_factory=list)
    transition_log: list[StateTransition] = field(default_factory=list)


# ──────────────────────────────────────────────────────────────────────────────
# Decision — output of allow_s5_trade (S5-104)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class Decision:
    """Output of every gating function. TRADE or PASS with reason if PASS.

    Spec §10.2 + S5-104. Every PASS reason logged to decision_log Postgres
    table (§10.3). Constraint: pass_requires_reason at DB level.
    """
    decision: Literal['TRADE', 'PASS']
    pass_reason: Optional[str] = None
    strategy: str = 'S5'
    candidate_id: str = ''
    ml_prob: Optional[float] = None
    ml_threshold_used: Optional[float] = None
    expected_value_net: Optional[float] = None
    risk_dollars: Optional[float] = None
    instrument: Optional['VerticalOrder'] = None
    timestamp: datetime = field(default_factory=lambda: datetime.now(tz=timezone.utc))


def TRADE(strategy: str, **kwargs) -> Decision:
    """Helper constructor for TRADE decisions."""
    return Decision(decision='TRADE', strategy=strategy, **kwargs)


def PASS(reason: str, strategy: str = 'S5', **kwargs) -> Decision:
    """Helper constructor for PASS decisions. Reason required."""
    if not reason:
        raise ValueError("PASS decision requires a non-empty reason")
    return Decision(decision='PASS', pass_reason=reason, strategy=strategy, **kwargs)


# ──────────────────────────────────────────────────────────────────────────────
# Order types — VerticalOrder + OptionLeg (S5 execution layer)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass
class OptionLeg:
    """One leg of an option spread."""
    symbol: str                       # OCC format e.g. 'SPY  260507C00470000'
    strike: float
    delta: float
    bid: float
    ask: float
    open_interest: int
    volume: int


@dataclass
class VerticalOrder:
    """Bull-call or bear-put vertical for S5 execution."""
    underlying: str
    long_leg: OptionLeg
    short_leg: OptionLeg
    side: Literal['call', 'put']
    qty: int
    limit_price: float                # debit + 2% buffer
    expiry: date
    spread_width: float
    debit: float                      # mid-debit at order time


# ──────────────────────────────────────────────────────────────────────────────
# UW types — GEXSnapshot + FlowRecord (data_clients/unusual_whales)
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class GEXSnapshot:
    """One snapshot of gamma exposure data from UW /greek-exposure."""
    ticker: str
    timestamp: datetime               # snapshot time from UW
    spot_price: float
    gamma_flip: float                 # zero-gamma level
    major_pos_gex_strike: float
    major_neg_gex_strike: float
    gex_by_strike: dict[float, float] # strike -> GEX (notional)
    age_min: int                      # now - timestamp

    def major_strike_at(self, ts: datetime) -> float:
        """Returns the major positive GEX strike at this snapshot."""
        return self.major_pos_gex_strike


@dataclass(frozen=True)
class FlowRecord:
    """One options flow record from UW /flow-recent."""
    ticker: str
    timestamp: datetime
    side: Literal['call', 'put']
    action: Literal['buy', 'sell']    # bought_at_ask or sold_at_bid
    premium: float                    # total dollars
    is_sweep: bool
    expiry: date
    strike: float
    iv_at_trade: float


# ──────────────────────────────────────────────────────────────────────────────
# Scalp Brain types
# ──────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ScalpState:
    """Output of scalp brain classifier — single bar's state.

    States per spec §B4 (SCALP-2):
      NEUTRAL
      SURGE_IGNITION  · SURGE_CONTINUATION · SURGE_CLIMAX · SURGE_REVERSE
      TANK_IGNITION   · TANK_CONTINUATION  · TANK_CLIMAX  · TANK_REVERSE
    """
    name: str
    score: float                      # primary score for this state, [0, 1]
    timestamp: datetime
    ticker: str
    bar_idx: int
    entry_bar: Optional[int] = None   # bar at which this state began
    age_bars: int = 0                 # bars since state entry
    score_at_entry: Optional[float] = None
    sequence_number: int = 0          # monotonic per ticker (SCALP-30.2)
