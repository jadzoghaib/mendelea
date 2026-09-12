"""The reanalysis report: what has moved under a laboratory's own variants.

The movement query is the easy half. The half that decides whether the report
is trustworthy is the accounting of what we could *not* answer, and there are
two distinct ways to fail silently:

  1. The variant is not in our evidence plane at all -- wrong gene panel, a
     variant ClinVar has never held, a coordinate that did not normalise.
  2. The variant is there, but the laboratory signed it out before our
     earliest snapshot, so we cannot say what the evidence said at the time.

Both look identical to "nothing has changed" if you only count movements. A
report that quietly drops them tells a customer their back catalogue is clean
when in truth it was never examined. So every input row is accounted for in
one of the buckets below, and the numbers are reported before any finding is.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..evidence.clinvar import ACTIONABLE as ACTIONABLE_BUCKETS  # noqa: E402


@dataclass
class Reconciliation:
    """Every input row lands in exactly one of these."""

    loaded: int = 0
    unmatched: int = 0        # not present in the evidence plane
    before_coverage: int = 0  # signed out before our earliest snapshot
    examined: int = 0         # actually comparable

    @property
    def match_rate(self) -> float:
        return self.examined / self.loaded if self.loaded else 0.0

    def is_sound(self, floor: float = 0.90) -> bool:
        """Whether the movement numbers are worth quoting at all."""
        return self.match_rate >= floor


@dataclass
class CaseReport:
    tenant_id: str
    evidence_panel: str
    evidence_from: str
    evidence_to: str
    reconciliation: Reconciliation
    unchanged: int = 0
    moved_to_pathogenic: int = 0
    moved_to_benign: int = 0
    moved_to_conflicting: int = 0
    retracted: int = 0
    policy_suspect: int = 0
    findings: list[dict] = field(default_factory=list)

    @property
    def moved(self) -> int:
        return (self.moved_to_pathogenic + self.moved_to_benign
                + self.moved_to_conflicting + self.retracted)

    @property
    def actionable(self) -> int:
        return self.moved_to_pathogenic + self.moved_to_benign

    @property
    def actionable_rate(self) -> float:
        examined = self.reconciliation.examined
        return self.actionable / examined if examined else 0.0


RECONCILE_SQL = """
SELECT cv.case_ref, cv.gene, cv.allele_id, cv.contig, cv.pos, cv.ref, cv.alt,
       cv.reported_classification, cv.reported_on,
       then_.bucket AS at_signout,
       now_.bucket  AS now_bucket,
       now_.stars   AS now_stars,
       now_.clnsig_raw AS now_clnsig,
       now_.valid_from AS changed_on,
       EXISTS (SELECT 1 FROM assertion_span e WHERE e.allele_id = cv.allele_id) AS known
FROM case_variant cv
LEFT JOIN assertion_span then_
       ON then_.allele_id = cv.allele_id
      AND then_.valid_from <= cv.reported_on
      AND then_.valid_to   >  cv.reported_on
LEFT JOIN assertion_span now_
       ON now_.allele_id = cv.allele_id
      AND now_.is_current
WHERE cv.tenant_id = $tenant
"""


def build(connection, tenant_id: str,
          suspect: set[tuple[str, str, str]] | None = None) -> CaseReport:
    """Reconcile one tenant's reported variants against the evidence timeline.

    `suspect` is `policy.suspect_keys(...)`; a finding is policy-suspect when
    its transition and the date its current state began match a detected
    event, so a relabelling is never presented as the evidence moving.
    """
    # Scope coverage to the panel the timeline actually holds. Reading it from
    # snapshot_manifest unfiltered reports whichever panel was ingested last,
    # and a coverage window that never applied to this comparison.
    from ..evidence.spans import coverage as panel_coverage
    from ..evidence.spans import loaded_panel

    panel = loaded_panel(connection)
    earliest, latest = panel_coverage(connection, panel) if panel else (None, None)

    rows = connection.execute(RECONCILE_SQL, {"tenant": tenant_id}).fetchall()

    counts = Reconciliation(loaded=len(rows))
    report = CaseReport(
        tenant_id=tenant_id,
        evidence_panel=panel or "unknown",
        evidence_from=str(earliest),
        evidence_to=str(latest),
        reconciliation=counts,
    )
    suspect = suspect or set()

    for row in rows:
        (case_ref, gene, allele_id, contig, pos, ref, alt, reported_classification,
         reported_on, at_signout, now_bucket, now_stars, now_clnsig,
         changed_on, known) = row

        if not known:
            counts.unmatched += 1
            continue
        if at_signout is None:
            # Present in the evidence plane, but signed out before we can see.
            counts.before_coverage += 1
            continue

        counts.examined += 1

        if at_signout == now_bucket:
            report.unchanged += 1
            continue

        if now_bucket in ("PATHOGENIC", "LIKELY_PATHOGENIC"):
            report.moved_to_pathogenic += 1
        elif now_bucket in ("BENIGN", "LIKELY_BENIGN"):
            report.moved_to_benign += 1
        elif now_bucket == "CONFLICTING":
            report.moved_to_conflicting += 1
        elif now_bucket == "ABSENT":
            report.retracted += 1

        is_suspect = (at_signout, now_bucket, str(changed_on)) in suspect
        if is_suspect:
            report.policy_suspect += 1

        report.findings.append({
            "case_ref": case_ref,
            "gene": gene,
            "variant": f"{contig}:{pos}{ref}>{alt}",
            "allele_id": allele_id,
            "reported_as": reported_classification,
            "reported_on": str(reported_on),
            "evidence_at_signout": at_signout,
            "evidence_now": now_bucket,
            "review_status_now": now_stars,
            "clinvar_now": now_clnsig,
            "changed_on": str(changed_on) if changed_on else None,
            "actionable": now_bucket in ACTIONABLE_BUCKETS,
            "policy_suspect": is_suspect,
        })

    # Most consequential first: actionable, then by ClinVar review status.
    report.findings.sort(
        key=lambda f: (not f["actionable"], f["policy_suspect"], -(f["review_status_now"] or 0))
    )
    return report
