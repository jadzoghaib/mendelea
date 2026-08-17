"""The case plane: a laboratory's own reported variants.

Deliberately the smallest surface in the system. A tenant supplies coordinates,
what they reported, and when -- nothing else. No genome, no phenotype, no
identifier, no date of birth.

That constraint is a design decision, not an MVP shortcut. Keeping personal
data out means GDPR Article 9 (special-category health data) does not engage,
the security review a hospital runs shrinks to something a small team can
answer, and the product can be demonstrated on real customer data in a first
meeting. `case_ref` is the laboratory's own pseudonymous key; we never learn
what it points to, and it must not be a patient identifier.

Ten years of a mid-size laboratory's reporting is on the order of tens of
thousands of rows -- a few megabytes. The expensive plane is the shared public
evidence; the private plane is tiny. That asymmetry is what makes serving many
tenants from modest infrastructure possible.
"""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from ..evidence.normalize import allele_id

SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS case_variant (
    tenant_id               VARCHAR,
    case_ref                VARCHAR,
    allele_id               VARCHAR,
    gene                    VARCHAR,
    contig                  VARCHAR,
    pos                     BIGINT,
    ref                     VARCHAR,
    alt                     VARCHAR,
    reported_classification VARCHAR,
    reported_on             DATE
);
"""

REQUIRED_COLUMNS = {"case_ref", "contig", "pos", "ref", "alt",
                    "reported_classification", "reported_on"}


@dataclass(frozen=True)
class CaseVariant:
    tenant_id: str
    case_ref: str
    allele_id: str
    gene: str | None
    contig: str
    pos: int
    ref: str
    alt: str
    reported_classification: str
    reported_on: date


class CaseLoadError(ValueError):
    pass


def load_csv(path: Path, tenant_id: str, assembly: str = "GRCh38",
             reference=None) -> tuple[list[CaseVariant], list[str]]:
    """Parse a laboratory's export. Returns (variants, rejected row descriptions).

    Pass `reference` to left-align indels while normalising. Unlike ClinVar,
    a laboratory export carries no guarantee of left-alignment, and an
    unaligned indel hashes to an identifier that matches nothing -- it drops
    out of the report silently, as an absence rather than an error.

    Rejections are returned rather than raised: a real export always has a few
    unparseable rows, and silently dropping them would corrupt the denominator
    of every statistic we later report back to the customer.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        columns = set(reader.fieldnames or [])
        missing = REQUIRED_COLUMNS - columns
        if missing:
            raise CaseLoadError(f"missing required columns: {sorted(missing)}")

        variants: list[CaseVariant] = []
        rejected: list[str] = []

        for line_no, row in enumerate(reader, start=2):
            try:
                key = allele_id(
                    assembly, row["contig"], int(row["pos"]),
                    row["ref"], row["alt"], reference=reference,
                )
                variants.append(
                    CaseVariant(
                        tenant_id=tenant_id,
                        case_ref=row["case_ref"].strip(),
                        allele_id=key,
                        gene=(row.get("gene") or "").strip().upper() or None,
                        contig=row["contig"].strip(),
                        pos=int(row["pos"]),
                        ref=row["ref"].strip().upper(),
                        alt=row["alt"].strip().upper(),
                        reported_classification=row["reported_classification"].strip(),
                        reported_on=date.fromisoformat(row["reported_on"].strip()),
                    )
                )
            except (ValueError, KeyError, AttributeError) as exc:
                rejected.append(f"line {line_no}: {exc}")

    return variants, rejected


def persist(connection, variants: list[CaseVariant]) -> int:
    connection.execute(SCHEMA_DDL)
    for variant in variants:
        # Columns named explicitly, not positional: it keeps tenant_id visible
        # in the statement, which is what the isolation check reads.
        connection.execute(
            "INSERT INTO case_variant "
            "(tenant_id, case_ref, allele_id, gene, contig, pos, ref, alt, "
            " reported_classification, reported_on) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            [
                variant.tenant_id, variant.case_ref, variant.allele_id,
                variant.gene, variant.contig, variant.pos, variant.ref,
                variant.alt, variant.reported_classification, variant.reported_on,
            ],
        )
    return len(variants)
