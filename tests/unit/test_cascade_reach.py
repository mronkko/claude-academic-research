"""What the default cascade is willing to ask, and on whose behalf.

Both things pinned here were found the same way: a live 1,895-item pass
over a real library, where the pipeline reported "no PDF" for items it
had never actually asked about. Neither was a crash, and neither showed
up in any log — a source that is never selected and a prefix that
matches no handler both fail by staying quiet.

1. **Wiley was excluded from the default cascade.** The justification
   was that it "requires a specific auth contract", but every other
   token-gated source in the list self-disables the same way and was
   never excluded. Because `--all` builds its Pass 1 from
   `pdf_sources()`, the documented "run everything" invocation skipped
   Wiley outright; a separate `--sources wiley` pass over the same
   library then recovered 47 PDFs.

2. **Legacy imprint prefixes matched no handler.** Publishers absorb
   each other and the acquired DOI prefixes keep resolving on the new
   owner's platform, under the same URL shape the handler already
   builds. Kluwer (12 items), Routledge / Haworth / Erlbaum (24), and
   Baywood (7) all fell through to the resolver route for want of one
   line each.
"""

from __future__ import annotations

import fetchers
import pytest
from fetchers.browser import resolve_by_doi


def _default_names(**kw) -> list[str]:
    import requests
    return [s.name for s in fetchers.pdf_sources(requests.Session(), None, **kw)]


# --- 1. the cascade selects Wiley ------------------------------------


def test_wiley_is_in_the_default_cascade() -> None:
    """Not selectable-on-request: selected by default, like every other
    token-gated publisher source."""
    assert "wiley" in _default_names()


def test_wiley_outranks_the_open_access_aggregators() -> None:
    """Ordering is by version quality. Wiley TDM returns the publisher's
    own file; the stage-3 aggregators frequently return an author
    accepted manuscript whose pagination does not match. Selecting Wiley
    but ranking it below them would attach the worse copy whenever both
    answer.
    """
    names = _default_names()
    for aggregator in ("openalex", "unpaywall", "semantic_scholar", "core"):
        assert names.index("wiley") < names.index(aggregator), aggregator


def test_browser_is_still_excluded_by_default() -> None:
    """The interactive source stays opt-in. Including Wiley was about an
    auth contract that self-disables; `browser` opens a real window a
    human may have to click, which does not."""
    assert "browser" not in _default_names()


def test_preprints_remain_opt_in() -> None:
    assert "preprint" not in _default_names()
    assert "preprint" in _default_names(allow_preprints=True)


# --- 2. legacy imprints reach their current publisher ----------------


@pytest.mark.parametrize(
    ("doi", "handler", "imprint"),
    [
        ("10.1023/A:1015630930326", "springer", "Kluwer Academic → Springer"),
        ("10.4324/9780203806098", "tandf", "Routledge → T&F"),
        ("10.1300/J075v26n03_01", "tandf", "Haworth → T&F"),
        ("10.1207/s15327043hup1803_2", "tandf", "Lawrence Erlbaum → T&F"),
        ("10.2190/AG.80.1.a", "sage", "Baywood → Sage"),
        ("10.1348/096317909x479692", "wiley", "BPS, published by Wiley"),
    ],
)
def test_a_legacy_imprint_doi_reaches_its_current_publisher(
    doi: str, handler: str, imprint: str,
) -> None:
    """Each of these serves from the acquiring publisher's platform under
    the same URL shape the handler already builds, so the prefix is the
    only thing that was missing."""
    found = resolve_by_doi(doi)
    assert found is not None, f"{imprint}: {doi} still matches no handler"
    assert found.name == handler, f"{imprint}: routed to {found.name}"


def test_the_wiley_api_source_and_its_browser_handler_agree() -> None:
    """Two prefix lists for one publisher drift. The browser handler is
    the fallback for exactly the DOIs the TDM route could not serve, so a
    prefix in one and not the other means an item with no second chance.
    """
    from fetchers.browser import all_handlers
    from fetchers.wiley import _WILEY_PREFIXES

    handler = next(h for h in all_handlers() if h.name == "wiley")
    assert set(handler.doi_prefixes) == set(_WILEY_PREFIXES)


# --- 3. --plan does not mutate the library ---------------------------


def test_plan_mode_builds_no_fetching_sources() -> None:
    """`--plan` is documented as "classify items and print the publisher
    queue, then exit without opening a browser". It also ran the Pass 2
    API retry, which downloads and attaches — the retry checked
    `--dry-run` but never `--plan`. Caught live: a `--plan` invocation on
    a 1,251-item queue attached Wiley PDFs to a real library.

    The retry is gated on `pass2_api_sources` being non-empty, so the
    refusal is pinned there: under `--plan` the list must stay empty and
    nothing downstream can fetch. The resolver lookups it performs are
    deliberate and stay — they are what answers "what will this ask of
    me" — but a preview that writes is not a preview.
    """
    import argparse
    import inspect

    import enrich_pdfs

    src = inspect.getsource(enrich_pdfs._run_browser_in_process)
    head = src.split("pass2_api_sources: list = []")[1].split("\n\n")[0]
    assert "plan_only" in head, (
        "the Pass 2 API retry source list is built without consulting "
        "--plan; a preview would fetch and attach again"
    )
    # `plan_only` is only a gate if it comes from the flag. Spelled once
    # and reused, so the banner and the exit cannot disagree with the
    # thing that actually suppresses fetching.
    assert 'plan_only = bool(getattr(args, "plan", False))' in src

    # And the flag it already honoured is still honoured.
    assert "args.dry_run" in src
    argparse.Namespace(plan=True)  # documents the shape the gate reads


def test_the_preflight_banner_does_not_promise_fetching_under_plan() -> None:
    """The fetch gate above shipped and the banner narrating it did not
    move, so `--plan` spent three days announcing "a real fetch, and it
    attaches" while attaching nothing. A user read their own terminal,
    believed it over the code, and filed issue #8 against the fix.

    That is not a cosmetic defect. `--plan` exists to tell you what a run
    will do to your library; a preview that misdescribes itself fails at
    the one job it has, and it fails *credibly*, which is worse than
    failing loudly. So the banner is pinned to the same flag the gate
    reads: under `--plan` it must not claim anything is fetched or
    attached.
    """
    import inspect

    import enrich_pdfs

    src = inspect.getsource(enrich_pdfs._run_browser_in_process)
    banner = src.split("Checking library access via")[1].split(
        "preflight_cost_line",
    )[0]
    assert "if plan_only:" in banner, (
        "the pre-flight banner does not branch on --plan; whatever it "
        "says is being said to both audiences"
    )
    plan_branch = banner.split("if plan_only:")[1].split("else:")[0]
    for promise in ("it attaches", "a real fetch"):
        assert promise not in plan_branch, (
            f"the --plan banner still promises {promise!r} — this is the "
            f"exact sentence that produced issue #8"
        )
    assert "nothing is attached" in plan_branch


def test_the_preflight_prices_itself_before_it_starts() -> None:
    """A serial sweep at ~2s per uncached item is an hour on a few
    thousand, and issue #8 was filed by someone who found that out at
    600/3,542. The cost cannot live in `--help`, because the number that
    matters is how many answers are already cached — so it is computed
    and printed before the loop.

    Pinned at the count rather than the wording: an estimate that ignores
    the cache would be worse than none, since it would be wrong in the
    direction that discourages re-running a warm queue.
    """
    from unittest.mock import MagicMock

    import enrich_pdfs
    from fetchers.library_resolver import ResolverCache

    cfg = MagicMock()
    cfg.cache = None      # nothing cached, so nothing is free
    cfg.resolvers = ()
    line = " ".join(
        enrich_pdfs._preflight_cost_line(["10.1/a", "10.1/b"], cfg).split()
    )
    assert "2 of 2" in line and "0 already cached" in line
    assert "min" in line or "s" in line

    assert enrich_pdfs._preflight_cost_line([], cfg).startswith("  No DOIs")
    assert isinstance(ResolverCache, type)


def test_the_cost_estimate_stays_coarse() -> None:
    """A duration derived from a constant rate must not be printed as if
    it were measured. `47.3 min` claims a precision the input does not
    have; `47 min` claims the right one.
    """
    import enrich_pdfs

    assert enrich_pdfs._humanize_duration(12) == "12s"
    assert enrich_pdfs._humanize_duration(600) == "10 min"
    assert enrich_pdfs._humanize_duration(3 * 3600 + 300) == "3h05m"
    assert "." not in enrich_pdfs._humanize_duration(1234.567)


# --- 4. --publisher must not pay for publishers it discards ----------


def test_publisher_filter_skips_the_resolver_for_other_publishers() -> None:
    """`--publisher wiley` used to ask the link resolver about every
    handler-matched item in the queue and then throw all but wiley's
    away: the filter on `items_by_pub` runs *after* the classification
    loop. On a live 1,251-item queue that was ~830 items at two Alma
    round-trips each, to keep 162 — and ten publisher blocks paid it ten
    times.

    Pinned structurally: the skip must sit before the `lookup_dual` call,
    because that call is the expensive half. A refactor that moves the
    guard below it restores the cost silently — nothing would fail, the
    run would just be slow again, which is exactly how this survived.
    """
    import inspect

    import enrich_pdfs

    src = inspect.getsource(enrich_pdfs._run_browser_in_process)
    guard = "if args.publisher and ("
    assert guard in src, "the --publisher early skip is gone"

    # It must precede *both* expensive things in the loop. Placing it
    # below the Pass 2 API retry is how a `--publisher apa` run ended up
    # sitting in `wiley_tdm`'s rate-limit sleep, re-asking Wiley about
    # pre-2000 articles it had already refused — visible only because a
    # user interrupted it and read the traceback.
    for costly, why in (
        ("retry_result = src.fetch_pdf(", "the Pass 2 API retry"),
        ("dual = lookup_dual(", "the link resolver"),
    ):
        assert src.index(guard) < src.index(costly), (
            f"the --publisher skip must precede {why}, or this run pays "
            f"for items it will discard"
        )


def test_the_preflight_reports_progress() -> None:
    """It is a serial loop over hundreds of items with two network calls
    apiece. Before this it printed one header and then nothing, which a
    user correctly read as a hang."""
    import inspect

    import enrich_pdfs

    assert enrich_pdfs._PREFLIGHT_TICK > 0
    src = inspect.getsource(enrich_pdfs._run_browser_in_process)
    assert "checked % _PREFLIGHT_TICK" in src


# --- 5. the resolver cache can outlive a per-pass cache directory ----


def test_resolver_cache_dir_defaults_to_the_pdf_cache_dir() -> None:
    """Issue #9: `--log-csv`, `--failure-log-csv` and `--cache-dir` are all
    per-invocation, which encourages one directory per pass so the logs
    stay separable — and that silently fragmented the one cache that is
    expensive to rebuild. On a 2,551-item pass it cost ~48 minutes of
    re-asking answers established minutes earlier.

    The two caches are asymmetric in exactly the wrong direction: the PDF
    cache is gigabytes and refetched in parallel; the resolver cache is
    megabytes, rebuilt serially against an institutional endpoint, and
    this package already caches its *misses* for a week because those
    lookups were the slowest thing in the pre-flight.

    Defaulting to `--cache-dir` keeps the documented "delete that
    directory and this goes too" property for anyone who does not opt in.
    """
    import enrich_pdfs

    parser = enrich_pdfs._build_parser()
    args = parser.parse_args([])
    assert args.resolver_cache_dir is None
    assert (args.resolver_cache_dir or args.cache_dir) == args.cache_dir

    args = parser.parse_args(["--resolver-cache-dir", "/shared/resolver"])
    assert (args.resolver_cache_dir or args.cache_dir) == "/shared/resolver"


def test_the_resolver_cache_dir_reaches_load_from_config() -> None:
    """One call site, so the whole feature is one argument — but an
    argument parsed and never passed is the most plausible way for this
    to ship broken, and it would fail silently: the run would simply be
    slow again.
    """
    import inspect

    import enrich_pdfs

    src = inspect.getsource(enrich_pdfs._run_browser_in_process)
    call = src.split("resolver_cfg = load_from_config(")[1].split("\n    )")[0]
    assert 'getattr(args, "resolver_cache_dir", None) or args.cache_dir' in call
