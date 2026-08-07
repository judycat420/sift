"""Sift: a provenance-preserving pipeline for plant reference data.

WHY THIS PACKAGE EXISTS
-----------------------
Botanical reference data is aggregated from many sources whose licences,
taxonomies and update cadences disagree with each other. The failure mode is
not "wrong data" so much as "data whose origin has been lost" — once a claim
is flattened into a bare string, no downstream consumer can tell whether it
came from a peer-reviewed dataset, a crowd-sourced observation, or a guess.

INVARIANT PROTECTED
-------------------
Every user-facing factual claim leaving this package carries its source and a
confidence with it (see STANDARDS.md, rule 4). Sub-packages may transform,
filter, or drop claims; none of them may launder one into an unattributed
value. Records that cannot be attributed are dropped and counted, never
guessed (rule 5).
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
