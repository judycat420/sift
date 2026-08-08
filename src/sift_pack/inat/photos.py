"""Selection of licence-cleared photos: seasonally, socially and taxonomically spread.

WHY THIS MODULE EXISTS
----------------------
A card that shows the same plant from the same angle four times teaches
recognition of that photograph, not of the plant. Variation is the whole point.

M2 asked for one page of 200 observations and took the first eight usable ones.
Every rule it enforced was satisfied — distinct observations, licence-cleared,
four minimum — and the result was still bad, because a single unstratified page
is a seasonally and socially clustered sample. Bloodroot photographed in April
is bloodroot in flower; a learner taught only that cannot identify its leaves in
July. And one enthusiastic local observer can supply much of a taxon's records,
so the pack teaches their camera, their habitat and their light.

So the sample is stratified before it is filtered. Four requests per taxon, one
per seasonal bucket, and selection round-robins across buckets so that spread is
what the algorithm optimises rather than what it hopes for.

INVARIANT PROTECTED
-------------------
No photo enters a candidate unless its licence is permitted, its observation has
not already contributed, and its observer is under the per-taxon cap. Selection
maximises seasonal spread; beyond that it makes no quality judgement at all,
because iNaturalist exposes no per-observation quality signal that survives
inspection (`docs/decisions.md`, 2026-08-07). Within a bucket, observations are
taken in ascending ID order — a tiebreak that claims nothing and is reproducible.

A taxon that cannot reach four photos under all of those rules is dropped; the
rules are never relaxed to reach the floor, because a taxon padded back to four
is exactly the unspread sample this module exists to avoid.

An empty bucket is not an error. A spring ephemeral has no October records, and
a fetch that treated that as a failure would drop precisely the plants whose
seasonality is most worth teaching.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from pydantic import ValidationError

from sift_pack.candidates import (
    MAX_PHOTOS_PER_CANDIDATE,
    MAX_PHOTOS_PER_OBSERVER,
    MIN_PHOTOS_PER_CANDIDATE,
    CandidatePhoto,
    DropRecord,
)
from sift_pack.inat.client import InatClient, InatError, ParamValue
from sift_pack.manifest import License

__all__ = [
    "MONTH_BUCKETS",
    "OBSERVATIONS_PER_BUCKET",
    "PERMITTED_LICENSES",
    "MonthBucket",
    "PhotoSelection",
    "distinct_observers",
    "months_represented",
    "observations_query",
    "select_photos",
]

_log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class MonthBucket:
    """One seasonal stratum of the sample.

    Attributes:
        label: Short identifier, recorded on every photo drawn from it.
        months: Calendar months, 1-12, queried together.
        description: What the bucket is for, in a report.
    """

    label: str
    months: tuple[int, ...]
    description: str


MONTH_BUCKETS: tuple[MonthBucket, ...] = (
    MonthBucket("A", (3, 4, 5), "spring: emergence and early flowering"),
    MonthBucket("B", (6, 7), "early summer: peak flowering"),
    MonthBucket("C", (8, 9), "late summer: fruit and senescence"),
    MonthBucket("D", (10, 11, 12, 1, 2), "autumn and winter: structure and dormancy"),
)
"""The four strata, sized so each holds a distinct appearance rather than an
equal number of months. These are northern-hemisphere temperate seasons: this is
a US state pack, and a southern-hemisphere domain would need its own buckets
rather than a silent reuse of these."""

OBSERVATIONS_PER_BUCKET = 25
"""Requested per bucket.

Enough to survive licence filtering and the observer cap while choosing at most
a few photos from a bucket, and small enough that a taxon costs four modest
responses instead of one enormous one. iNaturalist's high-`per_page` guidance is
for bulk retrieval; it is the wrong shape for picking eight things."""

PERMITTED_LICENSES: frozenset[str] = frozenset({"cc0", "cc-by", "cc-by-sa"})
"""Licences a photo may carry, matching `manifest.License`.

Responses report these lowercase; the request parameter wants them uppercase.
`_API_LICENSE_PARAM` holds the request spelling so the two cannot drift.
"""

_API_LICENSE_PARAM: tuple[str, ...] = ("CC0", "CC-BY", "CC-BY-SA")


@dataclass(frozen=True, slots=True)
class PhotoSelection:
    """The outcome of selecting photos for one taxon.

    Attributes:
        photos: The chosen photos; empty when the taxon was dropped.
        drop: Why it was dropped, or `None` when it was kept.
        bucket_observations: How many observations each bucket returned, before
            filtering. Recorded whether or not the taxon survived, because a
            bucket that yields nothing is a fact about the taxon and the region.
    """

    photos: list[CandidatePhoto]
    drop: DropRecord | None
    bucket_observations: dict[str, int]


@dataclass(frozen=True, slots=True)
class _Selectable:
    """One observation's best usable photo, with the facts selection ranks on."""

    photo: CandidatePhoto
    observer: str
    agreements: int
    bucket: str

    def rank(self) -> int:
        """Sort key within a bucket: observation ID ascending.

        Deliberately claimless. Sift previously ordered by agreeing-ID count,
        which looked like ranking by confidence and was not: iNaturalist's
        `num_identification_agreements` counts agreements with the observer's
        own identification, and research grade needs only two identifications
        in total, so the number reports whether an observation drew an extra
        identifier — which tracks being photogenic or contentious, not being
        right (`docs/decisions.md`, 2026-08-07).

        Ordering by ID asserts nothing about the observations it orders. It
        exists only to make selection reproducible, which the round-robin across
        buckets and the per-observer cap already do most of the work for.
        """
        return self.photo.observation_id


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
    bucket: str


def _photo_from(photo: dict[str, object], context: _ObservationContext) -> CandidatePhoto | None:
    """Build one candidate photo, or `None` if any required field is missing."""
    license_code = _license_of(photo)
    photo_id = photo.get("id")
    dims = _dimensions(photo)
    url = photo.get("url")
    if license_code is None or not isinstance(photo_id, int) or dims is None:
        return None
    if not isinstance(url, str) or not url:
        # No URL means no route to the bytes. Dropping here beats carrying a
        # candidate the resolve stage would have to reject anyway.
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
            month_bucket=context.bucket,
            source_url=url,
        )
    except ValidationError:
        return None


@dataclass(frozen=True, slots=True)
class _ObservationIdentity:
    """The observation-level facts read before any photo is considered."""

    observation_id: int
    uri: str
    login: str
    name: str | None
    agreements: int
    photos: list[object]


def _observation_identity(entry: object) -> _ObservationIdentity | None:
    """Read the fields every photo in an observation inherits, or `None`.

    Grouped into one read so the guard clauses live together: an observation
    missing any one of these is unusable, and there is no partial form worth
    carrying forward.
    """
    if not isinstance(entry, dict):
        return None
    observation_id = entry.get("id")
    uri = entry.get("uri")
    photos = entry.get("photos")
    user = entry.get("user")
    if not isinstance(observation_id, int) or not isinstance(photos, list):
        return None
    if not isinstance(uri, str) or not uri:
        return None
    if not isinstance(user, dict):
        return None
    login = user.get("login")
    if not isinstance(login, str) or not login:
        return None
    name = user.get("name")
    raw_agreements = entry.get("num_identification_agreements")
    return _ObservationIdentity(
        observation_id=observation_id,
        uri=uri,
        login=login,
        name=name if isinstance(name, str) and name else None,
        # A missing count is recorded as zero: the floor of what is known, never
        # a claim that somebody agreed.
        agreements=raw_agreements if isinstance(raw_agreements, int) and raw_agreements >= 0 else 0,
        photos=photos,
    )


def _selectable_from(entry: object, taxon_id: int, bucket: str) -> _Selectable | None:
    """Reduce one observation to its best usable photo, or `None`."""
    identity = _observation_identity(entry)
    if identity is None:
        return None

    context = _ObservationContext(
        taxon_id=taxon_id,
        observation_id=identity.observation_id,
        observation_url=identity.uri,
        agreements=identity.agreements,
        photographer_login=identity.login,
        photographer_name=identity.name,
        bucket=bucket,
    )
    # Rule (a): one photo per observation. Take the first usable one; the rest
    # of that observation's photos are the same plant on the same day.
    for photo in identity.photos:
        if isinstance(photo, dict):
            candidate = _photo_from(photo, context)
            if candidate is not None:
                return _Selectable(candidate, identity.login, identity.agreements, bucket)
    return None


def _choose(pooled: dict[str, list[_Selectable]]) -> list[CandidatePhoto]:
    """Pick up to eight photos, maximising seasonal spread then confidence.

    Round-robins across buckets: every bucket contributes one candidate before
    any bucket contributes a second. That makes maximising distinct month-buckets
    a property of the traversal order rather than something checked afterwards,
    so no later rule can trade it away. The per-observer cap is applied as
    candidates are taken, so an observer who dominates one bucket cannot consume
    another bucket's slot.

    Within a bucket the order is by observation ID, which is arbitrary but fixed.
    Selection makes no quality judgement beyond seasonal spread and the observer
    cap: there is no per-observation signal worth ranking on.
    """
    queues = {
        bucket.label: sorted(pooled.get(bucket.label, []), key=_Selectable.rank)
        for bucket in MONTH_BUCKETS
    }
    positions = dict.fromkeys(queues, 0)
    observer_counts: dict[str, int] = {}
    chosen: list[CandidatePhoto] = []

    while len(chosen) < MAX_PHOTOS_PER_CANDIDATE:
        progressed = False
        for bucket in MONTH_BUCKETS:
            if len(chosen) >= MAX_PHOTOS_PER_CANDIDATE:
                break
            queue = queues[bucket.label]
            index = positions[bucket.label]
            while index < len(queue):
                candidate = queue[index]
                index += 1
                if observer_counts.get(candidate.observer, 0) >= MAX_PHOTOS_PER_OBSERVER:
                    continue
                chosen.append(candidate.photo)
                observer_counts[candidate.observer] = observer_counts.get(candidate.observer, 0) + 1
                progressed = True
                break
            positions[bucket.label] = index
        if not progressed:
            break
    return chosen


def observations_query(taxon_id: int, place_id: int, bucket: MonthBucket) -> dict[str, ParamValue]:
    """Build the observations query for one taxon and one seasonal bucket.

    Args:
        taxon_id: Taxon to fetch.
        place_id: Place to scope to.
        bucket: Seasonal stratum being sampled.

    Returns:
        The parameter mapping, licence-filtered server-side.

    Example:
        >>> observations_query(47911, 29, MONTH_BUCKETS[0])["month"]
        [3, 4, 5]
    """
    return {
        "taxon_id": taxon_id,
        "place_id": place_id,
        "quality_grade": "research",
        "photo_license": list(_API_LICENSE_PARAM),
        "month": list(bucket.months),
        "per_page": OBSERVATIONS_PER_BUCKET,
        "order_by": "votes",
        "order": "desc",
    }


def select_photos(
    client: InatClient,
    taxon_id: int,
    scientific_name: str,
    place_id: int,
) -> PhotoSelection:
    """Choose up to eight photos for a taxon, spread across seasons and observers.

    Makes one request per seasonal bucket. Buckets that return nothing are
    normal, and are recorded rather than treated as failures. Within a bucket,
    observations are taken in ascending ID order; no quality ranking is applied.

    Args:
        client: Cached iNaturalist client.
        taxon_id: Taxon to fetch photos for.
        scientific_name: Name, used only in the drop record.
        place_id: Place to scope observations to, so the photos show the plant
            as it looks where the learner is.

    Returns:
        The selection: photos, a drop record when fewer than
        `MIN_PHOTOS_PER_CANDIDATE` survived every rule, and per-bucket yields.
        The rules are never relaxed to reach the floor.

    Raises:
        InatError: If a response is not shaped like an observations response.

    Example:
        >>> select_photos(client, 47911, "Asclepias syriaca", 29)  # doctest: +SKIP
        ... # SKIPPED: needs a populated client. Covered by
        ... # tests/test_inat_pipeline.py against recorded fixtures.
    """
    pooled: dict[str, list[_Selectable]] = {}
    bucket_observations: dict[str, int] = {}
    seen_observations: set[int] = set()

    for bucket in MONTH_BUCKETS:
        response = client.get("observations", observations_query(taxon_id, place_id, bucket))
        results = response.get("results")
        if not isinstance(results, list):
            message = f"observations for taxon {taxon_id} bucket {bucket.label} had no results list"
            raise InatError(message)

        bucket_observations[bucket.label] = len(results)
        usable: list[_Selectable] = []
        for entry in results:
            selectable = _selectable_from(entry, taxon_id, bucket.label)
            # An observation sits in one bucket by its observed month, but a
            # record with an odd date could surface twice; keep the first.
            if selectable is None or selectable.photo.observation_id in seen_observations:
                continue
            seen_observations.add(selectable.photo.observation_id)
            usable.append(selectable)
        pooled[bucket.label] = usable

    chosen = _choose(pooled)

    if len(chosen) < MIN_PHOTOS_PER_CANDIDATE:
        non_empty = sorted(label for label, count in bucket_observations.items() if count)
        _log.info(
            "dropping taxon %d (%s): %d photos after all rules, need %d",
            taxon_id,
            scientific_name,
            len(chosen),
            MIN_PHOTOS_PER_CANDIDATE,
        )
        return PhotoSelection(
            photos=[],
            drop=DropRecord(
                inat_taxon_id=taxon_id,
                name=scientific_name,
                reason="insufficient_licensed_photos",
                detail=(
                    f"{len(chosen)} photos after one-per-observation, max "
                    f"{MAX_PHOTOS_PER_OBSERVER} per observer and "
                    f"{sorted(PERMITTED_LICENSES)} licences; need "
                    f"{MIN_PHOTOS_PER_CANDIDATE}. Buckets with observations: "
                    f"{non_empty or 'none'}"
                ),
            ),
            bucket_observations=bucket_observations,
        )

    return PhotoSelection(photos=chosen, drop=None, bucket_observations=bucket_observations)


def months_represented(photos: list[CandidatePhoto]) -> int:
    """How many seasonal buckets the selected photos span.

    A learner-facing quality signal: M7 surfaces it so somebody can tell a card
    that shows a plant across the year from one that shows it only in flower.

    Args:
        photos: The selected photos. Must not be empty.

    Returns:
        The count of distinct `month_bucket` values.

    Raises:
        ValueError: If `photos` is empty — zero would read as a measurement.

    Example:
        >>> months_represented([])
        Traceback (most recent call last):
            ...
        ValueError: ...
    """
    if not photos:
        message = "months_represented requires at least one photo"
        raise ValueError(message)
    return len({photo.month_bucket for photo in photos})


def distinct_observers(photos: list[CandidatePhoto]) -> int:
    """How many different people took the selected photos.

    A learner-facing quality signal (M7): a card sourced from one person is
    teaching one camera and one habitat as much as it is teaching the plant.

    Args:
        photos: The selected photos. Must not be empty.

    Returns:
        The count of distinct photographer logins.

    Raises:
        ValueError: If `photos` is empty.

    Example:
        >>> distinct_observers([])
        Traceback (most recent call last):
            ...
        ValueError: ...
    """
    if not photos:
        message = "distinct_observers requires at least one photo"
        raise ValueError(message)
    return len({photo.photographer_login for photo in photos})
