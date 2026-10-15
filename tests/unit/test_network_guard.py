"""The no-network guard has to block tests without breaking the runtime.

It exists because 0.21.1 shipped a unit test that made a real request and
passed locally — a machine with Zotero running supplied the dependency
the test forgot to stub — then failed on all nine CI jobs.

The first version of the guard then broke every async test on Windows and
none anywhere else: Windows has no AF_UNIX, so CPython's `socketpair`
falls back to a real loopback connect, and asyncio builds its event
loop's self-pipe that way. These tests pin both halves so neither
regression can return quietly.

The second version failed open: its error derived from `RuntimeError`,
production code that catches broad exceptions swallowed it, and three
tests reached the network and passed. The guard now raises a
`BaseException` *and* fails the test at teardown if any attempt was
recorded; it also blocks name resolution, `connect_ex` and `sendto`.
Tests that provoke the guard on purpose clear `network_violations` once
they have asserted on the error.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests.unit.conftest import NetworkAccessInUnitTest

CONFTEST = Path(__file__).with_name("conftest.py")


def test_an_outbound_connection_is_blocked(network_violations) -> None:
    """The behaviour the guard is for. Port 9 is the discard port, so if
    the guard ever stops working this fails by connecting rather than by
    hanging."""
    with pytest.raises(NetworkAccessInUnitTest) as exc:
        socket.create_connection(("127.0.0.1", 9), timeout=1)
    assert "tests/live/" in str(exc.value), "the error should say where such a test belongs"
    network_violations.clear()


def test_loopback_is_blocked_like_anything_else(network_violations) -> None:
    """The leak that motivated the guard went to Zotero on 127.0.0.1, so
    a blanket loopback exemption would have let it through unchanged."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessInUnitTest):
            sock.connect(("127.0.0.1", 23119))
    finally:
        sock.close()
    network_violations.clear()


def test_the_error_is_not_an_exception() -> None:
    """`except Exception` is how production code degrades gracefully on
    network failure, and how the guard used to be swallowed."""
    assert not issubclass(NetworkAccessInUnitTest, Exception)


@pytest.mark.parametrize("call", [
    lambda: socket.getaddrinfo("api.zotero.org", 443),
    lambda: socket.gethostbyname("api.zotero.org"),
    lambda: socket.gethostbyname_ex("api.zotero.org"),
], ids=["getaddrinfo", "gethostbyname", "gethostbyname_ex"])
def test_name_resolution_is_blocked(call, network_violations) -> None:
    """A DNS lookup is traffic too, and leaves over UDP from C code that
    never calls `socket.connect`."""
    with pytest.raises(NetworkAccessInUnitTest):
        call()
    network_violations.clear()


def test_connect_ex_is_blocked(network_violations) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        with pytest.raises(NetworkAccessInUnitTest):
            sock.connect_ex(("127.0.0.1", 9))
    finally:
        sock.close()
    network_violations.clear()


def test_udp_sendto_is_blocked(network_violations) -> None:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        with pytest.raises(NetworkAccessInUnitTest):
            sock.sendto(b"x", ("127.0.0.1", 9))
    finally:
        sock.close()
    network_violations.clear()


def _run_with_guard(tmp_path: Path, body: str) -> subprocess.CompletedProcess:
    """Run `body` as a one-test pytest session under a copy of the guard.

    A swallowed violation fails the test at *teardown*, which no test can
    observe about itself, so these run in a child pytest.
    """
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_case.py").write_text(textwrap.dedent(body), encoding="utf-8")
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "-o", "addopts=", "--rootdir", str(tmp_path), str(tmp_path / "test_case.py")],
        capture_output=True, text=True, cwd=tmp_path, timeout=120,
    )


@pytest.mark.parametrize("handler", ["except Exception:", "except BaseException:", "except:"])
def test_a_swallowed_violation_still_fails_the_test(tmp_path, handler) -> None:
    """The failure mode that let three tests reach the network and pass:
    the code under test caught the guard's error and carried on."""
    result = _run_with_guard(tmp_path, f"""
        import socket

        def fetch():
            try:
                socket.create_connection(("127.0.0.1", 9), timeout=1)
            {handler}  # noqa: E722
                return None

        def test_swallows():
            assert fetch() is None
    """)
    assert result.returncode != 0, result.stdout
    assert "swallowed" in result.stdout, result.stdout


def test_a_clean_test_passes_under_the_guard(tmp_path) -> None:
    """The child-pytest harness above is only evidence if a test that
    does not touch the network passes in it."""
    result = _run_with_guard(tmp_path, """
        def test_clean():
            assert True
    """)
    assert result.returncode == 0, result.stdout


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
