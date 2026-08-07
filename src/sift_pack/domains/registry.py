"""Resolution of a domain slug to a domain implementation.

WHY THIS MODULE EXISTS
----------------------
The CLI takes `--domain plants` as a string and needs an object. Doing that
lookup inline would put the decision "what happens for an unknown slug?" in
whichever caller happened to need it first, and the tempting answer there is to
fall back to plants — which would silently build a plant pack for someone who
asked for birds.

It is a separate module from `domains/__init__.py` because the implementations
import `Axis1Result` from that package; importing them back into it would make
the package import itself.

INVARIANT PROTECTED
-------------------
An unknown slug raises `UnknownDomainError` and names the slugs that do exist.
There is no default domain and no fallback. A domain that is known but not
implemented is resolvable — the caller gets the object, and it raises when
used, so "you asked for birds, which we do not support yet" and "you typed
birbs" are distinguishable errors.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from sift_pack.domains import TaxonDomain
from sift_pack.domains.birds import BirdsDomain
from sift_pack.domains.plants import PlantsDomain

__all__ = ["DOMAINS", "UnknownDomainError", "resolve_domain"]


class UnknownDomainError(LookupError):
    """Raised when a domain slug does not name a known domain."""


DOMAINS: Mapping[str, Callable[[], TaxonDomain]] = {
    PlantsDomain.slug: PlantsDomain,
    BirdsDomain.slug: BirdsDomain,
}
"""Every domain slug Sift knows, mapped to its constructor.

Includes domains that are not implemented — see `BirdsDomain`. Being in this
mapping means "Sift has a considered position on this taxon group", not "this
works today".
"""


def resolve_domain(slug: str) -> TaxonDomain:
    """Look up a domain implementation by slug.

    Args:
        slug: Domain identifier, e.g. `"plants"`.

    Returns:
        A fresh instance of the domain.

    Raises:
        UnknownDomainError: If `slug` names no known domain. Never falls back
            to a default: building the wrong domain's pack is worse than
            building none.

    Example:
        >>> resolve_domain("plants").slug
        'plants'
        >>> resolve_domain("birbs")
        Traceback (most recent call last):
            ...
        sift_pack.domains.registry.UnknownDomainError: unknown domain 'birbs'; known ...
    """
    try:
        factory = DOMAINS[slug]
    except KeyError as exc:
        known = ", ".join(sorted(DOMAINS))
        message = f"unknown domain {slug!r}; known domains: {known}"
        raise UnknownDomainError(message) from exc
    return factory()
