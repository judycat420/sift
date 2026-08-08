"""Tests for the domain seam: the provenance wrapper and the `None` contract."""

from __future__ import annotations

import dataclasses
import re
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError as PydanticValidationError

from sift_pack.domains import Axis1Result, TaxonDomain
from sift_pack.domains.birds import BirdsDomain
from sift_pack.domains.plants import PlantsDomain
from sift_pack.domains.registry import DOMAINS, UnknownDomainError, resolve_domain
from sift_pack.manifest import SourceRef, Taxon

WHEN = datetime(2026, 7, 1, tzinfo=UTC)
SOURCE = SourceRef(
    name="USDA PLANTS",
    version="2026-07-01",
    retrieved_at=WHEN,
    url="https://plantsservices.sc.egov.usda.gov/api/",
)
OTHER_SOURCE = SourceRef(
    name="iNaturalist place checklist",
    version="Michigan checklist retrieved 2026-07-01",
    retrieved_at=WHEN,
    url="https://api.inaturalist.org/v1/taxa",
)


def _accepts_domain(domain: TaxonDomain) -> str:
    """Exists so mypy checks protocol conformance statically, not just at runtime."""
    return domain.slug


# --- Axis1Result: provenance is structural, not optional ---------------------


def test_axis1_result_requires_every_provenance_field() -> None:
    # Each of these is also a mypy error; see test_type_level_guarantees.py for
    # the static half. This is the runtime backstop.
    with pytest.raises(TypeError):
        Axis1Result("native")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        Axis1Result("native", (SOURCE,))  # type: ignore[call-arg]


def test_axis1_result_constructs_with_full_provenance() -> None:
    claim = Axis1Result(value="native", sources=(SOURCE,), confidence="high")
    assert claim.value == "native"
    assert [source.name for source in claim.sources] == ["USDA PLANTS"]
    assert claim.confidence == "high"
    assert claim.sources[0].version == "2026-07-01"


def test_axis1_result_carries_every_agreeing_source() -> None:
    # The M4.1 shape: a claim two sources agreed on names both, each with its
    # own version, so a reader can check the confidence against the evidence.
    claim = Axis1Result(value="native", sources=(SOURCE, OTHER_SOURCE), confidence="high")
    assert [source.name for source in claim.sources] == [
        "USDA PLANTS",
        "iNaturalist place checklist",
    ]
    assert len({source.version for source in claim.sources}) == 2


def test_axis1_result_is_frozen() -> None:
    claim = Axis1Result("native", (SOURCE,), "high")
    with pytest.raises(dataclasses.FrozenInstanceError):
        claim.value = "a guess"  # type: ignore[misc]


def test_axis1_result_has_no_dict_for_smuggled_attributes() -> None:
    # slots=True: a claim cannot carry an undeclared field past the schema.
    # frozen+slots raises TypeError rather than AttributeError here, from the
    # generated __setattr__; either way the assignment does not happen.
    claim = Axis1Result("native", (SOURCE,), "high")
    with pytest.raises((AttributeError, TypeError)):
        claim.unofficial_note = "actually not sure"  # type: ignore[attr-defined]
    assert not hasattr(claim, "unofficial_note")


def test_an_empty_source_list_cannot_reach_a_claim() -> None:
    # The type-level half of this is in test_type_level_guarantees.py, and it is
    # the stronger claim: mypy proves no empty tuple can reach the constructor,
    # including through `tuple(some_list)`. The runtime half lives on the
    # manifest, where values do arrive from outside the type system.
    with pytest.raises(PydanticValidationError):
        Taxon(
            inat_taxon_id=48662,
            scientific_name="Asclepias tuberosa",
            common_names=[],
            rank="species",
            genus="Asclepias",
            family="Apocynaceae",
            obs_count=1,
            axis1_value="native",
            axis1_sources=[],
            axis1_confidence="high",
            answer_rank="species",
            image_hashes=["0" * 64] * 4,
        )


# --- plants: resolves nothing in M1, and says so ------------------------------


def test_plants_conforms_to_the_protocol() -> None:
    assert _accepts_domain(PlantsDomain()) == "plants"
    assert isinstance(PlantsDomain(), TaxonDomain)


def test_plants_identifies_plantae() -> None:
    assert PlantsDomain().iconic_taxon_id == 47126


@pytest.mark.parametrize("taxon_id", [48662, 55849, 61944, 1, 999_999_999])
@pytest.mark.parametrize("state", ["MI", "CA", "", "not-a-state"])
def test_plants_axis1_answer_is_none_for_every_input(taxon_id: int, state: str) -> None:
    # M1: nothing is wired to USDA PLANTS, so "cannot determine" is the only
    # honest answer. This test is expected to change in M3.
    assert PlantsDomain().axis1_answer(taxon_id, state) is None


def test_plants_offers_both_nativity_options() -> None:
    assert PlantsDomain().axis1_options("MI") == ["native", "introduced"]


def test_plants_prompt_copy_has_the_required_slots() -> None:
    copy = PlantsDomain().prompt_copy()
    assert "question" in copy
    assert "axis1_prompt" in copy


# --- birds: imports cleanly, raises on use ------------------------------------


def test_birds_imports_and_exposes_its_identity() -> None:
    domain = BirdsDomain()
    assert domain.slug == "birds"
    assert domain.iconic_taxon_id == 3


def test_birds_axis1_answer_raises_rather_than_returning_none() -> None:
    # None would mean "we looked and could not tell". Nobody looked.
    with pytest.raises(NotImplementedError, match="seasonality"):
        BirdsDomain().axis1_answer(7089, "MI")


def test_birds_other_methods_raise_too() -> None:
    with pytest.raises(NotImplementedError, match="seasonality"):
        BirdsDomain().axis1_options("MI")
    with pytest.raises(NotImplementedError, match="seasonality"):
        BirdsDomain().prompt_copy()


def test_birds_error_points_at_the_adr() -> None:
    with pytest.raises(NotImplementedError, match=re.escape("docs/decisions.md")):
        BirdsDomain().prompt_copy()


# --- registry: no silent fallback ---------------------------------------------


def test_registry_resolves_known_domains() -> None:
    assert resolve_domain("plants").slug == "plants"
    assert resolve_domain("birds").slug == "birds"


def test_registry_lists_both_domains() -> None:
    assert set(DOMAINS) == {"plants", "birds"}


def test_registry_raises_on_unknown_slug_and_names_the_alternatives() -> None:
    with pytest.raises(UnknownDomainError, match="unknown domain 'birbs'") as excinfo:
        resolve_domain("birbs")
    assert "plants" in str(excinfo.value)


def test_registry_does_not_fall_back_to_a_default() -> None:
    for slug in ["", "PLANTS", "plant", "default"]:
        with pytest.raises(UnknownDomainError):
            resolve_domain(slug)
