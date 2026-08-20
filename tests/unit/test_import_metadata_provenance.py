"""Where an imported item's metadata comes from, and what it costs.

A live import produced items like "Vanacker Tom" — a single-field
creator name, no volume, no issue, no pages — because the searchers
dropped everything the databases told them and `_parse_authors` was
handed display-order names it could only guess at. Meanwhile the import
spent one Crossref request per DOI to learn a single thing: the item
type.

The rule now: a row that carries splittable creators and a known type
is mapped offline; anything else is rebuilt from the DOI by Crossref
via `zotero_mcp.citation_import.csl_json_to_zotero`. An item is wholly
one or wholly the other, never a blend — with the plugin's own layers
(tags, canonicalization, collection, abstract) applied to both.

The CSL fixtures below are real Crossref transform responses, trimmed.
Their awkward parts are the point: Crossref calls the type
`journal-article` where CSL says `article-journal`, and returns `ISSN`
as an array where the converter expects a string.
"""

from __future__ import annotations

import import_to_zotero as imp
import pytest
from zotero_mcp.citation_import import CSL_TYPE_MAP

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

#: Crossref's CSL for 10.1016/j.jbusvent.2019.105970, trimmed to the
#: mapped fields plus a sample of the bookkeeping Crossref sends along.
ARTICLE_CSL = {
    "type": "journal-article",
    "DOI": "10.1016/j.jbusvent.2019.105970",
    "title": "Using fuzzy-set qualitative comparative analysis",
    "container-title": "Journal of Business Venturing",
    "ISSN": ["0883-9026"],
    "volume": "35",
    "issue": "1",
    "page": "105970",
    "publisher": "Elsevier BV",
    "language": "en",
    "issued": {"date-parts": [[2020, 1]]},
    "author": [
        {"given": "Evan J.", "family": "Douglas", "sequence": "first"},
        {"given": "Dean A.", "family": "Shepherd", "sequence": "additional"},
        {"given": "Catherine", "family": "Prentice", "sequence": "additional"},
    ],
    # Crossref bookkeeping — none of this belongs in a Zotero item.
    "id": "10.1016/j.jbusvent.2019.105970",
    "indexed": {"date-parts": [[2026, 8, 19]], "timestamp": 1787165564438},
    "reference-count": 108,
    "member": "78",
    "score": 1.0,
    "reference": [{"key": "ref1", "DOI": "10.1/a"}] * 3,
    "abstract": "<jats:p>JATS-wrapped text.</jats:p>",
}

CHAPTER_CSL = {
    "type": "book-chapter",
    "DOI": "10.5876/9781607320395.c008",
    "title": "5 From Shacks to Shanties",
    "container-title": "The Archaeology of Class War",
    "ISBN": ["9781607320395"],
    "page": "161-185",
    "publisher": "University of Colorado Press",
    "issued": {"date-parts": [[2009, 11, 15]]},
    "author": [{"given": "Sarah J.", "family": "Chicone"}],
}

#: A row as the searchers now emit it: Scopus/WoS shape.
COMPLETE_ROW = {
    "db": "scopus",
    "query": "block_a",
    "doi": "10.1016/j.jbusvent.2019.105970",
    "title": "Using fuzzy-set qualitative comparative analysis",
    "authors": "Douglas, Evan J.; Shepherd, Dean A.; Prentice, Catherine",
    "year": "2020",
    "source": "Journal of Business Venturing",
    "issn": "08839026",
    "volume": "35",
    "issue": "1",
    "pages": "105970",
    "type": "journal-article",
    "abstract": "The search database's abstract.",
}

#: The OpenAlex / Semantic Scholar shape: display-order names, no split
#: available anywhere in the response.
DISPLAY_ORDER_ROW = {
    **COMPLETE_ROW,
    "db": "openalex",
    "authors": "Evan J. Douglas; Dean A. Shepherd; Catherine Prentice",
}


class _ExplodingSession:
    """Any HTTP use at all is a test failure."""

    def get(self, *a, **kw):  # pragma: no cover — the failure is the point
        raise AssertionError("the source-built path made an HTTP request")


@pytest.fixture
def crossref(monkeypatch):
    """Serve recorded CSL, and record which DOIs were asked for."""
    asked: list[str] = []

    def _serve(records: dict, *, fail: bool = False):
        def fake_get_json(session, url, **kw):
            doi = url.split("/works/", 1)[1].split("/transform")[0]
            asked.append(doi)
            if fail:
                return None
            return records.get(doi)

        monkeypatch.setattr(imp.http_client, "get_json", fake_get_json)
        return asked

    _serve.asked = asked  # type: ignore[attr-defined]
    return _serve


# ---------------------------------------------------------------------------
# Routing
# ---------------------------------------------------------------------------


def test_complete_row_is_built_offline() -> None:
    """The normal path: Scopus and WoS already said everything needed."""
    item, how = imp.build_item(
        COMPLETE_ROW, None, session=_ExplodingSession(), csl_cache={},
    )
    assert how == imp.BUILD_SOURCE
    assert item["itemType"] == "journalArticle"
    assert item["volume"] == "35"
    assert item["issue"] == "1"
    assert item["pages"] == "105970"
    assert item["creators"][0] == {
        "creatorType": "author", "firstName": "Evan J.", "lastName": "Douglas",
    }


def test_display_order_row_is_filled_from_the_doi(crossref) -> None:
    asked = crossref({ARTICLE_CSL["DOI"]: ARTICLE_CSL})
    item, how = imp.build_item(
        DISPLAY_ORDER_ROW, None, session=object(), csl_cache={},
    )
    assert how == imp.BUILD_AUTHORITY
    assert asked == [ARTICLE_CSL["DOI"]]
    assert item["creators"] == [
        {"creatorType": "author", "firstName": "Evan J.", "lastName": "Douglas"},
        {"creatorType": "author", "firstName": "Dean A.", "lastName": "Shepherd"},
        {"creatorType": "author", "firstName": "Catherine",
         "lastName": "Prentice"},
    ]
    assert (item["volume"], item["issue"], item["pages"]) == ("35", "1", "105970")


def test_a_row_without_a_type_is_filled_even_with_good_creators(crossref) -> None:
    """An older CSV has no `type` column at all. Guessing
    `journalArticle` is what mistyped book chapters in the first place."""
    crossref({CHAPTER_CSL["DOI"]: CHAPTER_CSL})
    row = {**COMPLETE_ROW, "doi": CHAPTER_CSL["DOI"], "type": "",
           "authors": "Chicone, Sarah J."}
    item, how = imp.build_item(row, None, session=object(), csl_cache={})
    assert how == imp.BUILD_AUTHORITY
    assert item["itemType"] == "bookSection"
    assert item["bookTitle"] == "The Archaeology of Class War"


def test_one_fetch_per_distinct_doi(crossref) -> None:
    """A merged Scopus+WoS batch sees most DOIs twice."""
    asked = crossref({ARTICLE_CSL["DOI"]: ARTICLE_CSL})
    cache: dict = {}
    for _ in range(4):
        imp.build_item(DISPLAY_ORDER_ROW, None, session=object(),
                       csl_cache=cache)
    assert len(asked) == 1, "Crossref was asked more than once for one DOI"


def test_a_dead_doi_is_not_retried_per_row(crossref) -> None:
    asked = crossref({}, fail=True)
    cache: dict = {}
    for _ in range(3):
        imp.build_item(DISPLAY_ORDER_ROW, None, session=object(),
                       csl_cache=cache)
    assert len(asked) == 1


# ---------------------------------------------------------------------------
# Fallback
# ---------------------------------------------------------------------------


def test_unreachable_crossref_falls_back_to_the_row(crossref) -> None:
    crossref({}, fail=True)
    item, how = imp.build_item(
        DISPLAY_ORDER_ROW, None, session=object(), csl_cache={},
    )
    assert how == imp.BUILD_FALLBACK
    assert item["itemType"] == "journalArticle"
    # The heuristic split — wrong for some names, far better than the
    # single-field blob it replaced.
    assert item["creators"][0]["lastName"] == "Douglas"
    assert item["volume"] == "35"


def test_a_conversion_failure_warns_and_falls_back(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        imp.http_client, "get_json", lambda *a, **kw: ARTICLE_CSL,
    )

    def boom(*a, **kw):
        raise ValueError("converter exploded")

    monkeypatch.setattr(imp, "csl_json_to_zotero", boom)
    item, how = imp.build_item(
        DISPLAY_ORDER_ROW, None, session=object(), csl_cache={},
    )
    assert how == imp.BUILD_FALLBACK
    assert item["title"] == DISPLAY_ORDER_ROW["title"]
    assert "WARNING" in capsys.readouterr().out


def test_a_row_without_a_doi_uses_the_row(crossref) -> None:
    """Nothing to look the record up by — by definition the row path."""
    asked = crossref({ARTICLE_CSL["DOI"]: ARTICLE_CSL})
    row = {**DISPLAY_ORDER_ROW, "doi": ""}
    item, how = imp.build_item(row, None, session=object(), csl_cache={})
    assert how == imp.BUILD_FALLBACK
    assert asked == []
    assert item["title"] == row["title"]


def test_no_session_means_no_fetch() -> None:
    """`build_item` without a session (a caller that must stay offline)
    degrades to the row rather than raising."""
    item, how = imp.build_item(DISPLAY_ORDER_ROW, None, session=None)
    assert how == imp.BUILD_FALLBACK
    assert item["title"] == DISPLAY_ORDER_ROW["title"]


# ---------------------------------------------------------------------------
# Plugin layers — identical on both paths
# ---------------------------------------------------------------------------


def test_source_built_item_carries_the_plugin_layers() -> None:
    item, _ = imp.build_item(
        COMPLETE_ROW, "COLL1234", session=_ExplodingSession(), csl_cache={},
    )
    assert item["collections"] == ["COLL1234"]
    assert {t["tag"] for t in item["tags"]} == {"search:block_a"}
    assert item["ISSN"] == "0883-9026"          # canonicalized from 08839026
    assert item["abstractNote"] == "The search database's abstract."


def test_authority_filled_item_carries_the_same_layers(crossref) -> None:
    crossref({ARTICLE_CSL["DOI"]: ARTICLE_CSL})
    item, how = imp.build_item(
        DISPLAY_ORDER_ROW, "COLL1234", session=object(), csl_cache={},
    )
    assert how == imp.BUILD_AUTHORITY
    assert item["collections"] == ["COLL1234"]
    assert {t["tag"] for t in item["tags"]} == {"search:block_a"}
    assert item["ISSN"] == "0883-9026"
    assert item["abstractNote"] == "The search database's abstract."


def test_the_search_abstract_beats_crossrefs(crossref) -> None:
    """Crossref deposits abstracts sparsely and wraps them in JATS. The
    search database's plain-text one is what the screener should read —
    losing it to a fill would be a downgrade, not an enrichment."""
    crossref({ARTICLE_CSL["DOI"]: ARTICLE_CSL})
    item, _ = imp.build_item(
        DISPLAY_ORDER_ROW, None, session=object(), csl_cache={},
    )
    assert item["abstractNote"] == "The search database's abstract."
    assert "jats" not in item.get("extra", "").lower()


def test_an_empty_row_abstract_does_not_import_crossref_markup(crossref) -> None:
    """Left for `enrich_abstracts.py`, which strips JATS and has better
    sources to try first."""
    crossref({ARTICLE_CSL["DOI"]: ARTICLE_CSL})
    row = {**DISPLAY_ORDER_ROW, "abstract": ""}
    item, _ = imp.build_item(row, None, session=object(), csl_cache={})
    assert item.get("abstractNote", "") == ""


def test_journal_name_is_canonicalized_on_the_source_path() -> None:
    row = {**COMPLETE_ROW, "source": "Strat Manag J", "issn": "0143-2095"}
    item, _ = imp.build_item(
        row, None, session=_ExplodingSession(), csl_cache={},
    )
    assert item["publicationTitle"] == "Strategic Management Journal"


def test_a_book_title_is_not_rewritten_by_an_issn_match() -> None:
    """The ISSN→name alias table answers "which journal is this?". A
    book chapter's container is not a journal, and an ISSN that rode
    along on the row would otherwise replace the book's title with a
    journal's."""
    row = {**COMPLETE_ROW, "type": "book-chapter",
           "source": "Some Edited Volume", "issn": "0143-2095"}
    item, how = imp.build_item(
        row, None, session=_ExplodingSession(), csl_cache={},
    )
    assert how == imp.BUILD_SOURCE
    assert item["bookTitle"] == "Some Edited Volume"


def test_journal_name_is_canonicalized_on_the_authority_path(crossref) -> None:
    crossref({"10.1002/smj.1": {**ARTICLE_CSL, "DOI": "10.1002/smj.1",
                                "container-title": "Strat Manag J",
                                "ISSN": ["0143-2095"]}})
    row = {**DISPLAY_ORDER_ROW, "doi": "10.1002/smj.1",
           "source": "Strat Manag J", "issn": "0143-2095"}
    item, how = imp.build_item(row, None, session=object(), csl_cache={})
    assert how == imp.BUILD_AUTHORITY
    assert item["publicationTitle"] == "Strategic Management Journal"


# ---------------------------------------------------------------------------
# The Crossref → converter adapter
# ---------------------------------------------------------------------------


def test_issn_array_is_flattened() -> None:
    """Crossref's `["0883-9026"]` reaches a converter that calls
    `.strip()` on it. Unadapted, that AttributeError fails the fill for
    essentially every journal article."""
    assert imp._csl_for_converter(ARTICLE_CSL)["ISSN"] == "0883-9026"


def test_crossref_type_names_are_translated_to_csl() -> None:
    assert imp._csl_for_converter(ARTICLE_CSL)["type"] == "article-journal"
    assert imp._csl_for_converter(CHAPTER_CSL)["type"] == "chapter"


def test_bookkeeping_fields_are_dropped() -> None:
    """The converter preserves unmapped fields in `extra`; a Crossref
    payload's unmapped fields include a 108-entry reference list."""
    adapted = imp._csl_for_converter(ARTICLE_CSL)
    for noise in ("indexed", "reference", "reference-count", "member",
                  "score", "id", "abstract"):
        assert noise not in adapted


def test_extra_stays_clean_through_the_whole_conversion(crossref) -> None:
    crossref({ARTICLE_CSL["DOI"]: ARTICLE_CSL})
    item, _ = imp.build_item(
        DISPLAY_ORDER_ROW, None, session=object(), csl_cache={},
    )
    assert item.get("extra", "") == ""


def test_an_unknown_type_is_left_for_the_converter_to_default() -> None:
    adapted = imp._csl_for_converter({"type": "component"})
    assert adapted["type"] == "component"


def test_crossref_and_csl_type_tables_agree() -> None:
    """The seam with zotero-mcp: routing a type through the converter
    must land where this module's own table says it should. If upstream
    changes `CSL_TYPE_MAP`, this fails here rather than silently
    mistyping a corpus."""
    for crossref_type, zotero_type in imp._CROSSREF_TYPE_TO_ZOTERO.items():
        csl_type = imp._CROSSREF_TYPE_TO_CSL.get(crossref_type)
        assert csl_type is not None, f"{crossref_type} has no CSL spelling"
        assert CSL_TYPE_MAP.get(csl_type) == zotero_type, (
            f"{crossref_type} → {csl_type} → "
            f"{CSL_TYPE_MAP.get(csl_type)}, expected {zotero_type}"
        )


def test_the_item_template_needs_no_network() -> None:
    """`valid_fields` answers from an on-disk cache, so `--dry-run`
    previews the real item shape offline."""
    template = imp._item_template("journalArticle")
    assert template["itemType"] == "journalArticle"
    assert template["creators"] == []
    assert "volume" in template and "publicationTitle" in template


def test_the_item_template_rejects_an_unknown_type() -> None:
    with pytest.raises(KeyError):
        imp._item_template("notAnItemType")


# ---------------------------------------------------------------------------
# What --dry-run shows
# ---------------------------------------------------------------------------


def test_dry_run_preview_shows_the_creators(capsys) -> None:
    """Counts cannot answer the question a dry run is asked. A blob
    creator and a split one are both "1 item to create"."""
    item, _ = imp.build_item(
        COMPLETE_ROW, None, session=_ExplodingSession(), csl_cache={},
    )
    imp._print_dry_run_preview({imp.BUILD_SOURCE: item})
    out = capsys.readouterr().out
    assert "Douglas, Evan J." in out
    assert "35 / 1 / 105970" in out
    assert "Journal of Business Venturing" in out


def test_dry_run_preview_marks_a_single_field_creator(capsys) -> None:
    """The failure mode that shipped: "Vanacker Tom" as one field. It
    must be visible in the preview, not merely representable."""
    item, _ = imp.build_item(
        {**COMPLETE_ROW, "authors": "OECD"}, None,
        session=_ExplodingSession(), csl_cache={},
    )
    imp._print_dry_run_preview({imp.BUILD_SOURCE: item})
    assert "single field" in capsys.readouterr().out


def test_dry_run_preview_labels_each_build_path(capsys) -> None:
    item, _ = imp.build_item(
        COMPLETE_ROW, None, session=_ExplodingSession(), csl_cache={},
    )
    imp._print_dry_run_preview({
        imp.BUILD_SOURCE: item, imp.BUILD_AUTHORITY: item,
        imp.BUILD_FALLBACK: item,
    })
    out = capsys.readouterr().out
    assert "source-built" in out
    assert "authority-filled" in out
    assert "fallback" in out


def test_dry_run_preview_is_silent_with_nothing_to_show(capsys) -> None:
    imp._print_dry_run_preview({})
    assert capsys.readouterr().out == ""


def test_provenance_summary_counts_each_path(capsys) -> None:
    imp._print_provenance_summary(
        {imp.BUILD_SOURCE: 27, imp.BUILD_AUTHORITY: 3, imp.BUILD_FALLBACK: 1},
        no_doi=1,
    )
    out = capsys.readouterr().out
    assert "27" in out and "3" in out
    assert "no DOI" in out


def test_provenance_summary_is_silent_when_nothing_was_built(capsys) -> None:
    """A run that only patches existing items has no provenance to
    report — the line would be four zeroes."""
    imp._print_provenance_summary(
        {imp.BUILD_SOURCE: 0, imp.BUILD_AUTHORITY: 0, imp.BUILD_FALLBACK: 0},
        no_doi=0,
    )
    assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# Creator parsing
# ---------------------------------------------------------------------------


class TestParseAuthors:
    def test_comma_form_splits_at_the_comma(self) -> None:
        assert imp._parse_authors("Doe, Jane") == [
            {"creatorType": "author", "firstName": "Jane", "lastName": "Doe"},
        ]

    def test_display_order_takes_the_last_token_as_family(self) -> None:
        """The safety net, not the plan: display-order rows are meant to
        be filled from their DOI first."""
        assert imp._parse_authors("Jane Doe") == [
            {"creatorType": "author", "firstName": "Jane", "lastName": "Doe"},
        ]

    def test_display_order_with_a_middle_name(self) -> None:
        assert imp._parse_authors("John Q. Public") == [
            {"creatorType": "author", "firstName": "John Q.",
             "lastName": "Public"},
        ]

    def test_a_single_token_stays_a_one_field_name(self) -> None:
        """Zotero's own convention for corporate authors."""
        assert imp._parse_authors("OECD") == [
            {"creatorType": "author", "name": "OECD"},
        ]

    def test_the_vanacker_case_no_longer_produces_a_blob(self) -> None:
        """What the live library actually got: one creator, one field."""
        creators = imp._parse_authors("Tom Vanacker; Sophie Manigart")
        assert all("name" not in c for c in creators)
        assert [c["lastName"] for c in creators] == ["Vanacker", "Manigart"]

    def test_empty_is_no_creators(self) -> None:
        assert imp._parse_authors("") == []
        assert imp._parse_authors(" ; ") == []


class TestHasSplitCreators:
    def test_comma_format(self) -> None:
        assert imp._has_split_creators("Doe, Jane; Public, John") is True

    def test_display_order(self) -> None:
        assert imp._has_split_creators("Jane Doe; John Public") is False

    def test_mixed_is_not_split(self) -> None:
        assert imp._has_split_creators("Doe, Jane; John Public") is False

    def test_empty_wants_an_authority(self) -> None:
        assert imp._has_split_creators("") is False
