"""Tests for the fetch lock: one guest on the API at a time."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest
from typer.testing import CliRunner

from sift_pack.cli import app
from sift_pack.lock import DEFAULT_LOCK_NAME, FetchLockError, LockInfo, fetch_lock

runner = CliRunner()


def _write_lock(directory: Path, pid: int) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / DEFAULT_LOCK_NAME
    path.write_text(
        json.dumps({"pid": pid, "started_at": "2026-08-07T12:00:00+00:00"}), encoding="utf-8"
    )
    return path


def _dead_pid() -> int:
    """A PID that is not running.

    Spawns nothing: PID 0 is never a normal process, and `os.kill(0, 0)` would
    signal our own process group, so a large unlikely PID is used and verified.
    """
    candidate = 4_000_000
    while True:
        try:
            os.kill(candidate, 0)
        except ProcessLookupError:
            return candidate
        except PermissionError:  # pragma: no cover - only on a very full PID table
            pass
        candidate -= 1


# --- the lock itself ----------------------------------------------------------


def test_lock_is_created_and_released(tmp_path: Path) -> None:
    with fetch_lock(tmp_path) as held:
        assert held.exists()
        assert json.loads(held.read_text(encoding="utf-8"))["pid"] == os.getpid()
    assert not (tmp_path / DEFAULT_LOCK_NAME).exists()


def _boom() -> None:
    message = "boom"
    raise RuntimeError(message)


def test_lock_is_released_even_when_the_body_raises(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="boom"), fetch_lock(tmp_path):
        _boom()
    assert not (tmp_path / DEFAULT_LOCK_NAME).exists()


def test_a_second_lock_is_refused_while_the_first_is_held(tmp_path: Path) -> None:
    with (
        fetch_lock(tmp_path),
        pytest.raises(FetchLockError, match="another fetch is already"),
        fetch_lock(tmp_path),
    ):
        pass  # pragma: no cover - the inner acquire must not succeed


def test_a_live_owner_is_reported_not_broken(tmp_path: Path) -> None:
    _write_lock(tmp_path, os.getpid())
    with (
        pytest.raises(FetchLockError, match="another fetch is already running"),
        fetch_lock(tmp_path),
    ):
        pass  # pragma: no cover


def test_the_refusal_explains_why_one_at_a_time(tmp_path: Path) -> None:
    _write_lock(tmp_path, os.getpid())
    with pytest.raises(FetchLockError, match="free public API"), fetch_lock(tmp_path):
        pass  # pragma: no cover


def test_a_stale_lock_is_reported_as_stale(tmp_path: Path) -> None:
    _write_lock(tmp_path, _dead_pid())
    with pytest.raises(FetchLockError, match="stale fetch lock"), fetch_lock(tmp_path):
        pass  # pragma: no cover


def test_a_stale_lock_suggests_force(tmp_path: Path) -> None:
    _write_lock(tmp_path, _dead_pid())
    with pytest.raises(FetchLockError, match="--force"), fetch_lock(tmp_path):
        pass  # pragma: no cover


def test_force_breaks_a_stale_lock(tmp_path: Path) -> None:
    _write_lock(tmp_path, _dead_pid())
    with fetch_lock(tmp_path, force=True) as held:
        assert json.loads(held.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_an_unreadable_lock_is_treated_as_held(tmp_path: Path) -> None:
    # More likely to mean "a fetch is writing it right now" than "nobody home".
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / DEFAULT_LOCK_NAME).write_text("{not json", encoding="utf-8")
    with pytest.raises(FetchLockError, match="could not be read"), fetch_lock(tmp_path):
        pass  # pragma: no cover


def test_lock_info_detects_a_live_process() -> None:
    assert LockInfo(pid=os.getpid(), started_at="now").is_running()


def test_lock_info_detects_a_dead_process() -> None:
    assert not LockInfo(pid=_dead_pid(), started_at="now").is_running()


# --- the CLI refuses, without making a request --------------------------------


def test_concurrent_fetch_exits_nonzero_without_touching_the_network(tmp_path: Path) -> None:
    # The cache dir is left empty and NOT marked as a Sift cache. If the refusal
    # were to happen after the client was built, the client would have created
    # and marked it — so an untouched directory proves the lock was checked
    # first. The conftest socket blocker covers the rest.
    work_dir = tmp_path / "work"
    cache_dir = tmp_path / "cache"
    _write_lock(work_dir, os.getpid())

    result = runner.invoke(
        app,
        [
            "fetch",
            "--domain",
            "plants",
            "--state",
            "MI",
            "--cache-dir",
            str(cache_dir),
            "--work-dir",
            str(work_dir),
        ],
    )
    assert result.exit_code == 7
    assert "another fetch is already running" in result.stderr
    assert not cache_dir.exists()
    assert not (work_dir / "candidates_MI.json").exists()


def test_a_stale_lock_blocks_the_cli_until_forced(tmp_path: Path) -> None:
    work_dir = tmp_path / "work"
    _write_lock(work_dir, _dead_pid())
    result = runner.invoke(
        app,
        [
            "fetch",
            "--domain",
            "plants",
            "--state",
            "MI",
            "--cache-dir",
            str(tmp_path / "cache"),
            "--work-dir",
            str(work_dir),
        ],
    )
    assert result.exit_code == 7
    assert "stale fetch lock" in result.stderr


def test_the_lock_is_taken_before_the_domain_is_even_resolved(tmp_path: Path) -> None:
    # Argument errors still beat the lock: an unknown domain is the user's typo,
    # and reporting a lock conflict instead would be actively misleading.
    work_dir = tmp_path / "work"
    _write_lock(work_dir, os.getpid())
    result = runner.invoke(
        app, ["fetch", "--domain", "birbs", "--state", "MI", "--work-dir", str(work_dir)]
    )
    assert result.exit_code == 2
    assert "unknown domain" in result.stderr
