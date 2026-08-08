"""Copy the PLANTS responses for the spot-check species into `tests/fixtures/`.

WHY THIS SCRIPT EXISTS
----------------------
The spot-check test asserts that fifteen hand-verified Michigan species get the
right nativity label. That is only worth anything if it runs against what USDA
actually said, so the fixtures are real recorded PLANTS responses rather than
hand-written ones — and because the responses are already projected and cached
by a normal `promote-pack` run, recording them means copying, not re-fetching.

RUN IT
------
    uv run python scripts/record_usda_fixtures.py

Requires a warm `cache/usda` from a promote run. Makes no network requests of
its own; if an entry is missing it says so rather than fetching, because a
fixture recorded by a different code path is not evidence about this one.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from sift_pack.usda.client import PlantsClient  # noqa: E402

SOURCE_CACHE = REPO_ROOT / "cache" / "usda"
FIXTURE_CACHE = REPO_ROOT / "tests" / "fixtures" / "usda_cache"

NATIVE = [
    "Asclepias tuberosa",
    "Monarda fistulosa",
    "Trillium grandiflorum",
    "Symplocarpus foetidus",
    "Arisaema triphyllum",
    "Phytolacca americana",
    "Pinus strobus",
    "Thuja occidentalis",
    "Impatiens capensis",
]
INTRODUCED = [
    "Alliaria petiolata",
    "Hesperis matronalis",
    "Daucus carota",
    "Cichorium intybus",
    "Leonurus cardiaca",
    "Elaeagnus umbellata",
]


SUPPORTING = [
    # Not part of the hand-verified ground truth; recorded because the CLI
    # tests need a taxon in a demoted genus to exercise genus demotion.
    "Carex intumescens",
]


def main() -> None:
    """Copy each spot-check species' search and profile entries."""
    if FIXTURE_CACHE.exists():
        shutil.rmtree(FIXTURE_CACHE)
    source = PlantsClient(SOURCE_CACHE, offline=True)
    destination = PlantsClient(FIXTURE_CACHE, offline=True)

    copied = 0
    for name in sorted(NATIVE + INTRODUCED + SUPPORTING):
        search_from = source._path("search", name)  # noqa: SLF001 - copying cache entries by key
        if not search_from.exists():
            print(f"MISSING search for {name}; run promote-pack first")
            continue
        search_to = destination._path("search", name)  # noqa: SLF001 - ditto
        search_to.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(search_from, search_to)
        copied += 1

        for record in source.search(name):
            symbol = record.accepted_symbol or record.symbol
            profile_from = source._path("profile", symbol)  # noqa: SLF001 - ditto
            if not profile_from.exists():
                continue
            profile_to = destination._path("profile", symbol)  # noqa: SLF001 - ditto
            profile_to.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(profile_from, profile_to)
            copied += 1

    (FIXTURE_CACHE / "SPOT_CHECK.json").write_text(
        json.dumps({"native": NATIVE, "introduced": INTRODUCED}, indent=1),
        encoding="utf-8",
    )
    print(f"copied {copied} cache entries into {FIXTURE_CACHE.relative_to(REPO_ROOT)}")


if __name__ == "__main__":
    main()
