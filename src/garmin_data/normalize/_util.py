"""Small helpers shared by normalizers."""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any


def get(obj: Any, *path: str | int, default: Any = None) -> Any:
    """Safe nested lookup: ``get(d, "a", "b", 0)``; returns default on any miss."""
    cur = obj
    for key in path:
        if isinstance(cur, dict):
            cur = cur.get(key)
        elif isinstance(cur, list) and isinstance(key, int) and -len(cur) <= key < len(cur):
            cur = cur[key]
        else:
            return default
        if cur is None:
            return default
    return cur


def num(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def integer(value: Any) -> int | None:
    f = num(value)
    return None if f is None else int(round(f))


def scaled(value: Any, factor: float) -> float | None:
    f = num(value)
    return None if f is None else f * factor


def pace_s_per_km(speed_mps: Any) -> float | None:
    """Convert speed (m/s) to pace (s/km). Zero/negative speed -> None."""
    s = num(speed_mps)
    if s is None or s <= 0:
        return None
    return 1000.0 / s


def f_to_c(value: Any) -> float | None:
    f = num(value)
    return None if f is None else round((f - 32.0) * 5.0 / 9.0, 1)


def timestamp(value: Any) -> datetime | None:
    """Parse Garmin timestamp strings ('2026-09-23T17:54:44.0', '2026-09-23 17:54:44')
    or epoch milliseconds into a naive datetime (UTC for epoch values)."""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return datetime.fromtimestamp(value / 1000, tz=UTC).replace(tzinfo=None)
    if isinstance(value, str):
        text = value.strip().replace("T", " ")
        if "." in text:
            text = text.split(".", 1)[0]
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M"):
            try:
                return datetime.strptime(text, fmt)
            except ValueError:
                continue
    return None


def to_date(value: Any) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, str) and len(value) >= 10:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


class UnexpectedPayload(ValueError):
    """A non-empty payload does not have the structure the normalizer knows.

    Raised instead of returning an empty result, so a changed Garmin response
    is reported and never silently overwrites previously normalized values.
    """


def is_empty(payload: Any) -> bool:
    """Garmin's legitimate "no data" responses."""
    return payload is None or payload == [] or payload == {}


def expect(payload: Any, kind: type, what: str) -> None:
    if not isinstance(payload, kind):
        raise UnexpectedPayload(f"{what}: expected {kind.__name__}, got {type(payload).__name__}")


def expect_keys(
    obj: Any, what: str, required: tuple[str, ...], any_of: tuple[str, ...] = ()
) -> None:
    """``obj`` must be a dict with all ``required`` keys and, if given, at least
    one of ``any_of`` present (the value may be null; a missing key means the
    field was renamed or removed)."""
    expect(obj, dict, what)
    missing = [k for k in required if k not in obj]
    if missing:
        raise UnexpectedPayload(f"{what}: missing keys {missing}")
    if any_of and not any(k in obj for k in any_of):
        raise UnexpectedPayload(f"{what}: none of the known fields present")
