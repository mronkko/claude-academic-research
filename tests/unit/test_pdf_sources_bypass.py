"""Tests for the `bypass_prefix_filter` kwarg on prefix-filtering PDF sources.

The three prefix-filtering API sources (Wiley TDM, Elsevier/
ScienceDirect, Springer) skip non-matching DOIs by default. Pass 2
of the browser pipeline passes `bypass_prefix_filter=True` after
DOI resolution reveals that the DOI's canonical publisher matches
the source even though the prefix doesn't — catches migrated-journal
cases (e.g. a journal that kept its old prefix after moving to
Elsevier).

Each test verifies the PREFIX GATE only. Whether the source
ultimately returns a PDF is out of scope (that's a network/live
question). What matters for v0.4.0 is that the bypass flag actually
lets the code advance past the prefix check.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fetchers.sciencedirect import ScienceDirectSource
from fetchers.springer import SpringerSource
from fetchers.wiley import WileySource


# ---------------------------------------------------------------------------
# direct_access_domains declarations (used by Pass 2 routing)
# ---------------------------------------------------------------------------


def test_wiley_declares_direct_access_domains() -> None:
    assert "onlinelibrary.wiley.com" in WileySource.direct_access_domains


def test_sciencedirect_declares_direct_access_domains() -> None:
    assert "sciencedirect.com" in ScienceDirectSource.direct_access_domains


def test_springer_declares_direct_access_domains() -> None:
    assert "link.springer.com" in SpringerSource.direct_access_domains


# ---------------------------------------------------------------------------
# Wiley — prefix gate + bypass
# ---------------------------------------------------------------------------


def test_wiley_non_prefix_doi_skipped_without_bypass(tmp_path) -> None:
    """10.9999/x is not a Wiley prefix. Without the bypass, fetch_pdf
    returns None WITHOUT checking the token (the prefix gate is the
    first short-circuit)."""
    src = WileySource(http=MagicMock(), config=MagicMock())
    src._token = MagicMock(return_value="")    # type: ignore[method-assign]

    result = src.fetch_pdf("10.9999/fake-journal-1", cache_dir=tmp_path)
    assert result is None
    assert not src._token.called   # never got past the prefix gate


def test_wiley_bypass_proceeds_past_prefix_gate(tmp_path) -> None:
    """With bypass=True, the prefix check is skipped; execution
    reaches the token check (which returns None because we left
    token empty, but it does get reached — observable via the
    _token() call)."""
    src = WileySource(http=MagicMock(), config=MagicMock())
    src._token = MagicMock(return_value="")    # type: ignore[method-assign]

    result = src.fetch_pdf(
        "10.9999/fake-journal-1", cache_dir=tmp_path,
        bypass_prefix_filter=True,
    )
    assert result is None
    assert src._token.called       # prefix gate was bypassed


def test_wiley_prefix_match_still_works(tmp_path) -> None:
    """Regression guard: existing behaviour (10.1002/* matches prefix)
    is unchanged — reaches the token check without bypass."""
    src = WileySource(http=MagicMock(), config=MagicMock())
    src._token = MagicMock(return_value="")    # type: ignore[method-assign]

    src.fetch_pdf("10.1002/sej.1234", cache_dir=tmp_path)
    assert src._token.called


# ---------------------------------------------------------------------------
# ScienceDirect / Elsevier — prefix gate + bypass
# ---------------------------------------------------------------------------


def _elsevier_source(status_code: int = 404) -> ScienceDirectSource:
    cfg = MagicMock()
    cfg.elsevier_api_key = "FAKE-KEY"
    http = MagicMock()
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b""
    http.get.return_value = resp
    return ScienceDirectSource(http=http, config=cfg)


def test_sciencedirect_non_prefix_doi_skipped_without_bypass(tmp_path) -> None:
    src = _elsevier_source()
    result = src.fetch_pdf("10.9999/fake", cache_dir=tmp_path)
    assert result is None
    assert not src.http.get.called


def test_sciencedirect_bypass_proceeds_to_api_call(tmp_path) -> None:
    """bypass=True lets the Elsevier API URL actually get hit. A 404
    response still produces None (prefix-bypass doesn't fabricate
    data), but the HTTP call confirms we got past the gate."""
    src = _elsevier_source(status_code=404)
    result = src.fetch_pdf(
        "10.9999/fake-journal", cache_dir=tmp_path,
        bypass_prefix_filter=True,
    )
    assert result is None
    assert src.http.get.called
    url_arg = src.http.get.call_args.args[0]
    assert "api.elsevier.com" in url_arg


def test_sciencedirect_prefix_match_still_works(tmp_path) -> None:
    src = _elsevier_source(status_code=404)
    src.fetch_pdf("10.1016/j.jbusvent.2023.01.001", cache_dir=tmp_path)
    assert src.http.get.called


# ---------------------------------------------------------------------------
# Springer — prefix gate + bypass
# ---------------------------------------------------------------------------


def _springer_source(status_code: int = 404) -> SpringerSource:
    http = MagicMock()
    resp = MagicMock()
    resp.status_code = status_code
    resp.content = b""
    http.get.return_value = resp
    return SpringerSource(http=http, config=MagicMock())


def test_springer_non_prefix_doi_skipped_without_bypass(tmp_path) -> None:
    src = _springer_source()
    result = src.fetch_pdf("10.9999/fake", cache_dir=tmp_path)
    assert result is None
    assert not src.http.get.called


def test_springer_bypass_proceeds_to_landing_page(tmp_path) -> None:
    src = _springer_source(status_code=404)
    result = src.fetch_pdf(
        "10.9999/fake", cache_dir=tmp_path, bypass_prefix_filter=True,
    )
    assert result is None
    assert src.http.get.called
    url_arg = src.http.get.call_args.args[0]
    assert "link.springer.com" in url_arg


def test_springer_prefix_match_still_works(tmp_path) -> None:
    src = _springer_source(status_code=404)
    src.fetch_pdf("10.1007/s11187-019-00267-1", cache_dir=tmp_path)
    assert src.http.get.called
