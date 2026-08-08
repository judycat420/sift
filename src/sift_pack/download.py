"""Concurrent retrieval of image bytes from the iNaturalist open-data bucket.

WHY THIS MODULE EXISTS
----------------------
Image bytes come from the S3 open-data bucket, not the API (`docs/decisions.md`,
2026-08-05), and the two have opposite characteristics. The API is a small,
donation-funded service where Sift paces itself to one request a second and holds
a lockfile so it is never two clients at once. The bucket exists to be read in
bulk — that is what "open data" means here — so the polite behaviour is different:
a modest concurrency limit, no artificial pacing, and no lock.

Eight concurrent downloads is enough to finish 2400 images in a couple of
minutes and small enough that Sift never looks like a scraper.

URLS ARE NEVER CONSTRUCTED
--------------------------
The API tells us where each photo lives, and that URL is carried on the
candidate. Sift only ever rewrites its *size segment* — `square.jpg` becomes
`medium.jpg` — which preserves the file extension the API reported. Rebuilding a
URL from a photo id would require guessing the extension, and guessing wrong
produces a 404 that looks exactly like a deleted photo: silent data loss, at an
unknown rate, discovered never.

INVARIANT PROTECTED
-------------------
Every download either yields bytes that are an image of the declared length, or
a `DownloadFailure` naming which of those it was not. There is no third outcome:
a truncated body, an HTML error page served with status 200, or a deleted photo
each produce a distinct, recorded reason rather than a short file that transcodes
into something unreadable later.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Iterator
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal, Protocol
from urllib.parse import urlparse, urlunparse

import httpx

__all__ = [
    "DEFAULT_CONCURRENCY",
    "IMAGE_SIZE",
    "DownloadFailure",
    "DownloadFailureReason",
    "DownloadOutcome",
    "Downloaded",
    "HttpxDownloader",
    "download_all",
    "sized_url",
]

_log = logging.getLogger(__name__)

DEFAULT_CONCURRENCY = 8
"""Simultaneous downloads. The bucket is built for bulk reads; this is polite
without being slow."""

IMAGE_SIZE = "medium"
"""Which iNaturalist size variant to fetch.

`medium` is 500px on its longest edge, which is exactly the transcode target —
so no pixels are downloaded that the encoder would immediately discard, and the
image is never upscaled. `original` would mean tens of megabytes per taxon for
output that ends up identical."""

_IMAGE_CONTENT_TYPES = ("image/",)
_TIMEOUT_SECONDS = 30.0


DownloadFailureReason = Literal[
    "photo_not_found",
    "photo_not_an_image",
    "photo_download_failed",
]
"""Every way one photo's retrieval can fail.

Typed rather than free-form so the resolve stage can put these straight into a
`ResolveDropRecord` without a cast: the two vocabularies are the same vocabulary,
and a cast is where they would drift apart unnoticed.
"""


class DownloadError(RuntimeError):
    """Base class for download failures that are not per-photo outcomes."""


def sized_url(url: str, size: str = IMAGE_SIZE) -> str:
    """Rewrite an iNaturalist photo URL to a different size variant.

    Only the final path segment's stem is replaced; the extension the API
    reported is preserved exactly, because extensions vary per photo and
    guessing one produces an indistinguishable 404.

    Args:
        url: The photo URL as the API reported it, e.g. `.../photos/1/square.jpg`.
        size: Variant to request.

    Returns:
        The same URL with its size segment replaced.

    Raises:
        DownloadError: If `url` does not have a `<stem>.<ext>` final segment.
            Rewriting something of an unexpected shape would be guessing.

    Example:
        >>> sized_url("https://host/photos/1/square.jpg")
        'https://host/photos/1/medium.jpg'
        >>> sized_url("https://host/photos/2/square.jpeg", "large")
        'https://host/photos/2/large.jpeg'
        >>> sized_url("https://host/photos/3/square")
        Traceback (most recent call last):
            ...
        sift_pack.download.DownloadError: ...
    """
    parsed = urlparse(url)
    path = PurePosixPath(parsed.path)
    if not path.suffix or not path.stem:
        message = f"photo URL {url!r} has no <stem>.<ext> final segment; refusing to rewrite it"
        raise DownloadError(message)
    return urlunparse(parsed._replace(path=str(path.with_name(f"{size}{path.suffix}"))))


@dataclass(frozen=True, slots=True)
class Downloaded:
    """Bytes successfully retrieved for one photo.

    Attributes:
        inat_photo_id: Which photo these bytes are.
        payload: The raw downloaded body.
    """

    inat_photo_id: int
    payload: bytes


@dataclass(frozen=True, slots=True)
class DownloadFailure:
    """One photo that could not be retrieved, and why.

    Attributes:
        inat_photo_id: Which photo failed.
        reason: Reason code, matching `resolved.ResolveDropReason`.
        detail: What specifically went wrong, with values.
    """

    inat_photo_id: int
    reason: DownloadFailureReason
    detail: str


DownloadOutcome = Downloaded | DownloadFailure


class Downloader(Protocol):
    """The seam between this package and the network.

    Exists so the resolve stage can be tested end to end with no HTTP library in
    the loop, exactly as `inat.client.Fetcher` does for the API.
    """

    def get(self, url: str) -> tuple[int, str, bytes]:
        """Retrieve one URL.

        Args:
            url: Absolute URL to fetch.

        Returns:
            Status code, content-type header, and body.

        Raises:
            DownloadError: On a transport failure.
        """
        ...


class HttpxDownloader:
    """Live downloader backed by a pooled `httpx.Client`.

    Args:
        client: An existing client, or `None` to build one.

    Example:
        >>> HttpxDownloader().get("https://example.invalid/x.jpg")  # doctest: +SKIP
        ... # SKIPPED: performs a live request. Doctests run under the conftest
        ... # socket blocker (STANDARDS.md rule 6); covered by
        ... # tests/test_resolve.py against a recorded downloader.
    """

    def __init__(self, client: httpx.Client | None = None) -> None:
        """Build or adopt a pooled HTTP client."""
        self.client = client or httpx.Client(
            timeout=_TIMEOUT_SECONDS,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=DEFAULT_CONCURRENCY),
            headers={"User-Agent": "sift/0.1 (plant study-pack builder)"},
        )

    def get(self, url: str) -> tuple[int, str, bytes]:
        """Retrieve one URL.

        Args:
            url: Absolute URL to fetch.

        Returns:
            Status code, content-type, and body.

        Raises:
            DownloadError: On a transport failure, wrapped so callers never see
                an httpx type.
        """
        try:
            response = self.client.get(url)
        except httpx.HTTPError as exc:
            message = f"transport failure for {url}: {exc}"
            raise DownloadError(message) from exc
        return response.status_code, response.headers.get("content-type", ""), response.content


_NOT_FOUND = 404
_OK = 200


def _classify(
    photo_id: int, url: str, status: int, content_type: str, body: bytes
) -> DownloadOutcome:
    """Turn one HTTP response into a usable payload or a named failure."""
    if status == _NOT_FOUND:
        return DownloadFailure(
            inat_photo_id=photo_id,
            reason="photo_not_found",
            detail=f"404 for {url}; the photo was deleted or made private since the fetch",
        )
    if status != _OK:
        return DownloadFailure(
            inat_photo_id=photo_id,
            reason="photo_download_failed",
            detail=f"HTTP {status} for {url}",
        )
    if not any(content_type.startswith(prefix) for prefix in _IMAGE_CONTENT_TYPES):
        # An error page served with status 200 would otherwise reach the encoder
        # and fail there, with a message about pixels rather than about HTTP.
        return DownloadFailure(
            inat_photo_id=photo_id,
            reason="photo_not_an_image",
            detail=f"content-type {content_type!r} for {url}",
        )
    if not body:
        return DownloadFailure(
            inat_photo_id=photo_id,
            reason="photo_download_failed",
            detail=f"empty body for {url}",
        )
    return Downloaded(inat_photo_id=photo_id, payload=body)


def download_one(downloader: Downloader, photo_id: int, url: str) -> DownloadOutcome:
    """Retrieve one photo, classifying every way it can fail.

    Args:
        downloader: The transport seam.
        photo_id: Photo being retrieved, for the record.
        url: Where to retrieve it from, already size-rewritten.

    Returns:
        The bytes, or a failure naming what went wrong.

    Example:
        >>> class Fake:
        ...     def get(self, url):
        ...         return 404, "text/html", b"gone"
        >>> download_one(Fake(), 1, "https://host/photos/1/medium.jpg").reason
        'photo_not_found'
    """
    try:
        status, content_type, body = downloader.get(url)
    except DownloadError as exc:
        return DownloadFailure(
            inat_photo_id=photo_id,
            reason="photo_download_failed",
            detail=str(exc),
        )
    return _classify(photo_id, url, status, content_type, body)


def download_all(
    downloader: Downloader,
    requests: Iterable[tuple[int, str]],
    concurrency: int = DEFAULT_CONCURRENCY,
) -> Iterator[DownloadOutcome]:
    """Retrieve many photos concurrently.

    Args:
        downloader: The transport seam.
        requests: `(photo_id, url)` pairs. URLs must already be size-rewritten.
        concurrency: Simultaneous downloads.

    Yields:
        One outcome per request, in completion order.

    Example:
        >>> class Fake:
        ...     def get(self, url):
        ...         return 200, "image/jpeg", b"bytes"
        >>> outcomes = list(download_all(Fake(), [(1, "https://host/a.jpg")]))
        >>> outcomes[0].payload
        b'bytes'
    """
    pending = list(requests)
    if not pending:
        return
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        yield from pool.map(lambda item: download_one(downloader, item[0], item[1]), pending)
