#!/usr/bin/env python3
"""Package an editorial-tools skill into a .zip for upload to claude.ai.

claude.ai Skills expect a zip whose top level is the skill directory, with
SKILL.md directly inside it (e.g. ``suggesting-reviewers/SKILL.md``,
``suggesting-reviewers/rosters/...``). This script builds exactly that from
``editorial-tools/skills/<name>/``.

Usage:
    python3 editorial-tools/scripts/package_skill.py [skill-name]

Defaults to ``suggesting-reviewers``. Output goes to ``editorial-tools/dist/<name>.zip``.
Pure standard library — no dependencies, runs the same on macOS/Windows/Linux.
"""

from __future__ import annotations

import sys
import zipfile
from pathlib import Path

# editorial-tools/ is two levels up from this file (scripts/package_skill.py).
PLUGIN_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = PLUGIN_ROOT / "skills"
DIST_DIR = PLUGIN_ROOT / "dist"

# Junk that must never end up in an uploaded artefact.
EXCLUDE_NAMES = {".DS_Store", "Thumbs.db"}
EXCLUDE_DIRS = {"__pycache__", ".ipynb_checkpoints"}
EXCLUDE_SUFFIXES = {".pyc", ".pyo"}


def _should_skip(path: Path) -> bool:
    if path.name in EXCLUDE_NAMES or path.suffix in EXCLUDE_SUFFIXES:
        return True
    return any(part in EXCLUDE_DIRS for part in path.parts)


def package(skill_name: str) -> Path:
    skill_dir = SKILLS_DIR / skill_name
    if not skill_dir.is_dir():
        sys.exit(f"error: no skill directory at {skill_dir}")
    if not (skill_dir / "SKILL.md").is_file():
        sys.exit(f"error: {skill_dir} has no SKILL.md — not a skill")

    DIST_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DIST_DIR / f"{skill_name}.zip"

    # Collect files first so the listing is deterministic (sorted) — a stable
    # archive is friendlier to diffing and re-uploads.
    files = sorted(
        p for p in skill_dir.rglob("*") if p.is_file() and not _should_skip(p)
    )
    if not files:
        sys.exit(f"error: {skill_dir} contains no packageable files")

    # ${CLAUDE_PLUGIN_ROOT}/skills/<name>/ is a Claude Code plugin variable that
    # does not resolve on claude.ai, where bundled files are addressed relative
    # to the skill root. Rewrite that prefix away in the packaged SKILL.md only;
    # the repo source keeps the variable for Claude Code.
    plugin_path_prefix = f"${{CLAUDE_PLUGIN_ROOT}}/skills/{skill_name}/"
    rewrote_paths = False

    total_bytes = 0
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in files:
            # arcname keeps the skill folder as the zip's top-level entry.
            arcname = Path(skill_name) / f.relative_to(skill_dir)
            total_bytes += f.stat().st_size
            if f.name == "SKILL.md":
                text = f.read_text(encoding="utf-8")
                if plugin_path_prefix in text:
                    text = text.replace(plugin_path_prefix, "")
                    rewrote_paths = True
                zf.writestr(arcname.as_posix(), text)
            else:
                zf.write(f, arcname.as_posix())

    size_kb = out_path.stat().st_size / 1024
    print(f"Packaged {skill_name}: {len(files)} files, "
          f"{total_bytes / 1024:.0f} KiB uncompressed → {size_kb:.0f} KiB zip")
    print(f"  {out_path}")
    if rewrote_paths:
        print("  rewrote ${CLAUDE_PLUGIN_ROOT} path in SKILL.md → skill-relative "
              "(claude.ai-compatible)")
    return out_path


def main() -> None:
    skill_name = sys.argv[1] if len(sys.argv) > 1 else "suggesting-reviewers"
    package(skill_name)


if __name__ == "__main__":
    main()
