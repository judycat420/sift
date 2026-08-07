"""Smoke tests for the skeleton: the package imports and the guardrails work."""

from __future__ import annotations

import re
import socket

import pytest

import sift_pack
from tests.conftest import NetworkAccessError


def test_package_exposes_version() -> None:
    assert sift_pack.__version__


def test_network_is_blocked_in_tests() -> None:
    expected = re.escape("STANDARDS.md rule 6")
    with pytest.raises(NetworkAccessError, match=expected), socket.socket() as sock:
        sock.connect(("example.com", 80))


def test_loopback_is_still_allowed() -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        with socket.socket() as client:
            client.connect(server.getsockname())
