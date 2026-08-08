"""Tests for the CLI verbs: what they emit, and what they refuse to emit."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from sift_pack.candidates import CandidatePool, CandidateTaxon, DropRecord
from sift_pack.cli import app, pool_path
from sift_pack.manifest import SourceRef
from tests.fixture_client import FIXTURE_CACHE
from tests.test_candidates import _photo

runner = CliRunner()

FETCHED_AT = datetime(2026, 8, 7, 12, 0, 0, tzinfo=UTC)


def _write_pool(work_dir: Path, state: str = "MI") -> Path:
    """Put a small, valid pool where the CLI expects to find one."""
    pool = CandidatePool(
        domain="plants",
        state=state,
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
        candidates=[
            CandidateTaxon(
                inat_taxon_id=47911,
                scientific_name="Asclepias syriaca",
                common_names=["common milkweed"],
                rank="species",
                genus="Asclepias",
                family="Apocynaceae",
                obs_count=9108,
                months_represented=1,
                distinct_observers=5,
                images=[_photo(n, 47911, photographer_login=f"a{n}") for n in range(1, 6)],
            ),
            CandidateTaxon(
                inat_taxon_id=52391,
                scientific_name="Pinus strobus",
                common_names=["eastern white pine"],
                rank="species",
                genus="Pinus",
                family="Pinaceae",
                obs_count=6426,
                months_represented=1,
                distinct_observers=7,
                images=[_photo(n, 52391, photographer_login=f"b{n}") for n in range(10, 17)],
            ),
        ],
        dropped=[
            DropRecord(inat_taxon_id=1, name="Carex", reason="rank_not_species", detail="genus"),
            DropRecord(
                inat_taxon_id=2,
                name="Rara avis",
                reason="obs_count_below_threshold",
                detail="12 observations, need 50",
            ),
        ],
    )
    destination = pool_path(state, work_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(pool.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return destination


# --- places -------------------------------------------------------------------


def test_places_lists_the_committed_table() -> None:
    result = runner.invoke(app, ["places"])
    assert result.exit_code == 0
    assert "MI\t29\tMichigan" in result.stdout
    assert len(result.stdout.strip().splitlines()) == 50


# --- stats --------------------------------------------------------------------


def test_stats_reports_kept_dropped_licences_and_distributions(tmp_path: Path) -> None:
    _write_pool(tmp_path)
    result = runner.invoke(app, ["stats", "--state", "MI", "--work-dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    out = result.stdout
    assert "candidates kept: 2" in out
    assert "dropped: 2" in out
    assert "rank_not_species: 1" in out
    assert "obs_count_below_threshold: 1" in out
    assert "cc0:" in out
    assert "4-5 images: 1 taxa" in out
    assert "6-8 images: 1 taxa" in out
    assert "distributions:" in out
    assert "months_represented" in out
    assert "distinct_observers" in out
    assert "month buckets:" in out
    assert "cache on disk:" in out


def test_stats_reports_every_drop_reason_even_at_zero(tmp_path: Path) -> None:
    # A category that stopped firing must stay visible, or a regression that
    # silences one filter looks like an improvement.
    _write_pool(tmp_path)
    result = runner.invoke(app, ["stats", "--state", "MI", "--work-dir", str(tmp_path)])
    assert "hybrid: 0" in result.stdout
    assert "insufficient_licensed_photos: 0" in result.stdout


def test_stats_without_a_pool_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["stats", "--state", "MI", "--work-dir", str(tmp_path)])
    assert result.exit_code == 6
    assert "no candidate pool" in result.stderr


# --- build: refuses to fabricate ---------------------------------------------


def test_build_refuses_and_explains_what_is_missing(tmp_path: Path) -> None:
    _write_pool(tmp_path)
    result = runner.invoke(app, ["build", "--state", "MI", "--work-dir", str(tmp_path)])
    assert result.exit_code == 4
    assert "cannot build a manifest yet" in result.stderr
    assert "USDA PLANTS" in result.stderr
    assert "sha256" in result.stderr


def test_build_emits_nothing_on_stdout(tmp_path: Path) -> None:
    # An empty manifest would be a false claim: that promotion ran and rejected
    # everything. Nothing ran.
    _write_pool(tmp_path)
    result = runner.invoke(app, ["build", "--state", "MI", "--work-dir", str(tmp_path)])
    assert result.stdout == ""


def test_build_without_a_pool_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["build", "--state", "MI", "--work-dir", str(tmp_path)])
    assert result.exit_code == 6


# --- fetch: argument handling (no network; offline mode proves it) -----------


def test_fetch_rejects_an_unknown_domain(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["fetch", "--domain", "birbs", "--state", "MI", "--work-dir", str(tmp_path)]
    )
    assert result.exit_code == 2
    assert "unknown domain 'birbs'" in result.stderr


def test_fetch_rejects_an_unknown_state(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["fetch", "--domain", "plants", "--state", "ZZ", "--work-dir", str(tmp_path)]
    )
    assert result.exit_code == 5
    assert "no place ID" in result.stderr


def test_fetch_offline_with_a_cold_cache_fails_loudly(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fetch",
            "--domain",
            "plants",
            "--state",
            "MI",
            "--offline",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--work-dir",
            str(tmp_path / "work"),
        ],
    )
    assert result.exit_code == 5
    assert "no cached response" in result.stderr


def test_fetch_over_recorded_fixtures_writes_a_pool(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fetch",
            "--domain",
            "plants",
            "--state",
            "MI",
            "--limit",
            "3",
            "--offline",
            "--cache-dir",
            str(FIXTURE_CACHE),
            "--work-dir",
            str(tmp_path),
        ],
    )
    assert result.exit_code == 0, result.output
    written = pool_path("MI", tmp_path)
    pool = CandidatePool.model_validate_json(written.read_text(encoding="utf-8"))
    assert pool.candidates
    assert "0 fetched" in result.stderr  # offline: zero network calls


def test_fetch_reports_its_cost(tmp_path: Path) -> None:
    result = runner.invoke(
        app,
        [
            "fetch",
            "--domain",
            "plants",
            "--state",
            "MI",
            "--limit",
            "3",
            "--offline",
            "--cache-dir",
            str(FIXTURE_CACHE),
            "--work-dir",
            str(tmp_path),
        ],
    )
    assert "candidates" in result.stderr
    assert "dropped" in result.stderr
    assert "requests:" in result.stderr


def test_fetched_pool_is_valid_json(tmp_path: Path) -> None:
    runner.invoke(
        app,
        [
            "fetch",
            "--domain",
            "plants",
            "--state",
            "MI",
            "--limit",
            "3",
            "--offline",
            "--cache-dir",
            str(FIXTURE_CACHE),
            "--work-dir",
            str(tmp_path),
        ],
    )
    payload = json.loads(pool_path("MI", tmp_path).read_text(encoding="utf-8"))
    assert payload["domain"] == "plants"
    assert payload["place_id"] == 29
