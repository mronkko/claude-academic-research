"""What survives when the same paper is found in two databases.

Dedup kept the first row it saw and folded in only the abstract, so a
run that searched OpenAlex before Web of Science threw away WoS's
comma-format creators and its issue number — the very fields the import
then had to buy back from Crossref, one HTTP request per record.

The merge is field-wise: first non-empty wins for the bibliographic
columns, and comma-format authors displace display-order authors
because they are a strictly better rendering of the same fact.
"""

from __future__ import annotations

import search
from searchers import empty_row


def _row(db: str, **fields) -> dict:
    row = empty_row()
    row.update({"db": db, **fields})
    return row


# ---------------------------------------------------------------------------
# _has_comma_authors
# ---------------------------------------------------------------------------


def test_comma_format_is_recognised() -> None:
    assert search._has_comma_authors("Doe, Jane; Public, John Q.") is True


def test_display_order_is_not_comma_format() -> None:
    assert search._has_comma_authors("Jane Doe; John Q. Public") is False


def test_a_single_display_order_name_spoils_the_string() -> None:
    """Mixed is not usable: the import splits the whole column one way."""
    assert search._has_comma_authors("Doe, Jane; John Q. Public") is False


def test_empty_authors_are_not_comma_format() -> None:
    assert search._has_comma_authors("") is False
    assert search._has_comma_authors("  ;  ") is False


# ---------------------------------------------------------------------------
# Field-wise merge across databases
# ---------------------------------------------------------------------------


def test_dedup_fills_missing_fields_from_the_duplicate() -> None:
    openalex = _row("openalex", doi="10.1/x", title="T",
                    authors="Jane Doe", volume="158", issue="",
                    pages="", type="journal-article")
    s2 = _row("semantic_scholar", doi="10.1/x", title="T",
              authors="Jane Doe", volume="158", issue="",
              pages="120001-120014", type="")

    deduped, _ = search._dedup([openalex, s2])

    assert len(deduped) == 1
    assert deduped[0]["pages"] == "120001-120014"
    assert deduped[0]["volume"] == "158"


def test_dedup_prefers_comma_format_authors_over_display_order() -> None:
    """The repair that costs nothing: a multi-database run fixes
    OpenAlex-style names from the WoS row instead of from Crossref."""
    openalex = _row("openalex", doi="10.1/x", title="T",
                    authors="Jane Doe; John Q. Public")
    wos = _row("wos", doi="10.1/x", title="T",
               authors="Doe, Jane; Public, John Q.")

    deduped, _ = search._dedup([openalex, wos])

    assert deduped[0]["authors"] == "Doe, Jane; Public, John Q."


def test_dedup_does_not_downgrade_comma_format_authors() -> None:
    wos = _row("wos", doi="10.1/x", title="T", authors="Doe, Jane")
    openalex = _row("openalex", doi="10.1/x", title="T", authors="Jane Doe")

    deduped, _ = search._dedup([wos, openalex])

    assert deduped[0]["authors"] == "Doe, Jane"


def test_dedup_keeps_the_first_non_empty_value() -> None:
    """Later rows fill gaps; they never overwrite a populated field."""
    scopus = _row("scopus", doi="10.1/x", title="T", authors="Doe, Jane",
                  volume="158", type="journal-article")
    wos = _row("wos", doi="10.1/x", title="T", authors="Doe, Jane",
               volume="999", type="book-chapter")

    deduped, _ = search._dedup([scopus, wos])

    assert deduped[0]["volume"] == "158"
    assert deduped[0]["type"] == "journal-article"


def test_dedup_still_merges_abstracts() -> None:
    a = _row("openalex", doi="10.1/x", title="T", abstract="")
    b = _row("scopus", doi="10.1/x", title="T", abstract="The abstract.")

    deduped, _ = search._dedup([a, b])

    assert deduped[0]["abstract"] == "The abstract."


def test_no_doi_row_contributes_its_metadata_to_the_doi_row() -> None:
    """The title+author merge path, which previously moved only the
    abstract — so a DOI-less WoS row's issue number was discarded."""
    with_doi = _row("openalex", doi="10.1/x", title="Anticipating",
                    authors="Doe, Jane", issue="")
    without_doi = _row("wos", doi="", title="Anticipating",
                       authors="Doe, Jane", issue="3", pages="1-20")

    deduped, merged = search._dedup([with_doi, without_doi])

    assert merged == 1
    assert len(deduped) == 1
    assert deduped[0]["issue"] == "3"
    assert deduped[0]["pages"] == "1-20"


def test_unmatched_no_doi_rows_survive_untouched() -> None:
    with_doi = _row("openalex", doi="10.1/x", title="One", authors="Doe, Jane")
    other = _row("wos", doi="", title="Something else", authors="Roe, Ann")

    deduped, merged = search._dedup([with_doi, other])

    assert merged == 0
    assert {r["title"] for r in deduped} == {"One", "Something else"}
