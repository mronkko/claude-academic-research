"""The long unattended browser run (plan `pdf-retrieval.md` §4, step 6).

Three phases: **setup** with the user present (consent where none is
stored, one solve per publisher, Connector hosts confirmed), the **run**
with no prompts at all (whatever needs a person is deferred as
DEFERRED_NEEDS_USER, never waited on), and the **summary**.

No session lifetimes were measured (user decision, 2026-10-15), so the
run re-checks the session before each publisher and every
`--recheck-minutes` within one; these tests pin that schedule, not a
lifetime.

Nothing here launches Playwright, touches Zotero or the network: the
browser is a fake context, handlers are stubs, `habanero` is a fake
module, and HOME is a scratch directory (suite fixture).
"""

from __future__ import annotations

import argparse
import asyncio
import builtins
import csv
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import enrich_pdfs
import pdf_fetch_log
import pdf_unattended as pu
import pytest
from core import publisher_consent as pc
from fetchers.browser import interaction
from fetchers.browser.base import NetworkOutage, looks_like_login, probe_page

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePage:
    def __init__(self) -> None:
        self.url = ""
        self.visited: list[str] = []

    async def goto(self, url, **_kw):
        self.visited.append(url)
        self.url = url

    async def close(self) -> None:
        pass


class _FakeContext:
    def __init__(self) -> None:
        self.pages = [_FakePage()]

    async def new_page(self) -> _FakePage:
        return _FakePage()

    async def close(self) -> None:
        pass


class _FakePlaywright:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False


@pytest.fixture
def fake_browser(monkeypatch):
    ctx = _FakeContext()
    launches: list[dict] = []
    import fetchers.browser as browser_pkg
    import playwright.async_api as pw

    async def _launch(_p, _cache_dir, **kw):
        launches.append(kw)
        return ctx

    monkeypatch.setattr(browser_pkg, "launch_context", _launch)
    monkeypatch.setattr(pw, "async_playwright", lambda: _FakePlaywright())
    return SimpleNamespace(ctx=ctx, launches=launches)


@pytest.fixture
def no_prompts(monkeypatch):
    """Every way a prompt could be raised fails the test, and the run's
    channel counts anything that reaches it."""
    channel = interaction.UnattendedChannel()
    previous = interaction.set_channel(channel)

    def boom(*a, **k):
        raise AssertionError("an unattended run asked a person")

    monkeypatch.setattr(builtins, "input", boom)
    monkeypatch.setattr(enrich_pdfs, "_prompt_on_first_failure", boom)
    yield channel
    interaction.set_channel(previous)


class _Handler:
    """A publisher handler stub. `probes` are the session-check answers,
    in order (then ""); `fails` the item indexes whose download fails."""

    name = "fake"
    display_name = "Fake Publisher"
    doi_prefixes = ()
    delay_s = 0.0
    attaches_directly = False
    concurrency = 1

    def __init__(self, *, needs_solve=True, probes=(), fails=(), last_error="no PDF link",
                 clock=None) -> None:
        self.needs_interactive_solve = needs_solve
        self.probes = list(probes)
        self.fails = set(fails)
        self.fail_error = last_error
        self.last_error = ""
        self.probe_calls: list[str] = []
        self.download_calls: list[str] = []
        self.clock = clock

    async def setup(self, page, doi):
        raise AssertionError("setup() asks a person; the run must not call it")

    async def probe_session(self, page, doi):
        self.probe_calls.append(doi)
        return self.probes.pop(0) if self.probes else ""

    async def download(self, page, ctx, item, cache_dir, *, counter, total, t_start):
        i = len(self.download_calls)
        self.download_calls.append(item["doi"])
        if self.clock is not None:
            self.clock[0] += 6 * 60          # each item takes six minutes
        if i in self.fails or "all" in self.fails:
            self.last_error = self.fail_error
            counter.failed += 1
            return None
        counter.ok += 1
        return Path(cache_dir) / "x.pdf", "https://example/pdf"


class _Rows:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def writerow(self, row: dict) -> None:
        self.rows.append(dict(row))


def _args(tmp_path, **kw) -> argparse.Namespace:
    base = dict(
        cache_dir=str(tmp_path / "cache"), dry_run=True, no_check_text=True,
        failure_log_csv=str(tmp_path / "fail.csv"), browser_workers=1,
        on_first_failure="", no_prompt=False, plan=False, control_file="",
        unattended=True, unattended_phase="run", unattended_recheck_s=15 * 60.0,
        unattended_state=str(tmp_path / "unattended.json"), item_types="",
    )
    base.update(kw)
    return argparse.Namespace(**base)


def _items(n: int) -> list[dict]:
    return [{"doi": f"10.1/{i}", "item_key": f"K{i}", "title": f"T{i}",
             "item_type": "journalArticle"} for i in range(n)]


def _drive(handler, items, args, rows, **kw):
    kw.setdefault("on_failure", "retry_bucket")
    kw.setdefault("retry_bucket", [])
    kw.setdefault("prompt_on_first_failure", True)
    kw.setdefault("defer_solve", True)
    asyncio.run(enrich_pdfs._drive_handler(
        handler, items, object(), rows, args, "2026-10-15", **kw,
    ))
    return kw["retry_bucket"]


def _deferred(rows: _Rows) -> dict[str, str]:
    return {r["item_key"]: pu.reason_of(r["detail"]) for r in rows.rows
            if r["status"] == pu.DEFERRED_STATUS}


# ---------------------------------------------------------------------------
# The run: never prompts, defers instead of blocking
# ---------------------------------------------------------------------------

def test_run_never_calls_setup_or_the_first_failure_prompt(
    fake_browser, no_prompts, tmp_path,
) -> None:
    handler = _Handler(fails={0})
    rows = _Rows()
    bucket = _drive(handler, _items(4), _args(tmp_path), rows)
    assert no_prompts.asked == []
    assert len(handler.download_calls) == 4          # one failure: keep going
    assert [it["item_key"] for it in bucket] == ["K0"]
    assert _deferred(rows) == {}


def test_expired_clearance_before_the_publisher_defers_it_without_blocking(
    fake_browser, no_prompts, tmp_path,
) -> None:
    handler = _Handler(probes=["challenge"])
    rows = _Rows()
    args = _args(tmp_path)
    bucket = _drive(handler, _items(3), args, rows)
    assert handler.download_calls == [] and bucket == []
    assert _deferred(rows) == {"K0": "challenge", "K1": "challenge", "K2": "challenge"}
    causes = {r["item_key"]: r["cause"] for r in pdf_fetch_log.read_failures(
        args.failure_log_csv)}
    assert set(causes.values()) == {"DEFERRED_NEEDS_USER"}
    assert no_prompts.asked == []


def test_a_session_lost_mid_lane_defers_that_item_and_the_rest(
    fake_browser, no_prompts, tmp_path,
) -> None:
    """The failure re-checks the session instead of asking; a login page
    there means the failure was the session's, not the article's."""
    handler = _Handler(probes=["", "login"], fails={1})
    rows = _Rows()
    bucket = _drive(handler, _items(4), _args(tmp_path), rows)
    assert handler.download_calls == ["10.1/0", "10.1/1"]
    assert _deferred(rows) == {"K1": "login", "K2": "login", "K3": "login"}
    assert bucket == []                       # not handed to the Connector


def test_repeated_unexplained_failures_defer_the_rest(
    fake_browser, no_prompts, tmp_path,
) -> None:
    handler = _Handler(fails={"all"})
    rows = _Rows()
    bucket = _drive(handler, _items(6), _args(tmp_path), rows)
    assert len(handler.download_calls) == pu.FAILURE_LIMIT
    assert len(bucket) == pu.FAILURE_LIMIT     # tried and failed: as attended
    assert set(_deferred(rows).values()) == {pu.REPEATED_FAILURES}
    assert len(_deferred(rows)) == 6 - pu.FAILURE_LIMIT


def test_a_success_breaks_the_run_of_unexplained_failures(
    fake_browser, no_prompts, tmp_path,
) -> None:
    handler = _Handler(fails={0, 1, 3, 4, 6})
    rows = _Rows()
    _drive(handler, _items(8), _args(tmp_path), rows)
    assert len(handler.download_calls) == 8 and _deferred(rows) == {}


def test_the_recheck_interval_is_honoured(fake_browser, no_prompts, tmp_path,
                                          monkeypatch) -> None:
    """Before the publisher, then once `recheck_s` has passed — not per
    item. Items take six minutes each against a 15-minute interval."""
    clock = [0.0]
    monkeypatch.setattr(enrich_pdfs, "_monotonic", lambda: clock[0])
    handler = _Handler(clock=clock)
    _drive(handler, _items(6), _args(tmp_path), _Rows())
    # start; then before item 4 (t=18 min) — 24 and 30 are within 15 of 18.
    assert handler.probe_calls == ["10.1/0", "10.1/3"]


def test_a_failed_recheck_defers_the_remaining_items(fake_browser, no_prompts,
                                                     tmp_path, monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(enrich_pdfs, "_monotonic", lambda: clock[0])
    handler = _Handler(clock=clock, probes=["", "challenge"])
    rows = _Rows()
    _drive(handler, _items(6), _args(tmp_path), rows)
    assert handler.download_calls == ["10.1/0", "10.1/1", "10.1/2"]
    assert _deferred(rows) == {"K3": "challenge", "K4": "challenge", "K5": "challenge"}


def test_recheck_zero_checks_only_before_the_publisher(fake_browser, no_prompts,
                                                       tmp_path, monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(enrich_pdfs, "_monotonic", lambda: clock[0])
    handler = _Handler(clock=clock)
    _drive(handler, _items(6), _args(tmp_path, unattended_recheck_s=0.0), _Rows())
    assert handler.probe_calls == ["10.1/0"]


def test_a_publisher_needing_no_solve_is_not_probed(fake_browser, no_prompts,
                                                    tmp_path, monkeypatch) -> None:
    clock = [0.0]
    monkeypatch.setattr(enrich_pdfs, "_monotonic", lambda: clock[0])
    handler = _Handler(needs_solve=False, clock=clock)
    _drive(handler, _items(6), _args(tmp_path), _Rows())
    assert handler.probe_calls == []


def test_the_network_outage_breaker_still_trips(fake_browser, no_prompts, tmp_path) -> None:
    handler = _Handler(fails={"all"}, last_error="net::ERR_INTERNET_DISCONNECTED at x")
    rows = _Rows()
    with pytest.raises(NetworkOutage):
        _drive(handler, _items(9), _args(tmp_path), rows)
    assert len(handler.download_calls) == enrich_pdfs._OUTAGE_THRESHOLD
    assert handler.probe_calls == ["10.1/0"]     # transport errors are not probed
    assert _deferred(rows) == {}                 # un-attempted items stay unlogged


def test_a_publisher_declined_in_setup_goes_where_an_attended_skip_goes(
    fake_browser, no_prompts, tmp_path,
) -> None:
    args = _args(tmp_path)
    state = pu.new_state()
    pu.record_site(state, "fake", pu.SKIPPED)
    pu.save_state(state, Path(args.unattended_state))
    handler = _Handler()
    bucket = _drive(handler, _items(2), args, _Rows())
    assert handler.download_calls == [] and handler.probe_calls == []
    assert [it["item_key"] for it in bucket] == ["K0", "K1"]


# ---------------------------------------------------------------------------
# Session probe helpers
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(("page_url", "target", "login"), [
    ("https://journals.aom.org/doi/10.5465/x", "https://journals.aom.org/doi/10.5465/x",
     False),
    ("https://idp.aalto.fi/idp/profile/SAML2/Redirect/SSO",
     "https://journals.sagepub.com/doi/10.1177/x", True),
    ("https://onlinelibrary.wiley.com/action/showLogin?uri=x",
     "https://onlinelibrary.wiley.com/doi/x", True),
    ("https://ezproxy.jyu.fi/login?url=https://x", "https://ezproxy.jyu.fi/login?url=https://x",
     True),
    ("https://www-jstor-org.ezproxy.jyu.fi/stable/1",
     "https://ezproxy.jyu.fi/login?url=https://www.jstor.org/stable/1", False),
    ("", "https://x", False),
])
def test_looks_like_login(page_url, target, login) -> None:
    assert looks_like_login(page_url, target) is login


class _ProbePage:
    def __init__(self, title: str, url: str) -> None:
        self._title, self.url = title, url
        self.context = self

    async def title(self):
        return self._title

    def locator(self, _sel):
        return self

    @property
    def first(self):
        return self

    async def is_visible(self, timeout=0):
        return False

    async def cookies(self):
        return []


def test_probe_page_reports_a_challenge_that_does_not_clear(monkeypatch) -> None:
    import fetchers.browser.base as base

    async def no_wait(page, *, timeout_s, poll_interval_s=0.5):
        return False

    monkeypatch.setattr(base, "wait_for_clearance", no_wait)
    page = _ProbePage("Just a moment...", "https://journals.sagepub.com/doi/x")
    assert asyncio.run(probe_page(page, page.url)) == "challenge"
    ok = _ProbePage("An article", "https://journals.sagepub.com/doi/x")
    assert asyncio.run(probe_page(ok, ok.url)) == ""


# ---------------------------------------------------------------------------
# The Connector, unattended
# ---------------------------------------------------------------------------

def test_connector_never_opens_an_unconfirmed_host(no_prompts) -> None:
    from fetchers.browser import Counter, ZoteroConnectorHandler

    h = ZoteroConnectorHandler(extension_path=None)
    h.unattended = True
    h._confirmed_hosts = {"www.jstor.org"}
    page = _FakePage()
    item = {"doi": "10.1/x", "item_key": "K", "title": "T",
            "resolver_target_url": "https://www.tandfonline.com/doi/10.1/x"}
    ok = asyncio.run(h.download_and_attach(page, None, None, item, None,
                                           counter=Counter(), total=1, t_start=0))
    assert ok is False and h.last_outcome == "host_unconfirmed"
    assert page.visited == []
    assert no_prompts.asked == []


def test_connector_loop_defers_walls_instead_of_logging_them() -> None:
    class H:
        unattended = True
        delay_s = 0.0
        last_error = ""
        outcomes = ["host_unconfirmed", "login_required", "challenge"]

        async def download_and_attach(self, *a, **k):
            self.last_outcome = self.outcomes.pop(0)
            return False

    deferred: list[tuple[str, str]] = []
    logged: list[tuple[str, str]] = []
    items = [{"doi": f"10.1/{i}", "item_key": f"K{i}",
              "resolver_target_url": f"https://h{i}.example/x"} for i in range(3)]
    asyncio.run(enrich_pdfs._connector_item_loop(
        H(), items, None, None, None, None, counter=SimpleNamespace(ok=0, queued=0),
        total=3, t_start=0.0, streak=enrich_pdfs._EmptySaveStreak(),
        log_row=lambda it, st: logged.append((it["item_key"], st)),
        defer=lambda it, reason: deferred.append((it["item_key"], reason)),
    ))
    assert deferred == [("K0", "host_unconfirmed"), ("K1", "login"), ("K2", "challenge")]
    assert logged == []


def test_connector_defers_everything_when_zotero_desktop_is_not_running(
    tmp_path, monkeypatch, no_prompts,
) -> None:
    import fetchers.browser as browser_pkg
    from fetchers.browser import ZoteroConnectorHandler

    monkeypatch.setattr(browser_pkg, "ping_zotero_desktop", lambda *a, **k: False)
    h = ZoteroConnectorHandler(extension_path=None)
    h.unattended = True
    rows = _Rows()
    asyncio.run(enrich_pdfs._drive_connector(
        h, _items(2), object(), rows, _args(tmp_path), "2026-10-15"))
    assert _deferred(rows) == {"K0": pu.ZOTERO_UNAVAILABLE, "K1": pu.ZOTERO_UNAVAILABLE}


# ---------------------------------------------------------------------------
# Setup: consent recorded, one solve per site
# ---------------------------------------------------------------------------

class _SetupHandler:
    name = "fake"
    display_name = "Fake Publisher"
    needs_interactive_solve = True

    def __init__(self, result="proceed") -> None:
        self.result = result
        self.setup_calls: list[str] = []

    async def setup(self, page, doi):
        self.setup_calls.append(doi)
        return self.result


@pytest.mark.parametrize(("answer", "result"), [
    ("proceed", pu.SOLVED), ("skip", pu.SKIPPED), ("always_skip", pu.ALWAYS_SKIP),
])
def test_setup_solves_a_publisher_once(fake_browser, tmp_path, answer, result) -> None:
    h = _SetupHandler(answer)
    got = asyncio.run(enrich_pdfs._unattended_setup_handler(
        h, _items(5), _args(tmp_path, unattended=False, unattended_phase="setup")))
    assert got == result and h.setup_calls == ["10.1/0"]


def test_setup_needs_no_browser_for_a_publisher_without_a_solve(fake_browser,
                                                                tmp_path) -> None:
    h = _SetupHandler()
    h.needs_interactive_solve = False
    got = asyncio.run(enrich_pdfs._unattended_setup_handler(h, _items(2), _args(tmp_path)))
    assert got == pu.NO_SOLVE_NEEDED and h.setup_calls == [] and fake_browser.launches == []


def test_setup_records_each_publisher_and_persists_always_skip(fake_browser,
                                                               tmp_path) -> None:
    pc.record("fake", "allow")
    pc.record("other", "allow")
    a, b = _SetupHandler(), _SetupHandler("always_skip")
    b.name = "other"
    persisted: list[str] = []
    args = _args(tmp_path, unattended=False, unattended_phase="setup")
    asyncio.run(enrich_pdfs._unattended_setup_handlers(
        [(a, _items(2)), (b, _items(3))], args, on_always_skip=persisted.append))
    state = pu.load_state(Path(args.unattended_state))
    assert state["sites"]["fake"]["result"] == pu.SOLVED
    assert state["sites"]["other"]["result"] == pu.ALWAYS_SKIP
    assert persisted == ["other"]
    assert len(a.setup_calls) == len(b.setup_calls) == 1


def test_setup_does_not_solve_a_site_allowed_only_once(fake_browser, tmp_path) -> None:
    """"once" lets the setup through but records nothing, and the run
    never asks — so the run would skip it as CONSENT_NEEDED anyway."""
    h = _SetupHandler()
    args = _args(tmp_path, unattended=False, unattended_phase="setup")
    asyncio.run(enrich_pdfs._unattended_setup_handlers([(h, _items(2))], args))
    assert h.setup_calls == []
    assert pu.load_state(Path(args.unattended_state))["sites"]["fake"]["result"] == (
        pu.CONSENT_NEEDED)


def test_setup_confirms_connector_hosts_in_the_connector_profile(
    fake_browser, tmp_path, monkeypatch,
) -> None:
    import fetchers.browser.connector as conn
    monkeypatch.setattr(conn, "resolve_connector_extension_path",
                        lambda p: tmp_path / "ext")
    answers = iter(["", "s"])

    class Fake(interaction.InteractionChannel):
        def wait_for_user(self, prompt):
            pass

        def read_line(self, prompt):
            return next(answers)

    previous = interaction.set_channel(Fake())
    try:
        items = [
            {"doi": "10.1/a", "item_key": "A", "resolver_target_url":
             "https://www.jstor.org/stable/1"},
            {"doi": "10.1/b", "item_key": "B", "resolver_target_url":
             "https://www.jstor.org/stable/2"},
            {"doi": "10.1/c", "item_key": "C", "resolver_target_url":
             "https://www.tandfonline.com/doi/10.1/c"},
        ]
        args = _args(tmp_path, unattended=False, unattended_phase="setup")
        got = asyncio.run(enrich_pdfs._unattended_setup_connector(items, args))
    finally:
        interaction.set_channel(previous)
    assert got == {"www.jstor.org": pu.CONFIRMED, "www.tandfonline.com": pu.HOST_SKIPPED}
    assert fake_browser.launches == [{"extensions": [tmp_path / "ext"]}]
    state = pu.load_state(Path(args.unattended_state))
    assert pu.confirmed_hosts(state) == {"www.jstor.org"}
    assert state["connector_hosts"]["www.jstor.org"]["items"] == 2


# ---------------------------------------------------------------------------
# The browser pass end to end, setup and run, with stub drivers
# ---------------------------------------------------------------------------

SAGE_DOI = "10.1177/0001"
OTHER_DOI = "10.9999/0002"


def _zitem(key: str, doi: str) -> dict:
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
    calls: dict[str, list] = {"drive": [], "connector": [], "setup": [],
                              "setup_connector": []}

    async def drive(handler, items, *a, **k):
        calls["drive"].append((handler.name, [it["item_key"] for it in items]))

    async def drive_connector(handler, items, *a, **k):
        calls["connector"].append((handler.unattended, set(handler._confirmed_hosts),
                                   [it["item_key"] for it in items]))

    async def setup_one(handler, items, args):
        calls["setup"].append((handler.name, [it["item_key"] for it in items]))
        return pu.SOLVED

    async def setup_connector(items, args):
        calls["setup_connector"].append([it["item_key"] for it in items])
        return {}

    monkeypatch.setattr(enrich_pdfs, "_drive_handler", drive)
    monkeypatch.setattr(enrich_pdfs, "_drive_connector", drive_connector)
    monkeypatch.setattr(enrich_pdfs, "_unattended_setup_handler", setup_one)
    monkeypatch.setattr(enrich_pdfs, "_unattended_setup_connector", setup_connector)
    previous = interaction.get_channel()
    state_path = tmp_path / "unattended.json"

    def run(phase, *flags, surface=False, answers=()):
        monkeypatch.setattr(enrich_pdfs, "_has_interactive_surface", lambda: surface)
        queue = list(answers)
        asked: list[str] = []

        class Fake(interaction.InteractionChannel):
            def wait_for_user(self, prompt):
                asked.append(prompt)

            def read_line(self, prompt):
                asked.append(prompt)
                return queue.pop(0) if queue else ""

        interaction.set_channel(Fake())
        args = enrich_pdfs._build_parser().parse_args([
            "--unattended", phase, "--cache-dir", str(tmp_path / "cache"),
            "--failure-log-csv", str(tmp_path / "fail.csv"), *flags,
        ])
        assert enrich_pdfs._check_unattended_args(args) == 0
        args.unattended_state = str(state_path)
        if phase == "run":
            enrich_pdfs._install_interaction_channel(args)
        else:
            enrich_pdfs._unattended_state(args)["setup"] = {"started": pu.now()}
        log = tmp_path / "log.csv"
        fh, writer = enrich_pdfs._open_log(str(log))
        try:
            rc = enrich_pdfs._run_browser_in_process(
                [_zitem("S1", SAGE_DOI), _zitem("S2", "10.1177/0002"),
                 _zitem("X1", OTHER_DOI)],
                SimpleNamespace(),
                enrich_pdfs._NullWriter() if phase == "setup" else writer,
                args, "2026-10-15",
            )
        finally:
            fh.close()
        with log.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        channel = interaction.get_channel()
        return SimpleNamespace(rc=rc, rows=rows, asked=asked + list(
            getattr(channel, "asked", [])), state=pu.load_state(state_path))

    yield SimpleNamespace(run=run, calls=calls)
    interaction.set_channel(previous)


def test_setup_records_consent_then_solves_once_per_site(browser_pass, capsys) -> None:
    out = browser_pass.run("setup", surface=True, answers=["a", "a"])
    assert out.rc == 0
    assert len(out.asked) == 2                         # sage, doi.org — consent only
    assert pc.lookup("sage").automated == "allow"
    assert browser_pass.calls["setup"] == [("sage", ["S1", "S2"])]
    assert browser_pass.calls["setup_connector"] == [["X1"]]
    assert browser_pass.calls["drive"] == [] and browser_pass.calls["connector"] == []
    assert out.state["sites"]["sage"]["result"] == pu.SOLVED
    assert out.state["setup"]["finished"]
    assert out.rows == []                              # downloads and logs nothing
    assert "UNATTENDED: phase=setup" in capsys.readouterr().out


def test_setup_through_a_control_file_never_relays_consent(browser_pass, tmp_path) -> None:
    """Step 5's rule stands: consent is recorded in the conversation
    (pdf_consent_set), not answered on the fly through the file."""
    out = browser_pass.run("setup", "--control-file", str(tmp_path / "c.json"),
                           surface=False)
    assert out.asked == [] and browser_pass.calls["setup"] == []
    assert out.state["sites"]["sage"]["result"] == pu.CONSENT_NEEDED
    assert out.state["sites"]["doi.org"]["result"] == pu.CONSENT_NEEDED


def test_run_asks_nothing_and_skips_unconsented_sites(browser_pass) -> None:
    out = browser_pass.run("run", surface=True)
    assert out.asked == []
    assert browser_pass.calls["drive"] == []
    assert {r["status"] for r in out.rows} == {"skipped_consent_needed"}


def test_run_hands_the_connector_the_hosts_confirmed_in_setup(browser_pass,
                                                               tmp_path) -> None:
    pc.record("sage", "allow")
    pc.record("doi.org", "allow")
    state = pu.new_state()
    pu.record_host(state, "doi.org", pu.CONFIRMED)
    pu.record_host(state, "www.jstor.org", pu.HOST_SKIPPED)
    pu.save_state(state, tmp_path / "unattended.json")
    out = browser_pass.run("run", surface=True)
    assert out.asked == []
    assert browser_pass.calls["drive"] == [("sage", ["S1", "S2"])]
    assert browser_pass.calls["connector"] == [(True, {"doi.org"}, ["X1"])]


# ---------------------------------------------------------------------------
# Flags
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [
    ["--unattended", "run", "--manual", "next"],
    ["--unattended", "run", "--control-file", "x.json"],
    ["--unattended", "run", "--no-prompt"],
    ["--unattended", "setup", "--plan"],
    ["--unattended", "setup", "--all"],
    ["--unattended", "run", "--sources", "crossref"],
    ["--unattended", "run", "--on-first-failure", "keep"],
    ["--unattended", "run", "--recheck-minutes", "-1"],
    ["--plan-strategy", "--unattended", "setup"],
    ["--recheck-minutes", "5"],
])
def test_conflicting_flags_exit_2_before_any_work(monkeypatch, argv) -> None:
    monkeypatch.setattr(sys, "argv", ["enrich_pdfs.py", *argv])
    monkeypatch.setattr(enrich_pdfs.zotero_io.ZoteroClient, "from_args",
                        lambda *a, **k: pytest.fail("reached Zotero"))
    assert enrich_pdfs.main() == 2


def test_unattended_defaults_to_the_browser_pass_and_sets_the_run_flag() -> None:
    args = enrich_pdfs._build_parser().parse_args(["--unattended", "run",
                                                   "--recheck-minutes", "5"])
    assert enrich_pdfs._check_unattended_args(args) == 0
    assert args.sources == "browser" and args.unattended is True
    assert args.unattended_recheck_s == 300.0
    setup = enrich_pdfs._build_parser().parse_args(["--unattended", "setup"])
    assert enrich_pdfs._check_unattended_args(setup) == 0
    assert setup.unattended is False                 # setup may ask (TTY)


def test_run_installs_a_channel_that_records_any_prompt(tmp_path) -> None:
    previous = interaction.get_channel()
    try:
        enrich_pdfs._install_interaction_channel(argparse.Namespace(
            unattended=True, progress_json="", control_file="", no_prompt=False))
        ch = interaction.get_channel()
        assert isinstance(ch, interaction.UnattendedChannel) and not ch.interactive
        assert ch.read_line("go on?") == "skip" and ch.asked == ["go on?"]
    finally:
        interaction.set_channel(previous)


@pytest.mark.parametrize(("flag", "config", "seconds", "where"), [
    (None, None, pu.DEFAULT_RECHECK_MINUTES * 60, "default"),
    (5.0, "30", 300.0, "flag"),
    (None, "30", 1800.0, "config"),
    (None, "0", 0.0, "config"),
    (None, "soon", pu.DEFAULT_RECHECK_MINUTES * 60, "default (config value 'soon' ignored)"),
])
def test_recheck_interval_resolution(flag, config, seconds, where) -> None:
    assert pu.recheck_seconds(flag, config) == (seconds, where)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

def test_summary_splits_failures_by_network_and_lists_deferrals() -> None:
    rows = [
        {"item_key": "A", "status": "attached"},
        {"item_key": "B", "status": pu.DEFERRED_STATUS, "detail": pu.detail(pu.LOGIN)},
        {"item_key": "C", "status": pu.DEFERRED_STATUS,
         "detail": pu.detail(pu.HOST_UNCONFIRMED)},
        {"item_key": "D", "status": "skipped_no_pdf"},
        {"item_key": "E", "status": "skipped_no_pdf"},
        {"item_key": "F", "status": "skipped_no_pdf"},
        {"item_key": "G", "status": "skipped_consent_needed"},
    ]
    t = "2026-10-15T10:00:00+00:00"
    failures = [
        {"item_key": "D", "cause": "ACCESS_BLOCKED", "network": "Aalto",
         "timestamp": "2026-10-15T10:05:00+00:00"},
        {"item_key": "E", "cause": "NETWORK_ERROR", "network": "Aalto",
         "timestamp": "2026-10-15T10:06:00+00:00"},
        {"item_key": "F", "cause": "ACCESS_BLOCKED", "network": "Jyu",
         "timestamp": "2026-10-15T10:07:00+00:00"},
        {"item_key": "D", "cause": "UNAVAILABLE", "network": "Aalto",
         "timestamp": "2026-10-14T10:00:00+00:00"},      # an older run's row
        {"item_key": "B", "cause": "DEFERRED_NEEDS_USER", "network": "Aalto",
         "timestamp": "2026-10-15T10:08:00+00:00"},
    ]
    s = pu.summarise(rows, failures, network="Aalto", since=t)
    assert s["attached"] == 1 and s["deferred"] == 2
    assert s["deferred_by_reason"] == {"login": 1, "host_unconfirmed": 1}
    assert s["deferred_keys"] == ["B", "C"]
    assert s["consent_needed"] == 1
    assert s["failed_items"] == 3 and s["network_errors"] == 1
    assert s["access_failures_this_network"] == 1
    assert s["access_failures_other_network"] == 1
    line = pu.summary_line(s)
    assert line.startswith("UNATTENDED: phase=run ") and "deferred=2" in line
    text = pu.format_summary(s)
    assert "--manual next" in text and "not measured" in text


def test_finishing_a_run_records_the_summary_in_the_state(tmp_path, capsys) -> None:
    args = _args(tmp_path, failure_log_csv="")
    previous = interaction.set_channel(interaction.UnattendedChannel())
    try:
        enrich_pdfs._finish_unattended_run(
            args, [{"item_key": "B", "status": pu.DEFERRED_STATUS,
                    "detail": pu.detail(pu.CHALLENGE)}], pu.now(), rc=0)
    finally:
        interaction.set_channel(previous)
    state = json.loads(Path(args.unattended_state).read_text())
    assert state["runs"][-1]["deferred_by_reason"] == {"challenge": 1}
    assert capsys.readouterr().out.strip().splitlines()[-1].startswith(
        "UNATTENDED: phase=run")
