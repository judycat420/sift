"""The birds domain: reserved, and deliberately not implemented.

WHY THIS MODULE EXISTS
----------------------
It exists to fail loudly. The tempting shortcut when adding birds is to reuse
the plants domain — the protocol fits, the card engine is identical, and the
code would run. It would also be wrong: a bird's axis 1 is seasonality, not
nativity. A Michigan snowy owl is not "introduced" in January, it is a winter
visitor, and a barn swallow is not "native" in the same sense a milkweed is —
it is here from April to September and in Argentina the rest of the year.
Nativity vocabulary applied to birds produces answers that are grammatical,
confident and false.

Sharing the plants implementation would produce exactly that. So this module
provides a domain that imports cleanly and refuses to run, which turns a subtle
data-quality bug into an immediate, obvious crash with an explanation.

INVARIANT PROTECTED
-------------------
No bird pack can be built until somebody implements a seasonality axis. Import
succeeds so the registry can list the domain and the CLI can report it as
known-but-unavailable; every method raises.

See `docs/decisions.md`, 2026-08-06, "Bird axis 1 is seasonality, not nativity".
"""

from __future__ import annotations

from typing import NoReturn

from sift_pack.domains import Axis1Result

__all__ = ["BirdsDomain"]

_AVES_ICONIC_TAXON_ID = 3

_NOT_IMPLEMENTED = (
    "The birds domain is not implemented. Bird axis 1 is seasonality "
    "(resident / summer / winter / migrant), not nativity, and reusing the "
    "plants implementation would emit confidently wrong labels. See "
    "docs/decisions.md, 2026-08-06, 'Bird axis 1 is seasonality, not nativity'."
)


class BirdsDomain:
    """Birds: reserved. Every method raises `NotImplementedError`.

    Implements the shape of `sift_pack.domains.TaxonDomain` so that the
    registry and the CLI can see it, and nothing else.

    Example:
        >>> BirdsDomain().slug
        'birds'
    """

    slug: str = "birds"
    iconic_taxon_id: int = _AVES_ICONIC_TAXON_ID
    axis1_label: str = "Seasonality (not implemented)"

    def axis1_options(self, state: str) -> NoReturn:
        """Raise: the seasonality vocabulary is not decided yet.

        Args:
            state: Region code. Unused.

        Raises:
            NotImplementedError: Always.

        Example:
            >>> BirdsDomain().axis1_options("MI")
            Traceback (most recent call last):
                ...
            NotImplementedError: The birds domain is not implemented. ...
        """
        del state
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def axis1_answer(self, taxon_id: int, state: str) -> Axis1Result | None:
        """Raise: there is no seasonality source wired up, or agreed on.

        Note this raises rather than returning `None`. `None` means "this
        domain looked and could not determine the answer", which would be a
        lie — there is no domain here to look. An unimplemented domain is an
        exceptional condition; an unknown taxon is not.

        Args:
            taxon_id: iNaturalist taxon ID. Unused.
            state: Region code. Unused.

        Returns:
            Never returns.

        Raises:
            NotImplementedError: Always.

        Example:
            >>> BirdsDomain().axis1_answer(7089, "MI")
            Traceback (most recent call last):
                ...
            NotImplementedError: The birds domain is not implemented. ...
        """
        del taxon_id, state
        raise NotImplementedError(_NOT_IMPLEMENTED)

    def prompt_copy(self) -> NoReturn:
        """Raise: copy depends on the seasonality vocabulary, which is undecided.

        Raises:
            NotImplementedError: Always.

        Example:
            >>> BirdsDomain().prompt_copy()
            Traceback (most recent call last):
                ...
            NotImplementedError: The birds domain is not implemented. ...
        """
        raise NotImplementedError(_NOT_IMPLEMENTED)
