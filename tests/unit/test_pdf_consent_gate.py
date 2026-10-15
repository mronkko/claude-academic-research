"""The browser pass's consent gate (plan `pdf-retrieval.md` §4, step 5).

Modes 1 and 2 — a browser handler, the Zotero Connector — ask once per
site whether automated download may go ahead, and only a person at a
terminal can answer. `--no-prompt`, `--control-file` and unattended runs
skip any site without a recorded `allow` and log CONSENT_NEEDED. A
`deny` sends the site's items to the manual queue. Publisher APIs, OA
sources and manual download never ask.

Nothing here opens a browser, touches Zotero or the network: the drivers
are stubbed and `habanero` is a fake module. The suite fixture points
HOME at a scratch directory, so recorded answers land in a scratch
config.toml.
"""

from __future__ import annotations

import csv
import datetime as dt
import inspect
import sys
from types import SimpleNamespace

import enrich_pdfs
import manual_pdfs
import pdf_fetch_log
import pytest
from core import publisher_consent as pc
from fetchers.browser import all_handlers, consent, interaction
from fetchers.resolvers import FulltextTarget

# ---------------------------------------------------------------------------
# Site keys
# ---------------------------------------------------------------------------

ALMA = "https://eu03.alma.exlibrisgroup.com/view/action/uresolver.do?x=1"


@pytest.mark.parametrize(("target", "url", "landing", "site"), [
    (None, "https://journals.sagepub.com/doi/10.1177/1", "", "sage"),
    (None, "https://www-jstor-org.ezproxy.jyu.fi/stable/1", "", "jstor"),
    (None, "https://login.ezproxy.jyu.fi/login?url=https://www.jstor.org/stable/1",
     "", "jstor"),
    (None, "https://research-ebsco-com.ezproxy.jyu.fi/c/x", "", "ebsco"),
    (FulltextTarget(url=ALMA, interface_name="Wiley Online Library"), ALMA, "", "wiley"),
    (FulltextTarget(url=ALMA, interface_name="EBSCOhost Business Source"), ALMA, "",
     "ebsco"),
    (FulltextTarget(url=ALMA, interface_name="Gale Academic OneFile"), ALMA, "",
     "gale-academic-onefile"),
    (None, "https://doi.org/10.1515/x", "www.degruyter.com", "degruyter.com"),
    (None, "https://doi.org/10.1177/x", "journals.sagepub.com", "sage"),
    (None, "https://doi.org/10.9999/x", "", "doi.org"),
    (None, "https://www.example.co.uk/article/1", "", "example.co.uk"),
])
def test_site_for_target(target, url, landing, site) -> None:
    got = consent.site_for_target(target, url, landing_host=landing)
    assert got == site
    pc.normalise_site(got)          # every key the gate can produce is storable


def test_every_handler_is_its_own_site_with_a_terms_attribute() -> None:
    """The terms link sits beside each handler; an empty one is an
    unverified URL (TODO in the handler), never a guessed one."""
    for h in all_handlers():
        assert isinstance(h.terms_url, str)
        assert h.terms_url == "" or h.terms_url.startswith("https://"), h.name
        info = consent.site_info(h.name)
        assert info.display == (h.display_name or h.name)
        assert info.terms_url or info.homepage.startswith("https://"), h.name
    assert consent.site_info("ebsco").terms_url
    assert consent.site_info("jstor").terms_url
    assert {s.site for s in consent.known_sites()} >= {"sage", "ebsco", "jstor"}


def test_prompt_shows_publisher_mode_count_and_link_and_no_summary() -> None:
    text = consent.prompt_text(consent.site_info("jstor"),
                               mode=consent.MODE_CONNECTOR, items=12)
    assert "JSTOR" in text and "Zotero Connector" in text and "12 items" in text
    assert "https://about.jstor.org/terms/" in text
    unverified = consent.prompt_text(consent.site_info("aom"),
                                     mode=consent.MODE_BROWSER, items=1)
    assert "no link recorded" in unverified and "https://journals.aom.org/" in unverified


@pytest.mark.parametrize(("text", "answer"), [
    ("a", "allow"), ("Allow", "allow"), ("o", "once"), ("d", "deny"),
    ("", "skip"), ("y", "skip"), ("yes", "skip"),
])
def test_parse_answer_never_reads_a_non_answer_as_yes(text, answer) -> None:
    assert consent.parse_answer(text) == answer


# ---------------------------------------------------------------------------
# The gate
# ---------------------------------------------------------------------------

def _gate(stored: dict[str, str], *, can_ask: bool, answers=()):
    asked: list[str] = []
    recorded: list[tuple[str, str]] = []
    queue = list(answers)

    def ask(prompt: str) -> str:
        asked.append(prompt)
        return queue.pop(0)

    gate = consent.ConsentGate(
        can_ask=can_ask, ask=ask,
        lookup=lambda s: pc.Consent(site=s, automated=stored.get(s, "ask"),
                                    stored=s in stored),
        record=lambda s, a: recorded.append((s, a)),
        out=lambda s: None,
    )
    return gate, asked, recorded


@pytest.mark.parametrize(("stored", "decision"), [
    ("allow", consent.ALLOW), ("deny", consent.DENY), ("ask", consent.NEEDED),
])
def test_without_a_person_only_a_recorded_allow_proceeds(stored, decision) -> None:
    gate, asked, recorded = _gate({"sage": stored}, can_ask=False)
    assert gate.decide("sage", mode=consent.MODE_BROWSER, items=3) == decision
    assert asked == [] and recorded == []


def test_a_missing_entry_without_a_person_is_needed() -> None:
    gate, asked, _ = _gate({}, can_ask=False)
    assert gate.decide("jstor", mode=consent.MODE_CONNECTOR, items=1) == consent.NEEDED
    assert asked == []


@pytest.mark.parametrize(("answer", "decision", "recorded"), [
    ("a", consent.ALLOW, [("sage", "allow")]),
    ("d", consent.DENY, [("sage", "deny")]),
    ("o", consent.ALLOW, []),
    ("", consent.NEEDED, []),
])
def test_interactive_answers(answer, decision, recorded) -> None:
    gate, asked, got = _gate({}, can_ask=True, answers=[answer])
    assert gate.decide("sage", mode=consent.MODE_BROWSER, items=4) == decision
    assert len(asked) == 1 and got == recorded


def test_asked_once_per_site_never_per_item() -> None:
    gate, asked, _ = _gate({}, can_ask=True, answers=["o"])
    for _ in range(3):
        assert gate.decide("sage", mode=consent.MODE_BROWSER, items=1) == consent.ALLOW
    assert gate.decide("sage", mode=consent.MODE_CONNECTOR, items=9) == consent.ALLOW
    assert len(asked) == 1


def test_a_recorded_answer_is_not_asked_again_even_when_a_person_is_there() -> None:
    gate, asked, _ = _gate({"sage": "allow", "tandf": "deny"}, can_ask=True)
    assert gate.decide("sage", mode=consent.MODE_BROWSER, items=1) == consent.ALLOW
    assert gate.decide("tandf", mode=consent.MODE_BROWSER, items=1) == consent.DENY
    assert asked == []


def test_the_interactive_answer_is_recorded_with_the_date() -> None:
    gate = consent.ConsentGate(can_ask=True, ask=lambda p: "a", out=lambda s: None)
    assert gate.decide("sage", mode=consent.MODE_BROWSER, items=2) == consent.ALLOW
    stored = pc.lookup("sage")
    assert stored.automated == pc.ALLOW and stored.confirmed == dt.date.today()


def test_no_ask_callable_means_no_question() -> None:
    gate = consent.ConsentGate(can_ask=True, ask=None)
    assert gate.decide("sage", mode=consent.MODE_BROWSER, items=1) == consent.NEEDED


# ---------------------------------------------------------------------------
# Which runs may ask
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("flags", [
    {"no_prompt": True}, {"control_file": ".claude/pdf/browser.json"},
    {"unattended": True}, {"plan": True},
])
def test_no_prompt_agent_and_unattended_runs_never_ask(monkeypatch, flags) -> None:
    monkeypatch.setattr(enrich_pdfs, "_has_interactive_surface", lambda: True)
    assert enrich_pdfs._consent_can_ask(SimpleNamespace(**flags)) is False


def test_a_terminal_run_asks(monkeypatch) -> None:
    monkeypatch.setattr(enrich_pdfs, "_has_interactive_surface", lambda: True)
    assert enrich_pdfs._consent_can_ask(SimpleNamespace()) is True
    monkeypatch.setattr(enrich_pdfs, "_has_interactive_surface", lambda: False)
    assert enrich_pdfs._consent_can_ask(SimpleNamespace()) is False


def test_api_oa_and_manual_paths_never_reach_the_gate() -> None:
    """Consent is for modes 1 and 2 only. The gate is consulted in the
    browser pass and nowhere else: not the API cascade (publisher APIs and
    OA sources are publisher-sanctioned), not cache recovery, not the
    manual mode (the user clicks download)."""
    src = inspect.getsource(enrich_pdfs)
    callers = {
        name for name, fn in inspect.getmembers(enrich_pdfs, inspect.isfunction)
        if fn.__module__ == "enrich_pdfs"
        and ("_consent_filter(" in inspect.getsource(fn)
             or "_consent_gate(" in inspect.getsource(fn))
        and name not in ("_consent_filter", "_consent_gate")
    }
    assert callers == {"_run_browser_in_process"}
    manual_src = inspect.getsource(manual_pdfs)
    assert "publisher_consent" not in manual_src and "ConsentGate" not in manual_src
    assert src.count("ConsentGate(") == 1


# ---------------------------------------------------------------------------
# The browser pass, end to end with stub drivers
# ---------------------------------------------------------------------------

SAGE_DOI = "10.1177/0001"
OTHER_DOI = "10.9999/0002"


def _item(key: str, doi: str) -> dict:
    return {"key": key, "data": {"DOI": doi, "title": f"T {key}",
                                 "itemType": "journalArticle"}}


@pytest.fixture
def browser_pass(tmp_path, monkeypatch):
    monkeypatch.setitem(sys.modules, "habanero",
                        SimpleNamespace(Crossref=lambda **k: None))
    from fetchers import doi_resolver
    monkeypatch.setattr(doi_resolver, "resolve_doi", lambda *a, **k: None)
    monkeypatch.setattr(enrich_pdfs, "_resolver_config", lambda args: None)
    monkeypatch.setattr(enrich_pdfs, "_print_browser_summary", lambda *a, **k: None)
    driven: dict[str, list[str]] = {}

    async def drive(handler, items, *a, **k):
        driven.setdefault(handler.name, []).extend(it["item_key"] for it in items)

    async def drive_connector(handler, items, *a, **k):
        driven.setdefault("connector", []).extend(it["item_key"] for it in items)

    monkeypatch.setattr(enrich_pdfs, "_drive_handler", drive)
    monkeypatch.setattr(enrich_pdfs, "_drive_connector", drive_connector)
    previous = interaction.get_channel()
    pdf_fetch_log.set_run_network("Aalto")

    def run(*flags, surface=False, answers=()):
        monkeypatch.setattr(enrich_pdfs, "_has_interactive_surface", lambda: surface)
        queue = list(answers)
        asked: list[str] = []

        class Fake(interaction.InteractionChannel):
            def wait_for_user(self, prompt):
                pass

            def read_line(self, prompt):
                asked.append(prompt)
                return queue.pop(0) if queue else ""

        interaction.set_channel(Fake())
        args = enrich_pdfs._build_parser().parse_args([
            "--sources", "browser", "--cache-dir", str(tmp_path / "cache"),
            "--failure-log-csv", str(tmp_path / "fail.csv"), *flags,
        ])
        log = tmp_path / "log.csv"
        fh, writer = enrich_pdfs._open_log(str(log))
        try:
            rc = enrich_pdfs._run_browser_in_process(
                [_item("S1", SAGE_DOI), _item("S2", "10.1177/0002"),
                 _item("X1", OTHER_DOI)],
                SimpleNamespace(), writer, args, "2026-10-15",
            )
        finally:
            fh.close()
        with log.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        fails = pdf_fetch_log.read_failures(tmp_path / "fail.csv")
        return SimpleNamespace(rc=rc, rows=rows, fails=fails, asked=asked)

    yield SimpleNamespace(run=run, driven=driven)
    interaction.set_channel(previous)
    pdf_fetch_log.set_run_network("")


def test_no_prompt_run_skips_unconsented_sites_as_consent_needed(browser_pass) -> None:
    out = browser_pass.run("--no-prompt")
    assert out.rc == 0 and out.asked == []
    assert browser_pass.driven == {}
    causes = {(r["item_key"], r["source"]): r["cause"] for r in out.fails}
    assert causes[("S1", "sage")] == "CONSENT_NEEDED"
    assert causes[("X1", "connector")] == "CONSENT_NEEDED"   # doi.org site
    assert {r["status"] for r in out.rows} == {"skipped_consent_needed"}
    assert {r["network"] for r in out.fails} == {"Aalto"}
    assert pc.read_all() == {}                                # nothing recorded


def test_agent_run_with_a_control_file_does_not_ask(browser_pass, tmp_path) -> None:
    out = browser_pass.run("--control-file", str(tmp_path / "c.json"), surface=True)
    assert out.asked == [] and browser_pass.driven == {}
    assert {r["cause"] for r in out.fails} == {"CONSENT_NEEDED"}


def test_recorded_allow_drives_the_handler_without_asking(browser_pass) -> None:
    pc.record("sage", "allow")
    pc.record("doi.org", "allow")
    out = browser_pass.run("--no-prompt")
    assert out.asked == []
    assert browser_pass.driven == {"sage": ["S1", "S2"], "connector": ["X1"]}
    assert out.fails == []


def test_deny_sends_items_to_manual_not_to_the_connector(browser_pass) -> None:
    pc.record("sage", "deny")
    pc.record("doi.org", "allow")
    out = browser_pass.run("--no-prompt")
    assert browser_pass.driven == {"connector": ["X1"]}
    rows = {r["item_key"]: r for r in out.rows}
    assert rows["S1"]["status"] == "skipped_consent_denied"
    assert {r["cause"] for r in out.fails if r["item_key"].startswith("S")} == {
        "CONSENT_DENIED"}


def test_terminal_run_asks_once_per_site_and_records_the_answer(browser_pass) -> None:
    out = browser_pass.run(surface=True, answers=["a", ""])
    assert len(out.asked) == 2                     # sage, then doi.org — not per item
    assert "Sage" in out.asked[0] and "2 items" in out.asked[0]
    assert browser_pass.driven == {"sage": ["S1", "S2"]}
    assert pc.lookup("sage").automated == "allow"
    assert pc.lookup("sage").confirmed == dt.date.today()
    assert not pc.lookup("doi.org").stored         # Enter: not now, nothing stored
    assert {(r["item_key"], r["cause"]) for r in out.fails} == {
        ("X1", "CONSENT_NEEDED")}


def test_plan_lists_consent_and_asks_nothing(browser_pass, capsys) -> None:
    pc.record("sage", "deny")
    out = browser_pass.run("--plan", surface=True)
    assert out.asked == [] and browser_pass.driven == {}
    assert "[consent: deny → manual]" in capsys.readouterr().out
    assert out.fails == []


def test_network_label_names_the_active_library(monkeypatch) -> None:
    from fetchers.library_resolver import LibraryResolverConfig
    from fetchers.resolvers import AlmaResolver, SfxResolver
    alma = AlmaResolver(
        "https://eu03.alma.exlibrisgroup.com/view/uresolver/358AALTO_INST/openurl")
    sfx = SfxResolver("https://sfx.finna.fi/jyu")
    cfg = LibraryResolverConfig(resolver=alma, additional_resolvers=(sfx,),
                                session=None, active_ids=frozenset({sfx.resolver_id}))
    monkeypatch.setattr(enrich_pdfs, "_resolver_config", lambda a: cfg)
    assert enrich_pdfs._network_label(SimpleNamespace()) == "Jyu"
    cfg.active_ids = None
    assert enrich_pdfs._network_label(SimpleNamespace()) == "Aalto+Jyu"
    monkeypatch.setattr(enrich_pdfs, "_resolver_config", lambda a: None)
    assert enrich_pdfs._network_label(SimpleNamespace()) == ""
