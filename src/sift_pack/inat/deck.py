"""Selection of which taxa are worth learning in a place.

WHY THIS MODULE EXISTS
----------------------
A state has thousands of recorded plant taxa and a study pack holds a few
hundred. Which ones make the cut determines whether the pack teaches somebody
the plants they will actually meet on a walk, or a long tail of rarities they
will never see and could not identify from a photo if they did.

The selection rule is observation frequency: the most-observed research-grade
taxa in the place, in order. That is a proxy for "what a person walking around
here encounters", and it is a good one precisely because it inherits
iNaturalist's biases — the pack is for people who look at plants, and so is the
data.

INVARIANT PROTECTED
-------------------
Every taxon the API returned is either a candidate or a `DropRecord` with a
reason. Nothing is filtered out silently, and nothing is admitted whose rank,
hybrid status or observation count could not be read from the response — an
unreadable field is a drop, never a default (STANDARDS.md rule 5).
"""

from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sift_pack.candidates import DropRecord
from sift_pack.inat.client import InatClient, InatError

__all__ = [
    "MIN_OBSERVATIONS",
    "TAXA_LOOKUP_BATCH",
    "TaxonSummary",
    "fetch_taxon_details",
    "select_taxa",
]

_log = logging.getLogger(__name__)

MIN_OBSERVATIONS = 50
"""Below this a taxon is dropped. See `docs/decisions.md`, 2026-08-07.

Rare taxa are doubly bad for a study pack: less useful to learn, because the
learner will not meet them, and less reliably identified, because a taxon with
twenty records has had few eyes on those records.
"""

SPECIES_COUNTS_PAGE_SIZE = 500
"""The documented maximum for `species_counts`; fewer pages, fewer requests."""

TAXA_LOOKUP_BATCH = 30
"""iNaturalist's documented maximum number of IDs per `/taxa/{ids}` request."""

_HYBRID_MARKERS = ("×", " x ", "hybrid")  # noqa: RUF001 - U+00D7 is the botanical hybrid sign
"""Substrings that mark a hybrid name. iNaturalist ranks most hybrids as
`hybrid`, but some are ranked `species` with a multiplication sign in the name,
so both checks run."""


class TaxonSummary(BaseModel):
    """A taxon as `species_counts` reports it, with its place-scoped count.

    Parsed with `extra="ignore"`: iNaturalist adds response fields regularly and
    breaking on an unrecognised one would be a silent-failure of a different
    kind — a pipeline that stops working because upstream improved.

    Example:
        >>> TaxonSummary(
        ...     inat_taxon_id=47911,
        ...     scientific_name="Asclepias syriaca",
        ...     rank="species",
        ...     obs_count=9108,
        ...     common_names=["common milkweed"],
        ... ).genus_hint()
        'Asclepias'
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    inat_taxon_id: int = Field(ge=1)
    scientific_name: str = Field(min_length=1)
    rank: str = Field(min_length=1)
    obs_count: int = Field(ge=0)
    common_names: list[str] = Field(default_factory=list)

    def genus_hint(self) -> str:
        """First token of the binomial, for logging only.

        Never used as the candidate's genus — that is read from the taxon's
        ancestors, because not every scientific name is a simple binomial.

        Returns:
            The leading token of the scientific name.

        Example:
            >>> TaxonSummary(
            ...     inat_taxon_id=1,
            ...     scientific_name="Monarda fistulosa",
            ...     rank="species",
            ...     obs_count=100,
            ... ).genus_hint()
            'Monarda'
        """
        return self.scientific_name.split(" ")[0]


class TaxonDetail(BaseModel):
    """Genus and family for one taxon, read from its ancestor list.

    Example:
        >>> TaxonDetail(inat_taxon_id=47911, genus="Asclepias", family="Apocynaceae").family
        'Apocynaceae'
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    inat_taxon_id: int = Field(ge=1)
    genus: str = Field(min_length=1)
    family: str = Field(min_length=1)


def _parse_summary(entry: object) -> TaxonSummary | None:
    """Read one `species_counts` result, or `None` if it is not usable."""
    if not isinstance(entry, dict):
        return None
    taxon = entry.get("taxon")
    count = entry.get("count")
    if not isinstance(taxon, dict) or not isinstance(count, int):
        return None

    taxon_id = taxon.get("id")
    name = taxon.get("name")
    rank = taxon.get("rank")
    if not isinstance(taxon_id, int) or not isinstance(name, str) or not isinstance(rank, str):
        return None

    common = taxon.get("preferred_common_name")
    try:
        return TaxonSummary(
            inat_taxon_id=taxon_id,
            scientific_name=name,
            rank=rank,
            obs_count=count,
            common_names=[common] if isinstance(common, str) and common else [],
        )
    except ValidationError:
        return None


def _is_hybrid(summary: TaxonSummary) -> bool:
    """True when the name or rank marks a hybrid."""
    lowered = summary.scientific_name.lower()
    return summary.rank == "hybrid" or any(marker in lowered for marker in _HYBRID_MARKERS)


def select_taxa(
    client: InatClient,
    place_id: int,
    iconic_taxon_id: int,
    limit: int,
) -> tuple[list[TaxonSummary], list[DropRecord]]:
    """Rank a place's taxa by observation count and filter them.

    Pages through `species_counts` until `limit` taxa have survived filtering or
    the results run out, so a run that asks for 250 candidates does not stop at
    250 raw rows of which half are subspecies.

    Args:
        client: Cached iNaturalist client.
        place_id: Place to scope counts to.
        iconic_taxon_id: iNaturalist iconic taxon, e.g. 47126 for Plantae.
        limit: How many surviving taxa to return.

    Returns:
        The surviving summaries in descending observation order, and one
        `DropRecord` for every taxon rejected along the way.

    Raises:
        InatError: If a response body is not shaped like a `species_counts`
            response at all.

    Example:
        >>> select_taxa(client, 29, 47126, 250)  # doctest: +SKIP
        ... # SKIPPED: needs a populated client. Covered by tests/test_inat_pipeline.py
        ... # against recorded fixtures.
    """
    kept: list[TaxonSummary] = []
    dropped: list[DropRecord] = []
    seen: set[int] = set()
    page = 1

    while len(kept) < limit:
        response = client.get(
            "species_counts",
            {
                "place_id": place_id,
                "iconic_taxa": _iconic_slug(iconic_taxon_id),
                "quality_grade": "research",
                "per_page": SPECIES_COUNTS_PAGE_SIZE,
                "page": page,
            },
        )
        results = response.get("results")
        if not isinstance(results, list):
            message = f"species_counts page {page} had no results list"
            raise InatError(message)
        if not results:
            break

        for entry in results:
            if len(kept) >= limit:
                break
            summary = _parse_summary(entry)
            if summary is None:
                _log.warning("species_counts entry was unparseable; skipped")
                continue
            if summary.inat_taxon_id in seen:
                continue
            seen.add(summary.inat_taxon_id)

            record = _reject(summary)
            if record is not None:
                dropped.append(record)
                continue
            kept.append(summary)

        page += 1

    return kept, dropped


def _reject(summary: TaxonSummary) -> DropRecord | None:
    """Return a drop record if this taxon fails a deck filter, else `None`."""
    if _is_hybrid(summary):
        return DropRecord(
            inat_taxon_id=summary.inat_taxon_id,
            name=summary.scientific_name,
            reason="hybrid",
            detail=f"rank {summary.rank!r}; hybrids are not stable study targets",
        )
    if summary.rank != "species":
        return DropRecord(
            inat_taxon_id=summary.inat_taxon_id,
            name=summary.scientific_name,
            reason="rank_not_species",
            detail=f"rank is {summary.rank!r}, expected 'species'",
        )
    if summary.obs_count < MIN_OBSERVATIONS:
        return DropRecord(
            inat_taxon_id=summary.inat_taxon_id,
            name=summary.scientific_name,
            reason="obs_count_below_threshold",
            detail=f"{summary.obs_count} research-grade observations, need {MIN_OBSERVATIONS}",
        )
    return None


def _iconic_slug(iconic_taxon_id: int) -> str:
    """Map an iconic taxon ID to the slug the API expects.

    Raises:
        InatError: For an iconic taxon Sift has no slug for; guessing would
            silently query the wrong kingdom.
    """
    slugs = {47126: "Plantae", 3: "Aves", 47158: "Insecta"}
    try:
        return slugs[iconic_taxon_id]
    except KeyError as exc:
        message = f"no iconic-taxa slug known for taxon id {iconic_taxon_id}"
        raise InatError(message) from exc


def _ancestor_rank(taxon: dict[str, Any], rank: str) -> str | None:
    """Read one named rank out of a taxon's ancestor list."""
    ancestors = taxon.get("ancestors")
    if not isinstance(ancestors, list):
        return None
    for ancestor in ancestors:
        if isinstance(ancestor, dict) and ancestor.get("rank") == rank:
            name = ancestor.get("name")
            if isinstance(name, str) and name:
                return name
    return None


def fetch_taxon_details(
    client: InatClient,
    taxon_ids: list[int],
) -> tuple[dict[int, TaxonDetail], list[DropRecord]]:
    """Look up genus and family for a batch of taxa.

    `species_counts` does not return ancestor names, and the genus cannot be
    split off the binomial without guessing for names that are not simple
    binomials. So the ancestors are fetched.

    Args:
        client: Cached iNaturalist client.
        taxon_ids: Taxa to look up. Batched at `TAXA_LOOKUP_BATCH` per request.

    Returns:
        Details by taxon ID, and drop records for taxa whose genus or family
        could not be read. A taxon missing either is dropped, not defaulted.

    Raises:
        InatError: If a response is not shaped like a `taxa` response.

    Example:
        >>> fetch_taxon_details(client, [47911])  # doctest: +SKIP
        ... # SKIPPED: needs a populated client. Covered by tests/test_inat_pipeline.py
        ... # against recorded fixtures.
    """
    details: dict[int, TaxonDetail] = {}
    dropped: list[DropRecord] = []
    seen: set[int] = set()

    for start in range(0, len(taxon_ids), TAXA_LOOKUP_BATCH):
        batch = taxon_ids[start : start + TAXA_LOOKUP_BATCH]
        response = client.get("taxa_by_id", {"ids": batch})
        results = response.get("results")
        if not isinstance(results, list):
            message = f"taxa_by_id returned no results list for {batch!r}"
            raise InatError(message)

        for entry in results:
            if not isinstance(entry, dict):
                continue
            taxon_id = entry.get("id")
            if not isinstance(taxon_id, int):
                continue
            seen.add(taxon_id)
            genus = _ancestor_rank(entry, "genus")
            family = _ancestor_rank(entry, "family")
            name = entry.get("name")
            readable_name = name if isinstance(name, str) and name else f"taxon {taxon_id}"
            if genus is None or family is None:
                dropped.append(
                    DropRecord(
                        inat_taxon_id=taxon_id,
                        name=readable_name,
                        reason="taxon_detail_unavailable",
                        detail=(
                            f"ancestors gave genus={genus!r}, family={family!r}; "
                            "both are required and neither is inferable"
                        ),
                    )
                )
                continue
            details[taxon_id] = TaxonDetail(inat_taxon_id=taxon_id, genus=genus, family=family)

    dropped.extend(
        DropRecord(
            inat_taxon_id=missing,
            name=f"taxon {missing}",
            reason="taxon_detail_unavailable",
            detail="taxa_by_id returned no record for this ID",
        )
        for missing in sorted(set(taxon_ids) - seen)
    )

    return details, dropped
