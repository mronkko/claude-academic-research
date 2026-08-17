"""Platform ranking and domain matching — the part that was dead on Alma.

`rank_key` and `matches_domains` live on the base class and match by
host **or** platform name, which is what gives the two dialects equal
standing. Before that, both keyed on the target URL's hostname, and every
Alma `resolution_url` points at the Alma redirector rather than a
publisher — so on Alma:

- `SFX_PLATFORM_PRIORITY` ranked every target as unranked, and whichever
  route the resolver happened to return first won. The reasoned
  preference for EBSCOhost over JSTOR over ProQuest (which sometimes
  serves scanned images) did nothing.
- `required_domains=("ebscohost.com",)` reported "no licensed route" for
  an article the library reached through EBSCOhost, JSTOR and three
  ProQuest packages.

These tests pin both halves so a future change cannot quietly restore
host-only matching.
"""

from __future__ import annotations

from fetchers.resolvers import (
    PLATFORM_PRIORITY,
    AlmaResolver,
    FulltextTarget,
    Platform,
    SfxResolver,
    effective_host,
    host_matches_domains,
    platform_priority_from_keys,
)

SFX = SfxResolver("https://sfx.example.org/inst01")
ALMA = AlmaResolver(
    "https://eu03.alma.exlibrisgroup.com/view/uresolver/358AALTO_INST/openurl"
)
ALMA_URL = "https://aalto.alma.exlibrisgroup.com/view/action/uresolver.do?x=1"


def _alma(package: str, interface: str) -> FulltextTarget:
    return FulltextTarget(
        url=ALMA_URL, package_name=package, interface_name=interface,
    )


# ---------------------------------------------------------------------------
# effective_host / host_matches_domains
# ---------------------------------------------------------------------------


def test_effective_host_unwraps_ezproxy() -> None:
    """EZproxy carries the real target in a `url=` query param; without
    unwrapping, every proxied route would look like the proxy."""
    assert effective_host(
        "http://ezproxy.example.edu/login?url=https://www.jstor.org/stable/1",
    ) == "www.jstor.org"


def test_effective_host_returns_host_when_not_wrapped() -> None:
    assert effective_host("https://onlinelibrary.wiley.com/doi/x") == (
        "onlinelibrary.wiley.com"
    )


def test_effective_host_is_empty_for_malformed_input() -> None:
    assert effective_host("") == ""
    assert effective_host("not a url") == ""


def test_host_matches_domains_suffix_and_exact() -> None:
    assert host_matches_domains("https://onlinelibrary.wiley.com/x", ("wiley.com",))
    assert host_matches_domains("https://wiley.com/x", ("wiley.com",))


def test_host_matches_domains_rejects_unrelated_and_substring_hosts() -> None:
    """`notwiley.com` shares a substring but is not a suffix match — the
    difference between routing to the publisher and routing to a stranger."""
    assert not host_matches_domains("https://www.jstor.org/x", ("wiley.com",))
    assert not host_matches_domains("https://notwiley.com/x", ("wiley.com",))


def test_host_matches_domains_unwraps_before_matching() -> None:
    assert host_matches_domains(
        "http://ezproxy.example.edu/login?url=https://onlinelibrary.wiley.com/x",
        ("wiley.com",),
    )


# ---------------------------------------------------------------------------
# Ranking — SFX ranks by host, Alma by name, one code path
# ---------------------------------------------------------------------------


def test_sfx_ranks_by_host() -> None:
    ebsco = FulltextTarget(url="https://search.ebscohost.com/x")
    jstor = FulltextTarget(url="https://www.jstor.org/stable/1")
    proquest = FulltextTarget(url="https://www.proquest.com/x")
    assert SFX.rank_key(ebsco) < SFX.rank_key(jstor) < SFX.rank_key(proquest)


def test_alma_ranks_by_platform_name() -> None:
    """Same ordering as SFX, reached through `interface_name` because the
    URL is identical for all three."""
    ebsco = _alma("EBSCOhost Business Source Ultimate", "EBSCOhost")
    jstor = _alma("JSTOR Archival Journals", "JSTOR")
    proquest = _alma("ABI/INFORM Collection", "ProQuest")
    assert ALMA.rank_key(ebsco) < ALMA.rank_key(jstor) < ALMA.rank_key(proquest)


def test_alma_ranking_was_the_bug_every_target_is_no_longer_unranked() -> None:
    """The regression this change exists to fix: an Alma target used to
    rank `len(priority)` regardless of platform."""
    assert ALMA.rank_key(_alma("EBSCOhost Business Source Ultimate", "EBSCOhost")) < (
        len(PLATFORM_PRIORITY)
    )


def test_unranked_target_sorts_last_but_still_counts() -> None:
    """An unrecognised platform loses to every ranked one and still beats
    having no target at all."""
    unknown = _alma("Some Local Repository", "LocalThing")
    assert ALMA.rank_key(unknown) == len(PLATFORM_PRIORITY)


def test_ranking_is_stable_for_equal_ranks() -> None:
    """Two ProQuest packages tie; `min` must keep the resolver's order so
    a re-run picks the same route."""
    first = _alma("ABI/INFORM Collection", "ProQuest")
    second = _alma("Social Science Premium Collection", "ProQuest")
    assert ALMA.rank_key(first) == ALMA.rank_key(second)
    assert min([first, second], key=ALMA.rank_key) is first


def test_priority_order_puts_ebscohost_first() -> None:
    assert PLATFORM_PRIORITY[0].key == "ebscohost"
    assert [p.key for p in PLATFORM_PRIORITY][-1] == "proquest"


# ---------------------------------------------------------------------------
# matches_domains — host or name
# ---------------------------------------------------------------------------


def test_alma_target_matches_a_requested_publisher_domain_by_name() -> None:
    """The false negative this fixes: the caller asks for EBSCOhost by
    domain, the target only says so by name."""
    target = _alma("EBSCOhost Business Source Ultimate", "EBSCOhost")
    assert ALMA.matches_domains(target, ("ebscohost.com",)) is True
    assert ALMA.matches_domains(target, ("proquest.com",)) is False


def test_alma_name_matching_is_case_insensitive_and_substring() -> None:
    """Alma spells one platform many ways across packages."""
    assert ALMA.matches_domains(
        _alma("EBSCOhost Academic Search Premier", "ebscohost"), ("ebsco.com",),
    ) is True


def test_sfx_target_matches_by_host_without_needing_names() -> None:
    target = FulltextTarget(url="https://onlinelibrary.wiley.com/doi/x")
    assert SFX.matches_domains(target, ("wiley.com",)) is True
    assert SFX.matches_domains(target, ("jstor.org",)) is False


def test_matches_domains_is_false_for_an_unknown_domain() -> None:
    """A true negative must survive: not everything should match."""
    target = _alma("EBSCOhost Business Source Ultimate", "EBSCOhost")
    assert ALMA.matches_domains(target, ("nosuch.example",)) is False


def test_a_nameless_alma_target_falls_back_to_host_only() -> None:
    """Degrades to the old behaviour rather than matching wrongly."""
    bare = FulltextTarget(url=ALMA_URL)
    assert ALMA.matches_domains(bare, ("ebscohost.com",)) is False


# ---------------------------------------------------------------------------
# PLATFORM_PRIORITY is the identity map too, not only the ranking.
#
# On Alma there is no publisher host to match, so a platform absent from
# this table is invisible to `matches_domains` — and a handler whose
# platform is invisible can only ever be Case 1 ("try direct anyway"),
# whatever the resolver actually said. Five handlers were missing
# entries, covering 61% of one real 655-item corpus.
# ---------------------------------------------------------------------------


def test_every_handler_has_a_platform_entry() -> None:
    """Without one the Case 1/2/3 coverage guard is dead for that
    handler — silently, and only on Alma."""
    from fetchers.browser import all_handlers

    known = {d.lower() for p in PLATFORM_PRIORITY for d in p.domains}
    missing = {
        h.name: h.direct_access_domains
        for h in all_handlers()
        if h.direct_access_domains
        and not any(d.lower() in known for d in h.direct_access_domains)
    }
    assert not missing, f"handlers with no PLATFORM_PRIORITY entry: {missing}"


def test_the_added_platforms_match_their_alma_naming() -> None:
    for package, interface, domain in (
        ("INFORMS PubsOnline", "INFORMS", "informs.org"),
        ("APA PsycNET", "PsycNET", "psycnet.apa.org"),
        ("Academy of Management Journals", "Academy of Management", "aom.org"),
        ("Emerald Premier", "Emerald Insight", "emerald.com"),
        ("AAA Digital Library", "American Accounting Association", "aaahq.org"),
    ):
        assert ALMA.matches_domains(
            _alma(package, interface), (domain,)
        ) is True, package


def test_added_names_do_not_collide_with_aggregator_packages() -> None:
    """Substring matching makes near-misses dangerous, and two are real.

    "ABI/INFORM Collection" is ProQuest, not INFORMS. And EBSCOhost
    resells APA PsycArticles — matching that onto the `apa` handler would
    send an EBSCOhost-licensed item to psycnet.apa.org, which this
    library cannot open at all. Hence `apa` is keyed on "psycnet", never
    "psycarticles".
    """
    for package, interface, domain in (
        ("ABI/INFORM Collection", "ProQuest", "informs.org"),
        ("ABI/INFORM Global", "ProQuest", "informs.org"),
        ("EBSCOhost APA PsycArticles", "EBSCOhost", "psycnet.apa.org"),
        ("EBSCOhost Business Source Ultimate", "EBSCOhost", "aom.org"),
    ):
        assert ALMA.matches_domains(
            _alma(package, interface), (domain,)
        ) is False, f"{package} wrongly matched {domain}"


def test_the_additions_join_the_publisher_block_not_the_end() -> None:
    """They are publisher-direct, so by this table's own rationale they
    outrank JSTOR's cover page and ProQuest's occasional scan — which
    must stay the last resorts."""
    keys = [p.key for p in PLATFORM_PRIORITY]
    assert keys[0] == "ebscohost"
    assert keys[-2:] == ["jstor", "proquest"]
    for added in ("informs", "apa", "aom", "emerald", "aaa"):
        assert keys.index("oup") < keys.index(added) < keys.index("jstor"), added


# ---------------------------------------------------------------------------
# platform_priority_from_keys — `[library] platform_priority`
# ---------------------------------------------------------------------------


def test_priority_override_reorders_named_platforms_first() -> None:
    reordered = platform_priority_from_keys(("jstor", "proquest"))
    assert [p.key for p in reordered][:2] == ["jstor", "proquest"]


def test_priority_override_keeps_unnamed_platforms_after_named_ones() -> None:
    """Naming one preference must not demote everything else to
    unranked — the rest keep their relative order behind it."""
    reordered = platform_priority_from_keys(("jstor",))
    assert reordered[0].key == "jstor"
    assert len(reordered) == len(PLATFORM_PRIORITY)
    assert [p.key for p in reordered][1:] == [
        p.key for p in PLATFORM_PRIORITY if p.key != "jstor"
    ]


def test_priority_override_ignores_unknown_keys() -> None:
    """Comes from user config; a typo should change nothing, not raise."""
    assert platform_priority_from_keys(("nope",)) == PLATFORM_PRIORITY
    assert [p.key for p in platform_priority_from_keys(("nope", "jstor"))][0] == (
        "jstor"
    )


def test_custom_priority_is_honoured_by_rank_key() -> None:
    only_proquest = (Platform("proquest", ("proquest.com",), ("ProQuest",)),)
    pq = _alma("ABI/INFORM Collection", "ProQuest")
    ebsco = _alma("EBSCOhost Business Source Ultimate", "EBSCOhost")
    assert ALMA.rank_key(pq, only_proquest) == 0
    assert ALMA.rank_key(ebsco, only_proquest) == 1
