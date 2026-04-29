"""V4 Dashboard Server — serves two parallel dashboards at /v1/ and /v2/.

  /v1/  → BASELINE strategy (Friday-close logic, no Phase 1-5 enhancements)
  /v2/  → ENHANCED strategy (current production)
  /     → redirects to /v2/ (default)

Each dashboard fetches its own API endpoints (suffixed accordingly) so the same
HTML/JS code base works for both modes. The dashboard JS auto-detects the URL
prefix and routes API calls to the right backing JSON file.
"""
import json
import os
import requests
from datetime import datetime, timezone
from http.server import HTTPServer, ThreadingHTTPServer, SimpleHTTPRequestHandler

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'swing_trade_strategy', '.env'))
except Exception:
    pass

ALPACA_TRADING_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
ALPACA_HEADERS = {
    "APCA-API-KEY-ID": os.getenv("ALPACA_API_KEY", ""),
    "APCA-API-SECRET-KEY": os.getenv("ALPACA_API_SECRET", ""),
}

PORT = 8084
BASE_DIR = os.path.dirname(__file__)
DIRECTORY = os.path.join(BASE_DIR, "dashboard")

# v2 (enhanced) — current production files
SIGNALS_FILE = os.path.join(BASE_DIR, "v4_signals.json")
WATCHES_FILE = os.path.join(BASE_DIR, "v4_watches.json")
LEDGER_FILE = os.path.join(BASE_DIR, "v4_ledger.json")
BREAKOUTS_FILE = os.path.join(BASE_DIR, "v4_preposition_watchlist.json")
POSITIONS_FILE = os.path.join(BASE_DIR, "v4_paper_positions.json")
GAPS_FILE = os.path.join(BASE_DIR, "v4_gap_watchlist.json")

# v1 (baseline) — shadow engine output
SIGNALS_BASELINE_FILE = os.path.join(BASE_DIR, "v4_signals_baseline.json")
WATCHES_BASELINE_FILE = os.path.join(BASE_DIR, "v4_watches_baseline.json")
LEDGER_BASELINE_FILE = os.path.join(BASE_DIR, "v4_ledger_baseline.json")

# Shared (no v1/v2 fork — same data)
MISSED_TRADES_FILE = os.path.join(BASE_DIR, "v4_missed_trades.json")
ACCUMULATION_FILE = os.path.join(BASE_DIR, "v4_accumulation_signals.json")
ACCUMULATION_SUCCESS_FILE = os.path.join(BASE_DIR, "v4_accumulation_success.json")
BREAKOUT_CANDIDATES_FILE = os.path.join(BASE_DIR, "v4_breakout_candidates.json")
FOOTPRINT_HISTORY_FILE   = os.path.join(BASE_DIR, "v4_footprint_history.jsonl")
FOOTPRINT_BACKTEST_FILE  = os.path.join(BASE_DIR, "v4_footprint_backtest.json")
PATTERN_TRADES_FILE      = os.path.join(BASE_DIR, "v4_pattern_trades.jsonl")
ZERODTE_SIGNALS_FILE     = os.path.join(BASE_DIR, "v4_0dte_signals.jsonl")
# Additive — LSFC + pre-break scanners write their own jsonl files
LSFC_SIGNALS_FILE        = os.path.join(BASE_DIR, "v4_lsfc_signals.jsonl")
PREBREAK_SIGNALS_FILE    = os.path.join(BASE_DIR, "v4_prebreak_signals.jsonl")


# ----------------------------------------------------------------------
# BCS/PCS overlay helpers — feed the conviction page synthetic TRADE_NOW
# cards from v4_bcs_pcs_scorer.decide_side() fires. See _serve_conviction.
# ----------------------------------------------------------------------

def _bcs_pcs_reasons(row, side):
    """Build direction-appropriate reasons / missing lists for a BCS/PCS fire.
    Replaces the rules-engine's bullish rules (PUT_SOLD ✓ / PUT_SUPPRESS ✓ /
    CALL_BUYING / CALL_VOL / CALL_ASK / DP_MODERATE) which read semantically
    wrong next to a PUT signal. Each entry is a short human-readable string;
    the dashboard renders `reasons` as "✓ …" and `missing` as "— …".
    """
    def _f(v, d=0.0):
        if v is None or v == '' or v == '—' or v == '-': return d
        try: return float(v)
        except (ValueError, TypeError): return d

    biggest = _f(row.get('biggest_bar_pct'))
    cratio  = _f(row.get('cratio'))
    pratio  = _f(row.get('pratio'))
    netC    = _f(row.get('net_call_m'))
    netP    = _f(row.get('net_put_m'))
    callAsk = _f(row.get('call_ask_pct'))
    dp_ab   = _f(row.get('dp_above'))
    dp_bl   = _f(row.get('dp_below'))
    pmDP    = _f(row.get('premarket_dp_m'))
    vol_r   = _f(row.get('vol_ratio'))
    patt    = (row.get('pattern') or '').upper().strip() or 'NONE'
    first1  = (row.get('first_1pct_et') or '').strip()
    iv_rank = _f(row.get('iv_rank'))

    reasons, missing = [], []
    if side == 'PUT':
        # biggest-bar bearish
        (reasons if biggest <= -1.0 else missing).append(
            f"BEARISH_BAR: biggest={biggest:+.2f}% (need ≤-1%)")
        # put volume elevated
        (reasons if pratio >= 1.5 else missing).append(
            f"PUT_VOL_SURGE: pratio={pratio:.2f}x (need ≥1.5x)")
        # put buying (positive net_put)
        (reasons if netP > 0 else missing).append(
            f"PUT_BUYING: net_put=${netP:+.2f}M (need >$0)")
        # put-dominant ask — puts lifted at ask when calls aren't
        (reasons if callAsk < 50 and callAsk > 0 else missing).append(
            f"PUT_ASK_PROXY: call_ask={callAsk:.0f}% (need <50%)")
        # dark-pool bearish distribution
        dp_total = dp_ab + dp_bl
        if dp_total > 10:
            below_sh = 100.0 * (dp_bl - dp_ab) / dp_total
            (reasons if below_sh >= 20 else missing).append(
                f"DP_BEARISH: below_share={below_sh:+.0f}% (need ≥+20%)")
        else:
            missing.append(f"DP_BEARISH: prints={int(dp_total)} (need >10)")
        # premarket DP size
        (reasons if pmDP >= 5.0 else missing).append(
            f"PM_DP_SIZE: ${pmDP:.1f}M (need ≥$5M)")
        # opening-drive timing
        (reasons if first1 and first1 < '11:00' else missing).append(
            f"OPENING_DRIVE: first_1%={first1 or '—'} (need ≤10:00)")
        # volume surge
        (reasons if vol_r >= 1.5 else missing).append(
            f"VOL_SURGE: vol_ratio={vol_r:.2f}x (need ≥1.5x)")
        # bearish pattern
        (reasons if patt in ('DISTRIBUTION','SQUEEZE') else missing).append(
            f"BEARISH_PATTERN: {patt} (DISTRIBUTION/SQUEEZE)")
        # IV supplement
        if iv_rank >= 60:
            reasons.append(f"IV_ELEVATED: iv_rank={iv_rank:.0f} (≥60)")
    else:  # CALL
        (reasons if biggest >= 1.0 else missing).append(
            f"BULLISH_BAR: biggest={biggest:+.2f}% (need ≥+1%)")
        (reasons if cratio >= 1.5 else missing).append(
            f"CALL_VOL_SURGE: cratio={cratio:.2f}x (need ≥1.5x)")
        (reasons if netC > 0 else missing).append(
            f"CALL_BUYING: net_call=${netC:+.2f}M (need >$0)")
        (reasons if callAsk >= 50 else missing).append(
            f"CALL_ASK: call_ask={callAsk:.0f}% (need ≥50%)")
        dp_total = dp_ab + dp_bl
        if dp_total > 10:
            above_sh = 100.0 * (dp_ab - dp_bl) / dp_total
            (reasons if above_sh >= 20 else missing).append(
                f"DP_BULLISH: above_share={above_sh:+.0f}% (need ≥+20%)")
        else:
            missing.append(f"DP_BULLISH: prints={int(dp_total)} (need >10)")
        (reasons if first1 and first1 < '11:00' else missing).append(
            f"OPENING_DRIVE: first_1%={first1 or '—'} (need ≤10:00)")
        (reasons if vol_r >= 1.5 else missing).append(
            f"VOL_SURGE: vol_ratio={vol_r:.2f}x (need ≥1.5x)")
        (reasons if patt in ('MOMENTUM','SQUEEZE','CONTINUATION','STEALTH') else missing).append(
            f"BULLISH_PATTERN: {patt}")
    return reasons, missing


def _load_bcs_pcs_fires(base_dir):
    """Scan v4_full_side_by_side.csv via v4_bcs_pcs_scorer.decide_side.
    Returns {ticker_upper: {side, score, bcs, pcs, bucket, row}} for the
    latest date present in the file. MEGA is already skipped inside
    decide_side(). Returns {} on any error (never raises)."""
    try:
        import csv as _csv, sys as _sys
        if base_dir not in _sys.path:
            _sys.path.insert(0, base_dir)
        from v4_bcs_pcs_scorer import decide_side  # noqa: E402
    except Exception:
        return {}
    csv_path = os.path.join(base_dir, 'v4_full_side_by_side.csv')
    if not os.path.exists(csv_path):
        return {}
    try:
        rows = list(_csv.DictReader(open(csv_path)))
    except Exception:
        return {}
    if not rows:
        return {}
    dates = sorted({r.get('date', '') for r in rows if r.get('date')})
    if not dates:
        return {}
    latest = dates[-1]
    out = {}
    for r in rows:
        if r.get('date') != latest:
            continue
        try:
            side, score, meta = decide_side(r)
        except Exception:
            continue
        if side is None:
            continue
        tk = (r.get('ticker') or '').upper().strip()
        if not tk:
            continue
        out[tk] = {
            'side':   side,
            'score':  float(score or 0.0),
            'bcs':    float(meta.get('bcs', 0.0) or 0.0),
            'pcs':    float(meta.get('pcs', 0.0) or 0.0),
            'bucket': meta.get('bucket', 'UNKNOWN'),
            'row':    r,
        }
    return out


def _build_synthetic_bcs_pcs_result(ticker, fire):
    """Build a conviction-result-shaped dict for a BCS/PCS fire when the
    ticker isn't in the rules-engine universe. Only used by _serve_conviction."""
    row  = fire['row']
    side = fire['side']
    score = fire['score']
    conf = int(round(score * 100))

    def _f(v, d=0.0):
        if v is None or v == '' or v == '—' or v == '-': return d
        try: return float(v)
        except (ValueError, TypeError): return d

    last    = _f(row.get('close'))
    iv_rank = _f(row.get('iv_rank'))
    day_pct = _f(row.get('day_pct'))
    nc_m    = _f(row.get('net_call_m'))
    np_m    = _f(row.get('net_put_m'))
    dp_m    = _f(row.get('dp_prem_m'))

    # Use the scanner's helpers for strike / expiry / premium so synthetic
    # cards match the paper-trader trigger contract exactly.
    try:
        import sys as _sys
        bd = os.path.dirname(os.path.abspath(__file__))
        if bd not in _sys.path: _sys.path.insert(0, bd)
        from v4_bcs_pcs_scanner import (_round_strike, _expiry_for_dte,
                                        _estimate_premium)
        from datetime import datetime as _dt2, timezone as _tz2
        strike = _round_strike(last) if last else None
        expiry = _expiry_for_dte(14, _dt2.now(_tz2.utc))
        est_prem = _estimate_premium(last, iv_rank, 14, side) if last else None
    except Exception:
        strike, expiry, est_prem = None, None, None

    contracts_for_1k = None
    if est_prem and est_prem > 0:
        contracts_for_1k = max(1, int(round(1000.0 / (est_prem * 100))))

    # Direction-appropriate reasons/missing so the dashboard panel reads
    # correctly for the fire direction (no more bullish rules next to PUT fires).
    reasons, missing = _bcs_pcs_reasons(row, side)
    hdr = f"BCS={fire['bcs']:.2f} · PCS={fire['pcs']:.2f} · router→{side} {score:.2f}"

    return {
        'ticker':         ticker,
        'cap_bucket':     fire['bucket'],
        'verdict':        'BUY',
        'confidence':     conf,
        'signal_source':  f"BCS_PCS_{side}",
        'pattern_type':   'BCS_PCS',
        'bcs_score':      fire['bcs'],
        'pcs_score':      fire['pcs'],
        'bcs_pcs_bucket': fire['bucket'],
        'day_pct':        day_pct,
        'last_price':     last,
        'net_call_prem':  nc_m * 1e6,
        'net_put_prem':   np_m * 1e6,
        'dp_prem':        dp_m * 1e6,
        'iv_rank':        iv_rank,
        'cratio':         _f(row.get('cratio')),
        'pratio':         _f(row.get('pratio')),
        'score':          round(score * 10, 1),
        'max_score':      10,
        'reasons':        [hdr] + reasons,
        'missing':        missing,
        'stealth_score': 0, 'is_stealth': False, 'stealth_days': 0,
        'trade_idea': {
            'side':                      side,
            'entry_urgency':             'ENTRY',
            'entry_action':              f'{side} fire — paper trader picks up on next tick',
            'strike':                    strike,
            'expiry':                    expiry,
            'dte':                       14,
            'est_premium':               est_prem,
            'contracts_for_1k_notional': contracts_for_1k,
            'pullback_pct':              0,
            'pullback_price':            last,
        },
    }


def _detect_pattern_type(prof):
    """Classify a ticker into one pattern category based on today's flow shape.
    Purely descriptive — no trade recommendation, just 'what kind of move is this?'

    MOMENTUM    — extreme call-vol surge AND big up day   (chase risk, late entry)
    STEALTH     — heavy dark pool AND flat day           (accumulation pre-break)
    CONTINUATION — confirmed bullish flow on steady up day (clean setup)
    DISTRIBUTION — up day BUT bearish call flow          (fade candidate)
    SQUEEZE     — extreme IV with both sides spiking     (binary event)
    NONE        — no dominant pattern
    """
    day = prof.get('day_pct') or 0
    dp  = prof.get('dp_prem') or 0
    nc  = prof.get('net_call_prem') or 0
    np_ = prof.get('net_put_prem') or 0
    cr  = prof.get('cratio') or 0
    pr  = prof.get('pratio') or 0
    iv  = prof.get('iv_rank') or 0

    # Order matters — most specific first
    if iv >= 70 and cr >= 2.5 and pr >= 2.5:     return 'SQUEEZE'
    if cr >= 2.5 and day >= 5:                    return 'MOMENTUM'
    if dp >= 100_000_000 and abs(day) < 2:        return 'STEALTH'
    if day >= 3 and nc < 0:                       return 'DISTRIBUTION'
    if nc > 0 and np_ <= 0 and day >= 1:          return 'CONTINUATION'
    return 'NONE'


# ════════════════════════════════════════════════════════════════════════
# v2 — SMC ZONE ANCHOR (Phase 2 of V2_PULLBACK_DESIGN.md)
# ════════════════════════════════════════════════════════════════════════
# Replaces v1's "% from current price" pullback target with the nearest
# unfilled Fair Value Gap. Falls back to v1 math when no zone is within
# the exhaustion bound. See V2_PULLBACK_DESIGN.md for the full algorithm.
try:
    from v4_smc_zones import smc_zones as _smc_zones_lookup
except Exception:
    _smc_zones_lookup = lambda *_a, **_k: None  # graceful degradation


# Multiple-FVG rule (per V2_PULLBACK_DESIGN.md risk #3): prefer the closest
# unfilled FVG; if it's been there >20 bars (stale), try the next one.
_SMC_STALE_AGE_BARS = 20


def smc_pullback_target(smc, current_price, direction, exhaustion_pct):
    """Return the nearest unfilled SMC zone in the pullback direction.

    For BULLISH: nearest unfilled Bull FVG BELOW current_price (pullback DOWN).
    For BEARISH: nearest unfilled Bear FVG ABOVE current_price (pullback UP).

    Returns a dict with:
      pullback_price  — zone midpoint (used as the entry trigger)
      zone_low        — bottom of the FVG band
      zone_high       — top of the FVG band
      zone_age_bars   — how many daily bars since the FVG formed
      zone_type       — 'BULL_FVG' | 'BEAR_FVG'

    Returns None when:
      • no smc data (caller falls back to v1 %)
      • no candidate zones in pullback direction
      • zone is BEYOND the exhaustion bound (don't anchor on a target
        farther than what would be flagged as EXHAUSTED)
      • zone WIDTH itself blows the exhaustion bound (a $20 FVG on a
        $100 stock is 20% — wider than the entire exhausted window, and
        the midpoint becomes meaningless as an entry trigger)
    """
    if not smc:
        return None

    if direction == 'BULLISH':
        cands = [z for z in (smc.get('unfilled_bull_fvg') or [])
                 if z.get('high') is not None and z['high'] < current_price]
        if not cands:
            return None
        # Closest first (highest 'high' = least pullback distance)
        cands.sort(key=lambda z: -z['high'])
        chosen = next((c for c in cands if c.get('age_bars', 99) <= _SMC_STALE_AGE_BARS),
                      cands[0])
        lo, hi, age = chosen['low'], chosen['high'], chosen.get('age_bars', 0)
        # Distance from current price to the TOP of the zone (the first
        # touch is what arms the pullback).
        pullback_pct = (current_price - hi) / current_price * 100
        if pullback_pct > exhaustion_pct:
            return None
        # Zone-width-too-wide guard
        zone_width_pct = (hi - lo) / current_price * 100
        if zone_width_pct > exhaustion_pct:
            return None
        return {
            'pullback_price': round((lo + hi) / 2, 2),
            'zone_low': round(lo, 4),
            'zone_high': round(hi, 4),
            'zone_age_bars': age,
            'zone_type': 'BULL_FVG',
        }
    else:  # BEARISH — pullback target ABOVE current price
        cands = [z for z in (smc.get('unfilled_bear_fvg') or [])
                 if z.get('low') is not None and z['low'] > current_price]
        if not cands:
            return None
        cands.sort(key=lambda z: z['low'])  # closest first (lowest 'low')
        chosen = next((c for c in cands if c.get('age_bars', 99) <= _SMC_STALE_AGE_BARS),
                      cands[0])
        lo, hi, age = chosen['low'], chosen['high'], chosen.get('age_bars', 0)
        pullback_pct = (lo - current_price) / current_price * 100
        if pullback_pct > exhaustion_pct:
            return None
        zone_width_pct = (hi - lo) / current_price * 100
        if zone_width_pct > exhaustion_pct:
            return None
        return {
            'pullback_price': round((lo + hi) / 2, 2),
            'zone_low': round(lo, 4),
            'zone_high': round(hi, 4),
            'zone_age_bars': age,
            'zone_type': 'BEAR_FVG',
        }


# CHoCH staleness window — Phase 3 guard. After this many bars the
# "structure shift" is too old to validate counter-trend re-entries.
_CHOCH_FRESHNESS_BARS = 5


def _suggest_trade(prof, result, smc_override=None):
    """Return a dict with strike, expiry, AND signed-aware entry timing.

    4-state decision tree based on (direction, day_pct sign, magnitude):
      EXHAUSTED — aligned move ≥ vol-adjusted threshold (default 8%) — too
                  late to chase; sets verdict_override='WATCH' so caller can
                  demote BUY → WATCH.
      PULLBACK  — aligned move 2%-exhaustion — wait for retrace AGAINST
                  signal direction. v2: pullback_price anchored to nearest
                  unfilled FVG zone instead of pure % math (when available).
      EXTENSION — counter-trend move ≥ 0.5% — wait for price to swing back
                  IN signal direction. v2: GUARDED by CHoCH — demoted to
                  EXHAUSTED if no recent CHoCH in signal direction.
      MARKET    — small move (<2% aligned, <0.5% counter) — fresh setup,
                  enter at current level.

    pb_sign convention:
      +1 = pullback target ABOVE current price
      -1 = pullback target BELOW current price
       0 = no pullback (market entry or exhausted)

    Volatility-aware exhaustion: uses result.price_range_pct (5-day window
    from stealth_accumulation) when available, else falls back to 8%.
    Caller MUST run trade_idea population AFTER stealth enrichment to get
    price_range_pct on `result`.

    smc_override: optional pre-computed SMC zone dict (matching the
    smc_zones() schema). When None, looks up zones from disk cache via
    smc_zones(ticker). Tests can pass a dict to inject zones without
    needing a real bar cache.
    """
    from datetime import datetime as _dt, timezone as _tz, timedelta as _td
    price = prof.get('last_price') or prof.get('prev_close')
    prev  = prof.get('prev_close')
    if not price: return None
    direction = result.get('direction', 'BULLISH')
    side = 'CALL' if direction == 'BULLISH' else 'PUT'

    # Strike increment by price band
    if   price < 25:   inc = 0.5
    elif price < 50:   inc = 1.0
    elif price < 100:  inc = 2.5
    elif price < 500:  inc = 5.0
    else:              inc = 10.0
    strike = round(price / inc) * inc

    # Expiry — next Friday ~14 days out
    today = _dt.now(_tz.utc).date()
    target = today + _td(days=14)
    while target.weekday() != 4: target += _td(days=1)
    expiry = target.isoformat()
    dte = (target - today).days

    # ─── Signed-aware state machine ──────────────────────────────────
    day_pct = (price - prev) / prev * 100 if prev and prev > 0 else 0
    abs_day = abs(day_pct)
    aligned = ((direction == 'BULLISH' and day_pct > 0) or
               (direction == 'BEARISH' and day_pct < 0))

    # Volatility-aware exhaustion threshold.
    # 5-day price_range_pct from stealth_accumulation; map to 4-12% band.
    range_5d = result.get('price_range_pct')
    if range_5d and range_5d > 0:
        exhaustion_threshold = max(4.0, min(range_5d * 0.6, 12.0))
    else:
        exhaustion_threshold = 8.0

    pb_sign = 0
    pb_pct = 0
    verdict_override = None

    # ── v2: SMC zones for the ticker (cached, ~10ms first hit, instant after) ──
    smc = smc_override
    if smc is None:
        ticker_for_smc = prof.get('ticker') or result.get('ticker')
        if ticker_for_smc:
            try:
                smc = _smc_zones_lookup(ticker_for_smc)
            except Exception:
                smc = None

    # New v2 fields — populated only when a zone wins or CHoCH guard fires.
    pullback_zone_low  = None
    pullback_zone_high = None
    pullback_zone_age_bars = None
    pullback_zone_type = None
    choch_guard = 'N/A'  # PASS / BLOCKED_NO_CHOCH / BLOCKED_WRONG_SIDE / BLOCKED_STALE_CHOCH

    if aligned and abs_day >= exhaustion_threshold:
        # ── EXHAUSTED — aligned move blew past the vol threshold ─────
        urgency = 'EXHAUSTED'
        action  = (f'Move already {abs_day:.1f}% (≥{exhaustion_threshold:.1f}% '
                   f'vol threshold) — do NOT chase; wait for genuine reversal '
                   f'or skip')
        verdict_override = 'WATCH'
        pullback_price = None
    elif aligned and abs_day >= 2.0:
        # ── PULLBACK — aligned 2-exhaustion%, wait for retrace ───────
        # v2: try SMC zone anchor first, fall back to v1 % math.
        zone = smc_pullback_target(smc, price, direction, exhaustion_threshold)
        if zone is not None:
            pullback_price = zone['pullback_price']
            pullback_zone_low      = zone['zone_low']
            pullback_zone_high     = zone['zone_high']
            pullback_zone_age_bars = zone['zone_age_bars']
            pullback_zone_type     = zone['zone_type']
            # Compute pb_pct/pb_sign so legacy consumers (and the math
            # invariant `pullback_price ≈ price·(1 + sign·pct/100)`) still
            # hold. pb_sign points from current price TO the zone.
            delta_pct = (pullback_price - price) / price * 100
            pb_sign = -1 if delta_pct < 0 else +1
            pb_pct  = round(abs(delta_pct), 2)
            urgency = 'PULLBACK'
            action  = (f'Wait for {pb_pct:.1f}% pullback to ${pullback_price:.2f} '
                       f'({zone["zone_type"]} ${zone["zone_low"]:.2f}-${zone["zone_high"]:.2f}, '
                       f'{zone["zone_age_bars"]}d old)')
        else:
            # v1 fallback — pure % math (unchanged from v1)
            base = max(0.8, min(abs_day * 0.35, 5.0))
            if (prof.get('iv_rank') or 0) >= 70: base *= 1.3      # IV pop = bigger pullbacks
            if result.get('pattern_type') == 'MOMENTUM': base *= 0.7  # MOMENTUM rarely deep-pulls
            pb_pct = round(base, 2)
            pb_sign = -1 if direction == 'BULLISH' else +1   # pull AGAINST signal
            pullback_price = round(price * (1 + pb_sign * pb_pct / 100), 2)
            urgency = 'PULLBACK'
            action  = f'Wait for {pb_pct:.1f}% pullback to ${pullback_price:.2f}'
    elif (not aligned) and abs_day >= 0.5:
        # ── EXTENSION — counter-trend day, wait for resume ───────────
        # v2 Phase 3: CHoCH guard. If there's no recent CHoCH in the signal
        # direction, "wait for price to bounce back" is mean-reversion in a
        # trend (catching a knife). Demote to EXHAUSTED→WATCH instead.
        # ONLY evaluate the guard when SMC data is available — if the
        # bar cache missed this ticker, fall back to v1 EXTENSION rather
        # than silently demoting all uncached tickers.
        needed_side = 'BULL' if direction == 'BULLISH' else 'BEAR'
        if smc is not None:
            last_choch = smc.get('last_choch') or {}
            choch_side = last_choch.get('side')
            choch_age  = last_choch.get('bars_ago', 99)
            if not choch_side:
                choch_guard = 'BLOCKED_NO_CHOCH'
            elif choch_side != needed_side:
                choch_guard = 'BLOCKED_WRONG_SIDE'
            elif choch_age > _CHOCH_FRESHNESS_BARS:
                choch_guard = 'BLOCKED_STALE_CHOCH'
            else:
                choch_guard = 'PASS'
        else:
            choch_guard = 'N/A'  # no bars cached — preserve v1 behavior

        if choch_guard not in ('PASS', 'N/A'):
            # Demote: counter-trend with no structural confirmation = no entry.
            urgency = 'EXHAUSTED'
            verdict_override = 'WATCH'
            pullback_price = None
            pb_pct = 0
            pb_sign = 0
            reason = {
                'BLOCKED_NO_CHOCH':    'no recent CHoCH on daily TF',
                'BLOCKED_WRONG_SIDE':  f'last CHoCH was {choch_side}, need {needed_side}',
                'BLOCKED_STALE_CHOCH': f'last {needed_side} CHoCH was {choch_age} bars ago (>5)',
            }[choch_guard]
            action = (f'Counter-trend {abs_day:.1f}% but {reason} — '
                      f'wait for structure shift before re-entering')
        else:
            # CHoCH passes (or no SMC data — keep v1 behavior) → proceed.
            base = max(0.5, min(abs_day * 0.5, 2.5))
            pb_pct = round(base, 2)
            pb_sign = +1 if direction == 'BULLISH' else -1   # wait for price IN signal direction
            pullback_price = round(price * (1 + pb_sign * pb_pct / 100), 2)
            urgency = 'EXTENSION'
            if choch_guard == 'PASS':
                action = (f'Counter-trend {abs_day:.1f}% — {needed_side} CHoCH '
                          f'{choch_age}d ago confirms; wait for {pb_pct:.1f}% '
                          f'resume to ${pullback_price:.2f}')
            else:
                action = (f'Counter-trend {abs_day:.1f}% — wait for {pb_pct:.1f}% '
                          f'resume to ${pullback_price:.2f}')
    else:
        # ── MARKET — small move, fresh setup ─────────────────────────
        urgency = 'MARKET'
        action  = 'Setup fresh — enter at current level with tight stop'
        pb_pct = 0
        pb_sign = 0
        pullback_price = round(price, 2)

    est_premium = round(price * 0.035, 2)
    contracts = max(1, int(1000 // max(est_premium * 100, 1)))

    # ── LIMIT ENTRY PRICE (empirical, from v4_backtest_page.py findings) ──
    # Two flavors of limit, depending on urgency:
    #
    # FRESH/MARKET cards → "chase-discount" limit at ±1.5%
    #   Backtest of 79 signals: median d1 MAE = -1.86% (direction-adjusted).
    #   A limit at 1.5% would have filled 54% of the time; 1.0% fills 68%.
    #
    # EXHAUSTED cards → "wait-for-reversal" limit at ±5% counter-move
    #   We don't want to chase a 10%+ move, but a 5% retrace tells us
    #   reversal has begun AND the option is meaningfully cheaper. We
    #   label it differently in the UI so the user knows it's a counter-move
    #   trigger, not a chase price.
    LIMIT_DISCOUNT  = 0.015  # 1.5% — fresh setups
    REVERSAL_RETRACE = 0.05  # 5% — exhausted setups
    if urgency == 'EXHAUSTED':
        # Counter-move limit: BULLISH (CALL) wants a 5% drop to fill;
        # BEARISH (PUT) wants a 5% bounce to fill.
        if direction == 'BULLISH':
            limit_entry_price = round(price * (1 - REVERSAL_RETRACE), 2)
            limit_entry_pct   = -REVERSAL_RETRACE * 100
        else:  # BEARISH
            limit_entry_price = round(price * (1 + REVERSAL_RETRACE), 2)
            limit_entry_pct   = +REVERSAL_RETRACE * 100
        limit_is_reversal = True
    elif direction == 'BULLISH':
        limit_entry_price = round(price * (1 - LIMIT_DISCOUNT), 2)
        limit_entry_pct   = -LIMIT_DISCOUNT * 100  # -1.5%
        limit_is_reversal = False
    else:  # BEARISH
        limit_entry_price = round(price * (1 + LIMIT_DISCOUNT), 2)
        limit_entry_pct   = +LIMIT_DISCOUNT * 100  # +1.5%
        limit_is_reversal = False

    # Pre-earnings IV-pump check (Sprint 2026-04-25) — swap structure when IV elevated.
    iv_rank = prof.get('iv_rank') or 0
    earn_days = prof.get('earnings_days_out')
    is_pre_earnings_high_iv = (
        earn_days is not None and 0 <= earn_days <= 5 and iv_rank >= 60
    )
    if is_pre_earnings_high_iv:
        if direction == 'BULLISH':
            short_strike = round(price * 1.05 / inc) * inc  # 5% above
        else:  # BEARISH PUT
            short_strike = round(price * 0.95 / inc) * inc  # 5% below
        spread_width = abs(short_strike - strike)
        net_debit = round(est_premium * 0.55, 2)
        spread_contracts = max(1, int(1000 // max(net_debit * 100, 1)))
        summary = (f"Buy {prof['ticker']} ${strike:g}/{short_strike:g} {side} debit spread "
                   f"exp {expiry} (~{dte}d) — net debit ~${net_debit} (caps IV crush)")
        action_note = (f'⚠ Earnings in {earn_days}d — IV rank {iv_rank:.0f} elevated. '
                       f'Spread caps premium decay. Skip naked.')
        return {
            'side':             side,
            'structure':        'DEBIT_SPREAD',
            'strike':           strike,
            'short_strike':     short_strike,
            'spread_width':     spread_width,
            'expiry':           expiry,
            'dte':              dte,
            'net_debit':        net_debit,
            'est_premium':      est_premium,
            'contracts_for_1k_notional': spread_contracts,
            'day_pct_already':  round(day_pct, 2),
            'entry_urgency':    urgency,
            'entry_action':     action_note,
            'pullback_price':   pullback_price,
            'pullback_pct':     pb_pct,
            'pullback_sign':    pb_sign,
            'pullback_zone_low':      pullback_zone_low,
            'pullback_zone_high':     pullback_zone_high,
            'pullback_zone_age_bars': pullback_zone_age_bars,
            'pullback_zone_type':     pullback_zone_type,
            'choch_guard':            choch_guard,
            'exhaustion_threshold': round(exhaustion_threshold, 2),
            'verdict_override': verdict_override,
            'limit_entry_price': limit_entry_price,   # NEW: empirical -1.5% limit (or ±5% reversal for EXHAUSTED)
            'limit_entry_pct':   limit_entry_pct,
            'limit_is_reversal': limit_is_reversal,    # True → "wait for reversal", False → "chase discount"
            'summary':          summary,
            'pre_earnings_iv_swap': True,
        }

    return {
        'side':             side,
        'structure':        'NAKED',
        'strike':           strike,
        'expiry':           expiry,
        'dte':              dte,
        'est_premium':      est_premium,
        'contracts_for_1k_notional': contracts,
        'day_pct_already':  round(day_pct, 2),
        'entry_urgency':    urgency,
        'entry_action':     action,
        'pullback_price':   pullback_price,
        'pullback_pct':     pb_pct,
        'pullback_sign':    pb_sign,
        'pullback_zone_low':      pullback_zone_low,
        'pullback_zone_high':     pullback_zone_high,
        'pullback_zone_age_bars': pullback_zone_age_bars,
        'pullback_zone_type':     pullback_zone_type,
        'choch_guard':            choch_guard,
        'exhaustion_threshold': round(exhaustion_threshold, 2),
        'verdict_override': verdict_override,
        'limit_entry_price': limit_entry_price,   # NEW: empirical -1.5% limit (or ±5% reversal for EXHAUSTED)
        'limit_entry_pct':   limit_entry_pct,
        'limit_is_reversal': limit_is_reversal,    # True → "wait for reversal", False → "chase discount"
        'summary':          f"Buy {prof['ticker']} ${strike:g} {side} exp {expiry} (~{dte}d, ATM)",
    }


# ─── Force-Run-All shared state ─────────────────────────────────────────────
# Background-thread state for /api/v4/force_run_all. Lets the user kick a long
# (~3 min) run from one page and watch the progress bar from any other page —
# both /today and /breakout poll /api/v4/force_run_all_status to render it.
import threading as _threading
# RLock (reentrant) — _serve_force_run_all holds the lock and then calls
# _fr_snapshot which acquires it again. Plain Lock would deadlock the request.
_FR_LOCK = _threading.RLock()
_FR_STATE = {
    'status': 'idle',              # 'idle' | 'running' | 'done' | 'error'
    'started_at':  None,           # epoch sec
    'finished_at': None,           # epoch sec
    'current_idx': 0,              # 0-based, which collector is running
    'current_collector': None,
    'total': 0,
    'collectors': [],              # accumulating list of completed entries
    'last_error': None,
    'total_elapsed_sec': None,
    'summary': None,
    'run_id': 0,                   # increments each new run, lets clients detect a fresh run
}

# (script_filename, label, output_filename) — same list as the legacy sync
# endpoint. Order matters: alpaca_news → news_llm_scorer (downstream consumer).
FR_COLLECTORS = [
    ('v4_flow_alerts_collector.py',     'flow_alerts',     'v4_flow_alerts.json'),
    ('v4_alpaca_news_collector.py',     'alpaca_news',     'v4_alpaca_news.json'),
    ('v4_news_llm_scorer.py',           'news_llm',        'v4_news_llm_scored.json'),
    ('v4_priority_signals_collector.py','priority_signals','v4_priority_signals_intraday.json'),
    ('v4_tape_pressure_collector.py',   'tape_pressure',   'v4_tape_pressure.json'),
    ('v4_regime_detector.py',           'regime',          'v4_regime.json'),
    ('v4_news_intraday_collector.py',   'news_intraday',   'v4_news_intraday.json'),
    # catalyst_alerter is a Discord dispatcher — it doesn't produce a JSON
    # data file; it writes only its dedup-memory state. Tracking that file
    # gives an accurate "ran" signal even when 0 alerts were sent.
    ('v4_catalyst_alerter.py',          'catalyst_alerter','v4_catalyst_alerter_state.json'),
]

def _force_run_worker(run_id):
    """Background-thread body. Runs each collector with FORCE_RUN=1 and updates
    _FR_STATE between each step so polling clients see live progress."""
    import subprocess as _sp, time as _time, os as _os
    env = {**_os.environ, 'FORCE_RUN': '1'}
    t0 = _time.time()
    try:
        for idx, (script, label, out_name) in enumerate(FR_COLLECTORS):
            with _FR_LOCK:
                # If a newer run started (shouldn't happen since the start path
                # blocks concurrent starts), bail out.
                if _FR_STATE['run_id'] != run_id: return
                _FR_STATE['current_idx'] = idx
                _FR_STATE['current_collector'] = label
            script_path = _os.path.join(BASE_DIR, script)
            out_path    = _os.path.join(BASE_DIR, out_name)
            mtime_before = _os.path.getmtime(out_path) if _os.path.exists(out_path) else 0
            entry = {'collector': label, 'script': script, 'output': out_name}
            ts = _time.time()
            try:
                proc = _sp.run(['/usr/bin/python3', '-u', script_path],
                               cwd=BASE_DIR, env=env,
                               capture_output=True, timeout=180)
                entry['returncode'] = proc.returncode
                entry['elapsed_sec'] = round(_time.time() - ts, 2)
                stderr = proc.stderr.decode('utf-8', errors='ignore').strip()
                if stderr:
                    entry['stderr_tail'] = stderr[-600:]
                mtime_after = _os.path.getmtime(out_path) if _os.path.exists(out_path) else 0
                entry['output_advanced'] = (mtime_after > mtime_before)
                if mtime_after:
                    entry['output_mtime'] = int(mtime_after)
            except _sp.TimeoutExpired:
                entry['error'] = 'timeout (>180s)'
                entry['elapsed_sec'] = round(_time.time() - ts, 2)
            except Exception as e:
                entry['error'] = str(e)
                entry['elapsed_sec'] = round(_time.time() - ts, 2)
            with _FR_LOCK:
                if _FR_STATE['run_id'] != run_id: return
                _FR_STATE['collectors'].append(entry)
        # Mark complete
        with _FR_LOCK:
            if _FR_STATE['run_id'] != run_id: return
            results = _FR_STATE['collectors']
            _FR_STATE['status'] = 'done'
            _FR_STATE['finished_at'] = _time.time()
            _FR_STATE['current_collector'] = None
            _FR_STATE['current_idx'] = len(results)
            _FR_STATE['total_elapsed_sec'] = round(_time.time() - t0, 2)
            _FR_STATE['summary'] = {
                'total':    len(results),
                'advanced': sum(1 for r in results if r.get('output_advanced')),
                'failed':   sum(1 for r in results if r.get('error') or r.get('returncode', 0) != 0),
            }
    except Exception as e:
        with _FR_LOCK:
            if _FR_STATE['run_id'] != run_id: return
            _FR_STATE['status'] = 'error'
            _FR_STATE['last_error'] = str(e)
            _FR_STATE['finished_at'] = _time.time()


class V4DashboardServer(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=DIRECTORY, **kwargs)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.send_header('Pragma', 'no-cache')
        self.send_header('Access-Control-Allow-Origin', '*')
        super().end_headers()

    def do_GET(self):
        # Strip any query string (e.g. ?_=Date.now() cache-busters) so exact
        # route matches still work. Handlers that need the query can read
        # self.path directly.
        path = self.path.split('?', 1)[0]

        # Default → redirect to /v2/
        if path == '/' or path == '':
            self.send_response(302)
            self.send_header('Location', '/v2/')
            self.end_headers()
            return

        # ===== v2 ENHANCED API endpoints =====
        # NOTE: /api/v4/signals serves v4_signals.json verbatim. That file is
        # written by v4_meta_engine.py (FVG/OB retest SMC engine), which is a
        # *separate* pipeline from the BCS/PCS/GAS/ICS conviction stack. The
        # v2 Confirmed page (filter: Status.startsWith('TRIGGER_')) is
        # intentionally reserved for meta_engine's TRIGGER_PULLBACK_CONFIRMED /
        # TRIGGER_BREAKOUT_CONFIRMED fires only. Do NOT merge conviction
        # triggers in here — that conflates two engines' outputs.
        # Force-Run-All status — polled by /today and /breakout to render the
        # sticky progress bar. Returns the live _FR_STATE snapshot. Cheap
        # (~1ms), so clients can poll every 1.5s while a run is in progress.
        if path in ('/api/v4/force_run_all_status', '/v2/api/v4/force_run_all_status'):
            self._serve_force_run_all_status(); return

        if path in ('/api/v4/signals', '/v2/api/v4/signals'):
            self._serve_json(SIGNALS_FILE,
                "v4_signals.json not found yet — v4_meta_engine.py hasn't run"); return
        if path in ('/api/v4/breakouts', '/v2/api/v4/breakouts'):
            self._serve_json(BREAKOUTS_FILE, "preposition watchlist not found yet"); return
        if path in ('/api/v4/positions', '/v2/api/v4/positions'):
            self._serve_positions(POSITIONS_FILE); return
        if path in ('/api/v4/gaps', '/v2/api/v4/gaps'):
            self._serve_json(GAPS_FILE, "v4_gap_watchlist.json not found yet"); return
        if path in ('/api/v4/missed', '/v2/api/v4/missed', '/v1/api/v4/missed'):
            self._serve_json(MISSED_TRADES_FILE,
                "v4_missed_trades.json not found yet — run v4_missed_trades_tracker.py"); return
        if path in ('/api/v4/live_account', '/v2/api/v4/live_account', '/v1/api/v4/live_account'):
            self._serve_live_alpaca(); return
        if path in ('/api/v4/preposition_history', '/v2/api/v4/preposition_history', '/v1/api/v4/preposition_history'):
            self._serve_preposition_history(); return
        if path in ('/api/v4/accumulation', '/v2/api/v4/accumulation', '/v1/api/v4/accumulation'):
            self._serve_json(ACCUMULATION_FILE,
                "v4_accumulation_signals.json not found yet — preposition scanner hasn't run with B1 yet"); return
        if path in ('/api/v4/accumulation_success', '/v2/api/v4/accumulation_success', '/v1/api/v4/accumulation_success'):
            self._serve_json(ACCUMULATION_SUCCESS_FILE,
                "v4_accumulation_success.json not found yet — run v4_accumulation_tracker.py"); return
        if path in ('/api/v4/breakout_candidates', '/v2/api/v4/breakout_candidates', '/v1/api/v4/breakout_candidates'):
            self._serve_breakout_candidates(); return
        if path in ('/api/v4/footprint_signals', '/v2/api/v4/footprint_signals', '/v1/api/v4/footprint_signals'):
            self._serve_footprint_signals(); return
        if path in ('/api/v4/chatgpt_mirror', '/v2/api/v4/chatgpt_mirror', '/v1/api/v4/chatgpt_mirror'):
            self._serve_chatgpt_mirror(); return
        if path in ('/api/v4/0dte_signals', '/v2/api/v4/0dte_signals', '/v1/api/v4/0dte_signals'):
            self._serve_0dte_signals(); return

        # ===== NEW scanners (additive — LSFC scalp + pre-break) =====
        if path in ('/api/v4/lsfc_signals', '/v2/api/v4/lsfc_signals', '/v1/api/v4/lsfc_signals'):
            self._serve_lsfc_signals(); return
        if path in ('/api/v4/prebreak_signals', '/v2/api/v4/prebreak_signals', '/v1/api/v4/prebreak_signals'):
            self._serve_prebreak_signals(); return
        if path in ('/api/v4/scanner_status', '/v2/api/v4/scanner_status', '/v1/api/v4/scanner_status'):
            self._serve_scanner_status(); return
        if path in ('/api/v4/conviction', '/v2/api/v4/conviction', '/v1/api/v4/conviction'):
            self._serve_conviction(); return
        if path in ('/api/v4/breakout_outcomes', '/v2/api/v4/breakout_outcomes', '/v1/api/v4/breakout_outcomes'):
            self._serve_breakout_outcomes(); return
        # V7 staging scanner output — empirically-weighted pre-break ranks
        if path in ('/api/v4/staging', '/v2/api/v4/staging'):
            self._serve_staging_with_log(); return
        # User's curated 156-ticker watchlist split into mega/mid/small buckets.
        # Source: data/picks_77.json (built from user-pasted list, mcap-bucketed).
        # Used by /mega, /mid, /small pages to scope patrol/conviction views.
        if path in ('/api/v4/picks_77', '/v2/api/v4/picks_77'):
            self._serve_json(os.path.join(BASE_DIR, 'data', 'picks_77.json'),
                "data/picks_77.json not found yet — run the bucket-builder."); return
        # Recent buy-click + view log entries (read-only tail of the JSONL log)
        if path in ('/api/v4/prebreak_log', '/v2/api/v4/prebreak_log'):
            self._serve_prebreak_log_tail(); return
        # Today-breakout scanner output — v2 score + GATE_D
        if path in ('/api/v4/today_breakouts', '/v2/api/v4/today_breakouts'):
            self._serve_json(os.path.join(BASE_DIR, 'v4_today_breakouts.json'),
                "v4_today_breakouts.json not found yet — run v4_today_breakout_scanner.py"); return
        # Continuation scanner output — gate-only (D1 breakout AND D2 holds)
        if path in ('/api/v4/continuation_signals', '/v2/api/v4/continuation_signals'):
            self._serve_json(os.path.join(BASE_DIR, 'v4_continuation_signals.json'),
                "v4_continuation_signals.json not found yet — run v4_continuation_scanner.py"); return
        # Morning check — live Alpaca quotes for yesterday's breakouts
        if path in ('/api/v4/morning_signals', '/v2/api/v4/morning_signals'):
            self._serve_json(os.path.join(BASE_DIR, 'v4_morning_signals.json'),
                "v4_morning_signals.json not found yet — run v4_morning_check.py"); return
        # Health check — surfaces data/health_check.json so the dashboard can
        # show a live status pill without scraping disk. Also exposes the
        # watchdog's cross-run drift findings so the user can verify directly.
        if path in ('/api/v4/health', '/v2/api/v4/health'):
            try:
                p = os.path.join(BASE_DIR, 'data', 'health_check.json')
                if os.path.exists(p):
                    import json as _json
                    with open(p) as f:
                        body = f.read()
                    self.send_response(200)
                    self.send_header("Content-type", "application/json")
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body.encode())
                else:
                    self.send_response(404)
                    self.send_header("Content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(b'{"error":"health_check.json not yet generated"}')
            except Exception as e:
                self.send_response(500); self.send_header("Content-type", "application/json"); self.end_headers()
                self.wfile.write(f'{{"error":"{e}"}}'.encode())
            return
        # Unified lifecycle — merges breakout + morning + multi-day continuation per ticker
        if path in ('/api/v4/lifecycle', '/v2/api/v4/lifecycle'):
            self._serve_lifecycle(); return
        # Lifecycle revalidate — re-runs continuation scanner + morning check, then merges
        if path in ('/api/v4/lifecycle_refresh', '/v2/api/v4/lifecycle_refresh'):
            self._serve_lifecycle_refresh(); return
        # /today — single trade card with conviction score + sizing for $200/day target
        if path in ('/api/v4/today', '/v2/api/v4/today'):
            self._serve_today(); return
        if path in ('/api/v4/today_refresh', '/v2/api/v4/today_refresh'):
            self._serve_today_refresh(); return
        # /analyze — backtest of every signal we've shown + UW research findings
        if path in ('/api/v4/analyze', '/v2/api/v4/analyze'):
            self._serve_json(os.path.join(BASE_DIR, 'v4_signal_analysis.json'),
                "v4_signal_analysis.json not found yet — run v4_signal_analyzer.py"); return
        if path in ('/api/v4/analyze_refresh', '/v2/api/v4/analyze_refresh'):
            self._serve_scanner_revalidate('v4_signal_analyzer.py', 'v4_signal_analysis.json'); return
        # /scalp_backtest — every signal we've EVER shown, scored as a SCALP (1-2d window)
        if path in ('/api/v4/scalp_backtest', '/v2/api/v4/scalp_backtest'):
            self._serve_json(os.path.join(BASE_DIR, 'v4_scalp_backtest.json'),
                "v4_scalp_backtest.json not found yet — run v4_scalp_backtest.py"); return
        if path in ('/api/v4/scalp_backtest_refresh', '/v2/api/v4/scalp_backtest_refresh'):
            self._serve_scanner_revalidate('v4_scalp_backtest.py', 'v4_scalp_backtest.json'); return
        # /put_backtest — every PUT signal we've shown, validated with downside framing
        if path in ('/api/v4/put_backtest', '/v2/api/v4/put_backtest'):
            self._serve_json(os.path.join(BASE_DIR, 'v4_put_backtest.json'),
                "v4_put_backtest.json not found yet — run v4_put_backtest.py"); return
        if path in ('/api/v4/put_backtest_refresh', '/v2/api/v4/put_backtest_refresh'):
            self._serve_scanner_revalidate('v4_put_backtest.py', 'v4_put_backtest.json'); return
        # /catalyst — news feed (left pane) — Alpaca/Benzinga news for priority tickers
        if path in ('/api/v4/catalyst', '/v2/api/v4/catalyst'):
            self._serve_json(os.path.join(BASE_DIR, 'v4_alpaca_news.json'),
                "v4_alpaca_news.json not found yet — run v4_alpaca_news_collector.py"); return
        # /api/v4/flow_patrol — TRIMMED list view (drops daily_trajectory + buffer_60min
        # from each ticker for bulk fetches to keep response < 200KB instead of 3.6MB).
        # Per-ticker endpoint /api/v4/flow_patrol/{T} returns full data.
        if path in ('/api/v4/flow_patrol', '/v2/api/v4/flow_patrol', '/v1/api/v4/flow_patrol'):
            self._serve_flow_patrol_list(); return
        # /api/v4/signal_fires — per-ticker first-fired timestamps for conviction/staging/breakout
        if path in ('/api/v4/signal_fires', '/v2/api/v4/signal_fires', '/v1/api/v4/signal_fires'):
            self._serve_json(os.path.join(BASE_DIR, 'data', 'signal_fire_log.json'),
                "signal_fire_log.json not found yet — start v4_signal_fire_tracker.py"); return
        # /api/v4/position_exits — patrol-gated exit alerts on open positions
        if path in ('/api/v4/position_exits', '/v2/api/v4/position_exits', '/v1/api/v4/position_exits'):
            self._serve_json(os.path.join(BASE_DIR, 'data', 'position_exit_alerts.json'),
                "position_exit_alerts.json not found yet — start v4_position_patrol.py"); return
        # /api/v4/intraday_tradeables — NEW tradeable stocks every 10 min from live WS aggregates
        if path in ('/api/v4/intraday_tradeables', '/v2/api/v4/intraday_tradeables', '/v1/api/v4/intraday_tradeables'):
            self._serve_json(os.path.join(BASE_DIR, 'data', 'intraday_tradeables.json'),
                "intraday_tradeables.json not found yet — start v4_intraday_flow_scanner.py"); return
        # /api/v4/flow_patrol/<TICKER> — per-ticker score, buffer, alerts
        if path.startswith('/api/v4/flow_patrol/') or path.startswith('/v2/api/v4/flow_patrol/') or path.startswith('/v1/api/v4/flow_patrol/'):
            ticker = path.rstrip('/').split('/')[-1].upper()
            self._serve_flow_patrol_ticker(ticker); return
        # /api/v4/ticker_flow/<TICKER> — per-ticker flow snapshot (right pane)
        if path.startswith('/api/v4/ticker_flow/') or path.startswith('/v2/api/v4/ticker_flow/'):
            ticker = path.rstrip('/').split('/')[-1].upper()
            self._serve_ticker_flow(ticker); return
        # On-demand rescan (triggered by the Conviction page's "Rescan" button)
        if path in ('/api/v4/rescan', '/api/v4/rescan_status'):
            self._serve_rescan(path); return
        # Standalone shortcuts — /short-term (new unified feed) + /conviction + /live
        if path == '/short-term' or path == '/short-term/' or path == '/shortterm':
            self.path = '/short_term.html'
            return super().do_GET()
        if path == '/prebreakout' or path == '/prebreakout/' or path == '/prebreak':
            self.path = '/prebreakout.html'
            return super().do_GET()
        if path == '/breakout' or path == '/breakout/':
            self.path = '/breakout.html'
            return super().do_GET()
        if path == '/continuation' or path == '/continuation/':
            self.path = '/continuation.html'
            return super().do_GET()
        if path == '/morning' or path == '/morning/':
            self.path = '/morning.html'
            return super().do_GET()
        if path == '/today' or path == '/today/':
            self.path = '/today.html'
            return super().do_GET()
        if path == '/analyze' or path == '/analyze/':
            self.path = '/analyze.html'
            return super().do_GET()
        if path in ('/flow_patrol', '/flow_patrol/', '/patrol', '/patrol/'):
            self.path = '/flow_patrol.html'
            return super().do_GET()
        if path in ('/scalp_backtest', '/scalp_backtest/', '/backtest', '/backtest/'):
            self.path = '/scalp_backtest.html'
            return super().do_GET()
        # /scalp_all_signals — every individual scalp signal across all 23 strategies
        # (built by engine_scalp/scalp_signals_dashboard.py — self-contained, embeds JSON)
        if path in ('/scalp_all_signals', '/scalp_all_signals/', '/scalp_signals', '/scalp_signals/',
                    '/all_signals', '/all_signals/'):
            self.path = '/scalp_all_signals.html'
            return super().do_GET()
        if path in ('/put_backtest', '/put_backtest/', '/puts', '/puts/'):
            self.path = '/put_backtest.html'
            return super().do_GET()
        if path in ('/catalyst', '/catalyst/', '/catalysts', '/catalysts/'):
            self.path = '/catalyst.html'
            return super().do_GET()
        if path == '/live' or path == '/live/':
            self.path = '/live.html'
            return super().do_GET()
        if path == '/scanners' or path == '/scanners/':
            self.path = '/scanners.html'
            return super().do_GET()
        if path == '/conviction' or path == '/conviction/':
            self.path = '/conviction.html'
            return super().do_GET()
        if path == '/breakouts' or path == '/breakouts/':
            self.path = '/breakouts.html'
            return super().do_GET()

        # ===== v1 BASELINE API endpoints =====
        if path == '/v1/api/v4/signals':
            self._serve_json(SIGNALS_BASELINE_FILE,
                "v4_signals_baseline.json not found yet — baseline engine hasn't run"); return
        if path == '/v1/api/v4/breakouts':
            # No baseline preposition fork yet — share the same data
            # (preposition_scanner has minimal Phase 1-5 changes; squeeze + sector_etfs
            # are additive, doesn't fundamentally differ from original)
            self._serve_json(BREAKOUTS_FILE, "preposition watchlist not found yet"); return
        if path == '/v1/api/v4/positions':
            # Baseline doesn't trade — return empty positions list
            self._send_empty({"positions": [], "_note": "baseline runs in shadow — no trades executed"})
            return
        if path == '/v1/api/v4/gaps':
            # Gap scanner is shared (no baseline fork)
            self._serve_json(GAPS_FILE, "v4_gap_watchlist.json not found yet"); return

        # ===== Static dashboard (HTML/CSS/JS) — serve from /v1/ or /v2/ prefix =====
        if path.startswith('/v1/') or path.startswith('/v2/'):
            # Strip the /v{n}/ prefix and let the static handler serve from there
            self.path = path[3:] or '/'
            return super().do_GET()

        super().do_GET()

    def _serve_positions(self, path):
        import json as _json
        positions = []
        if os.path.exists(path):
            try:
                with open(path) as f:
                    positions = _json.load(f)
            except Exception:
                positions = []
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps({"positions": positions}).encode())

    def _send_empty(self, payload):
        import json as _json
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps(payload).encode())

    def _serve_flow_patrol_list(self):
        """Trimmed list view — drops daily_trajectory + buffer_60min per ticker.
        Reduces payload from ~3.6MB to ~140KB. Per-ticker endpoint still has full data."""
        import json as _json
        state_path = os.path.join(BASE_DIR, 'data', 'flow_patrol_state.json')
        try:
            with open(state_path) as f:
                state = _json.load(f)
            # Trim heavy fields from each ticker
            trimmed_tickers = {}
            for t, td in (state.get('tickers') or {}).items():
                trimmed_tickers[t] = {k: v for k, v in td.items()
                                       if k not in ('daily_trajectory', 'buffer_60min')}
            out = {
                'ts_utc': state.get('ts_utc'),
                'session_date': state.get('session_date'),
                'mode': state.get('mode'),
                'baseline_age_h': state.get('baseline_age_h'),
                'baseline_stale': state.get('baseline_stale'),
                'tickers': trimmed_tickers,
            }
        except FileNotFoundError:
            out = {'tickers': {}, 'message': 'flow_patrol_state.json missing — start v4_flow_patrol.py'}
        except Exception as e:
            out = {'tickers': {}, 'message': f'state read err: {type(e).__name__}: {e}'}
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(_json.dumps(out).encode())

    def _serve_flow_patrol_ticker(self, ticker: str):
        """Per-ticker flow patrol slice — score, verdict, rolling buffer, alerts.
        Reads data/flow_patrol_state.json and extracts the requested ticker.
        Returns 404-ish JSON if ticker not tracked or state file missing."""
        import json as _json
        state_path = os.path.join(BASE_DIR, 'data', 'flow_patrol_state.json')
        out = {'ticker': ticker, 'tracked': False, 'message': 'patrol not running yet'}
        try:
            with open(state_path) as f:
                state = _json.load(f)
            tdata = (state.get('tickers') or {}).get(ticker)
            if tdata:
                out = {
                    'ticker': ticker,
                    'tracked': True,
                    'ts_utc': state.get('ts_utc'),
                    'session_date': state.get('session_date'),
                    **tdata,
                }
            else:
                out['message'] = f'{ticker} not in patrol universe'
        except FileNotFoundError:
            out['message'] = 'flow_patrol_state.json missing — start v4_flow_patrol.py'
        except Exception as e:
            out['message'] = f'state read error: {type(e).__name__}: {e}'
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps(out).encode())

    def _serve_preposition_history(self):
        """Returns triggered preposition setups across today + archive.
        Format: {triggered_today: [...], triggered_past: [...]}
        Each record: {ticker, direction, score, trigger_level, triggered_at_utc, date, tier}"""
        import json as _json, glob
        out = {'triggered_today': [], 'triggered_past': []}

        # Today's watchlist
        today_path = os.path.join(BASE_DIR, 'v4_preposition_watchlist.json')
        if os.path.exists(today_path):
            try:
                with open(today_path) as f: data = _json.load(f)
                for c in data.get('candidates', []):
                    if c.get('triggered_at_utc'):
                        out['triggered_today'].append({
                            'ticker': c.get('ticker'),
                            'direction': c.get('direction'),
                            'score': c.get('score'),
                            'tier': c.get('tier'),
                            'trigger_level': (c.get('key_levels') or {}).get('trigger_above'),
                            'triggered_at_utc': c.get('triggered_at_utc'),
                            'narrative': c.get('narrative', '')[:200],
                            'date': (data.get('metadata', {}).get('scan_completed_utc') or '')[:10],
                        })
            except Exception: pass

        # Archived days
        archive_dir = os.path.join(BASE_DIR, 'v4_preposition_archive')
        if os.path.isdir(archive_dir):
            for p in sorted(glob.glob(os.path.join(archive_dir, '*.json')), reverse=True):
                try:
                    with open(p) as f: data = _json.load(f)
                    scan_date = (data.get('metadata', {}).get('scan_completed_utc') or '')[:10]
                    for c in data.get('candidates', []):
                        if c.get('triggered_at_utc'):
                            out['triggered_past'].append({
                                'ticker': c.get('ticker'),
                                'direction': c.get('direction'),
                                'score': c.get('score'),
                                'tier': c.get('tier'),
                                'trigger_level': (c.get('key_levels') or {}).get('trigger_above'),
                                'triggered_at_utc': c.get('triggered_at_utc'),
                                'narrative': c.get('narrative', '')[:200],
                                'date': scan_date,
                            })
                except Exception: continue

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps(out).encode())

    def _serve_footprint_signals(self):
        """Combines today's footprint candidates (from v4_footprint_history.jsonl)
        with the backtest snapshot (v4_footprint_backtest.json).

        Response shape:
          {
            generated_at_utc, universe,
            today: {date, regime, spy_20d, candidates: [...]},
            history: {total_records, unique_dates, unique_tickers, regime_distribution,
                      fwd_5d_filled, oldest_date, newest_date, days_collected},
            backtest: <contents of v4_footprint_backtest.json>,
            note: "..."
          }
        """
        import json as _json
        from collections import defaultdict as _dd
        out = {
            'generated_at_utc': datetime.now(timezone.utc).isoformat(timespec='seconds'),
            'universe': ['AAPL','ABBV','AMAT','AMD','AMZN','AVGO','BAC','COST','CVX','GOOGL',
                         'HD','INTC','JNJ','JPM','LLY','MA','META','MRK','MSFT','MU',
                         'NFLX','NVDA','ORCL','QCOM','TSLA','TSM','UNH','V','WMT','XOM'],
            'note': ('⚠️ Monitoring dashboard. Footprint edge NOT statistically '
                     'significant in current 30-day mega-cap bull regime. Collecting '
                     'data forward; re-evaluate after 60 days.'),
        }

        # --- Load history JSONL ---
        records = []
        if os.path.exists(FOOTPRINT_HISTORY_FILE):
            with open(FOOTPRINT_HISTORY_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try: records.append(_json.loads(line))
                    except Exception: continue

        # --- Today: most-recent record per ticker ---
        latest = {}
        for r in records:
            t = r.get('ticker')
            if t not in latest or r.get('date','') > latest[t].get('date',''):
                latest[t] = r
        today_date = max((r.get('date','') for r in latest.values()), default='')
        today_records = [r for r in latest.values() if r.get('date') == today_date]
        today_records.sort(key=lambda r: -(r.get('scores',{}).get('footprint',0)))
        market = next(iter(today_records), {}).get('market', {}) if today_records else {}
        out['today'] = {
            'date': today_date or None,
            'regime': market.get('regime'),
            'spy_20d': market.get('spy_20d'),
            'spy_5d_vol': market.get('vol_5d'),
            'candidates': today_records,
        }

        # --- History stats ---
        if records:
            dates = {r.get('date') for r in records if r.get('date')}
            tickers = {r.get('ticker') for r in records if r.get('ticker')}
            regime_dist = _dd(int)
            fwd_filled = 0
            for r in records:
                reg = (r.get('market') or {}).get('regime','?')
                regime_dist[reg] += 1
                if r.get('fwd_5d_pct') is not None: fwd_filled += 1
            out['history'] = {
                'total_records': len(records),
                'unique_dates': len(dates),
                'unique_tickers': len(tickers),
                'oldest_date': min(dates) if dates else None,
                'newest_date': max(dates) if dates else None,
                'days_collected': len(dates),
                'days_until_60d_milestone': max(0, 60 - len(dates)),
                'regime_distribution': dict(regime_dist),
                'fwd_5d_filled': fwd_filled,
                'fwd_5d_pending': len(records) - fwd_filled,
            }
        else:
            out['history'] = {'total_records': 0, 'note': 'Awaiting first EOD snapshot.'}

        # --- Backtest snapshot (precomputed on the full 30-ticker dataset) ---
        if os.path.exists(FOOTPRINT_BACKTEST_FILE):
            try:
                with open(FOOTPRINT_BACKTEST_FILE) as f:
                    out['backtest'] = _json.load(f)
            except Exception as e:
                out['backtest'] = {'error': str(e)}
        else:
            out['backtest'] = {'note': 'Backtest snapshot not generated yet.'}

        # --- Paper-trade ledger (Pattern A + B) ---
        trades = []
        if os.path.exists(PATTERN_TRADES_FILE):
            with open(PATTERN_TRADES_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try: trades.append(_json.loads(line))
                    except Exception: continue
        # Sort newest entry first
        trades.sort(key=lambda t: t.get('entry_date',''), reverse=True)
        open_trades = [t for t in trades if t.get('status')=='OPEN']
        closed_trades = [t for t in trades if t.get('status')=='CLOSED']

        def _pattern_stats(rs):
            if not rs: return {'n':0}
            pnls = [r['pnl_pct'] for r in rs if r.get('pnl_pct') is not None]
            if not pnls: return {'n': len(rs)}
            wins = sum(1 for x in pnls if x >= 2)
            big  = sum(1 for x in pnls if x >= 5)
            loss = sum(1 for x in pnls if x <  0)
            target = sum(1 for t in rs if t.get('exit_reason')=='PROFIT_TARGET')
            stop   = sum(1 for t in rs if t.get('exit_reason')=='STOP_LOSS')
            time_  = sum(1 for t in rs if t.get('exit_reason')=='TIME_STOP')
            return {
                'n': len(pnls),
                'avg_pnl': round(sum(pnls)/len(pnls), 2),
                'median_pnl': round(sorted(pnls)[len(pnls)//2], 2),
                'win_rate': round(wins/len(pnls), 3),
                'big_wins': big, 'losses': loss,
                'profit_target_exits': target, 'stop_loss_exits': stop, 'time_stop_exits': time_,
            }

        pattern_defs = [
            ('A-CALL', 'HF Exit Into Strength',
             'day_pct ≥ +3% AND dp_net_buy_size ≤ -0.3M shares', 'LONG',
             'p=0.0088 highly significant'),
            ('B-CALL', 'Retail Contrarian (bullish)',
             'day_pct ≥ +3% AND net_call_premium ≤ -$15M', 'LONG',
             'small sample (100% win N=3)'),
            ('A-PUT',  'HF Sell Confirms Drop',
             'day_pct ≤ -3% AND dp_net_buy_size ≤ -0.3M shares', 'SHORT',
             'NOT validated — backtest shows loss in bull regime'),
            ('B-PUT',  'Put FOMO',
             'day_pct ≤ -3% AND net_put_premium ≥ +$15M', 'SHORT',
             'tiny sample (N=2 in backtest)'),
        ]
        patterns = {}
        for code, name, rule, side, note in pattern_defs:
            patterns[code] = {
                'name': name, 'rule': rule, 'side': side,
                'exit': '5d time-stop | ±8% profit target | ∓3% stop loss',
                'stat_significance': note,
                'skip_list': ['MU','TSLA'],
                'closed_stats': _pattern_stats([t for t in closed_trades if t.get('pattern')==code]),
                'open_trades':  [t for t in open_trades  if t.get('pattern')==code],
                'recent_closed':[t for t in closed_trades if t.get('pattern')==code][:20],
            }
        patterns['ledger'] = {
            'total_trades': len(trades),
            'total_open': len(open_trades),
            'total_closed': len(closed_trades),
        }
        out['patterns'] = patterns

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps(out).encode())

    def _serve_0dte_signals(self):
        """Returns the 0DTE flow alert feed (newest first, last 24h)."""
        import json as _json
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        signals = []
        if os.path.exists(ZERODTE_SIGNALS_FILE):
            with open(ZERODTE_SIGNALS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try: signals.append(_json.loads(line))
                    except: continue
        # Keep last 24h only for display
        cutoff = (_dt.now(_tz.utc) - _td(hours=24)).isoformat()
        recent = [s for s in signals if (s.get('caught_at_utc') or '') >= cutoff]
        recent.sort(key=lambda s: s.get('caught_at_utc',''), reverse=True)
        out = {
            'generated_at_utc': _dt.now(_tz.utc).isoformat(timespec='seconds'),
            'count_24h': len(recent),
            'count_total': len(signals),
            'signals': recent[:100],
            'note': '0DTE bullish flow alerts — monitor only, manual execution. '
                    'Scanner runs every 90s during 3:00-4:00 PM ET.',
        }
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps(out).encode())

    def _serve_lsfc_signals(self):
        """LSFC scalp signals (newest first, last 24h)."""
        import json as _json
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        signals = []
        if os.path.exists(LSFC_SIGNALS_FILE):
            with open(LSFC_SIGNALS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try: signals.append(_json.loads(line))
                    except: continue
        cutoff = (_dt.now(_tz.utc) - _td(hours=24)).isoformat()
        recent = [s for s in signals if (s.get('caught_at_utc') or '') >= cutoff]
        recent.sort(key=lambda s: s.get('caught_at_utc',''), reverse=True)
        out = {
            'generated_at_utc': _dt.now(_tz.utc).isoformat(timespec='seconds'),
            'count_24h':   len(recent),
            'count_total': len(signals),
            'signals': recent[:100],
            'note': 'LSFC scalp — retest/sweep-reject/vwap setups during OD + PH only.',
        }
        self.send_response(200); self.send_header("Content-type", "application/json"); self.end_headers()
        self.wfile.write(_json.dumps(out).encode())

    def _serve_prebreak_signals(self):
        """Pre-break anticipation signals (newest first, last 24h)."""
        import json as _json
        from datetime import datetime as _dt, timezone as _tz, timedelta as _td
        signals = []
        if os.path.exists(PREBREAK_SIGNALS_FILE):
            with open(PREBREAK_SIGNALS_FILE) as f:
                for line in f:
                    line = line.strip()
                    if not line: continue
                    try: signals.append(_json.loads(line))
                    except: continue
        cutoff = (_dt.now(_tz.utc) - _td(hours=24)).isoformat()
        recent = [s for s in signals if (s.get('caught_at_utc') or '') >= cutoff]
        recent.sort(key=lambda s: s.get('caught_at_utc',''), reverse=True)
        out = {
            'generated_at_utc': _dt.now(_tz.utc).isoformat(timespec='seconds'),
            'count_24h':   len(recent),
            'count_total': len(signals),
            'signals': recent[:100],
            'note': 'Pre-break — coiling + pressure + fresh flow, fires BEFORE the break.',
        }
        self.send_response(200); self.send_header("Content-type", "application/json"); self.end_headers()
        self.wfile.write(_json.dumps(out).encode())

    def _serve_conviction(self):
        """Run the bucket-classified rules engine over today's universe and return
        ranked per-ticker confidence scores. Uses snapshot file if available so
        we don't hammer UW.

        STALE-WHILE-REVALIDATE CACHE: conviction takes ~12-14s to build (327 tickers).
        Pinggy proxy times out at ~10s, so first cold call → 502. Strategy:
          1. If cache is FRESH (< 15s old) → serve cached, return.
          2. If cache is STALE (15s-5min) → serve cached IMMEDIATELY (X-Conv-Cache: STALE),
             then kick off background regeneration so the next call sees fresh data.
          3. If cache is COLD (no body or > 5 min) → block and build (one-time cold start).
        Result: zero pinggy 502s in steady state — the browser always gets a sub-50ms
        response, possibly up to 15s stale, and the cache is being refilled in parallel.
        """
        import time as _time, threading as _thr
        cls = self.__class__
        if not hasattr(cls, '_conv_cache'):
            cls._conv_cache = {'body': None, 'fetched_at': 0.0, 'rebuilding': False}
        _FRESH_TTL = 15.0
        _MAX_STALE = 300.0   # serve stale up to 5 min while a rebuild is in flight
        cache = cls._conv_cache
        now = _time.time()
        age = now - cache['fetched_at']
        if cache['body']:
            if age < _FRESH_TTL:
                # Fresh — just serve it
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Conv-Cache", f"HIT age={age:.1f}s")
                self.end_headers()
                self.wfile.write(cache['body'])
                return
            elif age < _MAX_STALE:
                # Stale — serve cached AND kick off async rebuild (if not already)
                if not cache.get('rebuilding'):
                    cache['rebuilding'] = True
                    def _bg_rebuild():
                        try:
                            self._build_conviction_into_cache()
                        finally:
                            cls._conv_cache['rebuilding'] = False
                    _thr.Thread(target=_bg_rebuild, daemon=True).start()
                self.send_response(200)
                self.send_header("Content-type", "application/json")
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Conv-Cache", f"STALE age={age:.1f}s rebuilding")
                self.end_headers()
                self.wfile.write(cache['body'])
                return
        # No cache (or > 5 min stale) — must block and build
        return self._serve_conviction_fresh()

    def _build_conviction_into_cache(self):
        """Run conviction generation but write to cache instead of HTTP response.
        Used for stale-while-revalidate background rebuilds."""
        import io as _io
        # Replace wfile with a buffer so _serve_conviction_fresh's writes go into a bytes blob
        class _NullWFile:
            def __init__(self): self._chunks = []
            def write(self, b): self._chunks.append(b)
            def flush(self): pass
        class _NullSocket:
            def sendall(self, b): pass
        orig_wfile = self.wfile
        orig_send_response = self.send_response
        orig_send_header = self.send_header
        orig_end_headers = self.end_headers
        # Stub everything except wfile.write so the cache write at the bottom still fires
        self.wfile = _NullWFile()
        self.send_response = lambda *a, **k: None
        self.send_header   = lambda *a, **k: None
        self.end_headers   = lambda *a, **k: None
        try:
            self._serve_conviction_fresh()
        finally:
            self.wfile = orig_wfile
            self.send_response = orig_send_response
            self.send_header = orig_send_header
            self.end_headers = orig_end_headers

    def _serve_conviction_fresh(self):
        import json as _json, sys as _sys, os as _os
        from datetime import datetime as _dt, timezone as _tz
        # Import here (not at top) so import errors don't break the whole server
        try:
            _sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
            from v4_ticker_profile import build_profile
            from v4_rules_engine import evaluate
        except Exception as e:
            self.send_response(500); self.send_header("Content-type","application/json"); self.end_headers()
            self.wfile.write(_json.dumps({'error': f'module import: {e}'}).encode()); return

        # Universe = UNION of today's fresh-but-in-progress .part + yesterday's finalized .json.
        # Per-ticker data comes from the freshest file via _load_snapshot (today's .part
        # preferred over yesterday's .json). This way fresh tickers show live data and
        # unprocessed ones fall back to yesterday's complete file.
        import glob as _glob, re as _re
        base_dir = _os.path.dirname(_os.path.abspath(__file__))
        all_files = _glob.glob(_os.path.join(base_dir, 'v4_snapshot_*.json')) + \
                    _glob.glob(_os.path.join(base_dir, 'v4_snapshot_*.json.part'))
        def _fkey(p):
            base = _os.path.basename(p)
            m = _re.search(r'(\d{4}-\d{2}-\d{2})', base)
            # FIX (2026-04-28): Within the same date, the in-progress .part
            # is FRESHER than the completed .json (which is written once at
            # premarket and never updated intraday). Original sort key did
            # the opposite — making the conviction page show 5-hr-old data
            # all session. Now: .part > .json for the same date, and any
            # today file > yesterday's files.
            return (m.group(1) if m else '0',
                     1 if base.endswith('.json.part') else 0)
        ranked = sorted(all_files, key=_fkey, reverse=True)
        path_to_use = ranked[0] if ranked else None
        tickers = set()
        snap_data = {}
        # Union ticker set from top 2 files (today + yesterday typically)
        for p in ranked[:2]:
            try:
                with open(p) as f: d = _json.load(f)
                for t, row in (d.get('data', {}) or {}).items():
                    tickers.add(t)
                    # Prefer FIRST-seen (ranked order = freshest first) — don't overwrite
                    if t not in snap_data: snap_data[t] = row
            except Exception: pass
        tickers = sorted(tickers)
        if not tickers:
            # Fallback: use today's spikers snapshot
            fb = _os.path.join('/tmp', 'today_spikers_snapshot.json')
            if _os.path.exists(fb):
                try:
                    with open(fb) as f: d = _json.load(f)
                    tickers = [s['ticker'] for s in d.get('spikers', [])]
                except: pass

        def _overlay_from_snapshot(t):
            """Pull last_price / prev_close / day_pct / dp_prem / dp_prints.

            Source priority:
              1. LIVE Alpaca SIP snapshot via v4_uw_ws.get_alpaca_snapshot
                 (real-time during premarket / RTH / AH — 60s cache).
              2. Snapshot file fallback (yesterday EOD blob) — only when
                 REST fallback fails (no creds, network error, etc.).

            This stops the dashboard from showing yesterday's close as
            today's price when the scalp2 WS bridge for Alpaca bars is
            disconnected.
            """
            blob = snap_data.get(t) or {}
            ov = {}
            # Try LIVE first (Alpaca SIP REST, 60s cache)
            live_snap = None
            try:
                from v4_uw_ws import get_alpaca_snapshot as _get_live_snap
                live_snap = _get_live_snap(t)
            except Exception:
                live_snap = None
            if live_snap and (live_snap.get('latestTrade') or live_snap.get('dailyBar')):
                snap = live_snap
            else:
                snap = blob.get('alpaca_snapshot') or {}
            db   = snap.get('dailyBar') or {}
            pdb  = snap.get('prevDailyBar') or {}
            lt   = snap.get('latestTrade') or {}
            # Prefer latestTrade.p (true intraday tick) over dailyBar.c
            last = lt.get('p') if lt.get('p') is not None else db.get('c')
            prev = pdb.get('c') or db.get('o')
            if last is not None: ov['last_price'] = last
            if prev is not None: ov['prev_close'] = prev
            if last is not None and prev:
                try: ov['day_pct'] = (float(last) - float(prev)) / float(prev) * 100.0
                except Exception: pass
            # Dark pool roll-up — sum today's prints
            dp = (blob.get('darkpool') or {}).get('data') or []
            if dp:
                try:
                    ov['dp_prem']   = sum(float(x.get('premium') or 0) for x in dp)
                    ov['dp_prints'] = len(dp)
                except Exception: pass
            return ov

        # Load conviction-trigger fire times (when each TRADE_NOW was first logged).
        # Written by v4_discord_alerter.py — lets UI show "fired 14 min ago" per card.
        # INVALIDATION: entries whose Status doesn't end in _CONFIRMED are ones a
        # downstream revalidator (GAS pre-market / ICS intraday) rejected — skip
        # them so the dashboard stops badging them as "fired today".
        trigger_fires = {}  # ticker -> {fired_at_utc, confidence_at_fire, invalidation}
        trig_path = _os.path.join(base_dir, 'v4_conviction_triggers.json')
        from datetime import datetime as _dt, timezone as _tz
        today_utc = _dt.now(_tz.utc).date().isoformat()
        if _os.path.exists(trig_path):
            try:
                with open(trig_path) as f: tdata = _json.load(f)
                for sig in tdata.get('signals', []):
                    tk = sig.get('Ticker')
                    if not tk: continue
                    status = sig.get('Status') or ''
                    if not status.endswith('_CONFIRMED'):
                        # Revalidator rejected this trigger — do not badge as fired.
                        continue
                    created = (sig.get('Exit_Protocol') or {}).get('Created_UTC')
                    if not created:
                        continue
                    # Only badge as "fired today" if Created_UTC actually IS today UTC.
                    # Otherwise yesterday's fires leak onto today's dashboard.
                    if created[:10] != today_utc:
                        continue
                    trigger_fires[tk] = {
                        'fired_at_utc':      created,
                        'fired_confidence':  sig.get('Confidence'),
                    }
            except Exception: pass

        # Bulk-warm the Alpaca SIP snapshot cache once for the entire universe
        # before the per-ticker conviction loop. This turns 327 × per-ticker
        # REST round-trips (~130s) into one bulk burst (~3s, 7 REST calls).
        # Falls through silently if creds/network unavailable — _overlay_from_snapshot
        # then uses the snapshot-file fallback path.
        try:
            from v4_uw_ws import prefetch_alpaca_snapshots as _prefetch_snaps, get_alpaca_snapshot_stats as _snap_stats
            _n_prefetched = _prefetch_snaps(tickers)
            print(f"[conviction] prefetched {_n_prefetched} live snapshots / {len(tickers)} requested; cache stats: {_snap_stats()}", flush=True)
        except Exception as _e:
            print(f"[conviction] prefetch err: {type(_e).__name__}: {_e}", flush=True)

        results = []
        # Cache prof per ticker so post-stealth-enrichment trade_idea pass + BCS/PCS
        # overlay can re-call _suggest_trade(prof, res) when direction flips.
        profs = {}
        for t in tickers:
            try:
                prof = build_profile(t)
                if not prof: continue
                # Merge snapshot-derived fields IN PLACE so downstream rules + pattern
                # detection see real day_pct / dp_prem / last_price.
                ov = _overlay_from_snapshot(t)
                # Live-price fields — from Alpaca SIP REST snapshot (60s cache,
                # see v4_uw_ws.prefetch_alpaca_snapshots). These ARE more
                # authoritative than build_profile's stale snapshot-file values,
                # so force-override even when prof already has a value.
                _LIVE_OVERRIDE = {'last_price', 'prev_close', 'day_pct'}
                for k, v in ov.items():
                    if k in _LIVE_OVERRIDE and v is not None:
                        prof[k] = v
                    elif not prof.get(k):
                        prof[k] = v
                profs[t] = prof
                res = evaluate(prof, 'BULLISH')
                # Sprint 2.7 — also evaluate BEARISH side, keep whichever scores higher
                # so the conviction list naturally surfaces both directions.
                res_bear = evaluate(prof, 'BEARISH')
                bull_score = res.get('score') or 0
                bear_score = res_bear.get('score') or 0
                # Pick the dominant direction (higher score wins; ties → bullish)
                if bear_score > bull_score:
                    res = res_bear
                # Always expose the OTHER direction's score for transparency
                res['bullish_score'] = bull_score
                res['bearish_score'] = bear_score
                # Tack on raw cumulative metrics for UI display
                res['day_pct']       = prof.get('day_pct')
                res['last_price']    = prof.get('last_price')
                res['call_vol']      = prof.get('call_vol')
                res['put_vol']       = prof.get('put_vol')
                res['avg_c_vol']     = prof.get('avg_c_vol')
                res['avg_p_vol']     = prof.get('avg_p_vol')
                res['cratio']        = prof.get('cratio')
                res['pratio']        = prof.get('pratio')
                res['call_prem']     = prof.get('call_prem')
                res['put_prem']      = prof.get('put_prem')
                res['net_call_prem'] = prof.get('net_call_prem')
                res['net_put_prem']  = prof.get('net_put_prem')
                res['dp_prem']       = prof.get('dp_prem')
                res['call_ask_share']= prof.get('call_ask_share')
                res['iv_rank']       = prof.get('iv_rank')
                # `fetched_at` is shown on card as "Scanned at" — reflect the
                # ACTUAL freshness (when this card was just recomputed) rather
                # than the snapshot's old per-ticker fetch time. Since 80% of
                # the score reads from live WS overlay, "scanned now" is true.
                res['fetched_at']    = datetime.now(timezone.utc).isoformat(timespec='seconds')
                res['snapshot_fetched_at'] = prof.get('fetched_at')    # original source ts
                res['pattern_type']  = _detect_pattern_type(prof)
                # Pre-earnings tier (Sprint 2026-04-25) — surfaced on card as badge
                ed = prof.get('earnings_days_out')
                res['days_until_next_report'] = ed
                if ed is None:
                    res['pre_earnings_tier'] = None
                elif 0 <= ed <= 2:
                    res['pre_earnings_tier'] = 'EARNINGS_IMMINENT'
                elif 3 <= ed <= 5:
                    res['pre_earnings_tier'] = 'PRE_EARNINGS_HIGH_IV'
                elif 6 <= ed <= 10:
                    res['pre_earnings_tier'] = 'PRE_EARNINGS_WATCH'
                else:
                    res['pre_earnings_tier'] = 'NORMAL'
                # NOTE: res['trade_idea'] is populated AFTER stealth enrichment
                # below, so _suggest_trade can read price_range_pct for the
                # volatility-aware EXHAUSTED threshold.
                # Fire time from v4_conviction_triggers.json (when alerter first logged it)
                fire = trigger_fires.get(t) or {}
                res['fired_at_utc']     = fire.get('fired_at_utc')
                res['fired_confidence'] = fire.get('fired_confidence')
                results.append(res)
            except Exception as ex:
                results.append({'ticker': t, 'error': str(ex), 'verdict':'SKIP', 'confidence':0, 'cap_bucket':'UNKNOWN'})

        # Enrich with stealth accumulation metrics (one batch load of snapshots)
        try:
            from v4_stealth_accumulation import get_all_stealth
            stealth_all = get_all_stealth()
        except Exception:
            stealth_all = {}
        # Stealth DISTRIBUTION mirror (multi-day institutional unloading)
        try:
            from v4_stealth_distribution import get_all_stealth_dist
            stealth_dist_all = get_all_stealth_dist()
        except Exception:
            stealth_dist_all = {}
        for r in results:
            tk = r.get('ticker') if isinstance(r, dict) else None
            s = stealth_all.get(tk)
            sd = stealth_dist_all.get(tk)
            if s:
                r['stealth_score']   = s['stealth_score']
                r['is_stealth']      = s['is_stealth']
                r['stealth_days']    = s['days_tracked']
                r['stealth_reasons'] = s['stealth_reasons']
                r['cum_dp_prem']     = s['cum_dp_prem']
                r['cum_net_call']    = s['cum_net_call_prem']
                r['cum_net_put']     = s['cum_net_put_prem']
                r['price_range_pct'] = s['price_range_pct']
                r['total_move_pct']  = s['total_move_pct']
            else:
                r['stealth_score']   = 0
                r['is_stealth']      = False
                r['stealth_days']    = 0
            if sd:
                r['stealth_dist_score']   = sd['stealth_dist_score']
                r['is_stealth_dist']      = sd['is_stealth_dist']
                r['stealth_dist_reasons'] = sd['stealth_dist_reasons']
            else:
                r['stealth_dist_score']   = 0
                r['is_stealth_dist']      = False

        # ============================================================
        # Trade-idea pass (POST-stealth so price_range_pct is on `r`).
        # Also applies the EXHAUSTED → WATCH verdict downgrade: when
        # _suggest_trade returns verdict_override='WATCH' (aligned move
        # ≥ vol-adjusted exhaustion threshold), demote a 'BUY' verdict
        # to 'WATCH' and stamp the reason for transparency. BCS/PCS
        # overlay below explicitly re-asserts 'BUY' for fires, so this
        # downgrade never blocks a true BCS/PCS signal.
        # ============================================================
        # Phase 4 — load all active watch states ONCE up-front so the per-
        # ticker loop doesn't re-read the JSONL file 307 times. The
        # update_watch_state() calls below append directly to the log.
        try:
            from v4_watch_state import update_watch_state, render_badge, _load_all_states
            _watch_states_snapshot = _load_all_states()
        except Exception:
            update_watch_state = lambda *a, **k: None
            render_badge       = lambda *a, **k: {}
            _watch_states_snapshot = {}

        for r in results:
            if not isinstance(r, dict) or r.get('error'):
                continue
            tk = r.get('ticker')
            p  = profs.get(tk)
            if not p:
                continue
            ti = _suggest_trade(p, r)
            if not ti:
                continue
            r['trade_idea'] = ti
            if (ti.get('verdict_override') == 'WATCH'
                    and r.get('verdict') == 'BUY'):
                r['verdict'] = 'WATCH'
                r['verdict_override_reason'] = ti.get('entry_action')

            # ── Phase 4: watch-state lifecycle ────────────────────────
            # Drive the ARMED → HIT → EXPIRED transitions from the new
            # trade_idea + cached SMC zones. Survives server restart via
            # data/conviction_watch_state.jsonl. The dashboard reads
            # `trade_idea.watch_state` to upgrade ARMED cards to HIT
            # (TRADE_NOW) when price reaches the zone.
            try:
                cur_price = p.get('last_price') or p.get('prev_close')
                # Reuse the SMC zones the trade_idea already derived from
                # (lookup is LRU-cached so this is essentially free).
                from v4_smc_zones import smc_zones as _sz
                smc_for_watch = _sz(tk) if tk else None
                signal_for_watch = {
                    'urgency':            ti.get('entry_urgency'),
                    'direction':          r.get('direction') or ti.get('side'),
                    'pullback_price':     ti.get('pullback_price'),
                    'pullback_zone_low':  ti.get('pullback_zone_low'),
                    'pullback_zone_high': ti.get('pullback_zone_high'),
                    'pullback_zone_type': ti.get('pullback_zone_type'),
                    'choch_guard':        ti.get('choch_guard'),
                }
                # Normalize direction to BULLISH/BEARISH for state machine
                if signal_for_watch['direction'] == 'CALL':
                    signal_for_watch['direction'] = 'BULLISH'
                elif signal_for_watch['direction'] == 'PUT':
                    signal_for_watch['direction'] = 'BEARISH'

                new_state = update_watch_state(tk, cur_price, smc_for_watch, signal_for_watch)
                # Use the new state if a transition just happened, else the
                # snapshot we loaded once at the top of the loop.
                active = new_state or _watch_states_snapshot.get(tk)
                if active and active.get('state') in ('ARMED', 'HIT', 'TAKEN'):
                    ti['watch_state'] = active.get('state')
                    ti['watch_state_full'] = active
                    badge = render_badge(active)
                    if badge:
                        ti['watch_badge'] = badge
                    # HIT → upgrade urgency so dashboard renders TRADE_NOW
                    if active.get('state') == 'HIT':
                        ti['entry_urgency'] = 'HIT'
                else:
                    ti['watch_state'] = None
            except Exception as _ex_watch:
                # Never break the conviction page over a watch-state hiccup
                ti['watch_state'] = None

        # ============================================================
        # BCS/PCS overlay — surface BCS+PCS fires as TRADE_NOW cards.
        # For each fire from v4_bcs_pcs_scorer.decide_side() on the
        # latest v4_full_side_by_side.csv date:
        #   - if the ticker is already in `results`, overlay fields so
        #     action_of() flips it to TRADE_NOW (verdict=BUY, pattern
        #     neutralized to 'BCS_PCS', trade_idea.entry_urgency=ENTRY)
        #   - otherwise, append a synthetic result card
        # MEGA is already excluded inside decide_side().
        #
        # INVALIDATION BYPASS GUARD: if a ticker has a *_INVALIDATED row
        # today in v4_conviction_triggers.json (i.e. GAS pre-market or ICS
        # intraday rescorers rejected it), do NOT overlay it — else the
        # stale CSV-based fire would resurrect the trigger onscreen and
        # defeat the whole revalidation chain. Collect the set once.
        # ============================================================
        invalidated_today = set()
        if _os.path.exists(trig_path):
            try:
                with open(trig_path) as f: tdata2 = _json.load(f)
                for sig in tdata2.get('signals', []):
                    tk2 = sig.get('Ticker')
                    st2 = sig.get('Status') or ''
                    cr2 = (sig.get('Exit_Protocol') or {}).get('Created_UTC', '')[:10]
                    if tk2 and st2.endswith('_INVALIDATED') and cr2 == today_utc:
                        invalidated_today.add(tk2)
            except Exception: pass

        bcs_pcs_fires = _load_bcs_pcs_fires(base_dir)
        if bcs_pcs_fires:
            results_by_ticker = {r.get('ticker'): r for r in results
                                 if isinstance(r, dict)}
            for tk, fire in bcs_pcs_fires.items():
                if tk in invalidated_today:
                    # Revalidator culled this one today — don't resurrect it.
                    continue
                conf = int(round(fire['score'] * 100))
                existing = results_by_ticker.get(tk)
                if existing is not None:
                    # Overlay — force TRADE_NOW, preserve rules-engine metrics
                    existing['signal_source']   = f"BCS_PCS_{fire['side']}"
                    existing['bcs_score']       = fire['bcs']
                    existing['pcs_score']       = fire['pcs']
                    existing['bcs_pcs_bucket']  = fire['bucket']
                    existing['verdict']         = 'BUY'
                    existing['confidence']      = conf
                    existing['pattern_type']    = 'BCS_PCS'  # neutralize bearish veto
                    # Direction must match BCS/PCS fire side, else strike/summary
                    # are direction-blind and we get bugs like trade_idea.side=CALL
                    # next to summary='Buy ... PUT ...' (the ON bug).
                    new_direction = 'BULLISH' if fire['side'] == 'CALL' else 'BEARISH'
                    if existing.get('direction') != new_direction:
                        existing['direction'] = new_direction
                    # Regenerate trade_idea with the (possibly flipped) direction
                    # so summary, pullback_price, pullback_sign, exhaustion_threshold
                    # etc. are all internally consistent for the BCS/PCS side.
                    p_for_trade = profs.get(tk)
                    if p_for_trade is not None:
                        ti = _suggest_trade(p_for_trade, existing) or {}
                    else:
                        # Fallback (synthetic ticker entered via results_by_ticker
                        # somehow, no prof cached) — at minimum flip side and clear
                        # the stale summary so the UI doesn't show conflicting text.
                        ti = dict(existing.get('trade_idea') or {})
                        ti['side'] = fire['side']
                        ti.pop('summary', None)
                    ti['entry_urgency'] = 'ENTRY'   # BCS/PCS = entry signal by definition
                    ti['side']          = fire['side']
                    # BCS/PCS overrides EXHAUSTED downgrade — explicit verdict=BUY above
                    # is authoritative; clear override marker so UI doesn't double-warn.
                    ti.pop('verdict_override', None)
                    existing['trade_idea'] = ti
                    # Replace the rules-engine's bullish reasons with
                    # direction-appropriate BCS/PCS confirmations.
                    bp_reasons, bp_missing = _bcs_pcs_reasons(fire['row'], fire['side'])
                    hdr = (f"BCS={fire['bcs']:.2f} · PCS={fire['pcs']:.2f} · "
                           f"router→{fire['side']} {fire['score']:.2f}")
                    existing['reasons'] = [hdr] + bp_reasons
                    existing['missing'] = bp_missing
                else:
                    # Synthetic — ticker wasn't in rules-engine results
                    results.append(_build_synthetic_bcs_pcs_result(tk, fire))

        out = {
            'generated_at_utc': _dt.now(_tz.utc).isoformat(timespec='seconds'),
            'universe_size': len(results),
            'source_file':   path_to_use and _os.path.basename(path_to_use),
            'results':       results,
        }
        # Persist to TWO files: v4_conviction_latest.json (always-fresh pointer for
        # downstream consumers) + v4_conviction_<DATE>.json (daily archive). Fixes
        # WDC-style cache miss where bidirectional engine evaluated live but consumers
        # read 3-day-stale dated file.
        try:
            today_str = _dt.now(_tz.utc).date().isoformat()
            for persist_name in ('v4_conviction_latest.json', f'v4_conviction_{today_str}.json'):
                with open(_os.path.join(base_dir, persist_name), 'w') as f:
                    _json.dump(out, f, indent=2, default=str)
        except Exception: pass
        # Cache the body so subsequent requests within 15s serve in <50ms
        # (instead of the 12s rebuild → 502 through pinggy proxy).
        body = _json.dumps(out, default=str).encode()
        try:
            import time as _t
            # Preserve the 'rebuilding' flag if a background rebuild set it
            prev = getattr(self.__class__, '_conv_cache', {}) or {}
            self.__class__._conv_cache = {
                'body': body,
                'fetched_at': _t.time(),
                'rebuilding': False,  # we just finished, not rebuilding
            }
        except Exception:
            pass
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Conv-Cache", "MISS")
        self.end_headers()
        self.wfile.write(body)

    def _serve_breakout_candidates(self):
        """Real-time breakout candidate endpoint (Phase B, 2026-04-28).

        Reads the file v4_breakout_scanner writes every 60s, then OVERLAYS
        live WS-derived data on each card so the page recomputes per
        request like /conviction does:
          · oi_builds_today  ← WS get_oi_builds(ticker)
          · today's flow contribution if needed
          · re-classify (STRONG/MODERATE/WEAK) using fresh inputs

        The Alpaca daily-bars-derived fields (prior14_pct, pos10_pct,
        today_pct from prev close) come from the file as-written — daily
        bars don't change intraday so cache-as-written is correct.
        """
        import json as _json, os as _os
        path = BREAKOUT_CANDIDATES_FILE
        if not _os.path.exists(path):
            self.send_response(404); self.send_header("Content-type","application/json"); self.end_headers()
            self.wfile.write(_json.dumps({
                "error": "v4_breakout_candidates.json not found yet — run v4_breakout_scanner.py"
            }).encode()); return
        try:
            with open(path) as f:
                payload = _json.load(f)
        except Exception as e:
            self.send_response(500); self.send_header("Content-type","application/json"); self.end_headers()
            self.wfile.write(_json.dumps({"error": str(e)}).encode()); return

        # WS overlay — refresh per-card oi_builds + today_pct from live cache
        try:
            from v4_uw_ws import get_oi_builds
            for section in ("strong", "moderate", "weak", "ready", "confirmed"):
                cards = payload.get(section) or []
                if not isinstance(cards, list):
                    continue
                for c in cards:
                    tk = c.get("ticker")
                    if not tk:
                        continue
                    # Live OI builds from option_trades:T tape
                    try:
                        ob = get_oi_builds(tk, vol_min=100, ratio=1.0, side="call")
                        if ob and ob.get("n_qualifying_contracts", 0) > 0:
                            c["oi_builds_today"] = int(ob.get("n_builds", 0))
                            c["oi_builds_source"] = "ws_live"
                    except Exception:
                        pass
                    # Mark refresh time so consumer can show "live as of X"
                    c["overlay_refreshed_at"] = __import__('datetime').datetime.now(
                        __import__('datetime').timezone.utc
                    ).isoformat(timespec="seconds")
        except Exception:
            pass    # if bridge fails, return file as-is (graceful degrade)

        payload["overlay_applied"] = True
        body = _json.dumps(payload, default=str).encode()
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


    def _serve_breakout_outcomes(self):
        """Read v4_breakout_outcomes.jsonl, return rows + aggregate win-rates by source/pattern/bucket."""
        import json as _json, os as _os
        from datetime import datetime as _dt, timezone as _tz
        path = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), 'v4_breakout_outcomes.jsonl')
        rows = []
        if _os.path.exists(path):
            for line in open(path):
                line = line.strip()
                if not line: continue
                try: rows.append(_json.loads(line))
                except Exception: continue

        # Aggregate by various dimensions for COMPLETE rows only
        complete = [r for r in rows if r.get('outcome_status') == 'COMPLETE']
        def stats(items):
            n = len(items)
            if n == 0: return {'n': 0}
            wins = [r for r in items if r.get('outcome_verdict') == 'SUCCESS']
            fails = [r for r in items if r.get('outcome_verdict') == 'FAIL']
            pnls = [r.get('pnl_5d_pct') for r in items if r.get('pnl_5d_pct') is not None]
            return {
                'n':         n,
                'success':   len(wins),
                'fail':      len(fails),
                'chop':      n - len(wins) - len(fails),
                'win_rate':  round(len(wins)/n*100, 1) if n else 0,
                'avg_pnl_5d':round(sum(pnls)/len(pnls), 2) if pnls else None,
            }

        def group_by(items, key_fn):
            buckets = {}
            for r in items:
                k = key_fn(r)
                if k is None: continue
                buckets.setdefault(k, []).append(r)
            return {k: stats(v) for k, v in buckets.items()}

        agg = {
            'overall':        stats(complete),
            'by_source':      group_by(complete, lambda r: ','.join(sorted(r.get('sources') or []))),
            'by_action':      group_by(complete, lambda r: r.get('conviction_action')),
            'by_pattern':     group_by(complete, lambda r: r.get('conviction_pattern')),
            'by_simple_pat':  group_by(complete, lambda r: r.get('conviction_simple')),
            'by_bucket':      group_by(complete, lambda r: r.get('cap_bucket')),
        }

        out = {
            'generated_at_utc': _dt.now(_tz.utc).isoformat(timespec='seconds'),
            'rows_total':       len(rows),
            'rows_pending':     len([r for r in rows if r.get('outcome_status') == 'PENDING']),
            'rows_complete':    len(complete),
            'aggregates':       agg,
            'rows':             sorted(rows, key=lambda r: r.get('fired_date',''), reverse=True),
        }
        self.send_response(200); self.send_header("Content-type", "application/json"); self.end_headers()
        self.wfile.write(_json.dumps(out, default=str).encode())

    def _serve_rescan(self, path):
        """On-demand day-end snapshot rescan triggered from the Conviction page.

        GET /api/v4/rescan         → spawn v4_snapshot_today.py in background, return {started, pid}
        GET /api/v4/rescan_status  → {running, pid, last_snapshot, mtime_age_min}

        Uses a .rescan.pid lockfile to prevent concurrent spawns. Check cadence is
        "snapshot mtime vs now" — callers can poll this every 5-10s and surface
        progress in the UI.
        """
        import json as _json, os as _os, subprocess as _sp, time as _time, glob as _glob
        base_dir = _os.path.dirname(_os.path.abspath(__file__))
        pid_file = _os.path.join(base_dir, '.rescan.pid')

        def _running_pid():
            """Return the live PID owning the rescan, or None.

            CRITICAL: a freshly-exited child of this server is a *zombie*
            until reaped. `os.kill(pid, 0)` returns success for zombies, so
            we must also check process state. Zombies → reap + treat as dead.
            Without this the UI gets stuck on "Snap 208/208 ZTS · 100%"
            forever after the snapshot finishes.
            """
            try:
                if not _os.path.exists(pid_file): return None
                with open(pid_file) as f: pid = int(f.read().strip() or 0)
                if pid <= 0: return None
                _os.kill(pid, 0)  # throws if pid doesn't exist at all

                # Check zombie state via `ps`. macOS + Linux compatible.
                try:
                    out = _sp.check_output(
                        ['ps', '-o', 'stat=', '-p', str(pid)],
                        stderr=_sp.DEVNULL, timeout=2,
                    ).decode().strip()
                    if out and 'Z' in out:
                        # Zombie — reap if it's our child (v4_server.py spawned it)
                        try: _os.waitpid(pid, _os.WNOHANG)
                        except (OSError, ChildProcessError): pass
                        try: _os.unlink(pid_file)
                        except Exception: pass
                        return None
                except (_sp.CalledProcessError, _sp.TimeoutExpired, FileNotFoundError):
                    pass  # ps failed → fall through and trust kill(pid,0)
                return pid
            except (OSError, ValueError):
                # pid dead or file corrupt — clean up
                try: _os.unlink(pid_file)
                except Exception: pass
                return None

        def _latest_snapshot():
            files = sorted(_glob.glob(_os.path.join(base_dir, 'v4_snapshot_*.json')))
            if not files: return None, None
            path = files[-1]
            mtime = _os.path.getmtime(path)
            return _os.path.basename(path), mtime

        def _scan_progress():
            """Parse the last '[N/TOTAL] TICKER ...' line from the scan log."""
            log_path = _os.path.join(base_dir, 'v4_snapshot_today.log')
            if not _os.path.exists(log_path): return None
            try:
                # Efficient tail — read last ~8KB
                with open(log_path, 'rb') as f:
                    f.seek(0, 2)
                    size = f.tell()
                    f.seek(max(0, size - 8192))
                    tail = f.read().decode(errors='ignore')
                import re as _re
                matches = list(_re.finditer(
                    r'\[(\d+)/(\d+)\]\s+(\S+)', tail))
                if not matches: return None
                m = matches[-1]
                return {
                    'current': int(m.group(1)),
                    'total':   int(m.group(2)),
                    'ticker':  m.group(3),
                    'pct':     round(int(m.group(1)) / int(m.group(2)) * 100, 1)
                            if int(m.group(2)) > 0 else None,
                }
            except Exception:
                return None

        pid = _running_pid()
        snap_name, snap_mtime = _latest_snapshot()
        age_min = (_time.time() - snap_mtime) / 60 if snap_mtime else None
        progress = _scan_progress() if pid else None

        if path.endswith('rescan_status'):
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(_json.dumps({
                'running': pid is not None,
                'pid': pid,
                'last_snapshot': snap_name,
                'snapshot_mtime_age_min': round(age_min, 1) if age_min is not None else None,
                'progress': progress,
            }).encode())
            return

        # /api/v4/rescan — spawn if not already running
        if pid is not None:
            self.send_response(200)
            self.send_header("Content-type", "application/json"); self.end_headers()
            self.wfile.write(_json.dumps({
                'status': 'already_running', 'pid': pid
            }).encode())
            return

        # Spawn v4_snapshot_today.py detached
        script = _os.path.join(base_dir, 'v4_snapshot_today.py')
        log = _os.path.join(base_dir, 'v4_snapshot_today.log')
        try:
            proc = _sp.Popen(
                ['/usr/bin/python3', '-u', script],
                cwd=base_dir,
                stdout=open(log, 'a'), stderr=_sp.STDOUT,
                preexec_fn=_os.setpgrp,  # detach from server process group
            )
            with open(pid_file, 'w') as f: f.write(str(proc.pid))
            self.send_response(200)
            self.send_header("Content-type", "application/json"); self.end_headers()
            self.wfile.write(_json.dumps({
                'status': 'started', 'pid': proc.pid,
                'estimated_duration_min': 18,
                'note': 'scan takes ~15-20 min · page will auto-refresh when complete',
            }).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-type", "application/json"); self.end_headers()
            self.wfile.write(_json.dumps({
                'status': 'error', 'error': str(e)
            }).encode())

    def _serve_scanner_status(self):
        """Scanner window state + loop-process presence (LSFC / 0DTE / pre-break)."""
        import json as _json, subprocess
        from datetime import datetime as _dt, timezone as _tz
        now = _dt.now(_tz.utc)
        hr = now.hour + now.minute / 60
        if now.weekday() >= 5:       window = 'WEEKEND'
        elif 13.25 <= hr <= 14.75:   window = 'OD'
        elif 18.25 <= hr <= 21.00:   window = 'PH'
        elif 14.75 < hr < 18.25:     window = 'CHOP'
        elif 13.25 <= hr <= 21.25:   window = 'RTH'
        else:                        window = 'RTH_OFF'
        def _running(pat):
            try:
                r = subprocess.run(['pgrep', '-f', pat], capture_output=True, text=True, timeout=2)
                return bool(r.stdout.strip())
            except Exception:
                return False
        out = {
            'window_state':  window,
            'lsfc_loop':     _running('v4_lsfc_loop_nohup'),
            'zdte_loop':     _running('v4_0dte_loop_nohup'),
            'prebreak_loop': _running('v4_prebreak_loop_nohup'),
            'server_utc':    now.isoformat(timespec='seconds'),
        }
        self.send_response(200); self.send_header("Content-type", "application/json"); self.end_headers()
        self.wfile.write(_json.dumps(out).encode())

    def _serve_chatgpt_mirror(self):
        """READ-ONLY proxy of the parallel chatgpt project's mega7-pattern-lab endpoint.
        We never modify their server — just fetch their JSON so our dashboard can
        render the same per-stock cards without cross-origin issues."""
        import json as _json
        url = "http://localhost:9010/api/v4/mega7-pattern-lab"
        try:
            r = requests.get(url, timeout=5)
            if r.status_code != 200:
                payload = {'error': f'chatgpt upstream HTTP {r.status_code}',
                           'upstream': url, 'available': False}
            else:
                payload = r.json()
                payload['_proxy'] = {'source': url, 'available': True}
        except requests.exceptions.ConnectionError:
            payload = {'error': 'chatgpt server on localhost:9010 not running',
                       'upstream': url, 'available': False}
        except Exception as e:
            payload = {'error': f'{type(e).__name__}: {e}',
                       'upstream': url, 'available': False}
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps(payload).encode())

    def _serve_live_alpaca(self):
        """Live snapshot of the entire Alpaca paper account — both bots' positions.
        Cross-references each Alpaca position with our local file to mark which
        ones are 'ours' vs 'other'."""
        import json as _json
        try:
            r = requests.get(f"{ALPACA_TRADING_URL}/v2/positions", headers=ALPACA_HEADERS, timeout=10)
            positions = r.json() if r.status_code == 200 else []
            r2 = requests.get(f"{ALPACA_TRADING_URL}/v2/account", headers=ALPACA_HEADERS, timeout=10)
            account = r2.json() if r2.status_code == 200 else {}
        except Exception as e:
            self.send_response(200); self.send_header("Content-type", "application/json"); self.end_headers()
            self.wfile.write(_json.dumps({"error": str(e), "positions": []}).encode())
            return

        # Build set of our local symbols
        our_symbols = set()
        if os.path.exists(POSITIONS_FILE):
            try:
                with open(POSITIONS_FILE) as f: ours = _json.load(f)
                our_symbols = {p.get('contract_symbol') for p in ours
                               if p.get('status') in ('OPEN', 'PENDING_ENTRY')}
            except Exception: pass

        # Tag each Alpaca position with origin
        enriched = []
        for p in positions:
            sym = p.get('symbol', '')
            origin = 'claude_v4' if sym in our_symbols else 'other_bot'
            enriched.append({
                'symbol': sym,
                'qty': int(p.get('qty', 0)),
                'avg_entry_price': float(p.get('avg_entry_price', 0)),
                'current_price': float(p.get('current_price', 0)),
                'market_value': float(p.get('market_value', 0)),
                'unrealized_pl': float(p.get('unrealized_pl', 0)),
                'unrealized_plpc': float(p.get('unrealized_plpc', 0)),
                'side': p.get('side'),
                'origin': origin,
            })
        # Sort by unrealized P&L descending
        enriched.sort(key=lambda x: -x['unrealized_pl'])

        # Aggregate stats
        total_unrealized = sum(p['unrealized_pl'] for p in enriched)
        ours = [p for p in enriched if p['origin'] == 'claude_v4']
        other = [p for p in enriched if p['origin'] == 'other_bot']

        payload = {
            'account': {
                'account_number': account.get('account_number'),
                'cash': float(account.get('cash', 0)) if account.get('cash') else 0,
                'equity': float(account.get('equity', 0)) if account.get('equity') else 0,
                'buying_power': float(account.get('buying_power', 0)) if account.get('buying_power') else 0,
                'options_buying_power': float(account.get('options_buying_power', 0)) if account.get('options_buying_power') else 0,
            },
            'summary': {
                'total_positions': len(enriched),
                'claude_v4_positions': len(ours),
                'other_bot_positions': len(other),
                'total_unrealized_pl': round(total_unrealized, 2),
                'claude_v4_unrealized_pl': round(sum(p['unrealized_pl'] for p in ours), 2),
                'other_bot_unrealized_pl': round(sum(p['unrealized_pl'] for p in other), 2),
            },
            'positions': enriched,
        }
        self.send_response(200); self.send_header("Content-type", "application/json"); self.end_headers()
        self.wfile.write(_json.dumps(payload).encode())

    def send_error(self, code, message=None, explain=None):
        if code == 404:
            self.send_response(302)
            self.send_header('Location', '/v2/')
            self.end_headers()
            return
        super().send_error(code, message, explain)

    # ─── Prebreakout BUY signal logging ────────────────────────────────
    # The prebreakout page surfaces high-confidence (score >= 300) BUYs only.
    # Every fetch + every user "Buy" click is logged to v4_prebreakout_log.jsonl
    # so we can backtest WHAT was shown and WHEN, separately from clicks.
    PREBREAK_LOG = None  # lazy

    def _prebreak_log_path(self):
        return os.path.join(BASE_DIR, 'v4_prebreakout_log.jsonl')

    def _append_prebreak_log(self, entry):
        import json as _json
        from datetime import datetime as _dt, timezone as _tz
        entry = dict(entry)
        entry.setdefault('logged_utc', _dt.now(_tz.utc).isoformat())
        try:
            with open(self._prebreak_log_path(), 'a') as f:
                f.write(_json.dumps(entry, default=str) + '\n')
        except Exception:
            pass  # never break the request on logging failure

    def _serve_staging_with_log(self):
        """Serve v4_staging_signals.json AND log the BUYs that were shown."""
        import json as _json
        path = os.path.join(BASE_DIR, 'v4_staging_signals.json')
        if not os.path.exists(path):
            self._serve_json(path, "v4_staging_signals.json not found yet — run v4_staging_scanner.py")
            return
        try:
            with open(path) as f: payload = _json.load(f)
            buys = payload.get('buys') or []
            # Throttle: don't log identical view more than once per 5 min
            should_log = True
            try:
                if os.path.exists(self._prebreak_log_path()):
                    with open(self._prebreak_log_path(), 'rb') as f:
                        f.seek(0, 2); size = f.tell()
                        f.seek(max(0, size - 4000))
                        tail = f.read().decode('utf-8', errors='ignore').splitlines()
                    from datetime import datetime as _dt, timezone as _tz
                    now = _dt.now(_tz.utc)
                    for line in reversed(tail):
                        try:
                            e = _json.loads(line)
                            if e.get('event') != 'view': continue
                            ts = _dt.fromisoformat(e.get('logged_utc','').replace('Z','+00:00'))
                            if (now - ts).total_seconds() < 300:
                                should_log = False
                            break
                        except Exception:
                            continue
            except Exception:
                pass
            if should_log:
                self._append_prebreak_log({
                    'event': 'view',
                    'snapshot': payload.get('snapshot_used'),
                    'generated_utc': payload.get('generated_utc'),
                    'buy_count': len(buys),
                    'buys': [{'ticker': b['ticker'], 'score': b['score'],
                              'pattern': b.get('pattern'), 'pattern_why': b.get('pattern_why'),
                              'last_close': b.get('last_close'),
                              'today_pct': b.get('today_pct'),
                              'cum_3d': b.get('cum_3d'),
                              'beta': b.get('beta'),
                              'sector': b.get('sector'),
                              'iv_today': b.get('iv_today'),
                              'iv_change_5d': b.get('iv_change_5d'),
                              'put_call_ratio': b.get('put_call_ratio'),
                              'call_ask_share': b.get('call_ask_share'),
                              'dp_prem_m': b.get('dp_prem_m'),
                              'last_hr_call_prem_m': b.get('last_hr_call_prem_m'),
                              'otm_call_prem_m': b.get('otm_call_prem_m'),
                              'bull_m': b.get('bull_m'),
                              'bear_m': b.get('bear_m'),
                              'pct_below_5d_high': b.get('pct_below_5d_high'),
                              'signals': b.get('signals')}
                             for b in buys],
                })
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps(payload, default=str).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps({'error': str(e)}).encode())

    def _serve_prebreak_log_tail(self):
        """Return last 100 log entries — for debugging / inspection."""
        import json as _json
        p = self._prebreak_log_path()
        out = []
        if os.path.exists(p):
            try:
                with open(p) as f: lines = f.readlines()
                for line in lines[-100:]:
                    try: out.append(_json.loads(line))
                    except Exception: pass
            except Exception:
                pass
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps({'count': len(out), 'entries': out}, default=str).encode())

    def do_POST(self):
        """POST endpoints — log writes + on-demand scanner revalidation."""
        import json as _json
        path = self.path.split('?', 1)[0]
        if path in ('/api/v4/prebreak_log', '/v2/api/v4/prebreak_log',
                    '/api/v4/prebreak_buy_click', '/v2/api/v4/prebreak_buy_click'):
            try:
                clen = int(self.headers.get('Content-Length') or 0)
                raw = self.rfile.read(clen) if clen else b'{}'
                body = _json.loads(raw or b'{}')
            except Exception:
                body = {}
            entry = {'event': body.get('event') or 'buy_click', **body}
            self._append_prebreak_log(entry)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps({'ok': True}).encode()); return
        # Re-run v4_staging_scanner.py against the latest snapshot, return updated payload
        if path in ('/api/v4/staging_revalidate', '/v2/api/v4/staging_revalidate'):
            self._serve_scanner_revalidate('v4_staging_scanner.py', 'v4_staging_signals.json'); return
        # Re-run v4_today_breakout_scanner.py and return updated payload
        if path in ('/api/v4/today_breakouts_revalidate', '/v2/api/v4/today_breakouts_revalidate'):
            self._serve_scanner_revalidate('v4_today_breakout_scanner.py', 'v4_today_breakouts.json'); return
        # Re-run v4_continuation_scanner.py and return updated payload
        if path in ('/api/v4/continuation_revalidate', '/v2/api/v4/continuation_revalidate'):
            self._serve_scanner_revalidate('v4_continuation_scanner.py', 'v4_continuation_signals.json'); return
        # Re-run v4_morning_check.py — hits Alpaca live, ~2-3s for ~50 tickers
        if path in ('/api/v4/morning_refresh', '/v2/api/v4/morning_refresh'):
            self._serve_scanner_revalidate('v4_morning_check.py', 'v4_morning_signals.json'); return
        # Lifecycle refresh — re-runs continuation+morning, returns merged payload.
        # Breakout page Refresh button POSTs here (was 404'ing before 2026-04-26).
        if path in ('/api/v4/lifecycle_refresh', '/v2/api/v4/lifecycle_refresh'):
            self._serve_lifecycle_refresh(); return
        # Force-Run All — kicks every RTH-gated collector with FORCE_RUN=1.
        # Lets the user pull fresh data outside RTH (e.g. weekend dry-run) or
        # verify the collector chain after launchd/TCC fixes. ~30-60s synchronous.
        if path in ('/api/v4/force_run_all', '/v2/api/v4/force_run_all'):
            self._serve_force_run_all(); return
        # ── Refresh-button safety net (added 2026-04-26) ──
        # Several dashboard pages POST to *_refresh endpoints that are only
        # registered in do_GET(). Rather than mirror every route, fall through
        # to do_GET() for any whitelisted *_refresh path. Side-effect-free
        # (each handler re-runs a scan and returns JSON either way).
        REFRESH_FALLBACK = {
            '/api/v4/today_refresh',          '/v2/api/v4/today_refresh',
            '/api/v4/analyze_refresh',        '/v2/api/v4/analyze_refresh',
            '/api/v4/scalp_backtest_refresh', '/v2/api/v4/scalp_backtest_refresh',
            '/api/v4/put_backtest_refresh',   '/v2/api/v4/put_backtest_refresh',
        }
        if path in REFRESH_FALLBACK:
            self.do_GET(); return
        # Unknown POST → 404
        self.send_response(404)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps({'error': 'unknown POST endpoint'}).encode())

    # ─── TWO-TRACK FRAMEWORK (SWING + SCALP) — ticker-class + day-of-week aware ───
    # Mega-caps with daily/2-DTE expirations — different option structure entirely.
    MEGA_CAP_TICKERS = {
        'NVDA','AAPL','MSFT','AMZN','GOOGL','GOOG','META','TSLA','AMD','AVGO',
        'SPY','QQQ','IWM','SPX','NFLX','MU','INTC','TSM','ORCL','CRM',
    }

    def _ticker_class(self, ticker):
        """MEGA = daily 2-DTE options available; MID = weekly Friday only."""
        return 'MEGA' if ticker in self.MEGA_CAP_TICKERS else 'MID'

    def _today_dow(self):
        """0=Mon, 1=Tue, 2=Wed, 3=Thu, 4=Fri, 5=Sat, 6=Sun (in PT)."""
        from datetime import datetime as _dt
        # Use UTC then map roughly to PT (UTC-7 or -8 depending on DST)
        utc_now = _dt.utcnow()
        pt_hour = (utc_now.hour - 7) % 24  # PT approx
        # If UTC time before 7am, the PT date is yesterday
        if utc_now.hour < 7:
            from datetime import timedelta as _td
            return (utc_now - _td(days=1)).weekday()
        return utc_now.weekday()

    def _swing_eligible_today(self):
        """Mon-Thu (0-3) = swing entry day. Friday = 0DTE-only."""
        return self._today_dow() < 4

    def _build_swing_plan(self, stock, today_dow, bankroll=20000):
        """SWING trade plan — DTE depends on (ticker class × day of week).
        Validated rules from swing_payoff_backtest:
          - Day 1 entry > Day 0 entry (so set entry_when = "tomorrow's close")
          - Layered exits: 1/3 at +30%, 1/3 at +50%, 1/3 to +100%
          - Hard SL: -40% premium
          - Time stop: 1-2d for mega 2DTE, Day 5 for mid weekly
        """
        underlying = stock.get('last_close')
        if not underlying: return None
        ticker = stock['ticker']
        cls = self._ticker_class(ticker)

        # DTE selection
        if cls == 'MEGA':
            dte = 2
            dte_label = '2 DTE (mega-cap daily expiry)'
            entry_premium_pct = 0.012  # ~1.2% of underlying for 2 DTE ATM mega-cap
            time_stop = '1-2 days hold'
        elif today_dow == 3:  # Thursday
            dte = 8
            dte_label = 'next Fri (~8 DTE)'
            entry_premium_pct = 0.04
            time_stop = 'Day 5 if flat'
        else:  # Mon-Wed
            dte = 5
            dte_label = 'this Fri (~5 DTE)'
            entry_premium_pct = 0.025
            time_stop = 'Day 5 if flat'

        # Strike — slightly OTM for high gamma
        if underlying >= 500:
            strike = round(underlying / 10) * 10
            if strike < underlying: strike += 10
        elif underlying >= 100:
            strike = round(underlying / 5) * 5
            if strike < underlying: strike += 5
        else:
            strike = round(underlying)
            if strike < underlying: strike += 1

        premium_est = round(underlying * entry_premium_pct, 2)
        # Layered exits
        tp1_premium = round(premium_est * 1.30, 2)  # +30%
        tp2_premium = round(premium_est * 1.50, 2)  # +50%
        tp3_premium = round(premium_est * 2.00, 2)  # +100%
        sl_premium  = round(premium_est * 0.60, 2)  # -40%

        # Sizing — risk 1% of bankroll on the SL distance
        risk_per_trade = bankroll * 0.01
        sl_distance = premium_est - sl_premium  # $ per contract loss at SL
        contracts = max(1, min(int(risk_per_trade / (sl_distance * 100)), 5))
        position_cost = round(contracts * premium_est * 100, 2)
        max_loss = round(contracts * sl_distance * 100, 2)
        # Profit if 1/3 each at TP1/TP2/TP3 — assume 1/3 of contracts at each level
        third = contracts / 3.0
        target_profit_layered = round(
            (third * (tp1_premium - premium_est) +
             third * (tp2_premium - premium_est) +
             third * (tp3_premium - premium_est)) * 100, 2)

        return {
            'track': 'SWING',
            'underlying_ticker': ticker,
            'underlying_price': round(underlying, 2),
            'ticker_class': cls,
            'dte': dte,
            'dte_label': dte_label,
            'strike': strike,
            'premium_est': premium_est,
            'tp1_premium': tp1_premium, 'tp1_pct': 30,
            'tp2_premium': tp2_premium, 'tp2_pct': 50,
            'tp3_premium': tp3_premium, 'tp3_pct': 100,
            'sl_premium': sl_premium, 'sl_pct': -40,
            'contracts': contracts,
            'position_cost': position_cost,
            'max_loss': max_loss,
            'target_profit_layered': target_profit_layered,
            'time_stop': time_stop,
            'entry_when': "Tomorrow's close (Day 1 confirmation > Day 0 reaction — validated 32% vs 23% TP hit rate)",
            'order_symbol_hint': f"{ticker} {strike}C exp {dte_label}",
        }

    def _build_scalp_plan(self, stock, bankroll=20000):
        """SCALP trade plan — fun-mode option, fast in/out.
        Same-day for mega-caps (0DTE), short weekly for mids.
        Fixed: TP +20%, SL -20%, exit by midday.
        """
        underlying = stock.get('last_close')
        if not underlying: return None
        ticker = stock['ticker']
        cls = self._ticker_class(ticker)
        if cls == 'MEGA':
            dte_label = '0 DTE (today, mega-cap)'
            premium_est = round(underlying * 0.005, 2)
        else:
            dte_label = 'this Fri (~5 DTE)'
            premium_est = round(underlying * 0.025, 2)

        if underlying >= 500: strike = round(underlying / 10) * 10
        elif underlying >= 100: strike = round(underlying / 5) * 5
        else: strike = round(underlying)
        if strike < underlying:
            strike += 10 if underlying >= 500 else (5 if underlying >= 100 else 1)

        tp_premium = round(premium_est * 1.20, 2)
        sl_premium = round(premium_est * 0.80, 2)
        risk_per_trade = bankroll * 0.005  # SCALP uses smaller 0.5% risk
        sl_distance = premium_est - sl_premium
        contracts = max(1, min(int(risk_per_trade / (sl_distance * 100)) if sl_distance > 0 else 1, 3))
        return {
            'track': 'SCALP',
            'underlying_ticker': ticker,
            'underlying_price': round(underlying, 2),
            'ticker_class': cls,
            'dte_label': dte_label,
            'strike': strike,
            'premium_est': premium_est,
            'tp_premium': tp_premium, 'tp_pct': 20,
            'sl_premium': sl_premium, 'sl_pct': -20,
            'contracts': contracts,
            'position_cost': round(contracts * premium_est * 100, 2),
            'max_loss': round(contracts * sl_distance * 100, 2),
            'target_profit': round(contracts * (tp_premium - premium_est) * 100, 2),
            'time_stop': '12:30 PM PT (exit by midday)',
            'entry_when': 'Right now (intraday momentum)',
            'order_symbol_hint': f"{ticker} {strike}C exp {dte_label}",
        }

    def _today_compute_conviction(self, stock):
        """Score 0-100 based on multi-day chain quality + morning extension + breakout score.
        A+ trades (≥80) get a card. Below 80 → page says PASS.
        """
        days = stock.get('days_in_chain') or 0
        cum = stock.get('cum_pct_from_origin') or 0
        origin_pct = stock.get('origin_pct') or 0

        # Base from chain length (proven multi-day momentum is the strongest signal)
        if days >= 4: base = 50
        elif days == 3: base = 45
        elif days == 2: base = 40
        elif days == 1: base = 20  # Day 0 fresh — no continuation history yet
        else: base = 0

        # Cumulative move bonus (+10 per 5%, capped at +25)
        cum_bonus = min(int(cum / 5) * 10, 25) if cum >= 0 else 0

        # Origin day strength (validated in our backtest — bigger D1 = stronger setup)
        if origin_pct >= 10: origin_bonus = 15
        elif origin_pct >= 7: origin_bonus = 10
        elif origin_pct >= 5: origin_bonus = 5
        else: origin_bonus = 0

        # Morning extension — live quote vs origin close
        morn = stock.get('morning') or {}
        morn_pct = morn.get('morning_pct_vs_d1_close')
        action = morn.get('action')
        morn_bonus = 0
        if action == 'BUY': morn_bonus = 15
        elif action == 'APPROACHING':
            if morn_pct is not None and morn_pct >= 0: morn_bonus = 5
            else: morn_bonus = -5
        elif action == 'HIDDEN': morn_bonus = -25  # faded overnight = avoid

        # Breakout score (D1 quality from breakout scanner)
        brk = stock.get('breakout') or {}
        brk_score = brk.get('score') or 0
        if brk_score >= 70: brk_bonus = 10
        elif brk_score >= 50: brk_bonus = 5
        else: brk_bonus = 0

        total = max(0, min(100, base + cum_bonus + origin_bonus + morn_bonus + brk_bonus))
        return {
            'score': total,
            'breakdown': {
                'chain_length': base,
                'cumulative_move': cum_bonus,
                'origin_strength': origin_bonus,
                'morning_extension': morn_bonus,
                'breakout_quality': brk_bonus,
            },
            'tier': 'A+' if total >= 90 else ('A' if total >= 80 else
                    ('B' if total >= 70 else ('C' if total >= 50 else 'PASS'))),
        }

    def _today_build_trade_plan(self, stock, bankroll=20000, daily_target=200):
        """Generate the actionable trade card: option strike, premium est, TP/SL, sizing."""
        underlying = stock.get('last_close')
        if not underlying: return None
        morn = stock.get('morning') or {}
        # Use live morning price if available, else last close
        entry_price = morn.get('morning_price') or underlying

        # Strike picker — slightly OTM weekly call (delta ~0.40)
        # For stocks > $100: round to nearest $5; > $500: nearest $10; else nearest $1
        if underlying >= 500:
            strike = round(underlying / 10) * 10
            if strike < underlying: strike += 10
        elif underlying >= 100:
            strike = round(underlying / 5) * 5
            if strike < underlying: strike += 5
        else:
            strike = round(underlying)
            if strike < underlying: strike += 1

        # Premium estimate (rough — ~2-2.5% of underlying for slightly OTM weekly)
        # Real chain lookup would replace this; v1 uses heuristic
        otm_pct = (strike - underlying) / underlying * 100
        if otm_pct <= 0.5:
            premium_est = round(underlying * 0.025, 2)  # ATM
        elif otm_pct <= 2:
            premium_est = round(underlying * 0.02, 2)
        else:
            premium_est = round(underlying * 0.015, 2)

        # TP / SL in premium dollars
        tp_premium = round(premium_est * 1.20, 2)
        sl_premium = round(premium_est * 0.80, 2)

        # Sizing — risk 1% of bankroll, max
        risk_per_trade = bankroll * 0.01  # $200 on $20K
        sl_distance = premium_est - sl_premium  # $ loss per contract at SL
        contracts_for_risk = max(1, int(risk_per_trade / (sl_distance * 100)))  # contracts (1 contract = 100 shares)
        # Cap at 5 contracts to limit complexity
        contracts = min(contracts_for_risk, 5)
        position_cost = contracts * premium_est * 100
        max_loss = contracts * sl_distance * 100
        target_profit = contracts * (tp_premium - premium_est) * 100

        # Days to expiration — pick nearest Friday at least 3 trading days out
        # For simplicity, suggest "this Fri" or "next Fri"
        # (Real impl would compute from datetime; v1 just labels it)
        dte_label = "this Fri (~5 DTE)"

        return {
            'underlying_ticker': stock['ticker'],
            'underlying_price': round(underlying, 2),
            'entry_price_underlying': round(entry_price, 2),
            'strike': strike,
            'expiration_label': dte_label,
            'premium_est': premium_est,
            'tp_premium': tp_premium,
            'sl_premium': sl_premium,
            'tp_pct': 20,
            'sl_pct': -20,
            'contracts': contracts,
            'position_cost': round(position_cost, 2),
            'max_loss': round(max_loss, 2),
            'target_profit': round(target_profit, 2),
            'meets_daily_target': target_profit >= daily_target * 0.8,  # within 80% of $200
            'time_stop': '12:30 PM PT',
            'order_symbol_hint': f"{stock['ticker']}{strike}C",
        }

    def _is_scalp_plus_eligible(self, stock):
        """SCALP+ tier: Tech mega-cap + IV ≥ 70 + whale activity confirmed in last 3d."""
        brk = stock.get('breakout') or {}
        sector = stock.get('sector') or brk.get('sector') or ''
        mcap = stock.get('mcap_b') or brk.get('mcap_b') or 0
        iv = stock.get('iv_rank_1y') or brk.get('iv_rank_1y') or 0
        if not (sector == 'Technology' and (mcap or 0) >= 100 and (iv or 0) >= 70):
            return False
        whales = stock.get('whales') or {}
        return (whales.get('alerts_3d_n') or 0) >= 3 or (whales.get('sweep_count_3d') or 0) >= 1

    def _serve_today(self):
        """Two-track framework: SWING (main) + SCALP (fun-mode, optional).
        SWING: Mon-Thu only, conviction ≥80, layered exits, multi-day hold.
        SCALP: any day, SCALP+ qualifying ticker, +20% TP / -20% SL, exit by midday.
        STEALTH watch: pre-position list (institutional accumulation, no price move yet).
        """
        import json as _json
        cont_path  = os.path.join(BASE_DIR, 'v4_continuation_signals.json')
        brk_path   = os.path.join(BASE_DIR, 'v4_today_breakouts.json')
        morn_path  = os.path.join(BASE_DIR, 'v4_morning_signals.json')
        whale_path = os.path.join(BASE_DIR, 'v4_flow_alerts.json')

        if not os.path.exists(cont_path):
            self.send_response(404)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps({'error': 'continuation signals not found — run v4_continuation_scanner.py'}).encode())
            return
        try:
            with open(cont_path) as f: cont = _json.load(f)
            brk_idx = {}; morn_idx = {}; whales_idx = {}
            if os.path.exists(brk_path):
                with open(brk_path) as f: brk_payload = _json.load(f)
                for r in (brk_payload.get('all_scored') or []):
                    brk_idx[r.get('ticker')] = r
            if os.path.exists(morn_path):
                with open(morn_path) as f: morn_payload = _json.load(f)
                for r in (morn_payload.get('all_visible') or []):
                    morn_idx[r.get('ticker')] = r
            if os.path.exists(whale_path):
                with open(whale_path) as f: whale_payload = _json.load(f)
                whales_idx = whale_payload.get('per_ticker') or {}

            # Day-of-week context
            today_dow = self._today_dow()
            dow_names = ['Mon','Tue','Wed','Thu','Fri','Sat','Sun']
            day_name = dow_names[today_dow]
            swing_eligible_day = self._swing_eligible_today()
            mode = ('PRIME' if today_dow == 3 else
                    ('FRIDAY_NO_SWING' if today_dow == 4 else
                     ('WEEKEND' if today_dow > 4 else 'NORMAL')))
            mode_msg = {
                'PRIME':            "🔥 PRIME ENTRY DAY — last swing day before weekend",
                'FRIDAY_NO_SWING':  "⚡ 0DTE-only mode — no new swing entries today",
                'WEEKEND':          "🌙 Market closed — check back Monday",
                'NORMAL':           "⚙ Standard swing day — Mon-Wed entries",
            }[mode]

            # Merge + score
            ranked = []
            for r in (cont.get('all_scored') or []):
                tkr = r['ticker']
                merged = {**r,
                          'breakout': brk_idx.get(tkr),
                          'morning': morn_idx.get(tkr),
                          'whales': whales_idx.get(tkr)}
                conv = self._today_compute_conviction(merged)
                merged['conviction'] = conv
                ranked.append(merged)
            ranked.sort(key=lambda s: -s['conviction']['score'])

            # SWING: top conviction-≥80 pick (only if Mon-Thu)
            swing_pick = None
            swing_top = next((s for s in ranked if s['conviction']['score'] >= 80), None)
            if swing_top and swing_eligible_day:
                swing_pick = {
                    **swing_top,
                    'plan': self._build_swing_plan(swing_top, today_dow),
                }

            # SCALP track removed from v4 — true scalping lives in LuxAlgo + SPY 0DTE,
            # which is a separate system. v4 is swing-only. _build_scalp_plan and
            # _is_scalp_plus_eligible methods preserved as dead code (no surface).
            tradeable = bool(swing_pick)
            # top conviction = actual highest, not 0 when below threshold
            top_score = ranked[0]['conviction']['score'] if ranked else 0

            # Pass reason
            pass_reason = None
            if not tradeable:
                if mode == 'WEEKEND':
                    pass_reason = f"It's {day_name} — markets closed. Top conviction in latest data: {top_score}/100."
                elif not swing_eligible_day and ranked and ranked[0]['conviction']['score'] >= 80:
                    pass_reason = f"It's {day_name} — no swing entries today. Top conviction is {top_score}/100 but Friday = 0DTE-only mode."
                elif top_score < 80:
                    pass_reason = f"Top conviction is {top_score}/100. Need ≥80 to take a swing."
                else:
                    pass_reason = "No actionable setup right now."

            # Backups: show top 5 — skip whichever index is already the swing/scalp pick
            backups_start = 1 if swing_pick else 0
            backups = []
            for s in ranked[backups_start:backups_start+5]:
                backups.append({
                    'ticker': s['ticker'],
                    'conviction_score': s['conviction']['score'],
                    'conviction_tier': s['conviction']['tier'],
                    'days_in_chain': s.get('days_in_chain'),
                    'cum_pct_from_origin': s.get('cum_pct_from_origin'),
                    'origin_date': s.get('origin_date'),
                    'pattern_label': s.get('pattern_label'),
                    'morning_action': (s.get('morning') or {}).get('action'),
                })

            payload = {
                'generated_utc': datetime.now(timezone.utc).isoformat(),
                'snapshot_used': cont.get('snapshot_used'),
                'snapshots_used': cont.get('snapshots_used'),
                'morning_generated_utc': (morn_payload.get('generated_utc') if os.path.exists(morn_path) else None),
                'day_of_week': day_name,
                'day_of_week_idx': today_dow,
                'mode': mode,
                'mode_message': mode_msg,
                'swing_eligible_today': swing_eligible_day,
                'tradeable': tradeable,
                'pass_reason': pass_reason,
                'swing_pick': swing_pick,
                'top_conviction_score': top_score,
                'backups': backups,
                'total_candidates': len(ranked),
                'methodology': 'SWING-ONLY. Conviction ≥80 + Mon-Thu, layered exits +30/+50/+100, hold 1-5 days. Scalp track removed — true scalping is LuxAlgo + SPY 0DTE (separate system).',
            }
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps(payload, default=str).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps({'error': str(e)}).encode())

    def _serve_today_refresh(self):
        """Re-run continuation + morning, then serve fresh /today payload."""
        import subprocess as _sp, time as _time
        t0 = _time.time()
        for script_name in ['v4_continuation_scanner.py', 'v4_morning_check.py']:
            try:
                _sp.run(['python3', os.path.join(BASE_DIR, script_name)],
                        cwd=BASE_DIR, capture_output=True, timeout=90)
            except Exception: pass
        # Now serve fresh
        self._serve_today()

    def _serve_lifecycle(self):
        """Merge continuation chains + breakout score + live morning quotes + whale alerts per ticker.
        Source of truth for which tickers to show: v4_continuation_signals.json.
        Enriches each ticker with:
          - breakout: matching entry from v4_today_breakouts.json (score, pattern, signals)
          - morning:  matching entry from v4_morning_signals.json (live quote vs origin close)
          - whales:   matching entry from v4_flow_alerts.json (UW whale alert stats, last 3d)
        """
        import json as _json
        cont_path  = os.path.join(BASE_DIR, 'v4_continuation_signals.json')
        brk_path   = os.path.join(BASE_DIR, 'v4_today_breakouts.json')
        morn_path  = os.path.join(BASE_DIR, 'v4_morning_signals.json')
        whale_path = os.path.join(BASE_DIR, 'v4_flow_alerts.json')

        if not os.path.exists(cont_path):
            self.send_response(404)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps({'error': 'continuation signals not found — run v4_continuation_scanner.py'}).encode())
            return
        try:
            with open(cont_path) as f: cont = _json.load(f)
            brk = {}; morn = {}; whales = {}
            if os.path.exists(brk_path):
                with open(brk_path) as f: brk_payload = _json.load(f)
                for r in (brk_payload.get('all_scored') or []):
                    brk[r.get('ticker')] = r
            if os.path.exists(morn_path):
                with open(morn_path) as f: morn_payload = _json.load(f)
                for r in (morn_payload.get('all_visible') or []):
                    morn[r.get('ticker')] = r
            if os.path.exists(whale_path):
                with open(whale_path) as f: whale_payload = _json.load(f)
                whales = whale_payload.get('per_ticker') or {}

            # Load latest v4_uw_signals (coiling, smart money, sympathy, IV/RV)
            import glob as _glob
            uw_signals_per_ticker = {}
            sector_fire_active = False
            leaders_firing = []
            uw_paths = sorted(_glob.glob(os.path.join(BASE_DIR, 'v4_uw_signals_*.json')))
            if uw_paths:
                try:
                    with open(uw_paths[-1]) as f: uw_payload = _json.load(f)
                    uw_signals_per_ticker = uw_payload.get('per_ticker') or {}
                    sector_fire_active = bool(uw_payload.get('sector_fire_active'))
                    leaders_firing = uw_payload.get('leaders_firing') or []
                except Exception: pass

            stocks = []
            for r in (cont.get('all_scored') or []):
                tkr = r['ticker']
                stocks.append({
                    **r,
                    'breakout': brk.get(tkr),
                    'morning': morn.get(tkr),
                    'whales': whales.get(tkr),
                    'uw_signals': uw_signals_per_ticker.get(tkr),
                })

            payload = {
                'generated_utc': datetime.now(timezone.utc).isoformat() if False else cont.get('generated_utc'),
                'snapshot_used': cont.get('snapshot_used'),
                'snapshots_used': cont.get('snapshots_used'),
                'continuation_methodology': cont.get('methodology'),
                'breakout_methodology': (brk_payload.get('methodology') if os.path.exists(brk_path) else None),
                'morning_methodology':   (morn_payload.get('methodology') if os.path.exists(morn_path) else None),
                'morning_generated_utc': (morn_payload.get('generated_utc') if os.path.exists(morn_path) else None),
                'count': len(stocks),
                'fresh_today': sum(1 for s in stocks if s.get('days_in_chain') == 1),
                'active_chains': sum(1 for s in stocks if s.get('days_in_chain', 0) >= 2),
                'strong_chains': sum(1 for s in stocks if (s.get('cum_pct_from_origin') or 0) >= 10),
                # NEW spike-causation tags (validated 2026-04-25):
                'sector_fire_active': sector_fire_active,
                'leaders_firing': leaders_firing,
                'coiled_count':       sum(1 for s in stocks if (s.get('uw_signals') or {}).get('is_coiled')),
                'sympathy_count':     sum(1 for s in stocks if (s.get('uw_signals') or {}).get('is_sympathy_candidate')),
                'smart_money_count':  sum(1 for s in stocks if (s.get('uw_signals') or {}).get('is_smart_money')),
                'stocks': stocks,
            }
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps(payload, default=str).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps({'error': str(e)}).encode())

    def _serve_lifecycle_refresh(self):
        """Re-run continuation scanner AND morning check (Alpaca live), then return merged.
        Continuation runs against snapshot files (~1s); morning hits Alpaca live (~1s).
        """
        import subprocess as _sp, time as _time
        t0 = _time.time()
        results = {}
        for script_name, label in [('v4_continuation_scanner.py', 'continuation'),
                                    ('v4_morning_check.py',       'morning')]:
            try:
                proc = _sp.run(['python3', os.path.join(BASE_DIR, script_name)],
                               cwd=BASE_DIR, capture_output=True, timeout=90)
                results[label] = {'returncode': proc.returncode}
            except Exception as e:
                results[label] = {'error': str(e)}
        elapsed = _time.time() - t0
        # Now serve the merged payload
        self._serve_lifecycle()
        # (Note: response already sent by _serve_lifecycle. Logging happens via merged endpoint.)

    # Force-Run All ─ kicks every RTH-gated collector with FORCE_RUN=1 (bypasses
    # the Mon-Fri 6:15 AM-1:00 PM PT gate). Now runs in a background thread so
    # the request returns instantly and the client can poll for live progress
    # via _serve_force_run_all_status. Lets the user kick a run from /today and
    # watch the bar continue if they navigate to /breakout (or vice versa).
    def _serve_force_run_all(self):
        import json as _json, time as _time
        with _FR_LOCK:
            if _FR_STATE['status'] == 'running':
                # No-op start: just return the live snapshot so the client sees
                # there's already a run in progress and starts polling.
                snapshot = self._fr_snapshot(already_running=True)
            else:
                # Reset state and kick a new background worker.
                _FR_STATE['run_id']           += 1
                _FR_STATE['status']            = 'running'
                _FR_STATE['started_at']        = _time.time()
                _FR_STATE['finished_at']       = None
                _FR_STATE['current_idx']       = 0
                _FR_STATE['current_collector'] = FR_COLLECTORS[0][1] if FR_COLLECTORS else None
                _FR_STATE['total']             = len(FR_COLLECTORS)
                _FR_STATE['collectors']        = []
                _FR_STATE['last_error']        = None
                _FR_STATE['total_elapsed_sec'] = None
                _FR_STATE['summary']           = None
                run_id = _FR_STATE['run_id']
                snapshot = self._fr_snapshot(already_running=False)
                t = _threading.Thread(target=_force_run_worker, args=(run_id,), daemon=True)
                t.start()
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps(snapshot, default=str).encode())

    def _serve_force_run_all_status(self):
        """Poll endpoint — returns the current Force-Run state snapshot."""
        import json as _json
        snapshot = self._fr_snapshot(already_running=False)
        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.end_headers()
        self.wfile.write(_json.dumps(snapshot, default=str).encode())

    def _fr_snapshot(self, already_running=False):
        """Build a JSON-safe copy of _FR_STATE. Caller must hold _FR_LOCK if
        the snapshot needs to be consistent — for simple polling the natural
        race (a collector finishing between fields) is harmless."""
        with _FR_LOCK:
            return {
                'status':             _FR_STATE['status'],
                'started_at':         _FR_STATE['started_at'],
                'finished_at':        _FR_STATE['finished_at'],
                'current_idx':        _FR_STATE['current_idx'],
                'current_collector':  _FR_STATE['current_collector'],
                'total':              _FR_STATE['total'],
                'collectors':         list(_FR_STATE['collectors']),
                'last_error':         _FR_STATE['last_error'],
                'total_elapsed_sec':  _FR_STATE['total_elapsed_sec'],
                'summary':            _FR_STATE['summary'],
                'run_id':             _FR_STATE['run_id'],
                'already_running':    already_running,
            }

    def _serve_scanner_revalidate(self, script_name, output_name):
        """Synchronously re-run a scanner script against the latest snapshot.
        Used by both staging and today-breakout scanners. Both finish in ~3s.
        Returns the freshly written output JSON with _revalidated=True flag.
        """
        import subprocess as _sp, json as _json, time as _time
        script = os.path.join(BASE_DIR, script_name)
        out_path = os.path.join(BASE_DIR, output_name)
        t0 = _time.time()
        try:
            proc = _sp.run(['python3', script], cwd=BASE_DIR,
                           capture_output=True, timeout=90)
            elapsed = _time.time() - t0
            if not os.path.exists(out_path):
                self.send_response(500)
                self.send_header("Content-type", "application/json")
                self.end_headers()
                self.wfile.write(_json.dumps({
                    'error': 'scanner ran but produced no output file',
                    'script': script_name,
                    'returncode': proc.returncode,
                    'stderr': proc.stderr.decode('utf-8', errors='ignore')[-2000:],
                }).encode()); return
            with open(out_path) as f: payload = _json.load(f)
            self._append_prebreak_log({
                'event': 'revalidate',
                'script': script_name,
                'output': output_name,
                'snapshot': payload.get('snapshot_used'),
                'generated_utc': payload.get('generated_utc'),
                'buy_count': len(payload.get('buys') or []),
                'returncode': proc.returncode,
                'elapsed_sec': round(elapsed, 2),
            })
            payload['_revalidated'] = True
            payload['_elapsed_sec'] = round(elapsed, 2)
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps(payload, default=str).encode())
        except _sp.TimeoutExpired:
            self.send_response(504)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps({'error': 'scanner timed out (>90s)', 'script': script_name}).encode())
        except Exception as e:
            self.send_response(500)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            self.wfile.write(_json.dumps({'error': str(e), 'script': script_name}).encode())

    def _compute_decision(self, snapshot):
        """Honest decision engine: TRADE NOW / WAIT / NO TRADE for the given
        per-ticker flow snapshot. Synthesizes whale tape + OI + IV regime +
        smart money + news. Backed by the call/put backtest findings.

        Bull/bear scoring (each side scored 0-100, decision based on margin):
          whale verdict           +30 if matches direction
          OI verdict              +25 if matches
          smart money score >=3   +15
          sector firing           +10
          news + flow agreement   +10
          biggest alert >= $1M    +10

        IV regime: surfaces warning ("EXPENSIVE — sell premium") but doesn't kill the trade.
        PUT-side: appends regime alignment reminder (per put_backtest 84% vs 40%).
        Day-of-week: Friday gates to "no swing" — flagged in why."""
        whale = snapshot.get('whale_tape_24h') or {}
        intra = snapshot.get('intraday_signals') or {}
        daily = snapshot.get('daily_signals') or {}
        news  = snapshot.get('recent_news') or []
        llm   = snapshot.get('llm_news_bias') or {}
        v4a   = snapshot.get('v4_analysis') or {}
        earn  = snapshot.get('earnings_proximity') or {}
        tape  = snapshot.get('tape_pressure') or {}
        dpreg = snapshot.get('dp_regime') or {}
        esurp = snapshot.get('earnings_surprise') or {}
        regime = snapshot.get('regime') or {}
        sector = snapshot.get('sector_rotation') or {}

        whale_v = whale.get('verdict')
        oi_v    = intra.get('oi_verdict')
        iv_reg  = intra.get('iv_verdict') or ''
        iv_pct  = daily.get('iv_percentile')
        is_smart = daily.get('is_smart_money')
        sm_score = daily.get('smart_money_score') or 0

        # Ticker's intraday move (signed % from prior close). Sourced from V7
        # staging which carries today_pct on every scored ticker. Used by:
        #   • tape-veto block below (CALLs into red tape / PUTs into green tape)
        #   • conviction-direction null fallback (infer direction from sign)
        try:
            today_pct = float((v4a.get('staging') or {}).get('today_pct') or 0)
        except Exception:
            today_pct = 0.0
        sector_fire = daily.get('sector_fire_active')
        biggest = whale.get('biggest_alert_m') or daily.get('fa_biggest_alert_m') or 0

        # LLM news bias (Claude-scored polarity per ticker)
        llm_label = llm.get('label')                 # BULLISH/BEARISH/MIXED/NEUTRAL
        llm_score = llm.get('score') or 0            # decay-weighted polarity sum
        llm_mag   = llm.get('max_magnitude') or 0    # 1-5
        llm_active = llm.get('llm_active', False)

        bull_pts = 0; bull_why = []
        bear_pts = 0; bear_why = []
        # info_why = lines that are SHOWN regardless of which decision branch fires
        # (PEAD info, IV warning, etc.). Appended to final `why` after composition.
        info_why = []

        # 0) V4 IN-HOUSE ANALYSIS — our backtested edge, drives the verdict
        stg = v4a.get('staging') or {}
        bo  = v4a.get('breakout') or {}
        cv  = v4a.get('conviction') or {}

        # V7 staging — Sprint 3.9 makes it bidirectional (BULLISH or BEARISH)
        stg_direction = stg.get('direction', 'BULLISH')
        if stg.get('tier') == 'BUY':
            if stg_direction == 'BEARISH':
                bear_pts += 35
                bear_why.append(f'🎯 V7 PUT BUY: {stg.get("pattern","—")} (score {stg.get("score")})')
            else:
                bull_pts += 35
                bull_why.append(f'🎯 V7 staging BUY: {stg.get("pattern","—")} (score {stg.get("score")})')
        elif stg.get('tier') == 'WATCH':
            if stg_direction == 'BEARISH':
                bear_pts += 18
                bear_why.append(f'V7 PUT WATCH (score {stg.get("score")})')
            else:
                bull_pts += 18
                bull_why.append(f'V7 staging WATCH (score {stg.get("score")})')

        # GATE_D breakout (validated 65% recall, 94% precision at thr=30)
        if bo.get('tier') == 'STRONG':
            bull_pts += 30
            bull_why.append(f'🚀 GATE_D STRONG breakout fired')
        elif bo.get('tier') == 'CONFIRMED':
            bull_pts += 25
            bull_why.append(f'GATE_D CONFIRMED breakout')
        elif bo.get('tier') == 'READY':
            bull_pts += 15
            bull_why.append(f'GATE_D READY breakout')

        # Conviction — DIRECTION-AWARE (BULLISH or BEARISH) — gives us native PUT signals
        if cv:
            cdir = cv.get('direction')
            cscore = cv.get('score') or 0
            cmax = cv.get('max_score') or 8
            cpct = cscore / cmax if cmax else 0
            cverdict = cv.get('verdict')
            ccon = cv.get('confidence') or 0

            # Bug fix 2026-04-27: when conviction has verdict=BUY but direction=None,
            # the score was being silently dropped. Fall back to inferring direction
            # from today_pct sign — if the ticker is solidly green, treat as BULLISH;
            # solidly red, BEARISH; flat (<0.3% in either direction), still skip.
            if cdir not in ('BULLISH', 'BEARISH') and cverdict == 'BUY' and cpct >= 0.5:
                if today_pct >= 0.3:
                    cdir = 'BULLISH'
                    bull_why.append('  ↳ direction inferred from tape (today +%.2f%%)' % today_pct)
                elif today_pct <= -0.3:
                    cdir = 'BEARISH'
                    bear_why.append('  ↳ direction inferred from tape (today %.2f%%)' % today_pct)

            if cdir == 'BULLISH' and cpct >= 0.5:
                pts = min(35, int(cpct * 35) + (10 if cverdict == 'BUY' else 0))
                bull_pts += pts
                bull_why.append(f'👁 Conviction {cverdict} BULLISH ({cscore}/{cmax}, conf {ccon})')
            elif cdir == 'BEARISH' and cpct >= 0.5:
                pts = min(35, int(cpct * 35) + (10 if cverdict == 'BUY' else 0))
                bear_pts += pts
                bear_why.append(f'👁 Conviction {cverdict} BEARISH ({cscore}/{cmax}, conf {ccon})')

        # 1) LLM news bias — DELIBERATELY NOT SCORED INTO bull/bear pts.
        # Reason: V7/GATE_D/conviction are backtested edges (60-94% precision).
        # LLM news bias is unvalidated — adding it would dilute the proven signals.
        # LLM bias is used ONLY as: (a) FADE gate (conflict with flow), (b) shown
        # on /catalyst page as context. We log (bias, V7, outcome) to validate
        # LLM as a scorer in ~4 weeks once we have N>=50 ground truth samples.

        # 2) Whale tape verdict
        # 2026-04-27: graded by strike × DTE quality. Old behavior gave a flat
        # +30 for ANY bullish/bearish verdict — even when the underlying premium
        # was 95% weekly OTM lotto. Now: scale points by weighted_premium. Cap
        # at +30 (same as before for top-quality), floor at +10 for verdict-only
        # signals where the weighted premium is small. Backward-compatible: if
        # collector is on the old version (no weighted_premium fields), behave
        # exactly like before.
        call_w_prem = whale.get('call_weighted_premium_m')
        put_w_prem  = whale.get('put_weighted_premium_m')
        call_raw_prem = whale.get('call_premium_m') or 0
        put_raw_prem  = whale.get('put_premium_m')  or 0

        def _whale_pts(weighted_m, raw_m, side_label):
            """Scale weighted premium → points. ≥$2M weighted = +30 (full),
            $0.5M = +20, ≥$0.1M = +12, anything = +10. Old fallback if collector
            hasn't been re-run yet: just give +30 like before."""
            if weighted_m is None:
                return 30, f'⚡ Whale tape {side_label} (calls/puts dominant)'
            wm = float(weighted_m or 0)
            rm = float(raw_m or 0)
            if wm >= 2.0:
                return 30, f'⚡ Whale tape {side_label}: ${rm:.2f}M raw / ${wm:.2f}M quality-weighted (ITM/leap conviction)'
            if wm >= 0.5:
                return 22, f'⚡ Whale tape {side_label}: ${rm:.2f}M raw / ${wm:.2f}M quality-weighted'
            if wm >= 0.1:
                return 14, f'⚡ Whale tape {side_label}: ${rm:.2f}M raw / ${wm:.2f}M quality-weighted (mostly ATM/short-dated)'
            return 10, f'⚡ Whale tape {side_label}: ${rm:.2f}M raw / ${wm:.2f}M quality-weighted (low conviction — mostly weekly OTM)'

        if whale_v == 'BULLISH':
            pts, msg = _whale_pts(call_w_prem, call_raw_prem, 'bullish')
            bull_pts += pts; bull_why.append(msg)
        elif whale_v == 'BEARISH':
            pts, msg = _whale_pts(put_w_prem, put_raw_prem, 'bearish')
            bear_pts += pts; bear_why.append(msg)

        # 3) OI verdict
        if oi_v == 'BULLISH':
            bull_pts += 25; bull_why.append('📊 Today OI building on call side')
        elif oi_v == 'BEARISH':
            bear_pts += 25; bear_why.append('📊 Today OI building on put side')

        # 5) TAPE PRESSURE — UNSCORED 2026-04-25 per user instruction.
        # Pending validation backtest. Data is still collected + shown on catalyst
        # page as INFORMATIONAL ONLY (entry-timing context, not a verdict driver).
        # Variables kept (tape_dir, tape_strength) so they can still be referenced
        # in the convergence flag check below — but no bull/bear pts assigned.
        tape_dir = tape.get('pressure_direction')
        tape_strength = tape.get('pressure_strength')
        tape_delta_m = tape.get('delta_premium_m') or 0

        # 5b1) PRE-EARNINGS PREMIUM OVERPRICED (NEW 2026-04-25, simplified per user)
        # IV pumps 5-10d before earnings. Naked options here are overpriced by 30-50%.
        # Fade the trade entirely when high-IV pump is active. Watch-tier just warns.
        pe_tier = esurp.get('pre_earnings_tier')
        d2e_warn = esurp.get('days_until_next_report')
        iv_pct_warn = daily.get('iv_percentile') or 0
        pe_warning = None
        pe_confidence_penalty = 0
        pe_fade_active = False  # NEW: triggers verdict override below
        if pe_tier == 'PRE_EARNINGS_HIGH_IV' and iv_pct_warn > 0.80:
            pe_warning = f'⚠ PRE-EARNINGS PREMIUM OVERPRICED ({d2e_warn}d, IV {int(iv_pct_warn*100)}th pct) — fade'
            pe_confidence_penalty = 15
            pe_fade_active = True  # ← FADE the trade
        elif pe_tier == 'PRE_EARNINGS_HIGH_IV':
            pe_warning = f'📅 PRE-EARNINGS {d2e_warn}d — verify IV before entering naked'
            pe_confidence_penalty = 8
        elif pe_tier == 'PRE_EARNINGS_WATCH':
            pe_warning = f'📅 Earnings in {d2e_warn}d — IV may pump further'
            pe_confidence_penalty = 5
        if pe_warning: info_why.append(pe_warning)

        # 5b) PEAD — UNSCORED 2026-04-25 by validation backtest.
        # Sample too small (n=4-7 in PEAD window across 135 historical signals).
        # Academic priors say PEAD works (Bernard & Thomas 1989), but our N is
        # insufficient to confirm. Surface as INFO only via earnings_proximity field;
        # add scoring weight after 30+ days of accumulated PEAD-window signals.
        if esurp.get('in_pead_window'):
            outcome = esurp.get('last_outcome')
            d_since = esurp.get('days_since_last_report') or 0
            surp_pct = esurp.get('last_surprise_pct') or 0
            # Informational why-line only — no bull/bear pts assigned
            if outcome in ('BIG_BEAT', 'BIG_MISS', 'BEAT', 'MISS'):
                # Add to info_why so it survives every decision branch (NO_TRADE, WAIT NONE, etc.)
                info_why.append(f'ℹ PEAD window: {outcome} (surp {surp_pct:+.1f}%, day +{d_since}) — pending validation, info only')

        # 5d) CATALYST CONVERGENCE (Sprint 3.18) — 3+ catalysts in 24h is the setup multiplier
        # Catalysts counted: V7 BUY/WATCH, GATE_D fired, conviction BUY, whale alerts ≥3,
        # OI shift directional, news count ≥5, PEAD active, DP DOMINANT, tape STRONG.
        catalyst_flags = []
        if (stg.get('tier') in ('BUY', 'WATCH')): catalyst_flags.append('V7')
        if (bo.get('tier') in ('STRONG', 'CONFIRMED', 'READY')): catalyst_flags.append('GATE_D')
        if cv.get('verdict') == 'BUY': catalyst_flags.append('CONV_BUY')
        if (whale.get('alerts_n') or 0) >= 3: catalyst_flags.append(f'WHALE×{whale.get("alerts_n")}')
        if oi_v in ('BULLISH', 'BEARISH'): catalyst_flags.append(f'OI_{oi_v[0]}')
        if len(news) >= 5: catalyst_flags.append(f'NEWS×{len(news)}')
        if esurp.get('in_pead_window'): catalyst_flags.append(f'PEAD_{esurp.get("last_outcome")}')
        if dpreg.get('dp_regime') == 'DOMINANT': catalyst_flags.append('DP_DOMINANT')
        # NOTE: tape_pressure removed from convergence flags 2026-04-25 — unvalidated

        catalyst_count = len(catalyst_flags)
        # 3+ converging catalysts = setup multiplier (per catalyst research)
        if catalyst_count >= 3:
            # Multiplier weighted to leading side
            mult_pts = min(catalyst_count * 5, 25)  # cap +25
            if bull_pts >= bear_pts:
                bull_pts += mult_pts
                bull_why.append(f'⚡ CONVERGENCE ×{catalyst_count} ({", ".join(catalyst_flags[:5])}) → setup multiplier +{mult_pts}')
            else:
                bear_pts += mult_pts
                bear_why.append(f'⚡ CONVERGENCE ×{catalyst_count} ({", ".join(catalyst_flags[:5])}) → setup multiplier +{mult_pts}')

        # 5c) MARKET REGIME ALIGNMENT (Sprint 3.13)
        # CORRECTED 2026-04-25 by signal_validator backtest (n=135):
        #   PUT side: SPY-aligned WORKS (lift 1.47x on n=19, 84% vs 57% baseline) ✅
        #   CALL side: SPY-aligned HURTS (lift 0.64x on n=33, 46% vs 70% baseline) ❌
        # Conclusion: only score PUT regime alignment. CALLs already select well enough.
        if regime:
            spy_dp = regime.get('spy_day_pct') or 0
            if bear_pts > bull_pts and bear_pts >= 30:
                if regime.get('put_alignment_active'):
                    bear_pts += 15
                    bear_why.append(f'📉 SPY-aligned ({spy_dp:+.2f}% today) → PUT regime confirmed (lift 1.47x backtest n=19)')
                elif spy_dp >= 0.5:
                    bear_pts -= 10
                    bear_why.append(f'⚠ Fighting trend (SPY {spy_dp:+.2f}% green) → PUT discount (40% win rate vs 84% aligned)')

        # 5e) SECTOR ROTATION (Sprint 3.16)
        # CORRECTED 2026-04-25 by signal_validator backtest (n=64):
        #   PUT in LAGGING sector: lift 1.43x on n=11 (82% vs 57% baseline) ✅
        #   CALL in LEADING sector: lift 0.76x on n=28 (54% vs 70% baseline) ❌
        # Hot-sector CALLs are crowded → mean-reversion risk. Only score PUT side.
        sec_tier = sector.get('sector_tier')
        if sec_tier == 'LAGGING' and bear_pts > bull_pts and bear_pts >= 30:
            bear_pts += 5
            bear_why.append(f'📉 Sector LAGGING ({sector.get("sector")} {sector.get("sector_pct_5d","—")}% 5d) — lift 1.43x backtest')

        # 6) DARK POOL REGIME (Sprint 1.3) — institutional accumulation context
        # DOMINANT (≥40% of day vol) + bullish flow already present = high conviction
        # Don't add direction; just AMPLIFY existing edge
        dp_pct = dpreg.get('dp_pct_of_volume') or 0
        if dpreg.get('dp_regime') == 'DOMINANT' and (bull_pts > 30 or bear_pts > 30):
            if bull_pts > bear_pts:
                bull_pts += 10
                bull_why.append(f'🌊 Dark pool DOMINANT ({dp_pct*100:.0f}% of day vol) — institutional positioning')
            else:
                bear_pts += 10
                bear_why.append(f'🌊 Dark pool DOMINANT ({dp_pct*100:.0f}% of day vol) — institutional distribution')

        # 4) NEWS↔FLOW CONFLICT — the highest-value signal (smart money fades the news)
        news_flow_fade = None
        if llm_label == 'BULLISH' and whale_v == 'BEARISH':
            news_flow_fade = ('FADE_BULL_NEWS',
                'News bullish but whales buying PUTS — smart money fading the headline')
        elif llm_label == 'BEARISH' and whale_v == 'BULLISH':
            news_flow_fade = ('FADE_BEAR_NEWS',
                'News bearish but whales buying CALLS — smart money fading the headline')

        if is_smart and sm_score >= 3:
            # Smart money flag is direction-agnostic; credit whichever side is leading
            if bull_pts > bear_pts:
                bull_pts += 15; bull_why.append(f'Smart money score {sm_score} (institutional positioning)')
            elif bear_pts > 0:
                bear_pts += 15; bear_why.append(f'Smart money score {sm_score} (institutional positioning)')

        if sector_fire:
            if bull_pts > bear_pts:
                bull_pts += 10; bull_why.append('Sector firing (multi-name move)')
            elif bear_pts > 0:
                bear_pts += 10; bear_why.append('Sector firing (multi-name move)')

        if biggest >= 1.0:
            if bull_pts >= bear_pts:
                bull_pts += 10; bull_why.append(f'Biggest whale alert ${biggest:.1f}M (size matters)')
            else:
                bear_pts += 10; bear_why.append(f'Biggest whale alert ${biggest:.1f}M (size matters)')

        # Note: removed "news + flow alignment" bonus — it was scoring a proxy
        # (raw news count) without checking news polarity. With LLM scoring on,
        # we have real polarity but choose NOT to score it (see comment above).
        # News count is shown on the page as context only.

        # IV regime — warning, not a kill
        iv_warn = None
        if 'EXPENSIVE' in iv_reg:
            iv_warn = '⚠ IV expensive (>30% above RV) → use spread, not naked'
        if iv_pct and iv_pct > 0.9:
            iv_warn = (iv_warn or '⚠') + f' · IV at {int(iv_pct*100)}th pct (extreme — premium rich)'

        # Detect market-closed state — affects how we frame the decision
        try:
            now_local = datetime.now()
            is_weekend = now_local.weekday() >= 5
            hm = now_local.hour * 60 + now_local.minute
            is_after_close = (hm >= 13*60) or (hm < 6*60+15)
            market_closed = is_weekend or is_after_close
        except Exception:
            market_closed = False

        # Compose decision
        margin = abs(bull_pts - bear_pts)
        max_pts = max(bull_pts, bear_pts)

        # EARNINGS GATE — don't swing into earnings within 48h (timing only, not direction)
        # Earnings prints can move ±10-30% — completely overwhelms our signals.
        # Override any TRADE_NOW with WAIT or NO_TRADE if earnings imminent.
        earnings_gate_msg = None
        d_to_earn = earn.get('days_until_earnings')
        if d_to_earn is not None:
            if 0 <= d_to_earn <= 2:
                earnings_gate_msg = f'⚠ Earnings in {d_to_earn}d — DO NOT swing through. Wait for post-earnings.'
            elif -3 <= d_to_earn < 0:
                earnings_gate_msg = f'📊 Earnings reported {abs(d_to_earn)}d ago — post-earnings drift window (PEAD)'

        # FADE check overrides any TRADE_NOW — smart money disagreement is a kill switch
        if news_flow_fade:
            decision = 'NO_TRADE'
            direction = 'NONE'
            confidence = 0
            why = [f'⚠ FADE: {news_flow_fade[1]}.',
                   'When the tape contradicts the headline, the headline is wrong.',
                   'Skip — or trade the opposite side only with extra confirmation.']
            if iv_warn: why.append(iv_warn)
            return {
                'decision': decision, 'direction': direction,
                'confidence': confidence, 'bull_pts': bull_pts, 'bear_pts': bear_pts,
                'why': why, 'suggested_action': None,
                'fade_signal': news_flow_fade[0],
            }

        # ─────────────────────────────────────────────────────────────
        # TAPE VETO (added 2026-04-27)
        # The rules engine was firing CALLs on red tape and PUTs on green
        # tape because UW flow + V7 score weren't filtered against the
        # underlying's intraday direction. Fix: when the implied direction
        # is fighting the tape by ≥1.5%, demote TRADE_NOW → WAIT.
        # Threshold 1.5% chosen to:
        #   • allow normal CALL setups when ticker is flat-to-down 1% (mean revert)
        #   • block CALLs when ticker is materially red (clear sell pressure)
        #   • symmetric for PUTs
        # ─────────────────────────────────────────────────────────────
        tape_veto_msg = None
        if max_pts >= 50 and margin >= 30:
            implied_dir = 'CALL' if bull_pts > bear_pts else 'PUT'
            if implied_dir == 'CALL' and today_pct <= -1.5:
                tape_veto_msg = f'🚫 Tape veto: CALL setup but ticker is {today_pct:+.2f}% red — wait for tape to flip'
            elif implied_dir == 'PUT' and today_pct >= 1.5:
                tape_veto_msg = f'🚫 Tape veto: PUT setup but ticker is {today_pct:+.2f}% green — wait for tape to flip'

        if max_pts >= 50 and margin >= 30 and not tape_veto_msg:
            # High conviction one-sided — only TRADE_NOW if market is open
            direction = 'CALL' if bull_pts > bear_pts else 'PUT'
            why = bull_why if direction == 'CALL' else bear_why
            if direction == 'PUT':
                why.append('⚠ PUT requires SPY also down (regime aligned wins 84% vs 40% unaligned)')
            if market_closed:
                decision = 'WAIT'
                confidence = min(85, max_pts)
                why = ['🗓 Market closed — verdict will activate at next open'] + why
            else:
                decision = 'TRADE_NOW'
                confidence = min(95, max_pts)
        elif max_pts >= 50 and margin >= 30 and tape_veto_msg:
            # TRADE_NOW would have fired but the tape disagrees — demote to WAIT
            decision = 'WAIT'
            direction = 'CALL' if bull_pts > bear_pts else 'PUT'
            confidence = min(60, max_pts - 20)  # haircut for tape mismatch
            why = [tape_veto_msg] + (bull_why if direction == 'CALL' else bear_why)
        elif bull_pts >= 30 and bear_pts >= 30:
            decision = 'NO_TRADE'
            direction = 'NONE'
            confidence = 0
            why = ['Conflicting signals — bulls and bears both active. Skip.']
        elif max_pts >= 30:
            # Some signal but not enough
            decision = 'WAIT'
            direction = 'CALL' if bull_pts > bear_pts else 'PUT'
            confidence = max_pts
            why = (bull_why if direction == 'CALL' else bear_why)
            why = why + ['Not enough confirmation — watch for stronger flow before entering']
        else:
            # No flow signal — but check if there's news + IV context worth surfacing
            news_count = len(news)
            iv_pct_val = iv_pct or 0
            if news_count >= 5 and market_closed:
                decision = 'WAIT'
                direction = 'NONE'
                confidence = 25
                why = [
                    f'📰 {news_count} fresh headlines but markets closed.',
                    'Flow data will refresh at open — re-check then for direction.',
                ]
                if iv_pct_val > 0.85:
                    why.append(f'⚠ IV at {int(iv_pct_val*100)}th pct — premium will be rich on open')
                if is_smart and sm_score >= 3:
                    why.append(f'Smart money score {sm_score} on yesterday close — pre-positioned')
            elif news_count >= 5 and not market_closed:
                decision = 'WAIT'
                direction = 'NONE'
                confidence = 30
                why = [
                    f'📰 {news_count} recent headlines but no flow confirmation yet.',
                    'Watch the whale tape — institutional follow-through is the trigger.',
                ]
            else:
                decision = 'NO_TRADE'
                direction = 'NONE'
                confidence = 0
                why = ['No signal on this ticker — ticker is quiet, no edge to trade.']

        if iv_warn and iv_warn not in why:
            why.append(iv_warn)
        # Always append info-only context (PEAD, pre-earnings, etc.) regardless of branch
        for line in info_why:
            if line not in why:
                why.append(line)

        # Apply pre-earnings IV-pump confidence penalty (Sprint 2026-04-25)
        if pe_confidence_penalty and confidence > 0:
            confidence = max(0, confidence - pe_confidence_penalty)

        # PRE-EARNINGS FADE GATE (NEW 2026-04-25 per user) — when 3-5d to earnings + IV
        # already pumped to 80th+ percentile, fade the trade. Premium is overpriced;
        # paying 30-50% inflated premium is a worse expected value than waiting for
        # post-earnings IV crush. Override TRADE_NOW → NO_TRADE with FADE label.
        if pe_fade_active and decision in ('TRADE_NOW', 'WAIT'):
            decision = 'NO_TRADE'
            direction = 'NONE'
            confidence = 0
            why = ['⚠ FADE: pre-earnings premium overpriced — wait for IV crush post-earnings'] + why
            # Tag the fade reason so the page can render distinct label
            news_flow_fade = ('FADE_PREEARNINGS', 'pre-earnings premium overpriced')

        # EARNINGS GATE — kill TRADE_NOW if earnings within 48h (it'll print, your direction call doesn't matter)
        if earnings_gate_msg and decision == 'TRADE_NOW' and d_to_earn is not None and 0 <= d_to_earn <= 2:
            decision = 'NO_TRADE'
            direction = 'NONE'
            confidence = 0
            why = [earnings_gate_msg, 'Original verdict overridden by earnings gate.'] + why
        elif earnings_gate_msg:
            why.append(earnings_gate_msg)

        # Friday-specific override (only if market open and we said TRADE_NOW)
        try:
            now_local = datetime.now()
            if now_local.weekday() == 4 and decision == 'TRADE_NOW':
                why.append('🗓 Friday — no swing entries; consider 0DTE or pass')
        except Exception: pass

        # Suggested action — just the generic line; the entry/exit math lives on /today
        suggested_action = None
        if decision == 'TRADE_NOW':
            if direction == 'CALL':
                suggested_action = 'ATM weekly CALL — see /today for size + premium math'
            else:
                suggested_action = 'ATM weekly PUT — see /today for size + premium math'

        result = {
            'decision': decision,
            'direction': direction,
            'confidence': confidence,
            'bull_pts': bull_pts,
            'bear_pts': bear_pts,
            'why': why,
            'suggested_action': suggested_action,
            'catalyst_count': catalyst_count,
            'catalyst_flags': catalyst_flags,
            'has_convergence': catalyst_count >= 3,
            'pre_earnings_tier': pe_tier,
            'pre_earnings_warning': pe_warning,
        }
        # Log to backtest journal — every ticker_flow query writes a row.
        # In ~4 weeks we can join these against forward returns to validate:
        #   does llm_bias actually predict moves? does V7+conviction correlate?
        try:
            import json as _j
            log_path = os.path.join(BASE_DIR, 'v4_decision_log.jsonl')
            row = {
                'logged_utc': datetime.now(timezone.utc).isoformat(),
                'ticker': snapshot.get('ticker'),
                'decision': decision, 'direction': direction, 'confidence': confidence,
                'bull_pts': bull_pts, 'bear_pts': bear_pts,
                'llm_label': llm_label, 'llm_score': llm_score, 'llm_mag': llm_mag,
                'v7_tier': stg.get('tier'), 'v7_score': stg.get('score'),
                'breakout_tier': bo.get('tier'),
                'conviction_verdict': cv.get('verdict'), 'conviction_direction': cv.get('direction'),
                'whale_verdict': whale_v, 'oi_verdict': oi_v, 'iv_regime': iv_reg,
                'days_to_earnings': d_to_earn,
                'fade_signal': news_flow_fade[0] if news_flow_fade else None,
            }
            with open(log_path, 'a') as f: f.write(_j.dumps(row, default=str) + '\n')
        except Exception: pass
        return result


    def _serve_ticker_flow(self, ticker):
        """Per-ticker flow snapshot — joins whale tape + priority signals + DP +
        recent news + IV/RV. This is the "right pane" of /catalyst — when user
        clicks a ticker on a headline, this loads all the flow context for
        decision-making."""
        import json as _json, glob as _glob
        out = {
            'ticker': ticker,
            'generated_utc': datetime.now(timezone.utc).isoformat(),
            'sources_used': [],
        }

        # 1) Whale tape — last 24h
        try:
            with open(os.path.join(BASE_DIR, 'v4_flow_alerts.json')) as f:
                fa = _json.load(f)
            tk_data = (fa.get('per_ticker') or {}).get(ticker) or {}
            if tk_data:
                out['whale_tape_24h'] = {
                    'alerts_n':         tk_data.get('alerts_3d_n'),
                    'call_alerts_n':    tk_data.get('call_alerts_3d_n'),
                    'put_alerts_n':     tk_data.get('put_alerts_3d_n'),
                    'sweep_count':      tk_data.get('sweep_count_3d'),
                    'floor_count':      tk_data.get('floor_count_3d'),
                    'call_premium_m':   tk_data.get('call_premium_3d_m'),
                    'put_premium_m':    tk_data.get('put_premium_3d_m'),
                    'biggest_alert_m':  tk_data.get('biggest_alert_m'),
                    # Strike × DTE quality (added 2026-04-27)
                    'call_strike_breakdown_m':  tk_data.get('call_strike_breakdown_m'),
                    'put_strike_breakdown_m':   tk_data.get('put_strike_breakdown_m'),
                    'call_dte_breakdown_m':     tk_data.get('call_dte_breakdown_m'),
                    'put_dte_breakdown_m':      tk_data.get('put_dte_breakdown_m'),
                    'call_weighted_premium_m':  tk_data.get('call_weighted_premium_m'),
                    'put_weighted_premium_m':   tk_data.get('put_weighted_premium_m'),
                    'lookback_hours':   fa.get('lookback_hours', 24),
                    'asof':             fa.get('generated_utc'),
                }
                # Verdict heuristic
                ca = tk_data.get('call_alerts_3d_n') or 0
                pa = tk_data.get('put_alerts_3d_n') or 0
                if ca + pa > 0:
                    ratio = ca / (pa or 1)
                    if ratio >= 2:    verdict = 'BULLISH'
                    elif ratio <= 0.5: verdict = 'BEARISH'
                    else: verdict = 'MIXED'
                    out['whale_tape_24h']['verdict'] = verdict
                    out['whale_tape_24h']['call_put_ratio'] = round(ratio, 2)
                out['sources_used'].append('whale_tape')
        except Exception as e:
            out['whale_tape_error'] = str(e)

        # 2) Intraday priority signals — oi-change, gex, IV/RV
        try:
            with open(os.path.join(BASE_DIR, 'v4_priority_signals_intraday.json')) as f:
                ps = _json.load(f)
            tk_data = (ps.get('per_ticker') or {}).get(ticker) or {}
            if tk_data:
                out['intraday_signals'] = {
                    'oi_call_diff_sum':    tk_data.get('oi_call_diff_sum'),
                    'oi_put_diff_sum':     tk_data.get('oi_put_diff_sum'),
                    'oi_biggest_call_add': tk_data.get('oi_biggest_call_add'),
                    'oi_biggest_put_add':  tk_data.get('oi_biggest_put_add'),
                    'oi_strike_count':     tk_data.get('oi_strike_count'),
                    'gex_call_charm':      tk_data.get('gex_call_charm'),
                    'gex_call_delta':      tk_data.get('gex_call_delta'),
                    'gex_put_delta':       tk_data.get('gex_put_delta'),
                    'gex_call_vanna':      tk_data.get('gex_call_vanna'),
                    'gex_asof':            tk_data.get('gex_asof'),
                    'iv_latest':           tk_data.get('vr_latest_iv'),
                    'rv_latest':           tk_data.get('vr_latest_rv'),
                    'iv_rv_ratio':         tk_data.get('vr_iv_rv_ratio'),
                    'iv_cheap':            tk_data.get('vr_iv_cheap'),
                    'asof':                ps.get('generated_utc'),
                }
                # OI verdict
                cd = tk_data.get('oi_call_diff_sum') or 0
                pd = tk_data.get('oi_put_diff_sum') or 0
                if abs(cd) + abs(pd) > 0:
                    out['intraday_signals']['oi_verdict'] = (
                        'BULLISH' if cd > pd*1.5 else 'BEARISH' if pd > cd*1.5 else 'MIXED'
                    )
                # IV verdict — rich vs cheap
                ratio = tk_data.get('vr_iv_rv_ratio')
                if ratio is not None:
                    out['intraday_signals']['iv_verdict'] = (
                        'EXPENSIVE — sell premium' if ratio > 1.3 else
                        'CHEAP — buy premium' if ratio < 0.9 else
                        'FAIR'
                    )
                out['sources_used'].append('intraday_signals')
        except Exception as e:
            out['intraday_signals_error'] = str(e)

        # 3) Daily UW signals — dark pool, deeper aggregates
        try:
            files = sorted(_glob.glob(os.path.join(BASE_DIR, 'v4_uw_signals_*.json')))
            if files:
                with open(files[-1]) as f:
                    daily = _json.load(f)
                tk_data = (daily.get('per_ticker') or {}).get(ticker) or {}
                if tk_data:
                    out['daily_signals'] = {
                        'fa_alerts_n':         tk_data.get('fa_alerts_n'),
                        'fa_call_premium_m':   tk_data.get('fa_call_premium_total_m'),
                        'fa_biggest_alert_m':  tk_data.get('fa_biggest_alert_m'),
                        'iv_atm':              tk_data.get('iv_atm'),
                        'iv_percentile':       tk_data.get('iv_percentile'),
                        'gamma_compression':   tk_data.get('ge_gamma_compression'),
                        'is_coiled':           tk_data.get('is_coiled'),
                        'is_smart_money':      tk_data.get('is_smart_money'),
                        'smart_money_score':   tk_data.get('smart_money_score'),
                        'sector_fire_active':  tk_data.get('sector_fire_active'),
                        'asof':                daily.get('generated_utc') or daily.get('date'),
                    }
                    out['sources_used'].append('daily_signals')
        except Exception as e:
            out['daily_signals_error'] = str(e)

        # 4) Recent news on this ticker
        try:
            with open(os.path.join(BASE_DIR, 'v4_alpaca_news.json')) as f:
                news = _json.load(f)
            tk_news = (news.get('per_ticker') or {}).get(ticker) or []
            if tk_news:
                out['recent_news'] = tk_news[:10]
                out['sources_used'].append('alpaca_news')
        except Exception as e:
            out['news_error'] = str(e)

        # 4b) LLM news bias for this ticker (decay-weighted polarity from Claude scoring)
        try:
            with open(os.path.join(BASE_DIR, 'v4_news_llm_scored.json')) as f:
                llm = _json.load(f)
            tk_bias = (llm.get('per_ticker_bias') or {}).get(ticker)
            if tk_bias:
                out['llm_news_bias'] = {
                    'label':         tk_bias.get('llm_bias_label'),
                    'score':         tk_bias.get('llm_bias_score'),
                    'max_magnitude': tk_bias.get('max_magnitude'),
                    'news_count':    tk_bias.get('news_count'),
                    'top_items':     tk_bias.get('items', [])[:3],
                    'llm_active':    llm.get('llm_active', False),
                }
                if llm.get('llm_active'):
                    out['sources_used'].append('llm_bias')
        except Exception as e:
            out['llm_bias_error'] = str(e)

        # 4c) V4 IN-HOUSE ANALYSIS — V7 staging + GATE_D breakout + conviction
        # This is our actual edge. Drives the verdict more than LLM/news.
        v4_analysis = {}
        # V7 staging (Sprint 3.9 — now bidirectional: BULLISH or BEARISH)
        try:
            with open(os.path.join(BASE_DIR, 'v4_staging_signals.json')) as f:
                stg = _json.load(f)
            pool = ((stg.get('buys') or []) + (stg.get('put_buys') or []) + (stg.get('top_50') or []))
            for t in pool:
                if t.get('ticker') == ticker:
                    bull_s = t.get('score') or 0
                    put_s = t.get('put_score') or 0
                    # Pick whichever side scored higher (matches main() sort logic)
                    if bull_s >= put_s:
                        score = bull_s; direction = 'BULLISH'
                        pattern = t.get('pattern'); pattern_why = t.get('pattern_why')
                    else:
                        score = put_s; direction = 'BEARISH'
                        pattern = t.get('put_pattern'); pattern_why = t.get('put_pattern_why')
                    tier = ('BUY' if score >= 300 else 'WATCH' if score >= 250 else 'NORMAL')
                    v4_analysis['staging'] = {
                        'score': score, 'tier': tier,
                        'direction': direction,
                        'bullish_score': bull_s, 'bearish_score': put_s,
                        'pattern': pattern,
                        'pattern_why': pattern_why,
                        'today_pct': t.get('today_pct'),
                        'cum_3d': t.get('cum_3d'),
                        'iv_today': t.get('iv_today'),
                    }
                    break
        except Exception: pass
        # GATE_D breakout (bullish; tiers: strong > confirmed > ready > moderate)
        try:
            with open(os.path.join(BASE_DIR, 'v4_breakout_candidates.json')) as f:
                bo = _json.load(f)
            for tier in ['strong', 'confirmed', 'ready', 'moderate']:
                for c in (bo.get(tier) or []):
                    if c.get('ticker') == ticker:
                        v4_analysis['breakout'] = {
                            'tier': tier.upper(),
                            'score': c.get('score') or c.get('rank_score'),
                            'pattern': c.get('pattern'),
                        }
                        break
                if 'breakout' in v4_analysis: break
        except Exception: pass
        # Conviction (direction-aware: BULLISH or BEARISH)
        try:
            # Prefer v4_conviction_latest.json (single fresh pointer written every
            # time _serve_conviction runs). Fall back to dated files (excluding
            # v4_conviction_triggers.json which has different shape).
            latest_pointer = os.path.join(BASE_DIR, 'v4_conviction_latest.json')
            if os.path.exists(latest_pointer):
                files = [latest_pointer]
            else:
                import re as _re
                all_files = _glob.glob(os.path.join(BASE_DIR, 'v4_conviction_*.json'))
                files = sorted([f for f in all_files if _re.search(r'v4_conviction_\d{4}-\d{2}-\d{2}\.json$', f)])
            if files:
                with open(files[-1]) as f:
                    cv = _json.load(f)
                for r in (cv.get('results') or []):
                    if r.get('ticker') == ticker:
                        v4_analysis['conviction'] = {
                            'score':      r.get('score'),
                            'max_score':  r.get('max_score'),
                            'confidence': r.get('confidence'),
                            'verdict':    r.get('verdict'),
                            'direction':  r.get('direction'),  # BULLISH/BEARISH
                            'reasons':    (r.get('reasons') or [])[:3],
                        }
                        break
        except Exception: pass
        if v4_analysis:
            out['v4_analysis'] = v4_analysis
            out['sources_used'].append('v4_analysis')

        # 4cb) Tape pressure (Sprint 2.5 — Lee-Ready classified underlying tape)
        try:
            with open(os.path.join(BASE_DIR, 'v4_tape_pressure.json')) as f:
                tp = _json.load(f)
            tk_tp = (tp.get('per_ticker') or {}).get(ticker)
            if tk_tp:
                out['tape_pressure'] = {
                    **tk_tp,
                    'lookback_minutes': tp.get('lookback_minutes'),
                    'asof': tp.get('generated_utc'),
                }
                out['sources_used'].append('tape_pressure')
        except Exception: pass

        # 4cf) Sector rotation (Sprint 3.16)
        try:
            with open(os.path.join(BASE_DIR, 'v4_sector_rotation.json')) as f:
                sr = _json.load(f)
            tk_sec = (sr.get('per_ticker') or {}).get(ticker)
            if tk_sec:
                out['sector_rotation'] = {
                    **tk_sec,
                    'market_rotation': sr.get('rotation'),  # RISK_ON / NEUTRAL / RISK_OFF
                }
                out['sources_used'].append('sector_rotation')
        except Exception: pass

        # 4ce) Market regime (Sprint 3.13 — SPY/VIX context)
        try:
            with open(os.path.join(BASE_DIR, 'v4_regime.json')) as f:
                rg = _json.load(f)
            out['regime'] = {
                'regime': rg.get('regime'),
                'description': rg.get('regime_description'),
                'spy_day_pct': (rg.get('spy') or {}).get('day_pct'),
                'spy_pct_5d': (rg.get('spy') or {}).get('pct_5d'),
                'vix_level': (rg.get('vix') or {}).get('last_close'),
                'put_alignment_active': rg.get('put_alignment_active'),
                'call_alignment_active': rg.get('call_alignment_active'),
            }
            out['sources_used'].append('regime')
        except Exception: pass

        # 4cd) Earnings surprise + PEAD window (Sprint 2.8)
        try:
            with open(os.path.join(BASE_DIR, 'v4_earnings_surprise.json')) as f:
                es = _json.load(f)
            tk_es = (es.get('per_ticker') or {}).get(ticker)
            if tk_es:
                out['earnings_surprise'] = tk_es
                out['sources_used'].append('earnings_surprise')
        except Exception: pass

        # 4cc) Dark pool regime (Sprint 1.3 — derived from snapshot DP data)
        try:
            with open(os.path.join(BASE_DIR, 'v4_dp_regime.json')) as f:
                dpr = _json.load(f)
            tk_dp = (dpr.get('per_ticker') or {}).get(ticker)
            if tk_dp:
                out['dp_regime'] = tk_dp
                out['sources_used'].append('dp_regime')
        except Exception: pass

        # 4d) Earnings proximity (from breakout_history per-ticker earnings_days)
        # Tracks ~30 days backward; for each ticker, take the most recent record's
        # earnings_days value and adjust by days elapsed.
        try:
            from datetime import date as _date
            today_d = _date.today()
            best_days = None; best_record_date = None
            with open(os.path.join(BASE_DIR, 'v4_breakout_history.jsonl')) as f:
                for line in f:
                    try: r = _json.loads(line)
                    except Exception: continue
                    if r.get('ticker') != ticker: continue
                    rd = r.get('date')
                    edays = r.get('earnings_days')
                    if rd and edays is not None:
                        if best_record_date is None or rd > best_record_date:
                            best_record_date = rd
                            best_days = edays
            if best_days is not None and best_record_date:
                rd_obj = _date.fromisoformat(best_record_date)
                elapsed = (today_d - rd_obj).days
                adjusted = best_days - elapsed
                out['earnings_proximity'] = {
                    'days_until_earnings': adjusted,
                    'source_record_date': best_record_date,
                    'reported_at_record': best_days,
                }
                out['sources_used'].append('earnings_calendar')
        except Exception: pass

        # 5) Compose verdict (legacy two-source)
        whale_v = (out.get('whale_tape_24h') or {}).get('verdict')
        intra_v = (out.get('intraday_signals') or {}).get('oi_verdict')
        verdicts = [v for v in [whale_v, intra_v] if v]
        if verdicts:
            agree_bull = sum(1 for v in verdicts if v == 'BULLISH')
            agree_bear = sum(1 for v in verdicts if v == 'BEARISH')
            if agree_bull == len(verdicts):
                out['composite_verdict'] = 'CONFIRMED BULLISH'
            elif agree_bear == len(verdicts):
                out['composite_verdict'] = 'CONFIRMED BEARISH'
            elif agree_bull > 0 and agree_bear > 0:
                out['composite_verdict'] = 'CONFLICTED'
            else:
                out['composite_verdict'] = 'MIXED'

        # 6) DECISION ENGINE — actionable TRADE NOW / WAIT / NO TRADE
        out['decision'] = self._compute_decision(out)

        self.send_response(200)
        self.send_header("Content-type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(_json.dumps(out, default=str).encode())

    def _serve_json(self, path, missing_msg):
        if os.path.exists(path):
            self.send_response(200)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            with open(path, "rb") as f:
                self.wfile.write(f.read())
        else:
            self.send_response(404)
            self.send_header("Content-type", "application/json")
            self.end_headers()
            import json as _json
            self.wfile.write(_json.dumps({"error": missing_msg, "candidates": [], "signals": [], "metadata": {}}).encode())

if __name__ == "__main__":
    print(f"🚀 V4 Dashboard Server live")
    print(f"   v2 (ENHANCED, production):  http://localhost:{PORT}/v2/")
    print(f"   v1 (BASELINE, shadow):      http://localhost:{PORT}/v1/")
    server = ThreadingHTTPServer(('', PORT), V4DashboardServer)
    server.serve_forever()
