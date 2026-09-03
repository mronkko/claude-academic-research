"""Per-project search configuration for a systematic literature review.

Copy this file to the root of your SLR project and edit every block
below for your specific review. The `search.py` and `search_openalex.py`
pipeline scripts read this module by path (via `--config`).

Keep this file in git alongside your manuscript. It IS the scope of
your review — reviewers will read it to judge whether the search is
appropriate.

Usage:
    uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/search.py --config ./search_config.py
"""

# ---------------------------------------------------------------------------
# 1. Time window
# ---------------------------------------------------------------------------

FROM_YEAR = 2016
TO_YEAR   = 2026


# ---------------------------------------------------------------------------
# 2. Journal scope — {ISSN: (rating, full_title)}
#
# The rating string is free-form (your project's own label — "ABS 4*",
# "FT50", "UTD24", etc.). It appears in the metadata JSON and in
# manuscript tables. The full title is displayed in logs.
#
# Sizing: a narrow domain-specific SR typically has 10–50 journals; a
# broader business-and-management SR (e.g. ABS-2024 rank 4/4* plus
# ABS-3 entrepreneurship) lands at ~150.
#
# ONE journal per line; comments are fine.
# ---------------------------------------------------------------------------

JOURNALS = {
    # Entrepreneurship — replace with your discipline's list.
    "1042-2587": ("ABS 4*", "Entrepreneurship Theory and Practice"),
    "0883-9026": ("ABS 4*", "Journal of Business Venturing"),
    "1932-4391": ("ABS 4",  "Strategic Entrepreneurship Journal"),
    "0898-5626": ("ABS 3",  "Entrepreneurship and Regional Development"),
    "0895-0067": ("ABS 3",  "Family Business Review"),
    # Add as many as your scope demands. Typical SLRs: 10–150 journals.
}


# ---------------------------------------------------------------------------
# 3. Scopus / WoS queries — each entry is (label, scopus_query, wos_query)
#
# Scopus stems phrase plurals automatically ("growth intention" matches
# "growth intentions"). WoS does NOT — wildcard the tail of multi-word
# phrases: `TS=("growth intenti*")` to cover both.
#
# Do not include ISSN or year filters — `search.py` adds them per-query
# from JOURNALS and FROM_YEAR/TO_YEAR.
# ---------------------------------------------------------------------------

QUERY_DEFS = [
    (
        "Q1_narrow_self_selecting",
        # Scopus — stemming handles plurals
        'TITLE-ABS-KEY("growth intention" OR "growth aspiration" OR '
        '"growth motivation")',
        # WoS — phrase wildcards for plurals
        'TS=("growth intenti*" OR "growth aspir*" OR "growth motivat*")',
    ),
    (
        "Q2_broad_concept_x_outcome",
        # Motivational constructs AND growth-related outcomes
        'TITLE-ABS-KEY(motivation OR intention OR aspiration) AND '
        'TITLE-ABS-KEY("firm growth" OR "venture growth" OR "high-growth")',
        'TS=(motivation OR intention OR aspiration) AND '
        'TS=("firm growth" OR "venture growth" OR "high-growth")',
    ),
    # Add Q3, Q4, … as your strategy requires.
]


# ---------------------------------------------------------------------------
# 4. OpenAlex block terms (used only by search_openalex.py)
#
# OpenAlex's `search=` parameter is relevance-ranked, so a single
# combined query can miss papers highly relevant to one concept but
# only weakly to another. The OpenAlex search script runs two block
# queries (concepts + outcomes) separately, then merges and dedupes.
# Leave empty ([]) if you are not using OpenAlex.
# ---------------------------------------------------------------------------

# NOTE ON FIELDS: these block terms go to OpenAlex's `search=`, which
# covers FULL TEXT, while QUERY_DEFS above go to Scopus TITLE-ABS-KEY and
# WoS TS=, which cover title/abstract/keywords only. That difference is
# large when your target papers are defined by what they DID rather than
# what they are ABOUT: across six management journals, "three-way
# interaction" appears in 17 titles/abstracts and 113 full texts, and
# "common method bias" in 1 versus 181. Pass `--search-fields
# title_abstract` to restrict OpenAlex to match the others; the choice is
# recorded in search_metadata.json, which PRISMA requires you to report.

BLOCK_A_TERMS = [
    "motivation",
    "intention",
    "aspiration",
]

BLOCK_B_TERMS = [
    "firm growth",
    "venture growth",
    "high-growth",
]


# ---------------------------------------------------------------------------
# 5. Citation seeds — forward snowballing (optional second search stream)
#
# DOIs whose CITING works should be retrieved, with no journal
# restriction. This is a different retrieval operation from the keyword
# search above, and it finds a different population.
#
# Why it is worth running: a paper that applies a method cites the paper
# that introduced the method, but often uses none of your topic's
# vocabulary in its title or abstract. No keyword query reaches it and no
# amount of term tuning will — the words are simply not there. Citing the
# seed is the only signal, so the seed is what you search on.
#
# Seed a DOI when a specific named work defines the thing you are
# reviewing: a method or estimator, a scale or instrument, a theoretical
# framework, a widely reused dataset.
#
# The year window above always applies. `JOURNALS` applies too, by
# default: `--citation-journal-scope auto` (the default) scopes the
# stream whenever JOURNALS is non-empty, and `off` opens it to any venue.
#
# Choose deliberately. Scoped asks "which papers in my journals cite this
# without matching my keywords?" — still a real gain over the keyword
# stream, and much cheaper, since OpenAlex filters venues server-side.
# Open asks "who cites this anywhere?" — right when the review's object
# is the method itself rather than a literature in a venue list, at the
# cost of volume: one review's seed returned 1839 citing works of which
# 107 were in its 22 target journals.
#
# Rows are tagged `discovery_source = "citation_search"` in the output
# CSVs and counted separately in `search_metadata.json`, because PRISMA
# reports a citation search under "other sources" rather than in the
# database counts. A record found by both streams is credited to the
# database search.
#
# Supported by OpenAlex and Semantic Scholar. Scopus needs a Scopus EID
# rather than a DOI for `REFEID()`, and the Web of Science Starter tier
# exposes no cited-reference endpoint; both are skipped with a message.
#
# Leave empty ([]) to run the keyword search alone. To pilot a seed
# before committing to a full run:
#     uv run search.py --config ./search_config.py --streams citation
#
# A database can suit one stream and not the other. Semantic Scholar
# returns no ISSN, so it cannot be scoped to the JOURNALS list above at
# the source (scope is matched on journal titles client-side), which
# makes it weak for a journal-restricted keyword search — but it is
# strong for citation search. Split them per stream rather than
# admitting it to both:
#     uv run search.py --config ./search_config.py \
#         --databases wos,openalex --citation-databases openalex,semantic_scholar
# ---------------------------------------------------------------------------

CITATION_SEEDS: list[str] = [
    # "10.1037/0021-9010.91.4.917",   # Dawson & Richter (2006), 3-way interactions
]
