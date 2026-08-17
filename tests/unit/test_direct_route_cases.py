"""Case 1a / 1b / 2 / 3 — whether to open the publisher's own site.

Pass 1 matches a browser handler by DOI prefix or resolved host, then
asks the link resolver whether that publisher is worth opening at all.
Three of the four answers predate this file; the fourth is `1b`, and it
was the expensive one to be missing.

The corpus that produced it: 655 items, of which 60 were queued for a
direct attempt at APA, Academy of Management, Emerald and AAA. Aalto
licenses none of those platforms — Academy of Management in particular
sells member access rather than institutional — and Alma said so plainly,
returning EBSCOhost / JSTOR / ProQuest routes and no publisher interface
for every one of them. The old code read "no target matched this
publisher's domains" as *silence* and failed open into a Cloudflare/SSO
prompt per publisher, when it was in fact a clear answer.

The complementary risk is over-correcting: a user with a society
membership or a second institution's login genuinely can open a
publisher the configured resolver knows nothing about. That is
`[library] direct_access`.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import enrich_pdfs
import pytest
from fetchers.library_resolver import DualResult, LibraryResolverConfig
from fetchers.resolvers import AlmaResolver, FulltextTarget

ALMA = AlmaResolver(
    "https://eu03.alma.exlibrisgroup.com/view/uresolver/358AALTO_INST/openurl"
)
ALMA_URL = "https://aalto.alma.exlibrisgroup.com/view/action/uresolver.do?x=1"

# Real strings from the live Aalto response for an AoM DOI.
EBSCO = FulltextTarget(
    url=ALMA_URL,
    package_name="EBSCOhost Business Source Ultimate",
    interface_name="EBSCOhost",
    coverage="Available from 01.03.1972.<br>Most recent 1 year(s) not available.<br>",
)
PROQUEST = FulltextTarget(
    url=ALMA_URL,
    package_name="ABI/INFORM Collection",
    interface_name="ProQuest",
    coverage="Available from 01.01.1972 volume: 11 issue: 4 until 31.10.1985 volume: 24 issue: 3.<br>",
)
WILEY_1996 = FulltextTarget(
    url=ALMA_URL,
    package_name="Wiley Online Library Database Model 2024",
    interface_name="Wiley Online Library",
    coverage="Available from 1996 volume: 35 issue: 1.<br>",
)

AOM_DOMAINS = ("journals.aom.org", "aom.org")
WILEY_DOMAINS = ("onlinelibrary.wiley.com", "wiley.com")


@pytest.fixture
def cfg():
    return LibraryResolverConfig(resolver=ALMA, session=MagicMock())


def _dual(targets):
    """Alma shape: one query reused for both fields, no date filtering."""
    return DualResult(
        in_range=list(targets), any_range=list(targets),
        query_ok=True, date_filtering_available=False,
    )


def _case(cfg, targets, domains, **kw):
    return enrich_pdfs.classify_direct_route(
        _dual(targets), domains, cfg, **kw
    )


# ---------------------------------------------------------------------------
# 1b — the case this file exists for
# ---------------------------------------------------------------------------


def test_resolver_named_routes_but_none_here_is_not_silence(cfg) -> None:
    """The AoM shape: EBSCOhost and ProQuest offered, no AoM interface."""
    assert _case(
        cfg, [EBSCO, PROQUEST], AOM_DOMAINS,
        pub_date="2011", handler_name="aom",
    ) == "1b-no-entitlement"


def test_1b_does_not_open_the_publisher(cfg) -> None:
    assert enrich_pdfs.DIRECT_ROUTE_CASES["1b-no-entitlement"] is False


def test_an_empty_resolver_answer_still_fails_open(cfg) -> None:
    """1a. No targets at all means the resolver knows nothing about this
    journal — unset, unreachable, or simply absent. A failed attempt is a
    real answer; a skipped one is not."""
    assert _case(
        cfg, [], AOM_DOMAINS, pub_date="2011", handler_name="aom",
    ) == "1a-unknown"
    assert enrich_pdfs.DIRECT_ROUTE_CASES["1a-unknown"] is True


def test_a_handler_with_no_domains_cannot_be_judged(cfg) -> None:
    """Nothing to compare against, so nothing is claimed."""
    assert _case(
        cfg, [EBSCO], (), pub_date="2011", handler_name="connector",
    ) == "1a-unknown"


# ---------------------------------------------------------------------------
# direct_access — the escape hatch, and its limits
# ---------------------------------------------------------------------------


def test_direct_access_reopens_a_publisher_the_resolver_cannot_see(cfg) -> None:
    """This user reaches APA PsycNET through a second institution while
    the configured resolver is the first institution's. Aalto's Alma
    honestly lists no APA route, and 1b would be wrong for exactly this
    publisher."""
    assert _case(
        cfg, [EBSCO], ("psycnet.apa.org", "apa.org"),
        pub_date="2011", handler_name="apa", direct_access={"apa"},
    ) == "1a-unknown"


def test_direct_access_is_per_publisher(cfg) -> None:
    """Declaring APA must not reopen Academy of Management."""
    assert _case(
        cfg, [EBSCO], AOM_DOMAINS,
        pub_date="2011", handler_name="aom", direct_access={"apa"},
    ) == "1b-no-entitlement"


def test_direct_access_does_not_override_a_coverage_gap(cfg) -> None:
    """Case 2 is a claim about the platform's *holdings*, not about
    entitlement. Wiley's run starts in 1996; no credential makes it hold
    a 1976 article, so declaring `wiley` must not resurrect a guaranteed
    30-second timeout."""
    assert _case(
        cfg, [WILEY_1996, EBSCO], WILEY_DOMAINS,
        pub_date="1976-12", handler_name="wiley", direct_access={"wiley"},
    ) == "2-out-of-coverage"


# ---------------------------------------------------------------------------
# The pre-existing cases must not have moved
# ---------------------------------------------------------------------------


def test_in_coverage_still_runs_direct(cfg) -> None:
    assert _case(
        cfg, [WILEY_1996], WILEY_DOMAINS,
        pub_date="2018", handler_name="wiley",
    ) == "3-in-coverage"
    assert enrich_pdfs.DIRECT_ROUTE_CASES["3-in-coverage"] is True


def test_right_platform_wrong_year_still_diverts(cfg) -> None:
    """The 1976 Wiley article that opened this whole investigation."""
    assert _case(
        cfg, [WILEY_1996, EBSCO], WILEY_DOMAINS,
        pub_date="1976-12", handler_name="wiley",
    ) == "2-out-of-coverage"
    assert enrich_pdfs.DIRECT_ROUTE_CASES["2-out-of-coverage"] is False


def test_every_case_says_whether_to_open_the_publisher() -> None:
    """A new case with no entry would raise KeyError mid-run."""
    for case in ("1a-unknown", "1b-no-entitlement",
                 "2-out-of-coverage", "3-in-coverage"):
        assert case in enrich_pdfs.DIRECT_ROUTE_CASES
