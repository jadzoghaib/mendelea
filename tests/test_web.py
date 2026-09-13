"""Queries behind the time machine."""

import pytest

from mendelea.reports.policy import PolicyEvent
from mendelea.web import queries
from tests.test_policy import build


def rows_on(connection, gene, on, **kwargs):
    return queries.variants_on(connection, gene, on, **kwargs)["rows"]


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


def test_genes_report_the_rate_the_headline_reports(warehouse):
    """The picker's number must be the one the view delivers.

    Of three variants uncertain in 2019 only A2 is now actionable, so the
    gene reads 33.3% — the same figure `headline` gives for that date.
    """
    gene = queries.genes(warehouse, "2019-01-02")[0]
    assert gene["gene"] == "TESTGENE"
    assert gene["vus"] == 3
    assert gene["actionable"] == 1
    assert gene["actionable_pct"] == pytest.approx(33.3, abs=0.1)
    assert gene["actionable_pct"] == queries.headline(
        warehouse, "TESTGENE", "2019-01-02")["actionable_pct"]


def test_appearing_in_clinvar_does_not_inflate_the_rate(warehouse):
    """A4 did not exist in 2019, so its benign classification is not movement.

    Counting it would put the gene at 50% on a denominator that never
    included it — which a laboratory reads as a reclassification rate.
    """
    gene = queries.genes(warehouse, "2019-01-02")[0]
    assert gene["vus"] == 3          # A1, A2, A3 — never A4
    assert gene["actionable"] == 1   # A2 only; A3 was retracted, not reclassified


def test_genes_can_be_restricted_to_the_panel_list(warehouse):
    """5 kb flanking pulls in neighbouring genes; the picker should hide them."""
    assert queries.genes(warehouse, "2019-01-02", {"TESTGENE"})[0]["gene"] == "TESTGENE"
    assert queries.genes(warehouse, "2019-01-02", {"SOMETHING_ELSE"}) == []


def test_genes_without_a_baseline_date_are_empty(warehouse):
    assert queries.genes(warehouse, None) == []


@pytest.fixture
def two_sizes(tmp_path):
    """A big gene with a modest rate and a thin one with a spectacular rate."""
    big_then = [(f"B{i}", "UNCERTAIN", 1, "BIGGENE") for i in range(250)]
    big_now = ([(f"B{i}", "PATHOGENIC", 3, "BIGGENE") for i in range(25)]
               + [(f"B{i}", "UNCERTAIN", 1, "BIGGENE") for i in range(25, 250)])
    small_then = [(f"S{i}", "UNCERTAIN", 1, "SMALLGENE") for i in range(10)]
    small_now = ([(f"S{i}", "PATHOGENIC", 3, "SMALLGENE") for i in range(5)]
                 + [(f"S{i}", "UNCERTAIN", 1, "SMALLGENE") for i in range(5, 10)])
    return build(tmp_path, [("2019-01-02", big_then + small_then),
                            ("2025-01-02", big_now + small_now)])


def test_a_thin_gene_does_not_lead_the_picker(two_sizes):
    """50% of 10 variants is not a headline; 10% of 250 is.

    Ranking on the rate alone would open the demo on a claim that falls over
    the moment someone asks what it was measured on.
    """
    genes = queries.genes(two_sizes, "2019-01-02")
    assert [g["gene"] for g in genes] == ["BIGGENE", "SMALLGENE"]
    assert genes[0]["actionable_pct"] == 10.0 and genes[0]["thin"] is False
    assert genes[1]["actionable_pct"] == 50.0 and genes[1]["thin"] is True


def test_the_thin_gene_is_still_selectable(two_sizes):
    """Ranked down, never removed: a laboratory may have reported in it."""
    assert "SMALLGENE" in {g["gene"] for g in queries.genes(two_sizes, "2019-01-02")}


# --------------------------------------------------------------------------
# The panel-level rate: the question a laboratory asks first
# --------------------------------------------------------------------------


def test_panel_headline_spans_every_gene(two_sizes):
    """30 of 260 uncertain variants became actionable, across both genes."""
    p = queries.panel_headline(two_sizes, "2019-01-02")
    assert p["uncertain"] == 260
    assert p["actionable"] == 30
    assert p["actionable_pct"] == pytest.approx(11.5, abs=0.1)


def test_panel_headline_is_not_any_single_gene(two_sizes):
    """The reason it is on screen: neither gene's rate is the panel's."""
    genes = queries.genes(two_sizes, "2019-01-02")
    panel = queries.panel_headline(two_sizes, "2019-01-02")["actionable_pct"]
    assert panel not in {g["actionable_pct"] for g in genes}


def test_panel_headline_without_a_baseline_is_none(warehouse):
    assert queries.panel_headline(warehouse, None) is None


# --------------------------------------------------------------------------
# A warehouse with no timeline at all: `serve` before `spans`
# --------------------------------------------------------------------------


def test_a_warehouse_with_no_timeline_answers_rather_than_raises(tmp_path):
    """The demo meets this on a fresh clone, and must say which command to run."""
    import duckdb
    empty = duckdb.connect(str(tmp_path / "empty.duckdb"))
    try:
        assert queries.loaded_panel(empty) == {"panel": None, "alleles": 0, "genes": 0}
        assert queries.release_dates(empty, None) == []
        assert queries.genes(empty, "2019-01-02") == []
        assert queries.panel_headline(empty, "2019-01-02") is None
        assert queries.policy_events(empty) == []
    finally:
        empty.close()


def test_variants_show_past_and_present_together(warehouse):
    """One row must carry both states or the UI has to reconcile two lists."""
    rows = {r["allele_id"]: r for r in rows_on(warehouse, "TESTGENE", "2019-01-02")}
    assert rows["A2"]["bucket"] == "UNCERTAIN"
    assert rows["A2"]["now_bucket"] == "LIKELY_PATHOGENIC"
    assert rows["A2"]["moved"] is True
    assert rows["A1"]["moved"] is False


def test_moved_variants_sort_first(warehouse):
    rows = rows_on(warehouse, "TESTGENE", "2019-01-02")
    assert rows[0]["moved"] is True


def test_absent_variants_are_not_shown_as_of_a_date(warehouse):
    """A4 did not exist in 2019 and must not appear in that view."""
    ids = {r["allele_id"] for r in rows_on(warehouse, "TESTGENE", "2019-01-02")}
    assert "A4" not in ids


# --------------------------------------------------------------------------
# Paging, filtering, searching: a page must never pass for the population
# --------------------------------------------------------------------------


def test_the_total_counts_the_population_not_the_page(warehouse):
    page = queries.variants_on(warehouse, "TESTGENE", "2019-01-02", limit=1)
    assert len(page["rows"]) == 1
    assert page["total"] == 3


def test_an_offset_past_the_end_still_reports_the_total(warehouse):
    page = queries.variants_on(warehouse, "TESTGENE", "2019-01-02", offset=10)
    assert page["rows"] == []
    assert page["total"] == 3


def test_only_moved_keeps_reclassifications_and_retractions(warehouse):
    page = queries.variants_on(warehouse, "TESTGENE", "2019-01-02", only_moved=True)
    assert {r["allele_id"] for r in page["rows"]} == {"A2", "A3"}
    assert page["total"] == 2


def test_numeric_search_matches_the_variation_id(warehouse):
    page = queries.variants_on(warehouse, "TESTGENE", "2019-01-02", q="2")
    assert [r["allele_id"] for r in page["rows"]] == ["A2"]


def test_text_search_matches_the_condition(warehouse):
    assert queries.variants_on(warehouse, "TESTGENE", "2019-01-02", q="test cond")["total"] == 3
    assert queries.variants_on(warehouse, "TESTGENE", "2019-01-02", q="nothing")["total"] == 0


# --------------------------------------------------------------------------
# Policy-suspect movement: the transition AND the release step must match
# --------------------------------------------------------------------------


def event(to_date, from_bucket="UNCERTAIN", to_bucket="LIKELY_PATHOGENIC"):
    return PolicyEvent(from_date="2019-01-02", to_date=to_date, from_bucket=from_bucket,
                       to_bucket=to_bucket, count=1, share=0.3)


def test_movement_at_the_event_step_is_flagged(warehouse):
    """A2's current state began 2022-01-02, which is the event's release step."""
    page = queries.variants_on(warehouse, "TESTGENE", "2019-01-02",
                               events=[event("2022-01-02")])
    by_id = {r["allele_id"]: r for r in page["rows"]}
    assert by_id["A2"]["policy_suspect"] is True
    assert by_id["A3"]["policy_suspect"] is False


def test_the_same_transition_at_another_step_is_evidence(warehouse):
    page = queries.variants_on(warehouse, "TESTGENE", "2019-01-02",
                               events=[event("2025-01-02")])
    assert all(r["policy_suspect"] is False for r in page["rows"])


def test_suspect_rows_sort_after_genuine_movement(warehouse):
    rows = rows_on(warehouse, "TESTGENE", "2019-01-02", events=[event("2022-01-02")])
    assert [r["allele_id"] for r in rows[:2]] == ["A3", "A2"]   # A3 moved for real


def test_headline_counts_suspect_movement_separately(warehouse):
    h = queries.headline(warehouse, "TESTGENE", "2019-01-02", [event("2022-01-02")])
    assert h["moved"] == 2            # A2 reclassified, A3 retracted
    assert h["policy_suspect"] == 1   # A2
    assert h["retracted"] == 1        # A3
    assert queries.headline(warehouse, "TESTGENE", "2019-01-02")["policy_suspect"] == 0


def test_policy_events_are_empty_when_the_dense_table_is_gone(warehouse):
    """An older or trimmed warehouse still serves a timeline; it just cannot flag."""
    warehouse.execute("DROP TABLE assertion_dense")
    assert queries.policy_events(warehouse) == []


# --------------------------------------------------------------------------
# Composition over releases
# --------------------------------------------------------------------------


def test_composition_has_one_entry_per_release(warehouse):
    series = queries.composition(warehouse, "TESTGENE", "testpanel")
    assert [s["on"] for s in series] == ["2019-01-02", "2022-01-02", "2025-01-02"]
    assert series[0]["counts"] == {"UNCERTAIN": 3}
    assert series[1]["counts"] == {"UNCERTAIN": 2, "LIKELY_PATHOGENIC": 1, "BENIGN": 1}
    assert series[2]["total"] == 3


def test_composition_keeps_releases_where_the_gene_is_empty(warehouse):
    series = queries.composition(warehouse, "NOSUCHGENE", "testpanel")
    assert [s["total"] for s in series] == [0, 0, 0]


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
