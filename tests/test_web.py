"""Queries behind the time machine."""

import pytest

from mendelea.web import queries
from tests.test_policy import build


@pytest.fixture
def warehouse(tmp_path):
    """One steady variant, one reclassified, one retracted, one newcomer."""
    return build(tmp_path, [
        ("2019-01-02", [("A1", "UNCERTAIN", 1), ("A2", "UNCERTAIN", 1),
                        ("A3", "UNCERTAIN", 1)]),
        ("2022-01-02", [("A1", "UNCERTAIN", 1), ("A2", "LIKELY_PATHOGENIC", 2),
                        ("A3", "UNCERTAIN", 1), ("A4", "BENIGN", 3)]),
        ("2025-01-02", [("A1", "UNCERTAIN", 1), ("A2", "LIKELY_PATHOGENIC", 2),
                        ("A4", "BENIGN", 3)]),
    ])


def test_loaded_panel_reports_extent(warehouse):
    panel = queries.loaded_panel(warehouse)
    assert panel["panel"] == "testpanel"
    assert panel["alleles"] == 4


def test_release_dates_are_the_slider_stops(warehouse):
    assert queries.release_dates(warehouse, "testpanel") == [
        "2019-01-02", "2022-01-02", "2025-01-02"
    ]


def test_release_dates_exclude_other_panels(warehouse):
    """The slider must only stop on dates this panel was actually observed on.

    The manifest table accumulates every panel ever ingested; offering another
    panel's dates would present carried-forward state as a measurement.
    """
    assert queries.release_dates(warehouse, "some-other-panel") == []


def test_genes_rank_by_movement(warehouse):
    genes = queries.genes(warehouse)
    assert genes[0]["gene"] == "TESTGENE"
    assert genes[0]["alleles"] == 4


def test_appearing_in_clinvar_does_not_count_as_movement(warehouse):
    """Of four alleles only A2 was truly reclassified.

    A1 never changed. A3 was retracted (real -> ABSENT). A4 merely appeared
    (ABSENT -> BENIGN). Counting spans would call three of the four "moved"
    and report 75%, which a laboratory reads as a reclassification rate.
    """
    gene = queries.genes(warehouse)[0]
    assert gene["moved"] == 1
    assert gene["moved_pct"] == 25.0


def test_genes_can_be_restricted_to_the_panel_list(warehouse):
    """5 kb flanking pulls in neighbouring genes; the picker should hide them."""
    assert queries.genes(warehouse, allowed={"TESTGENE"})[0]["gene"] == "TESTGENE"
    assert queries.genes(warehouse, allowed={"SOMETHING_ELSE"}) == []


def test_variants_show_past_and_present_together(warehouse):
    """One row must carry both states or the UI has to reconcile two lists."""
    rows = {r["allele_id"]: r for r in queries.variants_on(warehouse, "TESTGENE", "2019-01-02")}
    assert rows["A2"]["bucket"] == "UNCERTAIN"
    assert rows["A2"]["now_bucket"] == "LIKELY_PATHOGENIC"
    assert rows["A2"]["moved"] is True
    assert rows["A1"]["moved"] is False


def test_moved_variants_sort_first(warehouse):
    rows = queries.variants_on(warehouse, "TESTGENE", "2019-01-02")
    assert rows[0]["moved"] is True


def test_absent_variants_are_not_shown_as_of_a_date(warehouse):
    """A4 did not exist in 2019 and must not appear in that view."""
    ids = {r["allele_id"] for r in queries.variants_on(warehouse, "TESTGENE", "2019-01-02")}
    assert "A4" not in ids


def test_headline_counts_the_actionable_moves(warehouse):
    h = queries.headline(warehouse, "TESTGENE", "2019-01-02")
    assert h["uncertain"] == 3          # A1, A2, A3
    assert h["now_pathogenic"] == 1     # A2
    assert h["actionable"] == 1
    assert h["actionable_pct"] == pytest.approx(33.3, abs=0.1)


def test_headline_on_the_latest_date_shows_no_movement(warehouse):
    h = queries.headline(warehouse, "TESTGENE", "2025-01-02")
    assert h["actionable"] == 0


def test_timeline_returns_every_state_in_order(warehouse):
    steps = queries.timeline(warehouse, "A2")
    assert [s["bucket"] for s in steps] == ["UNCERTAIN", "LIKELY_PATHOGENIC"]
    assert steps[0]["from"] == "2019-01-02"


def test_open_ended_span_hides_the_sentinel_date(warehouse):
    """The UI should say "now", never 9999-12-31."""
    steps = queries.timeline(warehouse, "A2")
    assert steps[-1]["to"] is None
    assert steps[-1]["is_current"] is True


def test_retraction_appears_in_the_timeline(warehouse):
    steps = queries.timeline(warehouse, "A3")
    assert steps[-1]["bucket"] == "ABSENT"
