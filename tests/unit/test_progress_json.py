"""`--progress-json` on the six job scripts, and `--control-file` on enrich_dois.

An MCP job (BACKLOG P21) is followed through its `progress.jsonl`, not its
stdout, so each script must write the start / item / summary / done
events whatever path it takes. Each test drives the real `main()` in
process with Zotero, the network and the LLM stubbed out, then reads the
file back.
"""

from __future__ import annotations

import csv
import json
import sys
import threading
import time
import types
from pathlib import Path

import progress_json
import pytest

# ---------------------------------------------------------------------------
# The module itself
# ---------------------------------------------------------------------------


def _events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _names(events: list[dict]) -> list[str]:
    return [e["event"] for e in events]


@pytest.fixture(autouse=True)
def _reset_sink():
    progress_json.reset()
    yield
    progress_json.reset()


def test_emit_is_a_no_op_without_a_path(tmp_path: Path) -> None:
    progress_json.emit("item", done=1)  # must not raise or write anywhere
    assert not progress_json.active()


def test_configure_truncates_and_records_carry_ts_and_event(tmp_path: Path) -> None:
    path = tmp_path / "sub" / "p.jsonl"
    path.parent.mkdir()
    path.write_text("stale\n", encoding="utf-8")
    progress_json.configure(path)
    progress_json.emit("item", key="K1", done=1, queued=2)
    [record] = _events(path)
    assert record["event"] == "item" and record["key"] == "K1"
    assert isinstance(record["ts"], float)


@pytest.mark.parametrize("body, code, extra", [
    (lambda: 0, 0, {}),
    (lambda: 1, 1, {}),
    (lambda: sys.exit("ERROR: no config"), 1, {"error": "ERROR: no config"}),
    (lambda: sys.exit(2), 2, {}),
])
def test_reports_done_closes_every_exit_path(tmp_path, body, code, extra) -> None:
    path = tmp_path / "p.jsonl"

    @progress_json.reports_done
    def main():
        progress_json.configure(path)
        return body()

    try:
        main()
    except SystemExit:
        pass
    done = _events(path)[-1]
    assert done["event"] == "done" and done["exit_code"] == code
    assert done["ok"] is (code == 0)
    for k, v in extra.items():
        assert done[k] == v
    assert not progress_json.active()


def test_reports_done_records_a_crash(tmp_path: Path) -> None:
    path = tmp_path / "p.jsonl"

    @progress_json.reports_done
    def main():
        progress_json.configure(path)
        raise ValueError("boom")

    with pytest.raises(ValueError):
        main()
    done = _events(path)[-1]
    assert done["exit_code"] == 1 and "boom" in done["error"]


# ---------------------------------------------------------------------------
# search.py
# ---------------------------------------------------------------------------


def test_search_emits_progress(tmp_path, monkeypatch) -> None:
    import search
    from searchers import SEARCH_ROW_FIELDS

    class Source:
        def __init__(self, name, rows):
            self.name, self.rows = name, rows

        def credentials_error(self, _ctx):
            return None

        def run(self, _cfg, _ctx):
            return [{**{f: "" for f in SEARCH_ROW_FIELDS},
                     "doi": f"10.1/{self.name}{i}", "title": f"t{i}"}
                    for i in range(self.rows)]

    cfg = tmp_path / "search_config.py"
    cfg.write_text('FROM_YEAR = 2019\nTO_YEAR = 2019\n'
                   'JOURNALS = {"0883-9026": ("n/a", "JBV")}\n'
                   'QUERY_DEFS = [("q", "x", "y")]\n', encoding="utf-8")
    monkeypatch.setattr(search, "searchers_by_name",
                        lambda: {"alpha": Source("alpha", 2), "bravo": Source("bravo", 1)})
    events_path = tmp_path / "p.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "search.py", "--config", str(cfg), "--output-dir", str(tmp_path / "raw"),
        "--metadata-dir", str(tmp_path), "--streams", "keyword",
        "--progress-json", str(events_path),
    ])
    assert search.main() == 0
    events = _events(events_path)
    assert _names(events) == ["start", "item", "item", "summary", "done"]
    assert events[0]["queued"] == 2
    assert [(e["database"], e["outcome"], e["done"]) for e in events[1:3]] == [
        ("alpha", "ok", 1), ("bravo", "ok", 2)]
    assert events[3]["unique_records"] == 3
    assert events[-1]["ok"] is True


# ---------------------------------------------------------------------------
# import_to_zotero.py
# ---------------------------------------------------------------------------


def test_import_emits_progress(tmp_path, monkeypatch) -> None:
    import import_to_zotero as imp

    class Zot:
        def describe_library(self):
            return "group 1"

        def journal_articles(self):
            return []

        def create_items(self, batch):
            return {"success": {str(i): f"NEW{i}" for i in range(len(batch))},
                    "failed": {}}

    in_csv = tmp_path / "in.csv"
    with in_csv.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["doi", "title", "authors"])
        w.writeheader()
        w.writerow({"doi": "10.1/a", "title": "A", "authors": "Smith, J"})
        w.writerow({"doi": "10.1/b", "title": "B", "authors": "Doe, J"})
    monkeypatch.setattr(imp.zotero_io.ZoteroClient, "from_args",
                        classmethod(lambda cls, *a, **k: Zot()))
    monkeypatch.setattr(imp.screening_common, "load_tag_namespace", lambda *a, **k: "rev")
    monkeypatch.setattr(imp.time, "sleep", lambda _s: None)
    monkeypatch.setattr(imp, "build_item", lambda row, coll, **kw: (
        {"itemType": "journalArticle", "title": row["title"], "DOI": row["doi"]},
        imp.BUILD_SOURCE))
    events_path = tmp_path / "p.jsonl"
    monkeypatch.setattr(sys, "argv", ["import_to_zotero.py", "--input", str(in_csv),
                                      "--progress-json", str(events_path)])
    assert imp.main() == 0
    events = _events(events_path)
    assert _names(events) == ["start", "summary", "item", "summary", "done"]
    assert events[0]["records"] == 2
    assert events[1]["to_create"] == 2
    assert events[2]["outcome"] == "created" and events[2]["done"] == 2
    assert events[3]["created"] == 2


# ---------------------------------------------------------------------------
# enrich_abstracts.py
# ---------------------------------------------------------------------------


def test_enrich_abstracts_emits_progress(tmp_path, monkeypatch) -> None:
    import enrich_abstracts as ea

    items = [
        {"key": "AAAA1111", "data": {"DOI": "10.1/a", "title": "A", "abstractNote": ""}},
        {"key": "BBBB2222", "data": {"DOI": "10.1/b", "title": "B", "abstractNote": ""}},
    ]

    class Zot:
        def update_abstract(self, key, abstract):
            pass

    monkeypatch.setattr(ea, "_load_config", lambda: types.SimpleNamespace(crossref_mailto=""))
    monkeypatch.setattr(ea.http_client, "build_session", lambda *a, **k: object())
    monkeypatch.setattr(ea.zotero_io.ZoteroClient, "from_args",
                        classmethod(lambda cls, *a, **k: Zot()))
    monkeypatch.setattr(ea, "_select_items", lambda *a: items)
    monkeypatch.setattr(ea, "_excluded_sources", lambda flag: set())
    monkeypatch.setattr(ea.fetchers, "abstract_sources", lambda *a: [])
    monkeypatch.setattr(ea, "_choose_sources",
                        lambda *a: [types.SimpleNamespace(name="crossref")])
    monkeypatch.setattr(ea, "_try_cascade", lambda item, *a: (
        ea.CascadeResult(abstract="An abstract.", source="crossref")
        if item["key"] == "AAAA1111" else ea.CascadeResult(asked=["crossref"])))
    events_path = tmp_path / "p.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "enrich_abstracts.py", "--group", "1", "--workers", "1",
        "--log-csv", str(tmp_path / "log.csv"), "--cache-dir", str(tmp_path / "c"),
        "--progress-json", str(events_path),
    ])
    assert ea.main() == 0
    events = _events(events_path)
    assert _names(events) == ["start", "item", "item", "summary", "done"]
    assert events[0]["queued"] == 2
    assert {e["key"]: e["outcome"] for e in events[1:3]} == {
        "AAAA1111": "updated", "BBBB2222": "not_found"}
    assert events[3]["counts"]["updated"] == 1


# ---------------------------------------------------------------------------
# enrich_dois.py — progress, and the 2/3 question through --control-file
# ---------------------------------------------------------------------------


@pytest.fixture
def dois_main(tmp_path, monkeypatch):
    import enrich_dois as ed

    items = [
        {"key": "HASDOI11", "data": {"DOI": "10.1/x", "title": "Has DOI"}},
        {"key": "NODOI222", "data": {"DOI": "", "title": "Needs a DOI", "date": "2020",
                                     "creators": [{"creatorType": "author",
                                                   "lastName": "Smith"}]}},
    ]
    writes: list = []

    class Zot:
        def journal_articles(self):
            return items

        def update_fields(self, key, fields):
            writes.append((key, fields))

    cr = types.SimpleNamespace(author_surnames=["Smith"], issued_year="2020",
                               title="Needs a DOI")
    monkeypatch.setattr(ed.zotero_io.ZoteroClient, "from_args",
                        classmethod(lambda cls, *a, **k: Zot()))
    monkeypatch.setattr(ed, "_load_config", lambda: types.SimpleNamespace(crossref_mailto=""))
    monkeypatch.setitem(sys.modules, "habanero",
                        types.SimpleNamespace(Crossref=lambda **k: object()))
    monkeypatch.setattr(ed, "DoiResolverCache", lambda d: None)
    monkeypatch.setattr(ed, "_validate_doi",
                        lambda *a, **k: ed.ValidateResult(status="validate_ok"))
    monkeypatch.setattr(ed, "_crossref_search", lambda *a: [("10.1/found", cr)])
    monkeypatch.setattr(ed, "_match_score", lambda *a: (2, ["year"]))

    def run(*extra: str):
        events_path = tmp_path / "p.jsonl"
        monkeypatch.setattr(sys, "argv", [
            "enrich_dois.py", "--group", "1", "--log-csv", str(tmp_path / "log.csv"),
            "--cache-dir", str(tmp_path / "c"), "--progress-json", str(events_path),
            *extra,
        ])
        rc = ed.main()
        return rc, _events(events_path), writes

    return run


def test_enrich_dois_emits_progress(dois_main) -> None:
    rc, events, writes = dois_main()  # stdin is not a TTY under pytest
    assert rc == 0
    assert _names(events) == ["start", "item", "item", "summary", "done"]
    assert events[0]["validate"] == 1 and events[0]["find_missing"] == 1
    assert [(e["phase"], e["outcome"]) for e in events[1:3]] == [
        ("validate", "validate_ok"), ("find_missing", "proposed_not_applied")]
    assert writes == []


def _answer_when_asked(control: Path, answer: str) -> threading.Thread:
    def run():
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                state = json.loads(control.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            if state.get("state") == "awaiting_user":
                reply = control.with_name(control.name + ".reply")
                reply.write_text(json.dumps({"seq": state["seq"], "answer": answer}),
                                 encoding="utf-8")
                return
            time.sleep(0.05)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t


def test_enrich_dois_asks_through_the_control_file(dois_main, tmp_path) -> None:
    control = tmp_path / "control.json"
    answerer = _answer_when_asked(control, "y")
    rc, events, writes = dois_main("--control-file", str(control))
    answerer.join(5)
    assert rc == 0
    assert writes == [("NODOI222", {"DOI": "10.1/found"})]
    assert events[2]["outcome"] == "applied_after_prompt"
    assert json.loads(control.read_text(encoding="utf-8"))["state"] == "running"


def test_enrich_dois_no_prompt_wins_over_the_control_file(dois_main, tmp_path) -> None:
    control = tmp_path / "control.json"
    rc, events, writes = dois_main("--control-file", str(control), "--no-prompt")
    assert rc == 0 and writes == []
    assert events[2]["outcome"] == "proposed_not_applied"
    assert not control.exists()


# ---------------------------------------------------------------------------
# abstract_screen.py
# ---------------------------------------------------------------------------


def _coll_item(key: str) -> dict:
    return {"key": key, "data": {"key": key, "title": f"T {key}", "abstractNote": "a",
                                 "DOI": "", "tags": [], "itemType": "journalArticle"}}


class _ScreenZot:
    def __init__(self, items):
        self.items = items

    def describe_library(self):
        return "group 1"

    def library_ref(self):
        return "groups/1"

    def collection_items(self, coll, item_type=None):
        return self.items

    def all_attachments(self):
        return []


def test_abstract_screen_emits_progress(tmp_path, monkeypatch) -> None:
    import abstract_screen as asc

    items = [_coll_item("AAAA1111"), _coll_item("BBBB2222")]
    monkeypatch.setattr(asc, "_load_screening_config",
                        lambda p: ("prompt", "m", "v1", types.SimpleNamespace()))
    monkeypatch.setattr(asc.tag_prefix, "resolve", lambda *a: "rev")
    monkeypatch.setattr(asc, "effective_model", lambda *a, **k: "test-model")
    monkeypatch.setattr(asc.llm_provider, "require_credentials", lambda m: None)
    monkeypatch.setattr(asc.llm_provider, "get_provider", lambda m: object())
    monkeypatch.setattr(asc, "paid_run_banner", lambda *a, **k: "")
    monkeypatch.setattr(asc.zotero_io.ZoteroClient, "from_args",
                        classmethod(lambda cls, *a, **k: _ScreenZot(items)))
    monkeypatch.setattr(asc, "_flush_tag_buffer", lambda zot, buf: buf.clear())
    monkeypatch.setattr(asc, "screen_item", lambda item, **k: asc.ScreenResult(
        key=item["key"], query="", decision="include", reason="fits"))
    events_path = tmp_path / "p.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "abstract_screen.py", "--collection", "COLL1234", "--group", "1",
        "--skip-preflight", "--workers", "1",
        "--output", str(tmp_path / "screen.csv"), "--progress-json", str(events_path),
    ])
    assert asc.main() == 0
    events = _events(events_path)
    assert _names(events) == ["start", "item", "item", "summary", "done"]
    assert events[0]["queued"] == 2 and events[0]["model"] == "test-model"
    assert {e["outcome"] for e in events[1:3]} == {"include"}
    assert events[3]["counts"]["include"] == 2


# ---------------------------------------------------------------------------
# fulltext_code.py
# ---------------------------------------------------------------------------


def test_fulltext_code_emits_progress(tmp_path, monkeypatch) -> None:
    import fulltext_code as ftc

    items = [_coll_item("AAAA1111"), _coll_item("BBBB2222")]
    fields = [{"name": "design", "description": "Research design."}]
    monkeypatch.setattr(ftc, "_load_screening_config",
                        lambda p: ("prompt", fields, "m", "v1", types.SimpleNamespace()))
    monkeypatch.setattr(ftc, "_render_prompt", lambda t, f: "prompt")
    monkeypatch.setattr(ftc.tag_prefix, "resolve", lambda *a: "rev")
    monkeypatch.setattr(ftc, "effective_model", lambda *a, **k: "test-model")
    monkeypatch.setattr(ftc.llm_provider, "require_credentials", lambda m: None)
    monkeypatch.setattr(ftc.llm_provider, "get_provider", lambda m: object())
    monkeypatch.setattr(ftc, "paid_run_banner", lambda *a, **k: "")
    monkeypatch.setattr(ftc.zotero_io.ZoteroClient, "from_args",
                        classmethod(lambda cls, *a, **k: _ScreenZot(items)))
    monkeypatch.setattr(ftc, "_abstract_stage_eligible", lambda its, p: (its, "eligible"))
    monkeypatch.setattr(ftc, "_find_pdf_path", lambda item, *a, **k: tmp_path / "x.pdf")
    monkeypatch.setattr(ftc, "_code_one", lambda item, *a: {
        "item_key": item["key"], "title": item["data"]["title"],
        "decision": "exclude" if item["key"] == "BBBB2222" else "include"})
    monkeypatch.setattr(ftc, "apply_coded_row", lambda *a, **k: None)
    events_path = tmp_path / "p.jsonl"
    monkeypatch.setattr(sys, "argv", [
        "fulltext_code.py", "--collection", "COLL1234", "--group", "1",
        "--skip-preflight", "--workers", "1", "--zotero-storage", str(tmp_path),
        "--output", str(tmp_path / "ft.csv"), "--progress-json", str(events_path),
    ])
    assert ftc.main() == 0
    events = _events(events_path)
    assert _names(events) == ["start", "item", "item", "summary", "done"]
    assert events[0]["queued"] == 2 and events[0]["mode"] == "code"
    assert {e["key"]: e["outcome"] for e in events[1:3]} == {
        "AAAA1111": "include", "BBBB2222": "exclude"}
    assert events[3]["counts"]["include"] == 1


def test_without_the_flag_no_sink_is_opened(monkeypatch) -> None:
    """Terminal runs are unchanged: no flag, no sink, nothing written."""
    import search

    opened: list = []
    monkeypatch.setattr(progress_json, "configure",
                        lambda path: opened.append(path))
    monkeypatch.setattr(sys, "argv", ["search.py", "--streams", "nope"])
    with pytest.raises(SystemExit):
        search.main()
    assert opened == [""]
    assert not progress_json.active()
