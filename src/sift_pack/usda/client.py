"""Cached, projected access to the USDA PLANTS services API.

WHY THIS MODULE EXISTS
----------------------
PLANTS has no bulk download that carries what Sift needs. Its documented CSV
endpoints now serve an Angular application shell rather than data, and the one
bulk JSON endpoint that still works (`characteristicSearchResults`) covers 2186
records — a subset holding only taxa with characteristics data, missing
`Alliaria petiolata`, `Trillium grandiflorum` and `Daucus carota` among others.
So the facts are gathered per taxon, from two endpoints, and cached.

Responses are projected before caching for the same reasons as the iNaturalist
cache (`docs/decisions.md`, 2026-08-07): a PLANTS profile is 16 KB of which Sift
reads about six fields, and a cache of projections is a record of what Sift
understood rather than of what the server happened to send.

INVARIANT PROTECTED
-------------------
Nothing here interprets. It fetches names and native-status codes and stores
them verbatim; deciding what a code means is `reconcile.py`'s job, and keeping
the two apart is what lets the matching rules be tested without a network and
audited without reading HTTP code.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

import httpx

__all__ = [
    "PLANTS_API",
    "PROJECTION_VERSION",
    "PlantsCacheMissError",
    "PlantsClient",
    "PlantsError",
    "PlantsRecord",
    "parse_plants_name",
]

_log = logging.getLogger(__name__)

PLANTS_API = "https://plantsservices.sc.egov.usda.gov/api/"
"""Base URL for the PLANTS services API.

Pinned because PLANTS' download URLs have moved repeatedly — `plants.usda.gov`,
`plants.sc.egov.usda.gov`, `/java/downloadData`, `/csvdownload` — and every one
of those still returns HTTP 200 with an HTML application shell, so a broken
endpoint looks like a successful fetch. See `docs/sources.md`.
"""

PROJECTION_VERSION = 1
"""Bump when the projections below change shape. Part of every cache key."""

Endpoint = Literal["search", "profile"]

_REQUEST_INTERVAL_SECONDS = 0.7
"""Minimum spacing between live requests.

PLANTS publishes no rate limit. Sift is a guest on a public agency's service, so
it paces itself at roughly one request a second — the same courtesy it extends
to iNaturalist, for the same reason.
"""

_USER_AGENT = "sift/0.1 (plant study-pack builder; +judycatstudios@gmail.com)"
_HTTP_OK = 200

_INFRASPECIFIC = re.compile(r"\b(ssp\.|subsp\.|var\.|f\.|forma|nothosubsp\.|nothovar\.)", re.I)
_FIRST_ITALIC = re.compile(r"<i>(.*?)</i>", re.S)
_TAGS = re.compile(r"<[^>]+>")


class PlantsError(RuntimeError):
    """Base class for failures reaching or reading PLANTS."""


class PlantsCacheMissError(PlantsError):
    """Raised when an offline client is asked for something it has not cached."""


@dataclass(frozen=True, slots=True)
class PlantsRecord:
    """One PLANTS taxon, reduced to what reconciliation reads.

    Attributes:
        symbol: PLANTS symbol, e.g. `"ASTU"`.
        scientific_name: Full name including authority, HTML stripped.
        binomial: The name without authority, as PLANTS italicises it.
        is_infraspecific: True for subspecies, varieties and forms.
        accepted_symbol: Symbol of the accepted taxon when this is a synonym.
        is_synonym: Whether PLANTS treats this record as a synonym.
    """

    symbol: str
    scientific_name: str
    binomial: str
    is_infraspecific: bool
    accepted_symbol: str | None
    is_synonym: bool


def parse_plants_name(html: str | None) -> tuple[str | None, bool]:
    """Split a PLANTS scientific name into its binomial and infraspecific flag.

    PLANTS italicises exactly the botanical name and leaves the authority in
    plain text: `<i>Asclepias tuberosa</i> L.` The first italic block is
    therefore the binomial, and this is the only reliable way to strip an
    authority — authorities contain spaces, initials, parentheses and the word
    `ex`, so counting tokens gets `(Michx.) Salisb.` wrong.

    Args:
        html: The `ScientificName` field as PLANTS returns it.

    Returns:
        The binomial (or `None` if the name has no italic block), and whether
        the full name carries a subspecies, variety or form marker.

    Example:
        >>> parse_plants_name("<i>Asclepias tuberosa</i> L.")
        ('Asclepias tuberosa', False)
        >>> parse_plants_name("<i>Asclepias tuberosa</i> L. ssp. <i>interior</i> Woodson")
        ('Asclepias tuberosa', True)
        >>> parse_plants_name(None)
        (None, False)
    """
    if not html:
        return None, False
    first = _FIRST_ITALIC.search(html)
    binomial = re.sub(r"\s+", " ", first.group(1)).strip() if first else None
    return binomial, bool(_INFRASPECIFIC.search(_TAGS.sub("", html)))


def _strip(html: str | None) -> str:
    """Plain text of an HTML-decorated name."""
    return re.sub(r"\s+", " ", _TAGS.sub("", html or "")).strip()


def _project_plant(plant: dict[str, Any]) -> dict[str, Any] | None:
    """Reduce one PLANTS taxon to the fields reconciliation reads."""
    symbol = plant.get("Symbol")
    if not isinstance(symbol, str) or not symbol:
        return None
    binomial, infraspecific = parse_plants_name(plant.get("ScientificName"))
    if binomial is None:
        return None
    accepted = plant.get("AcceptedSymbol")
    return {
        "symbol": symbol,
        "scientific_name": _strip(plant.get("ScientificName")),
        "binomial": binomial,
        "is_infraspecific": infraspecific,
        "accepted_symbol": accepted if isinstance(accepted, str) and accepted else None,
        "is_synonym": bool(plant.get("SynonymSymbol")) or bool(accepted and accepted != symbol),
    }


def _project_search(body: object) -> dict[str, Any]:
    """Keep the taxa a name search returned, and nothing else."""
    hits = body if isinstance(body, list) else []
    plants = []
    for hit in hits:
        if not isinstance(hit, dict):
            continue
        plant = hit.get("Plant")
        if isinstance(plant, dict):
            projected = _project_plant(plant)
            if projected is not None:
                plants.append(projected)
    return {"results": plants}


def _project_profile(body: object) -> dict[str, Any]:
    """Keep a profile's identity and its native-status codes."""
    if not isinstance(body, dict):
        return {"symbol": None, "native_statuses": []}
    statuses = body.get("NativeStatuses")
    projected: list[dict[str, Any]] = []
    if isinstance(statuses, list):
        projected.extend(
            {"region": entry["Region"], "status": entry.get("Status")}
            for entry in statuses
            if isinstance(entry, dict) and isinstance(entry.get("Region"), str)
        )
    return {
        "symbol": body.get("Symbol"),
        "scientific_name": _strip(body.get("ScientificName")),
        "native_statuses": projected,
    }


@dataclass(slots=True)
class PlantsStats:
    """What a PLANTS ingest cost.

    Attributes:
        hits: Requests served from the local cache.
        misses: Requests that went to the network.
    """

    hits: int = 0
    misses: int = 0

    def summary(self) -> str:
        """One-line summary.

        Returns:
            Human-readable counts.

        Example:
            >>> PlantsStats(hits=2, misses=1).summary()
            '3 requests: 2 cached, 1 fetched'
        """
        return f"{self.hits + self.misses} requests: {self.hits} cached, {self.misses} fetched"


class Transport(Protocol):
    """The seam between this module and the network."""

    def get_json(self, url: str) -> object:
        """Fetch and decode one URL.

        Args:
            url: Absolute URL.

        Returns:
            The decoded JSON body.

        Raises:
            PlantsError: On a transport or decode failure.
        """
        ...


class HttpxTransport:
    """Live transport with a descriptive User-Agent.

    Example:
        >>> HttpxTransport().get_json("https://example.invalid/")  # doctest: +SKIP
        ... # SKIPPED: live request; covered by tests/test_usda.py against a
        ... # recorded transport.
    """

    def __init__(self, sleeper: Callable[[float], None] = time.sleep) -> None:
        """Build a client and record how to pace requests."""
        self.client = httpx.Client(
            timeout=45.0,
            follow_redirects=True,
            headers={"User-Agent": _USER_AGENT},
        )
        self.sleeper = sleeper
        self._last = 0.0

    def get_json(self, url: str) -> object:
        """Fetch one URL, pacing requests and failing loudly on a non-200."""
        wait = _REQUEST_INTERVAL_SECONDS - (time.monotonic() - self._last)
        if wait > 0:
            self.sleeper(wait)
        try:
            response = self.client.get(url)
        except httpx.HTTPError as exc:
            message = f"transport failure for {url}: {exc}"
            raise PlantsError(message) from exc
        finally:
            self._last = time.monotonic()

        if response.status_code != _HTTP_OK:
            message = f"PLANTS returned HTTP {response.status_code} for {url}"
            raise PlantsError(message)
        content_type = response.headers.get("content-type", "")
        if "json" not in content_type:
            # PLANTS serves its Angular shell with status 200 for retired
            # endpoints, so a wrong URL looks exactly like a successful fetch.
            message = (
                f"PLANTS returned {content_type!r} for {url}, not JSON. This usually means the "
                "endpoint has moved and is serving the web application shell."
            )
            raise PlantsError(message)
        try:
            return response.json()
        except ValueError as exc:
            message = f"PLANTS returned undecodable JSON for {url}: {exc}"
            raise PlantsError(message) from exc


class PlantsClient:
    """Disk-cached client for the two PLANTS endpoints Sift uses.

    Args:
        cache_dir: Where projected responses are cached.
        transport: Network seam; tests pass a recorded one.
        offline: When true, a cache miss raises instead of fetching.

    Example:
        >>> import tempfile
        >>> from pathlib import Path
        >>> class Fake:
        ...     def get_json(self, url):
        ...         name = "<i>A. tuberosa</i> L."
        ...         return [{"Plant": {"Symbol": "ASTU", "ScientificName": name}}]
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     client = PlantsClient(Path(tmp), Fake())
        ...     client.search("Asclepias tuberosa")[0].symbol
        'ASTU'
    """

    def __init__(
        self,
        cache_dir: Path,
        transport: Transport | None = None,
        *,
        offline: bool = False,
    ) -> None:
        """Set up the cache directory and miss behaviour."""
        self.cache_dir = cache_dir
        self.transport = transport if transport is not None else HttpxTransport()
        self.offline = offline
        self.stats = PlantsStats()
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def _path(self, endpoint: Endpoint, key: str) -> Path:
        digest = hashlib.sha256(
            json.dumps(
                {"endpoint": endpoint, "key": key, "projection": PROJECTION_VERSION},
                sort_keys=True,
            ).encode()
        ).hexdigest()
        return self.cache_dir / endpoint / f"{digest}.json"

    def _cached(self, endpoint: Endpoint, key: str, url: str) -> dict[str, Any]:
        """Serve one request from cache, or fetch, project and cache it."""
        path = self._path(endpoint, key)
        if path.exists():
            self.stats.hits += 1
            try:
                envelope = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                message = f"cached PLANTS response at {path} could not be read: {exc}"
                raise PlantsError(message) from exc
            response = envelope.get("response")
            if not isinstance(response, dict):
                message = f"cached PLANTS response at {path} is not a Sift envelope"
                raise PlantsError(message)
            return response

        if self.offline:
            message = f"offline PLANTS client has no cached response for {endpoint} {key!r}"
            raise PlantsCacheMissError(message)

        _log.info("PLANTS cache miss: %s %s", endpoint, key)
        self.stats.misses += 1
        body = self.transport.get_json(url)
        projected = _project_search(body) if endpoint == "search" else _project_profile(body)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(
                {
                    "endpoint": endpoint,
                    "key": key,
                    "projection_version": PROJECTION_VERSION,
                    "fetched_at": datetime.now(UTC).isoformat(),
                    "response": projected,
                },
                indent=1,
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return projected

    def search(self, name: str) -> list[PlantsRecord]:
        """Look up every PLANTS taxon matching a name.

        PLANTS matches synonyms as well as accepted names, which is what makes
        tier-2 reconciliation possible without a separate synonym table.

        Args:
            name: Scientific name to search for.

        Returns:
            The matching records, accepted and synonym alike.

        Raises:
            PlantsError: If the response cannot be read.

        Example:
            >>> import tempfile
            >>> from pathlib import Path
            >>> class Fake:
            ...     def get_json(self, url):
            ...         return []
            >>> with tempfile.TemporaryDirectory() as tmp:
            ...     PlantsClient(Path(tmp), Fake()).search("Nothing at all")
            []
        """
        url = PLANTS_API + "PlantSearch?searchText=" + urllib.parse.quote(name)
        payload = self._cached("search", name, url)
        return [
            PlantsRecord(
                symbol=row["symbol"],
                scientific_name=row["scientific_name"],
                binomial=row["binomial"],
                is_infraspecific=bool(row["is_infraspecific"]),
                accepted_symbol=row["accepted_symbol"],
                is_synonym=bool(row["is_synonym"]),
            )
            for row in payload.get("results", [])
        ]

    def native_statuses(self, symbol: str) -> dict[str, str]:
        """Read a taxon's native-status codes by region.

        Args:
            symbol: PLANTS symbol of the accepted taxon.

        Returns:
            Region code to status code, e.g. `{"L48": "N", "CAN": "N"}`. Regions
            are PLANTS' own — `L48`, `CAN`, `AK`, `HI` and others. There is no
            per-state entry, because PLANTS does not record native status per
            state (`docs/decisions.md`, 2026-08-08).

        Raises:
            PlantsError: If the response cannot be read.

        Example:
            >>> import tempfile
            >>> from pathlib import Path
            >>> class Fake:
            ...     def get_json(self, url):
            ...         return {
            ...             "Symbol": "ASTU",
            ...             "NativeStatuses": [{"Region": "L48", "Status": "N"}],
            ...         }
            >>> with tempfile.TemporaryDirectory() as tmp:
            ...     PlantsClient(Path(tmp), Fake()).native_statuses("ASTU")
            {'L48': 'N'}
        """
        url = PLANTS_API + "PlantProfile?symbol=" + urllib.parse.quote(symbol)
        payload = self._cached("profile", symbol, url)
        return {
            entry["region"]: entry["status"]
            for entry in payload.get("native_statuses", [])
            if isinstance(entry.get("status"), str)
        }
