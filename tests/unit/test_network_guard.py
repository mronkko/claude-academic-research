"""The no-network guard has to block tests without breaking the runtime.

It exists because 0.21.1 shipped a unit test that made a real request and
passed locally — a machine with Zotero running supplied the dependency
the test forgot to stub — then failed on all nine CI jobs.

The first version of the guard then broke every async test on Windows and
none anywhere else: Windows has no AF_UNIX, so CPython's `socketpair`
falls back to a real loopback connect, and asyncio builds its event
loop's self-pipe that way. These tests pin both halves so neither
regression can return quietly.
"""

from __future__ import annotations

import asyncio
import socket

import pytest

from tests.unit.conftest import NetworkAccessInUnitTest


def test_an_outbound_connection_is_blocked() -> None:
    """The behaviour the guard is for. Port 9 is the discard port, so if
    the guard ever stops working this fails by connecting rather than by
    hanging."""
    with pytest.raises(NetworkAccessInUnitTest) as exc:
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    assert "tests/live/" in str(exc.value), "the error should say where such a test belongs"


def test_loopback_is_blocked_like_anything_else() -> None:
    """The leak that motivated the guard went to Zotero on 127.0.0.1, so
    a blanket loopback exemption would have let it through unchanged."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessInUnitTest):
            sock.connect(("127.0.0.1", 23119))
    finally:
        sock.close()


def test_socketpair_still_works() -> None:
    """AF_UNIX on POSIX, a real loopback connect on Windows. Both must
    succeed — this is IPC the interpreter needs, not a test reaching out."""
    a, b = socket.socketpair()
    try:
        a.send(b"ping")
        assert b.recv(4) == b"ping"
    finally:
        a.close()
        b.close()


def test_an_event_loop_can_still_be_created() -> None:
    """The concrete Windows breakage: `asyncio.run` builds a self-pipe
    with `socket.socketpair()`, which the first guard rejected. Every
    async test in the suite depends on this working."""
    async def _work() -> str:
        await asyncio.sleep(0)
        return "ok"

    assert asyncio.run(_work()) == "ok"


@pytest.mark.allow_network
def test_the_marker_opts_out() -> None:
    """The escape hatch, exercised — otherwise it could rot and nobody
    would notice until a test that needs it started failing."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.settimeout(1)
    try:
        # Connecting to the discard port fails, but with a *socket* error,
        # which is the proof the guard stood aside.
        with pytest.raises(OSError) as exc:
            sock.connect(("127.0.0.1", 9))
        assert not isinstance(exc.value, NetworkAccessInUnitTest)
    finally:
        sock.close()
