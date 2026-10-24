"""The PDF retrieval planning step (`enrich_pdfs.py --plan-strategy`,
`pdf_plan.py`).

No network and no real Zotero: the library is a fake client, the
link resolver and OpenAlex are stubs, and every test runs in a tmp
project directory. What is pinned: how each item is classified, that
the fast path asks nothing of the network, that paid reachability is
either exact or reported as unknown (never guessed), the JSON shape the
skill reads, and that planning never writes to Zotero.
"""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import enrich_pdfs
import pdf_plan as pp
import pytest
from fetchers.library_resolver import TargetLookup

# ---------------------------------------------------------------------------
# Classification (pure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("facts", "route", "fallback"), [
    (dict(doi=""), pp.NO_DOI, pp.NO_DOI),
    (dict(publisher_api="sciencedirect"), pp.API, pp.CONNECTOR),
    (dict(open_access=True), pp.API, pp.CONNECTOR),
    (dict(), pp.API_UNTRIED, pp.CONNECTOR),
    (dict(api_tried=True, paid=True), pp.PAID, pp.CONNECTOR),
    (dict(api_tried=True, handler="sage"), pp.BROWSER, pp.BROWSER),
    (dict(api_tried=True, handler="sage", direct_case="3-in-coverage"),
     pp.BROWSER, pp.BROWSER),
    # 1b: the resolver names a licensed route, not via that publisher.
    (dict(api_tried=True, handler="aom", direct_case="1b-no-entitlement",
          resolver=pp.R_EBSCO), pp.EBSCO, pp.EBSCO),
    (dict(api_tried=True, handler="sage", direct_case="4-other-library"),
     pp.OTHER_LIBRARY, pp.OTHER_LIBRARY),
    (dict(api_tried=True, resolver=pp.R_EBSCO), pp.EBSCO, pp.EBSCO),
    (dict(api_tried=True, resolver=pp.R_ROUTE), pp.CONNECTOR, pp.CONNECTOR),
    # Silence is not "no access": the Connector fails open to doi.org.
    (dict(api_tried=True, resolver=pp.R_UNKNOWN), pp.CONNECTOR, pp.CONNECTOR),
    (dict(api_tried=True, resolver=pp.R_UNCONFIGURED), pp.CONNECTOR, pp.CONNECTOR),
    (dict(api_tried=True, resolver=pp.R_DEFERRED), pp.OTHER_LIBRARY, pp.OTHER_LIBRARY),
    (dict(api_tried=True, resolver=pp.R_NONE), pp.MANUAL, pp.MANUAL),
    # The browser pass already answered: a person is the next step.
    (dict(api_tried=True, handler="sage", browser_tried=True), pp.MANUAL, pp.MANUAL),
    # Paid is checked only after the free cascade has run.
    (dict(paid=True), pp.API_UNTRIED, pp.CONNECTOR),
])
def test_route_and_fallback(facts, route, fallback) -> None:
    f = pp.ItemFacts(key="K", **{"doi": "10.1/x", **facts})
    assert pp.route_for(f) == route
    assert pp.fallback_route(f) == fallback


def test_every_route_has_a_label_and_a_mode() -> None:
    assert set(pp.ROUTES) == set(pp.ROUTE_LABELS) == set(pp.ROUTE_MODE)
    import pdf_strategy
    assert {m for m in pp.ROUTE_MODE.values() if m} <= set(pdf_strategy.MODES)


def test_openalex_query_carries_no_api_key() -> None:
    """The plan must not be able to spend: no key, no download."""
    params = pp.openalex_params(["10.1/a", "10.1/b"], mailto="me@x.org")
    assert "api_key" not in params and "api-key" not in params
    assert params["filter"] == "doi:10.1/a|10.1/b"
    assert params["select"] == "doi,has_content,locations"


def test_openalex_batches_drop_dois_that_would_split_the_filter() -> None:
    batches = pp.openalex_batches(["10.1/a|b", "10.1/c,d"] + [f"10.1/{i}" for i in range(120)])
    assert [len(b) for b in batches] == [50, 50, 20]
    assert not any("|" in d or "," in d for b in batches for d in b)


def test_oa_and_paid_applies_the_policy_gate() -> None:
    work = {"has_content": {"pdf": True}, "locations": [
        {"pdf_url": "https://repo.example/x.pdf", "is_oa": True, "version": "submittedVersion"},
        {"pdf_url": "", "is_oa": True},
    ]}
    assert pp.oa_and_paid(work, lambda loc, url: True) == (True, True)
    assert pp.oa_and_paid(work, lambda loc, url: False) == (False, True)
    assert pp.oa_and_paid({}, lambda loc, url: True) == (False, False)


# ---------------------------------------------------------------------------
# Aggregation and outputs
# ---------------------------------------------------------------------------

POLICY = {"min_version": "accepted", "reconstructed": False, "paid": "prefer",
          "sources": {"min_version": "default", "paid": "default"}, "stored": False}


def _plan(facts, **kw) -> pp.Plan:
    return pp.Plan(library="group:1", lookups=kw.pop("lookups", "none"),
                   policy=POLICY, scope_items=len(facts) + 3, with_pdf=3,
                   facts=facts, **kw)


def test_unknown_paid_is_a_ceiling_not_a_count() -> None:
    facts = [pp.ItemFacts(key=f"K{i}", doi=f"10.1/{i}") for i in range(4)]
    s = pp.summarise(_plan(facts))
    paid = s["paid_openalex"]
    assert paid["known"] is False
    assert paid["items"] == 0 and paid["unchecked"] == 4
    assert paid["cost_ceiling_usd"] == 0.04
    assert "Not looked up" in paid["note"]


def test_known_paid_is_exact_with_its_cost() -> None:
    facts = [pp.ItemFacts(key="A", doi="10.1/a", paid=True, open_access=False,
                          api_tried=True),
             pp.ItemFacts(key="B", doi="10.1/b", paid=False, open_access=True),
             pp.ItemFacts(key="C", doi="10.1/c", paid=True, open_access=False)]
    s = pp.summarise(_plan(facts, paid_known=True, lookups="openalex"))
    paid = s["paid_openalex"]
    assert paid == {"known": True, "policy": "prefer", "items": 2,
                    "cost_usd": 0.02, "items_api_already_tried": 1}
    assert s["routes"] == {pp.API: 1, pp.API_UNTRIED: 1, pp.PAID: 1}
    assert s["signals"]["open_access"] == 1


def test_json_shape_the_skill_reads(tmp_path) -> None:
    facts = [
        pp.ItemFacts(key="A", doi="10.1177/a", api_tried=True, handler="sage",
                     handler_display="Sage", handler_needs_solve=True,
                     audit_sets=("retry.browser",)),
        pp.ItemFacts(key="B", doi="10.1/b", api_tried=True, resolver=pp.R_NONE,
                     below_policy=True, audit_sets=("retry.below_policy",)),
        pp.ItemFacts(key="C", doi=""),
    ]
    summary, path = pp.write_outputs(_plan(facts), tmp_path)
    data = json.loads(path.read_text(encoding="utf-8"))
    for key in ("version", "generated_at", "library", "lookups", "policy", "scope",
                "routes", "fallback_routes", "browser_publishers", "signals",
                "paid_openalex", "below_policy", "audit", "manual_queue",
                "unknown", "credentials", "resolver_configured", "keys_files",
                "items"):
        assert key in data, key
    assert data["scope"] == {"items": 6, "with_pdf": 3, "queue": 3,
                             "with_doi": 2, "no_doi": 1}
    assert data["manual_queue"] == 2
    assert data["below_policy"] == 1
    assert data["audit"] == {"retry.below_policy": 1, "retry.browser": 1}
    assert data["browser_publishers"] == {
        "sage": {"display": "Sage", "items": 1, "needs_solve": True,
                 "consent": "ask", "confirmed": "", "terms_url": ""}}
    assert data["routes"] == {pp.BROWSER: 1, pp.MANUAL: 1, pp.NO_DOI: 1}
    rows = {r["key"]: r for r in data["items"]}
    assert rows["A"]["route"] == pp.BROWSER and rows["A"]["handler"] == "sage"
    # Keys files, one per route, feed --filter-keys-file.
    assert (tmp_path / data["keys_files"][pp.BROWSER]).read_text() == "A\n"
    assert "items" not in summary


def test_a_route_that_emptied_loses_its_old_keys_file(tmp_path) -> None:
    pp.write_outputs(_plan([pp.ItemFacts(key="A", doi="10.1/a")]), tmp_path)
    stale = tmp_path / ".claude/pdf/plan.api_untried.keys"
    assert stale.exists()
    pp.write_outputs(_plan([pp.ItemFacts(key="A", doi="10.1/a", api_tried=True,
                                         resolver=pp.R_NONE)]), tmp_path)
    assert not stale.exists()
    assert (tmp_path / ".claude/pdf/plan.manual.keys").read_text() == "A\n"


def test_summary_ends_with_the_parseable_line() -> None:
    facts = [pp.ItemFacts(key="A", doi="10.1/a")]
    text = pp.format_summary(pp.summarise(_plan(facts)))
    last = text.splitlines()[-1]
    assert last.startswith("PLAN: ")
    fields = dict(p.split("=", 1) for p in last[len("PLAN: "):].split())
    assert fields["queue"] == "1" and fields["route_api_untried"] == "1"
    assert fields["paid_known"] == "0"
    assert "a ceiling" in text


# ---------------------------------------------------------------------------
# End to end through main()
# ---------------------------------------------------------------------------

ITEMS = [
    # Elsevier prefix, key configured, never tried: API-likely.
    ("ELS00001", "10.1016/j.jbusvent.2019.01.001", "2019"),
    # Sage, tried by the cascade: browser handler.
    ("SAGE0001", "10.1177/1042258719890991", "2020"),
    # Unknown publisher, tried: resolver decides.
    ("UNKN0001", "10.9999/zzz.1", "2018"),
    # No DOI.
    ("NODOI001", "", "2017"),
    # Already has a PDF: not in the queue.
    ("HASPDF01", "10.1016/j.x.2020.1", "2020"),
]


class _Zot:
    library_type = "group"
    group_id = 77

    def __init__(self) -> None:
        self.writes: list = []

    def journal_articles(self):
        return [{"key": k, "data": {"DOI": d, "title": f"Title {k}",
                                    "itemType": "journalArticle", "date": y}}
                for k, d, y in ITEMS]

    def pdf_map(self):
        return {"HASPDF01": (True, [])}

    def __getattr__(self, name):
        if name in {"attach_pdf", "trash_item", "delete_item", "update_tags",
                    "merge_duplicate_item", "update_item", "add_tags"}:
            def _w(*a, **k):
                self.writes.append(name)
            return _w
        raise AttributeError(name)


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    cache = tmp_path / "cache"
    cache.mkdir()
    log = tmp_path / "log.csv"
    log.write_text(
        "run_date,item_key,doi,title,status,source,detail\n"
        "2026-10-01,SAGE0001,10.1177/x,T,no_pdf,api_cascade,\n"
        "2026-10-01,UNKN0001,10.9999/zzz.1,T,no_pdf,api_cascade,\n",
        encoding="utf-8")
    audit = tmp_path / ".claude" / "audit"
    audit.mkdir(parents=True)
    (audit / "audit.retry.browser.keys").write_text("SAGE0001\n", encoding="utf-8")
    (audit / "audit.retry.browser.sage.keys").write_text("SAGE0001\n", encoding="utf-8")
    config = SimpleNamespace(
        crossref_mailto="me@example.org", elsevier_api_key="k", wiley_tdm_token="",
        openalex_api_key="secret-key", semantic_scholar_api_key="", core_api_key="",
        elsevier_render_xml_to_pdf=False, openalex_use_paid_content_api=True)
    monkeypatch.setattr(enrich_pdfs, "_load_config", lambda: config)
    monkeypatch.setattr(enrich_pdfs, "_library_handler_set", lambda key: set())
    state = SimpleNamespace(resolver=None, net=[], calls=[])

    def _no_network(*a, **k):
        state.net.append((a, k))
        raise AssertionError("the planner made a network call it should not")

    monkeypatch.setattr(enrich_pdfs.http_client, "get_json", _no_network)
    monkeypatch.setattr(enrich_pdfs, "_resolver_config", lambda args: state.resolver)

    def run(*flags, zot=None):
        zot = zot or _Zot()
        state.zot = zot
        monkeypatch.setattr(enrich_pdfs.zotero_io.ZoteroClient, "from_args",
                            classmethod(lambda cls, a: zot))
        monkeypatch.setattr(sys, "argv", [
            "enrich_pdfs.py", "--group", "77", "--cache-dir", str(cache),
            "--log-csv", str(log), "--failure-log-csv", str(tmp_path / "fail.csv"),
            "--plan-strategy", *flags])
        return enrich_pdfs.main()

    state.run = run
    state.plan = lambda: json.loads(
        (tmp_path / ".claude/pdf/plan.json").read_text(encoding="utf-8"))
    return state


def test_fast_path_asks_nothing_and_writes_nothing_to_zotero(rig, capsys) -> None:
    assert rig.run() == 0
    assert rig.net == []
    assert rig.zot.writes == []
    plan = rig.plan()
    rows = {r["key"]: r for r in plan["items"]}
    assert "HASPDF01" not in rows
    assert rows["ELS00001"]["route"] == pp.API
    assert rows["ELS00001"]["publisher_api"] == "sciencedirect"
    assert rows["SAGE0001"]["route"] == pp.BROWSER
    assert rows["SAGE0001"]["audit_sets"] == ["retry.browser"]
    assert rows["UNKN0001"]["route"] == pp.CONNECTOR       # no resolver configured
    assert rows["NODOI001"]["route"] == pp.NO_DOI
    assert plan["scope"] == {"items": 5, "with_pdf": 1, "queue": 4,
                             "with_doi": 3, "no_doi": 1}
    assert plan["paid_openalex"]["known"] is False
    assert plan["unknown"]["openalex_unchecked"] == 3
    assert plan["library"] == "group:77"
    assert plan["credentials"] == {"elsevier": True, "wiley": False,
                                   "openalex_key": True}
    out = capsys.readouterr().out
    assert "caches and logs only — no network" in out
    assert out.strip().splitlines()[-2].startswith("PLAN: ")


def test_openalex_lookup_is_keyless_and_exact(rig, monkeypatch) -> None:
    seen = []

    def _get_json(session, url, *, params=None, **kw):
        seen.append((url, params))
        return {"results": [
            {"doi": "https://doi.org/10.9999/zzz.1", "has_content": {"pdf": True},
             "locations": []},
            {"doi": "https://doi.org/10.1177/1042258719890991",
             "has_content": {"pdf": False},
             "locations": [{"pdf_url": "https://journals.sagepub.com/x.pdf",
                            "is_oa": True, "version": "publishedVersion",
                            "source": {"type": "journal"}}]},
        ]}

    monkeypatch.setattr(enrich_pdfs.http_client, "get_json", _get_json)
    assert rig.run("--plan-lookups", "openalex") == 0
    [(url, params)] = seen
    assert url == "https://api.openalex.org/works"
    assert "secret-key" not in json.dumps(params)
    plan = rig.plan()
    rows = {r["key"]: r for r in plan["items"]}
    assert rows["UNKN0001"]["paid"] is True
    assert rows["UNKN0001"]["route"] == pp.PAID
    assert rows["SAGE0001"]["open_access"] is True
    # Answered but absent from the results: OpenAlex does not hold it.
    assert "paid" not in rows["ELS00001"]
    assert plan["paid_openalex"] == {
        "known": True, "policy": "prefer", "items": 1, "cost_usd": 0.01,
        "items_api_already_tried": 1}
    assert "openalex_unchecked" not in plan["unknown"]


def test_a_refused_openalex_query_leaves_paid_unknown(rig, monkeypatch) -> None:
    monkeypatch.setattr(enrich_pdfs.http_client, "get_json",
                        lambda *a, **k: None)  # a 4xx
    assert rig.run("--plan-lookups", "openalex") == 0
    plan = rig.plan()
    assert plan["paid_openalex"]["known"] is False
    assert plan["unknown"]["openalex_unchecked"] == 3


class _Resolver:
    """A configured resolver whose answers come from a dict."""

    def __init__(self, answers, cached=()):
        self.answers = answers
        self.cached = set(cached)
        self.cache = object()


def _wire_resolver(rig, monkeypatch, resolver):
    rig.resolver = resolver
    monkeypatch.setattr(
        enrich_pdfs, "_resolver_keys_cached",
        lambda doi, cfg, *, dual: doi in cfg.cached)

    def _pass3(entry, cfg, *, ignore_coverage=False):
        rig.calls.append(entry["doi"])
        return cfg.answers.get(entry["doi"], TargetLookup(None, False))

    monkeypatch.setattr(enrich_pdfs, "_pass3_target", _pass3)
    monkeypatch.setattr("fetchers.library_resolver.lookup_dual",
                        lambda *a, **k: SimpleNamespace(
                            in_range=[], any_range=[], query_ok=True))
    monkeypatch.setattr(enrich_pdfs, "classify_direct_route",
                        lambda *a, **k: "3-in-coverage")


def test_cached_resolver_answers_are_used_without_lookups(rig, monkeypatch) -> None:
    resolver = _Resolver({"10.9999/zzz.1": TargetLookup(None, True)},
                         cached={"10.9999/zzz.1"})
    _wire_resolver(rig, monkeypatch, resolver)
    assert rig.run() == 0
    rows = {r["key"]: r for r in rig.plan()["items"]}
    assert rows["UNKN0001"]["resolver"] == pp.R_NONE
    assert rows["UNKN0001"]["route"] == pp.MANUAL


def test_uncached_resolver_answers_stay_unknown_on_the_fast_path(rig, monkeypatch) -> None:
    resolver = _Resolver({"10.9999/zzz.1": TargetLookup(None, True)})
    _wire_resolver(rig, monkeypatch, resolver)
    assert rig.run() == 0
    assert rig.calls == []
    plan = rig.plan()
    rows = {r["key"]: r for r in plan["items"]}
    assert rows["UNKN0001"]["route"] == pp.CONNECTOR
    assert plan["unknown"]["resolver_uncached"] >= 1


def test_all_lookups_price_the_sweep_first_then_ask(rig, monkeypatch, capsys) -> None:
    resolver = _Resolver({"10.9999/zzz.1": TargetLookup(None, True)})
    _wire_resolver(rig, monkeypatch, resolver)
    monkeypatch.setattr(enrich_pdfs.http_client, "get_json",
                        lambda *a, **k: {"results": []})
    monkeypatch.setattr("fetchers.doi_resolver.resolve_doi", lambda *a, **k: None)
    # habanero is in enrich_pdfs' PEP 723 block, not the dev group.
    monkeypatch.setitem(sys.modules, "habanero",
                        SimpleNamespace(Crossref=lambda **k: object()))
    assert rig.run("--plan-lookups", "all") == 0
    out = capsys.readouterr().out
    estimate = out.index("link-resolver queries (~2 s each")
    assert estimate < out.index("PDF retrieval plan")
    assert "10.9999/zzz.1" in rig.calls
    rows = {r["key"]: r for r in rig.plan()["items"]}
    assert rows["UNKN0001"]["route"] == pp.MANUAL
    assert rig.zot.writes == []


@pytest.mark.parametrize("flags", [
    ("--all",), ("--sources", "browser"), ("--plan",), ("--manual", "next"),
    ("--replace",),
])
def test_plan_strategy_takes_no_mode_of_its_own(rig, flags, capsys) -> None:
    assert rig.run(*flags) == 2
    assert "--plan-strategy plans every mode" in capsys.readouterr().err


def test_plan_lookups_needs_plan_strategy(rig, monkeypatch, capsys) -> None:
    monkeypatch.setattr(sys, "argv", ["enrich_pdfs.py", "--plan-lookups", "all"])
    assert enrich_pdfs.main() == 2
    assert "goes with --plan-strategy" in capsys.readouterr().err


def test_progress_events_for_a_job(rig, tmp_path) -> None:
    progress = tmp_path / "progress.jsonl"
    assert rig.run("--progress-json", str(progress)) == 0
    events = [json.loads(line)["event"] for line in
              progress.read_text(encoding="utf-8").splitlines()]
    assert events[0] == "start" and events[-1] == "summary"


# ---------------------------------------------------------------------------
# Consent (plan step 5): status per site, denied → manual
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("facts", "fallback"), [
    (dict(handler="sage", consent_site="sage", consent="deny"), pp.MANUAL),
    (dict(handler="sage", consent_site="sage", consent="ask"), pp.BROWSER),
    (dict(handler="sage", consent_site="sage", consent="allow"), pp.BROWSER),
    (dict(resolver=pp.R_EBSCO, consent_site="ebsco", consent="deny"), pp.MANUAL),
    (dict(resolver=pp.R_ROUTE, consent_site="jstor", consent="deny"), pp.MANUAL),
    # Consent is only for automated routes: a no-route item is untouched.
    (dict(resolver=pp.R_NONE, consent="deny"), pp.MANUAL),
    (dict(resolver=pp.R_DEFERRED, consent="deny"), pp.OTHER_LIBRARY),
])
def test_a_denied_site_falls_back_to_manual(facts, fallback) -> None:
    f = pp.ItemFacts(key="K", doi="10.1/x", api_tried=True, **facts)
    assert pp.fallback_route(f) == fallback
    assert pp.route_for(f) == fallback


def test_plan_reports_consent_per_site_with_the_counts(tmp_path) -> None:
    facts = [
        pp.ItemFacts(key="A", doi="10.1177/a", api_tried=True, handler="sage",
                     handler_display="Sage", consent_site="sage", consent="deny"),
        pp.ItemFacts(key="B", doi="10.1177/b", api_tried=True, handler="sage",
                     handler_display="Sage", consent_site="sage", consent="deny"),
        pp.ItemFacts(key="C", doi="10.1/c", api_tried=True, resolver=pp.R_EBSCO,
                     consent_site="ebsco", consent="ask"),
        pp.ItemFacts(key="D", doi="10.1/d", api_tried=True, resolver=pp.R_ROUTE,
                     consent_site="jstor", consent="allow"),
    ]
    sites = {
        "sage": {"display": "Sage", "terms_url": "", "confirmed": "2026-10-01"},
        "ebsco": {"display": "EBSCOhost", "terms_url": "https://www.ebsco.com/terms-of-use",
                  "confirmed": ""},
        "jstor": {"display": "JSTOR", "terms_url": "https://about.jstor.org/terms/",
                  "confirmed": "2026-10-15"},
    }
    summary, _ = pp.write_outputs(_plan(facts, consent_sites=sites), tmp_path)
    assert summary["fallback_routes"] == {pp.EBSCO: 1, pp.CONNECTOR: 1, pp.MANUAL: 2}
    # The denied publisher is still listed, with its answer.
    assert summary["browser_publishers"]["sage"]["consent"] == "deny"
    assert summary["browser_publishers"]["sage"]["items"] == 2
    c = summary["consent"]
    assert c["sage"]["automated"] == "deny" and c["sage"]["items"] == 2
    assert c["ebsco"]["modes"] == ["browser (EBSCOhost)"]
    assert c["ebsco"]["terms_url"] == "https://www.ebsco.com/terms-of-use"
    assert c["jstor"]["modes"] == ["connector"] and c["jstor"]["confirmed"] == "2026-10-15"
    assert summary["consent_counts"] == {"ask": 1, "deny": 2, "allow": 1,
                                         "sites_to_ask": 1}
    text = pp.format_summary(summary)
    assert "Consent for automated download" in text
    assert "EBSCOhost [ebsco]: 1 item via browser (EBSCOhost) · not asked yet" in text
    assert "denied → manual" in text
    assert "consent_ask=1 consent_deny=2" in text.splitlines()[-1]
    rows = {r["key"]: r for r in json.loads(
        (tmp_path / pp.PLAN_JSON).read_text(encoding="utf-8"))["items"]}
    assert rows["A"]["fallback"] == pp.MANUAL
    assert rows["A"]["automated_route"] == pp.BROWSER


def test_the_planner_reads_stored_consent_and_asks_nothing(rig, monkeypatch) -> None:
    from core import publisher_consent as pc
    pc.record("sage", "deny")
    assert rig.run() == 0
    plan = rig.plan()
    rows = {r["key"]: r for r in plan["items"]}
    assert rows["SAGE0001"]["consent_site"] == "sage"
    assert rows["SAGE0001"]["consent"] == "deny"
    assert rows["SAGE0001"]["route"] == pp.MANUAL
    assert plan["consent"]["sage"]["automated"] == "deny"
    # No resolver: the Connector fails open to doi.org, keyed by its site.
    assert rows["UNKN0001"]["consent_site"] == "doi.org"
    assert plan["consent"]["doi.org"]["automated"] == "ask"
    assert rig.zot.writes == []


def test_consent_skips_do_not_count_as_the_browser_pass_answering(tmp_path) -> None:
    """A CONSENT_NEEDED row opened nothing; reading it as browser_tried
    would send the item to manual download instead of asking consent."""
    log = tmp_path / "log.csv"
    log.write_text("run_date,item_key,doi,title,status,source,detail\n"
                   "d,K1,10.1177/x,T,skipped_consent_needed,sage,\n"
                   "d,K2,10.1177/y,T,no_pdf,sage,\n", encoding="utf-8")
    fail = tmp_path / "fail.csv"
    import pdf_fetch_log
    pdf_fetch_log.log_failure(fail, item_key="K3", source="connector",
                              cause=pdf_fetch_log.FailureCause.CONSENT_DENIED)
    _api, browser, latest, _below = enrich_pdfs._plan_log_signals(
        str(log), str(fail), {"sage", "connector"})
    assert browser == {"K2"}
    assert latest["K3"] == "CONSENT_DENIED"
