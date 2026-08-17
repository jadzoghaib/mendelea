"""Left-alignment of indels.

The failure this prevents is silent: an unaligned variant in a laboratory's
export simply fails to join the evidence plane and vanishes from the report.
The customer sees a shorter list, not an error.
"""

import pytest

from mendelea.evidence.normalize import allele_id
from mendelea.evidence.reference import ReferenceRegion, left_align


class FakeReference:
    """An in-memory reference so these tests stay offline and exact."""

    def __init__(self, contig: str, start: int, sequence: str):
        self.region = ReferenceRegion(contig, start, sequence)

    def base_at(self, contig, pos):
        return self.region.base_at(pos) if contig == self.region.contig else None


#            pos: 100 101 102 103 104 105 106 107
#                  G   T   T   T   T   T   C   A
POLY_T = FakeReference("17", 100, "GTTTTTCA")


def test_substitution_is_untouched():
    assert left_align(POLY_T, "17", 103, "T", "A") == (103, "T", "A")


def test_deletion_in_a_repeat_rolls_to_the_leftmost_position():
    """Deleting any T from the run must land on one canonical answer."""
    results = {left_align(POLY_T, "17", p, "TT", "T") for p in (101, 102, 103, 104)}
    assert len(results) == 1


def test_insertion_in_a_repeat_rolls_left():
    results = {left_align(POLY_T, "17", p, "T", "TT") for p in (101, 102, 103, 104)}
    assert len(results) == 1


def test_already_left_aligned_input_is_unchanged():
    """The property that lets us add a reference without invalidating ClinVar IDs."""
    once = left_align(POLY_T, "17", 101, "GT", "G")
    twice = left_align(POLY_T, "17", once[0], once[1], once[2])
    assert once == twice


def test_shift_stops_at_a_non_matching_base():
    # Deleting the C at 106 cannot roll into the T run.
    pos, ref, alt = left_align(POLY_T, "17", 106, "CA", "C")
    assert pos >= 105


def test_shift_stops_outside_the_cached_region():
    """Better to stop than to guess at bases we do not have."""
    short = FakeReference("17", 100, "TTT")
    pos, ref, alt = left_align(short, "17", 101, "TT", "T")
    assert pos >= 100


def test_identical_alleles_are_rejected():
    with pytest.raises(ValueError, match="identical"):
        left_align(POLY_T, "17", 103, "T", "T")


def test_unknown_contig_does_not_shift():
    pos, ref, alt = left_align(POLY_T, "22", 103, "TT", "T")
    assert pos == 103


# --------------------------------------------------------------------------
# The join this all exists to protect
# --------------------------------------------------------------------------


def test_shifted_spellings_hash_to_the_same_identifier():
    """A lab writing the deletion at 104 must match ClinVar writing it at 101."""
    lab = allele_id("GRCh38", "17", 104, "TT", "T", reference=POLY_T)
    clinvar = allele_id("GRCh38", "17", 101, "GT", "G", reference=POLY_T)
    assert lab == clinvar


def test_without_a_reference_those_spellings_do_not_match():
    """Documents precisely what the reference buys -- and what its absence costs."""
    lab = allele_id("GRCh38", "17", 104, "TT", "T")
    clinvar = allele_id("GRCh38", "17", 101, "GT", "G")
    assert lab != clinvar


def test_substitution_identity_is_unaffected_by_the_reference():
    """Adding a reference must not change an identifier that was already right."""
    with_ref = allele_id("GRCh38", "17", 103, "T", "A", reference=POLY_T)
    without = allele_id("GRCh38", "17", 103, "T", "A")
    assert with_ref == without


def test_region_bounds():
    region = ReferenceRegion("17", 100, "GTTTTTCA")
    assert region.start == 100
    assert region.end == 107
    assert region.base_at(100) == "G"
    assert region.base_at(107) == "A"
    assert region.base_at(99) is None
    assert region.base_at(108) is None


def test_soft_masked_sequence_is_normalised():
    """UCSC returns soft-masked lowercase; identity must not depend on case."""
    region = ReferenceRegion("17", 100, "gtttttca")
    assert region.base_at(100) == "G"


def test_ambiguous_bases_are_treated_as_unknown():
    region = ReferenceRegion("17", 100, "GNNTTTCA")
    assert region.base_at(101) is None
