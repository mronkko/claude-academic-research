#!/usr/bin/env python3
"""Copy plugin templates into a project in one shot.

Usage:
    python3 install_templates.py PAIR [PAIR ...]
    python3 install_templates.py --force PAIR [PAIR ...]

Each PAIR is `<template-basename>:<dest-relative-path>`. The template
is resolved under `${CLAUDE_PLUGIN_ROOT}/templates/<basename>` and
copied to `<dest>` relative to the current working directory (the
project). Parent directories of the destination are created as needed.

Both ends are confined: the source must resolve to a file inside the
plugin's `templates/`, and the destination must resolve to a path
inside the current directory. A pair that names `../../.ssh/config`,
an absolute path, or a symlink leading out of either tree is refused
and nothing is written for it. The pairs are usually composed by an
agent, and `--force` overwrites, so an unconfined destination would be
an arbitrary-file overwrite one typo or one injected instruction away.

Defaults to skip-if-exists so a re-run on an existing project does
not clobber the user's edits. Pass `--force` to overwrite.

Kept as a shipped script rather than inline `cp` calls: one auditable,
cross-platform command per bootstrap, instead of a shell chain that
differs between POSIX and Windows and that a user has to read
invocation by invocation.
"""
import shutil
import sys
from pathlib import Path

PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
TEMPLATES = PLUGIN_ROOT / "templates"


def _confined(path: Path, root: Path) -> bool:
    """Whether `path` resolves to somewhere strictly inside `root`."""
    resolved = path.resolve()
    root = root.resolve()
    return resolved != root and resolved.is_relative_to(root)


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

    project = Path.cwd()
    copied, skipped, missing, refused = [], [], [], []
    for pair in pairs:
        if ":" not in pair:
            print(f"bad pair (missing ':'): {pair}", file=sys.stderr)
            return 2
        src_name, dest_rel = pair.split(":", 1)
        src = TEMPLATES / src_name
        dest = project / dest_rel

        if not _confined(src, TEMPLATES):
            refused.append(f"{src_name} (source outside the plugin's templates/)")
            continue
        if Path(dest_rel).is_absolute() or not _confined(dest, project):
            refused.append(f"{dest_rel} (destination outside the project directory)")
            continue
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
    for what in refused:
        print(f"refused: {what}", file=sys.stderr)

    if refused:
        return 2
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
