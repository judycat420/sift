"""Transcoding of downloaded photos into the bytes a pack ships.

WHY THIS MODULE EXISTS
----------------------
Downloaded photos are the wrong size, the wrong format, and carry metadata Sift
must not redistribute. All three are fixed here, in one place, deterministically.

WHY EXIF IS STRIPPED
--------------------
Not for size. A JPEG's EXIF block can carry the GPS coordinates of the camera
that took it, along with the device serial and the photographer's software. Sift
redistributes these images to strangers. iNaturalist obscures coordinates for
threatened taxa in its *API responses*, but that protection does not reach
metadata embedded in an image file, and an observer who set their observation to
obscured has not thereby stripped their camera's EXIF.

So Sift keeps none of it. Not "keeps orientation and drops GPS" — none. There is
no field in there Sift needs, and a selective filter is a list somebody has to
maintain correctly forever, where the failure mode is publishing somebody's home
address. The image is decoded to raw pixels and re-encoded from scratch, so the
output carries only what the encoder puts there.

Orientation is the one thing worth losing sleep over, and it is handled: EXIF
rotation is *applied* to the pixels before the metadata is discarded, so a photo
that displayed upright still does.

INVARIANT PROTECTED
-------------------
Identical input bytes produce byte-identical output, because the content-addressed
store depends on it: non-determinism would mean the same photo stored twice under
two hashes, and the cross-state dedupe this is all for would quietly stop working.
Nothing here embeds a timestamp, a filename, or any other run-specific value.
"""

from __future__ import annotations

import io
import logging
from dataclasses import dataclass

import PIL
from PIL import Image, ImageOps, UnidentifiedImageError

from sift_pack.imagestore import TranscodeProfile

__all__ = [
    "DEFAULT_PROFILE",
    "MAX_EDGE_PIXELS",
    "QUALITY",
    "TranscodeError",
    "Transcoded",
    "current_profile",
    "transcode",
]

_log = logging.getLogger(__name__)

MAX_EDGE_PIXELS = 500
"""Longest edge of the output.

A study card shows one plant on a phone. 500px is the point past which more
pixels stop teaching anything and start costing download time on a trailhead
with one bar of signal."""

QUALITY = 75
"""WebP quality. Above this the file grows faster than the photograph improves."""

IMAGE_FORMAT = "WEBP"


class TranscodeError(RuntimeError):
    """Raised when downloaded bytes cannot be turned into a usable image."""


def current_profile() -> TranscodeProfile:
    """The encoder profile this build produces.

    Returns:
        The profile, including the exact Pillow version.

    Example:
        >>> current_profile().max_edge
        500
    """
    return TranscodeProfile(
        max_edge=MAX_EDGE_PIXELS,
        quality=QUALITY,
        image_format=IMAGE_FORMAT,
        pillow_version=PIL.__version__,
    )


@dataclass(frozen=True, slots=True)
class Transcoded:
    """One transcoded image and the facts a manifest needs about it.

    Attributes:
        payload: The encoded bytes, exactly as they will be stored.
        width: Output pixel width.
        height: Output pixel height.
    """

    payload: bytes
    width: int
    height: int


def transcode(source: bytes) -> Transcoded:
    """Decode, resize, strip metadata, and re-encode one photo.

    Args:
        source: Downloaded image bytes, in any format Pillow can read.

    Returns:
        The encoded output and its dimensions.

    Raises:
        TranscodeError: If the bytes are not a decodable image, or decode to a
            zero-size image. Never returns a placeholder: a card with a broken
            image is worse than a taxon that was dropped and counted.

    Example:
        >>> from PIL import Image
        >>> import io
        >>> buffer = io.BytesIO()
        >>> Image.new("RGB", (1200, 800), (10, 120, 40)).save(buffer, format="JPEG")
        >>> result = transcode(buffer.getvalue())
        >>> (result.width, result.height)
        (500, 333)
        >>> transcode(buffer.getvalue()).payload == result.payload
        True
    """
    try:
        with Image.open(io.BytesIO(source)) as opened:
            # Apply EXIF rotation to the pixels, then never look at EXIF again.
            upright = ImageOps.exif_transpose(opened)
            # Drop the alpha channel: source photos are opaque, and an alpha
            # plane would encode differently across Pillow versions for no gain.
            converted = upright.convert("RGB")
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        message = f"downloaded bytes are not a decodable image: {exc}"
        raise TranscodeError(message) from exc

    if converted.width == 0 or converted.height == 0:  # pragma: no cover - defensive
        message = "image decoded to zero size"
        raise TranscodeError(message)

    converted.thumbnail((MAX_EDGE_PIXELS, MAX_EDGE_PIXELS), Image.Resampling.LANCZOS)

    # A fresh image the pixels are pasted into, so nothing from the source's
    # `info` dict — EXIF, ICC profile, comments — can ride along into the
    # output. `paste` copies pixel data and nothing else.
    stripped = Image.new("RGB", converted.size)
    stripped.paste(converted)

    buffer = io.BytesIO()
    stripped.save(buffer, format=IMAGE_FORMAT, quality=QUALITY, method=6)
    return Transcoded(payload=buffer.getvalue(), width=stripped.width, height=stripped.height)


DEFAULT_PROFILE = current_profile()
"""The profile in force for this process, resolved once at import."""
