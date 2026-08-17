"""Allele normalisation and stable computed identifiers.

Variant identity across time is the problem that quietly destroys this product
if it is got wrong. The same allele is written differently by different
sources and in different years: transcript versions move, indels are padded
inconsistently, contigs gain and lose a "chr" prefix. If a 2019 record does
not hash to the same identifier as its 2026 counterpart, every movement
statistic downstream is wrong -- and wrong in the silent direction, inventing
"changes" that are really just re-spellings.

So every record from every source and every snapshot passes through here.

Conformance note
----------------
The digest construction (sha512, truncated to 24 bytes, base64url) is the one
specified by GA4GH VRS. The *serialisation* is not: true VRS identifies the
reference sequence by a refget accession resolved through SeqRepo, which is a
multi-gigabyte dependency we do not want in Phase 1. We therefore serialise
(assembly, contig, position, ref, alt) directly and namespace the result
`mendelea:VA.` rather than `ga4gh:VA.` -- claiming a ga4gh CURIE we cannot
reproduce would be worse than useless.

The property we actually need in Phase 0/1 is *stability*, not federation:
the same allele must always hash the same way. Swapping in real VRS is a
contained change to `allele_id`, and is Phase 1.5 work once SeqRepo is
available. Until then this identifier must not be published as a VRS ID.
"""

from __future__ import annotations

import base64
import hashlib
import re

VALID_ALLELE = re.compile(r"^[ACGTN]*$", re.IGNORECASE)

NAMESPACE = "mendelea:VA."


def sha512t24u(blob: bytes) -> str:
    """GA4GH truncated-digest: sha512, first 24 bytes, unpadded base64url."""
    digest = hashlib.sha512(blob).digest()[:24]
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def normalise_contig(contig: str) -> str:
    """Strip the chr prefix and canonicalise the mitochondrial contig."""
    name = contig.strip()
    name = name[3:] if name.lower().startswith("chr") else name
    return "MT" if name.upper() in {"M", "MT"} else name.upper()


def trim(pos: int, ref: str, alt: str) -> tuple[int, str, str]:
    """Remove shared suffix then shared prefix from an allele pair.

    This is the reference-free half of VCF normalisation. It makes padded
    representations of the same indel agree, which is the common case in
    ClinVar. It does *not* left-align across a repeat region -- that needs the
    reference sequence, and is why `allele_id` is only claimed to be stable,
    not canonical. ClinVar publishes pre-normalised records, so within our
    ClinVar-to-ClinVar timeline this is sufficient; it becomes a real
    limitation only when matching a customer's own file (Phase 3), where the
    reference sequence must be in play.
    """
    ref = ref.upper()
    alt = alt.upper()

    # Shared suffix, but never consume a whole allele: VCF requires at least
    # one base on each side.
    while len(ref) > 1 and len(alt) > 1 and ref[-1] == alt[-1]:
        ref, alt = ref[:-1], alt[:-1]

    # Shared prefix, advancing the coordinate as we go.
    while len(ref) > 1 and len(alt) > 1 and ref[0] == alt[0]:
        ref, alt = ref[1:], alt[1:]
        pos += 1

    return pos, ref, alt


def allele_id(assembly: str, contig: str, pos: int, ref: str, alt: str) -> str:
    """Stable identifier for a normalised allele.

    Raises on alleles we cannot represent (symbolic ALTs like <DEL>, or
    anything non-ACGTN) rather than hashing garbage into the timeline.
    """
    if not ref or not alt:
        raise ValueError(f"empty allele: ref={ref!r} alt={alt!r}")
    if not VALID_ALLELE.match(ref) or not VALID_ALLELE.match(alt):
        raise ValueError(f"non-nucleotide allele: ref={ref!r} alt={alt!r}")

    contig = normalise_contig(contig)
    pos, ref, alt = trim(int(pos), ref, alt)

    blob = f"{assembly}\t{contig}\t{pos}\t{ref}\t{alt}".encode()
    return NAMESPACE + sha512t24u(blob)
