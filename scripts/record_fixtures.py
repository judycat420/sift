"""Record trimmed iNaturalist responses into `tests/fixtures/inat_cache/`.

WHY THIS SCRIPT EXISTS
----------------------
Tests never touch the network (STANDARDS.md rule 6), so the suite needs real
responses on disk. This records them, in exactly the envelope format
`InatClient` writes, so a test can point a client at the fixture directory with
`offline=True` and exercise the production code path with no HTTP library and no
mock in the loop at all.

WHAT "TRIMMED" MEANS
--------------------
A single observations page at `per_page=200` is several megabytes, mostly fields
Sift never reads. Each response is projected down to the fields the parsers
actually consume, by the mechanical projections below — no value is altered,
reordered or invented, and the projection is committed so it is auditable. A
fixture that no longer matches upstream must be re-recorded, never hand-edited
(see `tests/fixtures/README.md`).

RUN IT
------
    uv run python scripts/record_fixtures.py

It makes live requests, so it is run by hand and never in CI.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sift_pack.inat.client import InatClient, cache_key  # noqa: E402
from sift_pack.inat.deck import SPECIES_COUNTS_PAGE_SIZE, select_taxa  # noqa: E402
from sift_pack.inat.photos import OBSERVATIONS_PAGE_SIZE  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "inat_cache"
RECORD_CACHE = REPO_ROOT / "cache"

MICHIGAN_PLACE_ID = 29
PLANTAE = "Plantae"
PLANTAE_ICONIC_TAXON_ID = 47126

# Tests exercise `fetch_pool(..., limit=3)`, which asks the deck stage for
# `limit * 2` taxa. Recording exactly that many means the fixture set is
# precisely what the pipeline requests — no more, and crucially no less, since
# a missing one surfaces as a CacheMissError rather than a silent skip.
FIXTURE_POOL_LIMIT = 3
FIXTURE_DECK_LIMIT = FIXTURE_POOL_LIMIT * 2


def _project_photo(photo: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": photo.get("id"),
        "license_code": photo.get("license_code"),
        "original_dimensions": photo.get("original_dimensions"),
        "url": photo.get("url"),
    }


def _project_observation(observation: dict[str, Any]) -> dict[str, Any]:
    user = observation.get("user") or {}
    return {
        "id": observation.get("id"),
        "uri": observation.get("uri"),
        "num_identification_agreements": observation.get("num_identification_agreements"),
        "user": {"login": user.get("login"), "name": user.get("name")},
        "photos": [_project_photo(p) for p in observation.get("photos", [])],
    }


def _project_species_count(entry: dict[str, Any]) -> dict[str, Any]:
    taxon = entry.get("taxon") or {}
    return {
        "count": entry.get("count"),
        "taxon": {
            "id": taxon.get("id"),
            "name": taxon.get("name"),
            "rank": taxon.get("rank"),
            "preferred_common_name": taxon.get("preferred_common_name"),
        },
    }


def _project_taxon_detail(taxon: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": taxon.get("id"),
        "name": taxon.get("name"),
        "ancestors": [
            {"rank": a.get("rank"), "name": a.get("name")} for a in taxon.get("ancestors", [])
        ],
    }


def _project_place(place: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": place.get("id"),
        "name": place.get("name"),
        "admin_level": place.get("admin_level"),
        "ancestor_place_ids": place.get("ancestor_place_ids"),
    }


_PROJECTIONS = {
    "observations": _project_observation,
    "species_counts": _project_species_count,
    "taxa_by_id": _project_taxon_detail,
    "places_autocomplete": _project_place,
}


def record(client: InatClient, endpoint: str, params: dict[str, Any], limit: int | None) -> None:
    """Fetch one request live and write its projected response to the fixture dir."""
    response = client.get(endpoint, params)  # type: ignore[arg-type] # endpoint is Literal at call sites below
    project = _PROJECTIONS[endpoint]
    results = response.get("results", [])
    if limit is not None:
        results = results[:limit]

    projected = {
        "total_results": response.get("total_results"),
        "results": [project(item) for item in results],
    }
    envelope = {
        "endpoint": endpoint,
        "params": params,
        "recorded_by": "scripts/record_fixtures.py",
        "note": "projected to the fields Sift reads; values are verbatim",
        "response": projected,
    }
    destination = FIXTURE_DIR / endpoint / f"{cache_key(endpoint, params)}.json"  # type: ignore[arg-type] # ditto
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(envelope, indent=1, sort_keys=True), encoding="utf-8")
    print(f"recorded {endpoint} -> {destination.relative_to(REPO_ROOT)}")


def main() -> None:
    """Record the full fixture set."""
    if FIXTURE_DIR.exists():
        shutil.rmtree(FIXTURE_DIR)
    client = InatClient(RECORD_CACHE)

    record(client, "places_autocomplete", {"q": "Michigan"}, limit=6)
    record(client, "places_autocomplete", {"q": "Washington"}, limit=6)

    record(
        client,
        "species_counts",
        {
            "place_id": MICHIGAN_PLACE_ID,
            "iconic_taxa": PLANTAE,
            "quality_grade": "research",
            "per_page": SPECIES_COUNTS_PAGE_SIZE,
            "page": 1,
        },
        limit=40,
    )

    # Ask the real deck stage, reading the fixture just written, which taxa the
    # pipeline will go on to request. Deriving the list beats hardcoding it:
    # a hardcoded list silently drifts out of step when upstream counts change.
    summaries, _ = select_taxa(
        InatClient(FIXTURE_DIR, offline=True),
        MICHIGAN_PLACE_ID,
        PLANTAE_ICONIC_TAXON_ID,
        FIXTURE_DECK_LIMIT,
    )
    taxon_ids = [summary.inat_taxon_id for summary in summaries]
    print(f"deck stage selected {taxon_ids}")

    record(client, "taxa_by_id", {"ids": taxon_ids}, limit=None)

    for taxon_id in taxon_ids:
        record(
            client,
            "observations",
            {
                "taxon_id": taxon_id,
                "place_id": MICHIGAN_PLACE_ID,
                "quality_grade": "research",
                "photo_license": ("CC0", "CC-BY", "CC-BY-SA"),
                "per_page": OBSERVATIONS_PAGE_SIZE,
                "order_by": "votes",
                "order": "desc",
            },
            limit=30,
        )

    (FIXTURE_DIR / "RECORDED_TAXON_IDS.json").write_text(json.dumps(taxon_ids), encoding="utf-8")
    print(f"\n{client.stats.summary()}")


if __name__ == "__main__":
    main()
