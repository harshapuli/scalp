#!/bin/bash
# Engine watchdog — auto-restarts on exit
cd /sessions/dazzling-epic-planck/mnt/outputs/live_engine

while true; do
    echo "[$(date)] Starting engine..." >> /tmp/v1_watchdog.log
    python3 engine.py >> /tmp/v1_engine.log 2>&1
    EXIT_CODE=$?
    echo "[$(date)] Engine exited (code $EXIT_CODE) — restarting in 5s..." >> /tmp/v1_watchdog.log
    sleep 5
done
