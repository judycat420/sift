"""The normalisation cascade applied to both sides of every comparison.

WHY THIS MODULE EXISTS
----------------------
A learner typing on a phone will not produce `Arisaema triphyllum` with the
capital A, and will not produce the hyphens in `Jack-in-the-Pulpit` in the same
places the manifest happens to have them. None of that is a knowledge gap, so
none of it should read as one.

What this module deliberately does *not* do is as important as what it does. It
does not stem, so `raspberry` and `raspberries` stay distinct — and more to the
point, so `Rubus` and `Ruby` cannot be collapsed by a suffix rule that has no
idea one is a genus. It does not strip punctuation inside a word, so
`St. John's wort` keeps its apostrophe rather than becoming `st johns wort` and
colliding with something else. Every transformation here is one that cannot
change which plant a string names.

INVARIANT PROTECTED
-------------------
`normalize` is idempotent: `normalize(normalize(s)) == normalize(s)` for every
input. Both the learner's typing and every accepted answer go through exactly
this function, so a difference that survives it is a real difference and not an
artefact of one side having been cleaned differently from the other.
"""

from __future__ import annotations

import re
import unicodedata
from functools import lru_cache

__all__ = ["STOPWORDS", "normalize", "token_set"]

_TOKEN_CACHE_SIZE = 4096
"""Distinct strings whose token sets are remembered.

Comfortably larger than a pack's accepted answers, so grading a deck recomputes
nothing: the matcher compares one answer against every card, and every card's
tokens are the same on the next keystroke as they were on the last.
"""

_WHITESPACE = re.compile(r"\s+")

_SEPARATORS = str.maketrans({"-": " ", "_": " "})
"""Characters that join words in one spelling and separate them in another.

`Jack-in-the-Pulpit` and `jack in the pulpit` are the same answer, and which one
a source recorded is an accident of that source's house style.
"""

_EDGE_PUNCTUATION = " \t\n\r\"'`.,;:!?()[]{}<>"
"""Stripped from the ends of the whole string only, never from inside a word.

Trailing and leading punctuation is typing debris — a stray full stop, a quote
somebody's keyboard inserted. The same characters *inside* a word are part of
the name: removing the apostrophe from `St. John's wort` or the full stop from
an abbreviation changes the string in a way that can change what it names.
"""

STOPWORDS = frozenset({"wild", "american"})
"""Words dropped before an unordered token comparison.

Modifiers a learner may reasonably omit — `bergamot` for `wild bergamot`,
`beech` for `American beech`. They are dropped **only** for the token-set rule,
never from the stored answer, and never from an exact comparison.

The list is short because it was measured rather than assumed. M5 shipped with
nine words, and three pairs of them turned out to be the *only* thing separating
two plants in the Michigan deck: `common`/`giant` ragweed, `greater`/`lesser`
celandine — different genera, different families — and `northern`/`southern`
blue flag. Dropping both members of such a pair collapses two species onto one
token set, converting a correct answer for one plant into a false accept for the
other.

The mechanism is worth stating precisely, because it is not what it looks like.
No single one of those words is dangerous: adding any one of the nine alone
merges nothing. The damage needs *both* halves of a contrasting pair, which is
why a word cannot be cleared by inspecting it on its own, and why the guard is
`tests/test_study_normalize.py::test_no_two_cards_collapse_onto_one_token_set`
— a check of the whole list against every shipped deck. Adding a word means
running that test, not arguing about the word.

`eastern` is gone with the rest, and it costs something real: `white pine` no
longer answers `eastern white pine` at this rule. That is the trade this list
now makes — see `docs/decisions.md`, 2026-08-09.
"""


def normalize(text: str) -> str:
    """Reduce a string to the form both sides of a comparison are held in.

    The cascade, in order: lowercase; decompose to NFKD and drop combining
    marks; turn hyphens and underscores into spaces; collapse runs of
    whitespace; strip punctuation from the two ends.

    Args:
        text: Raw text — a learner's typing, or an accepted answer from a
            manifest. Either may be empty.

    Returns:
        The normalised form, which may be empty when the input held nothing but
        punctuation and space. An empty result is returned as such rather than
        being replaced by the original: nothing here guesses.

    Example:
        >>> normalize("  Butterfly-Weed.  ")
        'butterfly weed'
        >>> normalize("St. John's wort")
        "st. john's wort"
        >>> normalize("Ærenea")
        'ærenea'
        >>> normalize("") == ""
        True
    """
    lowered = text.lower()
    decomposed = unicodedata.normalize("NFKD", lowered)
    without_marks = "".join(c for c in decomposed if not unicodedata.combining(c))
    spaced = without_marks.translate(_SEPARATORS)
    collapsed = _WHITESPACE.sub(" ", spaced).strip()
    return collapsed.strip(_EDGE_PUNCTUATION).strip()


@lru_cache(maxsize=_TOKEN_CACHE_SIZE)
def token_set(text: str) -> frozenset[str]:
    """Split normalised text into unordered tokens, minus the stopwords.

    Memoised: this is a pure function of its argument, and the matcher asks for
    the same accepted answers' tokens once per card per keystroke.

    Used only by the token-set rule in `sift_pack.study.matcher`. The result is
    a set, so word order stops mattering — `flag blue northern` and `northern
    blue flag` agree, which is the point.

    Args:
        text: Already-normalised text. Passing raw text is a caller error that
            will simply produce worse tokens, not an exception.

    Returns:
        The tokens that survived stopword removal. When *every* token was a
        stopword the original tokens are returned instead, because a name made
        entirely of modifiers is still a name and reducing it to the empty set
        would make it match everything.

    Example:
        >>> sorted(token_set("wild bergamot"))
        ['bergamot']
        >>> sorted(token_set("american beech"))
        ['beech']
        >>> sorted(token_set("wild"))
        ['wild']
        >>> sorted(token_set("eastern purple coneflower"))  # "eastern" discriminates
        ['coneflower', 'eastern', 'purple']
        >>> token_set("")
        frozenset()
    """
    tokens = text.split()
    kept = frozenset(token for token in tokens if token not in STOPWORDS)
    return kept if kept else frozenset(tokens)
