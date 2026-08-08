"""USDA PLANTS ingest and reconciliation: the only source of nativity claims.

WHY THIS SUBPACKAGE EXISTS
--------------------------
Every stage before this one was arranged so that a nativity claim could not
exist without a source. This is the source. Code here is the only code in Sift
that can produce an `Axis1Result`, and therefore the only code that can cause a
learner to be told a plant is native.

INVARIANT PROTECTED
-------------------
A claim leaves here only when a specific PLANTS record was matched to a specific
iNaturalist taxon by a rule that is written down and named. Everything else —
no match, an ambiguous match, a status PLANTS reports as uncertain — returns
`None`, and `None` means the taxon is dropped and written to the unmatched
report. There is no default nativity, and no code path that supplies one.
"""

__all__: list[str] = []
