"""Building the nativity index for a whole pool, and recording what it cost.

WHY THIS MODULE EXISTS
----------------------
`reconcile` decides one taxon at a time. Something has to run it across a pool,
keep the successes in a form the domain can look up, keep the failures in a form
the unmatched report can print, and count the tiers so the build can be
judged — and that bookkeeping does not belong inside the matching rules, where
it would obscure them.

Since M4.1 this stage stops short of producing the index. PLANTS is one of two
nativity sources, and which claim a taxon ends up with depends on what the other
one said (`sift_pack.nativity`). So what comes out here is the full
`Reconciliation` per taxon — matched or not, with its reason — and the join
happens where both sources are in view.

INVARIANT PROTECTED
-------------------
Every taxon in the pool appears in the returned mapping exactly once, matched or
rejected. A taxon in neither would be one that quietly vanished between the
resolved pool and the manifest, which is the specific failure the unmatched
report exists to make impossible.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path

from sift_pack.resolved import ResolvedPool
from sift_pack.usda.client import PlantsClient
from sift_pack.usda.reconcile import Reconciliation, reconcile

__all__ = [
    "DEFAULT_USDA_CACHE",
    "USDA_CACHE_SUBDIR",
    "plants_source_version",
    "reconcile_pool",
]

_log = logging.getLogger(__name__)

USDA_CACHE_SUBDIR = "usda"
"""Where the PLANTS cache sits inside the response-cache root.

A subdirectory rather than a sibling because the two caches are written by
different clients with different formats, and `InatClient` only ever looks at
its own endpoint directories — so they have always coexisted under one root and
a caller only needs to name the root.
"""

DEFAULT_USDA_CACHE = Path("cache") / USDA_CACHE_SUBDIR


def plants_source_version(cache_dir: Path = DEFAULT_USDA_CACHE) -> date:
    """The version stamp recorded on every claim derived from PLANTS.

    PLANTS exposes no publication date, edition number or dataset version
    through its services API — the profile endpoint returns taxonomy and status
    with nothing to say when either last changed. So the retrieval date is used,
    and it is genuinely weaker than a publication date: two retrievals months
    apart are distinguishable only by this stamp, and a PLANTS revision between
    them leaves no other trace. It is the best available answer, not a good one.
    See `docs/decisions.md`, 2026-08-08.

    Args:
        cache_dir: PLANTS response cache. Its oldest entry dates the data, since
            a cached response is what a claim was actually derived from.

    Returns:
        The retrieval date.

    Example:
        >>> from pathlib import Path
        >>> isinstance(plants_source_version(Path("nope")), date)
        True
    """
    entries = list(cache_dir.rglob("*.json")) if cache_dir.is_dir() else []
    if not entries:
        return datetime.now(UTC).date()
    oldest = min(entry.stat().st_mtime for entry in entries)
    return datetime.fromtimestamp(oldest, tz=UTC).date()


def reconcile_pool(
    client: PlantsClient,
    pool: ResolvedPool,
) -> tuple[dict[int, Reconciliation], dict[str, int]]:
    """Reconcile every taxon in a pool against PLANTS.

    Returns the whole `Reconciliation` rather than only the claims, because
    PLANTS is one of two nativity sources and the reason it declined a taxon is
    part of the input to the two-source rule — "PLANTS has no record of this
    name" and "PLANTS calls it introduced" lead to different outcomes when
    iNaturalist has an answer.

    Args:
        client: Cached PLANTS client.
        pool: The resolved pool to reconcile.

    Returns:
        The outcome per taxon ID, one entry for every taxon in the pool, and a
        count of matches per tier.

    Example:
        >>> reconcile_pool(client, pool)  # doctest: +SKIP
        ... # SKIPPED: needs a populated client and pool. Covered by
        ... # tests/test_usda.py against a recorded transport.
    """
    version = plants_source_version(client.cache_dir)
    outcomes: dict[int, Reconciliation] = {}
    tiers: dict[str, int] = {"tier_1": 0, "tier_2": 0, "tier_3": 0}

    for taxon in pool.taxa:
        outcome = reconcile(client, taxon.inat_taxon_id, taxon.scientific_name, version)
        outcomes[taxon.inat_taxon_id] = outcome
        if outcome.claim is not None and outcome.tier is not None:
            tiers[f"tier_{outcome.tier}"] += 1

    matched = sum(1 for outcome in outcomes.values() if outcome.matched)
    _log.info(
        "reconciled %d taxa against PLANTS: %d matched (%s), %d unmatched",
        len(pool.taxa),
        matched,
        ", ".join(f"{k}={v}" for k, v in sorted(tiers.items())),
        len(outcomes) - matched,
    )
    return outcomes, tiers
