"""Text a source serves that is not an abstract is not written.

Live 2026-09-23: ten items whose non-abstract text had been cleared by
hand got the same text back on the next `enrich_abstracts` run, because
the sources keep serving it. The fixtures below are those texts
(Semantic Scholar and Crossref), abbreviated.
"""

from __future__ import annotations

import pytest
from abstract_clean import not_an_abstract

JUNK = [
    ("too short", ",", ""),
    ("too short", "Abstract", ""),
    ("too short", "(C) 2016 by the American Psychological Association", ""),
    ("title",
     "Contradictions and Limitations of Final Offer Selection : The "
     "Manitoba Experience. A Comment",
     "Contradictions and Limitations of Final Offer Selection: The "
     "Manitoba Experience. A Comment"),
    ("acknowledgements",
     "The authors would like to thank Gerald Hosp and Hannelore "
     "Weck-Hannemann for valuable comments and suggestions. The usual "
     "disclaimer applies.", ""),
    ("affiliations",
     "1 International University of La Rioja, Department of Business "
     "Organization and Marketing, Logroño, Spain pedro.palos@unir.net 2 "
     "University of the Algarve, School of Management", ""),
    ("author list",
     "Author(s): Zhang, Dajie; Bedogni, Francesco; Boterberg, Sofie; "
     "Camfield, Carol; Camfield, Peter; Charman, Tony; Curfs, Leopold", ""),
    ("journal description",
     "Left History features articles from a variety of theoretical "
     "approaches; these include feminist, marxist, and postmodernist "
     "deliberations on topics such as race, ethnicity, class, gender.", ""),
    ("page header",
     "www.jogh.org • doi: 10.7189/jogh.13.03014 1 2023 • Vol. 13 • 03014 "
     "QUIET QUITTING: A SIGNIFICANT RISK FOR GLOBAL HEALTHCARE As the world "
     "faced the COVID-19 pandemic, most of us did not expect it", ""),
    ("placeholder",
     "(Uploaded by Plazi from the Biodiversity Heritage Library) No abstract "
     "provided for this treatment, which is part of a larger work.", ""),
]


@pytest.mark.parametrize("why,text,title", JUNK)
def test_non_abstract_text_is_named(why: str, text: str, title: str) -> None:
    assert not_an_abstract(text, title=title or None) == why


#: Real abstracts from the same library that an earlier, looser draft of
#: these rules rejected. Each is here for the pattern it resembles.
REAL = [
    "Scholars widely acknowledge that university research is critical to "
    "innovation and entrepreneurship. Much of the literature on university "
    "research, however, evolves separately.",
    "This paper seeks to describe several features of establishing a "
    "closed-loop supply chain for the collection of End-of-Life Vehicles "
    "in Mexico.",
    "Due to their distinctive features, multiteam systems (MTSs) face "
    "significant coordination challenges both within component teams and "
    "across the larger system.",
    "For Permissions, please email: journals.permissions@oup.com. Living "
    "Labs provide a 'human-centric' research approach for the design of "
    "new ICT artefacts.",
    "Students around the world are walking out of school to urge "
    "governments to do more about global warming.",
]


@pytest.mark.parametrize("text", REAL)
def test_real_abstracts_pass(text: str) -> None:
    assert not_an_abstract(text, title="Something else entirely") is None


def test_the_cascade_moves_past_a_rejected_text_and_counts_it_answered() -> None:
    import enrich_abstracts

    class _Src:
        def __init__(self, name, text):
            self.name, self.text = name, text

        def fetch_abstract(self, doi, **kw):
            return self.text

    body = ("We study how strikes affect mental health using panel data "
            "from three countries over two decades.")
    item = {"data": {"DOI": "10.1/x", "title": "Strikes and health"}}

    got = enrich_abstracts._try_cascade(
        item, [_Src("semantic_scholar", "Abstract"), _Src("scopus", body)],
        "/tmp",
    )
    assert (got.abstract, got.source) == (body, "scopus")

    miss = enrich_abstracts._try_cascade(
        item, [_Src("semantic_scholar", "Abstract")], "/tmp",
    )
    assert not miss.found
    assert miss.status() == "not_found"
    assert miss.confirmed_absent
    assert "semantic_scholar too short" in miss.detail()


def test_an_author_bio_is_named() -> None:
    """XWUEEAT3, Semantic Scholar, 2026-09-23."""
    assert not_an_abstract(
        "Bruce E. Zawacki, MD, MA, is Associate Professor of Surgery at the "
        "University of Southern California School of Medicine, Los Angeles.",
    ) == "author bio"


def test_an_abstract_about_a_professor_passes() -> None:
    assert not_an_abstract(
        "Professor Smith, a surgeon, is the subject of this case study of "
        "burnout among senior clinicians in teaching hospitals.",
    ) is None
