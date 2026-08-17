"""SFX and Alma as peer dialects: detection, query building, parsing.

Every SFX case here uses **inline XML**. The suite used to verify SFX
only through fixtures under `tests/fixtures/sfx/`, which is gitignored
because the responses are institution-specific — so on any fresh
checkout the SFX half simply skipped while the Alma half ran. That is
its own form of unequal standing: the dialect with weaker automated
coverage is the one that silently rots. Fixture-driven tests still exist
(see `test_library_resolver.py`) for real-response shapes; these prove
the parse contract without needing anyone's endpoint.
"""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse
from xml.sax.saxutils import escape

from fetchers.resolvers import (
    AlmaResolver,
    ResolverRequest,
    SfxResolver,
    resolver_for,
)

SFX_BASE = "https://sfx.example.org/inst01"
ALMA_BASE = (
    "https://eu03.alma.exlibrisgroup.com/view/uresolver/358AALTO_INST/openurl"
)
DOI = "10.1007/s10551-018-4026-8"


def _params(url: str) -> dict[str, list[str]]:
    return parse_qs(urlparse(url).query)


# ---------------------------------------------------------------------------
# XML builders
# ---------------------------------------------------------------------------


def sfx_xml(*targets: tuple[str, str, str]) -> str:
    """`(service_type, target_url, public_name)` triples in SFX's shape."""
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
            f'<key id="Availability">Available from 1990</key>'
            f"</keys>"
            f"<resolution_url>{escape(url)}</resolution_url>"
            f"</context_service>"
        )
    return f"<uresolver_content>{body}</uresolver_content>"


# ---------------------------------------------------------------------------
# Detection / registry
# ---------------------------------------------------------------------------


def test_alma_detected_by_uresolver_path() -> None:
    assert AlmaResolver.matches(ALMA_BASE) is True
    assert resolver_for(ALMA_BASE).flavour == "alma"


def test_sfx_detected_for_a_plain_openurl_base() -> None:
    assert AlmaResolver.matches(SFX_BASE) is False
    assert resolver_for(SFX_BASE).flavour == "sfx"


def test_sfx_is_the_fallback_and_accepts_anything_non_empty() -> None:
    """SFX has no distinguishing path marker, so it claims everything.
    Registry order is what keeps that from swallowing Alma."""
    assert SfxResolver.matches(SFX_BASE) is True
    assert SfxResolver.matches(ALMA_BASE) is True
    assert SfxResolver.matches("") is False


def test_explicit_override_beats_autodetection() -> None:
    """`[library] resolver = sfx` must win even against an Alma-shaped
    URL — the override exists for endpoints we guess wrong."""
    assert resolver_for(ALMA_BASE, "sfx").flavour == "sfx"
    assert resolver_for(SFX_BASE, "alma").flavour == "alma"


def test_unknown_override_falls_back_to_autodetection() -> None:
    """A typo in config degrades to previous behaviour, never raises."""
    assert resolver_for(ALMA_BASE, "nonsense").flavour == "alma"
    assert resolver_for(ALMA_BASE, "auto").flavour == "alma"


def test_no_resolver_for_empty_base() -> None:
    assert resolver_for("") is None
    assert resolver_for("   ") is None


# ---------------------------------------------------------------------------
# Query building
# ---------------------------------------------------------------------------


def test_sfx_query_carries_openurl_context_and_multi_obj_xml() -> None:
    url = SfxResolver(SFX_BASE).query_urls(ResolverRequest(doi=DOI))[0]
    p = _params(url)
    assert p["url_ver"] == ["Z39.88-2004"]
    assert p["ctx_ver"] == ["Z39.88-2004"]
    assert p["sfx.response_type"] == ["multi_obj_xml"]
    assert p["rft_id"] == [f"info:doi/{DOI}"]
    assert "svc_dat" not in p


def test_sfx_query_carries_custom_sid() -> None:
    url = SfxResolver(SFX_BASE).query_urls(
        ResolverRequest(doi=DOI, sid="my-tool"),
    )[0]
    assert _params(url)["sfx.sid"] == ["my-tool"]


def test_sfx_ignore_date_threshold_is_opt_in() -> None:
    r = SfxResolver(SFX_BASE)
    plain = _params(r.query_urls(ResolverRequest(doi=DOI))[0])
    assert "sfx.ignore_date_threshold" not in plain
    widened = _params(
        r.query_urls(ResolverRequest(doi=DOI, ignore_date_threshold=True))[0],
    )
    assert widened["sfx.ignore_date_threshold"] == ["1"]


def test_sfx_ignores_journal_identity_and_asks_once() -> None:
    """ISSN/date/volume are Alma's fallback keys; SFX has no use for them
    and must not gain a second request because they were supplied."""
    urls = SfxResolver(SFX_BASE).query_urls(
        ResolverRequest(doi=DOI, issn="0167-4544", pub_date="2018", volume="153"),
    )
    assert len(urls) == 1
    assert "rft.issn" not in _params(urls[0])


def test_alma_query_requires_svc_dat_and_sends_no_sfx_params() -> None:
    """Without `svc_dat=CTO` Alma serves its HTML discovery skin at HTTP
    200. The `sfx.*` namespace is SFX's and is verified live to make no
    difference here, so it is not sent."""
    url = AlmaResolver(ALMA_BASE).query_urls(ResolverRequest(doi=DOI))[0]
    p = _params(url)
    assert p["svc_dat"] == ["CTO"]
    assert p["rft_id"] == [f"info:doi/{DOI}"]
    assert not [k for k in p if k.startswith("sfx.")]


def test_alma_adds_an_issn_keyed_fallback_query() -> None:
    """Some Alma deployments link holdings only at journal level and
    answer a DOI-keyed query with nothing even for licensed journals, so
    the second query drops `rft_id` entirely."""
    urls = AlmaResolver(ALMA_BASE).query_urls(
        ResolverRequest(doi=DOI, issn="0167-4544", pub_date="2018", volume="153"),
    )
    assert len(urls) == 2
    first, second = _params(urls[0]), _params(urls[1])
    assert "rft_id" in first and "rft.issn" not in first
    assert "rft_id" not in second
    assert second["rft.issn"] == ["0167-4544"]
    assert second["rft.date"] == ["2018"]
    assert second["rft.volume"] == ["153"]
    assert second["svc_dat"] == ["CTO"]


def test_alma_omits_the_fallback_without_an_issn() -> None:
    urls = AlmaResolver(ALMA_BASE).query_urls(ResolverRequest(doi=DOI))
    assert len(urls) == 1


def test_alma_declares_no_date_filtering() -> None:
    """Live testing found Alma returns identical results for correct,
    wrong and absent date/volume. The flag is what stops callers from
    inventing a coverage verdict from two identical answers."""
    assert AlmaResolver.supports_date_threshold is False
    assert SfxResolver.supports_date_threshold is True


def test_alma_ignores_ignore_date_threshold_in_its_query() -> None:
    urls = AlmaResolver(ALMA_BASE).query_urls(
        ResolverRequest(doi=DOI, ignore_date_threshold=True),
    )
    assert len(urls) == 1
    assert "sfx.ignore_date_threshold" not in _params(urls[0])


# ---------------------------------------------------------------------------
# Parsing — SFX
# ---------------------------------------------------------------------------


def test_sfx_parse_keeps_only_fulltext_targets() -> None:
    xml = sfx_xml(
        ("getHolding", "https://melinda.example/record", ""),
        ("getFullTxt", "https://onlinelibrary.wiley.com/doi/x", "Wiley Online"),
        ("getAuthor", "https://author.example", ""),
        ("getWebSearch", "https://search.example", ""),
    )
    targets = SfxResolver(SFX_BASE).parse(xml)
    assert [t.url for t in targets] == ["https://onlinelibrary.wiley.com/doi/x"]
    assert targets[0].package_name == "Wiley Online"


def test_sfx_parse_returns_empty_list_when_no_fulltext() -> None:
    """Empty is a real answer — "the library has no route" — and must
    stay distinct from a parse failure."""
    xml = sfx_xml(("getHolding", "https://melinda.example/record", ""))
    assert SfxResolver(SFX_BASE).parse(xml) == []


def test_sfx_parse_returns_none_on_malformed_xml() -> None:
    assert SfxResolver(SFX_BASE).parse("<not-xml") is None


def test_sfx_parse_survives_a_target_without_names() -> None:
    """Name extraction is best-effort: SFX ranks by hostname, so a
    response with no `target_public_name` must still parse and rank."""
    xml = sfx_xml(("getFullTxt", "https://www.jstor.org/stable/1", ""))
    targets = SfxResolver(SFX_BASE).parse(xml)
    assert len(targets) == 1
    assert targets[0].package_name == ""


def test_sfx_parse_ignores_a_fulltext_target_with_no_url() -> None:
    xml = (
        "<ctx_obj_set><ctx_obj><targets><target>"
        "<service_type>getFullTxt</service_type>"
        "</target></targets></ctx_obj></ctx_obj_set>"
    )
    assert SfxResolver(SFX_BASE).parse(xml) == []


# ---------------------------------------------------------------------------
# Parsing — Alma
# ---------------------------------------------------------------------------


def test_alma_parse_reads_resolution_url_and_platform_names() -> None:
    """The names are the whole point: Alma's URL is always the redirector,
    so `interface_name` is the only way to know this is EBSCOhost."""
    xml = alma_xml((
        "getFullTxt",
        "https://aalto.alma.exlibrisgroup.com/view/action/uresolver.do?x=1",
        "EBSCOhost Business Source Ultimate", "EBSCOhost",
    ))
    targets = AlmaResolver(ALMA_BASE).parse(xml)
    assert len(targets) == 1
    assert targets[0].interface_name == "EBSCOhost"
    assert targets[0].package_name == "EBSCOhost Business Source Ultimate"
    assert targets[0].coverage == "Available from 1990"
    assert "exlibrisgroup.com" in targets[0].url


def test_alma_parse_ignores_non_fulltext_context_services() -> None:
    xml = alma_xml(
        ("getHolding", "https://alma.example/hold", "Print holdings", "Alma"),
        ("getFullTxt", "https://alma.example/ft", "JSTOR Archival", "JSTOR"),
    )
    targets = AlmaResolver(ALMA_BASE).parse(xml)
    assert [t.interface_name for t in targets] == ["JSTOR"]


def test_alma_parse_returns_none_on_malformed_xml() -> None:
    assert AlmaResolver(ALMA_BASE).parse("<uresolver_content") is None


def test_alma_parse_returns_empty_list_for_no_services() -> None:
    assert AlmaResolver(ALMA_BASE).parse(alma_xml()) == []


def test_alma_parse_skips_a_service_without_resolution_url() -> None:
    xml = (
        '<uresolver_content><context_service service_type="getFullTxt">'
        '<keys><key id="interface_name">EBSCOhost</key></keys>'
        "</context_service></uresolver_content>"
    )
    assert AlmaResolver(ALMA_BASE).parse(xml) == []


def test_both_dialects_parse_through_xml_namespaces() -> None:
    """Deployments serve these with and without namespace declarations,
    so both parses walk on local names."""
    ns_sfx = (
        '<ctx_obj_set xmlns="http://example.org/sfx"><ctx_obj><targets>'
        "<target><service_type>getFullTxt</service_type>"
        "<target_url>https://ebscohost.com/x</target_url></target>"
        "</targets></ctx_obj></ctx_obj_set>"
    )
    assert len(SfxResolver(SFX_BASE).parse(ns_sfx)) == 1

    ns_alma = (
        '<uresolver_content xmlns="http://com/exlibris/urm/uresolver">'
        '<context_service service_type="getFullTxt">'
        "<resolution_url>https://alma.example/x</resolution_url>"
        "</context_service></uresolver_content>"
    )
    assert len(AlmaResolver(ALMA_BASE).parse(ns_alma)) == 1
