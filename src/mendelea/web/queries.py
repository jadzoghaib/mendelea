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
    """Which panel the warehouse currently holds, and its extent.

    A warehouse with no timeline at all is a state the demo meets in the
    wild -- `serve` before `spans`, on a fresh clone -- so it answers
    "nothing loaded" rather than raising. The page then says which command to
    run; a 500 would have said "internal error".
    """
    try:
        row = connection.execute(
            "SELECT COUNT(DISTINCT allele_id), COUNT(DISTINCT gene) FROM assertion_span"
        ).fetchone()
    except duckdb.CatalogException:
        return {"panel": None, "alleles": 0, "genes": 0}
    return {"panel": spans.loaded_panel(connection), "alleles": row[0], "genes": row[1]}


def release_dates(connection, panel: str | None) -> list[str]:
    """Release dates ingested for this panel, oldest first. The slider stops.

    Delegates to `spans.release_dates`, which is the one place that knows the
    manifest table spans every panel ever ingested. Offering a date this panel
    was never observed on would present carried-forward state as a measurement.
    """
    if panel is None:
        return []
    try:
        return spans.release_dates(connection, panel)
    except duckdb.CatalogException:
        return []


def timeline_start(connection) -> str | None:
    """The earliest date the timeline can actually speak about.

    Not the same as the earliest release: `spans.build` records every
    manifest but skips any whose Parquet file is missing, which `provenance`
    reports and a half-synced data directory produces. The slider still stops
    on every ingested release, because a release we hold no data for is a
    real gap and hiding it would be worse. But anchoring the gene ranking to
    a date no span covers returned an empty list, and the page reads an empty
    gene list as "no timeline at all".
    """
    try:
        row = connection.execute("SELECT MIN(valid_from) FROM assertion_span").fetchone()
    except duckdb.CatalogException:
        return None
    return str(row[0]) if row and row[0] else None


def policy_events(connection,
                  threshold: float = policy.DEFAULT_THRESHOLD) -> list[policy.PolicyEvent]:
    """Detected policy events, or none if the warehouse cannot say.

    Detection reads `assertion_dense`, a build artefact `spans.build` leaves
    behind. A warehouse without it (an older build, or a trimmed copy) still
    has a perfectly good timeline; it simply cannot separate relabelling from
    movement, and the UI then says nothing rather than something wrong.

    Only a missing table is tolerated. Catching every DuckDB error here would
    turn a broken `DETECT_SQL` into a silently disabled safety feature, and
    the whole point of this one is that it fails loudly enough to be noticed.

    The threshold is a parameter because it is a share of the corpus, and the
    corpus depends on the panel. The 2023 re-aggregation sweeps 5,843 variants
    of the five-gene panel, which is 13.3% of it, and 6,083 of the thirty-one
    gene panel, which is only 4.05% of that much larger corpus -- so a single
    default cannot catch both. `spike` has had this as a flag
    from the start; without it here the demo silently stopped reporting an
    event it had found, which is the failure mode this detector exists to
    prevent, turned on itself.
    """
    try:
        return policy.detect(connection, threshold=threshold)
    except duckdb.CatalogException:
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


# A rate computed from no more uncertain variants than this is not a headline.
# The README quotes per-gene rates only *above* this bar for the same reason,
# and the comparison here matches it exactly: the thinnest declared gene in
# the 31-gene panel carries 91 uncertain variants at baseline, and seven of
# them moving reads as "7.7%".
HEADLINE_VUS_FLOOR = 200

ACTIONABLE_BY_GENE_SQL = """
WITH then_ AS (
    SELECT allele_id, gene, bucket FROM assertion_span
    WHERE valid_from <= CAST($on AS DATE) AND valid_to > CAST($on AS DATE)
),
now_ AS (
    SELECT allele_id, bucket FROM assertion_span WHERE is_current
)
SELECT t.gene,
       COUNT(*) FILTER (WHERE t.bucket = 'UNCERTAIN') AS vus,
       COUNT(*) FILTER (WHERE t.bucket = 'UNCERTAIN' AND n.bucket IN
              ('PATHOGENIC','LIKELY_PATHOGENIC','BENIGN','LIKELY_BENIGN')) AS actionable
FROM then_ t JOIN now_ n USING (allele_id)
GROUP BY t.gene
"""


def genes(connection, on: str | None, allowed: set[str] | None = None) -> list[dict]:
    """Panel genes, ranked by the rate the product actually reports.

    They were ranked by how many variants had ever held two classifications,
    which is the movement the rest of the system is at pains not to quote: it
    counts UNCERTAIN -> CONFLICTING, so the list led with BRCA2 at 28% beside
    a headline of 4.6%, and put RAD51C sixth at 22% where 0.3% is actionable.
    A picker that promises what the view does not deliver is worse than no
    picker. The rate here is the one in the headline, measured from the same
    baseline.

    `allowed` restricts the result to the panel's own gene list. Regions are
    fetched with 5 kb of flanking sequence, so ClinVar records belonging to
    neighbouring genes come along too -- 62 gene symbols appear in a 31-gene
    panel. Those neighbours are legitimately in the data but are not what the
    panel is about, and being tiny they dominate any rate-based ordering.
    """
    if not on:
        return []
    try:
        rows = connection.execute(ACTIONABLE_BY_GENE_SQL, {"on": on}).fetchall()
    except duckdb.CatalogException:
        return []

    out = [
        {"gene": r[0], "vus": r[1], "actionable": r[2],
         "actionable_pct": round(100.0 * r[2] / r[1], 1) if r[1] else 0.0,
         "thin": r[1] <= HEADLINE_VUS_FLOOR}
        for r in rows
        if allowed is None or r[0] in allowed
    ]
    # Thin genes stay selectable but sort below the ones whose rate carries
    # weight, so the list opens on a story that survives being questioned.
    out.sort(key=lambda g: (g["thin"], -g["actionable_pct"], -g["vus"]))
    return out


PANEL_HEADLINE_SQL = """
WITH then_ AS (
    SELECT allele_id, bucket FROM assertion_span
    WHERE valid_from <= CAST($on AS DATE) AND valid_to > CAST($on AS DATE)
),
now_ AS (
    SELECT allele_id, bucket FROM assertion_span WHERE is_current
)
SELECT COUNT(*) FILTER (WHERE t.bucket = 'UNCERTAIN'),
       COUNT(*) FILTER (WHERE t.bucket = 'UNCERTAIN' AND n.bucket IN
              ('PATHOGENIC','LIKELY_PATHOGENIC')),
       COUNT(*) FILTER (WHERE t.bucket = 'UNCERTAIN' AND n.bucket IN
              ('BENIGN','LIKELY_BENIGN'))
FROM then_ t JOIN now_ n USING (allele_id)
"""


def panel_headline(connection, on: str | None) -> dict | None:
    """The whole panel's actionable rate: the Phase 0 gate, in the browser.

    The demo could only answer "how much moved in this gene", and the first
    question a laboratory asks is about its whole back catalogue. That number
    lived in the CLI, which is no use in a meeting.

    Counted over every gene in the timeline rather than the panel's declared
    list, because that is what `movement.panel_movement` counts and the two
    must not disagree in front of a customer. On the 31-gene panel the
    flanking neighbours move it by 0.01 of a percentage point.
    """
    if not on:
        return None
    try:
        vus, to_path, to_benign = connection.execute(
            PANEL_HEADLINE_SQL, {"on": on}
        ).fetchone()
    except duckdb.CatalogException:
        return None
    actionable = (to_path or 0) + (to_benign or 0)
    vus = vus or 0
    return {
        "on": on,
        "uncertain": vus,
        "actionable": actionable,
        "actionable_pct": round(100.0 * actionable / vus, 1) if vus else 0.0,
    }


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


def _like_escape(value: str) -> str:
    """Neutralise LIKE wildcards in a user's search term.

    Unescaped, `_` matches any single character, so searching a condition
    containing one quietly returns unrelated conditions as well. The
    backslash must go first or it would escape the escapes.
    """
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


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
            clauses.append(r"AND s.condition ILIKE $q_like ESCAPE '\'")
            params["q_like"] = "%" + _like_escape(q) + "%"
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
