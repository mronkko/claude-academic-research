"""Abstract text as sources deliver it, and as it should be stored.

Measured on one real library on 2026-09-22: 2,715 of 22,229 abstracts
carried a publisher copyright notice (Scopus returns it inside the
abstract), 624 held HTML entities, and 1,716 opened with a fused
"Abstract" heading. Downstream screening reads these verbatim.
"""

from __future__ import annotations

import pytest
from abstract_clean import clean_abstract

SPRINGER = (
    "© 2021, The Author(s), under exclusive licence to Springer Science+"
    "Business Media, LLC part of Springer Nature."
)
BODY = "Given recent concerns about the replicability of findings, we test the model."


def test_a_leading_notice_fused_onto_the_first_sentence() -> None:
    """DOI 10.1007/s12144-021-02092-w, via Scopus."""
    assert clean_abstract(f"{SPRINGER}{BODY}") == BODY
    assert clean_abstract(f"{SPRINGER}{BODY}", copyright=SPRINGER) == BODY


def test_a_trailing_notice() -> None:
    """DOI 10.1016/j.ejor.2006.04.047, via Scopus."""
    text = f"{BODY} © 2006 Elsevier B.V. All rights reserved."
    assert clean_abstract(text) == BODY
    assert clean_abstract(text, copyright="© 2006 Elsevier B.V. All rights reserved.") == BODY


@pytest.mark.parametrize("notice", [
    "© 2019 Elsevier B.V. All rights reserved.",
    "Copyright © 2015 John Wiley & Sons, Ltd.",
    "© 2020 Elsevier Ltd",
])
def test_known_leading_shapes_with_a_space(notice) -> None:
    assert clean_abstract(f"{notice} {BODY}") == BODY


def test_an_unrecognised_leading_notice_is_left_alone() -> None:
    """Better a notice kept than a first sentence cut: with no clear end
    to the notice, nothing is removed. ("IEEERacial…" is a real one.)"""
    for text in ("© 2019 The editors. We study the market. Next we test it.",
                 "© 2020 IEEERacial equality is an important theme."):
        assert clean_abstract(text) == text


# Real shapes the first version missed, from a 22,074-abstract library.
@pytest.mark.parametrize("raw", [
    "© 2023 Elsevier LtdCOVID-19 pandemic has brought challenges.",
    "© 2013 Elsevier B.V.This paper analyzes the characteristics.",
    "© 2019 Elsevier B.V. We study the market. Next we test it.",
])
def test_fused_and_spaced_publisher_endings(raw) -> None:
    assert not clean_abstract(raw).startswith("©")
    assert clean_abstract(raw)[0].isupper()


def test_a_notice_ending_in_a_fused_acronym() -> None:
    """DOI 10.18374/ijbr-16-4.6 via Scopus, whose `.copyright` was a
    generic Elsevier line that matched nothing in the text."""
    raw = "© 2016 IABE.Strikes are a key tool for workers."
    hint = "Copyright 2016 Elsevier B.V., All rights reserved."
    assert clean_abstract(raw) == "Strikes are a key tool for workers."
    assert clean_abstract(raw, copyright=hint) == "Strikes are a key tool for workers."
    # One capital before the dot is still an initial, not a sentence end.
    assert clean_abstract("© 2013 Elsevier B.V.This paper.") == "This paper."


@pytest.mark.parametrize("tail", [
    "Copyright (C) 2001 John Wiley & Sons, Ltd.",
    "Copyright ? 2006 John Wiley & Sons, Ltd.",
    "Crown Copyright (C) 2008 Published by Elsevier B.V.",
    "© 2013 © 2013 City University of Hong Kong.",
    "Copyright of the Academy of Management, all rights reserved.",
    "ABSTRACT FROM AUTHOR Copyright of Academy of Management Journal is the "
    "property of Academy of Management and its content may not be copied or "
    "emailed to multiple sites or posted to a listserv without the copyright "
    "holder's express written permission. However, users may print, "
    "download, or email articles for individual use. This abstract may be "
    "abridged. No warranty is given about the accuracy of the copy. Users "
    "should refer to the original published version of the material for the "
    "full abstract. (Copyright applies to all Abstracts.)",
])
def test_trailing_notice_shapes(tail) -> None:
    assert clean_abstract(f"{BODY} {tail}") == BODY


def test_copyright_as_an_ordinary_word_is_kept() -> None:
    text = "Firms litigate often. Copyright infringement is the main cause."
    assert clean_abstract(text) == text


@pytest.mark.parametrize("raw", [
    "abstractScholars share the assumption.",
    "abstract:Scholars share the assumption.",
    "abstract Scholars share the assumption.",
    "Abstracts Scholars share the assumption.",
])
def test_lowercase_and_plural_headings(raw) -> None:
    assert clean_abstract(raw) == "Scholars share the assumption."


def test_the_copyright_hint_is_used_even_mid_shape() -> None:
    hint = "© 2019 Elsevier B.V."
    assert clean_abstract(f"{hint} We study the market.", copyright=hint) == (
        "We study the market."
    )


def test_entities_are_unescaped_including_double_escapes() -> None:
    assert clean_abstract("Firms &amp; markets: a &lt;review&gt;.") == "Firms & markets: a <review>."
    assert clean_abstract("R&amp;amp;D spending rose.") == "R&D spending rose."


@pytest.mark.parametrize("raw", [
    "AbstractWe study labor unions.",
    "ABSTRACT We study labor unions.",
    "Abstract: We study labor unions.",
    "Abstract. We study labor unions.",
    "ABSTRACTWe study labor unions.",
])
def test_a_leading_abstract_heading_is_removed(raw) -> None:
    assert clean_abstract(raw) == "We study labor unions."


def test_the_word_abstract_in_a_sentence_is_kept() -> None:
    text = "Abstract reasoning predicts job performance."
    assert clean_abstract(text) == text


def test_clean_text_is_unchanged() -> None:
    assert clean_abstract(BODY) == BODY


def test_nothing_left_means_none() -> None:
    assert clean_abstract("© 2006 Elsevier B.V. All rights reserved.") is None
    assert clean_abstract("   ") is None


def test_a_publisher_word_inside_the_abstract_is_not_the_notice_end() -> None:
    text = ("© 2020 Elsevier Ltd. We survey American Psychological "
            "Association Members about their careers.")
    assert clean_abstract(text) == (
        "We survey American Psychological Association Members about their careers."
    )


def test_a_published_by_line_belongs_to_the_notice() -> None:
    text = "© 2020 The Authors. Published by Elsevier Ltd. We study firms."
    assert clean_abstract(text) == "We study firms."


# ---------------------------------------------------------------------------
# Wiring: every abstract written is cleaned
# ---------------------------------------------------------------------------


def test_the_cascade_cleans_what_a_source_returns() -> None:
    import enrich_abstracts

    class _Src:
        def __init__(self, name, text):
            self.name, self.text = name, text

        def fetch_abstract(self, doi, **kw):
            return self.text

    item = {"data": {"DOI": "10.1/x", "title": "T"}}
    got = enrich_abstracts._try_cascade(
        item, [_Src("a", f"{SPRINGER}{BODY}")], "/tmp",
    )
    assert got.abstract == BODY

    # A source whose text is nothing but a notice has not answered.
    got = enrich_abstracts._try_cascade(item, [
        _Src("a", "© 2006 Elsevier B.V. All rights reserved."),
        _Src("b", BODY),
    ], "/tmp")
    assert (got.source, got.abstract) == ("b", BODY)


def test_scopus_removes_its_own_copyright_string(monkeypatch) -> None:
    import sys
    import types

    from fetchers.scopus import ScopusSource

    class _AR:
        def __init__(self, *a, **k):
            self.abstract = f"{BODY} © 2019 Elsevier B.V."
            self.copyright = "© 2019 Elsevier B.V."

    scopus_mod = types.SimpleNamespace(AbstractRetrieval=_AR)
    startup = types.SimpleNamespace(init=lambda: None)
    monkeypatch.setitem(sys.modules, "pybliometrics", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "pybliometrics.scopus", scopus_mod)
    monkeypatch.setitem(sys.modules, "pybliometrics.utils", types.SimpleNamespace())
    monkeypatch.setitem(sys.modules, "pybliometrics.utils.startup", startup)
    src = ScopusSource.__new__(ScopusSource)
    assert src.fetch_abstract("10.1/x") == BODY


# ---------------------------------------------------------------------------
# Web of Science is opt-in; any source can be excluded
# ---------------------------------------------------------------------------


class _Named:
    def __init__(self, name):
        self.name = name


ALL = [_Named(n) for n in (
    "crossref", "semantic_scholar", "scopus", "wos", "sciencedirect", "openalex",
)]


def test_the_default_cascade_leaves_out_wos() -> None:
    """Clarivate's Web of Science API terms (v3.8, 4(a)) bar using the
    data with language models without an AI Addendum, and these abstracts
    feed LLM screening. Asking for it by name is the opt-in."""
    import enrich_abstracts

    names = [s.name for s in enrich_abstracts._choose_sources(ALL, [], [])]
    assert "wos" not in names and names[0] == "crossref"
    names = [s.name for s in enrich_abstracts._choose_sources(ALL, ["scopus", "wos"], [])]
    assert names == ["scopus", "wos"]


def test_an_exclusion_wins_over_an_explicit_request() -> None:
    import enrich_abstracts

    got = enrich_abstracts._choose_sources(ALL, ["scopus", "wos"], ["wos"])
    assert [s.name for s in got] == ["scopus"]
    got = enrich_abstracts._choose_sources(ALL, [], ["scopus"])
    assert "scopus" not in [s.name for s in got]


def test_exclusions_come_from_flag_env_and_config(monkeypatch) -> None:
    import core.config_loader as cl
    import enrich_abstracts

    monkeypatch.setattr(cl, "load_config",
                        lambda: {"abstracts": {"exclude_sources": ["scopus"]}})
    monkeypatch.delenv("ABSTRACT_EXCLUDE_SOURCES", raising=False)
    assert enrich_abstracts._excluded_sources("") == {"scopus"}
    monkeypatch.setenv("ABSTRACT_EXCLUDE_SOURCES", "openalex, crossref")
    assert enrich_abstracts._excluded_sources("") == {"openalex", "crossref"}
    assert enrich_abstracts._excluded_sources("wos") == {"openalex", "crossref", "wos"}


@pytest.mark.parametrize("raw", [
    "© Academy of Management Annals.As research has accumulated, we test it.",
    "© 2023 The AuthorsCorporate social responsibility is widely adopted.",
    "© 2018A variable and person-centered approach was applied.",
    "© 2014.If entrepreneurs are constrained, how?",
    "© Academy of Management Learning & Education,2017.Using a framework, we review.",
    "© The authors severally 2020. All rights reserved.This monograph responds.",
])
def test_more_leading_shapes(raw) -> None:
    got = clean_abstract(raw)
    assert "©" not in got and got[0].isupper()


@pytest.mark.parametrize("tail", [
    "Copyright � 2007 John Wiley & Sons, Ltd.",
    "Copyright r 2019 by Emerald Publishing Limited All rights of reproduction in any form reserved.",
    "(Copyright applies to all Abstracts)",
])
def test_more_trailing_shapes(tail) -> None:
    assert clean_abstract(f"{BODY} {tail}") == BODY


def test_markup_revealed_by_unescaping_is_removed() -> None:
    raw = "&lt;p&gt;&lt;span&gt;American labor scholarship emphasizes unions.&lt;/span&gt;&lt;/p&gt;"
    assert clean_abstract(raw) == "American labor scholarship emphasizes unions."
    word = ("<!--[if gte mso 9]><xml> <o:OfficeDocumentSettings> </o:OfficeDocumentSettings>"
            "</xml><![endif]-->We study firms.")
    assert clean_abstract(word) == "We study firms."
    assert clean_abstract("Firms with &lt;50 employees &gt; others.") == "Firms with <50 employees > others."


@pytest.mark.parametrize("tail", [
    "COPYRIGHT © 2005 BLACKWELL PUBLISHING, INC.",
    "© 2014 American Psychological Association.",
])
def test_trailing_notice_without_a_sentence_end(tail) -> None:
    body = "We study smartphone use outside of work hours"
    assert clean_abstract(f"{body} {tail}") == body
