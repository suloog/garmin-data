"""Normalize daily health payloads into ``daily_health`` rows.

Each extractor takes one raw payload and returns ``{date: {column: value}}``.
``merge_daily`` combines them; values from later payloads override earlier
ones, and ``None`` never overrides a real value.

Sources (python-garminconnect 0.3.16):
- ``get_user_summary(date)``      steps, calories, stress, resting HR, Body Battery
- ``get_sleep_data(date)``        sleep duration/stages/score (+ HRV/RHR fallback)
- ``get_training_readiness(date)`` list of readiness entries; the morning one is used
- ``get_hrv_data_range(a, b)``    nightly HRV, weekly average, status
- ``get_max_metrics_range(a, b)`` VO2max (only days where it was (re)computed)
- ``get_weigh_ins(a, b)``         weight in grams
"""

from __future__ import annotations

from datetime import date
from typing import Any

from ._util import expect, expect_keys, get, integer, is_empty, num, scaled, timestamp, to_date

DailyFields = dict[date, dict[str, Any]]

# Columns each source can populate, keyed by raw endpoint name. Used to keep
# previous values when a payload fails to normalize (see build.build_daily).
# Kept in sync with the extractors by tests.
SLEEP_PRIMARY_COLUMNS = (
    "sleep_start_utc",
    "sleep_end_utc",
    "sleep_duration_s",
    "sleep_score",
    "deep_sleep_s",
    "light_sleep_s",
    "rem_sleep_s",
    "awake_s",
)
SLEEP_FALLBACK_COLUMNS = ("resting_hr", "hrv_last_night_avg", "hrv_status")
SOURCE_COLUMNS: dict[str, tuple[str, ...]] = {
    "user_summary": (
        "resting_hr",
        "steps",
        "active_kcal",
        "resting_kcal",
        "total_kcal",
        "stress_avg",
        "stress_max",
        "body_battery_high",
        "body_battery_low",
        "body_battery_wake",
    ),
    "sleep_data": SLEEP_PRIMARY_COLUMNS + SLEEP_FALLBACK_COLUMNS,
    "training_readiness": (
        "training_readiness",
        "training_readiness_level",
        "recovery_time_min",
        "tr_sleep_score_pct",
        "tr_recovery_time_pct",
        "tr_acwr_pct",
        "tr_hrv_pct",
        "tr_stress_history_pct",
        "tr_sleep_history_pct",
        "acute_load",
    ),
    "hrv_range": ("hrv_last_night_avg", "hrv_weekly_avg", "hrv_status"),
    "max_metrics_range": ("vo2max",),
    "weigh_ins_range": ("weight_kg",),
}


def _non_negative(value: Any) -> int | None:
    """Garmin uses negative sentinels (e.g. -1, -2) for 'not enough data'."""
    v = integer(value)
    return v if v is not None and v >= 0 else None


_USER_SUMMARY_KEYS = (
    "restingHeartRate",
    "totalSteps",
    "activeKilocalories",
    "bmrKilocalories",
    "totalKilocalories",
    "averageStressLevel",
    "maxStressLevel",
    "bodyBatteryHighestValue",
    "bodyBatteryLowestValue",
    "bodyBatteryAtWakeTime",
)
_SLEEP_KEYS = (
    "sleepStartTimestampGMT",
    "sleepEndTimestampGMT",
    "sleepTimeSeconds",
    "sleepScores",
    "deepSleepSeconds",
    "lightSleepSeconds",
    "remSleepSeconds",
    "awakeSleepSeconds",
)
_READINESS_KEYS = ("score", "level", "recoveryTime")
_HRV_KEYS = ("lastNightAvg", "weeklyAvg", "status")

# Every extractor returns {} for Garmin's "no data" responses (None, [], {}, or
# known keys with null values) and raises UnexpectedPayload for a non-empty
# response whose structure it does not recognize.


def from_user_summary(payload: dict | None) -> DailyFields:
    if is_empty(payload):
        return {}
    expect_keys(payload, "user_summary", ("calendarDate",), _USER_SUMMARY_KEYS)
    d = to_date(payload["calendarDate"])
    if d is None:
        return {}
    return {
        d: {
            "resting_hr": integer(get(payload, "restingHeartRate")),
            "steps": integer(get(payload, "totalSteps")),
            "active_kcal": num(get(payload, "activeKilocalories")),
            "resting_kcal": num(get(payload, "bmrKilocalories")),
            "total_kcal": num(get(payload, "totalKilocalories")),
            "stress_avg": _non_negative(get(payload, "averageStressLevel")),
            "stress_max": _non_negative(get(payload, "maxStressLevel")),
            "body_battery_high": integer(get(payload, "bodyBatteryHighestValue")),
            "body_battery_low": integer(get(payload, "bodyBatteryLowestValue")),
            "body_battery_wake": integer(get(payload, "bodyBatteryAtWakeTime")),
        }
    }


def from_sleep(payload: dict | None) -> tuple[DailyFields, DailyFields]:
    """Returns (primary, fallback). Fallback fields fill gaps only."""
    if is_empty(payload):
        return {}, {}
    expect_keys(payload, "sleep_data", ("dailySleepDTO",))
    s = payload["dailySleepDTO"]
    if is_empty(s):
        return {}, {}
    expect_keys(s, "sleep_data.dailySleepDTO", ("calendarDate",), _SLEEP_KEYS)
    d = to_date(s["calendarDate"])
    if d is None:
        return {}, {}
    primary = {
        "sleep_start_utc": timestamp(get(s, "sleepStartTimestampGMT")),
        "sleep_end_utc": timestamp(get(s, "sleepEndTimestampGMT")),
        "sleep_duration_s": integer(get(s, "sleepTimeSeconds")),
        "sleep_score": integer(get(s, "sleepScores", "overall", "value")),
        "deep_sleep_s": integer(get(s, "deepSleepSeconds")),
        "light_sleep_s": integer(get(s, "lightSleepSeconds")),
        "rem_sleep_s": integer(get(s, "remSleepSeconds")),
        "awake_s": integer(get(s, "awakeSleepSeconds")),
    }
    fallback = {
        "resting_hr": integer(get(payload, "restingHeartRate")),
        "hrv_last_night_avg": num(get(payload, "avgOvernightHrv")),
        "hrv_status": get(payload, "hrvStatus"),
    }
    return {d: primary}, {d: fallback}


def pick_morning_readiness(entries: list | None) -> dict | None:
    """Prefer the AFTER_WAKEUP_RESET entry (Morning Report); else the earliest."""
    if not isinstance(entries, list) or not entries:
        return None
    morning = [e for e in entries if get(e, "inputContext") == "AFTER_WAKEUP_RESET"]
    pool = morning or entries
    return min(pool, key=lambda e: str(get(e, "timestamp", default="")))


def from_training_readiness(payload: list | None) -> DailyFields:
    if is_empty(payload):
        return {}
    expect(payload, list, "training_readiness")
    for entry in payload:
        expect_keys(entry, "training_readiness[]", ("calendarDate",), _READINESS_KEYS)
    e = pick_morning_readiness(payload)
    d = to_date(get(e, "calendarDate"))
    if d is None:
        return {}
    return {
        d: {
            "training_readiness": integer(get(e, "score")),
            "training_readiness_level": get(e, "level"),
            "recovery_time_min": integer(get(e, "recoveryTime")),
            "tr_sleep_score_pct": integer(get(e, "sleepScoreFactorPercent")),
            "tr_recovery_time_pct": integer(get(e, "recoveryTimeFactorPercent")),
            "tr_acwr_pct": integer(get(e, "acwrFactorPercent")),
            "tr_hrv_pct": integer(get(e, "hrvFactorPercent")),
            "tr_stress_history_pct": integer(get(e, "stressHistoryFactorPercent")),
            "tr_sleep_history_pct": integer(get(e, "sleepHistoryFactorPercent")),
            "acute_load": num(get(e, "acuteLoad")),
        }
    }


def from_hrv_range(payload: dict | None) -> DailyFields:
    if is_empty(payload):
        return {}
    expect_keys(payload, "hrv_range", ("hrvSummaries",))
    rows = payload["hrvSummaries"] or []
    expect(rows, list, "hrv_range.hrvSummaries")
    out: DailyFields = {}
    for row in rows:
        expect_keys(row, "hrv_range.hrvSummaries[]", ("calendarDate",), _HRV_KEYS)
        d = to_date(row["calendarDate"])
        if d is None:
            continue
        out[d] = {
            "hrv_last_night_avg": num(get(row, "lastNightAvg")),
            "hrv_weekly_avg": num(get(row, "weeklyAvg")),
            "hrv_status": get(row, "status"),
        }
    return out


def from_max_metrics(payload: list | None) -> DailyFields:
    if is_empty(payload):
        return {}
    expect(payload, list, "max_metrics_range")
    out: DailyFields = {}
    for row in payload:
        # "generic" (running) may legitimately be null, e.g. cycling-only days.
        expect_keys(row, "max_metrics_range[]", ("generic",))
        generic = row["generic"]
        if generic is None:
            continue
        expect_keys(
            generic,
            "max_metrics_range[].generic",
            ("calendarDate",),
            ("vo2MaxPreciseValue", "vo2MaxValue"),
        )
        d = to_date(generic["calendarDate"])
        if d is None:
            continue
        out[d] = {
            "vo2max": num(_first(get(generic, "vo2MaxPreciseValue"), get(generic, "vo2MaxValue")))
        }
    return out


def from_weigh_ins(payload: dict | None) -> DailyFields:
    if is_empty(payload):
        return {}
    expect_keys(payload, "weigh_ins_range", ("dailyWeightSummaries",))
    rows = payload["dailyWeightSummaries"] or []
    expect(rows, list, "weigh_ins_range.dailyWeightSummaries")
    out: DailyFields = {}
    for row in rows:
        expect_keys(row, "weigh_ins_range.dailyWeightSummaries[]", ("summaryDate", "latestWeight"))
        d = to_date(row["summaryDate"])
        latest = row["latestWeight"]
        if d is None or latest is None:
            continue
        expect_keys(latest, "weigh_ins_range...latestWeight", ("weight",))
        if latest["weight"] is None:
            continue
        out[d] = {"weight_kg": scaled(latest["weight"], 0.001)}
    return out


def _first(*values: Any) -> Any:
    return next((v for v in values if v is not None), None)


def merge_daily(
    days: list[date], primary: list[DailyFields], fallback: list[DailyFields] = ()
) -> list[dict[str, Any]]:
    """Merge extracted fields into one row per day (every day gets a row)."""
    rows: dict[date, dict[str, Any]] = {d: {"date": d} for d in days}
    for source in primary:
        for d, fields in source.items():
            if d in rows:
                rows[d].update({k: v for k, v in fields.items() if v is not None})
    for source in fallback:
        for d, fields in source.items():
            if d in rows:
                for k, v in fields.items():
                    if v is not None and rows[d].get(k) is None:
                        rows[d][k] = v
    return [rows[d] for d in days]
