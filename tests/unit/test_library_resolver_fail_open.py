"""The link-resolver pre-flight must fail OPEN on an ambiguous signal.

`enrich_pdfs.py`'s Pass 3 uses this lookup as a *gate*: an item with no
full-text target never opens a browser at all. Failing that gate closed
on a transport error, an unparseable response, or an unset config makes
a network blip indistinguishable from a real entitlement gap.

That is not theoretical. A live run logged 16 items (15 Journal of
Business Ethics, 1 JIBS) as `skipped_no_library_coverage` and never
attempted them; the user then pasted their library's own holdings page
showing full-text access via EBSCOhost, SpringerLink and ProQuest for
exactly those journals.

`has_fulltext_access` has carried a "fail-open semantics" docstring
since it was written — but nothing in production ever called it. These
tests pin the behaviour onto the function the pipeline actually uses.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fetchers.library_resolver import (
    LibraryResolverConfig,
    SfxCache,
    lookup_fulltext_target,
)

DOI = "10.1007/s10551-020-04463-y"

_XML_WITH_TARGET = """<?xml version="1.0"?>
<ctx_obj_set><ctx_obj><ctx_obj_targets>
  <target>
    <service_type>getFullTxt</service_type>
    <target_url>https://link.springer.com/article/10.1007/s10551-020-04463-y</target_url>
  </target>
</ctx_obj_targets></ctx_obj></ctx_obj_set>
"""

_XML_NO_TARGETS = """<?xml version="1.0"?>
<ctx_obj_set><ctx_obj><ctx_obj_targets/></ctx_obj></ctx_obj_set>
"""


def _cfg(tmp_path, *, response=None, exc=None, base="https://sfx.example.org/sfx_local"):
    session = MagicMock()
    if exc is not None:
        session.get.side_effect = exc
    else:
        resp = MagicMock()
        resp.status_code = response[0]
        resp.text = response[1]
        resp.content = response[1].encode()
        session.get.return_value = resp
    return LibraryResolverConfig(
        openurl_base=base, sid="test", session=session,
        cache=SfxCache(tmp_path),
    )


# --- the ambiguous cases must NOT gate --------------------------------

def test_transport_error_reports_query_not_ok(tmp_path) -> None:
    result = lookup_fulltext_target(
        DOI, _cfg(tmp_path, exc=OSError("connection refused")),
    )
    assert result.url is None
    assert result.query_ok is False, "a network blip must not read as 'no access'"


def test_non_200_reports_query_not_ok(tmp_path) -> None:
    result = lookup_fulltext_target(DOI, _cfg(tmp_path, response=(503, "")))
    assert result.query_ok is False


def test_unparseable_xml_reports_query_not_ok(tmp_path) -> None:
    result = lookup_fulltext_target(
        DOI, _cfg(tmp_path, response=(200, "<not-xml")),
    )
    assert result.query_ok is False


def test_unset_openurl_base_reports_query_not_ok(tmp_path) -> None:
    """With no resolver configured there is nothing to gate on — gating
    anyway made the entire Connector fallback unreachable."""
    result = lookup_fulltext_target(DOI, _cfg(tmp_path, base="", response=(200, "")))
    assert result.query_ok is False


# --- the real verdicts must still be reported -------------------------

def test_genuinely_empty_response_is_a_real_no_coverage_verdict(tmp_path) -> None:
    """Fail-open must not degrade into "the pre-flight does nothing"."""
    result = lookup_fulltext_target(
        DOI, _cfg(tmp_path, response=(200, _XML_NO_TARGETS)),
    )
    assert result.url is None
    assert result.query_ok is True


def test_a_found_target_is_returned(tmp_path) -> None:
    result = lookup_fulltext_target(
        DOI, _cfg(tmp_path, response=(200, _XML_WITH_TARGET)),
    )
    assert result.url is not None
    assert "springer.com" in result.url
    assert result.query_ok is True


# --- negative caching -------------------------------------------------

def test_empty_results_are_not_cached(tmp_path) -> None:
    """A DOI-keyed miss was written to `sfx_cache.json` with no expiry,
    so a soft false negative became permanent — and clearing it meant
    deleting a directory that also holds the PDF cache and both
    Chromium profiles."""
    cfg = _cfg(tmp_path, response=(200, _XML_NO_TARGETS))
    lookup_fulltext_target(DOI, cfg)

    assert cfg.cache.get(DOI) is None, "a negative verdict was persisted"


def test_positive_results_are_still_cached(tmp_path) -> None:
    """Caching hits is the whole point — dropping it would re-query the
    resolver for every item on every run."""
    cfg = _cfg(tmp_path, response=(200, _XML_WITH_TARGET))
    lookup_fulltext_target(DOI, cfg)

    cached = cfg.cache.get(DOI)
    assert cached is not None
    assert cached["urls"]


def test_a_later_run_re_queries_after_an_empty_result(tmp_path) -> None:
    """The user's actual recovery path: gain access, re-run, get it."""
    cfg = _cfg(tmp_path, response=(200, _XML_NO_TARGETS))
    assert lookup_fulltext_target(DOI, cfg).url is None

    resp = MagicMock()
    resp.status_code, resp.text = 200, _XML_WITH_TARGET
    resp.content = _XML_WITH_TARGET.encode()
    cfg.session.get.return_value = resp

    assert lookup_fulltext_target(DOI, cfg).url is not None


# --- back-compat ------------------------------------------------------

@pytest.mark.parametrize(
    "response, exc",
    [((200, _XML_NO_TARGETS), None), (None, OSError("boom"))],
)
def test_legacy_helper_still_collapses_both_cases_to_none(
    tmp_path, response, exc,
) -> None:
    """`first_fulltext_target_preferred` keeps its old signature for
    existing callers; only the gating call site moved."""
    from fetchers.library_resolver import first_fulltext_target_preferred

    cfg = _cfg(tmp_path, response=response, exc=exc)
    assert first_fulltext_target_preferred(DOI, cfg) is None
