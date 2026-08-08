"""Tests that the type checker itself rejects unprovenanced claims.

WHY THIS MODULE EXISTS
----------------------
M1's stated goal is that shipping a wrong native-status label is
*unrepresentable in the type system*, not merely tested against. Every other
test in this suite checks runtime behaviour, which is the weaker claim: a
runtime `TypeError` means the mistake was made and then caught. These tests
check the stronger one, by running mypy over snippets that must not typecheck.

If mypy ever starts accepting one of these, the type-level guarantee has
silently regressed to a runtime one, and the headline claim of this milestone
is no longer true.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent

_PREAMBLE = """
from datetime import UTC, date, datetime

from sift_pack.domains import Axis1Result
from sift_pack.manifest import SourceRef, Taxon

SRC = SourceRef(
    name="USDA PLANTS",
    version="2026-07-01",
    retrieved_at=datetime(2026, 7, 1, tzinfo=UTC),
    url="https://plantsservices.sc.egov.usda.gov/api/",
)
SOURCES: list[SourceRef] = [SRC]
"""

REJECTED_SNIPPETS = {
    "value_only": 'claim = Axis1Result("native")',
    "no_confidence": 'claim = Axis1Result("native", (SRC,))',
    "bare_source_name": 'claim = Axis1Result("native", "USDA PLANTS", "high")',
    "low_confidence": 'claim = Axis1Result("native", (SRC,), "low")',
    # The M4.1 guarantee: an unsourced claim is not merely invalid at runtime,
    # it does not typecheck. `tuple[SourceRef, *tuple[SourceRef, ...]]` is
    # inhabited by every non-empty tuple and by nothing else.
    "no_sources_at_all": 'claim = Axis1Result("native", (), "high")',
    # The same guarantee through the back door most callers would reach for:
    # `tuple(a_list)` is `tuple[SourceRef, ...]`, which mypy will not accept
    # without the caller first proving the list is non-empty.
    "sources_from_an_unproven_list": 'claim = Axis1Result("native", tuple(SOURCES), "high")',
    "mutating_a_claim": 'claim = Axis1Result("native", (SRC,), "high")\nclaim.value = "a guess"',
    "smuggling_an_attribute": (
        'claim = Axis1Result("native", (SRC,), "high")\nclaim.note = "not sure"'
    ),
}

ACCEPTED_SNIPPET = 'claim = Axis1Result("native", (SRC,), "high")'


def _run_mypy(source: str, tmp_path: Path) -> subprocess.CompletedProcess[str]:
    """Type-check one snippet in isolation, against the installed package."""
    snippet = tmp_path / "snippet.py"
    snippet.write_text(_PREAMBLE + source + "\n", encoding="utf-8")
    return subprocess.run(  # noqa: S603 - fixed argv, no shell, path from sys.executable
        [
            sys.executable,
            "-m",
            "mypy",
            "--strict",
            "--no-incremental",
            # `--no-incremental` stops mypy *reading* a cache; it still creates
            # and writes the SQLite cache under cwd. With the suite running on
            # xdist workers these snippet checks overlap, so a shared
            # `<repo>/.mypy_cache` would be several processes writing one set of
            # database files. Each run gets its own under `tmp_path`.
            "--cache-dir",
            str(tmp_path / ".mypy_cache"),
            "--no-error-summary",
            "--hide-error-context",
            str(snippet),
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        check=False,
    )


@pytest.mark.parametrize("snippet", REJECTED_SNIPPETS.values(), ids=REJECTED_SNIPPETS.keys())
def test_mypy_rejects_unprovenanced_or_mutated_claims(snippet: str, tmp_path: Path) -> None:
    result = _run_mypy(snippet, tmp_path)
    assert result.returncode != 0, (
        f"mypy accepted a snippet it must reject:\n{snippet}\n{result.stdout}"
    )


def test_mypy_accepts_a_fully_provenanced_claim(tmp_path: Path) -> None:
    # The negative tests above would also pass if mypy were simply broken or
    # could not import the package. This is the control.
    result = _run_mypy(ACCEPTED_SNIPPET, tmp_path)
    assert result.returncode == 0, result.stdout


def test_mypy_rejects_a_taxon_built_without_a_source(tmp_path: Path) -> None:
    snippet = """
taxon = Taxon(
    inat_taxon_id=48662,
    scientific_name="Asclepias tuberosa",
    common_names=[],
    rank="species",
    genus="Asclepias",
    family="Apocynaceae",
    obs_count=1,
    axis1_value="native",
    axis1_confidence="high",
    answer_rank="species",
    image_hashes=["0" * 64] * 4,
)
"""
    result = _run_mypy(snippet, tmp_path)
    assert result.returncode != 0, result.stdout


def test_mypy_rejects_a_taxon_whose_source_is_a_bare_name(tmp_path: Path) -> None:
    # M4.1: `axis1_source: str` is gone. A claim names sources structurally, so
    # the old shape is not a weaker manifest — it is not a manifest.
    snippet = """
taxon = Taxon(
    inat_taxon_id=48662,
    scientific_name="Asclepias tuberosa",
    common_names=[],
    rank="species",
    genus="Asclepias",
    family="Apocynaceae",
    obs_count=1,
    axis1_value="native",
    axis1_source="USDA PLANTS",
    axis1_confidence="high",
    answer_rank="species",
    image_hashes=["0" * 64] * 4,
)
"""
    result = _run_mypy(snippet, tmp_path)
    assert result.returncode != 0, result.stdout
