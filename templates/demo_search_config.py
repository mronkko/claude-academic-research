"""search_config.py for the `demo-slr` skill.

The skill fills every `<...>` placeholder with the presenter's approved
topic, then installs this file as `search_config.py` in the demo project.
No journal scoping: a demo topic should search the whole literature.
"""

# Last three years: recent enough to be interesting, small enough to be quick.
FROM_YEAR = 0          # <FROM_YEAR, e.g. 2023>
TO_YEAR = 0            # <TO_YEAR, e.g. 2025>

# Empty = no journal restriction (search.py scopes only when this is non-empty).
JOURNALS = {}

# Scopus / Web of Science, used only when those databases are configured.
QUERY_DEFS = [
    (
        "Q1_demo",
        'TITLE-ABS-KEY(<SCOPUS_QUERY>)',
        'TS=(<WOS_QUERY>)',
    ),
]

# OpenAlex / Semantic Scholar: `search.py` runs block A and block B as two
# separate queries, not as A AND B. So put phrases that already join both
# concepts in block A ("<concept A> <concept B>") and leave block B empty.
BLOCK_A_TERMS = [
    # "<first concept term>",
]

BLOCK_B_TERMS = [
    # "<second concept term>",
]
