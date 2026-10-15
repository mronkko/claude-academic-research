"""Tests for scripts/pipelines/doctype.py.

Fixtures are real titles/DOIs from a 2026 management SLR search (Crossref,
journal-restricted), where APA supplements were 22% of the Journal of
Applied Psychology records and publisher front matter about 6% overall.
"""

from __future__ import annotations

import doctype
import pytest

SUPPLEMENTS = [
    ("Supplemental Material for Reconciling Female Agentic Advantage and "
     "Disadvantage", "10.1037/apl0000550.supp"),
    ("Supplemental Material for A Meta-Analysis of Leadership and Workplace "
     "Safety", "10.1037/apl0000557.supp"),
]

FRONT_MATTER = [
    ("Editorial Board", "10.1016/s0883-9026(21)00091-4"),
    ("Editorial Board", "10.1016/j.leaqua.2025.101865"),
    ("Issue Information", "10.1002/job.2525"),
    ("Issue Information", "10.1002/sej.1423"),
    ("Acknowledgment of Ad Hoc Reviewers", "10.1037/apl0001070"),
    ("Acknowledgment", "10.1037/apl0001329"),
    ("Call for Papers: The Leadership Quarterly Yearly Review (LQYR) for 2024",
     "10.1016/j.leaqua.2022.101599"),
    ("Call for proposals: The 2027 Leadership Quarterly Yearly Review (LQYR) "
     "issue", "10.1016/j.leaqua.2025.101896"),
]

NOTICES = [
    ("Publication Notice", "10.1177/10422587221120291"),
    ("Publication Notice", "10.1177/10422587251391494"),
]

ARTICLES = [
    ("Colocation of Entrepreneurs and New Firm Survival: Role of New Firm "
     "Founder's Experiential Relatedness to Local Entrepreneurs",
     "10.1177/10422587211057029"),
    ("Erratum", "10.1177/10422587221089911"),
    ("Correction to “The dynamics of gender and alternatives in "
     "negotiation”", "10.1037/apl0001275"),
    ("Rationality in the entrepreneurship process: Is being rational "
     "actually rational?", "10.1016/j.jbusvent.2023.106301"),
]


@pytest.mark.parametrize(("title", "doi"), SUPPLEMENTS)
def test_apa_supplements(title, doi):
    assert doctype.classify(title, doi) == doctype.SUPPLEMENT


def test_supplement_by_doi_alone_and_title_alone():
    assert doctype.classify("", "10.1037/APL0000550.SUPP") == "supplement"
    assert doctype.classify("", "https://doi.org/10.1037/apl0000550.supp") \
        == "supplement"
    assert doctype.classify("Supplemental Material for X", "") == "supplement"
    # The bare phrase with nothing after it is not a supplement title.
    assert doctype.classify("Supplemental material for", "") == ""


@pytest.mark.parametrize(("title", "doi"), FRONT_MATTER)
def test_front_matter(title, doi):
    assert doctype.classify(title, doi) == doctype.FRONT_MATTER


def test_featured_cover_matches_even_with_an_author():
    # Wiley's real "Featured Cover" record (10.1002/job.70003) carries a
    # byline; an exact front-matter title needs no author check.
    assert doctype.classify("Featured Cover", "10.1002/job.70003",
                            has_authors=True) == "front-matter"


def test_call_for_papers_prefix_needs_no_authors():
    title = "Call for papers in entrepreneurship: a bibliometric review"
    assert doctype.classify(title, "", has_authors=True) == ""
    assert doctype.classify(title, "", has_authors=False) == "front-matter"


@pytest.mark.parametrize(("title", "doi"), NOTICES)
def test_sage_publication_notice_is_a_notice_not_front_matter(title, doi):
    assert doctype.classify(title, doi) == doctype.NOTICE


@pytest.mark.parametrize(("title", "doi"), ARTICLES)
def test_ordinary_records_are_not_classified(title, doi):
    assert doctype.classify(title, doi, has_authors=True) == ""
    assert doctype.classify(title, doi, has_authors=False) == ""


def test_title_normalisation():
    assert doctype.classify("  EDITORIAL\n  board. ", "") == "front-matter"
    assert doctype.classify("<i>Issue</i> Information", "") == "front-matter"
    # Front-matter words inside a longer title do not count.
    assert doctype.classify("The editorial board as a gatekeeper", "") == ""


def test_parent_doi():
    assert doctype.parent_doi("10.1037/APL0000550.supp") == "10.1037/apl0000550"
    assert doctype.parent_doi("10.1037/apl0000550") == ""
    assert doctype.parent_doi(".supp") == ""


def test_tags_are_global_and_validated():
    assert doctype.tag_for("supplement") == "doctype:supplement"
    assert doctype.tag_for("front-matter") == "doctype:front-matter"
    assert doctype.tag_for("notice") == "doctype:notice"
    with pytest.raises(ValueError):
        doctype.tag_for("erratum")


def test_from_tags_reads_strings_and_dicts_and_unknown_values():
    assert doctype.from_tags([{"tag": "slr/abstract:include"},
                              {"tag": "doctype:supplement"}]) == "supplement"
    assert doctype.from_tags(["doctype:erratum"]) == "erratum"
    assert doctype.from_tags(["doctype:"]) == ""
    # Namespaced look-alikes are not the global tag.
    assert doctype.from_tags(["slr/doctype:supplement"]) == ""


def _item(key, title, tags=(), doi="", creators=()):
    return {"key": key, "data": {"key": key, "title": title, "DOI": doi,
                                 "creators": list(creators),
                                 "tags": [{"tag": t} for t in tags]}}


def test_split_items_counts_per_doctype_and_ignores_untagged_lookalikes():
    items = [
        _item("A", "Real paper"),
        _item("B", "Supplemental Material for X", ["doctype:supplement"]),
        _item("C", "Editorial Board", ["doctype:front-matter"]),
        _item("D", "Issue Information", ["doctype:front-matter"]),
        _item("E", "Editorial Board"),  # untagged: stays in
    ]
    kept, skipped = doctype.split_items(items)
    assert [i["key"] for i in kept] == ["A", "E"]
    assert skipped == {"supplement": 1, "front-matter": 2}
    assert doctype.untagged_lookalikes(items) == 1
    assert doctype.format_counts(skipped) == "supplement 1, front-matter 2"


def test_supplement_links_pairs_only_known_parents():
    key_by_doi = {
        "10.1037/apl0000550.supp": "SUPP0001",
        "10.1037/apl0000550": "ART00001",
        "10.1037/apl0000557.supp": "SUPP0002",  # parent not in library
    }
    pairs = doctype.supplement_links(
        ["10.1037/APL0000550.supp", "10.1037/apl0000557.supp",
         "10.1037/apl0000550.supp"],
        key_by_doi,
    )
    assert pairs == [("SUPP0001", "ART00001")]


class _RelZot:
    def __init__(self, fail_on=()):
        self.calls: list[tuple] = []
        self._fail_on = set(fail_on)

    def add_related(self, key, related, *, cloud=False):
        if key in self._fail_on:
            raise RuntimeError("412")
        self.calls.append((key, tuple(related), cloud))
        return 1


def test_link_supplements_links_both_ways_and_survives_failures(capsys):
    zot = _RelZot(fail_on={"SUPP0002"})
    linked, failed = doctype.link_supplements(
        [("SUPP0001", "ART00001"), ("SUPP0002", "ART00002")], zot, cloud=True,
    )
    assert (linked, failed) == (1, 1)
    assert zot.calls == [("SUPP0001", ("ART00001",), True),
                         ("ART00001", ("SUPP0001",), True)]
    assert "could not relate supplement SUPP0002" in capsys.readouterr().out
