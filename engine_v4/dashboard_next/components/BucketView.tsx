'use client';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState, useRef } from 'react';
import {
  fetchPicks77, fetchPatrol, fetchConviction,
  type Picks77Resp, type PatrolResp, type ConvictionResp, type ConvictionRow,
} from '@/lib/api';
import { fmtAge } from '@/lib/format';
import TickerCard from '@/components/TickerCard';
import RefreshStatus from '@/components/RefreshStatus';

/**
 * BucketView — uses the same conviction-page design pattern.
 * Picks_77 ticker list filtered to one bucket (mega/mid/small/indices),
 * rendered through the conviction TickerCard. Filter chips mirror /conviction.
 *
 * Replaces the previous custom-chip mess with the design the user
 * confirmed they liked (per Apr 29 feedback: "i liked the last design
 * we have on conviction page").
 */

type BucketName = 'mega' | 'mid' | 'small' | 'indices';
type StageKey = 'ALL' | 'STEALTH' | 'FORMING' | 'TRADE_NOW' | 'WAIT';
type SideKey = 'ALL' | 'CALL' | 'PUT';

const BUCKET_LABELS: Record<BucketName, { emoji: string; label: string; sub: string }> = {
  mega:    { emoji: '🐳', label: 'Mega',    sub: '≥$200B market cap' },
  mid:     { emoji: '🐬', label: 'Mid',     sub: '$20B–$200B' },
  small:   { emoji: '🐟', label: 'Small',   sub: 'under $20B' },
  indices: { emoji: '📊', label: 'Indices', sub: 'broad market + sector ETFs' },
};

// Mirror conviction page's stage derivation
const stageOf = (r: ConvictionRow): StageKey | 'PASS' => {
  const pt = (r as any).pattern_type;
  if (pt === 'DISTRIBUTION' || pt === 'SQUEEZE') return 'PASS';
  const stealthHidden = !!r.is_stealth && (r.day_pct == null || (r.day_pct as number) < 2.0);
  if (r.verdict === 'PENDING' || r.verdict === 'SKIP') {
    return stealthHidden ? 'STEALTH' : 'PASS';
  }
  const u = (r.trade_idea && r.trade_idea.entry_urgency) || (r as any).urgency;
  const isWaitUrgency = u === 'PULLBACK' || u === 'EXTENSION' || u === 'EXHAUSTED' || u === 'TOO_LATE';
  if (r.verdict === 'BUY') {
    if (u === 'ENTRY' || u === 'HIT') return 'TRADE_NOW';
    if (isWaitUrgency) return 'WAIT';
    return 'FORMING';
  }
  if (r.verdict === 'WATCH') {
    if (isWaitUrgency) return 'WAIT';
    return 'FORMING';
  }
  if (stealthHidden) return 'STEALTH';
  return 'PASS';
};
const sideOf = (r: ConvictionRow): SideKey =>
  (r.direction === 'BEARISH' || (r as any).verdict === 'PUT') ? 'PUT' : 'CALL';

const STAGE_RANK: Record<string, number> = {
  TRADE_NOW: 4, WAIT: 3, FORMING: 2, STEALTH: 1, PASS: 0,
};

export default function BucketView({ bucket }: { bucket: BucketName }) {
  const meta = BUCKET_LABELS[bucket];
  const [stage, setStage] = useState<StageKey>('ALL');
  const [side, setSide] = useState<SideKey>('ALL');

  const { data: picks } = useQuery<Picks77Resp>({
    queryKey: ['picks_77'],
    queryFn: fetchPicks77,
    refetchInterval: 5 * 60 * 1000,
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

  // Build set of tickers in this bucket
  const bucketTickers = useMemo(() => {
    const ticks = (picks?.buckets?.[bucket] as any[]) || [];
    return new Set(ticks.map((t) => t.ticker));
  }, [picks, bucket]);

  // Filter conviction rows to this bucket
  const rows = useMemo(() => {
    if (!cv?.results) return [];
    return cv.results.filter((r) => bucketTickers.has(r.ticker));
  }, [cv, bucketTickers]);

  // Stage / side counts
  const stageCounts = useMemo(() => {
    const c: Record<StageKey, number> = { ALL: 0, STEALTH: 0, FORMING: 0, TRADE_NOW: 0, WAIT: 0 };
    for (const r of rows) {
      const s = stageOf(r);
      if (s !== 'PASS') c.ALL++;
      if (s === 'STEALTH' || s === 'FORMING' || s === 'TRADE_NOW' || s === 'WAIT') c[s]++;
    }
    return c;
  }, [rows]);

  const sideCounts = useMemo(() => {
    const c: Record<SideKey, number> = { ALL: rows.length, CALL: 0, PUT: 0 };
    for (const r of rows) c[sideOf(r)]++;
    return c;
  }, [rows]);

  // Patrol summary (DP$ totals + top CALL/PUT, scoped to bucket)
  const patrolSummary = useMemo(() => {
    const tk = patrol?.tickers || {};
    let acc = 0, dist = 0, neutral = 0, n_inst = 0, n_strong_acc = 0, n_strong_dist = 0;
    let total_dp_today_m = 0, total_pm_dp_m = 0, pm_blocks = 0;
    const top_call: { t: string; s: number }[] = [];
    const top_put: { t: string; s: number }[] = [];
    for (const tkr of bucketTickers) {
      const p: any = tk[tkr];
      if (!p) continue;
      const v = p.verdict || 'NEUTRAL';
      const score = p.score ?? 50;
      if (v === 'ACC' || v === 'STRONG_ACC') { acc++; top_call.push({ t: tkr, s: score }); }
      else if (v === 'DIST' || v === 'STRONG_DIST') { dist++; top_put.push({ t: tkr, s: score }); }
      else neutral++;
      if (v === 'STRONG_ACC') n_strong_acc++;
      if (v === 'STRONG_DIST') n_strong_dist++;
      if ((p.positioning_score || 0) >= 60) n_inst++;
      const f = p.factors || {};
      total_dp_today_m += (f['dark_pool_$'] || 0) / 1e6;
      total_pm_dp_m   += (f['pm_dp_total_$'] || 0) / 1e6;
      pm_blocks       += (f['pm_dp_blocks'] || 0);
    }
    top_call.sort((a, b) => b.s - a.s);
    top_put.sort((a, b) => a.s - b.s);
    return {
      acc, dist, neutral, n_inst, n_strong_acc, n_strong_dist,
      total_dp_today_m, total_pm_dp_m, pm_blocks,
      top_call: top_call.slice(0, 5),
      top_put: top_put.slice(0, 5),
    };
  }, [patrol, bucketTickers]);

  // Filter + sort (stage rank → score → confidence → ticker)
  const filtered = useMemo(() => {
    const matched = rows.filter((r) => {
      const s = stageOf(r);
      if (stage === 'ALL') {
        if (s === 'PASS') return false;
      } else {
        if (s !== stage) return false;
      }
      if (side !== 'ALL' && sideOf(r) !== side) return false;
      return true;
    });
    matched.sort((a, b) => {
      const sA = STAGE_RANK[stageOf(a)] ?? 0;
      const sB = STAGE_RANK[stageOf(b)] ?? 0;
      if (sB !== sA) return sB - sA;
      const scA = (a.score ?? 0) as number;
      const scB = (b.score ?? 0) as number;
      if (scB !== scA) return scB - scA;
      const cA = (a.confidence ?? 0) as number;
      const cB = (b.confidence ?? 0) as number;
      if (cB !== cA) return cB - cA;
      return (a.ticker || '').localeCompare(b.ticker || '');
    });
    return matched;
  }, [rows, stage, side]);

  const cardRefs = useRef<Record<string, HTMLDivElement | null>>({});

  return (
    <main>
      <header>
        <h1>{meta.emoji} {meta.label} bucket</h1>
        <div className="sub-head">{meta.sub} · {bucketTickers.size} tickers · conviction-style view</div>
        <div className="meta">
          <RefreshStatus label={meta.label} />
          {cv?.generated_at_utc && <span className="meta-pill">conviction: {fmtAge(cv.generated_at_utc)?.ago}</span>}
          {patrol?.ts_utc && <span className="meta-pill">patrol: {fmtAge(patrol.ts_utc)?.ago}</span>}
        </div>
      </header>

      {/* Patrol summary scoped to bucket — same as before, simplified */}
      <div style={{
        margin: '12px auto 8px', padding: '12px 14px',
        maxWidth: 720, borderRadius: 10,
        background: 'var(--panel)', border: '1px solid var(--hair)',
        fontFamily: 'Poppins, Arial, sans-serif', fontSize: 12,
      }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: 8 }}>
          <strong style={{ color: 'var(--ink)', fontSize: 13 }}>🛡 Bucket patrol (live, 15 s)</strong>
          <span style={{ color: 'var(--dim)' }}>{bucketTickers.size} tickers · refresh {fmtAge(patrol?.ts_utc)?.ago ?? '—'}</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, 1fr)', gap: 6, color: 'var(--dim)', fontSize: 11 }}>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--bull)', fontVariantNumeric: 'tabular-nums' }}>{patrolSummary.acc}</div>
            <div>CALL firing</div>
            {patrolSummary.n_strong_acc > 0 && <div style={{ fontSize: 10, color: 'var(--bull)', fontWeight: 600 }}>incl. {patrolSummary.n_strong_acc} STRONG</div>}
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--bear)', fontVariantNumeric: 'tabular-nums' }}>{patrolSummary.dist}</div>
            <div>PUT firing</div>
            {patrolSummary.n_strong_dist > 0 && <div style={{ fontSize: 10, color: 'var(--bear)', fontWeight: 600 }}>incl. {patrolSummary.n_strong_dist} STRONG</div>}
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--accent)', fontVariantNumeric: 'tabular-nums' }}>{patrolSummary.n_inst}</div>
            <div>institutional</div>
            <div style={{ fontSize: 10 }}>positioning ≥60</div>
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--ink)', fontVariantNumeric: 'tabular-nums' }}>${patrolSummary.total_dp_today_m.toFixed(0)}M</div>
            <div>DP today</div>
            <div style={{ fontSize: 10 }}>${patrolSummary.total_pm_dp_m.toFixed(0)}M pm · {patrolSummary.pm_blocks} blocks</div>
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--mid)', fontVariantNumeric: 'tabular-nums' }}>{patrolSummary.neutral}</div>
            <div>neutral</div>
          </div>
        </div>
        {(patrolSummary.top_call.length > 0 || patrolSummary.top_put.length > 0) && (
          <div style={{ marginTop: 10, paddingTop: 8, borderTop: '1px dashed var(--hair)', display: 'flex', gap: 16, fontSize: 11, color: 'var(--dim)', flexWrap: 'wrap' }}>
            {patrolSummary.top_call.length > 0 && (
              <div>
                <strong style={{ color: 'var(--bull)' }}>Top CALL:</strong>{' '}
                {patrolSummary.top_call.map((x) => `${x.t}(${x.s})`).join(' · ')}
              </div>
            )}
            {patrolSummary.top_put.length > 0 && (
              <div>
                <strong style={{ color: 'var(--bear)' }}>Top PUT:</strong>{' '}
                {patrolSummary.top_put.map((x) => `${x.t}(${x.s})`).join(' · ')}
              </div>
            )}
          </div>
        )}
      </div>

      {/* Stage filter row — mirrors /conviction */}
      <div className="filter-row">
        <span className="lbl">stage</span>
        {(['STEALTH', 'FORMING', 'TRADE_NOW', 'WAIT', 'ALL'] as StageKey[]).map((k) => (
          <span
            key={k}
            className={`chip ${stage === k ? 'active' : ''}`}
            onClick={() => setStage(k)}
          >
            {k === 'STEALTH' ? '🌱 Stealth' : k === 'FORMING' ? '🌿 Forming' : k === 'TRADE_NOW' ? '⚡ Trade now' : k === 'WAIT' ? '⏸ Wait' : 'All'}
            <span className="count">{stageCounts[k]}</span>
          </span>
        ))}
      </div>

      {/* Side filter row */}
      <div className="filter-row">
        <span className="lbl">side</span>
        {(['ALL', 'CALL', 'PUT'] as SideKey[]).map((k) => (
          <span
            key={k}
            className={`chip ${side === k ? 'active' : ''}`}
            onClick={() => setSide(k)}
          >
            {k === 'CALL' ? '🐂 Call' : k === 'PUT' ? '🐻 Put' : 'All'}
            <span className="count">{sideCounts[k]}</span>
          </span>
        ))}
      </div>

      {/* Cards */}
      {!cv && <div className="empty">Loading {meta.label}…</div>}
      {cv && filtered.length === 0 && (
        <div className="empty">No tickers match the current filter.</div>
      )}
      {filtered.map((r) => (
        <div key={r.ticker} ref={(el) => { cardRefs.current[r.ticker] = el; }}>
          <TickerCard row={r} />
        </div>
      ))}
    </main>
  );
}
