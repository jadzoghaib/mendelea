"""ClinVar parsing, and the terminology drift that would otherwise fake movement."""

from datetime import date

from mendelea.evidence import clinvar


# --------------------------------------------------------------------------
# The drift guard. This is the test that protects the headline number.
# --------------------------------------------------------------------------


def test_conflicting_rename_is_absorbed():
    """ClinVar renamed this term mid-window. It is not a reclassification.

    `Conflicting_interpretations_of_pathogenicity` became
    `Conflicting_classifications_of_pathogenicity`. Comparing raw strings
    would read the rename as every conflicted variant in the database moving
    on a single date -- tens of thousands of phantom movements.
    """
    old = clinvar.bucket("Conflicting_interpretations_of_pathogenicity")
    new = clinvar.bucket("Conflicting_classifications_of_pathogenicity")
    assert old == new == clinvar.CONFLICTING


def test_conflicting_review_status_rename_is_absorbed():
    old = clinvar.stars("criteria_provided,_conflicting_interpretations")
    new = clinvar.stars("criteria_provided,_conflicting_classifications")
    assert old == new == 1


# --------------------------------------------------------------------------
# Buckets
# --------------------------------------------------------------------------


def test_combined_pathogenic_terms_collapse_to_pathogenic():
    assert clinvar.bucket("Pathogenic") == clinvar.PATHOGENIC
    assert clinvar.bucket("Pathogenic/Likely_pathogenic") == clinvar.PATHOGENIC
    assert clinvar.bucket("Likely_pathogenic") == clinvar.LIKELY_PATHOGENIC


def test_benign_terms():
    assert clinvar.bucket("Benign") == clinvar.BENIGN
    assert clinvar.bucket("Benign/Likely_benign") == clinvar.BENIGN
    assert clinvar.bucket("Likely_benign") == clinvar.LIKELY_BENIGN


def test_uncertain_significance():
    assert clinvar.bucket("Uncertain_significance") == clinvar.UNCERTAIN


def test_unmappable_terms_land_in_other_not_uncertain():
    """drug_response is not a VUS; mixing them would inflate the denominator."""
    assert clinvar.bucket("drug_response") == clinvar.OTHER
    assert clinvar.bucket("not_provided") == clinvar.OTHER
    assert clinvar.bucket(None) == clinvar.OTHER


def test_multi_valued_clnsig_takes_the_most_significant():
    assert clinvar.bucket("Pathogenic|risk_factor") == clinvar.PATHOGENIC
    assert clinvar.bucket("Likely_benign|drug_response") == clinvar.LIKELY_BENIGN


def test_star_ratings():
    assert clinvar.stars("practice_guideline") == 4
    assert clinvar.stars("reviewed_by_expert_panel") == 3
    assert clinvar.stars("criteria_provided,_multiple_submitters,_no_conflicts") == 2
    assert clinvar.stars("criteria_provided,_single_submitter") == 1
    assert clinvar.stars("no_assertion_criteria_provided") == 0
    assert clinvar.stars("something_ClinVar_invents_next_year") == 0


# --------------------------------------------------------------------------
# VCF records
# --------------------------------------------------------------------------

LINE = (
    b"17\t43045703\t55632\tC\tT\t.\t.\t"
    b"ALLELEID=68844;CLNDN=Breast-ovarian_cancer,_familial_1;"
    b"CLNREVSTAT=reviewed_by_expert_panel;CLNSIG=Pathogenic;"
    b"CLNVC=single_nucleotide_variant;GENEINFO=BRCA1:672"
)


def test_parse_vcf_line():
    record = clinvar.parse_vcf_line(LINE)
    assert record is not None
    assert record.variation_id == "55632"
    assert record.contig == "17"
    assert record.pos == 43045703
    assert record.ref == "C"
    assert record.alt == "T"
    assert record.gene == "BRCA1"
    assert record.bucket == clinvar.PATHOGENIC
    assert record.stars == 3


def test_parse_skips_headers_and_junk():
    assert clinvar.parse_vcf_line(b"##fileformat=VCFv4.1") is None
    assert clinvar.parse_vcf_line(b"#CHROM\tPOS\tID") is None
    assert clinvar.parse_vcf_line(b"") is None
    assert clinvar.parse_vcf_line(b"17\t100") is None


def test_parse_skips_symbolic_and_multiallelic():
    base = b"17\t100\t1\tC\t%s\t.\t.\tCLNSIG=Pathogenic"
    assert clinvar.parse_vcf_line(base % b"<DEL>") is None
    assert clinvar.parse_vcf_line(base % b"T,G") is None
    assert clinvar.parse_vcf_line(base % b".") is None


def test_parse_info_handles_flags():
    fields = clinvar.parse_info("A=1;FLAG;B=2")
    assert fields == {"A": "1", "FLAG": "", "B": "2"}


# --------------------------------------------------------------------------
# Releases
# --------------------------------------------------------------------------

LISTING = """
<a href="clinvar_20190102.vcf.gz">clinvar_20190102.vcf.gz</a>
<a href="clinvar_20190102.vcf.gz.tbi">clinvar_20190102.vcf.gz.tbi</a>
<a href="clinvar_20190108.vcf.gz">clinvar_20190108.vcf.gz</a>
<a href="clinvar_20190102_papu.vcf.gz">papu</a>
"""


def test_parse_release_listing_dedupes_and_ignores_index_files():
    releases = clinvar.parse_release_listing(LISTING)
    assert [r.release_date for r in releases] == [date(2019, 1, 2), date(2019, 1, 8)]


def test_release_urls():
    release = clinvar.Release(date(2019, 6, 3))
    assert release.vcf_url.endswith("/archive_2.0/2019/clinvar_20190603.vcf.gz")
    assert release.tbi_url == release.vcf_url + ".tbi"


def test_december_release_uses_its_directory_year_not_its_date_year():
    """NCBI files late-December releases under the following year.

    clinvar_20181225.vcf.gz lives in archive_2.0/2019/. Deriving the directory
    from the release date 404s on exactly these boundary releases -- which is
    how the first real ingest run failed on its first release.
    """
    release = clinvar.Release(date(2018, 12, 25), dir_year=2019)
    assert "/archive_2.0/2019/clinvar_20181225.vcf.gz" in release.vcf_url


def test_listing_stamps_releases_with_the_directory_year():
    releases = clinvar.parse_release_listing(LISTING, dir_year=2019)
    assert all(r.archive_year == 2019 for r in releases)


def test_archive_year_falls_back_to_the_date():
    assert clinvar.Release(date(2019, 6, 3)).archive_year == 2019
