"""Zotero Desktop's local API: the write key, and what it tells us.

The plugin talks only to Zotero Desktop. Three things that used to come
from the Web API now come from Desktop itself, and these tests pin them:

1. **The write key.** Local writes need a key granted through Zotero's
   consent dialog. `_LocalZotero._write` asks for one on the local API's
   401 for a missing / revoked key, saves it, and resends once — and only
   once per client, because a key Zotero keeps revoking (or a one-time
   "Allow") would otherwise loop into the endpoint's rate limit.
2. **The zotero.org user ID** (`local_user_id`), which a relation URI in
   the personal library needs: the local API calls that library `0`.
3. **The groups listing** (`_list_accessible_groups`), which the group
   auto-pick reads.

The HTTP side is an `httpx2.MockTransport` behind a real pyzotero client,
as in test_batch_update_tags.py, so the headers and status handling are
pyzotero's own.
"""

from __future__ import annotations

import json

import httpx2
import pytest
import zotero_io

AUTHORIZE = "/api/local/authorize"
ITEM_URL = "http://127.0.0.1:23119/api/groups/12345/items/ABCD1234"


class FakeDesktop:
    """Just enough of Zotero Desktop's local API for the key path."""

    def __init__(self, *, valid_key: str = "NEWKEY",
                 grant: httpx2.Response | None = None):
        self.valid_key = valid_key
        self.grant = grant or httpx2.Response(
            201, json={"key": valid_key, "remember": True},
        )
        self.writes: list[httpx2.Request] = []
        self.authorizes: list[httpx2.Request] = []
        #: Overrides the answer to every write, when set.
        self.write_answer: httpx2.Response | None = None
        #: The 401 body for a write without a usable key.
        self.rejected_body = "API key required"

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        if request.url.path == AUTHORIZE:
            self.authorizes.append(request)
            return self.grant
        self.writes.append(request)
        if self.write_answer is not None:
            return self.write_answer
        if request.headers.get("zotero-api-key") != self.valid_key:
            return httpx2.Response(401, text=self.rejected_body)
        return httpx2.Response(204)


def _local(desktop: FakeDesktop, *, key: str | None = None,
           saved: list[str] | None = None) -> zotero_io._LocalZotero:
    sink = saved if saved is not None else []
    return zotero_io._LocalZotero(
        "12345", "group", None, local=True, local_api_key=key,
        server_id="fake-server", on_new_key=sink.append,
        client=httpx2.Client(transport=httpx2.MockTransport(desktop.handler)),
    )


def _patch(z: zotero_io._LocalZotero) -> httpx2.Response:
    return z._write("PATCH", url=ITEM_URL, json={"title": "x"})


# --------------------------------------------------------------------
# _LocalZotero._write — re-grant on a rejected key
# --------------------------------------------------------------------

@pytest.mark.parametrize(
    "body", ["API key required", "Invalid or expired API key"],
)
def test_a_rejected_key_is_regranted_saved_and_the_write_resent(body) -> None:
    desktop = FakeDesktop()
    desktop.rejected_body = body
    saved: list[str] = []
    z = _local(desktop, key="REVOKED", saved=saved)

    resp = _patch(z)

    assert resp.status_code == 204
    assert len(desktop.authorizes) == 1
    assert saved == ["NEWKEY"], "the new key must reach config"
    assert z.local_api_key == "NEWKEY"
    assert [r.headers.get("zotero-api-key") for r in desktop.writes] == [
        "REVOKED", "NEWKEY",
    ]
    # The resend is the same write.
    assert json.loads(desktop.writes[1].content) == {"title": "x"}


def test_no_configured_key_is_granted_on_the_first_write() -> None:
    desktop = FakeDesktop()
    saved: list[str] = []
    z = _local(desktop, key=None, saved=saved)

    assert _patch(z).status_code == 204
    assert saved == ["NEWKEY"]
    assert "zotero-api-key" not in desktop.writes[0].headers


def test_the_grant_asks_under_the_plugin_name_with_a_long_timeout() -> None:
    """The dialog waits for a person; pyzotero's 30 s default does not."""
    desktop = FakeDesktop()
    _patch(_local(desktop))

    (grant,) = desktop.authorizes
    assert grant.method == "POST"
    assert json.loads(grant.content) == {"appName": zotero_io.LOCAL_APP_NAME}
    assert grant.headers["zotero-server-id"] == "fake-server"
    assert grant.extensions["timeout"]["read"] == zotero_io._AUTHORIZE_TIMEOUT_S


def test_a_key_rejected_again_is_not_regranted_in_the_same_client() -> None:
    """A second dialog in one process would be a key Zotero keeps
    revoking, or a one-time "Allow"; looping hits the rate limit."""
    desktop = FakeDesktop(valid_key="NEVER-MATCHES")
    desktop.grant = httpx2.Response(201, json={"key": "NEWKEY", "remember": True})
    saved: list[str] = []
    z = _local(desktop, key="OLD", saved=saved)

    first = _patch(z)
    second = _patch(z)

    assert first.status_code == 401 and second.status_code == 401
    assert len(desktop.authorizes) == 1
    assert saved == ["NEWKEY"]
    # First write, its one resend, then the second write: no resend.
    assert len(desktop.writes) == 3


def test_a_401_that_is_not_about_the_key_passes_through() -> None:
    desktop = FakeDesktop()
    desktop.write_answer = httpx2.Response(401, text="Library not editable")
    saved: list[str] = []
    z = _local(desktop, key="NEWKEY", saved=saved)

    resp = _patch(z)

    assert resp.status_code == 401
    assert desktop.authorizes == [] and saved == []
    assert len(desktop.writes) == 1


@pytest.mark.parametrize("status", [200, 204, 403, 404, 412, 428, 500])
def test_other_statuses_pass_through_untouched(status) -> None:
    desktop = FakeDesktop()
    # Even a body that names the key: only a 401 means "no usable key".
    desktop.write_answer = httpx2.Response(status, text="API key required")
    saved: list[str] = []
    z = _local(desktop, key="NEWKEY", saved=saved)

    resp = _patch(z)

    assert resp.status_code == status
    assert desktop.authorizes == [] and saved == []
    assert len(desktop.writes) == 1


# --------------------------------------------------------------------
# _grant_key — what counts as a key worth storing
# --------------------------------------------------------------------

@pytest.mark.parametrize("grant, says", [
    (httpx2.Response(403, text="Denied"), "denied"),
    (httpx2.Response(429, text="Too many requests"), "rate-limiting"),
    (httpx2.Response(201, json={"key": "ONCE", "remember": False}), "Always Allow"),
    (httpx2.Response(201, json={"key": "", "remember": True}), "Always Allow"),
])
def test_a_refused_grant_raises_local_key_refused(grant, says) -> None:
    """Deny, the rate limit, and a one-time "Allow" (spent by the write it
    was asked for) all leave nothing to store: say what to do instead."""
    desktop = FakeDesktop(grant=grant)
    saved: list[str] = []
    z = _local(desktop, key=None, saved=saved)

    with pytest.raises(zotero_io.LocalKeyRefused, match=says):
        _patch(z)
    assert saved == [], "a refused grant must not be saved"
    assert len(desktop.writes) == 1, "no resend without a key"


def test_grant_key_returns_a_remembered_key() -> None:
    desktop = FakeDesktop(grant=httpx2.Response(
        201, json={"key": "  KEEP  ", "remember": True},
    ))
    assert _local(desktop)._grant_key() == "KEEP"


# --------------------------------------------------------------------
# _save_local_api_key — the default `on_new_key`
# --------------------------------------------------------------------

def test_the_default_saver_writes_the_key_to_config(monkeypatch) -> None:
    from core import config_writer

    calls = []
    monkeypatch.setattr(config_writer, "set_value",
                        lambda *a: calls.append(a))
    zotero_io._save_local_api_key("NEWKEY")
    assert calls == [("zotero", "local_api_key", "NEWKEY")]


def test_a_failure_to_save_is_logged_not_raised(monkeypatch, caplog) -> None:
    """The write that asked for the key can still go ahead with it."""
    from core import config_writer

    def boom(*_a):
        raise OSError("read-only config")

    monkeypatch.setattr(config_writer, "set_value", boom)
    caplog.set_level("WARNING")
    zotero_io._save_local_api_key("NEWKEY")
    assert any("Could not save" in r.message for r in caplog.records)


def test_the_client_uses_the_default_saver() -> None:
    z = zotero_io.ZoteroClient(group_id="12345").local
    assert z._on_new_key is zotero_io._save_local_api_key


# --------------------------------------------------------------------
# local_user_id — from the listing's `alternate` link
# --------------------------------------------------------------------

class _FakePersonal:
    def __init__(self, link: str | None = None, groups=None, error=None):
        headers = {"link": link} if link is not None else {}
        self.request = httpx2.Response(200, headers=headers)
        self._groups = groups or []
        self._error = error

    def items(self, **_kw):
        if self._error:
            raise self._error
        return []

    def groups(self):
        if self._error:
            raise self._error
        return self._groups

    def everything(self, page):
        return page


def test_local_user_id_reads_the_alternate_link(monkeypatch) -> None:
    link = ('<http://127.0.0.1:23119/api/users/0/items?start=1>; rel="next", '
            '<https://www.zotero.org/users/5591/items>; rel="alternate"')
    monkeypatch.setattr(zotero_io, "_personal_library",
                        lambda: _FakePersonal(link))
    assert zotero_io.local_user_id() == "5591"


def test_local_user_id_is_empty_without_an_alternate_link(monkeypatch) -> None:
    """Desktop never signed in to zotero.org has no such link."""
    monkeypatch.setattr(
        zotero_io, "_personal_library",
        lambda: _FakePersonal('<http://127.0.0.1:23119/api/users/0/items>; '
                              'rel="next"'),
    )
    assert zotero_io.local_user_id() == ""
    monkeypatch.setattr(zotero_io, "_personal_library", lambda: _FakePersonal())
    assert zotero_io.local_user_id() == ""


def test_local_user_id_is_empty_when_desktop_is_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(
        zotero_io, "_personal_library",
        lambda: _FakePersonal(error=httpx2.ConnectError("refused")),
    )
    assert zotero_io.local_user_id() == ""


def test_the_personal_library_is_users_zero_on_the_local_api() -> None:
    z = zotero_io._personal_library()
    assert z.local is True
    assert z.library_type == "users" and str(z.library_id) == "0"


# --------------------------------------------------------------------
# _list_accessible_groups — the local groups listing
# --------------------------------------------------------------------

def test_groups_are_mapped_to_id_and_name(monkeypatch) -> None:
    listing = [
        {"id": 111, "data": {"id": 111, "name": "Lab A"}},
        {"id": 222, "data": {"name": ""}},          # unnamed → placeholder
        {"data": {"name": "no id"}},                 # unusable → dropped
    ]
    monkeypatch.setattr(zotero_io, "_personal_library",
                        lambda: _FakePersonal(groups=listing))
    assert zotero_io._list_accessible_groups() == [
        {"id": 111, "name": "Lab A"},
        {"id": 222, "name": "group 222"},
    ]


def test_groups_listing_is_empty_when_desktop_is_unreachable(monkeypatch) -> None:
    monkeypatch.setattr(
        zotero_io, "_personal_library",
        lambda: _FakePersonal(error=httpx2.ConnectError("refused")),
    )
    assert zotero_io._list_accessible_groups() == []


def test_find_group_by_name_reads_the_local_listing(monkeypatch) -> None:
    monkeypatch.setattr(zotero_io, "_list_accessible_groups", lambda: [
        {"id": 1, "name": "A"}, {"id": 2, "name": "B"},
    ])
    assert zotero_io.find_group_by_name("B") == {"id": 2, "name": "B"}
    assert zotero_io.find_group_by_name("C") is None


def test_group_name_comes_from_the_local_listing(monkeypatch) -> None:
    monkeypatch.setattr(zotero_io, "_list_accessible_groups", lambda: [
        {"id": 12345, "name": "My group"},
    ])
    zc = zotero_io.ZoteroClient(group_id="12345")
    assert zc.group_name() == "My group"
    assert zc.describe_library() == "group 12345 ('My group')"
