'use client';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import {
  fetchPicks77, fetchPatrol, fetchConviction, fetchStaging,
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
  is_stealth?: boolean;
  is_stealth_dist?: boolean;
  // staging (v8)
  staging_score?: number;
  staging_put_score?: number;
  is_buy?: boolean;
  is_put_buy?: boolean;
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

          // Build label chips for this row
          type Lbl = { text: string; bg: string; fg: string; border?: string; title?: string };
          const labels: Lbl[] = [];

          // Patrol verdict label (always present)
          labels.push({
            text: `${tag.text}${r.patrol_score ? ` ${r.patrol_score}` : ''}`,
            bg: tag.bg, fg: tag.fg,
            title: 'Patrol verdict (today)',
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

          return (
            <article key={r.ticker} className="card" style={{ padding: '10px 14px', display: 'flex', gap: 12, alignItems: 'center', flexWrap: 'wrap' }}>
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
                <span style={{ fontSize: 11, color: 'var(--dim)', marginLeft: 4 }}>
                  fires <span style={{ color: 'var(--bull)' }}>{r.acc_n}</span>/<span style={{ color: 'var(--bear)' }}>{r.dist_n}</span>
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
