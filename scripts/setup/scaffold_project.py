#!/usr/bin/env python3
"""Scaffold a new SLR / manuscript project: create the standard
directory layout and copy plugin templates into place.

Per user feedback (T3-1, 2026-04-25):
> Creating project structure should be a script instead of bash commands.

Bash `mkdir -p screening/ analysis/ manuscript/ ...` + a sequence of
`cp` calls used to be the canonical "set up a fresh SLR project" path.
That requires the wizard to allowlist `mkdir` and `cp`, which the user
considers too coarse a permission. This script does the same job
behind a single allowlist entry the wizard already covers
(`Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)`).

Idempotent: directories that already exist are silent no-ops; template
copies skip files already present (so a user's customisations don't
get clobbered on re-run). Cross-platform — pure Path operations, no
chmod or shell calls.

Usage:
    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/scaffold_project.py
    python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/scaffold_project.py \\
        --target /path/to/project --kind manuscript

`--kind sr` (default) — full systematic-review layout with screening/
pilot/ analysis/ manuscript/. Suits a fresh PRISMA pipeline.

`--kind manuscript` — manuscript-only layout. Suits a project that
already has data and just needs the manuscript scaffolding (revisions
loop, citation testing, manuscript stats helpers).
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent.parent
TEMPLATES_DIR = PLUGIN_ROOT / "templates"

# Standard SLR project layout. Order doesn't matter; mkdir is idempotent.
SR_DIRS = (
    "screening",
    "pilot",
    "analysis",
    "manuscript",
    "scripts",
    "output",
    ".claude",
    "critic-reviews",   # T-N: visible review reports (Package critic-loop)
)

# Subset for manuscript-only projects (no SLR pipeline).
MANUSCRIPT_DIRS = (
    "manuscript",
    "scripts",
    ".claude",
    "critic-reviews",
)


# Mapping from `templates/<source>` → relative project path.
# The script doesn't overwrite an existing target; users keep their
# customisations on re-run.
SR_TEMPLATE_COPIES = (
    ("sr_claude_md.md",          "CLAUDE.md"),
    ("search_config.py",         "search_config.py"),
    ("screening_config.py",      "screening_config.py"),
    ("manuscript.qmd",           "manuscript/manuscript.qmd"),
    ("manuscript_stats.py",      "manuscript/manuscript_stats.py"),
    ("manuscript_tables.py",     "manuscript/manuscript_tables.py"),
    ("test_systematic_review.py", "scripts/test_systematic_review.py"),
    ("test_citations.py",        "scripts/test_citations.py"),
    ("test_empirical_integrity.py", "scripts/test_empirical_integrity.py"),
    ("test_common.py",           "scripts/test_common.py"),
)

MANUSCRIPT_TEMPLATE_COPIES = (
    ("manuscript_claude_md.md",  "CLAUDE.md"),
    ("manuscript.qmd",           "manuscript/manuscript.qmd"),
    ("manuscript_stats.py",      "manuscript/manuscript_stats.py"),
    ("manuscript_tables.py",     "manuscript/manuscript_tables.py"),
    ("test_citations.py",        "scripts/test_citations.py"),
    ("test_empirical_integrity.py", "scripts/test_empirical_integrity.py"),
    ("test_common.py",           "scripts/test_common.py"),
)


# Lines added to project `.gitignore`. Idempotent: each line is added
# only if its (commented or uncommented) form isn't already present.
GITIGNORE_ENTRIES = (
    ".claude/",         # Internal scratch (audit, fact-check, etc.)
    "output/",          # Pipeline runtime artefacts (caches, logs)
    # critic-reviews/ is intentionally NOT ignored — users want those
    # reports in version control alongside manuscript history.
)


def ensure_dirs(target: Path, dirs: tuple[str, ...]) -> list[Path]:
    """Create each directory under `target`. Returns the list of
    directories actually created (for the post-run summary)."""
    created: list[Path] = []
    for rel in dirs:
        p = target / rel
        if not p.exists():
            p.mkdir(parents=True, exist_ok=True)
            created.append(p)
    return created


def copy_templates(
    target: Path, copies: tuple[tuple[str, str], ...],
) -> tuple[list[Path], list[Path]]:
    """Copy each (source, dest) pair from templates/ into `target`.

    Returns (newly_copied, already_present) so the caller can show a
    clear summary of what changed vs what stayed put. Existing files
    are left untouched — users' edits to e.g. `screening_config.py`
    survive re-running the scaffold.
    """
    newly_copied: list[Path] = []
    already_present: list[Path] = []
    for source_name, dest_rel in copies:
        source = TEMPLATES_DIR / source_name
        dest = target / dest_rel
        if dest.exists():
            already_present.append(dest)
            continue
        if not source.is_file():
            print(
                f"  warning: template {source} not found in plugin — "
                f"skipping {dest}",
                flush=True,
            )
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        newly_copied.append(dest)
    return newly_copied, already_present


def update_gitignore(target: Path, entries: tuple[str, ...]) -> list[str]:
    """Append each entry to `.gitignore` if not already present.

    Creates the file when missing (only inside an existing git repo —
    check for `.git/`; outside of git we skip silently). Returns the
    list of entries actually added.
    """
    if not (target / ".git").is_dir():
        return []
    gitignore = target / ".gitignore"
    existing_lines: set[str] = set()
    if gitignore.is_file():
        for ln in gitignore.read_text(encoding="utf-8").splitlines():
            stripped = ln.strip().lstrip("#").strip()
            if stripped:
                existing_lines.add(stripped)
    added: list[str] = []
    for entry in entries:
        if entry not in existing_lines:
            added.append(entry)
    if added:
        appendix = ""
        if gitignore.is_file():
            current = gitignore.read_text(encoding="utf-8")
            if not current.endswith("\n"):
                appendix = "\n"
        appendix += "\n# academic-research scaffold entries\n"
        appendix += "\n".join(added) + "\n"
        with gitignore.open("a", encoding="utf-8") as fh:
            fh.write(appendix)
    return added


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Create the standard SLR / manuscript project layout and "
            "copy plugin templates into place. Idempotent."
        ),
    )
    parser.add_argument(
        "--target", default=".",
        help="Project root (default: current directory).",
    )
    parser.add_argument(
        "--kind", choices=("sr", "manuscript"), default="sr",
        help=(
            "Layout shape. `sr` = full systematic-review (default). "
            "`manuscript` = manuscript + tests only, no screening / "
            "pilot / analysis directories."
        ),
    )
    args = parser.parse_args()

    target = Path(args.target).resolve()
    if not target.is_dir():
        sys.exit(f"ERROR: --target is not a directory: {target}")

    if args.kind == "manuscript":
        dirs = MANUSCRIPT_DIRS
        copies = MANUSCRIPT_TEMPLATE_COPIES
    else:
        dirs = SR_DIRS
        copies = SR_TEMPLATE_COPIES

    print(f"Scaffolding {args.kind!r} project at {target}", flush=True)
    print(f"  templates source: {TEMPLATES_DIR}", flush=True)

    created_dirs = ensure_dirs(target, dirs)
    if created_dirs:
        for d in created_dirs:
            print(f"  + {d.relative_to(target)}/", flush=True)
    else:
        print("  all directories already exist (no-op)", flush=True)

    print(flush=True)
    print(f"Copying templates ({args.kind} layout)", flush=True)
    new_copies, kept = copy_templates(target, copies)
    for p in new_copies:
        print(f"  + {p.relative_to(target)}", flush=True)
    for p in kept:
        print(f"  = {p.relative_to(target)} (already exists; not overwritten)",
              flush=True)

    added_gitignore = update_gitignore(target, GITIGNORE_ENTRIES)
    if added_gitignore:
        print(flush=True)
        print(f".gitignore updated with {len(added_gitignore)} entries:",
              flush=True)
        for entry in added_gitignore:
            print(f"  + {entry}", flush=True)
    elif (target / ".git").is_dir():
        print("\n.gitignore already covers expected entries (no-op).",
              flush=True)
    else:
        print(
            "\nNot a git repo — skipped .gitignore. Initialise git "
            "(`git init`) and re-run if you want the standard ignores.",
            flush=True,
        )

    print(flush=True)
    print("Done.", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
