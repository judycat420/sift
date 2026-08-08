"""What counts as an answer to one card, derived from the question it asked.

WHY THIS MODULE EXISTS
----------------------
`Taxon.answer_rank` decides which question a card puts to the learner, and the
accepted answers are a function of that question rather than of the taxon. A
species-rank card asks "what species is this?", so the species name earns full
credit and the genus alone earns partial. A genus-rank card asks "what genus is
this?", so the genus *is* the answer — full credit, nothing withheld.

The failure this module is shaped to prevent is the tempting one: building a
species-level accepted set for a genus-rank taxon and then discounting the
learner for not reaching it. Seven Michigan taxa are genus-rank precisely
because their species cannot be told apart in a photograph
(`docs/decisions.md`, 2026-08-07). Marking somebody down for not naming what the
photograph cannot show is not strictness, it is asking an unanswerable question
and calling the learner wrong. So the partial set is empty for those taxa, by
construction, and there is no branch in the matcher that can populate it.

INVARIANT PROTECTED
-------------------
`AcceptedAnswers.partial` is non-empty only for species-rank cards, and every
string in both sets is already normalised — so no comparison anywhere downstream
has to remember to normalise, and none can forget.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sift_pack.manifest import AnswerRank, Manifest, Taxon
from sift_pack.study.normalize import normalize

__all__ = ["AcceptedAnswers", "Deck", "accepted_for", "build_deck"]


class AcceptedAnswers(BaseModel):
    """Everything that counts as an answer to one card, and at what credit.

    Carries `answer_rank` alongside the sets so that scoring never re-derives
    which question was asked. A caller holding one of these has enough to grade
    an answer without reaching back to the manifest, which means there is no
    second place where the species/genus decision could be made differently.

    Example:
        >>> answers = AcceptedAnswers(
        ...     inat_taxon_id=47912,
        ...     answer_rank="species",
        ...     genus="asclepias",
        ...     full=frozenset({"asclepias tuberosa", "butterfly milkweed"}),
        ...     partial=frozenset({"asclepias"}),
        ...     scientific=frozenset({"asclepias tuberosa"}),
        ... )
        >>> answers.answer_rank, sorted(answers.partial)
        ('species', ['asclepias'])
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    inat_taxon_id: int = Field(ge=1, description="The taxon this card is about; the primary key.")
    answer_rank: AnswerRank = Field(
        description=(
            "Which question the card asked. Carried so scoring never re-derives it, "
            "and so `partial` can be checked against it."
        )
    )
    genus: str = Field(
        min_length=1,
        description=(
            "Normalised genus. Used to recognise two genus-rank cards as asking for "
            "the same answer — see `same_answer_as`."
        ),
    )
    full: frozenset[str] = Field(
        min_length=1,
        description=(
            "Normalised answers earning full credit. Never empty: a card nobody can "
            "answer correctly is not a card."
        ),
    )
    partial: frozenset[str] = Field(
        default=frozenset(),
        description=(
            "Normalised answers earning partial credit — the genus alone, on a "
            "species-rank card. Empty for genus-rank cards, where the genus is the "
            "whole answer and there is nothing to withhold."
        ),
    )
    scientific: frozenset[str] = Field(
        default=frozenset(),
        description=(
            "The subset of `full` that is a scientific name. Phonetic matching runs "
            "against these only; see `sift_pack.study.matcher`."
        ),
    )

    @model_validator(mode="after")
    def _check_partial_is_species_only(self) -> AcceptedAnswers:
        """Reject a genus-rank card that withholds credit it never asked for.

        This is the module's stated invariant made unrepresentable rather than
        merely intended: a genus-rank `AcceptedAnswers` carrying a partial set
        would mean somebody built a species-level expectation for a taxon whose
        species the photograph cannot show.
        """
        if self.answer_rank == "genus" and self.partial:
            message = (
                f"taxon {self.inat_taxon_id} is a genus-rank card but carries partial "
                f"answers {sorted(self.partial)}. The card asked for the genus, so the "
                "genus is full credit and there is nothing to award partially."
            )
            raise ValueError(message)
        return self

    def same_answer_as(self, other: AcceptedAnswers) -> bool:
        """Whether answering this card correctly also answers `other` correctly.

        True for two genus-rank cards in the same genus. Four Michigan taxa are
        genus-rank *Rubus*, so `rubus` is the whole correct answer to four
        different cards. Without this, an exact match on `rubus` would hit four
        taxa and be reported ambiguous, which would leave those four cards with
        no answer a learner could give — the correct one included.

        Two species-rank cards are never the same answer, even in one genus:
        there the genus is partial credit precisely because it does not settle
        the question.

        Args:
            other: The card to compare against.

        Returns:
            True when both ask for the same genus and neither asks for more.

        Example:
            >>> def card(taxon_id: int, genus: str) -> AcceptedAnswers:
            ...     return AcceptedAnswers(
            ...         inat_taxon_id=taxon_id,
            ...         answer_rank="genus",
            ...         genus=genus,
            ...         full=frozenset({genus}),
            ...     )
            >>> card(1, "rubus").same_answer_as(card(2, "rubus"))
            True
            >>> card(1, "rubus").same_answer_as(card(2, "carex"))
            False
        """
        return (
            self.answer_rank == "genus"
            and other.answer_rank == "genus"
            and self.genus == other.genus
        )


Deck = dict[int, AcceptedAnswers]
"""Every card in one pack, by taxon ID.

The matcher needs the whole deck rather than one card, because deciding whether
an answer is right for *this* taxon means checking it is not a better answer for
some *other* taxon. A per-card API would make that check impossible to write.
"""


_BINOMIAL_WORDS = 2
"""A species name is a genus and an epithet; anything after is authority or rank."""


def _binomial(scientific_name: str) -> str:
    """The genus-and-epithet form, with any authority or infraspecific tail cut.

    Manifest names are already bare binomials today, so this is a guard rather
    than a transformation: if a name ever arrives as `Daucus carota L.` or
    `Daucus carota ssp. sativus`, the first two tokens are the species and the
    rest is not part of what a learner would type.
    """
    tokens = scientific_name.split()
    return " ".join(tokens[:_BINOMIAL_WORDS]) if len(tokens) > _BINOMIAL_WORDS else scientific_name


def accepted_for(taxon: Taxon) -> AcceptedAnswers:
    """Build the accepted answer sets for one card.

    For a species-rank card: the scientific name, its bare binomial, and every
    common name earn full credit; the genus alone earns partial.

    For a genus-rank card: the genus earns full credit, and so do the taxon's
    own common and scientific names — a learner who named the plant more
    precisely than the card asked has not answered it worse. The epithet is
    neither required (the bare genus is fully correct) nor rewarded (naming it
    earns the same `correct`, not more). Nothing is placed in `partial`.

    Args:
        taxon: The manifest taxon the card is about.

    Returns:
        Its accepted answers, every string already normalised.

    Raises:
        ValueError: If the taxon yields no full-credit answer at all, which
            would be an uncheckable card rather than a hard one.

    Example:
        >>> from sift_pack.manifest import Manifest
        >>> from pathlib import Path
        >>> pack = Manifest.model_validate_json(
        ...     Path("packs/manifest_MI.json").read_text(encoding="utf-8")
        ... )
        >>> tuberosa = next(t for t in pack.taxa if t.scientific_name == "Asclepias tuberosa")
        >>> answers = accepted_for(tuberosa)
        >>> sorted(answers.full)
        ['asclepias tuberosa', 'butterfly milkweed']
        >>> sorted(answers.partial)
        ['asclepias']
    """
    genus = normalize(taxon.genus)
    scientific = {normalize(taxon.scientific_name), normalize(_binomial(taxon.scientific_name))}
    commons = {normalize(name) for name in taxon.common_names}

    full = {answer for answer in scientific | commons if answer}
    partial: set[str] = set()
    if taxon.answer_rank == "species":
        partial = {genus} if genus else set()
    else:
        full.add(genus)
        full.discard("")

    if not full:
        message = (
            f"taxon {taxon.inat_taxon_id} ({taxon.scientific_name!r}) yields no accepted "
            "answer after normalisation, so no learner could answer its card correctly"
        )
        raise ValueError(message)

    return AcceptedAnswers(
        inat_taxon_id=taxon.inat_taxon_id,
        answer_rank=taxon.answer_rank,
        genus=genus,
        full=frozenset(full),
        partial=frozenset(partial),
        scientific=frozenset(answer for answer in scientific if answer),
    )


def build_deck(manifest: Manifest) -> Deck:
    """Build the accepted answers for every card in a pack.

    Args:
        manifest: A parsed pack. Referential integrity was already checked when
            it parsed, so nothing here re-validates it.

    Returns:
        Accepted answers by taxon ID, one entry per taxon in the manifest.

    Raises:
        ValueError: If any taxon yields no full-credit answer. All-or-nothing on
            purpose: a deck with one uncheckable card would fail only for the
            learner unlucky enough to be shown it.

    Example:
        >>> from pathlib import Path
        >>> from sift_pack.manifest import Manifest
        >>> pack = Manifest.model_validate_json(
        ...     Path("packs/manifest_MI.json").read_text(encoding="utf-8")
        ... )
        >>> deck = build_deck(pack)
        >>> len(deck)
        295
        >>> deck[47912].answer_rank
        'species'
    """
    return {taxon.inat_taxon_id: accepted_for(taxon) for taxon in manifest.taxa}
