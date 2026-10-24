"""The Connector merge re-parents in Zotero Desktop, through the local API.

2026-09-23: of 213 PDFs merged on the Web API, 26 were still under the
trashed Connector item in Desktop hours later; Desktop, still writing
the attachment's upload fields, never took the cloud's re-parent. The
plugin now talks only to Desktop, so every read and write of a merge —
re-parent, late-PDF guard, trash — goes through the one local client.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import zotero_io


def _item(key, *, parent=None, pdf=False, version=1, doi="10.1/x"):
    data = {"DOI": doi, "tags": [], "collections": []}
    if parent is not None:
        data = {"itemType": "attachment", "parentItem": parent,
                "contentType": "application/pdf" if pdf else "text/html",
                "filename": f"{key}.pdf", "md5": key, "url": ""}
    return {"key": key, "version": version, "data": data}


def _local_client(local) -> zotero_io.ZoteroClient:
    zc = zotero_io.ZoteroClient("1", local_api_key="LK")
    local.endpoint, local.library_type, local.library_id = "http://l", "groups", "1"
    local._write.return_value = MagicMock(status_code=204)
    zc._local = local
    return zc


def test_the_merge_moves_the_pdf_in_desktop() -> None:
    local = MagicMock()
    pdf = _item("P", parent="DUPE", pdf=True)
    items = {"KEEPER": _item("KEEPER"), "DUPE": _item("DUPE"), "P": pdf}
    local.item.side_effect = lambda k: items[k]
    local.children.side_effect = lambda k: [pdf] if k == "DUPE" else []

    stats = _local_client(local).merge_duplicate_item(
        "KEEPER", "DUPE", union_tags=False,
        child_content_types=("application/pdf",),
    )

    assert stats["moved_pdf_keys"] == ["P"]
    (moved,), _ = local.update_item.call_args
    assert moved["data"]["parentItem"] == "KEEPER"
    # The trash goes through Desktop too, with Desktop's own version.
    assert stats["trashed"] == ["DUPE"]
    headers = local._write.call_args.kwargs["headers"]
    assert headers["If-Unmodified-Since-Version"] == "1"


def test_the_late_pdf_guard_reads_desktop() -> None:
    """The guard re-lists the duplicate's children after the moves; a
    PDF the merge itself moved must not make it refuse to trash."""
    local = MagicMock()
    pdf = _item("P", parent="DUPE", pdf=True)
    items = {"KEEPER": _item("KEEPER"), "DUPE": _item("DUPE"), "P": pdf}
    local.item.side_effect = lambda k: items[k]
    calls = {"n": 0}

    def children(k):
        if k != "DUPE":
            return []
        calls["n"] += 1
        return [pdf] if calls["n"] == 1 else []
    local.children.side_effect = children

    stats = _local_client(local).merge_duplicate_item("KEEPER", "DUPE")
    assert stats["kept_unmoved_pdf"] is False
    assert stats["trashed"] == ["DUPE"]
