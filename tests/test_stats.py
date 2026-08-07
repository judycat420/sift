"""Tests for the pool statistics."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import get_args

from sift_pack.candidates import CandidatePool, CandidateTaxon, DropReason, DropRecord
from sift_pack.manifest import SourceRef
from sift_pack.stats import summarise
from tests.test_candidates import _photo

FETCHED_AT = datetime(2026, 8, 7, tzinfo=UTC)


def _taxon(taxon_id: int, obs_count: int, agreement: int, photos: int) -> CandidateTaxon:
    return CandidateTaxon(
        inat_taxon_id=taxon_id,
        scientific_name=f"Genus species{taxon_id}",
        common_names=[],
        rank="species",
        genus="Genus",
        family="Familia",
        obs_count=obs_count,
        identification_agreement=agreement,
        images=[_photo(taxon_id * 100 + n, taxon_id) for n in range(photos)],
    )


def _pool(candidates: list[CandidateTaxon], dropped: list[DropRecord]) -> CandidatePool:
    return CandidatePool(
        domain="plants",
        state="MI",
        place_id=29,
        fetched_at=FETCHED_AT,
        sources=[
            SourceRef(
                name="iNaturalist API",
                version="v1",
                retrieved_at=FETCHED_AT,
                url="https://api.inaturalist.org/v1/",
            )
        ],
        candidates=candidates,
        dropped=dropped,
    )


def test_empty_pool_reports_absent_distributions_not_zero() -> None:
    # 0 would be a measurement; there is nothing to measure.
    stats = summarise(_pool([], []))
    assert stats.kept == 0
    assert stats.median_obs_count is None
    assert stats.p10_obs_count is None
    assert stats.median_agreement is None
    assert "n/a" in stats.render()


def test_image_depth_buckets_split_at_five() -> None:
    stats = summarise(
        _pool(
            [
                _taxon(1, 100, 2, 4),
                _taxon(2, 100, 2, 5),
                _taxon(3, 100, 2, 6),
                _taxon(4, 100, 2, 8),
            ],
            [],
        )
    )
    assert stats.thin_image_taxa == 2
    assert stats.rich_image_taxa == 2


def test_license_histogram_counts_photos_not_taxa() -> None:
    stats = summarise(_pool([_taxon(1, 100, 2, 4), _taxon(2, 100, 2, 6)], []))
    assert sum(stats.license_histogram.values()) == 10


def test_distributions_are_computed_over_candidates() -> None:
    stats = summarise(
        _pool(
            [
                _taxon(1, 50, 1, 4),
                _taxon(2, 100, 2, 4),
                _taxon(3, 150, 3, 4),
                _taxon(4, 5000, 9, 4),
            ],
            [],
        )
    )
    assert stats.median_obs_count == 125
    assert stats.p10_obs_count == 50
    assert stats.median_agreement == 2


def test_every_drop_reason_appears_even_when_unused() -> None:
    stats = summarise(
        _pool([], [DropRecord(inat_taxon_id=1, name="x", reason="hybrid", detail="d")])
    )
    assert set(stats.dropped_by_reason) == set(get_args(DropReason))
    assert stats.dropped_by_reason["hybrid"] == 1
    assert stats.dropped_by_reason["rank_not_species"] == 0


def test_render_includes_every_section() -> None:
    report = summarise(_pool([_taxon(1, 100, 2, 4)], [])).render()
    for heading in ("candidates kept", "dropped", "licenses", "image depth", "observation counts"):
        assert heading in report
