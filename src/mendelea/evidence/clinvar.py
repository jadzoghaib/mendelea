"""ClinVar release discovery and record parsing.

ClinVar archives every weekly release indefinitely under

    .../vcf_GRCh38/archive_2.0/<year>/clinvar_<YYYYMMDD>.vcf.gz

each with a .tbi index. That archive is what makes the whole product
buildable retroactively: the past is already published, so an evidence
timeline can be reconstructed today without waiting to accumulate one.

Terminology drift
-----------------
ClinVar renames things between releases. The big one in our window is
`Conflicting_interpretations_of_pathogenicity` becoming
`Conflicting_classifications_of_pathogenicity`, and the review status
following suit. A naive differ reads that rename as every conflicted variant
changing at once -- tens of thousands of fake movements on one date. Buckets
and star ratings exist to absorb exactly this: we compare stable categories,
never raw source strings.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from datetime import date


class ReleaseListingError(RuntimeError):
    """Raised when we could not determine what releases exist for a year.

    Distinct from "that year has no archive", which is an empty list.
    """

ARCHIVE_ROOT = "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38/archive_2.0"
CURRENT_ROOT = "https://ftp.ncbi.nlm.nih.gov/pub/clinvar/vcf_GRCh38"

ASSEMBLY = "GRCh38"

_RELEASE_RE = re.compile(r"clinvar_(\d{8})\.vcf\.gz(?!\.)")


# --------------------------------------------------------------------------
# Classification buckets
# --------------------------------------------------------------------------

# Coarse, stable categories. Movement between these is what a laboratory
# actually acts on; movement inside one of them usually is not.
PATHOGENIC = "PATHOGENIC"
LIKELY_PATHOGENIC = "LIKELY_PATHOGENIC"
UNCERTAIN = "UNCERTAIN"
LIKELY_BENIGN = "LIKELY_BENIGN"
BENIGN = "BENIGN"
CONFLICTING = "CONFLICTING"
OTHER = "OTHER"
ABSENT = "ABSENT"  # not present in this release at all

# Ordered for "did this move toward pathogenic or benign" questions.
BUCKET_ORDER = {
    BENIGN: -2,
    LIKELY_BENIGN: -1,
    UNCERTAIN: 0,
    LIKELY_PATHOGENIC: 1,
    PATHOGENIC: 2,
}

# Buckets a laboratory would act on if a reported VUS landed there.
ACTIONABLE = {PATHOGENIC, LIKELY_PATHOGENIC, BENIGN, LIKELY_BENIGN}

_SIG_MAP = {
    "pathogenic": PATHOGENIC,
    "pathogenic/likely_pathogenic": PATHOGENIC,
    "pathogenic/likely_pathogenic/pathogenic_low_penetrance": PATHOGENIC,
    "likely_pathogenic": LIKELY_PATHOGENIC,
    "likely_pathogenic_low_penetrance": LIKELY_PATHOGENIC,
    "uncertain_significance": UNCERTAIN,
    "uncertain_risk_allele": UNCERTAIN,
    "likely_benign": LIKELY_BENIGN,
    "benign": BENIGN,
    "benign/likely_benign": BENIGN,
    "conflicting_interpretations_of_pathogenicity": CONFLICTING,
    "conflicting_classifications_of_pathogenicity": CONFLICTING,
}

_STAR_MAP = {
    "practice_guideline": 4,
    "reviewed_by_expert_panel": 3,
    "criteria_provided,_multiple_submitters,_no_conflicts": 2,
    "criteria_provided,_single_submitter": 1,
    "criteria_provided,_conflicting_interpretations": 1,
    "criteria_provided,_conflicting_classifications": 1,
    "no_assertion_criteria_provided": 0,
    "no_assertion_provided": 0,
    "no_classification_provided": 0,
    "no_classifications_from_unflagged_records": 0,
    "no_interpretation_for_the_single_variant": 0,
}


def bucket(clnsig: str | None) -> str:
    """Map a raw CLNSIG string to a stable bucket.

    CLNSIG can carry several assertions joined by | or , -- take the most
    clinically significant one present, which is how a reviewer would read it.
    """
    if not clnsig:
        return OTHER

    parts = [p.strip().lower() for p in re.split(r"[|,]", clnsig) if p.strip()]
    mapped = [_SIG_MAP[p] for p in parts if p in _SIG_MAP]
    if not mapped:
        return OTHER
    if CONFLICTING in mapped and len(set(mapped)) == 1:
        return CONFLICTING

    ranked = [m for m in mapped if m in BUCKET_ORDER]
    if not ranked:
        return CONFLICTING if CONFLICTING in mapped else OTHER
    return max(ranked, key=lambda m: abs(BUCKET_ORDER[m]))


def stars(clnrevstat: str | None) -> int:
    """Map a raw CLNREVSTAT string to the 0-4 ClinVar star rating."""
    if not clnrevstat:
        return 0
    return _STAR_MAP.get(clnrevstat.strip().lower(), 0)


# --------------------------------------------------------------------------
# Releases
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Release:
    """One archived ClinVar weekly release.

    `dir_year` is the archive directory the release was discovered in, which
    is *not* always the year of its date: NCBI files the last releases of
    December under the following year, so clinvar_20181225.vcf.gz lives in
    archive_2.0/2019/. Deriving the directory from the date instead produces
    a 404 on exactly those boundary releases.
    """

    release_date: date
    dir_year: int | None = None

    @property
    def stamp(self) -> str:
        return self.release_date.strftime("%Y%m%d")

    @property
    def archive_year(self) -> int:
        return self.dir_year if self.dir_year is not None else self.release_date.year

    @property
    def vcf_url(self) -> str:
        return f"{ARCHIVE_ROOT}/{self.archive_year}/clinvar_{self.stamp}.vcf.gz"

    @property
    def tbi_url(self) -> str:
        return self.vcf_url + ".tbi"

    def __str__(self) -> str:
        return self.release_date.isoformat()


def parse_release_listing(html: str, dir_year: int | None = None) -> list[Release]:
    """Pull release dates out of an FTP directory index page.

    `dir_year` is the directory the listing came from; it is carried on each
    Release because it, not the date, determines the download URL.
    """
    seen: set[str] = set()
    releases = []
    for match in _RELEASE_RE.finditer(html):
        stamp = match.group(1)
        if stamp in seen:
            continue
        seen.add(stamp)
        releases.append(
            Release(date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:8])), dir_year)
        )
    return sorted(releases, key=lambda r: r.release_date)


def list_releases(year: int, session=None) -> list[Release]:
    """Every archived release in one year's directory.

    Note this is the directory's contents, not "releases dated in that year" --
    the December boundary means those differ. Empty list if the year has no
    archive directory.
    """
    import requests

    session = session or requests.Session()

    # A 404 means the year genuinely has no archive; anything else means we
    # failed to find out. Those must not be conflated: swallowing a transient
    # error silently shrinks the ingest scope, and the run still reports
    # success having quietly done less than it was asked to.
    last_error: Exception | None = None
    for attempt in range(3):
        try:
            response = session.get(f"{ARCHIVE_ROOT}/{year}/", timeout=60)
        except Exception as exc:  # noqa: BLE001 - retry transient transport errors
            last_error = exc
            time.sleep(2**attempt)
            continue

        if response.status_code == 404:
            return []
        if response.ok:
            return parse_release_listing(response.text, dir_year=year)

        last_error = requests.HTTPError(f"status {response.status_code}")
        time.sleep(2**attempt)

    raise ReleaseListingError(
        f"could not list releases for {year}: {last_error}"
    ) from last_error


def select_releases(start_year: int, end_year: int, per_year: int = 1,
                    session=None) -> list[Release]:
    """Pick an evenly spread sample of releases across a year range.

    A full weekly series is ~370 snapshots over eight years. For measuring
    movement between two dates, a handful per year resolves the curve at a
    fraction of the ingest cost; the pipeline is identical either way, so
    density is a knob rather than a design decision.
    """
    chosen: list[Release] = []
    for year in range(start_year, end_year + 1):
        available = list_releases(year, session=session)
        if not available:
            continue
        if per_year >= len(available):
            chosen.extend(available)
            continue
        step = len(available) / per_year
        chosen.extend(available[int(i * step)] for i in range(per_year))
    return sorted(chosen, key=lambda r: r.release_date)


# --------------------------------------------------------------------------
# Records
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class ClinVarRecord:
    variation_id: str
    contig: str
    pos: int
    ref: str
    alt: str
    clnsig_raw: str | None
    clnrevstat_raw: str | None
    gene: str | None
    condition: str | None

    @property
    def bucket(self) -> str:
        return bucket(self.clnsig_raw)

    @property
    def stars(self) -> int:
        return stars(self.clnrevstat_raw)


def parse_info(info: str) -> dict[str, str]:
    """Parse a VCF INFO column into a flat dict; bare flags map to ''."""
    fields = {}
    for item in info.split(";"):
        if not item:
            continue
        key, _, value = item.partition("=")
        fields[key] = value
    return fields


def parse_vcf_line(line: bytes) -> ClinVarRecord | None:
    """Parse one ClinVar VCF data line, or return None if it is unusable.

    Skips headers, multi-allelic rows (ClinVar does not emit them, but a
    truncated range can produce odd fragments) and symbolic ALTs.
    """
    if not line or line.startswith(b"#"):
        return None

    cols = line.rstrip(b"\r\n").split(b"\t")
    if len(cols) < 8:
        return None

    try:
        contig = cols[0].decode("ascii")
        pos = int(cols[1])
        variation_id = cols[2].decode("ascii")
        ref = cols[3].decode("ascii")
        alt = cols[4].decode("ascii")
        info = parse_info(cols[7].decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return None

    if "," in alt or alt.startswith("<") or alt in {".", ""} or ref in {".", ""}:
        return None

    geneinfo = info.get("GENEINFO")
    gene = geneinfo.split(":")[0] if geneinfo else None

    condition = info.get("CLNDN")
    if condition:
        condition = condition.replace("_", " ").replace("|", "; ")[:300]

    return ClinVarRecord(
        variation_id=variation_id,
        contig=contig,
        pos=pos,
        ref=ref,
        alt=alt,
        clnsig_raw=info.get("CLNSIG"),
        clnrevstat_raw=info.get("CLNREVSTAT"),
        gene=gene,
        condition=condition,
    )
