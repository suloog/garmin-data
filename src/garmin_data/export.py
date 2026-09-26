"""Export normalized tables and annotations to Parquet or CSV."""

from __future__ import annotations

from pathlib import Path

from .build import NORMALIZED_TABLES
from .store import Store

EXPORT_TABLES = (*NORMALIZED_TABLES, "annotation")


def export_tables(store: Store, out_dir: Path, fmt: str = "parquet") -> list[Path]:
    if fmt not in ("parquet", "csv"):
        raise ValueError("format must be 'parquet' or 'csv'")
    out_dir.mkdir(parents=True, exist_ok=True)
    options = "(FORMAT parquet)" if fmt == "parquet" else "(FORMAT csv, HEADER)"
    written = []
    for table in EXPORT_TABLES:
        path = out_dir / f"{table}.{fmt}"
        store.con.execute(f"COPY {table} TO '{path.as_posix()}' {options}")
        written.append(path)
    return written
