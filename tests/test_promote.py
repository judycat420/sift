"""Tests for promotion — the terminal step, and the one that can ship a wrong label."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import get_args

import pytest
from pydantic import ValidationError

from sift_pack.domains import Axis1Result
from sift_pack.domains.plants import PlantsDomain
from sift_pack.manifest import Confidence, Manifest, SourceRef, Taxon
from sift_pack.promote import (
    GenusDemotions,
    PromotionReport,
    load_demotions,
    promote,
    unmatched_path,
    write_unmatched,
)
from sift_pack.resolved import ResolvedPhoto, ResolvedPool, ResolvedTaxon

REPO_ROOT = Path(__file__).resolve().parent.parent
WHEN = datetime(2026, 8, 8, tzinfo=UTC)
VERSION = date(2026, 8, 8)


def photo(index: int, taxon_id: int) -> ResolvedPhoto:
    """One stored image record."""
    return ResolvedPhoto(
        sha256=f"{index:064x}",
        inat_photo_id=index,
        taxon_id=taxon_id,
        license="cc0",
        photographer_name=None,
        photographer_login=f"obs{index}",
        observation_url=f"https://www.inaturalist.org/observations/{index}",
        width=500,
        height=333,
        bytes=20480,
        month_bucket="ABCD"[index % 4],
    )


def resolved_taxon(taxon_id: int, name: str, genus: str) -> ResolvedTaxon:
    """One resolved taxon with four stored images."""
    images = [photo(taxon_id * 10 + n, taxon_id) for n in range(4)]
    return ResolvedTaxon(
        inat_taxon_id=taxon_id,
        scientific_name=name,
        common_names=[],
        rank="species",
        genus=genus,
        family="Familia",
        obs_count=500,
        months_represented=len({i.month_bucket for i in images}),
        distinct_observers=len({i.photographer_login for i in images}),
        images=images,
    )


def resolved_pool(taxa: list[ResolvedTaxon]) -> ResolvedPool:
    """A resolved pool wrapping the given taxa."""
    return ResolvedPool(
        domain="plants",
        state="MI",
        place_id=29,
        fetched_at=WHEN,
        resolved_at=WHEN,
        sources=[SourceRef(name="x", version="1", retrieved_at=WHEN, url="https://x.invalid/")],
        taxa=taxa,
        dropped=[],
        resolve_dropped=[],
    )


def claim(value: str, confidence: str = "high") -> Axis1Result:
    """One nativity claim."""
    return Axis1Result(value, "USDA PLANTS", confidence, VERSION)  # type: ignore[arg-type]


NO_DEMOTIONS = GenusDemotions(frozenset(), "")


# --- the invariant: no claim, no card -----------------------------------------


def test_a_taxon_without_a_claim_is_dropped_not_defaulted() -> None:
    pool = resolved_pool([resolved_taxon(1, "Genus one", "Genus")])
    manifest, report = promote(pool, PlantsDomain(), NO_DEMOTIONS, VERSION)
    assert manifest.taxa == []
    assert manifest.images == []
    assert len(report.unmatched) == 1


def test_a_taxon_with_a_claim_becomes_a_card_carrying_its_provenance() -> None:
    pool = resolved_pool([resolved_taxon(1, "Genus one", "Genus")])
    manifest, report = promote(pool, PlantsDomain({1: claim("native")}), NO_DEMOTIONS, VERSION)
    assert report.promoted == 1
    taxon = manifest.taxa[0]
    assert taxon.axis1_value == "native"
    assert taxon.axis1_source == "USDA PLANTS"
    assert taxon.axis1_confidence == "high"
    assert len(manifest.images) == 4


def test_promotion_partitions_the_pool() -> None:
    pool = resolved_pool([resolved_taxon(n, f"Genus {n}", "Genus") for n in range(1, 6)])
    manifest, report = promote(
        pool, PlantsDomain({1: claim("native"), 3: claim("introduced")}), NO_DEMOTIONS, VERSION
    )
    assert len(manifest.taxa) + len(report.unmatched) == len(pool.taxa)
    assert {t.inat_taxon_id for t in manifest.taxa} == {1, 3}
    assert {u.inat_taxon_id for u in report.unmatched} == {2, 4, 5}


def test_no_taxon_is_constructible_without_an_axis1_source() -> None:
    # The property the whole pipeline is built around, asserted directly.
    fields = {
        "inat_taxon_id": 1,
        "scientific_name": "Genus one",
        "common_names": [],
        "rank": "species",
        "genus": "Genus",
        "family": "Familia",
        "obs_count": 1,
        "axis1_value": "native",
        "axis1_confidence": "high",
        "answer_rank": "species",
        "image_hashes": [f"{n:064x}" for n in range(4)],
    }
    with pytest.raises(ValidationError, match="axis1_source"):
        Taxon(**fields)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="axis1_source"):
        Taxon(**{**fields, "axis1_source": ""})  # type: ignore[arg-type]


def test_confidence_below_medium_is_unrepresentable() -> None:
    # Not "rejected at validation" — there is no member for it in the type at
    # all, so no code path can produce one. `Axis1Result` is a plain dataclass
    # and does no runtime checking; that guarantee is mypy's, and is asserted in
    # tests/test_type_level_guarantees.py. What is checked here is that the
    # vocabulary has exactly two members and the manifest model refuses a third.
    assert set(get_args(Confidence)) == {"high", "medium"}

    with pytest.raises(ValidationError, match="axis1_confidence"):
        Taxon(
            inat_taxon_id=1,
            scientific_name="X",
            common_names=[],
            rank="species",
            genus="G",
            family="F",
            obs_count=1,
            axis1_value="native",
            axis1_source="USDA PLANTS",
            axis1_confidence="low",  # type: ignore[arg-type]
            answer_rank="species",
            image_hashes=[f"{n:064x}" for n in range(4)],
        )


def test_every_promoted_claim_is_high_or_medium() -> None:
    pool = resolved_pool([resolved_taxon(n, f"Genus {n}", "Genus") for n in (1, 2)])
    manifest, _ = promote(
        pool,
        PlantsDomain({1: claim("native", "high"), 2: claim("introduced", "medium")}),
        NO_DEMOTIONS,
        VERSION,
    )
    assert {t.axis1_confidence for t in manifest.taxa} <= {"high", "medium"}


# --- the unmatched report ------------------------------------------------------


def test_every_dropped_taxon_reaches_the_csv_with_a_reason(tmp_path: Path) -> None:
    pool = resolved_pool([resolved_taxon(n, f"Genus {n}", "Genus") for n in range(1, 4)])
    reasons = {n: ("no_plants_record", f"nothing for taxon {n}") for n in (1, 2, 3)}
    _, report = promote(pool, PlantsDomain(), NO_DEMOTIONS, VERSION, reasons)
    written = write_unmatched(report, "MI", tmp_path)

    with written.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 3
    assert {int(r["inat_taxon_id"]) for r in rows} == {1, 2, 3}
    for row in rows:
        assert row["reason"]
        assert row["detail"]


def test_the_report_is_written_even_when_nothing_was_dropped(tmp_path: Path) -> None:
    # An absent file could mean "no drops" or "the step never ran".
    written = write_unmatched(PromotionReport(), "MI", tmp_path)
    assert written.exists()
    assert written.read_text(encoding="utf-8").strip().splitlines() == [
        "inat_taxon_id,scientific_name,reason,detail"
    ]


def test_the_report_path_is_named_for_its_state() -> None:
    assert unmatched_path("mi", Path("work")).name == "unmatched_MI.csv"


# --- genus demotion ------------------------------------------------------------


def test_a_demoted_genus_asks_only_for_genus() -> None:
    pool = resolved_pool(
        [
            resolved_taxon(1, "Carex intumescens", "Carex"),
            resolved_taxon(2, "Pinus strobus", "Pinus"),
        ]
    )
    demotions = GenusDemotions(frozenset({"Carex"}), "")
    manifest, report = promote(
        pool, PlantsDomain({1: claim("native"), 2: claim("native")}), demotions, VERSION
    )
    ranks = {t.scientific_name: t.answer_rank for t in manifest.taxa}
    assert ranks == {"Carex intumescens": "genus", "Pinus strobus": "species"}
    assert report.demoted == ["Carex intumescens"]


def test_the_committed_demotion_list_holds_the_seeded_genera() -> None:
    genera = load_demotions(REPO_ROOT / "data" / "genus_demotions.json").genera
    assert {"Carex", "Symphyotrichum", "Solidago", "Crataegus", "Rubus", "Amelanchier"} <= genera


def test_the_demotion_list_does_not_catch_distinctive_genera() -> None:
    # Per docs/decisions.md 2026-08-07 this is a safety net, not a filter. A
    # genus whose common species are individually distinctive does not belong.
    genera = load_demotions(REPO_ROOT / "data" / "genus_demotions.json").genera
    assert not genera & {"Quercus", "Betula", "Populus", "Rosa", "Equisetum", "Taraxacum", "Pinus"}


def test_a_missing_demotion_list_is_an_error_not_an_empty_set(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="missing or unreadable"):
        load_demotions(tmp_path / "absent.json")


# --- the finished manifest -----------------------------------------------------


def test_the_manifest_round_trips() -> None:
    pool = resolved_pool([resolved_taxon(1, "Genus one", "Genus")])
    manifest, _ = promote(pool, PlantsDomain({1: claim("native")}), NO_DEMOTIONS, VERSION)
    first = manifest.model_dump_json(indent=2)
    assert Manifest.model_validate_json(first).model_dump_json(indent=2) == first


def test_the_manifest_is_referentially_intact() -> None:
    pool = resolved_pool([resolved_taxon(n, f"Genus {n}", "Genus") for n in (1, 2)])
    manifest, _ = promote(pool, PlantsDomain({1: claim("native")}), NO_DEMOTIONS, VERSION)
    # Only the promoted taxon's images are carried; a dropped taxon's images
    # would be orphans and the schema would have rejected the manifest.
    assert {i.taxon_id for i in manifest.images} == {1}
    assert set(manifest.taxa[0].image_hashes) == {i.sha256 for i in manifest.images}


def test_the_manifest_carries_the_pools_sources() -> None:
    pool = resolved_pool([resolved_taxon(1, "Genus one", "Genus")])
    manifest, _ = promote(pool, PlantsDomain({1: claim("native")}), NO_DEMOTIONS, VERSION)
    assert manifest.sources


# --- the static guarantee ------------------------------------------------------


def test_mypy_rejects_a_taxon_built_without_a_source(tmp_path: Path) -> None:
    # The runtime check above proves the model rejects it; this proves the type
    # checker does, so the mistake cannot reach a running program.
    snippet = tmp_path / "snippet.py"
    snippet.write_text(
        "from sift_pack.manifest import Taxon\n"
        "taxon = Taxon(\n"
        "    inat_taxon_id=1,\n"
        '    scientific_name="X",\n'
        "    common_names=[],\n"
        '    rank="species",\n'
        '    genus="G",\n'
        '    family="F",\n'
        "    obs_count=1,\n"
        '    axis1_value="native",\n'
        '    axis1_confidence="high",\n'
        '    answer_rank="species",\n'
        '    image_hashes=["0" * 64] * 4,\n'
        ")\n",
        encoding="utf-8",
    )
    result = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "mypy", "--strict", "--no-incremental", str(snippet)],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )
    assert result.returncode != 0, result.stdout
    assert "axis1_source" in result.stdout


def test_the_promoted_manifest_json_is_plain_json() -> None:
    pool = resolved_pool([resolved_taxon(1, "Genus one", "Genus")])
    manifest, _ = promote(pool, PlantsDomain({1: claim("native")}), NO_DEMOTIONS, VERSION)
    payload = json.loads(manifest.model_dump_json())
    assert payload["taxa"][0]["axis1_source"] == "USDA PLANTS"
