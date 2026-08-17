"""BGZF reading, including the truncated tail that every range fetch produces."""

import gzip

from mendelea.evidence.bgzf import decompress, iter_complete_lines, split_virtual_offset


def test_split_virtual_offset():
    # Upper 48 bits address the block, lower 16 the byte inside it.
    assert split_virtual_offset(0) == (0, 0)
    assert split_virtual_offset((12345 << 16) | 678) == (12345, 678)


def test_decompress_single_member():
    assert decompress(gzip.compress(b"hello")) == b"hello"


def test_decompress_concatenated_members():
    """A BGZF file is many independent members; a range spans several."""
    blob = gzip.compress(b"first\n") + gzip.compress(b"second\n") + gzip.compress(b"third\n")
    assert decompress(blob) == b"first\nsecond\nthird\n"


def test_decompress_tolerates_truncated_tail():
    """Ranges routinely end mid-member. Keep what inflated, drop the rest."""
    blob = gzip.compress(b"complete\n") + gzip.compress(b"cut off here")
    truncated = blob[: len(gzip.compress(b"complete\n")) + 12]
    result = decompress(truncated)
    assert result.startswith(b"complete\n")


def test_decompress_ignores_trailing_garbage():
    blob = gzip.compress(b"payload\n") + b"\x00\x01\x02not gzip at all"
    assert decompress(blob) == b"payload\n"


def test_decompress_empty():
    assert decompress(b"") == b""


def test_iter_complete_lines_drops_partial_final_line():
    assert iter_complete_lines(b"a\nb\nc") == [b"a", b"b"]


def test_iter_complete_lines_keeps_all_when_newline_terminated():
    assert iter_complete_lines(b"a\nb\nc\n") == [b"a", b"b", b"c"]


def test_iter_complete_lines_empty():
    assert iter_complete_lines(b"") == []


def test_a_half_parsed_record_never_escapes():
    """The failure mode this guards: a truncated VCF line parsed as real data."""
    payload = b"17\t100\t1\tC\tT\t.\t.\tCLNSIG=Pathogenic\n17\t200\t2\tC\tT\t.\t.\tCLNSI"
    lines = iter_complete_lines(payload)
    assert len(lines) == 1
    assert lines[0].endswith(b"CLNSIG=Pathogenic")
