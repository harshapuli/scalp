"""
V4 HISTORICAL FLOW BACKFILL — pull 60 days of UW per-ticker last-30-min flow.

For each ticker in picks_77 (mega + mid + small + indices ≈ 84 names),
fetch /api/stock/{T}/net-prem-ticks?date=YYYY-MM-DD for the last N trading
days. Compute last_30m_net_call_$ per (ticker, day). Join with close
prices from Alpaca bars cache. Write to a historical history file.

Why: our snapshot history is only 6 days. UW lets us pull historical
intraday flow per day. With 60 days × 71 tickers we get ~3000 ticker-days
of real samples — enough to firm up the per-ticker baselines AND re-run
the action-label backtest with a sample that actually means something.

Cost: ~3000 UW REST calls. Throttled at ~90 req/min via v4_uw_throttle.
Total runtime ~35-50 minutes. ONE-TIME cost (subsequent days are picked
up by the daily snapshot pipeline).

Run: python3 v4_historical_flow_backfill.py [--days 60] [--max-tickers 999]
Output: data/eod_flow_history_60d.json
"""
import os, sys, json, glob, time
from datetime import date, timedelta, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from v4_uw_helpers import _get  # type: ignore

OUT = os.path.join(HERE, 'data', 'eod_flow_history_60d.json')
PICKS = os.path.join(HERE, 'data', 'picks_77.json')
BARS_CACHE = os.path.join(HERE, 'data', 'bars_cache')


def trading_days_back(end_date: date, n: int):
    """Return last n weekdays ending on end_date (skip Sat/Sun, no holiday handling)."""
    out = []
    cur = end_date
    while len(out) < n:
        if cur.weekday() < 5:
            out.append(cur)
        cur = cur - timedelta(days=1)
    return list(reversed(out))


def load_close_for_ticker_date(tkr: str, d: date):
    """Read Alpaca bar cache for a ticker, find close on given date."""
    p = os.path.join(BARS_CACHE, f'{tkr}_daily.json')
    if not os.path.exists(p): return None
    try:
        bars = json.load(open(p)).get('bars') or []
    except: return None
    target = d.strftime('%Y-%m-%d')
    for b in bars:
        if (b.get('t') or '')[:10] == target:
            try: return float(b['c'])
            except: return None
    return None


def compute_last_30m_call(ticks: list) -> tuple:
    """Sum net_call_premium for ticks in 19:30-20:00 UTC window. Returns (sum_$, n_ticks)."""
    if not isinstance(ticks, list): return None, 0
    s = 0.0
    n = 0
    for t in ticks:
        hh_mm = (t.get('tape_time') or t.get('t') or '')[11:16]
        if '19:30' <= hh_mm <= '20:00':
            n += 1
            try: s += float(t.get('net_call_premium', 0) or 0)
            except: pass
    return (s, n) if n else (None, 0)


def compute_intraday_max_windows(ticks: list, window_min: int = 30) -> dict:
    """For one day's ticks, slide a 30-min window and find max CALL + max PUT.
    Returns {'max_call_m': X, 'max_call_t': 'HH:MM', 'max_put_m': X, 'max_put_t': 'HH:MM'}
    """
    if not isinstance(ticks, list) or len(ticks) < window_min:
        return {}
    sorted_ticks = sorted(ticks, key=lambda t: (t.get('tape_time') or t.get('t') or ''))
    best_call = best_put = 0.0
    best_call_t = best_put_t = None
    for i in range(len(sorted_ticks) - window_min + 1):
        s = 0.0
        for t in sorted_ticks[i:i + window_min]:
            try: s += float(t.get('net_call_premium', 0) or 0)
            except: pass
        t_iso = sorted_ticks[i].get('tape_time') or sorted_ticks[i].get('t')
        if s > best_call: best_call = s; best_call_t = t_iso
        if s < best_put:  best_put = s;  best_put_t = t_iso
    return {
        'intraday_max_call_m': round(best_call / 1e6, 3),
        'intraday_max_call_t': (best_call_t or '')[11:16],
        'intraday_max_put_m':  round(best_put / 1e6, 3),
        'intraday_max_put_t':  (best_put_t or '')[11:16],
    }


def main():
    days = 60
    max_tickers = 999
    if '--days' in sys.argv:
        days = int(sys.argv[sys.argv.index('--days') + 1])
    if '--max-tickers' in sys.argv:
        max_tickers = int(sys.argv[sys.argv.index('--max-tickers') + 1])

    picks = json.load(open(PICKS))
    tickers = []
    for k in ('mega', 'mid', 'small', 'indices'):
        for r in (picks.get('buckets') or {}).get(k, []) or []:
            tickers.append(r['ticker'])
    tickers = sorted(set(tickers))[:max_tickers]
    print(f'tickers: {len(tickers)}', file=sys.stderr)

    # Date range — last N weekdays ending yesterday
    today = date.today()
    end = today - timedelta(days=1)
    dates = trading_days_back(end, days)
    print(f'dates: {dates[0]} → {dates[-1]} ({len(dates)} sessions)', file=sys.stderr)

    # Existing data — resume support
    existing = {}
    if os.path.exists(OUT):
        try: existing = json.load(open(OUT)).get('history') or {}
        except: pass

    history = dict(existing)  # ticker → {date: {call_m, close_px}}
    total_calls = 0
    skipped_cached = 0
    failed = 0
    started = time.time()

    for ti, tkr in enumerate(tickers, 1):
        if tkr not in history: history[tkr] = {}
        for d in dates:
            d_iso = d.isoformat()
            # Skip if already cached AND has the new intraday_max field.
            # Older entries without intraday_max get re-fetched to populate.
            existing = history[tkr].get(d_iso)
            if existing and 'intraday_max_call_m' in existing:
                skipped_cached += 1
                continue
            try:
                r = _get(f'/api/stock/{tkr}/net-prem-ticks', {'date': d_iso})
                ticks = r.get('data') if isinstance(r, dict) else None
                call_sum, n = compute_last_30m_call(ticks)
                close_px = load_close_for_ticker_date(tkr, d)
                # Intraday max windows (anywhere in session)
                intraday = compute_intraday_max_windows(ticks)
                history[tkr][d_iso] = {
                    'call_m': round(call_sum / 1e6, 3) if call_sum is not None else None,
                    'n_ticks': n,
                    'close_px': close_px,
                    **intraday,
                }
                total_calls += 1
            except Exception as e:
                history[tkr][d_iso] = {'error': str(e)[:120]}
                failed += 1
                total_calls += 1
        # Periodic save (every ticker) so we can resume if interrupted
        elapsed = time.time() - started
        rate = total_calls / max(elapsed, 1) * 60
        with open(OUT, 'w') as f:
            json.dump({
                'generated_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                'days': days,
                'date_range': [dates[0].isoformat(), dates[-1].isoformat()],
                'tickers_count': len(tickers),
                'completed_tickers': ti,
                'total_calls': total_calls,
                'skipped_cached': skipped_cached,
                'failed': failed,
                'history': history,
            }, f, indent=2, default=str)
        print(f'[{ti}/{len(tickers)}] {tkr:<6} done  ·  '
              f'{total_calls} calls @ {rate:.0f}/min  ·  cached={skipped_cached}  failed={failed}',
              file=sys.stderr)

    # Final save
    print(f'\nDONE  ·  {total_calls} new calls  ·  {skipped_cached} cached skips  ·  {failed} failures',
          file=sys.stderr)
    print(f'Saved → {OUT}', file=sys.stderr)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
