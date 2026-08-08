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

from sift_pack.domains import Axis1Result, NonEmptySources
from sift_pack.domains.plants import PlantsDomain
from sift_pack.inat.nativity import inat_source_ref
from sift_pack.manifest import Confidence, Manifest, SourceRef, Taxon
from sift_pack.promote import (
    MAX_EXCLUSIONS_PER_STATE,
    ExcludedTaxon,
    GenusDemotions,
    PromotionPolicy,
    PromotionReport,
    StateExclusions,
    load_demotions,
    load_exclusions,
    promote,
    unmatched_path,
    write_unmatched,
)
from sift_pack.resolved import ResolvedPhoto, ResolvedPool, ResolvedTaxon
from sift_pack.usda.reconcile import usda_source_ref

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


USDA = usda_source_ref(VERSION)
INAT = inat_source_ref(WHEN, "Michigan")


def claim(value: str, confidence: str = "high", sources: int = 2) -> Axis1Result:
    """One nativity claim, backed by one source or by two agreeing ones."""
    both: NonEmptySources = (USDA, INAT)
    return Axis1Result(value, both if sources == 2 else (USDA,), confidence)  # type: ignore[arg-type]


NO_DEMOTIONS = GenusDemotions(frozenset(), "")
NO_EXCLUSIONS = StateExclusions()
OPEN_POLICY = PromotionPolicy(demotions=NO_DEMOTIONS, exclusions=NO_EXCLUSIONS)


def policy(
    demotions: GenusDemotions = NO_DEMOTIONS,
    exclusions: StateExclusions = NO_EXCLUSIONS,
) -> PromotionPolicy:
    """A promotion policy with either half overridden."""
    return PromotionPolicy(demotions=demotions, exclusions=exclusions)


# --- the invariant: no claim, no card -----------------------------------------


def test_a_taxon_without_a_claim_is_dropped_not_defaulted() -> None:
    pool = resolved_pool([resolved_taxon(1, "Genus one", "Genus")])
    manifest, report = promote(pool, PlantsDomain(), OPEN_POLICY, VERSION)
    assert manifest.taxa == []
    assert manifest.images == []
    assert len(report.unmatched) == 1


def test_a_taxon_with_a_claim_becomes_a_card_carrying_its_provenance() -> None:
    pool = resolved_pool([resolved_taxon(1, "Genus one", "Genus")])
    manifest, report = promote(pool, PlantsDomain({1: claim("native")}), OPEN_POLICY, VERSION)
    assert report.promoted == 1
    taxon = manifest.taxa[0]
    assert taxon.axis1_value == "native"
    assert [s.name for s in taxon.axis1_sources] == ["USDA PLANTS", INAT.name]
    assert taxon.axis1_confidence == "high"
    assert len(manifest.images) == 4


def test_promotion_partitions_the_pool() -> None:
    pool = resolved_pool([resolved_taxon(n, f"Genus {n}", "Genus") for n in range(1, 6)])
    manifest, report = promote(
        pool, PlantsDomain({1: claim("native"), 3: claim("introduced")}), OPEN_POLICY, VERSION
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
    with pytest.raises(ValidationError, match="axis1_sources"):
        Taxon(**fields)  # type: ignore[arg-type]
    with pytest.raises(ValidationError, match="axis1_sources"):
        Taxon(**{**fields, "axis1_sources": []})  # type: ignore[arg-type]


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
            axis1_sources=[USDA],
            axis1_confidence="low",  # type: ignore[arg-type]
            answer_rank="species",
            image_hashes=[f"{n:064x}" for n in range(4)],
        )


def test_every_promoted_claim_is_high_or_medium() -> None:
    pool = resolved_pool([resolved_taxon(n, f"Genus {n}", "Genus") for n in (1, 2)])
    manifest, _ = promote(
        pool,
        PlantsDomain({1: claim("native", "high"), 2: claim("introduced", "medium")}),
        OPEN_POLICY,
        VERSION,
    )
    assert {t.axis1_confidence for t in manifest.taxa} <= {"high", "medium"}


# --- the unmatched report ------------------------------------------------------


def test_every_dropped_taxon_reaches_the_csv_with_a_reason(tmp_path: Path) -> None:
    pool = resolved_pool([resolved_taxon(n, f"Genus {n}", "Genus") for n in range(1, 4)])
    reasons = {n: ("no_plants_record", f"nothing for taxon {n}") for n in (1, 2, 3)}
    _, report = promote(pool, PlantsDomain(), OPEN_POLICY, VERSION, reasons)
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
        pool, PlantsDomain({1: claim("native"), 2: claim("native")}), policy(demotions), VERSION
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
    manifest, _ = promote(pool, PlantsDomain({1: claim("native")}), OPEN_POLICY, VERSION)
    first = manifest.model_dump_json(indent=2)
    assert Manifest.model_validate_json(first).model_dump_json(indent=2) == first


def test_the_manifest_is_referentially_intact() -> None:
    pool = resolved_pool([resolved_taxon(n, f"Genus {n}", "Genus") for n in (1, 2)])
    manifest, _ = promote(pool, PlantsDomain({1: claim("native")}), OPEN_POLICY, VERSION)
    # Only the promoted taxon's images are carried; a dropped taxon's images
    # would be orphans and the schema would have rejected the manifest.
    assert {i.taxon_id for i in manifest.images} == {1}
    assert set(manifest.taxa[0].image_hashes) == {i.sha256 for i in manifest.images}


def test_the_manifest_carries_the_pools_sources() -> None:
    pool = resolved_pool([resolved_taxon(1, "Genus one", "Genus")])
    manifest, _ = promote(pool, PlantsDomain({1: claim("native")}), OPEN_POLICY, VERSION)
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
    assert "axis1_sources" in result.stdout


def test_the_promoted_manifest_json_is_plain_json() -> None:
    pool = resolved_pool([resolved_taxon(1, "Genus one", "Genus")])
    manifest, _ = promote(pool, PlantsDomain({1: claim("native")}), OPEN_POLICY, VERSION)
    payload = json.loads(manifest.model_dump_json())
    assert payload["taxa"][0]["axis1_sources"][0]["name"] == "USDA PLANTS"


# --- curated exclusions --------------------------------------------------------


def excluded(taxon_id: int, name: str) -> StateExclusions:
    """An exclusion list holding one Michigan entry."""
    return StateExclusions(
        states={
            "MI": [
                ExcludedTaxon(
                    taxon_id=taxon_id,
                    scientific_name=name,
                    reason="both sources agree and both are wrong here",
                    source="a citeable authority",
                )
            ]
        }
    )


def test_an_excluded_taxon_is_dropped_even_when_both_sources_agree() -> None:
    pool = resolved_pool(
        [resolved_taxon(1, "Genus one", "Genus"), resolved_taxon(2, "Two", "Genus")]
    )
    # Both taxa carry a two-source agreement claim; only the excluded one is withheld.
    manifest, report = promote(
        pool,
        PlantsDomain({1: claim("native"), 2: claim("native")}),
        policy(exclusions=excluded(1, "Genus one")),
        VERSION,
    )
    assert {t.inat_taxon_id for t in manifest.taxa} == {2}
    assert report.excluded == ["Genus one"]


def test_an_exclusion_reaches_the_csv_with_its_reason_and_source(tmp_path: Path) -> None:
    pool = resolved_pool([resolved_taxon(1, "Genus one", "Genus")])
    _, report = promote(
        pool,
        PlantsDomain({1: claim("native")}),
        policy(exclusions=excluded(1, "Genus one")),
        VERSION,
    )
    written = write_unmatched(report, "MI", tmp_path)
    with written.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert [r["reason"] for r in rows] == ["curated_exclusion"]
    assert "a citeable authority" in rows[0]["detail"]


def test_an_exclusion_for_another_state_does_not_apply() -> None:
    pool = resolved_pool([resolved_taxon(1, "Genus one", "Genus")])
    elsewhere = StateExclusions(
        states={
            "AZ": [ExcludedTaxon(taxon_id=1, scientific_name="Genus one", reason="r", source="s")]
        }
    )
    manifest, report = promote(
        pool, PlantsDomain({1: claim("native")}), policy(exclusions=elsewhere), VERSION
    )
    assert len(manifest.taxa) == 1
    assert report.excluded == []


def test_a_renamed_excluded_taxon_is_flagged_rather_than_silently_applied(tmp_path: Path) -> None:
    # The ID is the key, so the exclusion still fires — but the reasoning was
    # recorded about an organism, and a merged taxon may not be that organism.
    pool = resolved_pool([resolved_taxon(1, "Genus renamed", "Genus")])
    _, report = promote(
        pool,
        PlantsDomain({1: claim("native")}),
        policy(exclusions=excluded(1, "Genus one")),
        VERSION,
    )
    written = write_unmatched(report, "MI", tmp_path)
    with written.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert "re-check that the reasoning still applies" in rows[0]["detail"]


def test_the_committed_exclusion_list_withholds_the_two_seeded_taxa() -> None:
    entries = load_exclusions(REPO_ROOT / "data" / "state_exclusions.json").for_state("MI")
    assert {e.scientific_name for e in entries.values()} == {
        "Echinacea purpurea",
        "Phragmites australis",
    }


def test_every_committed_exclusion_cites_a_source() -> None:
    # An exclusion with no citeable basis is a preference, and this file is not
    # for preferences.
    exclusions = load_exclusions(REPO_ROOT / "data" / "state_exclusions.json")
    for entries in exclusions.states.values():
        for entry in entries:
            assert entry.source.strip()
            assert entry.reason.strip()


def test_the_exclusion_list_is_capped_so_it_cannot_hide_a_systematic_problem() -> None:
    too_many = [
        ExcludedTaxon(taxon_id=n, scientific_name=f"S {n}", reason="r", source="s")
        for n in range(1, MAX_EXCLUSIONS_PER_STATE + 2)
    ]
    with pytest.raises(ValidationError, match="over the cap"):
        StateExclusions(states={"MI": too_many})


def test_the_committed_list_is_within_the_cap() -> None:
    exclusions = load_exclusions(REPO_ROOT / "data" / "state_exclusions.json")
    for state, entries in exclusions.states.items():
        assert len(entries) <= MAX_EXCLUSIONS_PER_STATE, state


def test_a_missing_exclusion_list_is_an_error_not_an_empty_list(tmp_path: Path) -> None:
    # Silently excluding nothing would ship the exact cards the list withholds.
    with pytest.raises(ValueError, match="missing or unreadable"):
        load_exclusions(tmp_path / "absent.json")


def test_an_exclusion_missing_its_source_does_not_parse(tmp_path: Path) -> None:
    path = tmp_path / "exclusions.json"
    path.write_text(
        json.dumps({"states": {"MI": [{"taxon_id": 1, "scientific_name": "S", "reason": "r"}]}}),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="malformed"):
        load_exclusions(path)
