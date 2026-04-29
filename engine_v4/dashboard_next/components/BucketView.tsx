'use client';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import {
  fetchPicks77, fetchPatrol, fetchConviction, fetchStaging,
  type Picks77Resp, type PatrolResp, type ConvictionResp,
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

  // 0. EOD SURGE — REGIME-AWARE (the sweet zone differs by earnings context).
  // Backtested ranges (2026-04-22→29, n=409):
  //   EARN_WK  PUT  −$0.5 to −$2M    75-100% 3d-down win
  //   EARN_MTH PUT  −$0.5 to −$2M    100% (n=3-7), +34pp edge
  //   EARN_MTH CALL +$0.5 to +$3M    50-66%, +33-40pp edge
  //   NO_EARN  CALL +$1   to +$4M    50-66%, +20-36pp edge
  //   NO_EARN  PUT  ANY               INVERTED — do NOT promote to PUT
  if (r.last_hr_call_m != null && !r.has_flip) {
    const lhc = r.last_hr_call_m;
    const reg = r.earnings_regime;
    // PUT promotion: only when earnings ahead within 30d
    if (lhc <= -0.5 && lhc >= -2.0 && (reg === 'EARN_WK' || reg === 'EARN_MTH')) {
      return {
        action: 'PUT',
        bg: 'rgba(176,53,40,0.30)', fg: '#fff',
        why: `Late-hour put-flow $${lhc.toFixed(1)}M with earnings ${r.days_to_earnings ?? '?'}d away — ${reg === 'EARN_WK' ? '75-100%' : '100%'} backtested 3d-down hit rate.`,
      };
    }
    // CALL promotion: EARN_MTH gets lower threshold; NO_EARN needs +$1M+
    if (lhc >= 0.5 && lhc <= 3.0 && reg === 'EARN_MTH') {
      return {
        action: 'BUY',
        bg: 'rgba(63,140,71,0.32)', fg: 'var(--bull)',
        why: `Late-hour call $${lhc.toFixed(1)}M with earnings ${r.days_to_earnings ?? '?'}d away — backtested 50-66% 3d-up hit rate, +33-40pp edge.`,
      };
    }
    if (lhc >= 1.0 && lhc <= 4.0 && reg === 'NO_EARN') {
      return {
        action: 'BUY',
        bg: 'rgba(63,140,71,0.32)', fg: 'var(--bull)',
        why: `Late-hour organic call surge $${lhc.toFixed(1)}M (no near-term earnings) — backtested 50-66% 3d-up, +20-36pp edge.`,
      };
    }
    // NO_EARN PUT explicitly NOT promoted — backtest showed inverted edge.
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
      action: 'FORMING',
      bg: 'rgba(106,155,204,0.18)', fg: '#6a9bcc',
      why: 'ACC firing but no positioning/conviction confirmation yet',
    };
  }

  // 5. Neutral patrol — but other signals say something
  if (r.is_stealth) return {
    action: 'FORMING',
    bg: 'rgba(106,155,204,0.18)', fg: '#6a9bcc',
    why: 'Multi-day stealth accumulation — wait for patrol ACC to confirm',
  };
  if (r.is_buy) return {
    action: 'FORMING',
    bg: 'rgba(106,155,204,0.14)', fg: '#6a9bcc',
    why: 'V8 staging score crossed — wait for patrol ACC to confirm',
  };
  if (cv === 'BUY' && (u === 'PULLBACK' || u === 'HIT')) return {
    action: 'FORMING',
    bg: 'rgba(106,155,204,0.14)', fg: '#6a9bcc',
    why: 'Conviction BUY but patrol neutral — borderline, wait for ACC fire',
  };
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
      };
    });
  }, [picks, patrol, cvByT, stByT, bucket]);

  const counts = useMemo(() => {
    let acc = 0, dist = 0, neutral = 0, active = 0;
    let n_inst = 0, n_strong_acc = 0, n_strong_dist = 0;
    let total_acc_fires = 0, total_dist_fires = 0;
    for (const r of rows) {
      const v = r.patrol_verdict || 'NEUTRAL';
      if (v === 'ACC' || v === 'STRONG_ACC') acc++;
      else if (v === 'DIST' || v === 'STRONG_DIST') dist++;
      else neutral++;
      if (v === 'STRONG_ACC') n_strong_acc++;
      if (v === 'STRONG_DIST') n_strong_dist++;
      if ((r.positioning_score || 0) >= 60) n_inst++;
      total_acc_fires += r.acc_n;
      total_dist_fires += r.dist_n;
      if (v !== 'NEUTRAL' || ['BUY','WATCH'].includes(r.cv_verdict || '')) active++;
    }
    return { all: rows.length, acc, dist, neutral, active,
             n_inst, n_strong_acc, n_strong_dist, total_acc_fires, total_dist_fires };
  }, [rows]);

  const filtered = useMemo(() => {
    let out = rows;
    if (filter === 'ACTIVE') {
      out = rows.filter(r => {
        const v = r.patrol_verdict;
        return v === 'ACC' || v === 'STRONG_ACC' || v === 'DIST' || v === 'STRONG_DIST'
            || ['BUY','WATCH'].includes(r.cv_verdict || '')
            || r.is_buy || r.is_put_buy;
      });
    } else if (filter === 'ACC') {
      out = rows.filter(r => r.patrol_verdict === 'ACC' || r.patrol_verdict === 'STRONG_ACC');
    } else if (filter === 'DIST') {
      out = rows.filter(r => r.patrol_verdict === 'DIST' || r.patrol_verdict === 'STRONG_DIST');
    }
    // Sort by composite priority — bullish at top, bearish at bottom
    return [...out].sort((a, b) => priorityScore(b) - priorityScore(a));
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
    <main>
      <header>
        <h1>{meta.emoji} {meta.label} bucket</h1>
        <div className="sub-head">{meta.sub} · {counts.all} tickers · patrol scoped to this bucket</div>
        <div className="meta">
          <RefreshStatus label={meta.label} />
          {patrol?.ts_utc && <span className="meta-pill">patrol: {fmtAge(patrol.ts_utc)?.ago}</span>}
          {cv?.generated_at_utc && <span className="meta-pill">conviction: {fmtAge(cv.generated_at_utc)?.ago}</span>}
        </div>
      </header>

      {/* Bucket summary */}
      <div style={{
        margin: '12px auto 8px', padding: '12px 14px',
        maxWidth: 720, borderRadius: 10,
        background: 'var(--panel)', border: '1px solid var(--hair)',
        fontFamily: 'Poppins, Arial, sans-serif', fontSize: 12,
      }}>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 6, color: 'var(--dim)', fontSize: 11 }}>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--bull)' }}>{counts.acc}</div>
            <div>CALL firing</div>
            {counts.n_strong_acc > 0 && <div style={{ fontSize: 10, color: 'var(--bull)', fontWeight: 600 }}>incl. {counts.n_strong_acc} STRONG</div>}
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--bear)' }}>{counts.dist}</div>
            <div>PUT firing</div>
            {counts.n_strong_dist > 0 && <div style={{ fontSize: 10, color: 'var(--bear)', fontWeight: 600 }}>incl. {counts.n_strong_dist} STRONG</div>}
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--mid)' }}>{counts.neutral}</div>
            <div>neutral</div>
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--accent)' }}>{counts.n_inst}</div>
            <div>institutional</div>
            <div style={{ fontSize: 10 }}>positioning ≥60</div>
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--ink)' }}>{counts.total_acc_fires}/{counts.total_dist_fires}</div>
            <div>fires today</div>
            <div style={{ fontSize: 10 }}>call / put</div>
          </div>
        </div>
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

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, maxWidth: 960, margin: '0 auto' }}>
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
          if (r.is_buy) {
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
          // Flip warning
          if (r.has_flip) {
            labels.push({ text: '⚠ FLIP', bg: 'rgba(176,53,40,0.10)', fg: 'var(--bear)',
              title: 'Patrol flipped direction today (ACC↔DIST) — mixed signal' });
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

          return (
            <article key={r.ticker} className="card" style={{ padding: '10px 14px', display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
              {/* PRIMARY ACTION — the one thing you look at to decide */}
              <div style={{ flex: '0 0 auto' }}>
                <span title={act.why} style={{
                  background: act.bg, color: act.fg,
                  padding: '6px 12px', borderRadius: 6,
                  fontSize: 13, fontWeight: 700,
                  fontFamily: 'Poppins, Arial, sans-serif',
                  whiteSpace: 'nowrap', minWidth: 92, display: 'inline-block',
                  textAlign: 'center', letterSpacing: '0.02em',
                }}>{actionEmoji[act.action]} {actionDisplay}</span>
              </div>
              <div style={{ flex: '0 0 auto', minWidth: 70 }}>
                <span className="ticker" style={{ fontSize: 18, fontWeight: 700 }}>{r.ticker}</span>
              </div>
              <div style={{ flex: '1 1 auto', display: 'flex', flexWrap: 'wrap', gap: 4, alignItems: 'center' }}>
                {labels.map((l, i) => (
                  <span key={i} title={l.title || ''} style={{
                    background: l.bg, color: l.fg, padding: '3px 8px',
                    borderRadius: 4, fontSize: 11, fontWeight: 600,
                    fontFamily: 'Poppins, Arial, sans-serif', whiteSpace: 'nowrap',
                  }}>{l.text}</span>
                ))}
                <span title={`Patrol fired ${r.acc_n} CALL alert${r.acc_n === 1 ? '' : 's'} and ${r.dist_n} PUT alert${r.dist_n === 1 ? '' : 's'} today. More alerts = more institutional confirmations during the session.`}
                      style={{ fontSize: 11, color: 'var(--dim)', marginLeft: 6 }}>
                  {(r.acc_n > 0 || r.dist_n > 0) ? (
                    <>
                      today:&nbsp;
                      {r.acc_n > 0 && (
                        <span style={{ color: 'var(--bull)', fontWeight: 600 }}>
                          {r.acc_n}× call alert{r.acc_n === 1 ? '' : 's'}
                        </span>
                      )}
                      {r.acc_n > 0 && r.dist_n > 0 && <span style={{ color: 'var(--mid)' }}>, </span>}
                      {r.dist_n > 0 && (
                        <span style={{ color: 'var(--bear)', fontWeight: 600 }}>
                          {r.dist_n}× put alert{r.dist_n === 1 ? '' : 's'}
                        </span>
                      )}
                    </>
                  ) : (
                    <span style={{ color: 'var(--mid)' }}>today: no alerts</span>
                  )}
                </span>
              </div>
              <div style={{ flex: '0 0 auto', textAlign: 'right' }}>
                {dayPct != null && (
                  <span className={dpClass} style={{ marginRight: 10, fontFamily: 'Poppins, Arial, sans-serif', fontVariantNumeric: 'tabular-nums', fontWeight: 600 }}>
                    {dayPct >= 0 ? '+' : ''}{dayPct.toFixed(2)}%
                  </span>
                )}
                {r.live_price != null && (
                  <span style={{ marginRight: 10, fontFamily: 'Poppins, Arial, sans-serif', fontVariantNumeric: 'tabular-nums' }}>
                    ${r.live_price.toFixed(2)}
                  </span>
                )}
                {r.mcap_b != null && r.mcap_b > 0 && (
                  <span style={{ fontSize: 11, color: 'var(--dim)' }}>
                    ${r.mcap_b >= 1000 ? `${(r.mcap_b / 1000).toFixed(1)}T` : `${r.mcap_b.toFixed(0)}B`}
                  </span>
                )}
              </div>
            </article>
          );
        })}
      </div>
    </main>
  );
}
