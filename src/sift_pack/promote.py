"""Promotion: resolved taxa plus a sourced claim become a manifest.

WHY THIS MODULE EXISTS
----------------------
This is the terminal step, and the only one whose output a learner sees. Every
structural decision since M1 was arranged to make exactly this function unable
to lie: `Taxon` has no default for `axis1_source`, `CandidateTaxon` and
`ResolvedTaxon` have no axis-1 field at all, and `Axis1Result` cannot be built
without a source and a version. The result is that promotion has nothing to
promote with unless a reconciler handed it a claim, and no syntax available for
inventing one.

WHAT IS DROPPED HERE
--------------------
A resolved taxon with no claim. That is the expected outcome for a real
fraction of any pool — iNaturalist and USDA maintain independent taxonomies with
no shared identifier, so some names simply do not join. Those taxa are written to
`work/unmatched_<STATE>.csv` with the reason reconciliation gave, because
dropped and unrecorded are different things: a pack that quietly went from 300
taxa to 250 is indistinguishable from one that was always 250.

GENUS DEMOTION
--------------
Some taxa are identifiable in the photograph only to genus. Those get
`answer_rank="genus"` so the card asks a question the photograph can actually
answer. Per `docs/decisions.md`, 2026-08-07, this is a safety net over a
frequency ranking that already excludes most hard taxa, not a filter the pack
depends on.

INVARIANT PROTECTED
-------------------
Every taxon in the emitted manifest carries a claim that a named rule produced
from a named source at a recorded version, and every taxon that did not is in
the unmatched report with a reason. The two sets partition the input.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from sift_pack.domains import Axis1Result, TaxonDomain
from sift_pack.manifest import AnswerRank, Image, Manifest, Taxon
from sift_pack.resolved import ResolvedPool, ResolvedTaxon

__all__ = [
    "DEFAULT_DEMOTIONS_PATH",
    "GenusDemotions",
    "PromotionReport",
    "load_demotions",
    "manifest_path",
    "promote",
    "report_path",
    "unmatched_path",
    "write_report",
    "write_unmatched",
]

_log = logging.getLogger(__name__)

DEFAULT_DEMOTIONS_PATH = Path("data/genus_demotions.json")


def manifest_path(state: str, packs_dir: Path) -> Path:
    """Where a state's finished manifest is written.

    Args:
        state: Region code.
        packs_dir: Directory holding finished packs.

    Returns:
        The path.

    Example:
        >>> manifest_path("MI", Path("packs")).as_posix()
        'packs/manifest_MI.json'
    """
    return packs_dir / f"manifest_{state.upper()}.json"


def unmatched_path(state: str, work_dir: Path) -> Path:
    """Where a state's unmatched-taxon report is written.

    Args:
        state: Region code.
        work_dir: Directory holding build artefacts.

    Returns:
        The path.

    Example:
        >>> unmatched_path("MI", Path("work")).as_posix()
        'work/unmatched_MI.csv'
    """
    return work_dir / f"unmatched_{state.upper()}.csv"


@dataclass(frozen=True, slots=True)
class GenusDemotions:
    """Genera whose species a card may not ask about by name.

    Attributes:
        genera: Genus names, matched exactly against `Taxon.genus`.
        note: Why the list exists, carried so a reader of the pack build can see
            the reasoning without opening the data file.
    """

    genera: frozenset[str]
    note: str

    def rank_for(self, genus: str) -> AnswerRank:
        """The answer rank a taxon in this genus may be asked at.

        Args:
            genus: The taxon's genus.

        Returns:
            `"genus"` for a demoted genus, otherwise `"species"`.

        Example:
            >>> GenusDemotions(frozenset({"Carex"}), "").rank_for("Carex")
            'genus'
            >>> GenusDemotions(frozenset({"Carex"}), "").rank_for("Asclepias")
            'species'
        """
        return "genus" if genus in self.genera else "species"


def load_demotions(path: Path = DEFAULT_DEMOTIONS_PATH) -> GenusDemotions:
    """Read the committed genus-demotion list.

    Args:
        path: Where the list lives.

    Returns:
        The parsed list.

    Raises:
        ValueError: If the file is missing or malformed. An absent list is an
            error rather than an empty set: silently demoting nothing would ship
            species-level cards for genera nobody can key from a photograph.

    Example:
        >>> "Carex" in load_demotions().genera
        True
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        message = f"genus demotion list at {path} is missing or unreadable: {exc}"
        raise ValueError(message) from exc
    genera = payload.get("genera")
    if not isinstance(genera, list) or not all(isinstance(g, str) for g in genera):
        message = f"{path} does not contain a list of genus names"
        raise ValueError(message)
    return GenusDemotions(genera=frozenset(genera), note=str(payload.get("comment", "")))


@dataclass(frozen=True, slots=True)
class UnmatchedTaxon:
    """One resolved taxon that could not acquire a nativity claim.

    Attributes:
        inat_taxon_id: The taxon.
        scientific_name: Its name, for a readable report.
        reason: Why no claim could be made.
        detail: What specifically was wrong.
    """

    inat_taxon_id: int
    scientific_name: str
    reason: str
    detail: str


@dataclass(slots=True)
class PromotionReport:
    """What promotion kept, dropped and demoted.

    Attributes:
        promoted: Taxa that became cards.
        unmatched: Taxa dropped for want of a claim.
        demoted: Names of taxa restricted to a genus-level question.
        by_tier: How many claims came from each matching tier.
        by_value: How many claims said native, and how many introduced.
    """

    promoted: int = 0
    unmatched: list[UnmatchedTaxon] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)
    by_tier: dict[str, int] = field(default_factory=dict)
    by_value: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        """One-line summary for a report.

        Returns:
            Human-readable counts.

        Example:
            >>> PromotionReport(promoted=250).summary()
            '250 promoted, 0 unmatched, 0 genus-demoted'
        """
        return (
            f"{self.promoted} promoted, {len(self.unmatched)} unmatched, "
            f"{len(self.demoted)} genus-demoted"
        )


def _taxon_from(
    resolved: ResolvedTaxon,
    claim: Axis1Result,
    demotions: GenusDemotions,
) -> Taxon:
    """Build one manifest taxon from a resolved taxon and its claim.

    Takes `Axis1Result`, never `Axis1Result | None`: the check happens in
    `promote`, and this signature means no caller can skip it.
    """
    return Taxon(
        inat_taxon_id=resolved.inat_taxon_id,
        scientific_name=resolved.scientific_name,
        common_names=list(resolved.common_names),
        rank=resolved.rank,
        genus=resolved.genus,
        family=resolved.family,
        obs_count=resolved.obs_count,
        axis1_value=claim.value,
        axis1_source=claim.source,
        axis1_confidence=claim.confidence,
        answer_rank=demotions.rank_for(resolved.genus),
        image_hashes=[photo.sha256 for photo in resolved.images],
    )


def promote(
    pool: ResolvedPool,
    domain: TaxonDomain,
    demotions: GenusDemotions,
    taxonomy_date: date,
    reasons: dict[int, tuple[str, str]] | None = None,
) -> tuple[Manifest, PromotionReport]:
    """Promote a resolved pool into a manifest, dropping every unclaimed taxon.

    Args:
        pool: The resolved pool to promote.
        domain: Supplies axis-1 claims. A domain with no source returns `None`
            for everything, and the manifest comes out empty — which remains the
            correct output for a build that resolved nothing.
        demotions: Genera restricted to genus-level questions.
        taxonomy_date: iNaturalist taxonomy snapshot the IDs refer to.
        reasons: Per-taxon rejection reasons from reconciliation, so the
            unmatched report can say why rather than only that.

    Returns:
        The manifest and a report of what it cost.

    Raises:
        pydantic.ValidationError: If the assembled manifest is not internally
            consistent — a bug here, not in the inputs.

    Example:
        >>> promote(pool, domain, demotions, date(2026, 7, 1))  # doctest: +SKIP
        ... # SKIPPED: needs a resolved pool. Covered by tests/test_promote.py.
    """
    reasons = reasons or {}
    taxa: list[Taxon] = []
    images: list[Image] = []
    report = PromotionReport()

    for resolved in pool.taxa:
        claim = domain.axis1_answer(resolved.inat_taxon_id, pool.state)
        if claim is None:
            reason, detail = reasons.get(
                resolved.inat_taxon_id,
                ("no_claim", "the domain determined no axis-1 value for this taxon"),
            )
            report.unmatched.append(
                UnmatchedTaxon(
                    inat_taxon_id=resolved.inat_taxon_id,
                    scientific_name=resolved.scientific_name,
                    reason=reason,
                    detail=detail,
                )
            )
            continue

        taxon = _taxon_from(resolved, claim, demotions)
        taxa.append(taxon)
        images.extend(photo.as_image() for photo in resolved.images)
        report.promoted += 1
        report.by_value[claim.value] = report.by_value.get(claim.value, 0) + 1
        if taxon.answer_rank == "genus":
            report.demoted.append(resolved.scientific_name)

    manifest = Manifest(
        domain=pool.domain,
        state=pool.state,
        built_at=datetime.now(UTC),
        inat_taxonomy_date=taxonomy_date,
        sources=list(pool.sources),
        taxa=taxa,
        images=images,
    )
    _log.info("promotion complete: %s", report.summary())
    return manifest, report


def report_path(state: str, work_dir: Path) -> Path:
    """Where a state's promotion report is written.

    Args:
        state: Region code.
        work_dir: Directory holding build artefacts.

    Returns:
        The path.

    Example:
        >>> report_path("MI", Path("work")).as_posix()
        'work/promotion_MI.json'
    """
    return work_dir / f"promotion_{state.upper()}.json"


def write_report(report: PromotionReport, state: str, work_dir: Path) -> Path:
    """Record which matching tier produced each claim.

    The manifest deliberately does not carry the tier: it is a fact about how
    Sift built the pack, not about the plant, and the runtime has no use for it.
    But a build is not auditable without it — "288 taxa matched" says nothing
    about whether they matched exactly or loosely — so it is written alongside.

    Args:
        report: The promotion report.
        state: Region code.
        work_dir: Directory holding build artefacts.

    Returns:
        The path written.

    Example:
        >>> write_report(PromotionReport(), "MI", Path("/tmp"))  # doctest: +SKIP
        ... # SKIPPED: writes a file. Covered by tests/test_promote.py.
    """
    destination = report_path(state, work_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(
        json.dumps(
            {
                "promoted": report.promoted,
                "unmatched": len(report.unmatched),
                "by_tier": report.by_tier,
                "by_value": report.by_value,
                "demoted": sorted(report.demoted),
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return destination


def write_unmatched(report: PromotionReport, state: str, work_dir: Path) -> Path:
    """Write every dropped taxon to a CSV, with its reason.

    Written even when empty, so the absence of drops is a recorded fact rather
    than a missing file that might mean the step never ran.

    Args:
        report: The promotion report.
        state: Region code.
        work_dir: Directory holding build artefacts.

    Returns:
        The path written.

    Example:
        >>> write_unmatched(PromotionReport(), "MI", Path("/tmp"))  # doctest: +SKIP
        ... # SKIPPED: writes a file. Covered by tests/test_promote.py.
    """
    destination = unmatched_path(state, work_dir)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["inat_taxon_id", "scientific_name", "reason", "detail"])
        for row in sorted(report.unmatched, key=lambda u: u.scientific_name):
            writer.writerow([row.inat_taxon_id, row.scientific_name, row.reason, row.detail])
    return destination
