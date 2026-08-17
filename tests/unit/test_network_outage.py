"""A lost connection is not a verdict about an article.

Handlers report every download failure the same way — `download()`
returns None — so "the publisher has nothing for this DOI" and "this
machine is offline" arrived at the orchestrator indistinguishable, and
the second was classified UNAVAILABLE: the one cause that licenses a
full-text exclusion, applied to an article no server was ever asked
about.

The run that produced this: the network dropped for four minutes and the
EBSCOhost batch burned 193 consecutive items at ~1.2 s each, every one
recorded as a fetch failure. The user confirmed the outage afterwards;
nothing was wrong with any of the 193 articles.

Two defences. `is_transport_error` classifies those failures
NETWORK_ERROR ("retry next run"), and `NetworkOutage` stops the pass
after a run of them so the queue is not shredded — un-attempted items
get no log row at all, which is what keeps them re-runnable.
"""

from __future__ import annotations

import pdf_fetch_log
import pytest
from fetchers.browser.base import (
    TRANSPORT_ERROR_MARKERS,
    NetworkOutage,
    is_transport_error,
)

# Verbatim from the live run's log.
OBSERVED = [
    "Page.goto: net::ERR_INTERNET_DISCONNECTED at https://aalto.alma.exlibrisgroup.com/",
    "Page.goto: net::ERR_NAME_NOT_RESOLVED at https://aalto.alma.exlibrisgroup.com/",
    "Page.goto: net::ERR_NETWORK_CHANGED at https://aalto.alma.exlibrisgroup.com/",
]


@pytest.mark.parametrize("message", OBSERVED)
def test_the_observed_outage_errors_are_transport(message: str) -> None:
    assert is_transport_error(message) is True


def test_matching_is_case_insensitive() -> None:
    """Chromium spells them upper-case; the markers are lower-case."""
    assert is_transport_error("net::ERR_INTERNET_DISCONNECTED") is True
    assert is_transport_error("net::err_internet_disconnected") is True


def test_a_real_publisher_failure_is_not_transport() -> None:
    """The distinction the whole file rests on. These mean the server
    answered and had nothing — a genuine verdict, classified as usual."""
    for message in (
        "never reached the viewer (stopped at https://openurl.ebsco.com/detailv2)",
        "viewer loaded but served no PDF (article may be abstract-only here)",
        "truncated download: missing %%EOF",
        "client challenge (3038B)",
        "",
    ):
        assert is_transport_error(message) is False, message


def test_a_plain_timeout_is_not_treated_as_an_outage() -> None:
    """A slow publisher is not a dead network. Timeouts are excluded on
    purpose: they are the most common *non*-outage failure, and tripping
    the breaker on them would abandon queues over one slow server."""
    assert is_transport_error("Timeout 30000ms exceeded.") is False


def test_every_marker_is_lower_case() -> None:
    """`is_transport_error` lower-cases the haystack, not the needles, so
    an upper-case marker here would silently never match."""
    for marker in TRANSPORT_ERROR_MARKERS:
        assert marker == marker.lower(), marker


# ---------------------------------------------------------------------------
# What the classification has to produce
# ---------------------------------------------------------------------------


def test_network_error_is_recoverable_and_not_an_exclusion() -> None:
    assert (
        pdf_fetch_log.FailureCause.NETWORK_ERROR.value
        in pdf_fetch_log.RECOVERABLE_CAUSES
    )
    suggestion = pdf_fetch_log.SUGGESTED_FE_CODE[
        pdf_fetch_log.FailureCause.NETWORK_ERROR.value
    ]
    assert "FE" not in suggestion
    assert "etry" in suggestion


def test_the_default_for_an_unexplained_browser_failure_is_still_unavailable() -> None:
    """The transport branch must not become a blanket amnesty: a handler
    that genuinely found nothing, after the browser pass has run, is
    still a true negative."""
    assert (
        pdf_fetch_log.classify_failure(
            item_type="journalArticle", http_status=None,
        )
        == pdf_fetch_log.FailureCause.UNAVAILABLE
    )


def test_network_error_outranks_unavailable_when_rows_are_collapsed() -> None:
    """An item with one outage row and one genuine miss must read as
    recoverable, not as an FE6 candidate."""
    rows = [
        {"item_key": "A", "source": "crossref",
         "cause": pdf_fetch_log.FailureCause.UNAVAILABLE.value},
        {"item_key": "A", "source": "ebsco",
         "cause": pdf_fetch_log.FailureCause.NETWORK_ERROR.value},
    ]
    assert (
        pdf_fetch_log.latest_per_item(rows)["A"]["cause"]
        == pdf_fetch_log.FailureCause.NETWORK_ERROR.value
    )


# ---------------------------------------------------------------------------
# The breaker
# ---------------------------------------------------------------------------


def test_the_threshold_is_low_enough_to_matter() -> None:
    """At the observed ~1.2 s per failed item, the threshold is the whole
    cost of an outage. 193 items burned before; this caps it."""
    import enrich_pdfs

    assert 2 <= enrich_pdfs._OUTAGE_THRESHOLD <= 10


def test_outage_is_its_own_exception_type() -> None:
    """Caught by type at the call sites, so it cannot be confused with a
    handler bug and swallowed by a bare `except Exception`."""
    assert issubclass(NetworkOutage, RuntimeError)
    with pytest.raises(NetworkOutage):
        raise NetworkOutage("5 consecutive network errors on EBSCOhost")
