"""Tests for what the parsers do with data they cannot read.

STANDARDS.md rule 5 says unknown or malformed data is dropped and counted, never
guessed. These tests feed the parsers responses with fields missing, null, or of
the wrong type, and assert that nothing is invented to fill the gap — no default
genus, no assumed licence, no zero-width image.

Payloads here are constructed rather than recorded, because the point is to
exercise shapes iNaturalist does not currently return but might: a field that
goes null, a type that changes. Recorded fixtures cover the happy path; these
cover the response we have not seen yet.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from sift_pack.inat.client import (
    CACHE_FORMAT,
    Endpoint,
    InatClient,
    InatError,
    Params,
    ParamValue,
    cache_key,
)
from sift_pack.inat.deck import fetch_taxon_details, select_taxa
from sift_pack.inat.photos import MONTH_BUCKETS, select_photos
from sift_pack.inat.places import (
    PLACES_PATH,
    STATE_NAMES,
    PlaceTable,
    load_places,
    refresh_places,
)
from sift_pack.inat.projections import PROJECTION_VERSION
from tests.fixture_client import (
    FIXTURE_CACHE,
    MICHIGAN_PLACE_ID,
    PLANTAE_ICONIC_TAXON_ID,
    observations_params,
)

PLANTAE = "Plantae"


def _seed(cache_dir: Path, endpoint: Endpoint, params: Params, response: dict[str, Any]) -> None:
    """Write one response into a cache directory, as the client would have.

    Includes the format marker: a directory with entries and no marker is a
    pre-M2.1 cache, and the client rejects it — which is the behaviour under
    test elsewhere, not something to trip over here.
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    (cache_dir / ".sift-cache-format.json").write_text(
        json.dumps({"format": CACHE_FORMAT, "projection_version": PROJECTION_VERSION}),
        encoding="utf-8",
    )
    path = cache_dir / endpoint / f"{cache_key(endpoint, params)}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"endpoint": endpoint, "params": dict(params), "response": response}),
        encoding="utf-8",
    )


def _seed_observations(
    cache_dir: Path, taxon_id: int, results: list[Any], bucket_label: str = "A"
) -> None:
    """Seed one bucket with results and the other three as empty.

    Every bucket must be present: `select_photos` makes four requests, and an
    absent one is a cache miss rather than an empty bucket. Seeding the empty
    ones explicitly is also the point of several of these tests — an empty
    bucket is normal and must not fail the fetch.
    """
    for bucket in MONTH_BUCKETS:
        _seed(
            cache_dir,
            "observations",
            observations_params(taxon_id, bucket.label),
            {"results": results if bucket.label == bucket_label else []},
        )


def _species_counts_params(page: int = 1) -> dict[str, ParamValue]:
    return {
        "place_id": MICHIGAN_PLACE_ID,
        "iconic_taxa": PLANTAE,
        "quality_grade": "research",
        "per_page": 500,
        "page": page,
    }


# --- deck: unreadable species_counts entries ----------------------------------


def test_entries_that_are_not_objects_are_skipped(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        "species_counts",
        _species_counts_params(),
        {"results": ["not an object", 42, None]},
    )
    _seed(tmp_path, "species_counts", _species_counts_params(2), {"results": []})
    kept, dropped = select_taxa(
        InatClient(tmp_path, offline=True), MICHIGAN_PLACE_ID, PLANTAE_ICONIC_TAXON_ID, 5
    )
    assert kept == []
    assert dropped == []


@pytest.mark.parametrize(
    "taxon",
    [
        {"name": "Asclepias syriaca", "rank": "species"},  # no id
        {"id": 1, "rank": "species"},  # no name
        {"id": 1, "name": "Asclepias syriaca"},  # no rank
        {"id": None, "name": "X", "rank": "species"},  # null id
        {"id": "one", "name": "X", "rank": "species"},  # id of the wrong type
    ],
)
def test_a_taxon_missing_a_required_field_is_skipped_not_defaulted(
    tmp_path: Path, taxon: dict[str, Any]
) -> None:
    _seed(
        tmp_path,
        "species_counts",
        _species_counts_params(),
        {"results": [{"count": 100, "taxon": taxon}]},
    )
    _seed(tmp_path, "species_counts", _species_counts_params(2), {"results": []})
    kept, _ = select_taxa(
        InatClient(tmp_path, offline=True), MICHIGAN_PLACE_ID, PLANTAE_ICONIC_TAXON_ID, 5
    )
    assert kept == []


def test_a_response_with_no_results_list_is_an_error(tmp_path: Path) -> None:
    _seed(tmp_path, "species_counts", _species_counts_params(), {"total_results": 3})
    with pytest.raises(InatError, match="no results list"):
        select_taxa(
            InatClient(tmp_path, offline=True), MICHIGAN_PLACE_ID, PLANTAE_ICONIC_TAXON_ID, 5
        )


def test_hybrids_are_dropped_by_name_as_well_as_by_rank(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        "species_counts",
        _species_counts_params(),
        {
            "results": [
                {
                    "count": 500,
                    "taxon": {"id": 1, "name": "Quercus \u00d7 warei", "rank": "species"},
                },
                {"count": 400, "taxon": {"id": 2, "name": "Mentha piperita", "rank": "hybrid"}},
                {"count": 300, "taxon": {"id": 3, "name": "Carex", "rank": "genus"}},
                {"count": 10, "taxon": {"id": 4, "name": "Rara planta", "rank": "species"}},
            ]
        },
    )
    _seed(tmp_path, "species_counts", _species_counts_params(2), {"results": []})
    kept, dropped = select_taxa(
        InatClient(tmp_path, offline=True), MICHIGAN_PLACE_ID, PLANTAE_ICONIC_TAXON_ID, 5
    )
    assert kept == []
    by_reason = {record.inat_taxon_id: record.reason for record in dropped}
    assert by_reason == {
        1: "hybrid",
        2: "hybrid",
        3: "rank_not_species",
        4: "obs_count_below_threshold",
    }


def test_the_same_taxon_on_two_pages_is_counted_once(tmp_path: Path) -> None:
    entry = {"count": 300, "taxon": {"id": 7, "name": "Rosa carolina", "rank": "species"}}
    _seed(tmp_path, "species_counts", _species_counts_params(), {"results": [entry]})
    _seed(tmp_path, "species_counts", _species_counts_params(2), {"results": [entry]})
    _seed(tmp_path, "species_counts", _species_counts_params(3), {"results": []})
    kept, _ = select_taxa(
        InatClient(tmp_path, offline=True), MICHIGAN_PLACE_ID, PLANTAE_ICONIC_TAXON_ID, 5
    )
    assert [summary.inat_taxon_id for summary in kept] == [7]


# --- deck: taxa whose ancestry cannot be read ---------------------------------


def test_a_taxon_without_a_family_ancestor_is_dropped(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        "taxa_by_id",
        {"ids": [1]},
        {
            "results": [
                {
                    "id": 1,
                    "name": "Mystery plant",
                    "ancestors": [{"rank": "genus", "name": "Mysterium"}],
                }
            ]
        },
    )
    details, dropped = fetch_taxon_details(InatClient(tmp_path, offline=True), [1])
    assert details == {}
    assert dropped[0].reason == "taxon_detail_unavailable"
    assert "family=None" in dropped[0].detail


def test_a_taxon_absent_from_the_response_is_dropped(tmp_path: Path) -> None:
    _seed(
        tmp_path,
        "taxa_by_id",
        {"ids": [1, 2]},
        {
            "results": [
                {
                    "id": 1,
                    "name": "A",
                    "ancestors": [{"rank": "genus", "name": "G"}, {"rank": "family", "name": "F"}],
                }
            ]
        },
    )
    details, dropped = fetch_taxon_details(InatClient(tmp_path, offline=True), [1, 2])
    assert set(details) == {1}
    assert [record.inat_taxon_id for record in dropped] == [2]
    assert "no record for this ID" in dropped[0].detail


def test_taxa_response_without_a_results_list_is_an_error(tmp_path: Path) -> None:
    _seed(tmp_path, "taxa_by_id", {"ids": [1]}, {"nope": True})
    with pytest.raises(InatError, match="no results list"):
        fetch_taxon_details(InatClient(tmp_path, offline=True), [1])


def test_taxa_lookups_are_batched(tmp_path: Path) -> None:
    # 31 IDs must arrive as two requests, not one oversized one.
    first = list(range(1, 31))
    for batch in (first, [31]):
        _seed(tmp_path, "taxa_by_id", {"ids": batch}, {"results": []})
    _details, dropped = fetch_taxon_details(InatClient(tmp_path, offline=True), [*first, 31])
    assert len(dropped) == 31  # every ID unresolved, but every batch was asked for


# --- photos: entries that cannot be used --------------------------------------


def _observation(**overrides: Any) -> dict[str, Any]:  # noqa: ANN401 - builds deliberately-malformed payloads
    base: dict[str, Any] = {
        "id": 100,
        "uri": "https://www.inaturalist.org/observations/100",
        "num_identification_agreements": 3,
        "user": {"login": "someone", "name": "Some One"},
        "photos": [
            {
                "id": 900,
                "license_code": "cc0",
                "original_dimensions": {"width": 100, "height": 200},
            }
        ],
    }
    base.update(overrides)
    return base


@pytest.mark.parametrize(
    "override",
    [
        {"id": None},
        {"uri": ""},
        {"uri": None},
        {"user": {"name": "No Login"}},
        {"user": None},
        {"photos": "not a list"},
    ],
)
def test_an_unusable_observation_contributes_no_photos(
    tmp_path: Path, override: dict[str, Any]
) -> None:
    _seed_observations(tmp_path, 1, [_observation(**override)])
    selection = select_photos(InatClient(tmp_path, offline=True), 1, "Test", MICHIGAN_PLACE_ID)
    assert selection.photos == []
    assert selection.drop is not None


@pytest.mark.parametrize(
    "photo",
    [
        {"id": 1, "license_code": "cc-by-nc", "original_dimensions": {"width": 1, "height": 1}},
        {"id": 1, "license_code": None, "original_dimensions": {"width": 1, "height": 1}},
        {"id": 1, "original_dimensions": {"width": 1, "height": 1}},
        {"id": 1, "license_code": "cc0"},
        {"id": 1, "license_code": "cc0", "original_dimensions": {"width": 0, "height": 1}},
        {"id": 1, "license_code": "cc0", "original_dimensions": "wrong type"},
        {"license_code": "cc0", "original_dimensions": {"width": 1, "height": 1}},
    ],
)
def test_an_unusable_photo_is_skipped_not_repaired(tmp_path: Path, photo: dict[str, Any]) -> None:
    _seed_observations(tmp_path, 1, [_observation(photos=[photo])])
    selection = select_photos(InatClient(tmp_path, offline=True), 1, "Test", MICHIGAN_PLACE_ID)
    assert selection.photos == []
    assert selection.drop is not None
    assert selection.drop.reason == "insufficient_licensed_photos"


def test_licence_codes_are_matched_case_insensitively(tmp_path: Path) -> None:
    # The request parameter is uppercase and responses are lowercase; a taxon
    # must not be dropped because upstream changed the casing.
    observations = [
        _observation(
            id=n,
            uri=f"https://www.inaturalist.org/observations/{n}",
            user={"login": f"person{n}", "name": None},
            photos=[
                {
                    "id": n * 10,
                    "license_code": code,
                    "original_dimensions": {"width": 10, "height": 10},
                }
            ],
        )
        for n, code in enumerate(["CC0", "CC-BY", "cc-by-sa", "Cc0"], start=1)
    ]
    _seed_observations(tmp_path, 1, observations)
    selection = select_photos(InatClient(tmp_path, offline=True), 1, "Test", MICHIGAN_PLACE_ID)
    assert selection.drop is None
    assert sorted(photo.license for photo in selection.photos) == [
        "cc-by",
        "cc-by-sa",
        "cc0",
        "cc0",
    ]


def test_only_one_photo_is_taken_per_observation(tmp_path: Path) -> None:
    many_photos = [
        {"id": n, "license_code": "cc0", "original_dimensions": {"width": 10, "height": 10}}
        for n in range(1, 20)
    ]
    _seed_observations(tmp_path, 1, [_observation(photos=many_photos)])
    selection = select_photos(InatClient(tmp_path, offline=True), 1, "Test", MICHIGAN_PLACE_ID)
    assert selection.photos == []
    assert selection.drop is not None  # 19 photos, but only one observation


def test_missing_agreement_count_is_recorded_as_zero_not_assumed(tmp_path: Path) -> None:
    observations = [
        _observation(
            id=n,
            uri=f"https://www.inaturalist.org/observations/{n}",
            num_identification_agreements=None,
            user={"login": f"person{n}", "name": None},
            photos=[
                {
                    "id": n * 10,
                    "license_code": "cc0",
                    "original_dimensions": {"width": 10, "height": 10},
                }
            ],
        )
        for n in range(1, 5)
    ]
    _seed_observations(tmp_path, 1, observations)
    selection = select_photos(InatClient(tmp_path, offline=True), 1, "Test", MICHIGAN_PLACE_ID)
    assert selection.drop is None
    # 0 is the floor of the recorded range, not a claim that anyone agreed.
    assert all(photo.identification_agreements == 0 for photo in selection.photos)


def test_observations_response_without_results_is_an_error(tmp_path: Path) -> None:
    _seed(tmp_path, "observations", observations_params(1, "A"), {"nope": True})
    with pytest.raises(InatError, match="no results list"):
        select_photos(InatClient(tmp_path, offline=True), 1, "Test", MICHIGAN_PLACE_ID)


def test_a_photographer_without_a_display_name_is_left_absent(tmp_path: Path) -> None:
    observations = [
        _observation(
            id=n,
            uri=f"https://www.inaturalist.org/observations/{n}",
            user={"login": "anon", "name": None},
            photos=[
                {
                    "id": n * 10,
                    "license_code": "cc0",
                    "original_dimensions": {"width": 10, "height": 10},
                }
            ],
        )
        for n in range(1, 5)
    ]
    _seed_observations(tmp_path, 1, observations)
    selection = select_photos(InatClient(tmp_path, offline=True), 1, "Test", MICHIGAN_PLACE_ID)
    # Never backfilled from the login. Only two survive: the observer cap.
    assert all(photo.photographer_name is None for photo in selection.photos)
    assert selection.drop is not None


# --- places: resolution refuses to guess --------------------------------------


def test_refresh_resolves_only_genuine_state_records(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("sift_pack.inat.places.STATE_NAMES", {"MI": "Michigan", "WA": "Washington"})
    destination = tmp_path / "places.json"
    table = refresh_places(InatClient(FIXTURE_CACHE, offline=True), destination)
    assert {state.code: state.place_id for state in table.states} == {"MI": 29, "WA": 46}
    assert PlaceTable.model_validate_json(destination.read_text(encoding="utf-8")) == table


def test_refresh_fails_loudly_when_a_state_cannot_be_resolved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # All-or-nothing: a half-written table would break only the states it
    # omitted, and only for whoever tried to build them.
    _seed(tmp_path, "places_autocomplete", {"q": "Atlantis"}, {"results": []})
    monkeypatch.setattr("sift_pack.inat.places.STATE_NAMES", {"AT": "Atlantis"})
    with pytest.raises(InatError, match="could not resolve every state"):
        refresh_places(InatClient(tmp_path, offline=True), tmp_path / "places.json")
    assert not (tmp_path / "places.json").exists()


def test_refresh_rejects_a_place_that_is_not_a_us_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Right name, right admin level, wrong country: must not be accepted.
    _seed(
        tmp_path,
        "places_autocomplete",
        {"q": "Georgia"},
        {
            "results": [
                {"id": 7777, "name": "Georgia", "admin_level": 10, "ancestor_place_ids": [9999]}
            ]
        },
    )
    monkeypatch.setattr("sift_pack.inat.places.STATE_NAMES", {"GA": "Georgia"})
    with pytest.raises(InatError, match="could not resolve every state"):
        refresh_places(InatClient(tmp_path, offline=True), tmp_path / "places.json")


def test_refresh_rejects_a_response_without_results(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _seed(tmp_path, "places_autocomplete", {"q": "Michigan"}, {"nope": True})
    monkeypatch.setattr("sift_pack.inat.places.STATE_NAMES", {"MI": "Michigan"})
    with pytest.raises(InatError, match="no results list"):
        refresh_places(InatClient(tmp_path, offline=True), tmp_path / "places.json")


def test_a_missing_place_table_is_an_error_not_an_empty_table(tmp_path: Path) -> None:
    with pytest.raises(InatError, match="missing or unreadable"):
        load_places(tmp_path / "absent.json")


def test_the_committed_table_and_the_state_list_agree() -> None:
    assert {state.code for state in load_places(PLACES_PATH).states} == set(STATE_NAMES)
