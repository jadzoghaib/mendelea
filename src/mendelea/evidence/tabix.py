"""Minimal tabix (.tbi) index reader and region-to-byte-range planner.

Only the read path we need is implemented: parse an index, and turn a genomic
region into the set of compressed byte ranges that could contain overlapping
records. Everything else about htslib is out of scope.

Index layout (little-endian), per the tabix spec:

    magic   char[4]  "TBI\\1"
    n_ref   int32
    format  int32    0=generic 1=SAM 2=VCF
    col_seq int32    1-based column of the sequence name
    col_beg int32    1-based column of the start position
    col_end int32    1-based column of the end position
    meta    int32    comment-line leading character
    skip    int32    header lines to skip
    l_nm    int32    length of the NUL-delimited name blob
    names   char[l_nm]
    per reference:
        n_bin int32
        per bin: bin uint32, n_chunk int32, then n_chunk * (uint64, uint64)
        n_intv int32
        n_intv * uint64          -- the linear index
"""

from __future__ import annotations

import struct
from dataclasses import dataclass

from .bgzf import BGZF_MAX_BLOCK, split_virtual_offset

TBI_MAGIC = b"TBI\x01"

# UCSC/BAM binning scheme: (shift, first bin id) for each of the five levels.
_BIN_LEVELS = ((26, 1), (23, 9), (20, 73), (17, 585), (14, 4681))

# The linear index buckets the reference into 16 kb windows.
_LINEAR_SHIFT = 14


def reg2bins(beg: int, end: int) -> list[int]:
    """Bin ids that may hold features overlapping [beg, end), 0-based half-open."""
    bins = [0]
    if end <= beg:
        return bins
    last = end - 1
    for shift, first in _BIN_LEVELS:
        bins.extend(range(first + (beg >> shift), first + (last >> shift) + 1))
    return bins


@dataclass(frozen=True)
class TabixIndex:
    format: int
    col_seq: int
    col_beg: int
    col_end: int
    meta: int
    skip: int
    names: tuple[str, ...]
    # Per reference sequence, in the same order as `names`.
    bins: tuple[dict[int, list[tuple[int, int]]], ...]
    linear: tuple[tuple[int, ...], ...]

    def reference_index(self, chrom: str) -> int | None:
        """Resolve a contig name, tolerating the chr-prefix mismatch.

        ClinVar names contigs bare ("17"); plenty of callers say "chr17".
        """
        candidates = (chrom, chrom.removeprefix("chr"), f"chr{chrom}")
        for name in candidates:
            if name in self.names:
                return self.names.index(name)
        return None

    def query_chunks(self, chrom: str, beg: int, end: int) -> list[tuple[int, int]]:
        """Virtual-offset chunks that may contain records overlapping [beg, end)."""
        ref = self.reference_index(chrom)
        if ref is None:
            return []

        bin_map = self.bins[ref]
        linear = self.linear[ref]

        # The linear index gives a floor: no record starting at or after `beg`
        # lives earlier in the file than this offset. It prunes the large
        # low-resolution bins that would otherwise drag in the whole contig.
        window = beg >> _LINEAR_SHIFT
        if not linear:
            floor = 0
        elif window < len(linear):
            floor = linear[window]
        else:
            floor = linear[-1]

        chunks: list[tuple[int, int]] = []
        for bin_id in reg2bins(beg, end):
            for start, stop in bin_map.get(bin_id, ()):
                if stop > floor:
                    chunks.append((max(start, floor), stop))

        return _merge_chunks(chunks)

    def byte_ranges(self, chrom: str, beg: int, end: int) -> list[tuple[int, int]]:
        """Concrete inclusive HTTP byte ranges for a region.

        The end of a chunk is a virtual offset pointing *into* a block, so we
        extend to that block's start plus one maximum block length to be sure
        the member is whole.
        """
        ranges = []
        for start, stop in self.query_chunks(chrom, beg, end):
            first_block, _ = split_virtual_offset(start)
            last_block, _ = split_virtual_offset(stop)
            ranges.append((first_block, last_block + BGZF_MAX_BLOCK - 1))
        return _merge_ranges(ranges)


def _merge_chunks(chunks: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Coalesce chunks that land in the same or adjacent compressed blocks."""
    if not chunks:
        return []
    chunks.sort()
    merged = [chunks[0]]
    for start, stop in chunks[1:]:
        prev_start, prev_stop = merged[-1]
        if split_virtual_offset(start)[0] <= split_virtual_offset(prev_stop)[0]:
            merged[-1] = (prev_start, max(prev_stop, stop))
        else:
            merged.append((start, stop))
    return merged


def _merge_ranges(ranges: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Coalesce overlapping byte ranges so we issue as few requests as possible."""
    if not ranges:
        return []
    ranges.sort()
    merged = [ranges[0]]
    for start, stop in ranges[1:]:
        prev_start, prev_stop = merged[-1]
        if start <= prev_stop + 1:
            merged[-1] = (prev_start, max(prev_stop, stop))
        else:
            merged.append((start, stop))
    return merged


def parse(raw: bytes) -> TabixIndex:
    """Parse the *inflated* contents of a .tbi file."""
    if raw[:4] != TBI_MAGIC:
        raise ValueError(f"not a tabix index: magic was {raw[:4]!r}")

    n_ref, fmt, col_seq, col_beg, col_end, meta, skip, l_nm = struct.unpack_from(
        "<8i", raw, 4
    )
    pos = 36

    name_blob = raw[pos : pos + l_nm]
    pos += l_nm
    names = tuple(n.decode("ascii") for n in name_blob.split(b"\x00") if n)

    bins: list[dict[int, list[tuple[int, int]]]] = []
    linear: list[tuple[int, ...]] = []

    for _ in range(n_ref):
        (n_bin,) = struct.unpack_from("<i", raw, pos)
        pos += 4

        bin_map: dict[int, list[tuple[int, int]]] = {}
        for _ in range(n_bin):
            bin_id, n_chunk = struct.unpack_from("<Ii", raw, pos)
            pos += 8
            chunk_data = struct.unpack_from(f"<{2 * n_chunk}Q", raw, pos)
            pos += 16 * n_chunk
            bin_map[bin_id] = [
                (chunk_data[i], chunk_data[i + 1]) for i in range(0, len(chunk_data), 2)
            ]
        bins.append(bin_map)

        (n_intv,) = struct.unpack_from("<i", raw, pos)
        pos += 4
        linear.append(struct.unpack_from(f"<{n_intv}Q", raw, pos) if n_intv else ())
        pos += 8 * n_intv

    return TabixIndex(
        format=fmt,
        col_seq=col_seq,
        col_beg=col_beg,
        col_end=col_end,
        meta=meta,
        skip=skip,
        names=names,
        bins=tuple(bins),
        linear=tuple(linear),
    )
