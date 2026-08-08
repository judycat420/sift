"""Tests for per-place establishment status, and the guard that makes it usable.

The guard is the reason this module exists. iNaturalist answers a per-place
query from the nearest ancestor place that has a listing, so a value read
without checking `establishment_means.place.id` can be a claim about North
America wearing Michigan's name. `tests/fixtures/inat_cache/` holds a real
recorded response of exactly that shape, scoped to Arizona.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from sift_pack.inat.client import Endpoint, InatClient, Params
from sift_pack.inat.nativity import (
    INAT_SOURCE_NAME,
    TAXA_BATCH_SIZE,
    PlaceEstablishment,
    _read_one,
    fetch_establishment,
    inat_source_ref,
)
from sift_pack.inat.projections import project
from tests.fixture_client import (
    ARIZONA_PLACE_ID,
    MICHIGAN_PLACE_ID,
    RECORDED_INHERITED_IDS,
    RECORDED_NATIVITY_IDS,
    recorded_client,
)

NORTH_AMERICA_PLACE_ID = 97394

# Recorded at Arizona, where iNaturalist has no state listing and answers from
# North America instead. See scripts/record_fixtures.py, INHERITED_TAXON_IDS.
INHERITED = {"Elaeagnus umbellata": 64697, "Lythrum salicaria": 61321, "Solanum dulcamara": 55620}

MICHIGAN_NATIVE = {
    "Asclepias tuberosa": 47912,
    "Pinus strobus": 52391,
    "Trillium grandiflorum": 55402,
}
MICHIGAN_INTRODUCED = {
    "Alliaria petiolata": 56061,
    "Daucus carota": 76610,
    "Cichorium intybus": 52913,
}


@dataclass
class PartialFetcher:
    """A fetcher that answers for only some of the taxa it was asked about.

    No recorded response omits a taxon, so the "the API skipped one" branch has
    to be constructed. `Fetcher` is the seam the client documents for exactly
    this: there is no socket in the loop, so `conftest`'s network guard is not
    being worked around, it is simply not reached.
    """

    answers_for: set[int]
    results: list[dict[str, Any]] | None = field(default=None)

    def fetch(self, endpoint: Endpoint, params: Params) -> dict[str, Any]:
        """Answer for the subset in `answers_for`, and omit the rest."""
        assert endpoint == "taxa_by_id"
        if self.results is None and not self.answers_for:
            return {"total_results": 0}
        return {
            "total_results": len(self.answers_for),
            "results": [
                {
                    "id": taxon_id,
                    "name": f"Taxon {taxon_id}",
                    "establishment_means": {
                        "establishment_means": "native",
                        "place": {"id": params["preferred_place_id"], "name": "Michigan"},
                    },
                }
                for taxon_id in sorted(self.answers_for)
            ],
        }


def michigan() -> dict[int, PlaceEstablishment]:
    """Every recorded Michigan establishment status."""
    return fetch_establishment(recorded_client(), RECORDED_NATIVITY_IDS, MICHIGAN_PLACE_ID)


def arizona() -> dict[int, PlaceEstablishment]:
    """The recorded Arizona batch, every entry of which was answered by an ancestor."""
    return fetch_establishment(recorded_client(), RECORDED_INHERITED_IDS, ARIZONA_PLACE_ID)


# --- the place guard: an inherited answer is refused, not accepted -------------


def test_an_ancestor_place_answer_is_refused() -> None:
    outcomes = arizona()
    for name, taxon_id in INHERITED.items():
        outcome = outcomes[taxon_id]
        assert outcome.value is None, f"{name} was accepted from {outcome.place_name}"
        assert outcome.reason == "place_not_state_scoped"
        assert not outcome.usable


def test_the_refused_answer_had_a_value_that_would_otherwise_have_been_taken() -> None:
    # Without the guard this is not a missing value that fails safe — it is a
    # perfectly usable-looking "introduced" that a naive reader would promote.
    outcomes = arizona()
    raws = {outcomes[taxon_id].raw for taxon_id in INHERITED.values()}
    assert raws == {"introduced"}


def test_the_refusal_names_the_place_the_answer_actually_came_from() -> None:
    outcomes = arizona()
    for taxon_id in INHERITED.values():
        outcome = outcomes[taxon_id]
        assert outcome.place_id == NORTH_AMERICA_PLACE_ID
        assert outcome.place_name == "North America"
        assert "North America" in outcome.detail
        assert str(ARIZONA_PLACE_ID) in outcome.detail


def test_the_guard_does_not_depend_on_which_state_it_protects() -> None:
    # Michigan needs the guard zero times out of a real 300-taxon pool and
    # Arizona needs it constantly. The same call refuses one and accepts the
    # other, with no per-state configuration between them.
    accepted = michigan()
    refused = arizona()
    assert all(outcome.usable for outcome in accepted.values())
    assert not any(outcome.usable for outcome in refused.values())


# --- the accepted path ---------------------------------------------------------


def test_a_place_scoped_value_is_taken_and_records_its_place() -> None:
    for name, taxon_id in MICHIGAN_NATIVE.items():
        outcome = michigan()[taxon_id]
        assert outcome.value == "native", name
        assert outcome.place_id == MICHIGAN_PLACE_ID
        assert outcome.place_name == "Michigan"
        assert outcome.usable


def test_introduced_taxa_read_as_introduced() -> None:
    # A test that only checked natives would pass against a reader hard-wired
    # to say "native".
    for name, taxon_id in MICHIGAN_INTRODUCED.items():
        assert michigan()[taxon_id].value == "introduced", name


def test_every_requested_taxon_comes_back_present_or_accounted_for() -> None:
    outcomes = michigan()
    assert set(outcomes) == set(RECORDED_NATIVITY_IDS)


def test_a_taxon_the_api_omits_is_recorded_as_absent_not_dropped(tmp_path: Path) -> None:
    # A silently missing taxon is indistinguishable from one the checklist had
    # nothing for; the two get different reasons so a build can tell them apart.
    # Driven through the `Fetcher` seam because no recorded response omits a
    # taxon — the condition has to be constructed, and this is where the client
    # is designed to be constructed at.
    client = InatClient(tmp_path, fetcher=PartialFetcher(answers_for={11}))
    outcomes = fetch_establishment(client, [11, 22, 33], MICHIGAN_PLACE_ID)
    assert set(outcomes) == {11, 22, 33}
    assert outcomes[11].value == "native"
    for taxon_id in (22, 33):
        assert outcomes[taxon_id].reason == "absent_from_inat_response"
        assert str(taxon_id) in outcomes[taxon_id].detail


def test_a_response_with_no_results_list_leaves_every_taxon_accounted_for(
    tmp_path: Path,
) -> None:
    client = InatClient(tmp_path, fetcher=PartialFetcher(answers_for=set(), results=None))
    outcomes = fetch_establishment(client, [11, 22], MICHIGAN_PLACE_ID)
    assert {o.reason for o in outcomes.values()} == {"absent_from_inat_response"}


def test_a_repeated_taxon_id_is_asked_about_once() -> None:
    outcomes = fetch_establishment(
        recorded_client(), [*RECORDED_NATIVITY_IDS, *RECORDED_NATIVITY_IDS], MICHIGAN_PLACE_ID
    )
    assert set(outcomes) == set(RECORDED_NATIVITY_IDS)


# --- the projection carries the place, not only the value ----------------------


def test_the_projection_keeps_the_place_sub_object() -> None:
    # Dropping the place would repeat the version-2 mistake on the one field
    # where it puts a continent-scoped claim on a state card.
    raw = {
        "results": [
            {
                "id": 1,
                "name": "Test taxon",
                "establishment_means": {
                    "establishment_means": "introduced",
                    "place": {"id": 97394, "name": "North America", "admin_level": -10},
                },
            }
        ]
    }
    means = project("taxa_by_id", raw)["results"][0]["establishment_means"]
    assert means["establishment_means"] == "introduced"
    assert means["place"] == {"id": 97394, "name": "North America", "admin_level": -10}


def test_the_projection_preserves_absence_rather_than_defaulting() -> None:
    projected = project("taxa_by_id", {"results": [{"id": 1, "name": "T"}]})
    assert projected["results"][0]["establishment_means"] is None


# --- vocabulary: nothing outside native/introduced is coerced ------------------


@pytest.mark.parametrize("raw", ["naturalised", "invasive", "managed", "unknown", ""])
def test_an_uninterpretable_status_never_becomes_a_label(raw: str) -> None:
    # iNaturalist's vocabulary is wider than this domain's. "invasive" usually
    # implies introduced but is applied to aggressive natives too, so it is
    # refused with a reason rather than rounded to the nearer of the two.
    outcome = _read(raw, place_id=MICHIGAN_PLACE_ID)
    assert outcome.value is None
    assert outcome.reason == "uninterpretable_establishment_means"
    assert outcome.raw == raw


def test_endemic_reads_as_native() -> None:
    # Endemic is a strictly stronger statement of the same fact: native, and
    # found nowhere else.
    assert _read("endemic", place_id=MICHIGAN_PLACE_ID).value == "native"


def test_a_record_with_no_listing_has_no_value_and_a_reason() -> None:
    projected = project("taxa_by_id", {"results": [{"id": 1, "name": "T"}]})
    outcome = _read_one(projected["results"][0], MICHIGAN_PLACE_ID)
    assert outcome is not None
    assert outcome.value is None
    assert outcome.reason == "no_inat_listing"


def test_a_record_with_no_usable_id_is_dropped_rather_than_guessed_at() -> None:
    # It cannot be attributed to any taxon, so there is nothing to record it
    # against; the taxon it should have answered for falls out as absent.
    assert _read_one({"name": "T"}, MICHIGAN_PLACE_ID) is None


def _read(raw: str, place_id: int) -> PlaceEstablishment:
    """Run one synthetic response through the production projection and reader."""
    body = {
        "results": [
            {
                "id": 1,
                "name": "T",
                "establishment_means": {
                    "establishment_means": raw,
                    "place": {"id": place_id, "name": "Michigan", "admin_level": 10},
                },
            }
        ]
    }
    projected = project("taxa_by_id", body)
    outcome = _read_one(projected["results"][0], place_id)
    assert outcome is not None
    return outcome


# --- provenance ----------------------------------------------------------------


def test_the_source_ref_names_the_checklist_not_the_api() -> None:
    # The pack already cites "iNaturalist API" for photos and observation
    # counts. Collapsing the two would make a nativity claim look as though the
    # observation record backed it.
    ref = inat_source_ref(datetime(2026, 8, 8, tzinfo=UTC), "Michigan")
    assert ref.name == INAT_SOURCE_NAME
    assert ref.name != "iNaturalist API"


def test_the_source_version_records_the_place_and_the_retrieval_date() -> None:
    ref = inat_source_ref(datetime(2026, 8, 8, tzinfo=UTC), "Michigan")
    assert ref.version == "Michigan checklist retrieved 2026-08-08"


def test_two_places_produce_distinguishable_source_refs() -> None:
    when = datetime(2026, 8, 8, tzinfo=UTC)
    assert inat_source_ref(when, "Michigan") != inat_source_ref(when, "Arizona")


# --- batching ------------------------------------------------------------------


def test_the_batch_size_is_the_api_page_size() -> None:
    # Asking for more silently returns the first 30, so a larger batch would
    # lose taxa without erroring.
    assert TAXA_BATCH_SIZE == 30
