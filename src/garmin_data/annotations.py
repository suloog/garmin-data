"""Manual training notes. Stored in ``annotation``; never touched by sync."""

from __future__ import annotations

from datetime import date
from typing import Any

from .store import Store, utc_now

FIELDS = ("rpe", "fatigue", "legs", "motivation", "soreness_or_pain", "notes")


def add_annotation(
    store: Store,
    *,
    activity_id: int | None = None,
    day: date | None = None,
    **fields: Any,
) -> int:
    """Insert a note linked to an activity and/or a date. Returns its id."""
    if activity_id is None and day is None:
        raise ValueError("An annotation needs an activity_id or a date")
    unknown = set(fields) - set(FIELDS)
    if unknown:
        raise ValueError(f"Unknown annotation fields: {sorted(unknown)}")
    rpe = fields.get("rpe")
    if rpe is not None and not 1 <= int(rpe) <= 10:
        raise ValueError("rpe must be between 1 and 10")
    if day is None and activity_id is not None:
        row = store.con.execute(
            "SELECT date FROM activity WHERE activity_id=?", [activity_id]
        ).fetchone()
        day = row[0] if row else None
    values = {k: fields.get(k) for k in FIELDS}
    now = utc_now()
    row = store.con.execute(
        f"INSERT INTO annotation (activity_id, date, {', '.join(FIELDS)}, created_at, updated_at) "
        f"VALUES (?, ?, {', '.join('?' for _ in FIELDS)}, ?, ?) RETURNING id",
        [activity_id, day, *values.values(), now, now],
    ).fetchone()
    return int(row[0])


def update_annotation(store: Store, annotation_id: int, **fields: Any) -> None:
    changes = {k: v for k, v in fields.items() if k in FIELDS and v is not None}
    if not changes:
        return
    sets = ", ".join(f"{k}=?" for k in changes)
    store.con.execute(
        f"UPDATE annotation SET {sets}, updated_at=? WHERE id=?",
        [*changes.values(), utc_now(), annotation_id],
    )


def delete_annotation(store: Store, annotation_id: int) -> None:
    store.con.execute("DELETE FROM annotation WHERE id=?", [annotation_id])


def list_annotations(
    store: Store, start: date | None = None, end: date | None = None
) -> list[dict[str, Any]]:
    sql = (
        "SELECT * FROM annotation WHERE (? IS NULL OR date >= ?) AND (? IS NULL OR date <= ?) "
        "ORDER BY date, id"
    )
    cur = store.con.execute(sql, [start, start, end, end])
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r, strict=True)) for r in cur.fetchall()]
