"""The case plane: a laboratory's own reported variants.

The load path is where a customer's real export meets our assumptions, so the
tests here are mostly about failing loudly and counting honestly.
"""

import pytest

from mendelea.cases import model
from mendelea.db import connect
from mendelea.evidence.normalize import allele_id

HEADER = "case_ref,gene,contig,pos,ref,alt,reported_classification,reported_on\n"


def write_csv(tmp_path, body: str, header: str = HEADER):
    path = tmp_path / "cases.csv"
    path.write_text(header + body, encoding="utf-8")
    return path


def test_loads_a_clean_export(tmp_path):
    path = write_csv(
        tmp_path,
        "CASE-1,BRCA1,17,43045703,C,T,VUS,2019-06-03\n"
        "CASE-2,TP53,17,7676154,G,A,VUS,2020-01-15\n",
    )
    variants, rejected = model.load_csv(path, tenant_id="lab-a")
    assert len(variants) == 2
    assert rejected == []
    assert variants[0].case_ref == "CASE-1"
    assert variants[0].reported_on.isoformat() == "2019-06-03"


def test_allele_id_matches_the_evidence_plane(tmp_path):
    """The join key must be computed identically on both sides or nothing matches."""
    path = write_csv(tmp_path, "CASE-1,BRCA1,17,43045703,C,T,VUS,2019-06-03\n")
    variants, _ = model.load_csv(path, tenant_id="lab-a")
    assert variants[0].allele_id == allele_id("GRCh38", "17", 43045703, "C", "T")


def test_chr_prefixed_export_still_matches(tmp_path):
    """Laboratories export contigs both ways; identity must not depend on it."""
    path = write_csv(tmp_path, "CASE-1,BRCA1,chr17,43045703,C,T,VUS,2019-06-03\n")
    variants, _ = model.load_csv(path, tenant_id="lab-a")
    assert variants[0].allele_id == allele_id("GRCh38", "17", 43045703, "C", "T")


def test_missing_columns_raise_rather_than_guess(tmp_path):
    path = write_csv(
        tmp_path, "CASE-1,17,100,C,T\n", header="case_ref,contig,pos,ref,alt\n"
    )
    with pytest.raises(model.CaseLoadError, match="missing required columns"):
        model.load_csv(path, tenant_id="lab-a")


def test_bad_rows_are_reported_not_silently_dropped(tmp_path):
    """Silently dropping rows corrupts the denominator of every later statistic."""
    path = write_csv(
        tmp_path,
        "CASE-1,BRCA1,17,43045703,C,T,VUS,2019-06-03\n"
        "CASE-2,BRCA1,17,notanumber,C,T,VUS,2019-06-03\n"
        "CASE-3,BRCA1,17,43045704,C,T,VUS,not-a-date\n"
        "CASE-4,BRCA1,17,43045705,C,<DEL>,VUS,2019-06-03\n",
    )
    variants, rejected = model.load_csv(path, tenant_id="lab-a")
    assert len(variants) == 1
    assert len(rejected) == 3
    assert all("line" in message for message in rejected)


def test_persist_round_trips(tmp_path):
    path = write_csv(
        tmp_path,
        "CASE-1,BRCA1,17,43045703,C,T,VUS,2019-06-03\n"
        "CASE-2,TP53,17,7676154,G,A,Likely pathogenic,2020-01-15\n",
    )
    variants, _ = model.load_csv(path, tenant_id="lab-a")

    with connect(tmp_path / "cases.duckdb") as connection:
        assert model.persist(connection, variants) == 2
        rows = connection.execute(
            "SELECT tenant_id, case_ref, gene FROM case_variant ORDER BY case_ref"
        ).fetchall()

    assert rows == [("lab-a", "CASE-1", "BRCA1"), ("lab-a", "CASE-2", "TP53")]


def test_utf8_bom_export_is_tolerated(tmp_path):
    """Excel on Windows writes a BOM; a lab export routinely arrives that way."""
    path = tmp_path / "bom.csv"
    path.write_text(
        HEADER + "CASE-1,BRCA1,17,43045703,C,T,VUS,2019-06-03\n", encoding="utf-8-sig"
    )
    variants, rejected = model.load_csv(path, tenant_id="lab-a")
    assert len(variants) == 1 and rejected == []
