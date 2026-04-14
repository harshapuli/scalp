"""Profile where backtest time is spent."""
import time, numpy as np, pandas as pd
from datetime import datetime, timedelta
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockBarsRequest
from alpaca.data.timeframe import TimeFrame
from config_v2 import ALPACA_API_KEY, ALPACA_API_SECRET, UW_API_KEY
from unusual_whales import UnusualWhalesClient
from whale_tracker import WhaleTracker
from institutional_flow import InstitutionalFlowAnalyzer
from liquidity_map import LiquidityMapper
from institutional_signals import InstitutionalSignalGenerator
from whale_intent import WhaleIntentClassifier, ExecutionPlanner
import logging
logging.basicConfig(level=logging.WARNING)

# Fetch SPY data
client = StockHistoricalDataClient(api_key=ALPACA_API_KEY, secret_key=ALPACA_API_SECRET)
end = datetime.now()
start = end - timedelta(days=30)
req = StockBarsRequest(symbol_or_symbols='SPY', timeframe=TimeFrame.Minute, start=start, end=end, limit=50000)
bars = client.get_stock_bars(req)
df = bars.df
if isinstance(df.index, pd.MultiIndex):
    df = df.xs('SPY', level='symbol')
df = df.reset_index()
df['timestamp'] = pd.to_datetime(df['timestamp'])
df = df.sort_values('timestamp').reset_index(drop=True)

# Resample to 5min
df5 = df.set_index('timestamp').resample('5min').agg({
    'open':'first','high':'max','low':'min','close':'last','volume':'sum'
}).dropna().reset_index()

print(f"SPY: {len(df5)} 5min bars")

uw = UnusualWhalesClient(api_key=UW_API_KEY)
sig_gen = InstitutionalSignalGenerator(uw_client=uw)
flow = InstitutionalFlowAnalyzer(uw_client=uw)
whale = WhaleTracker(uw_client=uw)
whale.set_ticker('SPY')
flow.set_ticker('SPY')

window = df5.iloc[0:200].copy()

# Profile each scanner individually
print("\n=== INDIVIDUAL SCANNER TIMES (single call on 200 bars) ===")
scanners = [
    ('scan_inst_order_block', lambda: sig_gen.scan_inst_order_block(window, 'SPY', '5min')),
    ('scan_sweep_reversal', lambda: sig_gen.scan_sweep_reversal(window, 'SPY', '5min')),
    ('scan_smart_money_divergence', lambda: sig_gen.scan_smart_money_divergence(window, 'SPY', '5min')),
    ('scan_iceberg_fade', lambda: sig_gen.scan_iceberg_fade(window, 'SPY', '5min')),
    ('scan_whale_accumulation_entry', lambda: sig_gen.scan_whale_accumulation_entry(window, 'SPY', '5min')),
    ('scan_whale_exhaustion_fade', lambda: sig_gen.scan_whale_exhaustion_fade(window, 'SPY', '5min')),
    ('scan_whale_trap_reversal', lambda: sig_gen.scan_whale_trap_reversal(window, 'SPY', '5min')),
    ('scan_whale_divergence_reversal', lambda: sig_gen.scan_whale_divergence_reversal(window, 'SPY', '5min')),
]

times = {}
for name, fn in scanners:
    t0 = time.time()
    try:
        result = fn()
    except Exception as e:
        result = f"ERROR: {e}"
    elapsed = time.time() - t0
    times[name] = elapsed
    sigs = len(result) if isinstance(result, list) else 0
    print(f"  {name:40s}: {elapsed*1000:7.1f}ms | {sigs} signals")

# Profile scan_all_signals
t0 = time.time()
all_sigs = sig_gen.scan_all_signals(window, 'SPY', '5min')
all_time = time.time() - t0
print(f"\n  scan_all_signals (combined):             {all_time*1000:7.1f}ms | {len(all_sigs)} signals")

# Profile whale intent classification
t0 = time.time()
ohlcv_tracker = WhaleTracker()
ohlcv_tracker.get_whale_signal(window)
classifier = WhaleIntentClassifier()
wi = classifier.classify('SPY', whale_tracker=ohlcv_tracker)
whale_time = time.time() - t0
print(f"  whale_intent_classify:                    {whale_time*1000:7.1f}ms | {wi.state.value} {wi.confidence:.0f}%")

# Profile institutional state
t0 = time.time()
try:
    fs = flow.get_institutional_score(window)
except:
    fs = None
flow_time = time.time() - t0
print(f"  get_institutional_score:                  {flow_time*1000:7.1f}ms")

t0 = time.time()
try:
    ws = whale.get_whale_signal(window)
except:
    ws = None
whale_sig_time = time.time() - t0
print(f"  get_whale_signal:                         {whale_sig_time*1000:7.1f}ms")

# Now profile 10 consecutive scans to see if there's overhead
print("\n=== 10 CONSECUTIVE scan_all_signals CALLS ===")
total = 0
for i in range(10):
    idx = 200 + i * 10
    w = df5.iloc[max(0, idx-200):idx].copy()
    t0 = time.time()
    sigs = sig_gen.scan_all_signals(w, 'SPY', '5min')
    elapsed = time.time() - t0
    total += elapsed
    print(f"  Scan {i}: {elapsed*1000:7.1f}ms | {len(sigs)} signals")

print(f"  Average: {total/10*1000:.1f}ms per scan")
print(f"  Projected for 850 scans: {total/10*850:.1f}s = {total/10*850/60:.1f}min")

# Profile the FULL pipeline (scan + whale intent + filters)
print("\n=== FULL PIPELINE (scan + classify + filter) × 10 ===")
planner = ExecutionPlanner()
total_full = 0
for i in range(10):
    idx = 200 + i * 10
    w = df5.iloc[max(0, idx-200):idx].copy()
    t0 = time.time()

    # Scan
    sigs = sig_gen.scan_all_signals(w, 'SPY', '5min')

    # Whale intent
    ot = WhaleTracker()
    ot.get_whale_signal(w)
    cl = WhaleIntentClassifier()
    wi = cl.classify('SPY', whale_tracker=ot)

    # Flow score
    try:
        fs = flow.get_institutional_score(w)
    except:
        pass

    elapsed = time.time() - t0
    total_full += elapsed
    print(f"  Full pipeline {i}: {elapsed*1000:7.1f}ms")

print(f"  Average: {total_full/10*1000:.1f}ms per full pipeline")
print(f"  Projected for 850 scans: {total_full/10*850:.1f}s = {total_full/10*850/60:.1f}min")

# What % is each component
print(f"\n=== TIME BREAKDOWN ===")
scan_avg = total / 10
full_avg = total_full / 10
overhead = full_avg - scan_avg
print(f"  Signal scanning: {scan_avg*1000:.1f}ms ({scan_avg/full_avg*100:.0f}%)")
print(f"  Whale+Flow:      {overhead*1000:.1f}ms ({overhead/full_avg*100:.0f}%)")
sorted_times = sorted(times.items(), key=lambda x: -x[1])
print(f"\n  Top 3 slowest scanners:")
for name, t in sorted_times[:3]:
    print(f"    {name}: {t*1000:.1f}ms")
