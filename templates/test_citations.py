#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Citation-integrity tests — the recurring-test backstop for the
`grounded-citations` and `fact-check` skills.

Copy this file into your project's `scripts/` directory alongside
`test_common.py`. Edit the paths at the top if your manuscript or
bibliography live elsewhere.

Tests:

1. `references.bib` exists and is non-empty.
2. Every `@citekey` in the manuscript resolves in `references.bib`.
3. No bare "Author (YYYY)" mention in prose without a governing `@key`.
4. BBT keys in `coded_papers.csv` are unique and non-empty.
5. Every BBT key in `coded_papers.csv` appears in `references.bib`.

Tests 4 and 5 skip gracefully when `coded_papers.csv` is absent, so the
file is equally useful for pure manuscript-editing projects.

Run:
    python3 scripts/test_citations.py        # concise output
    python3 scripts/test_citations.py -v     # list every test

Exit code 0 if all pass, 1 if any fail.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections import Counter

from test_common import (
    PROJECT_ROOT,
    TestRunner,
    must_exist,
    read_csv,
    strip_yaml_and_code,
)

# ---------------------------------------------------------------------------
# Paths — edit for your project
# ---------------------------------------------------------------------------

MANUSCRIPT     = os.path.join(PROJECT_ROOT, "manuscript/manuscript.qmd")
REFERENCES_BIB = os.path.join(PROJECT_ROOT, "manuscript/references.bib")
CODED_PAPERS   = os.path.join(PROJECT_ROOT, "analysis/results/coded_papers.csv")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_references_bib_exists() -> None:
    must_exist(REFERENCES_BIB)


def test_manuscript_citekeys_resolve() -> None:
    """Every `@citekey` in prose must have an entry in `references.bib`.

    Handles: `@key`, `[@key1; @key2]`, `@key-with-dashes`, `@key:subcode`.
    Strips YAML and code chunks first to avoid false positives from
    `@decorator` Python syntax or email-like tokens in comments.
    """
    if not os.path.exists(MANUSCRIPT):
        return
    with open(MANUSCRIPT, encoding="utf-8") as f:
        src = f.read()
    body = strip_yaml_and_code(src)
    # BBT keys are alphanumeric + `-`, `_`, `:`; must start with a letter.
    cite_keys = set(re.findall(r"(?<![\w@])@([A-Za-z][\w:-]*)", body))
    if not cite_keys:
        return
    with open(REFERENCES_BIB, encoding="utf-8") as f:
        bib = f.read()
    bib_keys = set(re.findall(r"@\w+\{([^,\s]+)\s*,", bib))
    missing = sorted(cite_keys - bib_keys)
    assert not missing, (
        f"{len(missing)} manuscript citekey(s) missing from references.bib: "
        f"{missing[:5]}"
    )


def test_no_uncited_author_year_mentions() -> None:
    """Fail on prose like 'Kolvereid (1992)' or 'Smith & Jones (2019)' with
    no governing `@citekey`.

    LLM-drafted prose slips in bare author-year mentions that render as
    plain text and never appear in the References list. Every in-text
    author+year reference must be a proper `@citekey`. If a mention is
    rhetorical and should not be cited, rewrite the prose to drop the year
    (e.g. "Kolvereid's foundational study" rather than "Kolvereid 1992").
    There is no allowlist by design.
    """
    if not os.path.exists(MANUSCRIPT):
        return
    with open(MANUSCRIPT, encoding="utf-8") as f:
        src = f.read()
    body = strip_yaml_and_code(src)

    # Words that look like surnames but aren't (structural references in prose).
    non_names = {
        "Stage", "Table", "Figure", "Section", "Chapter", "Part",
        "Volume", "Page", "Study", "Wave", "Step", "Phase", "Appendix",
        "January", "February", "March", "April", "May", "June",
        "July", "August", "September", "October", "November", "December",
    }

    # Surname: starts with an uppercase letter, >= 3 chars (skip initials),
    # permits Unicode accents and hyphenation.
    surname_re = r"[A-Z][A-Za-zÀ-ɏ-]{2,}"
    coauthor_re = (
        rf"(?:,?\s+(?:et\s+al\.?|&\s+{surname_re}|and\s+{surname_re}))?"
    )
    pattern = re.compile(
        rf"(?<![@\w])({surname_re}){coauthor_re}"
        rf"\s*(?:\((\d{{4}})[a-z]?\)|(\d{{4}})[a-z]?\b)"
    )

    violations: list[str] = []
    for m in pattern.finditer(body):
        surname = m.group(1)
        year = m.group(2) or m.group(3)
        if not (1900 <= int(year) <= 2099):
            continue
        if surname in non_names:
            continue
        # If an `@key` governs this mention within ~60 chars before, skip.
        start = max(0, m.start() - 60)
        window = body[start:m.start()]
        if re.search(r"@[A-Za-z][\w:-]*[^\w]*$", window):
            continue
        # Inside a citation bracket group `[@a; @b]` — skip.
        last_open = body.rfind("[", 0, m.start())
        last_close = body.rfind("]", 0, m.start())
        if last_open > last_close and "@" in body[last_open:m.start()]:
            continue
        ctx_start = max(0, m.start() - 30)
        ctx_end = min(len(body), m.end() + 30)
        snippet = body[ctx_start:ctx_end].replace("\n", " ").strip()
        violations.append(f"{surname} {year} — …{snippet}…")

    assert not violations, (
        f"{len(violations)} uncited in-text author(year) mention(s) — "
        f"replace with @citekey or rewrite the prose to drop the year:\n"
        + "\n".join(f"  • {v}" for v in violations[:10])
    )


def test_bbt_keys_unique_in_coded_papers() -> None:
    """BBT citation keys must be non-empty and unique per included paper.

    Skips if `coded_papers.csv` is absent (pure manuscript-editing projects).
    """
    if not os.path.exists(CODED_PAPERS):
        return
    rows = read_csv(CODED_PAPERS)
    missing = [r["item_key"] for r in rows
               if not (r.get("bibtex_key") or "").strip()]
    assert not missing, (
        f"{len(missing)} included paper(s) missing bibtex_key: {missing[:5]}"
    )
    keys = [r["bibtex_key"] for r in rows if r.get("bibtex_key")]
    dups = [k for k, c in Counter(keys).items() if c > 1]
    assert not dups, f"duplicate bibtex_keys in coded_papers.csv: {dups[:5]}"


def test_coded_papers_keys_in_references_bib() -> None:
    """Every BBT key in `coded_papers.csv` should have a matching bib
    entry. Skips if either file is absent."""
    if not (os.path.exists(CODED_PAPERS) and os.path.exists(REFERENCES_BIB)):
        return
    coded_keys = {(r.get("bibtex_key") or "").strip() for r in read_csv(CODED_PAPERS)
                  if (r.get("bibtex_key") or "").strip()}
    with open(REFERENCES_BIB, encoding="utf-8") as f:
        bib_keys = set(re.findall(r"@\w+\{([^,\s]+)\s*,", f.read()))
    missing = coded_keys - bib_keys
    assert not missing, (
        f"{len(missing)} BBT key(s) in coded_papers.csv missing from "
        f"references.bib; first 5: {sorted(missing)[:5]}. "
        f"Run `uv run ${{CLAUDE_PLUGIN_ROOT}}/scripts/pipelines/generate_bib.py`."
    )


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args()

    r = TestRunner(verbose=args.verbose)
    r.run("references.bib exists", test_references_bib_exists)
    r.run("manuscript citekeys resolve in references.bib",
          test_manuscript_citekeys_resolve)
    r.run("no uncited 'Author (YYYY)' mentions in prose",
          test_no_uncited_author_year_mentions)
    r.run("BBT keys unique + non-empty in coded_papers.csv",
          test_bbt_keys_unique_in_coded_papers)
    r.run("coded_papers.csv BBT keys all in references.bib",
          test_coded_papers_keys_in_references_bib)
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
