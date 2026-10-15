"""`--plan` and `--dry-run` never write to Zotero; `--replace` fetches fresh.

Driven through `main()` with a fake Zotero client and the fetch passes
stubbed out, so what is asserted is the orchestration: which passes run,
and whether anything reached a Zotero write method.
"""

from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace

import enrich_pdfs
import pytest

_PDF = b"%PDF-1.4\n" + b"x" * 2000 + b"\nstartxref\n9\n%%EOF\n"
DOI = "10.1/abc"


class _Zot:
    """Read methods answer from fixtures; write methods are recorded."""

    def __init__(self, *, has_pdf: bool = False, md5: str = "") -> None:
        self.writes: list[tuple] = []
        self._has_pdf = has_pdf
        self._md5 = md5

    def journal_articles(self):
        return [{"key": "K1", "data": {"DOI": DOI, "title": "T",
                                       "itemType": "journalArticle"}}]

    def pdf_map(self):
        return {"K1": (self._has_pdf, [])}

    def real_pdf_md5_map(self):
        return {"K1": {"ATT1": self._md5}} if self._has_pdf else {}

    def __getattr__(self, name):
        if name in {"attach_pdf", "trash_item", "delete_item", "update_tags",
                    "merge_duplicate_item", "update_item"}:
            def _w(*a, **k):
                self.writes.append((name, a, k))
                return "NEWATT"
            return _w
        raise AttributeError(name)


@pytest.fixture
def harness(tmp_path, monkeypatch):
    cache = tmp_path / "cache"
    cache.mkdir()
    calls: dict[str, list] = {"browser": [], "cascade": []}

    def _browser(items, zot, log_writer, args, run_date, **kw):
        calls["browser"].append([it["key"] for it in items])
        return 0

    def _cascade(items, sources, args, run_date, zot, log_writer):
        calls["cascade"].append([it["key"] for it in items])
        return 0, 0, 0

    monkeypatch.setattr(enrich_pdfs, "_load_config", lambda: SimpleNamespace(
        crossref_mailto=""))
    monkeypatch.setattr(enrich_pdfs, "require", lambda *a, **k: "x")
    monkeypatch.setattr(enrich_pdfs, "_run_browser_in_process", _browser)
    monkeypatch.setattr(enrich_pdfs, "_run_api_cascade", _cascade)
    monkeypatch.setattr(enrich_pdfs, "_print_run_report", lambda *a, **k: None)
    monkeypatch.setattr(enrich_pdfs, "_install_interaction_channel", lambda a: None)
    monkeypatch.setattr(enrich_pdfs, "_check_browser_ready", lambda: 0)
    monkeypatch.setattr(enrich_pdfs, "_check_connector_access", lambda: 0)
    monkeypatch.setattr(enrich_pdfs.fetchers, "pdf_sources",
                        lambda *a, **k: [SimpleNamespace(name="crossref")])

    def run(zot, *flags):
        monkeypatch.setattr(enrich_pdfs.zotero_io.ZoteroClient, "from_args",
                            classmethod(lambda cls, a: zot))
        monkeypatch.setattr(sys, "argv", [
            "enrich_pdfs.py", "--user", "--cache-dir", str(cache),
            "--log-csv", str(tmp_path / "log.csv"),
            "--failure-log-csv", str(tmp_path / "fail.csv"), *flags,
        ])
        return enrich_pdfs.main()

    return SimpleNamespace(run=run, cache=cache, calls=calls)


def _cache_file(cache):
    path = cache / "10.1_abc.pdf"
    path.write_bytes(_PDF)
    return path


@pytest.mark.parametrize("flags", [
    ("--sources", "browser", "--plan"),
    ("--all", "--plan"),
    ("--sources", "browser", "--plan", "--replace"),
])
def test_plan_never_attaches_from_the_cache(harness, flags) -> None:
    """`_attach_from_cache` was guarded only by --dry-run, so --plan
    uploaded every cached PDF — and with --replace trashed the old one."""
    _cache_file(harness.cache)
    zot = _Zot(has_pdf="--replace" in flags, md5="other")
    assert harness.run(zot, *flags) == 0
    assert zot.writes == []


def test_all_plan_does_not_run_the_api_cascade(harness) -> None:
    """Pass 1 downloads and attaches; --all --plan ran it anyway."""
    zot = _Zot()
    assert harness.run(zot, "--all", "--plan") == 0
    assert harness.calls["cascade"] == []
    assert harness.calls["browser"] == [["K1"]]
    assert zot.writes == []


def test_plan_without_a_browser_pass_is_refused(harness, capsys) -> None:
    """--plan on the API-cascade path was silently ignored: a full run."""
    zot = _Zot()
    assert harness.run(zot, "--plan") == 2
    assert harness.calls["cascade"] == []
    assert "--plan" in capsys.readouterr().err


def test_replace_does_not_reuse_the_cached_bad_copy(harness) -> None:
    """The bad attachment's bytes sit in the cache. --replace offered them
    back, `_attach_and_log` logged `unchanged`, the item was marked done:
    a silent no-op. They must be dropped so the cascade fetches fresh."""
    bad = _cache_file(harness.cache)
    md5 = hashlib.md5(_PDF).hexdigest()
    zot = _Zot(has_pdf=True, md5=md5)
    assert harness.run(zot, "--replace") == 0
    assert not bad.exists()
    assert harness.calls["cascade"] == [["K1"]]
    assert zot.writes == []          # nothing attached from the cache


def test_replace_keeps_a_cached_copy_that_differs(harness) -> None:
    """A cached file that is *not* the attachment is a real alternative;
    attaching it is a replacement, not a no-op."""
    alt = _cache_file(harness.cache)
    zot = _Zot(has_pdf=True, md5="0" * 32)
    monkey_text = enrich_pdfs._pdf_has_text
    try:
        enrich_pdfs._pdf_has_text = lambda *a, **k: True
        assert harness.run(zot, "--replace") == 0
    finally:
        enrich_pdfs._pdf_has_text = monkey_text
    assert alt.exists()
    assert any(w[0] == "attach_pdf" for w in zot.writes)


def _log_attached(log_csv) -> None:
    import csv

    with open(log_csv, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=enrich_pdfs.LOG_FIELDS)
        w.writeheader()
        w.writerow({"run_date": "2026-01-01", "item_key": "K1", "doi": DOI,
                    "title": "T", "status": "attached", "source": "crossref"})


def test_a_logged_attach_whose_pdf_is_gone_is_refetched(harness, tmp_path) -> None:
    """The log gate ran before the Zotero check: an `attached` row kept
    an item out for good, even after the user deleted its bad PDF so it
    would be refetched. Zotero's state decides now."""
    _log_attached(tmp_path / "log.csv")
    assert harness.run(_Zot(has_pdf=False)) == 0
    assert harness.calls["cascade"] == [["K1"]]


def test_a_logged_attach_that_still_has_its_pdf_is_skipped(harness, tmp_path) -> None:
    _log_attached(tmp_path / "log.csv")
    assert harness.run(_Zot(has_pdf=True, md5="m")) == 0
    assert harness.calls["cascade"] == []


def test_dry_run_skips_the_connector_pass() -> None:
    """The Connector saves into Zotero, merges and trashes — there is no
    dry form of it, and --dry-run ran it anyway."""
    items = [{"item_key": "K1"}]
    assert enrich_pdfs._connector_items_for_run(
        items, SimpleNamespace(dry_run=True)) == []
    assert enrich_pdfs._connector_items_for_run(
        items, SimpleNamespace(dry_run=False)) == items
