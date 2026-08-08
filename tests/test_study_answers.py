"""Tests for accepted answer sets, built from the real Michigan pack.

The load-bearing assertions concern the seven genus-rank taxa. They exist
because their species cannot be told apart in a photograph, so a card that
withheld credit for not naming the species would be asking an unanswerable
question and calling the learner wrong for it.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from sift_pack.manifest import Manifest, SourceRef, Taxon
from sift_pack.study.answers import AcceptedAnswers, accepted_for, build_deck
from sift_pack.study.normalize import normalize
from tests.study_fixtures import MI_PACK, WHEN, deck_of, taxon_named

GENUS_RANK_TAXA = {
    "Rubus occidentalis": "rubus",
    "Rubus parviflorus": "rubus",
    "Rubus idaeus": "rubus",
    "Rubus pubescens": "rubus",
    "Symphyotrichum novae-angliae": "symphyotrichum",
    "Solidago caesia": "solidago",
    "Carex intumescens": "carex",
}


# --- the deck as a whole -------------------------------------------------------


def test_the_deck_covers_every_taxon_in_the_real_pack() -> None:
    deck = deck_of()
    assert len(deck) == len(MI_PACK.taxa) == 295


def test_every_card_has_at_least_one_full_credit_answer() -> None:
    # A card nobody can answer correctly is not a hard card, it is a broken one.
    for answers in deck_of().values():
        assert answers.full


def test_every_stored_answer_is_already_normalised() -> None:
    # So that no comparison downstream has to remember to normalise, and none
    # can forget.
    for answers in deck_of().values():
        for stored in answers.full | answers.partial | answers.scientific:
            assert stored == normalize(stored)


def test_the_deck_holds_the_expected_rank_split() -> None:
    ranks = [a.answer_rank for a in deck_of().values()]
    assert ranks.count("genus") == 7
    assert ranks.count("species") == 288


# --- species-rank cards ---------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "expected_full", "expected_partial"),
    [
        ("Asclepias tuberosa", {"asclepias tuberosa", "butterfly milkweed"}, {"asclepias"}),
        ("Acer saccharum", {"acer saccharum", "sugar maple"}, {"acer"}),
        ("Iris versicolor", {"iris versicolor", "northern blue flag"}, {"iris"}),
        (
            "Arisaema triphyllum",
            {"arisaema triphyllum", "jack in the pulpit"},
            {"arisaema"},
        ),
    ],
)
def test_species_rank_full_and_partial_sets(
    name: str, expected_full: set[str], expected_partial: set[str]
) -> None:
    answers = accepted_for(taxon_named(name))
    assert answers.full == frozenset(expected_full)
    assert answers.partial == frozenset(expected_partial)
    assert answers.answer_rank == "species"


def test_a_species_card_puts_the_genus_in_partial_not_full() -> None:
    answers = accepted_for(taxon_named("Asclepias tuberosa"))
    assert "asclepias" in answers.partial
    assert "asclepias" not in answers.full


@pytest.mark.parametrize("name", ["Asclepias tuberosa", "Acer rubrum", "Pinus strobus"])
def test_the_scientific_subset_holds_only_scientific_names(name: str) -> None:
    answers = accepted_for(taxon_named(name))
    assert answers.scientific <= answers.full
    assert all(len(entry.split()) == 2 for entry in answers.scientific)


# --- genus-rank cards -----------------------------------------------------------


@pytest.mark.parametrize(("name", "genus"), sorted(GENUS_RANK_TAXA.items()))
def test_genus_rank_cards_accept_the_bare_genus_at_full_credit(name: str, genus: str) -> None:
    answers = accepted_for(taxon_named(name))
    assert answers.answer_rank == "genus"
    assert genus in answers.full


@pytest.mark.parametrize("name", sorted(GENUS_RANK_TAXA))
def test_genus_rank_cards_withhold_nothing(name: str) -> None:
    # The species epithet is neither required nor rewarded: there is no partial
    # set to fall short of, by construction.
    assert accepted_for(taxon_named(name)).partial == frozenset()


@pytest.mark.parametrize("name", sorted(GENUS_RANK_TAXA))
def test_genus_rank_cards_still_accept_a_more_precise_answer(name: str) -> None:
    # Naming the species is not *worse* than naming the genus. It earns the same
    # `correct`, which is what "neither required nor rewarded" means.
    answers = accepted_for(taxon_named(name))
    taxon = taxon_named(name)
    assert answers.full >= {normalize(entry) for entry in taxon.common_names}
    assert normalize(taxon.scientific_name) in answers.full


def test_a_genus_rank_card_carrying_partial_answers_is_unrepresentable() -> None:
    # The module's invariant made a parse error rather than a convention.
    with pytest.raises(ValidationError, match="genus-rank card but carries partial"):
        AcceptedAnswers(
            inat_taxon_id=1,
            answer_rank="genus",
            genus="rubus",
            full=frozenset({"rubus"}),
            partial=frozenset({"rubus"}),
        )


def test_a_card_with_no_full_answers_is_unrepresentable() -> None:
    with pytest.raises(ValidationError):
        AcceptedAnswers(inat_taxon_id=1, answer_rank="species", genus="rubus", full=frozenset())


# --- same-answer equivalence ----------------------------------------------------


def test_the_four_rubus_cards_are_the_same_answer_as_each_other() -> None:
    # This is why "rubus" is correct rather than ambiguous: four cards, one
    # right answer between them.
    deck = deck_of()
    rubus = [a for a in deck.values() if a.genus == "rubus"]
    assert len(rubus) == 4
    for a in rubus:
        for b in rubus:
            if a is not b:
                assert a.same_answer_as(b)


def test_two_species_cards_in_one_genus_are_not_the_same_answer() -> None:
    # There the genus is partial credit precisely because it does not settle it.
    deck = deck_of()
    tuberosa = deck[taxon_named("Asclepias tuberosa").inat_taxon_id]
    syriaca = deck[taxon_named("Asclepias syriaca").inat_taxon_id]
    assert tuberosa.genus == syriaca.genus
    assert not tuberosa.same_answer_as(syriaca)


def test_genus_cards_in_different_genera_are_not_the_same_answer() -> None:
    deck = deck_of()
    carex = deck[taxon_named("Carex intumescens").inat_taxon_id]
    solidago = deck[taxon_named("Solidago caesia").inat_taxon_id]
    assert not carex.same_answer_as(solidago)


# --- authority and infraspecific handling ----------------------------------------


@pytest.mark.parametrize(
    ("scientific_name", "expected_binomial"),
    [
        ("Daucus carota", "daucus carota"),
        ("Daucus carota L.", "daucus carota"),
        ("Daucus carota ssp. sativus", "daucus carota"),
        ("Monarda fistulosa Sims", "monarda fistulosa"),
    ],
)
def test_the_binomial_is_accepted_whatever_tail_the_name_carries(
    scientific_name: str, expected_binomial: str
) -> None:
    taxon = Taxon(
        inat_taxon_id=1,
        scientific_name=scientific_name,
        common_names=["a common name"],
        rank="species",
        genus="Daucus",
        family="Apiaceae",
        obs_count=1,
        axis1_value="native",
        axis1_sources=[
            SourceRef(name="x", version="1", retrieved_at=WHEN, url="https://x.invalid/")
        ],
        axis1_confidence="high",
        answer_rank="species",
        image_hashes=[f"{n:064x}" for n in range(4)],
    )
    assert expected_binomial in accepted_for(taxon).full


def test_a_taxon_whose_names_normalise_away_is_refused() -> None:
    taxon = Taxon(
        inat_taxon_id=1,
        scientific_name="...",
        common_names=["   "],
        rank="species",
        genus="...",
        family="Apiaceae",
        obs_count=1,
        axis1_value="native",
        axis1_sources=[
            SourceRef(name="x", version="1", retrieved_at=WHEN, url="https://x.invalid/")
        ],
        axis1_confidence="high",
        answer_rank="species",
        image_hashes=[f"{n:064x}" for n in range(4)],
    )
    with pytest.raises(ValueError, match="no accepted answer"):
        accepted_for(taxon)


def test_build_deck_reads_the_real_pack_from_disk() -> None:
    pack = Manifest.model_validate_json(
        (Path("packs") / "manifest_MI.json").read_text(encoding="utf-8")
    )
    assert len(build_deck(pack)) == 295
