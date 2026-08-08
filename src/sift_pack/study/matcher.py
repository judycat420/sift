"""Grading a typed answer, and refusing to grade a guess generously.

WHY THIS MODULE EXISTS
----------------------
Four rules, tried in order, decide whether what the learner typed is what the
card asked for: an exact match, an unordered token match, a small edit distance,
and a phonetic match on scientific names. Each is more forgiving than the last,
and each exists to absorb a specific kind of noise — a missing modifier, a
transposed letter, a plausible-but-wrong spelling of a Latin name nobody has
heard said aloud.

Forgiveness that stops there is a bug. Every rule after the first is a rule that
can also say yes to the *wrong* plant, and the more forgiving the rule, the more
plants it can say yes to. `swamp milkweed` typed on a butterfly-milkweed card is
not a spelling mistake — it is the exact common name of a different Asclepias
growing in the same state, and a matcher that shrugs and accepts it has taught
somebody the two are one plant at the precise moment they were trying to learn
the difference.

THE CONFUSION GUARD
-------------------
So before any fuzzy rule grants credit, the answer is scored against every other
card in the deck. If another taxon matches by a better rule, or by the same rule
no worse, the answer is refused and the taxon it actually named is recorded. The
learner is told they named a different plant, which is the useful thing to know.

Exact matches are exempt, because an exact match is not a guess. But if one
exact answer fits two different cards — a common name two plants genuinely share
— the result is `ambiguous` and names both, rather than crediting whichever card
happened to come up.

INVARIANT PROTECTED
-------------------
No answer that is a better fit for another taxon is ever scored `correct` or
`partial` for this one. Nothing here decides what a match is *worth*: the result
records which rule fired and at what distance, and M6 turns that into a rating.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from metaphone import doublemetaphone

from sift_pack.study.answers import AcceptedAnswers, Deck
from sift_pack.study.normalize import normalize, token_set

__all__ = [
    "EXACT",
    "METAPHONE",
    "MISSPELLING",
    "TOKEN_SET",
    "MatchOutcome",
    "MatchResult",
    "damerau_levenshtein",
    "edit_threshold",
    "phonetic_codes",
    "score",
]

MatchOutcome = Literal["correct", "partial", "wrong", "ambiguous"]
"""What the matcher concluded.

`partial` means genus-only credit on a species-rank card, and is never produced
for a genus-rank card — there the genus is the whole answer, so there is nothing
to award partially (`sift_pack.study.answers`).
"""

EXACT = 1
TOKEN_SET = 2
MISSPELLING = 3
METAPHONE = 4
"""The four rules, in the order they are tried. Lower is a stronger match.

Named rather than written as bare integers at the call sites, because
`cascade_level=3` in a result is meant to be readable as "this was accepted as a
misspelling" by somebody who has not read this module.
"""

_EDIT_DIVISOR = 6
"""How much slack the misspelling rule allows, as a fraction of target length.

One edit per six characters, floor one. Deliberately mean: `Acer saccharum` and
`Acer saccharinum` are two edits apart and both in the Michigan deck, so a
threshold generous enough to bridge them would make sugar and silver maple the
same answer.
"""


def damerau_levenshtein(left: str, right: str) -> int:
    """Edit distance counting insertions, deletions, substitutions and swaps.

    The unrestricted Damerau-Levenshtein distance, so `tuberosa` -> `tubreosa`
    costs one edit rather than two. Transposition is counted as a single edit
    because it is the single commonest typing error, and charging two for it
    would push ordinary typos past a threshold sized for real mistakes.

    Args:
        left: One string, already normalised.
        right: The other, already normalised.

    Returns:
        The number of edits between them; zero when equal.

    Example:
        >>> damerau_levenshtein("tuberosa", "tubreosa")
        1
        >>> damerau_levenshtein("asclepias", "asklepias")
        1
        >>> damerau_levenshtein("", "abc")
        3
    """
    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)

    # Unrestricted DL needs, for each position, the last row in which each
    # character was seen; that is what separates it from the restricted
    # (optimal string alignment) variant, which cannot handle a transposition
    # with other edits between the swapped pair.
    last_row: dict[str, int] = {}
    height, width = len(left), len(right)
    infinity = height + width
    matrix = [[infinity] * (width + 2) for _ in range(height + 2)]
    matrix[0][0] = infinity
    for i in range(height + 1):
        matrix[i + 1][0] = infinity
        matrix[i + 1][1] = i
    for j in range(width + 1):
        matrix[0][j + 1] = infinity
        matrix[1][j + 1] = j

    for i in range(1, height + 1):
        last_match_column = 0
        for j in range(1, width + 1):
            last_match_row = last_row.get(right[j - 1], 0)
            cost = 0 if left[i - 1] == right[j - 1] else 1
            matrix[i + 1][j + 1] = min(
                matrix[i][j] + cost,  # substitution
                matrix[i + 1][j] + 1,  # insertion
                matrix[i][j + 1] + 1,  # deletion
                matrix[last_match_row][last_match_column]
                + (i - last_match_row - 1)
                + 1
                + (j - last_match_column - 1),  # transposition
            )
            if cost == 0:
                last_match_column = j
        last_row[left[i - 1]] = i

    return matrix[height + 1][width + 1]


def edit_threshold(target: str) -> int:
    """How many edits the misspelling rule tolerates against one target.

    Scales with the target so that a long Latin binomial gets the slack a long
    word needs, while a short common name stays tight. Floor of one, so even a
    four-letter answer survives a single slip.

    Args:
        target: The accepted answer being compared against, normalised.

    Returns:
        The maximum tolerated edit distance, at least 1.

    Example:
        >>> edit_threshold("asclepias tuberosa")
        3
        >>> edit_threshold("oak")
        1
    """
    return max(1, len(target) // _EDIT_DIVISOR)


_PHONETIC_CACHE_SIZE = 4096
"""Distinct strings whose phonetic codes are remembered.

Double Metaphone is a few hundred lines of pure-Python branching, and grading
one answer asks for the codes of every scientific name in the deck. Memoising
turns that from the dominant cost into a dictionary lookup.
"""


@lru_cache(maxsize=_PHONETIC_CACHE_SIZE)
def phonetic_codes(text: str) -> tuple[frozenset[str], ...]:
    """Double Metaphone codes for a string, per word and in order.

    A tuple of per-word code sets rather than one pooled set, because pooling
    loses the word boundary and with it the difference between a genus and a
    binomial: `asclepias` shares a code with the first word of
    `asclepias tuberosa`, and a pooled comparison would call that a match on the
    whole species name. Keeping the words apart means a phonetic match has to
    line up word for word.

    Applied to scientific names only. English phonetics collapse far too many
    distinct common names onto one code to be safe there — the point of this
    rule is `asklepias` for *Asclepias*, not deciding that two unrelated
    wildflowers sound alike.

    Args:
        text: A normalised scientific name.

    Returns:
        One code set per word, in order. Words yielding no code are kept as
        empty sets so positions still line up; an empty tuple means there was
        nothing to encode, and nothing can match it.

    Example:
        >>> "AXNS" in phonetic_codes("echinacea")[0]
        True
        >>> len(phonetic_codes("asclepias tuberosa"))
        2
        >>> phonetic_codes("")
        ()
    """
    return tuple(frozenset(code for code in doublemetaphone(word) if code) for word in text.split())


def _sounds_alike(answer: str, target: str) -> bool:
    """Whether two scientific names match word for word, phonetically.

    Requires the same number of words and a shared code at every position, so
    `asklepias tuberosa` matches `asclepias tuberosa` while `asclepias` alone
    does not.
    """
    left, right = phonetic_codes(answer), phonetic_codes(target)
    if not left or len(left) != len(right):
        return False
    return all(a & b for a, b in zip(left, right, strict=True))


@dataclass(frozen=True, slots=True)
class MatchResult:
    """What the matcher concluded, and on what basis.

    Everything a caller needs is here; M6 turns this into an FSRS rating without
    reaching back to the deck. `frozen` and `slots` for the same reason
    `Axis1Result` has them — a verdict that can be edited after the fact is not
    a verdict.

    Attributes:
        outcome: The verdict.
        matched_taxon_id: The taxon the answer was accepted for, or `None` when
            it was not accepted. For `ambiguous` this is the card that was
            asked, and `confused_with` names the other card it also fits.
        cascade_level: Which rule granted it — `EXACT`, `TOKEN_SET`,
            `MISSPELLING` or `METAPHONE`. `None` when nothing granted it.
        confused_with: The taxon the answer fits better, or equally well. Set on
            `wrong` when the answer names a different plant in the deck, and on
            `ambiguous` for the colliding card. `None` when the answer fits
            nothing else, which is the ordinary "just wrong" case.
        edit_distance: Edits between the answer and the accepted form, when the
            misspelling rule was involved. `None` for the other rules, which do
            not measure distance.

    Example:
        >>> MatchResult("correct", 47912, EXACT, None, 0).outcome
        'correct'
    """

    outcome: MatchOutcome
    matched_taxon_id: int | None
    cascade_level: int | None
    confused_with: int | None
    edit_distance: int | None


@dataclass(frozen=True, slots=True)
class _Candidate:
    """How well an answer fits one card: which rule fired, and how closely.

    Ordered by `rank`, so "better" is a comparison rather than a chain of
    special cases. Distance only means anything for the misspelling rule; the
    other rules report zero so that ties compare equal.
    """

    level: int
    distance: int

    @property
    def rank(self) -> tuple[int, int]:
        """Sort key — lower is a better match."""
        return (self.level, self.distance)


def _exact_match(answer: str, accepted: frozenset[str]) -> _Candidate | None:
    """Rule 1: the answer is one of the accepted forms, character for character."""
    return _Candidate(EXACT, 0) if answer in accepted else None


def _token_match(answer: str, accepted: frozenset[str]) -> _Candidate | None:
    """Rule 2: the same words in any order, once stopwords are dropped."""
    tokens = token_set(answer)
    if tokens and any(token_set(target) == tokens for target in accepted):
        return _Candidate(TOKEN_SET, 0)
    return None


def _misspelling_match(answer: str, accepted: frozenset[str], cap: int | None) -> _Candidate | None:
    """Rule 3: within a few edits of an accepted form; the closest one wins.

    `cap` lowers the per-target threshold when a match is already in hand and
    only a closer one could change the outcome.
    """
    closest: int | None = None
    length = len(answer)
    for target in accepted:
        threshold = edit_threshold(target)
        if cap is not None:
            threshold = min(threshold, cap)
        # A length gap alone already costs that many edits, so anything further
        # apart than the threshold cannot pass it. Checked before the distance
        # rather than after, because the distance is the expensive part and the
        # overwhelming majority of a 295-card deck is nowhere near any answer.
        if abs(length - len(target)) > threshold:
            continue
        distance = damerau_levenshtein(answer, target)
        if distance <= threshold and (closest is None or distance < closest):
            closest = distance
    return None if closest is None else _Candidate(MISSPELLING, closest)


def _phonetic_match(answer: str, scientific: frozenset[str]) -> _Candidate | None:
    """Rule 4: sounds like a scientific name, word for word.

    Scientific names only. A phonetic rule over common names would decide that
    too many unrelated English plant names sound alike to be safe.
    """
    if any(_sounds_alike(answer, name) for name in scientific):
        return _Candidate(METAPHONE, 0)
    return None


def _best_candidate(
    answer: str,
    accepted: frozenset[str],
    scientific: frozenset[str],
    ceiling: tuple[int, int] | None = None,
) -> _Candidate | None:
    """The strongest rule that accepts `answer` for this set, or `None`.

    Rules are tried in order and the first hit wins, so a later, more forgiving
    rule never overrides an earlier one.

    `ceiling` bounds the search to matches at least as good as a rank already in
    hand. The guard only ever asks "does another card match *this well or
    better*", so a rival that could only match by a weaker rule need not be
    computed at all — and the weaker rules are the expensive ones. The answer
    returned is unchanged whenever it is within the ceiling; only work that
    could not have affected a decision is skipped.
    """
    # Under a misspelling ceiling, only a distance at least as small as the one
    # already granted can matter, so the per-target threshold is capped there.
    cap = ceiling[1] if ceiling is not None and ceiling[0] == MISSPELLING else None
    rules: tuple[tuple[int, Callable[[], _Candidate | None]], ...] = (
        (EXACT, lambda: _exact_match(answer, accepted)),
        (TOKEN_SET, lambda: _token_match(answer, accepted)),
        (MISSPELLING, lambda: _misspelling_match(answer, accepted, cap)),
        (METAPHONE, lambda: _phonetic_match(answer, scientific)),
    )
    for level, rule in rules:
        if ceiling is not None and ceiling < (level, 0):
            return None
        found = rule()
        if found is not None:
            return found
    return None


_Search = Literal["full", "partial", "report"]
"""Why the deck is being searched, which decides who counts as a rival.

`full`    — guarding full credit. Cards asking for the same answer as the target
            are not rivals: four Michigan taxa are genus-rank *Rubus*, so `rubus`
            fully answers all four, and treating them as rivals would leave every
            one of those cards with no correct answer.
`partial` — guarding genus-only credit. Additionally, no card in the target's own
            genus is a rival: naming the genus is exactly what partial credit is
            for, and every *Asclepias* card accepts `asclepias` partially.
`report`  — nothing matched the target and the only question left is which plant
            the learner named. Nothing is excluded, because a sibling genus-rank
            card is the single most useful thing to say here.
"""


def _competitors(
    answer: str,
    target: AcceptedAnswers,
    deck: Deck,
    search: _Search,
    ceiling: tuple[int, int] | None = None,
) -> dict[int, _Candidate]:
    """How well the answer fits every *other* card in the deck.

    Args:
        answer: The normalised answer.
        target: The card that was actually asked.
        deck: Every card in the pack.
        search: Which rivals count; see `_Search`.
        ceiling: Stop looking once a rival could only match worse than this.
            Passed when a match is already in hand, since only rivals that tie
            it or beat it can change the outcome.
    """
    found: dict[int, _Candidate] = {}
    for taxon_id, other in deck.items():
        if taxon_id == target.inat_taxon_id:
            continue
        if search != "report" and target.same_answer_as(other):
            continue
        if search == "partial" and other.genus == target.genus:
            continue
        candidate = _best_candidate(answer, other.full, other.scientific, ceiling)
        if candidate is not None:
            found[taxon_id] = candidate
    return found


def _strongest(competitors: dict[int, _Candidate]) -> tuple[int, _Candidate]:
    """The best-fitting competitor, ties broken by taxon ID so results are stable."""
    return min(competitors.items(), key=lambda item: (item[1].rank, item[0]))


def _blockers(competitors: dict[int, _Candidate], granted: _Candidate) -> dict[int, _Candidate]:
    """Competitors that fit at least as well as the match about to be granted.

    "At least as well" rather than the narrower "strictly better". A rival that
    ties is a rival the answer cannot be told apart from: `ragweed` reduces to
    the same token set as both `common ragweed` and `giant ragweed`, so granting
    it to whichever card came up would resolve a genuine ambiguity by accident.
    See `docs/decisions.md`, 2026-08-09.
    """
    return {
        taxon_id: candidate
        for taxon_id, candidate in competitors.items()
        if candidate.rank <= granted.rank
    }


def score(answer: str, taxon_id: int, deck: Deck) -> MatchResult:
    """Grade one typed answer against one card, in the context of the whole deck.

    Args:
        answer: What the learner typed. Normalised here, so callers pass raw
            input; empty and whitespace-only are ordinary wrong answers, not
            errors.
        taxon_id: The card that was asked.
        deck: Every card in the pack, from `sift_pack.study.answers.build_deck`.
            Required rather than optional: deciding an answer is right for this
            taxon means checking it is not a better answer for another one, and
            that check is not possible from a single card.

    Returns:
        The verdict, the rule that produced it, and the taxon the answer named
        instead when it named one.

    Raises:
        KeyError: If `taxon_id` is not in `deck`. Grading a card that is not in
            the pack is a caller bug, not a wrong answer.

    Example:
        >>> from pathlib import Path
        >>> from sift_pack.manifest import Manifest
        >>> from sift_pack.study.answers import build_deck
        >>> deck = build_deck(
        ...     Manifest.model_validate_json(
        ...         Path("packs/manifest_MI.json").read_text(encoding="utf-8")
        ...     )
        ... )
        >>> score("butterfly milkweed", 47912, deck).outcome
        'correct'
        >>> score("asclepias", 47912, deck).outcome
        'partial'
        >>> wrong = score("swamp milkweed", 47912, deck)
        >>> wrong.outcome, wrong.confused_with
        ('wrong', 125381)
    """
    target = deck[taxon_id]
    normalised = normalize(answer)
    if not normalised:
        return MatchResult("wrong", None, None, None, None)

    full = _best_candidate(normalised, target.full, target.scientific)
    if full is not None:
        return _grade_full(normalised, target, deck, full)

    if target.partial:
        partial = _best_candidate(normalised, target.partial, frozenset())
        if partial is not None:
            return _grade_partial(normalised, target, deck, partial)

    # Nothing fits this card. The only question left is which plant the learner
    # named instead, so nothing is excluded from the search — not even a card
    # asking for the same genus, which is exactly the useful thing to report
    # when somebody types one raspberry's name on another raspberry's card.
    rivals = _competitors(normalised, target, deck, "report")
    if rivals:
        confused, _ = _strongest(rivals)
        return MatchResult("wrong", None, None, confused, None)
    return MatchResult("wrong", None, None, None, None)


def _grade_full(
    answer: str,
    target: AcceptedAnswers,
    deck: Deck,
    granted: _Candidate,
) -> MatchResult:
    """Decide a full-credit match, applying the guard to everything but an exact hit."""
    distance = granted.distance if granted.level == MISSPELLING else None
    rivals = _competitors(answer, target, deck, "full", granted.rank)

    if granted.level == EXACT:
        # An exact match is not a guess, so it is exempt from the guard. But one
        # exact answer fitting two cards is a name two plants share, and picking
        # the card that happened to come up would hide that from the learner.
        collisions = {tid: c for tid, c in rivals.items() if c.level == EXACT}
        if collisions:
            other, _ = _strongest(collisions)
            return MatchResult("ambiguous", target.inat_taxon_id, EXACT, other, 0)
        return MatchResult("correct", target.inat_taxon_id, EXACT, None, 0)

    blocked = _blockers(rivals, granted)
    if blocked:
        confused, _ = _strongest(blocked)
        return MatchResult("wrong", None, granted.level, confused, distance)
    return MatchResult("correct", target.inat_taxon_id, granted.level, None, distance)


def _grade_partial(
    answer: str,
    target: AcceptedAnswers,
    deck: Deck,
    granted: _Candidate,
) -> MatchResult:
    """Decide genus-only credit on a species-rank card.

    Cards in the target's own genus are not rivals here: naming the genus is
    exactly what partial credit is for, and every *Asclepias* card accepts
    `asclepias` partially. A card in a *different* genus that the answer fully
    fits is a different matter — the learner named another plant outright.
    """
    distance = granted.distance if granted.level == MISSPELLING else None
    rivals = _competitors(answer, target, deck, "partial", granted.rank)
    blocked = _blockers(rivals, granted)
    if blocked:
        confused, _ = _strongest(blocked)
        return MatchResult("wrong", None, granted.level, confused, distance)
    return MatchResult("partial", target.inat_taxon_id, granted.level, None, distance)
