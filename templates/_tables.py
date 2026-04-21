"""Manuscript table helpers — project template.

Pandas-based helpers that turn the `coded_papers.csv` file into
publication-ready tables. Imported by the Quarto manuscript's Python
chunks — keeps the manuscript source readable and puts the table
logic in a separate, testable module.

Typical Quarto usage:
    ```{python}
    from manuscript._tables import tbl_exclusion_reasons, tbl_methods
    print(tbl_methods().to_markdown(index=False))
    ```

All table functions follow the same shape: no arguments (they read
`coded_papers.csv` themselves), return a `pandas.DataFrame`. That
keeps each inline expression a one-liner.
"""

from __future__ import annotations

import csv
from collections import Counter
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parent.parent

CODED_PAPERS_PATH = PROJECT_ROOT / "analysis" / "results" / "coded_papers.csv"
FULLTEXT_LOG_PATH = PROJECT_ROOT / "screening" / "fulltext_screening.csv"


def _read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def tbl_methods() -> pd.DataFrame:
    """Count of included papers by (first-listed) research method."""
    rows = _read_csv(CODED_PAPERS_PATH)
    methods = Counter()
    for r in rows:
        m = (r.get("method", "") or "").split(";")[0].strip()
        if m:
            methods[m[:60]] += 1
    df = pd.DataFrame(methods.most_common(), columns=["Method", "N"])
    return df


def tbl_sample_regions() -> pd.DataFrame:
    """Count of papers by geographic region of the sample.

    Basic regex-based classifier; extend for your needs.
    """
    rows = _read_csv(CODED_PAPERS_PATH)
    region_of: dict[str, int] = Counter()
    for r in rows:
        sample = (r.get("sample", "") or "").lower()
        region = "Other / multi-country"
        if any(c in sample for c in ("united states", "u.s.", "usa", "american")):
            region = "United States"
        elif any(c in sample for c in ("uk", "britain", "england")):
            region = "United Kingdom"
        elif "china" in sample:
            region = "China"
        elif "finland" in sample or "finnish" in sample:
            region = "Finland"
        elif "europe" in sample:
            region = "Europe (multi-country)"
        region_of[region] += 1
    return pd.DataFrame(region_of.most_common(),
                        columns=["Region", "N"])


def tbl_exclusion_reasons() -> pd.DataFrame:
    """Count of full-text exclusions grouped by `exclusion_code`."""
    rows = _read_csv(FULLTEXT_LOG_PATH)
    last: dict[str, dict] = {}
    for r in rows:
        k = r.get("item_key")
        if k:
            last[k] = r
    excluded = [r for r in last.values() if r.get("decision") == "exclude"]
    codes = Counter((r.get("exclusion_code", "") or "uncoded") for r in excluded)
    return pd.DataFrame(codes.most_common(),
                        columns=["Exclusion code", "N"])


def tbl_included_papers() -> pd.DataFrame:
    """One row per included paper with short identifying columns."""
    rows = _read_csv(CODED_PAPERS_PATH)
    df = pd.DataFrame(rows)
    if df.empty:
        return df
    # Keep only the most useful columns for a manuscript table.
    keep = [c for c in ("bibtex_key", "year", "authors", "journal",
                        "method", "sample", "key_findings")
            if c in df.columns]
    return df[keep].sort_values(by="year" if "year" in df.columns else keep[0])
