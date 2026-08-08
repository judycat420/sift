"""The resolved pool schema: candidates whose image bytes now exist.

WHY THIS MODULE EXISTS
----------------------
A `CandidatePool` describes photos that exist somewhere on the internet. A
`ResolvedPool` describes photos Sift has downloaded, transcoded and stored, and
can therefore make claims about — a content hash, a byte size, real output
dimensions. That is a different kind of artefact, and conflating them would
reintroduce exactly the half-built state the candidate/manifest split removed:
a photo record whose `sha256` is present but might be a placeholder.

So the pipeline gains a third stage rather than a nullable field. A photo that
has not been fetched cannot be represented as a resolved one, because
`ResolvedPhoto` requires the digest and the digest can only come from bytes.

STILL NO AXIS 1
---------------
A resolved pool carries no nativity claim, for the same reason a candidate pool
does not: USDA promotion is terminal in M4, and it is the only path by which such
a claim may come to exist (`docs/decisions.md`, 2026-08-07).

WHY THIS IS NOT `manifest.Image`
--------------------------------
`ResolvedPhoto` *is* a `manifest.Image` — it inherits every field, so a resolved
photo is already a valid manifest image and promotion never has to construct one
from parts. It adds `month_bucket`, which the manifest has no use for but which
this stage needs in order to recompute `months_represented` from the photos
rather than trusting a number copied forward from the candidate stage. Promotion
narrows it back by dropping that one field.
"""

from __future__ import annotations

from collections import Counter
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from sift_pack.candidates import MAX_PHOTOS_PER_OBSERVER, MIN_PHOTOS_PER_CANDIDATE, DropRecord
from sift_pack.download import DownloadFailureReason
from sift_pack.manifest import Image, SourceRef

__all__ = [
    "ResolveDropReason",
    "ResolveDropRecord",
    "ResolvedPhoto",
    "ResolvedPool",
    "ResolvedTaxon",
]

ResolveDropReason = Literal[
    DownloadFailureReason,
    "photo_undecodable",
    "insufficient_resolved_images",
]
"""Every reason a photo or taxon can be lost at the resolve stage.

Closed, like `candidates.DropReason`, so a typo cannot invent a category and
split one real problem across two lines of the report.

The first four apply to individual photos; the last applies to a taxon that fell
below the four-image floor once its failed photos were removed.
"""


class _Frozen(BaseModel):
    """Base config for resolved-pool models: frozen, and no unrecognised keys."""

    model_config = ConfigDict(frozen=True, extra="forbid")


class ResolveDropRecord(_Frozen):
    """One photo or taxon lost at the resolve stage, and why.

    Example:
        >>> ResolveDropRecord(
        ...     inat_taxon_id=47911,
        ...     name="Asclepias syriaca",
        ...     reason="photo_not_found",
        ...     detail="404 for photo 123",
        ...     inat_photo_id=123,
        ... ).reason
        'photo_not_found'
    """

    inat_taxon_id: int = Field(ge=1, description="Taxon the loss belongs to.")
    name: str = Field(min_length=1, description="Scientific name, for a readable report.")
    reason: ResolveDropReason = Field(description="Closed-vocabulary reason code.")
    detail: str = Field(min_length=1, description="What specifically failed, with values.")
    inat_photo_id: int | None = Field(
        default=None,
        description="The photo, when the loss was one photo rather than a whole taxon.",
    )


class ResolvedPhoto(Image):
    """A stored image: a manifest `Image` plus the bucket it was sampled from.

    Example:
        >>> photo = ResolvedPhoto(
        ...     sha256="0" * 64,
        ...     inat_photo_id=1,
        ...     taxon_id=47911,
        ...     license="cc0",
        ...     photographer_name=None,
        ...     photographer_login="somebody",
        ...     observation_url="https://www.inaturalist.org/observations/1",
        ...     width=500,
        ...     height=333,
        ...     bytes=20480,
        ...     month_bucket="A",
        ... )
        >>> photo.month_bucket
        'A'
    """

    month_bucket: str = Field(
        min_length=1,
        description=(
            "Seasonal bucket the source observation came from. Carried so seasonal "
            "spread stays recomputable here; dropped when promoted to a manifest."
        ),
    )

    def as_image(self) -> Image:
        """Narrow to the manifest type, dropping build-half metadata.

        Returns:
            The same photo as a plain `manifest.Image`.

        Example:
            >>> photo = ResolvedPhoto(
            ...     sha256="0" * 64,
            ...     inat_photo_id=1,
            ...     taxon_id=47911,
            ...     license="cc0",
            ...     photographer_name=None,
            ...     photographer_login="somebody",
            ...     observation_url="https://www.inaturalist.org/observations/1",
            ...     width=500,
            ...     height=333,
            ...     bytes=20480,
            ...     month_bucket="A",
            ... )
            >>> type(photo.as_image()).__name__
            'Image'
        """
        return Image.model_validate(self.model_dump(exclude={"month_bucket"}))


class ResolvedTaxon(_Frozen):
    """A candidate whose photos now exist as stored bytes.

    Carries no axis-1 field, for the same reason `CandidateTaxon` does not.

    Example:
        >>> ResolvedTaxon.model_fields.keys() & {"axis1_value", "axis1_source"}
        set()
    """

    inat_taxon_id: int = Field(ge=1, description="PRIMARY KEY; names are mutable attributes.")
    scientific_name: str = Field(min_length=1, description="Current name.")
    common_names: list[str] = Field(description="Vernacular names; may be empty, never invented.")
    rank: str = Field(min_length=1, description="Taxonomic rank.")
    genus: str = Field(min_length=1, description="Genus, read from the taxon's ancestors.")
    family: str = Field(min_length=1, description="Family, read from the taxon's ancestors.")
    obs_count: int = Field(ge=0, description="Research-grade observations in this place.")
    months_represented: int = Field(
        ge=1, le=4, description="Seasonal buckets the stored photos span."
    )
    distinct_observers: int = Field(ge=1, description="Different people who took the photos.")
    images: list[ResolvedPhoto] = Field(
        min_length=MIN_PHOTOS_PER_CANDIDATE,
        description=(
            "Stored images. The floor is the same as at candidate stage: a taxon "
            "that lost photos to download failures is dropped, never padded."
        ),
    )

    @model_validator(mode="after")
    def _check_signals_match_the_stored_photos(self) -> ResolvedTaxon:
        """Reject a taxon whose recorded signals disagree with its stored photos.

        Recomputed rather than carried forward from the candidate stage. Photos
        can be lost between the two, and a `months_represented` copied across a
        stage where photos disappeared would be a stale claim about the pack a
        learner actually receives.
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

        digests = [photo.sha256 for photo in self.images]
        if len(digests) != len(set(digests)):
            errors.append("the same stored image appears twice")

        if any(photo.taxon_id != self.inat_taxon_id for photo in self.images):
            errors.append("carries photos belonging to another taxon")

        if errors:
            message = f"taxon {self.inat_taxon_id}: " + "; ".join(errors)
            raise ValueError(message)
        return self


class ResolvedPool(_Frozen):
    """Every taxon whose images are stored, and everything lost getting there.

    Example:
        >>> from datetime import UTC, datetime
        >>> from sift_pack.manifest import SourceRef
        >>> pool = ResolvedPool(
        ...     domain="plants",
        ...     state="MI",
        ...     place_id=29,
        ...     fetched_at=datetime(2026, 8, 7, tzinfo=UTC),
        ...     resolved_at=datetime(2026, 8, 7, tzinfo=UTC),
        ...     sources=[
        ...         SourceRef(
        ...             name="iNaturalist Open Dataset",
        ...             version="WEBP q75",
        ...             retrieved_at=datetime(2026, 8, 7, tzinfo=UTC),
        ...             url="https://inaturalist-open-data.s3.amazonaws.com/",
        ...         )
        ...     ],
        ...     taxa=[],
        ...     dropped=[],
        ...     resolve_dropped=[],
        ... )
        >>> pool.considered()
        0
    """

    domain: str = Field(min_length=1, description="Domain slug the pool was built for.")
    state: str = Field(min_length=1, description="Region code, e.g. 'MI'.")
    place_id: int = Field(ge=1, description="iNaturalist place ID.")
    fetched_at: datetime = Field(description="When the candidate fetch ran.")
    resolved_at: datetime = Field(description="When image resolution ran.")
    sources: list[SourceRef] = Field(
        min_length=1,
        description=(
            "Datasets and tools that contributed, including the encoder profile. "
            "Hashes are over transcoded bytes, so the encoder is part of this "
            "pool's provenance, not an implementation detail."
        ),
    )
    taxa: list[ResolvedTaxon] = Field(description="Taxa whose images are stored.")
    dropped: list[DropRecord] = Field(
        description="Taxa dropped during the candidate fetch, carried forward unchanged."
    )
    resolve_dropped: list[ResolveDropRecord] = Field(
        description=(
            "Photos and taxa lost during image resolution. Kept separate from the "
            "fetch-stage drops so a report can say which stage cost what."
        )
    )

    @model_validator(mode="after")
    def _check_taxa_are_unique(self) -> ResolvedPool:
        """Reject a pool listing the same taxon twice."""
        ids = [taxon.inat_taxon_id for taxon in self.taxa]
        if len(ids) != len(set(ids)):
            repeats = sorted({i for i in ids if ids.count(i) > 1})
            message = f"resolved pool lists taxa more than once: {repeats}"
            raise ValueError(message)
        return self

    def considered(self) -> int:
        """How many taxa the resolve stage was handed.

        Returns:
            Taxa kept, plus taxa lost at the resolve stage.

        Example:
            >>> from datetime import UTC, datetime
            >>> from sift_pack.manifest import SourceRef
            >>> ResolvedPool(
            ...     domain="plants",
            ...     state="MI",
            ...     place_id=29,
            ...     fetched_at=datetime(2026, 8, 7, tzinfo=UTC),
            ...     resolved_at=datetime(2026, 8, 7, tzinfo=UTC),
            ...     sources=[
            ...         SourceRef(
            ...             name="x",
            ...             version="1",
            ...             retrieved_at=datetime(2026, 8, 7, tzinfo=UTC),
            ...             url="https://example.invalid/",
            ...         )
            ...     ],
            ...     taxa=[],
            ...     dropped=[],
            ...     resolve_dropped=[],
            ... ).considered()
            0
        """
        lost = {
            record.inat_taxon_id
            for record in self.resolve_dropped
            if record.reason == "insufficient_resolved_images"
        }
        return len(self.taxa) + len(lost)

    def digests(self) -> set[str]:
        """Every stored image this pool references.

        Returns:
            The set of SHA-256 digests, for garbage collection.

        Example:
            >>> from datetime import UTC, datetime
            >>> from sift_pack.manifest import SourceRef
            >>> ResolvedPool(
            ...     domain="plants",
            ...     state="MI",
            ...     place_id=29,
            ...     fetched_at=datetime(2026, 8, 7, tzinfo=UTC),
            ...     resolved_at=datetime(2026, 8, 7, tzinfo=UTC),
            ...     sources=[
            ...         SourceRef(
            ...             name="x",
            ...             version="1",
            ...             retrieved_at=datetime(2026, 8, 7, tzinfo=UTC),
            ...             url="https://example.invalid/",
            ...         )
            ...     ],
            ...     taxa=[],
            ...     dropped=[],
            ...     resolve_dropped=[],
            ... ).digests()
            set()
        """
        return {photo.sha256 for taxon in self.taxa for photo in taxon.images}
