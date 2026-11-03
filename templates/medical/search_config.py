"""Search configuration for a BIOMEDICAL / DENTAL systematic review.

Pre-filled for: "Accuracy and acquisition efficiency of digital scanning
methods for auricular prosthesis fabrication". Replace the concept blocks
for another topic; keep the structure.

Differences from the social-science `templates/search_config.py`:

- `JOURNALS = {}`. Journal-ISSN scope is a rank-list device (ABS, FT50).
  A clinical search is scoped by MeSH, field tags and filters in the
  query instead. An empty `JOURNALS` is supported: no ISSN clause is
  added and the citation stream is unscoped.
- `PUBMED_QUERY_DEFS` is new: native PubMed syntax, sent verbatim, with
  only the year window added.
- The search is built from CONCEPT BLOCKS (population/condition AND
  index test) and does NOT include outcome terms such as "accuracy",
  "trueness" or "time". Outcome words are inconsistently reported in
  titles and abstracts; screening on them at the search stage loses
  eligible studies. Outcomes are applied at screening.

Report in the manuscript (PRISMA 2020 item 7 and PRISMA-S): every
database, the platform, the date last searched, and the full query as
written here. `search_metadata.json` records all of it.

Embase and the Cochrane Library have no API this pipeline can call.
Run those searches in their own interfaces, export RIS/CSV, and add the
records to the Zotero collection by import. Record them in the protocol,
because PRISMA counts them as databases.
"""

# ---------------------------------------------------------------------------
# 1. Time window (also the date limit PubMed applies via [Date - Publication])
# ---------------------------------------------------------------------------

FROM_YEAR = 1990
TO_YEAR = 2026


# ---------------------------------------------------------------------------
# 2. Journal scope — intentionally empty for a clinical review
# ---------------------------------------------------------------------------

JOURNALS: dict = {}


# ---------------------------------------------------------------------------
# 3. Concept blocks (written once, reused to build every query below)
#
# Block 1 — condition / device: the ear defect and its prosthesis.
# Block 2 — index test: digital 3D data acquisition of the ear or defect.
#
# PubMed notes:
# - `[tiab]` = title/abstract; `[MeSH Terms]` explodes the heading.
# - Phrase truncation is not reliable in PubMed, so plural / variant
#   phrases are written out ("auricular prosthesis" OR "auricular
#   prostheses"). Single-word truncation (`scan*`) is fine, but PubMed
#   expands it to at most 600 terms; check the Search Details box.
# - MeSH headings are indexed with a lag; always pair them with [tiab].
# ---------------------------------------------------------------------------

_EAR = (
    '"auricular prosthesis"[tiab] OR "auricular prostheses"[tiab] OR '
    '"ear prosthesis"[tiab] OR "ear prostheses"[tiab] OR '
    '"auricular epithesis"[tiab] OR "ear epithesis"[tiab] OR '
    '"pinna prosthesis"[tiab] OR "auricular defect"[tiab] OR '
    '"auricular defects"[tiab] OR "ear defect"[tiab] OR '
    '"ear defects"[tiab] OR "ear rehabilitation"[tiab] OR '
    'microtia[tiab] OR anotia[tiab] OR '
    '(("Maxillofacial Prosthesis"[MeSH Terms] OR "maxillofacial prosthesis"[tiab] '
    'OR "maxillofacial prostheses"[tiab] OR "facial prosthesis"[tiab] '
    'OR "facial prostheses"[tiab]) '
    'AND (ear[tiab] OR auricle[tiab] OR auricular[tiab] OR pinna[tiab] '
    'OR "Ear, External"[MeSH Terms]))'
)

_SCAN = (
    'scan[tiab] OR scans[tiab] OR scanner[tiab] OR scanners[tiab] OR '
    'scanning[tiab] OR "digital impression"[tiab] OR "digital impressions"[tiab] '
    'OR "optical impression"[tiab] OR "optical impressions"[tiab] OR '
    '"3D imaging"[tiab] OR "three-dimensional imaging"[tiab] OR '
    'photogrammetry[tiab] OR stereophotogrammetry[tiab] OR '
    '"structured light"[tiab] OR "laser scanning"[tiab] OR '
    '"3D photography"[tiab] OR "3D surface imaging"[tiab] OR '
    '"Imaging, Three-Dimensional"[MeSH Terms] OR Photogrammetry[MeSH Terms]'
)

# Sensitivity block: digital-workflow reports that use a scanner without
# saying so in the title or abstract. Screen these for scan data; most
# will be excluded, which is the price of recall.
_WORKFLOW = (
    '"digital workflow"[tiab] OR "digital technique"[tiab] OR '
    '"digital technology"[tiab] OR "digital technologies"[tiab] OR '
    '"computer-aided design"[tiab] OR CAD-CAM[tiab] OR "CAD/CAM"[tiab] OR '
    '"3D printing"[tiab] OR "three-dimensional printing"[tiab] OR '
    '"additive manufacturing"[tiab] OR "rapid prototyping"[tiab] OR '
    '"Computer-Aided Design"[MeSH Terms] OR "Printing, Three-Dimensional"[MeSH Terms]'
)


# ---------------------------------------------------------------------------
# 4. PubMed queries — each entry is (label, query)
# ---------------------------------------------------------------------------

PUBMED_QUERY_DEFS = [
    (
        "P1_ear_x_scanning",
        f"({_EAR}) AND ({_SCAN})",
    ),
    (
        "P2_sensitivity_ear_x_digital_workflow",
        f"({_EAR}) AND ({_WORKFLOW})",
    ),
]


# ---------------------------------------------------------------------------
# 5. Scopus / Web of Science — same concepts, each database's own syntax
#    (each entry is (label, scopus_query, wos_query); the journal and year
#    clauses are added by search.py, and JOURNALS is empty so no ISSN clause)
# ---------------------------------------------------------------------------

QUERY_DEFS = [
    (
        "S1_ear_x_scanning",
        'TITLE-ABS-KEY(("auricular prosthesis" OR "auricular prostheses" OR '
        '"ear prosthesis" OR "ear prostheses" OR "auricular epithesis" OR '
        '"ear epithesis" OR "auricular defect*" OR "ear defect*" OR microtia '
        'OR anotia OR (("maxillofacial prosthe*" OR "facial prosthe*") AND '
        "(ear OR auricle OR auricular OR pinna))) AND "
        '(scan* OR "digital impression*" OR "optical impression*" OR '
        '"3D imaging" OR "three-dimensional imaging" OR photogrammetry OR '
        'stereophotogrammetry OR "structured light" OR "3D photography"))',
        'TS=(("auricular prosthe*" OR "ear prosthe*" OR "auricular epithe*" OR '
        '"ear epithe*" OR "auricular defect*" OR "ear defect*" OR microtia '
        'OR anotia OR (("maxillofacial prosthe*" OR "facial prosthe*") AND '
        "(ear OR auricle OR auricular OR pinna))) AND "
        '(scan* OR "digital impression*" OR "optical impression*" OR '
        '"3D imaging" OR "three-dimensional imaging" OR photogrammetry OR '
        'stereophotogrammetry OR "structured light" OR "3D photography"))',
    ),
]


# ---------------------------------------------------------------------------
# 6. OpenAlex block terms. Its `search=` covers full text; pass
#    `--search-fields title_abstract` to match PubMed/Scopus/WoS.
# ---------------------------------------------------------------------------

BLOCK_A_TERMS = [
    "auricular prosthesis",
    "ear prosthesis",
    "auricular epithesis",
    "ear epithesis",
]

BLOCK_B_TERMS = [
    "3D scanning",
    "intraoral scanner",
    "photogrammetry",
    "digital impression",
    "structured light scanner",
]


# ---------------------------------------------------------------------------
# 7. Citation seeds — forward snowballing from key studies
#
# With JOURNALS empty the citation stream is unscoped by venue. Seeds are
# DOIs already in the library (from Zotero), chosen because later work on
# ear scanning cites them. Uncomment after confirming they are central.
# ---------------------------------------------------------------------------

CITATION_SEEDS: list[str] = [
    # "10.1016/j.prosdent.2023.09.028",  # Gadallah 2025, intraoral scanners for auricular prostheses
    # "10.1016/j.prosdent.2020.03.022",  # Bannink 2021, optical scanning + SLA printing workflow
]
