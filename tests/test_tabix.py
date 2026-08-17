"""Tabix index parsing and region-to-byte-range planning.

Indexes are built synthetically here so the tests stay offline and fast. The
live end-to-end check against NCBI lives in test_integration.py.
"""

import struct

import pytest

from mendelea.evidence import tabix
from mendelea.evidence.bgzf import BGZF_MAX_BLOCK


def build_index(
    names=(b"17",),
    bins_per_ref=({1: [((1000 << 16) | 0, (2000 << 16) | 5)]},),
    linear_per_ref=(((1000 << 16) | 0,),),
) -> bytes:
    """Assemble a minimal but spec-shaped .tbi payload (already inflated)."""
    name_blob = b"".join(n + b"\x00" for n in names)
    out = bytearray(tabix.TBI_MAGIC)
    out += struct.pack(
        "<8i", len(names), 2, 1, 2, 0, ord("#"), 0, len(name_blob)
    )
    out += name_blob

    for bin_map, linear in zip(bins_per_ref, linear_per_ref):
        out += struct.pack("<i", len(bin_map))
        for bin_id, chunks in bin_map.items():
            out += struct.pack("<Ii", bin_id, len(chunks))
            for start, stop in chunks:
                out += struct.pack("<QQ", start, stop)
        out += struct.pack("<i", len(linear))
        for offset in linear:
            out += struct.pack("<Q", offset)

    return bytes(out)


# --------------------------------------------------------------------------
# Binning scheme
# --------------------------------------------------------------------------


def test_reg2bins_includes_every_level_for_a_single_base():
    assert tabix.reg2bins(0, 1) == [0, 1, 9, 73, 585, 4681]


def test_reg2bins_always_includes_bin_zero():
    """Bin 0 spans the whole reference; records can always hide there."""
    assert 0 in tabix.reg2bins(1_000_000, 1_000_100)


def test_reg2bins_widens_with_the_region():
    narrow = tabix.reg2bins(0, 16_384)
    wide = tabix.reg2bins(0, 1_000_000)
    assert len(wide) > len(narrow)


def test_reg2bins_degenerate_region():
    assert tabix.reg2bins(100, 100) == [0]


# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------


def test_parse_rejects_bad_magic():
    with pytest.raises(ValueError, match="not a tabix index"):
        tabix.parse(b"NOPE" + b"\x00" * 64)


def test_parse_roundtrip():
    index = tabix.parse(build_index())
    assert index.names == ("17",)
    assert index.format == 2
    assert index.col_seq == 1
    assert index.bins[0][1] == [((1000 << 16) | 0, (2000 << 16) | 5)]


def test_parse_multiple_references():
    raw = build_index(
        names=(b"1", b"2"),
        bins_per_ref=({1: [(0, 100)]}, {9: [(200, 300)]}),
        linear_per_ref=((0,), (200,)),
    )
    index = tabix.parse(raw)
    assert index.names == ("1", "2")
    assert index.bins[1][9] == [(200, 300)]


# --------------------------------------------------------------------------
# Contig naming
# --------------------------------------------------------------------------


def test_contig_lookup_tolerates_chr_prefix_mismatch():
    """ClinVar names contigs bare; plenty of callers say chr17."""
    index = tabix.parse(build_index(names=(b"17",)))
    assert index.reference_index("17") == 0
    assert index.reference_index("chr17") == 0


def test_contig_lookup_works_the_other_way():
    index = tabix.parse(build_index(names=(b"chr17",)))
    assert index.reference_index("17") == 0
    assert index.reference_index("chr17") == 0


def test_unknown_contig_yields_no_chunks():
    index = tabix.parse(build_index())
    assert index.query_chunks("22", 0, 1000) == []
    assert index.byte_ranges("22", 0, 1000) == []


# --------------------------------------------------------------------------
# Query planning
# --------------------------------------------------------------------------


def test_query_returns_the_matching_chunk():
    index = tabix.parse(build_index())
    assert index.query_chunks("17", 0, 1000) == [((1000 << 16) | 0, (2000 << 16) | 5)]


def test_byte_ranges_extend_past_the_last_block():
    """The chunk end points inside a block; we must read that block whole."""
    index = tabix.parse(build_index())
    ranges = index.byte_ranges("17", 0, 1000)
    assert ranges == [(1000, 2000 + BGZF_MAX_BLOCK - 1)]


def test_linear_index_prunes_chunks_that_end_too_early():
    """A chunk entirely before the linear floor cannot hold our records."""
    floor = (5000 << 16) | 0
    raw = build_index(
        bins_per_ref=({0: [(0, (100 << 16) | 0)], 1: [(floor, (6000 << 16) | 0)]},),
        linear_per_ref=((floor,),),
    )
    index = tabix.parse(raw)
    chunks = index.query_chunks("17", 0, 1000)
    # The bin-0 chunk ends at block 100, well below the floor, so it is dropped.
    assert all(stop > floor for _, stop in chunks)


def test_adjacent_ranges_are_merged_into_one_request():
    """Fewer, larger requests beat many small ones over HTTP."""
    raw = build_index(
        bins_per_ref=(
            {
                1: [((1000 << 16) | 0, (1010 << 16) | 0)],
                9: [((1005 << 16) | 0, (1020 << 16) | 0)],
            },
        ),
        linear_per_ref=((0,),),
    )
    index = tabix.parse(raw)
    assert len(index.byte_ranges("17", 0, 1000)) == 1
