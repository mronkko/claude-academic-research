"""Three sources that answered wrongly on a 723-item re-check (2026-09-23).

- OpenAlex's GROBID `<abstract>` for letters, news items and editorials
  was a reference, body text or another article: all ten it wrote.
- ScienceDirect refused the FULL view for 15 journals outside the
  entitlement, where META_ABS is served.
- WoS answered HTTP 400 to ten title queries carrying punctuation or
  operator words.
"""

from __future__ import annotations

import sys
import xml.etree.ElementTree as ET
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from fetchers import ScienceDirectSource
from fetchers.openalex import _grobid_abstract_problem
from fetchers.wos import _query_title

ABSTRACT = (
    "This article considers the uses and decline of workplace occupations "
    "in the 1980s, drawing on archival sources."
)
TITLE = "Job Destruction and Closures in Deindustrializing Britain: The Uses and Decline of Workplace Occupations"


def _tei(header_title: str) -> ET.Element:
    return ET.fromstring(
        '<TEI xmlns="http://www.tei-c.org/ns/1.0"><teiHeader><fileDesc>'
        f"<titleStmt><title>{header_title}</title></titleStmt>"
        "</fileDesc></teiHeader></TEI>"
    )


def test_a_real_abstract_passes() -> None:
    root = _tei("Job destruction and closures in deindustrialising Britain")
    assert _grobid_abstract_problem(root, ABSTRACT, TITLE) is None


@pytest.mark.parametrize(("header", "text", "title", "why"), [
    # BI3IVVAW: a news item; GROBID found no front matter and took a reference.
    ("", "COMMIT collaborative group. Addition of clopidogrel to aspirin.",
     "Medical strike in India", "no title"),
    # J9JJMWWZ: the PDF was another paper, sharing one title word in four.
    ("Disability, Program Access, Empathy and Burnout in US Medical Students",
     "Objective: To investigate whether self-disclosed disability and burnout.",
     "The job demands-resources model of burnout.", "not the item's"),
    # C8XRJ2T3 / VKH8DJNN: a footnote, a sentence picked up mid-way.
    ("Industrial Relations in Transition: The Paper Industry Example",
     "I1. See BLUESTONE & BLUESTONE, supra note 6, at 142-43.",
     "Industrial Relations in Transition: The Paper Industry Example", "fragment"),
    ("Thermal stress in the U.S.A.", "effects. In particular, adrenaline is associated.",
     "Thermal stress in the U.S.A.: effects on violence", "fragment"),
    # SH625THJ: body text that never mentions the title.
    ("Public security in a violent country Segurança pública num país violento",
     "The reflections here arise from recent events in the capital.",
     "Segurança pública num país violento", "no word"),
])
def test_what_is_not_the_items_abstract_is_rejected(header, text, title, why) -> None:
    assert why in _grobid_abstract_problem(_tei(header), text, title)


@pytest.mark.parametrize(("title", "query"), [
    ("Dutch GPs are set to strike over 10% cuts", "Dutch GPs are set to strike over 10 cuts"),
    ("To Strike or Not To Strike", "To Strike To Strike"),
    ("And who is looking after <i>you</i>", "who is looking after you"),
    ("“The strike that failed’ Canadian physician strike, 1987",
     "The strike that failed Canadian physician strike 1987"),
])
def test_wos_title_queries_are_words_only(title, query) -> None:
    assert _query_title(title, drop_operators=True) == query


def test_wos_quoted_phrase_keeps_operator_words() -> None:
    assert _query_title("To Strike or Not?", drop_operators=False) == "To Strike or Not"


def test_sciencedirect_falls_back_to_meta_abs(monkeypatch) -> None:
    views = []

    def retrieval(doi, view, **kw):
        views.append(view)
        if view == "FULL":
            raise type("Scopus400Error", (Exception,), {})(
                "View parameter specified in request is not valid",
            )
        return SimpleNamespace(abstract="An abstract long enough to keep.", originalText="")

    monkeypatch.setitem(sys.modules, "pybliometrics.utils.startup", SimpleNamespace(init=lambda: None))
    monkeypatch.setitem(sys.modules, "pybliometrics.sciencedirect", SimpleNamespace(ArticleRetrieval=retrieval))
    src = ScienceDirectSource(MagicMock(), None)
    assert src.fetch_abstract("10.1016/j.cgh.2019.02.001") == "An abstract long enough to keep."
    assert views == ["FULL", "META_ABS"]
