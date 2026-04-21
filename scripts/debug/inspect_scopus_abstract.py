#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pybliometrics>=3.6",
# ]
# ///
"""Debug helper: print every abstract-related field pybliometrics returns.

Our Scopus live test was asserting len(ar.abstract) > 60 and failing with 0
characters. This script dumps every candidate field on the
AbstractRetrieval object so we can see where the abstract actually lives
(or confirm Scopus genuinely has no abstract for this DOI).

Usage:
    uv run scripts/debug/inspect_scopus_abstract.py 10.1016/j.jbusvent.2006.10.003

Looks up `~/.config/pybliometrics.cfg` for credentials.
"""

from __future__ import annotations

import sys

from pybliometrics.scopus import AbstractRetrieval

if len(sys.argv) != 2:
    sys.exit("usage: inspect_scopus_abstract.py <DOI>")

doi = sys.argv[1]

try:
    from pybliometrics.utils.startup import init
    init()
except Exception as e:  # noqa: BLE001
    print(f"pybliometrics init warning: {e}", file=sys.stderr)


def _show(label: str, value) -> None:
    if value is None:
        print(f"  {label:30s} = None")
        return
    if isinstance(value, str):
        s = value.strip()
        print(f"  {label:30s} = [{len(s)} chars] {s[:120]!r}"
              + ("..." if len(s) > 120 else ""))
        return
    if isinstance(value, list):
        print(f"  {label:30s} = list of {len(value)}")
        for i, item in enumerate(value[:3]):
            print(f"      [{i}] {item!r}")
        if len(value) > 3:
            print(f"      ... {len(value) - 3} more")
        return
    print(f"  {label:30s} = {value!r}")


for view in ("META_ABS", "FULL", "REF"):
    print(f"\n=== view={view} ===")
    try:
        ar = AbstractRetrieval(doi, view=view)
    except Exception as e:  # noqa: BLE001
        print(f"  ERROR: {type(e).__name__}: {e}")
        continue

    for attr in ("abstract", "description", "originalText",
                 "coverDate", "title", "publicationName",
                 "authkeywords", "idxterms", "subject_areas"):
        if hasattr(ar, attr):
            _show(attr, getattr(ar, attr))
        else:
            _show(f"{attr} (missing)", None)

    # Dump all attributes that look plausibly abstract-like
    print("  -- all string/list attributes --")
    for attr in sorted(dir(ar)):
        if attr.startswith("_") or attr in ("abstract", "description",
                                            "originalText", "coverDate",
                                            "title", "publicationName",
                                            "authkeywords", "idxterms",
                                            "subject_areas"):
            continue
        try:
            v = getattr(ar, attr)
        except Exception:
            continue
        if callable(v):
            continue
        if isinstance(v, str) and v.strip() and len(v.strip()) > 20:
            _show(attr, v)
