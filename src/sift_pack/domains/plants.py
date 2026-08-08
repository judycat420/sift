"""The plants domain: axis 1 is nativity, and only where two sources agree.

WHY THIS MODULE EXISTS
----------------------
Nativity is the claim Sift is most likely to get wrong and least likely to be
caught getting wrong. "Is this native to Michigan?" has a real answer, learners
act on it — what to plant, what to pull — and a confidently wrong answer looks
exactly like a right one on a card.

INVARIANT PROTECTED
-------------------
`axis1_answer` returns a claim only when a `NativityIndex` was supplied and that
index holds one for the taxon. A `PlantsDomain()` built with no index returns
`None` for everything — which is what it did from M1 through M3, and is still
the correct behaviour for a domain nobody has given a source to. Nothing changed
about the contract when the sources arrived; the claims simply stopped being
absent.

The index is built by `sift_pack.nativity`, which reconciles USDA PLANTS against
the state's iNaturalist place checklist and produces a claim only where they
agree or where exactly one of them has an answer. A taxon the two sources
contradict each other about is absent from the index, and therefore gets no card
— see `docs/decisions.md`, 2026-08-08. This module looks claims up; it does not
derive them, cannot break a tie, and has no path that invents one for a taxon
the index does not cover.
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
claim — for want of a source, or because two sources disagreed — not because a
lookup happened to fail at card-building time.

Scoped to one state by construction. The claims in it were built against one
place's checklist, so the index for Michigan is not a general answer that
happens to be about Michigan; it is the only thing `axis1_answer` can return."""


class PlantsDomain:
    """Plants: native or introduced, where PLANTS and the place checklist agree.

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
                `sift_pack.nativity.decide_pool`. Omitted, the domain determines
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
        """Look up the reconciled nativity claim for a taxon.

        Args:
            taxon_id: iNaturalist taxon ID to look up.
            state: Two-letter US state code the pack is for. Accepted for
                protocol conformance and not consulted *here*, because the index
                this domain was built with is already scoped to one state: the
                place checklist half of every claim in it was answered by that
                state's own list and refused otherwise (`sift_pack.nativity`,
                `sift_pack.inat.nativity`). Looking the state up again at card
                time would be re-deriving something already decided, and would
                invite a second, divergent answer.

        Returns:
            The reconciled claim, or `None` when the index holds none — which
            means reconciliation declined to make one. Callers must drop the
            taxon and count the drop; they must not substitute a default.

        Example:
            >>> PlantsDomain().axis1_answer(48662, "MI") is None
            True
            >>> from datetime import date
            >>> from sift_pack.domains import Axis1Result
            >>> from sift_pack.usda.reconcile import usda_source_ref
            >>> claim = Axis1Result("native", (usda_source_ref(date(2026, 8, 8)),), "high")
            >>> PlantsDomain({48662: claim}).axis1_answer(48662, "MI").value
            'native'
        """
        del state  # See Args: the index is already scoped to one state.
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
