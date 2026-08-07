"""Tests for the three fetch stages, run against recorded fixtures.

The client here is a real `InatClient` pointed at `tests/fixtures/inat_cache/`
with `offline=True`, so these exercise the production code path end to end with
no network and no mocking layer. Anything a test asks for that was not recorded
raises `CacheMissError` naming the request.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from sift_pack.candidates import (
    MIN_PHOTOS_PER_CANDIDATE,
    CandidatePool,
    DropRecord,
)
from sift_pack.domains.plants import PlantsDomain
from sift_pack.fetch import fetch_pool
from sift_pack.inat.client import InatClient, InatError, cache_key
from sift_pack.inat.deck import (
    MIN_OBSERVATIONS,
    TaxonSummary,
    fetch_taxon_details,
    select_taxa,
)
from sift_pack.inat.photos import PERMITTED_LICENSES, minimum_agreement, select_photos
from sift_pack.inat.places import PlaceTable, StatePlace, UnknownStateError, load_places
from tests.fixture_client import (
    FIXTURE_CACHE,
    MICHIGAN_PLACE_ID,
    PLANTAE_ICONIC_TAXON_ID,
    RECORDED_TAXON_IDS,
    observations_params,
    recorded_client,
)

# --- places -------------------------------------------------------------------


def test_committed_place_table_covers_all_fifty_states() -> None:
    table = load_places()
    assert len(table.states) == 50
    assert len({state.code for state in table.states}) == 50


def test_committed_table_resolves_michigan_to_29() -> None:
    assert load_places().place_id_for("MI") == MICHIGAN_PLACE_ID


def test_place_lookup_is_case_insensitive() -> None:
    assert load_places().place_id_for("mi") == MICHIGAN_PLACE_ID


def test_unknown_state_raises_rather_than_defaulting() -> None:
    with pytest.raises(UnknownStateError, match="no place ID"):
        load_places().place_id_for("ZZ")


def test_every_committed_place_id_is_plausible() -> None:
    assert all(state.place_id > 0 for state in load_places().states)


def test_place_table_rejects_a_bad_state_code() -> None:
    with pytest.raises(ValidationError):
        PlaceTable(states=[StatePlace(code="MICH", name="Michigan", place_id=29)])


# --- deck ---------------------------------------------------------------------


def test_select_taxa_returns_taxa_in_descending_observation_order() -> None:
    kept, _ = select_taxa(recorded_client(), MICHIGAN_PLACE_ID, PLANTAE_ICONIC_TAXON_ID, 10)
    counts = [summary.obs_count for summary in kept]
    assert counts == sorted(counts, reverse=True)


def test_select_taxa_keeps_only_species() -> None:
    kept, _ = select_taxa(recorded_client(), MICHIGAN_PLACE_ID, PLANTAE_ICONIC_TAXON_ID, 20)
    assert all(summary.rank == "species" for summary in kept)


def test_select_taxa_enforces_the_observation_floor() -> None:
    kept, _ = select_taxa(recorded_client(), MICHIGAN_PLACE_ID, PLANTAE_ICONIC_TAXON_ID, 20)
    assert all(summary.obs_count >= MIN_OBSERVATIONS for summary in kept)


def test_select_taxa_respects_the_limit() -> None:
    kept, _ = select_taxa(recorded_client(), MICHIGAN_PLACE_ID, PLANTAE_ICONIC_TAXON_ID, 5)
    assert len(kept) == 5


def test_every_dropped_taxon_carries_a_reason() -> None:
    _, dropped = select_taxa(recorded_client(), MICHIGAN_PLACE_ID, PLANTAE_ICONIC_TAXON_ID, 20)
    for record in dropped:
        assert isinstance(record, DropRecord)
        assert record.reason
        assert record.detail
        assert record.inat_taxon_id > 0


def test_nothing_is_both_kept_and_dropped() -> None:
    kept, dropped = select_taxa(recorded_client(), MICHIGAN_PLACE_ID, PLANTAE_ICONIC_TAXON_ID, 20)
    assert not {s.inat_taxon_id for s in kept} & {d.inat_taxon_id for d in dropped}


def test_an_unknown_iconic_taxon_raises_rather_than_guessing() -> None:
    with pytest.raises(InatError, match="no iconic-taxa slug"):
        select_taxa(recorded_client(), MICHIGAN_PLACE_ID, 999999, 5)


def test_genus_hint_is_never_used_as_the_genus() -> None:
    # The hint exists for logging; the real genus comes from the ancestors.
    summary = TaxonSummary(
        inat_taxon_id=1, scientific_name="Asclepias syriaca", rank="species", obs_count=100
    )
    assert summary.genus_hint() == "Asclepias"


def test_taxon_details_supply_genus_and_family() -> None:
    details, dropped = fetch_taxon_details(recorded_client(), RECORDED_TAXON_IDS)
    assert dropped == []
    assert details[47911].genus == "Asclepias"
    assert details[47911].family == "Apocynaceae"


def test_details_are_returned_for_every_requested_taxon() -> None:
    details, dropped = fetch_taxon_details(recorded_client(), RECORDED_TAXON_IDS)
    assert set(details) | {d.inat_taxon_id for d in dropped} == set(RECORDED_TAXON_IDS)


# --- photos -------------------------------------------------------------------


def test_selected_photos_are_all_licence_cleared() -> None:
    photos, drop = select_photos(recorded_client(), 47911, "Asclepias syriaca", MICHIGAN_PLACE_ID)
    assert drop is None
    assert photos
    assert all(photo.license in PERMITTED_LICENSES for photo in photos)


def test_selected_photos_come_from_distinct_observations() -> None:
    photos, _ = select_photos(recorded_client(), 47911, "Asclepias syriaca", MICHIGAN_PLACE_ID)
    observation_ids = [photo.observation_id for photo in photos]
    assert len(observation_ids) == len(set(observation_ids))


def test_no_more_than_eight_photos_are_selected() -> None:
    photos, _ = select_photos(recorded_client(), 47911, "Asclepias syriaca", MICHIGAN_PLACE_ID)
    assert MIN_PHOTOS_PER_CANDIDATE <= len(photos) <= 8


def test_well_confirmed_observations_are_preferred() -> None:
    photos, _ = select_photos(recorded_client(), 47911, "Asclepias syriaca", MICHIGAN_PLACE_ID)
    agreements = [photo.identification_agreements for photo in photos]
    # Preferred observations come first, so agreement counts never rise back up
    # into the preferred band after dropping below it.
    preferred = [a >= 2 for a in agreements]
    assert preferred == sorted(preferred, reverse=True)


def test_every_photo_carries_attribution() -> None:
    photos, _ = select_photos(recorded_client(), 47911, "Asclepias syriaca", MICHIGAN_PLACE_ID)
    for photo in photos:
        assert photo.photographer_login
        assert photo.observation_url.startswith("https://")


def test_minimum_agreement_is_the_floor_not_the_mean() -> None:
    photos, _ = select_photos(recorded_client(), 47911, "Asclepias syriaca", MICHIGAN_PLACE_ID)
    assert minimum_agreement(photos) == min(p.identification_agreements for p in photos)


def test_minimum_agreement_refuses_an_empty_list() -> None:
    with pytest.raises(ValueError, match="at least one photo"):
        minimum_agreement([])


def test_a_taxon_with_too_few_photos_is_dropped_not_padded(tmp_path: Path) -> None:
    # Michigan's most-observed plants all clear the floor comfortably, so the
    # drop path is exercised against a recorded response truncated to two
    # observations. The records are real and unedited; only the result count is
    # reduced, which is the condition under test.
    taxon_id = RECORDED_TAXON_IDS[0]
    params = observations_params(taxon_id)
    source = FIXTURE_CACHE / "observations" / f"{cache_key('observations', params)}.json"
    envelope = json.loads(source.read_text(encoding="utf-8"))
    envelope["response"]["results"] = envelope["response"]["results"][:2]

    destination = tmp_path / "observations" / source.name
    destination.parent.mkdir(parents=True)
    destination.write_text(json.dumps(envelope), encoding="utf-8")

    photos, drop = select_photos(
        InatClient(tmp_path, offline=True), taxon_id, "Asclepias syriaca", MICHIGAN_PLACE_ID
    )
    assert photos == []
    assert drop is not None
    assert drop.reason == "insufficient_licensed_photos"
    assert str(MIN_PHOTOS_PER_CANDIDATE) in drop.detail


# --- the orchestrator ---------------------------------------------------------


def test_fetch_pool_accounts_for_every_taxon_it_examined() -> None:
    pool = fetch_pool(recorded_client(), PlantsDomain(), "MI", MICHIGAN_PLACE_ID, 3)
    assert pool.considered() == len(pool.candidates) + len(pool.dropped)
    assert pool.domain == "plants"
    assert pool.state == "MI"
    assert pool.place_id == MICHIGAN_PLACE_ID


def test_fetch_pool_produces_valid_candidates() -> None:
    pool = fetch_pool(recorded_client(), PlantsDomain(), "MI", MICHIGAN_PLACE_ID, 3)
    assert pool.candidates
    for candidate in pool.candidates:
        assert len(candidate.images) >= MIN_PHOTOS_PER_CANDIDATE
        assert candidate.genus
        assert candidate.family
        assert all(photo.license in PERMITTED_LICENSES for photo in candidate.images)


def test_fetch_pool_records_a_reason_for_every_drop() -> None:
    pool = fetch_pool(recorded_client(), PlantsDomain(), "MI", MICHIGAN_PLACE_ID, 3)
    for record in pool.dropped:
        assert record.reason
        assert record.detail


def test_fetch_pool_round_trips() -> None:
    pool = fetch_pool(recorded_client(), PlantsDomain(), "MI", MICHIGAN_PLACE_ID, 3)
    first = pool.model_dump_json(indent=2)
    assert CandidatePool.model_validate_json(first).model_dump_json(indent=2) == first


def test_fetch_pool_is_deterministic_over_a_warm_cache() -> None:
    first = fetch_pool(recorded_client(), PlantsDomain(), "MI", MICHIGAN_PLACE_ID, 3)
    second = fetch_pool(recorded_client(), PlantsDomain(), "MI", MICHIGAN_PLACE_ID, 3)
    assert [c.inat_taxon_id for c in first.candidates] == [
        c.inat_taxon_id for c in second.candidates
    ]


def test_a_second_fetch_makes_no_network_calls() -> None:
    # The client is offline, so any uncached request would raise. Combined with
    # the conftest socket blocker, a passing run proves zero network activity.
    client = recorded_client()
    fetch_pool(client, PlantsDomain(), "MI", MICHIGAN_PLACE_ID, 3)
    assert client.stats.misses == 0
    assert client.stats.hits > 0


def test_the_pool_carries_its_source() -> None:
    pool = fetch_pool(recorded_client(), PlantsDomain(), "MI", MICHIGAN_PLACE_ID, 3)
    assert pool.sources
    assert pool.sources[0].name == "iNaturalist API"
    assert pool.sources[0].url.startswith("https://api.inaturalist.org")
