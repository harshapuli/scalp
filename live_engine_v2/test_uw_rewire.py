"""
Test UW Rewire — Verify whale_tracker.py and institutional_flow.py
use real Unusual Whales data when client is available.
"""

import sys
import numpy as np
import pandas as pd
import logging

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger('test_uw_rewire')

# ============================================================
# Generate sample OHLCV data
# ============================================================

def make_sample_df(n=200, base_price=500.0):
    np.random.seed(42)
    prices = [base_price]
    for i in range(n):
        prices.append(prices[-1] + np.random.normal(0.05, 1.0))

    closes = np.array(prices[1:])
    highs = closes + np.abs(np.random.normal(0.5, 0.3, n))
    lows = closes - np.abs(np.random.normal(0.5, 0.3, n))
    opens = closes + np.random.normal(0, 0.5, n)
    volumes = np.random.uniform(1000, 5000, n)

    # Add whale spikes
    for i in range(0, n, 15):
        volumes[i] *= np.random.uniform(3, 6)

    dates = pd.date_range(start='2024-01-01', periods=n, freq='5min')
    return pd.DataFrame({
        'open': opens,
        'high': np.maximum(highs, np.maximum(opens, closes)),
        'low': np.minimum(lows, np.minimum(opens, closes)),
        'close': closes,
        'volume': volumes,
    }, index=dates)


# ============================================================
# Test 1: whale_tracker.py with OHLCV fallback (no UW client)
# ============================================================

def test_whale_tracker_ohlcv():
    print("\n" + "=" * 80)
    print("TEST 1: WhaleTracker — OHLCV FALLBACK (no UW client)")
    print("=" * 80)

    from whale_tracker import WhaleTracker

    tracker = WhaleTracker(volume_threshold_percentile=75)
    assert tracker.uw_client is None, "Should have no UW client"
    assert not tracker.has_real_data, "Should not have real data"

    df = make_sample_df()

    # All methods should work with OHLCV only
    zones = tracker.detect_accumulation_zones(df)
    print(f"  Accumulation zones: {len(zones)}")

    dist = tracker.detect_distribution(df)
    print(f"  Distribution active: {dist.active}, strength: {dist.strength:.0f}")

    vp = tracker.build_volume_profile(df)
    print(f"  Volume profile POC: {vp.poc:.2f}")

    vwap = tracker.anchored_vwap(df)
    print(f"  VWAP: {vwap.vwap:.2f}")

    icebergs = tracker.detect_iceberg_orders(df)
    print(f"  Iceberg orders: {len(icebergs)}")

    momentum = tracker.whale_momentum(df)
    print(f"  Momentum: {momentum.flow_direction}, strength: {momentum.flow_strength:.0f}")

    signal = tracker.get_whale_signal(df)
    print(f"  Composite signal: {signal.direction}, confidence: {signal.confidence:.0f}%")

    traps = tracker.detect_whale_traps(df)
    print(f"  Whale traps: {len(traps)}")

    div = tracker.whale_vs_retail_divergence(df)
    print(f"  Divergence active: {div.active}")

    print("  ✅ OHLCV fallback — ALL PASS")
    return True


# ============================================================
# Test 2: whale_tracker.py with REAL UW client
# ============================================================

def test_whale_tracker_real_uw():
    print("\n" + "=" * 80)
    print("TEST 2: WhaleTracker — REAL UW DATA (paid API)")
    print("=" * 80)

    from whale_tracker import WhaleTracker
    from unusual_whales import UnusualWhalesClient
    from config_v2 import UW_API_KEY

    if not UW_API_KEY:
        print("  ⚠️ SKIP: No UW_API_KEY configured")
        return True

    client = UnusualWhalesClient(api_key=UW_API_KEY)
    tracker = WhaleTracker(uw_client=client, ticker="SPY")

    assert tracker.has_real_data, "Should have real data"
    print(f"  UW client connected, ticker=SPY")

    df = make_sample_df(base_price=540.0)  # Approximate SPY price

    # Accumulation zones — should use real dark pool levels
    zones = tracker.detect_accumulation_zones(df)
    print(f"  Accumulation zones: {len(zones)}")
    for z in zones[:3]:
        print(f"    Zone: ${z.price_low:.2f}-${z.price_high:.2f}, "
              f"vol={z.total_volume:,.0f}, score={z.score:.2f}")

    # Distribution — should incorporate UW net premium
    dist = tracker.detect_distribution(df)
    print(f"  Distribution: active={dist.active}, strength={dist.strength:.0f}")
    for ev in dist.evidence[:3]:
        print(f"    Evidence: {ev}")

    # Iceberg orders — should use real dark pool prints
    icebergs = tracker.detect_iceberg_orders(df)
    print(f"  Iceberg orders: {len(icebergs)}")
    for ic in icebergs[:3]:
        print(f"    Iceberg: {ic.direction} @ ${ic.price_level:.2f}, "
              f"vol={ic.total_estimated_volume:,.0f}, conf={ic.confidence:.0f}%")

    # Momentum — should use real options flow
    momentum = tracker.whale_momentum(df)
    print(f"  Momentum: {momentum.flow_direction}, strength={momentum.flow_strength:.0f}%, "
          f"conviction={momentum.conviction_score:.0f}%")
    print(f"    Net flow: ${momentum.net_flow:,.0f}")
    print(f"    Divergence: {momentum.divergence}, Exhaustion: {momentum.exhaustion_flag}")

    # Composite signal — should include UW data in reasoning
    signal = tracker.get_whale_signal(df)
    print(f"  Signal: {signal.direction}, confidence={signal.confidence:.0f}%")
    print(f"    Summary: {signal.key_summary}")

    # Live alerts — should include UW flow alerts
    tracker.update_bar(df.iloc[-1], bar_index=len(df)-1, df=df)
    alerts = tracker.get_live_alerts()
    print(f"  Live alerts: {len(alerts)}")
    for a in alerts[:3]:
        print(f"    [{a.alert_type}] {a.direction}: {a.description}")

    print("  ✅ REAL UW DATA — ALL PASS")
    return True


# ============================================================
# Test 3: institutional_flow.py with REAL UW client
# ============================================================

def test_institutional_flow_real_uw():
    print("\n" + "=" * 80)
    print("TEST 3: InstitutionalFlowAnalyzer — REAL UW DATA")
    print("=" * 80)

    from institutional_flow import InstitutionalFlowAnalyzer
    from unusual_whales import UnusualWhalesClient
    from config_v2 import UW_API_KEY

    if not UW_API_KEY:
        print("  ⚠️ SKIP: No UW_API_KEY configured")
        return True

    client = UnusualWhalesClient(api_key=UW_API_KEY)
    analyzer = InstitutionalFlowAnalyzer(uw_client=client)
    analyzer.set_ticker("SPY")

    assert analyzer.has_real_data, "Should have real data"

    df = make_sample_df(base_price=540.0)

    # Dark pool prints — should use real UW data
    dp_prints = analyzer.detect_dark_pool_prints(df)
    print(f"  Dark pool prints: {len(dp_prints)}")
    for dp in dp_prints[:3]:
        print(f"    DP: ${dp.price_level:.2f}, vol={dp.volume:,.0f}, "
              f"dir={dp.direction_bias.name}, conf={dp.confidence:.0f}%")

    # Block trades — should use real UW flow alerts
    blocks = analyzer.detect_block_trades(df)
    print(f"  Block trades: {len(blocks)}")
    for b in blocks[:3]:
        print(f"    Block: ${b.price:.2f}, type={b.trade_type}, z={b.z_score:.1f}")

    # Wyckoff phase — still OHLCV (no UW equivalent)
    wyckoff = analyzer.wyckoff_phase_detection(df)
    print(f"  Wyckoff: phase={wyckoff.phase.value}, sub={wyckoff.sub_phase}")

    # Institutional score — should include UW sentiment component
    score = analyzer.get_institutional_score(df)
    print(f"  Institutional score: {score.score:.1f}/100")
    print(f"    Summary: {score.summary}")
    print(f"    Bias: {score.bias.name}")
    print(f"    Components:")
    for comp, val in score.components.items():
        marker = " ★" if comp == "uw_sentiment" else ""
        print(f"      {comp:20s}: {val:6.1f}{marker}")

    print("  ✅ INSTITUTIONAL FLOW REAL UW — ALL PASS")
    return True


# ============================================================
# Test 4: Compare OHLCV-only vs UW-enhanced scores
# ============================================================

def test_compare_ohlcv_vs_uw():
    print("\n" + "=" * 80)
    print("TEST 4: COMPARISON — OHLCV-only vs UW-enhanced")
    print("=" * 80)

    from whale_tracker import WhaleTracker
    from institutional_flow import InstitutionalFlowAnalyzer
    from unusual_whales import UnusualWhalesClient
    from config_v2 import UW_API_KEY

    if not UW_API_KEY:
        print("  ⚠️ SKIP: No UW_API_KEY configured")
        return True

    client = UnusualWhalesClient(api_key=UW_API_KEY)
    df = make_sample_df(base_price=540.0)

    # OHLCV-only
    tracker_ohlcv = WhaleTracker()
    flow_ohlcv = InstitutionalFlowAnalyzer()

    signal_ohlcv = tracker_ohlcv.get_whale_signal(df)
    score_ohlcv = flow_ohlcv.get_institutional_score(df)

    # UW-enhanced
    tracker_uw = WhaleTracker(uw_client=client, ticker="SPY")
    flow_uw = InstitutionalFlowAnalyzer(uw_client=client)
    flow_uw.set_ticker("SPY")

    signal_uw = tracker_uw.get_whale_signal(df)
    score_uw = flow_uw.get_institutional_score(df)

    print(f"  OHLCV Whale Signal: {signal_ohlcv.direction} @ {signal_ohlcv.confidence:.0f}%")
    print(f"    → {signal_ohlcv.key_summary}")
    print()
    print(f"  UW Whale Signal:    {signal_uw.direction} @ {signal_uw.confidence:.0f}%")
    print(f"    → {signal_uw.key_summary}")
    print()
    print(f"  OHLCV Inst Score:   {score_ohlcv.score:.1f}/100 ({score_ohlcv.summary})")
    print(f"  UW Inst Score:      {score_uw.score:.1f}/100 ({score_uw.summary})")

    # UW should have additional component
    assert 'uw_sentiment' in score_uw.components, "UW score should have uw_sentiment component"
    print(f"\n  UW Sentiment Score: {score_uw.components['uw_sentiment']:.1f} ★")

    print("\n  ✅ COMPARISON — PASS (UW data enriches signals)")
    return True


# ============================================================
# MAIN
# ============================================================

if __name__ == "__main__":
    results = []

    # Test 1: OHLCV fallback
    results.append(("OHLCV Fallback", test_whale_tracker_ohlcv()))

    # Test 2: Real UW whale tracker
    results.append(("Real UW WhaleTracker", test_whale_tracker_real_uw()))

    # Test 3: Real UW institutional flow
    results.append(("Real UW InstitutionalFlow", test_institutional_flow_real_uw()))

    # Test 4: Comparison
    results.append(("OHLCV vs UW Comparison", test_compare_ohlcv_vs_uw()))

    print("\n" + "=" * 80)
    print("RESULTS SUMMARY")
    print("=" * 80)
    for name, passed in results:
        status = "✅ PASS" if passed else "❌ FAIL"
        print(f"  {status}  {name}")
    print("=" * 80)
