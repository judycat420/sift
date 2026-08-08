"""The candidate pool schema: what iNaturalist alone can establish, and nothing more.

WHY THIS MODULE EXISTS
----------------------
M1 established that a `Manifest` cannot be built from iNaturalist data alone.
`Taxon.axis1_source` has no default, and iNaturalist does not know whether a
plant is native to Michigan — that claim comes from USDA PLANTS in M3. The
tempting fix was to relax `Taxon` so it could be constructed half-finished and
completed later. That would have made the unattributed state representable
everywhere, forever, to solve a problem that exists in one stage of one
pipeline.

Instead the pipeline is split, and this module is the first half. A
`CandidatePool` is everything the iNaturalist fetch can honestly assert: which
taxa are common in a place, how well the community agrees on them, and which
licence-cleared photos exist. M3 promotes candidates to manifest taxa by adding
the one thing missing, and only taxa that survive promotion become cards.

INVARIANT PROTECTED
-------------------
`CandidateTaxon` has no field capable of holding a nativity claim — not a
nullable one, not an empty-string one, not one under another name. The absence
is the point: a candidate cannot express a claim it has no source for, so
promotion is the only path by which a nativity value can come to exist, and
promotion requires a source by construction.

This extends to iNaturalist's own `establishment_means` field, which reports
native/introduced status per place and is deliberately NOT carried here. It is
curator-maintained, sparsely populated, and disagrees with USDA; copying it in
would put an unsourced nativity claim into the pool through the back door and
make M3's promotion step look redundant. See `docs/decisions.md`, 2026-08-07.

WHY THIS IS NOT IN `manifest.py`
--------------------------------
`manifest.py` is the contract with the runtime half: changing it is a breaking
change that bumps `pack_version`. A candidate pool never leaves the build half
— it is an intermediate artefact on disk under `work/`, read only by the next
stage of this same package. Keeping the two in one file would mean every change
to the fetch pipeline's intermediate shape looked like a change to the runtime
contract, and the discipline around `pack_version` would erode from false
alarms. Separate files, separate stability guarantees.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sift_pack.manifest import License, SourceRef

__all__ = [
    "MAX_PHOTOS_PER_CANDIDATE",
    "MAX_PHOTOS_PER_OBSERVER",
    "MIN_PHOTOS_PER_CANDIDATE",
    "CandidatePhoto",
    "CandidatePool",
    "CandidateTaxon",
    "DropReason",
    "DropRecord",
]

MIN_PHOTOS_PER_CANDIDATE = 4
"""Below this a taxon is dropped rather than padded. See `docs/decisions.md`."""

MAX_PHOTOS_PER_CANDIDATE = 8
"""Above this we stop collecting; more photos cost bytes without teaching more."""

MAX_PHOTOS_PER_OBSERVER = 2
"""No observer may supply more than this many of one taxon's photos.

One person's photos share a camera, a habitat, a season and an eye. Four photos
from one observer teach that observer's way of seeing a plant, which is not the
same as teaching the plant."""

DropReason = Literal[
    "rank_not_species",
    "hybrid",
    "obs_count_below_threshold",
    "taxon_detail_unavailable",
    "insufficient_licensed_photos",
]
"""Every reason a taxon can leave the pipeline.

A closed set rather than free-form strings: a typo in a reason code would
silently create a new category, splitting one real problem across two lines of
the stats report and making it look smaller than it is.
"""


class _Frozen(BaseModel):
    """Base config for candidate-pool models: frozen, and no unrecognised keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class DropRecord(_Frozen):
    """One taxon that did not make it into the pool, and why.

    Example:
        >>> DropRecord(
        ...     inat_taxon_id=1234,
        ...     name="Carex sp.",
        ...     reason="rank_not_species",
        ...     detail="rank is 'genus'",
        ... ).reason
        'rank_not_species'
    """

    inat_taxon_id: int = Field(ge=1, description="The taxon that was dropped.")
    name: str = Field(min_length=1, description="Scientific name, for a readable report.")
    reason: DropReason = Field(description="Closed-vocabulary reason code.")
    detail: str = Field(min_length=1, description="What specifically was wrong, with values.")


class CandidatePhoto(_Frozen):
    """A licence-cleared photo, described by what the iNaturalist API actually returns.

    NOTE ON `sha256` AND `bytes`
    ----------------------------
    `manifest.Image` carries both; this type carries neither, because the API
    returns neither. They can only be computed by fetching the image bytes from
    the open-data S3 bucket, which is a later phase (`docs/decisions.md`,
    2026-08-05). Inventing a digest to satisfy a required field is the same
    class of error as inventing a nativity label, so the field is absent until
    the bytes exist. See `docs/decisions.md`, 2026-08-07.

    Example:
        >>> photo = CandidatePhoto(
        ...     inat_photo_id=712294871,
        ...     taxon_id=48662,
        ...     observation_id=388886183,
        ...     observation_url="https://www.inaturalist.org/observations/388886183",
        ...     license="cc0",
        ...     photographer_login="erinsuzanne",
        ...     photographer_name=None,
        ...     width=1536,
        ...     height=2048,
        ...     identification_agreements=1,
        ...     month_bucket="B",
        ...     source_url="https://inaturalist-open-data.s3.amazonaws.com/photos/1/square.jpg",
        ... )
        >>> photo.license
        'cc0'
    """

    inat_photo_id: int = Field(ge=1, description="iNaturalist photo ID.")
    taxon_id: int = Field(ge=1, description="Taxon this photo depicts.")
    observation_id: int = Field(
        ge=1,
        description=(
            "Observation the photo belongs to. Recorded so the one-photo-per-"
            "observation rule is auditable after the fact, not just at selection."
        ),
    )
    observation_url: str = Field(min_length=1, description="Source observation, for attribution.")
    license: License = Field(
        description="Photo licence, verbatim from the API. NC cannot be represented."
    )
    photographer_login: str = Field(
        min_length=1, description="iNaturalist login; CC-BY attribution depends on it."
    )
    photographer_name: str | None = Field(
        description="Display name when set. Left absent when the API returns null."
    )
    width: int = Field(ge=1, description="Original pixel width.")
    height: int = Field(ge=1, description="Original pixel height.")
    identification_agreements: int = Field(
        ge=0,
        description=(
            "Agreeing IDs on the source observation at fetch time, recorded "
            "verbatim. NOT a confidence measure: it counts identifications "
            "agreeing with the observer's own, and research grade needs only two "
            "identifications in total, so an ordinary record reports 1 whatever "
            "the taxon. Kept as an observed datum and used only as a weak "
            "within-bucket tiebreaker; no taxon-level statistic is derived from "
            "it. See docs/decisions.md, 2026-08-07."
        ),
    )
    month_bucket: str = Field(
        min_length=1,
        description=(
            "Which seasonal bucket the source observation was sampled from. "
            "Recorded so that seasonal spread is auditable in the finished pool "
            "rather than merely intended at selection time."
        ),
    )
    source_url: str = Field(
        min_length=1,
        description=(
            "Where the image bytes live, verbatim from the API. Carried on the "
            "candidate so the resolve stage reads one artefact rather than "
            "reaching back into the response cache, and so a pool is a complete "
            "description of what it would take to build the pack. Never rebuilt "
            "from the photo id: extensions vary and a templated URL would 404 "
            "on an unknown fraction."
        ),
    )


class CandidateTaxon(_Frozen):
    """A taxon iNaturalist can vouch for, awaiting the claim that would make it a card.

    Deliberately has no axis-1 field under any name. See the module docstring.

    Example:
        >>> taxon = CandidateTaxon(
        ...     inat_taxon_id=47911,
        ...     scientific_name="Asclepias syriaca",
        ...     common_names=["common milkweed"],
        ...     rank="species",
        ...     genus="Asclepias",
        ...     family="Apocynaceae",
        ...     obs_count=9108,
        ...     images=[],
        ... )
        Traceback (most recent call last):
            ...
        pydantic_core._pydantic_core.ValidationError: ...
    """

    inat_taxon_id: int = Field(ge=1, description="PRIMARY KEY; names are mutable attributes.")
    scientific_name: str = Field(min_length=1, description="Current name.")
    common_names: list[str] = Field(
        description="Vernacular names as iNaturalist reports them. May be empty; never invented."
    )
    rank: str = Field(min_length=1, description="Taxonomic rank. Always 'species' after filtering.")
    genus: str = Field(
        min_length=1,
        description=(
            "Genus name, read from the taxon's ancestors — never split off the "
            "binomial, which would guess for names that are not simple binomials."
        ),
    )
    family: str = Field(min_length=1, description="Family name, read from the taxon's ancestors.")
    obs_count: int = Field(ge=0, description="Research-grade observations in this place.")
    months_represented: int = Field(
        ge=1,
        le=4,
        description=(
            "How many of the four seasonal buckets the selected photos span. A "
            "taxon shown only in flower is a taxon nobody can identify in August. "
            "A learner-facing quality signal (M7): a card built from one season "
            "is teaching less than its photo count suggests."
        ),
    )
    distinct_observers: int = Field(
        ge=1,
        description=(
            "How many different people took the selected photos. Guards against "
            "learning one person's camera and habitat rather than the plant. A "
            "learner-facing quality signal (M7)."
        ),
    )
    images: list[CandidatePhoto] = Field(
        min_length=MIN_PHOTOS_PER_CANDIDATE,
        max_length=MAX_PHOTOS_PER_CANDIDATE,
        description="Licence-cleared photos, each from a distinct observation.",
    )

    @model_validator(mode="after")
    def _check_photos_are_from_distinct_observations(self) -> CandidateTaxon:
        """Reject a candidate whose photos repeat an observation.

        Eight photos of one plant from one angle teach less than four of four
        plants, so the selection rule is one photo per observation. Enforced
        here as well as at selection: a pool that parses has already obeyed it.
        """
        observation_ids = [photo.observation_id for photo in self.images]
        if len(observation_ids) != len(set(observation_ids)):
            message = (
                f"taxon {self.inat_taxon_id} has multiple photos from one observation: "
                f"{sorted({o for o in observation_ids if observation_ids.count(o) > 1})}"
            )
            raise ValueError(message)
        if any(photo.taxon_id != self.inat_taxon_id for photo in self.images):
            message = f"taxon {self.inat_taxon_id} carries photos belonging to another taxon"
            raise ValueError(message)
        return self

    @model_validator(mode="after")
    def _check_quality_signals_match_the_photos(self) -> CandidateTaxon:
        """Reject a candidate whose recorded signals disagree with its photos.

        The three signals are learner-facing claims about the pack's quality, so
        they are recomputed here rather than trusted. A stated four-season spread
        that the photos do not support would be exactly the kind of confident,
        unverifiable claim this project exists to prevent — and unlike a nativity
        label, this one can be checked against the record carrying it.
        """
        errors: list[str] = []

        observers = Counter(photo.photographer_login for photo in self.images)
        over_cap = sorted(
            login for login, count in observers.items() if count > MAX_PHOTOS_PER_OBSERVER
        )
        if over_cap:
            errors.append(f"observers over the {MAX_PHOTOS_PER_OBSERVER}-photo cap: {over_cap}")
        if self.distinct_observers != len(observers):
            errors.append(
                f"distinct_observers is {self.distinct_observers}, photos show {len(observers)}"
            )

        buckets = {photo.month_bucket for photo in self.images}
        if self.months_represented != len(buckets):
            errors.append(
                f"months_represented is {self.months_represented}, photos span {len(buckets)}"
            )

        if errors:
            message = f"taxon {self.inat_taxon_id} signals disagree with its photos: " + "; ".join(
                errors
            )
            raise ValueError(message)
        return self


class CandidatePool(_Frozen):
    """The output of a fetch: candidates kept, and every taxon dropped, with reasons.

    Args:
        Field values as declared below.

    Returns:
        Not applicable; this is a model.

    Raises:
        pydantic.ValidationError: If a taxon appears in both `candidates` and
            `dropped`, or if IDs repeat within either.

    Example:
        >>> from datetime import UTC, datetime
        >>> from sift_pack.manifest import SourceRef
        >>> pool = CandidatePool(
        ...     domain="plants",
        ...     state="MI",
        ...     place_id=29,
        ...     fetched_at=datetime(2026, 8, 7, tzinfo=UTC),
        ...     sources=[
        ...         SourceRef(
        ...             name="iNaturalist API",
        ...             version="v1",
        ...             retrieved_at=datetime(2026, 8, 7, tzinfo=UTC),
        ...             url="https://api.inaturalist.org/v1/",
        ...         )
        ...     ],
        ...     candidates=[],
        ...     dropped=[],
        ... )
        >>> pool.place_id
        29
    """

    domain: str = Field(min_length=1, description="Domain slug the pool was fetched for.")
    state: str = Field(min_length=1, description="Region code, e.g. 'MI'.")
    place_id: int = Field(ge=1, description="iNaturalist place ID the counts are scoped to.")
    fetched_at: datetime = Field(description="When the fetch ran. Timezone-aware.")
    sources: list[SourceRef] = Field(min_length=1, description="Datasets that contributed.")
    candidates: list[CandidateTaxon] = Field(description="Taxa that survived every filter.")
    dropped: list[DropRecord] = Field(
        description=(
            "Every taxon considered and rejected, with a reason. Never elided: a "
            "pool that kept 40 of 300 must show the other 260 (STANDARDS.md rule 5)."
        )
    )
    bucket_observations: dict[str, int] = Field(
        default_factory=dict,
        description=(
            "Observations each seasonal bucket returned across the whole fetch. "
            "A bucket that yields nothing for a region is a fact about the region "
            "worth seeing, not an error — and a bucket that yields nothing at all "
            "is a broken query worth catching."
        ),
    )

    @model_validator(mode="after")
    def _check_no_taxon_is_both_kept_and_dropped(self) -> CandidatePool:
        """Reject a pool that both keeps and drops the same taxon."""
        kept_ids = [taxon.inat_taxon_id for taxon in self.candidates]
        dropped_ids = [record.inat_taxon_id for record in self.dropped]
        errors: list[str] = []

        if len(kept_ids) != len(set(kept_ids)):
            repeats = sorted({t for t in kept_ids if kept_ids.count(t) > 1})
            errors.append(f"duplicate candidates: {repeats}")
        if len(dropped_ids) != len(set(dropped_ids)):
            errors.append(
                f"duplicate drops: {sorted({t for t in dropped_ids if dropped_ids.count(t) > 1})}"
            )
        both = sorted(set(kept_ids) & set(dropped_ids))
        if both:
            errors.append(f"taxa both kept and dropped: {both}")

        if errors:
            message = "candidate pool is not internally consistent: " + "; ".join(errors)
            raise ValueError(message)
        return self

    def considered(self) -> int:
        """Total taxa the fetch looked at.

        Returns:
            Kept plus dropped. Always equals what the source stage fed in.

        Example:
            >>> from datetime import UTC, datetime
            >>> from sift_pack.manifest import SourceRef
            >>> pool = CandidatePool(
            ...     domain="plants",
            ...     state="MI",
            ...     place_id=29,
            ...     fetched_at=datetime(2026, 8, 7, tzinfo=UTC),
            ...     sources=[
            ...         SourceRef(
            ...             name="iNaturalist API",
            ...             version="v1",
            ...             retrieved_at=datetime(2026, 8, 7, tzinfo=UTC),
            ...             url="https://api.inaturalist.org/v1/",
            ...         )
            ...     ],
            ...     candidates=[],
            ...     dropped=[],
            ... )
            >>> pool.considered()
            0
        """
        return len(self.candidates) + len(self.dropped)
