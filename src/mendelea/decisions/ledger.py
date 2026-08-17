"""Append-only, hash-chained decision ledger.

The reviews a laboratory records are the only asset in this system that
accumulates and cannot be recreated from public data. They are also the
artefact an auditor asks for. So the ledger is append-only and each entry
commits to its predecessor: altering or removing any past entry breaks every
hash after it, and `verify` finds the break.

This replaces browser localStorage, which was the previous design and which
lost the asset on a cache clear while proving nothing about integrity.

Scope note: this is tamper-*evident*, not tamper-proof. It detects edits made
through any path other than `append`. It is not a substitute for database
access control, and the hash chain is not a signature -- an actor who can
rewrite the whole table can recompute the whole chain. Per-entry signing is
the Phase 4 upgrade, once there is an identity provider to sign against.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

GENESIS = "0" * 64

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS decision_ledger (
    seq             BIGINT,
    tenant_id       VARCHAR,
    allele_id       VARCHAR,
    case_ref        VARCHAR,
    verdict         VARCHAR,
    rationale       VARCHAR,
    reviewer        VARCHAR,
    evidence_snapshot_id VARCHAR,
    created_at      VARCHAR,
    prev_hash       VARCHAR,
    entry_hash      VARCHAR
);
"""

VERDICTS = {"ACT", "NO_ACT", "DEFER", "RECONTACT"}


@dataclass(frozen=True)
class Entry:
    seq: int
    tenant_id: str
    allele_id: str
    case_ref: str
    verdict: str
    rationale: str
    reviewer: str
    evidence_snapshot_id: str
    created_at: str
    prev_hash: str
    entry_hash: str


def compute_hash(payload: dict, prev_hash: str) -> str:
    """Commit to the entry's content and to the entire history before it.

    Serialisation is sorted and separator-pinned so the digest is stable
    across Python versions and platforms.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(f"{prev_hash}\n{blob}".encode()).hexdigest()


def init(connection) -> None:
    connection.execute(SCHEMA_DDL)


def append(connection, tenant_id: str, allele_id: str, case_ref: str,
           verdict: str, rationale: str, reviewer: str,
           evidence_snapshot_id: str) -> Entry:
    """Add one review. The only supported way to write to the ledger."""
    if verdict not in VERDICTS:
        raise ValueError(f"verdict must be one of {sorted(VERDICTS)}, got {verdict!r}")

    init(connection)
    row = connection.execute(
        "SELECT seq, entry_hash FROM decision_ledger "
        "WHERE tenant_id = ? ORDER BY seq DESC LIMIT 1",
        [tenant_id],
    ).fetchone()
    seq = (row[0] + 1) if row else 1
    prev_hash = row[1] if row else GENESIS

    payload = {
        "seq": seq,
        "tenant_id": tenant_id,
        "allele_id": allele_id,
        "case_ref": case_ref,
        "verdict": verdict,
        "rationale": rationale,
        "reviewer": reviewer,
        "evidence_snapshot_id": evidence_snapshot_id,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    entry_hash = compute_hash(payload, prev_hash)

    connection.execute(
        "INSERT INTO decision_ledger VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        [
            payload["seq"], payload["tenant_id"], payload["allele_id"],
            payload["case_ref"], payload["verdict"], payload["rationale"],
            payload["reviewer"], payload["evidence_snapshot_id"],
            payload["created_at"], prev_hash, entry_hash,
        ],
    )
    return Entry(**payload, prev_hash=prev_hash, entry_hash=entry_hash)


def verify(connection, tenant_id: str) -> tuple[bool, int | None]:
    """Recompute the chain. Returns (intact, first_bad_seq)."""
    init(connection)
    rows = connection.execute(
        """
        SELECT seq, tenant_id, allele_id, case_ref, verdict, rationale,
               reviewer, evidence_snapshot_id, created_at, prev_hash, entry_hash
        FROM decision_ledger WHERE tenant_id = ? ORDER BY seq
        """,
        [tenant_id],
    ).fetchall()

    expected_prev = GENESIS
    for row in rows:
        payload = {
            "seq": row[0], "tenant_id": row[1], "allele_id": row[2],
            "case_ref": row[3], "verdict": row[4], "rationale": row[5],
            "reviewer": row[6], "evidence_snapshot_id": row[7],
            "created_at": row[8],
        }
        if row[9] != expected_prev or compute_hash(payload, row[9]) != row[10]:
            return False, row[0]
        expected_prev = row[10]

    return True, None
