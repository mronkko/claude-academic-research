"""Tests for scripts/pipelines/fetchers/browser/connector.py.

Exercises extension-path resolution, Zotero Desktop ping, and the
new-item poll. Playwright is NOT loaded — the Chromium-bound paths
are live-tested separately (tests/live/).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fetchers.browser.connector import (
    ZoteroConnectorHandler,
    _poll_for_new_item,
    _wait_for_child_attachment,
    _wait_for_cloud_sync,
    ping_zotero_desktop,
    resolve_connector_extension_path,
)

# ---------------------------------------------------------------------------
# resolve_connector_extension_path
# ---------------------------------------------------------------------------


@pytest.fixture
def no_installed_connector(monkeypatch):
    """Stub the platform defaults empty.

    Without this the "returns None" assertions below pass only on a
    machine that has no Zotero Connector installed — they were quietly
    environment-dependent, because an explicit path used to short-circuit
    the platform probe and the probe was never reached.
    """
    from fetchers.browser import connector
    monkeypatch.setattr(
        connector, "_default_extension_search_paths", lambda: [],
    )


def test_resolve_connector_path_none_when_base_missing(
    no_installed_connector,
) -> None:
    assert resolve_connector_extension_path("/does/not/exist/anywhere") is None


def test_explicit_path_that_is_stale_falls_back_to_platform_defaults(
    monkeypatch, tmp_path: Path,
) -> None:
    """A dead explicit path must not mask a working install.

    Chrome auto-updates the Connector and deletes the superseded version
    folder, so a config value pinned to `.../<ext-id>/5.0.200_0` becomes
    a dead path. Returning None there reported "extension not found"
    while the extension sat installed one directory up, and the browser
    stage aborted with `connector_extension_missing` on every row.
    """
    from fetchers.browser import connector

    installed = tmp_path / "ext" / "5.0.211_0"
    installed.mkdir(parents=True)
    (installed / "manifest.json").write_text("{}")
    monkeypatch.setattr(
        connector, "_default_extension_search_paths", lambda: [tmp_path / "ext"],
    )

    stale_pin = tmp_path / "ext" / "5.0.200_0"  # never created
    assert resolve_connector_extension_path(stale_pin) == installed


def test_a_resolvable_explicit_path_still_wins_over_the_defaults(
    monkeypatch, tmp_path: Path,
) -> None:
    """The fallback must not demote an explicit value that does resolve."""
    from fetchers.browser import connector

    chosen = tmp_path / "chosen" / "1.0_0"
    chosen.mkdir(parents=True)
    (chosen / "manifest.json").write_text("{}")
    default = tmp_path / "ext" / "9.9.9_0"
    default.mkdir(parents=True)
    (default / "manifest.json").write_text("{}")
    monkeypatch.setattr(
        connector, "_default_extension_search_paths", lambda: [tmp_path / "ext"],
    )

    assert resolve_connector_extension_path(chosen) == chosen


def test_resolve_connector_path_explicit_version_dir(tmp_path: Path) -> None:
    """An explicit path that already points at a version folder
    (contains manifest.json) is returned verbatim."""
    version = tmp_path / "5.0.130_0"
    version.mkdir()
    (version / "manifest.json").write_text("{}")
    assert resolve_connector_extension_path(version) == version


def test_resolve_connector_path_picks_latest_version_subdir(tmp_path: Path) -> None:
    """When passed the extension base (not a version dir), the helper
    picks the highest-named subdirectory so future Connector updates
    are picked up automatically."""
    (tmp_path / "5.0.100_0").mkdir()
    (tmp_path / "5.0.130_0").mkdir()
    (tmp_path / "4.9.9_0").mkdir()
    result = resolve_connector_extension_path(tmp_path)
    assert result is not None and result.name == "5.0.130_0"


def test_resolve_connector_path_returns_none_on_empty_base(
    no_installed_connector, tmp_path: Path,
) -> None:
    (tmp_path / "random_file").write_text("")  # not a dir — ignored
    assert resolve_connector_extension_path(tmp_path) is None


def test_resolve_connector_path_falls_back_to_platform_defaults(
    monkeypatch, tmp_path: Path,
) -> None:
    """With no explicit path, the helper probes the platform defaults
    in order. Redirect one of them to a fake extension dir and confirm
    it's picked up."""
    from fetchers.browser import connector

    fake_ext = tmp_path / "ext" / "5.0.0_0"
    fake_ext.mkdir(parents=True)
    (fake_ext / "manifest.json").write_text("{}")

    monkeypatch.setattr(
        connector, "_default_extension_search_paths",
        lambda: [tmp_path / "nonexistent", tmp_path / "ext"],
    )
    assert resolve_connector_extension_path() == fake_ext


# ---------------------------------------------------------------------------
# ZoteroConnectorHandler construction
# ---------------------------------------------------------------------------


def test_handler_accepts_explicit_extension_path(tmp_path: Path) -> None:
    """Explicit path flows through into the instance attribute."""
    version = tmp_path / "5.0.130_0"
    version.mkdir()
    (version / "manifest.json").write_text("{}")
    h = ZoteroConnectorHandler(extension_path=version)
    assert h.extension_path == version


def test_handler_declares_attaches_directly() -> None:
    """Signals to the driver that this handler uses download_and_attach,
    not download()."""
    h = ZoteroConnectorHandler(extension_path=None)
    assert h.attaches_directly is True
    # And the standard download() raises — the driver must route to
    # download_and_attach for attaches_directly=True handlers.
    import asyncio
    with pytest.raises(NotImplementedError):
        asyncio.run(h.download(
            None, None, {"doi": "x"}, ".", counter=MagicMock(),
            total=1, t_start=0.0,
        ))


def test_handler_direct_access_domains_empty() -> None:
    """The Connector handler does not claim any direct-access domains —
    it trusts the routing layer to hand it a reachable URL."""
    h = ZoteroConnectorHandler(extension_path=None)
    assert h.direct_access_domains == ()


# ---------------------------------------------------------------------------
# Zotero Desktop ping
# ---------------------------------------------------------------------------


def test_ping_zotero_desktop_true_on_200() -> None:
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    session.get.return_value = resp
    assert ping_zotero_desktop(session) is True


def test_ping_zotero_desktop_false_on_non_200() -> None:
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 502
    session.get.return_value = resp
    assert ping_zotero_desktop(session) is False


def test_ping_zotero_desktop_false_on_exception() -> None:
    """Zotero Desktop being off is the expected miss — never raise."""
    session = MagicMock()
    session.get.side_effect = RuntimeError("connection refused")
    assert ping_zotero_desktop(session) is False


# ---------------------------------------------------------------------------
# _poll_for_new_item — the DOI-based dedup lookup used after a save.
# ---------------------------------------------------------------------------


def test_poll_for_new_item_returns_new_key_when_found() -> None:
    """A new Zotero item with the same DOI as the keeper (but a
    different key) is exactly what the Connector creates."""
    zot = MagicMock()
    zot.recent_items.return_value = [
        {"key": "KEEPER", "data": {"DOI": "10.1/x"}},
        {"key": "NEW123", "data": {"DOI": "10.1/x"}},
    ]
    result = _poll_for_new_item(zot, "10.1/x", "KEEPER", timeout_s=0.1)
    assert result == "NEW123"


def test_poll_for_new_item_ignores_keeper_itself() -> None:
    """If the only item with the matching DOI IS the keeper, the poll
    returns None (no duplicate was created)."""
    zot = MagicMock()
    zot.recent_items.return_value = [
        {"key": "KEEPER", "data": {"DOI": "10.1/x"}},
    ]
    assert _poll_for_new_item(
        zot, "10.1/x", "KEEPER", timeout_s=0.2,
    ) is None


def test_poll_for_new_item_matches_case_insensitive() -> None:
    zot = MagicMock()
    zot.recent_items.return_value = [
        {"key": "NEW", "data": {"DOI": "10.1/ABC"}},
    ]
    assert _poll_for_new_item(
        zot, "10.1/abc", "KEEPER", timeout_s=0.1,
    ) == "NEW"


def test_poll_for_new_item_survives_zotero_errors() -> None:
    """Transient errors from the library listing must not propagate —
    the pipeline would otherwise crash mid-batch."""
    zot = MagicMock()
    zot.recent_items.side_effect = RuntimeError("zotero down")
    assert _poll_for_new_item(
        zot, "10.1/x", "KEEPER", timeout_s=0.1,
    ) is None


def test_poll_asks_only_for_the_newest_items() -> None:
    """Each poll used to list every journal article: 25,703 of them took
    95 s through the local API, so a save Zotero had finished in seconds
    was reported after 142 s or more."""
    zot = MagicMock()
    zot.recent_items.return_value = [
        {"key": "ATT", "data": {"itemType": "attachment", "DOI": "10.1/x"}},
        {"key": "NEW", "data": {"itemType": "journalArticle", "DOI": "10.1/x"}},
    ]
    assert _poll_for_new_item(zot, "10.1/x", "KEEPER", timeout_s=0.1) == "NEW"
    zot.journal_articles.assert_not_called()  # the full scan is gone


# ---------------------------------------------------------------------------
# _wait_for_cloud_sync — closes the race between Desktop-save and
# the subsequent cloud-API merge.
# ---------------------------------------------------------------------------


def test_wait_for_cloud_sync_returns_true_when_item_is_visible() -> None:
    """The item is already in the cloud on first poll."""
    zot = MagicMock()
    zot.cloud.item.return_value = {"key": "NEW", "data": {}}
    assert _wait_for_cloud_sync(zot, "NEW", timeout_s=0.2) is True


def test_wait_for_cloud_sync_returns_false_on_persistent_404() -> None:
    """Cloud never replicates within timeout → return False so the
    caller can skip the merge and log a specific error."""
    zot = MagicMock()
    zot.cloud.item.side_effect = Exception("404 Not Found")
    assert _wait_for_cloud_sync(zot, "NEW", timeout_s=0.3) is False


def test_wait_for_cloud_sync_recovers_after_transient_404() -> None:
    """First two poll attempts raise 404; third returns the item.
    Simulates the common case where Desktop sync takes ~2s."""
    zot = MagicMock()
    attempts = {"n": 0}

    def fake_item(key):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise Exception("404 Not Found")
        return {"key": key, "data": {}}

    zot.cloud.item.side_effect = fake_item
    assert _wait_for_cloud_sync(zot, "NEW", timeout_s=5) is True
    assert attempts["n"] >= 2


# ---------------------------------------------------------------------------
# _wait_for_child_attachment — closes the race between parent-synced
# and PDF-child-synced. JSTOR is the canonical slow case.
# ---------------------------------------------------------------------------


def test_wait_for_child_attachment_returns_true_when_pdf_has_md5(monkeypatch) -> None:
    """Real attached PDF: md5 set and the version steady on re-read."""
    from fetchers.browser import connector
    monkeypatch.setattr(connector.time, "sleep", lambda s: None)
    pdf = {"key": "PDF1", "version": 3,
           "data": {"itemType": "attachment", "md5": "deadbeef",
                    "contentType": "application/pdf"}}
    zot = MagicMock()
    zot.cloud.children.return_value = [pdf]
    zot.cloud.item.return_value = pdf
    assert _wait_for_child_attachment(zot, "NEW", timeout_s=0.2) is True


def test_wait_for_child_attachment_refuses_a_shell_without_md5() -> None:
    """Reversed on 2026-09-23. An attachment with no md5 is one whose
    upload Zotero Desktop has not finished, and Desktop's later push of
    that attachment overwrote our re-parent: two keepers logged ATTACHED
    ended with no PDF (RPW9D2U6, FZZT98PT), their new PDFs back under the
    trashed temporary item. The merge now waits for the upload to finish."""
    zot = MagicMock()
    zot.cloud.children.return_value = [
        {"key": "PDF1",
         "data": {"itemType": "attachment", "md5": "",
                  "contentType": "application/pdf"}},
    ]
    assert _wait_for_child_attachment(zot, "NEW", timeout_s=0.2) is False


def test_wait_for_child_attachment_ignores_non_attachment_children() -> None:
    """A note-only child doesn't count — we specifically want an
    attachment (the PDF)."""
    zot = MagicMock()
    zot.cloud.children.return_value = [
        {"key": "NOTE1", "data": {"itemType": "note"}},
    ]
    assert _wait_for_child_attachment(zot, "NEW", timeout_s=0.2) is False


def test_wait_for_child_attachment_times_out_on_empty() -> None:
    """Translator saved metadata only — no attachment ever appears.
    We must time out (not hang) and return False so the caller can
    proceed with the merge and log PARTIAL."""
    zot = MagicMock()
    zot.cloud.children.return_value = []
    assert _wait_for_child_attachment(zot, "NEW", timeout_s=0.3) is False


def test_wait_for_child_attachment_recovers_after_transient_error(monkeypatch) -> None:
    """First poll raises; second returns the attachment with md5 —
    simulates the narrow window where the PDF is mid-upload."""
    zot = MagicMock()
    attempts = {"n": 0}

    def fake_children(_key):
        attempts["n"] += 1
        if attempts["n"] < 2:
            raise Exception("transient")
        return [pdf]

    from fetchers.browser import connector
    monkeypatch.setattr(connector.time, "sleep", lambda s: None)
    pdf = {"key": "PDF1", "version": 2,
           "data": {"itemType": "attachment", "md5": "abc123",
                    "contentType": "application/pdf"}}
    zot.cloud.item.return_value = pdf
    zot.cloud.children.side_effect = fake_children
    assert _wait_for_child_attachment(zot, "NEW", timeout_s=5) is True
    assert attempts["n"] >= 2


# ---------------------------------------------------------------------
# What the failure message is allowed to blame
# ---------------------------------------------------------------------


def test_the_failure_message_stops_blaming_the_library_once_one_item_saved(
) -> None:
    """"Check the left pane" is good advice for the first item and wrong
    for the fiftieth.

    Once anything has saved in this run, the library selection is
    demonstrably correct — `counter.ok`, or a save queued for merging
    (`counter.queued`), proves it — so leading with that
    cause sends the user to inspect a setting that is fine. Reported live
    against a run whose real reason was no access to those articles.
    """
    import inspect

    from fetchers.browser import connector

    src = inspect.getsource(connector)
    assert "saved = counter.ok + counter.queued" in src and "if saved:" in src, (
        "the message does not branch on what the run already knows"
    )
    # The no-access reading must be offered in both branches, since it is
    # the likeliest cause in one and a real possibility in the other.
    # Matched on a single word: these messages are hand-wrapped for the
    # terminal, so a phrase can be split across two Python literals and
    # is then unjoinable by any amount of whitespace normalising.
    assert src.count("subscription") >= 2, (
        "only one branch offers the no-access explanation"
    )


def test_both_branches_offer_the_no_access_explanation() -> None:
    """The original message listed three causes and omitted the one the
    user actually hit: they simply could not reach the article. A
    paywall, an expired subscription and a dropped VPN all hand the
    translator the same page with no PDF on it."""
    import inspect

    from fetchers.browser import connector

    src = inspect.getsource(connector).lower()
    assert "subscription" in src
    assert "vpn" in src or "proxy" in src


# ---------------------------------------------------------------------------
# Title fallback in _poll_for_new_item.
#
# Zotero translators frequently save a record with no DOI field, so a
# DOI-only match reported failure for items that had saved perfectly and
# left an unmerged duplicate holding the PDF. Three of five orphans from
# one live run had no DOI at all.
# ---------------------------------------------------------------------------

import datetime as _dt  # noqa: E402

from fetchers.browser.connector import _normalise_title  # noqa: E402


def _recent(seconds_ago: int = 1) -> str:
    return (
        _dt.datetime.now(_dt.UTC) - _dt.timedelta(seconds=seconds_ago)
    ).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_normalise_title_ignores_case_and_punctuation() -> None:
    assert (
        _normalise_title("RUSSIAN MINERS BOW TO THE ANGEL OF HISTORY")
        == _normalise_title("Russian Miners Bow to the Angel of History")
    )


def test_poll_matches_on_title_when_the_save_has_no_doi() -> None:
    zot = MagicMock()
    zot.recent_items.return_value = [
        {"key": "NEW", "data": {"DOI": "", "title": "Scientific Specialties",
                                "dateAdded": _recent()}},
    ]
    assert _poll_for_new_item(
        zot, "10.1/x", "KEEPER", timeout_s=0.2,
        title="Scientific  Specialties!",
    ) == "NEW"


def test_poll_title_match_ignores_items_predating_the_poll() -> None:
    """Without the recency window a title match would return a
    pre-existing copy and the caller would merge the wrong pair."""
    zot = MagicMock()
    zot.recent_items.return_value = [
        {"key": "OLDCOPY", "data": {"DOI": "", "title": "Scientific Specialties",
                                    "dateAdded": "2019-01-01T00:00:00Z"}},
    ]
    assert _poll_for_new_item(
        zot, "10.1/x", "KEEPER", timeout_s=0.2,
        title="Scientific Specialties",
    ) is None


def test_poll_doi_match_is_not_subject_to_the_recency_window() -> None:
    """A DOI identifies the article on its own; narrowing that path
    would change behaviour that was already correct."""
    zot = MagicMock()
    zot.recent_items.return_value = [
        {"key": "NEW", "data": {"DOI": "10.1/x",
                                "dateAdded": "2019-01-01T00:00:00Z"}},
    ]
    assert _poll_for_new_item(
        zot, "10.1/x", "KEEPER", timeout_s=0.2, title="anything",
    ) == "NEW"


# ---------------------------------------------------------------------------
# Search-result-page guard.
#
# A BMJ item routed to JSTOR produced doBasicSearch?Query=sn:09598138 AND
# surname:"Iacobucci" AND year:2017 — 172 hits, JSTOR itself admitting the
# inbound link had no exact match. Firing the translator there blocks the
# run on Zotero's item picker, and any non-exact pick attaches the wrong
# article's PDF.
# ---------------------------------------------------------------------------


import asyncio  # noqa: E402

from fetchers.browser.connector import (  # noqa: E402
    _click_matching_result,
    _is_result_list,
)


def test_is_result_list_detects_the_jstor_search_page() -> None:
    assert _is_result_list(
        "https://www.jstor.org/action/doBasicSearch?Query=sn%3A09598138"
        "+AND+surname%3A%22Iacobucci%22+AND+year%3A2017&so=rel"
    )
    assert _is_result_list("https://www.jstor.org/action/doAdvancedSearch?q=x")


def test_is_result_list_passes_a_real_jstor_article_through() -> None:
    assert not _is_result_list("https://www.jstor.org/stable/3116217")


def test_is_result_list_does_not_catch_other_platforms() -> None:
    """EBSCO's result pages resolve to the article on their own; catching
    them here would skip items that currently succeed."""
    assert not _is_result_list(
        "https://web.p.ebscohost.com/ehost/results?vid=1&sid=abc"
    )
    assert not _is_result_list("")


class _FakePage:
    def __init__(self, href: str = "", raise_on_goto: bool = False) -> None:
        self._href = href
        self._raise = raise_on_goto
        self.url = "https://www.jstor.org/action/doBasicSearch?Query=x"
        self.goto_calls: list[str] = []

    async def evaluate(self, _script, _arg=None):
        return self._href

    async def goto(self, url, **_kw):
        if self._raise:
            raise RuntimeError("nav failed")
        self.goto_calls.append(url)
        self.url = url

    async def wait_for_timeout(self, _ms):
        return None


def test_click_matching_result_navigates_on_an_exact_title_match() -> None:
    page = _FakePage(href="https://www.jstor.org/stable/999")
    assert asyncio.run(
        _click_matching_result(page, "Some Article Title")
    ) is True
    assert page.goto_calls == ["https://www.jstor.org/stable/999"]


def test_click_matching_result_gives_up_when_nothing_matches() -> None:
    """No fuzzy fallback on purpose — we are here because the resolver
    already failed to identify the article, so a second guess would
    attach some other paper."""
    page = _FakePage(href="")
    assert asyncio.run(
        _click_matching_result(page, "Some Article Title")
    ) is False
    assert page.goto_calls == []


def test_click_matching_result_needs_a_title() -> None:
    page = _FakePage(href="https://www.jstor.org/stable/999")
    assert asyncio.run(_click_matching_result(page, "")) is False
    assert page.goto_calls == []


# ---------------------------------------------------------------------------
# A folder the OS will not let us list
# ---------------------------------------------------------------------------
#
# macOS privacy protection can refuse a terminal or editor access to
# Chrome's app data: the Connector folder exists, but listing it raises
# PermissionError. That used to escape from the handler's constructor and
# end a whole attended run after Pass 2, with 314 queued Connector items
# never attempted.


@pytest.fixture
def unreadable(tmp_path: Path):
    import sys
    if sys.platform == "win32":
        pytest.skip("POSIX permission bits")
    blocked = tmp_path / "blocked-ext"
    (blocked / "5.0.211_0").mkdir(parents=True)
    blocked.chmod(0)
    try:
        next(blocked.iterdir())
    except PermissionError:
        pass
    else:
        blocked.chmod(0o755)
        pytest.skip("running with privileges that ignore permission bits")
    yield blocked
    blocked.chmod(0o755)


def test_an_unlistable_folder_falls_through_instead_of_raising(
    monkeypatch, tmp_path: Path, unreadable: Path,
) -> None:
    from fetchers.browser import connector

    installed = tmp_path / "other" / "5.0.300_0"
    installed.mkdir(parents=True)
    monkeypatch.setattr(
        connector, "_default_extension_search_paths",
        lambda: [unreadable, tmp_path / "other"],
    )
    assert resolve_connector_extension_path(unreadable) == installed


def test_a_blocked_extension_is_named_with_its_remedies(
    monkeypatch, unreadable: Path,
) -> None:
    from fetchers.browser import connector

    monkeypatch.setattr(connector, "_default_extension_search_paths", lambda: [])
    assert resolve_connector_extension_path(unreadable) is None
    problem = connector.connector_extension_problem(unreadable)
    assert str(unreadable) in problem
    assert "Privacy & Security" in problem
    assert "extension_dir" in problem


def test_no_problem_when_the_extension_resolves(monkeypatch, tmp_path: Path) -> None:
    from fetchers.browser import connector

    ok = tmp_path / "ext" / "5.0.1_0"
    ok.mkdir(parents=True)
    monkeypatch.setattr(connector, "_default_extension_search_paths", lambda: [])
    assert connector.connector_extension_problem(tmp_path / "ext") is None


def test_the_connector_check_runs_before_any_work() -> None:
    import inspect

    import enrich_pdfs

    src = inspect.getsource(enrich_pdfs.main)
    assert src.index("_check_connector_access(") < src.index("os.makedirs(args.cache_dir")


def test_versions_are_compared_numerically(monkeypatch, tmp_path: Path) -> None:
    """As strings, "5.0.99_0" sorts above "5.0.215_0"; Chrome can hold
    both for a moment during an update."""
    from fetchers.browser import connector

    base = tmp_path / "ext"
    for v in ("5.0.99_0", "5.0.215_0"):
        (base / v).mkdir(parents=True)
    monkeypatch.setattr(connector, "_default_extension_search_paths", lambda: [])
    assert resolve_connector_extension_path(base) == base / "5.0.215_0"


# ---------------------------------------------------------------------------
# Logged-out proxy — a sign-in page is not an access verdict
# ---------------------------------------------------------------------------

_WRAPPED = "http://ezproxy.jyu.fi/login?url=https://doi.org/10.1016/j.ejor.2022.11.003"
_REWRITTEN = "https://research-ebsco-com.ezproxy.jyu.fi/linkprocessor/plink?id=1"


def test_a_proxy_login_page_is_recognised() -> None:
    """Twice on 2026-09-23 a relaunched Connector profile (EZproxy keeps
    its session in a browser-session cookie) landed on the proxy's
    sign-in page. "No translator" followed and the items were logged
    ACCESS_BLOCKED, removed by hand afterwards.
    """
    from fetchers.browser.connector import _proxy_base, is_proxy_login_url

    assert _proxy_base(_WRAPPED) == "ezproxy.jyu.fi"
    assert _proxy_base(_REWRITTEN) == "ezproxy.jyu.fi"
    for target in (_WRAPPED, _REWRITTEN):
        # EZproxy's own form, and the institution's IdP behind it.
        assert is_proxy_login_url("https://ezproxy.jyu.fi/login?url=x", target)
        assert is_proxy_login_url(
            "https://login.jyu.fi/idp/profile/SAML2/Redirect/SSO?execution=e1s1",
            target,
        )
        # Signed in: the publisher rewritten onto the proxy.
        assert not is_proxy_login_url(
            "https://www-sciencedirect-com.ezproxy.jyu.fi/science/article/pii/S1",
            target,
        )


def test_unproxied_routes_are_never_judged() -> None:
    """Most publisher pages link to a login of their own. Without a
    signing-in proxy in the route there is nothing to be logged out of,
    and Aalto's IP-authenticated OCLC proxy must not be dragged in.
    """
    from fetchers.browser.connector import _proxy_base, is_proxy_login_url

    direct = "https://www.sciencedirect.com/science/article/pii/S1"
    assert not is_proxy_login_url("https://login.elsevier.com/x", direct)
    oclc = "https://login.aalto-libproxy.idm.oclc.org/login?url=https://doi.org/10.1/x"
    assert _proxy_base(oclc) == ""
    # Off the proxy on an ordinary publisher page is not a login page.
    assert not is_proxy_login_url("https://direct.mit.edu/article/1", _WRAPPED)


def test_login_required_is_its_own_status() -> None:
    import enrich_pdfs

    assert "connector_login_required" in enrich_pdfs.pdf_run_report.STATUS_INFO
    assert "connector_login_required" not in enrich_pdfs.DONE_STATUSES
