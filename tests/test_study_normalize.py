"""Tests for the normalisation cascade.

The interesting assertions here are the negative ones. Normalisation that does
too much is indistinguishable from normalisation that does enough, right up
until it collapses two plant names onto one string.
"""

from __future__ import annotations

import json
import unicodedata
from itertools import combinations
from pathlib import Path

import pytest

from sift_pack.study.normalize import STOPWORDS, normalize, token_set
from tests.study_fixtures import deck_of

REPO_ROOT = Path(__file__).resolve().parent.parent


# --- the cascade, step by step -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Asclepias", "asclepias"),
        ("ASCLEPIAS", "asclepias"),
        ("AsClEpIaS", "asclepias"),
        ("Butterfly-Weed", "butterfly weed"),
        ("butterfly_weed", "butterfly weed"),
        ("Jack-in-the-Pulpit", "jack in the pulpit"),
        ("jack—in—the—pulpit", "jack—in—the—pulpit"),  # em dash is not a hyphen
        ("  leading and trailing  ", "leading and trailing"),
        ("collapse    inner     runs", "collapse inner runs"),
        ("tabs\tand\nnewlines", "tabs and newlines"),
        ("trailing period.", "trailing period"),
        ("...surrounded...", "surrounded"),
        ('"quoted"', "quoted"),
        ("(parenthesised)", "parenthesised"),
        ("trailing comma,", "trailing comma"),
        ("exclaim!", "exclaim"),
        ("", ""),
        ("   ", ""),
        ("...", ""),
    ],
)
def test_normalisation_cascade(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Ærenea", "ærenea"),  # Æ has no decomposition; left alone rather than guessed at
        ("café", "cafe"),
        ("CAFÉ", "cafe"),
        ("naïve", "naive"),
        ("Ranunculus \u00d7 hybridus", "ranunculus \u00d7 hybridus"),  # U+00D7 hybrid sign
        ("Ǎsclepias", "asclepias"),
        ("Åkerö", "akero"),
    ],
)
def test_diacritics_are_stripped_without_dropping_letters(raw: str, expected: str) -> None:
    assert normalize(raw) == expected


# --- what normalisation deliberately does NOT do -------------------------------


def test_internal_punctuation_survives() -> None:
    # "St. John's wort" keeping its apostrophe is the stated requirement: an
    # apostrophe inside a word is part of the name, not typing debris.
    assert normalize("St. John's wort") == "st. john's wort"


def test_an_internal_period_is_not_stripped() -> None:
    assert normalize("var. sativus") == "var. sativus"


@pytest.mark.parametrize(
    ("singular", "plural"),
    [("raspberry", "raspberries"), ("rubus", "rubi"), ("sedge", "sedges"), ("iris", "irises")],
)
def test_nothing_is_stemmed(singular: str, plural: str) -> None:
    # A stemmer would collapse these, and a stemmer has no idea which of them
    # is a genus. Distinct strings must stay distinct.
    assert normalize(singular) != normalize(plural)


def test_case_folding_does_not_merge_distinct_names() -> None:
    assert normalize("Rubus") != normalize("Ruby")


# --- idempotence (property) ----------------------------------------------------


def _corpus() -> list[str]:
    """Every name in the real pack, plus adversarial strings."""
    payload = json.loads((REPO_ROOT / "packs" / "manifest_MI.json").read_text(encoding="utf-8"))
    names: list[str] = []
    for taxon in payload["taxa"]:
        names.append(taxon["scientific_name"])
        names.append(taxon["genus"])
        names.extend(taxon["common_names"])
    names.extend(
        [
            "",
            " ",
            "   \t\n ",
            "...",
            "--__--",
            "St. John's wort",
            "Jack-in-the-Pulpit",
            "café-au-lait",
            "アサガオ",
            "Ærenea",
            "a" * 5000,
            "MiXeD-CaSe_Name.",
            '"""',
            "((()))",
            "Ranunculus \u00d7 hybridus",
        ]
    )
    return names


# The three corpus properties below expand to roughly 3,000 test items — the
# bulk of the suite's collection, for a fixed set of properties over every name
# in the shipped pack. Marked `slow` so `make test-fast` can leave them out;
# `make check` runs them, and they are what would catch a normalisation change
# that only misbehaves on one real name.
@pytest.mark.slow
@pytest.mark.parametrize("text", _corpus())
def test_normalisation_is_idempotent(text: str) -> None:
    once = normalize(text)
    assert normalize(once) == once


@pytest.mark.slow
@pytest.mark.parametrize("text", _corpus())
def test_normalised_text_has_no_edge_whitespace_or_double_spaces(text: str) -> None:
    once = normalize(text)
    assert once == once.strip()
    assert "  " not in once


@pytest.mark.slow
@pytest.mark.parametrize("text", _corpus())
def test_normalisation_never_introduces_combining_marks(text: str) -> None:
    assert not any(unicodedata.combining(c) for c in normalize(text))


# --- token sets ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Dropped: the two words that never discriminate in the deck.
        ("wild bergamot", {"bergamot"}),
        ("American beech", ["american", "beech"][1:]),
        ("wild carrot", {"carrot"}),
        ("American pokeweed", {"pokeweed"}),
        # Kept: everything else, including the modifiers M5 used to drop.
        ("eastern purple coneflower", {"eastern", "purple", "coneflower"}),
        ("purple coneflower", {"purple", "coneflower"}),
        ("common milkweed", {"common", "milkweed"}),
        ("giant ragweed", {"giant", "ragweed"}),
        ("northern blue flag", {"northern", "blue", "flag"}),
        ("southern blue flag", {"southern", "blue", "flag"}),
        ("swamp milkweed", {"swamp", "milkweed"}),
        ("", set()),
    ],
)
def test_token_set_drops_only_the_validated_stopwords(
    text: str, expected: set[str] | list[str]
) -> None:
    assert token_set(normalize(text)) == frozenset(expected)


def test_token_order_does_not_matter() -> None:
    assert token_set("northern blue flag") == token_set("flag blue northern")


@pytest.mark.parametrize("word", sorted(STOPWORDS))
def test_a_name_made_only_of_stopwords_keeps_them(word: str) -> None:
    # Reducing such a name to the empty set would make it match everything,
    # which is the opposite of what stopword removal is for.
    assert token_set(word) == frozenset({word})


def test_the_stopword_list_is_the_validated_one() -> None:
    # Narrowed from nine words after the M5 confusion scan; see
    # docs/decisions.md, 2026-08-09. Changing this set means re-running
    # test_no_two_cards_collapse_onto_one_token_set, not just editing here.
    assert frozenset({"wild", "american"}) == STOPWORDS


@pytest.mark.parametrize(
    "word", ["common", "eastern", "western", "giant", "lesser", "greater", "northern", "southern"]
)
def test_the_discriminating_modifiers_are_not_dropped(word: str) -> None:
    # Each of these separates two real Michigan taxa from each other. A learner
    # who omits one has not named the plant, and the token-set rule must not
    # pretend otherwise.
    assert word not in STOPWORDS
    assert token_set(f"{word} something") == frozenset({word, "something"})


# --- the stopword list is validated against the real deck, not assumed ---------


def _full_answers_by_taxon() -> dict[int, frozenset[str]]:
    """Every full-credit answer in the shipped Michigan pack, by taxon."""
    return {taxon_id_: answers.full for taxon_id_, answers in deck_of().items()}


def test_no_two_cards_collapse_onto_one_token_set() -> None:
    """No two taxa become indistinguishable once stopwords are removed.

    This is the guard the stopword list is only safe behind. A word that
    discriminates between two plants is worse than no stopword list at all: it
    silently turns a correct answer for one taxon into a false accept for the
    other, at the token-set rule, in the direction the learner is least able to
    detect.

    Run over the real pack rather than a fixture, so a future deck that would
    collide fails the build rather than shipping. Cards that ask for the same
    answer by design — the four genus-rank *Rubus* — are excluded, since sharing
    an answer is the point of them.
    """
    deck = deck_of()
    answers = _full_answers_by_taxon()
    collisions: list[tuple[str, str, int, int]] = []
    for left, right in combinations(sorted(answers), 2):
        if deck[left].same_answer_as(deck[right]):
            continue
        collisions.extend(
            (a, b, left, right)
            for a in sorted(answers[left])
            for b in sorted(answers[right])
            if a != b and token_set(a) == token_set(b)
        )
    assert not collisions, (
        "these answers become identical once stopwords are dropped, so each would "
        f"be a false accept for the other taxon: {collisions}"
    )


@pytest.mark.parametrize(
    ("first", "second"),
    [
        ("common ragweed", "giant ragweed"),
        ("greater celandine", "lesser celandine"),
        ("northern blue flag", "southern blue flag"),
        ("eastern poison ivy", "western poison ivy"),
    ],
)
def test_the_pairs_that_forced_the_narrowing_are_now_distinguished(first: str, second: str) -> None:
    # These are real Michigan pairs that the nine-word list merged. Each is a
    # named regression test: if a future edit re-adds both halves of one of
    # these contrasts, this fails before the deck-wide check has to.
    assert token_set(first) != token_set(second)


def test_no_single_candidate_word_would_collide_on_its_own() -> None:
    # Recorded because it is the counter-intuitive part, and the reason a word
    # cannot be cleared by inspecting it alone: every one of the words removed
    # in the narrowing is harmless by itself. The collisions need *both* halves
    # of a contrasting pair, which only a whole-list check can catch.
    deck = deck_of()
    answers = _full_answers_by_taxon()
    for word in ("common", "eastern", "giant", "lesser", "greater", "northern", "southern"):
        merged = [
            (a, b)
            for left, right in combinations(sorted(answers), 2)
            if not deck[left].same_answer_as(deck[right])
            for a in answers[left]
            for b in answers[right]
            if a != b and _drop(a, {word}) == _drop(b, {word})
        ]
        assert not merged, (word, merged)


def _drop(text: str, stop: set[str]) -> frozenset[str]:
    """Token set under an arbitrary stopword list, for validating candidates."""
    kept = frozenset(token for token in text.split() if token not in stop)
    return kept if kept else frozenset(text.split())
