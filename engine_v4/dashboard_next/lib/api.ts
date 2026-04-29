/**
 * V4 API client. All requests go through Next's rewrite to Python on :8084.
 * Single client so refetch behavior is consistent.
 */

async function getJSON<T>(path: string): Promise<T> {
  const r = await fetch(path, { cache: 'no-store' });
  if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
  return (await r.json()) as T;
}

async function postJSON<T>(path: string): Promise<T> {
  const r = await fetch(path, { method: 'POST', cache: 'no-store' });
  if (!r.ok) throw new Error(`${path} → HTTP ${r.status}`);
  return (await r.json()) as T;
}

// ─── Conviction ───────────────────────────────────────────────────────
export interface ConvictionRow {
  ticker: string;
  cap_bucket?: string;
  direction?: string;
  score?: number;
  max_score?: number;
  confidence?: number;
  verdict?: string;
  conviction_action?: string;
  reasons?: string[];
  missing?: string[];
  day_pct?: number;
  last_price?: number;
  prev_close?: number;
  call_vol?: number;
  put_vol?: number;
  is_stealth?: boolean;
  is_stealth_dist?: boolean;
  bullish_score?: number;
  bearish_score?: number;
  fired_at_utc?: string;
  snapshot_fetched_at?: string;
  trade_idea?: any;
  // Allow any extra
  [k: string]: any;
}
export interface ConvictionResp {
  generated_at_utc?: string;
  source_file?: string;
  universe_size?: number;
  results: ConvictionRow[];
}
export const fetchConviction = () => getJSON<ConvictionResp>('/api/v4/conviction');

// ─── Patrol ──────────────────────────────────────────────────────────
export interface PatrolFactors {
  vol_x?: number;
  price_chg_pct?: number;
  'dark_pool_$'?: number;
  dark_blocks_10k?: number;
  aggressor_ratio?: number;
  'net_call_$'?: number;
  'net_put_$'?: number;
  'pm_dp_total_$'?: number;
  pm_dp_blocks?: number;
  [k: string]: any;
}
export interface PatrolTickerState {
  score: number;
  verdict: string;
  acc_strength?: number;
  dist_strength?: number;
  factors?: PatrolFactors;
  today_first_acc_fire?: any[];
  today_first_dist_fire?: any[];
  today_max_score?: number;
  today_min_score?: number;
}
export interface PatrolResp {
  ts_utc?: string;
  session_date?: string;
  baseline_age_h?: number;
  baseline_stale?: boolean;
  tickers: Record<string, PatrolTickerState>;
}
export const fetchPatrol = () => getJSON<PatrolResp>('/api/v4/flow_patrol');

// ─── Health ──────────────────────────────────────────────────────────
export interface HealthResp {
  checked_at_utc?: string;
  checked_at_pt?: string;
  session_phase?: string;
  overall?: 'OK' | 'DEGRADED' | 'FAIL';
  n_down?: number;
  n_stale?: number;
  n_endpoint_fail?: number;
  patrol_dist?: any;
  ws_stats?: any;
  live_overlay?: any;
  alpaca_cache?: any;
  issues?: string[];
}
export const fetchHealth = () => getJSON<HealthResp>('/api/v4/health');

// ─── Staging / Prebreakout ──────────────────────────────────────────
export const fetchStaging = () => getJSON<any>('/api/v4/staging');
export const revalidateStaging = () => postJSON<any>('/api/v4/staging_revalidate');

// ─── Lifecycle / Breakout ───────────────────────────────────────────
export const fetchLifecycle = () => getJSON<any>('/api/v4/lifecycle');
export const refreshLifecycle = () => postJSON<any>('/api/v4/lifecycle_refresh');

// ─── Today ──────────────────────────────────────────────────────────
export const fetchToday = () => getJSON<any>('/api/v4/today');
export const refreshToday = () => postJSON<any>('/api/v4/today_refresh');

// ─── Continuation ───────────────────────────────────────────────────
export const fetchContinuation = () => getJSON<any>('/api/v4/continuation_signals');
export const revalidateContinuation = () => postJSON<any>('/api/v4/continuation_revalidate');

// ─── Misc ───────────────────────────────────────────────────────────
export const fetchPositions = () => getJSON<any>('/api/v4/positions');
export const fetchIntradayTradeables = () => getJSON<any>('/api/v4/intraday_tradeables');
export const fetchSignalFires = () => getJSON<any>('/api/v4/signal_fires');

// ─── Picks 77 buckets ───────────────────────────────────────────────
// User's curated 156-ticker watchlist split into mega/mid/small.
// Source: data/picks_77.json. Each /mega /mid /small page filters
// patrol + conviction + staging output to its bucket.
export interface BucketTicker {
  ticker: string;
  mcap_b: number | null;
  sector?: string;
}
export interface Picks77Resp {
  generated_utc?: string;
  source?: string;
  thresholds?: { mega_min: number; mid_min: number; small_min: number; unit: string };
  counts: { mega: number; mid: number; small: number; unscannable: number };
  buckets: {
    mega: BucketTicker[];
    mid: BucketTicker[];
    small: BucketTicker[];
    unscannable: { ticker: string; mcap_b: null; reason: string }[];
  };
}
export const fetchPicks77 = () => getJSON<Picks77Resp>('/api/v4/picks_77');
