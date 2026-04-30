'use client';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import {
  fetchPicks77, fetchPatrol, fetchConviction, fetchStaging, fetchEodBaselines,
  fetchTodayBreakouts, fetchContinuation, fetchIntradaySurge,
  type Picks77Resp, type PatrolResp, type ConvictionResp, type EodBaselinesResp, type EodBaseline,
  type IntradaySurgeResp,
} from '@/lib/api';
import { fmtAge } from '@/lib/format';
import RefreshStatus from '@/components/RefreshStatus';

type BucketName = 'mega' | 'mid' | 'small' | 'indices';

const BUCKET_LABELS: Record<BucketName, { emoji: string; label: string; sub: string }> = {
  mega:    { emoji: '🐳', label: 'Mega',    sub: '≥$200B market cap' },
  mid:     { emoji: '🐬', label: 'Mid',     sub: '$20B–$200B' },
  small:   { emoji: '🐟', label: 'Small',   sub: 'under $20B' },
  indices: { emoji: '📊', label: 'Indices', sub: 'broad market + sector ETFs' },
};

type Filter = 'ACTIVE' | 'ACC' | 'DIST' | 'ALL';

interface RowState {
  ticker: string;
  mcap_b: number | null;
  sector?: string;
  // patrol
  patrol_verdict?: string;
  patrol_score?: number;
  positioning?: string;
  positioning_score?: number;
  acc_n: number;
  dist_n: number;
  has_flip: boolean;
  // conviction (live)
  cv_verdict?: string;
  cv_urgency?: string;
  live_price?: number;
  day_pct?: number;
  is_stealth?: boolean;
  is_stealth_dist?: boolean;
  // staging (v8)
  staging_score?: number;
  staging_put_score?: number;
  is_buy?: boolean;
  is_put_buy?: boolean;
  // late-hour flow surge ("someone always knows" signal)
  // Source: patrol factors.last_hr_call_$ — net call premium in final hour.
  // Regime-aware backtest (2026-04-22→29, n=409 ticker-days):
  //   EARN_WK  PUT  -$0.5 to -$2M  → 75-100% next-3d down (n=3-4)
  //   EARN_MTH CALL +$0.5 to +$3M  → 50-66% up (+33-40pp edge)
  //   EARN_MTH PUT  -$0.5 to -$2M  → 100% down (n=2-7)
  //   NO_EARN  CALL +$1 to +$4M    → 50-66% up (+20-36pp edge)
  //   NO_EARN  PUT  any threshold  → INVERTED, predicts bounce
  last_hr_call_m?: number;
  earnings_regime?: 'EARN_WK' | 'EARN_MTH' | 'NO_EARN';  // null if no earnings data
  days_to_earnings?: number;
  // Per-ticker baseline (for z-score normalization)
  baseline?: EodBaseline;
  z_score?: number;  // (last_hr_call_m - baseline.median_m) / baseline.std_floor_m
  // Cross-engine signals (replaces the secondary nav)
  triggered_today?: boolean;       // ticker fired in v4_today_breakout_scanner today
  triggered_strong?: boolean;      // STRONG-tier breakout
  continuation_chain_days?: number; // multi-day chain length (0 if not in chain)
  continuation_strong?: boolean;
  // Intraday max surges (anywhere in today's session, not just power hour)
  // Backtested 2026-04-22→29: PUT at OPEN $1-7M = 79-86% 3d-down hit rate
  intraday_max_call_m?: number;
  intraday_max_call_t?: string;    // 'YYYY-MM-DDTHH:MM:SSZ'
  intraday_max_put_m?: number;
  intraday_max_put_t?: string;
}

// Map a ticker's next earnings date to one of three regimes.
// Returns null if the ticker has no earnings date (e.g. ETFs).
function regimeFromEarningsDate(earningsDate?: string): { regime: RowState['earnings_regime']; days?: number } {
  if (!earningsDate) return { regime: undefined, days: undefined };
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const e = new Date(earningsDate);
  e.setHours(0, 0, 0, 0);
  const days = Math.abs(Math.round((e.getTime() - today.getTime()) / (1000 * 60 * 60 * 24)));
  if (days <= 7)  return { regime: 'EARN_WK',  days };
  if (days <= 30) return { regime: 'EARN_MTH', days };
  return { regime: 'NO_EARN', days };
}

// PREDICTION layer — translates observed late-hour flow + regime into
// a forward-looking 3d move estimate, backed by backtest cells.
// "If we see this much flow, here's what historically happened next."
//
// Each cell carries: bias direction, expected move range, win rate, sample n.
// Calibration source: v4_eod_surge_regime_sweep.json (Apr 22-29, n=409).
type Prediction = {
  bias: 'CALL' | 'PUT';
  pct_low: number;   // typical winner magnitude (low end)
  pct_high: number;  // typical winner magnitude (high end)
  win_rate: number;  // % historical hit rate at this cell
  n: number;         // sample size
  regime: string;
  basis: string;     // human-readable basis for the prediction
};

// HYBRID predictor — uses z-score where the per-ticker baseline is reliable
// (STRONG/USABLE quality), falls back to absolute-$ thresholds for THIN data,
// and skips entirely when no baseline exists.
//
// Z-score cells (calibrated 2026-04-22→29, n=104):
//   z ≥ +2.0  → 67% win rate, +44pp edge (n=6)  STRONG CALL signal
//   z ≥ +1.5  → 41% win rate, +18pp edge (n=17) MOD CALL signal
//   z ≤ -0.5  → 80% win rate, +13pp edge (n=10) PUT signal
//   z ≤ -1.0  → 70% win rate, +3pp edge (n=20)  weaker PUT
//
// Earnings regime layered on top (refines magnitude estimate):
//   EARN_WK PUT:  bigger magnitude expected (-15% to -28%)
//   EARN_MTH:     moderate magnitude (-7% to -12% / +1% to +4%)
//   NO_EARN:      smaller magnitude (+2% to +6%)
function predictionFor(r: RowState): Prediction | null {
  if (r.last_hr_call_m == null || r.has_flip) return null;
  const lhc = r.last_hr_call_m;
  const reg = r.earnings_regime;
  const z = r.z_score;
  const bq = r.baseline?.quality;

  // Helper: build a prediction object
  const mk = (bias: 'CALL' | 'PUT', lo: number, hi: number, win: number, n: number,
              regimeLbl: string, basis: string): Prediction =>
    ({ bias, pct_low: lo, pct_high: hi, win_rate: win, n, regime: regimeLbl, basis });

  // ─── PATH A: z-score-based (STRONG / USABLE baseline) ───
  if (z != null && (bq === 'STRONG' || bq === 'USABLE')) {
    const baselineLbl = `z=${z.toFixed(2)}σ vs ticker baseline ($${r.baseline?.median_m.toFixed(1)}M ± $${r.baseline?.std_floor_m.toFixed(1)}M${bq === 'USABLE' ? ', n='+r.baseline?.n_days+' days' : ''})`;
    // CALL z ≥ +2.0 (rare, strong) — but EARN_WK CALL has NO edge (regime sweep)
    if (z >= 2.0 && reg !== 'EARN_WK') {
      const [lo, hi] = reg === 'NO_EARN' ? [2, 8] : reg === 'EARN_MTH' ? [1, 5] : [1, 6];
      return mk('CALL', lo, hi, 67, 6, `Z≥+2 / ${reg || 'unknown'}`,
        `Unusual late-hour call surge — ${baselineLbl}. Backtested 67% 3d-up, +44pp edge.`);
    }
    if (z >= 1.5 && reg !== 'EARN_WK') {
      const [lo, hi] = reg === 'NO_EARN' ? [1, 5] : [0, 3];
      return mk('CALL', lo, hi, 41, 17, `Z≥+1.5 / ${reg || 'unknown'}`,
        `Moderate call surge — ${baselineLbl}. Backtested 41% 3d-up, +18pp edge.`);
    }
    // PUT z ≤ -0.5 — strongest PUT cell
    if (z <= -0.5 && z > -1.5) {
      const [lo, hi] = reg === 'EARN_WK' ? [-25, -10] :
                       reg === 'EARN_MTH' ? [-12, -5] : [-5, -1];
      return mk('PUT', lo, hi, 80, 10, `-1.5<Z≤-0.5 / ${reg || 'unknown'}`,
        `Unusual late-hour put-flow — ${baselineLbl}. Backtested 80% 3d-down, +13pp edge.`);
    }
    if (z <= -1.5) {
      if (reg === 'EARN_WK' || reg === 'EARN_MTH') {
        const [lo, hi] = reg === 'EARN_WK' ? [-30, -15] : [-12, -5];
        return mk('PUT', lo, hi, 70, 9, `Z≤-1.5 + earnings`,
          `Heavy late-hour put-flow with earnings catalyst — ${baselineLbl}. Magnitude expected larger but historical hit-rate degrades past -1.5σ.`);
      }
      return null;
    }
    return null;
  }

  // ─── PATH B: absolute-$ fallback (THIN baseline or unknown) ───
  // Use the original earnings-regime-aware cells for tickers without
  // enough history for a stable z-score.
  if (!reg) return null;  // need at least an earnings regime to fall back

  if (reg === 'EARN_WK' && lhc <= -0.5 && lhc >= -2.0) {
    return mk('PUT', -28, -12, 88, 4, 'EARN_WK (abs-$ fallback)',
      `Late-hour put-flow $${lhc.toFixed(1)}M with earnings ${r.days_to_earnings}d away. Smart money de-risking before binary event. (No stable baseline — using absolute-$.)`);
  }
  if (reg === 'EARN_MTH' && lhc <= -0.5 && lhc >= -2.0) {
    return mk('PUT', -12, -7, 100, 7, 'EARN_MTH (abs-$ fallback)',
      `Late-hour put-flow $${lhc.toFixed(1)}M with earnings ${r.days_to_earnings}d away. (No stable baseline — using absolute-$.)`);
  }
  if (reg === 'EARN_MTH' && lhc >= 0.5 && lhc <= 3.0) {
    return mk('CALL', 1, 4, 60, 5, 'EARN_MTH (abs-$ fallback)',
      `Late-hour call $${lhc.toFixed(1)}M with earnings ${r.days_to_earnings}d away. (No stable baseline — using absolute-$.)`);
  }
  if (reg === 'NO_EARN' && lhc >= 1.0 && lhc <= 4.0) {
    return mk('CALL', 2, 6, 60, 6, 'NO_EARN (abs-$ fallback)',
      `Late-hour organic call surge $${lhc.toFixed(1)}M. (No stable baseline — using absolute-$.)`);
  }
  return null;
}

// Single primary action per ticker — what to do, at a glance.
// Implements the trading playbook (patrol + conviction + positioning + flip).
// Returns the chip text, color, and a tooltip explaining why.
type ActionLevel = 'BUY' | 'WAIT' | 'FORMING' | 'HOLD' | 'SKIP' | 'PUT' | 'PUT_WAIT';
function actionFor(r: RowState): { action: ActionLevel; bg: string; fg: string; why: string } {
  const v = r.patrol_verdict;
  const cv = r.cv_verdict;
  const u = r.cv_urgency;
  const inst = (r.positioning_score || 0) >= 60;
  const dayPct = r.day_pct || 0;

  // 0a. Z-SCORE PROMOTION (strongest measured edge: +44pp at z≥+2)
  // BUT: respect earnings regime — EARN_WK CALL has no edge.
  if (r.z_score != null && r.baseline?.quality && !r.has_flip) {
    const z = r.z_score;
    const bq = r.baseline.quality;
    const reg = r.earnings_regime;
    if (bq === 'STRONG' || bq === 'USABLE') {
      // CALL promotions blocked in EARN_WK (regime sweep showed no edge)
      if (z >= 2.0 && reg !== 'EARN_WK') {
        return { action: 'BUY',
          bg: 'rgba(63,140,71,0.32)', fg: 'var(--bull)',
          why: `Unusual call surge — z=+${z.toFixed(2)}σ vs baseline. Backtest: 67% 3d-up at z≥+2 cell.` };
      }
      if (z >= 1.5 && reg !== 'EARN_WK') {
        return { action: 'BUY',
          bg: 'rgba(63,140,71,0.28)', fg: 'var(--bull)',
          why: `Moderate call surge — z=+${z.toFixed(2)}σ vs baseline. Backtest: 41% 3d-up, +18pp edge.` };
      }
      // PUT promotion: meaningful only with earnings catalyst (EARN_WK/EARN_MTH)
      if (z <= -0.5 && z > -1.5 && (reg === 'EARN_WK' || reg === 'EARN_MTH')) {
        return { action: 'PUT',
          bg: 'rgba(176,53,40,0.30)', fg: '#fff',
          why: `Unusual put-flow — z=${z.toFixed(2)}σ with earnings ${r.days_to_earnings}d away. Backtest: 80% 3d-down at this cell.` };
      }
      if (z <= -1.5 && (reg === 'EARN_WK' || reg === 'EARN_MTH')) {
        return { action: 'PUT',
          bg: 'rgba(176,53,40,0.30)', fg: '#fff',
          why: `Heavy put-flow — z=${z.toFixed(2)}σ with earnings catalyst. Larger expected drop, slightly degraded hit rate.` };
      }
    }
  }

  // 0b. EOD SURGE absolute-$ fallback — only fires when z-score path didn't.
  // Same regime constraints. EARN_WK CALL stays blocked.
  if (r.last_hr_call_m != null && !r.has_flip) {
    const lhc = r.last_hr_call_m;
    const reg = r.earnings_regime;
    if (lhc <= -0.5 && lhc >= -2.0 && (reg === 'EARN_WK' || reg === 'EARN_MTH')) {
      return { action: 'PUT',
        bg: 'rgba(176,53,40,0.30)', fg: '#fff',
        why: `Late-hour put-flow $${lhc.toFixed(1)}M with earnings ${r.days_to_earnings ?? '?'}d away — ${reg === 'EARN_WK' ? '75-100%' : '100%'} backtested hit rate.` };
    }
    if (lhc >= 0.5 && lhc <= 3.0 && reg === 'EARN_MTH') {
      return { action: 'BUY',
        bg: 'rgba(63,140,71,0.32)', fg: 'var(--bull)',
        why: `Late-hour call $${lhc.toFixed(1)}M with earnings ${r.days_to_earnings ?? '?'}d away — 50-66% backtested up rate.` };
    }
    if (lhc >= 1.0 && lhc <= 4.0 && reg === 'NO_EARN') {
      return { action: 'BUY',
        bg: 'rgba(63,140,71,0.32)', fg: 'var(--bull)',
        why: `Late-hour organic call surge $${lhc.toFixed(1)}M (no near-term earnings) — 50-66% backtested up rate.` };
    }
    // EARN_WK CALL explicitly NOT promoted (no edge).
    // NO_EARN PUT explicitly NOT promoted (inverted).
  }

  // 1. FLIP overrides everything — institutions undecided
  if (r.has_flip) return {
    action: 'SKIP',
    bg: 'rgba(120,120,120,0.18)', fg: 'var(--dim)',
    why: 'Patrol flipped direction today — mixed signal, no edge',
  };

  // 2. Strong distribution → PUT
  if (v === 'STRONG_DIST') return {
    action: 'PUT',
    bg: 'rgba(176,53,40,0.30)', fg: '#fff',
    why: 'Multi-day institutional distribution — best PUT setups',
  };
  if (v === 'DIST' && inst) return {
    action: 'PUT',
    bg: 'rgba(176,53,40,0.25)', fg: 'var(--bear)',
    why: 'Distribution + institutional positioning — short setup',
  };
  if (v === 'DIST' && r.is_put_buy) return {
    action: 'PUT',
    bg: 'rgba(176,53,40,0.20)', fg: 'var(--bear)',
    why: 'Patrol DIST + V8 staging confirms PUT setup',
  };
  if (v === 'DIST') return {
    action: 'SKIP',
    bg: 'rgba(120,120,120,0.18)', fg: 'var(--dim)',
    why: 'Distribution but no institutional confirmation — just weakness',
  };

  // 3. Strong accumulation paths
  if (v === 'STRONG_ACC') {
    if (u === 'EXHAUSTED' || u === 'TOO_LATE' || dayPct >= 8) return {
      action: 'WAIT',
      bg: 'rgba(217,119,87,0.25)', fg: 'var(--accent)',
      why: 'Setup real but entry chasing — wait for pullback to add',
    };
    return {
      action: 'BUY',
      bg: 'rgba(63,140,71,0.32)', fg: 'var(--bull)',
      why: 'STRONG institutional acc + actionable entry — top setup',
    };
  }

  // 4. Single-day ACC paths
  if (v === 'ACC') {
    if (cv === 'BUY' && (u === 'PULLBACK' || u === 'EXTENSION')) return {
      action: 'BUY',
      bg: 'rgba(63,140,71,0.32)', fg: 'var(--bull)',
      why: 'ACC + buy-the-dip conviction — best entry profile',
    };
    if (cv === 'BUY' && (u === 'HIT' || u === 'ENTRY' || u === 'MARKET') && dayPct < 5) return {
      action: 'BUY',
      bg: 'rgba(63,140,71,0.28)', fg: 'var(--bull)',
      why: 'ACC + conviction BUY in entry zone, not extended',
    };
    if (cv === 'BUY' && (u === 'EXHAUSTED' || u === 'TOO_LATE')) return {
      action: 'WAIT',
      bg: 'rgba(217,119,87,0.20)', fg: 'var(--accent)',
      why: 'ACC firing but conviction says exhausted — wait for pullback',
    };
    if (dayPct >= 7) return {
      action: 'WAIT',
      bg: 'rgba(217,119,87,0.18)', fg: 'var(--accent)',
      why: 'ACC firing but already +7% today — chase risk, wait for pullback',
    };
    if (inst || cv === 'WATCH') return {
      action: 'BUY',
      bg: 'rgba(63,140,71,0.22)', fg: 'var(--bull)',
      why: 'ACC + (institutional positioning OR WATCH conviction) — actionable',
    };
    return {
      action: 'HOLD',
      bg: 'rgba(176,176,176,0.10)', fg: 'var(--dim)',
      why: 'ACC firing but no positioning/conviction confirmation yet — backtest showed FORMING tier had -6.3pp negative edge.',
    };
  }

  // 5. Neutral patrol — collapse to HOLD.
  // Backtest verdict: the old FORMING tier (n=39) had -6.3 pp 1d edge —
  // CONSISTENTLY NEGATIVE across all buckets. Promoting these to a
  // distinct "FORMING" action was misleading. The underlying signals
  // (stealth, v8 staging, conviction WATCH) still surface as chips on
  // the row, but the action label reserves itself for composite signals
  // that actually predict edge.
  //
  // PUT_WAIT preserved — small sample but directionally consistent so far.
  if (r.is_stealth_dist || r.is_put_buy) return {
    action: 'PUT_WAIT',
    bg: 'rgba(176,53,40,0.10)', fg: 'var(--bear)',
    why: 'Distribution signal but patrol neutral — wait for DIST fire',
  };

  return {
    action: 'HOLD',
    bg: 'rgba(176,176,176,0.10)', fg: 'var(--dim)',
    why: 'Neutral — no actionable signal right now',
  };
}

// Composite priority for sorting — higher = more actionable LONG, lower = SHORT
function priorityScore(r: RowState): number {
  let s = 0;
  // Patrol verdict (biggest weight)
  if (r.patrol_verdict === 'STRONG_ACC') s += 100;
  else if (r.patrol_verdict === 'ACC') s += 60;
  else if (r.patrol_verdict === 'DIST') s -= 60;
  else if (r.patrol_verdict === 'STRONG_DIST') s -= 100;
  // Conviction overlay
  if (r.cv_verdict === 'BUY') s += 30;
  else if (r.cv_verdict === 'WATCH') s += 15;
  else if (r.cv_urgency === 'EXHAUSTED' || r.cv_urgency === 'TOO_LATE') s -= 10;
  // Institutional positioning
  if ((r.positioning_score || 0) >= 60) s += 25;
  else if ((r.positioning_score || 0) >= 40) s += 10;
  // V8 staging fires
  if (r.is_buy) s += 20;
  if (r.is_put_buy) s -= 20;
  // Stealth tag (multi-day acc)
  if (r.is_stealth) s += 15;
  if (r.is_stealth_dist) s -= 15;
  // Flip penalty (mixed signal)
  if (r.has_flip) s -= 10;
  // Net fire count tilt
  s += (r.acc_n - r.dist_n) * 2;
  // Late-hour flow surge — backtested +30.7pp edge for ≥$5M call surges
  if (r.last_hr_call_m != null) {
    if (r.last_hr_call_m >= 5)  s += 35;   // SURGE_5M+ tier (45.5% win rate)
    else if (r.last_hr_call_m >= 1)  s += 12;
    else if (r.last_hr_call_m <= -5) s -= 35;
    else if (r.last_hr_call_m <= -1) s -= 12;
  }
  // Cross-engine signals — only continuation matters for entry timing.
  // Breakout-today is "missed the trigger" so it gets only a small bump.
  if (r.triggered_today) s += 5;  // small acknowledgment, not actionable
  if (r.continuation_chain_days && r.continuation_chain_days >= 1) {
    // Longer chain holding = stronger signal, but cap to avoid runaway sort
    s += Math.min(r.continuation_chain_days * 8, 40);
  }
  return s;
}

export default function BucketView({ bucket }: { bucket: BucketName }) {
  const meta = BUCKET_LABELS[bucket];
  const [filter, setFilter] = useState<Filter>('ACTIVE');

  const { data: picks } = useQuery<Picks77Resp>({
    queryKey: ['picks_77'],
    queryFn: fetchPicks77,
    refetchInterval: 5 * 60 * 1000, // bucket list rarely changes
  });
  const { data: patrol } = useQuery<PatrolResp>({
    queryKey: ['flow_patrol'],
    queryFn: fetchPatrol,
    refetchInterval: 15_000,
  });
  const { data: cv } = useQuery<ConvictionResp>({
    queryKey: ['conviction'],
    queryFn: fetchConviction,
    refetchInterval: 30_000,
  });
  const { data: staging } = useQuery<any>({
    queryKey: ['staging'],
    queryFn: fetchStaging,
    refetchInterval: 60_000,
  });
  const { data: baselines } = useQuery<EodBaselinesResp>({
    queryKey: ['eod_baselines'],
    queryFn: fetchEodBaselines,
    refetchInterval: 30 * 60 * 1000,  // 30 min — baselines update daily
  });
  const { data: breakouts } = useQuery<any>({
    queryKey: ['today_breakouts'],
    queryFn: fetchTodayBreakouts,
    refetchInterval: 60_000,
  });
  const { data: continuation } = useQuery<any>({
    queryKey: ['continuation'],
    queryFn: fetchContinuation,
    refetchInterval: 60_000,
  });
  const { data: intraday } = useQuery<IntradaySurgeResp>({
    queryKey: ['intraday_surge'],
    queryFn: fetchIntradaySurge,
    refetchInterval: 5 * 60 * 1000,  // recomputed every 5 min by daily script
  });

  const cvByT = useMemo(() => {
    const m: Record<string, any> = {};
    for (const r of cv?.results || []) m[r.ticker] = r;
    return m;
  }, [cv]);
  const stByT = useMemo(() => {
    const m: Record<string, any> = {};
    for (const r of (staging?.all_scored || [])) m[r.ticker] = r;
    return m;
  }, [staging]);
  const breakoutByT = useMemo(() => {
    const m: Record<string, any> = {};
    for (const r of (breakouts?.buys || [])) m[r.ticker] = r;
    return m;
  }, [breakouts]);
  const contByT = useMemo(() => {
    const m: Record<string, any> = {};
    for (const r of (continuation?.buys || [])) m[r.ticker] = r;
    return m;
  }, [continuation]);

  const rows = useMemo<RowState[]>(() => {
    const tickers = (picks?.buckets?.[bucket] as any[]) || [];
    return tickers.map((t: any) => {
      const p = patrol?.tickers?.[t.ticker] || ({} as any);
      const alerts = (p as any).alerts || [];
      let acc_n = 0, dist_n = 0, has_flip = false;
      let prev: 'ACC' | 'DIST' | null = null;
      for (const a of alerts) {
        const tt = a?.type || '';
        const side: 'ACC' | 'DIST' | null = tt.includes('ACC') ? 'ACC' : tt.includes('DIST') ? 'DIST' : null;
        if (!side) continue;
        if (side === 'ACC') acc_n++; else dist_n++;
        if (prev && prev !== side) has_flip = true;
        prev = side;
      }
      const cvr = cvByT[t.ticker] || {};
      const sr = stByT[t.ticker] || {};
      const factors = (p as any).factors || {};
      const last_hr_raw = factors['last_hr_call_$'];
      const last_hr_call_m = (typeof last_hr_raw === 'number') ? last_hr_raw / 1e6 : undefined;
      const { regime, days } = regimeFromEarningsDate((t as any).next_earnings_date);
      // Per-ticker baseline + z-score (if last_hr_call_m available)
      const baseline = baselines?.baselines?.[t.ticker];
      let z_score: number | undefined = undefined;
      if (last_hr_call_m != null && baseline && baseline.std_floor_m > 0) {
        z_score = (last_hr_call_m - baseline.median_m) / baseline.std_floor_m;
      }
      // Cross-engine signals
      const br = breakoutByT[t.ticker];
      const co = contByT[t.ticker];
      const triggered_today = !!br;
      const triggered_strong = !!(br?.strong);
      const continuation_chain_days = co ? (co.days_in_chain || 0) : 0;
      const continuation_strong = !!(co?.strong);
      // Intraday max surges
      const ints = intraday?.tickers?.[t.ticker];
      return {
        ticker: t.ticker,
        mcap_b: t.mcap_b ?? null,
        sector: t.sector,
        patrol_verdict: p.verdict,
        patrol_score: p.score,
        positioning: (p as any).positioning,
        positioning_score: (p as any).positioning_score,
        acc_n, dist_n, has_flip,
        cv_verdict: cvr.verdict,
        cv_urgency: cvr.trade_idea?.entry_urgency || cvr.urgency,
        live_price: cvr.last_price,
        day_pct: cvr.day_pct,
        is_stealth: cvr.is_stealth,
        is_stealth_dist: cvr.is_stealth_dist,
        staging_score: sr.score,
        staging_put_score: sr.put_score,
        is_buy: sr.is_buy,
        is_put_buy: sr.is_put_buy,
        last_hr_call_m,
        earnings_regime: regime,
        days_to_earnings: days,
        baseline,
        z_score,
        triggered_today,
        triggered_strong,
        continuation_chain_days,
        continuation_strong,
        intraday_max_call_m: ints?.max_call_m,
        intraday_max_call_t: ints?.max_call_window_t,
        intraday_max_put_m: ints?.max_put_m,
        intraday_max_put_t: ints?.max_put_window_t,
      };
    });
  }, [picks, patrol, cvByT, stByT, baselines, breakoutByT, contByT, intraday, bucket]);

  const counts = useMemo(() => {
    let acc = 0, dist = 0, neutral = 0, active = 0;
    let n_inst = 0, n_strong_acc = 0, n_strong_dist = 0;
    let total_acc_fires = 0, total_dist_fires = 0;
    let total_dp_today_m = 0;
    let pm_blocks = 0;
    let total_pm_dp_m = 0;
    const top_call: { t: string; s: number }[] = [];
    const top_put:  { t: string; s: number }[] = [];
    const top_eod_call: { t: string; m: number }[] = [];
    const top_eod_put:  { t: string; m: number }[] = [];
    // Patrol state has factors per ticker — pull from cache via patrol query
    const ptickers = patrol?.tickers || {};
    for (const r of rows) {
      const v = r.patrol_verdict || 'NEUTRAL';
      const score = r.patrol_score ?? 50;
      if (v === 'ACC' || v === 'STRONG_ACC') {
        acc++;
        top_call.push({ t: r.ticker, s: score });
      } else if (v === 'DIST' || v === 'STRONG_DIST') {
        dist++;
        top_put.push({ t: r.ticker, s: score });
      } else neutral++;
      if (v === 'STRONG_ACC') n_strong_acc++;
      if (v === 'STRONG_DIST') n_strong_dist++;
      if ((r.positioning_score || 0) >= 60) n_inst++;
      total_acc_fires += r.acc_n;
      total_dist_fires += r.dist_n;
      if (v !== 'NEUTRAL' || ['BUY','WATCH'].includes(r.cv_verdict || '')) active++;

      // DP + PM block totals (from patrol factors)
      const f: any = (ptickers[r.ticker] as any)?.factors || {};
      total_dp_today_m += (f['dark_pool_$'] || 0) / 1e6;
      total_pm_dp_m   += (f['pm_dp_total_$'] || 0) / 1e6;
      pm_blocks       += (f['pm_dp_blocks'] || 0);

      // Top EOD CALL / PUT surges (sorted by absolute $)
      if (r.last_hr_call_m != null && r.last_hr_call_m >= 0.5) {
        top_eod_call.push({ t: r.ticker, m: r.last_hr_call_m });
      }
      if (r.last_hr_call_m != null && r.last_hr_call_m <= -0.5) {
        top_eod_put.push({ t: r.ticker, m: r.last_hr_call_m });
      }
    }
    top_call.sort((a, b) => b.s - a.s);
    top_put.sort((a, b) => a.s - b.s);
    top_eod_call.sort((a, b) => b.m - a.m);
    top_eod_put.sort((a, b) => a.m - b.m);
    return {
      all: rows.length, acc, dist, neutral, active,
      n_inst, n_strong_acc, n_strong_dist, total_acc_fires, total_dist_fires,
      total_dp_today_m, total_pm_dp_m, pm_blocks,
      top_call: top_call.slice(0, 5),
      top_put: top_put.slice(0, 5),
      top_eod_call: top_eod_call.slice(0, 5),
      top_eod_put: top_eod_put.slice(0, 5),
    };
  }, [rows, patrol?.tickers]);

  // Action-tier rank: BUY = top, PUT = next (also actionable), then WAIT etc.
  // HOLD/SKIP at bottom. Within same tier, sort by absolute composite quality.
  // FORMING removed — backtest confirmed -6.3pp negative edge.
  const ACTION_RANK: Record<string, number> = {
    BUY: 100, PUT: 90, WAIT: 70, PUT_WAIT: 65, HOLD: 10, SKIP: 5,
  };

  const filtered = useMemo(() => {
    // Compute action label per row once for filtering + sorting
    const withAction = rows.map(r => ({ r, action: actionFor(r).action }));
    let out = withAction;

    if (filter === 'ACTIVE') {
      // Use ACTION LABEL (not raw signals) — show only actionable rows
      out = withAction.filter(({ action }) =>
        action === 'BUY' || action === 'PUT' || action === 'WAIT' ||
        action === 'PUT_WAIT'
      );
    } else if (filter === 'ACC') {
      out = withAction.filter(({ r }) => r.patrol_verdict === 'ACC' || r.patrol_verdict === 'STRONG_ACC');
    } else if (filter === 'DIST') {
      out = withAction.filter(({ r }) => r.patrol_verdict === 'DIST' || r.patrol_verdict === 'STRONG_DIST');
    }

    // Sort: action tier first (BUY at top, PUT next, WAIT, FORMING, then HOLD/SKIP)
    // Within tier: by absolute composite priority (strongest signal first)
    return [...out].sort((a, b) => {
      const ar = ACTION_RANK[a.action] ?? 0;
      const br = ACTION_RANK[b.action] ?? 0;
      if (ar !== br) return br - ar;
      return Math.abs(priorityScore(b.r)) - Math.abs(priorityScore(a.r));
    }).map(x => x.r);
  }, [rows, filter]);

  // ACC/DIST renamed to CALL/PUT in the UI per user — clearer trade direction
  const verdictTag = (v?: string) => {
    if (v === 'STRONG_ACC') return { bg: 'var(--bull-soft)', fg: 'var(--bull)', text: 'STRONG CALL' };
    if (v === 'ACC') return { bg: 'var(--bull-soft)', fg: 'var(--bull)', text: 'CALL' };
    if (v === 'STRONG_DIST') return { bg: 'var(--bear-soft)', fg: 'var(--bear)', text: 'STRONG PUT' };
    if (v === 'DIST') return { bg: 'var(--bear-soft)', fg: 'var(--bear)', text: 'PUT' };
    return { bg: 'var(--hair)', fg: 'var(--mid)', text: 'NEUTRAL' };
  };

  return (
    <main className="bucket-page">
      <header>
        <h1>{meta.emoji} {meta.label} bucket</h1>
        <div className="sub-head">{meta.sub} · {counts.all} tickers · patrol scoped to this bucket</div>
        <div className="meta">
          <RefreshStatus label={meta.label} />
          {patrol?.ts_utc && <span className="meta-pill">patrol: {fmtAge(patrol.ts_utc)?.ago}</span>}
          {cv?.generated_at_utc && <span className="meta-pill">conviction: {fmtAge(cv.generated_at_utc)?.ago}</span>}
        </div>
      </header>

      {/* Bucket summary — Anthropic-tightened, three rows */}
      <div className="bucket-summary">
        <div className="bucket-summary__row cols-5">
          <div className="bucket-summary__metric">
            <strong style={{ color: 'var(--bull)' }}>{counts.acc}</strong>
            CALL firing
            {counts.n_strong_acc > 0 && <div style={{ fontSize: 10, color: 'var(--bull)', fontWeight: 600 }}>incl. {counts.n_strong_acc} STRONG</div>}
          </div>
          <div className="bucket-summary__metric">
            <strong style={{ color: 'var(--bear)' }}>{counts.dist}</strong>
            PUT firing
            {counts.n_strong_dist > 0 && <div style={{ fontSize: 10, color: 'var(--bear)', fontWeight: 600 }}>incl. {counts.n_strong_dist} STRONG</div>}
          </div>
          <div className="bucket-summary__metric">
            <strong style={{ color: 'var(--accent)' }}>{counts.n_inst}</strong>
            institutional
            <div style={{ fontSize: 10 }}>positioning ≥60</div>
          </div>
          <div className="bucket-summary__metric">
            <strong style={{ color: 'var(--mid)' }}>{counts.neutral}</strong>
            neutral
          </div>
          <div className="bucket-summary__metric">
            <strong style={{ color: 'var(--ink)' }}>{counts.total_acc_fires}/{counts.total_dist_fires}</strong>
            alerts today
            <div style={{ fontSize: 10 }}>call / put</div>
          </div>
        </div>
        <div className="bucket-summary__row cols-3">
          <div className="bucket-summary__metric">
            <strong style={{ color: 'var(--ink)' }}>${counts.total_dp_today_m.toFixed(0)}M</strong>
            dark pool today
          </div>
          <div className="bucket-summary__metric">
            <strong style={{ color: 'var(--ink)' }}>${counts.total_pm_dp_m.toFixed(0)}M</strong>
            premarket DP · {counts.pm_blocks} blocks
          </div>
          <div className="bucket-summary__metric">
            <strong style={{ color: 'var(--ink)' }}>{counts.top_eod_call.length}↑ / {counts.top_eod_put.length}↓</strong>
            EOD surges (last 30 min)
          </div>
        </div>
        {(counts.top_call.length > 0 || counts.top_put.length > 0 || counts.top_eod_call.length > 0 || counts.top_eod_put.length > 0) && (
          <div className="bucket-summary__row" style={{ display: 'block' }}>
            <div className="bucket-summary__movers">
              {counts.top_call.length > 0 && (
                <div><strong style={{ color: 'var(--bull)' }}>Top CALL:</strong> {counts.top_call.map(x => `${x.t}(${x.s})`).join(' · ')}</div>
              )}
              {counts.top_put.length > 0 && (
                <div><strong style={{ color: 'var(--bear)' }}>Top PUT:</strong> {counts.top_put.map(x => `${x.t}(${x.s})`).join(' · ')}</div>
              )}
              {counts.top_eod_call.length > 0 && (
                <div><strong style={{ color: 'var(--bull)' }}>EOD CALL:</strong> {counts.top_eod_call.map(x => `${x.t} +$${x.m.toFixed(1)}M`).join(' · ')}</div>
              )}
              {counts.top_eod_put.length > 0 && (
                <div><strong style={{ color: 'var(--bear)' }}>EOD PUT:</strong> {counts.top_eod_put.map(x => `${x.t} -$${Math.abs(x.m).toFixed(1)}M`).join(' · ')}</div>
              )}
            </div>
          </div>
        )}
      </div>

      <div className="filter-row">
        <span className="lbl">show</span>
        <span className={`chip ${filter === 'ACTIVE' ? 'active' : ''}`} onClick={() => setFilter('ACTIVE')}>
          ⚡ Active <span className="count">{counts.active}</span>
        </span>
        <span className={`chip ${filter === 'ACC' ? 'active' : ''}`} onClick={() => setFilter('ACC')}>
          🟢 CALL <span className="count">{counts.acc}</span>
        </span>
        <span className={`chip ${filter === 'DIST' ? 'active' : ''}`} onClick={() => setFilter('DIST')}>
          🔴 PUT <span className="count">{counts.dist}</span>
        </span>
        <span className={`chip ${filter === 'ALL' ? 'active' : ''}`} onClick={() => setFilter('ALL')}>
          All <span className="count">{counts.all}</span>
        </span>
      </div>

      {!picks && <div className="empty">Loading {meta.label}…</div>}
      {picks && filtered.length === 0 && (
        <div className="empty">
          {filter === 'ACTIVE' ? `No setups firing in ${meta.label} right now.` :
           filter === 'ACC' ? `No CALL verdicts in ${meta.label}.` :
           filter === 'DIST' ? `No PUT verdicts in ${meta.label}.` :
           `${meta.label} bucket is empty.`}
        </div>
      )}

      <div>
        {filtered.map((r) => {
          const tag = verdictTag(r.patrol_verdict);
          const positioning = r.positioning || 'THIN';
          const isInst = (r.positioning_score || 0) >= 60;
          const dayPct = r.day_pct;
          const dpClass = dayPct == null ? '' : (dayPct > 0 ? 'bull' : dayPct < 0 ? 'bear' : '');

          // Build label chips for this row
          type Lbl = { text: string; bg: string; fg: string; border?: string; title?: string };
          const labels: Lbl[] = [];

          // Patrol verdict label — text only, score moves to tooltip
          // (e.g. "CALL 66" was confusing; the score is auxiliary detail)
          const scoreContext = r.patrol_score == null ? '' :
            r.patrol_score >= 76 ? ` (${r.patrol_score}/100 — heavy buying)` :
            r.patrol_score >= 60 ? ` (${r.patrol_score}/100 — moderate buying)` :
            r.patrol_score >= 40 ? ` (${r.patrol_score}/100 — balanced)` :
            r.patrol_score >= 26 ? ` (${r.patrol_score}/100 — moderate selling)` :
                                   ` (${r.patrol_score}/100 — heavy selling)`;
          labels.push({
            text: tag.text,
            bg: tag.bg, fg: tag.fg,
            title: `Patrol verdict today${scoreContext}. Scale 0-100, neutral=50, ≥60 institutional buying, ≤39 distribution.`,
          });
          // Conviction action
          if (r.cv_verdict === 'BUY') {
            const tooLate = r.cv_urgency === 'EXHAUSTED' || r.cv_urgency === 'TOO_LATE';
            labels.push({
              text: tooLate ? `BUY/${r.cv_urgency}` : `🚀 BUY${r.cv_urgency ? `/${r.cv_urgency}` : ''}`,
              bg: tooLate ? 'rgba(176,53,40,0.10)' : 'rgba(63,140,71,0.20)',
              fg: tooLate ? 'var(--bear)' : 'var(--bull)',
              title: 'Conviction engine signal',
            });
          } else if (r.cv_verdict === 'WATCH') {
            labels.push({
              text: `👁 WATCH${r.cv_urgency ? `/${r.cv_urgency}` : ''}`,
              bg: 'rgba(217,119,87,0.12)', fg: 'var(--accent)',
              title: 'Conviction engine watching',
            });
          }
          // Institutional positioning
          if (isInst) {
            labels.push({
              text: `⭐ ${positioning} ${r.positioning_score}`,
              bg: 'rgba(217,119,87,0.18)', fg: 'var(--accent)',
              title: 'Premarket DP + flow + aggressor skew indicate institutional positioning',
            });
          } else if ((r.positioning_score || 0) >= 40) {
            labels.push({
              text: `${positioning} ${r.positioning_score}`,
              bg: 'rgba(176,176,176,0.08)', fg: 'var(--dim)',
            });
          }
          // V8 staging: PRE-BREAK (call) or DISTRO (put)
          // If the breakout already FIRED today, suppress PRE-BREAK
          // (the setup is now stale — 🚀 BREAK chip carries the info).
          if (r.is_buy && !r.triggered_today) {
            labels.push({ text: `📈 PRE-BREAK ${r.staging_score}`, bg: 'rgba(63,140,71,0.10)', fg: 'var(--bull)',
              title: 'V8 staging score crossed BUY threshold' });
          }
          if (r.is_put_buy) {
            labels.push({ text: `📉 DISTRO ${r.staging_put_score}`, bg: 'rgba(176,53,40,0.10)', fg: 'var(--bear)',
              title: 'V8 staging score crossed PUT threshold' });
          }
          // Stealth (multi-day acc)
          if (r.is_stealth) {
            labels.push({ text: '🌱 STEALTH', bg: 'rgba(120,140,93,0.15)', fg: 'var(--green, #788c5d)',
              title: 'Multi-day institutional accumulation (≥5 days)' });
          }
          if (r.is_stealth_dist) {
            labels.push({ text: '☠ STEALTH-DIST', bg: 'rgba(176,53,40,0.10)', fg: 'var(--bear)',
              title: 'Multi-day institutional distribution' });
          }
          // Breakout-fired-today chip removed per user feedback:
          // "if it broke today you missed the entry — only continuation
          //  matters because that's where you can still get in."
          // The triggered_today fact still feeds the priority sort
          // (slightly), but no chip — keeps the row uncluttered.
          //
          // Multi-day continuation chain — THIS is the actionable signal.
          // Means: ticker broke out N days ago AND is still holding above
          // the breakout origin. Entry on a pullback is still viable.
          if (r.continuation_chain_days && r.continuation_chain_days >= 1) {
            labels.push({
              text: `🏃 CONTINUE × ${r.continuation_chain_days}d`,
              bg: 'rgba(120,140,93,0.18)', fg: 'var(--green, #788c5d)',
              title: `In a ${r.continuation_chain_days}-day breakout continuation chain. Origin breakout still holding — entry on a pullback is still viable.`,
            });
          }
          // Flip warning
          if (r.has_flip) {
            labels.push({ text: '⚠ FLIP', bg: 'rgba(176,53,40,0.10)', fg: 'var(--bear)',
              title: 'Patrol flipped direction today (ACC↔DIST) — mixed signal' });
          }
          // INTRADAY MAX SURGE chips — anywhere in today's session.
          // Backtested 2026-04-22→29 (n=195 with 3d forward):
          //   PUT at OPEN $1-7M → 79-86% 3d-down hit rate ★★★ best signal
          //   PUT $1-3M anywhere → 60-80% 3d-down
          //   CALL $15M+ at any time → 67%+ 3d-up
          //   CALL $1-3M at OPEN → 0% (noise)
          //   PUT/CALL ≥$15M → degrades (mean reversion)
          if (r.intraday_max_call_m != null && r.intraday_max_call_m >= 7) {
            const t = (r.intraday_max_call_t || '').slice(11, 16);
            labels.push({
              text: `🌅 INTRADAY CALL +$${r.intraday_max_call_m.toFixed(0)}M @ ${t}`,
              bg: 'var(--bull-soft)', fg: 'var(--bull)',
              title: `Max 30-min CALL surge today: +$${r.intraday_max_call_m.toFixed(1)}M at ${t} UTC. Backtested 67%+ 3d-up rate at $15M+ tier.`,
            });
          }
          if (r.intraday_max_put_m != null && r.intraday_max_put_m <= -1 && r.intraday_max_put_m > -7) {
            const t = (r.intraday_max_put_t || '').slice(11, 16);
            const hh = parseInt(t.slice(0,2) || '0');
            const window_lbl = hh < 14 ? 'OPEN' : hh < 17 ? 'morning' : hh < 19 ? 'midday' : 'power-hour';
            const winRate = (hh < 14 && r.intraday_max_put_m <= -3) ? '86%' :
                            (hh < 14) ? '79%' : '60-73%';
            labels.push({
              text: `🌅 ${window_lbl} PUT $${r.intraday_max_put_m.toFixed(1)}M @ ${t}`,
              bg: 'var(--bear-soft)', fg: 'var(--bear)',
              title: `Max 30-min PUT surge today: $${r.intraday_max_put_m.toFixed(1)}M at ${t} UTC (${window_lbl}). Backtested ${winRate} 3d-down hit rate at this cell.`,
            });
          }
          // Late-hour flow surge — REGIME-AWARE.
          // Show chip only in regimes where the signal has a measured edge.
          // NO_EARN PUT explicitly hidden (backtest showed inverted signal).
          if (r.last_hr_call_m != null) {
            const lhc = r.last_hr_call_m;
            const reg = r.earnings_regime;
            const earnDays = r.days_to_earnings;
            const earnSuffix = earnDays != null ? ` · earn ${earnDays}d` : '';

            // CALL chip: EARN_MTH gets the lower-threshold tier; NO_EARN needs +$1M
            if (reg === 'EARN_MTH' && lhc >= 0.5 && lhc <= 3.0) {
              labels.push({
                text: `🎯 EOD CALL +$${lhc.toFixed(1)}M${earnSuffix}`,
                bg: 'var(--bull-soft)', fg: 'var(--bull)',
                title: `Late-hour call +$${lhc.toFixed(1)}M with earnings ${earnDays}d away (EARN_MTH sweet zone). Backtested 50-66% 3d-up, +33-40pp edge.`,
              });
            } else if (reg === 'NO_EARN' && lhc >= 1.0 && lhc <= 4.0) {
              labels.push({
                text: `🎯 EOD CALL +$${lhc.toFixed(1)}M · organic`,
                bg: 'var(--bull-soft)', fg: 'var(--bull)',
                title: `Late-hour organic call +$${lhc.toFixed(1)}M (no near-term earnings). Backtested 50-66% 3d-up, +20-36pp edge.`,
              });
            } else if (reg === 'NO_EARN' && lhc > 4.0) {
              labels.push({
                text: `📞 EOD CALL +$${lhc.toFixed(0)}M · large`,
                bg: 'var(--hair)', fg: 'var(--dim)',
                title: `Late-hour call +$${lhc.toFixed(1)}M — above the +$4M sweet zone, signal weakens (small sample at higher amounts).`,
              });
            }
            // PUT chip: only meaningful in EARN_WK / EARN_MTH regimes
            else if ((reg === 'EARN_WK' || reg === 'EARN_MTH') && lhc <= -0.5 && lhc >= -2.0) {
              const winRate = reg === 'EARN_WK' ? '75-100%' : '100%';
              labels.push({
                text: `🎯 EOD PUT -$${Math.abs(lhc).toFixed(1)}M${earnSuffix}`,
                bg: 'var(--bear-soft)', fg: 'var(--bear)',
                title: `Late-hour put-flow -$${Math.abs(lhc).toFixed(1)}M with earnings ${earnDays}d away (${reg}). Backtested ${winRate} 3d-down hit rate. Smart money de-risking before binary.`,
              });
            } else if (reg === 'NO_EARN' && lhc <= -0.5) {
              labels.push({
                text: `⚠ EOD PUT -$${Math.abs(lhc).toFixed(1)}M · contrarian`,
                bg: 'var(--hair)', fg: 'var(--dim)',
                title: `Late-hour put-flow -$${Math.abs(lhc).toFixed(1)}M but NO earnings ahead — backtest showed signal INVERTS in this regime (predicts bounce, not drop). Don't trade this side.`,
              });
            }
          }

          // Primary action — what to do, at a glance
          const act = actionFor(r);
          const actionEmoji: Record<typeof act.action, string> = {
            BUY: '🚀', WAIT: '🟡', FORMING: '📊', HOLD: '⏸',
            SKIP: '⛔', PUT: '🔴', PUT_WAIT: '🔻',
          };
          const actionDisplay = act.action === 'PUT_WAIT' ? 'PUT WAIT' : act.action;

          // PREDICTION (forward-looking 3d move estimate based on backtest cell)
          const pred = predictionFor(r);

          // Map label bg/fg to brand classes (instead of inline rgba)
          const bkChipClass = (l: { fg: string }) => {
            if (l.fg.includes('bull') || l.fg === 'var(--bull)') return 'bk-chip bk-chip--bull';
            if (l.fg.includes('bear') || l.fg === 'var(--bear)') return 'bk-chip bk-chip--bear';
            if (l.fg.includes('accent') || l.fg === 'var(--accent)') return 'bk-chip bk-chip--accent';
            if (l.fg.includes('green') || l.fg === '#788c5d') return 'bk-chip bk-chip--green';
            if (l.fg === '#6a9bcc' || l.fg === 'var(--blue)') return 'bk-chip bk-chip--blue';
            return 'bk-chip bk-chip--neutral';
          };

          const sectorMcap = [
            r.sector,
            r.mcap_b != null && r.mcap_b > 0
              ? (r.mcap_b >= 1000 ? `$${(r.mcap_b/1000).toFixed(1)}T` : `$${r.mcap_b.toFixed(0)}B`)
              : null
          ].filter(Boolean).join(' · ');

          return (
            <article key={r.ticker} className="bucket-card" data-action={act.action}>
              <div className="bucket-card__hero">
                <div className="bucket-card__id">
                  <div className="symbol">{r.ticker}</div>
                  {sectorMcap && <div className="meta">{sectorMcap}</div>}
                  {r.patrol_score != null && (
                    <div className="gauge"
                         title={`Patrol score ${r.patrol_score}/100. ≥60 = institutional buying, ≤39 = distribution. 50 = neutral.`}>
                      <div className="gauge__needle" style={{ left: `${Math.max(0, Math.min(100, r.patrol_score))}%` }} />
                    </div>
                  )}
                </div>

                <div title={act.why} className={`action-pill action-pill--${act.action}`}>
                  {actionEmoji[act.action]} {actionDisplay}
                </div>

                <div>
                  {pred && (
                    <span
                      className={`prediction-strip prediction-strip--${pred.bias}`}
                      title={`${pred.basis}\n\nBacktest: ${pred.regime} · n=${pred.n} · ${pred.win_rate}% historical hit rate.`}
                    >
                      {pred.bias === 'CALL' ? '📈' : '📉'} 3d: {pred.pct_low > 0 ? '+' : ''}{pred.pct_low}% → {pred.pct_high > 0 ? '+' : ''}{pred.pct_high}%
                      <span className="prediction-strip__conf">{pred.win_rate}% · n={pred.n}</span>
                    </span>
                  )}
                </div>

                <div className="bucket-card__price">
                  {dayPct != null && (
                    <div className={`change ${dpClass}`}>{dayPct >= 0 ? '+' : ''}{dayPct.toFixed(2)}%</div>
                  )}
                  {r.live_price != null && <div className="price">${r.live_price.toFixed(2)}</div>}
                  {/* TRADE NOW — only on actionable cards (BUY or PUT) */}
                  {(act.action === 'BUY' || act.action === 'PUT') && (
                    <a
                      className={`trade-btn trade-btn--${act.action}`}
                      href={`https://www.tradingview.com/chart/?symbol=NASDAQ%3A${r.ticker}`}
                      target="_blank"
                      rel="noopener noreferrer"
                      title={`Open ${r.ticker} on TradingView to ${act.action === 'BUY' ? 'BUY' : 'short'}`}
                    >
                      {act.action === 'BUY' ? '↗ TRADE NOW' : '↘ SHORT NOW'}
                    </a>
                  )}
                </div>
              </div>

              <div className="bucket-card__chips">
                {labels.map((l, i) => {
                  // Skip the verdict chip itself if action label already conveys it loud enough?
                  // Keep all for now — they're informational.
                  let cls = bkChipClass(l);
                  // Add strong-pulse on STRONG verdict
                  if (l.text.startsWith('STRONG ')) cls += ' bk-chip--strong';
                  return (
                    <span key={i} className={cls} title={l.title || ''}>{l.text}</span>
                  );
                })}
                <span className="bk-chip__meta"
                      title={`Patrol fired ${r.acc_n} CALL alert${r.acc_n === 1 ? '' : 's'} and ${r.dist_n} PUT alert${r.dist_n === 1 ? '' : 's'} today.`}>
                  {(r.acc_n > 0 || r.dist_n > 0) ? (
                    <>
                      today:&nbsp;
                      {r.acc_n > 0 && (<span style={{ color: 'var(--bull)', fontWeight: 600 }}>{r.acc_n}× call</span>)}
                      {r.acc_n > 0 && r.dist_n > 0 && ', '}
                      {r.dist_n > 0 && (<span style={{ color: 'var(--bear)', fontWeight: 600 }}>{r.dist_n}× put</span>)}
                    </>
                  ) : 'no alerts today'}
                </span>
              </div>
            </article>
          );
        })}
      </div>
    </main>
  );
}
