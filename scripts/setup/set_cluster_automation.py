#!/usr/bin/env python3
"""Set how much of the cluster loop the agent may drive unattended.

Usage:
    python3 set_cluster_automation.py manual
    python3 set_cluster_automation.py confirm
    python3 set_cluster_automation.py --list

Writes `[cluster] automation` into
`~/.config/academic-research/config.toml` and reports the new state,
including whether permission rules already in place mean the level will
behave as something else.

There is **no secret here** — a level is a policy statement, not a
credential — so unlike an API key this is safe for an assistant to write
on the user's behalf when they ask for it. What it is not safe to do is
*decide*: the resource at stake is a shared facility account the plugin
does not own. Set what the user asked for and report what it will
actually do; never raise the level to get past a permission prompt.

Follow with `check_cluster_config.py` (this script calls it for you) —
the level written here is only half the answer, and the other half lives
in the harness's settings files.

Stdlib-only, like everything in `scripts/setup/`.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS_ROOT = _HERE.parent
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from check_cluster_config import (  # noqa: E402
    ENV_VAR,
    LEVEL_HELP,
    LEVELS,
    effective_level,
    scan_permissions,
    settings_paths,
    status_lines,
)
from core import config_writer  # noqa: E402


def _print_choices() -> None:
    print("Automation levels (default: manual):")
    for level in LEVELS:
        print(f"  {level:<8} {LEVEL_HELP[level]}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "level", nargs="?",
        help="Level to write. Omit with --list to see the options.",
    )
    parser.add_argument(
        "--list", action="store_true", help="List the levels and exit.",
    )
    parser.add_argument(
        "--project-dir", default="",
        help="Project directory whose .claude/ settings are read when "
             "reporting the effective level (default: current directory).",
    )
    args = parser.parse_args(argv)

    if args.list or not args.level:
        _print_choices()
        return 0 if args.list else 2

    level = args.level.strip().lower()
    if level not in LEVELS:
        print(
            f"ERROR: unknown automation level {args.level!r}. "
            f"Choose one of {', '.join(LEVELS)}.",
            file=sys.stderr,
        )
        return 2

    config_writer.set_value("cluster", "automation", level)

    home = Path.home()
    project_dir = Path(args.project_dir) if args.project_dir else Path.cwd()
    perms = scan_permissions(settings_paths(project_dir, home))
    effective, notes = effective_level(level, perms)

    for line in status_lines(
        level, "config.toml [cluster] automation", effective, perms, home,
    ):
        print(line)
    for note in notes:
        print(f"NOTE: {note}")

    print()
    if effective != level:
        # The written value is now a description of an intention the
        # machine will not carry out. Say so plainly rather than
        # reporting success and letting the next run be the discovery.
        print(
            f"NEXT: the file now says {level!r}, but this machine will "
            f"behave as {effective!r}. Fix the permission rules above, or "
            f"accept the difference knowingly — do not let the config and "
            f"the behaviour disagree silently.",
        )
    else:
        print(
            f"NEXT: a single run can override this with "
            f"`--automation LEVEL` or {ENV_VAR}, without rewriting the "
            f"file. Nothing in the plugin raises the level on its own.",
        )
    return 0


if __name__ == "__main__":
    # Windows takes stdout's encoding from the locale when output is
    # redirected — normally cp1252, which cannot encode the arrows, em
    # dashes and rules printed below. See scripts/core/console.py.
    from core.console import enable_utf8_output
    enable_utf8_output()
    raise SystemExit(main())
