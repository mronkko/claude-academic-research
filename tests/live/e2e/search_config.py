"""search_config.py fixture for the live mini end-to-end SLR test (BACKLOG.md "L1").

NOT a template for downstream projects — copy `templates/search_config.py`
for that. This file is the fixed input `scripts/dev/mini_slr.py` copies
into each run's project root (`output/e2e/<run-id>/search_config.py`).

Scope, deliberately narrow and frozen (BACKLOG's settled corpus):
    - Three entrepreneurship journals, one PDF-source cascade each:
      JBV (Elsevier/ScienceDirect TDM), SEJ (Wiley TDM), Small Business
      Economics (Springer).
    - A single closed year (FROM_YEAR == TO_YEAR) so the corpus is
      near-frozen and reruns stay comparable — see the
      `from_year <= to_year` fix in templates/test_systematic_review.py
      (a strict `<` rejected exactly this legitimate case).
    - Both QUERY_DEFS (Scopus/WoS) and BLOCK_A/B_TERMS (OpenAlex/Semantic
      Scholar) are populated so all four search databases run — that's
      the only way search.py's cross-database dedup and the
      search_run.json DOI hash get exercised against real, messy data.
    - Broad terms on purpose: `mini_slr.py`'s `trim` stage cuts the
      result down to ~8 rows afterwards, so recall here matters more
      than precision.
"""

FROM_YEAR = 2019
TO_YEAR = 2019


JOURNALS = {
    "0883-9026": ("n/a", "Journal of Business Venturing"),
    "1932-4391": ("n/a", "Strategic Entrepreneurship Journal"),
    "0921-898X": ("n/a", "Small Business Economics"),
}


QUERY_DEFS = [
    (
        "Q1_growth_broad",
        'TITLE-ABS-KEY("firm growth" OR "growth intention" OR '
        '"growth aspiration" OR entrepreneur* OR venture OR startup)',
        'TS=("firm growth" OR "growth intenti*" OR "growth aspir*" OR '
        'entrepreneur* OR venture OR startup)',
    ),
]


BLOCK_A_TERMS = [
    "entrepreneur",
    "venture",
    "startup",
    "small business",
]

BLOCK_B_TERMS = [
    "growth",
    "performance",
    "innovation",
]
