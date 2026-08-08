"""Tests for the two-source rule: claim on agreement, refuse on conflict.

The point of these is not that the combination logic runs. It is that a
disagreement between the two sources produces *no card*, and that the three taxa
the M4.1 probe found disagreeing in Michigan are named here — because they are
the known contested set, and a change that quietly starts asserting one of them
is the regression this milestone exists to prevent.
"""

from __future__ import annotations

from datetime import UTC, date, datetime

import pytest

from sift_pack.domains import Axis1Result
from sift_pack.inat.nativity import INAT_SOURCE_NAME, PlaceEstablishment
from sift_pack.nativity import NativityDecision, combine, decide_pool
from sift_pack.usda.reconcile import USDA_SOURCE_NAME, Reconciliation, usda_source_ref

WHEN = datetime(2026, 8, 8, tzinfo=UTC)
VERSION = date(2026, 8, 8)
PLACE = "Michigan"
MICHIGAN_PLACE_ID = 29

# The three taxa the probe found the sources disagreeing on across the 300-taxon
# Michigan pool. USDA calls all three L48-native; the Michigan checklist calls
# all three introduced. Michigan DNR lists Robinia pseudoacacia invasive, while
# Michigan Flora considers the other two native circumpolar species — so one of
# these is USDA being wrong and two are iNaturalist being wrong, which is
# precisely why neither source wins and all three are refused.
CONTESTED = {
    "Robinia pseudoacacia": 56088,
    "Geranium robertianum": 55925,
    "Clinopodium vulgare": 84281,
}


def usda(
    value: str | None, confidence: str = "high", reason: str = "no_plants_record"
) -> Reconciliation:
    """A PLANTS reconciliation that either matched or did not."""
    if value is None:
        return Reconciliation(1, "Test taxon", reason=reason, detail="nothing matched")  # type: ignore[arg-type]
    claim = Axis1Result(value, (usda_source_ref(VERSION),), confidence)  # type: ignore[arg-type]
    return Reconciliation(1, "Test taxon", claim=claim, tier=1)


def inat(value: str | None, reason: str = "no_inat_listing") -> PlaceEstablishment:
    """A place-scoped establishment status, usable or not."""
    if value is None:
        return PlaceEstablishment(1, reason=reason, detail="no listing")  # type: ignore[arg-type]
    return PlaceEstablishment(1, value=value, raw=value, place_id=MICHIGAN_PLACE_ID)


def decide(
    usda_value: str | None, inat_value: str | None, confidence: str = "high"
) -> NativityDecision:
    """Run one taxon through the two-source rule."""
    return combine(usda(usda_value, confidence), inat(inat_value), WHEN, PLACE)


# --- agreement -----------------------------------------------------------------


@pytest.mark.parametrize("value", ["native", "introduced"])
def test_agreement_claims_the_value_and_names_both_sources(value: str) -> None:
    out = decide(value, value)
    assert out.outcome == "agreement"
    assert out.claim is not None
    assert out.claim.value == value
    assert [s.name for s in out.claim.sources] == [USDA_SOURCE_NAME, INAT_SOURCE_NAME]


def test_agreement_is_high_confidence() -> None:
    claim = decide("native", "native").claim
    assert claim is not None
    assert claim.confidence == "high"


def test_agreement_with_a_loose_usda_match_is_not_promoted_to_high() -> None:
    # STANDARDS.md rule 4: an aggregate is never more confident than its
    # weakest input. A tier-3 PLANTS match is `medium`, and a second source
    # agreeing with it does not make the name match any less loose.
    out = decide("native", "native", confidence="medium")
    assert out.outcome == "agreement"
    assert out.claim is not None
    assert out.claim.confidence == "medium"


def test_each_source_carries_its_own_version() -> None:
    claim = decide("native", "native").claim
    assert claim is not None
    versions = {s.name: s.version for s in claim.sources}
    assert versions[USDA_SOURCE_NAME] == "retrieved 2026-08-08"
    assert versions[INAT_SOURCE_NAME] == "Michigan checklist retrieved 2026-08-08"


# --- conflict: the refusal this milestone exists for ---------------------------


def test_a_conflict_produces_no_claim() -> None:
    out = decide("native", "introduced")
    assert out.outcome == "conflict"
    assert out.claim is None
    assert out.reason == "source_conflict"


def test_a_conflict_is_refused_in_both_directions() -> None:
    # Neither source wins. A rule that preferred the per-place source would
    # assert Geranium robertianum introduced; one that preferred USDA would
    # assert Robinia pseudoacacia native. Both are wrong.
    assert decide("native", "introduced").claim is None
    assert decide("introduced", "native").claim is None


def test_the_conflict_detail_records_what_each_source_said() -> None:
    out = decide("native", "introduced")
    assert "native" in out.detail
    assert "introduced" in out.detail
    assert PLACE in out.detail
    assert out.usda_value == "native"
    assert out.inat_value == "introduced"


def test_the_known_contested_michigan_taxa_are_all_refused() -> None:
    # USDA L48-native, Michigan checklist introduced — the shape the probe found
    # for all three. Named here because a change that starts asserting any of
    # them is the regression this milestone exists to prevent.
    reconciliations = {}
    establishment = {}
    for name, taxon_id in CONTESTED.items():
        claim = Axis1Result("native", (usda_source_ref(VERSION),), "high")
        reconciliations[taxon_id] = Reconciliation(taxon_id, name, claim=claim, tier=1)
        establishment[taxon_id] = PlaceEstablishment(
            taxon_id, value="introduced", raw="introduced", place_id=MICHIGAN_PLACE_ID
        )

    index, rejections, report = decide_pool(reconciliations, establishment, WHEN, PLACE)

    assert index == {}
    assert set(rejections) == set(CONTESTED.values())
    assert {reason for reason, _ in rejections.values()} == {"source_conflict"}
    assert {c.scientific_name for c in report.conflicts} == set(CONTESTED)
    for conflict in report.conflicts:
        assert (conflict.usda_value, conflict.inat_value) == ("native", "introduced")


# --- single source --------------------------------------------------------------


def test_only_usda_yields_a_medium_claim_naming_only_usda() -> None:
    out = decide("native", None)
    assert out.outcome == "single_source"
    assert out.claim is not None
    assert out.claim.confidence == "medium"
    assert [s.name for s in out.claim.sources] == [USDA_SOURCE_NAME]


def test_only_the_checklist_yields_a_medium_claim_naming_only_the_checklist() -> None:
    out = combine(usda(None), inat("introduced"), WHEN, PLACE)
    assert out.outcome == "single_source"
    assert out.claim is not None
    assert out.claim.confidence == "medium"
    assert [s.name for s in out.claim.sources] == [INAT_SOURCE_NAME]


def test_a_single_source_claim_is_never_high_even_from_a_tier_1_match() -> None:
    # One dataset's unchecked opinion is exactly what the two-source rule exists
    # to distinguish from agreement, so it cannot reach the top band.
    claim = decide("native", None, confidence="high").claim
    assert claim is not None
    assert claim.confidence == "medium"


def test_a_single_source_claim_records_why_the_other_source_was_silent() -> None:
    out = combine(usda("native"), inat(None, reason="place_not_state_scoped"), WHEN, PLACE)
    assert out.detail


def test_an_inherited_place_answer_counts_as_silence_not_as_a_source() -> None:
    # The guard upstream already refused it; here it must not quietly become
    # the second source that turns a medium claim into a high one.
    refused = PlaceEstablishment(
        1,
        raw="introduced",
        place_id=97394,
        place_name="North America",
        reason="place_not_state_scoped",
        detail="answered from North America",
    )
    out = combine(usda("native"), refused, WHEN, PLACE)
    assert out.outcome == "single_source"
    assert out.claim is not None
    assert out.claim.confidence == "medium"
    assert [s.name for s in out.claim.sources] == [USDA_SOURCE_NAME]


# --- neither source --------------------------------------------------------------


def test_neither_source_produces_no_claim_and_a_reason() -> None:
    out = combine(usda(None), inat(None), WHEN, PLACE)
    assert out.outcome == "no_source"
    assert out.claim is None
    assert out.reason == "no_nativity_source"


def test_the_no_source_detail_names_what_each_source_said_instead() -> None:
    out = combine(usda(None, reason="no_plants_record"), inat(None), WHEN, PLACE)
    assert "no_plants_record" in out.detail
    assert "no_inat_listing" in out.detail


# --- the pool-level partition ----------------------------------------------------


def test_the_pool_is_partitioned_into_claims_and_reasons() -> None:
    reconciliations = {
        1: Reconciliation(
            1, "Agrees", claim=Axis1Result("native", (usda_source_ref(VERSION),), "high"), tier=1
        ),
        2: Reconciliation(
            2, "Conflicts", claim=Axis1Result("native", (usda_source_ref(VERSION),), "high"), tier=1
        ),
        3: Reconciliation(
            3, "UsdaOnly", claim=Axis1Result("native", (usda_source_ref(VERSION),), "high"), tier=1
        ),
        4: Reconciliation(4, "Neither", reason="no_plants_record", detail="nothing"),
        5: Reconciliation(5, "InatOnly", reason="no_plants_record", detail="nothing"),
    }
    establishment = {
        1: PlaceEstablishment(1, value="native", raw="native", place_id=MICHIGAN_PLACE_ID),
        2: PlaceEstablishment(2, value="introduced", raw="introduced", place_id=MICHIGAN_PLACE_ID),
        3: PlaceEstablishment(3, reason="no_inat_listing", detail="none"),
        4: PlaceEstablishment(4, reason="no_inat_listing", detail="none"),
        5: PlaceEstablishment(5, value="introduced", raw="introduced", place_id=MICHIGAN_PLACE_ID),
    }
    index, rejections, report = decide_pool(reconciliations, establishment, WHEN, PLACE)

    assert set(index) | set(rejections) == set(reconciliations)
    assert not set(index) & set(rejections)
    assert set(index) == {1, 3, 5}
    assert report.agreement == 1
    assert len(report.conflicts) == 1
    assert len(report.single_source) == 2
    assert report.no_source == 1


def test_a_taxon_with_no_fetched_establishment_is_treated_as_silence_not_agreement() -> None:
    # A missing entry must not read as "the checklist agreed"; that would turn a
    # fetch failure into a high-confidence claim.
    claim = Axis1Result("native", (usda_source_ref(VERSION),), "high")
    index, _, report = decide_pool(
        {7: Reconciliation(7, "Missing", claim=claim, tier=1)}, {}, WHEN, PLACE
    )
    assert index[7].confidence == "medium"
    assert report.agreement == 0
    assert len(report.single_source) == 1


def test_the_report_summary_counts_every_outcome() -> None:
    _, _, report = decide_pool({}, {}, WHEN, PLACE)
    assert report.summary() == "0 agreed, 0 conflicted, 0 single-source, 0 unsourced"
