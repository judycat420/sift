"""The study half: deciding whether a typed answer is right.

WHY THIS PACKAGE EXISTS
-----------------------
Everything before this point was about not shipping a claim Sift could not
source. This package is about not *accepting* an answer the learner did not
give. It is the last place in the system where being generous is the same as
lying: a matcher that says "close enough" to a different species has taught
somebody that two plants are one plant, and it has done so at the exact moment
they were most receptive to being taught.

Forgiveness here exists to absorb typing and spelling noise — a transposed
letter, a missing hyphen, a diacritic nobody can type. It does not exist to
absorb naming a different plant, and the confusion guard in `matcher` is what
keeps those two things apart.

INVARIANT PROTECTED
-------------------
Nothing in this package decides what a match is *worth*. It reports what
matched, by which rule, and at what distance; ratings, scheduling and review
history belong to M6 and are not importable from here.
"""

from __future__ import annotations

__all__: list[str] = []
