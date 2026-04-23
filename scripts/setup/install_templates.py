#!/usr/bin/env python3
"""Copy plugin templates into a project in one shot.

Usage:
    python3 install_templates.py PAIR [PAIR ...]
    python3 install_templates.py --force PAIR [PAIR ...]

Each PAIR is `<template-basename>:<dest-relative-path>`. The template
is resolved under `${CLAUDE_PLUGIN_ROOT}/templates/<basename>` and
copied to `<dest>` relative to the current working directory. Parent
directories of the destination are created as needed.

Defaults to skip-if-exists so a re-run on an existing project does
not clobber the user's edits. Pass `--force` to overwrite.

Kept as a script (not inline `cp` calls) so the wizard's existing
`Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)` allow rule covers it.
A long chain of `cp` invocations otherwise triggers a permission
prompt every time a skill bootstraps a project.
"""
import shutil
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = PLUGIN_ROOT / "templates"


def main(argv: list[str]) -> int:
    force = False
    pairs: list[str] = []
    for arg in argv:
        if arg == "--force":
            force = True
        else:
            pairs.append(arg)

    if not pairs:
        print("usage: install_templates.py [--force] PAIR [PAIR ...]", file=sys.stderr)
        print("  each PAIR is <template-basename>:<dest-relative-path>", file=sys.stderr)
        return 2

    copied, skipped, missing = [], [], []
    for pair in pairs:
        if ":" not in pair:
            print(f"bad pair (missing ':'): {pair}", file=sys.stderr)
            return 2
        src_name, dest_rel = pair.split(":", 1)
        src = TEMPLATES / src_name
        dest = Path(dest_rel)

        if not src.is_file():
            missing.append(src_name)
            continue
        if dest.is_file() and not force:
            skipped.append(dest_rel)
            continue
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)
        copied.append(dest_rel)

    for path in copied:
        print(f"copied: {path}")
    for path in skipped:
        print(f"skipped (exists): {path}")
    for name in missing:
        print(f"missing template: {name}", file=sys.stderr)

    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
