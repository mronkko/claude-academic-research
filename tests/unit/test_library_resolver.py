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
    LibraryResolverConfig,
    SfxCache,
    _build_query_url,
    _count_fulltext_targets,
    _effective_host,
    _fulltext_target_urls,
    _target_matches_domains,
    has_fulltext_access,
    load_from_config,
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


def test_required_domains_cache_is_keyed_separately(tmp_path: Path) -> None:
    """Different handlers for the same DOI can legitimately get
    different answers (one handler's domain matches, another's
    doesn't). Cache keys must incorporate the domain filter."""
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
    # INFORMS handler sees the same SFX response but for its required
    # domain there's no match → access False. Cache must not return
    # Wiley's True answer here.
    assert has_fulltext_access(
        "10.1/x", cfg, required_domains=("informs.org",),
    ) is False


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
    """A cached 'no access' answer must not hit the network a second time."""
    c = SfxCache(tmp_path)
    c.put("10.1/already_checked", {"has_access": False, "targets": 0})

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
    # Confirm the cache file now has an entry.
    c2 = SfxCache(tmp_path)
    assert c2.get("10.1/new") == {"has_access": False, "targets": 0}


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
