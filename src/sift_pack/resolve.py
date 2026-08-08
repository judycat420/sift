"""The resolve stage: candidate photos become stored bytes.

WHY THIS MODULE EXISTS
----------------------
This is the only stage that turns a URL into something a learner can be shown.
It is also the longest-running one — roughly 2400 downloads for a state — which
makes process death the failure mode to design against rather than an
afterthought. M2's fetch got away with a single write at the end because the
disk cache made a re-run free; this stage has no such luxury, because the
expensive work is transcoding as much as downloading.

RESUMABILITY
------------
Progress is journalled per taxon to `work/resolved_<STATE>.partial.jsonl`, one
line appended and flushed as each taxon finishes. A restart reads the journal,
skips every taxon already recorded, and continues. The content-addressed store
handles the rest: a photo whose bytes are already present is not re-downloaded
even when its taxon is being redone, because the ledger records which digest each
photo produced.

The final `resolved_<STATE>.json` is written once, atomically, and the journal is
removed only after it lands. A crash between the two leaves both, and the next
run rebuilds from the journal — never a half-written pool.

INVARIANT PROTECTED
-------------------
Every candidate photo ends as a stored image or a `ResolveDropRecord` naming why
not, and every candidate taxon ends in the pool or in the drop list. A taxon that
loses photos to 404s and falls below four images is dropped and counted; it is
never padded back up, because the photos available to pad with are the ones the
selection stage already declined.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sift_pack.candidates import MIN_PHOTOS_PER_CANDIDATE, CandidatePhoto, CandidatePool
from sift_pack.download import (
    DEFAULT_CONCURRENCY,
    Downloaded,
    Downloader,
    download_all,
    sized_url,
)
from sift_pack.imagestore import ImageStore, sha256_of
from sift_pack.manifest import SourceRef
from sift_pack.resolved import (
    ResolvedPhoto,
    ResolvedPool,
    ResolveDropReason,
    ResolveDropRecord,
    ResolvedTaxon,
)
from sift_pack.transcode import TranscodeError, transcode

__all__ = [
    "OPEN_DATA_URL",
    "ResolveStats",
    "journal_path",
    "resolve_pool",
    "resolved_path",
]

_log = logging.getLogger(__name__)

OPEN_DATA_URL = "https://inaturalist-open-data.s3.amazonaws.com/"


def resolved_path(state: str, work_dir: Path) -> Path:
    """Where a state's resolved pool is written.

    Args:
        state: Region code.
        work_dir: Directory holding build artefacts.

    Returns:
        The path.

    Example:
        >>> resolved_path("MI", Path("work")).as_posix()
        'work/resolved_MI.json'
    """
    return work_dir / f"resolved_{state.upper()}.json"


def journal_path(state: str, work_dir: Path) -> Path:
    """Where a state's in-progress resolve journal lives.

    Args:
        state: Region code.
        work_dir: Directory holding build artefacts.

    Returns:
        The path.

    Example:
        >>> journal_path("MI", Path("work")).as_posix()
        'work/resolved_MI.partial.jsonl'
    """
    return work_dir / f"resolved_{state.upper()}.partial.jsonl"


@dataclass(slots=True)
class ResolveStats:
    """What the resolve stage cost.

    Attributes:
        downloaded: Photos fetched over the network this run.
        deduped: Photos whose transcoded bytes were already stored.
        stored: Photos written to the store this run.
        skipped: Taxa replayed from the journal without any work.
        failures_by_reason: Photo-level failure counts.
        transcode_seconds: Wall-clock spent encoding.
    """

    downloaded: int = 0
    deduped: int = 0
    stored: int = 0
    skipped: int = 0
    failures_by_reason: dict[str, int] = field(default_factory=dict)
    transcode_seconds: float = 0.0

    def record_failure(self, reason: str) -> None:
        """Count one photo-level failure."""
        self.failures_by_reason[reason] = self.failures_by_reason.get(reason, 0) + 1

    def summary(self) -> str:
        """One-line summary for a report.

        Returns:
            Human-readable counts.

        Example:
            >>> ResolveStats(downloaded=4, deduped=1, stored=3).summary()
            '4 downloaded, 3 stored, 1 deduped, 0 failed'
        """
        failed = sum(self.failures_by_reason.values())
        return (
            f"{self.downloaded} downloaded, {self.stored} stored, "
            f"{self.deduped} deduped, {failed} failed"
        )


@dataclass(slots=True)
class _ResolveContext:
    """Everything photo resolution needs beyond the photos themselves.

    Bundled so the resolver has a readable signature; `stats` and `ledger` are
    mutated across taxa, which is why this is not frozen.

    Attributes:
        downloader: Transport seam for image bytes.
        store: Content-addressed store to write into.
        stats: Running cost counters for the whole run.
        ledger: Photo id -> already-stored image, so a redone taxon reuses bytes.
        concurrency: Simultaneous downloads.
    """

    downloader: Downloader
    store: ImageStore
    stats: ResolveStats
    ledger: dict[int, ResolvedPhoto]
    concurrency: int = DEFAULT_CONCURRENCY


@dataclass(frozen=True, slots=True)
class _TaxonOutcome:
    """One taxon's resolve result, as journalled."""

    taxon: ResolvedTaxon | None
    drops: list[ResolveDropRecord]


def _read_journal(path: Path) -> tuple[dict[int, _TaxonOutcome], int]:
    """Replay a partial run, or start empty.

    A malformed trailing line is discarded rather than fatal: it means the
    process died mid-write, which is exactly the case this journal exists for.
    Earlier lines are still good, so the run resumes from them.
    """
    if not path.exists():
        return {}, 0

    outcomes: dict[int, _TaxonOutcome] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            taxon = (
                ResolvedTaxon.model_validate(record["taxon"])
                if record.get("taxon") is not None
                else None
            )
            drops = [ResolveDropRecord.model_validate(d) for d in record.get("drops", [])]
        except (json.JSONDecodeError, KeyError, ValueError):
            _log.warning("discarding a truncated journal line; the run will redo that taxon")
            continue
        outcomes[record["inat_taxon_id"]] = _TaxonOutcome(taxon=taxon, drops=drops)
    return outcomes, len(outcomes)


def _seed_from_previous(pool: CandidatePool, work_dir: Path) -> dict[int, _TaxonOutcome]:
    """Reuse a previous resolved pool for taxa whose candidate photos are unchanged.

    A taxon is only reused when its candidate photo-id set matches exactly. If
    the fetch stage picked different photos, the taxon is redone — though any
    individual photo already stored is still reused, because a photo id maps to
    fixed bytes.

    Previously-dropped taxa are reused too, drops and all. That makes a re-run
    genuinely free rather than retrying every 404 each time; to retry failures,
    delete `resolved_<STATE>.json` and resolve again.
    """
    previous = resolved_path(pool.state, work_dir)
    if not previous.exists():
        return {}
    try:
        prior = ResolvedPool.model_validate_json(previous.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        _log.warning("%s could not be read; resolving from scratch", previous)
        return {}

    drops_by_taxon: dict[int, list[ResolveDropRecord]] = {}
    for drop in prior.resolve_dropped:
        drops_by_taxon.setdefault(drop.inat_taxon_id, []).append(drop)

    wanted = {
        candidate.inat_taxon_id: {photo.inat_photo_id for photo in candidate.images}
        for candidate in pool.candidates
    }
    seeded: dict[int, _TaxonOutcome] = {}
    for taxon in prior.taxa:
        have = {photo.inat_photo_id for photo in taxon.images}
        expected = wanted.get(taxon.inat_taxon_id)
        if expected is None:
            continue
        drops = drops_by_taxon.get(taxon.inat_taxon_id, [])
        failed = {drop.inat_photo_id for drop in drops if drop.inat_photo_id is not None}
        # Every candidate photo must be accounted for: stored, or recorded as
        # having failed. A photo that is neither means the fetch stage selected
        # differently since, so the taxon is resolved again rather than served
        # a pack that silently omits the new photo.
        if have | failed >= expected:
            seeded[taxon.inat_taxon_id] = _TaxonOutcome(taxon=taxon, drops=drops)

    resolved_ids = {taxon.inat_taxon_id for taxon in prior.taxa}
    for taxon_id, drops in drops_by_taxon.items():
        if taxon_id in resolved_ids or taxon_id not in wanted:
            continue
        if any(drop.reason == "insufficient_resolved_images" for drop in drops):
            seeded[taxon_id] = _TaxonOutcome(taxon=None, drops=drops)
    return seeded


def _append_journal(path: Path, taxon_id: int, outcome: _TaxonOutcome) -> None:
    """Append one taxon's result and flush it to disk before continuing."""
    payload = {
        "inat_taxon_id": taxon_id,
        "taxon": outcome.taxon.model_dump(mode="json") if outcome.taxon else None,
        "drops": [drop.model_dump(mode="json") for drop in outcome.drops],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload) + "\n")
        handle.flush()


def _resolve_photos(
    photos: list[CandidatePhoto], context: _ResolveContext
) -> tuple[list[ResolvedPhoto], list[tuple[int, ResolveDropReason, str]]]:
    """Download, transcode and store one taxon's photos.

    Returns the stored photos and one `(photo_id, reason, detail)` per loss.
    Photos already in the ledger — resolved earlier in this run or replayed from
    the journal — are reused without touching the network.
    """
    resolved: list[ResolvedPhoto] = []
    failures: list[tuple[int, ResolveDropReason, str]] = []

    wanted: list[tuple[int, str]] = []
    by_id = {photo.inat_photo_id: photo for photo in photos}
    for photo in photos:
        cached = context.ledger.get(photo.inat_photo_id)
        if cached is not None:
            resolved.append(cached)
            continue
        wanted.append((photo.inat_photo_id, sized_url(photo.source_url)))

    for outcome in download_all(context.downloader, wanted, context.concurrency):
        source = by_id[outcome.inat_photo_id]
        if not isinstance(outcome, Downloaded):
            failures.append((outcome.inat_photo_id, outcome.reason, outcome.detail))
            context.stats.record_failure(outcome.reason)
            continue

        context.stats.downloaded += 1
        started = datetime.now(UTC)
        try:
            encoded = transcode(outcome.payload)
        except TranscodeError as exc:
            failures.append((outcome.inat_photo_id, "photo_undecodable", str(exc)))
            context.stats.record_failure("photo_undecodable")
            continue
        context.stats.transcode_seconds += (datetime.now(UTC) - started).total_seconds()

        digest = sha256_of(encoded.payload)
        if context.store.has(digest):
            # Same photograph already stored — from another taxon, another
            # state, or an earlier run. This is the cross-state dedupe working.
            context.stats.deduped += 1
        else:
            context.store.put(encoded.payload)
            context.stats.stored += 1

        stored = ResolvedPhoto(
            sha256=digest,
            inat_photo_id=source.inat_photo_id,
            taxon_id=source.taxon_id,
            license=source.license,
            photographer_name=source.photographer_name,
            photographer_login=source.photographer_login,
            observation_url=source.observation_url,
            width=encoded.width,
            height=encoded.height,
            bytes=len(encoded.payload),
            month_bucket=source.month_bucket,
        )
        context.ledger[source.inat_photo_id] = stored
        resolved.append(stored)

    # Preserve the candidate's photo order, which encodes the seasonal
    # round-robin; download completion order does not.
    order = {photo.inat_photo_id: index for index, photo in enumerate(photos)}
    resolved.sort(key=lambda photo: order[photo.inat_photo_id])
    return resolved, failures


def resolve_pool(
    pool: CandidatePool,
    downloader: Downloader,
    store: ImageStore,
    work_dir: Path,
    concurrency: int = DEFAULT_CONCURRENCY,
) -> tuple[ResolvedPool, ResolveStats]:
    """Resolve every candidate's photos into stored images.

    Args:
        pool: The candidate pool to resolve.
        downloader: Transport seam for image bytes.
        store: Content-addressed store to write into.
        work_dir: Where the resume journal lives.
        concurrency: Simultaneous downloads.

    Returns:
        The resolved pool and what it cost.

    Raises:
        StoreProfileError: If the store was written by a different encoder.

    Example:
        >>> resolve_pool(pool, downloader, store, Path("work"))  # doctest: +SKIP
        ... # SKIPPED: needs a populated pool and store. Covered by
        ... # tests/test_resolve.py against a recorded downloader.
    """
    started = datetime.now(UTC)
    journal = journal_path(pool.state, work_dir)

    # A completed pool is itself a ledger. Seeding from it makes a re-run free,
    # which matters because the journal is retired on commit and there would
    # otherwise be nothing to stop a second run re-downloading 2400 images.
    outcomes = _seed_from_previous(pool, work_dir)
    if outcomes:
        _log.info("reusing %d taxa from a previous resolved pool", len(outcomes))
    journalled, _ = _read_journal(journal)
    outcomes.update(journalled)
    replayed = len(outcomes)
    if replayed:
        _log.info("resuming: %d taxa already resolved", replayed)

    stats = ResolveStats(skipped=replayed)
    # Photo id -> already-stored image, so a taxon redone after a crash reuses
    # bytes that are already on disk instead of downloading them again.
    ledger: dict[int, ResolvedPhoto] = {}
    for outcome in outcomes.values():
        if outcome.taxon is not None:
            for photo in outcome.taxon.images:
                ledger[photo.inat_photo_id] = photo
    context = _ResolveContext(
        downloader=downloader, store=store, stats=stats, ledger=ledger, concurrency=concurrency
    )

    for candidate in pool.candidates:
        if candidate.inat_taxon_id in outcomes:
            continue

        photos, failures = _resolve_photos(list(candidate.images), context)
        drops = [
            ResolveDropRecord(
                inat_taxon_id=candidate.inat_taxon_id,
                name=candidate.scientific_name,
                reason=reason,
                detail=detail,
                inat_photo_id=photo_id,
            )
            for photo_id, reason, detail in failures
        ]

        if len(photos) < MIN_PHOTOS_PER_CANDIDATE:
            drops.append(
                ResolveDropRecord(
                    inat_taxon_id=candidate.inat_taxon_id,
                    name=candidate.scientific_name,
                    reason="insufficient_resolved_images",
                    detail=(
                        f"{len(photos)} images survived download and transcode, need "
                        f"{MIN_PHOTOS_PER_CANDIDATE}; the selection stage already "
                        "declined the alternatives, so there is nothing honest to pad with"
                    ),
                )
            )
            outcome = _TaxonOutcome(taxon=None, drops=drops)
        else:
            outcome = _TaxonOutcome(
                taxon=ResolvedTaxon(
                    inat_taxon_id=candidate.inat_taxon_id,
                    scientific_name=candidate.scientific_name,
                    common_names=list(candidate.common_names),
                    rank=candidate.rank,
                    genus=candidate.genus,
                    family=candidate.family,
                    obs_count=candidate.obs_count,
                    months_represented=len({photo.month_bucket for photo in photos}),
                    distinct_observers=len({photo.photographer_login for photo in photos}),
                    images=photos,
                ),
                drops=drops,
            )

        outcomes[candidate.inat_taxon_id] = outcome
        _append_journal(journal, candidate.inat_taxon_id, outcome)

    resolved = ResolvedPool(
        domain=pool.domain,
        state=pool.state,
        place_id=pool.place_id,
        fetched_at=pool.fetched_at,
        resolved_at=started,
        sources=[*pool.sources, _open_data_source(started, store), _encoder_source(started, store)],
        taxa=[o.taxon for o in outcomes.values() if o.taxon is not None],
        dropped=list(pool.dropped),
        resolve_dropped=[drop for o in outcomes.values() for drop in o.drops],
    )
    _log.info("resolve complete: %d taxa, %s", len(resolved.taxa), stats.summary())
    return resolved, stats


def _open_data_source(at: datetime, store: ImageStore) -> SourceRef:
    """Provenance for the bucket the bytes came from."""
    del store
    return SourceRef(
        name="iNaturalist Open Dataset",
        version="s3://inaturalist-open-data",
        retrieved_at=at,
        url=OPEN_DATA_URL,
    )


def _encoder_source(at: datetime, store: ImageStore) -> SourceRef:
    """Provenance for the encoder.

    Recorded as a source because the hashes in this pool are over transcoded
    bytes: the encoder is as much a producer of the artefact as the bucket is,
    and a pool that does not say which encoder made it cannot be checked.
    """
    return SourceRef(
        name="Sift transcoder",
        version=store.profile.describe(),
        retrieved_at=at,
        url="https://python-pillow.org/",
    )


def commit(resolved: ResolvedPool, work_dir: Path) -> Path:
    """Write the finished pool atomically and retire its journal.

    Args:
        resolved: The pool to write.
        work_dir: Where build artefacts live.

    Returns:
        The path written.

    Example:
        >>> commit(pool, Path("work"))  # doctest: +SKIP
        ... # SKIPPED: needs a resolved pool. Covered by tests/test_resolve.py.
    """
    destination = resolved_path(resolved.state, work_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    tmp = destination.with_suffix(".json.tmp")
    tmp.write_text(resolved.model_dump_json(indent=2) + "\n", encoding="utf-8")
    tmp.replace(destination)
    journal_path(resolved.state, work_dir).unlink(missing_ok=True)
    return destination


def referenced_digests(work_dir: Path) -> Iterator[str]:
    """Every image digest referenced by any resolved pool in `work_dir`.

    Args:
        work_dir: Directory holding build artefacts.

    Yields:
        Digests that must survive garbage collection.

    Example:
        >>> list(referenced_digests(Path("nope")))
        []
    """
    for path in sorted(work_dir.glob("resolved_*.json")):
        pool = ResolvedPool.model_validate_json(path.read_text(encoding="utf-8"))
        yield from pool.digests()
