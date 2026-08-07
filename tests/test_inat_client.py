"""Tests for the cache: keys, hits, misses, and the zero-network guarantee."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from sift_pack.inat.client import (
    CacheMissError,
    Endpoint,
    InatClient,
    InatError,
    Params,
    cache_key,
)


class CountingFetcher:
    """A fetcher that records what it was asked for and never touches a socket."""

    def __init__(self) -> None:
        """Start with an empty call log."""
        self.calls: list[tuple[str, dict[str, object]]] = []

    def fetch(self, endpoint: Endpoint, params: Params) -> dict[str, object]:
        """Record the request and return a deterministic stand-in response."""
        self.calls.append((endpoint, dict(params)))
        return {"results": [{"id": len(self.calls)}], "total_results": 1}


# --- cache keys ---------------------------------------------------------------


def test_key_is_order_independent() -> None:
    assert cache_key("observations", {"a": 1, "b": 2}) == cache_key(
        "observations", {"b": 2, "a": 1}
    )


def test_key_changes_with_any_parameter() -> None:
    base = cache_key("observations", {"taxon_id": 1, "place_id": 29})
    assert base != cache_key("observations", {"taxon_id": 2, "place_id": 29})
    assert base != cache_key("observations", {"taxon_id": 1, "place_id": 30})
    assert base != cache_key("observations", {"taxon_id": 1})


def test_key_changes_with_endpoint() -> None:
    assert cache_key("observations", {"x": 1}) != cache_key("species_counts", {"x": 1})


def test_sequence_values_do_not_collide_with_their_joined_string() -> None:
    # ["CC0", "CC-BY"] and "CC0,CC-BY" mean different things to the API.
    assert cache_key("observations", {"photo_license": ["CC0", "CC-BY"]}) != cache_key(
        "observations", {"photo_license": "CC0,CC-BY"}
    )


def test_tuples_and_lists_share_a_key() -> None:
    assert cache_key("observations", {"x": ("a", "b")}) == cache_key(
        "observations", {"x": ["a", "b"]}
    )


# --- hits and misses ----------------------------------------------------------


def test_first_call_misses_and_second_hits(tmp_path: Path) -> None:
    fetcher = CountingFetcher()
    client = InatClient(tmp_path, fetcher=fetcher)

    first = client.get("observations", {"taxon_id": 1})
    second = client.get("observations", {"taxon_id": 1})

    assert first == second
    assert len(fetcher.calls) == 1
    assert client.stats.misses == 1
    assert client.stats.hits == 1


def test_a_fresh_client_over_a_warm_cache_makes_no_calls(tmp_path: Path) -> None:
    # This is the resumability guarantee: a killed run replays from disk.
    warm = InatClient(tmp_path, fetcher=CountingFetcher())
    warm.get("observations", {"taxon_id": 1})
    warm.get("species_counts", {"place_id": 29})

    fetcher = CountingFetcher()
    resumed = InatClient(tmp_path, fetcher=fetcher)
    resumed.get("observations", {"taxon_id": 1})
    resumed.get("species_counts", {"place_id": 29})

    assert fetcher.calls == []
    assert resumed.stats.misses == 0
    assert resumed.stats.hits == 2


def test_different_params_do_not_share_a_cache_entry(tmp_path: Path) -> None:
    fetcher = CountingFetcher()
    client = InatClient(tmp_path, fetcher=fetcher)
    client.get("observations", {"taxon_id": 1})
    client.get("observations", {"taxon_id": 2})
    assert len(fetcher.calls) == 2


def test_miss_counts_are_broken_down_by_endpoint(tmp_path: Path) -> None:
    client = InatClient(tmp_path, fetcher=CountingFetcher())
    client.get("observations", {"taxon_id": 1})
    client.get("observations", {"taxon_id": 2})
    client.get("species_counts", {"place_id": 29})
    assert client.stats.by_endpoint == {"observations": 2, "species_counts": 1}
    assert "observations=2" in client.stats.summary()


def test_misses_are_logged(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    # "Log every miss so build cost is visible" — a silent miss is an
    # unaccounted-for request.
    with caplog.at_level("INFO", logger="sift_pack.inat.client"):
        InatClient(tmp_path, fetcher=CountingFetcher()).get("observations", {"taxon_id": 1})
    assert any("cache miss" in record.message for record in caplog.records)


# --- offline mode -------------------------------------------------------------


def test_offline_client_raises_on_a_miss(tmp_path: Path) -> None:
    client = InatClient(tmp_path, offline=True)
    with pytest.raises(CacheMissError, match="no cached response"):
        client.get("observations", {"taxon_id": 1})


def test_offline_error_names_the_request(tmp_path: Path) -> None:
    client = InatClient(tmp_path, offline=True)
    with pytest.raises(CacheMissError, match="taxon_id"):
        client.get("observations", {"taxon_id": 4242})


def test_offline_client_serves_warm_entries(tmp_path: Path) -> None:
    InatClient(tmp_path, fetcher=CountingFetcher()).get("observations", {"taxon_id": 1})
    client = InatClient(tmp_path, offline=True)
    assert client.get("observations", {"taxon_id": 1})["total_results"] == 1


# --- corrupt cache is reported, not papered over ------------------------------


def test_unreadable_cache_entry_raises(tmp_path: Path) -> None:
    client = InatClient(tmp_path, fetcher=CountingFetcher())
    client.get("observations", {"taxon_id": 1})
    path = tmp_path / "observations" / f"{cache_key('observations', {'taxon_id': 1})}.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(InatError, match="could not be read"):
        client.get("observations", {"taxon_id": 1})


def test_cache_entry_missing_its_envelope_raises(tmp_path: Path) -> None:
    client = InatClient(tmp_path, fetcher=CountingFetcher())
    client.get("observations", {"taxon_id": 1})
    path = tmp_path / "observations" / f"{cache_key('observations', {'taxon_id': 1})}.json"
    path.write_text(json.dumps({"nope": 1}), encoding="utf-8")
    with pytest.raises(InatError, match="not a Sift cache envelope"):
        client.get("observations", {"taxon_id": 1})


def test_cache_envelope_records_what_produced_it(tmp_path: Path) -> None:
    # A cache directory must be auditable without reversing the hash.
    client = InatClient(tmp_path, fetcher=CountingFetcher())
    client.get("observations", {"taxon_id": 77})
    path = tmp_path / "observations" / f"{cache_key('observations', {'taxon_id': 77})}.json"
    envelope = json.loads(path.read_text(encoding="utf-8"))
    assert envelope["endpoint"] == "observations"
    assert envelope["params"] == {"taxon_id": 77}
    assert "fetched_at" in envelope


# --- responses are normalised so hits and misses are indistinguishable --------


class DatetimeFetcher:
    """Returns a `datetime`, the way pyinaturalist does after parsing timestamps."""

    def fetch(self, endpoint: Endpoint, params: Params) -> dict[str, object]:
        """Return a body containing a parsed datetime, as pyinaturalist does."""
        del endpoint, params
        return {"results": [{"observed_on": datetime(2026, 8, 7, tzinfo=UTC)}]}


def test_a_live_result_matches_the_same_result_from_cache(tmp_path: Path) -> None:
    client = InatClient(tmp_path, fetcher=DatetimeFetcher())
    live = client.get("observations", {"taxon_id": 1})
    cached = client.get("observations", {"taxon_id": 1})
    assert live == cached
    assert live["results"][0]["observed_on"] == "2026-08-07T00:00:00+00:00"


class UnserialisableFetcher:
    """Returns something with no JSON form, to prove it is not coerced silently."""

    def fetch(self, endpoint: Endpoint, params: Params) -> dict[str, object]:
        """Return a body containing a value with no JSON representation."""
        del endpoint, params
        return {"results": [{"weird": object()}]}


def test_a_value_with_no_json_form_is_reported_not_stringified(tmp_path: Path) -> None:
    client = InatClient(tmp_path, fetcher=UnserialisableFetcher())
    with pytest.raises(InatError, match="has no JSON form"):
        client.get("observations", {"taxon_id": 1})
