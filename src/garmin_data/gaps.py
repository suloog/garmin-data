"""Find data that was requested by an earlier sync but is still missing.

"Requested" means covered by a row in ``sync_run``, so days the user never
asked for are not treated as gaps. An item is missing when no raw payload
exists for it: either its fetch failed or the run was aborted before
reaching it. Items that already failed ``max_attempts`` times are reported
as given up instead of being retried forever (e.g. days before the account
had any data).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from . import sources as src
from .store import Store


@dataclass
class Gaps:
    per_day: list[tuple[str, date]] = field(default_factory=list)  # (endpoint, day)
    monthly: list[tuple[str, date]] = field(default_factory=list)  # (endpoint, month start)
    details: list[tuple[str, str]] = field(default_factory=list)  # (endpoint, activity id)
    workouts: list[str] = field(default_factory=list)
    given_up: int = 0

    @property
    def pending(self) -> int:
        return len(self.per_day) + len(self.monthly) + len(self.details) + len(self.workouts)


def requested_days(store: Store) -> set[date]:
    days: set[date] = set()
    for lo, hi in store.con.execute("SELECT date_from, date_to FROM sync_run").fetchall():
        days.update(src.days_between(lo, hi))
    return days


def _error_counts(store: Store) -> Counter[tuple[str, str]]:
    rows = store.con.execute(
        "SELECT endpoint, source_key, count(*) FROM sync_log WHERE status = 'error' GROUP BY ALL"
    ).fetchall()
    return Counter({(e, k): n for e, k, n in rows})


def find_gaps(store: Store, max_attempts: int, before: date) -> Gaps:
    """Missing items for requested days strictly before ``before``.

    Days from ``before`` on are left to the regular sync window, and months
    overlapping it are skipped because the window re-fetches them anyway.
    """
    gaps = Gaps()
    errors = _error_counts(store)
    days = sorted(d for d in requested_days(store) if d < before)
    if not days:
        return gaps

    def missing(endpoint: str, key: str) -> bool:
        """True if the item should be retried; counts it as given up if exhausted."""
        if store.has_raw(endpoint, key):
            return False
        if errors[(endpoint, key)] >= max_attempts:
            gaps.given_up += 1
            return False
        return True

    for endpoint in src.PER_DAY_ENDPOINTS:
        stored = set(store.raw_keys(endpoint))
        for d in days:
            if d.isoformat() not in stored and missing(endpoint, d.isoformat()):
                gaps.per_day.append((endpoint, d))

    window_month = src.month_start(before)
    for first in sorted({src.month_start(d) for d in days}):
        if first >= window_month:
            continue
        for endpoint in src.MONTHLY_ENDPOINTS:
            if missing(endpoint, src.month_key(first)):
                gaps.monthly.append((endpoint, first))

    day_set = set(days)
    seen_workouts: set[str] = set()
    for _, items in store.latest_raw_by_endpoint(src.ACTIVITIES):
        for item in items if isinstance(items, list) else []:
            if src.activity_date(item) not in day_set or item.get("activityId") is None:
                continue
            aid = str(item["activityId"])
            for endpoint in src.ACTIVITY_DETAIL_METHODS:
                if missing(endpoint, aid):
                    gaps.details.append((endpoint, aid))
            wid = item.get("workoutId")
            if wid and str(wid) not in seen_workouts:
                seen_workouts.add(str(wid))
                if missing(src.WORKOUT, str(wid)):
                    gaps.workouts.append(str(wid))
    return gaps
