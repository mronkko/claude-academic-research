"""Predatory-journal lookup.

Checks a journal against a cached snapshot of Beall's list
(<https://beallslist.net>). The systematic-review pipeline calls this
during import and tags flagged items with ``predatory:flag`` in Zotero.

Policy: **flag, do not silence.** Flagged items remain in the corpus;
the author decides during full-text review whether to keep each one.
This matches the social-sciences convention that predatory-journal
status is a warning to readers, not an auto-exclusion.

Data source
-----------
Snapshot files live under ``scripts/sources/data/``:

- ``beall_publishers.txt`` — one publisher name per line (substring match
  against journal name).
- ``beall_standalone.txt`` — one journal name per line (exact or
  substring match).
- ``beall_issn.txt`` — one ISSN per line (exact match on normalised ISSN).

The repo ships with a ``_placeholder`` marker line in each file. Before
running the pipeline, populate them with a current snapshot from
<https://beallslist.net/>.

Usage::

    from sources.predatory import check_predatory, PredatoryResult

    result = check_predatory(journal="Journal of X", issn="1234-5678")
    if result.is_predatory:
        print(result.reason)
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from functools import lru_cache

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")


@dataclass(frozen=True)
class PredatoryResult:
    is_predatory: bool
    reason: str
    source: str  # "beall_publisher" | "beall_standalone" | "beall_issn" | ""


def _normalise_issn(issn: str | None) -> str:
    if not issn:
        return ""
    return re.sub(r"[^0-9xX]", "", issn).upper()


def _load_lines(filename: str) -> set[str]:
    path = os.path.join(DATA_DIR, filename)
    if not os.path.exists(path):
        return set()
    out: set[str] = set()
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or line == "_placeholder":
                continue
            out.add(line)
    return out


@lru_cache(maxsize=1)
def _publisher_patterns() -> set[str]:
    return {p.lower() for p in _load_lines("beall_publishers.txt")}


@lru_cache(maxsize=1)
def _standalone_patterns() -> set[str]:
    return {p.lower() for p in _load_lines("beall_standalone.txt")}


@lru_cache(maxsize=1)
def _issn_set() -> set[str]:
    return {_normalise_issn(i) for i in _load_lines("beall_issn.txt")}


def check_predatory(journal: str | None, issn: str | None = None) -> PredatoryResult:
    """Check a journal against the cached Beall's list snapshot.

    Returns a PredatoryResult. The first match wins (ISSN → standalone →
    publisher).
    """
    norm_issn = _normalise_issn(issn)
    if norm_issn and norm_issn in _issn_set():
        return PredatoryResult(
            is_predatory=True,
            reason=f"ISSN {issn} listed on Beall's list (ISSN match).",
            source="beall_issn",
        )

    j = (journal or "").strip().lower()
    if j:
        for pattern in _standalone_patterns():
            if pattern in j:
                return PredatoryResult(
                    is_predatory=True,
                    reason=f'Journal name matches Beall standalone entry: "{pattern}".',
                    source="beall_standalone",
                )
        for pattern in _publisher_patterns():
            if pattern in j:
                return PredatoryResult(
                    is_predatory=True,
                    reason=f'Journal name matches Beall publisher entry: "{pattern}".',
                    source="beall_publisher",
                )

    return PredatoryResult(is_predatory=False, reason="", source="")
