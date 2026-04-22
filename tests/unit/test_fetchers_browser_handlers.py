"""URL-template regression tests for the per-publisher browser handlers.

Each publisher's download URL pattern is a string contract with the
publisher's web interface. If the pattern drifts (e.g. someone
"cleans up" a `?download=true` query param), downloads start failing
silently for that publisher only. These tests pin the patterns so
that kind of drift fails at PR time.

Values are copied verbatim from the working old-project script at
`/Users/mronkko/Desktop/SLR motivation/scripts/fetch_pdfs_browser.py`
so any handler whose URL differs from the known-working version
surfaces here.
"""

from __future__ import annotations

from fetchers.browser import (
    AaaHandler,
    AomHandler,
    ApaHandler,
    EmeraldHandler,
    InformsHandler,
    OupHandler,
    SageHandler,
    TandfHandler,
    WileyHandler,
    all_handlers,
    resolve_by_doi,
)


# ---------------------------------------------------------------------------
# Registry shape
# ---------------------------------------------------------------------------


def test_registry_lists_all_simple_handlers() -> None:
    names = {h.name for h in all_handlers()}
    assert {"aaa", "aom", "emerald", "sage", "tandf", "wiley"} <= names


def test_registry_lists_all_custom_handlers() -> None:
    """The three handlers with bespoke download flows (apa, informs, oup)
    must be registered — their absence was the root cause of the 0/12
    browser run against the AI Entrepreneurship library."""
    names = {h.name for h in all_handlers()}
    assert {"apa", "informs", "oup"} <= names


def test_registry_has_exactly_nine_handlers() -> None:
    """Pin the total so a silently-dropped handler fails a test."""
    assert len(all_handlers()) == 9


def test_registry_handler_names_are_unique() -> None:
    names = [h.name for h in all_handlers()]
    assert len(names) == len(set(names)), f"duplicate handler name: {names}"


def test_registry_doi_prefixes_are_unique_across_handlers() -> None:
    """No two handlers should claim overlapping DOI prefixes.
    Overlap would make dispatch non-deterministic for those DOIs."""
    seen: dict[str, str] = {}
    for h in all_handlers():
        for prefix in h.doi_prefixes:
            assert prefix not in seen, (
                f"prefix {prefix} is claimed by both {seen[prefix]} and {h.name}"
            )
            seen[prefix] = h.name


# ---------------------------------------------------------------------------
# Per-publisher URL templates (pins what the old working script used)
# ---------------------------------------------------------------------------


def test_emerald_url_template() -> None:
    url = EmeraldHandler().url_template.format(doi="10.1108/IJEBR-08-2019-0513")
    assert url == (
        "https://www.emerald.com/insight/content/doi/"
        "10.1108/IJEBR-08-2019-0513/full/pdf?download=true"
    )


def test_sage_url_template() -> None:
    url = SageHandler().url_template.format(doi="10.1177/1042258717725967")
    assert url == (
        "https://journals.sagepub.com/doi/pdf/"
        "10.1177/1042258717725967?download=true"
    )


def test_tandf_url_template() -> None:
    url = TandfHandler().url_template.format(doi="10.1080/08985626.2020.1727096")
    assert url == (
        "https://www.tandfonline.com/doi/pdf/"
        "10.1080/08985626.2020.1727096?download=true"
    )


def test_wiley_url_template() -> None:
    url = WileyHandler().url_template.format(doi="10.1002/smj.70090")
    assert url == (
        "https://onlinelibrary.wiley.com/doi/pdf/10.1002/smj.70090?download=true"
    )


def test_aom_url_template() -> None:
    url = AomHandler().url_template.format(doi="10.5465/amj.2014.0387")
    assert url == "https://journals.aom.org/doi/pdf/10.5465/amj.2014.0387?download=true"


def test_aaa_url_template() -> None:
    url = AaaHandler().url_template.format(doi="10.2308/accr-52421")
    assert url == (
        "https://publications.aaahq.org/accounting-review/"
        "article-pdf/doi/10.2308/accr-52421"
    )


# ---------------------------------------------------------------------------
# Dispatch round-trip: every sample DOI resolves to the expected handler
# ---------------------------------------------------------------------------


def test_resolve_by_doi_routes_each_sample_doi_to_its_handler() -> None:
    """The DOIs match those in tests/live/conftest.py KNOWN_DOIS so
    that live-browser tests hit the same handler they claim to test."""
    cases = [
        ("10.1108/IJEBR-08-2019-0513", "emerald"),
        ("10.1177/1042258717725967",   "sage"),
        ("10.1080/08985626.2020.1727096", "tandf"),
        ("10.1002/smj.70090",          "wiley"),
        ("10.1111/j.1460-2466.2011.01539.x", "wiley"),  # Wiley alt prefix
        ("10.5465/amj.2014.0387",      "aom"),
        ("10.2308/accr-52421",         "aaa"),
    ]
    for doi, expected in cases:
        h = resolve_by_doi(doi)
        assert h is not None, f"no handler matched {doi}"
        assert h.name == expected, f"{doi}: expected {expected}, got {h.name}"


def test_resolve_by_doi_returns_none_on_unsupported_prefix() -> None:
    assert resolve_by_doi("10.1371/journal.pone.0012345") is None
    assert resolve_by_doi("10.1016/j.jbusvent.2006.10.003") is None


def test_resolve_by_doi_routes_custom_flow_publishers() -> None:
    """Pin the dispatch for the three custom-flow publishers — their
    DOI prefixes are the only thing that gets them into the right
    handler's download() method."""
    cases = [
        ("10.1037/0021-9010.93.3.481", "apa"),
        ("10.1287/orsc.2017.1182",     "informs"),
        ("10.1093/jleo/ewaa004",       "oup"),
    ]
    for doi, expected in cases:
        h = resolve_by_doi(doi)
        assert h is not None, f"no handler matched {doi}"
        assert h.name == expected, f"{doi}: expected {expected}, got {h.name}"


# ---------------------------------------------------------------------------
# Custom handlers — URL templates point at doi.org (landing page, not PDF)
# ---------------------------------------------------------------------------


def test_custom_handlers_use_doi_redirect_as_entry_url() -> None:
    """APA, INFORMS, and OUP all extract the PDF URL from the landing
    page at runtime — their url_template is the doi.org redirect."""
    for cls in (ApaHandler, InformsHandler, OupHandler):
        assert cls.url_template == "https://doi.org/{doi}", (
            f"{cls.__name__}: url_template must be the doi.org redirect "
            f"so we hit the publisher's landing page, not a guessed PDF URL"
        )


# ---------------------------------------------------------------------------
# Handler subclass shape
# ---------------------------------------------------------------------------


def test_every_simple_handler_has_display_name() -> None:
    """display_name is what shows up in the console banner; must be set."""
    for h in all_handlers():
        assert h.display_name, f"{h.name}: missing display_name"


# ---------------------------------------------------------------------------
# setup_url_template — opening the PDF URL auto-downloads and strands the
# user at about:blank; publishers with `?download=true` in the download URL
# must override setup_url_template to the article landing page.
# ---------------------------------------------------------------------------


def test_emerald_setup_url_is_landing_page_not_pdf() -> None:
    """Regression guard: opening the Emerald PDF URL directly triggers
    an auto-download that consumes the one-shot session and strands the
    user at about:blank (observed 2026-04-21 on
    10.1108/ijchm-04-2020-0259). Setup must use the landing page."""
    h = EmeraldHandler()
    setup = h._setup_url_for("10.1108/ijchm-04-2020-0259")
    assert "?download=true" not in setup
    assert "/full/pdf" not in setup
    assert setup.startswith("https://www.emerald.com/insight/content/doi/")


def test_handlers_with_download_true_have_distinct_setup_url() -> None:
    """Every handler whose `url_template` uses `?download=true` must
    override `setup_url_template` to a non-download URL."""
    affected = [h for h in all_handlers() if "?download=true" in h.url_template]
    assert affected, "sanity: some handlers should use ?download=true"
    for h in affected:
        setup = h._setup_url_for("10.1/x")
        assert "?download=true" not in setup, (
            f"{h.name}: setup_url_template must NOT use ?download=true "
            f"— Chromium auto-downloads and strands the user at about:blank"
        )


def test_handler_default_setup_url_falls_back_to_url_template() -> None:
    """Handlers without an explicit setup_url_template should use the
    download URL as setup URL (appropriate for custom flows like OUP
    where the landing-page URL IS the url_template)."""
    from fetchers.browser import OupHandler
    h = OupHandler()
    assert h._setup_url_for("10.1093/x") == "https://doi.org/10.1093/x"
