"""The plants domain: axis 1 is nativity, sourced from USDA PLANTS.

WHY THIS MODULE EXISTS
----------------------
Nativity is the claim Sift is most likely to get wrong and least likely to be
caught getting wrong. "Is this native to Michigan?" has a real answer, learners
act on it — what to plant, what to pull — and a confidently wrong answer looks
exactly like a right one on a card.

INVARIANT PROTECTED
-------------------
`axis1_answer` currently returns `None` for every input, because nothing is
wired to USDA PLANTS until M3. This is the module working correctly, not a stub
awaiting completion: with no source consulted, `None` ("cannot determine") is
the only honest answer, and the protocol's `None` contract means every such
taxon is dropped and counted. The result is an empty deck, which is the right
output for a build that knows nothing — an empty deck teaches nobody anything,
but a wrong one teaches them something false.

When M3 lands, only the body of `axis1_answer` changes. Its signature, and
every caller's obligation to handle `None`, stay exactly as they are now.
"""

from __future__ import annotations

from sift_pack.domains import Axis1Result

__all__ = ["PlantsDomain"]

_PLANTAE_ICONIC_TAXON_ID = 47126


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
        """Determine whether a taxon is native to a state.

        Returns `None` for every input until M3 wires in USDA PLANTS. Nothing
        has been consulted, so there is nothing to claim, and inventing a claim
        here is the precise failure the type system in this package exists to
        prevent.

        Args:
            taxon_id: iNaturalist taxon ID to look up.
            state: Two-letter US state code the claim is about.

        Returns:
            Always `None` in M1, meaning "cannot determine". Callers must drop
            the taxon and count the drop; they must not substitute a default.
            From M3, an `Axis1Result` sourced from USDA PLANTS when that dataset
            covers the taxon in that state, and `None` when it does not.

        Example:
            >>> PlantsDomain().axis1_answer(48662, "MI") is None
            True
        """
        del taxon_id, state  # No source is wired up yet; see the module docstring.
        return None

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
