"""Tests for the build path: the drop contract, and the end-to-end CLI."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sift_pack.cli import MICHIGAN_CANDIDATES, Candidate, Drop, DropReport, app, assemble
from sift_pack.domains import Axis1Result
from sift_pack.domains.birds import BirdsDomain
from sift_pack.domains.plants import PlantsDomain
from sift_pack.manifest import Manifest

BUILT_AT = datetime(2026, 8, 6, 12, 0, 0, tzinfo=UTC)

runner = CliRunner()


class _ResolvingDomain:
    """A stand-in for the M3 plants domain: resolves every taxon with provenance.

    Exists so the assembly path can be tested at full strength before USDA is
    wired in. If this stub produces a valid, complete manifest, then M3 only has
    to make `PlantsDomain.axis1_answer` return real claims.
    """

    slug: str = "plants"
    iconic_taxon_id: int = 47126
    axis1_label: str = "Native or introduced?"

    def axis1_options(self, state: str) -> list[str]:
        del state
        return ["native", "introduced"]

    def axis1_answer(self, taxon_id: int, state: str) -> Axis1Result | None:
        del state
        value = "introduced" if taxon_id == 55849 else "native"
        return Axis1Result(
            value=value,
            source="USDA PLANTS",
            confidence="high",
            source_version=date(2026, 7, 1),
        )

    def prompt_copy(self) -> dict[str, str]:
        return {"question": "What plant is this?", "axis1_prompt": "Native or introduced?"}


class _PartialDomain(_ResolvingDomain):
    """Resolves some taxa and not others — the realistic M3 case."""

    def axis1_answer(self, taxon_id: int, state: str) -> Axis1Result | None:
        if taxon_id == 55849:
            return None
        return super().axis1_answer(taxon_id, state)


# --- the drop contract --------------------------------------------------------


def test_unresolved_taxa_are_dropped_not_defaulted() -> None:
    manifest, report = assemble(PlantsDomain(), "MI", MICHIGAN_CANDIDATES, built_at=BUILT_AT)
    assert manifest.taxa == []
    assert manifest.images == []
    assert report.considered == 3
    assert report.kept == 0
    assert report.counts_by_reason() == {"axis1_undetermined": 3}


def test_the_empty_pack_is_still_a_valid_manifest() -> None:
    manifest, _ = assemble(PlantsDomain(), "MI", MICHIGAN_CANDIDATES, built_at=BUILT_AT)
    round_tripped = Manifest.model_validate_json(manifest.model_dump_json())
    assert round_tripped.domain == "plants"
    assert round_tripped.sources  # attribution survives even with no taxa


def test_every_drop_names_the_taxon_and_a_reason() -> None:
    _, report = assemble(PlantsDomain(), "MI", MICHIGAN_CANDIDATES, built_at=BUILT_AT)
    dropped_ids = {drop.inat_taxon_id for drop in report.drops}
    assert dropped_ids == {c.inat_taxon_id for c in MICHIGAN_CANDIDATES}
    for drop in report.drops:
        assert drop.reason == "axis1_undetermined"
        assert drop.scientific_name
        assert drop.detail


def test_considered_always_equals_kept_plus_dropped() -> None:
    for domain in (PlantsDomain(), _ResolvingDomain(), _PartialDomain()):
        _, report = assemble(domain, "MI", MICHIGAN_CANDIDATES, built_at=BUILT_AT)
        assert report.considered == report.kept + len(report.drops)


# --- the path M3 will take ----------------------------------------------------


def test_a_resolving_domain_produces_a_complete_manifest() -> None:
    manifest, report = assemble(_ResolvingDomain(), "MI", MICHIGAN_CANDIDATES, built_at=BUILT_AT)
    assert report.kept == 3
    assert report.drops == ()
    assert len(manifest.taxa) == 3
    assert len(manifest.images) == 12
    for taxon in manifest.taxa:
        assert taxon.axis1_source == "USDA PLANTS"
        assert taxon.axis1_confidence == "high"


def test_the_claim_travels_into_the_manifest_unchanged() -> None:
    manifest, _ = assemble(_ResolvingDomain(), "MI", MICHIGAN_CANDIDATES, built_at=BUILT_AT)
    by_id = {taxon.inat_taxon_id: taxon for taxon in manifest.taxa}
    assert by_id[55849].axis1_value == "introduced"  # garlic mustard
    assert by_id[48662].axis1_value == "native"  # butterfly weed


def test_a_partial_domain_keeps_only_what_it_resolved() -> None:
    manifest, report = assemble(_PartialDomain(), "MI", MICHIGAN_CANDIDATES, built_at=BUILT_AT)
    assert report.kept == 2
    assert report.counts_by_reason() == {"axis1_undetermined": 1}
    assert 55849 not in {taxon.inat_taxon_id for taxon in manifest.taxa}
    # The dropped taxon's images went with it, or the manifest would not validate.
    assert 55849 not in {image.taxon_id for image in manifest.images}
    assert len(manifest.images) == 8


def test_a_full_manifest_round_trips_byte_identically() -> None:
    manifest, _ = assemble(_ResolvingDomain(), "MI", MICHIGAN_CANDIDATES, built_at=BUILT_AT)
    first = manifest.model_dump_json(indent=2)
    second = Manifest.model_validate_json(first).model_dump_json(indent=2)
    assert first == second


def test_assemble_propagates_the_birds_refusal() -> None:
    with pytest.raises(NotImplementedError, match="seasonality"):
        assemble(BirdsDomain(), "MI", MICHIGAN_CANDIDATES, built_at=BUILT_AT)


# --- the CLI ------------------------------------------------------------------


def test_build_emits_a_valid_manifest() -> None:
    result = runner.invoke(app, ["build", "--domain", "plants", "--state", "MI", "--limit", "3"])
    assert result.exit_code == 0, result.output
    manifest = Manifest.model_validate_json(result.stdout)
    assert manifest.domain == "plants"
    assert manifest.state == "MI"
    assert manifest.pack_version == 1
    assert manifest.taxa == []


def test_build_reports_its_drops_on_stderr() -> None:
    result = runner.invoke(app, ["build", "--domain", "plants", "--state", "MI", "--limit", "3"])
    assert result.exit_code == 0
    assert "considered 3, kept 0, dropped 3" in result.stderr
    assert "axis1_undetermined" in result.stderr
    assert "is empty" in result.stderr


def test_build_keeps_stdout_pipeable() -> None:
    # stdout must be nothing but JSON, so `sift-pack build ... | jq` works.
    result = runner.invoke(app, ["build", "--domain", "plants", "--state", "MI"])
    assert json.loads(result.stdout)["domain"] == "plants"


def test_build_honours_the_limit() -> None:
    result = runner.invoke(app, ["build", "--domain", "plants", "--state", "MI", "--limit", "1"])
    assert "considered 1," in result.stderr


def test_build_writes_to_a_file_when_asked(tmp_path: Path) -> None:
    out = tmp_path / "pack.json"
    result = runner.invoke(app, ["build", "--domain", "plants", "--state", "MI", "--out", str(out)])
    assert result.exit_code == 0
    assert Manifest.model_validate_json(out.read_text(encoding="utf-8")).domain == "plants"


def test_build_rejects_an_unknown_domain_without_falling_back() -> None:
    result = runner.invoke(app, ["build", "--domain", "birbs", "--state", "MI"])
    assert result.exit_code == 2
    assert "unknown domain 'birbs'" in result.stderr
    assert result.stdout == ""


def test_build_refuses_birds_with_an_explanation() -> None:
    result = runner.invoke(app, ["build", "--domain", "birds", "--state", "MI"])
    assert result.exit_code == 3
    assert "seasonality" in result.stderr
    assert "docs/decisions.md" in result.stderr
    assert result.stdout == ""


# --- report accounting --------------------------------------------------------


def test_drop_report_counts_group_by_reason() -> None:
    report = DropReport(
        considered=3,
        kept=1,
        drops=(
            Drop(1, "One", "axis1_undetermined", "d"),
            Drop(2, "Two", "axis1_undetermined", "d"),
        ),
    )
    assert report.counts_by_reason() == {"axis1_undetermined": 2}


def test_candidates_carry_enough_images_to_pass_the_schema() -> None:
    for candidate in MICHIGAN_CANDIDATES:
        assert len(candidate.images) >= 4
        assert all(image.taxon_id == candidate.inat_taxon_id for image in candidate.images)


def test_candidate_has_no_axis1_fields() -> None:
    # A candidate must not be able to carry a claim; that is the domain's job.
    assert not [f for f in Candidate.__dataclass_fields__ if f.startswith("axis1")]
