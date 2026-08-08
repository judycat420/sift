"""Shared helpers: the real Michigan pack, loaded once, plus deck constructors.

WHY THIS MODULE EXISTS
----------------------
The matcher's whole job is deciding an answer against a *deck*, so testing it
against a hand-written three-card deck would exercise the interesting logic —
the confusion guard — against exactly the confusions somebody thought to invent.
The Michigan pack has 295 real cards with real name collisions in it, so these
tests run against that, and the pack is parsed once here rather than in every
test that needs it.

A constructed deck is still needed for one case: two taxa genuinely sharing a
common name. The Michigan pack contains no such pair (verified by
`test_the_real_deck_has_no_shared_common_name`), so the ambiguity path is
exercised against a fixture that says so out loud.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from sift_pack.manifest import AnswerRank, Manifest, SourceRef, Taxon
from sift_pack.study.answers import Deck, build_deck

REPO_ROOT = Path(__file__).resolve().parent.parent
MI_MANIFEST = REPO_ROOT / "packs" / "manifest_MI.json"

WHEN = datetime(2026, 8, 9, tzinfo=UTC)

MI_PACK = Manifest.model_validate_json(MI_MANIFEST.read_text(encoding="utf-8"))
"""The real Michigan pack. Parsed once; every test reads the same object."""

_DECK: Deck = build_deck(MI_PACK)
_IDS: dict[str, int] = {t.scientific_name: t.inat_taxon_id for t in MI_PACK.taxa}
_NAMES: dict[int, str] = {t.inat_taxon_id: t.scientific_name for t in MI_PACK.taxa}


def deck_of() -> Deck:
    """The real Michigan deck.

    Returns:
        Accepted answers for all 295 cards.

    Example:
        >>> len(deck_of())
        295
    """
    return _DECK


def taxon_id(scientific_name: str) -> int:
    """Look up a real taxon's ID by name, so tests read as botany not integers.

    Args:
        scientific_name: Name exactly as the manifest records it.

    Returns:
        The iNaturalist taxon ID.

    Raises:
        KeyError: If the pack holds no such name — which means the test is
            stale, not that the answer is wrong.

    Example:
        >>> taxon_id("Asclepias tuberosa")
        47912
    """
    return _IDS[scientific_name]


def taxon_named(scientific_name: str) -> Taxon:
    """The real manifest taxon with this name.

    Args:
        scientific_name: Name exactly as the manifest records it.

    Returns:
        The taxon.

    Raises:
        StopIteration: If the pack holds no such name.

    Example:
        >>> taxon_named("Asclepias tuberosa").answer_rank
        'species'
    """
    return next(t for t in MI_PACK.taxa if t.scientific_name == scientific_name)


def name_of(taxon_id_: int | None) -> str | None:
    """Render a taxon ID back to a name, for readable assertion failures.

    Args:
        taxon_id_: The ID, or `None`.

    Returns:
        The scientific name, or `None`.

    Example:
        >>> name_of(47912)
        'Asclepias tuberosa'
        >>> name_of(None) is None
        True
    """
    return None if taxon_id_ is None else _NAMES.get(taxon_id_)


def _taxon(
    taxon_id_: int,
    scientific_name: str,
    genus: str,
    common_names: list[str],
    answer_rank: AnswerRank = "species",
) -> Taxon:
    """One synthetic manifest taxon, for decks the real pack cannot supply."""
    return Taxon(
        inat_taxon_id=taxon_id_,
        scientific_name=scientific_name,
        common_names=common_names,
        rank="species",
        genus=genus,
        family="Testaceae",
        obs_count=100,
        axis1_value="native",
        axis1_sources=[
            SourceRef(name="x", version="1", retrieved_at=WHEN, url="https://x.invalid/")
        ],
        axis1_confidence="high",
        answer_rank=answer_rank,
        image_hashes=[f"{taxon_id_:063x}{n}" for n in range(4)],
    )


def shared_common_name_deck() -> Deck:
    """A constructed deck where two taxa genuinely share one common name.

    The Michigan pack has no such pair, so the ambiguity path has nothing real
    to run against. `mayflower` is a genuine example of the phenomenon — it is
    used in different regions for *Epigaea repens*, *Maianthemum canadense* and
    others — but the Michigan pack records only one common name per taxon and
    they do not collide.

    Returns:
        A two-card deck sharing the common name `mayflower`.

    Example:
        >>> sorted(shared_common_name_deck())
        [900001, 900002]
    """
    manifest = MI_PACK.model_copy(
        update={
            "taxa": [
                _taxon(900001, "Epigaea repens", "Epigaea", ["mayflower"]),
                _taxon(900002, "Maianthemum canadense", "Maianthemum", ["mayflower"]),
            ],
            "images": [],
        }
    )
    return build_deck(manifest)


def taxon_full_name(scientific_name: str) -> str:
    """The first common name a real taxon carries, for readable assertions.

    Args:
        scientific_name: Name exactly as the manifest records it.

    Returns:
        Its first common name.

    Example:
        >>> taxon_full_name("Asclepias tuberosa")
        'butterfly milkweed'
    """
    return taxon_named(scientific_name).common_names[0]


def genus_collision_deck() -> Deck:
    """A constructed deck where one card's genus is another card's whole answer.

    The Michigan pack contains no such collision — verified by
    `test_no_michigan_genus_collides_with_another_cards_answer` — so the branch
    that refuses partial credit because the learner fully named a *different*
    plant has nothing real to run against.

    The collision is not far-fetched: genus names do double as common names
    (`Iris`, `Rosa`, `Magnolia`, `Hosta`), and a pack containing both a *Rosa*
    species and something commonly called "rosa" would hit exactly this.

    Returns:
        A two-card deck: 900011 is a *Rosa* species, and 900012 is a different
        genus whose common name is `rosa`.

    Example:
        >>> sorted(genus_collision_deck())
        [900011, 900012]
    """
    manifest = MI_PACK.model_copy(
        update={
            "taxa": [
                _taxon(900011, "Rosa carolina", "Rosa", ["pasture rose"]),
                _taxon(900012, "Rhodotypos scandens", "Rhodotypos", ["rosa"]),
            ],
            "images": [],
        }
    )
    return build_deck(manifest)
