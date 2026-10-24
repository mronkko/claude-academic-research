"""`attach_pdf` driving pyzotero 1.16's real `Zupload` over a fake server.

`test_attach_pdf_retry.py` mocks `Zupload` away and raises httpx
exceptions into it. That hid two faults, because what pyzotero actually
does differs on both counts:

- A dropped connection or timeout in the S3 PUT surfaces as
  `pyzotero.errors.UploadError`, raised `from None` — no status, no httpx
  class in the chain — so the retry predicate matched nothing and the
  documented transport retry never ran.
- `Zupload` creates the attachment item *before* uploading, and each
  retry began from a fresh template, so every attempt left one more
  file-less attachment under the parent.

Here the attachment-create, upload-auth and register steps go through a
real local pyzotero client (`zotero_io._LocalZotero`, as Zotero Desktop
is the only surface) over `httpx2.MockTransport`; the file POST is
pyzotero's module-level `httpx2.post`, patched to fail the way the
network does.
"""

from __future__ import annotations

import json

import httpx2
import pytest
import zotero_io
from pyzotero import errors as pz_errors

LIB = "/api/groups/12345"
S3 = "https://s3.example.test/upload"


class FakeUploadServer:
    def __init__(self) -> None:
        self.created: list[dict] = []
        self.registered: set[str] = set()
        self.trashed: list[str] = []
        self.next_key = 0

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        path = request.url.path.rstrip("/")
        if request.method == "POST" and path == f"{LIB}/items":
            payload = json.loads(request.content)
            self.created.extend(payload)
            self.next_key += 1
            key = f"ATTKEY{self.next_key:02d}"
            return httpx2.Response(
                200,
                json={"success": {"0": key},
                      "successful": {"0": {"key": key, "version": 5}},
                      "unchanged": {}, "failed": {}},
                headers={"Last-Modified-Version": "5"},
            )
        if request.method == "POST" and path.endswith("/file"):
            if b"upload=" in request.content:          # step 3: register
                self.registered.add(path.split("/")[-2])
                return httpx2.Response(204, headers={"Last-Modified-Version": "6"})
            return httpx2.Response(200, json={         # step 1: auth
                "url": S3, "uploadKey": "UPKEY",
                "params": {"key": "s3key", "acl": "private"},
            })
        if request.method == "GET" and path.startswith(f"{LIB}/items/"):
            key = path.split("/")[-1]
            md5 = "abc123" if key in self.registered else None
            return httpx2.Response(200, json={
                "key": key, "version": 6,
                "data": {"key": key, "version": 6, "md5": md5,
                         "linkMode": "imported_file"},
            })
        if request.method == "PATCH" and path.startswith(f"{LIB}/items/"):
            if json.loads(request.content) == {"deleted": 1}:
                self.trashed.append(path.split("/")[-1])
            return httpx2.Response(204)
        return httpx2.Response(404, text="not in fake")


@pytest.fixture(autouse=True)
def _no_backoff(monkeypatch):
    monkeypatch.setattr(zotero_io, "_write_sleep", lambda s: None, raising=False)


@pytest.fixture
def pdf(tmp_path):
    path = tmp_path / "paper.pdf"
    path.write_bytes(b"%PDF-1.4\n" + b"0" * 2000 + b"\n%%EOF\n")
    return path


def _client(server: FakeUploadServer) -> zotero_io.ZoteroClient:
    zc = zotero_io.ZoteroClient("12345", local_api_key="local-key")
    # A known server ID, so no write first has to ask for one.
    zc._local = zotero_io._LocalZotero(
        "12345", "group", None, local=True, local_api_key="local-key",
        server_id="SERVER01",
        client=httpx2.Client(transport=httpx2.MockTransport(server.handler)),
    )
    return zc


def _s3(monkeypatch, outcomes: list) -> list:
    """Patch pyzotero's S3 PUT. Each outcome is an exception to raise or
    a status to answer with; returns the list of calls made."""
    calls: list = []

    def fake_post(url, **kwargs):
        calls.append(url)
        outcome = outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return httpx2.Response(outcome, request=httpx2.Request("POST", url))

    monkeypatch.setattr(httpx2, "post", fake_post)
    return calls


def test_pyzotero_reports_a_dropped_upload_as_a_bare_upload_error(
    monkeypatch, pdf,
) -> None:
    """Pins what the retry has to recognise: no status, no httpx cause."""
    from pyzotero._upload import Zupload

    server = FakeUploadServer()
    _s3(monkeypatch, [httpx2.ConnectError("reset")])
    template = {"itemType": "attachment", "linkMode": "imported_file",
                "title": pdf.name, "filename": pdf.name}
    with pytest.raises(pz_errors.UploadError) as info:
        Zupload(_client(server).local, [template], "PARENT01",
                basedir=pdf.parent).upload()
    assert info.value.__cause__ is None
    assert zotero_io._http_status_of(info.value) is None
    assert template["key"] == "ATTKEY01"   # the item exists already


@pytest.mark.parametrize("network_fault", [
    httpx2.ConnectError("connection reset"),
    httpx2.ReadTimeout("timed out"),
])
def test_a_dropped_upload_is_retried_on_the_same_attachment(
    monkeypatch, pdf, network_fault,
) -> None:
    server = FakeUploadServer()
    calls = _s3(monkeypatch, [network_fault, 201])

    key = _client(server).attach_pdf("PARENT01", pdf)

    assert key == "ATTKEY01"
    assert len(calls) == 2                 # retried
    assert len(server.created) == 1        # ...without a second attachment
    assert server.registered == {"ATTKEY01"}
    assert server.trashed == []


def test_a_retried_upload_that_never_succeeds_trashes_its_empty_attachment(
    monkeypatch, pdf,
) -> None:
    server = FakeUploadServer()
    _s3(monkeypatch, [httpx2.ConnectError("reset")] * 3)
    zc = _client(server)

    with pytest.raises(pz_errors.UploadError):
        zc.attach_pdf("PARENT01", pdf)

    assert len(server.created) == 1
    assert server.trashed == ["ATTKEY01"]


def test_an_upload_whose_bytes_landed_anyway_is_a_success(monkeypatch, pdf) -> None:
    """The last attempt errors, but the file is registered (an answer
    lost in transit): the attachment is real, so it is kept and returned."""
    server = FakeUploadServer()
    _s3(monkeypatch, [httpx2.ConnectError("reset")] * 3)
    server.registered.add("ATTKEY01")

    assert _client(server).attach_pdf("PARENT01", pdf) == "ATTKEY01"
    assert server.trashed == []


def test_a_client_error_is_not_retried_and_leaves_no_empty_attachment(
    monkeypatch, pdf,
) -> None:
    server = FakeUploadServer()
    calls = _s3(monkeypatch, [403])

    with pytest.raises(pz_errors.UserNotAuthorisedError):
        _client(server).attach_pdf("PARENT01", pdf)

    assert len(calls) == 1
    assert server.trashed == ["ATTKEY01"]
