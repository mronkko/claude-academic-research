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
