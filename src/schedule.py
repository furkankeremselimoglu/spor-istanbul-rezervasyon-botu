"""Scheduling logic for reservation bot: session timing, poll intervals, open windows."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, time
from zoneinfo import ZoneInfo

from src.config import Pref, TimingConfig


@dataclass(frozen=True)
class OpenWindow:
    """An upcoming reservation window for a session preference."""

    pref: Pref
    session_start: datetime  # aware, in tz
    open_at: datetime  # session_start - open_offset_hours, aware, in tz


def session_starts(pref: Pref, now: datetime, tz: ZoneInfo, count: int = 3) -> list[datetime]:
    """Next `count` session start datetimes for pref strictly after `now`, in tz (weekly recurrence).

    Args:
        pref: Preference with weekday (0=Mon..6=Sun) and start time (HH:MM).
        now: Reference datetime (aware).
        tz: Timezone for constructing datetimes.
        count: Number of future sessions to return.

    Returns:
        List of `count` aware datetimes in tz, each strictly after `now`, in ascending order.
    """
    sessions = []
    # Start from today at the pref time, then advance week by week
    today = now.date()
    start_hour, start_min = map(int, pref.start.split(":"))
    start_time = time(hour=start_hour, minute=start_min)

    # Find the first date >= today that has the right weekday
    days_until_next = (pref.weekday - today.weekday()) % 7
    first_date = today + timedelta(days=days_until_next)
    session_dt = datetime.combine(first_date, start_time, tzinfo=tz)

    # If that datetime is not strictly after now, move to next week
    if session_dt <= now:
        session_dt += timedelta(weeks=1)

    # Generate `count` sessions
    for _ in range(count):
        sessions.append(session_dt)
        session_dt += timedelta(weeks=1)

    return sessions


def open_time(session_start: datetime, offset_hours: float) -> datetime:
    """Calculate when a session window opens.

    Args:
        session_start: Session start datetime (aware).
        offset_hours: Hours before session_start that the window opens.

    Returns:
        Aware datetime at session_start - offset_hours.
    """
    return session_start - timedelta(hours=offset_hours)


def next_open_window(
    prefs: list[Pref], now: datetime, timing: TimingConfig, tz: ZoneInfo
) -> OpenWindow:
    """Find the next upcoming open window across all preferences.

    Among all prefs, generates multiple future sessions per pref, filters to those
    with upcoming or currently-hot windows (open_at + hot_after_s >= now),
    and picks the one with the earliest open_at. Ties favor earlier prefs.

    Args:
        prefs: List of preferences (in priority order, used for tie-breaking).
        now: Reference datetime (aware).
        timing: Timing configuration.
        tz: Timezone.

    Returns:
        OpenWindow with the earliest upcoming open_at.

    Raises:
        ValueError: If no upcoming window is found.
    """
    best_window = None
    best_open_at = None

    for pref in prefs:
        sessions = session_starts(pref, now, tz, count=3)
        for sess in sessions:
            open_at = open_time(sess, timing.open_offset_hours)
            # Check if this window is "upcoming or currently-hot"
            if open_at + timedelta(seconds=timing.hot_after_s) >= now:
                if best_open_at is None or open_at < best_open_at:
                    best_open_at = open_at
                    best_window = OpenWindow(pref=pref, session_start=sess, open_at=open_at)

    if best_window is None:
        raise ValueError("No upcoming open window found")

    return best_window


def poll_interval(now: datetime, prefs: list[Pref], timing: TimingConfig, tz: ZoneInfo) -> tuple[float, bool, OpenWindow]:
    """Calculate how long to sleep before the next poll, and whether we're in a hot window.

    Hot if open_at - hot_before_s <= now <= open_at + hot_after_s -> seconds = hot_poll_s.
    Otherwise seconds = min(idle_poll_s, seconds until (open_at - hot_before_s)), floored at 0.2.

    Args:
        now: Current datetime (aware).
        prefs: List of preferences.
        timing: Timing configuration.
        tz: Timezone.

    Returns:
        Tuple of (seconds_to_sleep, is_hot, OpenWindow).
    """
    window = next_open_window(prefs, now, timing, tz)
    open_at = window.open_at

    hot_start = open_at - timedelta(seconds=timing.hot_before_s)
    hot_end = open_at + timedelta(seconds=timing.hot_after_s)
    is_hot = hot_start <= now <= hot_end

    if is_hot:
        seconds = timing.hot_poll_s
    else:
        # Calculate seconds until hot window starts
        seconds_to_hot = (hot_start - now).total_seconds()
        # Cap at idle_poll_s, floor at 0.2
        seconds = max(0.2, min(timing.idle_poll_s, seconds_to_hot))

    return (seconds, is_hot, window)
