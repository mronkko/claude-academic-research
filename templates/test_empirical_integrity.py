#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Empirical-integrity tests — the recurring-test backstop for the
`empirical-integrity` skill.

Copy this file into your project's `scripts/` directory alongside
`test_common.py`. Edit the paths and `FORBIDDEN_LITERALS` tuple at the
top for your project.

Tests:

1. Manuscript file exists and is non-empty.
2. No forbidden methodology literals in prose (hand-typed model names,
   search dates, counts that should come from inline expressions).
3. Quarto / R Markdown labels (`tbl-`, `fig-`, `sec-`) are unique.
4. Every inline `s['...']` lookup resolves in the **live
   `build_stats()` dict**. The live call is the canonical source of
   truth — the on-disk `manuscript_stats.json` is for human inspection
   and the content-integrity check below. The plugin convention is one
   flat dictionary: pipeline provenance from `search_metadata.json` is
   folded into `s['search.*']` and `s['provenance.*']` by
   `build_stats()` rather than exposed as a separate variable. See the
   `empirical-integrity` skill.
5. Figure-file paths referenced in prose exist on disk.
6. `manuscript_stats.json` matches the live `build_stats()` output
   (content check). Catches both staleness (`build_stats()` reads
   pipeline files newer than the last regeneration) and tampering
   (someone hand-edited the JSON). Replaces the old mtime-only
   freshness check.
7. Optional `--with-render`: run the project's render command and verify
   the output file exists. Off by default — render is critic-loop's job
   during the revision loop.

Run:
    python3 scripts/test_empirical_integrity.py
    python3 scripts/test_empirical_integrity.py -v
    python3 scripts/test_empirical_integrity.py --with-render

Exit code 0 if all pass, 1 if any fail.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter

from test_common import (
    PROJECT_ROOT,
    TestRunner,
    must_exist,
    strip_yaml_and_code,
)

# ---------------------------------------------------------------------------
# Paths — edit for your project
# ---------------------------------------------------------------------------

MANUSCRIPT     = os.path.join(PROJECT_ROOT, "manuscript/manuscript.qmd")
ANALYSIS_DIR   = os.path.join(PROJECT_ROOT, "analysis")
STATS_MODULE   = os.path.join(ANALYSIS_DIR, "manuscript_stats.py")
STATS_JSON     = os.path.join(PROJECT_ROOT, "analysis/results/manuscript_stats.json")
FIGURES_ROOT   = os.path.join(PROJECT_ROOT, "manuscript")  # relative root for ![](…)

# Forbidden methodology literals — hand-typed values that should come
# from inline expressions. Extend for your project with specific counts,
# dates, and version strings you never want to see literally in prose.
FORBIDDEN_LITERALS: tuple[str, ...] = (
    # Model names — always read from manuscript_stats.build_stats() / inline expressions.
    "claude-haiku", "claude-sonnet", "claude-3",
    # Reproducibility strings — if these appear literally, prose is drifting.
    "temperature=0", "temperature = 0",
    # Examples to uncomment / adapt for your SLR:
    # " 81 papers", " 143 included", "1,708 unique",
    # "149 journals",
    # Date strings that could go stale:
    # "2026-04", "October 2025",
)

# Render command — used only when `--with-render` is passed. Override to
# match your project's CLAUDE.md render command.
DEFAULT_RENDER_CMD  = "quarto render {doc} --to gfm"
DEFAULT_RENDERED_EXT = ".md"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_live_stats() -> dict | None:
    """Import `manuscript_stats.build_stats()` from `analysis/` and call it.
    Returns the dict, or None if the module / function is unavailable —
    signalling "no live-stats source; caller should skip"."""
    if not os.path.exists(STATS_MODULE):
        return None
    if ANALYSIS_DIR not in sys.path:
        sys.path.insert(0, ANALYSIS_DIR)
    try:
        from manuscript_stats import build_stats  # type: ignore[import-not-found]
    except ImportError:
        return None
    return build_stats()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_manuscript_exists() -> None:
    must_exist(MANUSCRIPT)


def test_no_forbidden_methodology_literals() -> None:
    """Hand-typed methodology facts must not appear in prose. Code chunks
    and YAML are exempt; grep only the narrative body."""
    if not os.path.exists(MANUSCRIPT):
        return
    with open(MANUSCRIPT, encoding="utf-8") as f:
        body = strip_yaml_and_code(f.read())
    hits = [lit for lit in FORBIDDEN_LITERALS if lit in body]
    assert not hits, (
        f"Forbidden methodology literals in manuscript prose: {hits}\n"
        "Use inline expressions (e.g. `{python} s['screen.n_included']`) instead."
    )


def test_labels_unique() -> None:
    """Every Quarto / R Markdown label (`tbl-`, `fig-`, `sec-`) should be
    unique. Duplicates break cross-references silently."""
    if not os.path.exists(MANUSCRIPT):
        return
    with open(MANUSCRIPT, encoding="utf-8") as f:
        src = f.read()
    labels: list[str] = []
    labels += re.findall(r"#\|\s*label:\s*(\S+)", src)
    labels += re.findall(r"\{#(tbl-[\w:-]+|fig-[\w:-]+|sec-[\w:-]+)\}", src)
    dups = [k for k, v in Counter(labels).items() if v > 1]
    assert not dups, f"duplicate Quarto/Rmd labels: {dups}"


def _extract_inline_keys(body: str, var_name: str) -> set[str]:
    """Find `s['key']`, `s["key"]`, or `r stats$key` patterns for the
    named variable. Returns the bare key names (without quotes)."""
    keys: set[str] = set()
    # `{python} s['key.path']` or `s["key.path"]` anywhere in text
    keys |= set(re.findall(
        rf"(?<!\w){re.escape(var_name)}\[['\"]([\w.:-]+)['\"]\]", body
    ))
    # R-Markdown `r stats$key` style
    keys |= set(re.findall(
        rf"(?<!\w){re.escape(var_name)}\$([\w.:-]+)", body
    ))
    return keys


def test_inline_stats_keys_resolve() -> None:
    """Every `s['...']` lookup in the manuscript must exist in the live
    `build_stats()` dict. Falls back to on-disk JSON if the module is
    unavailable (degraded but functional check); skips entirely if
    neither source exists."""
    if not os.path.exists(MANUSCRIPT):
        return
    stats = _load_live_stats()
    source = "build_stats()"
    if stats is None:
        if not os.path.exists(STATS_JSON):
            return
        with open(STATS_JSON, encoding="utf-8") as f:
            stats = json.load(f)
        source = "manuscript_stats.json (fallback — producer module missing)"
    with open(MANUSCRIPT, encoding="utf-8") as f:
        src = f.read()
    used = _extract_inline_keys(src, "s")
    missing = sorted(k for k in used if k not in stats)
    assert not missing, (
        f"{len(missing)} inline-expression key(s) not in {source}: "
        f"{missing[:5]}. Add the derivation to `build_stats()` and "
        f"re-run `python3 analysis/manuscript_stats.py`."
    )


def test_figure_files_exist() -> None:
    """Markdown image references and `knitr::include_graphics` paths must
    resolve to files on disk."""
    if not os.path.exists(MANUSCRIPT):
        return
    with open(MANUSCRIPT, encoding="utf-8") as f:
        src = f.read()
    body = strip_yaml_and_code(src)
    paths: list[str] = []
    paths += re.findall(r"!\[[^\]]*\]\(([^)\s]+)\)", body)
    paths += re.findall(
        r"knitr::include_graphics\(\s*['\"]([^'\"]+)['\"]\s*\)", src
    )
    missing: list[str] = []
    manuscript_dir = os.path.dirname(MANUSCRIPT)
    for p in paths:
        if p.startswith(("http://", "https://")):
            continue
        abs_p = p if os.path.isabs(p) else os.path.join(manuscript_dir, p)
        if not os.path.exists(abs_p):
            alt = os.path.join(PROJECT_ROOT, p)
            if not os.path.exists(alt):
                missing.append(p)
    assert not missing, (
        f"{len(missing)} referenced figure file(s) missing: {missing[:5]}"
    )


def test_stats_json_matches_build_stats() -> None:
    """On-disk `manuscript_stats.json` must equal the live `build_stats()`
    output. Catches staleness (pipeline outputs have changed since the
    last regeneration) and tampering (someone hand-edited the JSON).

    Skipped when either the producer module or the JSON is absent —
    these are pure manuscript-editing projects that don't have the SR
    pipeline."""
    if not os.path.exists(STATS_JSON):
        return
    fresh = _load_live_stats()
    if fresh is None:
        return  # no producer to compare against
    with open(STATS_JSON, encoding="utf-8") as f:
        on_disk = json.load(f)
    # Round-trip through JSON so type-serialization quirks (tuples vs
    # lists, datetime objects) don't cause false positives.
    fresh_normalized = json.loads(
        json.dumps(fresh, default=str, ensure_ascii=False)
    )
    if fresh_normalized == on_disk:
        return
    # Build a compact diff summary
    fresh_keys = set(fresh_normalized)
    disk_keys = set(on_disk)
    added = sorted(fresh_keys - disk_keys)
    removed = sorted(disk_keys - fresh_keys)
    changed = sorted(
        k for k in fresh_keys & disk_keys
        if fresh_normalized[k] != on_disk[k]
    )
    parts: list[str] = []
    if added:
        parts.append(f"missing from JSON: {added[:3]}")
    if removed:
        parts.append(f"stale keys in JSON: {removed[:3]}")
    if changed:
        samples = [
            f"{k} ({on_disk[k]!r} → {fresh_normalized[k]!r})"
            for k in changed[:3]
        ]
        parts.append(f"value drift: {samples}")
    raise AssertionError(
        "manuscript_stats.json differs from live build_stats() output — "
        + "; ".join(parts) + ". Regenerate via "
        "`python3 analysis/manuscript_stats.py`."
    )


def test_render_succeeds() -> None:
    """Invoke the project's render command and verify the output exists
    and is non-empty. Only runs when `--with-render` is passed."""
    if not os.path.exists(MANUSCRIPT):
        return
    cmd = DEFAULT_RENDER_CMD.format(doc=MANUSCRIPT)
    result = subprocess.run(
        cmd, shell=True, capture_output=True, text=True,
        cwd=PROJECT_ROOT, check=False,
    )
    assert result.returncode == 0, (
        f"render failed ({cmd}):\n{result.stderr[-500:]}"
    )
    base, _ = os.path.splitext(MANUSCRIPT)
    out = base + DEFAULT_RENDERED_EXT
    must_exist(out)


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument(
        "--with-render", action="store_true",
        help="also invoke the render command (off by default; critic-loop "
             "renders every iteration)",
    )
    args = parser.parse_args()

    r = TestRunner(verbose=args.verbose)
    r.run("manuscript file exists", test_manuscript_exists)
    r.run("no forbidden methodology literals in prose",
          test_no_forbidden_methodology_literals)
    r.run("Quarto/Rmd labels are unique", test_labels_unique)
    r.run("inline s['...'] keys resolve in build_stats()",
          test_inline_stats_keys_resolve)
    r.run("figure files exist on disk", test_figure_files_exist)
    r.run("manuscript_stats.json matches live build_stats() output",
          test_stats_json_matches_build_stats)
    if args.with_render:
        r.run("render succeeds and produces output", test_render_succeeds)
    return r.report()


if __name__ == "__main__":
    sys.exit(main())
