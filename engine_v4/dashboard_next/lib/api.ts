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
// Today's breakouts — tickers that broke out above N-day high or vol threshold today.
// Source: v4_today_breakouts.json (output of v4_today_breakout_scanner.py)
export const fetchTodayBreakouts = () => getJSON<any>('/api/v4/today_breakouts');

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
  next_earnings_date?: string;  // 'YYYY-MM-DD' from snapshot info.data
  announce_time?: string;       // 'premarket' | 'postmarket' | 'unknown'
}
export interface Picks77Resp {
  generated_utc?: string;
  source?: string;
  thresholds?: { mega_min: number; mid_min: number; small_min: number; unit: string };
  counts: { mega: number; mid: number; small: number; unscannable: number; indices?: number };
  buckets: {
    mega: BucketTicker[];
    mid: BucketTicker[];
    small: BucketTicker[];
    indices?: BucketTicker[];
    unscannable: { ticker: string; mcap_b: null; reason: string }[];
  };
}
export const fetchPicks77 = () => getJSON<Picks77Resp>('/api/v4/picks_77');

// Per-ticker baseline of last-30-min net call premium.
// Used by the predictor to z-score current flow vs the ticker's own norm.
// Quality field tells the UI which formula to use (z-score vs absolute-$).
export interface EodBaseline {
  median_m: number;       // median last-30m net_call_$ in millions
  std_m: number;          // raw stdev (may be 0 for thin data)
  std_floor_m: number;    // floored stdev (used as denominator for z)
  n_days: number;         // sessions in baseline
  n_nonzero: number;      // sessions with non-trivial flow
  quality: 'STRONG' | 'USABLE' | 'THIN';
}
export interface EodBaselinesResp {
  generated_utc?: string;
  sessions_loaded?: number;
  tickers_baselined?: number;
  bucket_quality?: Record<string, { STRONG: number; USABLE: number; THIN: number; NO_DATA: number }>;
  baselines: Record<string, EodBaseline>;
}
export const fetchEodBaselines = () => getJSON<EodBaselinesResp>('/api/v4/eod_flow_baselines');

// Per-ticker max 30-min CALL/PUT surge anywhere in today's session.
// Source: data/intraday_surge_state.json (computed by v4_intraday_surge_compute.py).
// Catches institutional surges that don't happen during power hour —
// 79-86% of put surges occur at OPEN per backtest.
export interface IntradaySurgeTicker {
  ticker: string;
  max_call_m: number;
  max_call_window_t?: string;
  max_put_m: number;
  max_put_window_t?: string;
  call_alert?: boolean;
  put_alert?: boolean;
}
export interface IntradaySurgeResp {
  generated_utc?: string;
  snapshot_used?: string;
  window_size_min?: number;
  call_alert_threshold_m?: number;
  put_alert_threshold_m?: number;
  n_call_alerts?: number;
  n_put_alerts?: number;
  tickers: Record<string, IntradaySurgeTicker>;
}
export const fetchIntradaySurge = () => getJSON<IntradaySurgeResp>('/api/v4/intraday_surge');

// Silence-streak detector — silent N days then BREAK pattern.
// Captures the institutional "pent-up energy releases" signal the user
// observed in AMD/NVDA/META/AMZN.
export interface SilenceStreakTicker {
  ticker: string;
  silent_streak_days: number;
  today_call_m?: number | null;
  today_z?: number | null;
  is_breakout: boolean;
  breakout_side?: 'CALL' | 'PUT' | null;
  breakout_strength?: number;
  baseline_quality?: string;
  baseline_n_days?: number;
}
export interface SilenceStreakResp {
  generated_utc?: string;
  today_iso?: string;
  thresholds?: { silent_threshold: number; break_threshold: number; min_silent_days: number };
  n_breakouts?: number;
  tickers: Record<string, SilenceStreakTicker>;
}
export const fetchSilenceStreak = () => getJSON<SilenceStreakResp>('/api/v4/silence_streak');
