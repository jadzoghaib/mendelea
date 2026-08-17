"""Allele identity. If these break, every movement statistic downstream is wrong."""

import pytest

from mendelea.evidence.normalize import (
    NAMESPACE,
    allele_id,
    normalise_contig,
    sha512t24u,
    trim,
)


def test_sha512t24u_is_unpadded_base64url_of_24_bytes():
    digest = sha512t24u(b"")
    assert "=" not in digest
    assert len(digest) == 32  # 24 bytes -> 32 base64 chars
    # Stable across runs and platforms, which is the whole point.
    assert digest == sha512t24u(b"")


def test_normalise_contig_strips_prefix_and_canonicalises_mito():
    assert normalise_contig("chr17") == "17"
    assert normalise_contig("17") == "17"
    assert normalise_contig("chrX") == "X"
    assert normalise_contig("chrM") == "MT"
    assert normalise_contig("MT") == "MT"


def test_trim_removes_shared_suffix():
    # A two-base deletion carrying a redundant shared trailing G.
    assert trim(100, "CTAG", "CG") == (100, "CTA", "C")


def test_trim_removes_shared_prefix_and_advances_position():
    # Shared leading base is dropped and the coordinate moves with it.
    assert trim(100, "AGT", "ACT") == (101, "G", "C")


def test_trim_leaves_a_clean_snv_alone():
    assert trim(100, "C", "T") == (100, "C", "T")


def test_trim_padded_indel_representations_agree():
    # An insertion written with and without extra shared context.
    left = trim(100, "AT", "ATT")
    right = trim(99, "GAT", "GATT")
    assert left[1:] == right[1:]
    assert left[0] == right[0]


def test_trim_never_empties_an_allele():
    pos, ref, alt = trim(100, "AAA", "A")
    assert ref and alt  # VCF requires at least one base each side


def test_chr_prefix_does_not_change_identity():
    bare = allele_id("GRCh38", "17", 43045703, "C", "T")
    prefixed = allele_id("GRCh38", "chr17", 43045703, "C", "T")
    assert bare == prefixed


def test_padding_does_not_change_identity():
    """The same insertion spelled two ways must hash identically."""
    a = allele_id("GRCh38", "13", 32340300, "AT", "ATT")
    b = allele_id("GRCh38", "13", 32340299, "GAT", "GATT")
    assert a == b


def test_different_alleles_differ():
    a = allele_id("GRCh38", "17", 43045703, "C", "T")
    b = allele_id("GRCh38", "17", 43045703, "C", "G")
    assert a != b


def test_assembly_is_part_of_identity():
    """Same coordinates on a different build are a different allele."""
    assert allele_id("GRCh38", "17", 100, "C", "T") != allele_id("GRCh37", "17", 100, "C", "T")


def test_identifier_is_namespaced_not_ga4gh():
    """We must not claim a ga4gh CURIE we cannot reproduce."""
    identifier = allele_id("GRCh38", "17", 43045703, "C", "T")
    assert identifier.startswith(NAMESPACE)
    assert not identifier.startswith("ga4gh:")


@pytest.mark.parametrize("ref,alt", [("<DEL>", "A"), ("A", "<DUP>"), ("", "A"), ("A", "")])
def test_unrepresentable_alleles_raise(ref, alt):
    """Better to drop a record than hash nonsense into the timeline."""
    with pytest.raises(ValueError):
        allele_id("GRCh38", "17", 100, ref, alt)
