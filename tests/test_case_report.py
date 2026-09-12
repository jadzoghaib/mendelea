"""The reanalysis report.

Most of these test the reconciliation rather than the movement, because
reconciliation is where the report can be quietly, confidently wrong: a
variant we could not examine looks exactly like a variant that did not move.
"""

import pytest

from mendelea.cases import model, report as case_report
from tests.test_policy import build


@pytest.fixture
def evidence(tmp_path):
    """A2 was reclassified; A1 never moved; A3 was retracted."""
    return build(tmp_path, [
        ("2019-01-02", [("A1", "UNCERTAIN", 1), ("A2", "UNCERTAIN", 1),
                        ("A3", "UNCERTAIN", 1)]),
        ("2025-01-02", [("A1", "UNCERTAIN", 1), ("A2", "PATHOGENIC", 3)]),
    ])


def load_cases(connection, rows):
    """rows: (case_ref, allele_id, reported_on)"""
    connection.execute(model.SCHEMA_DDL)
    for case_ref, allele_id, reported_on in rows:
        connection.execute(
            "INSERT INTO case_variant VALUES (?,?,?,?,?,?,?,?,?,?)",
            ["lab", case_ref, allele_id, "TESTGENE", "17", 1000, "C", "T",
             "VUS", reported_on],
        )


def test_reclassified_variant_is_reported(evidence):
    load_cases(evidence, [("C1", "A2", "2019-01-02")])
    rep = case_report.build(evidence, "lab")
    assert rep.moved_to_pathogenic == 1
    assert rep.actionable == 1
    assert rep.findings[0]["evidence_at_signout"] == "UNCERTAIN"
    assert rep.findings[0]["evidence_now"] == "PATHOGENIC"


def test_unchanged_variant_is_not_a_finding(evidence):
    load_cases(evidence, [("C1", "A1", "2019-01-02")])
    rep = case_report.build(evidence, "lab")
    assert rep.unchanged == 1
    assert rep.findings == []


def test_retraction_is_reported(evidence):
    load_cases(evidence, [("C1", "A3", "2019-01-02")])
    rep = case_report.build(evidence, "lab")
    assert rep.retracted == 1


# --------------------------------------------------------------------------
# Reconciliation: the failures that would otherwise be invisible
# --------------------------------------------------------------------------


def test_unknown_variant_is_counted_not_dropped(evidence):
    """A variant absent from our evidence is unknown, not unchanged."""
    load_cases(evidence, [("C1", "NOT_IN_EVIDENCE", "2019-01-02")])
    rep = case_report.build(evidence, "lab")
    assert rep.reconciliation.unmatched == 1
    assert rep.reconciliation.examined == 0
    assert rep.unchanged == 0


def test_signout_before_coverage_is_counted_separately(evidence):
    """We cannot say what the evidence said in 2015; we must not imply we can."""
    load_cases(evidence, [("C1", "A2", "2015-01-01")])
    rep = case_report.build(evidence, "lab")
    assert rep.reconciliation.before_coverage == 1
    assert rep.reconciliation.examined == 0
    assert rep.moved == 0


def test_every_input_row_is_accounted_for(evidence):
    """The invariant that makes the report trustworthy."""
    load_cases(evidence, [
        ("C1", "A2", "2019-01-02"),        # examined, moved
        ("C2", "A1", "2019-01-02"),        # examined, unchanged
        ("C3", "NOT_IN_EVIDENCE", "2019-01-02"),  # unmatched
        ("C4", "A2", "2015-01-01"),        # before coverage
    ])
    r = case_report.build(evidence, "lab").reconciliation
    assert r.loaded == 4
    assert r.unmatched + r.before_coverage + r.examined == r.loaded


def test_low_match_rate_is_flagged_as_unsound(evidence):
    load_cases(evidence, [("C1", "A2", "2019-01-02")]
               + [(f"X{i}", "NOT_IN_EVIDENCE", "2019-01-02") for i in range(9)])
    r = case_report.build(evidence, "lab").reconciliation
    assert r.match_rate == pytest.approx(0.1)
    assert r.is_sound() is False


def test_high_match_rate_is_sound(evidence):
    load_cases(evidence, [("C1", "A2", "2019-01-02"), ("C2", "A1", "2019-01-02")])
    assert case_report.build(evidence, "lab").reconciliation.is_sound() is True


def test_tenants_are_isolated(evidence):
    load_cases(evidence, [("C1", "A2", "2019-01-02")])
    rep = case_report.build(evidence, "other-lab")
    assert rep.reconciliation.loaded == 0


def test_policy_suspect_findings_are_marked(evidence):
    load_cases(evidence, [("C1", "A2", "2019-01-02")])
    rep = case_report.build(evidence, "lab",
                            suspect={("UNCERTAIN", "PATHOGENIC", "2025-01-02")})
    assert rep.policy_suspect == 1
    assert rep.findings[0]["policy_suspect"] is True


def test_the_same_transition_at_another_release_is_not_suspect(evidence):
    """Only movement that landed at the event's own release step is a relabelling."""
    load_cases(evidence, [("C1", "A2", "2019-01-02")])
    rep = case_report.build(evidence, "lab",
                            suspect={("UNCERTAIN", "PATHOGENIC", "2023-06-01")})
    assert rep.policy_suspect == 0
    assert rep.findings[0]["policy_suspect"] is False


def test_coverage_is_scoped_to_the_loaded_panel(evidence, tmp_path):
    """snapshot_manifest accumulates every panel; the header must not report
    a coverage window belonging to a different one."""
    evidence.execute(
        "INSERT INTO snapshot_manifest VALUES "
        "('other-1','clinvar',DATE '2005-01-01','other-panel','u','t','h',1,1,1,1,'g')"
    )
    load_cases(evidence, [("C1", "A2", "2019-01-02")])
    rep = case_report.build(evidence, "lab")
    assert rep.evidence_panel == "testpanel"
    assert rep.evidence_from == "2019-01-02"   # not 2005 from the other panel


def test_actionable_findings_sort_first(evidence):
    load_cases(evidence, [("C1", "A3", "2019-01-02"), ("C2", "A2", "2019-01-02")])
    rep = case_report.build(evidence, "lab")
    assert rep.findings[0]["actionable"] is True
