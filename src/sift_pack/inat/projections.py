"""Reduction of API responses to the fields Sift actually reads.

WHY THIS MODULE EXISTS
----------------------
M2 cached raw responses and paid 1.5 GB per state for it. An observations page
is mostly fields Sift never looks at — geoprivacy, quality metrics, project
memberships, comment threads, a dozen photo size variants. Caching them stored
the API's whole worldview to answer four questions about it.

Projecting at the point of caching is not only a size fix. It makes the cache a
record of *what Sift understood*, which is the thing worth keeping: a cached
entry that replays correctly proves the parser saw what it needed, and a field
this module does not carry is a field no parser can quietly start depending on
without the omission showing up as a test failure.

INVARIANT PROTECTED
-------------------
Projection never invents. Every key here is copied verbatim or left as `None`
when absent, so a missing upstream field stays missing and the parsers'
drop-rather-than-guess logic (STANDARDS.md rule 5) still fires. Projection also
never drops a *record*: result counts are preserved exactly, so a bucket that
returned three observations still reads as three.

`PROJECTION_VERSION` is part of every cache key. Widening a projection means
bumping it, which invalidates cleanly — old entries are simply never read again,
rather than being served with a shape the new parser does not expect.
"""

from __future__ import annotations

from typing import Any

__all__ = ["PROJECTION_VERSION", "project"]

PROJECTION_VERSION = 1
"""Bump when any projection below changes shape.

Included in the cache key, so a bump partitions the cache rather than
corrupting it. Entries at older versions become unreachable and prunable.
"""


def _dimensions(photo: dict[str, Any]) -> dict[str, Any] | None:
    """Copy a photo's original dimensions, preserving absence."""
    dims = photo.get("original_dimensions")
    if not isinstance(dims, dict):
        return None
    return {"width": dims.get("width"), "height": dims.get("height")}


def _project_photo(photo: dict[str, Any]) -> dict[str, Any]:
    """Photo id, licence and original size.

    The `url` is deliberately not carried: image bytes come from the open-data
    bucket keyed by photo id, never from an API URL.
    """
    return {
        "id": photo.get("id"),
        "license_code": photo.get("license_code"),
        "original_dimensions": _dimensions(photo),
    }


def _project_observation(observation: dict[str, Any]) -> dict[str, Any]:
    """Identity, attribution, agreement count and photos."""
    user = observation.get("user")
    user_projection = (
        {"login": user.get("login"), "name": user.get("name")} if isinstance(user, dict) else None
    )
    photos = observation.get("photos")
    return {
        "id": observation.get("id"),
        "uri": observation.get("uri"),
        "num_identification_agreements": observation.get("num_identification_agreements"),
        "user": user_projection,
        "photos": [_project_photo(p) for p in photos if isinstance(p, dict)]
        if isinstance(photos, list)
        else None,
    }


def _project_species_count(entry: dict[str, Any]) -> dict[str, Any]:
    """Observation count plus the taxon's identity and rank."""
    taxon = entry.get("taxon")
    if not isinstance(taxon, dict):
        return {"count": entry.get("count"), "taxon": None}
    return {
        "count": entry.get("count"),
        "taxon": {
            "id": taxon.get("id"),
            "name": taxon.get("name"),
            "rank": taxon.get("rank"),
            "preferred_common_name": taxon.get("preferred_common_name"),
        },
    }


def _project_taxon(taxon: dict[str, Any]) -> dict[str, Any]:
    """Identity and the ancestor list, from which genus and family are read."""
    ancestors = taxon.get("ancestors")
    return {
        "id": taxon.get("id"),
        "name": taxon.get("name"),
        "ancestors": [
            {"rank": a.get("rank"), "name": a.get("name")} for a in ancestors if isinstance(a, dict)
        ]
        if isinstance(ancestors, list)
        else None,
    }


def _project_place(place: dict[str, Any]) -> dict[str, Any]:
    """Identity, admin level and ancestry — the three checks a state must pass."""
    return {
        "id": place.get("id"),
        "name": place.get("name"),
        "admin_level": place.get("admin_level"),
        "ancestor_place_ids": place.get("ancestor_place_ids"),
    }


_PROJECTIONS = {
    "observations": _project_observation,
    "species_counts": _project_species_count,
    "taxa_by_id": _project_taxon,
    "places_autocomplete": _project_place,
}


def project(endpoint: str, response: dict[str, Any]) -> dict[str, Any]:
    """Reduce one response to the fields Sift reads.

    Args:
        endpoint: Which endpoint produced `response`.
        response: The decoded, JSON-normalised response body.

    Returns:
        A body with the same `results` length and `total_results`, whose entries
        carry only the projected fields. Entries that are not JSON objects are
        preserved as `None` rather than removed, so a malformed record still
        occupies its place in the count and still reaches the parser's guards.

    Raises:
        KeyError: If `endpoint` has no projection. Adding an endpoint means
            deciding what Sift reads from it, so there is no passthrough
            default that would silently cache a whole response body.

    Example:
        >>> project("places_autocomplete", {"results": [{"id": 29, "name": "Michigan"}]})
        {'total_results': None, 'results': [{'id': 29, 'name': 'Michigan', ...}]}
    """
    projector = _PROJECTIONS[endpoint]
    results = response.get("results")
    projected: list[dict[str, Any] | None] = (
        [projector(item) if isinstance(item, dict) else None for item in results]
        if isinstance(results, list)
        else []
    )
    return {"total_results": response.get("total_results"), "results": projected}
