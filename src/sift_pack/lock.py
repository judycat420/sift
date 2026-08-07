"""Exclusive lock around the fetch, so Sift is never two guests at once.

WHY THIS MODULE EXISTS
----------------------
Sift is a guest on a free, donation-funded public API. Its rate limiting comes
from `pyinaturalist` and is per-process, so two concurrent fetches quietly
double the request rate against someone else's infrastructure — and running two
at once is not a hypothetical: it is what happens when a long fetch appears to
have stalled and somebody starts another in a second terminal. That is the
behaviour that gets a User-Agent blocked, and it would be blocked for every Sift
user at once, not just the one who did it.

STANDARDS.md rule 6 set the precedent: a rule that matters gets a mechanism, not
a convention. The socket blocker makes "tests never touch the network"
unbreakable-by-accident; this lock does the same for "one fetch at a time".

INVARIANT PROTECTED
-------------------
At most one fetch process holds `work/.fetch.lock` at a time, and the lock is
acquired before any network call — so a rejected second fetch has cost the API
nothing. The lock records its owner's PID and start time, so a stale lock left
by a killed process is distinguishable from a live one and is reported as such
rather than either blocking forever or being silently stolen.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

__all__ = ["DEFAULT_LOCK_NAME", "FetchLockError", "LockInfo", "fetch_lock"]

_log = logging.getLogger(__name__)

DEFAULT_LOCK_NAME = ".fetch.lock"


class FetchLockError(RuntimeError):
    """Raised when the fetch lock cannot be acquired."""


class LockInfo:
    """Who holds a lock, and since when.

    Example:
        >>> LockInfo(pid=1234, started_at="2026-08-07T12:00:00+00:00").pid
        1234
    """

    def __init__(self, pid: int, started_at: str) -> None:
        """Record the owning process and its start time."""
        self.pid = pid
        self.started_at = started_at

    def is_running(self) -> bool:
        """Whether the owning process still exists.

        Signal 0 performs the permission and existence checks without delivering
        anything. A `PermissionError` means the process exists but belongs to
        another user, which still counts as running.

        Returns:
            True if the PID is live.

        Example:
            >>> LockInfo(pid=os.getpid(), started_at="now").is_running()
            True
        """
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True


def _read_lock(path: Path) -> LockInfo | None:
    """Read an existing lock file, or `None` if it is unreadable.

    An unreadable lock is treated as held rather than absent by the caller: a
    corrupt lock file is more likely to mean "a fetch is writing it right now"
    than "nobody is here".
    """
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    pid = payload.get("pid")
    started_at = payload.get("started_at")
    if not isinstance(pid, int) or not isinstance(started_at, str):
        return None
    return LockInfo(pid=pid, started_at=started_at)


@contextmanager
def fetch_lock(directory: Path, *, force: bool = False) -> Iterator[Path]:
    """Hold an exclusive fetch lock for the duration of the block.

    Args:
        directory: Where the lock lives, normally the work directory.
        force: Break an existing lock. Intended for a lock left behind by a
            killed process; it will also break a live one, which is why the
            error message asks for it only when the owner is confirmed dead.

    Yields:
        The path of the lock file being held.

    Raises:
        FetchLockError: If another fetch holds the lock, or a stale lock exists
            and `force` was not given. Raised before any network call, so a
            refused fetch costs the API nothing.

    Example:
        >>> import tempfile
        >>> from pathlib import Path
        >>> with tempfile.TemporaryDirectory() as tmp:
        ...     with fetch_lock(Path(tmp)) as held:
        ...         held.exists()
        True
    """
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / DEFAULT_LOCK_NAME

    if force and path.exists():
        _log.warning("breaking existing fetch lock at %s (--force)", path)
        path.unlink(missing_ok=True)

    try:
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError as exc:
        raise _describe_conflict(path) from exc

    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(
                {"pid": os.getpid(), "started_at": datetime.now(UTC).isoformat()},
                handle,
            )
        yield path
    finally:
        path.unlink(missing_ok=True)


def _describe_conflict(path: Path) -> FetchLockError:
    """Build the error for a lock we could not take, naming who holds it."""
    existing = _read_lock(path)
    if existing is None:
        return FetchLockError(
            f"a fetch lock exists at {path} but could not be read. Another fetch may be "
            f"starting right now. If you are certain none is running, remove it: rm {path}"
        )
    if existing.is_running():
        return FetchLockError(
            f"another fetch is already running (pid {existing.pid}, started "
            f"{existing.started_at}). Sift holds one lock at a time because concurrent "
            "fetches double our request rate against a free public API. Wait for it to "
            "finish — a re-run is cheap, since everything it fetched is cached."
        )
    return FetchLockError(
        f"a stale fetch lock at {path} names pid {existing.pid} (started "
        f"{existing.started_at}), which is no longer running. If no fetch is in progress, "
        "re-run with --force to break it."
    )
