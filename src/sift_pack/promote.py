"""Promotion: resolved taxa plus a sourced claim become a manifest.

WHY THIS MODULE EXISTS
----------------------
This is the terminal step, and the only one whose output a learner sees. Every
structural decision since M1 was arranged to make exactly this function unable
to lie: `Taxon` has no default for `axis1_sources`, `CandidateTaxon` and
`ResolvedTaxon` have no axis-1 field at all, and `Axis1Result` cannot be built
without at least one source — a constraint mypy enforces, not a runtime check.
The result is that promotion has nothing to promote with unless a reconciler
handed it a claim, and no syntax available for inventing one.

WHAT IS DROPPED HERE
--------------------
A resolved taxon with no claim, for any of three reasons: the two nativity
sources disagreed (`source_conflict`), neither could label it
(`no_nativity_source`), or a human withheld it (`curated_exclusion`). Drops are
expected for a real fraction of any pool — iNaturalist and USDA maintain
independent taxonomies with no shared identifier, so some names simply do not
join, and genuinely contested plants are refused on purpose. Every dropped taxon
is written to `work/unmatched_<STATE>.csv` with its reason, because dropped and
unrecorded are different things: a pack that quietly went from 300 taxa to 250
is indistinguishable from one that was always 250.

GENUS DEMOTION
--------------
Some taxa are identifiable in the photograph only to genus. Those get
`answer_rank="genus"` so the card asks a question the photograph can actually
answer. Per `docs/decisions.md`, 2026-08-07, this is a safety net over a
frequency ranking that already excludes most hard taxa, not a filter the pack
depends on.

CURATED EXCLUSIONS
------------------
A short, committed, per-state list of taxa that must not become cards even when
both nativity sources agree — because both are answering a question subtly
different from the one the card asks. It is applied here, before the claim is
looked up, so an excluded taxon can never acquire one. See
`docs/decisions.md`, 2026-08-08.

INVARIANT PROTECTED
-------------------
Every taxon in the emitted manifest carries a claim that a named rule produced
from at least one named source at a recorded version, and every taxon that did
not is in the unmatched report with a reason. The two sets partition the input.
"""

from __future__ import annotations

import csv
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic import ValidationError as PydanticValidationError

from sift_pack.domains import Axis1Result, TaxonDomain
from sift_pack.manifest import AnswerRank, Image, Manifest, Taxon
from sift_pack.nativity import NativityReport
from sift_pack.resolved import ResolvedPool, ResolvedTaxon

__all__ = [
    "DEFAULT_DEMOTIONS_PATH",
    "DEFAULT_EXCLUSIONS_PATH",
    "MAX_EXCLUSIONS_PER_STATE",
    "ExcludedTaxon",
    "GenusDemotions",
    "PromotionPolicy",
    "PromotionReport",
    "StateExclusions",
    "load_demotions",
    "load_exclusions",
    "manifest_path",
    "promote",
    "report_path",
    "unmatched_path",
    "write_report",
    "write_unmatched",
]

_log = logging.getLogger(__name__)

DEFAULT_DEMOTIONS_PATH = Path("data/genus_demotions.json")
DEFAULT_EXCLUSIONS_PATH = Path("data/state_exclusions.json")

MAX_EXCLUSIONS_PER_STATE = 15
"""How many hand-curated exclusions one state may carry before this is a bug.

A cap rather than a guideline, enforced by `load_exclusions` raising. The list
exists because two independent sources can agree and both still be wrong for a
handful of taxa; at fifteen entries for a 300-taxon pack that explanation stops
being credible and the honest reading is that something systematic is wrong
upstream — a rank policy, a source, a region mapping — which more hand-patches
would hide rather than fix. See `docs/decisions.md`, 2026-08-08.
"""


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


class ExcludedTaxon(BaseModel):
    """One taxon a human decided must not become a card in one state.

    A pydantic model rather than a hand-parsed dict, for the same reason
    `data/places.json` is one: this is committed data edited by people, and
    every field being required with a minimum length is the difference between
    an entry that can be argued with and an entry that says `""`.

    Example:
        >>> ExcludedTaxon(
        ...     taxon_id=48627,
        ...     scientific_name="Echinacea purpurea",
        ...     reason="presumed extirpated as a native",
        ...     source="MNFI",
        ... ).taxon_id
        48627
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    taxon_id: int = Field(
        ge=1,
        description=(
            "iNaturalist taxon ID — the primary key, so the entry survives a "
            "rename (docs/decisions.md, 2026-08-05)."
        ),
    )
    scientific_name: str = Field(
        min_length=1,
        description=(
            "The name when the entry was written. Checked against the pool rather "
            "than trusted: if the ID now resolves to a different name, the taxon "
            "was merged or split and the reasoning may no longer apply to it."
        ),
    )
    reason: str = Field(
        min_length=1,
        description="Why this taxon cannot be shown, in enough detail to be argued with.",
    )
    source: str = Field(
        min_length=1,
        description=(
            "What establishes the reason. Required: an exclusion with no citeable "
            "basis is a preference, and this file is not for preferences."
        ),
    )


class StateExclusions(BaseModel):
    """The committed per-state exclusion list.

    Example:
        >>> entry = ExcludedTaxon(taxon_id=1, scientific_name="X", reason="y", source="z")
        >>> StateExclusions(states={"MI": [entry]}).for_state("mi")[1].scientific_name
        'X'
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    comment: str = Field(
        default="",
        description="Why the list exists, so a build log can carry the reasoning.",
    )
    states: dict[str, list[ExcludedTaxon]] = Field(
        default_factory=dict,
        description="Exclusions per state code. A state with no entry is normal, not an error.",
    )

    @model_validator(mode="after")
    def _check_cap(self) -> StateExclusions:
        """Refuse a list that has grown past the point of being edge cases.

        See `MAX_EXCLUSIONS_PER_STATE`. Enforced on parse rather than at the
        call site so that the cap cannot be bypassed by a caller that forgot it.
        """
        over = {
            state: len(entries)
            for state, entries in self.states.items()
            if len(entries) > MAX_EXCLUSIONS_PER_STATE
        }
        if over:
            listed = ", ".join(f"{state}={count}" for state, count in sorted(over.items()))
            message = (
                f"state exclusion list is over the cap of {MAX_EXCLUSIONS_PER_STATE} "
                f"per state ({listed}). That many hand-patches means a systematic problem "
                "upstream — a rank policy, a source, a region mapping — and lengthening "
                "this list would hide it rather than fix it. See docs/decisions.md, "
                "2026-08-08."
            )
            raise ValueError(message)
        return self

    def for_state(self, state: str) -> dict[int, ExcludedTaxon]:
        """The exclusions that apply to one state, keyed by taxon ID.

        Args:
            state: Region code, e.g. `"MI"`. Case-insensitive.

        Returns:
            Exclusions for that state; empty when it has none.

        Example:
            >>> StateExclusions().for_state("MI")
            {}
        """
        wanted = state.upper()
        for code, entries in self.states.items():
            if code.upper() == wanted:
                return {entry.taxon_id: entry for entry in entries}
        return {}


def load_exclusions(path: Path = DEFAULT_EXCLUSIONS_PATH) -> StateExclusions:
    """Read the committed per-state exclusion list.

    Args:
        path: Where the list lives.

    Returns:
        The parsed list.

    Raises:
        ValueError: If the file is missing, unreadable, malformed, has an entry
            with a field missing, or gives any state more than
            `MAX_EXCLUSIONS_PER_STATE` entries. A missing file is an error
            rather than an empty list: a pack that silently stopped excluding
            anything would ship the exact cards this list exists to withhold.

    Example:
        >>> sorted(entry.scientific_name for entry in load_exclusions().for_state("MI").values())
        ['Echinacea purpurea', 'Phragmites australis']
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        message = (
            f"state exclusion list at {path} is missing or unreadable: {exc}. "
            "It is committed data; restore it rather than building without it."
        )
        raise ValueError(message) from exc
    try:
        return StateExclusions.model_validate_json(text)
    except PydanticValidationError as exc:
        message = f"state exclusion list at {path} is malformed: {exc}"
        raise ValueError(message) from exc


@dataclass(frozen=True, slots=True)
class PromotionPolicy:
    """The two committed lists that decide what may become a card, and how.

    Bundled because they answer one question between them — "may this taxon be
    a card, and at what rank?" — and because a promotion that received one but
    not the other would silently apply half a policy.

    Attributes:
        demotions: Genera restricted to a genus-level question.
        exclusions: Taxa withheld entirely, per state.
    """

    demotions: GenusDemotions
    exclusions: StateExclusions

    @classmethod
    def load(
        cls,
        demotions_path: Path = DEFAULT_DEMOTIONS_PATH,
        exclusions_path: Path = DEFAULT_EXCLUSIONS_PATH,
    ) -> PromotionPolicy:
        """Read both committed lists.

        Args:
            demotions_path: Where the genus-demotion list lives.
            exclusions_path: Where the per-state exclusion list lives.

        Returns:
            The loaded policy.

        Raises:
            ValueError: If either list is missing or malformed.

        Example:
            >>> "Carex" in PromotionPolicy.load().demotions.genera
            True
        """
        return cls(
            demotions=load_demotions(demotions_path),
            exclusions=load_exclusions(exclusions_path),
        )


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
        excluded: Names of taxa withheld by the curated exclusion list.
        by_tier: How many claims came from each matching tier.
        by_value: How many claims said native, and how many introduced.
        nativity: How the two nativity sources related across the pool. `None`
            when promotion ran without a two-source index behind it.
    """

    promoted: int = 0
    unmatched: list[UnmatchedTaxon] = field(default_factory=list)
    demoted: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)
    by_tier: dict[str, int] = field(default_factory=dict)
    by_value: dict[str, int] = field(default_factory=dict)
    nativity: NativityReport | None = None

    def summary(self) -> str:
        """One-line summary for a report.

        Returns:
            Human-readable counts.

        Example:
            >>> PromotionReport(promoted=250).summary()
            '250 promoted, 0 unmatched, 0 genus-demoted, 0 excluded'
        """
        return (
            f"{self.promoted} promoted, {len(self.unmatched)} unmatched, "
            f"{len(self.demoted)} genus-demoted, {len(self.excluded)} excluded"
        )


def _exclusion_detail(exclusion: ExcludedTaxon, current_name: str) -> str:
    """Render an exclusion's reasoning for the unmatched report.

    A name that has moved under the ID since the entry was written is called out
    rather than ignored: the reasoning was recorded about an organism, and a
    merged or split taxon may no longer be that organism.
    """
    detail = f"{exclusion.reason} [{exclusion.source}]"
    if exclusion.scientific_name != current_name:
        return (
            f"{detail} — NOTE: this entry was written for "
            f"{exclusion.scientific_name!r}, but taxon {exclusion.taxon_id} now resolves "
            f"to {current_name!r}; re-check that the reasoning still applies"
        )
    return detail


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
        axis1_sources=list(claim.sources),
        axis1_confidence=claim.confidence,
        answer_rank=demotions.rank_for(resolved.genus),
        image_hashes=[photo.sha256 for photo in resolved.images],
    )


def promote(
    pool: ResolvedPool,
    domain: TaxonDomain,
    policy: PromotionPolicy,
    taxonomy_date: date,
    reasons: dict[int, tuple[str, str]] | None = None,
) -> tuple[Manifest, PromotionReport]:
    """Promote a resolved pool into a manifest, dropping every unclaimed taxon.

    Args:
        pool: The resolved pool to promote.
        domain: Supplies axis-1 claims. A domain with no source returns `None`
            for everything, and the manifest comes out empty — which remains the
            correct output for a build that resolved nothing.
        policy: The committed curation — genera restricted to a genus-level
            question, and taxa withheld entirely for this state. Exclusions are
            applied before the claim is looked up, so an excluded taxon cannot
            acquire one.
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
    excluded = policy.exclusions.for_state(pool.state)
    taxa: list[Taxon] = []
    images: list[Image] = []
    report = PromotionReport()

    for resolved in pool.taxa:
        exclusion = excluded.get(resolved.inat_taxon_id)
        if exclusion is not None:
            report.excluded.append(resolved.scientific_name)
            report.unmatched.append(
                UnmatchedTaxon(
                    inat_taxon_id=resolved.inat_taxon_id,
                    scientific_name=resolved.scientific_name,
                    reason="curated_exclusion",
                    detail=_exclusion_detail(exclusion, resolved.scientific_name),
                )
            )
            continue

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

        taxon = _taxon_from(resolved, claim, policy.demotions)
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
    nativity = report.nativity
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
                "excluded": sorted(report.excluded),
                "agreement": nativity.agreement if nativity else 0,
                "no_source": nativity.no_source if nativity else 0,
                "conflicts": [
                    {
                        "scientific_name": c.scientific_name,
                        "usda": c.usda_value,
                        "inat": c.inat_value,
                    }
                    for c in sorted(
                        nativity.conflicts if nativity else [],
                        key=lambda c: c.scientific_name,
                    )
                ],
                "single_source": [
                    {
                        "scientific_name": s.scientific_name,
                        "source": s.source,
                        "value": s.value,
                    }
                    for s in sorted(
                        nativity.single_source if nativity else [],
                        key=lambda s: s.scientific_name,
                    )
                ],
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
