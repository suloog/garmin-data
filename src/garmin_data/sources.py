"""Which Garmin endpoints are fetched and how their raw payloads are keyed.

Only python-garminconnect methods that return the Garmin response unchanged
are used, so ``raw_payload`` holds the real API response. (Some library
helpers such as ``get_sleep_daily`` / ``get_rhr_daily`` reshape responses and
are deliberately not used.)

Range endpoints are always requested per calendar month (key ``YYYY-MM``),
regardless of the range the user asked for. Every day is therefore covered
by exactly one range payload per endpoint, and re-syncing any sub-range
re-fetches the same key instead of creating an overlapping one.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import date, timedelta
from typing import Any

from garminconnect import Garmin

from .client import Fetcher
from .store import Store

# Endpoint names used in raw_payload.endpoint
ACTIVITIES = "activities_by_date"  # key: 'YYYY-MM'
ACTIVITY = "activity"  # key: activity id
ACTIVITY_SPLITS = "activity_splits"
ACTIVITY_TYPED_SPLITS = "activity_typed_splits"
ACTIVITY_HR_ZONES = "activity_hr_zones"
ACTIVITY_WEATHER = "activity_weather"
ACTIVITY_GEAR = "activity_gear"
WORKOUT = "workout"  # key: workout id
DEVICES = "devices"  # key: 'all'
USER_SUMMARY = "user_summary"  # key: 'YYYY-MM-DD'
SLEEP = "sleep_data"
TRAINING_READINESS = "training_readiness"
HRV_RANGE = "hrv_range"  # key: 'YYYY-MM'
MAX_METRICS_RANGE = "max_metrics_range"
WEIGH_INS_RANGE = "weigh_ins_range"

# endpoint -> python-garminconnect method name
ACTIVITY_DETAIL_METHODS = {
    ACTIVITY: "get_activity",
    ACTIVITY_SPLITS: "get_activity_splits",
    ACTIVITY_TYPED_SPLITS: "get_activity_typed_splits",
    ACTIVITY_HR_ZONES: "get_activity_hr_in_timezones",
    ACTIVITY_WEATHER: "get_activity_weather",
    ACTIVITY_GEAR: "get_activity_gear",
}
PER_DAY_METHODS = {
    USER_SUMMARY: "get_user_summary",
    SLEEP: "get_sleep_data",
    TRAINING_READINESS: "get_training_readiness",
}
RANGE_METHODS = {
    HRV_RANGE: "get_hrv_data_range",
    MAX_METRICS_RANGE: "get_max_metrics_range",
    WEIGH_INS_RANGE: "get_weigh_ins",
}

PER_DAY_ENDPOINTS = tuple(PER_DAY_METHODS)
RANGE_ENDPOINTS = tuple(RANGE_METHODS)
DAILY_ENDPOINTS = PER_DAY_ENDPOINTS + RANGE_ENDPOINTS
MONTHLY_ENDPOINTS = (ACTIVITIES, *RANGE_ENDPOINTS)


# -- date helpers ---------------------------------------------------------------


def days_between(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def month_start(d: date) -> date:
    return d.replace(day=1)


def month_end(d: date) -> date:
    nxt = (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return nxt - timedelta(days=1)


def month_key(d: date) -> str:
    return f"{d.year:04d}-{d.month:02d}"


def parse_month_key(key: str) -> date | None:
    try:
        return date.fromisoformat(f"{key}-01")
    except ValueError:
        return None


def months(start: date, end: date) -> Iterator[date]:
    """First day of every calendar month overlapping [start, end]."""
    cur = month_start(start)
    while cur <= end:
        yield cur
        cur = month_end(cur) + timedelta(days=1)


def month_span(first: date, today: date) -> tuple[date, date]:
    """Request range for a month: the whole month, clipped to today."""
    return first, min(month_end(first), today)


def activity_date(item: Any) -> date | None:
    if not isinstance(item, dict):
        return None
    try:
        return date.fromisoformat(str(item.get("startTimeLocal"))[:10])
    except ValueError:
        return None


# -- fetch units ------------------------------------------------------------------


def fetch_range(g: Garmin, f: Fetcher, endpoint: str, first: date, today: date) -> Any:
    a, b = month_span(first, today)
    fn = getattr(g, RANGE_METHODS[endpoint])
    return f.fetch(
        endpoint, month_key(first), fn, a.isoformat(), b.isoformat(), date_from=a, date_to=b
    )


def fetch_day(g: Garmin, f: Fetcher, endpoint: str, day: date) -> Any:
    key = day.isoformat()
    fn = getattr(g, PER_DAY_METHODS[endpoint])
    return f.fetch(endpoint, key, fn, key, date_from=day, date_to=day)


def fetch_activity_detail(g: Garmin, f: Fetcher, endpoint: str, aid: str) -> Any:
    return f.fetch(endpoint, aid, getattr(g, ACTIVITY_DETAIL_METHODS[endpoint]), aid)


def fetch_workout(g: Garmin, f: Fetcher, workout_id: str) -> Any:
    # Workout definitions are mutable; every fetched version is kept and the
    # normalizer picks one that predates the activity.
    return f.fetch(WORKOUT, workout_id, g.get_workout_by_id, workout_id)


def fetch_activity_month(
    g: Garmin,
    f: Fetcher,
    store: Store,
    first: date,
    today: date,
    lo: date,
    hi: date,
    refresh_from: date,
    touched: set[int],
) -> None:
    """Fetch one month's activity list, then details for activities in [lo, hi].

    Stored details are skipped unless the activity is on/after ``refresh_from``
    (names, RPE or gear are often edited shortly after the workout).
    """
    a, b = month_span(first, today)
    items = f.fetch(
        ACTIVITIES,
        month_key(first),
        g.get_activities_by_date,
        a.isoformat(),
        b.isoformat(),
        date_from=a,
        date_to=b,
    )
    for item in items if isinstance(items, list) else []:
        day = activity_date(item)
        aid = item.get("activityId") if day else None
        if aid is None or not lo <= day <= hi:
            continue
        touched.add(int(aid))
        refresh = day >= refresh_from
        for endpoint in ACTIVITY_DETAIL_METHODS:
            if refresh or not store.has_raw(endpoint, str(aid)):
                fetch_activity_detail(g, f, endpoint, str(aid))
        workout_id = item.get("workoutId")
        if workout_id and (refresh or not store.has_raw(WORKOUT, str(workout_id))):
            fetch_workout(g, f, str(workout_id))


# -- range sync -------------------------------------------------------------------


def sync_activities(
    g: Garmin,
    f: Fetcher,
    store: Store,
    start: date,
    end: date,
    today: date,
    refresh_from: date,
    touched: set[int],
) -> None:
    """Fetch activity lists (per month) and details for activities in [start, end]."""
    f.fetch(DEVICES, "all", g.get_devices)
    for first in months(start, end):
        fetch_activity_month(g, f, store, first, today, start, end, refresh_from, touched)


def sync_daily(
    g: Garmin, f: Fetcher, store: Store, start: date, end: date, today: date, refresh_from: date
) -> None:
    """Fetch monthly range endpoints and per-day endpoints for [start, end]."""
    for first in months(start, end):
        for endpoint in RANGE_ENDPOINTS:
            fetch_range(g, f, endpoint, first, today)
    for day in days_between(start, end):
        for endpoint in PER_DAY_ENDPOINTS:
            if day >= refresh_from or not store.has_raw(endpoint, day.isoformat()):
                fetch_day(g, f, endpoint, day)
