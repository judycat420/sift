"""Tests for the photo selection policy: the rules, and their priority order.

The policy is the M2.1 defect fix that matters. Its rules interact, so these
tests are built from constructed observations where each rule can be isolated
and its precedence over the others made visible. The recorded fixtures in
`test_inat_pipeline.py` cover the same policy against real data.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any

from sift_pack.candidates import (
    MAX_PHOTOS_PER_CANDIDATE,
    MAX_PHOTOS_PER_OBSERVER,
    MIN_PHOTOS_PER_CANDIDATE,
)
from sift_pack.inat.client import CACHE_FORMAT, InatClient, cache_key
from sift_pack.inat.photos import MONTH_BUCKETS, PhotoSelection, select_photos
from sift_pack.inat.projections import PROJECTION_VERSION
from tests.fixture_client import MICHIGAN_PLACE_ID, observations_params

TAXON = 1


def _observation(
    observation_id: int, login: str, agreements: int = 3, photos: int = 1
) -> dict[str, Any]:
    """One well-formed observation with `photos` usable photos."""
    return {
        "id": observation_id,
        "uri": f"https://www.inaturalist.org/observations/{observation_id}",
        "num_identification_agreements": agreements,
        "user": {"login": login, "name": None},
        "photos": [
            {
                "id": observation_id * 100 + n,
                "license_code": "cc0",
                "original_dimensions": {"width": 10, "height": 10},
                "url": (
                    "https://inaturalist-open-data.s3.amazonaws.com/photos/"
                    f"{observation_id * 100 + n}/square.jpg"
                ),
            }
            for n in range(photos)
        ],
    }


def _run(tmp_path: Path, by_bucket: dict[str, list[dict[str, Any]]]) -> PhotoSelection:
    """Seed every bucket and run selection."""
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / ".sift-cache-format.json").write_text(
        json.dumps({"format": CACHE_FORMAT, "projection_version": PROJECTION_VERSION}),
        encoding="utf-8",
    )
    for bucket in MONTH_BUCKETS:
        params = observations_params(TAXON, bucket.label)
        path = tmp_path / "observations" / f"{cache_key('observations', params)}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "endpoint": "observations",
                    "params": {},
                    "response": {"results": by_bucket.get(bucket.label, [])},
                }
            ),
            encoding="utf-8",
        )
    return select_photos(InatClient(tmp_path, offline=True), TAXON, "Test taxon", MICHIGAN_PLACE_ID)


# --- rule (b): the observer cap -----------------------------------------------


def test_one_observer_cannot_supply_more_than_two_photos(tmp_path: Path) -> None:
    # Twenty observations, all by the same person. Without the cap this would
    # sail to eight photos of one person's plants.
    selection = _run(tmp_path, {"A": [_observation(n, "prolific") for n in range(1, 21)]})
    assert selection.drop is not None
    assert selection.photos == []


def test_the_cap_applies_across_buckets_not_within_one(tmp_path: Path) -> None:
    # The same observer in every bucket still contributes at most two photos.
    selection = _run(
        tmp_path,
        {
            bucket.label: [_observation(int(f"{index}{n}"), "prolific") for n in range(1, 4)]
            for index, bucket in enumerate(MONTH_BUCKETS, start=1)
        },
    )
    counts = Counter(photo.photographer_login for photo in selection.photos)
    assert all(count <= MAX_PHOTOS_PER_OBSERVER for count in counts.values())


def test_a_mixed_field_respects_the_cap_and_still_fills(tmp_path: Path) -> None:
    observations = [_observation(n, "prolific") for n in range(1, 10)]
    observations += [_observation(n, f"person{n}") for n in range(20, 26)]
    selection = _run(tmp_path, {"A": observations})
    assert selection.drop is None
    counts = Counter(photo.photographer_login for photo in selection.photos)
    assert counts["prolific"] == MAX_PHOTOS_PER_OBSERVER
    assert len(selection.photos) == MAX_PHOTOS_PER_CANDIDATE


# --- rule (c): seasonal spread is maximised -----------------------------------


def test_every_bucket_contributes_before_any_contributes_twice(tmp_path: Path) -> None:
    selection = _run(
        tmp_path,
        {
            bucket.label: [_observation(int(f"{index}{n}"), f"p{index}_{n}") for n in range(1, 6)]
            for index, bucket in enumerate(MONTH_BUCKETS, start=1)
        },
    )
    assert selection.drop is None
    counts = Counter(photo.month_bucket for photo in selection.photos)
    assert set(counts) == {bucket.label for bucket in MONTH_BUCKETS}
    assert max(counts.values()) - min(counts.values()) <= 1


def test_a_lone_record_from_an_unrepresented_season_is_still_taken(tmp_path: Path) -> None:
    # Bucket A offers ten candidates, bucket C exactly one. The single autumn
    # photo must still be selected: it teaches something no eleventh spring
    # photo can. Spread is the only thing selection optimises for.
    selection = _run(
        tmp_path,
        {
            "A": [_observation(n, f"spring{n}") for n in range(1, 11)],
            "C": [_observation(99, "autumn")],
        },
    )
    assert selection.drop is None
    assert "C" in {photo.month_bucket for photo in selection.photos}


def test_a_taxon_present_in_one_season_only_still_qualifies(tmp_path: Path) -> None:
    # A spring ephemeral. Its three empty buckets are a fact about the plant,
    # not a failure, and it must not be dropped for them.
    selection = _run(tmp_path, {"A": [_observation(n, f"person{n}") for n in range(1, 9)]})
    assert selection.drop is None
    assert len(selection.photos) == MAX_PHOTOS_PER_CANDIDATE
    assert {photo.month_bucket for photo in selection.photos} == {"A"}


def test_empty_buckets_are_recorded_as_zero_not_omitted(tmp_path: Path) -> None:
    selection = _run(tmp_path, {"A": [_observation(n, f"person{n}") for n in range(1, 9)]})
    assert selection.bucket_observations == {"A": 8, "B": 0, "C": 0, "D": 0}


def test_a_completely_empty_field_drops_without_erroring(tmp_path: Path) -> None:
    selection = _run(tmp_path, {})
    assert selection.drop is not None
    assert selection.drop.reason == "insufficient_licensed_photos"
    assert "none" in selection.drop.detail
    assert selection.bucket_observations == {b.label: 0 for b in MONTH_BUCKETS}


# --- the within-bucket tiebreak claims nothing ---------------------------------


def test_agreement_counts_do_not_influence_selection(tmp_path: Path) -> None:
    # Formerly rule (d). A high agreement count means an observation drew an
    # extra identifier, which tracks being photogenic or contentious rather
    # than being right, so it must not decide which photos a learner sees.
    field = {
        "A": [
            _observation(1, "a", agreements=0),
            _observation(2, "b", agreements=0),
            _observation(3, "c", agreements=5),
            _observation(4, "d", agreements=7),
        ]
    }
    selection = _run(tmp_path, field)
    assert [p.observation_id for p in selection.photos] == [1, 2, 3, 4]

    # Same observations, agreement counts permuted: the same photos, in the
    # same order. Selection cannot see the field at all.
    permuted = {
        "A": [
            _observation(1, "a", agreements=9),
            _observation(2, "b", agreements=4),
            _observation(3, "c", agreements=0),
            _observation(4, "d", agreements=1),
        ]
    }
    other = _run(tmp_path / "permuted", permuted)
    assert [p.observation_id for p in other.photos] == [p.observation_id for p in selection.photos]


def test_within_a_bucket_order_is_ascending_observation_id(tmp_path: Path) -> None:
    selection = _run(
        tmp_path,
        {"A": [_observation(oid, f"p{oid}") for oid in (70, 10, 50, 30)]},
    )
    assert [p.observation_id for p in selection.photos] == [10, 30, 50, 70]


# --- rule (e): never relax to reach the floor ---------------------------------


def test_three_usable_photos_is_a_drop_not_a_relaxation(tmp_path: Path) -> None:
    selection = _run(tmp_path, {"A": [_observation(n, f"person{n}") for n in range(1, 4)]})
    assert selection.drop is not None
    assert selection.photos == []
    assert str(MIN_PHOTOS_PER_CANDIDATE) in selection.drop.detail


def test_extra_photos_in_one_observation_cannot_pad_the_count(tmp_path: Path) -> None:
    # Three observations carrying eight photos each is still three photos.
    selection = _run(
        tmp_path, {"A": [_observation(n, f"person{n}", photos=8) for n in range(1, 4)]}
    )
    assert selection.drop is not None


def test_the_drop_detail_names_the_rules_that_bound(tmp_path: Path) -> None:
    selection = _run(tmp_path, {"A": [_observation(n, "prolific") for n in range(1, 21)]})
    assert selection.drop is not None
    assert "per observer" in selection.drop.detail
    assert "one-per-observation" in selection.drop.detail


def test_exactly_four_is_kept(tmp_path: Path) -> None:
    selection = _run(tmp_path, {"A": [_observation(n, f"person{n}") for n in range(1, 5)]})
    assert selection.drop is None
    assert len(selection.photos) == MIN_PHOTOS_PER_CANDIDATE


# --- determinism --------------------------------------------------------------


def test_selection_is_deterministic(tmp_path: Path) -> None:
    field = {
        bucket.label: [_observation(int(f"{i}{n}"), f"p{i}_{n}") for n in range(1, 5)]
        for i, bucket in enumerate(MONTH_BUCKETS, start=1)
    }
    first = _run(tmp_path / "one", field)
    second = _run(tmp_path / "two", field)
    assert [p.inat_photo_id for p in first.photos] == [p.inat_photo_id for p in second.photos]
