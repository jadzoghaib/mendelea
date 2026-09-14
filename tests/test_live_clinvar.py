"""The live path: a real ingest, checked against ClinVar's own API.

Everything else in this suite runs offline against synthetic snapshots, which
left the part that produces all the data uncovered: `ingest_release` was
called by `cli.py` and by nothing else. A `network` marker was declared in
pyproject and carried by no test, so `-m "not network"` deselected nothing
and implied a coverage that did not exist.

These are the tests that marker was for. They are slow and they depend on
NCBI being up, so they are excluded by default:

    pytest -m "not network"     # the offline suite
    pytest -m network           # this file, against the live endpoints

The second test is the Phase 1 gate in the README's own terms -- "point-in-time
query correct on N hand-checked variants" -- done by machine against ClinVar's
API rather than by hand, so it can be re-run whenever the pipeline changes.
"""

import json
import urllib.request
from datetime import date

import duckdb
import pytest

from mendelea.evidence import clinvar, snapshot
from mendelea.evidence.genes import GeneRegion

pytestmark = pytest.mark.network

# Straight from the bundled coordinate cache, so resolving it needs no second
# service. One gene keeps the fetch small; the pipeline is identical at any width.
TP53 = GeneRegion("TP53", "17", 7661779, 7687546, source="cache")

ESUMMARY = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"

# Only variants ClinVar itself rates "reviewed by expert panel". They are the
# ones that do not drift between the last archived weekly release and whatever
# the API says today, which is what makes an exact-match assertion fair rather
# than flaky.
EXPERT_PANEL_STARS = 3


@pytest.fixture(scope="module")
def ingested(tmp_path_factory):
    """One real release of one gene, fetched by range request and written to Parquet."""
    root = tmp_path_factory.mktemp("live")
    releases = clinvar.select_releases(date.today().year, date.today().year, per_year=1)
    if not releases:
        pytest.skip("no archived releases published for this year yet")

    result = snapshot.ingest_release(
        release=releases[0],
        regions=[TP53],
        panel_slug="livecheck",
        snapshot_root=root / "snapshots",
        cache_dir=root / "cache",
    )
    path = snapshot.snapshot_path(
        root / "snapshots", releases[0].release_date, "livecheck"
    )
    return result, path


def test_ingest_writes_a_snapshot_that_reads_back(ingested):
    """The whole chain: tabix -> byte ranges -> BGZF -> VCF -> DuckDB -> Parquet."""
    result, path = ingested
    manifest = result.manifest

    assert result.written is True
    assert path.exists()
    assert manifest.row_count > 1000, "TP53 holds thousands of records; this is not a stub"

    connection = duckdb.connect()
    try:
        described = connection.execute(
            "DESCRIBE SELECT * FROM read_parquet($p)", {"p": path.as_posix()}
        ).fetchall()
        rows, genes, unnamed = connection.execute(
            "SELECT COUNT(*), COUNT(DISTINCT gene), COUNT(*) FILTER (WHERE allele_id IS NULL) "
            "FROM read_parquet($p)",
            {"p": path.as_posix()},
        ).fetchone()
    finally:
        connection.close()

    assert [(n, t) for n, t, *_ in described] == list(snapshot.COLUMNS)
    assert rows == manifest.row_count
    assert unnamed == 0
    assert genes >= 1


def test_the_manifest_describes_the_file_it_wrote(ingested):
    """Content addressing is the basis of every provenance claim downstream."""
    result, path = ingested
    assert snapshot._sha256(path) == result.manifest.parquet_sha256
    assert result.manifest.source_url.endswith(".vcf.gz")


def test_the_range_request_moved_a_fraction_of_the_file(ingested):
    """The ingest design rests on this; if it ever silently downloads the lot, say so."""
    manifest = ingested[0].manifest
    assert manifest.source_full_bytes > 50_000_000, "ClinVar releases are ~90 MB"
    assert manifest.bytes_fetched < manifest.source_full_bytes * 0.20


def test_the_resolved_region_really_is_tp53(ingested):
    """Wrong-assembly coordinates return a plausible but wrong stretch of genome.

    `verify_regions` cross-checks against the gene symbols ClinVar reports in
    the region it actually fetched, and an empty warning list is that check
    passing on live data rather than on a fixture.
    """
    assert ingested[0].warnings == []


def _clinvar_says(variation_ids):
    """Current germline classification for each variation id, from ClinVar's API."""
    url = f"{ESUMMARY}?db=clinvar&retmode=json&id={','.join(variation_ids)}"
    with urllib.request.urlopen(url, timeout=60) as response:
        payload = json.loads(response.read())
    result = payload.get("result", {})
    out = {}
    for uid in result.get("uids", []):
        germline = result[uid].get("germline_classification") or {}
        if germline.get("description"):
            out[uid] = germline["description"]
    return out


def test_expert_panel_classifications_match_clinvars_own_api(ingested):
    """The Phase 1 gate: what we say the evidence says is what ClinVar says.

    Every bucket in the product is derived from a CLNSIG string we parsed out
    of a VCF we assembled from byte ranges. If any step of that is wrong the
    whole timeline is wrong in a way no offline test can see, because the
    fixtures were written by us too.
    """
    _, path = ingested
    connection = duckdb.connect()
    try:
        sample = connection.execute(
            "SELECT variation_id, bucket, clnsig_raw FROM read_parquet($p) "
            "WHERE stars = $stars AND bucket <> 'OTHER' "
            "ORDER BY variation_id LIMIT 20",
            {"p": path.as_posix(), "stars": EXPERT_PANEL_STARS},
        ).fetchall()
    finally:
        connection.close()

    if len(sample) < 5:
        pytest.skip(f"only {len(sample)} expert-panel variants in this region")

    live = _clinvar_says([row[0] for row in sample])
    assert live, "ClinVar returned no classifications; the check proved nothing"

    disagreements = []
    for variation_id, ours, raw in sample:
        if variation_id not in live:
            continue
        theirs = clinvar.bucket(live[variation_id].replace(" ", "_"))
        if theirs != ours:
            disagreements.append(
                f"variation {variation_id}: we say {ours} from {raw!r}, "
                f"ClinVar says {theirs} from {live[variation_id]!r}"
            )

    assert not disagreements, "point-in-time classification disagrees with ClinVar:\n  " + \
        "\n  ".join(disagreements)


def test_a_missing_year_is_empty_not_an_error():
    """An unarchived year is a real answer; a transport failure is not.

    Conflating them lets a network blip silently shrink the ingest scope while
    the run still reports success.
    """
    assert clinvar.list_releases(1998) == []
