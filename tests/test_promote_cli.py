"""Tests for the promote and gc commands, and the manifest report.

These drive the CLI end to end over recorded PLANTS fixtures and a temporary
store, so the whole terminal path — reconcile, promote, write the reports —
runs with no network in the loop.
"""

from __future__ import annotations

import csv
import json
import shutil
from dataclasses import dataclass
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
from tests.fixture_client import (
    FIXTURE_CACHE,
    RECORDED_NATIVITY_TAXA,
    RECORDED_SILENT_TAXON_ID,
)
from tests.test_promote import resolved_pool, resolved_taxon

runner = CliRunner()

FIXTURE_USDA = Path(__file__).parent / "fixtures" / "usda_cache"

# Every taxon whose per-place establishment status was recorded. The whole set
# is used because `fetch_establishment` asks about every taxon in the pool in
# one batch, and a cache key covers the whole parameter set — so a pool holding
# a subset is a different request and a miss. Promoting the recorded set is
# therefore the only end-to-end shape available, and it happens to be the
# interesting one: it contains the three contested taxa and both exclusions.
RECORDED_POOL = RECORDED_NATIVITY_TAXA

CONTESTED = ("Robinia pseudoacacia", "Geranium robertianum", "Clinopodium vulgare")
EXCLUDED = ("Echinacea purpurea", "Phragmites australis")


@dataclass(frozen=True)
class CliOutcome:
    """What a CLI run produced, in a shape mypy can see.

    `CliRunner.invoke` returns `click.testing.Result`, and click ships no type
    information — naming it in a signature trips `disallow_any_unimported`.
    The two fields the tests read are copied out instead.
    """

    exit_code: int
    output: str


def cache_root(tmp_path: Path) -> Path:
    """Assemble one response-cache root holding both fixture caches.

    `promote-pack` reads the iNaturalist cache at the root and the PLANTS cache
    in its `usda` subdirectory, which is how they sit on disk in a real build.
    """
    root = tmp_path / "cache"
    shutil.copytree(FIXTURE_CACHE, root)
    shutil.copytree(FIXTURE_USDA, root / "usda")
    return root


def full_pool() -> ResolvedPool:
    """A resolved pool over exactly the taxa both fixture halves cover."""
    return resolved_pool(
        [
            resolved_taxon(taxon_id, name, name.split()[0])
            for name, taxon_id in sorted(RECORDED_POOL.items())
        ]
    )


def pool_with(names: list[tuple[int, str, str]]) -> ResolvedPool:
    """A resolved pool built from `(taxon_id, scientific_name, genus)` triples."""
    return resolved_pool([resolved_taxon(tid, name, genus) for tid, name, genus in names])


def write_pool(pool: ResolvedPool, work_dir: Path) -> Path:
    """Put a resolved pool where the CLI expects one."""
    return commit(pool, work_dir)


def promote_cli(tmp_path: Path, *, packs: Path | None = None) -> CliOutcome:
    """Run `promote-pack` over the fixture caches, offline."""
    result = runner.invoke(
        app,
        [
            "promote-pack",
            "--state",
            "MI",
            "--work-dir",
            str(tmp_path),
            "--packs-dir",
            str(packs or tmp_path / "packs"),
            "--cache-dir",
            str(cache_root(tmp_path)),
            "--offline",
        ],
    )
    return CliOutcome(exit_code=result.exit_code, output=result.output)


def test_promote_builds_a_manifest_from_both_sources(tmp_path: Path) -> None:
    write_pool(full_pool(), tmp_path)
    result = promote_cli(tmp_path)
    assert result.exit_code == 0, result.output

    manifest = Manifest.model_validate_json(
        (tmp_path / "packs" / "manifest_MI.json").read_text(encoding="utf-8")
    )
    labels = {t.scientific_name: t.axis1_value for t in manifest.taxa}
    assert labels["Asclepias tuberosa"] == "native"
    assert labels["Alliaria petiolata"] == "introduced"


def test_an_agreed_claim_names_both_sources_and_is_high(tmp_path: Path) -> None:
    write_pool(full_pool(), tmp_path)
    promote_cli(tmp_path)
    manifest = Manifest.model_validate_json(
        (tmp_path / "packs" / "manifest_MI.json").read_text(encoding="utf-8")
    )
    taxon = next(t for t in manifest.taxa if t.scientific_name == "Asclepias tuberosa")
    assert [s.name for s in taxon.axis1_sources] == [
        "USDA PLANTS",
        "iNaturalist place checklist",
    ]
    assert taxon.axis1_confidence == "high"
    assert all(s.version for s in taxon.axis1_sources)


def test_no_promoted_claim_rests_on_a_non_state_scoped_place(tmp_path: Path) -> None:
    # The gate: every checklist source in the pack names the Michigan checklist
    # specifically. A value inherited from North America would have been refused
    # upstream and could not have reached a card at all.
    write_pool(full_pool(), tmp_path)
    promote_cli(tmp_path)
    manifest = Manifest.model_validate_json(
        (tmp_path / "packs" / "manifest_MI.json").read_text(encoding="utf-8")
    )
    checklist_versions = {
        source.version
        for taxon in manifest.taxa
        for source in taxon.axis1_sources
        if source.name == "iNaturalist place checklist"
    }
    assert checklist_versions
    assert all(version.startswith("Michigan checklist") for version in checklist_versions)


def test_the_contested_taxa_are_dropped_as_source_conflicts(tmp_path: Path) -> None:
    # Robinia pseudoacacia, Geranium robertianum and Clinopodium vulgare are the
    # taxa the probe found the two sources disagreeing on. None may become a
    # card, and each must say why.
    write_pool(full_pool(), tmp_path)
    promote_cli(tmp_path)
    manifest = Manifest.model_validate_json(
        (tmp_path / "packs" / "manifest_MI.json").read_text(encoding="utf-8")
    )
    assert not {t.scientific_name for t in manifest.taxa} & set(CONTESTED)

    with unmatched_path("MI", tmp_path).open(encoding="utf-8", newline="") as handle:
        rows = {r["scientific_name"]: r for r in csv.DictReader(handle)}
    for name in CONTESTED:
        assert rows[name]["reason"] == "source_conflict", name
        assert "USDA PLANTS" in rows[name]["detail"]
        assert "Michigan" in rows[name]["detail"]


def test_the_curated_exclusions_are_dropped_as_such(tmp_path: Path) -> None:
    write_pool(full_pool(), tmp_path)
    promote_cli(tmp_path)
    manifest = Manifest.model_validate_json(
        (tmp_path / "packs" / "manifest_MI.json").read_text(encoding="utf-8")
    )
    assert not {t.scientific_name for t in manifest.taxa} & set(EXCLUDED)

    with unmatched_path("MI", tmp_path).open(encoding="utf-8", newline="") as handle:
        rows = {r["scientific_name"]: r for r in csv.DictReader(handle)}
    for name in EXCLUDED:
        assert rows[name]["reason"] == "curated_exclusion", name
        assert rows[name]["detail"].strip()


def test_every_dropped_taxon_reaches_the_csv(tmp_path: Path) -> None:
    # The partition: promoted plus dropped is the whole pool, with no taxon
    # silently vanishing in between.
    write_pool(full_pool(), tmp_path)
    promote_cli(tmp_path)
    manifest = Manifest.model_validate_json(
        (tmp_path / "packs" / "manifest_MI.json").read_text(encoding="utf-8")
    )
    with unmatched_path("MI", tmp_path).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    assert len(manifest.taxa) + len(rows) == len(RECORDED_POOL)
    assert {t.scientific_name for t in manifest.taxa} | {r["scientific_name"] for r in rows} == set(
        RECORDED_POOL
    )
    for row in rows:
        assert row["reason"]
        assert row["detail"]


def test_promote_writes_both_reports(tmp_path: Path) -> None:
    write_pool(full_pool(), tmp_path)
    promote_cli(tmp_path)
    assert unmatched_path("MI", tmp_path).exists()
    recorded = json.loads(report_path("MI", tmp_path).read_text(encoding="utf-8"))
    assert recorded["promoted"] > 0
    assert recorded["by_tier"]["tier_1"] > 0
    assert recorded["agreement"] > 0
    assert {c["scientific_name"] for c in recorded["conflicts"]} == set(CONTESTED)
    assert recorded["excluded"] == sorted(EXCLUDED)


def test_a_taxon_plants_cannot_resolve_reaches_the_csv(tmp_path: Path) -> None:
    pool = full_pool()
    write_pool(pool, tmp_path)
    promote_cli(tmp_path)
    with unmatched_path("MI", tmp_path).open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert rows
    assert all(row["reason"] for row in rows)


def test_promote_refuses_to_write_a_pack_with_no_cards(tmp_path: Path) -> None:
    # An empty manifest would be a pack; it is not one. Exit non-zero and say so.
    # The taxon ID is one the Michigan checklist has no listing for, under a
    # name PLANTS has no record of — so neither source speaks and nothing can
    # be promoted. Michigan lists almost everything, so this pairing has to be
    # constructed deliberately; see scripts/record_fixtures.py, SILENT_TAXON_ID.
    write_pool(
        pool_with([(RECORDED_SILENT_TAXON_ID, "Nonexistent plantus", "Nonexistent")]), tmp_path
    )
    result = promote_cli(tmp_path)
    assert result.exit_code == 10
    assert "no pack to emit" in result.output


def test_promote_without_a_resolved_pool_exits_nonzero(tmp_path: Path) -> None:
    result = promote_cli(tmp_path)
    assert result.exit_code == 6


def test_stats_manifest_reports_tiers_split_and_demotions(tmp_path: Path) -> None:
    write_pool(full_pool(), tmp_path)
    packs = tmp_path / "packs"
    promote_cli(tmp_path, packs=packs)
    manifest = Manifest.model_validate_json(
        (packs / "manifest_MI.json").read_text(encoding="utf-8")
    )
    report = summarise_manifest(
        manifest, unmatched_path("MI", tmp_path), report_path("MI", tmp_path)
    ).render()

    assert "tier_1:" in report
    assert "native:" in report
    assert "introduced:" in report
    assert "high:" in report
    assert "genus-demoted: 1" in report
    assert "Carex intumescens" in report


def test_stats_manifest_reports_how_the_two_sources_related(tmp_path: Path) -> None:
    write_pool(full_pool(), tmp_path)
    packs = tmp_path / "packs"
    promote_cli(tmp_path, packs=packs)
    manifest = Manifest.model_validate_json(
        (packs / "manifest_MI.json").read_text(encoding="utf-8")
    )
    report = summarise_manifest(
        manifest, unmatched_path("MI", tmp_path), report_path("MI", tmp_path)
    ).render()

    assert "nativity sources:" in report
    assert "claims backed by both:" in report
    assert "refused as conflicting: 3" in report
    assert "curated exclusions applied: 2" in report
    # Named, not merely counted: which three conflicted is the thing worth
    # knowing, and a count alone hides it.
    for name in CONTESTED:
        assert name in report
    assert "USDA native, checklist introduced" in report
    for name in EXCLUDED:
        assert name in report


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
        "excluded": [],
        "agreement": 0,
        "no_source": 0,
        "conflicts": [],
        "single_source": [],
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
