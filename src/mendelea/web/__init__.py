"""Read-only web surface: the evidence time machine.

Public ClinVar data only. The case and decision planes are deliberately not
reachable from here -- serving them needs auth and tenant isolation, which is
Phase 3 work.
"""
