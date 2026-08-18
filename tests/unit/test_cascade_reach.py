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
    assert 'getattr(args, "plan", False)' in head, (
        "the Pass 2 API retry source list is built without consulting "
        "--plan; a preview would fetch and attach again"
    )

    # And the flag it already honoured is still honoured.
    assert "args.dry_run" in src
    argparse.Namespace(plan=True)  # documents the shape the gate reads
