"""Granting Zotero's local write key, and the two answers worth refusing.

Zotero 10 accepts writes on its local API once the user approves a
dialog. The wizard is the only place that asks: the grant is a modal, a
pipeline running unattended in a background lane cannot answer one, and
Zotero rate-limits the endpoint, so it must not sit anywhere that
retries.

Two of Zotero's own answers must not be stored.

**"Allow" (one-time)** returns a real key with `remember: false`. It is
spent by the first write, and every write after it fails with a
missing-key error. Storing it would hand the user a config entry that
works exactly once and then looks like a bug.

**A Zotero with no `Zotero-Server-ID` header** is too old for local
writes at all. The write endpoint rejects a request without that header
with 428, so a key obtained without one could never be used.

The wizard is stdlib-only by project rule (`scripts/setup/` is exempt
from the shared HTTP session), so this handshake is hand-rolled on
`urllib` rather than delegated to pyzotero.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

WIZARD = Path(__file__).resolve().parents[2] / "scripts" / "setup" / "wizard.py"


def _load():
    """Load the wizard by path — `scripts/setup/` is not on sys.path."""
    spec = importlib.util.spec_from_file_location("wizard", WIZARD)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def wizard():
    return _load()


class _Resp:
    def __init__(self, body: dict, headers: dict | None = None):
        self._body = json.dumps(body).encode()
        self.headers = headers or {}

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _urlopen(monkeypatch, handler):
    """The wizard calls `urllib.request.urlopen`; patch the module it holds."""
    monkeypatch.setattr(urllib.request, "urlopen", handler)


def test_always_allow_yields_a_storable_key(monkeypatch, wizard):
    def handler(req, timeout=None):
        if req.get_method() == "GET":
            return _Resp({}, {"Zotero-Server-ID": "SRV1"})
        assert req.headers["Zotero-server-id"] == "SRV1"
        assert json.loads(req.data)["appName"] == "academic-research"
        return _Resp({"key": "LOCALKEY", "remember": True})

    _urlopen(monkeypatch, handler)
    status, key, _ = wizard._authorize_zotero_local()
    assert (status, key) == ("ok", "LOCALKEY")


def test_a_one_time_allow_is_not_stored(monkeypatch, wizard):
    """`remember: false` is good for a single write; a stored one is a trap."""
    def handler(req, timeout=None):
        if req.get_method() == "GET":
            return _Resp({}, {"Zotero-Server-ID": "SRV1"})
        return _Resp({"key": "SPENT", "remember": False})

    _urlopen(monkeypatch, handler)
    status, key, message = wizard._authorize_zotero_local()
    assert status == "once_only"
    assert key == ""
    assert "Always Allow" in message


def test_a_zotero_without_a_server_id_header_is_reported_unsupported(monkeypatch, wizard):
    _urlopen(monkeypatch, lambda req, timeout=None: _Resp({}, {}))
    status, key, _ = wizard._authorize_zotero_local()
    assert (status, key) == ("unsupported", "")


@pytest.mark.parametrize(
    ("code", "expected"), [(403, "denied"), (429, "rate_limited"), (500, "error")],
)
def test_zoteros_refusals_are_named_not_collapsed(monkeypatch, wizard, code, expected):
    """A denial, a rate limit and a fault need different user advice."""
    def handler(req, timeout=None):
        if req.get_method() == "GET":
            return _Resp({}, {"Zotero-Server-ID": "SRV1"})
        raise urllib.error.HTTPError("u", code, "boom", {}, None)

    _urlopen(monkeypatch, handler)
    status, key, _ = wizard._authorize_zotero_local()
    assert (status, key) == (expected, "")


# --------------------------------------------------------------------
# The prompt around it
# --------------------------------------------------------------------

def test_a_non_interactive_run_never_prompts_and_keeps_the_key(monkeypatch, wizard):
    """`--non-interactive` re-runs must not drop a key already granted."""
    def _boom(*a, **k):
        raise AssertionError("a non-interactive run must not prompt")

    monkeypatch.setattr("builtins.input", _boom)
    assert wizard._prompt_zotero_local_writes(False, {}) == {}
    assert wizard._prompt_zotero_local_writes(
        False, {"zotero": {"local_api_key": "abc"}},
    ) == {"local_api_key": "abc"}


def test_an_unreachable_local_api_skips_the_grant(monkeypatch, wizard):
    """No point opening a dialog on a Zotero that is not running."""
    monkeypatch.setattr(
        wizard, "_check_zotero_local",
        lambda *a, **k: (wizard.ZOTERO_LOCAL_STATUS_NOT_RUNNING, "down"),
    )
    monkeypatch.setattr(
        wizard, "_authorize_zotero_local",
        lambda *a, **k: pytest.fail("must not ask an unreachable Zotero"),
    )
    assert wizard._prompt_zotero_local_writes(True, {}) == {}


def test_declining_the_prompt_leaves_writes_on_the_web_api(monkeypatch, wizard):
    monkeypatch.setattr(
        wizard, "_check_zotero_local",
        lambda *a, **k: (wizard.ZOTERO_LOCAL_STATUS_OK, "ok"),
    )
    monkeypatch.setattr(
        wizard, "_authorize_zotero_local",
        lambda *a, **k: pytest.fail("must not ask after the user declined"),
    )
    monkeypatch.setattr("builtins.input", lambda *a: "n")
    assert wizard._prompt_zotero_local_writes(True, {}) == {}
