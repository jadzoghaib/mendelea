"""DuckDB connection handling.

DuckDB reads the Parquet snapshots in place, so the warehouse file holds only
manifests and derived tables. Deleting it is always safe: everything in it can
be rebuilt from the immutable snapshots, which are the real system of record.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import duckdb


@contextmanager
def connect(path: Path, read_only: bool = False):
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = duckdb.connect(str(path), read_only=read_only)
    try:
        yield connection
    finally:
        connection.close()


def as_posix(path: Path) -> str:
    """DuckDB wants forward slashes even on Windows."""
    return path.resolve().as_posix()
