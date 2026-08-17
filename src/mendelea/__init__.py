"""Mendelea -- an evidence timeline for reported genetic variants.

Three planes, kept strictly apart:

  evidence/  public, shared by every tenant, immutable, large
  cases/     one tenant's reported variants, private, pseudonymous, tiny
  decisions/ one tenant's review log, append-only, hash-chained

The separation is what lets the same image run as shared SaaS, as a dedicated
instance, or air-gapped on-premise, with only configuration changing.
"""

__version__ = "0.1.0"
