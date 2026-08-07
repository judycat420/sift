"""Tests for the candidate schema — above all, what it refuses to represent."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, get_args

import pytest
from pydantic import ValidationError

from sift_pack.candidates import (
    CandidatePhoto,
    CandidatePool,
    CandidateTaxon,
    DropReason,
    DropRecord,
)
from sift_pack.manifest import SourceRef

FETCHED_AT = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)

# Every name a nativity claim could plausibly hide under, including
# iNaturalist's own field name for it.
NATIVITY_FIELD_NAMES = [
    "axis1",
    "axis1_value",
    "axis1_source",
    "axis1_confidence",
    "native",
    "is_native",
    "nativity",
    "native_status",
    "establishment_means",
    "introduced",
    "origin",
    "provenance",
    "status",
]


def _photo(observation_id: int, taxon_id: int = 47911, **overrides: Any) -> CandidatePhoto:  # noqa: ANN401 - helper injects deliberately-invalid values
    fields: dict[str, Any] = {
        "inat_photo_id": observation_id * 10,
        "taxon_id": taxon_id,
        "observation_id": observation_id,
        "observation_url": f"https://www.inaturalist.org/observations/{observation_id}",
        "license": "cc0",
        "photographer_login": "somebody",
        "photographer_name": None,
        "width": 1024,
        "height": 768,
        "identification_agreements": 2,
    }
    fields.update(overrides)
    return CandidatePhoto(**fields)


def _taxon(taxon_id: int = 47911, **overrides: Any) -> CandidateTaxon:  # noqa: ANN401 - ditto
    fields: dict[str, Any] = {
        "inat_taxon_id": taxon_id,
        "scientific_name": "Asclepias syriaca",
        "common_names": ["common milkweed"],
        "rank": "species",
        "genus": "Asclepias",
        "family": "Apocynaceae",
        "obs_count": 9108,
        "identification_agreement": 2,
        "images": [_photo(n, taxon_id) for n in range(1, 5)],
    }
    fields.update(overrides)
    return CandidateTaxon(**fields)


def _pool(**overrides: Any) -> CandidatePool:  # noqa: ANN401 - ditto
    fields: dict[str, Any] = {
        "domain": "plants",
        "state": "MI",
        "place_id": 29,
        "fetched_at": FETCHED_AT,
        "sources": [
            SourceRef(
                name="iNaturalist API",
                version="v1",
                retrieved_at=FETCHED_AT,
                url="https://api.inaturalist.org/v1/",
            )
        ],
        "candidates": [_taxon()],
        "dropped": [],
    }
    fields.update(overrides)
    return CandidatePool(**fields)


# --- the headline invariant: no nativity field, by any name -------------------


def test_candidate_taxon_field_set_is_exactly_this() -> None:
    # Asserting the whole set, not the absence of specific names: any field
    # added to this model — under any name — fails here and must be justified.
    assert set(CandidateTaxon.model_fields) == {
        "inat_taxon_id",
        "scientific_name",
        "common_names",
        "rank",
        "genus",
        "family",
        "obs_count",
        "identification_agreement",
        "images",
    }


@pytest.mark.parametrize("field_name", NATIVITY_FIELD_NAMES)
def test_candidate_taxon_has_no_field_that_could_hold_nativity(field_name: str) -> None:
    assert field_name not in CandidateTaxon.model_fields


@pytest.mark.parametrize("field_name", NATIVITY_FIELD_NAMES)
def test_candidate_taxon_rejects_a_nativity_value_at_construction(field_name: str) -> None:
    # extra="forbid" means it cannot be smuggled in as an extra key either.
    smuggled: dict[str, Any] = {field_name: "native"}
    with pytest.raises(ValidationError, match=field_name):
        _taxon(**smuggled)


@pytest.mark.parametrize("field_name", NATIVITY_FIELD_NAMES)
def test_candidate_photo_has_no_nativity_field_either(field_name: str) -> None:
    assert field_name not in CandidatePhoto.model_fields


def test_candidate_photo_field_set_is_exactly_this() -> None:
    assert set(CandidatePhoto.model_fields) == {
        "inat_photo_id",
        "taxon_id",
        "observation_id",
        "observation_url",
        "license",
        "photographer_login",
        "photographer_name",
        "width",
        "height",
        "identification_agreements",
    }


def test_candidate_photo_has_no_digest_field() -> None:
    # sha256 and bytes only exist once image bytes are fetched from S3.
    # A placeholder digest here would be M1's fabricated-hash mistake again.
    assert "sha256" not in CandidatePhoto.model_fields
    assert "bytes" not in CandidatePhoto.model_fields


# --- photo constraints --------------------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_fewer_than_four_photos_is_rejected(count: int) -> None:
    with pytest.raises(ValidationError, match="images"):
        _taxon(images=[_photo(n) for n in range(1, count + 1)])


def test_more_than_eight_photos_is_rejected() -> None:
    with pytest.raises(ValidationError, match="images"):
        _taxon(images=[_photo(n) for n in range(1, 10)])


def test_photos_must_come_from_distinct_observations() -> None:
    with pytest.raises(ValidationError, match="multiple photos from one observation"):
        _taxon(images=[_photo(1), _photo(1), _photo(2), _photo(3)])


def test_photos_must_belong_to_their_taxon() -> None:
    photos = [_photo(n) for n in range(1, 4)]
    photos.append(_photo(9, taxon_id=99999))
    with pytest.raises(ValidationError, match="another taxon"):
        _taxon(images=photos)


@pytest.mark.parametrize("bad_license", ["cc-by-nc", "cc-by-nc-sa", "all-rights-reserved"])
def test_noncommercial_photos_cannot_be_represented(bad_license: str) -> None:
    with pytest.raises(ValidationError, match="license"):
        _photo(1, license=bad_license)


# --- pool integrity -----------------------------------------------------------


def test_a_pool_round_trips_byte_identically() -> None:
    first = _pool().model_dump_json(indent=2)
    second = CandidatePool.model_validate_json(first).model_dump_json(indent=2)
    assert first == second


def test_considered_is_kept_plus_dropped() -> None:
    pool = _pool(
        dropped=[
            DropRecord(inat_taxon_id=1, name="One", reason="hybrid", detail="x"),
            DropRecord(inat_taxon_id=2, name="Two", reason="rank_not_species", detail="x"),
        ]
    )
    assert pool.considered() == 3


def test_a_taxon_cannot_be_both_kept_and_dropped() -> None:
    with pytest.raises(ValidationError, match="both kept and dropped"):
        _pool(
            dropped=[
                DropRecord(
                    inat_taxon_id=47911, name="Asclepias syriaca", reason="hybrid", detail="x"
                )
            ]
        )


def test_duplicate_candidates_are_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate candidates"):
        _pool(candidates=[_taxon(), _taxon()])


def test_duplicate_drops_are_rejected() -> None:
    record = DropRecord(inat_taxon_id=5, name="Five", reason="hybrid", detail="x")
    with pytest.raises(ValidationError, match="duplicate drops"):
        _pool(dropped=[record, record])


def test_drop_reason_vocabulary_is_closed() -> None:
    with pytest.raises(ValidationError, match="reason"):
        DropRecord(inat_taxon_id=1, name="One", reason="because", detail="x")  # type: ignore[arg-type]


def test_every_drop_reason_is_usable() -> None:
    for reason in get_args(DropReason):
        assert DropRecord(inat_taxon_id=1, name="One", reason=reason, detail="d").reason == reason


def test_a_pool_needs_at_least_one_source() -> None:
    with pytest.raises(ValidationError, match="sources"):
        _pool(sources=[])


def test_unknown_pool_fields_are_rejected() -> None:
    with pytest.raises(ValidationError, match="surprise"):
        _pool(surprise=True)


def test_an_empty_pool_is_valid() -> None:
    assert _pool(candidates=[], dropped=[]).considered() == 0
