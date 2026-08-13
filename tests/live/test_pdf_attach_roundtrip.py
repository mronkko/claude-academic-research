"""Live tests for the fetch → attach → verify seam.

Opt in with `pytest -m live`. Needs `ZOTERO_API_KEY` and the
hand-created `academic-research-e2e` Zotero group; skips cleanly
otherwise. Seconds, not minutes — no LLM spend, no Cloudflare solve, no
full SLR run.

This is the coverage gap that let a live run lose 48 PDFs. The browser
cascade solved Cloudflare, downloaded 68 Sage PDFs, and attached 20; the
other 48 failed at the Zotero upload step, were logged as a bare
`upload_failed` with the reason discarded, and sat fully downloaded in
`output/pdf_cache/` while the run reported success and moved on. Nothing
in the suite exercised the attach step against a real Zotero library.

The bug was publisher-independent — it lived in the upload, not in the
fetch — so covering it needs no Cloudflare-gated publisher and no
interactive terminal. That is the point of putting it here rather than
in `test_browser_publishers.py`.

Every test creates its own scratch item and deletes it in teardown, so
a failed run leaves no residue in the group.
"""

from __future__ import annotations

import uuid

import pytest
import zotero_io

from tests.live.conftest import require_config

pytestmark = pytest.mark.live

GROUP_NAME = "academic-research-e2e"


def _pdf_bytes(marker: bytes | None = None) -> bytes:
    """A small but structurally complete PDF, unique per call by default.

    Must satisfy `fetchers._pdf_validate`: `%PDF-` header, >= 1000
    bytes, and an `%%EOF` trailer whose `startxref` is in bounds.

    The content is randomised because Zotero deduplicates uploads by
    file hash: a fixture with fixed bytes uploads cleanly the first
    time, then comes back `unchanged` on every later run — so the test
    passes once and then fails forever on a real library.
    """
    if marker is None:
        marker = uuid.uuid4().hex.encode()
    return b"%PDF-1.4\n" + marker + b"\n" + b"0" * 2000 + b"\nstartxref\n9\n%%EOF\n"


def _truncated_bytes() -> bytes:
    """The OpenAlex incident's signature: xref offset past end of file."""
    return b"%PDF-1.4\n" + b"0" * 2000 + b"\nstartxref\n1744085\n%%EOF\n"


@pytest.fixture(scope="module")
def client():
    require_config("zotero", "api_key", env="ZOTERO_API_KEY")
    try:
        group = zotero_io.find_group_by_name(GROUP_NAME)
    except Exception as exc:
        pytest.skip(f"could not list Zotero groups ({exc})")
    if group is None:
        pytest.skip(
            f"Zotero group {GROUP_NAME!r} not found. Create it by hand "
            f"(the Web API cannot create groups) — see BACKLOG.md's live "
            f"end-to-end SLR entry."
        )
    return zotero_io.ZoteroClient.from_args(
        type("A", (), {"group": str(group["id"]), "user": False})(),
    )


@pytest.fixture
def scratch_item(client):
    """A throwaway parent item, removed after the test."""
    key = client.cloud.create_items([{
        "itemType": "journalArticle",
        "title": f"[live-test] attach roundtrip {uuid.uuid4().hex[:8]}",
        "DOI": f"10.9999/live-test-{uuid.uuid4().hex[:8]}",
    }])["successful"]["0"]["key"]
    yield key
    try:
        client.delete_item(key)
    except Exception:
        pass


def _attachment_keys(client, parent_key: str) -> list[dict]:
    return [
        child for child in client.cloud.children(parent_key)
        if child["data"].get("contentType") == "application/pdf"
    ]


# --- the round trip ---------------------------------------------------

def test_attached_pdf_actually_lands_with_bytes(client, scratch_item, tmp_path):
    """The invariant the lost-48 incident violated: an item the log
    calls `attached` must really carry a PDF.

    Asserting on the returned attachment key alone is not enough — a
    metadata-only attachment with no `md5` is exactly what
    `pdf_map()` classifies as a stub, i.e. an item with no real PDF.
    So this asserts on the md5, which only exists once the bytes are
    stored.
    """
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_pdf_bytes())

    attachment_key = client.attach_pdf(scratch_item, pdf)
    assert attachment_key, "attach_pdf returned no key"

    children = _attachment_keys(client, scratch_item)
    assert len(children) == 1, f"expected one PDF child, got {len(children)}"
    assert children[0]["data"].get("md5"), (
        "attachment has no md5 — the file bytes never landed, and "
        "pdf_map() will treat this item as having no PDF"
    )


def test_pdf_map_agrees_the_item_has_a_real_pdf(client, scratch_item, tmp_path):
    """`pdf_map()` decides whether a later run re-processes an item, so
    it has to agree with the attach step.

    Reads the cloud library explicitly. `ZoteroClient` prefers the local
    Zotero server for reads while uploads go to the Web API, so the
    default client would race Zotero Desktop's sync — the first version
    of this test failed for exactly that reason, which is also why
    `mini_slr.py` has a dedicated sync stage between import and enrich.
    """
    import zotero_io as zio

    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_pdf_bytes())
    client.attach_pdf(scratch_item, pdf)

    cloud_reader = zio.ZoteroClient(
        api_key=client.api_key, group_id=client.group_id, prefer_local=False,
    )
    has_real, stubs = cloud_reader.pdf_map().get(scratch_item, (False, []))
    assert has_real, "pdf_map does not see the PDF we just attached"
    assert not stubs


def test_reattaching_the_same_file_is_not_an_error(client, scratch_item, tmp_path):
    """Zotero reports an identical re-upload as `unchanged`; the wrapper
    must treat that as success, not raise into an `upload_failed` row."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_pdf_bytes())

    client.attach_pdf(scratch_item, pdf)
    client.attach_pdf(scratch_item, pdf)      # must not raise


# --- failures are diagnosable ----------------------------------------

def test_attach_to_a_nonexistent_parent_raises(client, tmp_path):
    """A failure must surface as an exception carrying a usable reason —
    the run log's `detail` column is built from it."""
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(_pdf_bytes())

    with pytest.raises(Exception) as exc:
        client.attach_pdf("ZZZZZZZZ", pdf)
    assert str(exc.value).strip(), "exception carried no message to log"


def test_attach_of_an_empty_file_fails_loudly(client, scratch_item, tmp_path):
    """A zero-byte upload must be refused before it reaches Zotero.

    Zotero accepts one happily, and the resulting attachment still
    carries an md5 — of nothing — which `pdf_map()` reads as "this item
    has a real PDF". The item would then be marked complete and skipped
    by every future run, holding an attachment with no content. This
    test found that gap live; `attach_pdf` now rejects empty files.
    """
    empty = tmp_path / "empty.pdf"
    empty.write_bytes(b"")

    with pytest.raises(RuntimeError, match="empty file"):
        client.attach_pdf(scratch_item, empty)


# --- structural validation against the live library -------------------

def test_truncated_pdf_is_rejected_before_upload(tmp_path):
    """The OpenAlex incident: five truncated files were attached and
    counted as successes, then misdiagnosed as scans needing OCR.

    Runs locally — the point is that the corrupt file never reaches
    Zotero at all, so there is nothing to clean up.
    """
    from fetchers import _pdf_validate

    bad = tmp_path / "truncated.pdf"
    bad.write_bytes(_truncated_bytes())

    defect = _pdf_validate.file_defect(bad)
    assert defect is not None and "truncated" in defect

    good = tmp_path / "ok.pdf"
    good.write_bytes(_pdf_bytes())
    assert _pdf_validate.file_defect(good) is None
