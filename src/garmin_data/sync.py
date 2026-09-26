"""Sync orchestration: fetch raw data for a date range, then normalize it."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import date, timedelta

from garminconnect import Garmin

from . import build
from . import sources as src
from .client import Fetcher, FetchStats, SyncAborted
from .config import Settings
from .gaps import Gaps, find_gaps
from .store import Store, utc_now

log = logging.getLogger(__name__)


@dataclass
class SyncResult:
    run_id: str
    start: date
    end: date
    stats: FetchStats
    activities: int
    days: int
    aborted: str | None = None
    gaps_retried: int = 0
    gaps_given_up: int = 0
    normalize_errors: list[str] = field(default_factory=list)

    @property
    def status(self) -> str:
        if self.aborted:
            return "aborted"
        if self.stats.errors or self.normalize_errors:
            return "partial"
        return "ok"


def incremental_window(
    store: Store, settings: Settings, today: date | None = None
) -> tuple[date, date]:
    """From the last synced day (minus an overlap, since Garmin revises recent
    sleep/HRV/readiness values) to today. Empty DB: the last N days.

    Older missing data is handled separately by gap retry (``fill_gaps``).
    """
    today = today or date.today()
    row = store.con.execute(
        "SELECT max(date_to) FROM raw_payload WHERE endpoint = ?", [src.USER_SUMMARY]
    ).fetchone()
    last = row[0] if row else None
    if last is None:
        return today - timedelta(days=settings.initial_sync_days - 1), today
    return min(last, today) - timedelta(days=settings.incremental_overlap_days), today


def _retry_gaps(
    g: Garmin, f: Fetcher, store: Store, gaps: Gaps, today: date, touched: set[int]
) -> set[date]:
    """Fetch the missing items; returns the days whose daily rows need rebuilding."""
    days: set[date] = {d for _, d in gaps.per_day}
    no_refresh = today + timedelta(days=1)
    for endpoint, first in gaps.monthly:
        lo, hi = src.month_span(first, today)
        if endpoint == src.ACTIVITIES:
            src.fetch_activity_month(g, f, store, first, today, lo, hi, no_refresh, touched)
        else:
            src.fetch_range(g, f, endpoint, first, today)
            days.update(src.days_between(lo, hi))
    for endpoint, day in gaps.per_day:
        src.fetch_day(g, f, endpoint, day)
    for endpoint, aid in gaps.details:
        src.fetch_activity_detail(g, f, endpoint, aid)
        touched.add(int(aid))
    for workout_id in gaps.workouts:
        src.fetch_workout(g, f, workout_id)
    return days


def sync(
    g: Garmin,
    store: Store,
    settings: Settings,
    start: date,
    end: date,
    refresh: bool = False,
    fill_gaps: bool = False,
    today: date | None = None,
) -> SyncResult:
    """Sync [start, end].

    Data already stored is skipped except for recent days (within
    ``incremental_overlap_days`` of today) or when ``refresh`` is set. With
    ``fill_gaps``, items missing from earlier requested ranges (failed or
    never reached) are retried too, up to ``max_fetch_attempts`` each.
    """
    today = today or date.today()
    end = min(end, today)
    if start > end:
        raise ValueError(f"Invalid range: {start} > {end}")
    refresh_from = start if refresh else today - timedelta(days=settings.incremental_overlap_days)
    run_id = uuid.uuid4().hex[:12]
    fetcher = Fetcher(store, run_id, settings.request_delay_s)
    log.info("Sync %s .. %s (run %s)", start, end, run_id)

    gaps = find_gaps(store, settings.max_fetch_attempts, start) if fill_gaps else Gaps()
    store.con.execute(
        "INSERT INTO sync_run VALUES (?, ?, NULL, ?, ?, 'running')",
        [run_id, utc_now(), start, end],
    )

    touched: set[int] = set()
    gap_days: set[date] = set()
    aborted = None
    try:
        src.sync_activities(g, fetcher, store, start, end, today, refresh_from, touched)
        src.sync_daily(g, fetcher, store, start, end, today, refresh_from)
        if gaps.pending:
            log.info("Retrying %d missing items from earlier syncs", gaps.pending)
            gap_days = _retry_gaps(g, fetcher, store, gaps, today, touched)
    except SyncAborted as e:
        aborted = str(e)
        log.error("Sync aborted: %s", e)

    # Normalize whatever was fetched, even after an abort. Monthly range
    # payloads affect every day of their month, so rebuild whole months.
    report = build.build_activities(store, touched) if touched else build.BuildReport()
    build.build_daily(store, src.month_start(start), min(src.month_end(end), today), report=report)
    if gap_days:
        extra = build.build_daily(store, min(gap_days), max(gap_days))
        report.errors += extra.errors
    for err in report.errors:
        kind, _, rest = err.partition(" ")
        key, _, message = rest.partition(": ")
        store.log(run_id, f"normalize:{kind}", key, "normalize_error", message)

    result = SyncResult(
        run_id,
        start,
        end,
        fetcher.stats,
        report.activities,
        report.days,
        aborted,
        gaps_retried=gaps.pending,
        gaps_given_up=gaps.given_up,
        normalize_errors=report.errors,
    )
    store.con.execute(
        "UPDATE sync_run SET finished_at = ?, status = ? WHERE run_id = ?",
        [utc_now(), result.status, run_id],
    )
    return result
