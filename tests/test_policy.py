"""Policy-event detection.

Guards the distinction the product's credibility rests on: a database changing
how it aggregates is not a laboratory's variants changing meaning. Reporting
the former as the latter sends a customer to re-review thousands of cases for
nothing.

Modelled on the real event found in ClinVar: 6,107 variants moved
UNCERTAIN -> CONFLICTING in one release step, 14.5% of the corpus, against
1-3% in adjacent steps.
"""

import duckdb
import pytest

from mendelea.evidence import spans
from mendelea.reports import policy
from tests.test_spans import PANEL, write_snapshot


def build(tmp_path, releases):
    """releases: list of (date, [(allele_id, bucket, stars), ...])

    Uses duckdb.connect directly rather than mendelea.db.connect: the latter
    is a context manager, and holding a bare reference to its yielded value
    lets the generator close the connection out from under the test.
    """
    root = tmp_path / "snapshots"
    manifests = [write_snapshot(root, date, rows) for date, rows in releases]
    connection = duckdb.connect(str(tmp_path / "policy.duckdb"))
    spans.build(connection, root, manifests, PANEL)
    return connection


def cohort(n, bucket, start=0):
    return [(f"A{i}", bucket, 1) for i in range(start, start + n)]


@pytest.fixture
def swept(tmp_path):
    """100 VUS; 60 relabelled at once (policy), 2 genuinely reclassified."""
    return build(tmp_path, [
        ("2019-01-02", cohort(100, "UNCERTAIN")),
        ("2022-01-02",
         cohort(60, "CONFLICTING") + cohort(2, "PATHOGENIC", 60)
         + cohort(38, "UNCERTAIN", 62)),
        ("2023-01-02",
         cohort(60, "CONFLICTING") + cohort(2, "PATHOGENIC", 60)
         + cohort(3, "BENIGN", 62) + cohort(35, "UNCERTAIN", 65)),
    ])


def test_detects_the_mass_relabelling(swept):
    events = policy.detect(swept, threshold=0.05)
    pairs = {e.pair for e in events}
    assert ("UNCERTAIN", "CONFLICTING") in pairs


def test_does_not_flag_ordinary_movement(swept):
    """2% and 3% transitions are normal and must survive."""
    events = policy.detect(swept, threshold=0.05)
    pairs = {e.pair for e in events}
    assert ("UNCERTAIN", "PATHOGENIC") not in pairs
    assert ("UNCERTAIN", "BENIGN") not in pairs


def test_event_reports_the_right_magnitude(swept):
    event = next(e for e in policy.detect(swept, 0.05)
                 if e.pair == ("UNCERTAIN", "CONFLICTING"))
    assert event.count == 60
    assert event.share == pytest.approx(0.60, abs=0.01)
    assert event.from_date == "2019-01-02"
    assert event.to_date == "2022-01-02"


def test_threshold_is_honoured(swept):
    assert policy.detect(swept, threshold=0.99) == []
    assert len(policy.detect(swept, threshold=0.01)) > 1


def test_appearance_and_retraction_are_not_policy_events(tmp_path):
    """Corpus growth would otherwise swamp every step and flag everything."""
    connection = build(tmp_path, [
        ("2019-01-02", cohort(10, "UNCERTAIN")),
        # 90 brand-new variants appear: ABSENT -> UNCERTAIN for 90% of the grid.
        ("2022-01-02", cohort(100, "UNCERTAIN")),
    ])
    events = policy.detect(connection, threshold=0.05)
    assert all("ABSENT" not in e.pair for e in events)
    assert events == []


def test_suspect_keys_carry_the_event_release_date(swept):
    """The date is part of the key: the same transition at another release is evidence."""
    keys = policy.suspect_keys(policy.detect(swept, 0.05))
    assert ("UNCERTAIN", "CONFLICTING", "2022-01-02") in keys
    assert all(len(key) == 3 for key in keys)


def test_step_series_returns_everything_unfiltered(swept):
    series = policy.step_series(swept)
    assert len(series) >= len(policy.detect(swept, 0.05))
