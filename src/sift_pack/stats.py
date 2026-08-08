"""Summary statistics for a candidate pool.

WHY THIS MODULE EXISTS
----------------------
A pool is a few thousand lines of JSON, and the questions worth asking about it
are not answerable by reading it: is the pack photo-starved, is one licence
carrying everything, are the taxa well enough identified to build cards from.
STANDARDS.md rule 5 says every stage reports what it dropped and why; this is
the report.

It is deliberately blunt about distribution rather than averages. A median
observation count says what a typical card looks like; a p10 says what the
worst tenth looks like, which is where a pack goes wrong.

INVARIANT PROTECTED
-------------------
The counts here are computed from the pool, never carried alongside it, so they
cannot drift out of date with the artefact they describe. Every drop reason in
the closed vocabulary is reported even when its count is zero, so a category
that stopped firing is visible rather than silently absent.
"""

from __future__ import annotations

import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import get_args

from sift_pack.candidates import CandidatePool, DropReason
from sift_pack.inat.photos import MONTH_BUCKETS
from sift_pack.manifest import SourceRef
from sift_pack.resolved import ResolvedPool, ResolveDropReason

__all__ = [
    "Distribution",
    "PoolStats",
    "ResolvedStats",
    "empty_stats",
    "human_bytes",
    "summarise",
    "summarise_resolved",
]

_LOW_IMAGE_CEILING = 5
"""Candidates with at most this many photos are the thin end of the pack."""

_BYTES_PER_UNIT = 1024


def _percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile, with no interpolation between neighbours."""
    if not values:
        message = "percentile of an empty sequence is undefined"
        raise ValueError(message)
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[index]


@dataclass(frozen=True, slots=True)
class Distribution:
    """Median and 10th percentile of one measure across the candidates.

    The p10 matters more than the median for pack quality: the median says what
    a typical card looks like, the p10 says what the worst tenth looks like, and
    a pack goes wrong at its thin end.

    Attributes:
        median: Middle value, or `None` when there are no candidates.
        p10: 10th-percentile value, or `None` when there are no candidates.
    """

    median: int | None
    p10: int | None

    def render(self, label: str) -> str:
        """Format as one report line.

        Args:
            label: What is being measured.

        Returns:
            An aligned `label: median X, p10 Y` line.

        Example:
            >>> Distribution(median=3, p10=2).render("months")
            '  months: median 3, p10 2'
        """
        return f"  {label}: median {_or_na(self.median)}, p10 {_or_na(self.p10)}"


def _distribution(values: list[int]) -> Distribution:
    """Summarise one measure, or report absence when there is nothing to measure."""
    if not values:
        return Distribution(median=None, p10=None)
    return Distribution(median=int(statistics.median(values)), p10=_percentile(values, 0.10))


def directory_size(path: Path) -> int:
    """Total bytes of every file under a directory.

    Args:
        path: Directory to measure. A missing directory measures zero.

    Returns:
        Size in bytes.

    Example:
        >>> directory_size(Path("does-not-exist"))
        0
    """
    if not path.is_dir():
        return 0
    return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())


def human_bytes(size: int) -> str:
    """Render a byte count at human scale.

    Args:
        size: Bytes.

    Returns:
        A short string such as `'12.3 MB'`.

    Example:
        >>> human_bytes(1536)
        '1.5 KB'
    """
    scaled = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if scaled < _BYTES_PER_UNIT or unit == "GB":
            return f"{scaled:.1f} {unit}" if unit != "B" else f"{int(scaled)} B"
        scaled /= _BYTES_PER_UNIT
    return f"{scaled:.1f} GB"


@dataclass(frozen=True, slots=True)
class PoolStats:
    """Everything `sift-pack stats` prints, computed once.

    Attributes:
        kept: Candidates in the pool.
        dropped_by_reason: Count per reason, including reasons with zero.
        license_histogram: Photo count per licence.
        thin_image_taxa: Candidates with 4-5 photos.
        rich_image_taxa: Candidates with 6-8 photos.
        obs_count: Research-grade observation count distribution.
        months: Seasonal-spread distribution.
        observers: Distinct-observer distribution.
        bucket_observations: Observations each seasonal bucket returned.
        bucket_photos: Selected photos drawn from each seasonal bucket.
        cache_bytes: Size of the response cache on disk, or `None` if not asked.
    """

    kept: int
    dropped_by_reason: dict[str, int]
    license_histogram: dict[str, int]
    thin_image_taxa: int
    rich_image_taxa: int
    obs_count: Distribution
    months: Distribution
    observers: Distribution
    bucket_observations: dict[str, int]
    bucket_photos: dict[str, int]
    cache_bytes: int | None

    def render(self) -> str:
        """Format the statistics as a plain-text report.

        Returns:
            A multi-line report, one section per question.

        Example:
            >>> print(empty_stats().render().splitlines()[0])
            candidates kept: 0
        """
        lines = [f"candidates kept: {self.kept}"]

        total_dropped = sum(self.dropped_by_reason.values())
        lines.append(f"dropped: {total_dropped}")
        lines.extend(
            f"  {reason}: {count}" for reason, count in sorted(self.dropped_by_reason.items())
        )

        lines.append("licenses:")
        if self.license_histogram:
            lines.extend(
                f"  {code}: {count} photos"
                for code, count in sorted(self.license_histogram.items())
            )
        else:
            lines.append("  (no photos)")

        lines.append("image depth:")
        lines.append(f"  4-5 images: {self.thin_image_taxa} taxa")
        lines.append(f"  6-8 images: {self.rich_image_taxa} taxa")

        lines.append("distributions:")
        lines.append(self.obs_count.render("obs_count               "))
        lines.append(self.months.render("months_represented      "))
        lines.append(self.observers.render("distinct_observers      "))

        lines.append("month buckets:")
        for bucket in MONTH_BUCKETS:
            returned = self.bucket_observations.get(bucket.label, 0)
            selected = self.bucket_photos.get(bucket.label, 0)
            months = ",".join(str(month) for month in bucket.months)
            lines.append(
                f"  {bucket.label} (months {months}): {returned} observations, "
                f"{selected} photos selected — {bucket.description}"
            )

        lines.append(
            "cache on disk: "
            + ("not measured" if self.cache_bytes is None else human_bytes(self.cache_bytes))
        )
        return "\n".join(lines)


def _or_na(value: int | None) -> str:
    """Render an absent statistic as `n/a` rather than as zero.

    Zero would be a measurement; these are the absence of one.
    """
    return "n/a" if value is None else str(value)


def empty_stats() -> PoolStats:
    """A zeroed report, used in doctests and as the shape of "nothing measured".

    Returns:
        Stats describing a pool with no candidates and no drops.

    Example:
        >>> empty_stats().kept
        0
    """
    absent = Distribution(median=None, p10=None)
    return PoolStats(
        kept=0,
        dropped_by_reason={},
        license_histogram={},
        thin_image_taxa=0,
        rich_image_taxa=0,
        obs_count=absent,
        months=absent,
        observers=absent,
        bucket_observations={},
        bucket_photos={},
        cache_bytes=None,
    )


def summarise(pool: CandidatePool, cache_dir: Path | None = None) -> PoolStats:
    """Compute the statistics for one pool.

    Args:
        pool: The pool to describe.
        cache_dir: Response cache to measure, or `None` to skip measuring. A
            skipped measurement reports as "not measured", never as zero.

    Returns:
        The computed statistics. Distribution figures are `None` for an empty
        pool rather than 0, because no candidates is not the same measurement as
        candidates with no observations.

    Example:
        >>> from datetime import UTC, datetime
        >>> from sift_pack.manifest import SourceRef
        >>> pool = CandidatePool(
        ...     domain="plants",
        ...     state="MI",
        ...     place_id=29,
        ...     fetched_at=datetime(2026, 8, 7, tzinfo=UTC),
        ...     sources=[
        ...         SourceRef(
        ...             name="iNaturalist API",
        ...             version="v1",
        ...             retrieved_at=datetime(2026, 8, 7, tzinfo=UTC),
        ...             url="https://api.inaturalist.org/v1/",
        ...         )
        ...     ],
        ...     candidates=[],
        ...     dropped=[],
        ... )
        >>> summarise(pool).obs_count.median is None
        True
    """
    reasons: Counter[str] = Counter(record.reason for record in pool.dropped)
    dropped_by_reason = {reason: reasons.get(reason, 0) for reason in get_args(DropReason)}

    licenses: Counter[str] = Counter(
        photo.license for candidate in pool.candidates for photo in candidate.images
    )
    bucket_photos: Counter[str] = Counter(
        photo.month_bucket for candidate in pool.candidates for photo in candidate.images
    )

    image_counts = [len(candidate.images) for candidate in pool.candidates]
    thin = sum(1 for count in image_counts if count <= _LOW_IMAGE_CEILING)
    rich = sum(1 for count in image_counts if count > _LOW_IMAGE_CEILING)

    return PoolStats(
        kept=len(pool.candidates),
        dropped_by_reason=dropped_by_reason,
        license_histogram=dict(licenses),
        thin_image_taxa=thin,
        rich_image_taxa=rich,
        obs_count=_distribution([c.obs_count for c in pool.candidates]),
        months=_distribution([c.months_represented for c in pool.candidates]),
        observers=_distribution([c.distinct_observers for c in pool.candidates]),
        bucket_observations=dict(pool.bucket_observations),
        bucket_photos=dict(bucket_photos),
        cache_bytes=None if cache_dir is None else directory_size(cache_dir),
    )


@dataclass(frozen=True, slots=True)
class ResolvedStats:
    """Everything `sift-pack stats --resolved` prints.

    Attributes:
        taxa: Taxa whose images are stored.
        images: Stored image records across those taxa.
        unique_digests: Distinct images, after content-address dedupe.
        losses_by_reason: Photo and taxon losses at the resolve stage.
        taxa_dropped: Taxa that fell below the image floor while resolving.
        store_bytes: Size of the image store on disk.
        image_bytes: Per-image size distribution.
        months: Seasonal-spread distribution, recomputed from stored photos.
        observers: Distinct-observer distribution.
    """

    taxa: int
    images: int
    unique_digests: int
    losses_by_reason: dict[str, int]
    taxa_dropped: int
    store_bytes: int
    image_bytes: Distribution
    months: Distribution
    observers: Distribution

    def render(self) -> str:
        """Format the statistics as a plain-text report.

        Returns:
            A multi-line report.

        Example:
            >>> print(_empty_resolved().render().splitlines()[0])
            taxa resolved: 0
        """
        lines = [f"taxa resolved: {self.taxa}"]
        lines.append(f"taxa dropped at resolve: {self.taxa_dropped}")
        lines.append(f"images stored: {self.images} records, {self.unique_digests} distinct")
        deduped = self.images - self.unique_digests
        lines.append(f"  deduped by content address: {deduped}")

        lines.append("losses:")
        if self.losses_by_reason:
            lines.extend(
                f"  {reason}: {count}" for reason, count in sorted(self.losses_by_reason.items())
            )
        else:
            lines.append("  (none)")

        lines.append("distributions:")
        lines.append(self.image_bytes.render("image bytes        "))
        lines.append(self.months.render("months_represented "))
        lines.append(self.observers.render("distinct_observers "))

        lines.append(f"store on disk: {human_bytes(self.store_bytes)}")
        return "\n".join(lines)


def _empty_resolved() -> ResolvedStats:
    """A zeroed resolved report, used in doctests.

    Returns:
        Stats describing a pool with nothing in it.

    Example:
        >>> _empty_resolved().taxa
        0
    """
    absent = Distribution(median=None, p10=None)
    return ResolvedStats(
        taxa=0,
        images=0,
        unique_digests=0,
        losses_by_reason={},
        taxa_dropped=0,
        store_bytes=0,
        image_bytes=absent,
        months=absent,
        observers=absent,
    )


def summarise_resolved(pool: ResolvedPool, store_dir: Path | None = None) -> ResolvedStats:
    """Compute the statistics for one resolved pool.

    Args:
        pool: The resolved pool to describe.
        store_dir: Image store to measure, or `None` to report zero.

    Returns:
        The computed statistics.

    Example:
        >>> summarise_resolved(_empty_resolved_pool()).taxa
        0
    """
    losses: Counter[str] = Counter(record.reason for record in pool.resolve_dropped)
    losses_by_reason = {reason: losses.get(reason, 0) for reason in get_args(ResolveDropReason)}

    sizes = [photo.bytes for taxon in pool.taxa for photo in taxon.images]
    digests = {photo.sha256 for taxon in pool.taxa for photo in taxon.images}

    return ResolvedStats(
        taxa=len(pool.taxa),
        images=len(sizes),
        unique_digests=len(digests),
        losses_by_reason=losses_by_reason,
        taxa_dropped=losses_by_reason.get("insufficient_resolved_images", 0),
        store_bytes=0 if store_dir is None else directory_size(store_dir),
        image_bytes=_distribution(sizes),
        months=_distribution([t.months_represented for t in pool.taxa]),
        observers=_distribution([t.distinct_observers for t in pool.taxa]),
    )


def _empty_resolved_pool() -> ResolvedPool:
    """An empty resolved pool, for doctests.

    Returns:
        A valid pool with no taxa.

    Example:
        >>> _empty_resolved_pool().state
        'MI'
    """
    when = datetime(2026, 8, 7, tzinfo=UTC)
    return ResolvedPool(
        domain="plants",
        state="MI",
        place_id=29,
        fetched_at=when,
        resolved_at=when,
        sources=[SourceRef(name="x", version="1", retrieved_at=when, url="https://x.invalid/")],
        taxa=[],
        dropped=[],
        resolve_dropped=[],
    )
