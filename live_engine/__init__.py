"""
ICT/SMC Master Trading Engine — THE ONE SYSTEM
================================================
20 strategies, 7 modules, decision tree routing.
Backtest: 7 years, 7 tickers, 5,436 trades, 66.7% WR, $0.46/trade.

Files:
  config.py      — 20 strategies, 7 modules, V3 config, decision tree params
  signals.py     — Signal detection including Displacement Engine
  v3_filters.py  — Full filter stack: Mode machine, EMA bias, CHoCH, sweep, etc.
  engine.py      — Main orchestrator (ONE process, ONE decision tree)
  indicators.py  — Technical indicators + SMC patterns
  execution.py   — Alpaca API client + position management
  data_feed.py   — Live bar streaming + multi-timeframe aggregation

Quick Start:
  python engine.py              # Paper trading
  python engine.py --live       # Live trading
"""
