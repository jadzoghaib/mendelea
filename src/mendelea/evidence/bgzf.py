"""BGZF (blocked gzip) reading.

A BGZF file is a concatenation of independent gzip members, each holding at
most 64 KiB of uncompressed data. Independence is the whole point: a byte
range that begins on a member boundary can be inflated without touching the
rest of the file. That is what lets us pull one gene out of a 193 MB ClinVar
release over HTTP instead of downloading all of it.

Nothing here knows about tabix or VCF -- see tabix.py for the index that turns
a genomic region into the byte ranges to fetch.
"""

from __future__ import annotations

import zlib

# Max uncompressed payload of a single BGZF member. Used to decide how far
# past the last chunk we must read to be sure we captured its final member.
BGZF_MAX_BLOCK = 65536

# gzip member magic with FEXTRA set, which BGZF always sets.
BGZF_MAGIC = b"\x1f\x8b\x08\x04"


def split_virtual_offset(voffset: int) -> tuple[int, int]:
    """Split a tabix virtual offset into (file offset of block, offset within block).

    Virtual offsets pack both coordinates into one 64-bit integer: the upper
    48 bits address the compressed block in the file, the lower 16 address a
    byte inside that block once inflated.
    """
    return voffset >> 16, voffset & 0xFFFF


def decompress(data: bytes) -> bytes:
    """Inflate a run of concatenated gzip members, tolerating a truncated tail.

    A range fetched from a remote BGZF file routinely ends mid-member. We keep
    every member that inflated cleanly and stop at the first one that did not,
    rather than raising -- callers discard the trailing partial record anyway.
    """
    out = bytearray()
    pos = 0
    size = len(data)

    while pos < size:
        engine = zlib.decompressobj(31)  # 31 => expect a gzip wrapper
        try:
            out += engine.decompress(data[pos:])
            out += engine.flush()
        except zlib.error:
            break

        if not engine.eof:
            # Ran out of bytes inside this member: the fetched range was cut
            # short. Whatever inflated is still usable.
            break

        consumed = size - pos - len(engine.unused_data)
        if consumed <= 0:
            break
        pos += consumed

    return bytes(out)


def iter_complete_lines(raw: bytes) -> list[bytes]:
    """Split inflated bytes into whole lines, dropping any partial final line.

    The last line of a truncated range is almost never complete, and a
    half-parsed VCF record is worse than a missing one.
    """
    if not raw:
        return []
    lines = raw.split(b"\n")
    if raw.endswith(b"\n"):
        lines.pop()  # trailing empty element from the final newline
        return lines
    lines.pop()  # genuinely incomplete final line
    return lines
