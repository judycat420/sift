"""Content-addressed store for transcoded image bytes.

WHY THIS MODULE EXISTS
----------------------
Michigan, Ohio and Indiana share most of their flora. Built naively, three packs
would hold three copies of the same milkweed photograph — and the duplication
would be invisible, because nothing about a file path says "this is the same
image you already have". Addressing bytes by their own hash makes the
duplication impossible instead of merely undesirable: two identical images
cannot occupy two paths.

The hash is taken over the **transcoded** output, not the downloaded original.
That is the byte sequence a pack actually ships, so it is the one worth
addressing. It also means the store is only meaningful relative to an encoder:
change the resize, the quality, or the Pillow version, and the same source photo
produces different bytes and a different address.

INVARIANT PROTECTED
-------------------
A file at `images/ab/abcd….webp` contains bytes whose SHA-256 is `abcd…`, and
was produced by the encoder profile recorded in the store's marker file. A store
opened under a different profile is reported, not silently mixed — a store
holding output from two encoders would return different bytes for the same
logical image depending on when each entry was written, and nothing downstream
could detect it.

Writes are append-only within a run. Nothing here deletes; `sift-pack gc` does
that, explicitly and never automatically.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "STORE_FORMAT",
    "ImageStore",
    "StoreError",
    "StoreProfileError",
    "TranscodeProfile",
    "sha256_of",
]

_log = logging.getLogger(__name__)

STORE_FORMAT = "sift-image-store"
"""Marker written into every store directory, so its format is self-describing."""

_FORMAT_MARKER = ".sift-store-format.json"
_SHARD_WIDTH = 2
"""Leading hex characters used as a subdirectory, so no directory holds 100k files."""


class StoreError(RuntimeError):
    """Base class for image-store failures."""


class StoreProfileError(StoreError):
    """Raised when a store was written by a different encoder profile."""


@dataclass(frozen=True, slots=True)
class TranscodeProfile:
    """The encoder settings a store's contents were produced with.

    Recorded because the hash is over transcoded bytes: any change here means
    the same source photo hashes differently, so every existing entry becomes
    unreachable rather than wrong. That has to be detectable. `pillow_version`
    is part of it at full precision — a patch release can change WebP output,
    and a store silently holding two encoders' output is worse than one that
    refuses to open.

    Attributes:
        max_edge: Longest edge in pixels after resize.
        quality: WebP quality setting.
        image_format: Pillow format name.
        pillow_version: Exact Pillow version that encoded the store.
    """

    max_edge: int
    quality: int
    image_format: str
    pillow_version: str

    def as_dict(self) -> dict[str, object]:
        """Render for the marker file and for a `SourceRef`.

        Returns:
            A JSON-serialisable description of the profile.

        Example:
            >>> TranscodeProfile(500, 75, "WEBP", "12.3.0").as_dict()["quality"]
            75
        """
        return {
            "max_edge": self.max_edge,
            "quality": self.quality,
            "image_format": self.image_format,
            "pillow_version": self.pillow_version,
        }

    def describe(self) -> str:
        """One-line summary for a provenance record.

        Returns:
            Human-readable encoder description.

        Example:
            >>> TranscodeProfile(500, 75, "WEBP", "12.3.0").describe()
            'WEBP q75 max-edge 500px, Pillow 12.3.0'
        """
        return (
            f"{self.image_format} q{self.quality} max-edge {self.max_edge}px, "
            f"Pillow {self.pillow_version}"
        )


def sha256_of(payload: bytes) -> str:
    """Content address for a byte string.

    Args:
        payload: The bytes to address.

    Returns:
        Lowercase hex SHA-256, matching `manifest.Sha256`.

    Example:
        >>> sha256_of(b"")[:16]
        'e3b0c44298fc1c14'
    """
    return hashlib.sha256(payload).hexdigest()


class ImageStore:
    """A directory of transcoded images, addressed by the hash of their bytes.

    Args:
        root: Directory holding the store. Created if absent.
        profile: Encoder profile the caller intends to write with.

    Raises:
        StoreProfileError: If `root` already holds images written by a different
            encoder profile.

    Example:
        >>> import tempfile
        >>> from pathlib import Path
        >>> profile = TranscodeProfile(500, 75, "WEBP", "12.3.0")
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     store = ImageStore(Path(tmp), profile)
        ...     digest = store.put(b"pretend webp bytes")
        ...     (store.has(digest), store.put(b"pretend webp bytes") == digest)
        (True, True)
    """

    def __init__(self, root: Path, profile: TranscodeProfile) -> None:
        """Open or create a store, checking its encoder profile."""
        self.root = root
        self.profile = profile
        self.root.mkdir(parents=True, exist_ok=True)
        self._check_profile()

    def _check_profile(self) -> None:
        """Verify the store's encoder profile, or claim an empty store."""
        marker = self.root / _FORMAT_MARKER
        if not marker.exists():
            self._write_marker(marker)
            return

        try:
            recorded = json.loads(marker.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            message = f"store marker at {marker} is unreadable: {exc}"
            raise StoreError(message) from exc

        if not isinstance(recorded, dict) or recorded.get("format") != STORE_FORMAT:
            message = f"{marker} does not describe a Sift image store"
            raise StoreError(message)

        stored = recorded.get("profile")
        if stored != self.profile.as_dict():
            message = (
                f"{self.root} was written by a different encoder: {stored!r}, but this "
                f"build encodes with {self.profile.as_dict()!r}. Hashes are taken over "
                "transcoded bytes, so the existing entries are unreachable under this "
                "encoder rather than merely stale. Remove the store and re-resolve, or "
                "pin the previous Pillow version."
            )
            raise StoreProfileError(message)

    def _write_marker(self, marker: Path) -> None:
        """Record the format and encoder profile this store holds."""
        marker.write_text(
            json.dumps({"format": STORE_FORMAT, "profile": self.profile.as_dict()}, indent=1),
            encoding="utf-8",
        )

    def path_for(self, digest: str) -> Path:
        """Where one digest's bytes live.

        Args:
            digest: Lowercase hex SHA-256.

        Returns:
            The path, sharded on the first two hex characters.

        Example:
            >>> import tempfile
            >>> from pathlib import Path
            >>> with tempfile.TemporaryDirectory() as tmp:
            ...     store = ImageStore(Path(tmp), TranscodeProfile(500, 75, "WEBP", "1.0"))
            ...     store.path_for("ab" + "0" * 62).relative_to(Path(tmp)).as_posix()
            'ab/ab00000000000000000000000000000000000000000000000000000000000000.webp'
        """
        return self.root / digest[:_SHARD_WIDTH] / f"{digest}.webp"

    def has(self, digest: str) -> bool:
        """Whether the store already holds these bytes.

        Args:
            digest: Lowercase hex SHA-256.

        Returns:
            True if present.

        Example:
            >>> import tempfile
            >>> from pathlib import Path
            >>> with tempfile.TemporaryDirectory() as tmp:
            ...     ImageStore(Path(tmp), TranscodeProfile(500, 75, "WEBP", "1.0")).has("0" * 64)
            False
        """
        return self.path_for(digest).exists()

    def put(self, payload: bytes) -> str:
        """Store bytes under their own hash, reusing an existing entry.

        Args:
            payload: Transcoded image bytes.

        Returns:
            The digest the bytes are addressed by.

        Example:
            >>> import tempfile
            >>> from pathlib import Path
            >>> with tempfile.TemporaryDirectory() as tmp:
            ...     store = ImageStore(Path(tmp), TranscodeProfile(500, 75, "WEBP", "1.0"))
            ...     store.put(b"abc") == sha256_of(b"abc")
            True
        """
        digest = sha256_of(payload)
        path = self.path_for(digest)
        if path.exists():
            return digest
        path.parent.mkdir(parents=True, exist_ok=True)
        # Write-then-rename: a killed process leaves a stray .tmp, never a
        # truncated file at an address claiming to hold the whole image.
        tmp = path.with_suffix(".webp.tmp")
        tmp.write_bytes(payload)
        tmp.replace(path)
        return digest

    def digests(self) -> Iterator[str]:
        """Every digest currently stored.

        Yields:
            Lowercase hex digests, in filesystem order.

        Example:
            >>> import tempfile
            >>> from pathlib import Path
            >>> with tempfile.TemporaryDirectory() as tmp:
            ...     store = ImageStore(Path(tmp), TranscodeProfile(500, 75, "WEBP", "1.0"))
            ...     _ = store.put(b"abc")
            ...     len(list(store.digests()))
            1
        """
        for path in self.root.glob(f"{'[0-9a-f]' * _SHARD_WIDTH}/*.webp"):
            yield path.stem

    def size_bytes(self) -> int:
        """Total bytes held.

        Returns:
            Sum of stored file sizes, excluding the marker.

        Example:
            >>> import tempfile
            >>> from pathlib import Path
            >>> with tempfile.TemporaryDirectory() as tmp:
            ...     store = ImageStore(Path(tmp), TranscodeProfile(500, 75, "WEBP", "1.0"))
            ...     _ = store.put(b"abc")
            ...     store.size_bytes()
            3
        """
        return sum(self.path_for(digest).stat().st_size for digest in self.digests())

    def collect(self, referenced: set[str]) -> tuple[int, int]:
        """Delete stored images nothing references.

        The only method here that removes anything, and it is never called
        automatically — an unreferenced image is usually a pool that has not been
        rebuilt yet, not garbage.

        Args:
            referenced: Digests that must be kept.

        Returns:
            How many entries were deleted, and how many bytes they held.

        Example:
            >>> import tempfile
            >>> from pathlib import Path
            >>> with tempfile.TemporaryDirectory() as tmp:
            ...     store = ImageStore(Path(tmp), TranscodeProfile(500, 75, "WEBP", "1.0"))
            ...     keep, drop = store.put(b"keep me"), store.put(b"bin me")
            ...     store.collect({keep})
            (1, 6)
        """
        removed = 0
        freed = 0
        for digest in list(self.digests()):
            if digest in referenced:
                continue
            path = self.path_for(digest)
            freed += path.stat().st_size
            path.unlink()
            removed += 1
            _log.info("gc: removed %s", digest)
        return removed, freed
