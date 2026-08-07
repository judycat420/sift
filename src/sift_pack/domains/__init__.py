"""The domain seam: what differs between plants, birds and pollinators.

WHY THIS MODULE EXISTS
----------------------
Sift will ship three kinds of pack — plants, birds, pollinators — and they are
far more alike than they look. All three ask the same first question ("what is
this organism?") and all three run on the same card engine, the same image
pipeline, the same scheduler. They differ in exactly one place: the *second*
axis of the question, the thing the learner is asked once identification is
settled.

For plants that axis is nativity: is this native here, or introduced? For birds
it is seasonality — a bird is not "introduced" in Michigan in January, it is
a winter resident, and forcing it into a nativity vocabulary would produce
confident nonsense. For pollinators it is something else again.

`TaxonDomain` is that difference and nothing else. Everything a domain does not
customise is absent from this protocol on purpose: each attribute here is a
place where the three domains genuinely disagree, and adding one is a claim
that they do.

INVARIANT PROTECTED
-------------------
An axis-1 claim can only exist as an `Axis1Result`, which cannot be constructed
without a source, a confidence and a source version. There is no code path that
produces a bare nativity string. The failure this prevents is the one that
matters most in this project: shipping a card that tells someone a plant is
native when nobody actually checked.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol, runtime_checkable

from sift_pack.manifest import Confidence

__all__ = ["Axis1Result", "TaxonDomain"]


@dataclass(frozen=True, slots=True)
class Axis1Result:
    """An axis-1 claim, inseparable from its provenance.

    This is STANDARDS.md rule 4 made structural. No field has a default, so
    `Axis1Result("native")` is not an incomplete object to be filled in later —
    it is a type error that mypy rejects before the code runs, and a `TypeError`
    if it somehow reaches runtime. There is deliberately no `from_value()`
    shortcut, no `unknown()` sentinel constructor, and no mutable field: an
    instance that exists is a claim somebody can stand behind.

    `frozen` so a claim cannot be edited away from its source after the fact;
    `slots` so a stray attribute cannot be bolted on to smuggle one through.

    Attributes:
        value: The claim itself, in the domain's vocabulary — e.g. `"native"`
            or `"introduced"` for plants. Never a display string.
        source: Which dataset asserted it, e.g. `"USDA PLANTS"`. The name must
            match a `SourceRef.name` in the manifest the claim ends up in.
        confidence: How much to trust it. Cannot be `"low"`: low-confidence
            claims are dropped upstream rather than downgraded into a pack.
        source_version: Which version of `source` was consulted. Without this,
            a claim cannot be re-checked when the upstream dataset changes, and
            "USDA says native" quietly means "USDA said native, once".

    Example:
        >>> from datetime import date
        >>> claim = Axis1Result(
        ...     value="native",
        ...     source="USDA PLANTS",
        ...     confidence="high",
        ...     source_version=date(2026, 7, 1),
        ... )
        >>> claim.value, claim.source
        ('native', 'USDA PLANTS')
    """

    value: str
    source: str
    confidence: Confidence
    source_version: date


@runtime_checkable
class TaxonDomain(Protocol):
    """What one kind of pack — plants, birds, pollinators — customises.

    THE `None` CONTRACT
    -------------------
    `axis1_answer` returns `None` to mean **"cannot determine"**, and that is
    the core reliability invariant of this project. A caller that receives
    `None` MUST:

    1. drop the taxon from the pack entirely, and
    2. increment a drop counter with a reason, so the run reports it.

    A caller MUST NOT substitute a default, fall back to the commonest value,
    infer from a neighbouring taxon, or emit the taxon with an empty axis-1
    field. There is no "unknown" member in the vocabulary to fall back *to*,
    which is deliberate: the schema gives a caller nowhere to put a guess.

    `None` is a normal, expected outcome, not an error. Most taxa will return
    `None` for most domains most of the time — regional datasets are patchy,
    and a pack of 40 confident cards is worth more than 400 shaky ones. Raising
    would be wrong here, because "we don't know" is not an exceptional
    condition; it is the default state of knowledge about a species in a place.

    Attributes:
        slug: Stable identifier used on the CLI and in `Manifest.domain`.
        iconic_taxon_id: iNaturalist iconic taxon this domain draws from —
            47126 Plantae, 3 Aves, 47158 Insecta. Bounds every query.
        axis1_label: Short human-readable name of the second axis, shown to the
            learner as the question, e.g. `"Native or introduced?"`.
    """

    slug: str
    iconic_taxon_id: int
    axis1_label: str

    def axis1_options(self, state: str) -> list[str]:
        """List the axis-1 values a learner may be asked to choose between.

        The option set is region-dependent because the honest set of answers
        is: a domain may offer a distinction in one region that is meaningless
        in another.

        Args:
            state: Region code the pack is being built for, e.g. `"MI"`.

        Returns:
            The permitted `Axis1Result.value` strings for this region, in the
            order they should be offered. Never empty.

        Raises:
            NotImplementedError: If the domain has not been implemented yet.

        Example:
            >>> from sift_pack.domains.plants import PlantsDomain
            >>> PlantsDomain().axis1_options("MI")
            ['native', 'introduced']
        """
        ...

    def axis1_answer(self, taxon_id: int, state: str) -> Axis1Result | None:
        """Determine the axis-1 claim for one taxon in one region.

        Args:
            taxon_id: iNaturalist taxon ID. The primary key — never a name.
            state: Region code the claim is being made about, e.g. `"MI"`.

        Returns:
            The claim with its provenance attached, or `None` if this domain
            cannot determine it for this taxon and region. See the `None`
            contract in the class docstring: `None` means drop and count, never
            substitute.

        Raises:
            NotImplementedError: If the domain has not been implemented yet.

        Example:
            >>> from sift_pack.domains.plants import PlantsDomain
            >>> PlantsDomain().axis1_answer(48662, "MI") is None
            True
        """
        ...

    def prompt_copy(self) -> dict[str, str]:
        """Return the domain's user-facing wording.

        Kept out of the card engine so that adding a domain does not mean
        editing a shared string table that every other domain also reads.

        Returns:
            Copy keyed by slot name. `"question"` and `"axis1_prompt"` are
            required; domains may add their own keys.

        Raises:
            NotImplementedError: If the domain has not been implemented yet.

        Example:
            >>> from sift_pack.domains.plants import PlantsDomain
            >>> PlantsDomain().prompt_copy()["axis1_prompt"]
            'Is it native to this state, or introduced?'
        """
        ...
