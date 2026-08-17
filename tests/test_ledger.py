"""The decision ledger: append-only, and provably so."""

import pytest

from mendelea.db import connect
from mendelea.decisions import ledger


@pytest.fixture
def connection(tmp_path):
    with connect(tmp_path / "ledger.duckdb") as conn:
        ledger.init(conn)
        yield conn


def add(conn, tenant="lab-a", allele="A1", verdict="ACT", rationale="moved to LP"):
    return ledger.append(
        conn,
        tenant_id=tenant,
        allele_id=allele,
        case_ref="CASE-001",
        verdict=verdict,
        rationale=rationale,
        reviewer="curator@lab",
        evidence_snapshot_id="clinvar-20260808-spike",
    )


def test_first_entry_chains_from_genesis(connection):
    entry = add(connection)
    assert entry.seq == 1
    assert entry.prev_hash == ledger.GENESIS


def test_entries_chain_to_their_predecessor(connection):
    first = add(connection)
    second = add(connection, allele="A2")
    assert second.seq == 2
    assert second.prev_hash == first.entry_hash


def test_sequences_are_per_tenant(connection):
    add(connection, tenant="lab-a")
    other = add(connection, tenant="lab-b")
    assert other.seq == 1
    assert other.prev_hash == ledger.GENESIS


def test_verify_passes_on_an_untouched_chain(connection):
    for index in range(5):
        add(connection, allele=f"A{index}")
    assert ledger.verify(connection, "lab-a") == (True, None)


def test_verify_detects_an_edited_entry(connection):
    for index in range(4):
        add(connection, allele=f"A{index}")

    # Rewrite a verdict directly, bypassing append() -- exactly the tampering
    # this design exists to catch.
    connection.execute(
        "UPDATE decision_ledger SET verdict = 'NO_ACT' WHERE seq = 2 AND tenant_id = 'lab-a'"
    )
    intact, bad_seq = ledger.verify(connection, "lab-a")
    assert intact is False
    assert bad_seq == 2


def test_verify_detects_a_deleted_entry(connection):
    for index in range(4):
        add(connection, allele=f"A{index}")

    connection.execute("DELETE FROM decision_ledger WHERE seq = 2 AND tenant_id = 'lab-a'")
    intact, bad_seq = ledger.verify(connection, "lab-a")
    assert intact is False
    assert bad_seq == 3  # the survivor whose prev_hash no longer resolves


def test_tampering_with_one_tenant_does_not_implicate_another(connection):
    add(connection, tenant="lab-a")
    add(connection, tenant="lab-b")
    connection.execute(
        "UPDATE decision_ledger SET rationale = 'edited' WHERE tenant_id = 'lab-a'"
    )
    assert ledger.verify(connection, "lab-a")[0] is False
    assert ledger.verify(connection, "lab-b")[0] is True


def test_empty_chain_verifies(connection):
    assert ledger.verify(connection, "nobody") == (True, None)


def test_unknown_verdict_is_rejected(connection):
    with pytest.raises(ValueError, match="verdict must be one of"):
        add(connection, verdict="MAYBE")


def test_hash_is_stable_for_identical_payloads():
    payload = {"seq": 1, "verdict": "ACT"}
    assert ledger.compute_hash(payload, "abc") == ledger.compute_hash(payload, "abc")


def test_hash_changes_with_history():
    """Same content under a different history must not collide."""
    payload = {"seq": 1, "verdict": "ACT"}
    assert ledger.compute_hash(payload, "abc") != ledger.compute_hash(payload, "def")
