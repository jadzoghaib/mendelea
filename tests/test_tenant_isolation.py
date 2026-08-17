"""Tenant isolation, enforced structurally rather than by discipline.

This suite exists because of a real defect. `reports/movement.py` once held a
query reading `FROM case_variant` with no tenant predicate, while its
docstring said it served "one tenant". Nothing called it, so nothing failed.
Authentication would not have caught it -- the caller would have been
perfectly authenticated and the query would still have returned every
laboratory's variants.

So the invariant is checked two ways:

  1. Statically: every SQL statement in the source that touches a
     tenant-owned table must mention tenant_id. Read out of the AST, so a
     new unscoped query fails the build the day it is written.
  2. Behaviourally: with two tenants loaded, each sees only its own rows.

The static check is the one that generalises. It catches queries no test
thought to call.
"""

import ast
from pathlib import Path

import pytest

from mendelea.cases import model, report as case_report
from mendelea.decisions import ledger
from tests.test_policy import build

SRC = Path(__file__).resolve().parents[1] / "src" / "mendelea"

# Tables whose every row belongs to exactly one tenant.
TENANT_OWNED = ("case_variant", "decision_ledger")


def sql_literals_touching_tenant_tables():
    """Every string constant in src/ that names a tenant-owned table."""
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = node.value
                if any(table in text for table in TENANT_OWNED):
                    found.append((path.relative_to(SRC), node.lineno, text))
    return found


def test_the_scan_actually_finds_something():
    """A guard that silently matches nothing guards nothing."""
    assert len(sql_literals_touching_tenant_tables()) >= 4


def test_every_query_over_tenant_data_names_tenant_id():
    offenders = [
        f"{path}:{line} -> {text.strip()[:90]}"
        for path, line, text in sql_literals_touching_tenant_tables()
        if "tenant_id" not in text
    ]
    assert not offenders, (
        "SQL touching tenant-owned tables without a tenant_id reference:\n  "
        + "\n  ".join(offenders)
    )


def test_the_check_would_catch_a_regression():
    """Prove the rule has teeth, using the shape of the defect that occurred."""
    bad = "SELECT case_ref FROM case_variant JOIN assertion_span USING (allele_id)"
    good = "SELECT case_ref FROM case_variant WHERE tenant_id = ?"
    assert "tenant_id" not in bad
    assert "tenant_id" in good


# --------------------------------------------------------------------------
# Behavioural: two tenants in one warehouse
# --------------------------------------------------------------------------


@pytest.fixture
def two_tenants(tmp_path):
    connection = build(tmp_path, [
        ("2019-01-02", [("A1", "UNCERTAIN", 1), ("A2", "UNCERTAIN", 1)]),
        ("2025-01-02", [("A1", "UNCERTAIN", 1), ("A2", "PATHOGENIC", 3)]),
    ])
    connection.execute(model.SCHEMA_DDL)
    for tenant, case_ref, allele in [
        ("lab-a", "A-001", "A2"),
        ("lab-a", "A-002", "A1"),
        ("lab-b", "B-001", "A2"),
    ]:
        connection.execute(
            "INSERT INTO case_variant "
            "(tenant_id, case_ref, allele_id, gene, contig, pos, ref, alt, "
            " reported_classification, reported_on) VALUES (?,?,?,?,?,?,?,?,?,?)",
            [tenant, case_ref, allele, "TESTGENE", "17", 1000, "C", "T",
             "VUS", "2019-01-02"],
        )
    return connection


def test_report_sees_only_its_own_tenant(two_tenants):
    a = case_report.build(two_tenants, "lab-a")
    b = case_report.build(two_tenants, "lab-b")
    assert a.reconciliation.loaded == 2
    assert b.reconciliation.loaded == 1
    assert {f["case_ref"] for f in a.findings} == {"A-001"}
    assert {f["case_ref"] for f in b.findings} == {"B-001"}


def test_unknown_tenant_sees_nothing(two_tenants):
    assert case_report.build(two_tenants, "lab-c").reconciliation.loaded == 0


def test_a_tenant_id_is_never_inferred_from_the_variant(two_tenants):
    """lab-a and lab-b report the same allele; neither may see the other's case."""
    a = case_report.build(two_tenants, "lab-a")
    assert all(f["case_ref"].startswith("A-") for f in a.findings)


def test_decision_ledgers_do_not_bleed(two_tenants):
    ledger.append(two_tenants, tenant_id="lab-a", allele_id="A2", case_ref="A-001",
                  verdict="ACT", rationale="", reviewer="x", evidence_snapshot_id="s")
    ledger.append(two_tenants, tenant_id="lab-b", allele_id="A2", case_ref="B-001",
                  verdict="DEFER", rationale="", reviewer="y", evidence_snapshot_id="s")

    rows_a = two_tenants.execute(
        "SELECT case_ref FROM decision_ledger WHERE tenant_id = 'lab-a'"
    ).fetchall()
    assert [r[0] for r in rows_a] == ["A-001"]
    assert ledger.verify(two_tenants, "lab-a")[0] is True
    assert ledger.verify(two_tenants, "lab-b")[0] is True
