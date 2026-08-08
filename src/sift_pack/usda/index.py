"""Building the nativity index for a whole pool, and recording what it cost.

WHY THIS MODULE EXISTS
----------------------
`reconcile` decides one taxon at a time. Something has to run it across a pool,
keep the successes in a form the domain can look up, keep the failures in a form
the unmatched report can print, and count the tiers so the build can be
judged — and that bookkeeping does not belong inside the matching rules, where
it would obscure them.

INVARIANT PROTECTED
-------------------
The index and the rejection map partition the pool: every taxon appears in
exactly one. A taxon in neither would be one that quietly vanished between the
resolved pool and the manifest, which is the specific failure the unmatched
report exists to make impossible.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from pathlib import Path

from sift_pack.domains import Axis1Result
from sift_pack.resolved import ResolvedPool
from sift_pack.usda.client import PlantsClient
from sift_pack.usda.reconcile import reconcile

__all__ = ["DEFAULT_USDA_CACHE", "build_nativity_index", "plants_source_version"]

_log = logging.getLogger(__name__)

DEFAULT_USDA_CACHE = Path("cache/usda")


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


def build_nativity_index(
    client: PlantsClient,
    pool: ResolvedPool,
) -> tuple[dict[int, Axis1Result], dict[int, tuple[str, str]], dict[str, int]]:
    """Reconcile every taxon in a pool against PLANTS.

    Args:
        client: Cached PLANTS client.
        pool: The resolved pool to reconcile.

    Returns:
        Claims by taxon ID, rejection `(reason, detail)` by taxon ID, and a
        count of matches per tier. The first two partition the pool's taxa.

    Example:
        >>> build_nativity_index(client, pool)  # doctest: +SKIP
        ... # SKIPPED: needs a populated client and pool. Covered by
        ... # tests/test_usda.py against a recorded transport.
    """
    version = plants_source_version(client.cache_dir)
    index: dict[int, Axis1Result] = {}
    rejections: dict[int, tuple[str, str]] = {}
    tiers: dict[str, int] = {"tier_1": 0, "tier_2": 0, "tier_3": 0}

    for taxon in pool.taxa:
        outcome = reconcile(client, taxon.inat_taxon_id, taxon.scientific_name, version)
        if outcome.claim is not None and outcome.tier is not None:
            index[taxon.inat_taxon_id] = outcome.claim
            tiers[f"tier_{outcome.tier}"] += 1
            continue
        rejections[taxon.inat_taxon_id] = (
            outcome.reason or "no_plants_record",
            outcome.detail or "reconciliation produced no claim and no reason",
        )

    _log.info(
        "reconciled %d taxa: %d matched (%s), %d unmatched",
        len(pool.taxa),
        len(index),
        ", ".join(f"{k}={v}" for k, v in sorted(tiers.items())),
        len(rejections),
    )
    return index, rejections, tiers
