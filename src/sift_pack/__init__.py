"""Sift pack builder: the Python half that produces study packs.

WHY THIS PACKAGE EXISTS
-----------------------
Reference data about living things is aggregated from sources whose licences,
taxonomies and update cadences disagree with each other. The failure mode is
not "wrong data" so much as "data whose origin has been lost" — once a claim
is flattened into a bare string, no downstream consumer can tell whether it
came from a curated dataset, a crowd-sourced observation, or a guess.

`sift_pack` is the build half: it fetches, filters and assembles packs. The
runtime half consumes the manifest this package emits and never talks to any
upstream source itself. `manifest.py` is the contract between the two.

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
