"""A resolver route that opens only the journal must not be opened.

Live 2026-09-23 (JYU SFX, Connector pass): 10.5897/ajbmx11.031 was sent to
https://academicjournals.org/ajbm/ — the journal's home page, from SFX's
"EZB-FREE" free-journal list. No translator fires on a page with no
article, so the item failed although its DOI landing page would work.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fetchers.library_resolver import (
    LibraryResolverConfig,
    lookup_fulltext_target,
)
from fetchers.resolvers import (
    FulltextTarget,
    SfxResolver,
    doi_landing_via,
    is_journal_level,
    looks_journal_level,
)

DOI = "10.5897/ajbmx11.031"
BASE = "https://sfx.example.org/inst01"


def _target(url: str, parser: str, name: str = "T") -> str:
    parser_el = f"<parser>{parser}</parser>" if parser else ""
    return (
        "<target>"
        f"<target_name>{name}</target_name>"
        "<service_type>getFullTxt</service_type>"
        f"{parser_el}"
        f"<target_url>{url}</target_url>"
        "</target>"
    )


def _xml(*targets: str) -> str:
    return (
        "<ctx_obj_set><ctx_obj><ctx_obj_targets>"
        + "".join(targets)
        + "</ctx_obj_targets></ctx_obj></ctx_obj_set>"
    )


def _cfg(xml: str) -> LibraryResolverConfig:
    session = MagicMock()
    resp = MagicMock()
    resp.status_code = 200
    resp.text = xml
    session.get.return_value = resp
    return LibraryResolverConfig(
        resolver=SfxResolver(BASE), session=session, cache=None,
    )


# --- SFX says so -----------------------------------------------------------


def test_sfx_bulk_parser_marks_a_journal_level_target() -> None:
    targets = SfxResolver(BASE).parse(_xml(
        _target("https://academicjournals.org/ajbm/", "Bulk::DI"),
        _target("https://doi.org/10.5897/ajbmx11.031?nosfx=y", "DOAJ::DOAJ"),
        _target("https://x.example/a", ""),
    ))
    assert [t.journal_level for t in targets] == [True, False, None]


def test_cache_round_trip_keeps_the_label_and_old_entries_read_unknown() -> None:
    t = FulltextTarget(url="https://a.example/", journal_level=True)
    assert FulltextTarget.from_cache_dict(t.as_cache_dict()) == t
    assert FulltextTarget.from_cache_dict(
        {"url": "https://a.example/"},
    ).journal_level is None


def test_the_resolvers_label_beats_the_url_guess() -> None:
    root = "https://academicjournals.org/ajbm/"
    assert is_journal_level(FulltextTarget(url=root, journal_level=False), DOI) is False
    assert is_journal_level(FulltextTarget(url=root), DOI) is True


# --- the URL guess, for unlabelled (older cached) routes -------------------


@pytest.mark.parametrize("url", [
    "https://academicjournals.org/ajbm/",
    "https://jibm.org/",
    "https://www.sciencedirect.com/science/journal/03135926",
    "https://www.tandfonline.com/loi/ccos20",
    "https://www.frontiersin.org/journals/psychology",
    "https://ojs.mruni.eu/ojs/public-policy-and-administration",
    "https://scindeks.ceon.rs/journaldetails.aspx?issn=0048-5705",
    "http://ezproxy.jyu.fi/login?url=https://onlinelibrary.wiley.com/loi/15719979",
])
def test_journal_pages_are_recognised(url: str) -> None:
    assert looks_journal_level(url, DOI)


@pytest.mark.parametrize("url", [
    # carries the DOI
    "http://ezproxy.jyu.fi/login?url=https://doi.org/10.5897/ajbmx11.031",
    "https://academicjournals.org/journal/AJBM/article-abstract/ajbmx11.031",
    # built from the citation
    "http://ezproxy.jyu.fi/login?url=https://gateway.proquest.com/openurl?issn=1&genre=article",
    "https://www.sciencedirect.com/science?_ob=GatewayURL&_volkey=1",
    "https://search.ebscohost.com/login.aspx?direct=true&AN=12345678",
    "https://www.jstor.org/stable/796853",
    # repository copies (Unpaywall): no DOI, but an item id or a file
    "https://hal.science/hal-02290402",
    "https://zenodo.org/record/5425194",
    "https://research.vu.nl/en/publications/8af0e7a2-7b14-4c20-a3f1-776cabff7760",
    "https://peer.asee.org/30593.pdf",
    # Alma's redirector hides the destination
    "https://aalto.alma.exlibrisgroup.com/view/action/uresolver.do?operation=resolveService",
])
def test_article_level_urls_are_left_alone(url: str) -> None:
    assert not looks_journal_level(url, DOI)


# --- the lookup ------------------------------------------------------------


def test_an_article_route_beats_a_journal_page_listed_first() -> None:
    lookup = lookup_fulltext_target(DOI, _cfg(_xml(
        _target("https://academicjournals.org/ajbm/", "Bulk::DI"),
        _target("https://doi.org/10.5897/ajbmx11.031?nosfx=y", "DOAJ::DOAJ"),
    )))
    assert lookup.url == "https://doi.org/10.5897/ajbmx11.031?nosfx=y"
    assert not lookup.journal_page_replaced


def test_a_lone_journal_page_is_replaced_by_the_doi_landing_page() -> None:
    lookup = lookup_fulltext_target(DOI, _cfg(_xml(
        _target("https://academicjournals.org/ajbm/", "Bulk::DI"),
    )))
    assert lookup.url == f"https://doi.org/{DOI}"
    assert lookup.journal_page_replaced
    assert lookup.target.url == "https://academicjournals.org/ajbm/"


def test_the_replacement_keeps_the_routes_proxy() -> None:
    proxied = "http://ezproxy.jyu.fi/login?url=https://pediatrics.example.org/"
    assert doi_landing_via(proxied, DOI) == (
        f"http://ezproxy.jyu.fi/login?url=https://doi.org/{DOI}"
    )
    assert doi_landing_via("https://a.example/", DOI) == f"https://doi.org/{DOI}"
