"""`attach_pdf`'s return value has to say what actually happened.

The contract used to be "the new attachment key on success, None if the
file was already attached". The second half was wrong, and wrong in a way
that misreports library state.

`Zupload.upload()` creates the attachment *item* first, for every call,
in `_create_prelim` — a POST to /items with `parentItem` set. Only after
that does it ask for an upload authorisation, and only there can the
server answer `exists`, meaning it already holds those bytes. When it
does, pyzotero skips the byte transfer and files the entry under
`unchanged` (`_upload.py:299-302`) — but the new child attachment item
has already been created and is not removed.

So `unchanged` means "the bytes were already on the server", never "no
attachment was added". Returning None there told callers nothing had
happened while the parent item had in fact just gained a second
attachment. A downstream repair loop read those Nones as "already
attached, skip", reported 137 items swapped and 2 with no key, and both
halves were wrong: two items ended up holding two byte-identical PDFs
added 49 seconds apart.

A None that is load-bearing as "no action taken" has to mean only that.
Since an attachment item is created on every non-raising path, there is
no no-op to report, and the honest return is always a key.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import zotero_io


def _client() -> zotero_io.ZoteroClient:
    zc = zotero_io.ZoteroClient(api_key="fake-key", group_id="12345")
    cloud = MagicMock()
    cloud._attachment_template.return_value = {
        "itemType": "attachment", "linkMode": "imported_file",
        "title": "", "filename": "", "md5": None, "mtime": None,
    }
    zc._cloud = cloud
    return zc


def _patch_upload(monkeypatch, return_value):
    upload = MagicMock(return_value=return_value)
    monkeypatch.setattr(
        "pyzotero._upload.Zupload",
        MagicMock(return_value=MagicMock(upload=upload)),
    )
    return upload


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"0" * 2000 + b"\n%%EOF\n")
    return path


def test_returns_the_key_on_a_fresh_upload(monkeypatch, pdf) -> None:
    _patch_upload(monkeypatch, {
        "success": [{"key": "NEWATT01"}], "failure": [], "unchanged": [],
    })
    assert _client().attach_pdf("PARENT01", pdf) == "NEWATT01"


def test_returns_the_key_when_the_server_already_held_the_bytes(
    monkeypatch, pdf,
) -> None:
    """The `unchanged` bucket. An attachment item was still created, so a
    caller that reads this as "nothing happened" is being misinformed."""
    _patch_upload(monkeypatch, {
        "success": [], "failure": [], "unchanged": [{"key": "DUPEATT1"}],
    })
    assert _client().attach_pdf("PARENT01", pdf) == "DUPEATT1"


def test_never_returns_none_on_a_non_raising_path(monkeypatch, pdf) -> None:
    """The property the caller depends on: no silent no-op verdict."""
    for result in (
        {"success": [{"key": "K1"}], "failure": [], "unchanged": []},
        {"success": [], "failure": [], "unchanged": [{"key": "K2"}]},
    ):
        _patch_upload(monkeypatch, result)
        assert _client().attach_pdf("PARENT01", pdf) is not None


def test_raises_when_the_api_rejected_the_file(monkeypatch, pdf) -> None:
    _patch_upload(monkeypatch, {
        "success": [], "unchanged": [],
        "failure": [{"error": "Stored-file filename cannot contain a path"}],
    })
    with pytest.raises(RuntimeError, match="attach_pdf failed"):
        _client().attach_pdf("PARENT01", pdf)


def test_raises_when_the_result_names_no_attachment_at_all(
    monkeypatch, pdf,
) -> None:
    """An empty result is not a success with no key — it is an outcome
    nobody can act on, and guessing "fine" would re-create the very
    ambiguity this contract exists to remove."""
    _patch_upload(monkeypatch, {"success": [], "failure": [], "unchanged": []})
    with pytest.raises(RuntimeError):
        _client().attach_pdf("PARENT01", pdf)
