"""Test-suite guardrails.

WHY THIS MODULE EXISTS
----------------------
STANDARDS.md rule 6 says tests never touch the network. A rule that is only
written down gets broken by accident — a new dependency opens a socket during
import, a fixture falls through to a live call, and the suite silently starts
depending on the internet being up and on a third party's data not changing.

INVARIANT PROTECTED
-------------------
Any attempt to open a network socket during a test raises `NetworkAccessError`
immediately, naming the address that was dialled. Loopback is left alone so
local test servers still work. Mock outbound HTTP with `respx` against a
recorded fixture in `tests/fixtures/` instead.
"""

from __future__ import annotations

import socket
from typing import Any

import pytest

_ALLOWED_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", ""})


class NetworkAccessError(RuntimeError):
    """Raised when a test tries to open a non-loopback socket."""


def _is_loopback(address: Any) -> bool:  # noqa: ANN401 - socket addresses are untyped tuples
    """Return True for addresses the suite is allowed to reach."""
    if isinstance(address, tuple) and address:
        return str(address[0]) in _ALLOWED_HOSTS
    return False


@pytest.fixture(autouse=True)
def _block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that dials a non-loopback address.

    Autouse: this applies to every test in the suite without opt-in, because
    the standard it enforces is not optional.
    """
    real_connect = socket.socket.connect

    def guarded_connect(self: socket.socket, address: Any) -> None:  # noqa: ANN401 - matches socket API
        if not _is_loopback(address):
            message = (
                f"Test attempted a network connection to {address!r}. "
                "STANDARDS.md rule 6: mock it with respx against a fixture in tests/fixtures/."
            )
            raise NetworkAccessError(message)
        real_connect(self, address)

    # No teardown needed: monkeypatch restores the original attribute itself.
    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
