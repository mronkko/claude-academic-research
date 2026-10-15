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


class NetworkAccessInUnitTest(BaseException):
    """A unit test tried to open a socket or resolve a host name.

    A `BaseException`, not an `Exception`: the first version derived from
    `RuntimeError`, and production code that catches broad exceptions —
    which network code does on purpose, to degrade gracefully — swallowed
    it. Three tests reached the network that way and passed. Deriving
    from `BaseException` gets past `except Exception`; recording every
    violation and failing at teardown (see `_no_network`) catches the
    rest, including a bare `except:`.
    """


#: Violations recorded during the current test. Read by `_no_network`'s
#: teardown; tests that provoke the guard on purpose clear it through the
#: `network_violations` fixture.
_VIOLATIONS_KEY = pytest.StashKey[list]()


@pytest.fixture
def network_violations(request: pytest.FixtureRequest) -> list[str]:
    """The current test's recorded violations, for tests of the guard
    itself: a test that expects a block clears this list once it has
    asserted on it, so teardown does not fail it."""
    return request.node.stash.setdefault(_VIOLATIONS_KEY, [])


@pytest.fixture(autouse=True)
def _no_network(request: pytest.FixtureRequest,
                monkeypatch: pytest.MonkeyPatch):
    """Fail any unit test that opens a real connection or resolves a name.

    Patches at the socket layer — `socket.socket.connect`/`connect_ex`/
    `sendto` and the resolver functions — the chokepoint every HTTP
    client in this project funnels through (httpx, urllib and requests
    alike), rather than at each client, so a new dependency cannot slip
    past it. Name resolution is blocked too: a DNS lookup is network
    traffic even when nothing connects afterwards, and it goes out over
    UDP from C code that never calls `socket.connect`.

    Every attempt is recorded, and the test fails at teardown if any
    were, so code that catches the raised error and carries on does not
    turn the guard into a no-op.

    `socket.socketpair` and AF_UNIX are left alone: they are local IPC,
    used by tooling rather than by code under test.

    `socketpair` needs an explicit bypass rather than merely being
    ignored. On POSIX it is AF_UNIX and never reaches this check, but
    Windows has no AF_UNIX, so CPython falls back to a real loopback
    TCP connect — and asyncio builds its event loop's self-pipe that way
    (`proactor_events._make_self_pipe`). Blocking it took out every
    async test on the Windows matrix while all six POSIX jobs passed,
    which is a good reminder that "works on my machine" and "works on
    the platform this guard is protecting" are different claims.

    A test that genuinely needs the network layer — probing a closed port
    to exercise an unreachable-host path, say — opts out with
    `@pytest.mark.allow_network`. Deliberately a marker rather than a
    loopback exemption: loopback is precisely what leaked here, since the
    call that escaped went to Zotero on 127.0.0.1.
    """
    if request.node.get_closest_marker("allow_network"):
        yield
        return
    violations: list[str] = request.node.stash.setdefault(_VIOLATIONS_KEY, [])
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_sendto = socket.socket.sendto
    real_socketpair = socket.socketpair
    # Set while CPython's own socketpair fallback runs, so the loopback
    # connect it makes internally is not mistaken for a test reaching out.
    building_socketpair = False

    def guarded_socketpair(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal building_socketpair
        building_socketpair = True
        try:
            return real_socketpair(*args, **kwargs)
        finally:
            building_socketpair = False

    def is_local_ipc(sock: socket.socket) -> bool:
        return building_socketpair or sock.family == getattr(socket, "AF_UNIX", object())

    def block(what: str) -> NetworkAccessInUnitTest:
        violations.append(what)
        return NetworkAccessInUnitTest(
            f"unit test attempted {what}. "
            f"Stub the client method the code under test calls — see "
            f"tests/unit/conftest.py's docstring for the failure this "
            f"prevents. If the test genuinely needs a live service, it "
            f"belongs in tests/live/."
        )

    def guarded_connect(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        if is_local_ipc(self):
            return real_connect(self, address, *args, **kwargs)
        raise block(f"a network connection to {address!r}")

    def guarded_connect_ex(self, address, *args, **kwargs):  # type: ignore[no-untyped-def]
        if is_local_ipc(self):
            return real_connect_ex(self, address, *args, **kwargs)
        raise block(f"a network connection (connect_ex) to {address!r}")

    def guarded_sendto(self, data, *args, **kwargs):  # type: ignore[no-untyped-def]
        if is_local_ipc(self):
            return real_sendto(self, data, *args, **kwargs)
        raise block(f"a datagram send (sendto) to {args[-1] if args else '?'!r}")

    def resolver(name: str):  # type: ignore[no-untyped-def]
        def guarded(host, *args, **kwargs):  # type: ignore[no-untyped-def]
            raise block(f"a name lookup ({name}) for {host!r}")
        return guarded

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)
    monkeypatch.setattr(socket.socket, "sendto", guarded_sendto)
    monkeypatch.setattr(socket, "socketpair", guarded_socketpair)
    for fn in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr"):
        monkeypatch.setattr(socket, fn, resolver(fn))
    yield
    if violations:
        pytest.fail(
            "unit test reached the network and the error was swallowed by "
            "the code under test (it never reached the test):\n  "
            + "\n  ".join(violations)
            + "\nStub the client call — see tests/unit/conftest.py.",
            pytrace=False,
        )
