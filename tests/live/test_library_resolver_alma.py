"""Live test: Ex Libris Alma `uresolver` support in library_resolver.py
(issue #6).

Targets a real, public Alma instance (Aalto University) rather than a
mock — the SFX-shaped unit tests in tests/unit/test_library_resolver.py
can't catch a real Alma deployment returning an unexpected shape or a
non-XML response (both bugs this fix addresses were only found by
querying the live endpoint). Requires network access to Aalto's Alma
instance (works over Aalto's VPN; may also work unauthenticated from
off-campus — Alma uresolver endpoints commonly do).

Skipped automatically (as with all `-m live` tests) unless a
contributor runs `pytest -m live tests/live/test_library_resolver_alma.py`
explicitly; also skips gracefully if the endpoint is unreachable rather
than failing the whole run.
"""

from __future__ import annotations

import pytest
import requests
from fetchers.library_resolver import (
    LibraryResolverConfig,
    first_fulltext_target_preferred,
    has_fulltext_access,
    sfx_lookup_dual,
)

pytestmark = pytest.mark.live

_ALMA_URESOLVER_BASE = (
    "https://eu03.alma.exlibrisgroup.com/view/uresolver/358AALTO_INST/openurl"
)
# Entrepreneurship Theory and Practice, 2024 — confirmed live (2026-08-11)
# to return multiple getFullTxt services (EBSCOhost among them) via a
# DOI-only Alma query at Aalto.
_KNOWN_COVERED_DOI = "10.1177/10422587231198450"
# Syntactically valid but never registered — stable negative control;
# a fail-open regression (this fix's bug #1) would incorrectly report
# access for this DOI too.
_KNOWN_UNREGISTERED_DOI = "10.9999/nonexistent.test.doi"
# ETP's own ISSN/date/volume — confirmed live (2026-08-11) to return
# getFullTxt matches via the ISSN-fallback query regardless of DOI.
_KNOWN_COVERED_ISSN = "1042-2587"
_KNOWN_COVERED_PUB_DATE = "2024"
_KNOWN_COVERED_VOLUME = "48"


@pytest.fixture
def alma_cfg() -> LibraryResolverConfig:
    session = requests.Session()
    try:
        # Cheap reachability probe before handing the config to a test —
        # this endpoint is institution/network-gated (Aalto VPN or
        # on-campus), unlike doi.org or other public live-test targets.
        session.get(_ALMA_URESOLVER_BASE, timeout=10)
    except requests.exceptions.RequestException as e:
        pytest.skip(
            f"Aalto Alma uresolver unreachable ({e}) — requires Aalto "
            f"network/VPN access."
        )
    return LibraryResolverConfig(openurl_base=_ALMA_URESOLVER_BASE, session=session)


def test_alma_uresolver_reports_access_for_covered_doi(
    alma_cfg: LibraryResolverConfig,
) -> None:
    assert has_fulltext_access(_KNOWN_COVERED_DOI, alma_cfg) is True


def test_alma_uresolver_reports_no_access_for_unregistered_doi(
    alma_cfg: LibraryResolverConfig,
) -> None:
    """Catches the fail-open regression (bug #1): if `svc_dat=CTO` were
    dropped again, Alma would serve its HTML skin instead of XML, the
    parser would return None, and this would incorrectly assert True."""
    assert has_fulltext_access(_KNOWN_UNREGISTERED_DOI, alma_cfg) is False


def test_alma_uresolver_returns_resolvable_target_url(
    alma_cfg: LibraryResolverConfig,
) -> None:
    """Catches the schema-mismatch regression (bug #2): a parser that
    only understands SFX's <target>/<target_url> shape would return
    None here even though Alma reports real getFullTxt coverage."""
    target = first_fulltext_target_preferred(_KNOWN_COVERED_DOI, alma_cfg)
    assert target is not None
    assert "alma.exlibrisgroup.com" in target


def test_alma_uresolver_issn_fallback_recovers_access_when_doi_unmatched(
    alma_cfg: LibraryResolverConfig,
) -> None:
    """The BACKLOG.md P11 scenario, reproduced deterministically: pair
    a DOI Alma will never link to a holdings record (guaranteed empty
    DOI-only query) with a real, licensed journal's ISSN/date/volume.
    Aalto's own DOI matching always succeeds (see the other tests
    here), so this is the only way to exercise the fallback trigger
    condition against a real Alma instance without depending on an
    institution where DOI matching is naturally broken."""
    assert has_fulltext_access(
        _KNOWN_UNREGISTERED_DOI, alma_cfg,
        issn=_KNOWN_COVERED_ISSN,
        pub_date=_KNOWN_COVERED_PUB_DATE,
        volume=_KNOWN_COVERED_VOLUME,
    ) is True


def test_alma_uresolver_issn_fallback_applies_to_dual_lookup(
    alma_cfg: LibraryResolverConfig,
) -> None:
    result = sfx_lookup_dual(
        _KNOWN_UNREGISTERED_DOI, alma_cfg,
        issn=_KNOWN_COVERED_ISSN,
        pub_date=_KNOWN_COVERED_PUB_DATE,
        volume=_KNOWN_COVERED_VOLUME,
    )
    assert result.query_ok
    assert len(result.in_range) >= 1
