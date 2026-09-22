"""Following one library's routes while consulting all of them.

A reader with two affiliations — here Aalto (Alma) and JYU (SFX) — can
be on only one institution's network at a time. The pre-flight needs
every library, so that "no coverage" means none of them has it; a browser
pass needs only the reachable one, because another institution's link
opens its login page instead of a PDF. On 2026-09-05 a pass on the JYU
VPN did exactly that with Aalto's links.

So `--library` filters routes; it does not narrow the config. An item
whose only route is at an inactive library is *deferred*: neither
attempted nor logged, so the run on the other network picks it up.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

import enrich_pdfs
from fetchers import library_resolver as LR
from fetchers.library_resolver import DualResult, LibraryResolverConfig, TargetLookup
from fetchers.resolvers import AlmaResolver, FulltextTarget, SfxResolver

ALMA = AlmaResolver(
    "https://eu03.alma.exlibrisgroup.com/view/uresolver/358AALTO_INST/openurl"
)
SFX = SfxResolver("https://sfx.finna.fi/nelli09")

AALTO_WILEY = FulltextTarget(
    url="https://aalto.alma.exlibrisgroup.com/view/action/uresolver.do?x=1",
    interface_name="Wiley Online Library", resolver_id=ALMA.resolver_id,
)
JYU_EBSCO = FulltextTarget(
    url="https://search.ebscohost.com/login.aspx?direct=true&db=bsu&an=1",
    interface_name="EBSCOhost", resolver_id=SFX.resolver_id,
)
WILEY_DOMAINS = ("onlinelibrary.wiley.com", "wiley.com")


def _cfg(active=None) -> LibraryResolverConfig:
    return LibraryResolverConfig(
        resolver=ALMA, additional_resolvers=(SFX,), session=MagicMock(),
        active_ids=None if active is None else frozenset(active),
    )


def _lookup(monkeypatch, targets, cfg):
    monkeypatch.setattr(LR, "_query_targets", lambda *a, **k: list(targets))
    return LR.lookup_fulltext_target("10.1/x", cfg)


# ---------------------------------------------------------------------------
# lookup_fulltext_target
# ---------------------------------------------------------------------------


def test_only_the_active_librarys_routes_are_ranked(monkeypatch) -> None:
    got = _lookup(monkeypatch, [AALTO_WILEY, JYU_EBSCO], _cfg({SFX.resolver_id}))
    assert got.url == JYU_EBSCO.url and not got.deferred


def test_routes_only_at_an_inactive_library_are_deferred(monkeypatch) -> None:
    got = _lookup(monkeypatch, [AALTO_WILEY], _cfg({SFX.resolver_id}))
    assert got == TargetLookup(None, True, None, deferred=True)


def test_no_selection_follows_every_library(monkeypatch) -> None:
    got = _lookup(monkeypatch, [AALTO_WILEY], _cfg())
    assert got.url == AALTO_WILEY.url and not got.deferred


def test_no_route_anywhere_is_not_deferred(monkeypatch) -> None:
    """Deferral means "another library has it"; "nobody has it" stays
    the ILL verdict."""
    got = _lookup(monkeypatch, [], _cfg({SFX.resolver_id}))
    assert got == TargetLookup(None, True, None, deferred=False)


# ---------------------------------------------------------------------------
# classify_direct_route
# ---------------------------------------------------------------------------


def _case(targets, cfg, domains=WILEY_DOMAINS):
    dual = DualResult(in_range=list(targets), any_range=list(targets))
    return enrich_pdfs.classify_direct_route(dual, domains, cfg)


def test_publisher_licensed_only_at_the_inactive_library_is_deferred() -> None:
    assert _case([AALTO_WILEY, JYU_EBSCO], _cfg({SFX.resolver_id})) == "4-other-library"
    assert enrich_pdfs.DIRECT_ROUTE_CASES["4-other-library"] is False


def test_publisher_licensed_at_the_active_library_is_opened() -> None:
    assert _case([AALTO_WILEY], _cfg({ALMA.resolver_id})) == "3-in-coverage"
    assert _case([AALTO_WILEY], _cfg()) == "3-in-coverage"


def test_evidence_from_an_inactive_library_still_counts_against_a_publisher() -> None:
    """Only JYU answered, with EBSCOhost; the active library (Aalto) said
    nothing. That is still evidence Wiley is not the route, so 1b, not a
    fail-open 1a."""
    assert _case([JYU_EBSCO], _cfg({ALMA.resolver_id})) == "1b-no-entitlement"


# ---------------------------------------------------------------------------
# Wiring: a deferred item is never logged
# ---------------------------------------------------------------------------


def test_pass3_checks_deferral_before_failing_open_or_logging() -> None:
    """Deferred must be tested first: its shape (no url, query ok) is
    otherwise the "no licensed route" branch, which writes a skip row and
    an ILL-candidate failure — exactly what the other network's run must
    not find."""
    src = inspect.getsource(enrich_pdfs._run_browser_in_process)
    pass3 = src[src.index("lookup = _pass3_target("):]
    assert pass3.index("if lookup.deferred:") < pass3.index("log_writer.writerow(")
    assert "deferred_pass3 += 1" in pass3[:pass3.index("log_writer.writerow(")]


def test_preflight_drops_deferred_items_from_every_queue() -> None:
    src = inspect.getsource(enrich_pdfs._run_browser_in_process)
    block = src[src.index("case = classify_direct_route("):]
    block = block[:block.index("items_by_pub[direct.name].append(entry)")]
    deferral = block[block.index('if case == "4-other-library":'):]
    assert deferral.index("continue") < deferral.index("connector_upfront.append")
