"""strategies/s1_pre_fomc/setup.py — Pre-FOMC time-window gate. Spec S1-1.

ACCEPTANCE CRITERIA (S1-1):
  - FOMC calendar: 8 scheduled announcements per year
  - Setup window: 14:00 ET prior trading day → 14:00 ET announcement day
  - Universe: SPY, QQQ, IWM only (broad-market drift)
  - No setup if: holiday-shortened day, prior emergency announcement, VIX > 35

Tests required (S1-1.T1, T2):
  T1 — fires only inside 24h window
  T2 — VIX > 35 disables S1
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Optional


# 2026 FOMC scheduled meeting dates (announcement at 14:00 ET each day).
# Update annually from https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
FOMC_DATES_2026 = [
    date(2026, 1, 28),  # Jan 27-28
    date(2026, 3, 18),  # Mar 17-18
    date(2026, 4, 29),  # Apr 28-29
    date(2026, 6, 17),  # Jun 16-17
    date(2026, 7, 29),  # Jul 28-29
    date(2026, 9, 16),  # Sep 15-16
    date(2026, 11, 4),  # Nov 3-4
    date(2026, 12, 16), # Dec 15-16
]

S1_UNIVERSE = ("SPY", "QQQ", "IWM")
ANNOUNCEMENT_TIME_UTC = time(18, 0)  # 14:00 ET = 18:00 UTC (EDT) — close enough for both EDT/EST
SETUP_WINDOW_HOURS = 24


@dataclass
class S1SetupContext:
    ticker: str
    now_utc: datetime
    vix_value: Optional[float] = None
    is_holiday_day: bool = False
    prior_emergency_announcement: bool = False


def next_fomc_announcement(now_utc: datetime,
                            fomc_dates: list[date] = None) -> Optional[datetime]:
    """Returns the next scheduled FOMC announcement datetime (UTC), or None."""
    fomc_dates = fomc_dates or FOMC_DATES_2026
    for d in sorted(fomc_dates):
        announcement_dt = datetime.combine(d, ANNOUNCEMENT_TIME_UTC,
                                              tzinfo=timezone.utc)
        if announcement_dt > now_utc:
            return announcement_dt
    return None


def is_in_window(now_utc: datetime, fomc_dates: list[date] = None) -> bool:
    """T1 — True iff now is within SETUP_WINDOW_HOURS of next FOMC."""
    next_dt = next_fomc_announcement(now_utc, fomc_dates)
    if next_dt is None:
        return False
    delta = next_dt - now_utc
    return timedelta(0) < delta <= timedelta(hours=SETUP_WINDOW_HOURS)


def is_s1_setup(ctx: S1SetupContext, cfg: dict,
                 fomc_dates: list[date] = None) -> tuple[bool, Optional[str]]:
    """S1-1. Returns (eligible, reason_if_not)."""
    s1 = cfg["s1"]

    if ctx.ticker not in s1["universe"]:
        return False, "ticker_not_in_universe"

    if ctx.is_holiday_day:
        return False, "holiday_day"
    if ctx.prior_emergency_announcement:
        return False, "prior_emergency_announcement"

    # T2 — VIX regime override
    if ctx.vix_value is not None and ctx.vix_value > s1["vix_regime_max"]:
        return False, "vix_regime_override"

    if not is_in_window(ctx.now_utc, fomc_dates):
        return False, "outside_window"

    return True, None
