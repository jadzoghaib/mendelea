"""Read-only queries behind the time machine.

Kept free of any HTTP concern so the transport underneath can change without
touching them: today a stdlib server, tomorrow FastAPI, or a batch export for
an air-gapped deployment. Every function takes a connection and returns plain
Python.

All of these are point-in-time reads against `assertion_span`. That they are
this short is the entire payoff of the bitemporal model -- "what did the
evidence say on date D" is a range predicate, not a reconstruction.
"""

from __future__ import annotations

from ..evidence import spans


def loaded_panel(connection) -> dict:
    """Which panel the warehouse currently holds, and its extent."""
    row = connection.execute(
        "SELECT COUNT(DISTINCT allele_id), COUNT(DISTINCT gene) FROM assertion_span"
    ).fetchone()
    return {"panel": spans.loaded_panel(connection), "alleles": row[0], "genes": row[1]}


def release_dates(connection, panel: str) -> list[str]:
    """Release dates ingested for this panel, oldest first. The slider stops.

    Delegates to `spans.release_dates`, which is the one place that knows the
    manifest table spans every panel ever ingested. Offering a date this panel
    was never observed on would present carried-forward state as a measurement.
    """
    return spans.release_dates(connection, panel)


def genes(connection, allowed: set[str] | None = None) -> list[dict]:
    """Panel genes, with how much has ever moved in each.

    `allowed` restricts the result to the panel's own gene list. Regions are
    fetched with 5 kb of flanking sequence, so ClinVar records belonging to
    neighbouring genes come along too -- 62 gene symbols appear in a 31-gene
    panel. Those neighbours are legitimately in the data but are not what the
    panel is about, and being tiny they dominate any rate-based ordering: a
    gene with 8 variants trivially shows "100% moved".
    """
    # "Moved" means the allele has held two or more *real* classifications.
    # Counting spans instead would count a variant merely appearing in ClinVar
    # (ABSENT -> UNCERTAIN) as movement, which puts every gene above 90% and
    # reads, to a laboratory, as "94% of these were reclassified". They were
    # not. Only transitions between genuine classifications count.
    rows = connection.execute(
        """
        WITH per_allele AS (
            SELECT gene, allele_id,
                   COUNT(DISTINCT bucket) FILTER (WHERE bucket <> 'ABSENT') AS states
            FROM assertion_span
            GROUP BY gene, allele_id
        )
        SELECT gene,
               COUNT(*) AS alleles,
               COUNT(*) FILTER (WHERE states > 1) AS moved
        FROM per_allele
        GROUP BY gene
        """
    ).fetchall()

    out = [
        {"gene": r[0], "alleles": r[1], "moved": r[2],
         "moved_pct": round(100.0 * r[2] / r[1], 1) if r[1] else 0.0}
        for r in rows
        if allowed is None or r[0] in allowed
    ]
    out.sort(key=lambda g: (-g["moved_pct"], -g["alleles"]))
    return out


VARIANTS_SQL = """
SELECT s.allele_id, s.variation_id, s.gene, s.contig, s.pos, s.ref, s.alt,
       s.bucket, s.stars, s.clnsig_raw, s.condition,
       n.bucket AS now_bucket, n.stars AS now_stars, n.valid_from AS changed_on
FROM assertion_span s
JOIN assertion_span n
  ON n.allele_id = s.allele_id AND n.is_current
WHERE s.gene = $gene
  AND s.valid_from <= CAST($on AS DATE)
  AND s.valid_to   >  CAST($on AS DATE)
  AND s.bucket <> 'ABSENT'
ORDER BY (s.bucket IS DISTINCT FROM n.bucket) DESC, n.stars DESC, s.pos
LIMIT $limit
"""


def variants_on(connection, gene: str, on: str, limit: int = 400) -> list[dict]:
    """Every variant in a gene as the evidence stood on `on`, plus where it ended up.

    Returning both states in one row is what makes the diff immediate: the UI
    never has to fetch twice or reconcile two lists.
    """
    rows = connection.execute(
        VARIANTS_SQL, {"gene": gene.upper(), "on": on, "limit": limit}
    ).fetchall()
    return [
        {
            "allele_id": r[0], "variation_id": r[1], "gene": r[2],
            "contig": r[3], "pos": r[4], "ref": r[5], "alt": r[6],
            "bucket": r[7], "stars": r[8], "clnsig": r[9], "condition": r[10],
            "now_bucket": r[11], "now_stars": r[12],
            "changed_on": str(r[13]) if r[13] else None,
            "moved": r[7] != r[11],
        }
        for r in rows
    ]


def headline(connection, gene: str, on: str) -> dict:
    """The number the demo exists to deliver.

    "On this date, N variants here were uncertain. M of them are now
    classified something a laboratory would act on."
    """
    row = connection.execute(
        """
        WITH then_ AS (
            SELECT allele_id, bucket FROM assertion_span
            WHERE gene = $gene
              AND valid_from <= CAST($on AS DATE)
              AND valid_to   >  CAST($on AS DATE)
        ),
        now_ AS (
            SELECT allele_id, bucket FROM assertion_span
            WHERE gene = $gene AND is_current
        )
        SELECT
            COUNT(*) FILTER (WHERE t.bucket <> 'ABSENT'),
            COUNT(*) FILTER (WHERE t.bucket = 'UNCERTAIN'),
            COUNT(*) FILTER (WHERE t.bucket = 'UNCERTAIN'
                             AND n.bucket IN ('PATHOGENIC','LIKELY_PATHOGENIC')),
            COUNT(*) FILTER (WHERE t.bucket = 'UNCERTAIN'
                             AND n.bucket IN ('BENIGN','LIKELY_BENIGN'))
        FROM then_ t JOIN now_ n USING (allele_id)
        """,
        {"gene": gene.upper(), "on": on},
    ).fetchone()

    classified, uncertain, to_path, to_benign = row
    actionable = (to_path or 0) + (to_benign or 0)
    return {
        "on": on,
        "gene": gene.upper(),
        "classified": classified or 0,
        "uncertain": uncertain or 0,
        "now_pathogenic": to_path or 0,
        "now_benign": to_benign or 0,
        "actionable": actionable,
        "actionable_pct": round(100.0 * actionable / uncertain, 1) if uncertain else 0.0,
    }


def timeline(connection, allele_id: str) -> list[dict]:
    """One variant's full history: every state it has held, and when."""
    rows = connection.execute(
        """
        SELECT bucket, stars, clnsig_raw, valid_from, valid_to, is_current
        FROM assertion_span
        WHERE allele_id = ?
        ORDER BY valid_from
        """,
        [allele_id],
    ).fetchall()
    return [
        {
            "bucket": r[0], "stars": r[1], "clnsig": r[2],
            "from": str(r[3]),
            # The open-ended sentinel is an implementation detail; the UI
            # should say "now", not "9999-12-31".
            "to": None if str(r[4]).startswith("9999") else str(r[4]),
            "is_current": bool(r[5]),
        }
        for r in rows
    ]
