"""Manual download mode (`enrich_pdfs.py --manual`, `manual_pdfs.py`).

No network, no real browser: tabs go to a recording opener, the
Downloads folder is a tmp directory, and page text comes from a dict
instead of pdftotext.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import enrich_pdfs
import manual_pdfs as mp
import pytest
from fetchers import pdf_version


@pytest.fixture(autouse=True)
def _project(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mp, "TAB_DELAY_S", 0)
    (tmp_path / "Downloads").mkdir()


def _pdf(marker: str) -> bytes:
    return b"%PDF-1.4\n" + marker.encode() + b"\n" + b"0" * 2000 + b"\n%%EOF\n"


def _raw(key, doi="", title="", **data):
    return {"key": key, "data": {"DOI": doi, "title": title,
                                 "itemType": "journalArticle", **data}}


ITEMS = [
    _raw("AAAA1111", "10.1016/j.jbusvent.2019.105971",
         "Entrepreneurial orientation and firm growth in emerging markets",
         url="https://www.sciencedirect.com/science/article/pii/S0883902619301234"),
    _raw("BBBB2222", "10.1002/smj.3456",
         "Dynamic capabilities and the theory of the multinational firm"),
    _raw("CCCC3333", "10.1177/1042258719890991",
         "Crowdfunding signals and venture success: a meta-analysis"),
]


class Rig:
    """A Context with recording fakes."""

    def __init__(self, tmp_path, items=ITEMS, *, has_pdf=(), routes=None,
                 texts=None, batch_size=2, priority=(), cached=None,
                 attach_outcome=mp.ATTACHED):
        self.downloads = tmp_path / "Downloads"
        self.opened: list[str] = []
        self.attached: list[tuple[str, str, str]] = []
        self.texts = texts if texts is not None else {}
        self.routes = routes or {}
        self.pdfs = set(has_pdf)
        self.outcome = attach_outcome
        self.ctx = mp.Context(
            library="group:1",
            scope_items=list(items),
            has_pdf=lambda k: k in self.pdfs,
            attach=self._attach,
            downloads=self.downloads,
            batch_size=batch_size,
            route_lookup=self._route,
            priority=set(priority),
            cached_url=lambda doi: (cached or {}).get(doi, ""),
            text_extractor=lambda path, digest: self.texts.get(path.name, ""),
            opener=self._open,
        )

    def _open(self, url):
        self.opened.append(url)
        return True

    def _route(self, doi, data):
        return self.routes.get(doi, (None, None))

    def _attach(self, item, path):
        self.attached.append((item.key, path.name, pdf_version.version_of(path)))
        return self.outcome

    def download(self, name, marker=None):
        (self.downloads / name).write_bytes(_pdf(marker or name))

    def cycle(self, action="next"):
        rc, summary = mp.run_cycle(self.ctx, action)
        assert rc == 0
        return summary


def _state():
    return json.loads(mp.STATE_PATH.read_text())


# --- opening ----------------------------------------------------------------

def test_batch_opens_in_queue_order_with_resolver_then_doi_links(tmp_path):
    route = "https://research-ebsco-com.libproxy.example.org/article?a=1"
    rig = Rig(tmp_path, routes={"10.1002/smj.3456": (route, None)})
    s = rig.cycle()
    assert rig.opened == ["https://doi.org/10.1016/j.jbusvent.2019.105971", route]
    assert s.opened == 2 and s.batch == 1
    batch = _state()["batches"][0]
    assert batch["keys"] == ["AAAA1111", "BBBB2222"]
    assert (mp.STAGING_DIR / "batch-1" / "links.txt").read_text().count("\n") == 2


def test_priority_sets_open_first(tmp_path):
    rig = Rig(tmp_path, priority={"CCCC3333"}, batch_size=1)
    rig.cycle()
    assert rig.opened == ["https://doi.org/10.1177/1042258719890991"]


def test_items_with_a_pdf_or_without_doi_are_not_opened(tmp_path):
    items = [*ITEMS, _raw("DDDD4444", "", "No DOI here at all for this item")]
    rig = Rig(tmp_path, items, has_pdf={"AAAA1111"}, batch_size=10)
    rig.cycle()
    assert rig.opened == ["https://doi.org/10.1002/smj.3456",
                          "https://doi.org/10.1177/1042258719890991"]


def test_login_first_opens_one_tab_then_the_rest(tmp_path, capsys):
    proxied = "https://research-ebsco-com.ezproxy.jyu.fi/c/abc"
    rig = Rig(tmp_path, routes={"10.1002/smj.3456": (proxied, None)}, batch_size=3)
    s = rig.cycle()
    assert rig.opened == [proxied]
    assert s.login_first and "SIGN IN FIRST" in capsys.readouterr().out
    rig.cycle()
    # Signed in recently: the rest open in full, the proxied one is not reopened.
    assert rig.opened[1:] == ["https://doi.org/10.1016/j.jbusvent.2019.105971",
                              "https://doi.org/10.1177/1042258719890991"]
    assert [b["login_first"] for b in _state()["batches"]] == [True, False]


def test_a_tab_that_fails_to_open_is_reported(tmp_path):
    rig = Rig(tmp_path, batch_size=1)
    rig.ctx.opener = lambda url: False
    s = rig.cycle()
    assert s.failed_to_open == ["AAAA1111"]


# --- collecting -------------------------------------------------------------

def test_only_files_new_since_the_batch_opened_move_and_partials_stay(tmp_path):
    rig = Rig(tmp_path)
    rig.download("old-invoice.pdf")
    rig.cycle()                                   # opens batch 1, snapshot
    rig.download("paper.pdf")
    (rig.downloads / "big.pdf.crdownload").write_bytes(b"partial")
    s = rig.cycle("collect")
    assert s.collected == ["paper.pdf"]
    assert s.in_progress == ["big.pdf.crdownload"]
    assert (rig.downloads / "old-invoice.pdf").exists()
    assert (rig.downloads / "big.pdf.crdownload").exists()
    assert (mp.STAGING_DIR / "batch-1" / "paper.pdf").exists()
    assert not (rig.downloads / "paper.pdf").exists()


def test_finished_partial_is_collected_next_cycle(tmp_path):
    rig = Rig(tmp_path, texts={"late.pdf": "doi: 10.1002/smj.3456"})
    rig.cycle()
    (rig.downloads / "late.pdf.crdownload").write_bytes(b"x")
    rig.cycle("collect")
    (rig.downloads / "late.pdf.crdownload").unlink()
    rig.download("late.pdf")
    s = rig.cycle("collect")
    assert s.collected == ["late.pdf"]
    assert rig.attached[0][0] == "BBBB2222"


# --- matching ---------------------------------------------------------------

def test_match_by_doi_on_first_page(tmp_path):
    rig = Rig(tmp_path, texts={
        "x.pdf": "Strategic Mgmt J\nhttps://doi.org/10.1002/smj.3456\f"
                 "refs 10.1177/1042258719890991",
    })
    rig.cycle()
    rig.download("x.pdf")
    s = rig.cycle("collect")
    assert [k for k, _, _ in rig.attached] == ["BBBB2222"]
    assert s.attached == [("x.pdf", "BBBB2222")]


def test_match_by_title_on_first_page(tmp_path):
    rig = Rig(tmp_path, texts={
        "download.pdf": "CROWDFUNDING SIGNALS AND VENTURE\nSUCCESS: A META-ANALYSIS\n",
    })
    rig.cycle()
    rig.download("download.pdf")
    rig.cycle("collect")
    assert [k for k, _, _ in rig.attached] == ["CCCC3333"]


def test_match_by_elsevier_pii_file_name(tmp_path):
    rig = Rig(tmp_path)              # no text: only the name can say
    rig.cycle()
    rig.download("1-s2.0-S0883902619301234-main.pdf")
    rig.cycle("collect")
    assert [k for k, _, _ in rig.attached] == ["AAAA1111"]


def test_pii_from_the_cached_crossref_url(tmp_path):
    items = [_raw("EEEE5555", "10.1016/j.respol.2020.104000", "Some title long enough")]
    rig = Rig(tmp_path, items, cached={
        "10.1016/j.respol.2020.104000":
            "https://linkinghub.elsevier.com/retrieve/pii/S0048733320300789",
    })
    rig.cycle()
    rig.download("1-s2.0-S0048733320300789-main.pdf")
    rig.cycle("collect")
    assert [k for k, _, _ in rig.attached] == ["EEEE5555"]


def test_match_by_doi_shaped_file_name(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.download("10.1002_smj.3456.pdf")
    rig.cycle("collect")
    assert [k for k, _, _ in rig.attached] == ["BBBB2222"]


def test_late_download_matches_an_item_from_an_earlier_batch(tmp_path):
    rig = Rig(tmp_path, batch_size=1, texts={"a.pdf": "doi 10.1016/j.jbusvent.2019.105971"})
    rig.cycle()                      # batch 1: AAAA
    rig.cycle()                      # batch 2: BBBB
    rig.download("a.pdf")            # the batch-1 article arrives late
    rig.cycle()                      # batch 3 opens; batch 2's window collects it
    assert [k for k, _, _ in rig.attached] == ["AAAA1111"]
    assert (mp.STAGING_DIR / "batch-2" / "a.pdf").exists()


def test_duplicate_download_is_skipped_by_hash(tmp_path):
    rig = Rig(tmp_path, texts={"p.pdf": "10.1002/smj.3456",
                               "p (1).pdf": "10.1002/smj.3456"})
    rig.cycle()
    rig.download("p.pdf", marker="same")
    rig.download("p (1).pdf", marker="same")
    s = rig.cycle("collect")
    assert len(rig.attached) == 1
    assert s.duplicates == ["p.pdf"] or s.duplicates == ["p (1).pdf"]


def test_ambiguous_title_is_never_attached(tmp_path):
    items = [_raw("K1", "10.1/a", "The same long title used by two items"),
             _raw("K2", "10.1/b", "The same long title used by two items")]
    rig = Rig(tmp_path, items, texts={"t.pdf": "The same long title used by two items"})
    rig.cycle()
    rig.download("t.pdf")
    s = rig.cycle("collect")
    assert rig.attached == []
    assert s.unmatched[0]["status"] == mp.AMBIGUOUS
    report = json.loads(mp.UNMATCHED_REPORT.read_text())
    assert {c["item_key"] for c in report["files"][0]["candidates"]} == {"K1", "K2"}


def test_conflicting_signals_are_ambiguous(tmp_path):
    rig = Rig(tmp_path, texts={"10.1002_smj.3456.pdf": "doi 10.1177/1042258719890991"})
    rig.cycle()
    rig.download("10.1002_smj.3456.pdf")
    s = rig.cycle("collect")
    assert rig.attached == []
    assert s.unmatched[0]["status"] == mp.AMBIGUOUS


def test_unmatched_file_is_listed_not_guessed(tmp_path, capsys):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.download("random.pdf")
    s = rig.cycle("collect")
    assert rig.attached == [] and s.unmatched[0]["name"] == "random.pdf"
    out = capsys.readouterr().out
    assert "Unmatched 1" in out and "MANUAL: action=collect" in out


def test_page_doi_of_an_item_that_has_a_pdf_is_not_needed(tmp_path):
    rig = Rig(tmp_path, has_pdf={"BBBB2222"}, texts={"b.pdf": "10.1002/smj.3456"})
    rig.cycle()
    rig.download("b.pdf")
    s = rig.cycle("collect")
    assert rig.attached == [] and s.not_needed


def test_corrupt_file_is_rejected(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    (rig.downloads / "broken.pdf").write_bytes(b"%PDF-1.4 tiny")
    s = rig.cycle("collect")
    assert rig.attached == [] and s.rejected[0][0] == "broken.pdf"


# --- assign / ignore --------------------------------------------------------

def test_explicit_assign_attaches_an_unmatched_file(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.download("random.pdf")
    rig.cycle("collect")
    assert mp.assign(rig.ctx, "random.pdf", "CCCC3333") == 0
    assert [k for k, _, _ in rig.attached] == ["CCCC3333"]
    entry = next(iter(_state()["files"].values()))
    assert entry["status"] == mp.ATTACHED and entry["item_key"] == "CCCC3333"
    # A settled file is not matched again.
    rig.cycle("collect")
    assert len(rig.attached) == 1


def test_assign_refuses_an_item_that_already_has_a_pdf(tmp_path):
    rig = Rig(tmp_path, has_pdf={"CCCC3333"})
    rig.cycle()
    rig.download("random.pdf")
    rig.cycle("collect")
    assert mp.assign(rig.ctx, "random.pdf", "CCCC3333") == 2
    assert rig.attached == []


def test_ignore_settles_a_file(tmp_path):
    rig = Rig(tmp_path)
    rig.cycle()
    rig.download("random.pdf")
    rig.cycle("collect")
    assert mp.ignore(rig.ctx, "random.pdf") == 0
    s = rig.cycle("collect")
    assert s.unmatched == []


# --- version ----------------------------------------------------------------

def test_version_recorded_from_the_route(tmp_path):
    rig = Rig(tmp_path, texts={"x.pdf": "10.1002/smj.3456"})
    rig.cycle()
    rig.download("x.pdf")
    rig.cycle("collect")
    assert rig.attached[0][2] == pdf_version.PUBLISHED      # doi.org landing


def test_first_page_manuscript_notice_lowers_the_version(tmp_path):
    rig = Rig(tmp_path, texts={
        "x.pdf": "Author Accepted Manuscript\n10.1002/smj.3456"})
    rig.cycle()
    rig.download("x.pdf")
    rig.cycle("collect")
    assert rig.attached[0][2] == pdf_version.ACCEPTED


def test_never_opened_item_counts_as_accepted(tmp_path):
    rig = Rig(tmp_path, batch_size=1, texts={"c.pdf": "10.1177/1042258719890991"})
    rig.cycle()                       # opens only AAAA
    rig.download("c.pdf")             # but the user fetched CCCC on their own
    rig.cycle("collect")
    assert rig.attached == [("CCCC3333", "c.pdf", pdf_version.ACCEPTED)]


def test_content_version_phrases():
    assert mp.content_version("This is a preprint; not peer reviewed") == "submitted"
    assert mp.content_version("arXiv:2101.01234v2 [econ.GN]") == "submitted"
    assert mp.content_version("Received 3 May; accepted for publication 9 June") is None


# --- state ------------------------------------------------------------------

def test_state_resumes_across_contexts(tmp_path):
    Rig(tmp_path, batch_size=1).cycle()
    rig = Rig(tmp_path, batch_size=1)          # a new session
    s = rig.cycle()
    assert s.batch == 2
    assert rig.opened == ["https://doi.org/10.1002/smj.3456"]


def test_state_refuses_another_library(tmp_path):
    Rig(tmp_path).cycle()
    rig = Rig(tmp_path)
    rig.ctx.library = "user:0"
    rc, _ = mp.run_cycle(rig.ctx, "next")
    assert rc == 2 and rig.opened == []


def test_dry_run_opens_and_moves_nothing(tmp_path):
    rig = Rig(tmp_path, texts={"x.pdf": "10.1002/smj.3456"})
    rig.cycle()
    rig.download("x.pdf")
    rig.ctx.dry_run = True
    s = rig.cycle()
    assert len(rig.opened) == 2                 # only the first, real cycle
    assert (rig.downloads / "x.pdf").exists()
    assert rig.attached == [] and s.attached == [("x.pdf", "BBBB2222")]


def test_priority_keys_read_retry_sets_and_log(tmp_path):
    stem = tmp_path / "audit"
    Path(f"{stem}.retry.manual.keys").write_text("K1\n")
    Path(f"{stem}.retry.below_policy.keys").write_text("K2\n")
    log = tmp_path / "log.csv"
    log.write_text("item_key,status\nK3,skipped_below_policy\nK4,attached\n")
    assert mp.priority_keys(str(stem), str(log)) == {"K1", "K2", "K3"}


# --- through enrich_pdfs: the attach path, the gate and the tag --------------

def _zot(pdf_map=None):
    zot = MagicMock()
    zot.library_type, zot.group_id = "group", "1"
    zot.pdf_map.return_value = pdf_map or {}
    zot.attach_pdf.return_value = "ATT1"
    return zot


def _cli(tmp_path, *argv, min_version="accepted"):
    args = enrich_pdfs._build_parser().parse_args([
        "--downloads-dir", str(tmp_path / "Downloads"),
        "--log-csv", str(tmp_path / "log.csv"), "--failure-log-csv", "",
        "--no-check-text", *argv,
    ])
    args.pdf_policy = MagicMock(min_version=min_version)
    return args


def _stage(tmp_path, name, rows):
    """Put a staged file in place and record it in the state as opened."""
    d = mp.STAGING_DIR / "batch-1"
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_bytes(_pdf(name))
    state = mp.load_state()
    state["library"] = "group:1"
    state["batches"] = [{"n": 1, "opened_at": 0, "keys": list(rows),
                         "links": {}, "snapshot": {}, "login_first": False}]
    state["items"] = rows
    mp.save_state(state)


def test_cli_attach_tags_manual_and_version(tmp_path, monkeypatch):
    _stage(tmp_path, "x.pdf", {"BBBB2222": {"route_version": "published"}})
    zot = _zot()
    args = _cli(tmp_path, "--manual", "collect")
    ctx_cls = mp.Context

    def ctx(**kw):
        kw["text_extractor"] = lambda path, digest: "10.1002/smj.3456"
        return ctx_cls(**kw)

    monkeypatch.setattr(mp, "Context", ctx)
    assert enrich_pdfs._run_manual(args, zot, ITEMS, "2026-10-15") == 0
    zot.attach_pdf.assert_called_once()
    added = zot.update_tags.call_args.kwargs["add"]
    assert "pdf:manual" in added and "pdf:version:published" in added
    log = (tmp_path / "log.csv").read_text()
    assert ",attached,manual," in log


def test_cli_policy_gate_refuses_a_lower_version(tmp_path, monkeypatch):
    _stage(tmp_path, "x.pdf", {"BBBB2222": {"route_version": "accepted"}})
    zot = _zot()
    args = _cli(tmp_path, "--manual", "collect", min_version="published")
    ctx_cls = mp.Context

    def ctx(**kw):
        kw["text_extractor"] = lambda path, digest: "10.1002/smj.3456"
        return ctx_cls(**kw)

    monkeypatch.setattr(mp, "Context", ctx)
    assert enrich_pdfs._run_manual(args, zot, ITEMS, "2026-10-15") == 0
    zot.attach_pdf.assert_not_called()
    entry = next(iter(mp.load_state()["files"].values()))
    assert entry["status"] == mp.BELOW_POLICY
    assert "skipped_below_policy" in (tmp_path / "log.csv").read_text()


@pytest.mark.parametrize("argv", [
    ["--manual", "next", "--all"],
    ["--manual", "next", "--sources", "wiley"],
    ["--manual", "assign", "--assign-file", "x.pdf"],
    ["--manual", "ignore"],
    ["--manual", "next", "--batch-size", "0"],
])
def test_manual_flag_clashes_exit_2(argv):
    args = enrich_pdfs._build_parser().parse_args(argv)
    assert enrich_pdfs._check_manual_args(args) == 2


def test_manual_settings_default_to_home_downloads(tmp_path, monkeypatch):
    monkeypatch.setattr(enrich_pdfs, "get", lambda *a, **k: "")
    args = enrich_pdfs._build_parser().parse_args(["--manual", "next"])
    downloads, size = enrich_pdfs._manual_settings(args)
    assert downloads == Path.home() / "Downloads" and size == 25
    monkeypatch.setattr(enrich_pdfs, "get",
                        lambda s, k, env=None: {"downloads_dir": str(tmp_path),
                                                "manual_batch_size": "10"}[k])
    assert enrich_pdfs._manual_settings(args) == (tmp_path, 10)
