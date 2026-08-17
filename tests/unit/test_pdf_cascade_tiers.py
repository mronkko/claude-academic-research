"""The PDF cascade's tier order, and the paid OpenAlex opt-in gate.

Two things are pinned here, both of which cost the user money or
research quality when they drift:

1. **Tier order.** The cascade is ranked by version quality first, cost
   second: free version of record → paid version of record → open-access
   author versions. The paid OpenAlex Content API therefore sits *above*
   Unpaywall / Semantic Scholar / CORE, and *below* the free
   publisher-direct sources. Getting this wrong is silent — retrieval
   still "works", it just quietly buys PDFs the free tiers would have
   served, or files an author manuscript when the version of record was
   a cent away.

2. **The opt-in gate is tri-state.** Absent means enabled (a configured
   key is itself consent, and an upgrade must not disable a working
   tier); only an explicit false turns it off. A plain `bool()` cast
   would read the string `"false"` as True, which is exactly the bug
   this guards.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import fetchers
import pytest
from fetchers.openalex import (
    OpenAlexContentSource,
    OpenAlexSource,
    coerce_paid_opt_in,
)

DOI = "10.1002/hrm.21999"


# ---------------------------------------------------------------------
# Tier order
# ---------------------------------------------------------------------

def _default_names() -> list[str]:
    return [s.name for s in fetchers.pdf_sources(MagicMock(), None)]


def test_paid_content_api_ranks_above_the_open_access_aggregators() -> None:
    """Stage 2 (paid version of record) beats stage 3 (author versions)."""
    names = _default_names()
    paid = names.index("openalex_content")
    for free_oa in ("openalex", "unpaywall", "semantic_scholar", "core"):
        assert paid < names.index(free_oa), (
            f"openalex_content must precede {free_oa}: a paid version of "
            f"record outranks a free author manuscript."
        )


def test_paid_content_api_ranks_below_every_free_version_of_record() -> None:
    """Stage 1 is free or already covered by an institutional
    subscription, so nothing paid may run before it."""
    names = _default_names()
    paid = names.index("openalex_content")
    for free_vor in ("sciencedirect", "springer", "crossref", "pubmed_central"):
        assert names.index(free_vor) < paid, (
            f"{free_vor} must precede openalex_content: never spend before "
            f"exhausting the free version-of-record sources."
        )


def test_paid_content_api_is_the_only_per_item_cost_in_the_cascade() -> None:
    """If a second metered source is ever added, this test should fail so
    whoever adds it has to place it in the tier scheme deliberately."""
    assert [n for n in _default_names() if n.startswith("openalex")] == [
        "openalex_content", "openalex",
    ]


def test_both_openalex_sources_are_selectable_by_name() -> None:
    """`--sources openalex` must not silently pull in the paid tier."""
    free_only = [
        s.name for s in fetchers.pdf_sources(MagicMock(), None, names=["openalex"])
    ]
    assert free_only == ["openalex"]
    paid_only = [
        s.name
        for s in fetchers.pdf_sources(MagicMock(), None, names=["openalex_content"])
    ]
    assert paid_only == ["openalex_content"]


# ---------------------------------------------------------------------
# Opt-in coercion
# ---------------------------------------------------------------------

@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (True, True), (False, False),
        ("true", True), ("yes", True), ("on", True), ("1", True),
        ("false", False), ("no", False), ("off", False), ("0", False),
        ("TRUE", True), ("  false  ", False),
    ],
)
def test_coerce_paid_opt_in_recognised_values(raw, expected) -> None:
    assert coerce_paid_opt_in(raw) is expected


@pytest.mark.parametrize("raw", [None, "", "   ", "maybe", "1.0"])
def test_coerce_paid_opt_in_returns_default_when_undecidable(raw) -> None:
    """Unset or unrecognised must not accidentally read as off — the
    caller's default decides, and a typo cannot silently stop the tier."""
    assert coerce_paid_opt_in(raw) is None
    assert coerce_paid_opt_in(raw, default=True) is True
    assert coerce_paid_opt_in(raw, default=False) is False


# ---------------------------------------------------------------------
# Gate behaviour on the fetchers
# ---------------------------------------------------------------------

def _paid_source(monkeypatch, *, api_key="key", opt_in=None):
    cfg = MagicMock()
    cfg.openalex_api_key = api_key
    cfg.crossref_mailto = ""
    # MagicMock invents attributes, so an "absent" setting has to be set
    # to None explicitly for the tri-state to see it as unanswered.
    cfg.openalex_use_paid_content_api = opt_in
    src = OpenAlexContentSource(MagicMock(), config=cfg)
    src._ensure_configured = lambda: None
    fake_pyalex = MagicMock()
    fake_pyalex.Works.return_value = {
        f"doi:{DOI}": {
            "id": "https://openalex.org/W1", "has_content": {"pdf": True},
        },
    }
    monkeypatch.setitem(__import__("sys").modules, "pyalex", fake_pyalex)
    return src


def test_explicit_opt_out_makes_the_paid_source_inert(monkeypatch, tmp_path) -> None:
    src = _paid_source(monkeypatch, opt_in=False)
    assert src.fetch_pdf(DOI, cache_dir=str(tmp_path)) is None
    src.http.get.assert_not_called()


def test_string_false_from_toml_or_env_also_opts_out(monkeypatch, tmp_path) -> None:
    """The bug a plain `bool()` cast would reintroduce."""
    src = _paid_source(monkeypatch, opt_in="false")
    assert src.fetch_pdf(DOI, cache_dir=str(tmp_path)) is None
    src.http.get.assert_not_called()


def test_absent_opt_in_keeps_the_paid_tier_enabled(monkeypatch, tmp_path) -> None:
    """Backward compatibility: an existing config has no flag, and must
    not lose a tier that works today."""
    monkeypatch.delenv("OPENALEX_USE_PAID_CONTENT_API", raising=False)
    src = _paid_source(monkeypatch, opt_in=None)
    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"%PDF-1.4\n" + b"0" * 3000 + b"\nstartxref\n12\n%%EOF\n"
    resp.headers = {}
    src.http.get.return_value = resp
    assert src.fetch_pdf(DOI, cache_dir=str(tmp_path)) is not None


def test_env_var_can_opt_out_when_config_is_silent(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("OPENALEX_USE_PAID_CONTENT_API", "off")
    src = _paid_source(monkeypatch, opt_in=None)
    assert src.fetch_pdf(DOI, cache_dir=str(tmp_path)) is None
    src.http.get.assert_not_called()


def test_no_api_key_makes_the_paid_source_inert(monkeypatch, tmp_path) -> None:
    """The gate is about intent; capability is checked separately."""
    monkeypatch.delenv("OPENALEX_API_KEY", raising=False)
    src = _paid_source(monkeypatch, api_key="", opt_in=True)
    assert src.fetch_pdf(DOI, cache_dir=str(tmp_path)) is None
    src.http.get.assert_not_called()


def test_free_tier_never_touches_the_paid_endpoint(monkeypatch, tmp_path) -> None:
    """`openalex` must be free even with a key configured and the paid
    tier switched on — that is the whole point of the split."""
    cfg = MagicMock()
    cfg.openalex_api_key = "key"
    cfg.crossref_mailto = ""
    cfg.openalex_use_paid_content_api = True
    src = OpenAlexSource(MagicMock(), config=cfg)
    src._ensure_configured = lambda: None

    resp = MagicMock()
    resp.status_code = 200
    resp.content = b"%PDF-1.4\n" + b"0" * 3000 + b"\nstartxref\n12\n%%EOF\n"
    resp.headers = {}
    src.http.get.return_value = resp

    fake_pyalex = MagicMock()
    fake_pyalex.Works.return_value = {
        f"doi:{DOI}": {
            "id": "https://openalex.org/W1",
            "has_content": {"pdf": True},
            "open_access": {"oa_url": "https://repo.example.org/paper.pdf"},
        },
    }
    monkeypatch.setitem(__import__("sys").modules, "pyalex", fake_pyalex)

    result = src.fetch_pdf(DOI, cache_dir=str(tmp_path))
    assert result is not None
    assert result[1] == "https://repo.example.org/paper.pdf"
    for call in src.http.get.call_args_list:
        assert "content.openalex.org" not in str(call), (
            "the free tier billed the Content API"
        )
