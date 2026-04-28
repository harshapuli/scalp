"""journal/alerts.py — Pushover or Discord webhook alerts. Spec S5-83.

Acceptance:
  - Fires on: trade fill, kill switch, drift, GEX stale rejection rate >20%
  - Rate-limited: max 5 alerts/hour
  - Pushover or Discord webhook (env: PUSHOVER_USER/TOKEN, DISCORD_WEBHOOK)
"""
from __future__ import annotations

import os
import sys
import time
from collections import deque

import httpx


PUSHOVER_USER_ENV = "PUSHOVER_USER"
PUSHOVER_TOKEN_ENV = "PUSHOVER_TOKEN"
DISCORD_WEBHOOK_ENV = "DISCORD_WEBHOOK"

RATE_LIMIT_MAX_PER_HOUR = 5
RATE_LIMIT_WINDOW_SECONDS = 3600


_recent_alerts: deque = deque()


def _rate_limit_check() -> bool:
    """Returns True if we may fire (within rate limit)."""
    now = time.time()
    while _recent_alerts and (now - _recent_alerts[0]) > RATE_LIMIT_WINDOW_SECONDS:
        _recent_alerts.popleft()
    if len(_recent_alerts) >= RATE_LIMIT_MAX_PER_HOUR:
        return False
    _recent_alerts.append(now)
    return True


def alert(message: str, severity: str = "info", title: str = "scalp2") -> bool:
    """S5-83. Returns True if alert was sent (or False if rate-limited / no webhook).

    Tries Pushover first, falls back to Discord.
    """
    if not _rate_limit_check():
        return False

    pushover_user = os.environ.get(PUSHOVER_USER_ENV)
    pushover_token = os.environ.get(PUSHOVER_TOKEN_ENV)
    discord = os.environ.get(DISCORD_WEBHOOK_ENV)

    full_msg = f"[{severity.upper()}] {message}"
    sent = False

    if pushover_user and pushover_token:
        try:
            r = httpx.post(
                "https://api.pushover.net/1/messages.json",
                data={
                    "user": pushover_user, "token": pushover_token,
                    "title": title, "message": full_msg,
                    "priority": 1 if severity in ("critical", "error") else 0,
                },
                timeout=5.0,
            )
            if r.status_code == 200:
                sent = True
        except Exception:
            pass

    if not sent and discord:
        try:
            r = httpx.post(
                discord, json={"content": f"**{title}**\n{full_msg}"},
                timeout=5.0,
            )
            if r.status_code in (200, 204):
                sent = True
        except Exception:
            pass

    if not sent:
        print(f"ALERT (no webhook): {full_msg}", file=sys.stderr)

    return sent


def reset_rate_limit() -> None:
    _recent_alerts.clear()


if __name__ == "__main__":
    reset_rate_limit()
    fired = alert("foundation verification complete", severity="info")
    print(f"[alerts] alert sent: {fired}")
    for i in range(7):
        alert(f"test message {i}")
    print(f"[alerts] queued {len(_recent_alerts)} (max {RATE_LIMIT_MAX_PER_HOUR})")
    assert len(_recent_alerts) <= RATE_LIMIT_MAX_PER_HOUR
    print("[alerts] OK")
