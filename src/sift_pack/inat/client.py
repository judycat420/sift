"""Cached, rate-limited access to the iNaturalist API.

WHY THIS MODULE EXISTS
----------------------
A full fetch is hundreds of requests against a free, donation-funded API. Three
things follow, and this module is where all three are handled so that no caller
has to remember them:

1. **Rate limiting is not ours to invent.** `pyinaturalist` already implements
   iNaturalist's published guidance (~1 request/second, ~10k/day) in its
   default session. Hand-rolling a limiter on top would double-count delays and
   drift from upstream guidance as it changes. We use theirs and add a
   User-Agent that identifies Sift with a contact address, as the API terms ask.

2. **Re-running must be free.** The fetch is long enough that it will be
   interrupted, and iterating on the code downstream of it must not mean
   re-fetching. Every response is written to a content-addressed file under
   `cache/`, keyed by endpoint, sorted parameters and projection version, so a
   second run makes zero network calls and the pipeline resumes wherever it was
   killed.

3. **Cache misses are the cost.** A miss is a second of wall-clock and a request
   against someone else's budget, so every one is logged. A run that reports no
   misses did no network work; a run that reports thousands should be
   explicable.

4. **What is cached is the projection, not the raw body.** Storing whole
   responses cost 1.5 GB per state to answer four questions about them. The
   cache holds `projections.project()` output, which is what the parsers read;
   `--keep-raw` additionally writes untouched bodies to `cache/raw/` for
   debugging, and that directory can be deleted at any time without affecting
   correctness.

INVARIANT PROTECTED
-------------------
A cache hit performs no I/O beyond reading a local file — verified in the test
suite by the socket blocker rather than by inspection. The cache key is derived
from the endpoint, the full parameter set and `PROJECTION_VERSION`, so two
different queries can never collide onto one file, a query whose parameters
changed can never silently serve the old answer, and an entry written under a
different projection shape can never be read back under this one.

Caches written before M2.1 hold raw bodies under unversioned keys. They are
unreadable rather than misreadable — the keys do not match — and the client
detects and reports them on construction so the wasted gigabytes get pruned
instead of sitting there looking like a warm cache.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Literal, Protocol, get_args

import requests
from pyinaturalist.v1.observations import get_observation_species_counts, get_observations
from pyinaturalist.v1.places import get_places_autocomplete
from pyinaturalist.v1.taxa import get_taxa_by_id

from sift_pack.inat.projections import PROJECTION_VERSION, project

__all__ = [
    "CACHE_FORMAT",
    "INAT_THROTTLE_STATUS",
    "USER_AGENT",
    "CacheMissError",
    "Endpoint",
    "InatClient",
    "InatError",
    "LegacyCacheError",
    "PyinaturalistFetcher",
    "ThrottledError",
]

_log = logging.getLogger(__name__)

USER_AGENT = "sift/0.1 (plant study-pack builder; +https://github.com/judycat420/sift)"
"""Identifies Sift to the API, as iNaturalist's terms of service ask.

A contact route is included so that if Sift misbehaves, somebody can tell us
rather than silently blocking us.
"""

Endpoint = Literal[
    "species_counts",
    "observations",
    "taxa_by_id",
    "places_autocomplete",
]
"""Every iNaturalist endpoint Sift is allowed to call.

Closed on purpose: adding one is a deliberate act that comes with documenting
the source (STANDARDS.md rule 7), not something a caller does by passing a
different string.
"""

ParamValue = str | int | bool | Sequence[str] | Sequence[int]
Params = Mapping[str, ParamValue]
JsonDict = dict[str, Any]
"""A decoded JSON object. `Any` is unavoidable at the boundary; every consumer
in this package parses it into a pydantic model before use."""


class InatError(RuntimeError):
    """Base class for every failure originating in the iNaturalist ingest."""


class CacheMissError(InatError):
    """Raised when an offline client is asked for something it has not cached."""


class LegacyCacheError(InatError):
    """Raised when a cache directory predates the projected-cache format."""


class ThrottledError(InatError):
    """Raised when iNaturalist kept throttling us after every retry."""


INAT_THROTTLE_STATUS = 429
_TOO_MANY_REQUESTS = INAT_THROTTLE_STATUS

THROTTLE_RETRIES = 4
"""How many times a throttled request is retried before giving up.

`pyinaturalist` retries 500, 502, 503 and 504 but *not* 429, so without this a
single throttle response ends a 1200-request fetch. This is error handling, not
a second rate limiter: the request pacing stays entirely pyinaturalist's, and
this only decides what to do when the server says "slow down" anyway.
"""

THROTTLE_BACKOFF_SECONDS = 30.0
"""First backoff after a throttle, doubling each retry.

Starts near iNaturalist's per-minute window rather than at a token second: a
429 means the window is already exhausted, and retrying inside it just spends
another request telling us the same thing.
"""


CACHE_FORMAT = "sift-projected-cache"
"""Marker written into every cache directory, so its format is self-describing."""

_FORMAT_MARKER = ".sift-cache-format.json"


class Fetcher(Protocol):
    """The seam between this package and the network.

    Exists so the cache, the parsers and the orchestrator can all be tested
    against recorded fixtures with no HTTP library in the loop at all. This is
    a stronger guarantee than mocking transport: with a fake fetcher there is
    no socket to block.
    """

    def fetch(self, endpoint: Endpoint, params: Params) -> JsonDict:
        """Perform one live request.

        Args:
            endpoint: Which iNaturalist endpoint to call.
            params: Query parameters, already in the API's vocabulary.

        Returns:
            The decoded JSON response body.

        Raises:
            InatError: If the response cannot be used.
        """
        ...


def _retry_after(response: requests.Response, fallback: float) -> float:
    """How long to wait after a throttle, preferring the server's own advice."""
    header = response.headers.get("Retry-After")
    if header:
        try:
            return max(1.0, float(header))
        except ValueError:
            _log.warning("unparseable Retry-After header %r; using %.0fs", header, fallback)
    return fallback


def _as_json_dict(raw: object, endpoint: Endpoint) -> JsonDict:
    """Narrow an untyped response body to a JSON object, or fail loudly."""
    if not isinstance(raw, dict):
        message = f"{endpoint} returned {type(raw).__name__}, expected a JSON object"
        raise InatError(message)
    return raw


def _plain_json(value: object, path: str = "$") -> object:
    """Convert a response into values that survive a JSON round-trip unchanged.

    `pyinaturalist` helpfully parses timestamp strings into `datetime` objects
    before handing the body back. That would make a live result differ from the
    same result reloaded from cache — the caller would get `datetime` on a miss
    and `str` on a hit, and any bug that depended on the difference would only
    appear on the first run. Normalising here makes the two paths identical by
    construction.

    Raises:
        InatError: On a type with no obvious JSON form. Coercing it with `str()`
            would silently invent a representation; failing names the field.
    """
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(key): _plain_json(item, f"{path}.{key}") for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain_json(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    message = f"response field {path} has type {type(value).__name__}, which has no JSON form"
    raise InatError(message)


class PyinaturalistFetcher:
    """Live fetcher backed by `pyinaturalist`, with its default rate limiting.

    Example:
        >>> PyinaturalistFetcher().user_agent == USER_AGENT
        True
    """

    def __init__(
        self,
        user_agent: str = USER_AGENT,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        """Store the User-Agent sent with every request.

        Args:
            user_agent: Identifies Sift to the API.
            sleeper: How to wait between throttle retries. Injected so tests can
                exercise the backoff without actually sleeping.
        """
        self.user_agent = user_agent
        self.sleeper = sleeper
        self.throttled = 0

    def fetch(self, endpoint: Endpoint, params: Params) -> JsonDict:
        """Call one endpoint live.

        Args:
            endpoint: Which endpoint to call.
            params: Query parameters in the API's vocabulary. For `taxa_by_id`,
                `ids` carries the taxon IDs.

        Returns:
            The decoded JSON response body.

        Raises:
            InatError: If `endpoint` is not one Sift knows, or the body is not
                a JSON object.

        Example:
            >>> PyinaturalistFetcher().fetch("observations", {"taxon_id": 48662})
            ... # doctest: +SKIP
            ... # SKIPPED: performs a live request. Doctests run under the
            ... # conftest socket blocker (STANDARDS.md rule 6), and this
            ... # module's behaviour is covered by tests/test_inat_client.py
            ... # against recorded fixtures.
        """
        # pyinaturalist normalises multiple-choice parameters in place and only
        # handles `list`; a tuple reaches it as an unhandled type. Converting
        # here keeps callers free to pass any sequence, and keeps the cache key
        # (which already canonicalises sequences) independent of the choice.
        kwargs: dict[str, Any] = {
            key: list(value) if isinstance(value, (list, tuple)) else value
            for key, value in params.items()
        }
        kwargs["user_agent"] = self.user_agent

        # A mapping rather than an if/elif chain, so that an endpoint string
        # arriving from outside the Literal (from a cache envelope, say) is a
        # reported error rather than an unreachable branch mypy prunes away.
        return _as_json_dict(self._call_with_backoff(endpoint, kwargs), endpoint)

    def _call_with_backoff(self, endpoint: Endpoint, kwargs: dict[str, Any]) -> object:
        """Call the endpoint, waiting out throttles rather than dying on one.

        Raises:
            ThrottledError: If the API is still throttling after every retry.
                Nothing is lost: everything already fetched is cached, so a
                re-run resumes from where this stopped.
        """
        backoff = THROTTLE_BACKOFF_SECONDS
        for attempt in range(THROTTLE_RETRIES + 1):
            try:
                return self._call(endpoint, dict(kwargs))
            except requests.HTTPError as exc:
                response = exc.response
                if response is None or response.status_code != _TOO_MANY_REQUESTS:
                    raise
                if attempt == THROTTLE_RETRIES:
                    message = (
                        f"iNaturalist is throttling us: {endpoint} still returned 429 after "
                        f"{THROTTLE_RETRIES} retries. Everything fetched so far is cached, so "
                        "re-running resumes from here rather than starting over."
                    )
                    raise ThrottledError(message) from exc
                wait = _retry_after(response, backoff)
                self.throttled += 1
                _log.warning(
                    "throttled by iNaturalist on %s; waiting %.0fs before retry %d/%d",
                    endpoint,
                    wait,
                    attempt + 1,
                    THROTTLE_RETRIES,
                )
                self.sleeper(wait)
                backoff *= 2
        message = "unreachable: the retry loop always returns or raises"  # pragma: no cover
        raise InatError(message)  # pragma: no cover

    def _call(self, endpoint: Endpoint, kwargs: dict[str, Any]) -> object:
        """Dispatch one endpoint to its pyinaturalist function."""
        if endpoint == "taxa_by_id":
            # /taxa/{ids} takes its IDs positionally, unlike the others.
            ids = kwargs.pop("ids")
            return get_taxa_by_id(ids, **kwargs)

        routes = {
            "species_counts": get_observation_species_counts,
            "observations": get_observations,
            "places_autocomplete": get_places_autocomplete,
        }
        route = routes.get(endpoint)
        if route is None:
            message = f"no live route for endpoint {endpoint!r}"
            raise InatError(message)
        return route(**kwargs)


@dataclass(slots=True)
class CacheStats:
    """Running count of what a client actually cost.

    Attributes:
        hits: Requests served from disk.
        misses: Requests that went to the network.
        by_endpoint: Miss count per endpoint, so an expensive stage is visible.
    """

    hits: int = 0
    misses: int = 0
    by_endpoint: dict[str, int] = field(default_factory=dict)

    def summary(self) -> str:
        """One-line summary for a report.

        Returns:
            Human-readable counts.

        Example:
            >>> CacheStats(hits=3, misses=1, by_endpoint={"observations": 1}).summary()
            '4 requests: 3 cached, 1 fetched (observations=1)'
        """
        total = self.hits + self.misses
        detail = ", ".join(f"{name}={count}" for name, count in sorted(self.by_endpoint.items()))
        suffix = f" ({detail})" if detail else ""
        return f"{total} requests: {self.hits} cached, {self.misses} fetched{suffix}"


def cache_key(
    endpoint: Endpoint,
    params: Params,
    projection_version: int | None = PROJECTION_VERSION,
) -> str:
    """Derive the cache filename for one request.

    Parameters are sorted so that call sites written in different orders share a
    cache entry, and sequence values are rendered element-wise so that
    `["CC0", "CC-BY"]` and `"CC0,CC-BY"` do not collide onto one key while
    meaning different things to the API.

    Args:
        endpoint: The endpoint being called.
        params: The full parameter set.
        projection_version: Which projection shape the entry holds. `None` keys
            the raw body, which no projection applies to — that is what the
            `--keep-raw` debug copies use, so they survive a version bump.

    Returns:
        A 64-character hex digest.

    Example:
        >>> a = cache_key("observations", {"taxon_id": 1, "place_id": 2})
        >>> b = cache_key("observations", {"place_id": 2, "taxon_id": 1})
        >>> a == b
        True
        >>> a == cache_key("observations", {"taxon_id": 1, "place_id": 3})
        False
        >>> a == cache_key("observations", {"taxon_id": 1, "place_id": 2}, None)
        False
    """
    canonical = json.dumps(
        {
            "endpoint": endpoint,
            "params": _canonical_params(params),
            "projection_version": projection_version,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _canonical_params(params: Params) -> dict[str, object]:
    """Render parameters into a stable, JSON-serialisable form."""
    canonical: dict[str, object] = {}
    for key, value in params.items():
        if isinstance(value, (str, int, bool)):
            canonical[key] = value
        else:
            canonical[key] = list(value)
    return canonical


class InatClient:
    """Disk-cached, rate-limited iNaturalist client.

    Args:
        cache_dir: Directory for cached responses. Created if absent.
        fetcher: What to call on a miss. Defaults to the live pyinaturalist
            fetcher; tests pass a recorded one.
        offline: When true, a miss raises `CacheMissError` instead of fetching.
            Used to prove a second run costs nothing, and to run the pipeline
            with no network available.

    Example:
        >>> import tempfile
        >>> from pathlib import Path
        >>> class FakeFetcher:
        ...     calls = 0
        ...
        ...     def fetch(self, endpoint, params):
        ...         FakeFetcher.calls += 1
        ...         return {"results": [{"id": 1}]}
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     client = InatClient(Path(tmp), fetcher=FakeFetcher())
        ...     first = client.get("observations", {"taxon_id": 48662})
        ...     second = client.get("observations", {"taxon_id": 48662})
        ...     (first == second, FakeFetcher.calls, client.stats.hits)
        (True, 1, 1)
    """

    def __init__(
        self,
        cache_dir: Path,
        fetcher: Fetcher | None = None,
        *,
        offline: bool = False,
        keep_raw: bool = False,
    ) -> None:
        """Set up the cache directory, check its format, and set miss behaviour.

        Raises:
            LegacyCacheError: If `cache_dir` holds entries from before the
                projected-cache format. Those entries are unreadable rather than
                misreadable, but they are also gigabytes of dead weight, so this
                is reported loudly instead of silently re-fetching around them.
        """
        self.cache_dir = cache_dir
        self.fetcher = fetcher if fetcher is not None else PyinaturalistFetcher()
        self.offline = offline
        self.keep_raw = keep_raw
        self.stats = CacheStats()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self._check_cache_format()

    def _check_cache_format(self) -> None:
        """Verify the cache directory's format, or claim an empty one."""
        marker = self.cache_dir / _FORMAT_MARKER
        if marker.exists():
            try:
                recorded = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                message = f"cache format marker at {marker} is unreadable: {exc}"
                raise InatError(message) from exc
            if not isinstance(recorded, dict) or recorded.get("format") != CACHE_FORMAT:
                message = f"cache format marker at {marker} does not describe a Sift cache"
                raise InatError(message)
            stored = recorded.get("projection_version")
            if stored != PROJECTION_VERSION:
                # Not an error: keys carry the version, so old entries are
                # simply never read. Worth saying so, because they cost disk.
                _log.warning(
                    "cache %s holds projection version %s; this build uses %d. "
                    "Old entries are unreachable and prunable.",
                    self.cache_dir,
                    stored,
                    PROJECTION_VERSION,
                )
                self._write_format_marker(marker)
            return

        if self._has_cached_entries():
            message = (
                f"{self.cache_dir} holds cache entries in a pre-M2.1 format (raw response "
                "bodies under unversioned keys). They cannot be read by this build and are "
                f"pure disk cost. Remove the directory and re-fetch: rm -rf {self.cache_dir}"
            )
            raise LegacyCacheError(message)

        self._write_format_marker(marker)

    def _write_format_marker(self, marker: Path) -> None:
        """Record which format and projection version this directory holds."""
        marker.write_text(
            json.dumps({"format": CACHE_FORMAT, "projection_version": PROJECTION_VERSION}),
            encoding="utf-8",
        )

    def _has_cached_entries(self) -> bool:
        """True when the directory already holds per-endpoint cache files."""
        return any(
            (self.cache_dir / endpoint).is_dir() and any((self.cache_dir / endpoint).iterdir())
            for endpoint in get_args(Endpoint)
        )

    def _path_for(self, endpoint: Endpoint, params: Params) -> Path:
        """Where one request's projected response lives on disk."""
        return self.cache_dir / endpoint / f"{cache_key(endpoint, params)}.json"

    def _raw_path_for(self, endpoint: Endpoint, params: Params) -> Path:
        """Where the untouched body lives when `keep_raw` is set.

        Keyed without a projection version: the raw body does not change shape
        when a projection does, so debug copies survive a version bump.
        """
        return self.cache_dir / "raw" / endpoint / f"{cache_key(endpoint, params, None)}.json"

    def get(self, endpoint: Endpoint, params: Params) -> JsonDict:
        """Fetch one endpoint, from cache when possible.

        Args:
            endpoint: Which endpoint to call.
            params: Query parameters in the API's vocabulary.

        Returns:
            The decoded JSON response body, identical whether cached or live.

        Raises:
            CacheMissError: If the client is offline and the response is not
                cached.
            InatError: If the cached file is unreadable or the live response is
                unusable. A corrupt cache entry is reported, never silently
                re-fetched over — that would hide a bug in this module.

        Example:
            >>> import tempfile
            >>> from pathlib import Path
            >>> with tempfile.TemporaryDirectory() as tmp:
            ...     client = InatClient(Path(tmp), offline=True)
            ...     client.get("observations", {"taxon_id": 1})
            Traceback (most recent call last):
                ...
            sift_pack.inat.client.CacheMissError: ...
        """
        path = self._path_for(endpoint, params)
        if path.exists():
            self.stats.hits += 1
            return self._read_cached(path)

        if self.offline:
            message = (
                f"offline client has no cached response for {endpoint} "
                f"with {dict(params)!r} (expected {path})"
            )
            raise CacheMissError(message)

        _log.info("cache miss: %s %s", endpoint, dict(sorted(_canonical_params(params).items())))
        self.stats.misses += 1
        self.stats.by_endpoint[endpoint] = self.stats.by_endpoint.get(endpoint, 0) + 1

        # Normalise before projecting, so a hit and a miss are indistinguishable
        # to the caller, and project before caching, so the cache holds what the
        # parsers read rather than the API's whole worldview.
        raw = _as_json_dict(_plain_json(self.fetcher.fetch(endpoint, params)), endpoint)
        if self.keep_raw:
            self._write_raw(endpoint, params, raw)
        projected = project(endpoint, raw)
        self._write_cached(path, endpoint, params, projected)
        return projected

    def _write_raw(self, endpoint: Endpoint, params: Params, raw: JsonDict) -> None:
        """Keep an untouched copy for debugging. Never read back by the client."""
        path = self._raw_path_for(endpoint, params)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(raw, indent=1, sort_keys=True), encoding="utf-8")

    def _read_cached(self, path: Path) -> JsonDict:
        """Load one cached envelope, failing loudly if it is not readable."""
        try:
            envelope = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            message = f"cached response at {path} could not be read: {exc}"
            raise InatError(message) from exc

        if not isinstance(envelope, dict) or "response" not in envelope:
            message = f"cached response at {path} is not a Sift cache envelope"
            raise InatError(message)
        return _as_json_dict(envelope["response"], "observations")

    def _write_cached(
        self,
        path: Path,
        endpoint: Endpoint,
        params: Params,
        response: JsonDict,
    ) -> None:
        """Write one response, recording what produced it alongside the body.

        The envelope carries the endpoint, parameters and timestamp so a cache
        directory is auditable on its own — you can tell what a file is without
        reversing the hash.
        """
        envelope = {
            "endpoint": endpoint,
            "params": _canonical_params(params),
            "projection_version": PROJECTION_VERSION,
            "fetched_at": datetime.now(UTC).isoformat(),
            "response": response,
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(envelope, indent=1, sort_keys=True), encoding="utf-8")
        tmp.replace(path)
