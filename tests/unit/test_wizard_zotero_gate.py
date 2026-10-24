"""The wizard's Zotero Desktop gate, and the write key it obtains.

The plugin reads and writes Zotero only through Desktop's local API, so
`/setup` starts with a local read test and a local write test (create a
note, delete it again) and goes no further unless both pass. The write
uses the stored key; when Zotero refuses it — none yet, or one revoked —
the wizard asks Zotero for a new one through its consent dialog, once
(Zotero rate-limits that endpoint), and tests again.

Two of Zotero's own answers to that request must not be stored.

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

from tests.unit.wizard_stubs import stub_zotero_gate

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
    def __init__(self, body: dict | None, headers: dict | None = None,
                 status: int = 200):
        self._body = json.dumps(body).encode() if body is not None else b""
        self.headers = headers or {}
        self.status = status

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
# _zotero_read_test / _zotero_write_test
# --------------------------------------------------------------------

def test_the_read_test_lists_the_personal_library(monkeypatch, wizard):
    seen = []

    def fake(url, *a, **k):
        seen.append(url)
        return 200, [], ""

    monkeypatch.setattr(wizard, "_http_json", fake)
    assert wizard._zotero_read_test()[0] is True
    assert seen == [wizard.ZOTERO_LOCAL_URL + "users/0/items?limit=1"]


def test_the_read_test_does_not_retry_a_closed_zotero(monkeypatch, wizard):
    """A refused connection on loopback means Zotero is closed; the gate
    says so at once instead of backing off first."""
    import urllib.error
    calls = []

    def refuse(*a, **k):
        calls.append(1)
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(wizard.urllib.request, "urlopen", refuse)
    monkeypatch.setattr(wizard.time, "sleep",
                        lambda s: pytest.fail("the read test backed off"))
    ok, message = wizard._zotero_read_test()
    assert not ok and "not reachable" in message
    assert calls == [1]


@pytest.mark.parametrize(("answer", "fragment"), [
    ((0, None, "Connection refused"), "not reachable"),
    ((403, None, "Forbidden"), "HTTP 403"),
])
def test_the_read_test_says_why_it_failed(monkeypatch, wizard, answer, fragment):
    monkeypatch.setattr(wizard, "_http_json", lambda *a, **k: answer)
    ok, message = wizard._zotero_read_test()
    assert not ok
    assert fragment in message


def test_the_write_test_deletes_what_it_created(monkeypatch, wizard):
    """A key that can only add would pass a create-only test; the
    pipeline also trashes and merges. And a test that leaves a note in
    the user's library is a test that writes junk on every re-run."""
    calls: list = []

    def handler(req, timeout=None):
        calls.append(req)
        if req.get_method() == "POST":
            return _Resp({"successful": {"0": {"key": "NOTE0001", "version": 5}}})
        return _Resp(None, status=204)

    _urlopen(monkeypatch, handler)
    status, _ = wizard._zotero_write_test("KEY", "SRV1")
    assert status == "ok"
    post, delete = calls
    assert post.full_url == wizard.ZOTERO_LOCAL_URL + "users/0/items"
    assert json.loads(post.data)[0]["itemType"] == "note"
    assert delete.get_method() == "DELETE"
    assert delete.full_url == wizard.ZOTERO_LOCAL_URL + "users/0/items/NOTE0001"
    assert delete.headers["If-unmodified-since-version"] == "5"
    for req in calls:
        assert req.headers["Zotero-api-key"] == "KEY"
        assert req.headers["Zotero-server-id"] == "SRV1"


def test_a_401_on_create_is_a_rejected_key(monkeypatch, wizard):
    calls: list = []

    def handler(req, timeout=None):
        calls.append(req.get_method())
        raise urllib.error.HTTPError("u", 401, "Invalid or expired API key", {}, None)

    _urlopen(monkeypatch, handler)
    assert wizard._zotero_write_test("OLD", "SRV1")[0] == "key_rejected"
    assert calls == ["POST"], "nothing was created, so nothing to delete"


def test_a_failed_delete_fails_the_test_and_names_the_note(monkeypatch, wizard):
    def handler(req, timeout=None):
        if req.get_method() == "POST":
            return _Resp({"successful": {"0": {"key": "NOTE0001", "version": 5}}})
        raise urllib.error.HTTPError("u", 412, "Precondition Failed", {}, None)

    _urlopen(monkeypatch, handler)
    status, message = wizard._zotero_write_test("KEY", "SRV1")
    assert status == "error"
    assert "NOTE0001" in message


# --------------------------------------------------------------------
# _zotero_local_gate
# --------------------------------------------------------------------

class _Gate:
    """Scripted read test, server ID, write tests and grant; records calls."""

    def __init__(self, monkeypatch, wizard, *, read=(True, "read"),
                 server_id="SRV1", writes=(("ok", "wrote"),),
                 grant=("ok", "NEWKEY", "granted")):
        self.writes = list(writes)
        self.write_calls: list[tuple[str, str]] = []
        self.grants = 0
        monkeypatch.setattr(wizard, "_zotero_read_test", lambda *a, **k: read)
        monkeypatch.setattr(wizard, "_zotero_local_server_id",
                            lambda *a, **k: server_id)

        def write(key, sid):
            self.write_calls.append((key, sid))
            return self.writes.pop(0)

        def authorize(*a, **k):
            self.grants += 1
            return grant

        monkeypatch.setattr(wizard, "_zotero_write_test", write)
        monkeypatch.setattr(wizard, "_authorize_zotero_local", authorize)


def _stored(key: str) -> dict:
    return {"zotero": {"local_api_key": key}}


def test_a_failed_read_stops_the_gate_before_any_write(monkeypatch, wizard):
    gate = _Gate(monkeypatch, wizard, read=(False, "Zotero is not reachable"))
    assert wizard._zotero_local_gate(True, _stored("K")) == (False, {})
    assert gate.write_calls == [] and gate.grants == 0


def test_a_working_stored_key_needs_no_dialog(monkeypatch, wizard):
    gate = _Gate(monkeypatch, wizard)
    assert wizard._zotero_local_gate(True, _stored("STORED")) == (
        True, {"local_api_key": "STORED"},
    )
    assert gate.write_calls == [("STORED", "SRV1")]
    assert gate.grants == 0


def test_the_environment_key_wins_over_config(monkeypatch, wizard):
    monkeypatch.setenv("ZOTERO_LOCAL_API_KEY", "FROMENV")
    gate = _Gate(monkeypatch, wizard)
    ok, entry = wizard._zotero_local_gate(False, _stored("STORED"))
    assert ok and entry == {"local_api_key": "FROMENV"}
    assert gate.write_calls == [("FROMENV", "SRV1")]


def test_a_rejected_key_asks_once_and_tests_again(monkeypatch, wizard):
    gate = _Gate(monkeypatch, wizard,
                 writes=[("key_rejected", "refused"), ("ok", "wrote")])
    assert wizard._zotero_local_gate(True, _stored("REVOKED")) == (
        True, {"local_api_key": "NEWKEY"},
    )
    assert gate.grants == 1
    assert gate.write_calls == [("REVOKED", "SRV1"), ("NEWKEY", "SRV1")]


def test_no_stored_key_goes_straight_to_the_dialog(monkeypatch, wizard):
    """A non-interactive run asks too: the dialog is in Zotero, not here."""
    gate = _Gate(monkeypatch, wizard)
    assert wizard._zotero_local_gate(False, {}) == (
        True, {"local_api_key": "NEWKEY"},
    )
    assert gate.grants == 1
    assert gate.write_calls == [("NEWKEY", "SRV1")]


@pytest.mark.parametrize("grant", [
    ("denied", "", "you chose Deny in Zotero"),
    ("once_only", "", "you chose Allow (one-time)"),
    ("rate_limited", "", "rate-limited"),
])
def test_a_refused_grant_stops_the_gate(monkeypatch, wizard, capsys, grant):
    gate = _Gate(monkeypatch, wizard, writes=[("key_rejected", "refused")],
                 grant=grant)
    assert wizard._zotero_local_gate(True, _stored("REVOKED")) == (False, {})
    assert gate.grants == 1
    assert gate.write_calls == [("REVOKED", "SRV1")], "no re-test without a key"
    assert grant[2] in capsys.readouterr().out


def test_a_new_key_that_still_fails_is_not_asked_for_again(monkeypatch, wizard):
    gate = _Gate(monkeypatch, wizard,
                 writes=[("key_rejected", "refused"), ("key_rejected", "refused")])
    assert wizard._zotero_local_gate(True, _stored("REVOKED")) == (False, {})
    assert gate.grants == 1


def test_a_write_error_with_a_good_key_stops_the_gate(monkeypatch, wizard):
    gate = _Gate(monkeypatch, wizard, writes=[("error", "HTTP 500")])
    assert wizard._zotero_local_gate(True, _stored("K")) == (False, {})
    assert gate.grants == 0


def test_a_zotero_too_old_for_local_writes_stops_the_gate(monkeypatch, wizard):
    gate = _Gate(monkeypatch, wizard, server_id="")
    assert wizard._zotero_local_gate(True, _stored("K")) == (False, {})
    assert gate.write_calls == [] and gate.grants == 0


# --------------------------------------------------------------------
# main(): the gate comes first and stops setup
# --------------------------------------------------------------------

def _run_main(wizard, monkeypatch, tmp_path):
    monkeypatch.setattr(wizard, "CONFIG_PATH", tmp_path / "config.toml")
    monkeypatch.setattr(sys, "argv", ["wizard.py", "--non-interactive", "--skip-verify"])
    return wizard.main()


def test_a_failed_read_stops_setup_with_exit_3_and_writes_nothing(
    monkeypatch, wizard, tmp_path, capsys,
):
    _Gate(monkeypatch, wizard, read=(False, "Zotero is not reachable"))
    monkeypatch.setattr(
        wizard, "_choose_provider",
        lambda *a, **k: pytest.fail("setup must stop before any other question"),
    )
    assert _run_main(wizard, monkeypatch, tmp_path) == 3
    assert not (tmp_path / "config.toml").exists()
    assert "Setup stopped" in capsys.readouterr().out


def test_a_refused_grant_stops_setup_and_keeps_the_old_config(
    monkeypatch, wizard, tmp_path,
):
    cfg = tmp_path / "config.toml"
    cfg.write_text('[zotero]\nlocal_api_key = "REVOKED"\n', encoding="utf-8")
    _Gate(monkeypatch, wizard, writes=[("key_rejected", "refused")],
          grant=("denied", "", "you chose Deny in Zotero"))
    assert _run_main(wizard, monkeypatch, tmp_path) == 3
    assert cfg.read_text(encoding="utf-8") == '[zotero]\nlocal_api_key = "REVOKED"\n'


def test_the_shared_stub_lets_main_past_the_gate(monkeypatch, wizard, tmp_path):
    """What every other `main()` test relies on: `wizard_stubs` makes the
    gate pass with no socket opened (the unit network guard would fail
    the test otherwise)."""
    stub_zotero_gate(wizard, monkeypatch)
    monkeypatch.setattr(wizard, "_check_zotero_cli", lambda: ("ok", "stub"))
    monkeypatch.setattr(wizard, "_check_mcp_servers", lambda: {})
    monkeypatch.setattr(wizard, "_agy_available", lambda: False)
    monkeypatch.setattr(wizard, "_maybe_add_claude_to_gitignore", lambda *a, **k: None)
    _run_main(wizard, monkeypatch, tmp_path)
    assert 'local_api_key = "k"' in (tmp_path / "config.toml").read_text(encoding="utf-8")
