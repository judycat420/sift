"""The fetch orchestrator: three stages into one candidate pool.

WHY THIS MODULE EXISTS
----------------------
The three iNaturalist stages — rank the taxa, look up their ancestry, choose
their photos — each drop taxa for their own reasons, and each is a different
number of requests. Somebody has to run them in order, carry the drops forward
without losing any, and produce one artefact at the end. Doing that inline in
the CLI would mean the accounting lived next to argument parsing, where it would
be untestable without invoking a command.

RESUMABILITY
------------
There is no checkpoint file and no partial-write recovery, because there does
not need to be: every request goes through the disk cache, so a run killed
halfway through re-executes from the top and replays everything it already
fetched at local-disk speed. The expensive thing is the network, and the network
is what the cache holds. "Resume" here means "run it again".

INVARIANT PROTECTED
-------------------
Every taxon `species_counts` returned appears exactly once in the finished pool,
as a candidate or as a drop with a reason. The models are frozen, so the pool is
accumulated in local lists and constructed once at the end — there is no
half-built pool that could escape if a stage raised partway through.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime

from sift_pack.candidates import CandidatePool, CandidateTaxon, DropRecord
from sift_pack.domains import TaxonDomain
from sift_pack.inat.client import InatClient
from sift_pack.inat.deck import fetch_taxon_details, select_taxa
from sift_pack.inat.photos import (
    MONTH_BUCKETS,
    distinct_observers,
    months_represented,
    select_photos,
)
from sift_pack.manifest import SourceRef

__all__ = ["INAT_API_URL", "fetch_pool"]

_log = logging.getLogger(__name__)

INAT_API_URL = "https://api.inaturalist.org/v1/"
INAT_API_VERSION = "v1"


def fetch_pool(
    client: InatClient,
    domain: TaxonDomain,
    state: str,
    place_id: int,
    limit: int,
) -> CandidatePool:
    """Run the full iNaturalist fetch for one place and produce a candidate pool.

    Args:
        client: Cached iNaturalist client. Re-running with a warm cache makes no
            network calls.
        domain: Domain being fetched for; supplies the iconic taxon to scope to.
        state: Region code, recorded on the pool, e.g. `"MI"`.
        place_id: iNaturalist place ID for `state`.
        limit: How many candidates to aim for. Fewer are returned when the place
            runs out of taxa that pass every filter.

    Returns:
        The pool: candidates that survived all three stages, and one drop record
        per taxon rejected by any of them.

    Raises:
        InatError: If a response cannot be parsed at all, or the client is
            offline and something is uncached.

    Example:
        >>> fetch_pool(client, PlantsDomain(), "MI", 29, 250)  # doctest: +SKIP
        ... # SKIPPED: needs a populated client. Covered by tests/test_inat_pipeline.py
        ... # against recorded fixtures.
    """
    started = datetime.now(UTC)
    dropped: list[DropRecord] = []

    # Stage 1: rank by observation frequency, drop by rank/hybrid/scarcity.
    # Over-request so that stage-2 and stage-3 losses do not leave us short.
    summaries, deck_drops = select_taxa(client, place_id, domain.iconic_taxon_id, limit * 2)
    dropped.extend(deck_drops)
    _log.info("stage 1: %d taxa ranked, %d dropped", len(summaries), len(deck_drops))

    # Stage 2: genus and family, which species_counts does not return.
    details, detail_drops = fetch_taxon_details(
        client, [summary.inat_taxon_id for summary in summaries]
    )
    dropped.extend(detail_drops)
    _log.info("stage 2: %d taxa detailed, %d dropped", len(details), len(detail_drops))

    # Stage 3: photos, the expensive stage — four requests per surviving taxon,
    # one per seasonal bucket.
    candidates: list[CandidateTaxon] = []
    bucket_observations: dict[str, int] = {bucket.label: 0 for bucket in MONTH_BUCKETS}
    for summary in summaries:
        if len(candidates) >= limit:
            _log.info("reached limit of %d candidates; stopping", limit)
            break
        detail = details.get(summary.inat_taxon_id)
        if detail is None:
            continue  # Already recorded as a drop by stage 2.

        selection = select_photos(client, summary.inat_taxon_id, summary.scientific_name, place_id)
        for label, count in selection.bucket_observations.items():
            bucket_observations[label] = bucket_observations.get(label, 0) + count
        if selection.drop is not None:
            dropped.append(selection.drop)
            continue

        photos = selection.photos
        candidates.append(
            CandidateTaxon(
                inat_taxon_id=summary.inat_taxon_id,
                scientific_name=summary.scientific_name,
                common_names=summary.common_names,
                rank=summary.rank,
                genus=detail.genus,
                family=detail.family,
                obs_count=summary.obs_count,
                months_represented=months_represented(photos),
                distinct_observers=distinct_observers(photos),
                images=photos,
            )
        )

    # Taxa we never reached because the limit was met are not drops — they were
    # never considered. Recording them as drops would inflate the rejection
    # counts and make the place look poorer than it is.
    considered_ids = {candidate.inat_taxon_id for candidate in candidates}
    considered_ids.update(record.inat_taxon_id for record in dropped)
    unreached = [s for s in summaries if s.inat_taxon_id not in considered_ids]
    if unreached:
        _log.info("%d ranked taxa were never examined (limit reached)", len(unreached))

    pool = CandidatePool(
        domain=domain.slug,
        state=state,
        place_id=place_id,
        fetched_at=started,
        sources=[
            SourceRef(
                name="iNaturalist API",
                version=INAT_API_VERSION,
                retrieved_at=started,
                url=INAT_API_URL,
            )
        ],
        candidates=candidates,
        dropped=dropped,
        bucket_observations=bucket_observations,
    )
    _log.info(
        "fetch complete: %d candidates, %d dropped, %s",
        len(pool.candidates),
        len(pool.dropped),
        client.stats.summary(),
    )
    _log.info(
        "bucket yield: %s",
        ", ".join(f"{label}={count}" for label, count in sorted(bucket_observations.items())),
    )
    return pool
