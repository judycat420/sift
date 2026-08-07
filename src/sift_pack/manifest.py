"""The pack manifest schema: the contract between the build half and the runtime half.

WHY THIS MODULE EXISTS
----------------------
`sift_pack` builds packs in Python; something else consumes them at study time.
The manifest is the only thing that crosses between the two, which makes it the
one place where a mistake is expensive: the runtime cannot re-derive a claim,
re-check a licence, or ask where a value came from. Whatever this schema
permits, the runtime will eventually be handed.

So the schema is written to make the failure modes we care about *unspeakable*
rather than merely tested against. A NonCommercial licence has no member in the
`License` union, so a non-compliant pack cannot be serialised at all — not
"fails a check we remembered to run". `Confidence` has no `"low"` member, so a
low-confidence claim cannot enter a manifest even by accident. `Taxon` has no
default for `axis1_source`, so an unattributed claim is a construction error.

INVARIANT PROTECTED
-------------------
A manifest that parses is internally consistent and licence-compliant. Every
hash a taxon references resolves to an image in the same manifest, every image
belongs to a taxon in the same manifest, and every factual axis-1 claim carries
a source and a confidence. There is no partially-valid manifest: parsing either
yields a usable pack or raises.

CHANGING THIS SCHEMA IS A BREAKING CHANGE
-----------------------------------------
The runtime half pins `pack_version`. Any change to a field name, type, or
constraint — including *tightening* one, which will reject packs the previous
runtime accepted — requires bumping `pack_version` and a note in
`docs/decisions.md`.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator

__all__ = [
    "AnswerRank",
    "Confidence",
    "Image",
    "License",
    "Manifest",
    "Sha256",
    "SourceRef",
    "Taxon",
]

License = Literal["cc0", "cc-by", "cc-by-sa"]
"""Licences a pack may contain.

NonCommercial is deliberately absent — see `docs/decisions.md`, 2026-08-05,
"Licences: CC0, CC-BY and CC-BY-SA only". Excluding it from the type means a
non-compliant pack cannot be serialised, rather than failing a validation step
somebody has to remember to run.
"""

Confidence = Literal["high", "medium"]
"""Confidence bands a claim may carry into a manifest.

`"low"` has no member here on purpose. Low-confidence claims are dropped
upstream (STANDARDS.md rule 5) and counted; there is no representation for one
inside a pack, so the runtime never has to decide what to do with it.
"""

AnswerRank = Literal["species", "genus"]
"""How precisely a card may ask the user to answer.

`"genus"` marks taxa where community identifications are unreliable at species
level, so the card must only ask for genus. The rule for assigning this is
decided in M3, when real observation data exists to base it on; an ADR follows
then.
"""

Sha256 = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
"""A lowercase hex SHA-256 digest, used as the content address of an image."""


class _Frozen(BaseModel):
    """Base config for every manifest model.

    `frozen` because a parsed manifest is a record of what was built, not a
    working buffer. `extra="forbid"` because an unrecognised key means the
    producer and the consumer disagree about the schema, and guessing which of
    them is right is exactly the silent failure STANDARDS.md rule 5 forbids.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")


class SourceRef(_Frozen):
    """A dataset that contributed to a pack, as structured provenance.

    Structured rather than a free-form dict so that provenance is queryable:
    the runtime can show "USDA PLANTS, retrieved 2026-08-06" without parsing
    prose, and a stale pack can be identified by comparing versions.

    Example:
        >>> from datetime import UTC, datetime
        >>> ref = SourceRef(
        ...     name="USDA PLANTS",
        ...     version="2026-07",
        ...     retrieved_at=datetime(2026, 8, 6, tzinfo=UTC),
        ...     url="https://plants.usda.gov/downloads",
        ... )
        >>> ref.name
        'USDA PLANTS'
    """

    name: str = Field(min_length=1, description="Human-readable dataset name, e.g. 'USDA PLANTS'.")
    version: str = Field(
        min_length=1,
        description=(
            "Upstream version or export identifier. Free-form because upstreams "
            "disagree about what a version is; must be recorded verbatim, never "
            "normalised or invented."
        ),
    )
    retrieved_at: datetime = Field(
        description="When Sift read this source. Timezone-aware; UTC by convention."
    )
    url: str = Field(min_length=1, description="Where it was read from.")


class Image(_Frozen):
    """One licence-cleared photo, content-addressed by SHA-256.

    Example:
        >>> img = Image(
        ...     sha256="0" * 64,
        ...     inat_photo_id=1,
        ...     taxon_id=48662,
        ...     license="cc0",
        ...     photographer_name=None,
        ...     photographer_login="placeholder",
        ...     observation_url="https://www.inaturalist.org/observations/1",
        ...     width=1024,
        ...     height=768,
        ...     bytes=204800,
        ... )
        >>> img.license
        'cc0'
    """

    sha256: Sha256 = Field(description="Content address of the image bytes; the join key.")
    inat_photo_id: int = Field(ge=1, description="iNaturalist photo ID this came from.")
    taxon_id: int = Field(
        ge=1,
        description="`Taxon.inat_taxon_id` this image depicts. Must exist in the same manifest.",
    )
    license: License = Field(
        description=(
            "Per-photo licence, taken verbatim from the source. Typed as `License`, "
            "so an NC photo cannot be represented here at all."
        )
    )
    photographer_name: str | None = Field(
        description=(
            "Display name, when the photographer set one. Nullable because many "
            "have not — and an absent name is left absent, never backfilled from "
            "the login (STANDARDS.md rule 5)."
        )
    )
    photographer_login: str = Field(
        min_length=1,
        description="iNaturalist login. Required: CC-BY and CC-BY-SA attribution depends on it.",
    )
    observation_url: str = Field(
        min_length=1, description="Source observation, for attribution and for auditing."
    )
    width: int = Field(ge=1, description="Pixel width.")
    height: int = Field(ge=1, description="Pixel height.")
    bytes: int = Field(
        ge=1, description="Encoded size in bytes, so the runtime can budget a download."
    )


class Taxon(_Frozen):
    """One taxon in a pack, with its axis-1 claim and its images.

    Example:
        >>> taxon = Taxon(
        ...     inat_taxon_id=48662,
        ...     scientific_name="Asclepias tuberosa",
        ...     common_names=["butterfly weed"],
        ...     rank="species",
        ...     genus="Asclepias",
        ...     family="Apocynaceae",
        ...     obs_count=12345,
        ...     axis1_value="native",
        ...     axis1_source="USDA PLANTS",
        ...     axis1_confidence="high",
        ...     answer_rank="species",
        ...     image_hashes=["0" * 64, "1" * 64, "2" * 64, "3" * 64],
        ... )
        >>> taxon.axis1_source
        'USDA PLANTS'
    """

    inat_taxon_id: int = Field(
        ge=1,
        description=(
            "PRIMARY KEY. Every join, cache entry and cross-reference goes through "
            "this ID. The names below are mutable attributes of it: species are "
            "split, merged and renamed, and common names vary by region, so code "
            "keyed on a name silently comes to mean a different organism while "
            "still resolving. See docs/decisions.md, 2026-08-05."
        ),
    )
    scientific_name: str = Field(min_length=1, description="Current name. Mutable; not a key.")
    common_names: list[str] = Field(
        description="Vernacular names, most-used first. May be empty; never invented."
    )
    rank: str = Field(min_length=1, description="Taxonomic rank as iNaturalist reports it.")
    genus: str = Field(min_length=1, description="Genus name, for genus-rank cards.")
    family: str = Field(min_length=1, description="Family name, for grouping.")
    obs_count: int = Field(
        ge=0, description="Research-grade observation count backing this taxon's identifiability."
    )
    axis1_value: str = Field(
        min_length=1,
        description="The domain's axis-1 claim, e.g. 'native'. Meaning is domain-specific.",
    )
    axis1_source: str = Field(
        min_length=1,
        description=(
            "Which dataset asserted `axis1_value`. Required, no default: an "
            "unattributed claim is the exact failure this schema exists to "
            "prevent (STANDARDS.md rule 4)."
        ),
    )
    axis1_confidence: Confidence = Field(
        description="Confidence in `axis1_value`. Cannot be 'low' — those are dropped upstream."
    )
    answer_rank: AnswerRank = Field(
        description="Whether the card may ask for species, or must stop at genus."
    )
    image_hashes: list[Sha256] = Field(
        min_length=4,
        description=(
            "At least four images, so a card can show variation rather than one "
            "memorable photo. Enforced here so a starved taxon fails at build "
            "time, when it can be fixed, instead of at study time."
        ),
    )


class Manifest(_Frozen):
    """A complete, self-consistent study pack.

    Referential integrity is enforced at parse time by `_check_referential_integrity`,
    so any `Manifest` instance that exists — however it was obtained — is already
    known-good. Callers never need to re-check it.

    Args:
        Field values as declared below.

    Returns:
        Not applicable; this is a model.

    Raises:
        pydantic.ValidationError: If any field is invalid, or if taxa and images
            do not reference each other consistently.

    Example:
        >>> from datetime import UTC, date, datetime
        >>> manifest = Manifest(
        ...     domain="plants",
        ...     state="MI",
        ...     built_at=datetime(2026, 8, 6, tzinfo=UTC),
        ...     inat_taxonomy_date=date(2026, 7, 1),
        ...     sources=[
        ...         SourceRef(
        ...             name="iNaturalist",
        ...             version="v1",
        ...             retrieved_at=datetime(2026, 8, 6, tzinfo=UTC),
        ...             url="https://api.inaturalist.org/v1/",
        ...         )
        ...     ],
        ...     taxa=[],
        ...     images=[],
        ... )
        >>> manifest.pack_version
        1
    """

    pack_version: int = Field(
        default=1,
        ge=1,
        description="Schema version. Bump on any change to this module's shapes.",
    )
    domain: str = Field(min_length=1, description="Domain slug, e.g. 'plants'.")
    state: str = Field(min_length=1, description="Region the pack was built for, e.g. 'MI'.")
    built_at: datetime = Field(description="When this pack was assembled. Timezone-aware.")
    inat_taxonomy_date: date = Field(
        description=(
            "Which iNaturalist taxonomy snapshot the IDs refer to. Taxon IDs are "
            "only stable relative to a snapshot; without this, a merged taxon is "
            "undetectable after the fact."
        )
    )
    sources: list[SourceRef] = Field(
        min_length=1,
        description=(
            "Every dataset that contributed. At least one: a pack with no "
            "declared sources is unattributable by construction (STANDARDS.md rule 7)."
        ),
    )
    taxa: list[Taxon] = Field(
        description="Taxa in the pack. May be empty — an empty deck beats a wrong one."
    )
    images: list[Image] = Field(description="Every image referenced by `taxa`, and no others.")

    @model_validator(mode="after")
    def _check_referential_integrity(self) -> Manifest:
        """Reject a manifest whose taxa and images disagree.

        Four ways a pack can be internally broken, all of which would surface at
        study time as a missing card or a blank image if not caught here:
        duplicate image hashes, duplicate taxon IDs, a taxon referencing a hash
        no image provides, and an image attributed to a taxon not in the pack.
        """
        errors: list[str] = []

        image_hashes = [image.sha256 for image in self.images]
        known_hashes = set(image_hashes)
        if len(image_hashes) != len(known_hashes):
            duplicates = sorted({h for h in image_hashes if image_hashes.count(h) > 1})
            errors.append(f"duplicate image sha256: {duplicates}")

        taxon_ids = [taxon.inat_taxon_id for taxon in self.taxa]
        known_taxon_ids = set(taxon_ids)
        if len(taxon_ids) != len(known_taxon_ids):
            duplicates_ids = sorted({t for t in taxon_ids if taxon_ids.count(t) > 1})
            errors.append(f"duplicate inat_taxon_id: {duplicates_ids}")

        for taxon in self.taxa:
            dangling = sorted(set(taxon.image_hashes) - known_hashes)
            if dangling:
                errors.append(
                    f"taxon {taxon.inat_taxon_id} references unknown image hashes: {dangling}"
                )

        orphans = sorted({i.taxon_id for i in self.images} - known_taxon_ids)
        if orphans:
            errors.append(f"images reference unknown taxa: {orphans}")

        if errors:
            message = "manifest is not internally consistent: " + "; ".join(errors)
            raise ValueError(message)
        return self
