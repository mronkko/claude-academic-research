#!/usr/bin/env python3
"""Shared test infrastructure for `test_citations.py`,
`test_empirical_integrity.py`, and `test_systematic_review.py`.

Copy this file into your project's `scripts/` directory alongside
whichever `test_*.py` templates you use. Each test file does:

    from test_common import TestRunner, must_exist, read_csv, PROJECT_ROOT

and keeps its own configuration block at the top (paths, forbidden
literals, etc.).
"""

from __future__ import annotations

import csv
import os
from collections.abc import Callable

# Project root is two levels up from this file when placed at
# `<project>/scripts/test_common.py`.
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestRunner:
    """Minimal test collector. Prints failures as it goes; emits a summary
    at the end. Exit code 0 if all pass, 1 if any fail."""

    def __init__(self, verbose: bool = False) -> None:
        self.verbose = verbose
        self.passed: list[str] = []
        self.failed: list[tuple[str, str]] = []

    def run(self, name: str, fn: Callable[[], None]) -> None:
        try:
            fn()
            self.passed.append(name)
            if self.verbose:
                print(f"  ✓ {name}", flush=True)
        except AssertionError as e:
            self.failed.append((name, str(e)))
            print(f"  ✗ {name}\n      {e}", flush=True)
        except Exception as e:
            self.failed.append((name, f"unhandled {type(e).__name__}: {e}"))
            print(f"  ✗ {name}\n      unhandled {type(e).__name__}: {e}",
                  flush=True)

    def report(self) -> int:
        total = len(self.passed) + len(self.failed)
        print(f"\n{'=' * 60}")
        print(f"Tests passed: {len(self.passed)}/{total}")
        if self.failed:
            print(f"Failures ({len(self.failed)}):")
            for name, err in self.failed:
                print(f"  - {name}: {err}")
            return 1
        print("ALL PASS.")
        return 0


def must_exist(path: str) -> None:
    assert os.path.exists(path), f"missing file: {path}"
    assert os.path.getsize(path) > 0, f"empty file: {path}"


def read_csv(path: str) -> list[dict]:
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def last_row_per_key(rows: list[dict], key_col: str = "item_key") -> dict[str, dict]:
    """Last-row-wins reduction — the canonical pattern for append-only
    screening logs. Preserves history while letting the latest decision
    per item win."""
    out: dict[str, dict] = {}
    for r in rows:
        k = r.get(key_col)
        if k:
            out[k] = r
    return out


def strip_yaml_and_code(src: str) -> str:
    """Strip YAML frontmatter, fenced code chunks (Quarto + plain), and
    inline `{python}`/`{r}` expressions from manuscript source. Leaves
    prose suitable for regex grep without false positives from code.

    The import is local so the module remains dependency-free at top level.
    """
    import re

    body = re.sub(r"\A---\n.*?\n---\n", "", src, count=1, flags=re.S)
    body = re.sub(r"```\{[^}]*\}.*?```", "", body, flags=re.S)
    body = re.sub(r"```.*?```", "", body, flags=re.S)
    body = re.sub(r"`\{python\}[^`]*`", "", body)
    body = re.sub(r"`r\s+[^`]*`", "", body)
    return body
