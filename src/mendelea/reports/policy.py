"""Detecting policy events: relabelings that are not evidence movement.

The problem this exists to solve, found the hard way on real data:

    2022-12-24 -> 2023-12-30 :  UNCERTAIN -> CONFLICTING   6,107 variants
                                = 14.5% of the entire corpus in one step

Adjacent release steps move 1-3%. A change of that magnitude is not thousands
of laboratories independently revising thousands of variants in the same
direction at the same moment -- it is the database changing how it aggregates.
ClinVar documents exactly such a change to its conflict-reporting rules,
effective June 2022:

    before May 2018   any conflict among the five ACMG/AMP terms
    May 2018-Jun 2022 conflicts between three levels: B/LB vs VUS vs P/LP
    June 2022 onward  any pathogenic/risk vs any uncertain; any pathogenic/risk
                      vs any benign; any uncertain vs any benign

    https://www.ncbi.nlm.nih.gov/clinvar/docs/clinsig/

Why this matters commercially, not just scientifically
------------------------------------------------------
The product tells a laboratory "these variants you reported have moved". If it
reports 6,107 variants as having moved when what actually happened is that
ClinVar changed a rule, the laboratory re-reviews thousands of cases for
nothing, discovers the cause, and never trusts the tool again. Detecting these
is not a refinement -- it is a precondition for the product being safe to sell.

Note this is a heuristic on cohort behaviour, not a reading of ClinVar's
release notes. It flags transitions for a human to adjudicate; it does not
silently discard them.
"""

from __future__ import annotations

from dataclasses import dataclass

# A single release step moving more than this share of the classified corpus
# between two specific buckets is treated as suspect. Chosen from observation:
# real steps in this corpus run 1-3%, the known policy event ran 14.5%.
DEFAULT_THRESHOLD = 0.05


@dataclass(frozen=True)
class PolicyEvent:
    from_date: str
    to_date: str
    from_bucket: str
    to_bucket: str
    count: int
    share: float

    @property
    def pair(self) -> tuple[str, str]:
        return (self.from_bucket, self.to_bucket)

    def describe(self) -> str:
        return (
            f"{self.from_date} -> {self.to_date}: "
            f"{self.from_bucket} -> {self.to_bucket} "
            f"{self.count:,} variants ({self.share:.1%} of corpus)"
        )


DETECT_SQL = """
WITH stepped AS (
    SELECT allele_id, release_date, bucket,
           LAG(bucket)       OVER w AS prev_bucket,
           LAG(release_date) OVER w AS prev_date
    FROM assertion_dense
    WINDOW w AS (PARTITION BY allele_id ORDER BY release_date)
),
moves AS (
    SELECT prev_date, release_date, prev_bucket, bucket, COUNT(*) AS n
    FROM stepped
    WHERE prev_bucket IS NOT NULL
      AND prev_bucket IS DISTINCT FROM bucket
      -- Variants appearing or being retracted are corpus growth, not
      -- relabeling, and would otherwise dominate every release step.
      AND prev_bucket <> 'ABSENT'
      AND bucket <> 'ABSENT'
    GROUP BY 1, 2, 3, 4
),
corpus AS (
    SELECT release_date, COUNT(*) AS n
    FROM assertion_dense
    WHERE bucket <> 'ABSENT'
    GROUP BY 1
)
SELECT m.prev_date, m.release_date, m.prev_bucket, m.bucket, m.n,
       m.n::DOUBLE / c.n AS share
FROM moves m
JOIN corpus c ON c.release_date = m.prev_date
WHERE m.n::DOUBLE / c.n >= $threshold
ORDER BY share DESC
"""


def detect(connection, threshold: float = DEFAULT_THRESHOLD) -> list[PolicyEvent]:
    """Find release steps where one transition swept an implausible share of the corpus.

    Requires `assertion_dense`, which spans.build leaves in place.
    """
    rows = connection.execute(DETECT_SQL, {"threshold": threshold}).fetchall()
    return [
        PolicyEvent(
            from_date=str(row[0]),
            to_date=str(row[1]),
            from_bucket=row[2],
            to_bucket=row[3],
            count=row[4],
            share=row[5],
        )
        for row in rows
    ]


def suspect_keys(events: list[PolicyEvent]) -> set[tuple[str, str, str]]:
    """(from_bucket, to_bucket, to_date) for every detected event.

    A movement is policy-suspect when it matches an event's transition *and*
    the variant's current state began at that event's release step. Matching
    the pair alone -- which an earlier version did -- flags every
    UNCERTAIN -> CONFLICTING transition ever recorded, including the ordinary
    handful arising at every release from submitters who genuinely disagree.
    Those are evidence; only the sweep is a relabelling. On the 31-gene panel
    the pair-only rule subtracted 10,027 movements where the sweep accounts
    for 3,850, reporting 5.3% adjusted movement against a true 26.7%.

    This is the one definition of "policy-suspect": the case report, the
    Phase 0 gate and the time machine all read it from here.

    Residual imprecision, measured rather than assumed. The exact question is
    "did this allele participate in the sweep", which needs `assertion_dense`
    and a join per allele. Keying on the date the current span opened answers
    a near-enough one: on the 31-gene panel it flags 3,868 against the exact
    3,850 -- 18 alleles, 0.5%, that reached the target bucket at the event's
    release step from some third bucket. Both give 26.7% adjusted movement.
    Not worth a second scan of the dense table.
    """
    return {(event.from_bucket, event.to_bucket, event.to_date) for event in events}


def step_series(connection) -> list[tuple[str, str, str, str, int, float]]:
    """Every release-step transition with its corpus share, largest first.

    The diagnostic view behind `detect`: useful for choosing a threshold and
    for showing a reviewer why something was flagged.
    """
    return connection.execute(
        DETECT_SQL, {"threshold": 0.0}
    ).fetchall()
