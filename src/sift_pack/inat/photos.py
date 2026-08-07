"""Selection of licence-cleared photos, one per observation.

WHY THIS MODULE EXISTS
----------------------
A card that shows the same plant from the same angle four times teaches
recognition of that photograph, not of the plant. Variation is the whole point,
so photos are selected one per observation — different plants, different
photographers, different light, different phenological stages.

The second job here is licence enforcement at the point of ingest. The API is
asked for CC0/CC-BY/CC-BY-SA only, and the licence on each returned photo is
checked again before it is kept, because a filter that is only applied
server-side is a filter that stops working silently when a parameter name
changes.

INVARIANT PROTECTED
-------------------
No photo enters a candidate unless its licence is one of the three permitted
values, its observation has not already contributed a photo, and its dimensions
are known. A taxon that cannot reach four such photos is dropped with a reason
rather than padded to four — padding would mean repeating an observation, which
is the thing this module exists to prevent.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from sift_pack.candidates import (
    MAX_PHOTOS_PER_CANDIDATE,
    MIN_PHOTOS_PER_CANDIDATE,
    CandidatePhoto,
    DropRecord,
)
from sift_pack.inat.client import InatClient, InatError
from sift_pack.manifest import License

__all__ = [
    "OBSERVATIONS_PAGE_SIZE",
    "PERMITTED_LICENSES",
    "PREFERRED_MIN_AGREEMENTS",
    "select_photos",
]

_log = logging.getLogger(__name__)

OBSERVATIONS_PAGE_SIZE = 200
"""The documented maximum `per_page` for the observations endpoint."""

PERMITTED_LICENSES: frozenset[str] = frozenset({"cc0", "cc-by", "cc-by-sa"})
"""Licences a photo may carry, matching `manifest.License`.

Responses report these lowercase; the request parameter wants them uppercase.
`_API_LICENSE_PARAM` holds the request spelling so the two cannot drift.
"""

_API_LICENSE_PARAM: tuple[str, ...] = ("CC0", "CC-BY", "CC-BY-SA")

PREFERRED_MIN_AGREEMENTS = 2
"""Observations with at least this many agreeing IDs are selected first.

Not a hard filter: for an uncommon taxon, requiring two agreements can leave
fewer than four usable photos, and dropping a genuinely common plant because
its observations are under-reviewed would bias the pack toward the popular. The
count that actually held is recorded on the candidate, so the compromise is
visible rather than assumed.
"""


def _license_of(photo: object) -> License | None:
    """Read a permitted licence code off a photo, or `None` if it has none."""
    if not isinstance(photo, dict):
        return None
    code = photo.get("license_code")
    if not isinstance(code, str):
        return None
    lowered = code.lower()
    if lowered not in PERMITTED_LICENSES:
        return None
    # Narrowing for mypy: the membership test above is the runtime guarantee.
    if lowered == "cc0":
        return "cc0"
    if lowered == "cc-by":
        return "cc-by"
    return "cc-by-sa"


def _dimensions(photo: dict[str, object]) -> tuple[int, int] | None:
    """Read original pixel dimensions, or `None` when the API omits them."""
    dims = photo.get("original_dimensions")
    if not isinstance(dims, dict):
        return None
    width = dims.get("width")
    height = dims.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return width, height
    return None


@dataclass(frozen=True, slots=True)
class _ObservationContext:
    """The observation-level facts every photo in one observation inherits."""

    taxon_id: int
    observation_id: int
    observation_url: str
    agreements: int
    photographer_login: str
    photographer_name: str | None


def _photo_from(photo: dict[str, object], context: _ObservationContext) -> CandidatePhoto | None:
    """Build one candidate photo, or `None` if any required field is missing."""
    license_code = _license_of(photo)
    photo_id = photo.get("id")
    dims = _dimensions(photo)
    if license_code is None or not isinstance(photo_id, int) or dims is None:
        return None
    try:
        return CandidatePhoto(
            inat_photo_id=photo_id,
            taxon_id=context.taxon_id,
            observation_id=context.observation_id,
            observation_url=context.observation_url,
            license=license_code,
            photographer_login=context.photographer_login,
            photographer_name=context.photographer_name,
            width=dims[0],
            height=dims[1],
            identification_agreements=context.agreements,
        )
    except ValidationError:
        return None


def _candidates_from_observation(
    entry: object,
    taxon_id: int,
) -> tuple[int, list[CandidatePhoto]]:
    """Return one observation's agreement count and its usable photos."""
    if not isinstance(entry, dict):
        return 0, []
    observation_id = entry.get("id")
    uri = entry.get("uri")
    photos = entry.get("photos")
    user = entry.get("user")
    if not isinstance(observation_id, int) or not isinstance(photos, list):
        return 0, []
    if not isinstance(uri, str) or not uri:
        return 0, []
    if not isinstance(user, dict):
        return 0, []
    login = user.get("login")
    if not isinstance(login, str) or not login:
        return 0, []
    name = user.get("name")
    photographer_name = name if isinstance(name, str) and name else None

    raw_agreements = entry.get("num_identification_agreements")
    agreements = raw_agreements if isinstance(raw_agreements, int) and raw_agreements >= 0 else 0

    context = _ObservationContext(
        taxon_id=taxon_id,
        observation_id=observation_id,
        observation_url=uri,
        agreements=agreements,
        photographer_login=login,
        photographer_name=photographer_name,
    )
    usable = [
        candidate
        for photo in photos
        if isinstance(photo, dict) and (candidate := _photo_from(photo, context)) is not None
    ]
    return agreements, usable


def select_photos(
    client: InatClient,
    taxon_id: int,
    scientific_name: str,
    place_id: int,
) -> tuple[list[CandidatePhoto], DropRecord | None]:
    """Choose up to eight photos for a taxon, each from a distinct observation.

    Observations with at least `PREFERRED_MIN_AGREEMENTS` agreeing IDs are taken
    first; less-confirmed ones are used only to reach the four-photo floor.

    Args:
        client: Cached iNaturalist client.
        taxon_id: Taxon to fetch photos for.
        scientific_name: Name, used only in the drop record.
        place_id: Place to scope observations to, so the photos show the plant
            as it looks where the learner is.

    Returns:
        The selected photos, and a `DropRecord` when there were fewer than
        `MIN_PHOTOS_PER_CANDIDATE` — in which case the photo list is empty.
        Never pads to reach the floor.

    Raises:
        InatError: If the response is not shaped like an observations response.

    Example:
        >>> select_photos(client, 48662, "Asclepias tuberosa", 29)  # doctest: +SKIP
        ... # SKIPPED: needs a populated client. Covered by tests/test_inat_pipeline.py
        ... # against recorded fixtures.
    """
    response = client.get(
        "observations",
        {
            "taxon_id": taxon_id,
            "place_id": place_id,
            "quality_grade": "research",
            "photo_license": _API_LICENSE_PARAM,
            "per_page": OBSERVATIONS_PAGE_SIZE,
            "order_by": "votes",
            "order": "desc",
        },
    )
    results = response.get("results")
    if not isinstance(results, list):
        message = f"observations for taxon {taxon_id} had no results list"
        raise InatError(message)

    preferred: list[CandidatePhoto] = []
    fallback: list[CandidatePhoto] = []
    for entry in results:
        agreements, usable = _candidates_from_observation(entry, taxon_id)
        if not usable:
            continue
        # One photo per observation: take the first usable one and move on.
        bucket = preferred if agreements >= PREFERRED_MIN_AGREEMENTS else fallback
        bucket.append(usable[0])

    selected = (preferred + fallback)[:MAX_PHOTOS_PER_CANDIDATE]

    if len(selected) < MIN_PHOTOS_PER_CANDIDATE:
        _log.info(
            "dropping taxon %d (%s): %d licensed photos, need %d",
            taxon_id,
            scientific_name,
            len(selected),
            MIN_PHOTOS_PER_CANDIDATE,
        )
        return [], DropRecord(
            inat_taxon_id=taxon_id,
            name=scientific_name,
            reason="insufficient_licensed_photos",
            detail=(
                f"{len(selected)} photos from distinct observations under "
                f"{sorted(PERMITTED_LICENSES)}, need {MIN_PHOTOS_PER_CANDIDATE}"
            ),
        )
    return selected, None


def minimum_agreement(photos: list[CandidatePhoto]) -> int:
    """Lowest agreeing-ID count among the selected photos.

    The floor rather than the mean: one heavily-confirmed observation should not
    make a taxon look better-identified than its weakest included photo.

    Args:
        photos: The selected photos. Must not be empty.

    Returns:
        The minimum `identification_agreements` across them.

    Raises:
        ValueError: If `photos` is empty — there is no honest answer for a taxon
            with no photos, and returning 0 would look like a real measurement.

    Example:
        >>> photo = CandidatePhoto(
        ...     inat_photo_id=1,
        ...     taxon_id=1,
        ...     observation_id=1,
        ...     observation_url="https://www.inaturalist.org/observations/1",
        ...     license="cc0",
        ...     photographer_login="somebody",
        ...     photographer_name=None,
        ...     width=10,
        ...     height=10,
        ...     identification_agreements=3,
        ... )
        >>> minimum_agreement([photo])
        3
    """
    if not photos:
        message = "minimum_agreement requires at least one photo"
        raise ValueError(message)
    return min(photo.identification_agreements for photo in photos)
