"""Build normalized tables from the raw layer (no network access).

Two modes:
- incremental (after a sync): a payload that fails to normalize is logged and
  skipped; its previously normalized rows are left untouched.
- ``rebuild``: all-or-nothing. Any normalization error rolls back the whole
  rebuild, so existing normalized data is never lost.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from . import sources as src
from .gaps import requested_days
from .normalize import activities as na
from .normalize import daily as nd
from .normalize._util import timestamp
from .store import Store, utc_now

log = logging.getLogger(__name__)

NORMALIZED_TABLES = (
    "activity",
    "activity_lap",
    "activity_split",
    "activity_hr_zone",
    "activity_sample",
    "daily_health",
)


class RebuildError(RuntimeError):
    """Rebuild failed and was rolled back; normalized tables are unchanged."""


@dataclass
class BuildReport:
    activities: int = 0
    days: int = 0
    errors: list[str] = field(default_factory=list)


def _describe(exc: BaseException) -> str:
    return f"{type(exc).__name__}: {exc}"


def _activity_list_items(store: Store) -> dict[int, dict[str, Any]]:
    """activity id -> list item from the activities_by_date payloads."""
    items: dict[int, dict[str, Any]] = {}
    for _, payload in store.latest_raw_by_endpoint(src.ACTIVITIES):
        for item in payload if isinstance(payload, list) else []:
            if isinstance(item, dict) and item.get("activityId") is not None:
                items[int(item["activityId"])] = item
    return items


def _pick_workout(store: Store, workout_id: int | None, start_utc: datetime | None) -> dict | None:
    """Latest stored workout version that was last edited before the activity started."""
    if workout_id is None:
        return None
    candidates = [
        w
        for w in store.raw_versions(src.WORKOUT, str(workout_id))
        if na.workout_is_trustworthy(w, start_utc)
    ]
    return max(candidates, key=lambda w: timestamp(w.get("updatedDate")), default=None)


def _compute_activity(
    store: Store, aid: int, list_item: dict | None, devices: Any
) -> dict[str, Any] | None:
    """All normalized rows for one activity. Pure computation, no writes."""
    key = str(aid)
    row = na.normalize_activity(
        store.latest_raw(src.ACTIVITY, key),
        list_item,
        weather=store.latest_raw(src.ACTIVITY_WEATHER, key),
        gear=store.latest_raw(src.ACTIVITY_GEAR, key),
        devices=devices,
    )
    if row is None:
        return None
    start = row["start_time_utc"]
    workout = _pick_workout(store, row["workout_id"], start)
    return {
        "activity": row,
        "activity_lap": na.normalize_laps(
            aid, store.latest_raw(src.ACTIVITY_SPLITS, key), workout, start
        ),
        "activity_split": na.normalize_typed_splits(
            aid, store.latest_raw(src.ACTIVITY_TYPED_SPLITS, key)
        ),
        "activity_hr_zone": na.normalize_hr_zones(
            aid, store.latest_raw(src.ACTIVITY_HR_ZONES, key)
        ),
        "activity_sample": na.normalize_samples(aid, store.latest_raw(src.ACTIVITY_DETAILS, key)),
    }


def build_activities(
    store: Store,
    activity_ids: Iterable[int] | None = None,
    report: BuildReport | None = None,
    strict: bool = False,
) -> BuildReport:
    report = report or BuildReport()
    list_items = _activity_list_items(store)
    if activity_ids is None:
        # Same set a sync produces: activities with stored details, plus list
        # items inside requested ranges (monthly lists also contain activities
        # outside the range the user asked for).
        requested = requested_days(store)
        ids = {int(k) for k in store.raw_keys(src.ACTIVITY)} | {
            aid for aid, item in list_items.items() if src.activity_date(item) in requested
        }
    else:
        ids = set(activity_ids)
    devices = store.latest_raw(src.DEVICES, "all")
    now = utc_now()
    with store.transaction():
        for aid in sorted(ids):
            try:
                rows = _compute_activity(store, aid, list_items.get(aid), devices)
            except Exception as e:
                if strict:
                    raise RebuildError(f"activity {aid}: {_describe(e)}") from e
                report.errors.append(f"activity {aid}: {_describe(e)}")
                log.error("Could not normalize activity %s: %s", aid, _describe(e))
                continue
            if rows is None:
                continue
            rows["activity"]["normalized_at"] = now
            store.upsert("activity", [rows["activity"]])
            for table in ("activity_lap", "activity_split", "activity_hr_zone", "activity_sample"):
                store.replace_children(table, aid, rows[table])
            report.activities += 1
    return report


def _daily_sources() -> list[tuple[str, Any]]:
    """Daily sources in precedence order: later entries override earlier ones.
    Looked up at call time so the extractors stay patchable."""
    return [
        (src.HRV_RANGE, nd.from_hrv_range),
        (src.MAX_METRICS_RANGE, nd.from_max_metrics),
        (src.WEIGH_INS_RANGE, nd.from_weigh_ins),
        (src.USER_SUMMARY, nd.from_user_summary),
        (src.SLEEP, nd.from_sleep),
        (src.TRAINING_READINESS, nd.from_training_readiness),
    ]


# Sleep's fallback fields only fill gaps, i.e. rank below every primary source.
_FALLBACK_RANK = 0


def build_daily(
    store: Store,
    start: date | None = None,
    end: date | None = None,
    report: BuildReport | None = None,
    strict: bool = False,
) -> BuildReport:
    """Rebuild ``daily_health`` rows for [start, end] from raw payloads.

    If a payload fails to normalize (non-strict mode), the columns it could
    have set keep their previously stored values on the days it covers,
    unless a healthy source with higher precedence set them anyway.
    """
    report = report or BuildReport()
    if start is None or end is None:
        span_start, span_end = store.raw_date_span(src.DAILY_ENDPOINTS)
        start, end = start or span_start, end or span_end
    if start is None or end is None:
        return report
    days = src.days_between(start, end)

    primary: list[nd.DailyFields] = []
    fallback: list[nd.DailyFields] = []
    # (day, column) -> highest rank of a healthy source that supplied a value
    supplied: dict[tuple[date, str], int] = {}
    # (endpoint, key, rank) of payloads that failed to normalize
    failed: list[tuple[str, str, int]] = []

    def note(fields: nd.DailyFields, rank: int) -> None:
        for d, cols in fields.items():
            for col, value in cols.items():
                if value is not None:
                    supplied[(d, col)] = max(rank, supplied.get((d, col), -1))

    for rank, (endpoint, fn) in enumerate(_daily_sources(), start=1):
        for key, payload in store.latest_raw_by_endpoint(endpoint, start, end):
            try:
                out = fn(payload)
            except Exception as e:
                if strict:
                    raise RebuildError(f"{endpoint} {key}: {_describe(e)}") from e
                report.errors.append(f"{endpoint} {key}: {_describe(e)}")
                log.error("Could not normalize %s %s: %s", endpoint, key, _describe(e))
                failed.append((endpoint, key, rank))
                continue
            if endpoint == src.SLEEP:
                prim, fb = out
                primary.append(prim)
                fallback.append(fb)
                note(prim, rank)
                note(fb, _FALLBACK_RANK)
            else:
                primary.append(out)
                note(out, rank)

    rows = nd.merge_daily(days, primary, fallback)
    if failed:
        _keep_previous_values(store, rows, failed, supplied)
    now = utc_now()
    for r in rows:
        r["normalized_at"] = now
    with store.transaction():
        store.upsert("daily_health", rows)
    report.days += len(rows)
    return report


def _keep_previous_values(
    store: Store,
    rows: list[dict[str, Any]],
    failed: list[tuple[str, str, int]],
    supplied: dict[tuple[date, str], int],
) -> None:
    by_day = {r["date"]: r for r in rows}
    affected: dict[date, list[tuple[str, int]]] = {}
    for endpoint, key, rank in failed:
        lo, hi = store.raw_coverage(endpoint, key)
        if lo is None or hi is None:
            continue
        columns = [(c, rank) for c in nd.SOURCE_COLUMNS[endpoint]]
        if endpoint == src.SLEEP:
            columns = [(c, rank) for c in nd.SLEEP_PRIMARY_COLUMNS] + [
                (c, _FALLBACK_RANK) for c in nd.SLEEP_FALLBACK_COLUMNS
            ]
        for d in src.days_between(lo, hi):
            if d in by_day:
                affected.setdefault(d, []).extend(columns)
    if not affected:
        return
    cur = store.con.execute(
        "SELECT * FROM daily_health WHERE date IN (SELECT unnest(?::DATE[]))", [list(affected)]
    )
    names = [c[0] for c in cur.description]
    previous = {r[0]: dict(zip(names, r, strict=True)) for r in cur.fetchall()}
    for d, columns in affected.items():
        old = previous.get(d)
        if old is None:
            continue
        for col, rank in columns:
            if supplied.get((d, col), -1) > rank:
                continue  # a healthy higher-precedence source set it anyway
            if old.get(col) is not None:
                by_day[d][col] = old[col]


def rebuild(store: Store) -> BuildReport:
    """Rebuild all normalized tables from raw in one transaction.

    Annotations are untouched. On any error the transaction is rolled back and
    ``RebuildError`` is raised; the previous normalized data stays in place.
    """
    report = BuildReport()
    try:
        with store.transaction():
            for table in NORMALIZED_TABLES:
                store.con.execute(f"DELETE FROM {table}")
            build_activities(store, report=report, strict=True)
            build_daily(store, report=report, strict=True)
    except RebuildError:
        raise
    except Exception as e:
        raise RebuildError(_describe(e)) from e
    return report
