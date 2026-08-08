"""Per-place nativity from iNaturalist checklists, and the place guard on it.

WHY THIS MODULE EXISTS
----------------------
USDA PLANTS records native status for the whole lower 48 and nothing finer
(`docs/decisions.md`, 2026-08-08). iNaturalist records it per *place*: a taxon
listed on the Michigan Check List carries a Michigan-scoped establishment
status, which is the scope a Michigan card actually claims. That is the reason
to read this source at all.

It comes with a trap. iNaturalist answers a query for one place from the
nearest ancestor place that has a listing, so asking about Arizona can hand back
North America's answer — the probe recorded exactly that for `Elaeagnus
umbellata`, `Lythrum salicaria` and `Solanum dulcamara`. The response says which
place it answered from, in `establishment_means.place`, and nothing else in the
payload distinguishes the two cases. A caller that reads only the value gets a
continent-scoped claim wearing a state's name, which is the same class of error
this milestone exists to remove from the USDA path.

INVARIANT PROTECTED
-------------------
A value is returned only when `establishment_means.place.id` is the place that
was asked about. An answer inherited from an ancestor place is refused and
counted under `place_not_state_scoped`, never accepted and never downgraded into
a lower confidence band. Michigan needs this guard zero times out of 300 taxa
and Arizona would need it constantly; the guard does not depend on knowing
which state it is protecting.

Absence is left absent. Six of Michigan's 300 taxa have no Michigan listing
while having a United States or North America one, and this module reports those
as having no value rather than reaching up the place tree for something to say.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Literal

from sift_pack.inat.client import InatClient
from sift_pack.manifest import SourceRef

__all__ = [
    "INAT_SOURCE_NAME",
    "INAT_SOURCE_URL",
    "TAXA_BATCH_SIZE",
    "InatRejectionReason",
    "PlaceEstablishment",
    "fetch_establishment",
    "inat_source_ref",
]

_log = logging.getLogger(__name__)

INAT_SOURCE_NAME = "iNaturalist place checklist"
"""Recorded as a `SourceRef.name` on every claim this module contributes to.

Named for the checklist rather than for iNaturalist as a whole, because the pack
already cites "iNaturalist API" as the source of its photos and observation
counts. Those are different assertions by different contributors, and collapsing
them into one name would make a nativity claim look as though it were backed by
the observation record.
"""

INAT_SOURCE_URL = "https://api.inaturalist.org/v1/taxa"

TAXA_BATCH_SIZE = 30
"""Taxa per `/v1/taxa` request; the endpoint's own page size.

Asking for more silently returns the first 30, so this is a property of the API
rather than a tuning knob.
"""

InatRejectionReason = Literal[
    "no_inat_listing",
    "place_not_state_scoped",
    "uninterpretable_establishment_means",
    "absent_from_inat_response",
]
"""Every reason iNaturalist can fail to yield a per-place nativity value.

Closed, so the drop accounting cannot grow a category through a typo.
"""

_VALUES: dict[str, str] = {
    "native": "native",
    "endemic": "native",
    "introduced": "introduced",
}
"""iNaturalist establishment status to the plants domain's axis-1 vocabulary.

`endemic` maps to native because it is a strictly stronger statement of the same
fact — native, and found nowhere else. Nothing else is mapped. iNaturalist also
uses `naturalised`, `invasive`, `managed` and `unknown`, and while the first two
usually imply introduction they do not always: "invasive" is applied to
aggressive natives too. Each becomes a rejection with its own reason rather than
being rounded to the nearer of native and introduced, exactly as PLANTS' own
hedges are (`sift_pack.usda.reconcile`).
"""


@dataclass(frozen=True, slots=True)
class PlaceEstablishment:
    """What iNaturalist says about one taxon in one place, or why it says nothing.

    Exactly one of `value` and `reason` is set. Both unset would be a taxon that
    was neither answered for nor accounted for, which is the state this module
    exists to make impossible.

    Attributes:
        inat_taxon_id: The taxon asked about.
        value: The nativity value in the domain's vocabulary, when the answer was
            usable and place-scoped.
        raw: What iNaturalist actually said, verbatim, even when unusable — so a
            rejection can be audited without re-fetching.
        place_id: The place iNaturalist answered from. Equal to the place asked
            about whenever `value` is set; an ancestor's ID when the answer was
            refused for being inherited.
        place_name: That place's name, for a readable report.
        reason: Why no value could be taken.
        detail: What specifically was wrong, with values.
    """

    inat_taxon_id: int
    value: str | None = None
    raw: str | None = None
    place_id: int | None = None
    place_name: str | None = None
    reason: InatRejectionReason | None = None
    detail: str = ""

    @property
    def usable(self) -> bool:
        """Whether this carries a place-scoped nativity value.

        Returns:
            True when a claim may be derived from it.

        Example:
            >>> PlaceEstablishment(1, reason="no_inat_listing").usable
            False
        """
        return self.value is not None


def inat_source_ref(retrieved_at: datetime, place_name: str) -> SourceRef:
    """Build the provenance record for a claim taken from a place checklist.

    The version is the retrieval date, for the same reason PLANTS claims are
    versioned that way (`docs/decisions.md`, 2026-08-08): iNaturalist publishes
    no version, edition or revision stamp for a place checklist, and a listing
    can be edited by a curator at any time with no trace in the response. A
    retrieval date is weaker than a publication date and is recorded as the best
    available answer rather than a good one.

    Args:
        retrieved_at: When Sift read the checklist. Timezone-aware.
        place_name: Which place's checklist, e.g. `"Michigan"`. Carried in the
            version string because the same source name covers every place, and
            the place is the whole point of the claim.

    Returns:
        The source reference to attach to claims from this checklist.

    Example:
        >>> from datetime import UTC, datetime
        >>> ref = inat_source_ref(datetime(2026, 8, 8, tzinfo=UTC), "Michigan")
        >>> ref.name, ref.version
        ('iNaturalist place checklist', 'Michigan checklist retrieved 2026-08-08')
    """
    stamp: date = retrieved_at.astimezone(UTC).date()
    return SourceRef(
        name=INAT_SOURCE_NAME,
        version=f"{place_name} checklist retrieved {stamp.isoformat()}",
        retrieved_at=retrieved_at,
        url=INAT_SOURCE_URL,
    )


def _read_one(result: dict[str, Any], place_id: int) -> PlaceEstablishment | None:
    """Classify one taxon record from a `/v1/taxa` response.

    The place check happens before the value is looked at, deliberately: an
    inherited answer is refused whatever it says, so there is no ordering in
    which a continent-scoped value could be read first and rescued later.

    Returns `None` for a record carrying no integer ID. Such a record cannot be
    attributed to any taxon, so there is nothing to record it against; the
    caller logs it, and the taxon it should have answered for falls out as
    `absent_from_inat_response` — dropped and counted, per STANDARDS.md rule 5.
    """
    taxon_id = result.get("id")
    if not isinstance(taxon_id, int):
        return None

    means = result.get("establishment_means")
    if not isinstance(means, dict):
        return PlaceEstablishment(
            inat_taxon_id=taxon_id,
            reason="no_inat_listing",
            detail=f"no checklist covers this taxon at place {place_id}",
        )

    raw = means.get("establishment_means")
    raw_value = raw if isinstance(raw, str) else None
    place = means.get("place") if isinstance(means.get("place"), dict) else None
    answered_id = place.get("id") if place else None
    answered_name = place.get("name") if place else None
    answered_name = answered_name if isinstance(answered_name, str) else None

    if answered_id != place_id:
        return PlaceEstablishment(
            inat_taxon_id=taxon_id,
            raw=raw_value,
            place_id=answered_id if isinstance(answered_id, int) else None,
            place_name=answered_name,
            reason="place_not_state_scoped",
            detail=(
                f"iNaturalist answered place {place_id} from {answered_name or 'an ancestor'} "
                f"(place {answered_id}), so {raw_value!r} is not a claim about place {place_id}"
            ),
        )

    value = _VALUES.get(raw_value) if raw_value is not None else None
    if value is None:
        return PlaceEstablishment(
            inat_taxon_id=taxon_id,
            raw=raw_value,
            place_id=place_id,
            place_name=answered_name,
            reason="uninterpretable_establishment_means",
            detail=(
                f"iNaturalist reports establishment_means {raw_value!r}, which is neither "
                "native nor introduced in this domain's vocabulary"
            ),
        )

    return PlaceEstablishment(
        inat_taxon_id=taxon_id,
        value=value,
        raw=raw_value,
        place_id=place_id,
        place_name=answered_name,
    )


def fetch_establishment(
    client: InatClient,
    taxon_ids: Sequence[int],
    place_id: int,
) -> dict[int, PlaceEstablishment]:
    """Read every taxon's establishment status for one place.

    Args:
        client: Cached iNaturalist client. Batches of `TAXA_BATCH_SIZE` go out
            as one request each, so a 300-taxon state costs ten.
        taxon_ids: Taxa to ask about. Order is not significant; duplicates are
            collapsed.
        place_id: The place the answer must be scoped to. An answer inherited
            from an ancestor place is refused, not accepted.

    Returns:
        One `PlaceEstablishment` per requested taxon ID, carrying either a
        place-scoped value or the reason there is none. Every requested ID is
        present: a taxon the API omitted is recorded as
        `absent_from_inat_response` rather than silently missing.

    Raises:
        ValueError: If a returned taxon record carries no integer ID, which
            would make the response unattributable to a request.
        sift_pack.inat.client.InatError: If a request or a cached entry cannot
            be used.

    Example:
        >>> fetch_establishment(client, [48662], 29)  # doctest: +SKIP
        ... # SKIPPED: performs requests. Covered by tests/test_inat_nativity.py
        ... # against recorded fixtures, including an ancestor-place response.
    """
    wanted = sorted(set(taxon_ids))
    found: dict[int, PlaceEstablishment] = {}
    unattributable = 0

    for start in range(0, len(wanted), TAXA_BATCH_SIZE):
        batch = wanted[start : start + TAXA_BATCH_SIZE]
        response = client.get("taxa_by_id", {"ids": batch, "preferred_place_id": place_id})
        results = response.get("results")
        if not isinstance(results, list):
            _log.warning(
                "taxa_by_id returned no results list for %d taxa at place %d", len(batch), place_id
            )
            continue
        for result in results:
            if not isinstance(result, dict):
                unattributable += 1
                continue
            outcome = _read_one(result, place_id)
            if outcome is None:
                unattributable += 1
                continue
            found[outcome.inat_taxon_id] = outcome

    for taxon_id in wanted:
        if taxon_id not in found:
            found[taxon_id] = PlaceEstablishment(
                inat_taxon_id=taxon_id,
                reason="absent_from_inat_response",
                detail=f"iNaturalist returned no record for taxon {taxon_id}",
            )

    if unattributable:
        _log.warning(
            "%d record(s) from place %d carried no usable taxon id and were dropped; "
            "the taxa they should have answered for are reported as absent",
            unattributable,
            place_id,
        )

    usable = sum(1 for outcome in found.values() if outcome.usable)
    inherited = sum(1 for o in found.values() if o.reason == "place_not_state_scoped")
    _log.info(
        "iNaturalist place %d: %d/%d taxa have a place-scoped value (%d refused as inherited)",
        place_id,
        usable,
        len(wanted),
        inherited,
    )
    return found
