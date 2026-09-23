"""The Connector merge re-parents in Zotero Desktop, not on the cloud.

2026-09-23: of 213 PDFs merged on the Web API, 26 were still under the
trashed Connector item in Desktop hours later; Desktop, still writing
the attachment's upload fields, never took the cloud's re-parent.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import reconcile_merged_pdfs as rmp
import zotero_io


def _item(key, *, parent=None, pdf=False, version=1, doi="10.1/x"):
    data = {"DOI": doi, "tags": [], "collections": []}
    if parent is not None:
        data = {"itemType": "attachment", "parentItem": parent,
                "contentType": "application/pdf" if pdf else "text/html",
                "filename": f"{key}.pdf", "md5": key, "url": ""}
    return {"key": key, "version": version, "data": data}


def _local_client(local, cloud) -> zotero_io.ZoteroClient:
    zc = zotero_io.ZoteroClient(api_key="k", group_id="1", local_api_key="LK")
    zc._local, zc._cloud = local, cloud
    return zc


def test_with_a_local_key_the_merge_moves_the_pdf_in_desktop() -> None:
    local, cloud = MagicMock(), MagicMock()
    pdf = _item("P", parent="DUPE", pdf=True)
    items = {"KEEPER": _item("KEEPER"), "DUPE": _item("DUPE"), "P": pdf}
    local.item.side_effect = lambda k: items[k]
    local.children.side_effect = lambda k: [pdf] if k == "DUPE" else []
    cloud.item.return_value = _item("DUPE", version=9)
    cloud.endpoint, cloud.library_type, cloud.library_id = "https://x", "groups", "1"
    cloud.client.patch.return_value = MagicMock(status_code=204)

    stats = _local_client(local, cloud).merge_duplicate_item(
        "KEEPER", "DUPE", union_tags=False,
        child_content_types=("application/pdf",),
    )

    assert stats["moved_pdf_keys"] == ["P"]
    (moved,), _ = local.update_item.call_args
    assert moved["data"]["parentItem"] == "KEEPER"
    cloud.update_item.assert_not_called()
    cloud.children.assert_not_called()
    # The trash alone stays on the cloud, with the cloud's own version.
    assert stats["trashed"] == ["DUPE"]
    headers = cloud.client.patch.call_args.kwargs["headers"]
    assert headers["If-Unmodified-Since-Version"] == "9"


def test_the_late_pdf_guard_reads_desktop_too() -> None:
    """Checking the cloud here would see the PDF still under the
    duplicate (Desktop has not synced the move up yet) and refuse to
    trash every time."""
    local, cloud = MagicMock(), MagicMock()
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
    cloud.item.return_value = _item("DUPE")
    cloud.client.patch.return_value = MagicMock(status_code=204)
    cloud.endpoint, cloud.library_type, cloud.library_id = "https://x", "groups", "1"

    stats = _local_client(local, cloud).merge_duplicate_item("KEEPER", "DUPE")
    assert stats["kept_unmoved_pdf"] is False


# --- reconcile_merged_pdfs --------------------------------------------------


def test_reconcile_finds_pdfs_desktop_still_files_elsewhere() -> None:
    zot = MagicMock()
    zot.cloud.children.side_effect = lambda k: {
        "K1": [_item("P1", parent="K1", pdf=True), _item("H", parent="K1")],
        "K2": [_item("P2", parent="K2", pdf=True)],
        "K3": [_item("P3", parent="K3", pdf=True)],
    }[k]

    def local_item(k):
        if k == "P3":
            raise RuntimeError("404")
        return {"P1": _item("P1", parent="TRASHED", pdf=True),
                "P2": _item("P2", parent="K2", pdf=True)}[k]
    zot.local.item.side_effect = local_item

    rows = rmp.find_stale(zot, ["K1", "K2", "K3"])
    assert [(r.keeper, r.pdf_key, r.local_parent) for r in rows] == [
        ("K1", "P1", "TRASHED"), ("K3", "P3", ""),
    ]


def test_reconcile_fix_reparents_on_the_write_surface() -> None:
    zot = MagicMock()
    rmp.fix_one(zot, rmp.Stale("K1", "P1", "TRASHED"))
    zot.reparent.assert_called_once_with("P1", "K1")
