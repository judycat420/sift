"""The plants domain: axis 1 is nativity, sourced from USDA PLANTS.

WHY THIS MODULE EXISTS
----------------------
Nativity is the claim Sift is most likely to get wrong and least likely to be
caught getting wrong. "Is this native to Michigan?" has a real answer, learners
act on it — what to plant, what to pull — and a confidently wrong answer looks
exactly like a right one on a card.

INVARIANT PROTECTED
-------------------
`axis1_answer` returns a claim only when a `NativityIndex` was supplied and that
index holds a reconciled PLANTS match for the taxon. A `PlantsDomain()` built
with no index returns `None` for everything — which is what it did from M1
through M3, and is still the correct behaviour for a domain nobody has given a
source to. Nothing changed about the contract when the source arrived; the
claims simply stopped being absent.

The index is built by `sift_pack.usda.reconcile`, which is the only code in Sift
that can construct an `Axis1Result`. This module looks claims up; it does not
derive them, and it has no path that invents one for a taxon the index does not
cover.
"""

from __future__ import annotations

from collections.abc import Mapping

from sift_pack.domains import Axis1Result

__all__ = ["NativityIndex", "PlantsDomain"]

_PLANTAE_ICONIC_TAXON_ID = 47126

NativityIndex = Mapping[int, Axis1Result]
"""iNaturalist taxon ID to its reconciled nativity claim.

A mapping rather than a lookup function so that the domain cannot trigger work:
every claim in it was derived, recorded and auditable before the domain saw it,
and a taxon missing from it is missing because reconciliation declined to make a
claim, not because a lookup happened to fail at card-building time."""


class PlantsDomain:
    """Plants: native or introduced, per USDA PLANTS state distribution data.

    Implements `sift_pack.domains.TaxonDomain`. Conformance is checked
    statically — see `tests/test_domains.py` — rather than by inheriting from
    the protocol, so the protocol stays a description of the seam and not a
    base class that accumulates shared behaviour.

    Example:
        >>> PlantsDomain().axis1_label
        'Native or introduced?'
    """

    # Annotated instance attributes, not ClassVar: the protocol declares these
    # as instance variables, and a ClassVar would not satisfy it.
    slug: str = "plants"
    iconic_taxon_id: int = _PLANTAE_ICONIC_TAXON_ID
    axis1_label: str = "Native or introduced?"

    def __init__(self, nativity: NativityIndex | None = None) -> None:
        """Build the domain, optionally with reconciled nativity claims.

        Args:
            nativity: Claims keyed by iNaturalist taxon ID, from
                `sift_pack.usda.reconcile`. Omitted, the domain determines
                nothing and every taxon is dropped — the M1 behaviour, which is
                still correct for a domain with no source behind it.
        """
        self.nativity: NativityIndex = {} if nativity is None else nativity

    def axis1_options(self, state: str) -> list[str]:
        """List the nativity values a learner may choose between.

        Args:
            state: Region code, e.g. `"MI"`. Accepted for protocol conformance
                and ignored for now: the native/introduced distinction holds in
                every US state Sift covers. A region where it does not — a
                territory with no USDA distribution data, say — would need its
                own handling rather than a silent reuse of this list.

        Returns:
            `["native", "introduced"]`, in the order to offer them.

        Example:
            >>> PlantsDomain().axis1_options("MI")
            ['native', 'introduced']
        """
        del state  # Intentionally unused; see Args.
        return ["native", "introduced"]

    def axis1_answer(self, taxon_id: int, state: str) -> Axis1Result | None:
        """Look up whether USDA PLANTS calls a taxon native.

        Args:
            taxon_id: iNaturalist taxon ID to look up.
            state: Two-letter US state code the pack is for. Accepted for
                protocol conformance and not consulted: PLANTS records native
                status per region, never per state, so the honest scope of the
                claim is the lower 48 rather than Michigan specifically. See
                `docs/decisions.md`, 2026-08-08.

        Returns:
            The reconciled claim, or `None` when the index holds none — which
            means reconciliation declined to make one. Callers must drop the
            taxon and count the drop; they must not substitute a default.

        Example:
            >>> PlantsDomain().axis1_answer(48662, "MI") is None
            True
            >>> from datetime import date
            >>> from sift_pack.domains import Axis1Result
            >>> claim = Axis1Result("native", "USDA PLANTS", "high", date(2026, 8, 8))
            >>> PlantsDomain({48662: claim}).axis1_answer(48662, "MI").value
            'native'
        """
        del state  # See Args: PLANTS has no per-state native status.
        return self.nativity.get(taxon_id)

    def prompt_copy(self) -> dict[str, str]:
        """Return the plants domain's user-facing wording.

        Returns:
            Copy keyed by slot name, with `"question"` and `"axis1_prompt"`.

        Example:
            >>> PlantsDomain().prompt_copy()["question"]
            'What plant is this?'
        """
        return {
            "question": "What plant is this?",
            "axis1_prompt": "Is it native to this state, or introduced?",
            "axis1_native": "Native",
            "axis1_introduced": "Introduced",
        }
