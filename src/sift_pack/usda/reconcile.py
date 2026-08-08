"""Matching iNaturalist taxa to USDA PLANTS records, and refusing to when unsure.

WHY THIS MODULE EXISTS
----------------------
iNaturalist and USDA maintain independent taxonomies that agree on most names
and disagree on the interesting ones (`docs/sources.md`). There is no shared
identifier between them, so the join is by name — and a name join is exactly
the kind of operation that produces a confident wrong answer, because a name
that fails to match looks the same as a name that matches the wrong record.

Every rule here is therefore named, ordered, and recorded on the claim it
produces. A caller can ask of any card in a finished pack: which rule matched
this, and how sure was it.

THE THREE TIERS
---------------
1. The iNaturalist name is a PLANTS accepted species name.        -> high
2. The iNaturalist name is a PLANTS synonym; follow it to the     -> high
   accepted taxon.
3. The names match once authority and infraspecific rank are      -> medium
   normalised away.

WHAT PLANTS ACTUALLY SAYS, AND WHAT IT DOES NOT
-----------------------------------------------
PLANTS records native status by *region* — `L48`, `CAN`, `AK`, `HI` — and not by
state. There is no per-state native status in the database at all. So the claim
Sift can honestly derive is "USDA records this taxon as native within the lower
48 states", which is not the same as "native to Michigan": a plant native to the
Sonoran Desert and naturalised in Michigan is `L48 (N)`. This is a real and
unfixable-from-PLANTS limitation, recorded on every claim through the source
name and documented at `docs/decisions.md`, 2026-08-08.

INVARIANT PROTECTED
-------------------
`reconcile` returns a claim or `None`, and `None` is returned whenever the
answer is not unambiguous: no match, several accepted taxa matching one name,
a status code PLANTS itself marks uncertain, or a taxon whose regions disagree.
Nothing here has a fallback, a default, or a most-likely guess.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Literal

from sift_pack.domains import Axis1Result
from sift_pack.manifest import Confidence, SourceRef
from sift_pack.usda.client import PLANTS_API, PlantsClient, PlantsError, PlantsRecord

__all__ = [
    "NATIVITY_REGION",
    "USDA_SOURCE_NAME",
    "MatchTier",
    "Reconciliation",
    "RejectionReason",
    "reconcile",
    "usda_source_ref",
]

_log = logging.getLogger(__name__)

USDA_SOURCE_NAME = "USDA PLANTS"
"""Recorded as a `SourceRef.name` on every claim derived here."""

NATIVITY_REGION = "L48"
"""The PLANTS region Sift reads native status from.

The lower 48 states. Michigan sits inside it, and PLANTS offers nothing finer —
see the module docstring. A pack for Alaska or Hawaii would need a different
region and would be a different decision, not a parameter tweak.
"""

MatchTier = Literal[1, 2, 3]

RejectionReason = Literal[
    "no_plants_record",
    "ambiguous_plants_match",
    "no_native_status",
    "uncertain_native_status",
    "conflicting_native_status",
    "plants_lookup_failed",
]
"""Every reason a taxon can fail to acquire a nativity claim.

Closed, so the unmatched report cannot grow a category through a typo.
"""

_HYBRID_SIGN = "\u00d7"  # U+00D7, the botanical hybrid sign
_NATIVE = "N"
_INTRODUCED = "I"

_VALUES: dict[str, str] = {_NATIVE: "native", _INTRODUCED: "introduced"}
"""PLANTS status code to the plants domain's axis-1 vocabulary.

Only these two codes yield a claim. PLANTS also uses `N?` and `I?` for uncertain,
`NI` for taxa both native and introduced within the region, `W` for waifs and
`GP` for taxa known only from cultivation. None of those is a fact a learner can
be shown as "native" or "introduced", so each becomes a rejection with its own
reason rather than being coerced into the nearer of the two.
"""

_TIER_CONFIDENCE: dict[MatchTier, Confidence] = {1: "high", 2: "high", 3: "medium"}


def usda_source_ref(source_version: date) -> SourceRef:
    """Build the provenance record for a claim derived from PLANTS.

    The version is the retrieval date because PLANTS publishes no edition or
    dataset version through its services API — see `docs/decisions.md`,
    2026-08-08, "Nativity claims are versioned by retrieval date".

    Args:
        source_version: The date the PLANTS records behind the claim were read.

    Returns:
        The source reference to attach to claims from that retrieval.

    Example:
        >>> from datetime import date
        >>> usda_source_ref(date(2026, 8, 8)).version
        'retrieved 2026-08-08'
    """
    return SourceRef(
        name=USDA_SOURCE_NAME,
        version=f"retrieved {source_version.isoformat()}",
        retrieved_at=datetime.combine(source_version, time.min, tzinfo=UTC),
        url=PLANTS_API,
    )


@dataclass(frozen=True, slots=True)
class Reconciliation:
    """The outcome of matching one iNaturalist taxon to PLANTS.

    Exactly one of `claim` and `reason` is set. A `Reconciliation` with neither
    would be a taxon that was neither accepted nor accounted for, which is the
    state this module exists to make impossible.

    Attributes:
        inat_taxon_id: The taxon that was reconciled.
        scientific_name: Its iNaturalist name, as searched.
        claim: The nativity claim, when one could be made.
        tier: Which matching rule produced the claim.
        plants_symbol: The PLANTS record matched, for auditing.
        plants_name: That record's accepted name.
        reason: Why no claim could be made.
        detail: What specifically was wrong, with values.
    """

    inat_taxon_id: int
    scientific_name: str
    claim: Axis1Result | None = None
    tier: MatchTier | None = None
    plants_symbol: str | None = None
    plants_name: str | None = None
    reason: RejectionReason | None = None
    detail: str = ""

    @property
    def matched(self) -> bool:
        """Whether a claim was produced.

        Returns:
            True when this taxon may become a card.

        Example:
            >>> Reconciliation(1, "X", reason="no_plants_record").matched
            False
        """
        return self.claim is not None


def _normalise(name: str) -> str:
    """Loosen a name for tier-3 comparison.

    Case, the hybrid sign and repeated whitespace are the differences that are
    never meaningful between the two taxonomies. Anything else — a different
    epithet, a different genus — must not be normalised away, because that is
    the difference between two species.
    """
    return " ".join(name.replace(_HYBRID_SIGN, "").lower().split())


def _species_records(records: list[PlantsRecord], name: str) -> list[PlantsRecord]:
    """PLANTS records whose binomial is exactly this name, at species rank."""
    return [r for r in records if r.binomial == name and not r.is_infraspecific]


def _loose_records(records: list[PlantsRecord], name: str) -> list[PlantsRecord]:
    """PLANTS records matching once case and hybrid signs are normalised away."""
    wanted = _normalise(name)
    return [r for r in records if _normalise(r.binomial) == wanted and not r.is_infraspecific]


def _classify(statuses: dict[str, str]) -> tuple[str | None, RejectionReason | None, str]:
    """Turn PLANTS' region statuses into a nativity value, or a rejection.

    Only the `L48` entry is consulted; see the module docstring on why nothing
    finer exists.
    """
    if not statuses:
        return None, "no_native_status", "PLANTS records no native status for this taxon"

    code = statuses.get(NATIVITY_REGION)
    if code is None:
        regions = ", ".join(sorted(statuses)) or "none"
        return (
            None,
            "no_native_status",
            f"PLANTS has no {NATIVITY_REGION} status; regions present: {regions}",
        )

    value = _VALUES.get(code)
    if value is not None:
        return value, None, ""

    if "?" in code:
        return (
            None,
            "uncertain_native_status",
            f"PLANTS reports {NATIVITY_REGION} status {code!r}, which it marks uncertain",
        )
    if _NATIVE in code and _INTRODUCED in code:
        return (
            None,
            "conflicting_native_status",
            (
                f"PLANTS reports {NATIVITY_REGION} status {code!r}: native in part of the "
                "region and introduced in another, which cannot be shown as one label"
            ),
        )
    return (
        None,
        "uncertain_native_status",
        f"PLANTS reports {NATIVITY_REGION} status {code!r}, which is neither native nor introduced",
    )


def reconcile(
    client: PlantsClient,
    inat_taxon_id: int,
    scientific_name: str,
    source_version: date,
) -> Reconciliation:
    """Match one iNaturalist taxon to PLANTS and derive its nativity claim.

    Args:
        client: Cached PLANTS client.
        inat_taxon_id: The taxon being reconciled; the primary key throughout.
        scientific_name: Its iNaturalist scientific name.
        source_version: Which PLANTS retrieval the claim is derived from. Recorded
            on the claim so a card can be re-checked when PLANTS changes.

    Returns:
        A `Reconciliation` carrying either a claim or a reason there is none.
        Never both, never neither.

    Example:
        >>> import tempfile
        >>> from datetime import date
        >>> from pathlib import Path
        >>> from sift_pack.usda.client import PlantsClient
        >>> class Fake:
        ...     def get_json(self, url):
        ...         if "PlantSearch" in url:
        ...             name = "<i>Asclepias tuberosa</i> L."
        ...             return [{"Plant": {"Symbol": "ASTU", "ScientificName": name}}]
        ...         return {"Symbol": "ASTU", "NativeStatuses": [{"Region": "L48", "Status": "N"}]}
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     client = PlantsClient(Path(tmp), Fake())
        ...     out = reconcile(client, 48662, "Asclepias tuberosa", date(2026, 8, 8))
        ...     (out.claim.value, out.claim.confidence, out.tier)
        ('native', 'high', 1)
    """
    try:
        records = client.search(scientific_name)
    except PlantsError as exc:
        return Reconciliation(
            inat_taxon_id=inat_taxon_id,
            scientific_name=scientific_name,
            reason="plants_lookup_failed",
            detail=str(exc),
        )

    tier: MatchTier
    exact = _species_records(records, scientific_name)
    accepted = [r for r in exact if not r.is_synonym]
    synonyms = [r for r in exact if r.is_synonym]

    if accepted:
        tier, chosen = 1, accepted
    elif synonyms:
        tier, chosen = 2, synonyms
    else:
        loose = _loose_records(records, scientific_name)
        if not loose:
            return Reconciliation(
                inat_taxon_id=inat_taxon_id,
                scientific_name=scientific_name,
                reason="no_plants_record",
                detail=(
                    f"PLANTS returned {len(records)} record(s) for {scientific_name!r}, "
                    "none of them a species-rank name match"
                ),
            )
        tier, chosen = 3, loose

    targets = sorted({record.accepted_symbol or record.symbol for record in chosen})

    # Several accepted records can share one binomial — PLANTS lists later
    # homonyms (`Monarda fistulosa Sims, nom. illeg.`) as taxa of their own.
    # Ambiguity only matters if it changes the answer, so each candidate is
    # classified and the result is accepted when they agree. Picking one would
    # be a guess; observing that every choice gives the same label is not.
    outcomes: dict[str, tuple[str | None, RejectionReason | None, str]] = {}
    for candidate in targets:
        try:
            outcomes[candidate] = _classify(client.native_statuses(candidate))
        except PlantsError as exc:
            return Reconciliation(
                inat_taxon_id=inat_taxon_id,
                scientific_name=scientific_name,
                plants_symbol=candidate,
                reason="plants_lookup_failed",
                detail=str(exc),
            )

    values = {value for value, _, _ in outcomes.values()}
    if len(values) != 1:
        rendered = ", ".join(f"{sym}={out[0] or out[1]}" for sym, out in sorted(outcomes.items()))
        return Reconciliation(
            inat_taxon_id=inat_taxon_id,
            scientific_name=scientific_name,
            reason="ambiguous_plants_match",
            detail=(
                f"{scientific_name!r} matched {len(targets)} accepted PLANTS taxa that "
                f"disagree ({rendered}); picking one would be a guess"
            ),
        )

    symbol = targets[0]
    value, reason, detail = outcomes[symbol]
    if len(targets) > 1 and value is not None:
        _log.info(
            "%s matched %d homonymous PLANTS taxa (%s), all %s",
            scientific_name,
            len(targets),
            ", ".join(targets),
            value,
        )
    plants_name = next(
        (r.scientific_name for r in chosen if r.symbol == symbol), chosen[0].scientific_name
    )
    if value is None:
        return Reconciliation(
            inat_taxon_id=inat_taxon_id,
            scientific_name=scientific_name,
            plants_symbol=symbol,
            plants_name=plants_name,
            reason=reason,
            detail=detail,
        )

    return Reconciliation(
        inat_taxon_id=inat_taxon_id,
        scientific_name=scientific_name,
        tier=tier,
        plants_symbol=symbol,
        plants_name=plants_name,
        claim=Axis1Result(
            value=value,
            sources=(usda_source_ref(source_version),),
            confidence=_TIER_CONFIDENCE[tier],
        ),
    )
