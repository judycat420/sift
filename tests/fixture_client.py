"""Shared helper: an `InatClient` backed by recorded fixtures, never the network.

WHY THIS MODULE EXISTS
----------------------
Fixtures are stored in exactly the envelope format `InatClient` writes, so a
test client is just a real client pointed at `tests/fixtures/inat_cache/` with
`offline=True`. That means tests exercise the production cache path — key
derivation, envelope parsing, the lot — with no HTTP library and no mock in the
loop. A test that reaches past its fixtures raises `CacheMissError` naming the
request it wanted, which is a far better failure message than a socket error.
"""

from __future__ import annotations

import json
from pathlib import Path

from sift_pack.inat.client import InatClient, ParamValue

FIXTURE_CACHE = Path(__file__).parent / "fixtures" / "inat_cache"

MICHIGAN_PLACE_ID = 29
PLANTAE_ICONIC_TAXON_ID = 47126

# Read from the fixture set rather than restated here: the recorder derives the
# list from the recorded species_counts response, so a hardcoded copy would
# drift the moment observation counts reorder the deck.
RECORDED_TAXON_IDS: list[int] = json.loads(
    (FIXTURE_CACHE / "RECORDED_TAXON_IDS.json").read_text(encoding="utf-8")
)

# `fetch_pool` asks the deck stage for `limit * 2`, so this is the largest pool
# the recorded fixtures can serve.
FIXTURE_POOL_LIMIT = len(RECORDED_TAXON_IDS) // 2


def recorded_client() -> InatClient:
    """Build an offline client over the recorded fixtures.

    Returns:
        A client that serves recorded responses and raises `CacheMissError` for
        anything not recorded.

    Example:
        >>> recorded_client().offline
        True
    """
    return InatClient(FIXTURE_CACHE, offline=True)


def observations_params(taxon_id: int) -> dict[str, ParamValue]:
    """The exact parameters `select_photos` uses, so a test can find its fixture.

    Restated from `sift_pack.inat.photos` rather than imported, so that a change
    to the query shape fails a test instead of silently re-keying the cache and
    quietly re-fetching everything.

    Args:
        taxon_id: Taxon whose observations are wanted.

    Returns:
        The parameter mapping.

    Example:
        >>> observations_params(47911)["taxon_id"]
        47911
    """
    params: dict[str, ParamValue] = {
        "taxon_id": taxon_id,
        "place_id": MICHIGAN_PLACE_ID,
        "quality_grade": "research",
        "photo_license": ("CC0", "CC-BY", "CC-BY-SA"),
        "per_page": 200,
        "order_by": "votes",
        "order": "desc",
    }
    return params
