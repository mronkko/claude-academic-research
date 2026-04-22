"""Unit tests for scripts/pipelines/enrich_dois.py.

Covers:
- DOI normalisation (strip prefixes, detect malformed).
- Year/author matching helpers.
- Validate flow (ok / title_mismatch / not_in_crossref / malformed-fixed).
- Find-missing flow (3/3 apply, 2/3 prompt accept, 2/3 prompt decline,
  ambiguous, not_found, dry-run, non-TTY).
- Invalid-DOI replacement (with and without --replace-invalid).

Crossref is always mocked — no live api.crossref.org traffic.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

ENRICH = Path(__file__).resolve().parents[2] / "scripts" / "pipelines" / "enrich_dois.py"


def _load():
    """Load enrich_dois as a module without invoking PEP 723.

    The script's imports (fetchers.*, zotero_io, etc.) are already on
    sys.path thanks to conftest.py.
    """
    spec = importlib.util.spec_from_file_location("enrich_dois", ENRICH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["enrich_dois"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# _normalise_doi
# ---------------------------------------------------------------------------


def test_normalise_doi_strips_https_doi_org_prefix() -> None:
    mod = _load()
    clean, malformed = mod._normalise_doi("https://doi.org/10.1/x")
    assert clean == "10.1/x"
    assert malformed is True


def test_normalise_doi_strips_http_dx_doi_org() -> None:
    mod = _load()
    clean, malformed = mod._normalise_doi("http://dx.doi.org/10.1/x")
    assert clean == "10.1/x"
    assert malformed is True


def test_normalise_doi_strips_doi_prefix_case_insensitive() -> None:
    mod = _load()
    assert mod._normalise_doi("doi:10.1/x")[0] == "10.1/x"
    assert mod._normalise_doi("DOI:10.1/x")[0] == "10.1/x"


def test_normalise_doi_detects_whitespace() -> None:
    mod = _load()
    clean, malformed = mod._normalise_doi("  10.1/x  ")
    assert clean == "10.1/x"
    assert malformed is True


def test_normalise_doi_clean_input_is_not_malformed() -> None:
    mod = _load()
    clean, malformed = mod._normalise_doi("10.1/x")
    assert clean == "10.1/x"
    assert malformed is False


# ---------------------------------------------------------------------------
# _year_matches
# ---------------------------------------------------------------------------


def test_year_matches_exact() -> None:
    mod = _load()
    assert mod._year_matches("2014", "2014") is True


def test_year_matches_plus_minus_one() -> None:
    mod = _load()
    assert mod._year_matches("2014", "2013") is True
    assert mod._year_matches("2014", "2015") is True


def test_year_matches_rejects_larger_gap() -> None:
    mod = _load()
    assert mod._year_matches("2014", "2010") is False


def test_year_matches_rejects_empty() -> None:
    mod = _load()
    assert mod._year_matches("", "2014") is False
    assert mod._year_matches("2014", "") is False


# ---------------------------------------------------------------------------
# _first_author_matches
# ---------------------------------------------------------------------------


def _item_with_authors(authors: list[tuple[str, str]], **fields) -> dict:
    creators = [
        {"creatorType": "author", "lastName": last, "firstName": first}
        for (last, first) in authors
    ]
    return {
        "key": fields.get("key", "K1"),
        "version": fields.get("version", 1),
        "data": {
            "creators": creators,
            "title": fields.get("title", ""),
            "date": fields.get("date", ""),
            "DOI": fields.get("DOI", ""),
        },
    }


def test_first_author_matches_case_insensitive() -> None:
    mod = _load()
    item = _item_with_authors([("Orlikowski", "Wanda J.")])
    assert mod._first_author_matches(item, ["orlikowski"]) is True
    assert mod._first_author_matches(item, ["ORLIKOWSKI"]) is True


def test_first_author_matches_only_first_author() -> None:
    """Only the first author is compared. Crossref's order is used."""
    mod = _load()
    item = _item_with_authors([("Smith", "A."), ("Orlikowski", "W.")])
    # Crossref's first = "Smith" → matches Zotero's first.
    assert mod._first_author_matches(item, ["Smith", "Other"]) is True
    # Crossref's first = "Orlikowski" → Zotero's first is "Smith" → no match.
    assert mod._first_author_matches(item, ["Orlikowski", "Smith"]) is False


def test_first_author_matches_false_when_no_creators() -> None:
    mod = _load()
    item = _item_with_authors([])
    assert mod._first_author_matches(item, ["Smith"]) is False


def test_first_author_matches_false_when_crossref_surnames_empty() -> None:
    mod = _load()
    item = _item_with_authors([("Smith", "A.")])
    assert mod._first_author_matches(item, []) is False


# ---------------------------------------------------------------------------
# _zot_year — extracts from Zotero's `date` field
# ---------------------------------------------------------------------------


def test_zot_year_handles_common_formats() -> None:
    mod = _load()
    assert mod._zot_year({"data": {"date": "2014"}}) == "2014"
    assert mod._zot_year({"data": {"date": "2014-06"}}) == "2014"
    assert mod._zot_year({"data": {"date": "2014-06-15"}}) == "2014"
    assert mod._zot_year({"data": {"date": "June 2014"}}) == "2014"
    assert mod._zot_year({"data": {"date": ""}}) == ""
    assert mod._zot_year({"data": {}}) == ""


# ---------------------------------------------------------------------------
# _validate_doi
# ---------------------------------------------------------------------------


def _crossref_returning(message: dict | None) -> MagicMock:
    cr = MagicMock()
    if message is None:
        cr.works.return_value = None
    else:
        cr.works.return_value = {"status": "ok", "message": message}
    return cr


def test_validate_doi_ok_on_matching_title() -> None:
    mod = _load()
    item = _item_with_authors(
        [("Smith", "A.")],
        title="A Study of Things",
        DOI="10.1/x",
        date="2014",
    )
    cr = _crossref_returning({
        "URL": "https://example/x",
        "title": ["A Study of Things"],
        "author": [{"family": "Smith"}],
        "issued": {"date-parts": [[2014]]},
    })
    cache = MagicMock()
    cache.get.return_value = None
    cache.put.return_value = None
    zot = MagicMock()

    result = mod._validate_doi(
        item, cr, cache,
        fix_malformed=False, dry_run=False, zot=zot,
    )
    assert result.status == "validate_ok"
    assert result.crossref_title == "A Study of Things"


def test_validate_doi_title_mismatch() -> None:
    mod = _load()
    item = _item_with_authors(
        [], title="A Study of Things", DOI="10.1/x",
    )
    cr = _crossref_returning({
        "URL": "https://example/x",
        "title": ["An Entirely Different Paper About Nothing"],
    })
    cache = MagicMock()
    cache.get.return_value = None
    zot = MagicMock()

    result = mod._validate_doi(
        item, cr, cache,
        fix_malformed=False, dry_run=False, zot=zot,
    )
    assert result.status == "validate_title_mismatch"


def test_validate_doi_not_in_crossref_when_empty_title() -> None:
    """Crossref returned a resolution but it has no title to compare
    → same effective outcome as 'not found'."""
    mod = _load()
    item = _item_with_authors([], title="Anything", DOI="10.2307/2640412")
    cr = _crossref_returning({"publisher": "Some Publisher"})   # no title
    cache = MagicMock()
    cache.get.return_value = None
    zot = MagicMock()

    result = mod._validate_doi(
        item, cr, cache,
        fix_malformed=False, dry_run=False, zot=zot,
    )
    assert result.status == "validate_not_in_crossref"


def test_validate_doi_not_in_crossref_when_resolve_returns_none() -> None:
    """resolve_doi catches Crossref's own errors (including 404 on
    a broken DOI) and returns None. We route that to
    `validate_not_in_crossref` so --replace-invalid kicks in and
    find-missing gets a chance to rescue the item. A transient
    network error here is indistinguishable, but the downstream
    search call would also fail cleanly."""
    mod = _load()
    item = _item_with_authors([], title="Anything", DOI="10.2307/2640412")
    cr = MagicMock()
    cr.works.side_effect = RuntimeError("not found")
    cache = MagicMock()
    cache.get.return_value = None
    zot = MagicMock()

    result = mod._validate_doi(
        item, cr, cache,
        fix_malformed=False, dry_run=False, zot=zot,
    )
    assert result.status == "validate_not_in_crossref"


def test_validate_doi_skipped_no_zotero_title() -> None:
    """Can't compare without a Zotero title."""
    mod = _load()
    item = _item_with_authors([], title="", DOI="10.1/x")
    cr = _crossref_returning({"title": ["Crossref has this title"]})
    cache = MagicMock()
    cache.get.return_value = None
    zot = MagicMock()

    result = mod._validate_doi(
        item, cr, cache,
        fix_malformed=False, dry_run=False, zot=zot,
    )
    assert result.status == "validate_skipped_no_zotero_title"


def test_validate_doi_fix_malformed_writes_clean_form() -> None:
    mod = _load()
    item = _item_with_authors(
        [], title="Paper", DOI="https://doi.org/10.1/x",
    )
    cr = _crossref_returning({"title": ["Paper"]})
    cache = MagicMock()
    cache.get.return_value = None
    zot = MagicMock()

    result = mod._validate_doi(
        item, cr, cache,
        fix_malformed=True, dry_run=False, zot=zot,
    )
    assert result.status == "validate_malformed_doi_fixed"
    zot.update_item.assert_called_once()
    payload = zot.update_item.call_args[0][0]
    assert payload["DOI"] == "10.1/x"


def test_validate_doi_fix_malformed_respects_dry_run() -> None:
    mod = _load()
    item = _item_with_authors(
        [], title="Paper", DOI="https://doi.org/10.1/x",
    )
    cr = _crossref_returning({"title": ["Paper"]})
    cache = MagicMock()
    cache.get.return_value = None
    zot = MagicMock()

    result = mod._validate_doi(
        item, cr, cache,
        fix_malformed=True, dry_run=True, zot=zot,
    )
    # Still classified as ok (malformed but dry-run means no fix was
    # applied, so we report the plain validate_ok status).
    assert result.status == "validate_ok"
    zot.update_item.assert_not_called()


# ---------------------------------------------------------------------------
# _find_missing_doi
# ---------------------------------------------------------------------------


def _search_returning(items: list[dict]) -> MagicMock:
    cr = MagicMock()
    cr.works.return_value = {"status": "ok", "message": {"items": items}}
    return cr


def _crossref_item(doi: str, title: str, surnames: list[str],
                   year: int | None) -> dict:
    msg: dict = {"DOI": doi, "title": [title]}
    msg["author"] = [{"family": s} for s in surnames]
    if year is not None:
        msg["issued"] = {"date-parts": [[year]]}
    return msg


def test_find_missing_3_of_3_auto_applies() -> None:
    mod = _load()
    item = _item_with_authors(
        [("Smith", "A.")], title="A Study of Things", date="2014",
    )
    cr = _search_returning([
        _crossref_item("10.1/new", "A Study of Things", ["Smith"], 2014),
    ])
    zot = MagicMock()

    result = mod._find_missing_doi(
        item, cr, zot,
        is_replacement=False, replace_invalid=False,
        dry_run=False, no_prompt=False,
    )
    assert result.status == "applied_high_confidence"
    zot.update_item.assert_called_once()
    assert zot.update_item.call_args[0][0]["DOI"] == "10.1/new"


def test_find_missing_3_of_3_dry_run_does_not_write() -> None:
    mod = _load()
    item = _item_with_authors(
        [("Smith", "A.")], title="A Study of Things", date="2014",
    )
    cr = _search_returning([
        _crossref_item("10.1/new", "A Study of Things", ["Smith"], 2014),
    ])
    zot = MagicMock()

    result = mod._find_missing_doi(
        item, cr, zot,
        is_replacement=False, replace_invalid=False,
        dry_run=True, no_prompt=False,
    )
    assert result.status == "applied_high_confidence_dry_run"
    zot.update_item.assert_not_called()


def test_find_missing_3_of_3_on_invalid_without_replace_flag_proposes_only() -> None:
    """Invalid DOI + 3/3 match + --replace-invalid off → just propose."""
    mod = _load()
    item = _item_with_authors(
        [("Smith", "A.")], title="A Study of Things", date="2014",
        DOI="10.2307/invalid",
    )
    cr = _search_returning([
        _crossref_item("10.1/new", "A Study of Things", ["Smith"], 2014),
    ])
    zot = MagicMock()

    result = mod._find_missing_doi(
        item, cr, zot,
        is_replacement=True, replace_invalid=False,
        dry_run=False, no_prompt=False,
    )
    assert result.status == "proposed_replacement"
    zot.update_item.assert_not_called()


def test_find_missing_3_of_3_on_invalid_with_replace_flag_overwrites() -> None:
    mod = _load()
    item = _item_with_authors(
        [("Smith", "A.")], title="A Study of Things", date="2014",
        DOI="10.2307/invalid",
    )
    cr = _search_returning([
        _crossref_item("10.1/new", "A Study of Things", ["Smith"], 2014),
    ])
    zot = MagicMock()

    result = mod._find_missing_doi(
        item, cr, zot,
        is_replacement=True, replace_invalid=True,
        dry_run=False, no_prompt=False,
    )
    assert result.status == "replaced_high_confidence"
    zot.update_item.assert_called_once()


def test_find_missing_2_of_3_non_tty_proposes_not_applied() -> None:
    mod = _load()
    item = _item_with_authors(
        [("Smith", "A.")], title="A Study of Things", date="2014",
    )
    cr = _search_returning([
        _crossref_item(
            "10.1/new", "A Study of Things", ["DifferentAuthor"], 2014,
        ),
    ])
    zot = MagicMock()

    # no_prompt=True simulates non-TTY / explicit flag.
    result = mod._find_missing_doi(
        item, cr, zot,
        is_replacement=False, replace_invalid=False,
        dry_run=False, no_prompt=True,
    )
    assert result.status == "proposed_not_applied"
    zot.update_item.assert_not_called()


def test_find_missing_2_of_3_prompt_accept_applies() -> None:
    mod = _load()
    item = _item_with_authors(
        [("Smith", "A.")], title="A Study of Things", date="2014",
    )
    cr = _search_returning([
        _crossref_item(
            "10.1/new", "A Study of Things", ["DifferentAuthor"], 2014,
        ),
    ])
    zot = MagicMock()

    with patch("sys.stdin") as stdin_mock, \
         patch.object(mod, "_prompt_confirm", return_value=True):
        stdin_mock.isatty.return_value = True
        result = mod._find_missing_doi(
            item, cr, zot,
            is_replacement=False, replace_invalid=False,
            dry_run=False, no_prompt=False,
        )
    assert result.status == "applied_after_prompt"
    zot.update_item.assert_called_once()


def test_find_missing_2_of_3_prompt_decline_skips() -> None:
    mod = _load()
    item = _item_with_authors(
        [("Smith", "A.")], title="A Study of Things", date="2014",
    )
    cr = _search_returning([
        _crossref_item(
            "10.1/new", "A Study of Things", ["Different"], 2014,
        ),
    ])
    zot = MagicMock()

    with patch("sys.stdin") as stdin_mock, \
         patch.object(mod, "_prompt_confirm", return_value=False):
        stdin_mock.isatty.return_value = True
        result = mod._find_missing_doi(
            item, cr, zot,
            is_replacement=False, replace_invalid=False,
            dry_run=False, no_prompt=False,
        )
    assert result.status == "proposed_skipped_by_user"
    zot.update_item.assert_not_called()


def test_find_missing_no_candidates_not_found() -> None:
    mod = _load()
    item = _item_with_authors(
        [("Smith", "A.")], title="A Study of Things", date="2014",
    )
    cr = _search_returning([])
    zot = MagicMock()

    result = mod._find_missing_doi(
        item, cr, zot,
        is_replacement=False, replace_invalid=False,
        dry_run=False, no_prompt=False,
    )
    assert result.status == "not_found_in_crossref"
    zot.update_item.assert_not_called()


def test_find_missing_zero_score_ambiguous_when_multiple_candidates() -> None:
    mod = _load()
    item = _item_with_authors(
        [("Smith", "A.")], title="A Study of Things", date="2014",
    )
    # 3 candidates, none matching on any criterion.
    cr = _search_returning([
        _crossref_item("10.1/a", "Unrelated Paper One", ["Other"], 1990),
        _crossref_item("10.1/b", "Unrelated Paper Two", ["Another"], 1995),
        _crossref_item("10.1/c", "Unrelated Paper Three", ["Third"], 2000),
    ])
    zot = MagicMock()

    result = mod._find_missing_doi(
        item, cr, zot,
        is_replacement=False, replace_invalid=False,
        dry_run=False, no_prompt=True,
    )
    assert result.status == "ambiguous_no_clear_match"
    zot.update_item.assert_not_called()


def test_find_missing_skipped_no_title() -> None:
    mod = _load()
    item = _item_with_authors([("Smith", "A.")], title="", date="2014")
    cr = MagicMock()
    zot = MagicMock()

    result = mod._find_missing_doi(
        item, cr, zot,
        is_replacement=False, replace_invalid=False,
        dry_run=False, no_prompt=False,
    )
    assert result.status == "skipped_no_title"
    cr.works.assert_not_called()


def test_find_missing_year_tolerance() -> None:
    """±1 year mismatch should not block auto-apply."""
    mod = _load()
    item = _item_with_authors(
        [("Smith", "A.")], title="A Study of Things", date="2014",
    )
    cr = _search_returning([
        _crossref_item(
            "10.1/new", "A Study of Things", ["Smith"], 2015,
        ),
    ])
    zot = MagicMock()

    result = mod._find_missing_doi(
        item, cr, zot,
        is_replacement=False, replace_invalid=False,
        dry_run=False, no_prompt=False,
    )
    assert result.status == "applied_high_confidence"
