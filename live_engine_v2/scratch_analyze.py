import json

with open("backtest_spy_results.json") as f:
    data = json.load(f)

print("--- OVERALL ---")
print(f"WR: {data.get('win_rate', 0):.2f}% | PNL: {data.get('total_pnl_pct', 0):.2f}%")

print("\n--- GRADES ---")
for grade, stats in data.get('per_execution_grade', {}).items():
    print(f"Grade {grade}: WR={stats['win_rate']:.2f}% | PNL={stats['total_pnl']:.2f}% | Trades={stats['trades']}")

print("\n--- TIERS ---")
for tier, stats in data.get('per_signal_tier', {}).items():
    print(f"Tier {tier}: WR={stats['win_rate']:.2f}% | PNL={stats['total_pnl']:.2f}% | Trades={stats['trades']}")
    
print("\n--- WHALE STATES ---")
state_pnl = {}
state_trades = {}
state_wins = {}
for ticker, tdata in data.items():
    if not isinstance(tdata, dict) or 'trades' not in tdata: continue
    for t in tdata['trades']:
        st = t.get('whale_state', 'UNKNOWN')
        pnl = t.get('pnl_pct', 0)
        state_trades[st] = state_trades.get(st, 0) + 1
        state_pnl[st] = state_pnl.get(st, 0) + pnl
        if pnl > 0: state_wins[st] = state_wins.get(st, 0) + 1

for st in state_trades:
    wr = (state_wins.get(st, 0) / state_trades[st]) * 100
    print(f"State {st}: WR={wr:.2f}% | PNL={state_pnl[st]:.2f}% | Trades={state_trades[st]}")

