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

Since M4.1 it also records per-place establishment status for the taxa the
nativity tests need — the fifteen hand-verified species, the three known
contested ones, and the two curated exclusions — plus a second batch scoped to
Arizona, which iNaturalist answers from North America. That last batch is the
only recorded case in which the place guard actually has something to refuse:
Michigan's checklist answers every Michigan query from Michigan, so a
Michigan-only fixture set would exercise the guard zero times.

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
from sift_pack.inat.nativity import fetch_establishment  # noqa: E402
from sift_pack.inat.photos import select_photos  # noqa: E402

FIXTURE_DIR = REPO_ROOT / "tests" / "fixtures" / "inat_cache"

MICHIGAN_PLACE_ID = 29

ARIZONA_PLACE_ID = 40
"""A second state, recorded to prove the place guard actually fires.

Michigan's checklist is complete enough that all 300 taxa of a real pool answer
from Michigan itself, so a Michigan-only fixture set would exercise the guard
zero times and pass whether or not it worked. Arizona answers several of these
taxa from North America instead, which is the case the guard exists for.
"""

# Tests exercise `fetch_pool(..., limit=3)`, which asks the deck stage for
# `limit * 2` taxa. Recording exactly that many makes the fixture set precisely
# what the pipeline requests — no more, and crucially no less, since a missing
# entry surfaces as a CacheMissError rather than a silent skip.
FIXTURE_POOL_LIMIT = 3
FIXTURE_DECK_LIMIT = FIXTURE_POOL_LIMIT * 2

NATIVITY_TAXON_IDS: dict[str, int] = {
    # The fifteen hand-verified spot-check species. The USDA half of this set is
    # recorded by scripts/record_usda_fixtures.py; both halves are needed now
    # that a claim requires two sources to agree.
    "Alliaria petiolata": 56061,
    "Hesperis matronalis": 47697,
    "Daucus carota": 76610,
    "Cichorium intybus": 52913,
    "Leonurus cardiaca": 56171,
    "Elaeagnus umbellata": 64697,
    "Asclepias tuberosa": 47912,
    "Monarda fistulosa": 85320,
    "Trillium grandiflorum": 55402,
    "Symplocarpus foetidus": 48961,
    "Arisaema triphyllum": 50310,
    "Phytolacca americana": 48599,
    "Pinus strobus": 52391,
    "Thuja occidentalis": 54037,
    "Impatiens capensis": 47888,
    # The known contested set: USDA calls all three L48-native, the Michigan
    # checklist calls all three introduced. They must come out as conflicts.
    "Robinia pseudoacacia": 56088,
    "Geranium robertianum": 55925,
    "Clinopodium vulgare": 84281,
    # Both sources call these native and both are wrong for a Michigan learner;
    # they are withheld by data/state_exclusions.json.
    "Echinacea purpurea": 48627,
    "Phragmites australis": 64237,
    # A demoted genus, so the CLI tests can exercise genus demotion.
    "Carex intumescens": 128095,
}
"""Taxa whose per-place establishment status is recorded, by name.

Keyed by name for readability only; the ID is the key everywhere else.
"""

SILENT_TAXON_ID = 1677724
"""A taxon the Michigan checklist has no listing for at all (`Artemisia caudata`).

Recorded as a batch of its own so a test can build a pool in which *neither*
source speaks — which is the only way to exercise the CLI's refusal to write a
pack with no cards. Michigan has a listing for almost everything, so this
condition has to be sought out rather than stumbled upon.
"""

INHERITED_TAXON_IDS: dict[str, int] = {
    "Elaeagnus umbellata": 64697,
    "Lythrum salicaria": 61321,
    "Solanum dulcamara": 55620,
}
"""Taxa iNaturalist answers for Arizona from an ancestor place.

Recorded at `ARIZONA_PLACE_ID` so the place guard has a real inherited response
to refuse, rather than a hand-written one. If upstream ever adds Arizona
listings for these, the guard test will start passing for the wrong reason —
which is why it asserts on the recorded place, not merely on the refusal.
"""


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

    # Per-place establishment status. Recorded through the production reader, so
    # the fixture is exactly what the place guard will later be replayed against.
    nativity_taxa = {
        **NATIVITY_TAXON_IDS,
        **{summary.scientific_name: summary.inat_taxon_id for summary in summaries},
    }
    nativity_ids = sorted(set(nativity_taxa.values()))
    michigan = fetch_establishment(client, nativity_ids, MICHIGAN_PLACE_ID)
    scoped = sum(1 for entry in michigan.values() if entry.usable)
    print(f"\nMichigan checklist: {scoped}/{len(nativity_ids)} taxa have a place-scoped value")

    # The cache key covers the whole parameter set, so a request for a subset of
    # these IDs is a different key and a miss. Tests therefore replay the exact
    # batch that was recorded — written out here rather than restated in the
    # test, for the same reason RECORDED_TAXON_IDS.json exists. Names travel
    # with the IDs so a test can build a resolved pool over this exact set
    # without hardcoding a name that upstream may later move.
    (FIXTURE_DIR / "RECORDED_NATIVITY_IDS.json").write_text(
        json.dumps(
            {
                "michigan": dict(sorted(nativity_taxa.items())),
                "arizona": sorted(INHERITED_TAXON_IDS.values()),
                "silent": SILENT_TAXON_ID,
            },
            indent=1,
        ),
        encoding="utf-8",
    )

    silent = fetch_establishment(client, [SILENT_TAXON_ID], MICHIGAN_PLACE_ID)[SILENT_TAXON_ID]
    print(f"  silent taxon {SILENT_TAXON_ID}: {silent.reason}")
    if silent.usable:
        print(
            "  WARNING: the silent taxon now has a Michigan listing, so no recorded pool "
            "leaves both sources speechless and the empty-pack test cannot fire. Pick another."
        )

    arizona = fetch_establishment(client, sorted(INHERITED_TAXON_IDS.values()), ARIZONA_PLACE_ID)
    inherited = {
        species: arizona[taxon_id]
        for species, taxon_id in INHERITED_TAXON_IDS.items()
        if arizona[taxon_id].reason == "place_not_state_scoped"
    }
    for species, refused in sorted(inherited.items()):
        print(f"  {species}: Arizona answered from {refused.place_name} ({refused.place_id})")
    if not inherited:
        print(
            "  WARNING: no Arizona lookup was answered from an ancestor place. The place "
            "guard now has no recorded case to refuse, and tests/test_inat_nativity.py "
            "will fail rather than silently pass. Pick different taxa."
        )

    print(f"\n{client.stats.summary()}")


if __name__ == "__main__":
    main()
