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
from typing import get_args

from sift_pack.candidates import CandidatePool, DropReason

__all__ = ["PoolStats", "summarise"]

_LOW_IMAGE_CEILING = 5
"""Candidates with at most this many photos are the thin end of the pack."""


def _percentile(values: list[int], fraction: float) -> int:
    """Nearest-rank percentile, with no interpolation between neighbours."""
    if not values:
        message = "percentile of an empty sequence is undefined"
        raise ValueError(message)
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round(fraction * (len(ordered) - 1))))
    return ordered[index]


@dataclass(frozen=True, slots=True)
class PoolStats:
    """Everything `sift-pack stats` prints, computed once.

    Attributes:
        kept: Candidates in the pool.
        dropped_by_reason: Count per reason, including reasons with zero.
        license_histogram: Photo count per licence.
        thin_image_taxa: Candidates with 4-5 photos.
        rich_image_taxa: Candidates with 6-8 photos.
        median_obs_count: Median research-grade observation count.
        p10_obs_count: 10th-percentile observation count.
        median_agreement: Median identification-agreement floor.
    """

    kept: int
    dropped_by_reason: dict[str, int]
    license_histogram: dict[str, int]
    thin_image_taxa: int
    rich_image_taxa: int
    median_obs_count: int | None
    p10_obs_count: int | None
    median_agreement: int | None

    def render(self) -> str:
        """Format the statistics as a plain-text report.

        Returns:
            A multi-line report, one section per question.

        Example:
            >>> stats = PoolStats(
            ...     kept=0,
            ...     dropped_by_reason={"hybrid": 2},
            ...     license_histogram={},
            ...     thin_image_taxa=0,
            ...     rich_image_taxa=0,
            ...     median_obs_count=None,
            ...     p10_obs_count=None,
            ...     median_agreement=None,
            ... )
            >>> print(stats.render().splitlines()[0])
            candidates kept: 0
        """
        lines = [f"candidates kept: {self.kept}"]

        total_dropped = sum(self.dropped_by_reason.values())
        lines.append(f"dropped: {total_dropped}")
        for reason, count in sorted(self.dropped_by_reason.items()):
            lines.append(f"  {reason}: {count}")

        lines.append("licenses:")
        if self.license_histogram:
            for code, count in sorted(self.license_histogram.items()):
                lines.append(f"  {code}: {count} photos")
        else:
            lines.append("  (no photos)")

        lines.append("image depth:")
        lines.append(f"  4-5 images: {self.thin_image_taxa} taxa")
        lines.append(f"  6-8 images: {self.rich_image_taxa} taxa")

        lines.append("observation counts:")
        lines.append(f"  median: {_or_na(self.median_obs_count)}")
        lines.append(f"  p10:    {_or_na(self.p10_obs_count)}")

        lines.append(f"median identification agreement: {_or_na(self.median_agreement)}")
        return "\n".join(lines)


def _or_na(value: int | None) -> str:
    """Render an absent statistic as `n/a` rather than as zero.

    Zero would be a measurement; these are the absence of one.
    """
    return "n/a" if value is None else str(value)


def summarise(pool: CandidatePool) -> PoolStats:
    """Compute the statistics for one pool.

    Args:
        pool: The pool to describe.

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
        >>> summarise(pool).median_obs_count is None
        True
    """
    reasons: Counter[str] = Counter(record.reason for record in pool.dropped)
    dropped_by_reason = {reason: reasons.get(reason, 0) for reason in get_args(DropReason)}

    licenses: Counter[str] = Counter(
        photo.license for candidate in pool.candidates for photo in candidate.images
    )

    image_counts = [len(candidate.images) for candidate in pool.candidates]
    thin = sum(1 for count in image_counts if count <= _LOW_IMAGE_CEILING)
    rich = sum(1 for count in image_counts if count > _LOW_IMAGE_CEILING)

    obs_counts = [candidate.obs_count for candidate in pool.candidates]
    agreements = [candidate.identification_agreement for candidate in pool.candidates]

    return PoolStats(
        kept=len(pool.candidates),
        dropped_by_reason=dropped_by_reason,
        license_histogram=dict(licenses),
        thin_image_taxa=thin,
        rich_image_taxa=rich,
        median_obs_count=int(statistics.median(obs_counts)) if obs_counts else None,
        p10_obs_count=_percentile(obs_counts, 0.10) if obs_counts else None,
        median_agreement=int(statistics.median(agreements)) if agreements else None,
    )
