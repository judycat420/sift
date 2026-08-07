"""Command-line entry point, and the assembly stage that enforces the drop contract.

WHY THIS MODULE EXISTS
----------------------
This is where a candidate taxon either becomes a card or does not. It is the
one place in the pack pipeline that consults a domain's `axis1_answer`, and
therefore the one place that can violate the `None` contract by inventing a
value for a taxon the domain could not resolve.

So the conversion is written to make that impossible rather than to remember
not to do it: `_taxon_from` takes an `Axis1Result`, not an optional one, and
the only caller that can produce one is the branch that already checked for
`None`. There is no code path from `axis1_answer() -> None` to a `Taxon`.

INVARIANT PROTECTED
-------------------
Every candidate is either in the manifest with a sourced axis-1 claim, or in
the drop report with a reason. The two counts always sum to the number
considered, and `assemble` returns both together so a caller cannot read the
pack without also being handed what it cost.

In M1 the plants domain resolves nothing, so a build drops every candidate and
emits a valid, empty pack. That is the designed outcome, not a bug: see
`sift_pack.domains.plants`.
"""

from __future__ import annotations

import hashlib
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated

import typer

from sift_pack.domains import Axis1Result, TaxonDomain
from sift_pack.domains.registry import UnknownDomainError, resolve_domain
from sift_pack.manifest import AnswerRank, Image, Manifest, SourceRef, Taxon

__all__ = ["Candidate", "Drop", "DropReport", "app", "assemble", "build"]

_MIN_IMAGES_PER_TAXON = 4

# The iNaturalist taxonomy snapshot the candidate IDs below are stated against.
# Placeholder alongside the placeholder pool; M2 records the real export date.
_PLACEHOLDER_TAXONOMY_DATE = date(2026, 7, 1)

_EXIT_UNKNOWN_DOMAIN = 2
_EXIT_DOMAIN_UNAVAILABLE = 3


@dataclass(frozen=True, slots=True)
class Candidate:
    """A taxon under consideration for a pack, before its axis-1 claim is known.

    Deliberately has no axis-1 fields. A candidate is everything Sift knows
    from the taxonomy and image sources; the claim that makes it a card comes
    from the domain, and keeping them in separate types means a candidate
    cannot be handed to the manifest by mistake.

    Attributes:
        inat_taxon_id: Primary key.
        scientific_name: Current name; a mutable attribute of the ID.
        common_names: Vernacular names, most-used first.
        rank: Taxonomic rank as iNaturalist reports it.
        genus: Genus name.
        family: Family name.
        obs_count: Research-grade observation count.
        answer_rank: Whether cards may ask for species or must stop at genus.
        images: Licence-cleared images for this taxon.

    Example:
        >>> candidate = MICHIGAN_CANDIDATES[0]
        >>> candidate.scientific_name
        'Asclepias tuberosa'
    """

    inat_taxon_id: int
    scientific_name: str
    common_names: tuple[str, ...]
    rank: str
    genus: str
    family: str
    obs_count: int
    answer_rank: AnswerRank
    images: tuple[Image, ...]


@dataclass(frozen=True, slots=True)
class Drop:
    """One candidate that did not make it into the pack, and why.

    Attributes:
        inat_taxon_id: The candidate's primary key.
        scientific_name: Carried for readability in the report only.
        reason: Machine-readable reason code, e.g. `"axis1_undetermined"`.
        detail: Human-readable elaboration.
    """

    inat_taxon_id: int
    scientific_name: str
    reason: str
    detail: str


@dataclass(frozen=True, slots=True)
class DropReport:
    """What a build cost: how many candidates were considered, kept and dropped.

    Returned alongside every manifest so that a silently shrinking pack is
    impossible to miss (STANDARDS.md rule 5).

    Attributes:
        considered: How many candidates entered the stage.
        kept: How many became taxa in the manifest.
        drops: One entry per dropped candidate, in input order.
    """

    considered: int
    kept: int
    drops: tuple[Drop, ...]

    def counts_by_reason(self) -> dict[str, int]:
        """Summarise drops by reason code.

        Returns:
            Reason code to count, ordered most-frequent first.

        Example:
            >>> report = DropReport(
            ...     considered=1,
            ...     kept=0,
            ...     drops=(Drop(1, "Example one", "axis1_undetermined", "no source"),),
            ... )
            >>> report.counts_by_reason()
            {'axis1_undetermined': 1}
        """
        return dict(Counter(drop.reason for drop in self.drops).most_common())


def _placeholder_images(taxon_id: int, count: int = _MIN_IMAGES_PER_TAXON) -> tuple[Image, ...]:
    """Build deterministic stand-in image records for a candidate.

    These are NOT real photos. The digests are derived from the taxon ID rather
    than from any image bytes, and the attribution fields name nobody, because
    inventing a photographer to fill a required field is precisely the kind of
    fabrication this project exists to prevent. M2 replaces this with records
    read from the iNaturalist open dataset.
    """
    images: list[Image] = []
    for index in range(count):
        seed = f"sift-m1-placeholder:{taxon_id}:{index}".encode()
        images.append(
            Image(
                sha256=hashlib.sha256(seed).hexdigest(),
                inat_photo_id=index + 1,
                taxon_id=taxon_id,
                license="cc0",
                photographer_name=None,
                photographer_login="placeholder-not-a-real-account",
                observation_url="https://www.inaturalist.org/observations/0",
                width=1024,
                height=768,
                bytes=204_800,
            )
        )
    return tuple(images)


# Three Michigan species, chosen so the fixture reads like the real thing: a
# native prairie forb, an aggressive invasive, and a native the invasive
# competes with. The names are real; the taxon IDs, observation counts and
# images are UNVERIFIED PLACEHOLDERS pending the M2 iNaturalist ingest, and
# nothing downstream may treat them as authoritative. They exist to exercise
# the assembly path, and in M1 every one of them is dropped before it can
# reach a manifest.
MICHIGAN_CANDIDATES: tuple[Candidate, ...] = (
    Candidate(
        inat_taxon_id=48662,
        scientific_name="Asclepias tuberosa",
        common_names=("butterfly weed", "butterfly milkweed"),
        rank="species",
        genus="Asclepias",
        family="Apocynaceae",
        obs_count=0,
        answer_rank="species",
        images=_placeholder_images(48662),
    ),
    Candidate(
        inat_taxon_id=55849,
        scientific_name="Alliaria petiolata",
        common_names=("garlic mustard",),
        rank="species",
        genus="Alliaria",
        family="Brassicaceae",
        obs_count=0,
        answer_rank="species",
        images=_placeholder_images(55849),
    ),
    Candidate(
        inat_taxon_id=61944,
        scientific_name="Monarda fistulosa",
        common_names=("wild bergamot",),
        rank="species",
        genus="Monarda",
        family="Lamiaceae",
        obs_count=0,
        answer_rank="species",
        images=_placeholder_images(61944),
    ),
)

_INAT_SOURCE = SourceRef(
    name="iNaturalist",
    version="v1",
    retrieved_at=datetime(2026, 8, 6, tzinfo=UTC),
    url="https://api.inaturalist.org/v1/",
)


def _taxon_from(candidate: Candidate, claim: Axis1Result) -> Taxon:
    """Convert a candidate plus a resolved claim into a manifest taxon.

    Takes `Axis1Result`, never `Axis1Result | None`: the check happens in
    `assemble`, and this signature means no caller can skip it.
    """
    return Taxon(
        inat_taxon_id=candidate.inat_taxon_id,
        scientific_name=candidate.scientific_name,
        common_names=list(candidate.common_names),
        rank=candidate.rank,
        genus=candidate.genus,
        family=candidate.family,
        obs_count=candidate.obs_count,
        axis1_value=claim.value,
        axis1_source=claim.source,
        axis1_confidence=claim.confidence,
        answer_rank=candidate.answer_rank,
        image_hashes=[image.sha256 for image in candidate.images],
    )


def assemble(
    domain: TaxonDomain,
    state: str,
    candidates: tuple[Candidate, ...],
    *,
    built_at: datetime,
    taxonomy_date: date = _PLACEHOLDER_TAXONOMY_DATE,
) -> tuple[Manifest, DropReport]:
    """Assemble candidates into a pack, dropping any whose axis-1 claim is unknown.

    This is the enforcement point for the `None` contract in
    `sift_pack.domains.TaxonDomain`. A candidate whose `axis1_answer` is `None`
    is dropped and counted; it is never emitted with a default, a guess, or an
    empty axis-1 field. Images belonging to dropped candidates are dropped with
    them, which is what keeps the manifest referentially intact.

    Args:
        domain: The domain supplying axis-1 claims.
        state: Region code the pack is built for, e.g. `"MI"`.
        candidates: Taxa to consider, in the order they should appear.
        built_at: Build timestamp; must be timezone-aware.
        taxonomy_date: Which iNaturalist taxonomy snapshot the IDs refer to.

    Returns:
        The validated manifest, and the report of what was dropped to build it.
        Always both: reading the pack without its drop report is not offered.

    Raises:
        NotImplementedError: If `domain` is a reserved domain such as birds.
        pydantic.ValidationError: If the assembled pack is not internally
            consistent — a bug in this function, not in its inputs.

    Example:
        >>> from datetime import UTC, datetime
        >>> from sift_pack.domains.plants import PlantsDomain
        >>> manifest, report = assemble(
        ...     PlantsDomain(),
        ...     "MI",
        ...     MICHIGAN_CANDIDATES,
        ...     built_at=datetime(2026, 8, 6, tzinfo=UTC),
        ... )
        >>> len(manifest.taxa), report.counts_by_reason()
        (0, {'axis1_undetermined': 3})
    """
    taxa: list[Taxon] = []
    images: list[Image] = []
    drops: list[Drop] = []

    for candidate in candidates:
        claim = domain.axis1_answer(candidate.inat_taxon_id, state)
        if claim is None:
            drops.append(
                Drop(
                    inat_taxon_id=candidate.inat_taxon_id,
                    scientific_name=candidate.scientific_name,
                    reason="axis1_undetermined",
                    detail=(
                        f"domain {domain.slug!r} could not determine "
                        f"{domain.axis1_label!r} for state {state!r}"
                    ),
                )
            )
            continue
        taxa.append(_taxon_from(candidate, claim))
        images.extend(candidate.images)

    manifest = Manifest(
        domain=domain.slug,
        state=state,
        built_at=built_at,
        inat_taxonomy_date=taxonomy_date,
        sources=[_INAT_SOURCE],
        taxa=taxa,
        images=images,
    )
    report = DropReport(considered=len(candidates), kept=len(taxa), drops=tuple(drops))
    return manifest, report


app = typer.Typer(
    add_completion=False,
    help="Build Sift study packs.",
    no_args_is_help=True,
)


@app.callback()
def _root() -> None:
    """Anchor the CLI as a command group.

    Without a callback, Typer collapses a single-command app into a bare
    command, so `sift-pack build` would become `sift-pack`. Naming the verb
    keeps room for the fetch and verify commands M2 adds.
    """


def _emit_report(report: DropReport, state: str) -> None:
    """Write the drop accounting to stderr, so stdout stays pipeable JSON."""
    typer.echo(
        f"considered {report.considered}, kept {report.kept}, dropped {len(report.drops)}",
        err=True,
    )
    for reason, count in report.counts_by_reason().items():
        typer.echo(f"  dropped {count} for {reason}", err=True)
    for drop in report.drops:
        typer.echo(f"    {drop.inat_taxon_id} {drop.scientific_name}: {drop.detail}", err=True)
    if report.kept == 0:
        typer.echo(
            f"pack for {state!r} is empty: nothing could be resolved. "
            "An empty pack is correct output for a build that resolved nothing.",
            err=True,
        )


@app.command()
def build(
    domain: Annotated[str, typer.Option(help="Domain slug, e.g. 'plants'.")],
    state: Annotated[str, typer.Option(help="Region code, e.g. 'MI'.")],
    limit: Annotated[int, typer.Option(min=1, help="Maximum candidates to consider.")] = 50,
    out: Annotated[
        Path | None,
        typer.Option(help="Write the manifest here instead of stdout."),
    ] = None,
) -> None:
    """Build a pack and emit its manifest as JSON.

    The manifest goes to stdout (or `--out`); the drop accounting goes to
    stderr, so the JSON stays pipeable while the cost of the build stays
    visible.

    Args:
        domain: Which domain to build. Unknown slugs exit 2 rather than
            falling back to a default.
        state: Region code the pack is for.
        limit: Maximum number of candidates to consider.
        out: Optional path to write the manifest to.

    Raises:
        typer.Exit: Code 2 for an unknown domain, code 3 for a domain that is
            known but not implemented. Never exits 0 having guessed.

    Example:
        Run from a shell:

        >>> from typer.testing import CliRunner
        >>> result = CliRunner().invoke(app, ["build", "--domain", "plants", "--state", "MI"])
        >>> result.exit_code
        0
    """
    try:
        resolved = resolve_domain(domain)
    except UnknownDomainError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_UNKNOWN_DOMAIN) from exc

    try:
        manifest, report = assemble(
            resolved,
            state,
            MICHIGAN_CANDIDATES[:limit],
            built_at=datetime.now(UTC),
        )
    except NotImplementedError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(code=_EXIT_DOMAIN_UNAVAILABLE) from exc

    payload = manifest.model_dump_json(indent=2)
    if out is None:
        typer.echo(payload)
    else:
        out.write_text(payload + "\n", encoding="utf-8")
        typer.echo(f"wrote {out}", err=True)

    _emit_report(report, state)


def main() -> None:
    """Entry point for the `sift-pack` console script.

    Example:
        >>> callable(main)
        True
    """
    app()


if __name__ == "__main__":  # pragma: no cover - exercised via the console script
    sys.exit(app())
