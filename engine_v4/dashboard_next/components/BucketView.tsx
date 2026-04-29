'use client';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import {
  fetchPicks77, fetchPatrol, fetchConviction,
  type Picks77Resp, type PatrolResp, type ConvictionResp,
} from '@/lib/api';
import { fmtAge } from '@/lib/format';
import RefreshStatus from '@/components/RefreshStatus';

type BucketName = 'mega' | 'mid' | 'small';

const BUCKET_LABELS: Record<BucketName, { emoji: string; label: string; sub: string }> = {
  mega:  { emoji: '🐳', label: 'Mega',  sub: '≥$200B market cap' },
  mid:   { emoji: '🐬', label: 'Mid',   sub: '$20B–$200B' },
  small: { emoji: '🐟', label: 'Small', sub: 'under $20B' },
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

  const cvByT = useMemo(() => {
    const m: Record<string, any> = {};
    for (const r of cv?.results || []) m[r.ticker] = r;
    return m;
  }, [cv]);

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
      };
    });
  }, [picks, patrol, cvByT, bucket]);

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
            || ['BUY','WATCH'].includes(r.cv_verdict || '');
      });
    } else if (filter === 'ACC') {
      out = rows.filter(r => r.patrol_verdict === 'ACC' || r.patrol_verdict === 'STRONG_ACC');
    } else if (filter === 'DIST') {
      out = rows.filter(r => r.patrol_verdict === 'DIST' || r.patrol_verdict === 'STRONG_DIST');
    }
    // Sort: STRONG_ACC > ACC > BUY > WATCH > DIST > STRONG_DIST > NEUTRAL
    const rank = (r: RowState) => {
      if (r.patrol_verdict === 'STRONG_ACC') return 6;
      if (r.patrol_verdict === 'ACC') return 5;
      if (r.cv_verdict === 'BUY') return 4;
      if (r.cv_verdict === 'WATCH') return 3;
      if (r.patrol_verdict === 'DIST') return 2;
      if (r.patrol_verdict === 'STRONG_DIST') return 1;
      return 0;
    };
    return [...out].sort((a, b) => rank(b) - rank(a) ||
      (b.positioning_score || 0) - (a.positioning_score || 0) ||
      (b.acc_n - b.dist_n) - (a.acc_n - a.dist_n));
  }, [rows, filter]);

  const verdictTag = (v?: string) => {
    if (v === 'STRONG_ACC') return { bg: 'rgba(63,140,71,0.20)', fg: 'var(--bull)', text: 'STRONG ACC' };
    if (v === 'ACC') return { bg: 'rgba(63,140,71,0.10)', fg: 'var(--bull)', text: 'ACC' };
    if (v === 'STRONG_DIST') return { bg: 'rgba(176,53,40,0.20)', fg: 'var(--bear)', text: 'STRONG DIST' };
    if (v === 'DIST') return { bg: 'rgba(176,53,40,0.10)', fg: 'var(--bear)', text: 'DIST' };
    return { bg: 'rgba(176,176,176,0.08)', fg: 'var(--mid)', text: 'NEUTRAL' };
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
            <div>ACC firing</div>
            {counts.n_strong_acc > 0 && <div style={{ fontSize: 10, color: 'var(--bull)', fontWeight: 600 }}>incl. {counts.n_strong_acc} STRONG</div>}
          </div>
          <div>
            <div style={{ fontSize: 22, fontWeight: 700, color: 'var(--bear)' }}>{counts.dist}</div>
            <div>DIST firing</div>
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
            <div style={{ fontSize: 10 }}>acc / dist</div>
          </div>
        </div>
      </div>

      <div className="filter-row">
        <span className="lbl">show</span>
        <span className={`chip ${filter === 'ACTIVE' ? 'active' : ''}`} onClick={() => setFilter('ACTIVE')}>
          ⚡ Active <span className="count">{counts.active}</span>
        </span>
        <span className={`chip ${filter === 'ACC' ? 'active' : ''}`} onClick={() => setFilter('ACC')}>
          🟢 ACC <span className="count">{counts.acc}</span>
        </span>
        <span className={`chip ${filter === 'DIST' ? 'active' : ''}`} onClick={() => setFilter('DIST')}>
          🔴 DIST <span className="count">{counts.dist}</span>
        </span>
        <span className={`chip ${filter === 'ALL' ? 'active' : ''}`} onClick={() => setFilter('ALL')}>
          All <span className="count">{counts.all}</span>
        </span>
      </div>

      {!picks && <div className="empty">Loading {meta.label}…</div>}
      {picks && filtered.length === 0 && (
        <div className="empty">
          {filter === 'ACTIVE' ? `No setups firing in ${meta.label} right now.` :
           filter === 'ACC' ? `No ACC verdicts in ${meta.label}.` :
           filter === 'DIST' ? `No DIST verdicts in ${meta.label}.` :
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
          return (
            <article key={r.ticker} className="card" style={{ padding: '10px 14px', display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
              <div style={{ flex: '0 0 auto', minWidth: 70 }}>
                <span className="ticker" style={{ fontSize: 18, fontWeight: 700 }}>{r.ticker}</span>
              </div>
              <div style={{ flex: '0 0 auto' }}>
                <span style={{
                  background: tag.bg, color: tag.fg, padding: '3px 10px',
                  borderRadius: 4, fontSize: 11, fontWeight: 600,
                  fontFamily: 'Poppins, Arial, sans-serif',
                }}>{tag.text} {r.patrol_score ?? ''}</span>
              </div>
              <div style={{ flex: '0 0 auto', fontSize: 11, color: 'var(--dim)' }}>
                <span style={{ color: isInst ? 'var(--accent)' : 'var(--dim)', fontWeight: isInst ? 600 : 400 }}>
                  {positioning} {r.positioning_score ?? 0}
                </span>
              </div>
              <div style={{ flex: '0 0 auto', fontSize: 11, color: 'var(--dim)' }}>
                fires <span style={{ color: 'var(--bull)' }}>{r.acc_n}</span>/
                <span style={{ color: 'var(--bear)' }}>{r.dist_n}</span>
                {r.has_flip && <span style={{ color: 'var(--bear)', marginLeft: 4 }}>⚠flip</span>}
              </div>
              <div style={{ flex: '1 1 auto', textAlign: 'right' }}>
                {dayPct != null && (
                  <span className={dpClass} style={{ marginRight: 12, fontFamily: 'Poppins, Arial, sans-serif', fontVariantNumeric: 'tabular-nums' }}>
                    {dayPct >= 0 ? '+' : ''}{dayPct.toFixed(2)}%
                  </span>
                )}
                {r.live_price != null && (
                  <span style={{ marginRight: 12, fontFamily: 'Poppins, Arial, sans-serif', fontVariantNumeric: 'tabular-nums' }}>
                    ${r.live_price.toFixed(2)}
                  </span>
                )}
                {r.cv_verdict && (
                  <span style={{ fontSize: 11, color: 'var(--dim)' }}>
                    {r.cv_verdict}{r.cv_urgency ? `/${r.cv_urgency}` : ''}
                  </span>
                )}
                {r.mcap_b != null && r.mcap_b > 0 && (
                  <span style={{ marginLeft: 12, fontSize: 11, color: 'var(--dim)' }}>
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
