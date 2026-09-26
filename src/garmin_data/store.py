"""DuckDB storage: connection, migrations, raw payload persistence and upserts."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from datetime import UTC, date, datetime
from importlib import resources
from pathlib import Path
from typing import Any

import duckdb


def _migrations() -> list[str]:
    """Ordered list of migration scripts; index + 1 == schema version."""
    pkg = resources.files("garmin_data")
    return [
        pkg.joinpath(name).read_text(encoding="utf-8") for name in ("schema.sql", "schema_v2.sql")
    ]


def canonical_json(payload: Any) -> str:
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str
    )


def utc_now() -> datetime:
    """Naive UTC timestamp (all *_at columns are UTC)."""
    return datetime.now(UTC).replace(tzinfo=None)


class Store:
    """Thin wrapper around a DuckDB connection."""

    def __init__(self, db_path: Path | str) -> None:
        if str(db_path) != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(str(db_path))
        self._columns: dict[str, list[str]] = {}
        self._tx_depth = 0
        self._migrate()

    def close(self) -> None:
        self.con.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    @contextmanager
    def transaction(self) -> Iterator[None]:
        """Atomic block. Nested blocks join the outermost transaction."""
        if self._tx_depth:
            self._tx_depth += 1
            try:
                yield
            finally:
                self._tx_depth -= 1
            return
        self.con.execute("BEGIN")
        self._tx_depth = 1
        try:
            yield
        except BaseException:
            self.con.execute("ROLLBACK")
            raise
        else:
            self.con.execute("COMMIT")
        finally:
            self._tx_depth = 0

    # -- schema ---------------------------------------------------------------

    def _migrate(self) -> None:
        self.con.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = self.con.execute("SELECT max(version) FROM schema_version").fetchone()
        current = row[0] or 0
        for version, script in enumerate(_migrations(), start=1):
            if version <= current:
                continue
            with self.transaction():
                self.con.execute(script)
                self.con.execute("INSERT INTO schema_version VALUES (?)", [version])

    # -- raw layer ------------------------------------------------------------

    def put_raw(
        self,
        endpoint: str,
        source_key: str,
        payload: Any,
        date_from: date | None = None,
        date_to: date | None = None,
    ) -> bool:
        """Store a raw payload. Returns True if the content is new.

        Identical content for the same (endpoint, source_key) only bumps
        ``last_fetched_at`` (and the coverage dates); changed content is
        appended as a new version. Payloads themselves are never modified.
        """
        text = canonical_json(payload)
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        now = utc_now()
        exists = self.con.execute(
            "SELECT 1 FROM raw_payload WHERE endpoint=? AND source_key=? AND sha256=?",
            [endpoint, source_key, digest],
        ).fetchone()
        if exists:
            self.con.execute(
                "UPDATE raw_payload SET last_fetched_at=?, "
                "date_from=coalesce(?, date_from), date_to=coalesce(?, date_to) "
                "WHERE endpoint=? AND source_key=? AND sha256=?",
                [now, date_from, date_to, endpoint, source_key, digest],
            )
            return False
        self.con.execute(
            "INSERT INTO raw_payload VALUES (?, ?, ?, ?, ?, ?, ?, ?::JSON)",
            [endpoint, source_key, digest, date_from, date_to, now, now, text],
        )
        return True

    def has_raw(self, endpoint: str, source_key: str) -> bool:
        return bool(
            self.con.execute(
                "SELECT 1 FROM raw_payload WHERE endpoint=? AND source_key=? LIMIT 1",
                [endpoint, source_key],
            ).fetchone()
        )

    def latest_raw(self, endpoint: str, source_key: str) -> Any | None:
        """Most recently seen payload version for a key, or None."""
        row = self.con.execute(
            "SELECT payload FROM raw_payload WHERE endpoint=? AND source_key=? "
            "ORDER BY last_fetched_at DESC, first_fetched_at DESC LIMIT 1",
            [endpoint, source_key],
        ).fetchone()
        return json.loads(row[0]) if row and row[0] is not None else None

    def raw_coverage(self, endpoint: str, source_key: str) -> tuple[date | None, date | None]:
        """Date coverage of the latest version of a payload."""
        row = self.con.execute(
            "SELECT date_from, date_to FROM raw_payload WHERE endpoint=? AND source_key=? "
            "ORDER BY last_fetched_at DESC, first_fetched_at DESC LIMIT 1",
            [endpoint, source_key],
        ).fetchone()
        return (row[0], row[1]) if row else (None, None)

    def raw_versions(self, endpoint: str, source_key: str) -> list[Any]:
        """All stored versions of a payload, oldest first."""
        rows = self.con.execute(
            "SELECT payload FROM raw_payload WHERE endpoint=? AND source_key=? "
            "ORDER BY first_fetched_at",
            [endpoint, source_key],
        ).fetchall()
        return [json.loads(r[0]) for r in rows if r[0] is not None]

    def latest_raw_by_endpoint(
        self, endpoint: str, date_from: date | None = None, date_to: date | None = None
    ) -> list[tuple[str, Any]]:
        """Latest version per source_key, ordered by coverage start.

        Range endpoints are keyed by calendar month, so every day is covered by
        exactly one source_key per endpoint and no cross-key precedence exists.
        Within a key, the most recently observed version is the current one.
        When a date window is given, only payloads overlapping it are returned.
        """
        sql = (
            "SELECT source_key, payload FROM raw_payload WHERE endpoint=? "
            "{window} QUALIFY row_number() OVER "
            "(PARTITION BY source_key ORDER BY last_fetched_at DESC, first_fetched_at DESC) = 1 "
            "ORDER BY date_from, source_key"
        )
        params: list[Any] = [endpoint]
        window = ""
        if date_from is not None and date_to is not None:
            window = "AND date_from <= ? AND date_to >= ?"
            params += [date_to, date_from]
        rows = self.con.execute(sql.format(window=window), params).fetchall()
        return [(k, json.loads(p) if p is not None else None) for k, p in rows]

    def raw_keys(self, endpoint: str) -> list[str]:
        rows = self.con.execute(
            "SELECT DISTINCT source_key FROM raw_payload WHERE endpoint=?", [endpoint]
        ).fetchall()
        return [r[0] for r in rows]

    def raw_date_span(self, endpoints: Sequence[str]) -> tuple[date | None, date | None]:
        placeholders = ",".join("?" for _ in endpoints)
        row = self.con.execute(
            "SELECT min(date_from), max(date_to) FROM raw_payload "
            f"WHERE endpoint IN ({placeholders})",
            list(endpoints),
        ).fetchone()
        return row[0], row[1]

    # -- normalized layer -----------------------------------------------------

    def columns(self, table: str) -> list[str]:
        if table not in self._columns:
            rows = self.con.execute(f"PRAGMA table_info('{table}')").fetchall()
            self._columns[table] = [r[1] for r in rows]
        return self._columns[table]

    def upsert(self, table: str, rows: Iterable[Mapping[str, Any]]) -> int:
        """INSERT OR REPLACE rows by primary key. Columns missing from a row become NULL."""
        rows = list(rows)
        if not rows:
            return 0
        cols = self.columns(table)
        sql = (
            f"INSERT OR REPLACE INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join('?' for _ in cols)})"
        )
        self.con.executemany(sql, [[r.get(c) for c in cols] for r in rows])
        return len(rows)

    def replace_children(
        self, table: str, activity_id: int, rows: Sequence[Mapping[str, Any]]
    ) -> None:
        """Replace all rows of a child table (laps, splits, zones) for one activity."""
        self.con.execute(f"DELETE FROM {table} WHERE activity_id=?", [activity_id])
        self.upsert(table, rows)

    # -- sync log -------------------------------------------------------------

    def log(
        self, run_id: str, endpoint: str, source_key: str, status: str, error: str | None = None
    ) -> None:
        self.con.execute(
            "INSERT INTO sync_log VALUES (?, ?, ?, ?, ?, ?)",
            [run_id, utc_now(), endpoint, source_key, status, error],
        )
