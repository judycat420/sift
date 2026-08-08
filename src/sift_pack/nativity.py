"""Two-source nativity: claim on agreement, refuse on conflict.

WHY THIS MODULE EXISTS
----------------------
Neither available source is authoritative on its own, and they fail in opposite
directions. USDA PLANTS has a *scope* error: it records status for the whole
lower 48, so `Robinia pseudoacacia` — native to the Appalachians, listed
invasive by Michigan DNR — reads native on a Michigan card. iNaturalist's place
checklists have an *accuracy* error on contested taxa: they call `Geranium
robertianum` and `Clinopodium vulgare` introduced in Michigan where Michigan
Flora considers both native circumpolar species.

Measured over the 300-taxon Michigan pool, switching from one to the other was
one fix and two regressions — which is the finding that decided this design.
The sources agree on 282 of 285 taxa they both cover. Where they agree, the
claim is stronger than either source alone: two datasets built from different
evidence by different people arrived at the same answer. Where they disagree,
the disagreement is not noise to be broken by a tiebreak rule — all three
Michigan conflicts are genuinely contested taxa that a card should not assert
either way.

So agreement produces a claim, and disagreement produces a drop. Nothing here
picks a winner, prefers a source, or falls back to the one with better coverage:
those would all be ways of turning "the evidence conflicts" into an answer.

CONFIDENCE
----------
Agreement is `high`, capped at the weakest contributing input, per STANDARDS.md
rule 4 — an aggregate is never more confident than what it aggregates. A USDA
tier-3 match (names reconciled loosely) is `medium`, so agreement with it stays
`medium` rather than being promoted by the presence of a second source. A single
source is `medium`, because one dataset's unchecked opinion is exactly the
situation the two-source rule exists to distinguish from.

INVARIANT PROTECTED
-------------------
Every taxon in the pool leaves this module with either a claim naming every
source that supports it, or a reason it has none. The two sets partition the
pool. No claim carries a value that only one source asserted while the other
asserted the opposite, and no claim is built from a value iNaturalist answered
from an ancestor place — that guard lives in `sift_pack.inat.nativity` and this
module consumes only what survives it.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from sift_pack.domains import Axis1Result, NonEmptySources
from sift_pack.inat.nativity import PlaceEstablishment, inat_source_ref
from sift_pack.manifest import Confidence
from sift_pack.usda.reconcile import Reconciliation

__all__ = [
    "Conflict",
    "NativityDecision",
    "NativityOutcome",
    "NativityRejectionReason",
    "NativityReport",
    "SingleSource",
    "combine",
    "decide_pool",
]

_log = logging.getLogger(__name__)

NativityOutcome = Literal["agreement", "conflict", "single_source", "no_source"]
"""How the two sources related to each other for one taxon.

Closed, and exhaustive by construction: the two sources either both spoke and
matched, both spoke and differed, exactly one spoke, or neither did.
"""

NativityRejectionReason = Literal["source_conflict", "no_nativity_source"]
"""Every reason this module declines to make a claim.

`source_conflict` is not a failure of the pipeline. It is the pipeline working:
two sources examined the same taxon and disagreed, and a card that asserted
either answer would be asserting something the evidence does not support.
"""

_CONFIDENCE_RANK: dict[Confidence, int] = {"medium": 0, "high": 1}
"""Ordering used to take the weakest of several confidences. STANDARDS.md rule 4."""

_INAT_CONFIDENCE: Confidence = "high"
"""How much a place-scoped checklist value is worth as an input to agreement.

`high` because it is a direct assertion about the place the card is for, made
against the place's own checklist and refused unless it came from that place
(`sift_pack.inat.nativity`). This is its weight as *one* input; on its own it
still yields a `medium` claim, because a single unchecked source is what the
two-source rule exists to mark.
"""


def _weakest(*confidences: Confidence) -> Confidence:
    """The least confident of several bands.

    Aggregating claims must never produce something more confident than its
    inputs, so agreement between a `high` and a `medium` source is `medium`.
    """
    return min(confidences, key=lambda band: _CONFIDENCE_RANK[band])


@dataclass(frozen=True, slots=True)
class Conflict:
    """One taxon the two sources labelled differently.

    Attributes:
        inat_taxon_id: The taxon.
        scientific_name: Its name, for a readable report.
        usda_value: What USDA PLANTS said about the lower 48.
        inat_value: What the place checklist said about this state.
    """

    inat_taxon_id: int
    scientific_name: str
    usda_value: str
    inat_value: str


@dataclass(frozen=True, slots=True)
class SingleSource:
    """One taxon only one source could speak to.

    Attributes:
        inat_taxon_id: The taxon.
        scientific_name: Its name, for a readable report.
        source: Which source had an answer.
        value: What it said.
        other_reason: Why the other source had nothing, so a reader can tell a
            coverage gap from a refused ancestor-place answer.
    """

    inat_taxon_id: int
    scientific_name: str
    source: str
    value: str
    other_reason: str


@dataclass(frozen=True, slots=True)
class NativityDecision:
    """The two-source outcome for one taxon.

    Exactly one of `claim` and `reason` is set.

    Attributes:
        inat_taxon_id: The taxon.
        scientific_name: Its name.
        outcome: How the sources related.
        claim: The nativity claim, when one could be made.
        reason: Why none could be, otherwise.
        detail: What specifically happened, with values.
        usda_value: What USDA said, when it said anything.
        inat_value: What the place checklist said, when it said anything.
    """

    inat_taxon_id: int
    scientific_name: str
    outcome: NativityOutcome
    claim: Axis1Result | None = None
    reason: NativityRejectionReason | None = None
    detail: str = ""
    usda_value: str | None = None
    inat_value: str | None = None


@dataclass(slots=True)
class NativityReport:
    """What the two-source rule decided across a pool.

    Attributes:
        agreement: Taxa both sources labelled the same way.
        conflicts: Taxa they labelled differently, named.
        single_source: Taxa only one source could label, named.
        no_source: Taxa neither could label.
    """

    agreement: int = 0
    conflicts: list[Conflict] = field(default_factory=list)
    single_source: list[SingleSource] = field(default_factory=list)
    no_source: int = 0

    def summary(self) -> str:
        """One-line summary for a report.

        Returns:
            Human-readable counts.

        Example:
            >>> NativityReport(agreement=282).summary()
            '282 agreed, 0 conflicted, 0 single-source, 0 unsourced'
        """
        return (
            f"{self.agreement} agreed, {len(self.conflicts)} conflicted, "
            f"{len(self.single_source)} single-source, {self.no_source} unsourced"
        )


def combine(
    usda: Reconciliation,
    inat: PlaceEstablishment,
    inat_retrieved_at: datetime,
    place_name: str,
) -> NativityDecision:
    """Decide one taxon's nativity from what both sources said.

    Args:
        usda: The PLANTS reconciliation for this taxon, matched or not.
        inat: The place-scoped establishment status, usable or not. Anything
            iNaturalist answered from an ancestor place has already been refused
            upstream and arrives here as unusable.
        inat_retrieved_at: When the checklist was read, for the source record.
        place_name: Which place's checklist, for the source record.

    Returns:
        A decision carrying either a claim naming every supporting source, or a
        reason there is none. Never both, never neither.

    Example:
        >>> from datetime import UTC, date, datetime
        >>> from sift_pack.domains import Axis1Result
        >>> from sift_pack.inat.nativity import PlaceEstablishment
        >>> from sift_pack.usda.reconcile import Reconciliation, usda_source_ref
        >>> claim = Axis1Result("native", (usda_source_ref(date(2026, 8, 8)),), "high")
        >>> matched = Reconciliation(48662, "Asclepias tuberosa", claim=claim, tier=1)
        >>> listed = PlaceEstablishment(48662, value="native", raw="native", place_id=29)
        >>> out = combine(matched, listed, datetime(2026, 8, 8, tzinfo=UTC), "Michigan")
        >>> out.outcome, out.claim.confidence, len(out.claim.sources)
        ('agreement', 'high', 2)
    """
    usda_claim = usda.claim
    usda_value = usda_claim.value if usda_claim is not None else None
    inat_value = inat.value

    if usda_claim is not None and inat_value is not None:
        if usda_claim.value != inat_value:
            return NativityDecision(
                inat_taxon_id=usda.inat_taxon_id,
                scientific_name=usda.scientific_name,
                outcome="conflict",
                reason="source_conflict",
                detail=(
                    f"USDA PLANTS records {usda_claim.value!r} for the lower 48 and the "
                    f"{place_name} checklist records {inat_value!r} for this state; the "
                    "sources disagree, so no claim is made"
                ),
                usda_value=usda_claim.value,
                inat_value=inat_value,
            )
        sources: NonEmptySources = (
            *usda_claim.sources,
            inat_source_ref(inat_retrieved_at, place_name),
        )
        return NativityDecision(
            inat_taxon_id=usda.inat_taxon_id,
            scientific_name=usda.scientific_name,
            outcome="agreement",
            claim=Axis1Result(
                value=inat_value,
                sources=sources,
                confidence=_weakest(usda_claim.confidence, _INAT_CONFIDENCE),
            ),
            usda_value=usda_claim.value,
            inat_value=inat_value,
        )

    if usda_claim is not None:
        return NativityDecision(
            inat_taxon_id=usda.inat_taxon_id,
            scientific_name=usda.scientific_name,
            outcome="single_source",
            claim=Axis1Result(
                value=usda_claim.value,
                sources=usda_claim.sources,
                confidence="medium",
            ),
            detail=inat.detail or "the place checklist had no usable value",
            usda_value=usda_claim.value,
        )

    if inat_value is not None:
        return NativityDecision(
            inat_taxon_id=usda.inat_taxon_id,
            scientific_name=usda.scientific_name,
            outcome="single_source",
            claim=Axis1Result(
                value=inat_value,
                sources=(inat_source_ref(inat_retrieved_at, place_name),),
                confidence="medium",
            ),
            detail=usda.detail or "PLANTS had no usable status",
            inat_value=inat_value,
        )

    return NativityDecision(
        inat_taxon_id=usda.inat_taxon_id,
        scientific_name=usda.scientific_name,
        outcome="no_source",
        reason="no_nativity_source",
        detail=(
            f"neither source could label this taxon: PLANTS — "
            f"{usda.reason or 'no claim'} ({usda.detail or 'no detail'}); "
            f"{place_name} checklist — {inat.reason or 'no value'} "
            f"({inat.detail or 'no detail'})"
        ),
        usda_value=usda_value,
    )


def decide_pool(
    reconciliations: Mapping[int, Reconciliation],
    establishment: Mapping[int, PlaceEstablishment],
    inat_retrieved_at: datetime,
    place_name: str,
) -> tuple[dict[int, Axis1Result], dict[int, tuple[str, str]], NativityReport]:
    """Apply the two-source rule across a whole pool.

    Args:
        reconciliations: PLANTS outcome per taxon ID.
        establishment: Place-scoped establishment status per taxon ID. A taxon
            missing here is treated as one the checklist had nothing for.
        inat_retrieved_at: When the checklist was read.
        place_name: Which place's checklist.

    Returns:
        Claims by taxon ID, rejection `(reason, detail)` by taxon ID, and the
        report of how the sources related. The first two partition the input.

    Example:
        >>> decide_pool({}, {}, datetime.now(), "Michigan")  # doctest: +SKIP
        ... # SKIPPED: needs reconciled inputs. Covered by tests/test_nativity.py.
    """
    index: dict[int, Axis1Result] = {}
    rejections: dict[int, tuple[str, str]] = {}
    report = NativityReport()

    for taxon_id, usda in reconciliations.items():
        inat = establishment.get(
            taxon_id,
            PlaceEstablishment(
                inat_taxon_id=taxon_id,
                reason="absent_from_inat_response",
                detail="no establishment status was fetched for this taxon",
            ),
        )
        decision = combine(usda, inat, inat_retrieved_at, place_name)

        if decision.outcome == "agreement":
            report.agreement += 1
        elif decision.outcome == "conflict":
            report.conflicts.append(
                Conflict(
                    inat_taxon_id=decision.inat_taxon_id,
                    scientific_name=decision.scientific_name,
                    usda_value=decision.usda_value or "",
                    inat_value=decision.inat_value or "",
                )
            )
        elif decision.outcome == "single_source":
            sole = decision.claim.sources[0].name if decision.claim is not None else ""
            report.single_source.append(
                SingleSource(
                    inat_taxon_id=decision.inat_taxon_id,
                    scientific_name=decision.scientific_name,
                    source=sole,
                    value=decision.usda_value or decision.inat_value or "",
                    other_reason=decision.detail,
                )
            )
        else:
            report.no_source += 1

        if decision.claim is not None:
            index[taxon_id] = decision.claim
        else:
            rejections[taxon_id] = (
                decision.reason or "no_nativity_source",
                decision.detail or "the two-source rule produced no claim and no reason",
            )

    _log.info("two-source nativity: %s", report.summary())
    return index, rejections, report
