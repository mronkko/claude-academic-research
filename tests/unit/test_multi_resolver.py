"""Several libraries, one merged answer.

A reader with two affiliations has two sets of entitlements, and neither
institution's resolver knows the other's. The case that forced this: an
Alma tenant reported *no route at all* for nine `Nursing Standard`
articles, while the reader's second institution served the same journal
through Journals@Ovid and ProQuest Central. Configured singly, the
pipeline called those items "no licensed route" and sent them to ILL.

Design points pinned here:

- `resolver` stays the primary and additional libraries sit alongside
  it, so existing construction sites are untouched.
- Each library is cached under a key naming it, whatever its position in
  the list, so reordering or narrowing the list cannot hand one library
  another's answers.
- None is returned only when *no* library could be asked. One library
  being down must not read as "nobody has this".
- Routes that share a platform are kept, not merged: they are different
  entitlements behind different logins.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fetchers import library_resolver as LR
from fetchers.library_resolver import LibraryResolverConfig
from fetchers.resolvers import AlmaResolver, FulltextTarget, SfxResolver

ALMA = AlmaResolver(
    "https://eu03.alma.exlibrisgroup.com/view/uresolver/358AALTO_INST/openurl"
)
SFX = SfxResolver("https://sfx.example.fi/jyu")


def _cfg(**kw) -> LibraryResolverConfig:
    kw.setdefault("resolver", ALMA)
    kw.setdefault("session", MagicMock())
    return LibraryResolverConfig(**kw)


@pytest.fixture
def answers(monkeypatch):
    """Stub the per-URL fetch, keyed by which resolver is asking."""
    calls: list[str] = []
    table: dict[str, list[FulltextTarget] | None] = {}

    def fake(url, cfg, doi, resolver=None):
        resolver = resolver if resolver is not None else cfg.resolver
        calls.append(resolver.openurl_base)
        return table.get(resolver.openurl_base)

    monkeypatch.setattr(LR, "_fetch_and_parse", fake)
    return table, calls


OVID = FulltextTarget(
    url="https://sfx.example.fi/ovid",
    package_name="Journals@Ovid", interface_name="Ovid",
    coverage="Available from 2000 volume: 15 issue: 9 until 2014 volume: 29 issue: 10",
)
EBSCO = FulltextTarget(
    url="https://alma/redirect?x=1",
    package_name="EBSCOhost Business Source Ultimate", interface_name="EBSCOhost",
)


# ---------------------------------------------------------------------------
# The merge
# ---------------------------------------------------------------------------


def test_a_second_library_supplies_a_route_the_first_lacks(answers) -> None:
    """The Nursing Standard case, in miniature."""
    table, _ = answers
    table[ALMA.openurl_base] = []          # Alma: no route at all
    table[SFX.openurl_base] = [OVID]       # the other institution has one

    got = LR._query_targets("10.7748/ns.29.10.9.s7", _cfg(additional_resolvers=(SFX,)))
    assert [t.package_name for t in got] == ["Journals@Ovid"]


def test_routes_from_both_libraries_are_merged(answers) -> None:
    table, _ = answers
    table[ALMA.openurl_base] = [EBSCO]
    table[SFX.openurl_base] = [OVID]

    got = LR._query_targets("10.1/x", _cfg(additional_resolvers=(SFX,)))
    assert {t.interface_name for t in got} == {"EBSCOhost", "Ovid"}


def test_the_primary_is_queried_first(answers) -> None:
    """Order decides ranking ties, so the library the reader is normally
    authenticated to must come first."""
    table, calls = answers
    table[ALMA.openurl_base] = [EBSCO]
    table[SFX.openurl_base] = [OVID]

    LR._query_targets("10.1/x", _cfg(additional_resolvers=(SFX,)))
    assert calls[0] == ALMA.openurl_base


def test_every_route_is_stamped_with_its_library(answers) -> None:
    """Every route names its library, primary included: a filter on the
    active library needs the id on every target."""
    table, _ = answers
    table[ALMA.openurl_base] = [EBSCO]
    table[SFX.openurl_base] = [OVID]

    by_iface = {
        t.interface_name: t
        for t in LR._query_targets("10.1/x", _cfg(additional_resolvers=(SFX,)))
    }
    assert by_iface["EBSCOhost"].resolver_name == "Aalto"
    assert by_iface["Ovid"].resolver_name == "Jyu"
    assert by_iface["Ovid"].resolver_id == SFX.resolver_id


def test_identical_urls_are_deduped_but_shared_platforms_are_not(answers) -> None:
    """Two libraries naming the same free route collapse; two libraries
    each licensing ProQuest do not — different entitlements, different
    logins, and only one may work."""
    table, _ = answers
    same = FulltextTarget(url="https://doaj.org/article/1", interface_name="DOAJ")
    pq_a = FulltextTarget(url="https://alma/pq", interface_name="ProQuest")
    pq_b = FulltextTarget(url="https://sfx.example.fi/pq", interface_name="ProQuest")
    table[ALMA.openurl_base] = [same, pq_a]
    table[SFX.openurl_base] = [same, pq_b]

    got = LR._query_targets("10.1/x", _cfg(additional_resolvers=(SFX,)))
    assert [t.url for t in got].count("https://doaj.org/article/1") == 1
    assert sum(t.interface_name == "ProQuest" for t in got) == 2


# ---------------------------------------------------------------------------
# Failure semantics — "could not ask" must stay distinct from "no route"
# ---------------------------------------------------------------------------


def test_one_library_down_does_not_gate_the_other(answers) -> None:
    """A transport blip at one institution must not deny access at the
    other. The whole module's fail-open contract depends on this."""
    table, _ = answers
    table[ALMA.openurl_base] = None        # could not ask
    table[SFX.openurl_base] = [OVID]

    got = LR._query_targets("10.1/x", _cfg(additional_resolvers=(SFX,)))
    assert [t.interface_name for t in got] == ["Ovid"]


def test_none_only_when_no_library_answered(answers) -> None:
    table, _ = answers
    table[ALMA.openurl_base] = None
    table[SFX.openurl_base] = None
    assert LR._query_targets("10.1/x", _cfg(additional_resolvers=(SFX,))) is None


def test_both_answering_empty_is_a_real_no_route(answers) -> None:
    """Distinct from the above: everyone was asked and nobody has it."""
    table, _ = answers
    table[ALMA.openurl_base] = []
    table[SFX.openurl_base] = []
    assert LR._query_targets("10.1/x", _cfg(additional_resolvers=(SFX,))) == []


# ---------------------------------------------------------------------------
# Cache isolation
# ---------------------------------------------------------------------------


def test_every_cache_key_names_its_resolver() -> None:
    """Keys used to be bare for the first-listed library, which made the
    key depend on list position (see `_cache_key`)."""
    assert LR._cache_key("10.1/x", False, ALMA.resolver_id) == f"10.1/x@@{ALMA.resolver_id}"
    assert LR._cache_key("10.1/x", True, SFX.resolver_id) == f"10.1/x::any@@{SFX.resolver_id}"
    with pytest.raises(ValueError):
        LR._cache_key("10.1/x", False, "")


def test_resolver_id_ignores_a_trailing_slash() -> None:
    assert SfxResolver("https://sfx.example.fi/jyu/ ").resolver_id == SFX.resolver_id


def test_list_order_does_not_change_which_key_a_library_reads(tmp_path, answers) -> None:
    """The 2026-09-05 incident: narrow or reorder the list so another
    library comes first, and it must not read the old first entry's
    routes."""
    table, calls = answers
    cache = LR.ResolverCache(tmp_path)
    table[ALMA.openurl_base] = [EBSCO]
    LR._query_targets("10.1/x", _cfg(cache=cache))            # Aalto first, alone
    table[SFX.openurl_base] = [OVID]
    got = LR._query_targets("10.1/x", _cfg(resolver=SFX, cache=cache))
    assert calls[-1] == SFX.openurl_base                        # JYU was asked
    assert [t.url for t in got] == [OVID.url]                   # not served Aalto's


def test_targets_carry_their_resolver_even_with_one_library(answers) -> None:
    table, _ = answers
    table[ALMA.openurl_base] = [EBSCO]
    (t,) = LR._query_targets("10.1/x", _cfg())
    assert t.resolver_id == ALMA.resolver_id
    assert t.resolver_name == "Aalto"


def test_resolvers_property_skips_an_unconfigured_primary() -> None:
    assert _cfg(resolver=None).resolvers == ()
    assert _cfg(resolver=None, additional_resolvers=(SFX,)).resolvers == (SFX,)
    assert _cfg(additional_resolvers=(SFX,)).resolvers == (ALMA, SFX)


def test_the_label_names_the_library_not_the_product() -> None:
    """Both dialects hide the institution in the path. An SFX host
    usually starts with "sfx", which would label every SFX library
    identically."""
    assert LR._resolver_label(ALMA) == "Aalto"
    assert LR._resolver_label(SFX) == "Jyu"
    assert LR._resolver_label(SfxResolver("https://sfx.example.fi/")) == "Sfx"


def test_describe_names_every_endpoint() -> None:
    assert _cfg().describe() == ALMA.openurl_base
    assert _cfg(additional_resolvers=(SFX,)).describe().startswith("Aalto + Jyu")
    assert _cfg(resolver=None).describe() == "(no link resolver configured)"


def test_each_dialect_parses_its_own_response(answers) -> None:
    """Parsing an SFX response with Alma's parser yields nothing, and
    silently — so the resolver that produced a URL must be the one that
    reads it back."""
    table, _ = answers
    table[ALMA.openurl_base] = [EBSCO]
    table[SFX.openurl_base] = [OVID]
    got = LR._query_targets("10.1/x", _cfg(additional_resolvers=(SFX,)))
    assert len(got) == 2


def test_an_sfx_secondary_is_asked_its_any_query_under_an_alma_primary(
    monkeypatch,
) -> None:
    """`lookup_dual` used to let the primary's dialect decide whether the
    date-ignoring query happens at all, for every library."""
    asked: list[tuple[str, bool]] = []

    def fake(url, cfg, doi, resolver=None):
        asked.append((resolver.openurl_base, "ignore_date_threshold=1" in url))
        return [OVID] if resolver is SFX else []

    monkeypatch.setattr(LR, "_fetch_and_parse", fake)
    dual = LR.lookup_dual("10.1/x", _cfg(additional_resolvers=(SFX,)))
    assert (SFX.openurl_base, True) in asked
    assert [t.url for t in dual.any_range] == [OVID.url]
