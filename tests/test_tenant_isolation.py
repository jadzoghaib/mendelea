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


def _literal_text(node) -> str | None:
    """The literal text of a string node, including f-strings.

    f-strings are `JoinedStr`, not `Constant`. An earlier version of this
    scanner walked only `Constant`, so an f-string SQL statement would have
    slipped past it entirely -- a guard with a silent bypass, which is worse
    than no guard because it manufactures confidence. Interpolated values are
    ignored; only the literal parts are inspected, which is where a table name
    and a WHERE clause actually appear.
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.JoinedStr):
        return "".join(
            part.value for part in node.values
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        )
    return None


def _walk_strings(tree):
    """Yield string nodes without descending into f-strings twice.

    `ast.walk` would also visit each `Constant` fragment inside a `JoinedStr`,
    and a fragment like "FROM case_variant " on its own would look like a
    violation even when the WHERE clause sits in the next fragment.
    """
    stack = [tree]
    while stack:
        node = stack.pop()
        if isinstance(node, ast.JoinedStr):
            yield node
            continue  # handled whole; do not recurse into its parts
        if isinstance(node, ast.Constant):
            yield node
            continue
        stack.extend(ast.iter_child_nodes(node))


def sql_literals_touching_tenant_tables():
    """Every string literal in src/ that names a tenant-owned table.

    Residual limit, stated rather than hidden: SQL assembled by concatenating
    two separate literals, where one holds the table name and the other the
    predicate, is not caught. Keep statements in one literal.
    """
    found = []
    for path in sorted(SRC.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in _walk_strings(tree):
            text = _literal_text(node)
            if text and any(table in text for table in TENANT_OWNED):
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


def scan_source(source: str):
    """Run the same rule over a source string, for testing the scanner itself."""
    tree = ast.parse(source)
    return [
        text for node in _walk_strings(tree)
        if (text := _literal_text(node))
        and any(table in text for table in TENANT_OWNED)
        and "tenant_id" not in text
    ]


def test_the_check_catches_a_plain_unscoped_query():
    """The exact shape of the defect that occurred."""
    assert scan_source(
        'q = "SELECT case_ref FROM case_variant JOIN assertion_span USING (allele_id)"'
    )


def test_the_check_catches_an_f_string_query():
    """The bypass an earlier version of this scanner had."""
    assert scan_source('q = f"SELECT case_ref FROM case_variant WHERE gene = {g}"')


def test_a_scoped_f_string_passes():
    assert not scan_source(
        'q = f"SELECT case_ref FROM case_variant WHERE tenant_id = {t} AND gene = {g}"'
    )


def test_an_f_string_is_judged_whole_not_fragment_by_fragment():
    """Fragments either side of an interpolation must be read as one statement."""
    assert not scan_source(
        'q = f"SELECT * FROM case_variant WHERE gene={g} AND tenant_id = ?"'
    )


def test_a_scoped_plain_query_passes():
    assert not scan_source('q = "SELECT case_ref FROM case_variant WHERE tenant_id = ?"')


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
