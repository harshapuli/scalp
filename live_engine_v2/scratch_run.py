from backtest_alpaca_30day import AlpacaBacktester
import json
import logging
logging.getLogger().setLevel(logging.CRITICAL)

tester = AlpacaBacktester(tickers=['SPY'], days=30)
results = tester.run()

with open('backtest_spy_results.json', 'w') as f:
    json.dump(results, f, indent=4, default=str)
print("Dumped to backtest_spy_results.json")
