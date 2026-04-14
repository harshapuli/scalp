"""
Diagnostic: Run the backtest pipeline but log EVERY signal (passed + filtered)
with the exact filter reason, whale state, confidence, and what P&L it WOULD
have made if it traded. This tells us exactly what the gates are blocking.
"""
import sys, json, logging, numpy as np, pandas as pd
from datetime import datetime, timedelta
from collections import defaultdict

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame

from config_v2 import ALPACA_API_KEY, ALPACA_API_SECRET, TICKERS, UW_API_KEY
from whale_tracker import WhaleTracker
from institutional_flow import InstitutionalFlowAnalyzer
from liquidity_map import LiquidityMapper
from institutional_signals import InstitutionalSignalGenerator
from whale_intent import WhaleIntentClassifier, ExecutionPlanner, WhaleState

logging.basicConfig(level=logging.WARNING)  # Quiet — we'll print our own

client = StockHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_API_SECRET)

def get_bars(ticker, days=30):
    end = datetime.now()
    start = end - timedelta(days=days)
    req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=TimeFrame.Minute,
                           start=start, end=end, limit=50000)
    bars = client.get_stock_bars(req)
    if hasattr(bars, 'df'):
        df = bars.df
        if isinstance(df.index, pd.MultiIndex):
            df = df.xs(ticker, level='symbol')
        df = df.reset_index()
        if 'timestamp' in df.columns:
            df['timestamp'] = pd.to_datetime(df['timestamp'])
            df = df.sort_values('timestamp').reset_index(drop=True)
        return df
    return pd.DataFrame()

def resample_5min(df):
    if df.empty:
        return df
    df = df.set_index('timestamp')
    r = df.resample('5min').agg({'open':'first','high':'max','low':'min','close':'last','volume':'sum'}).dropna()
    return r.reset_index()

def simulate_pnl(signal, full_df, start_idx):
    """Walk forward to find what P&L this signal WOULD have made."""
    entry = signal.entry_price
    sl = signal.stop_loss
    tp1 = signal.tp1
    if not all([entry, sl, tp1]):
        return None, None
    is_long = signal.direction in ('CALL', 'LONG')
    max_bars = 100
    end_idx = min(start_idx + max_bars, len(full_df))
    for i in range(start_idx + 1, end_idx):
        bar = full_df.iloc[i]
        if is_long:
            if bar['low'] <= sl:
                return ((sl - entry) / entry) * 100, 'stop_loss'
            if bar['high'] >= tp1:
                return ((tp1 - entry) / entry) * 100, 'tp1'
        else:
            if bar['high'] >= sl:
                return ((entry - sl) / entry) * 100, 'stop_loss'
            if bar['low'] <= tp1:
                return ((entry - tp1) / entry) * 100, 'tp1'
    # Timeout — mark to market
    last = full_df.iloc[end_idx - 1]['close']
    if is_long:
        return ((last - entry) / entry) * 100, 'timeout'
    else:
        return ((entry - last) / entry) * 100, 'timeout'

# Collect all signals with diagnostics
all_signals = []

for ticker in TICKERS:
    print(f"Processing {ticker}...")
    df_1m = get_bars(ticker, days=30)
    if df_1m.empty:
        continue
    df_5m = resample_5min(df_1m)
    if len(df_5m) < 200:
        continue

    flow_analyzer = InstitutionalFlowAnalyzer()
    liq_mapper = LiquidityMapper()
    sig_gen = InstitutionalSignalGenerator(flow_analyzer, liq_mapper)

    window_size = 200
    step = 5

    for idx in range(window_size, len(df_5m) - 100, step):
        window = df_5m.iloc[idx - window_size:idx].copy()
        signals = sig_gen.scan_all_signals(window, ticker, '5min')
        if not signals:
            continue

        for sig in signals:
            # Get filter results
            flow_score = 50  # default

            # Whale intent
            try:
                ohlcv_tracker = WhaleTracker()
                ohlcv_tracker.get_whale_signal(window)
                classifier = WhaleIntentClassifier()
                whale_intent = classifier.classify(ticker, whale_tracker=ohlcv_tracker)
                planner = ExecutionPlanner()
                setup_type = planner.classify_ict_setup(sig.signal_type)
                plan = planner.get_plan(setup_type, whale_intent, sig.direction)

                whale_state = whale_intent.state.value
                confidence = whale_intent.confidence
                exec_grade = plan.grade.value
                should_trade = plan.should_trade
                plan_reason = plan.reason
                size_mult = plan.size_multiplier
            except Exception as e:
                whale_state = 'ERROR'
                confidence = 0
                exec_grade = '?'
                should_trade = True
                plan_reason = str(e)
                size_mult = 1.0

            # Simulate P&L regardless of filter
            pnl, exit_reason = simulate_pnl(sig, df_5m, idx)

            # Determine which filter would block
            blocked_by = None
            if not should_trade:
                blocked_by = f"whale_intent({plan_reason})"
            elif confidence < 40 and whale_state == 'ACCUMULATION':
                blocked_by = f"conf_block(ACCUM {confidence:.0f}% < 40%)"
            elif confidence < 25 and whale_state == 'DISTRIBUTION':
                blocked_by = f"conf_block(DIST {confidence:.0f}% < 25%)"
            elif confidence < 20 and whale_state == 'AGGRESSION':
                blocked_by = f"conf_block(AGG {confidence:.0f}% < 20%)"

            all_signals.append({
                'ticker': ticker,
                'signal_type': sig.signal_type,
                'direction': sig.direction,
                'conviction': sig.conviction,
                'whale_state': whale_state,
                'confidence': confidence,
                'exec_grade': exec_grade,
                'should_trade': should_trade,
                'blocked_by': blocked_by,
                'would_pass': blocked_by is None,
                'pnl': pnl,
                'exit_reason': exit_reason,
                'size_mult': size_mult,
            })

# Analysis
df = pd.DataFrame(all_signals)
print(f"\n{'='*80}")
print(f"FILTER DIAGNOSTIC — ALL {len(df)} SIGNALS")
print(f"{'='*80}")

passed = df[df['would_pass'] == True]
blocked = df[df['would_pass'] == False]

print(f"\nPassed: {len(passed)} | Blocked: {len(blocked)}")
print(f"\n--- PASSED signals P&L ---")
if len(passed) > 0:
    for _, r in passed.iterrows():
        pnl_str = f"{r['pnl']:+.2f}%" if r['pnl'] is not None else "N/A"
        print(f"  {r['ticker']:5s} {r['signal_type']:20s} {r['direction']:4s} | "
              f"whale={r['whale_state']:15s} conf={r['confidence']:5.1f}% | "
              f"grade={r['exec_grade']:3s} | P&L={pnl_str}")

print(f"\n--- BLOCKED signals (what we're missing) ---")
if len(blocked) > 0:
    # Group by block reason
    for reason in blocked['blocked_by'].unique():
        subset = blocked[blocked['blocked_by'] == reason]
        pnls = [r for r in subset['pnl'] if r is not None]
        wins = [p for p in pnls if p > 0]
        losses = [p for p in pnls if p < 0]
        total_pnl = sum(pnls) if pnls else 0
        wr = len(wins) / len(pnls) * 100 if pnls else 0
        pf = sum(wins) / abs(sum(losses)) if losses else float('inf')
        pf_str = f"{pf:.2f}" if pf != float('inf') else "INF"

        print(f"\n  Block reason: {reason}")
        print(f"    Trades: {len(subset)} | WR: {wr:.0f}% | Total P&L: {total_pnl:+.2f}% | PF: {pf_str}")

        for _, r in subset.iterrows():
            pnl_str = f"{r['pnl']:+.2f}%" if r['pnl'] is not None else "N/A"
            print(f"      {r['ticker']:5s} {r['signal_type']:20s} {r['direction']:4s} | "
                  f"whale={r['whale_state']:15s} conf={r['confidence']:5.1f}% | P&L={pnl_str}")

# Summary table: if we let ALL blocked trades through
print(f"\n{'='*80}")
print(f"WHAT-IF ANALYSIS")
print(f"{'='*80}")
all_pnls = [r for r in df['pnl'] if r is not None]
all_wins = [p for p in all_pnls if p > 0]
all_losses = [p for p in all_pnls if p < 0]
if all_pnls:
    print(f"ALL signals (no filter): {len(all_pnls)} trades, "
          f"WR={len(all_wins)/len(all_pnls)*100:.0f}%, "
          f"P&L={sum(all_pnls):+.2f}%, "
          f"PF={sum(all_wins)/abs(sum(all_losses)):.2f}" if all_losses else "INF")

passed_pnls = [r for r in passed['pnl'] if r is not None]
passed_wins = [p for p in passed_pnls if p > 0]
passed_losses = [p for p in passed_pnls if p < 0]
if passed_pnls:
    pf = sum(passed_wins) / abs(sum(passed_losses)) if passed_losses else float('inf')
    print(f"Current V3.1  : {len(passed_pnls)} trades, "
          f"WR={len(passed_wins)/len(passed_pnls)*100:.0f}%, "
          f"P&L={sum(passed_pnls):+.2f}%, PF={pf:.2f}")

blocked_pnls = [r for r in blocked['pnl'] if r is not None]
blocked_wins = [p for p in blocked_pnls if p > 0]
blocked_losses = [p for p in blocked_pnls if p < 0]
if blocked_pnls:
    pf = sum(blocked_wins) / abs(sum(blocked_losses)) if blocked_losses else float('inf')
    print(f"Blocked only  : {len(blocked_pnls)} trades, "
          f"WR={len(blocked_wins)/len(blocked_pnls)*100:.0f}%, "
          f"P&L={sum(blocked_pnls):+.2f}%, PF={pf:.2f}")

# Per signal type: what's being blocked
print(f"\n--- Per signal type: passed vs blocked ---")
for st in df['signal_type'].unique():
    st_passed = passed[passed['signal_type'] == st]
    st_blocked = blocked[blocked['signal_type'] == st]
    sp = [r for r in st_passed['pnl'] if r is not None]
    sb = [r for r in st_blocked['pnl'] if r is not None]
    print(f"  {st:20s}: passed={len(sp):3d} (P&L={sum(sp):+.2f}%) | "
          f"blocked={len(sb):3d} (P&L={sum(sb):+.2f}%)")

# Save for further analysis
df.to_json('/tmp/filter_diagnostic.json', orient='records', indent=2)
print(f"\nFull data saved to /tmp/filter_diagnostic.json")
