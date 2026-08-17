"""Movement detection: what has changed since a variant was signed out.

This is the product. Everything else in the repository exists so that this
query can be six lines of SQL instead of a research project.

Two shapes:

  panel_movement  -- population level, needs no customer. "Of everything
                     classified VUS in 2019, how much has moved by now?"
                     This is the Phase 0 gate and the number that decides
                     whether the business is real.

  case_movement   -- customer level. "Of the variants *you* reported, which
                     have moved since *your* sign-out date?" Same join, with
                     the laboratory's own dates supplying the left edge.

Note on what is claimed: this reports that the *evidence* moved. It never
asserts a classification. That distinction is the entire regulatory position
-- reporting a documented change in a public database is not a diagnostic
claim, and it is what keeps this the right side of IVDR while the product is
research-use-only.
"""

from __future__ import annotations

from dataclasses import dataclass

# Buckets a laboratory would act on if a variant it reported as VUS landed there.
ACTIONABLE_BUCKETS = ("PATHOGENIC", "LIKELY_PATHOGENIC", "BENIGN", "LIKELY_BENIGN")


@dataclass
class MovementSummary:
    baseline_date: str
    current_date: str
    total_tracked: int
    vus_at_baseline: int
    vus_moved: int
    moved_to_pathogenic: int
    moved_to_benign: int
    moved_to_conflicting: int
    retracted: int
    # Moved variants whose net transition matches a detected policy event --
    # i.e. probably the database changing a rule, not evidence changing.
    policy_suspect: int = 0

    @property
    def movement_rate(self) -> float:
        if not self.vus_at_baseline:
            return 0.0
        return self.vus_moved / self.vus_at_baseline

    @property
    def movement_rate_adjusted(self) -> float:
        """Movement with policy-suspect transitions removed."""
        if not self.vus_at_baseline:
            return 0.0
        return (self.vus_moved - self.policy_suspect) / self.vus_at_baseline

    @property
    def actionable_rate(self) -> float:
        """The number that matters commercially: moved to something a lab acts on."""
        if not self.vus_at_baseline:
            return 0.0
        return (self.moved_to_pathogenic + self.moved_to_benign) / self.vus_at_baseline


PANEL_MOVEMENT_SQL = """
WITH baseline AS (
    SELECT allele_id, gene, variation_id, bucket, stars
    FROM assertion_span
    WHERE valid_from <= CAST($baseline AS DATE)
      AND valid_to   >  CAST($baseline AS DATE)
),
current AS (
    SELECT allele_id, bucket, stars, clnsig_raw
    FROM assertion_span
    WHERE valid_from <= CAST($current AS DATE)
      AND valid_to   >  CAST($current AS DATE)
)
SELECT b.allele_id, b.gene, b.variation_id,
       b.bucket AS from_bucket, b.stars AS from_stars,
       c.bucket AS to_bucket,   c.stars AS to_stars,
       c.clnsig_raw AS to_clnsig
FROM baseline b
JOIN current c USING (allele_id)
WHERE b.bucket IS DISTINCT FROM c.bucket
"""


def panel_movement(connection, baseline: str, current: str,
                   suspect: set[tuple[str, str]] | None = None) -> MovementSummary:
    """Population-level movement between two dates. No customer data required.

    `suspect` is the set of (from_bucket, to_bucket) pairs implicated by
    detected policy events; matching movements are counted separately rather
    than discarded, so the adjustment stays visible and reversible.
    """
    # DuckDB rejects named parameters a statement does not reference, so each
    # query gets exactly the bindings it uses.
    baseline_only = {"baseline": baseline}
    both = {"baseline": baseline, "current": current}

    total = connection.execute(
        """
        SELECT COUNT(*) FROM assertion_span
        WHERE valid_from <= CAST($baseline AS DATE)
          AND valid_to   >  CAST($baseline AS DATE)
          AND bucket <> 'ABSENT'
        """,
        baseline_only,
    ).fetchone()[0]

    vus_baseline = connection.execute(
        """
        SELECT COUNT(*) FROM assertion_span
        WHERE valid_from <= CAST($baseline AS DATE)
          AND valid_to   >  CAST($baseline AS DATE)
          AND bucket = 'UNCERTAIN'
        """,
        baseline_only,
    ).fetchone()[0]

    rows = connection.execute(
        PANEL_MOVEMENT_SQL + " AND b.bucket = 'UNCERTAIN'", both
    ).fetchall()

    to_path = sum(1 for r in rows if r[5] in ("PATHOGENIC", "LIKELY_PATHOGENIC"))
    to_benign = sum(1 for r in rows if r[5] in ("BENIGN", "LIKELY_BENIGN"))
    to_conflicting = sum(1 for r in rows if r[5] == "CONFLICTING")
    retracted = sum(1 for r in rows if r[5] == "ABSENT")

    suspect = suspect or set()
    policy_suspect = sum(1 for r in rows if (r[3], r[5]) in suspect)

    return MovementSummary(
        baseline_date=baseline,
        current_date=current,
        total_tracked=total,
        vus_at_baseline=vus_baseline,
        vus_moved=len(rows),
        moved_to_pathogenic=to_path,
        moved_to_benign=to_benign,
        moved_to_conflicting=to_conflicting,
        retracted=retracted,
        policy_suspect=policy_suspect,
    )


def movement_detail(connection, baseline: str, current: str,
                    from_bucket: str = "UNCERTAIN", limit: int = 50):
    """The individual variants behind a movement summary, for the diff panel."""
    return connection.execute(
        PANEL_MOVEMENT_SQL
        + " AND b.bucket = $from_bucket ORDER BY c.stars DESC, b.gene LIMIT $limit",
        {
            "baseline": baseline,
            "current": current,
            "from_bucket": from_bucket,
            "limit": limit,
        },
    ).fetchall()


CASE_MOVEMENT_SQL = """
SELECT cv.case_ref,
       cv.gene,
       cv.allele_id,
       cv.reported_classification,
       cv.reported_on,
       at_signout.bucket AS evidence_at_signout,
       now_.bucket       AS evidence_now,
       now_.stars        AS stars_now,
       now_.clnsig_raw   AS clnsig_now,
       now_.valid_from   AS changed_on
FROM case_variant cv
JOIN assertion_span at_signout
       ON at_signout.allele_id = cv.allele_id
      AND at_signout.valid_from <= cv.reported_on
      AND at_signout.valid_to   >  cv.reported_on
JOIN assertion_span now_
       ON now_.allele_id = cv.allele_id
      AND now_.is_current
WHERE at_signout.bucket IS DISTINCT FROM now_.bucket
ORDER BY now_.stars DESC, cv.reported_on
"""


def case_movement(connection):
    """Movement for one tenant's reported variants. Requires a loaded case plane."""
    return connection.execute(CASE_MOVEMENT_SQL).fetchall()
