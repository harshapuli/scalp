# WhaleTracker Live Tracking - Quick Start Guide

## What's New

The WhaleTracker now has **real-time live tracking** for options traders. Instead of just batch analysis, you can now:

- Track whale activity **bar-by-bar**
- Get **instant alerts** when whales start moving
- Estimate **position size and direction**
- Detect **stop hunts** (traps)
- Compare **whale vs retail flows**
- See **price-volume intensity heatmaps**

---

## Basic Usage

### 1. Initialize the Tracker

```python
from whale_tracker import WhaleTracker
import pandas as pd

tracker = WhaleTracker(volume_threshold_percentile=75)
df = pd.read_csv('market_data.csv')  # OHLCV dataframe
```

### 2. Per-Bar Live Updates

Call `update_bar()` on every new candle:

```python
# On each new bar
new_bar = df.iloc[-1]  # Latest OHLCV
event = tracker.update_bar(new_bar, bar_index=len(df)-1, df=df)

if event:
    print(f"Whale bar detected!")
    print(f"Direction: {event.direction}")
    print(f"Volume: {event.whale_volume:.0f}")
    if event.at_accumulation_zone:
        print(f"At known accumulation zone: {event.zone_reference}")
```

### 3. Check for Active Alerts

```python
alerts = tracker.get_live_alerts()

for alert in alerts:
    print(f"\n[{alert.alert_type}]")
    print(f"Direction: {alert.direction}")
    print(f"Confidence: {alert.confidence:.0f}%")
    print(f"Active for {alert.bars_active} bars")
    print(f"{alert.description}")
    
    # Trade on high confidence alerts
    if alert.confidence > 75:
        if alert.alert_type == "WHALE_ENTRY":
            execute_entry(alert.direction)
```

### 4. Monitor Large Player Position

```python
position = tracker.track_whale_positions(df)

print(f"Direction: {position.direction}")  # LONG, SHORT, or FLAT
print(f"Size: {position.estimated_size_pct:.1f}% of volume")
print(f"Loading rate: {position.loading_rate:.2f}")
print(f"Inventory state: {position.inventory_change}")  # loading, reducing, flat, flipping
print(f"Confidence: {position.confidence:.0f}%")

# Example: Alert if whale is accumulating heavily
if (position.direction == "LONG" and 
    position.inventory_change == "loading" and 
    position.confidence > 70):
    print("WARNING: Large player heavily loading long position")
```

### 5. Detect Stop Hunts (Traps)

```python
traps = tracker.detect_whale_traps(df)

for trap in traps[:3]:  # Top 3 by quality
    print(f"\nTrap detected:")
    print(f"Sweep at: {trap.sweep_price:.2f}")
    print(f"Direction: {trap.trap_direction}")
    print(f"Quality: {trap.trap_quality:.0f}%")
    
    # High quality traps are institutional stop hunts
    if trap.trap_quality > 80:
        print(">>> This is a HIGH QUALITY trap - likely institutional")
        print(f">>> Whale entered {trap.bars_after_sweep} bar(s) after sweep")
```

### 6. Whale vs Retail Divergence

```python
divergence = tracker.whale_vs_retail_divergence(df)

if divergence.active:
    print(f"DIVERGENCE DETECTED!")
    print(f"Whales: {divergence.whale_direction}")
    print(f"Retail: {divergence.retail_direction}")
    print(f"Strength: {divergence.strength:.0f}%")
    print(f"{divergence.trade_signal}")
    
    # Example signals:
    # "Strong bearish divergence - whales distributing into retail (SELL)"
    # "Strong bullish divergence - whales buying panic (BUY)"
```

### 7. Price-Volume Heatmap

```python
heatmap = tracker.get_whale_heatmap(df)

# Find hottest whale zones
hot_zones = sorted(heatmap.items(), 
                   key=lambda x: x[1]['intensity'], 
                   reverse=True)

print("Top 5 whale activity levels:")
for level, data in hot_zones[:5]:
    direction = "BUYING" if data['direction_bias'] > 0 else "SELLING"
    print(f"\nPrice {level:.2f}:")
    print(f"  Intensity: {data['intensity']:.0f}/100")
    print(f"  Whale volume: {data['whale_volume']:.0f}")
    print(f"  Direction bias: {data['direction_bias']:.2f} ({direction})")
    print(f"  Recency: {data['recency_weight']:.2f}")
    
    # Use for support/resistance
    if data['intensity'] > 70 and data['direction_bias'] > 0.5:
        print(f"  >>> Major whale buy support level")
```

### 8. Enhanced Composite Signal

```python
signal = tracker.get_whale_signal_live(df)

print(f"Direction: {signal.direction}")
print(f"Confidence: {signal.confidence:.0f}%")
print(f"Summary: {signal.key_summary}")

# Check momentum component
momentum = signal.momentum
print(f"\nMomentum:")
print(f"  Flow: {momentum.net_flow:.0f} ({momentum.flow_direction})")
print(f"  Strength: {momentum.flow_strength:.0f}%")
print(f"  Divergence: {momentum.divergence}")
print(f"  Exhaustion: {momentum.exhaustion_flag}")

# Check nearby zones
print(f"\nNearby accumulation zones: {len(signal.nearby_zones)}")
for zone in signal.nearby_zones[:2]:
    print(f"  {zone.price_low:.2f}-{zone.price_high:.2f}")
```

---

## Complete Real-Time Loop Example

```python
import pandas as pd
from whale_tracker import WhaleTracker
import time

# Setup
tracker = WhaleTracker()
df = load_historical_data()

# Establish initial state
zones = tracker.detect_accumulation_zones(df)
print(f"Detected {len(zones)} accumulation zones")

# Live trading loop
while trading:
    # Get new bar
    new_bar = fetch_latest_bar()
    df = pd.concat([df, pd.DataFrame([new_bar])], ignore_index=True)
    
    bar_idx = len(df) - 1
    
    # === LIVE TRACKING ===
    
    # 1. Update bar-by-bar
    event = tracker.update_bar(new_bar, bar_idx, df)
    if event and event.is_whale_bar:
        log(f"Whale bar: {event.direction} @ {new_bar['close']:.2f}")
    
    # 2. Check alerts
    alerts = tracker.get_live_alerts()
    for alert in alerts:
        if alert.confidence > 75:
            if alert.alert_type == "WHALE_ENTRY":
                place_order(alert.direction, quantity=10)
                log(f"ENTRY SIGNAL: {alert.direction}")
            elif alert.alert_type == "WHALE_EXIT":
                place_order(-current_position, quantity=10)
                log(f"EXIT SIGNAL")
    
    # 3. Monitor position
    position = tracker.track_whale_positions(df)
    if position.direction != last_whale_direction:
        log(f"Whale position changed to {position.direction}")
        last_whale_direction = position.direction
    
    # 4. Check for traps (every 5 bars for efficiency)
    if bar_idx % 5 == 0:
        traps = tracker.detect_whale_traps(df)
        if traps and traps[0].trap_quality > 80:
            log(f"TRAP DETECTED: {traps[0].trap_direction} @ {traps[0].sweep_price:.2f}")
    
    # 5. Divergence check
    div = tracker.whale_vs_retail_divergence(df)
    if div.active and div.strength > 70:
        log(f"Strong divergence: {div.trade_signal}")
    
    # 6. Update main signal (every 3 bars for efficiency)
    if bar_idx % 3 == 0:
        signal = tracker.get_whale_signal_live(df)
        update_display(signal)
    
    time.sleep(1)  # Wait for next bar
```

---

## Key Alert Types

### WHALE_ENTRY
- **What:** 3+ consecutive whale bars in same direction
- **Why it matters:** Large player opening position
- **Trade:** Follow the entry direction
- **Confidence:** Increases with consecutive bars

### WHALE_EXIT
- **What:** Distribution detected after accumulation
- **Why it matters:** Large player closing position
- **Trade:** Opposite of their long position
- **Confidence:** Higher with volume decline

### WHALE_EXHAUSTION
- **What:** Whale volume declining at new highs/lows
- **Why it matters:** Move is running out of steam
- **Trade:** Start closing positions
- **Confidence:** Increases with repetition

### WHALE_ABSORPTION
- **What:** Massive volume but price not moving
- **Why it matters:** Someone absorbing all flow
- **Trade:** Watch for breakout direction
- **Confidence:** Higher with more bars flat

### WHALE_DIVERGENCE
- **What:** Whale flow opposite to price direction
- **Why it matters:** Whales and retail disagree
- **Trade:** Whales usually right, retail wrong
- **Confidence:** Increases with bar count

### WHALE_TRAP
- **What:** Liquidity sweep + whale entry opposite
- **Why it matters:** Institutional stop hunt then trade
- **Trade:** Follow the whale entry
- **Confidence:** Quality score 0-100%

---

## Performance Tips

1. **Call update_bar on every bar** (minimal overhead - O(1))
2. **Check alerts every bar** (O(1) counter checks)
3. **Update position every 5 bars** (O(n) but cached)
4. **Check traps every 10 bars** (O(n²) worst case, expensive)
5. **Refresh main signal every 3 bars** (full analysis)
6. **Heatmap is expensive** - update every 30 bars or on demand

---

## Dataclass Return Values Quick Reference

| Method | Returns | Key Fields |
|--------|---------|-----------|
| update_bar() | Optional[WhaleBarEvent] | is_whale_bar, direction, at_zone, exhaustion |
| get_live_alerts() | List[WhaleAlert] | alert_type, direction, confidence, bars_active |
| get_whale_heatmap() | dict[price → metrics] | whale_volume, intensity, direction_bias |
| track_whale_positions() | WhalePosition | direction, estimated_size_pct, inventory_change |
| detect_whale_traps() | List[WhaleTrap] | sweep_price, trap_direction, trap_quality |
| whale_vs_retail_divergence() | DivergenceState | whale_direction, retail_direction, strength |
| get_whale_signal_live() | WhaleSignal | direction, confidence, momentum, key_summary |

---

## Troubleshooting

**Q: No whale bars detected?**
- Reduce volume_threshold_percentile (currently 75th)
- Increase lookback window
- Check if data has enough volume variation

**Q: Alerts not triggering?**
- Need at least 3+ consecutive whale bars for WHALE_ENTRY
- Divergence requires whale ≠ retail direction
- Exhaustion needs new high/low with whale volume

**Q: Traps have low quality?**
- Quality increases with time gap between sweep and entry
- Sweeps must be clear (new highs/lows)
- Follow-up must be clear whale bar

**Q: Position tracking seems off?**
- Check lookback period (default 50 bars)
- Verify whale volume threshold is reasonable
- Ensure sufficient whale bar activity

---

## Best Practices

1. **Always check confidence scores** - Higher is more reliable
2. **Cross-reference multiple signals** - Don't trade single alert alone
3. **Use zones for context** - Whale bars at zones are stronger
4. **Watch divergence closely** - Whales vs retail = alpha opportunity
5. **Size into exhaustion** - Reduce on exhaustion flags
6. **Trail stops after traps** - Stop hunts often reverse immediately
7. **Monitor position changes** - Loading/reducing/flipping state matters
8. **Update DataFrame incrementally** - Avoid full rebuilds per bar

---

## Real Trading Example

```python
# Hypothetical options strategy using WhaleTracker live

class WhaleOptionsTrader:
    def __init__(self):
        self.tracker = WhaleTracker()
        self.position = None
        self.entry_alerts = []
    
    def on_bar(self, df):
        # Get latest updates
        bar = df.iloc[-1]
        event = self.tracker.update_bar(bar, len(df)-1, df)
        alerts = self.tracker.get_live_alerts()
        position = self.tracker.track_whale_positions(df)
        
        # ENTRY LOGIC
        for alert in alerts:
            if alert.alert_type == "WHALE_ENTRY" and alert.confidence > 80:
                # Whale is entering - follow them
                size = int(position.estimated_size_pct / 10)
                if alert.direction == "BUY":
                    self.buy_call(size)
                else:
                    self.buy_put(size)
                self.entry_alerts.append(alert)
        
        # EXIT LOGIC
        if self.position and len(self.entry_alerts) > 0:
            entry = self.entry_alerts[0]
            
            # Exit on exhaustion
            if event and event.is_exhaustion_bar:
                self.close_position()
                self.entry_alerts.clear()
            
            # Exit if whale reverses
            elif (position.direction != entry.direction and 
                  position.confidence > 75):
                self.close_position()
                self.entry_alerts.clear()
            
            # Exit after 20 bars
            elif len(self.entry_alerts[0].timestamp) >= 20:
                self.close_position()
                self.entry_alerts.clear()
```

---

## Summary

The live tracking layer transforms WhaleTracker from batch analysis into a **real-time signal generator**. Key benefits:

- ✓ Instant whale detection (per-bar)
- ✓ High-confidence entry signals
- ✓ Position tracking with state changes
- ✓ Stop hunt identification
- ✓ Whale vs retail analysis
- ✓ Production-ready (all methods tested)
- ✓ Backward compatible (all old methods still work)

Start with simple alerts (WHALE_ENTRY > 75% confidence), then layer in additional signals as you get comfortable with the system.
