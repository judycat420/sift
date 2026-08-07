"""Resolution of US state names to iNaturalist place IDs.

WHY THIS MODULE EXISTS
----------------------
Every observation query is scoped by `place_id`, and iNaturalist's place IDs are
not guessable: Michigan is 29, and nothing about "MI" or "Michigan" implies
that. Resolving a name at fetch time would mean an autocomplete request before
every run and, worse, a silent dependence on autocomplete ranking — a query for
"Washington" returns a national heritage corridor and a wildlife foundation
alongside the state, and picking the first result would scope an entire pack to
the wrong polygon while looking like it worked.

So resolution happens once, deliberately, and the answer is committed to
`data/places.json`. Fetches read the file; nobody resolves at runtime.

INVARIANT PROTECTED
-------------------
A place ID in the table is one that iNaturalist reported as `admin_level == 10`
(state) with the United States among its ancestors. A state whose lookup does
not meet both conditions is recorded as unresolved rather than guessed at, and
a fetch for it fails loudly instead of quietly building a pack for a park.
"""

from __future__ import annotations

import logging
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from sift_pack.inat.client import InatClient, InatError

__all__ = [
    "PLACES_PATH",
    "STATE_NAMES",
    "PlaceTable",
    "StatePlace",
    "UnknownStateError",
    "load_places",
    "refresh_places",
]

_log = logging.getLogger(__name__)

PLACES_PATH = Path("data/places.json")
"""Committed lookup table. Regenerated only by `sift-pack places --refresh`."""

_US_PLACE_ID = 1
"""iNaturalist's place ID for the United States; states must descend from it."""

_STATE_ADMIN_LEVEL = 10
"""iNaturalist's admin level for a first-order division (a US state)."""

STATE_NAMES: dict[str, str] = {
    "AL": "Alabama",
    "AK": "Alaska",
    "AZ": "Arizona",
    "AR": "Arkansas",
    "CA": "California",
    "CO": "Colorado",
    "CT": "Connecticut",
    "DE": "Delaware",
    "FL": "Florida",
    "GA": "Georgia",
    "HI": "Hawaii",
    "ID": "Idaho",
    "IL": "Illinois",
    "IN": "Indiana",
    "IA": "Iowa",
    "KS": "Kansas",
    "KY": "Kentucky",
    "LA": "Louisiana",
    "ME": "Maine",
    "MD": "Maryland",
    "MA": "Massachusetts",
    "MI": "Michigan",
    "MN": "Minnesota",
    "MS": "Mississippi",
    "MO": "Missouri",
    "MT": "Montana",
    "NE": "Nebraska",
    "NV": "Nevada",
    "NH": "New Hampshire",
    "NJ": "New Jersey",
    "NM": "New Mexico",
    "NY": "New York",
    "NC": "North Carolina",
    "ND": "North Dakota",
    "OH": "Ohio",
    "OK": "Oklahoma",
    "OR": "Oregon",
    "PA": "Pennsylvania",
    "RI": "Rhode Island",
    "SC": "South Carolina",
    "SD": "South Dakota",
    "TN": "Tennessee",
    "TX": "Texas",
    "UT": "Utah",
    "VT": "Vermont",
    "VA": "Virginia",
    "WA": "Washington",
    "WV": "West Virginia",
    "WI": "Wisconsin",
    "WY": "Wyoming",
}
"""The 50 states, by USPS code. Territories are absent because USDA PLANTS
coverage for them differs enough to need its own handling rather than silent
inclusion here."""


class UnknownStateError(InatError):
    """Raised when a state code has no resolved place ID."""


class StatePlace(BaseModel):
    """One resolved state.

    Example:
        >>> StatePlace(code="MI", name="Michigan", place_id=29).place_id
        29
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=2, max_length=2, description="USPS two-letter code.")
    name: str = Field(min_length=1, description="State name as iNaturalist reports it.")
    place_id: int = Field(ge=1, description="iNaturalist place ID, admin_level 10.")


class PlaceTable(BaseModel):
    """The committed state-to-place-ID table.

    Example:
        >>> table = PlaceTable(states=[StatePlace(code="MI", name="Michigan", place_id=29)])
        >>> table.place_id_for("MI")
        29
        >>> table.place_id_for("ZZ")
        Traceback (most recent call last):
            ...
        sift_pack.inat.places.UnknownStateError: ...
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    states: list[StatePlace] = Field(description="Resolved states, in code order.")

    def place_id_for(self, code: str) -> int:
        """Look up one state's place ID.

        Args:
            code: USPS two-letter code, e.g. `"MI"`. Case-insensitive.

        Returns:
            The iNaturalist place ID.

        Raises:
            UnknownStateError: If the code is not in the table. Never falls back
                to a nearby or default place.

        Example:
            >>> PlaceTable(
            ...     states=[StatePlace(code="MI", name="Michigan", place_id=29)]
            ... ).place_id_for("mi")
            29
        """
        wanted = code.upper()
        for state in self.states:
            if state.code == wanted:
                return state.place_id
        known = ", ".join(sorted(state.code for state in self.states))
        message = f"no place ID for state {code!r}; resolved states: {known or '(none)'}"
        raise UnknownStateError(message)


def load_places(path: Path = PLACES_PATH) -> PlaceTable:
    """Read the committed place table.

    Args:
        path: Where the table lives.

    Returns:
        The parsed table.

    Raises:
        InatError: If the file is missing or unparseable. Missing is an error,
            not an empty table: silently resolving nothing would make every
            subsequent fetch fail with a confusing message instead of this one.

    Example:
        >>> load_places().place_id_for("MI")
        29
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        message = (
            f"place table {path} is missing or unreadable: {exc}. "
            "Regenerate it with `sift-pack places --refresh`."
        )
        raise InatError(message) from exc
    return PlaceTable.model_validate_json(text)


def _select_state_place(results: list[object], name: str) -> int | None:
    """Pick the one autocomplete result that is actually a US state.

    Returns `None` rather than a best guess when nothing qualifies: a wrong
    place ID silently scopes a whole pack to the wrong polygon.
    """
    for entry in results:
        if not isinstance(entry, dict):
            continue
        admin_level = entry.get("admin_level")
        ancestors = entry.get("ancestor_place_ids")
        place_id = entry.get("id")
        if (
            admin_level == _STATE_ADMIN_LEVEL
            and isinstance(ancestors, list)
            and _US_PLACE_ID in ancestors
            and entry.get("name") == name
            and isinstance(place_id, int)
        ):
            return place_id
    return None


def refresh_places(client: InatClient, path: Path = PLACES_PATH) -> PlaceTable:
    """Re-resolve every state against the API and rewrite the table.

    A one-time cost, run by hand. Not run in CI: it makes 50 live requests and
    the answers change essentially never.

    Args:
        client: Client to resolve through.
        path: Where to write the table.

    Returns:
        The freshly resolved table.

    Raises:
        InatError: If any state fails to resolve. All-or-nothing on purpose —
            a half-written table would silently break exactly the states it
            omitted, and only for whoever tried to build them.

    Example:
        >>> refresh_places(InatClient(Path("cache")))  # doctest: +SKIP
        ... # SKIPPED: makes 50 live requests. Covered by tests/test_inat_pipeline.py
        ... # against recorded fixtures.
    """
    resolved: list[StatePlace] = []
    unresolved: list[str] = []

    for code, name in sorted(STATE_NAMES.items()):
        response = client.get("places_autocomplete", {"q": name})
        results = response.get("results")
        if not isinstance(results, list):
            unresolved.append(f"{code}: response had no results list")
            continue
        place_id = _select_state_place(results, name)
        if place_id is None:
            unresolved.append(f"{code}: no admin_level {_STATE_ADMIN_LEVEL} result named {name!r}")
            continue
        _log.info("resolved %s (%s) to place_id %d", code, name, place_id)
        resolved.append(StatePlace(code=code, name=name, place_id=place_id))

    if unresolved:
        message = "could not resolve every state: " + "; ".join(unresolved)
        raise InatError(message)

    table = PlaceTable(states=resolved)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(table.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return table
