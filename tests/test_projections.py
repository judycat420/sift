"""Tests for the projected cache: what it keeps, what it drops, and how it versions."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from sift_pack.inat.client import (
    CACHE_FORMAT,
    Endpoint,
    InatClient,
    InatError,
    LegacyCacheError,
    Params,
    cache_key,
)
from sift_pack.inat.projections import PROJECTION_VERSION, project

FAT_OBSERVATION: dict[str, Any] = {
    "id": 100,
    "uri": "https://www.inaturalist.org/observations/100",
    "num_identification_agreements": 3,
    "user": {"login": "someone", "name": "Some One", "roles": [], "site_id": 1},
    "photos": [
        {
            "id": 900,
            "license_code": "cc0",
            "original_dimensions": {"width": 100, "height": 200},
            "url": "https://example.invalid/square.jpg",
            "attribution": "no rights reserved",
            "flags": [],
            "moderator_actions": [],
            "hidden": False,
        }
    ],
    "geoprivacy": "obscured",
    "comments": [{"body": "nice find"}],
    "identifications": [{"id": 1}, {"id": 2}],
    "quality_metrics": [],
    "project_observations": [],
}


class FatFetcher:
    """Returns a response with far more fields than Sift reads."""

    def __init__(self) -> None:
        """Start with an empty call log."""
        self.calls = 0

    def fetch(self, endpoint: Endpoint, params: Params) -> dict[str, Any]:
        """Return one fat observation."""
        del endpoint, params
        self.calls += 1
        return {"total_results": 705, "results": [FAT_OBSERVATION], "page": 1, "per_page": 25}


# --- what projection keeps and drops ------------------------------------------


def test_projection_keeps_the_fields_the_parsers_read() -> None:
    projected = project("observations", {"results": [FAT_OBSERVATION]})
    entry = projected["results"][0]
    assert entry["id"] == 100
    assert entry["uri"] == "https://www.inaturalist.org/observations/100"
    assert entry["num_identification_agreements"] == 3
    assert entry["user"] == {"login": "someone", "name": "Some One"}
    assert entry["photos"][0]["license_code"] == "cc0"
    assert entry["photos"][0]["original_dimensions"] == {"width": 100, "height": 200}


@pytest.mark.parametrize(
    "unread",
    ["geoprivacy", "comments", "identifications", "quality_metrics", "project_observations"],
)
def test_projection_drops_fields_no_parser_reads(unread: str) -> None:
    entry = project("observations", {"results": [FAT_OBSERVATION]})["results"][0]
    assert unread not in entry


def test_projection_drops_unread_photo_fields() -> None:
    photo = project("observations", {"results": [FAT_OBSERVATION]})["results"][0]["photos"][0]
    assert set(photo) == {"id", "license_code", "original_dimensions", "url"}


def test_projection_preserves_record_counts() -> None:
    # Dropping a record would make an empty bucket indistinguishable from a
    # bucket whose records were all unreadable.
    response = {"results": [FAT_OBSERVATION, "not an object", {"id": 2}]}
    assert len(project("observations", response)["results"]) == 3


def test_a_malformed_record_survives_as_none() -> None:
    projected = project("observations", {"results": ["not an object"]})
    assert projected["results"] == [None]


def test_projection_preserves_absence_rather_than_defaulting() -> None:
    projected = project("observations", {"results": [{"id": 1}]})
    entry = projected["results"][0]
    assert entry["uri"] is None
    assert entry["user"] is None
    assert entry["photos"] is None
    assert entry["num_identification_agreements"] is None


def test_projection_keeps_total_results() -> None:
    assert project("observations", {"total_results": 705, "results": []})["total_results"] == 705


def test_an_unprojected_endpoint_raises_rather_than_passing_through() -> None:
    # A passthrough default would silently cache a whole response body.
    with pytest.raises(KeyError):
        project("some_new_endpoint", {"results": []})


def test_projection_shrinks_the_payload() -> None:
    fat = len(json.dumps({"results": [FAT_OBSERVATION]}))
    thin = len(json.dumps(project("observations", {"results": [FAT_OBSERVATION]})))
    assert thin < fat * 0.6


# --- the cache stores projections ---------------------------------------------


def test_the_client_caches_and_returns_the_projection(tmp_path: Path) -> None:
    fetcher = FatFetcher()
    client = InatClient(tmp_path, fetcher=fetcher)
    live = client.get("observations", {"taxon_id": 1})
    assert "geoprivacy" not in live["results"][0]

    entry = tmp_path / "observations" / f"{cache_key('observations', {'taxon_id': 1})}.json"
    on_disk = json.loads(entry.read_text(encoding="utf-8"))
    assert "geoprivacy" not in on_disk["response"]["results"][0]
    assert on_disk["projection_version"] == PROJECTION_VERSION


def test_a_cached_projection_matches_the_live_one(tmp_path: Path) -> None:
    client = InatClient(tmp_path, fetcher=FatFetcher())
    assert client.get("observations", {"taxon_id": 1}) == client.get(
        "observations", {"taxon_id": 1}
    )


def test_raw_bodies_are_not_kept_by_default(tmp_path: Path) -> None:
    InatClient(tmp_path, fetcher=FatFetcher()).get("observations", {"taxon_id": 1})
    assert not (tmp_path / "raw").exists()


def test_keep_raw_writes_untouched_bodies(tmp_path: Path) -> None:
    client = InatClient(tmp_path, fetcher=FatFetcher(), keep_raw=True)
    client.get("observations", {"taxon_id": 1})
    raw_files = list((tmp_path / "raw" / "observations").glob("*.json"))
    assert len(raw_files) == 1
    body = json.loads(raw_files[0].read_text(encoding="utf-8"))
    assert body["results"][0]["geoprivacy"] == "obscured"


def test_deleting_the_raw_directory_does_not_affect_correctness(tmp_path: Path) -> None:
    fetcher = FatFetcher()
    client = InatClient(tmp_path, fetcher=fetcher, keep_raw=True)
    first = client.get("observations", {"taxon_id": 1})
    shutil.rmtree(tmp_path / "raw")
    assert client.get("observations", {"taxon_id": 1}) == first
    assert fetcher.calls == 1


# --- versioning ---------------------------------------------------------------


def test_the_projection_version_is_part_of_the_key() -> None:
    assert cache_key("observations", {"a": 1}, 1) != cache_key("observations", {"a": 1}, 2)


def test_raw_entries_are_keyed_without_a_projection_version() -> None:
    # So debug copies survive a version bump rather than being re-fetched.
    assert cache_key("observations", {"a": 1}, None) != cache_key("observations", {"a": 1}, 1)


def test_bumping_the_version_invalidates_rather_than_misreads(tmp_path: Path) -> None:
    fetcher = FatFetcher()
    client = InatClient(tmp_path, fetcher=fetcher)
    client.get("observations", {"taxon_id": 1})

    # An entry written under a different version is at a different path, so the
    # new build cannot read it — it re-fetches instead of serving a stale shape.
    other = tmp_path / "observations" / f"{cache_key('observations', {'taxon_id': 1}, 99)}.json"
    assert not other.exists()


# --- legacy cache detection ---------------------------------------------------


def test_a_pre_m21_cache_is_detected_and_reported(tmp_path: Path) -> None:
    # A raw-body cache with no format marker: unreadable rather than
    # misreadable, but gigabytes of dead weight worth telling somebody about.
    legacy = tmp_path / "observations"
    legacy.mkdir(parents=True)
    (legacy / "deadbeef.json").write_text(json.dumps({"response": {}}), encoding="utf-8")

    with pytest.raises(LegacyCacheError, match=re.escape("pre-M2.1 format")):
        InatClient(tmp_path)


def test_the_legacy_error_says_how_to_fix_it(tmp_path: Path) -> None:
    legacy = tmp_path / "species_counts"
    legacy.mkdir(parents=True)
    (legacy / "deadbeef.json").write_text("{}", encoding="utf-8")
    with pytest.raises(LegacyCacheError, match=re.escape("rm -rf")):
        InatClient(tmp_path)


def test_an_empty_directory_is_claimed_not_rejected(tmp_path: Path) -> None:
    client = InatClient(tmp_path)
    marker = json.loads((tmp_path / ".sift-cache-format.json").read_text(encoding="utf-8"))
    assert marker == {"format": CACHE_FORMAT, "projection_version": PROJECTION_VERSION}
    assert client.stats.misses == 0


def test_a_marked_cache_is_reopened_without_complaint(tmp_path: Path) -> None:
    InatClient(tmp_path, fetcher=FatFetcher()).get("observations", {"taxon_id": 1})
    reopened = InatClient(tmp_path, offline=True)
    assert reopened.get("observations", {"taxon_id": 1})["total_results"] == 705


def test_a_foreign_marker_is_rejected(tmp_path: Path) -> None:
    (tmp_path).mkdir(parents=True, exist_ok=True)
    (tmp_path / ".sift-cache-format.json").write_text(
        json.dumps({"format": "somebody-elses-cache"}), encoding="utf-8"
    )
    with pytest.raises(InatError, match="does not describe a Sift cache"):
        InatClient(tmp_path)


def test_an_unreadable_marker_is_reported(tmp_path: Path) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".sift-cache-format.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(InatError, match=r"marker .* is unreadable"):
        InatClient(tmp_path)


def test_an_older_projection_version_warns_and_is_reclaimed(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / ".sift-cache-format.json").write_text(
        json.dumps({"format": CACHE_FORMAT, "projection_version": PROJECTION_VERSION - 1}),
        encoding="utf-8",
    )
    with caplog.at_level("WARNING", logger="sift_pack.inat.client"):
        InatClient(tmp_path)
    assert any("prunable" in record.message for record in caplog.records)
    marker = json.loads((tmp_path / ".sift-cache-format.json").read_text(encoding="utf-8"))
    assert marker["projection_version"] == PROJECTION_VERSION
