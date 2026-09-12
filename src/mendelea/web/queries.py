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

import re

import duckdb

from ..evidence import spans
from ..reports import policy

_BUCKET = re.compile(r"^[A-Z_]{1,24}$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


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


def policy_events(connection) -> list[policy.PolicyEvent]:
    """Detected policy events, or none if the warehouse cannot say.

    Detection reads `assertion_dense`, a build artefact `spans.build` leaves
    behind. A warehouse without it (an older build, or a trimmed copy) still
    has a perfectly good timeline; it simply cannot separate relabelling from
    movement, and the UI then says nothing rather than something wrong.
    """
    try:
        return policy.detect(connection)
    except duckdb.Error:
        return []


def _suspect_cte(events: list[policy.PolicyEvent]) -> str:
    """A `suspect(from_bucket, to_bucket, on_date)` CTE from detected events.

    Joined against a variant's transition and the date its current state
    began, this is `policy.suspect_keys` expressed in SQL, so the flag can
    order, filter and count rows without a second pass in Python.

    Interpolated rather than bound: DuckDB binds list parameters slowly and
    VALUES cannot take one. The values come from our own warehouse, never
    from a request, and every one is shape-checked anyway.
    """
    rows = [
        f"('{e.from_bucket}', '{e.to_bucket}', DATE '{e.to_date}')"
        for e in events
        if _BUCKET.match(e.from_bucket) and _BUCKET.match(e.to_bucket)
        and _DATE.match(e.to_date)
    ]
    if not rows:
        return ("suspect(from_bucket, to_bucket, on_date) AS "
                "(SELECT '', '', DATE '1900-01-01' WHERE FALSE)")
    return "suspect(from_bucket, to_bucket, on_date) AS (VALUES " + ", ".join(rows) + ")"


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
WITH {suspect}
SELECT s.allele_id, s.variation_id, s.gene, s.contig, s.pos, s.ref, s.alt,
       s.bucket, s.stars, s.clnsig_raw, s.condition,
       n.bucket AS now_bucket, n.stars AS now_stars, n.valid_from AS changed_on,
       (s.bucket IS DISTINCT FROM n.bucket) AS moved,
       (p.on_date IS NOT NULL) AS policy_suspect,
       COUNT(*) OVER () AS total
FROM assertion_span s
JOIN assertion_span n
  ON n.allele_id = s.allele_id AND n.is_current
LEFT JOIN suspect p
  ON p.from_bucket = s.bucket AND p.to_bucket = n.bucket AND p.on_date = n.valid_from
WHERE s.gene = $gene
  AND s.valid_from <= CAST($on AS DATE)
  AND s.valid_to   >  CAST($on AS DATE)
  AND s.bucket <> 'ABSENT'
  {filters}
ORDER BY moved DESC, policy_suspect, n.stars DESC, s.pos
LIMIT $limit OFFSET $offset
"""

COUNT_SQL = """
SELECT COUNT(*)
FROM assertion_span s
JOIN assertion_span n
  ON n.allele_id = s.allele_id AND n.is_current
WHERE s.gene = $gene
  AND s.valid_from <= CAST($on AS DATE)
  AND s.valid_to   >  CAST($on AS DATE)
  AND s.bucket <> 'ABSENT'
  {filters}
"""


def _filters(only_moved: bool, q: str | None) -> tuple[str, dict]:
    """Optional predicates, and exactly the bindings they use.

    A numeric search matches a ClinVar variation ID or a position prefix;
    anything else searches the condition text. DuckDB rejects named
    parameters a statement does not reference, so bindings are added only
    with the clause that reads them.
    """
    clauses, params = [], {}
    if only_moved:
        clauses.append("AND s.bucket IS DISTINCT FROM n.bucket")
    if q:
        q = q.strip()
        if q.isdigit():
            clauses.append("AND (s.variation_id = $q OR CAST(s.pos AS VARCHAR) LIKE $q_prefix)")
            params.update(q=q, q_prefix=q + "%")
        else:
            clauses.append("AND s.condition ILIKE $q_like")
            params["q_like"] = "%" + q + "%"
    return "\n  ".join(clauses), params


def variants_on(connection, gene: str, on: str, *,
                events: list[policy.PolicyEvent] | None = None,
                only_moved: bool = False, q: str | None = None,
                limit: int = 400, offset: int = 0) -> dict:
    """One page of a gene as the evidence stood on `on`, plus where each variant ended up.

    Returns {"rows": [...], "total": n}. `total` is the size of the whole
    filtered result, not the page, so a caller can never mistake a page for
    the population -- the previous version returned a bare list capped at
    400, and the table showed 400 of the 9,708 BRCA2 variants with nothing
    saying so.

    Each row carries both states: the UI never has to fetch twice or
    reconcile two lists. `policy_suspect` marks a movement that matches a
    detected relabelling event; such rows sort after genuine movement.
    """
    filters, params = _filters(only_moved, q)
    params.update(gene=gene.upper(), on=on, limit=limit, offset=offset)
    rows = connection.execute(
        VARIANTS_SQL.format(suspect=_suspect_cte(events or []), filters=filters), params
    ).fetchall()

    if rows:
        total = rows[0][16]
    else:
        # The window count travels with the rows, so an empty page (an offset
        # past the end) has to ask separately.
        del params["limit"], params["offset"]
        total = connection.execute(COUNT_SQL.format(filters=filters), params).fetchone()[0]

    return {
        "total": total,
        "rows": [
            {
                "allele_id": r[0], "variation_id": r[1], "gene": r[2],
                "contig": r[3], "pos": r[4], "ref": r[5], "alt": r[6],
                "bucket": r[7], "stars": r[8], "clnsig": r[9], "condition": r[10],
                "now_bucket": r[11], "now_stars": r[12],
                "changed_on": str(r[13]) if r[13] else None,
                "moved": bool(r[14]),
                "policy_suspect": bool(r[15]),
            }
            for r in rows
        ],
    }


def headline(connection, gene: str, on: str,
             events: list[policy.PolicyEvent] | None = None) -> dict:
    """The number the demo exists to deliver, with the accounting around it.

    "On this date, N variants here were uncertain. M of them are now
    classified something a laboratory would act on." Alongside: how many
    variants moved at all, how many of those moves match a relabelling
    event rather than evidence, and how many were retracted since.
    """
    row = connection.execute(
        f"""
        WITH {_suspect_cte(events or [])},
        then_ AS (
            SELECT allele_id, bucket FROM assertion_span
            WHERE gene = $gene
              AND valid_from <= CAST($on AS DATE)
              AND valid_to   >  CAST($on AS DATE)
        ),
        now_ AS (
            SELECT allele_id, bucket, valid_from FROM assertion_span
            WHERE gene = $gene AND is_current
        )
        SELECT
            COUNT(*) FILTER (WHERE t.bucket <> 'ABSENT'),
            COUNT(*) FILTER (WHERE t.bucket = 'UNCERTAIN'),
            COUNT(*) FILTER (WHERE t.bucket = 'UNCERTAIN'
                             AND n.bucket IN ('PATHOGENIC','LIKELY_PATHOGENIC')),
            COUNT(*) FILTER (WHERE t.bucket = 'UNCERTAIN'
                             AND n.bucket IN ('BENIGN','LIKELY_BENIGN')),
            COUNT(*) FILTER (WHERE t.bucket <> 'ABSENT'
                             AND t.bucket IS DISTINCT FROM n.bucket),
            COUNT(*) FILTER (WHERE t.bucket <> 'ABSENT' AND p.on_date IS NOT NULL),
            COUNT(*) FILTER (WHERE t.bucket <> 'ABSENT' AND n.bucket = 'ABSENT')
        FROM then_ t
        JOIN now_ n USING (allele_id)
        LEFT JOIN suspect p
          ON p.from_bucket = t.bucket AND p.to_bucket = n.bucket
         AND p.on_date = n.valid_from
        """,
        {"gene": gene.upper(), "on": on},
    ).fetchone()

    classified, uncertain, to_path, to_benign, moved, suspect, retracted = (
        v or 0 for v in row
    )
    actionable = to_path + to_benign
    return {
        "on": on,
        "gene": gene.upper(),
        "classified": classified,
        "uncertain": uncertain,
        "now_pathogenic": to_path,
        "now_benign": to_benign,
        "actionable": actionable,
        "actionable_pct": round(100.0 * actionable / uncertain, 1) if uncertain else 0.0,
        "moved": moved,
        "policy_suspect": suspect,
        "retracted": retracted,
    }


def composition(connection, gene: str, panel: str) -> list[dict]:
    """How a gene's classifications were distributed at every release.

    One entry per release the panel was observed on, even when the gene had
    nothing in it, so the chart's x axis is the panel's coverage and not a
    subset of it.
    """
    rows = connection.execute(
        """
        SELECT r.release_date, s.bucket, COUNT(s.allele_id)
        FROM (SELECT DISTINCT release_date FROM snapshot_manifest WHERE panel = $panel) r
        LEFT JOIN assertion_span s
               ON s.gene = $gene
              AND s.valid_from <= r.release_date
              AND s.valid_to   >  r.release_date
              AND s.bucket <> 'ABSENT'
        GROUP BY 1, 2
        ORDER BY 1
        """,
        {"panel": panel, "gene": gene.upper()},
    ).fetchall()

    by_date: dict[str, dict] = {}
    for release_date, bucket, count in rows:
        entry = by_date.setdefault(
            str(release_date), {"on": str(release_date), "counts": {}, "total": 0}
        )
        if bucket is not None:
            entry["counts"][bucket] = count
            entry["total"] += count
    return list(by_date.values())


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
