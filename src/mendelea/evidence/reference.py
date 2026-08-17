"""Reference sequence access, for left-aligning indels.

Why this exists
---------------
Trimming shared bases makes padded spellings of the same variant agree. It
does not make *shifted* spellings agree. Inside a repeat, one deletion has
many equally valid coordinates:

    reference   ... G T T T T T C ...
                      ^ ^ ^ ^ ^
    "delete a T" is writable at five different positions, all correct VCF

ClinVar left-aligns its records, so ClinVar-to-ClinVar comparison never
noticed. A laboratory's own export has no such guarantee -- and the failure is
silent: the variant simply fails to join, and it is quietly absent from the
"what moved" report. A customer would never see it, which is the worst
possible way to be wrong.

Left-alignment needs the reference sequence, so we fetch it once per gene
region and cache it. A gene is ~100 kb; the whole 31-gene panel is a few MB.
That is the same trick the evidence ingest uses -- fetch the region, not the
genome.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import requests

UCSC = "https://api.genome.ucsc.edu/getData/sequence"
ENSEMBL = "https://rest.ensembl.org/sequence/region/human/{contig}:{start}..{end}"

# How far an indel may roll left before we give up. Real repeats are short;
# a runaway shift means something is wrong with the coordinates.
MAX_SHIFT = 500


class ReferenceUnavailable(RuntimeError):
    pass


class ReferenceRegion:
    """One cached stretch of reference sequence, addressed by 1-based position."""

    def __init__(self, contig: str, start: int, sequence: str):
        self.contig = contig
        self.start = start  # 1-based position of sequence[0]
        self.sequence = sequence.upper()

    @property
    def end(self) -> int:
        return self.start + len(self.sequence) - 1

    def base_at(self, pos: int) -> str | None:
        index = pos - self.start
        if index < 0 or index >= len(self.sequence):
            return None
        base = self.sequence[index]
        return base if base in "ACGT" else None


class ReferenceCache:
    """Fetches and caches reference regions.

    Two providers because a single upstream outage must not silently disable
    left-alignment -- degrading to trim-only would reintroduce exactly the
    join failures this module exists to prevent, without saying so.
    """

    def __init__(self, cache_dir: Path, session: requests.Session | None = None,
                 assembly: str = "GRCh38"):
        self.cache_dir = cache_dir
        self.assembly = assembly
        self.session = session or requests.Session()
        self._regions: list[ReferenceRegion] = []

    # -- providers ---------------------------------------------------------

    def _get(self, url: str, params=None, headers=None, key: str = "dna") -> str | None:
        """One provider call, retried through transient transport failures.

        Dropped connections are routine against both of these hosts, and a
        single failure here silently disables left-alignment for a whole gene.
        """
        for attempt in range(3):
            try:
                response = self.session.get(
                    url, params=params, headers=headers, timeout=60
                )
            except Exception:  # noqa: BLE001 - retry transport errors
                time.sleep(2**attempt)
                continue
            if response.ok:
                return (response.json() or {}).get(key)
            if response.status_code < 500:
                return None  # a real "no", not a blip
            time.sleep(2**attempt)
        return None

    def _from_ucsc(self, contig: str, start: int, end: int) -> str | None:
        genome = "hg38" if self.assembly == "GRCh38" else "hg19"
        return self._get(
            UCSC,
            params={"genome": genome, "chrom": f"chr{contig}",
                    "start": start - 1, "end": end},  # UCSC is 0-based, end-exclusive
            key="dna",
        )

    def _from_ensembl(self, contig: str, start: int, end: int) -> str | None:
        return self._get(
            ENSEMBL.format(contig=contig, start=start, end=end),
            headers={"Content-Type": "application/json"},
            key="seq",
        )

    # -- cache -------------------------------------------------------------

    def _path(self, contig: str, start: int, end: int) -> Path:
        return self.cache_dir / self.assembly / f"{contig}_{start}_{end}.json"

    def load_region(self, contig: str, start: int, end: int) -> ReferenceRegion:
        contig = contig.removeprefix("chr")
        path = self._path(contig, start, end)

        if path.exists():
            payload = json.loads(path.read_text(encoding="utf-8"))
            region = ReferenceRegion(contig, payload["start"], payload["sequence"])
        else:
            sequence = self._from_ucsc(contig, start, end) or self._from_ensembl(contig, start, end)
            if not sequence:
                raise ReferenceUnavailable(
                    f"no reference sequence for {contig}:{start}-{end}"
                )
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps({"start": start, "sequence": sequence.upper()}),
                encoding="utf-8",
            )
            region = ReferenceRegion(contig, start, sequence)

        self._regions.append(region)
        return region

    # -- lookup ------------------------------------------------------------

    def base_at(self, contig: str, pos: int) -> str | None:
        """Base at a 1-based position, or None if no loaded region covers it."""
        contig = contig.removeprefix("chr")
        for region in self._regions:
            if region.contig == contig and region.start <= pos <= region.end:
                return region.base_at(pos)
        return None

    def covers(self, contig: str, pos: int) -> bool:
        return self.base_at(contig, pos) is not None


def left_align(reference, contig: str, pos: int, ref: str, alt: str,
               max_shift: int = MAX_SHIFT) -> tuple[int, str, str]:
    """Roll an indel as far left as the reference allows, then re-pad to VCF form.

    `reference` is anything with `.base_at(contig, pos) -> str | None`.

    A no-op for substitutions, and a no-op for indels that are already
    left-aligned -- which is why applying this to ClinVar's own records leaves
    every identifier unchanged.
    """
    ref, alt = ref.upper(), alt.upper()

    # Reduce to the pure indel form, where one side may be empty.
    while ref and alt and ref[-1] == alt[-1]:
        ref, alt = ref[:-1], alt[:-1]
    while ref and alt and ref[0] == alt[0]:
        ref, alt = ref[1:], alt[1:]
        pos += 1

    if ref and alt:
        return pos, ref, alt  # substitution or MNV: nothing can shift

    if not ref and not alt:
        raise ValueError("ref and alt are identical")

    shifts = 0
    while pos > 1 and shifts < max_shift:
        previous = reference.base_at(contig, pos - 1)
        if previous is None:
            break  # outside cached reference; stop rather than guess
        moving = ref or alt
        if moving[-1] != previous:
            break
        moving = previous + moving[:-1]  # rotate the repeat unit left
        pos -= 1
        shifts += 1
        if ref:
            ref = moving
        else:
            alt = moving

    # VCF requires a padding base on both alleles for indels.
    pad = reference.base_at(contig, pos - 1)
    if pad is None:
        return pos, ref, alt
    return pos - 1, pad + ref, pad + alt
