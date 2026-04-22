"""Unit tests for fetchers/library_resolver.py.

Uses vendored SFX XML fixtures under `tests/fixtures/sfx/` (gitignored
because SFX responses embed institution-specific metadata). Each test
that needs a fixture skips cleanly if the file is absent — a fresh
clone without fixtures runs everything else without noise.

Capture fresh fixtures against your own endpoint by running:
    curl -sS "<openurl_base>?url_ver=Z39.88-2004&ctx_ver=...&rft_id=info:doi/<DOI>&..." \\
        > tests/fixtures/sfx/has_fulltext.xml
(see the fixture-capture block at the top of the file for full command)
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fetchers.library_resolver import (
    SFX_PLATFORM_PRIORITY,
    LibraryResolverConfig,
    SfxCache,
    SfxDualResult,
    _build_query_url,
    _count_fulltext_targets,
    _effective_host,
    _fulltext_target_urls,
    _target_matches_domains,
    first_fulltext_target_preferred,
    has_fulltext_access,
    load_from_config,
    sfx_lookup_dual,
)

# ---------------------------------------------------------------------------
# Fixture directory
# ---------------------------------------------------------------------------

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sfx"


def _load_fixture(name: str) -> str:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(
            f"SFX fixture {name!r} not present at {path}. Capture one "
            f"against your own endpoint — see this file's docstring."
        )
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# XML parsing — pure functions, no network
# ---------------------------------------------------------------------------


def test_count_fulltext_targets_returns_zero_when_only_ancillary_services() -> None:
    """An SFX response for a DOI the library has no full-text access to
    lists ancillary services (MELINDA holdings, Google Scholar search,
    WoS author profile) but zero `getFullTxt` targets."""
    xml = _load_fixture("no_fulltext.xml")
    assert _count_fulltext_targets(xml) == 0


def test_count_fulltext_targets_returns_positive_on_fulltext_target() -> None:
    """A DOI within the library's coverage shows at least one
    `getFullTxt` entry."""
    xml = _load_fixture("has_fulltext.xml")
    count = _count_fulltext_targets(xml)
    assert count >= 1


def test_count_fulltext_targets_returns_minus_one_on_malformed_xml() -> None:
    """Parse failure is reported as -1 so callers can tell it apart
    from 'parsed but zero targets'."""
    assert _count_fulltext_targets("this is not xml at all") == -1


def test_count_fulltext_targets_ignores_non_fulltext_service_types() -> None:
    """Synthetic XML: only one of the four targets is full-text."""
    xml = (
        "<ctx_obj_set><ctx_obj><targets>"
        "<target><service_type>getHolding</service_type>"
        "<target_url>https://a</target_url></target>"
        "<target><service_type>getFullTxt</service_type>"
        "<target_url>https://b</target_url></target>"
        "<target><service_type>getAuthor</service_type>"
        "<target_url>https://c</target_url></target>"
        "<target><service_type>getWebSearch</service_type>"
        "<target_url>https://d</target_url></target>"
        "</targets></ctx_obj></ctx_obj_set>"
    )
    assert _count_fulltext_targets(xml) == 1


# ---------------------------------------------------------------------------
# _fulltext_target_urls — returns the actual target URLs, not just a count
# ---------------------------------------------------------------------------


def test_fulltext_target_urls_pairs_service_and_url_within_target() -> None:
    """Only the <target_url> inside a <target> whose <service_type> is
    getFullTxt is returned — non-fulltext targets' urls are ignored."""
    xml = (
        "<ctx_obj_set><ctx_obj><targets>"
        "<target>"
        "  <service_type>getHolding</service_type>"
        "  <target_url>https://melinda.example/record</target_url>"
        "</target>"
        "<target>"
        "  <service_type>getFullTxt</service_type>"
        "  <target_url>https://onlinelibrary.wiley.com/doi/x</target_url>"
        "</target>"
        "<target>"
        "  <service_type>getFullTxt</service_type>"
        "  <target_url>https://www.jstor.org/stable/y</target_url>"
        "</target>"
        "</targets></ctx_obj></ctx_obj_set>"
    )
    urls = _fulltext_target_urls(xml)
    assert urls is not None
    assert len(urls) == 2
    assert any("wiley.com" in u for u in urls)
    assert any("jstor.org" in u for u in urls)
    assert not any("melinda" in u for u in urls)


def test_fulltext_target_urls_returns_none_on_parse_error() -> None:
    assert _fulltext_target_urls("not xml at all") is None


# ---------------------------------------------------------------------------
# _effective_host — EZproxy/link-resolver unwrapping
# ---------------------------------------------------------------------------


def test_effective_host_unwraps_ezproxy() -> None:
    """EZproxy URLs carry the real target URL in a `url=` query param;
    the effective host is the target, not the proxy."""
    url = (
        "http://ezproxy.jyu.fi/login"
        "?url=https://onlinelibrary.wiley.com/doi/10.1002/x"
    )
    assert _effective_host(url) == "onlinelibrary.wiley.com"


def test_effective_host_returns_host_when_not_wrapped() -> None:
    assert (
        _effective_host("https://www.jstor.org/stable/123")
        == "www.jstor.org"
    )


def test_effective_host_returns_empty_on_malformed_input() -> None:
    assert _effective_host("") == ""


def test_effective_host_handles_ebscohost_url_param() -> None:
    """EBSCOhost targets nest their real URL after `url=`."""
    url = (
        "http://ezproxy.jyu.fi/login"
        "?url=https://openurl.ebscohost.com/linksvc/linking.aspx?doi=10.1"
    )
    assert _effective_host(url) == "openurl.ebscohost.com"


# ---------------------------------------------------------------------------
# _target_matches_domains — suffix matching with EZproxy-aware host
# ---------------------------------------------------------------------------


def test_target_matches_domains_exact_match() -> None:
    assert _target_matches_domains(
        "https://onlinelibrary.wiley.com/doi/x",
        ("onlinelibrary.wiley.com",),
    )


def test_target_matches_domains_suffix_match() -> None:
    """Matching by domain suffix lets `wiley.com` match
    `onlinelibrary.wiley.com` without a separate entry per subdomain."""
    assert _target_matches_domains(
        "https://onlinelibrary.wiley.com/doi/x",
        ("wiley.com",),
    )


def test_target_matches_domains_rejects_unrelated_host() -> None:
    """SFX's indirect routes (JSTOR, EBSCOhost) must NOT match the
    publisher-direct domain filter."""
    assert not _target_matches_domains(
        "https://www.jstor.org/stable/x",
        ("pubsonline.informs.org",),
    )


def test_target_matches_domains_unwraps_ezproxy_before_matching() -> None:
    assert _target_matches_domains(
        "http://ezproxy.jyu.fi/login"
        "?url=https://onlinelibrary.wiley.com/doi/x",
        ("wiley.com",),
    )
    # EZproxy wrapper host alone must NOT match the publisher filter.
    assert not _target_matches_domains(
        "http://ezproxy.jyu.fi/login?url=https://www.jstor.org/x",
        ("onlinelibrary.wiley.com",),
    )


def test_target_matches_domains_is_false_for_substring_false_positive() -> None:
    """A host that shares a substring but isn't a suffix must not match
    — e.g. `evil-wiley.com` should not match `wiley.com`."""
    assert not _target_matches_domains(
        "https://fake-wiley.com/doi/x",
        ("wiley.com",),
    )


# ---------------------------------------------------------------------------
# URL construction
# ---------------------------------------------------------------------------


def test_build_query_url_includes_required_openurl_params() -> None:
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=MagicMock(),
    )
    url = _build_query_url("10.1111/x.y", cfg)
    assert url.startswith("https://example.org/sfx?")
    # Key parameters SFX needs.
    assert "rft_id=info%3Adoi%2F10.1111%2Fx.y" in url
    assert "url_ver=Z39.88-2004" in url
    assert "sfx.response_type=multi_obj_xml" in url


def test_build_query_url_carries_custom_sid() -> None:
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=MagicMock(),
        sid="my-project",
    )
    assert "sfx.sid=my-project" in _build_query_url("10.1/x", cfg)


# ---------------------------------------------------------------------------
# has_fulltext_access — network mocked, fail-open on errors
# ---------------------------------------------------------------------------


def _cfg_with_response(body: str, status: int = 200) -> LibraryResolverConfig:
    """Build a resolver config whose session.get returns `body`."""
    fake_resp = MagicMock()
    fake_resp.status_code = status
    fake_resp.text = body
    session = MagicMock()
    session.get.return_value = fake_resp
    return LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=session,
    )


def test_has_fulltext_access_false_on_empty_fulltext_targets() -> None:
    xml = _load_fixture("no_fulltext.xml")
    cfg = _cfg_with_response(xml)
    assert has_fulltext_access("10.1/x", cfg) is False


def test_has_fulltext_access_true_when_library_has_coverage() -> None:
    xml = _load_fixture("has_fulltext.xml")
    cfg = _cfg_with_response(xml)
    assert has_fulltext_access("10.1016/x.y", cfg) is True


def test_has_fulltext_access_is_true_when_openurl_base_unset() -> None:
    """No library configured → pre-flight is a no-op; callers proceed."""
    cfg = LibraryResolverConfig(
        openurl_base="",
        session=MagicMock(),
    )
    assert has_fulltext_access("10.1/x", cfg) is True


def test_has_fulltext_access_fail_open_on_network_error() -> None:
    """Transport failure doesn't block the pipeline — the handler will
    try and possibly succeed."""
    session = MagicMock()
    session.get.side_effect = RuntimeError("connection reset")
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=session,
    )
    assert has_fulltext_access("10.1/x", cfg) is True


def test_has_fulltext_access_fail_open_on_non_200() -> None:
    cfg = _cfg_with_response("oops", status=503)
    assert has_fulltext_access("10.1/x", cfg) is True


def test_has_fulltext_access_fail_open_on_malformed_xml() -> None:
    """Parse failure → unknown → proceed (fail-open)."""
    cfg = _cfg_with_response("this is not xml")
    assert has_fulltext_access("10.1/x", cfg) is True


# ---------------------------------------------------------------------------
# required_domains filter — only count targets our handler can reach
# ---------------------------------------------------------------------------


def _synthetic_sfx_xml(fulltext_urls: list[str]) -> str:
    """Minimal SFX-shaped XML listing the given URLs as full-text targets."""
    targets = "".join(
        f"<target><service_type>getFullTxt</service_type>"
        f"<target_url>{u}</target_url></target>"
        for u in fulltext_urls
    )
    return (
        f"<ctx_obj_set><ctx_obj><targets>{targets}</targets></ctx_obj></ctx_obj_set>"
    )


def test_has_fulltext_access_respects_required_domains() -> None:
    """Real INFORMS case (2026-04-21): SFX reports access via JSTOR and
    EBSCOhost for the DOI, but our InformsHandler only knows the
    direct-publisher URL. `required_domains=("pubsonline.informs.org",)`
    must return False because neither JSTOR nor EBSCOhost is reachable
    by the handler."""
    xml = _synthetic_sfx_xml([
        "http://links.jstor.org/openurl?x",
        "http://ezproxy.jyu.fi/login?url=https://openurl.ebscohost.com/y",
    ])
    cfg = _cfg_with_response(xml)
    assert has_fulltext_access(
        "10.1287/x",
        cfg,
        required_domains=("pubsonline.informs.org",),
    ) is False


def test_has_fulltext_access_allows_matching_direct_route() -> None:
    """When SFX reports a target on the handler's direct-access domain
    (here: Wiley via the standard EZproxy wrapper), access is True."""
    xml = _synthetic_sfx_xml([
        "http://ezproxy.jyu.fi/login?url=https://onlinelibrary.wiley.com/doi/x",
    ])
    cfg = _cfg_with_response(xml)
    assert has_fulltext_access(
        "10.1002/x",
        cfg,
        required_domains=("wiley.com",),
    ) is True


def test_has_fulltext_access_without_required_domains_allows_indirect_routes() -> None:
    """Backwards-compat: empty `required_domains` means any full-text
    target (including JSTOR) counts as access. Callers who care about
    platform specificity must pass domains explicitly."""
    xml = _synthetic_sfx_xml(["http://links.jstor.org/openurl?x"])
    cfg = _cfg_with_response(xml)
    assert has_fulltext_access("10.1/x", cfg) is True
    assert has_fulltext_access("10.1/x", cfg, required_domains=()) is True


def test_required_domains_filter_uses_single_cached_url_list(tmp_path: Path) -> None:
    """v0.4.0: the cache stores the raw SFX target URL list per DOI,
    not a pre-filtered access bool. Different handlers for the same
    DOI reach different conclusions by filtering the same cached
    list — only ONE network call per DOI, regardless of how many
    handlers query it."""
    c = SfxCache(tmp_path)
    xml = _synthetic_sfx_xml([
        "https://onlinelibrary.wiley.com/doi/x",
    ])
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = xml
    session = MagicMock()
    session.get.return_value = fake_resp
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=session,
        cache=c,
    )

    # Wiley handler sees the Wiley target → access True.
    assert has_fulltext_access(
        "10.1/x", cfg, required_domains=("wiley.com",),
    ) is True
    # INFORMS handler applies a different filter to the same cached
    # URL list → access False. No extra network call.
    assert has_fulltext_access(
        "10.1/x", cfg, required_domains=("informs.org",),
    ) is False
    # Confirm we reached the network exactly once.
    assert session.get.call_count == 1


# ---------------------------------------------------------------------------
# _build_query_url — ignore_date_threshold parameter (v0.4.0)
# ---------------------------------------------------------------------------


def test_build_query_url_omits_ignore_date_flag_by_default() -> None:
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=MagicMock(),
    )
    url = _build_query_url("10.1/x", cfg)
    assert "sfx.ignore_date_threshold" not in url


def test_build_query_url_adds_ignore_date_flag_when_requested() -> None:
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=MagicMock(),
    )
    url = _build_query_url("10.1/x", cfg, ignore_date_threshold=True)
    assert "sfx.ignore_date_threshold=1" in url


# ---------------------------------------------------------------------------
# sfx_lookup_dual — the two SFX queries that classify Case 1 / 2 / 3
# ---------------------------------------------------------------------------


def _dual_session(*, in_range_urls: list[str], any_urls: list[str]):
    """Mock session whose `.get` returns `in_range_urls` for the
    default query and `any_urls` when `sfx.ignore_date_threshold=1`
    is present. Returns (session, calls) so tests can inspect the
    request history."""
    calls: list[str] = []

    def fake_get(url, **_kw):
        calls.append(url)
        resp = MagicMock()
        resp.status_code = 200
        if "sfx.ignore_date_threshold=1" in url:
            resp.text = _synthetic_sfx_xml(any_urls)
        else:
            resp.text = _synthetic_sfx_xml(in_range_urls)
        return resp

    sess = MagicMock()
    sess.get.side_effect = fake_get
    return sess, calls


def test_sfx_lookup_dual_returns_both_lists_case_3(tmp_path: Path) -> None:
    """Case 3 (Wiley 2015 at JYU): library covers this DOI. Both
    queries return the Wiley target."""
    sess, calls = _dual_session(
        in_range_urls=[
            "http://ezproxy.jyu.fi/login?url=https://onlinelibrary.wiley.com/x",
        ],
        any_urls=[
            "http://ezproxy.jyu.fi/login?url=https://onlinelibrary.wiley.com/x",
        ],
    )
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=sess, cache=SfxCache(tmp_path),
    )
    result = sfx_lookup_dual("10.1002/wiley2015", cfg)
    assert result.query_ok
    assert len(result.in_range) == 1
    assert len(result.any_range) == 1
    # Two separate HTTP calls — one per query — then cached.
    assert len(calls) == 2


def test_sfx_lookup_dual_case_2_wiley_out_of_coverage(tmp_path: Path) -> None:
    """Case 2 (Wiley 1993): library knows about Wiley for this journal
    but this year is out of range. Query A lists Wiley, Query B is
    empty."""
    sess, _ = _dual_session(
        in_range_urls=[],
        any_urls=[
            "http://ezproxy.jyu.fi/login?url=https://onlinelibrary.wiley.com/x",
            "http://ezproxy.jyu.fi/login?url=https://openurl.ebsco.com/x",
        ],
    )
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=sess, cache=SfxCache(tmp_path),
    )
    result = sfx_lookup_dual("10.1002/wiley1993", cfg)
    assert result.query_ok
    assert result.in_range == []
    assert len(result.any_range) == 2
    # The case-2 detector used by enrich_pdfs.py:
    wiley_in_any = any(
        _target_matches_domains(u, ("wiley.com",)) for u in result.any_range
    )
    wiley_in_range = any(
        _target_matches_domains(u, ("wiley.com",)) for u in result.in_range
    )
    assert wiley_in_any and not wiley_in_range


def test_sfx_lookup_dual_case_1_aom_no_sfx_relationship(tmp_path: Path) -> None:
    """Case 1 (AoM at JYU): library has no AoM relationship at all —
    both queries are empty. Direct handler should still be tried
    (user might be a member)."""
    sess, _ = _dual_session(in_range_urls=[], any_urls=[])
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=sess, cache=SfxCache(tmp_path),
    )
    result = sfx_lookup_dual("10.5465/amj.x", cfg)
    assert result.query_ok
    assert result.in_range == []
    assert result.any_range == []


def test_sfx_lookup_dual_caches_both_queries_separately(tmp_path: Path) -> None:
    """The two SFX queries get independent cache slots; a second call
    on the same DOI makes zero network calls."""
    cache = SfxCache(tmp_path)
    sess, calls = _dual_session(
        in_range_urls=["https://onlinelibrary.wiley.com/x"],
        any_urls=["https://onlinelibrary.wiley.com/x"],
    )
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=sess, cache=cache,
    )
    sfx_lookup_dual("10.1002/x", cfg)
    assert len(calls) == 2

    # Second call: everything from cache.
    sess2, calls2 = _dual_session(
        in_range_urls=[],                # if hit, would produce []
        any_urls=[],
    )
    cfg2 = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=sess2, cache=SfxCache(tmp_path),
    )
    res = sfx_lookup_dual("10.1002/x", cfg2)
    assert calls2 == []
    assert res.in_range and res.any_range   # Still the cached URLs.


def test_sfx_lookup_dual_returns_query_ok_false_when_sfx_fails() -> None:
    session = MagicMock()
    session.get.side_effect = RuntimeError("network down")
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=session,
    )
    result = sfx_lookup_dual("10.1/x", cfg)
    assert result.query_ok is False
    assert result.in_range == []
    assert result.any_range == []


def test_sfx_lookup_dual_returns_empty_result_when_unset() -> None:
    cfg = LibraryResolverConfig(openurl_base="", session=MagicMock())
    result = sfx_lookup_dual("10.1/x", cfg)
    assert not result.query_ok


# ---------------------------------------------------------------------------
# first_fulltext_target_preferred — platform priority ranking
# ---------------------------------------------------------------------------


def test_first_fulltext_target_preferred_picks_ebsco_over_jstor() -> None:
    """When SFX offers EBSCOhost + JSTOR + ProQuest routes, the handler
    should pick EBSCOhost per SFX_PLATFORM_PRIORITY."""
    xml = _synthetic_sfx_xml([
        "https://www.jstor.org/stable/x",
        "http://ezproxy.jyu.fi/login?url=https://openurl.ebscohost.com/x",
        "https://search.proquest.com/docview/x",
    ])
    cfg = _cfg_with_response(xml)
    target = first_fulltext_target_preferred("10.1/x", cfg)
    assert target is not None
    assert "ebscohost.com" in target


def test_first_fulltext_target_preferred_falls_back_when_nothing_ranked() -> None:
    """When no target matches any entry in the priority list, the
    function still returns a target (SFX's response order decides)."""
    xml = _synthetic_sfx_xml([
        "https://unknown-platform.example/a",
        "https://another-unknown.example/b",
    ])
    cfg = _cfg_with_response(xml)
    target = first_fulltext_target_preferred("10.1/x", cfg)
    assert target == "https://unknown-platform.example/a"


def test_first_fulltext_target_preferred_returns_none_with_no_targets() -> None:
    xml = _synthetic_sfx_xml([])
    cfg = _cfg_with_response(xml)
    assert first_fulltext_target_preferred("10.1/x", cfg) is None


def test_first_fulltext_target_preferred_unset_returns_none() -> None:
    cfg = LibraryResolverConfig(openurl_base="", session=MagicMock())
    assert first_fulltext_target_preferred("10.1/x", cfg) is None


def test_first_fulltext_target_preferred_filters_by_required_domains() -> None:
    """required_domains restricts the candidates: no matching target →
    None even when other full-text routes exist."""
    xml = _synthetic_sfx_xml([
        "https://www.jstor.org/stable/x",
    ])
    cfg = _cfg_with_response(xml)
    assert first_fulltext_target_preferred(
        "10.1/x", cfg, required_domains=("pubsonline.informs.org",),
    ) is None


def test_first_fulltext_target_preferred_stable_tie_break() -> None:
    """Two targets at the same priority rank return the first one
    SFX listed (list order preserved)."""
    xml = _synthetic_sfx_xml([
        "https://onlinelibrary.wiley.com/doi/a",
        "https://onlinelibrary.wiley.com/doi/b",
    ])
    cfg = _cfg_with_response(xml)
    target = first_fulltext_target_preferred("10.1/x", cfg)
    # Both rank equally (both "onlinelibrary.wiley.com" → same index).
    # `min` with stable key preserves original order.
    assert target == "https://onlinelibrary.wiley.com/doi/a"


def test_sfx_platform_priority_is_non_empty() -> None:
    assert isinstance(SFX_PLATFORM_PRIORITY, tuple)
    assert len(SFX_PLATFORM_PRIORITY) > 0
    # EBSCOhost is the current top-ranked platform.
    assert SFX_PLATFORM_PRIORITY[0] == "ebscohost.com"


def test_sfx_dual_result_dataclass_fields() -> None:
    r = SfxDualResult(in_range=["a"], any_range=["a", "b"])
    assert r.in_range == ["a"]
    assert r.any_range == ["a", "b"]
    assert r.query_ok is True


# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# SfxCache
# ---------------------------------------------------------------------------


def test_sfx_cache_round_trips_data(tmp_path: Path) -> None:
    c = SfxCache(tmp_path)
    c.put("10.1/x", {"has_access": True, "targets": 3})
    c.put("10.1/y", {"has_access": False, "targets": 0})

    # Read back via a fresh instance — exercises the on-disk load path.
    c2 = SfxCache(tmp_path)
    assert c2.get("10.1/x") == {"has_access": True, "targets": 3}
    assert c2.get("10.1/y") == {"has_access": False, "targets": 0}
    assert c2.get("10.1/z") is None


def test_sfx_cache_recovers_from_corrupt_json(tmp_path: Path) -> None:
    """A corrupt cache file must not crash the pipeline. Start fresh."""
    (tmp_path / "sfx_cache.json").write_text("{not valid json")
    c = SfxCache(tmp_path)
    assert c.get("10.1/x") is None
    c.put("10.1/x", {"has_access": True, "targets": 1})
    assert c.get("10.1/x") == {"has_access": True, "targets": 1}


def test_has_fulltext_access_uses_cache_on_hit(tmp_path: Path) -> None:
    """A cached 'no access' answer must not hit the network a second time.

    v0.4.0 cache shape is `{"urls": [...]}` — the empty list encodes
    "SFX returned no full-text targets".
    """
    c = SfxCache(tmp_path)
    c.put("10.1/already_checked", {"urls": []})

    session = MagicMock()
    session.get.side_effect = AssertionError("must not hit network")
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=session,
        cache=c,
    )
    assert has_fulltext_access("10.1/already_checked", cfg) is False


def test_has_fulltext_access_writes_cache_on_miss(tmp_path: Path) -> None:
    c = SfxCache(tmp_path)
    xml = _load_fixture("no_fulltext.xml")
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = xml
    session = MagicMock()
    session.get.return_value = fake_resp
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=session,
        cache=c,
    )
    assert has_fulltext_access("10.1/new", cfg) is False
    # Cache now has the URL list for this DOI. No full-text targets →
    # empty list; the has_access bool is derived, not stored.
    c2 = SfxCache(tmp_path)
    assert c2.get("10.1/new") == {"urls": []}


def test_legacy_cache_entry_without_urls_is_re_queried(tmp_path: Path) -> None:
    """A v0.3.x cache entry (shape `{has_access, targets}` without a
    `urls` key) is treated as a miss so v0.4.0 callers can derive the
    filtered access bool from the full URL list."""
    c = SfxCache(tmp_path)
    c.put("10.1/legacy", {"has_access": True, "targets": 1})

    xml = _synthetic_sfx_xml([
        "http://ezproxy.jyu.fi/login?url=https://onlinelibrary.wiley.com/x",
    ])
    fake_resp = MagicMock()
    fake_resp.status_code = 200
    fake_resp.text = xml
    session = MagicMock()
    session.get.return_value = fake_resp
    cfg = LibraryResolverConfig(
        openurl_base="https://example.org/sfx",
        session=session,
        cache=c,
    )
    assert has_fulltext_access("10.1/legacy", cfg,
                                required_domains=("wiley.com",)) is True
    # After the re-query the cache is upgraded to the new shape.
    reloaded = SfxCache(tmp_path).get("10.1/legacy")
    assert reloaded is not None and "urls" in reloaded


# ---------------------------------------------------------------------------
# load_from_config
# ---------------------------------------------------------------------------


def test_load_from_config_returns_none_when_openurl_base_unset(monkeypatch) -> None:
    from core import config_loader
    monkeypatch.setattr(config_loader, "load_config", lambda: {})
    monkeypatch.delenv("LIBRARY_OPENURL_BASE", raising=False)
    assert load_from_config(MagicMock()) is None


def test_load_from_config_returns_config_when_present(monkeypatch, tmp_path) -> None:
    from core import config_loader
    monkeypatch.setattr(config_loader, "load_config", lambda: {
        "library": {"openurl_base": "https://example.org/sfx"}
    })
    monkeypatch.delenv("LIBRARY_OPENURL_BASE", raising=False)
    cfg = load_from_config(MagicMock(), cache_dir=tmp_path)
    assert cfg is not None
    assert cfg.openurl_base == "https://example.org/sfx"
    assert cfg.cache is not None
