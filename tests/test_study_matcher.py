"""Tests for the match cascade and the confusion guard.

These run against the real 295-card Michigan pack, not a hand-built deck. That
matters: the guard's job is to notice that an answer fits some *other* card, and
a three-card fixture would only ever contain the confusions somebody thought to
invent. Every confusion pair asserted here was found by scanning the actual
manifest, and each is named in the test that uses it.

Tests whose body walks the whole deck carry `@pytest.mark.slow`: between them
they are most of the suite's wall clock (one takes ~131s under `--cov`), and
`make test-fast` leaves them out so the inner loop stays worth running. They are
not optional — `make check` runs them, and they are the only tests that would
notice a future pack introducing a confusion nobody has thought of yet.
"""

from __future__ import annotations

import dataclasses
import itertools

import pytest

from sift_pack.study.matcher import (
    EXACT,
    METAPHONE,
    MISSPELLING,
    TOKEN_SET,
    MatchResult,
    _best_candidate,
    damerau_levenshtein,
    edit_threshold,
    phonetic_codes,
    score,
)
from sift_pack.study.normalize import token_set
from tests.study_fixtures import (
    MI_PACK,
    deck_of,
    genus_collision_deck,
    name_of,
    shared_common_name_deck,
    taxon_full_name,
    taxon_id,
)

DECK_SCAN_TIMEOUT = 300
"""Seconds allowed for a scan that walks the whole deck.

The suite's global limit is 60s (pyproject.toml). These few scans need more, and
not because they are at risk of hanging: the longest measures ~22s on its own and
~131s under `--cov`, because coverage traces every line of the match cascade and
the scan runs it about a million times. 60s would fail the gate on a green build,
which is the one thing a timeout must never do.

300s is still a bound — a genuinely deadlocked scan is reported in five minutes
rather than never — and it applies to three tests, not to the suite.
"""

# --- the real confusion pairs found in the Michigan deck -----------------------
#
# (input, card asked, taxon the input actually names). Each was found by
# scanning packs/manifest_MI.json rather than invented; see the M5 report and
# docs/decisions.md, 2026-08-09.
CONFUSION_PAIRS = [
    # Exact common name of a different Asclepias in the same state.
    ("swamp milkweed", "Asclepias tuberosa", "Asclepias incarnata"),
    ("common milkweed", "Asclepias tuberosa", "Asclepias syriaca"),
    ("butterfly milkweed", "Asclepias syriaca", "Asclepias tuberosa"),
    # "common" and "giant" are both stopwords, so both names reduce to {ragweed}.
    ("giant ragweed", "Ambrosia artemisiifolia", "Ambrosia trifida"),
    ("common ragweed", "Ambrosia trifida", "Ambrosia artemisiifolia"),
    # "greater" and "lesser" are both stopwords — and these are different genera
    # in different families, which makes this the worst collision in the deck.
    ("lesser celandine", "Chelidonium majus", "Ficaria verna"),
    ("greater celandine", "Ficaria verna", "Chelidonium majus"),
    # "northern" and "southern" are both stopwords.
    ("southern blue flag", "Iris versicolor", "Iris virginica"),
    ("northern blue flag", "Iris virginica", "Iris versicolor"),
    # Two edits apart, and the deck holds both.
    ("Acer saccharinum", "Acer saccharum", "Acer saccharinum"),
    ("Acer saccharum", "Acer saccharinum", "Acer saccharum"),
    ("silver maple", "Acer saccharum", "Acer saccharinum"),
    ("sugar maple", "Acer saccharinum", "Acer saccharum"),
    # eastern / western poison ivy: two edits apart.
    ("western poison ivy", "Toxicodendron radicans", "Toxicodendron rydbergii"),
    ("eastern poison ivy", "Toxicodendron rydbergii", "Toxicodendron radicans"),
    # cut-leaved / two-leaved toothwort: three edits apart.
    ("two-leaved toothwort", "Cardamine concatenata", "Cardamine diphylla"),
    ("cut-leaved toothwort", "Cardamine diphylla", "Cardamine concatenata"),
]


# Bare stems shared by two Michigan taxa. Under the nine-word M5 stopword list
# each of these reduced to the same token set as *both* full names, so the guard
# had to refuse a tie. With the list narrowed to words that never discriminate,
# they no longer match anything at all: an incomplete answer is now simply
# incomplete, which is a better thing to tell a learner than "ambiguous".
SHARED_STEMS = [
    ("ragweed", "Ambrosia artemisiifolia", "Ambrosia trifida"),
    ("celandine", "Chelidonium majus", "Ficaria verna"),
    ("blue flag", "Iris versicolor", "Iris virginica"),
]


def result_for(text: str, asked: str) -> MatchResult:
    """Grade `text` as an answer to the card for `asked`, in the real deck."""
    return score(text, taxon_id(asked), deck_of())


# --- cascade level 1: exact ----------------------------------------------------


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("Asclepias tuberosa", "Asclepias tuberosa"),
        ("asclepias tuberosa", "Asclepias tuberosa"),
        ("ASCLEPIAS TUBEROSA", "Asclepias tuberosa"),
        ("butterfly milkweed", "Asclepias tuberosa"),
        ("Butterfly Milkweed", "Asclepias tuberosa"),
        ("  butterfly milkweed  ", "Asclepias tuberosa"),
        ("butterfly milkweed.", "Asclepias tuberosa"),
        ("Jack-in-the-Pulpit", "Arisaema triphyllum"),
        ("jack in the pulpit", "Arisaema triphyllum"),
        ("jack-in-the-pulpit", "Arisaema triphyllum"),
        ("sugar maple", "Acer saccharum"),
        ("Pinus strobus", "Pinus strobus"),
        ("eastern white pine", "Pinus strobus"),
        ("northern blue flag", "Iris versicolor"),
        ("greater celandine", "Chelidonium majus"),
        ("common ragweed", "Ambrosia artemisiifolia"),
        ("giant ragweed", "Ambrosia trifida"),
    ],
)
def test_exact_answers_are_correct_at_level_one(text: str, asked: str) -> None:
    outcome = result_for(text, asked)
    assert outcome.outcome == "correct"
    assert outcome.cascade_level == EXACT
    assert outcome.matched_taxon_id == taxon_id(asked)
    assert outcome.confused_with is None


@pytest.mark.slow
def test_every_accepted_answer_in_the_deck_scores_correct_for_its_own_card() -> None:
    # The deck-wide sanity check: if the guard ever blocks a card's own answer,
    # that card becomes unanswerable and nothing else in this file would notice.
    deck = deck_of()
    failures: list[tuple[str, str, str]] = []
    for taxon in MI_PACK.taxa:
        for answer in sorted(deck[taxon.inat_taxon_id].full):
            outcome = score(answer, taxon.inat_taxon_id, deck)
            if outcome.outcome != "correct":
                failures.append((taxon.scientific_name, answer, outcome.outcome))
    assert not failures, failures


# --- cascade level 2: token set ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("bergamot", "Monarda fistulosa"),  # "wild" dropped
        ("beech", "Fagus grandifolia"),  # "American" dropped
        ("carrot", "Daucus carota"),
        ("sarsaparilla", "Aralia nudicaulis"),
        ("pokeweed", "Phytolacca americana"),
        ("bergamot wild", "Monarda fistulosa"),  # order does not matter
        ("pine white eastern", "Pinus strobus"),  # nothing dropped, only reordered
        ("milkweed swamp", "Asclepias incarnata"),
    ],
)
def test_token_set_matches_are_correct_at_level_two(text: str, asked: str) -> None:
    outcome = result_for(text, asked)
    assert outcome.outcome == "correct"
    assert outcome.cascade_level == TOKEN_SET


def test_the_brief_s_coneflower_example_no_longer_holds_and_that_is_the_trade() -> None:
    # "purple coneflower" answering "eastern purple coneflower" was the worked
    # example for this rule in the M5 brief. Narrowing the stopword list to the
    # two words that never discriminate in the deck gave it up: "eastern"
    # separates real Michigan taxa, so it cannot be dropped for anyone.
    # Recorded as a deliberate loss rather than left to be rediscovered.
    assert token_set("purple coneflower") != token_set("eastern purple coneflower")


@pytest.mark.parametrize(
    ("text", "asked"),
    [("white pine", "Pinus strobus"), ("skunk cabbage", "Symplocarpus foetidus")],
)
def test_omitting_a_discriminating_modifier_is_no_longer_forgiven(text: str, asked: str) -> None:
    # The cost of the narrowing, stated as a test so nobody has to discover it
    # from a bug report. "eastern" used to be droppable; it no longer is.
    assert result_for(text, asked).outcome == "wrong"


def test_a_token_subset_is_not_a_token_match() -> None:
    # {milkweed} is a subset of {swamp, milkweed} but not equal to it; accepting
    # subsets would make every modifier optional.
    outcome = result_for("milkweed", "Asclepias incarnata")
    assert outcome.outcome == "wrong"


# --- cascade level 3: misspelling ----------------------------------------------


@pytest.mark.parametrize(
    ("text", "asked", "distance"),
    [
        ("Asclepias tuberosa", "Asclepias tuberosa", 0),
        ("asclepias tubreosa", "Asclepias tuberosa", 1),
        ("asclepias tuberosaa", "Asclepias tuberosa", 1),
        ("ascleias tuberosa", "Asclepias tuberosa", 1),
        ("arisaema triphylum", "Arisaema triphyllum", 1),
        ("arisema triphyllum", "Arisaema triphyllum", 1),
        ("symplocarpus foetidas", "Symplocarpus foetidus", 1),
        ("thuja occidentales", "Thuja occidentalis", 1),
    ],
)
def test_small_misspellings_are_correct_at_level_three(
    text: str, asked: str, distance: int
) -> None:
    outcome = result_for(text, asked)
    assert outcome.outcome == "correct"
    assert outcome.cascade_level in (EXACT, MISSPELLING)
    if outcome.cascade_level == MISSPELLING:
        assert outcome.edit_distance == distance


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("asclepias tuberculosis", "Asclepias tuberosa"),
        ("completely different words", "Asclepias tuberosa"),
        ("xxxxxxxxxxxxxxxxxx", "Asclepias tuberosa"),
    ],
)
def test_large_misspellings_are_not_forgiven(text: str, asked: str) -> None:
    assert result_for(text, asked).outcome == "wrong"


@pytest.mark.parametrize(
    ("left", "right", "expected"),
    [
        ("", "", 0),
        ("a", "", 1),
        ("", "abc", 3),
        ("abc", "abc", 0),
        ("abc", "abd", 1),
        ("abc", "ab", 1),
        ("ab", "ba", 1),  # transposition costs one, not two
        ("tuberosa", "tubreosa", 1),
        ("asclepias", "asklepias", 1),
        ("ca", "abc", 2),  # unrestricted DL; OSA would say 3
        ("acer saccharum", "acer saccharinum", 2),
    ],
)
def test_damerau_levenshtein(left: str, right: str, expected: int) -> None:
    assert damerau_levenshtein(left, right) == expected
    assert damerau_levenshtein(right, left) == expected


@pytest.mark.parametrize(
    ("target", "expected"),
    [("oak", 1), ("abcdef", 1), ("abcdefghijkl", 2), ("asclepias tuberosa", 3), ("", 1)],
)
def test_edit_threshold_scales_with_target_length(target: str, expected: int) -> None:
    assert edit_threshold(target) == expected


# --- cascade level 4: phonetics ------------------------------------------------


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("asklepias tuberosa", "Asclepias tuberosa"),
        ("thuya occidentalis", "Thuja occidentalis"),
    ],
)
def test_phonetic_spellings_of_scientific_names_are_accepted(text: str, asked: str) -> None:
    outcome = result_for(text, asked)
    assert outcome.outcome == "correct"
    assert outcome.cascade_level in (MISSPELLING, METAPHONE)


@pytest.mark.parametrize(
    ("text", "asked"),
    [("kwerkus alba", "Quercus alba"), ("kwerkus rubra", "Quercus rubra")],
)
def test_a_phonetic_only_spelling_is_accepted_at_level_four(text: str, asked: str) -> None:
    # Beyond the misspelling threshold (three edits against a two-edit budget)
    # but phonetically identical word for word, so only rule 4 can accept it.
    # Both cases are real cards in the Michigan deck.
    outcome = result_for(text, asked)
    assert outcome.outcome == "correct"
    assert outcome.cascade_level == METAPHONE
    assert outcome.edit_distance is None


def test_the_phonetic_rule_still_refuses_a_different_sound() -> None:
    # Rule 4 is the most forgiving rule in the cascade and still not a shrug.
    assert result_for("kikorium intybus", "Cichorium intybus").outcome == "wrong"


def test_a_phonetic_match_on_a_common_name_is_not_available() -> None:
    # Only the `scientific` subset is offered to rule 4, so a common name that
    # merely sounds right cannot be accepted by it.
    assert result_for("shugar maypul", "Acer saccharum").outcome == "wrong"


def test_phonetics_run_on_scientific_names_only() -> None:
    # "sugar maple" and a phonetically identical common name must not match via
    # this rule; only the `scientific` subset is offered to it.
    deck = deck_of()
    assert deck[taxon_id("Acer saccharum")].scientific == frozenset({"acer saccharum"})


def test_phonetic_codes_are_per_word_and_ordered() -> None:
    # Pooling the codes would let a bare genus match a full binomial.
    assert len(phonetic_codes("asclepias tuberosa")) == 2
    assert phonetic_codes("") == ()


def test_a_bare_genus_does_not_phonetically_match_the_binomial() -> None:
    # It should reach `partial` by the genus route, not `correct` by phonetics.
    outcome = result_for("asclepias", "Asclepias tuberosa")
    assert outcome.outcome == "partial"


# --- partial credit -------------------------------------------------------------


@pytest.mark.parametrize(
    ("genus", "asked"),
    [
        ("Asclepias", "Asclepias tuberosa"),
        ("acer", "Acer saccharum"),
        ("IRIS", "Iris versicolor"),
        ("Pinus", "Pinus strobus"),
        ("Arisaema", "Arisaema triphyllum"),
        ("Trillium", "Trillium grandiflorum"),
    ],
)
def test_the_genus_alone_earns_partial_on_a_species_card(genus: str, asked: str) -> None:
    outcome = result_for(genus, asked)
    assert outcome.outcome == "partial"
    assert outcome.matched_taxon_id == taxon_id(asked)
    assert outcome.cascade_level == EXACT


def test_a_misspelled_genus_still_earns_partial() -> None:
    outcome = result_for("asclepais", "Asclepias tuberosa")
    assert outcome.outcome == "partial"
    assert outcome.cascade_level == MISSPELLING


@pytest.mark.slow
def test_partial_is_never_returned_for_a_genus_rank_card() -> None:
    # There the genus is the whole answer, so there is nothing to withhold.
    deck = deck_of()
    for taxon in MI_PACK.taxa:
        if taxon.answer_rank != "genus":
            continue
        for text in (taxon.genus, taxon.scientific_name, *taxon.common_names):
            assert score(text, taxon.inat_taxon_id, deck).outcome != "partial"


# --- the seven genus-rank cards --------------------------------------------------

GENUS_RANK = [
    ("Rubus occidentalis", "Rubus", "black raspberry"),
    ("Rubus parviflorus", "Rubus", "thimbleberry"),
    ("Rubus idaeus", "Rubus", "red raspberry"),
    ("Rubus pubescens", "Rubus", "dwarf raspberry"),
    ("Symphyotrichum novae-angliae", "Symphyotrichum", "New England aster"),
    ("Solidago caesia", "Solidago", "bluestem goldenrod"),
    ("Carex intumescens", "Carex", "bladder sedge"),
]


@pytest.mark.parametrize(("asked", "genus", "_common"), GENUS_RANK)
def test_the_bare_genus_is_full_credit_on_a_genus_card(
    asked: str, genus: str, _common: str
) -> None:
    outcome = result_for(genus, asked)
    assert outcome.outcome == "correct"
    assert outcome.cascade_level == EXACT
    assert outcome.matched_taxon_id == taxon_id(asked)


@pytest.mark.parametrize(("asked", "_genus", "common"), GENUS_RANK)
def test_naming_the_species_is_neither_required_nor_penalised(
    asked: str, _genus: str, common: str
) -> None:
    # The epithet earns the same `correct` as the bare genus — not more, not
    # less. The card never asked for it.
    assert result_for(common, asked).outcome == "correct"
    assert result_for(asked, asked).outcome == "correct"


@pytest.mark.parametrize(("asked", "genus", "_common"), GENUS_RANK)
def test_the_genus_and_the_species_score_identically(asked: str, genus: str, _common: str) -> None:
    assert result_for(genus, asked).outcome == result_for(asked, asked).outcome


def test_four_rubus_cards_share_one_correct_answer_without_being_ambiguous() -> None:
    # `rubus` fully answers four different cards. Reporting that as ambiguous
    # would leave all four with no answer a learner could give.
    for asked, _genus, _common in GENUS_RANK[:4]:
        outcome = result_for("Rubus", asked)
        assert outcome.outcome == "correct", asked
        assert outcome.confused_with is None


def test_another_rubus_species_name_is_still_flagged_on_a_genus_card() -> None:
    # The genus is right, but the learner asserted a species the photograph does
    # not show, and the deck holds that species as its own card.
    outcome = result_for("thimbleberry", "Rubus idaeus")
    assert outcome.outcome == "wrong"
    assert name_of(outcome.confused_with) == "Rubus parviflorus"


# --- the confusion guard ---------------------------------------------------------


def test_the_swamp_milkweed_case() -> None:
    # The worked example from the M5 brief, and the reason the guard exists.
    # "swamp milkweed" is the exact common name of Asclepias incarnata, which
    # grows in the same state. A matcher that accepts it for A. tuberosa has
    # taught the learner the two are one plant.
    outcome = result_for("swamp milkweed", "Asclepias tuberosa")
    assert outcome.outcome == "wrong"
    assert outcome.matched_taxon_id is None
    assert name_of(outcome.confused_with) == "Asclepias incarnata"


@pytest.mark.parametrize(("text", "asked", "names"), CONFUSION_PAIRS)
def test_real_michigan_confusion_pairs_are_refused(text: str, asked: str, names: str) -> None:
    outcome = result_for(text, asked)
    assert outcome.outcome == "wrong", (text, asked, outcome)
    assert outcome.matched_taxon_id is None
    assert name_of(outcome.confused_with) == names


@pytest.mark.parametrize(("text", "asked", "names"), CONFUSION_PAIRS)
def test_the_confused_taxon_accepts_the_answer_it_was_confused_with(
    text: str, asked: str, names: str
) -> None:
    # The other half of each pair: the input really is a good answer, just to a
    # different card. If it were not, the guard would be firing on noise rather
    # than on a genuine confusion.
    assert result_for(text, names).outcome in ("correct", "partial")
    assert result_for(text, asked).outcome == "wrong"


@pytest.mark.parametrize(("stem", "first", "second"), SHARED_STEMS)
def test_a_stem_shared_by_two_taxa_is_refused_for_both(stem: str, first: str, second: str) -> None:
    # Neither card may claim a stem that does not say which plant it is. Since
    # the narrowing this is a plain miss rather than a tie the guard has to
    # break — the modifier that would have identified the plant is now required
    # rather than discarded.
    for asked in (first, second):
        outcome = result_for(stem, asked)
        assert outcome.outcome == "wrong", (stem, asked)


@pytest.mark.parametrize(("stem", "first", "second"), SHARED_STEMS)
def test_the_full_name_answers_its_own_card(stem: str, first: str, second: str) -> None:
    # The stopwords are dropped only for the token-set rule. The full names are
    # still exact answers, and exactness is exempt from the guard — so the tie
    # costs the learner nothing when they type the whole name.
    for card in (first, second):
        full_name = taxon_full_name(card)
        assert stem in full_name, (stem, full_name)
        assert result_for(full_name, card).outcome == "correct"


def test_the_guard_records_which_plant_was_actually_named() -> None:
    outcome = result_for("silver maple", "Acer saccharum")
    assert outcome.outcome == "wrong"
    assert name_of(outcome.confused_with) == "Acer saccharinum"


def test_an_answer_matching_nothing_has_no_confused_with() -> None:
    outcome = result_for("qqqqzzzz wwwwvvvv", "Asclepias tuberosa")
    assert outcome.outcome == "wrong"
    assert outcome.confused_with is None
    assert outcome.matched_taxon_id is None


def test_an_exact_match_is_exempt_from_the_guard() -> None:
    # "common ragweed" exactly names A. artemisiifolia while token-matching
    # A. trifida. Exactness wins; the guard does not fire.
    outcome = result_for("common ragweed", "Ambrosia artemisiifolia")
    assert outcome.outcome == "correct"
    assert outcome.cascade_level == EXACT


# --- ambiguity -------------------------------------------------------------------


@pytest.mark.slow
def test_the_real_deck_has_no_shared_common_name() -> None:
    # Recorded as a fact about the Michigan pack, not an assumption: the
    # ambiguity path below therefore runs against a constructed fixture, and
    # this test is what will notice if a future pack changes that.
    seen: dict[str, list[str]] = {}
    for taxon in MI_PACK.taxa:
        for common in taxon.common_names:
            seen.setdefault(common.lower(), []).append(taxon.scientific_name)
    assert not {k: v for k, v in seen.items() if len(v) > 1}


def test_a_common_name_two_taxa_share_is_ambiguous_not_a_coin_toss() -> None:
    deck = shared_common_name_deck()
    outcome = score("mayflower", 900001, deck)
    assert outcome.outcome == "ambiguous"
    assert outcome.matched_taxon_id == 900001
    assert outcome.confused_with == 900002
    assert outcome.cascade_level == EXACT


def test_ambiguity_is_reported_from_either_side() -> None:
    deck = shared_common_name_deck()
    assert score("mayflower", 900002, deck).confused_with == 900001


# --- the partial-credit guard ------------------------------------------------------


@pytest.mark.slow
def test_no_michigan_genus_collides_with_another_cards_answer() -> None:
    # Recorded as a fact about this pack: no genus name is also some other
    # card's full answer, so the branch below has no real case to run against
    # and is exercised with a constructed deck instead.
    deck = deck_of()
    collisions = [
        (answers.genus, other.inat_taxon_id)
        for answers in deck.values()
        if answers.answer_rank == "species"
        for other in deck.values()
        if other.genus != answers.genus and answers.genus in other.full
    ]
    assert not collisions


def test_partial_credit_is_refused_when_the_answer_fully_names_another_plant() -> None:
    # "rosa" is the genus of card 900011, which would ordinarily earn partial
    # credit — but it is the whole common name of card 900012, a different
    # genus. Naming another plant outright is not partial credit for this one.
    deck = genus_collision_deck()
    outcome = score("rosa", 900011, deck)
    assert outcome.outcome == "wrong"
    assert outcome.matched_taxon_id is None
    assert outcome.confused_with == 900012


def test_partial_credit_survives_a_collision_within_the_same_genus() -> None:
    # The contrast: another *Asclepias* card accepting "asclepias" is not a
    # competitor, because naming the genus is exactly what partial credit is for.
    assert result_for("asclepias", "Asclepias tuberosa").outcome == "partial"
    assert result_for("asclepias", "Asclepias syriaca").outcome == "partial"
    assert result_for("asclepias", "Asclepias incarnata").outcome == "partial"


def test_an_unshared_name_in_the_same_deck_is_not_ambiguous() -> None:
    deck = shared_common_name_deck()
    outcome = score("Epigaea repens", 900001, deck)
    assert outcome.outcome == "correct"
    assert outcome.confused_with is None


# --- edge-case input --------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["", " ", "\t", "\n", "   \t\n  ", ".", "...", "-", "_", '"', "()", "!?"],
)
def test_empty_and_punctuation_only_input_is_wrong_without_a_confusion(text: str) -> None:
    outcome = result_for(text, "Asclepias tuberosa")
    assert outcome.outcome == "wrong"
    assert outcome.matched_taxon_id is None
    assert outcome.confused_with is None
    assert outcome.cascade_level is None
    assert outcome.edit_distance is None


@pytest.mark.parametrize("length", [500, 5_000, 50_000])
def test_very_long_input_is_rejected_without_error(length: int) -> None:
    outcome = result_for("a" * length, "Asclepias tuberosa")
    assert outcome.outcome == "wrong"


def test_repeating_the_right_answer_is_still_the_right_answer() -> None:
    # The token-set rule compares sets, so repetition is invisible to it. That
    # is not a hole: the learner typed this plant's name and no other, and the
    # guard is about naming a *different* plant.
    assert result_for("asclepias tuberosa " * 200, "Asclepias tuberosa").outcome == "correct"


def test_repeating_a_different_plants_name_is_still_refused() -> None:
    # The case that would be a hole, if repetition could smuggle one past.
    outcome = result_for("swamp milkweed " * 200, "Asclepias tuberosa")
    assert outcome.outcome == "wrong"
    assert name_of(outcome.confused_with) == "Asclepias incarnata"


@pytest.mark.parametrize(
    "text",
    [
        "アサガオ",
        "Ærenea",
        "Ασκληπιάς",
        "молочай",
        "🌿🌱",
        "asclépias tuberosa",
        "ÁSCLEPIAS TUBEROSA",
    ],
)
def test_non_ascii_input_is_handled_without_error(text: str) -> None:
    outcome = result_for(text, "Asclepias tuberosa")
    assert outcome.outcome in ("correct", "partial", "wrong", "ambiguous")


@pytest.mark.parametrize(
    ("text", "asked"),
    [("asclépias tuberosa", "Asclepias tuberosa"), ("ÁSCLEPIAS TUBEROSA", "Asclepias tuberosa")],
)
def test_diacritics_do_not_cost_the_learner_credit(text: str, asked: str) -> None:
    assert result_for(text, asked).outcome == "correct"


def test_scoring_an_unknown_taxon_is_a_caller_bug_not_a_wrong_answer() -> None:
    with pytest.raises(KeyError):
        score("anything", 99_999_999, deck_of())


# --- result model -----------------------------------------------------------------


def test_the_result_is_frozen() -> None:
    outcome = result_for("butterfly milkweed", "Asclepias tuberosa")
    with pytest.raises(dataclasses.FrozenInstanceError):
        outcome.outcome = "wrong"  # type: ignore[misc]


def test_the_result_carries_no_smuggled_attributes() -> None:
    outcome = result_for("butterfly milkweed", "Asclepias tuberosa")
    with pytest.raises((AttributeError, TypeError)):
        outcome.note = "not sure"  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("butterfly milkweed", "Asclepias tuberosa"),
        ("asclepias", "Asclepias tuberosa"),
        ("swamp milkweed", "Asclepias tuberosa"),
        ("", "Asclepias tuberosa"),
        ("Rubus", "Rubus idaeus"),
    ],
)
def test_a_matched_result_always_names_the_rule_that_granted_it(text: str, asked: str) -> None:
    outcome = result_for(text, asked)
    if outcome.outcome in ("correct", "partial"):
        assert outcome.cascade_level is not None
        assert outcome.matched_taxon_id == taxon_id(asked)
    else:
        assert outcome.matched_taxon_id is None or outcome.outcome == "ambiguous"


@pytest.mark.slow
@pytest.mark.timeout(DECK_SCAN_TIMEOUT)
def test_edit_distance_is_reported_only_for_the_misspelling_rule() -> None:
    deck = deck_of()
    for taxon in MI_PACK.taxa[:60]:
        for text in ("asclepias tubreosa", taxon.scientific_name, "zzzz"):
            outcome = score(text, taxon.inat_taxon_id, deck)
            if outcome.cascade_level in (TOKEN_SET, METAPHONE):
                assert outcome.edit_distance is None


# --- properties ---------------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.timeout(DECK_SCAN_TIMEOUT)
def test_no_exact_answer_for_one_taxon_is_ever_correct_for_another() -> None:
    """The property the whole guard exists to hold, checked over the real deck.

    For every card B and every answer that exactly identifies B, that answer is
    never scored `correct` or `partial` for a different card A — unless A and B
    ask for the same thing, which is the four genus-rank *Rubus* cards, or the
    answer is genuinely in A's accepted set too.

    Scoring all 295 x 295 x answers combinations directly would take hours, so
    the pairs are filtered first by the same candidate function the matcher
    uses: a card the answer cannot match by any rule cannot be granted it, so
    those pairs hold the property trivially. Nothing about the *decision* is
    reimplemented here — only the impossible pairs are skipped.
    """
    deck = deck_of()
    checked = 0
    violations: list[tuple[str, str, str, str]] = []
    for source in MI_PACK.taxa:
        source_answers = deck[source.inat_taxon_id]
        for answer in sorted(source_answers.full):
            for other in MI_PACK.taxa:
                if other.inat_taxon_id == source.inat_taxon_id:
                    continue
                other_answers = deck[other.inat_taxon_id]
                if source_answers.same_answer_as(other_answers):
                    continue
                if answer in other_answers.full or answer in other_answers.partial:
                    continue  # a genuinely shared answer, not a confusion
                could_match = _best_candidate(
                    answer, other_answers.full, other_answers.scientific
                ) or (
                    other_answers.partial
                    and _best_candidate(answer, other_answers.partial, frozenset())
                )
                if not could_match:
                    continue
                checked += 1
                outcome = score(answer, other.inat_taxon_id, deck)
                if outcome.outcome in ("correct", "partial"):
                    violations.append(
                        (answer, source.scientific_name, other.scientific_name, outcome.outcome)
                    )
    assert not violations, violations[:20]
    # The filter must not have emptied the test: if nothing was scored, the
    # property passed vacuously and this assertion is what would say so.
    assert checked > 0


def test_scoring_is_deterministic() -> None:
    deck = deck_of()
    for text, asked, _ in CONFUSION_PAIRS:
        first = score(text, taxon_id(asked), deck)
        assert all(score(text, taxon_id(asked), deck) == first for _ in range(3))


@pytest.mark.parametrize(
    ("text", "asked"),
    [
        ("swamp milkweed", "Asclepias tuberosa"),
        ("ragweed", "Ambrosia artemisiifolia"),
        ("silver maple", "Acer saccharum"),
        ("butterfly milkweed", "Asclepias tuberosa"),
        ("asclepias", "Asclepias tuberosa"),
        ("zzz", "Asclepias tuberosa"),
    ],
)
def test_the_guard_only_ever_removes_credit(text: str, asked: str) -> None:
    # Scoring against the whole deck can only be as strict as scoring against
    # the card alone. A one-card deck has no rivals, so anything the full deck
    # grants must also be granted alone; the guard subtracts, never adds.
    deck = deck_of()
    target = taxon_id(asked)
    alone = {target: deck[target]}
    granted = {"correct", "partial"}
    assert not (
        score(text, target, deck).outcome in granted
        and score(text, target, alone).outcome not in granted
    )


@pytest.mark.slow
@pytest.mark.timeout(DECK_SCAN_TIMEOUT)
def test_every_pair_of_cards_in_a_genus_is_distinguishable_or_declared_the_same() -> None:
    # Either two cards ask different questions (and each other's answers are
    # refused), or they ask the same one (and share an answer openly). There is
    # no third state where an answer quietly counts for both.
    deck = deck_of()
    by_genus: dict[str, list[int]] = {}
    for answers in deck.values():
        by_genus.setdefault(answers.genus, []).append(answers.inat_taxon_id)
    for members in by_genus.values():
        for a, b in itertools.combinations(members, 2):
            if deck[a].same_answer_as(deck[b]):
                continue
            for answer in sorted(deck[b].full):
                if answer in deck[a].full:
                    continue
                assert score(answer, a, deck).outcome != "correct"
