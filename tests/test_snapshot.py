"""Snapshot files: written by DuckDB, read by DuckDB.

The column set is the contract between ingest and everything downstream, so
the round trip is checked column by column rather than trusted.
"""

import duckdb
import pytest

from mendelea.evidence import snapshot


def read_back(path):
    """Bound, not interpolated -- a tmp path containing a quote would
    otherwise break the statement, and `write_snapshot` binds its own."""
    connection = duckdb.connect()
    try:
        described = connection.execute(
            "DESCRIBE SELECT * FROM read_parquet($p)", {"p": path.as_posix()}
        ).fetchall()
        rows = connection.execute(
            "SELECT * FROM read_parquet($p) ORDER BY pos", {"p": path.as_posix()}
        ).fetchall()
    finally:
        connection.close()
    return [(name, kind) for name, kind, *_ in described], rows


def row(**overrides):
    base = {
        "allele_id": "mendelea:VA.x", "variation_id": "1", "contig": "17", "pos": 100,
        "ref": "C", "alt": "T", "gene": "TP53", "bucket": "UNCERTAIN", "stars": 1,
        "clnsig_raw": "Uncertain_significance",
        "clnrevstat_raw": "criteria_provided,_single_submitter",
        "condition": "Li-Fraumeni syndrome",
    }
    return {**base, **overrides}


def test_schema_is_exactly_the_declared_columns(tmp_path):
    target = tmp_path / "s.parquet"
    snapshot.write_snapshot(target, [row()])
    schema, _ = read_back(target)
    assert schema == list(snapshot.COLUMNS)


def test_values_round_trip_including_null_and_unicode(tmp_path):
    target = tmp_path / "s.parquet"
    snapshot.write_snapshot(target, [
        row(pos=1, condition=None, clnsig_raw=None, stars=0),
        row(pos=2, condition='Déjà vu; "quoted", comma', stars=4),
    ])
    _, rows = read_back(target)
    assert rows[0][11] is None and rows[0][9] is None and rows[0][8] == 0
    assert rows[1][11] == 'Déjà vu; "quoted", comma' and rows[1][8] == 4


def test_empty_snapshot_keeps_its_schema(tmp_path):
    """A gene with no records in a release must still produce a readable file."""
    target = tmp_path / "empty.parquet"
    snapshot.write_snapshot(target, [])
    schema, rows = read_back(target)
    assert rows == []
    assert schema == list(snapshot.COLUMNS)


def test_write_leaves_no_temporary_files_behind(tmp_path):
    target = tmp_path / "s.parquet"
    snapshot.write_snapshot(target, [row()])
    assert sorted(p.name for p in tmp_path.iterdir()) == ["s.parquet"]


def test_a_failed_write_leaves_no_partial_snapshot(tmp_path):
    target = tmp_path / "s.parquet"
    with pytest.raises(Exception):
        snapshot.write_snapshot(target, [row(pos="not a position")])
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_identical_rows_write_identical_bytes(tmp_path):
    """Content addressing (the manifest sha256) relies on this.

    Within one DuckDB version. The Parquet writer stamps itself into the
    file's `created_by` metadata, so an upgrade changes the bytes of an
    otherwise identical snapshot. That does not invalidate a stored
    checksum -- snapshots are never rewritten -- but a forced re-ingest
    after an upgrade will produce a new one, which is what
    `pipeline_git_sha` and the provenance audit exist to make visible.
    """
    a, b = tmp_path / "a.parquet", tmp_path / "b.parquet"
    snapshot.write_snapshot(a, [row(pos=1), row(pos=2)])
    snapshot.write_snapshot(b, [row(pos=1), row(pos=2)])
    assert a.read_bytes() == b.read_bytes()
