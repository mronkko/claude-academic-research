"""The Pass 2 API retry must not count a failed attach as attached.

It ignored `_attach_and_log`'s False and `continue`d past the browser
handler and Connector, so an item whose fetched file was corrupt or whose
upload failed was given up on with no PDF — and the run summary counted
it as attached.
"""

from __future__ import annotations

import inspect
from types import SimpleNamespace

import enrich_pdfs
import pytest


@pytest.fixture(autouse=True)
def _in_tmp_dir(tmp_path, monkeypatch):
    """The sources hand back relative paths ("p.pdf", "cache"). Run in an
    empty directory so they never resolve to a real file: when the repo
    root once held a `p.pdf`, this module stamped a `p.pdf.version.json`
    sidecar next to it."""
    monkeypatch.chdir(tmp_path)


class _Src:
    def __init__(self, name, path):
        self.name = name
        self.direct_access_domains = ("wiley.com",)
        self._path = path
        self.calls = 0

    def fetch_pdf(self, doi, *, cache_dir, bypass_prefix_filter):
        self.calls += 1
        return (self._path, "u") if self._path else None


class _Log:
    def __init__(self):
        self.rows = []

    def writerow(self, row):
        self.rows.append(row)


def _args(**kw):
    base = dict(cache_dir="cache", dry_run=False, failure_log_csv="",
                no_check_text=True)
    base.update(kw)
    return SimpleNamespace(**base)


def _retry(sources, **kw):
    return enrich_pdfs._pass2_api_retry(
        {"item_key": "K1", "title": "T"}, "10.1/x",
        "onlinelibrary.wiley.com", sources,
        zot=None, log_writer=_Log(), args=_args(**kw), run_date="d",
    )


def test_a_failed_attach_routes_the_item_on(monkeypatch) -> None:
    monkeypatch.setattr(enrich_pdfs, "_attach_and_log", lambda *a, **k: False)
    assert _retry([_Src("wiley", "p.pdf")]) is None


def test_a_failed_attach_tries_the_next_source(monkeypatch) -> None:
    calls = []

    def attach(*a, **k):
        calls.append(k["source"])
        return k["source"] == "wiley2"

    monkeypatch.setattr(enrich_pdfs, "_attach_and_log", attach)
    assert _retry([_Src("wiley", "a.pdf"), _Src("wiley2", "b.pdf")]) == "attached"
    assert calls == ["wiley", "wiley2"]


def test_a_successful_attach_and_a_dry_run_stop_routing(monkeypatch) -> None:
    monkeypatch.setattr(enrich_pdfs, "_attach_and_log", lambda *a, **k: True)
    assert _retry([_Src("wiley", "p.pdf")]) == "attached"
    assert _retry([_Src("wiley", "p.pdf")], dry_run=True) == "dry_run"


def test_a_source_for_another_host_is_not_asked(monkeypatch) -> None:
    monkeypatch.setattr(enrich_pdfs, "_attach_and_log", lambda *a, **k: True)
    src = _Src("wiley", "p.pdf")
    src.direct_access_domains = ("elsevier.com",)
    assert _retry([src]) is None
    assert src.calls == 0


def test_browser_entries_carry_the_item_type() -> None:
    """Browser failure rows logged item_type "" (then "journalArticle"),
    so a book chapter's miss read as an article's."""
    src = inspect.getsource(enrich_pdfs._run_browser_in_process)
    assert '"item_type": item_data.get("itemType"' in src
