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
from sift_pack.inat.photos import MONTH_BUCKETS, observations_query

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

# The exact per-place establishment batches that were recorded. A cache key
# covers the whole parameter set, so asking about a subset of these IDs is a
# different key and a miss — tests replay the batch verbatim. Read from the
# fixture set for the same reason RECORDED_TAXON_IDS is: a restated copy would
# drift the moment the recorded set changed.
_NATIVITY = json.loads((FIXTURE_CACHE / "RECORDED_NATIVITY_IDS.json").read_text(encoding="utf-8"))
RECORDED_NATIVITY_TAXA: dict[str, int] = _NATIVITY["michigan"]
RECORDED_NATIVITY_IDS: list[int] = sorted(set(RECORDED_NATIVITY_TAXA.values()))
RECORDED_INHERITED_IDS: list[int] = _NATIVITY["arizona"]

RECORDED_SILENT_TAXON_ID: int = _NATIVITY["silent"]
"""A taxon the Michigan checklist has no listing for, recorded as its own batch.

Lets a test build a pool in which neither nativity source speaks — the only way
to reach the CLI's refusal to write a pack with no cards.
"""

ARIZONA_PLACE_ID = 40
"""A state iNaturalist answers from an ancestor place, so the guard has work to do."""


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


def observations_params(taxon_id: int, bucket_label: str = "A") -> dict[str, ParamValue]:
    """The exact parameters `select_photos` uses for one seasonal bucket.

    Delegates to the production query builder rather than restating it: with
    four requests per taxon, a restated copy would drift silently and every
    fixture lookup would miss.

    Args:
        taxon_id: Taxon whose observations are wanted.
        bucket_label: Which seasonal bucket, by label.

    Returns:
        The parameter mapping.

    Example:
        >>> observations_params(47911)["taxon_id"]
        47911
    """
    bucket = next(b for b in MONTH_BUCKETS if b.label == bucket_label)
    return observations_query(taxon_id, MICHIGAN_PLACE_ID, bucket)
