"""Unit tests must not touch the network.

This exists because of a defect that shipped green. `get_bbt_keys` was
changed to read Zotero's native `citationKey` before falling back to
Better BibTeX, which made it call `items_by_keys` — and one unit test
stubbed only the BBT call. On a development machine with Zotero running,
that unstubbed call reached the local API and the test passed. In CI,
with no Zotero, it failed with ECONNREFUSED on all nine matrix jobs.

The full suite passing locally was therefore not evidence of anything:
the environment supplied the dependency the test forgot to stub. Blocking
outbound connections here makes that impossible to repeat — a unit test
that reaches for a socket now fails on the machine of whoever wrote it,
which is the only place a fast failure is useful.

Live tests are unaffected: this fixture is scoped to `tests/unit/`, and
`tests/live/` opts into real services deliberately.
"""

from __future__ import annotations

import socket

import pytest


class NetworkAccessInUnitTest(RuntimeError):
    """A unit test tried to open a socket."""


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest,
                monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any unit test that opens a real connection.

    Patches at `socket.socket.connect`, the chokepoint every HTTP client
    in this project funnels through — httpx, urllib and requests alike —
    rather than at each client, so a new dependency cannot slip past it.

    `socket.socketpair` and AF_UNIX are left alone: they are local IPC,
    used by tooling rather than by code under test, and blocking them
    breaks pytest itself on some platforms.

    A test that genuinely needs the network layer — probing a closed port
    to exercise an unreachable-host path, say — opts out with
    `@pytest.mark.allow_network`. Deliberately a marker rather than a
    loopback exemption: loopback is precisely what leaked here, since the
    call that escaped went to Zotero on 127.0.0.1.
    """
    if request.node.get_closest_marker("allow_network"):
        return
    real_connect = socket.socket.connect

    def guarded(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        if self.family == getattr(socket, "AF_UNIX", object()):
            return real_connect(self, address, *args, **kwargs)
        raise NetworkAccessInUnitTest(
            f"unit test attempted a network connection to {address!r}. "
            f"Stub the client method the code under test calls — see this "
            f"file's docstring for the failure this prevents. If the test "
            f"genuinely needs a live service, it belongs in tests/live/."
        )

    monkeypatch.setattr(socket.socket, "connect", guarded)
