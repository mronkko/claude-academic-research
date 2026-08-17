"""Resolver *policy*: caching, fail-open, coverage queries, config.

Dialect specifics live in `test_resolvers_dialects.py`; ranking and
domain matching in `test_resolvers_ranking.py`. This module covers the
decisions `library_resolver.py` makes regardless of which dialect
answered — and each of them encodes a bug that reached a real run:

- **Fail-open.** `query_ok=False` means "could not ask", never "no
  access". Gating a browser open on an ambiguous signal makes a transport
  blip indistinguishable from a real entitlement gap.
- **Positive-only caching.** An empty answer must never be persisted. It
  once turned a soft DOI-keying miss permanent: 15 *Journal of Business
  Ethics* articles were skipped that the user demonstrably had access to,
  and re-running could not re-check because the empty answer was cached
  with no expiry.
- **One query on Alma.** `date_filtering_available=False` tells callers
  the two coverage lists are the same request, so they cannot read a
  coverage verdict out of comparing them.

Captured real responses are exercised by the fixture tests at the end.
They skip on a fresh checkout because the fixtures are
institution-specific and gitignored; the inline-XML tests in
`test_resolvers_dialects.py` carry the parse contract meanwhile.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock
from xml.sax.saxutils import escape

import pytest
from fetchers.library_resolver import (
    LibraryResolverConfig,
    ResolverCache,
    has_fulltext_access,
    load_from_config,
    lookup_dual,
    lookup_fulltext_target,
    targets_match_domains,
)
from fetchers.resolvers import AlmaResolver, FulltextTarget, SfxResolver

SFX_BASE = "https://sfx.example.org/inst01"
ALMA_BASE = (
    "https://eu03.alma.exlibrisgroup.com/view/uresolver/358AALTO_INST/openurl"
)
DOI = "10.1002/hrm.21999"

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "sfx"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def sfx_xml(*targets: tuple[str, str, str]) -> str:
    """`(service_type, target_url, public_name)` triples in SFX's shape.

    Duplicated from `test_resolvers_dialects.py` rather than imported:
    `tests/unit` is not an importable package, and two four-line builders
    are cheaper than coupling test modules through sys.path.
    """
    body = "".join(
        f"<target><service_type>{svc}</service_type>"
        f"<target_url>{escape(url)}</target_url>"
        + (f"<target_public_name>{name}</target_public_name>" if name else "")
        + "</target>"
        for svc, url, name in targets
    )
    return f"<ctx_obj_set><ctx_obj><targets>{body}</targets></ctx_obj></ctx_obj_set>"


def alma_xml(*services: tuple[str, str, str, str]) -> str:
    """`(service_type, resolution_url, package, interface)` in Alma's shape."""
    body = ""
    for svc, url, package, iface in services:
        body += (
            f'<context_service service_type="{svc}">'
            f"<keys>"
            f'<key id="package_public_name">{escape(package)}</key>'
            f'<key id="interface_name">{iface}</key>'
            f"</keys>"
            f"<resolution_url>{escape(url)}</resolution_url>"
            f"</context_service>"
        )
    return f"<uresolver_content>{body}</uresolver_content>"


def _session(*, status: int = 200, text: str = "", error: bool = False):
    """Session whose every GET returns the same response."""
    session = MagicMock()
    if error:
        session.get.side_effect = RuntimeError("boom")
        return session
    resp = MagicMock()
    resp.status_code = status
    resp.text = text
    session.get.return_value = resp
    return session


def _routing_session(routes: list[tuple[str, str]]):
    """Session returning the first body whose marker appears in the URL.

    Lets one mock serve a DOI-keyed query and an ISSN-keyed fallback
    differently, which is the only way to exercise Alma's two-query flow.
    """
    def _get(url, **_kw):
        resp = MagicMock()
        resp.status_code = 200
        resp.text = ""
        for marker, body in routes:
            if marker in url:
                resp.text = body
                break
        return resp

    session = MagicMock()
    session.get.side_effect = _get
    return session


def _sfx_cfg(session, cache=None, **kw) -> LibraryResolverConfig:
    return LibraryResolverConfig(
        resolver=SfxResolver(SFX_BASE), session=session, cache=cache, **kw
    )


def _alma_cfg(session, cache=None, **kw) -> LibraryResolverConfig:
    return LibraryResolverConfig(
        resolver=AlmaResolver(ALMA_BASE), session=session, cache=cache, **kw
    )


def _ft(*urls: str) -> str:
    return sfx_xml(*(("getFullTxt", u, "") for u in urls))


# ---------------------------------------------------------------------------
# Fail-open semantics
# ---------------------------------------------------------------------------


def test_unconfigured_resolver_reports_query_not_ok() -> None:
    """A None resolver means "no pre-flight", which callers must not read
    as "no access" — that once made the whole Connector pass unreachable."""
    cfg = LibraryResolverConfig(resolver=None, session=MagicMock())
    assert lookup_fulltext_target(DOI, cfg) == (None, False)
    assert has_fulltext_access(DOI, cfg) is True


def test_transport_error_reports_query_not_ok() -> None:
    assert lookup_fulltext_target(DOI, _sfx_cfg(_session(error=True))) == (
        None, False,
    )


def test_non_200_reports_query_not_ok() -> None:
    assert lookup_fulltext_target(DOI, _sfx_cfg(_session(status=503))) == (
        None, False,
    )


def test_malformed_xml_reports_query_not_ok() -> None:
    assert lookup_fulltext_target(DOI, _sfx_cfg(_session(text="<not-xml"))) == (
        None, False,
    )


def test_empty_response_is_a_real_no_access_verdict() -> None:
    """The one case that legitimately means "no licensed route": the
    resolver answered, and had nothing."""
    assert lookup_fulltext_target(DOI, _sfx_cfg(_session(text=_ft()))) == (
        None, True,
    )


def test_has_fulltext_access_fails_open_on_every_ambiguous_signal() -> None:
    for session in (
        _session(error=True), _session(status=500), _session(text="<bad"),
    ):
        assert has_fulltext_access(DOI, _sfx_cfg(session)) is True


def test_has_fulltext_access_is_false_only_on_a_clean_empty_answer() -> None:
    assert has_fulltext_access(DOI, _sfx_cfg(_session(text=_ft()))) is False


# ---------------------------------------------------------------------------
# required_domains
# ---------------------------------------------------------------------------


def test_required_domains_rejects_indirect_routes() -> None:
    """Real INFORMS case: the resolver reports JSTOR and EBSCOhost, but
    the handler can only drive pubsonline.informs.org, so those routes are
    not access *for that handler*."""
    cfg = _sfx_cfg(_session(text=_ft(
        "https://www.jstor.org/stable/1", "https://search.ebscohost.com/x",
    )))
    assert lookup_fulltext_target(
        DOI, cfg, required_domains=("pubsonline.informs.org",),
    ) == (None, True)


def test_required_domains_accepts_a_matching_direct_route() -> None:
    cfg = _sfx_cfg(_session(text=_ft("https://onlinelibrary.wiley.com/doi/x")))
    got = lookup_fulltext_target(DOI, cfg, required_domains=("wiley.com",))
    assert got.url is not None and got.query_ok is True


def test_no_required_domains_accepts_any_route() -> None:
    cfg = _sfx_cfg(_session(text=_ft("https://www.jstor.org/stable/1")))
    assert lookup_fulltext_target(DOI, cfg).url is not None


def test_required_domains_matches_an_alma_target_by_platform_name() -> None:
    """End-to-end through the facade: the fix that makes Alma a peer.
    Before it, this returned (None, True) — "no licensed route" — for an
    article the library reaches through EBSCOhost."""
    cfg = _alma_cfg(_session(text=alma_xml((
        "getFullTxt", "https://aalto.alma.exlibrisgroup.com/view/action/x",
        "EBSCOhost Business Source Ultimate", "EBSCOhost",
    ))))
    got = lookup_fulltext_target(DOI, cfg, required_domains=("ebscohost.com",))
    assert got.url is not None and got.query_ok is True


# ---------------------------------------------------------------------------
# Ranking through the facade
# ---------------------------------------------------------------------------


def test_best_target_prefers_ebscohost_over_jstor_and_proquest() -> None:
    """EBSCOhost's own targets carry `direct=true&db=…&AN=…`, not a
    `url=` param. That matters: `effective_host` unwraps `?url=` to
    support EZproxy, so a hypothetical `ebscohost.com/login?url=<article>`
    would rank as the *inner* publisher rather than as EBSCOhost. The
    EZproxy direction is the one that occurs in practice and works —
    see `test_ezproxy_wrapped_ebscohost_still_ranks_as_ebscohost`."""
    cfg = _sfx_cfg(_session(text=_ft(
        "https://www.proquest.com/x",
        "https://www.jstor.org/stable/1",
        "https://search.ebscohost.com/login.aspx?direct=true&db=bth&AN=1234",
    )))
    assert "ebscohost" in lookup_fulltext_target(DOI, cfg).url


def test_ezproxy_wrapped_ebscohost_still_ranks_as_ebscohost() -> None:
    """The real-world wrapping order: the proxy is outside, the platform
    inside. Unwrapping is what makes ranking see EBSCOhost at all."""
    cfg = _sfx_cfg(_session(text=_ft(
        "https://www.jstor.org/stable/1",
        "http://ezproxy.example.edu/login?url=https://search.ebscohost.com/x",
    )))
    assert "ebscohost" in lookup_fulltext_target(DOI, cfg).url


def test_config_priority_overrides_the_default_order() -> None:
    """`[library] platform_priority` has to reach the ranking; the Pass-3
    call site relies on `cfg.priority` rather than passing a default."""
    from fetchers.resolvers import platform_priority_from_keys
    cfg = _sfx_cfg(
        _session(text=_ft(
            "https://search.ebscohost.com/x", "https://www.jstor.org/stable/1",
        )),
        priority=platform_priority_from_keys(("jstor",)),
    )
    assert "jstor" in lookup_fulltext_target(DOI, cfg).url


# ---------------------------------------------------------------------------
# Alma ISSN fallback
# ---------------------------------------------------------------------------


def test_alma_recovers_via_issn_when_the_doi_query_is_empty() -> None:
    """Issue #6: the deployment links holdings at journal level only, so
    the DOI-keyed query comes back empty for a journal it does license."""
    session = _routing_session([
        ("rft.issn", alma_xml((
            "getFullTxt", "https://alma.example/ft", "ABI/INFORM", "ProQuest",
        ))),
        ("rft_id", alma_xml()),
    ])
    got = lookup_fulltext_target(DOI, _alma_cfg(session), issn="1042-2587")
    assert got.url == "https://alma.example/ft"


def test_alma_skips_the_fallback_when_the_doi_query_answered() -> None:
    """No reason to spend a second request; also proves the loop stops."""
    session = _routing_session([
        ("rft_id", alma_xml((
            "getFullTxt", "https://alma.example/direct", "JSTOR", "JSTOR",
        ))),
    ])
    got = lookup_fulltext_target(DOI, _alma_cfg(session), issn="1042-2587")
    assert got.url == "https://alma.example/direct"
    assert session.get.call_count == 1


def test_sfx_never_uses_an_issn_fallback() -> None:
    session = _session(text=_ft())
    assert lookup_fulltext_target(
        DOI, _sfx_cfg(session), issn="1042-2587",
    ) == (None, True)
    assert session.get.call_count == 1


# ---------------------------------------------------------------------------
# Coverage queries
# ---------------------------------------------------------------------------


def test_sfx_dual_runs_two_queries_and_reports_date_filtering(tmp_path) -> None:
    session = _routing_session([
        ("ignore_date_threshold", _ft(
            "https://onlinelibrary.wiley.com/a", "https://www.jstor.org/b",
        )),
        ("rft_id", _ft("https://onlinelibrary.wiley.com/a")),
    ])
    result = lookup_dual(DOI, _sfx_cfg(session, ResolverCache(tmp_path)))
    assert len(result.in_range) == 1
    assert len(result.any_range) == 2
    assert result.query_ok is True
    assert result.date_filtering_available is True
    assert session.get.call_count == 2


def test_alma_dual_runs_one_query_and_says_dates_are_unavailable() -> None:
    """Halves resolver traffic and, more importantly, stops Pass 1 from
    diffing two identical answers into a coverage verdict."""
    session = _session(text=alma_xml((
        "getFullTxt", "https://alma.example/ft", "EBSCOhost BSU", "EBSCOhost",
    )))
    result = lookup_dual(DOI, _alma_cfg(session))
    assert result.date_filtering_available is False
    assert result.in_range == result.any_range
    assert len(result.in_range) == 1
    assert session.get.call_count == 1


def test_dual_reports_not_ok_when_the_resolver_fails() -> None:
    result = lookup_dual(DOI, _sfx_cfg(_session(error=True)))
    assert result.query_ok is False
    assert result.in_range == [] and result.any_range == []


def test_dual_is_empty_and_not_ok_when_unconfigured() -> None:
    cfg = LibraryResolverConfig(resolver=None, session=MagicMock())
    assert lookup_dual(DOI, cfg).query_ok is False


def test_dual_caches_the_two_sfx_queries_separately(tmp_path) -> None:
    session = _routing_session([
        ("ignore_date_threshold", _ft("https://a.example/1", "https://b.example/2")),
        ("rft_id", _ft("https://a.example/1")),
    ])
    lookup_dual(DOI, _sfx_cfg(session, ResolverCache(tmp_path)))
    # Fresh cache object over the same directory: served from disk.
    session2 = _session(error=True)
    again = lookup_dual(DOI, _sfx_cfg(session2, ResolverCache(tmp_path)))
    assert len(again.in_range) == 1
    assert len(again.any_range) == 2
    session2.get.assert_not_called()


# ---------------------------------------------------------------------------
# targets_match_domains
# ---------------------------------------------------------------------------


def test_targets_match_domains_uses_the_dialect_matching() -> None:
    cfg = _alma_cfg(_session())
    targets = [FulltextTarget(
        url="https://aalto.alma.exlibrisgroup.com/view/action/x",
        package_name="EBSCOhost Business Source Ultimate",
        interface_name="EBSCOhost",
    )]
    assert targets_match_domains(targets, ("ebscohost.com",), cfg) is True
    assert targets_match_domains(targets, ("wiley.com",), cfg) is False


def test_targets_match_domains_is_false_without_a_resolver() -> None:
    cfg = LibraryResolverConfig(resolver=None, session=MagicMock())
    assert targets_match_domains([], ("wiley.com",), cfg) is False


# ---------------------------------------------------------------------------
# ResolverCache
# ---------------------------------------------------------------------------


def test_cache_round_trips_targets_with_names(tmp_path: Path) -> None:
    """Names must survive the cache, or ranking degrades to host-only on
    the second run — the original bug, deferred by one run."""
    cache = ResolverCache(tmp_path)
    cache.put(DOI, [FulltextTarget(
        url="https://alma.example/x", package_name="EBSCOhost BSU",
        interface_name="EBSCOhost", coverage="from 1990", is_free=True,
    )])
    reloaded = ResolverCache(tmp_path).get(DOI)
    assert reloaded is not None and len(reloaded) == 1
    assert reloaded[0].interface_name == "EBSCOhost"
    assert reloaded[0].coverage == "from 1990"
    assert reloaded[0].is_free is True


def test_cache_recovers_from_corrupt_json(tmp_path: Path) -> None:
    """A corrupt cache must not crash a run; start fresh."""
    (tmp_path / "resolver_cache.json").write_text("{not json")
    assert ResolverCache(tmp_path).get(DOI) is None


def test_cache_is_a_hit_and_skips_the_network(tmp_path: Path) -> None:
    cache = ResolverCache(tmp_path)
    cache.put(DOI, [FulltextTarget(url="https://search.ebscohost.com/x")])
    session = _session(error=True)
    assert lookup_fulltext_target(DOI, _sfx_cfg(session, cache)).url is not None
    session.get.assert_not_called()


def test_an_empty_answer_is_never_cached(tmp_path: Path) -> None:
    """The JBE incident. An empty result is a claim about what the resolver
    could see *for this DOI*, and it is wrong often enough that persisting
    it turns a soft miss into a permanent one."""
    cache = ResolverCache(tmp_path)
    cfg = _sfx_cfg(_session(text=_ft()), cache)
    assert lookup_fulltext_target(DOI, cfg) == (None, True)
    assert ResolverCache(tmp_path).get(DOI) is None
    # A later run with real coverage must still be able to find it.
    cfg2 = _sfx_cfg(_session(text=_ft("https://search.ebscohost.com/x")), cache)
    assert lookup_fulltext_target(DOI, cfg2).url is not None


def test_a_legacy_sfx_cache_file_is_ignored_not_misread(tmp_path: Path) -> None:
    """Old runs wrote `sfx_cache.json` with a bare URL list, which cannot
    answer the platform question. It is left alone and re-queried rather
    than migrated into targets that would rank as unranked forever."""
    (tmp_path / "sfx_cache.json").write_text(
        '{"' + DOI + '": {"urls": ["https://x"]}}',
    )
    assert ResolverCache(tmp_path).get(DOI) is None
    cfg = _sfx_cfg(
        _session(text=_ft("https://search.ebscohost.com/x")),
        ResolverCache(tmp_path),
    )
    assert "ebscohost" in lookup_fulltext_target(DOI, cfg).url


def test_cache_entry_without_targets_key_is_a_miss(tmp_path: Path) -> None:
    (tmp_path / "resolver_cache.json").write_text(
        '{"' + DOI + '": {"other": 1}}',
    )
    assert ResolverCache(tmp_path).get(DOI) is None


# ---------------------------------------------------------------------------
# load_from_config
# ---------------------------------------------------------------------------


def _stub_config(monkeypatch, data: dict) -> None:
    import core.config_loader as cl
    monkeypatch.setattr(cl, "load_config", lambda: data)
    monkeypatch.delenv("LIBRARY_OPENURL_BASE", raising=False)
    monkeypatch.delenv("LIBRARY_RESOLVER", raising=False)


def test_load_from_config_returns_none_when_unset(monkeypatch) -> None:
    _stub_config(monkeypatch, {})
    assert load_from_config(MagicMock()) is None


def test_load_from_config_detects_the_dialect(monkeypatch) -> None:
    _stub_config(monkeypatch, {"library": {"openurl_base": ALMA_BASE}})
    cfg = load_from_config(MagicMock())
    assert cfg is not None and cfg.resolver.flavour == "alma"
    assert cfg.openurl_base == ALMA_BASE


def test_load_from_config_honours_an_explicit_resolver(monkeypatch) -> None:
    _stub_config(
        monkeypatch,
        {"library": {"openurl_base": ALMA_BASE, "resolver": "sfx"}},
    )
    assert load_from_config(MagicMock()).resolver.flavour == "sfx"


def test_load_from_config_reads_platform_priority(monkeypatch) -> None:
    _stub_config(monkeypatch, {
        "library": {
            "openurl_base": SFX_BASE, "platform_priority": "jstor,ebscohost",
        },
    })
    cfg = load_from_config(MagicMock())
    assert [p.key for p in cfg.priority][:2] == ["jstor", "ebscohost"]


def test_load_from_config_env_var_overrides_toml(monkeypatch) -> None:
    """`LIBRARY_OPENURL_BASE` is what the setup wizard's KeySpec writes."""
    _stub_config(monkeypatch, {"library": {"openurl_base": SFX_BASE}})
    monkeypatch.setenv("LIBRARY_OPENURL_BASE", ALMA_BASE)
    cfg = load_from_config(MagicMock())
    assert cfg.openurl_base == ALMA_BASE
    assert cfg.resolver.flavour == "alma"


# ---------------------------------------------------------------------------
# Captured real responses (skip when absent — institution-specific)
# ---------------------------------------------------------------------------


def _load_fixture(name: str) -> str:
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(
            f"SFX fixture {name!r} not present at {path}. Capture one against "
            f"your own endpoint; the inline-XML tests in "
            f"test_resolvers_dialects.py cover the parse contract meanwhile."
        )
    return path.read_text(encoding="utf-8")


def test_real_sfx_response_with_fulltext_parses() -> None:
    targets = SfxResolver(SFX_BASE).parse(_load_fixture("has_fulltext.xml"))
    assert targets and len(targets) >= 1


def test_real_sfx_response_without_fulltext_parses_as_empty() -> None:
    assert SfxResolver(SFX_BASE).parse(_load_fixture("no_fulltext.xml")) == []
