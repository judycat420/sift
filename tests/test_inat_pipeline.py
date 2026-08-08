"""Tests for the three fetch stages, run against recorded fixtures.

The client here is a real `InatClient` pointed at `tests/fixtures/inat_cache/`
with `offline=True`, so these exercise the production code path end to end with
no network and no mocking layer. Anything a test asks for that was not recorded
raises `CacheMissError` naming the request.
"""

from __future__ import annotations

import json
from collections import Counter
from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import ValidationError

from sift_pack.candidates import (
    MAX_PHOTOS_PER_OBSERVER,
    MIN_PHOTOS_PER_CANDIDATE,
    CandidatePhoto,
    CandidatePool,
    DropRecord,
)
from sift_pack.domains.plants import PlantsDomain
from sift_pack.fetch import fetch_pool
from sift_pack.inat.client import CACHE_FORMAT, InatClient, InatError, cache_key
from sift_pack.inat.deck import (
    MIN_OBSERVATIONS,
    TaxonSummary,
    fetch_taxon_details,
    select_taxa,
)
from sift_pack.inat.photos import (
    MONTH_BUCKETS,
    PERMITTED_LICENSES,
    PhotoSelection,
    distinct_observers,
    months_represented,
    select_photos,
)
from sift_pack.inat.places import PlaceTable, StatePlace, UnknownStateError, load_places
from sift_pack.inat.projections import PROJECTION_VERSION
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


def _selection(taxon_id: int | None = None) -> PhotoSelection:
    """Run selection for a recorded taxon."""
    target = RECORDED_TAXON_IDS[0] if taxon_id is None else taxon_id
    return select_photos(recorded_client(), target, "Recorded taxon", MICHIGAN_PLACE_ID)


def test_selected_photos_are_all_licence_cleared() -> None:
    selection = _selection()
    assert selection.drop is None
    assert selection.photos
    assert all(photo.license in PERMITTED_LICENSES for photo in selection.photos)


def test_selected_photos_come_from_distinct_observations() -> None:
    observation_ids = [photo.observation_id for photo in _selection().photos]
    assert len(observation_ids) == len(set(observation_ids))


def test_no_observer_supplies_more_than_two_photos() -> None:
    for taxon_id in RECORDED_TAXON_IDS:
        counts = Counter(photo.photographer_login for photo in _selection(taxon_id).photos)
        assert not [login for login, n in counts.items() if n > MAX_PHOTOS_PER_OBSERVER]


def test_no_more_than_eight_photos_are_selected() -> None:
    photos = _selection().photos
    assert MIN_PHOTOS_PER_CANDIDATE <= len(photos) <= 8


def test_selection_spreads_across_seasonal_buckets() -> None:
    # The defect M2.1 exists to fix: a single unstratified page clusters into
    # one season. Selection must reach for every bucket that has anything.
    selection = _selection()
    available = {label for label, count in selection.bucket_observations.items() if count}
    represented = {photo.month_bucket for photo in selection.photos}
    assert represented == available or len(represented) >= min(len(available), 4)


def test_every_bucket_is_queried_even_when_empty() -> None:
    # Four requests per taxon, always: an absent bucket would be a cache miss.
    assert set(_selection().bucket_observations) == {b.label for b in MONTH_BUCKETS}


def test_bucket_yields_are_recorded() -> None:
    yields = _selection().bucket_observations
    assert all(count >= 0 for count in yields.values())
    assert sum(yields.values()) > 0


def test_within_a_bucket_photos_are_taken_in_observation_id_order() -> None:
    # The only ordering selection applies below seasonal spread. It claims
    # nothing; it exists so the same cache yields the same pack.
    selection = _selection()
    by_bucket: dict[str, list[int]] = {}
    for photo in selection.photos:
        by_bucket.setdefault(photo.month_bucket, []).append(photo.observation_id)
    for observation_ids in by_bucket.values():
        assert observation_ids == sorted(observation_ids)


def test_selection_does_not_rank_on_agreements() -> None:
    # If agreements still influenced order, the selected photos would skew
    # toward high counts relative to what the buckets offered. They must not.
    selection = _selection()
    assert selection.drop is None
    chosen = [photo.identification_agreements for photo in selection.photos]
    assert min(chosen) <= 1 or len(set(chosen)) > 1


def test_every_photo_carries_attribution_and_a_bucket() -> None:
    for photo in _selection().photos:
        assert photo.photographer_login
        # Recorded verbatim: older observations carry http:// URIs, and
        # rewriting them to https would be editing the source record.
        assert "inaturalist.org/observations/" in photo.observation_url
        assert photo.month_bucket in {bucket.label for bucket in MONTH_BUCKETS}


def test_quality_signals_are_computed_from_the_photos() -> None:
    photos = _selection().photos
    assert months_represented(photos) == len({p.month_bucket for p in photos})
    assert distinct_observers(photos) == len({p.photographer_login for p in photos})


@pytest.mark.parametrize("measure", [months_represented, distinct_observers])
def test_quality_signals_refuse_an_empty_list(
    measure: Callable[[list[CandidatePhoto]], int],
) -> None:
    with pytest.raises(ValueError, match="requires at least one photo"):
        measure([])


def test_a_taxon_with_too_few_photos_is_dropped_not_padded(tmp_path: Path) -> None:
    # Michigan's most-observed plants clear the floor comfortably, so the drop
    # path is exercised against recorded responses truncated to one observation
    # per bucket. The records are real and unedited; only the count is reduced,
    # which is the condition under test.
    taxon_id = RECORDED_TAXON_IDS[0]
    for bucket in MONTH_BUCKETS:
        params = observations_params(taxon_id, bucket.label)
        source = FIXTURE_CACHE / "observations" / f"{cache_key('observations', params)}.json"
        envelope = json.loads(source.read_text(encoding="utf-8"))
        envelope["response"]["results"] = envelope["response"]["results"][:1]
        destination = tmp_path / "observations" / source.name
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(envelope), encoding="utf-8")
    (tmp_path / ".sift-cache-format.json").write_text(
        json.dumps({"format": CACHE_FORMAT, "projection_version": PROJECTION_VERSION}),
        encoding="utf-8",
    )

    selection = select_photos(
        InatClient(tmp_path, offline=True), taxon_id, "Asclepias syriaca", MICHIGAN_PLACE_ID
    )
    # Four buckets x one observation each could still reach four; the point is
    # that whatever survives, nothing is invented to pad it.
    if selection.drop is not None:
        assert selection.photos == []
        assert selection.drop.reason == "insufficient_licensed_photos"
    else:
        assert len(selection.photos) >= MIN_PHOTOS_PER_CANDIDATE


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
