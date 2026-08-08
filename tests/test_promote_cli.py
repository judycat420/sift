"""Tests for the promote and gc commands, and the manifest report.

These drive the CLI end to end over recorded PLANTS fixtures and a temporary
store, so the whole terminal path — reconcile, promote, write the reports —
runs with no network in the loop.
"""

from __future__ import annotations

import csv
import json
from pathlib import Path

from typer.testing import CliRunner

from sift_pack.cli import app
from sift_pack.imagestore import ImageStore
from sift_pack.manifest import Manifest
from sift_pack.promote import PromotionReport as Report
from sift_pack.promote import report_path, unmatched_path, write_report
from sift_pack.resolve import commit
from sift_pack.resolved import ResolvedPool
from sift_pack.stats import summarise_manifest
from sift_pack.transcode import current_profile
from tests.test_promote import resolved_pool, resolved_taxon

runner = CliRunner()

FIXTURE_USDA = Path(__file__).parent / "fixtures" / "usda_cache"


def pool_with(names: list[tuple[int, str, str]]) -> ResolvedPool:
    """A resolved pool built from `(taxon_id, scientific_name, genus)` triples."""
    return resolved_pool([resolved_taxon(tid, name, genus) for tid, name, genus in names])


def write_pool(pool: ResolvedPool, work_dir: Path) -> Path:
    """Put a resolved pool where the CLI expects one."""
    return commit(pool, work_dir)


def test_promote_builds_a_manifest_from_recorded_plants(tmp_path: Path) -> None:
    write_pool(
        pool_with(
            [
                (1, "Asclepias tuberosa", "Asclepias"),
                (2, "Alliaria petiolata", "Alliaria"),
                (3, "Carex intumescens", "Carex"),
            ]
        ),
        tmp_path,
    )
    result = runner.invoke(
        app,
        [
            "promote-pack",
            "--state",
            "MI",
            "--work-dir",
            str(tmp_path),
            "--packs-dir",
            str(tmp_path / "packs"),
            "--cache-dir",
            str(FIXTURE_USDA),
            "--offline",
        ],
    )
    assert result.exit_code == 0, result.output

    manifest = Manifest.model_validate_json(
        (tmp_path / "packs" / "manifest_MI.json").read_text(encoding="utf-8")
    )
    labels = {t.scientific_name: t.axis1_value for t in manifest.taxa}
    assert labels["Asclepias tuberosa"] == "native"
    assert labels["Alliaria petiolata"] == "introduced"
    assert all(t.axis1_source == "USDA PLANTS" for t in manifest.taxa)


def test_promote_writes_both_reports(tmp_path: Path) -> None:
    write_pool(pool_with([(1, "Asclepias tuberosa", "Asclepias")]), tmp_path)
    runner.invoke(
        app,
        [
            "promote-pack",
            "--state",
            "MI",
            "--work-dir",
            str(tmp_path),
            "--packs-dir",
            str(tmp_path / "packs"),
            "--cache-dir",
            str(FIXTURE_USDA),
            "--offline",
        ],
    )
    assert unmatched_path("MI", tmp_path).exists()
    recorded = json.loads(report_path("MI", tmp_path).read_text(encoding="utf-8"))
    assert recorded["promoted"] == 1
    assert recorded["by_tier"]["tier_1"] == 1


def test_a_taxon_plants_cannot_resolve_reaches_the_csv(tmp_path: Path) -> None:
    write_pool(
        pool_with(
            [(1, "Asclepias tuberosa", "Asclepias"), (2, "Nonexistent plantus", "Nonexistent")]
        ),
        tmp_path,
    )
    runner.invoke(
        app,
        [
            "promote-pack",
            "--state",
            "MI",
            "--work-dir",
            str(tmp_path),
            "--packs-dir",
            str(tmp_path / "packs"),
            "--cache-dir",
            str(FIXTURE_USDA),
            "--offline",
        ],
    )
    with unmatched_path("MI", tmp_path).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["scientific_name"] for r in rows] == ["Nonexistent plantus"]
    assert rows[0]["reason"]
    assert rows[0]["detail"]


def test_promote_refuses_to_write_a_pack_with_no_cards(tmp_path: Path) -> None:
    # An empty manifest would be a pack; it is not one. Exit non-zero and say so.
    write_pool(pool_with([(1, "Nonexistent plantus", "Nonexistent")]), tmp_path)
    result = runner.invoke(
        app,
        [
            "promote-pack",
            "--state",
            "MI",
            "--work-dir",
            str(tmp_path),
            "--packs-dir",
            str(tmp_path / "packs"),
            "--cache-dir",
            str(FIXTURE_USDA),
            "--offline",
        ],
    )
    assert result.exit_code == 10
    assert "no pack to emit" in result.stderr
    assert not (tmp_path / "packs" / "manifest_MI.json").exists()
    assert unmatched_path("MI", tmp_path).exists()


def test_promote_without_a_resolved_pool_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(app, ["promote-pack", "--state", "MI", "--work-dir", str(tmp_path)])
    assert result.exit_code == 6
    assert "no resolved pool" in result.stderr


# --- the manifest report -------------------------------------------------------


def test_stats_manifest_reports_tiers_split_and_demotions(tmp_path: Path) -> None:
    write_pool(
        pool_with(
            [
                (1, "Asclepias tuberosa", "Asclepias"),
                (2, "Alliaria petiolata", "Alliaria"),
                (3, "Carex intumescens", "Carex"),
            ]
        ),
        tmp_path,
    )
    packs = tmp_path / "packs"
    runner.invoke(
        app,
        [
            "promote-pack",
            "--state",
            "MI",
            "--work-dir",
            str(tmp_path),
            "--packs-dir",
            str(packs),
            "--cache-dir",
            str(FIXTURE_USDA),
            "--offline",
        ],
    )
    manifest = Manifest.model_validate_json(
        (packs / "manifest_MI.json").read_text(encoding="utf-8")
    )
    report = summarise_manifest(
        manifest, unmatched_path("MI", tmp_path), report_path("MI", tmp_path)
    ).render()

    assert "taxa in pack: 3" in report
    assert "tier_1: 3" in report
    assert "native: 2" in report
    assert "introduced: 1" in report
    assert "high: 3" in report
    assert "genus-demoted: 1" in report
    assert "Carex intumescens" in report


def test_stats_manifest_without_a_pack_exits_nonzero(tmp_path: Path) -> None:
    result = runner.invoke(
        app, ["stats", "--state", "ZZ", "--manifest", "--work-dir", str(tmp_path)]
    )
    assert result.exit_code == 6


def test_the_promotion_report_survives_a_round_trip(tmp_path: Path) -> None:
    report = Report(promoted=2, by_tier={"tier_1": 2}, by_value={"native": 2}, demoted=["X"])
    written = write_report(report, "MI", tmp_path)
    payload = json.loads(written.read_text(encoding="utf-8"))
    assert payload == {
        "promoted": 2,
        "unmatched": 0,
        "by_tier": {"tier_1": 2},
        "by_value": {"native": 2},
        "demoted": ["X"],
    }


# --- gc ------------------------------------------------------------------------


def test_gc_is_a_dry_run_by_default(tmp_path: Path) -> None:
    store = tmp_path / "images"
    result = runner.invoke(app, ["gc", "--work-dir", str(tmp_path), "--store-dir", str(store)])
    assert result.exit_code == 0
    assert "dry run" in result.stderr


def test_gc_deletes_only_when_asked(tmp_path: Path) -> None:
    store_dir = tmp_path / "images"
    store = ImageStore(store_dir, current_profile())
    orphan = store.put(b"nothing references me")
    assert store.has(orphan)

    result = runner.invoke(
        app,
        ["gc", "--work-dir", str(tmp_path), "--store-dir", str(store_dir), "--no-dry-run"],
    )
    assert result.exit_code == 0
    assert not store.has(orphan)
    assert "removed 1 images" in result.stderr


def test_gc_keeps_what_a_pool_references(tmp_path: Path) -> None:
    pool = pool_with([(1, "Asclepias tuberosa", "Asclepias")])
    commit(pool, tmp_path)
    store_dir = tmp_path / "images"
    store = ImageStore(store_dir, current_profile())
    # Referenced digests are the pool's; put an unreferenced one alongside.
    orphan = store.put(b"orphan")

    runner.invoke(
        app,
        ["gc", "--work-dir", str(tmp_path), "--store-dir", str(store_dir), "--no-dry-run"],
    )
    assert not store.has(orphan)
