"""The bitemporal timeline.

These are the tests that matter most: every headline number the product
reports is a query over `assertion_span`, so an error here is invisible and
propagates into customer-facing claims.
"""

from datetime import date

import pytest

from mendelea.db import connect
from mendelea.evidence import snapshot, spans
from mendelea.evidence.snapshot import SnapshotManifest, snapshot_path

PANEL = "testpanel"


def write_snapshot(root, release_date: str, rows: list[tuple]) -> SnapshotManifest:
    """Write one synthetic snapshot.

    Rows are (allele_id, bucket, stars), optionally with a fourth element
    naming the gene; it defaults to TESTGENE so every existing caller is
    unaffected. Two genes of different sizes are what the gene-ranking tests
    need, and a panel really does hold them.
    """
    records = [
        {
            "allele_id": row[0],
            "variation_id": row[0].replace("A", ""),
            "contig": "17",
            "pos": 1000 + index,
            "ref": "C",
            "alt": "T",
            "gene": row[3] if len(row) > 3 else "TESTGENE",
            "bucket": row[1],
            "stars": row[2],
            "clnsig_raw": row[1].title(),
            "clnrevstat_raw": "criteria_provided,_single_submitter",
            "condition": "Test condition",
        }
        for index, row in enumerate(rows)
    ]
    path = snapshot_path(root, date.fromisoformat(release_date), PANEL)
    snapshot.write_snapshot(path, records)

    return SnapshotManifest(
        snapshot_id=f"clinvar-{release_date.replace('-', '')}-{PANEL}",
        source="clinvar",
        release_date=release_date,
        panel=PANEL,
        source_url=f"http://example.invalid/{release_date}",
        retrieved_at="2026-01-01T00:00:00+00:00",
        parquet_sha256="0" * 64,
        row_count=len(records),
        bytes_fetched=1,
        source_full_bytes=100,
        genes_requested=1,
        pipeline_git_sha="test",
    )


@pytest.fixture
def timeline(tmp_path):
    """Three releases exercising every transition we care about.

      A1  steady VUS throughout
      A2  VUS, then likely pathogenic       -> a real reclassification
      A3  VUS, then gone                    -> a retraction
      A4  absent, then appears as pathogenic -> a new submission
    """
    root = tmp_path / "snapshots"
    manifests = [
        write_snapshot(root, "2019-01-02", [
            ("A1", "UNCERTAIN", 1),
            ("A2", "UNCERTAIN", 1),
            ("A3", "UNCERTAIN", 1),
        ]),
        write_snapshot(root, "2022-06-01", [
            ("A1", "UNCERTAIN", 1),
            ("A2", "LIKELY_PATHOGENIC", 2),
            ("A3", "UNCERTAIN", 1),
            ("A4", "PATHOGENIC", 3),
        ]),
        write_snapshot(root, "2026-08-08", [
            ("A1", "UNCERTAIN", 1),
            ("A2", "LIKELY_PATHOGENIC", 2),
            ("A4", "PATHOGENIC", 3),
        ]),
    ]

    with connect(tmp_path / "w.duckdb") as connection:
        spans.build(connection, root, manifests, PANEL)
        yield connection


def spans_for(connection, allele_id):
    return connection.execute(
        "SELECT bucket, stars, valid_from, valid_to, is_current "
        "FROM assertion_span WHERE allele_id = ? ORDER BY valid_from",
        [allele_id],
    ).fetchall()


# --------------------------------------------------------------------------


def test_unchanged_allele_collapses_to_one_span(timeline):
    """Three identical observations must not become three rows."""
    rows = spans_for(timeline, "A1")
    assert len(rows) == 1
    assert rows[0][0] == "UNCERTAIN"
    assert rows[0][2] == date(2019, 1, 2)


def test_reclassification_splits_into_two_spans(timeline):
    rows = spans_for(timeline, "A2")
    assert [r[0] for r in rows] == ["UNCERTAIN", "LIKELY_PATHOGENIC"]
    # The first span closes exactly where the second opens: no gap, no overlap.
    assert rows[0][3] == rows[1][2] == date(2022, 6, 1)


def test_current_span_is_open_ended_and_flagged(timeline):
    rows = spans_for(timeline, "A2")
    assert rows[-1][3] == date(9999, 12, 31)
    assert rows[-1][4] is True
    assert rows[0][4] is False


def test_retraction_becomes_an_explicit_absent_span(timeline):
    """A variant vanishing from ClinVar is news, not a gap in the data."""
    rows = spans_for(timeline, "A3")
    assert [r[0] for r in rows] == ["UNCERTAIN", "ABSENT"]
    assert rows[1][2] == date(2026, 8, 8)


def test_allele_absent_before_first_appearance(timeline):
    """A4 did not exist in 2019; densification must say so rather than guess."""
    rows = spans_for(timeline, "A4")
    assert rows[0][0] == "ABSENT"
    assert rows[0][2] == date(2019, 1, 2)
    assert rows[-1][0] == "PATHOGENIC"


def test_star_change_alone_opens_a_new_span(tmp_path):
    """Review status strengthening matters even when the classification holds."""
    root = tmp_path / "snapshots"
    manifests = [
        write_snapshot(root, "2019-01-02", [("B1", "PATHOGENIC", 1)]),
        write_snapshot(root, "2026-08-08", [("B1", "PATHOGENIC", 3)]),
    ]
    with connect(tmp_path / "w2.duckdb") as connection:
        spans.build(connection, root, manifests, PANEL)
        rows = spans_for(connection, "B1")
    assert len(rows) == 2
    assert [r[1] for r in rows] == [1, 3]


# --------------------------------------------------------------------------
# Point-in-time lookup
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "as_of,expected",
    [
        ("2019-01-02", "UNCERTAIN"),   # first release
        ("2020-05-05", "UNCERTAIN"),   # between releases: carry forward
        ("2022-06-01", "LIKELY_PATHOGENIC"),
        ("2026-08-08", "LIKELY_PATHOGENIC"),
        ("2030-01-01", "LIKELY_PATHOGENIC"),  # beyond the last release
    ],
)
def test_state_on_answers_point_in_time(timeline, as_of, expected):
    assert spans.state_on(timeline, "A2", as_of)[0] == expected


def test_state_before_first_release_is_unknown(timeline):
    """We must not claim knowledge of a date we have no snapshot for."""
    assert spans.state_on(timeline, "A2", "2015-01-01") is None


def test_spans_never_overlap(timeline):
    """The invariant that makes point-in-time queries single-valued."""
    overlaps = timeline.execute(
        """
        SELECT COUNT(*) FROM assertion_span a
        JOIN assertion_span b
          ON a.allele_id = b.allele_id
         AND a.valid_from < b.valid_from
         AND a.valid_to   > b.valid_from
        """
    ).fetchone()[0]
    assert overlaps == 0


def test_manifest_recorded_for_provenance(timeline):
    count = timeline.execute("SELECT COUNT(*) FROM snapshot_manifest").fetchone()[0]
    assert count == 3


def test_build_refuses_empty_input(tmp_path):
    with connect(tmp_path / "w3.duckdb") as connection:
        with pytest.raises(ValueError, match="no snapshots"):
            spans.build(connection, tmp_path, [], PANEL)
