"""Where a write goes, and which surface the read that feeds it comes from.

`ZoteroClient` has always read from Zotero Desktop's local API and
written through the Web API. That split is the cause of a whole family
of bugs — a script writes an item, reads it back locally, sees nothing
because Desktop has not synced, and reports "already done". It is why
`cloud_journal_articles()` exists at all.

Zotero 10.0.1 accepts writes on the local API after a one-time consent
dialog, with pyzotero >= 1.15.1 passing the resulting key as
`local_api_key`. So the split is no longer forced, and these tests fix
the rules for closing it.

Two of them are load-bearing.

**Read-for-write must use the write surface.** `update_abstract` reads
an item, takes its `version`, and sends that version back. Zotero
rejects a mismatched version with HTTP 412. Local and cloud version
counters are unrelated, so a read from one surface feeding a write to
the other 412s on every single call. Today `get_item` reads the cloud
precisely because writes go there; the rule generalises that rather
than replacing it, and it is what makes a per-operation fallback safe.

**Availability is never decided by comparing versions.** A downstream
project gated "prefer local" on the local API's `Last-Modified-Version`
matching the Web API's. On Zotero 9 that header carried the last synced
server version. Zotero 10 serves `clientVersion`, a per-transaction
local counter with no relation to Web API versions, so the comparison
can never pass: their checker returned "cannot determine" forever, fell
back to the metered surface on every call, and nothing said so — 46 of
67 live tests skipped on that gate and 286 abstracts were read over the
Web API before anyone noticed. Configuration decides the surface here.
A health probe would reintroduce exactly that silent-fallback shape.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from zotero_io import ZoteroClient


def _client(**kw) -> ZoteroClient:
    zc = ZoteroClient(api_key="k", group_id="123", **kw)
    zc._local = MagicMock(name="local")
    zc._cloud = MagicMock(name="cloud")
    return zc


# --------------------------------------------------------------------
# Which surface
# --------------------------------------------------------------------

def test_writes_go_to_the_cloud_when_no_local_key_is_configured():
    """The default is unchanged: no key, no local writes.

    Local writes need a key the user grants through Zotero's own dialog.
    Absent one, every existing install must behave exactly as before.
    """
    zc = _client()
    assert zc.local_writes_enabled is False
    assert zc._write_client() is zc._cloud


def test_writes_go_to_the_local_client_when_a_local_key_is_configured():
    zc = _client(local_api_key="local-secret")
    assert zc.local_writes_enabled is True
    assert zc._write_client() is zc._local


def test_remote_turns_off_local_writes_too():
    """`--remote` means "use the Web API", not "use it for reads only".

    A caller reaching for the flag knows the desktop client is behind or
    unreachable; honouring that for reads while still writing locally
    would put the two halves of a run on different surfaces.
    """
    zc = _client(local_api_key="local-secret", prefer_local=False)
    assert zc.local_writes_enabled is False
    assert zc._write_client() is zc._cloud


def test_the_local_client_is_built_with_the_local_api_key(monkeypatch):
    """pyzotero sends it as the `Zotero-API-Key` header on local writes."""
    seen = {}

    def _fake_zotero(lib_id, lib_type, api_key, **kwargs):
        seen.update(kwargs)
        return MagicMock()

    monkeypatch.setattr("zotero_io.zotero.Zotero", _fake_zotero)
    zc = ZoteroClient(api_key="k", group_id="123", local_api_key="local-secret")
    _ = zc.local
    assert seen.get("local") is True
    assert seen.get("local_api_key") == "local-secret"


# --------------------------------------------------------------------
# The 412 invariant
# --------------------------------------------------------------------

def test_read_for_write_uses_the_write_surface_not_the_read_surface():
    """`get_item` feeds a version back into a write, so it follows the write.

    Local and cloud version counters are unrelated. Reading local and
    writing cloud sends a version the cloud never issued, and Zotero
    answers 412 every time.
    """
    zc = _client(local_api_key="local-secret")
    zc.get_item("ABCD1234")
    zc._local.item.assert_called_once_with("ABCD1234")
    zc._cloud.item.assert_not_called()

    cloud_only = _client()
    cloud_only.get_item("ABCD1234")
    cloud_only._cloud.item.assert_called_once_with("ABCD1234")
    cloud_only._local.item.assert_not_called()


def test_update_abstract_reads_and_writes_on_one_surface():
    zc = _client(local_api_key="local-secret")
    zc._local.item.return_value = {"key": "ABCD1234", "version": 7}
    zc._local.update_item.return_value = True

    assert zc.update_abstract("ABCD1234", "new abstract") is True

    zc._local.item.assert_called_once_with("ABCD1234")
    payload = zc._local.update_item.call_args[0][0]
    assert payload["version"] == 7
    zc._cloud.update_item.assert_not_called()


def test_upsert_child_note_reads_children_from_the_write_surface():
    zc = _client(local_api_key="local-secret")
    zc._local.children.return_value = []
    zc._local.create_items.return_value = {"successful": {"0": {"key": "NOTE1"}}}

    zc.upsert_child_note("PARENT12", "<h1>marker</h1>", "<h1>marker</h1>body")

    zc._local.children.assert_called_once_with("PARENT12")
    zc._cloud.children.assert_not_called()


# --------------------------------------------------------------------
# What must never happen
# --------------------------------------------------------------------

def test_choosing_the_write_surface_never_compares_library_versions():
    """No version header, on either client, may decide the surface.

    This is the downstream failure this feature exists to avoid: a gate
    that compares the local API's `Last-Modified-Version` against the
    Web API's cannot pass on Zotero 10, so it silently routes every call
    to the metered surface forever.
    """
    zc = _client(local_api_key="local-secret")
    for client in (zc._local, zc._cloud):
        client.last_modified_version.side_effect = AssertionError(
            "the write surface must not be chosen by comparing versions"
        )

    assert zc._write_client() is zc._local
    zc._local.last_modified_version.assert_not_called()
    zc._cloud.last_modified_version.assert_not_called()


def test_attach_pdf_stays_on_the_cloud_client():
    """pyzotero's upload path is Web-API-only.

    `attachment_simple` / `attachment_both` do not exist locally and the
    3-step S3 handshake `Zupload` performs has no local equivalent in
    our code. It creates a CHILD item and never bumps the parent's
    version, so it does not collide with local metadata writes.
    """
    zc = _client(local_api_key="local-secret")
    assert zc._upload_client() is zc._cloud
