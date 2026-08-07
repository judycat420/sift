"""Record iNaturalist responses into `tests/fixtures/inat_cache/`.

WHY THIS SCRIPT EXISTS
----------------------
Tests never touch the network (STANDARDS.md rule 6), so the suite needs real
responses on disk. Since M2.1 the client caches projections rather than raw
bodies, and a fixture directory is simply a cache directory — so recording is
just running the real pipeline with its cache pointed at `tests/fixtures/`.

That is the whole script now. It used to carry its own copy of the projections,
which was a second definition of "what Sift reads" and could drift from the one
the parsers actually used. There is now one definition, in
`sift_pack.inat.projections`, and fixtures are produced by the same code path
that later replays them.

WHAT IS RECORDED
----------------
Exactly what the pipeline requests for a small pool: the deck stage's
`species_counts` page, the `taxa_by_id` batch for the taxa it selects, and four
month-bucketed `observations` requests per taxon. The taxon list is derived from
the recorded deck response rather than hardcoded, so the fixture set cannot
drift out of step when upstream observation counts reorder the deck.

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

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sift_pack.domains.plants import PlantsDomain  # noqa: E402
from sift_pack.inat.client import InatClient  # noqa: E402
from sift_pack.inat.deck import fetch_taxon_details, select_taxa  # noqa: E402
from sift_pack.inat.photos import select_photos  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "inat_cache"

MICHIGAN_PLACE_ID = 29

# Tests exercise `fetch_pool(..., limit=3)`, which asks the deck stage for
# `limit * 2` taxa. Recording exactly that many makes the fixture set precisely
# what the pipeline requests — no more, and crucially no less, since a missing
# entry surfaces as a CacheMissError rather than a silent skip.
FIXTURE_POOL_LIMIT = 3
FIXTURE_DECK_LIMIT = FIXTURE_POOL_LIMIT * 2


def main() -> None:
    """Record the full fixture set by running the pipeline against it."""
    if FIXTURE_DIR.exists():
        shutil.rmtree(FIXTURE_DIR)

    client = InatClient(FIXTURE_DIR)
    domain = PlantsDomain()

    # Places: Michigan is used throughout; Washington gives the resolver a
    # second state to prove it picks the admin-level-10 record.
    for name in ("Michigan", "Washington"):
        client.get("places_autocomplete", {"q": name})

    summaries, _ = select_taxa(
        client, MICHIGAN_PLACE_ID, domain.iconic_taxon_id, FIXTURE_DECK_LIMIT
    )
    taxon_ids = [summary.inat_taxon_id for summary in summaries]
    print(f"deck stage selected {taxon_ids}")

    fetch_taxon_details(client, taxon_ids)

    for summary in summaries:
        selection = select_photos(
            client, summary.inat_taxon_id, summary.scientific_name, MICHIGAN_PLACE_ID
        )
        yields = ", ".join(
            f"{label}={count}" for label, count in sorted(selection.bucket_observations.items())
        )
        outcome = "dropped" if selection.drop is not None else f"{len(selection.photos)} photos"
        print(f"  {summary.scientific_name}: {outcome} (buckets {yields})")

    (FIXTURE_DIR / "RECORDED_TAXON_IDS.json").write_text(json.dumps(taxon_ids), encoding="utf-8")
    print(f"\n{client.stats.summary()}")


if __name__ == "__main__":
    main()
