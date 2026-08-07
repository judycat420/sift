"""Tests for the manifest schema: licence, confidence, and referential integrity."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from typing import Any

import pytest
from pydantic import ValidationError

from sift_pack.manifest import Image, Manifest, SourceRef, Taxon

BUILT_AT = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)
TAXONOMY_DATE = date(2026, 7, 1)


def _hash(seed: str) -> str:
    """A valid-shaped sha256 that is readable in a failure message."""
    return seed.rjust(64, "0")


def _image(seed: str, taxon_id: int, **overrides: Any) -> Image:  # noqa: ANN401 - these helpers exist to inject deliberately-invalid values
    fields: dict[str, Any] = {
        "sha256": _hash(seed),
        "inat_photo_id": 1,
        "taxon_id": taxon_id,
        "license": "cc0",
        "photographer_name": None,
        "photographer_login": "placeholder",
        "observation_url": "https://www.inaturalist.org/observations/0",
        "width": 1024,
        "height": 768,
        "bytes": 204800,
    }
    fields.update(overrides)
    return Image(**fields)


def _taxon(taxon_id: int = 48662, **overrides: Any) -> Taxon:  # noqa: ANN401 - these helpers exist to inject deliberately-invalid values
    fields: dict[str, Any] = {
        "inat_taxon_id": taxon_id,
        "scientific_name": "Asclepias tuberosa",
        "common_names": ["butterfly weed"],
        "rank": "species",
        "genus": "Asclepias",
        "family": "Apocynaceae",
        "obs_count": 4211,
        "axis1_value": "native",
        "axis1_source": "USDA PLANTS",
        "axis1_confidence": "high",
        "answer_rank": "species",
        "image_hashes": [_hash(f"a{n}") for n in range(4)],
    }
    fields.update(overrides)
    return Taxon(**fields)


def _source() -> SourceRef:
    return SourceRef(
        name="iNaturalist",
        version="v1",
        retrieved_at=BUILT_AT,
        url="https://api.inaturalist.org/v1/",
    )


def _manifest(**overrides: Any) -> Manifest:  # noqa: ANN401 - these helpers exist to inject deliberately-invalid values
    taxon = _taxon()
    fields: dict[str, Any] = {
        "domain": "plants",
        "state": "MI",
        "built_at": BUILT_AT,
        "inat_taxonomy_date": TAXONOMY_DATE,
        "sources": [_source()],
        "taxa": [taxon],
        "images": [_image(f"a{n}", taxon.inat_taxon_id, inat_photo_id=n + 1) for n in range(4)],
    }
    fields.update(overrides)
    return Manifest(**fields)


# --- the happy path and round-tripping ----------------------------------------


def test_a_consistent_manifest_validates() -> None:
    manifest = _manifest()
    assert manifest.pack_version == 1
    assert len(manifest.taxa) == 1
    assert len(manifest.images) == 4


def test_round_trip_is_byte_identical() -> None:
    first = _manifest().model_dump_json(indent=2)
    reloaded = Manifest.model_validate_json(first)
    second = reloaded.model_dump_json(indent=2)
    assert first == second


def test_round_trip_survives_an_empty_pack() -> None:
    first = _manifest(taxa=[], images=[]).model_dump_json(indent=2)
    second = Manifest.model_validate_json(first).model_dump_json(indent=2)
    assert first == second


def test_manifest_json_is_parseable_as_plain_json() -> None:
    # The runtime half is not necessarily Python; the manifest must be ordinary JSON.
    payload = json.loads(_manifest().model_dump_json())
    assert payload["taxa"][0]["axis1_source"] == "USDA PLANTS"
    assert payload["images"][0]["license"] == "cc0"


# --- licence: NC cannot be represented ----------------------------------------


@pytest.mark.parametrize("bad_license", ["cc-by-nc", "cc-by-nc-sa", "cc-by-nc-nd", "all-rights"])
def test_noncommercial_and_reserved_licenses_are_rejected(bad_license: str) -> None:
    with pytest.raises(ValidationError, match="license"):
        _image("b0", 48662, license=bad_license)


def test_a_manifest_containing_an_nc_image_fails_to_parse() -> None:
    # Constructed as raw JSON, i.e. the path a hand-edited or third-party
    # manifest would take. The type must reject it at the boundary too.
    payload = json.loads(_manifest().model_dump_json())
    payload["images"][0]["license"] = "cc-by-nc"
    with pytest.raises(ValidationError, match="license"):
        Manifest.model_validate(payload)


@pytest.mark.parametrize("good_license", ["cc0", "cc-by", "cc-by-sa"])
def test_permitted_licenses_are_accepted(good_license: str) -> None:
    assert _image("b1", 48662, license=good_license).license == good_license


# --- confidence: "low" has no member ------------------------------------------


def test_low_confidence_cannot_enter_a_manifest() -> None:
    with pytest.raises(ValidationError, match="axis1_confidence"):
        _taxon(axis1_confidence="low")


@pytest.mark.parametrize("good", ["high", "medium"])
def test_permitted_confidences_are_accepted(good: str) -> None:
    assert _taxon(axis1_confidence=good).axis1_confidence == good


# --- provenance is required ---------------------------------------------------


def test_taxon_cannot_be_built_without_an_axis1_source() -> None:
    fields = {
        "inat_taxon_id": 48662,
        "scientific_name": "Asclepias tuberosa",
        "common_names": [],
        "rank": "species",
        "genus": "Asclepias",
        "family": "Apocynaceae",
        "obs_count": 1,
        "axis1_value": "native",
        # axis1_source deliberately omitted
        "axis1_confidence": "high",
        "answer_rank": "species",
        "image_hashes": [_hash(f"a{n}") for n in range(4)],
    }
    with pytest.raises(ValidationError, match="axis1_source"):
        Taxon(**fields)  # type: ignore[arg-type]


def test_axis1_source_cannot_be_blank() -> None:
    with pytest.raises(ValidationError, match="axis1_source"):
        _taxon(axis1_source="")


# --- referential integrity ----------------------------------------------------


def test_a_dangling_image_hash_fails_validation() -> None:
    taxon = _taxon(image_hashes=[_hash("a0"), _hash("a1"), _hash("a2"), _hash("dead")])
    with pytest.raises(ValidationError, match="unknown image hashes"):
        _manifest(taxa=[taxon])


def test_an_image_for_an_absent_taxon_fails_validation() -> None:
    with pytest.raises(ValidationError, match="images reference unknown taxa"):
        _manifest(images=[*_manifest().images, _image("c0", 99999, inat_photo_id=9)])


def test_duplicate_image_hashes_fail_validation() -> None:
    images = [_image("a0", 48662, inat_photo_id=n + 1) for n in range(2)]
    with pytest.raises(ValidationError, match="duplicate image sha256"):
        _manifest(taxa=[_taxon(image_hashes=[_hash("a0")] * 4)], images=images)


def test_duplicate_taxon_ids_fail_validation() -> None:
    with pytest.raises(ValidationError, match="duplicate inat_taxon_id"):
        _manifest(taxa=[_taxon(), _taxon()])


def test_an_empty_pack_is_valid() -> None:
    # Nothing resolved is a legitimate outcome; an empty deck beats a wrong one.
    assert _manifest(taxa=[], images=[]).taxa == []


# --- starved taxa fail at build time ------------------------------------------


@pytest.mark.parametrize("count", [0, 1, 2, 3])
def test_fewer_than_four_images_fails_validation(count: int) -> None:
    with pytest.raises(ValidationError, match="image_hashes"):
        _taxon(image_hashes=[_hash(f"a{n}") for n in range(count)])


def test_exactly_four_images_is_enough() -> None:
    assert len(_taxon().image_hashes) == 4


def test_image_hashes_must_look_like_sha256() -> None:
    with pytest.raises(ValidationError, match="image_hashes"):
        _taxon(image_hashes=["not-a-hash"] * 4)


# --- schema strictness --------------------------------------------------------


def test_unknown_fields_are_rejected() -> None:
    # A key we do not recognise means producer and consumer disagree; guessing
    # which is right is the silent failure rule 5 forbids.
    payload = json.loads(_manifest().model_dump_json())
    payload["surprise"] = True
    with pytest.raises(ValidationError, match="surprise"):
        Manifest.model_validate(payload)


def test_a_manifest_is_frozen() -> None:
    manifest = _manifest()
    with pytest.raises(ValidationError):
        manifest.state = "CA"


def test_sources_cannot_be_empty() -> None:
    with pytest.raises(ValidationError, match="sources"):
        _manifest(sources=[])
