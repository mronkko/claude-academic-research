"""`wizard._http_json` retries transient failures with backoff.

The wizard verifies every API key against its provider, and several of
those providers rate-limit. A single-attempt GET meant a 429 or a
momentary DNS blip during `/setup` was reported to the user as a bad
key, which is both wrong and hard to argue with — the user retypes a key
that was fine.

`scripts/setup/` cannot use `pipelines/http_client.py`: skills invoke
these scripts as `python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**` with no `uv`
and no venv, so `requests` / `urllib3` / `tenacity` are not importable.
Hence the hand-rolled stdlib policy these tests pin.
"""

from __future__ import annotations

import importlib.util
import urllib.error
from pathlib import Path

WIZARD = Path(__file__).resolve().parents[2] / "scripts" / "setup" / "wizard.py"


def _load():
    import sys

    spec = importlib.util.spec_from_file_location("wizard", WIZARD)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


def _http_error(code: str | int, headers: dict | None = None):
    return urllib.error.HTTPError(
        url="https://example.test", code=int(code), msg="boom",
        hdrs=headers or {}, fp=None,
    )


def _patch_urlopen(mod, monkeypatch, side_effects):
    """Feed `urlopen` a list of exceptions / responses; record sleeps."""
    calls = {"n": 0}
    slept: list[float] = []

    def fake_urlopen(*_a, **_kw):
        i = calls["n"]
        calls["n"] += 1
        effect = side_effects[min(i, len(side_effects) - 1)]
        if isinstance(effect, Exception):
            raise effect
        return effect

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(mod.time, "sleep", slept.append)
    return calls, slept


class _Response:
    """Minimal context-manager stand-in for an urlopen result."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def read(self):
        return self._body


# ---------------------------------------------------------------------------
# Which statuses retry
# ---------------------------------------------------------------------------


def test_429_is_retried_then_succeeds(monkeypatch) -> None:
    mod = _load()
    calls, slept = _patch_urlopen(
        mod, monkeypatch,
        [_http_error(429), _Response(200, b'{"ok": true}')],
    )
    status, data, err = mod._http_json("https://example.test")
    assert (status, data, err) == (200, {"ok": True}, "")
    assert calls["n"] == 2
    assert len(slept) == 1


def test_401_is_not_retried(monkeypatch) -> None:
    """An auth failure is an answer, not a blip.

    Retrying it would trip the provider's failed-auth throttling and
    make the user wait to be told their key is wrong.
    """
    mod = _load()
    calls, slept = _patch_urlopen(mod, monkeypatch, [_http_error(401)])
    status, _data, err = mod._http_json("https://example.test")
    assert status == 401
    assert "401" in err
    assert calls["n"] == 1
    assert slept == []


def test_transport_errors_are_retried_then_reported(monkeypatch) -> None:
    mod = _load()
    calls, slept = _patch_urlopen(
        mod, monkeypatch, [urllib.error.URLError("dns go boom")],
    )
    status, data, err = mod._http_json("https://example.test")
    assert (status, data) == (0, None)
    assert "boom" in err
    assert calls["n"] == mod._MAX_ATTEMPTS
    assert len(slept) == mod._MAX_ATTEMPTS - 1


def test_attempts_are_bounded(monkeypatch) -> None:
    """The point of the exercise: never an unbounded retry loop."""
    mod = _load()
    calls, _slept = _patch_urlopen(mod, monkeypatch, [_http_error(503)])
    status, _data, _err = mod._http_json("https://example.test")
    assert status == 503
    assert calls["n"] == mod._MAX_ATTEMPTS


def test_success_makes_exactly_one_request(monkeypatch) -> None:
    mod = _load()
    calls, slept = _patch_urlopen(
        mod, monkeypatch, [_Response(200, b'{"a": 1}')],
    )
    assert mod._http_json("https://example.test") == (200, {"a": 1}, "")
    assert calls["n"] == 1
    assert slept == []


def test_non_json_body_is_not_retried(monkeypatch) -> None:
    mod = _load()
    calls, _slept = _patch_urlopen(mod, monkeypatch, [_Response(200, b"<html>")])
    status, data, err = mod._http_json("https://example.test")
    assert (status, data, err) == (200, None, "non-JSON response")
    assert calls["n"] == 1


# ---------------------------------------------------------------------------
# Delay shape
# ---------------------------------------------------------------------------


def test_retry_after_header_wins_over_backoff(monkeypatch) -> None:
    mod = _load()
    _calls, slept = _patch_urlopen(
        mod, monkeypatch,
        [_http_error(429, {"Retry-After": "2"}), _Response(200, b"{}")],
    )
    mod._http_json("https://example.test")
    assert slept == [2.0]


def test_retry_after_is_capped(monkeypatch) -> None:
    """A provider asking for an hour must not hang an interactive wizard."""
    mod = _load()
    _calls, slept = _patch_urlopen(
        mod, monkeypatch,
        [_http_error(429, {"Retry-After": "3600"}), _Response(200, b"{}")],
    )
    mod._http_json("https://example.test")
    assert slept == [mod._MAX_DELAY_S]


def test_http_date_retry_after_falls_back_to_backoff() -> None:
    """Only delta-seconds is parsed; the HTTP-date form yields None."""
    mod = _load()
    assert mod._retry_after_seconds("Wed, 21 Oct 2026 07:28:00 GMT") is None
    assert mod._retry_after_seconds(None) is None
    assert mod._retry_after_seconds("5") == 5.0


def test_backoff_is_exponential_and_capped() -> None:
    mod = _load()
    # Full jitter: each delay is drawn from [0, ceiling], and the
    # ceiling doubles per attempt until it hits the cap.
    for attempt, ceiling in ((1, 1.0), (2, 2.0), (3, 4.0), (9, mod._MAX_DELAY_S)):
        samples = [mod._backoff_delay(attempt, None) for _ in range(200)]
        assert all(0.0 <= s <= ceiling for s in samples)
        assert max(samples) > ceiling * 0.5, "jitter should span the range"


def test_backoff_never_exceeds_the_cap() -> None:
    mod = _load()
    assert mod._backoff_delay(99, None) <= mod._MAX_DELAY_S
    assert mod._backoff_delay(1, 999.0) == mod._MAX_DELAY_S


# ---------------------------------------------------------------------------
# The stdlib-only constraint this policy exists to satisfy
# ---------------------------------------------------------------------------


def test_setup_scripts_import_only_the_stdlib() -> None:
    """`scripts/setup/` runs under bare `python3`, with no venv.

    The wizard's allow rule is
    `Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)`, so a third-party
    import here fails at skill-load time on a machine that has not run
    `uv sync`. That is why this backoff is hand-rolled instead of
    reusing `pipelines/http_client.py`.
    """
    import ast

    third_party = {"requests", "urllib3", "tenacity", "httpx", "pyzotero"}
    setup_dir = WIZARD.parent
    offenders: list[str] = []
    for path in sorted(setup_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [(node.module or "").split(".")[0]]
            else:
                continue
            for name in names:
                if name in third_party:
                    offenders.append(f"{path.name}:{node.lineno} imports {name}")
    assert not offenders, "\n  ".join(["stdlib-only rule broken:", *offenders])
