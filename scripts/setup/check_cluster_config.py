#!/usr/bin/env python3
"""Report how much of the cluster loop the agent is allowed to drive.

Usage:
    python3 check_cluster_config.py
    python3 check_cluster_config.py --automation confirm
    python3 check_cluster_config.py --list

Prints a short status block and exits 0:

    automation: confirm
    source: config.toml [cluster] automation
    effective: auto
    allow_rules: Bash(ssh:*) (~/.claude/settings.json)
    settings_files: 2 read

`cluster-screening` runs this first and obeys what it says. **The level
is not the agent's to choose.** The resource at stake is a shared
facility account the plugin does not own, on hardware the user is
accountable for, and a level is a statement about how much of that the
user has agreed to hand over.

Precedence, highest first:

    1. --automation LEVEL          (this run only)
    2. ACADEMIC_RESEARCH_CLUSTER_AUTOMATION
    3. config.toml [cluster] automation
    4. manual                      (the default)

Levels:

    manual   emit and apply locally; print the ssh/sbatch commands and
             stop. Touches no remote host.
    confirm  the same loop, with every remote call going through the
             permission prompt. See below — this level is a claim about
             the *harness*, not about this script.
    auto     the whole loop unattended. Requires allow rules the user
             added themselves.

**The default is `manual`, and deliberately.** `confirm`'s safety comes
from a human answering a permission prompt. In a headless session there
is nobody to answer one, so `confirm` degrades toward `auto` exactly
where a mistake is least likely to be noticed. A level whose safety
depends on a TTY is not a safe default.

## What `effective` means, and why it can differ

This script does not grant, withhold, or enforce anything — it reports.
Enforcement lives in the permission system: **absent an allow rule, the
agent's Bash prompt IS the approval**, and the plugin therefore never
adds `Bash(ssh:*)`, `Bash(scp:*)`, `Bash(rsync:*)` or `Bash(sbatch:*)`
to `permissions.allow` (pinned by `tests/unit/test_cluster_automation.py`).

The failure that needs catching is a user who once clicked "don't ask
again" on an `ssh` call and has silently promoted themselves to `auto`
without ever choosing it — six months later, in a different project, on
a manifest nobody reviewed. So the settings files are read here and the
promotion is reported out loud:

    automation: confirm BUT Bash(ssh:*) is in permissions.allow —
    effective level is auto

The reverse is reported too: `auto` with no allow rule in place will
prompt on every call, which stalls a headless run rather than speeding
it up.

This mitigates; it does not guarantee. Permission state is per-machine
and the plugin does not own it.

Network-free and stdlib-only, like everything in `scripts/setup/`. It
reports configuration, not reachability — no host is contacted, and no
key value is ever printed.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS_ROOT = _HERE.parent
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from core.config_loader import get  # noqa: E402

#: The three levels, weakest first. Order is meaningful — `_higher_of`
#: compares by index.
LEVELS = ("manual", "confirm", "auto")

DEFAULT_LEVEL = "manual"

ENV_VAR = "ACADEMIC_RESEARCH_CLUSTER_AUTOMATION"

#: One-line descriptions, shared with `set_cluster_automation.py --list`
#: so the two scripts cannot describe the same level differently.
LEVEL_HELP = {
    "manual": (
        "emit and apply locally, print the remote commands, stop. "
        "The agent never runs ssh/scp/rsync/sbatch itself."
    ),
    "confirm": (
        "the agent runs them, and every call goes through the "
        "permission prompt. Works only where somebody is there to answer."
    ),
    "auto": (
        "the whole loop unattended. Needs allow rules you added "
        "yourself, and still hard-stops on a degenerate or mismatched run."
    ),
}

#: Commands whose approval gate is the only thing standing between the
#: agent and a shared facility account: they move data, spend an
#: allocation, or open a shell. An allow rule on any of them means
#: `confirm` no longer confirms anything.
GATED_COMMANDS = ("ssh", "scp", "rsync", "sbatch")

#: Read-only scheduler queries. An allow rule here is worth reporting —
#: it is how polling discipline stops being a decision — but it does not
#: promote the level, because nothing it can do costs an allocation.
QUERY_COMMANDS = ("squeue", "sacct", "seff")

#: `defaultMode` values under which nothing prompts at all. A session in
#: one of these is `auto` regardless of what any allow list says.
BYPASS_MODES = ("bypassPermissions",)


def settings_paths(project_dir: Path, home: Path) -> list[Path]:
    """Every settings file a permission rule can reach this run from.

    Ordered least- to most-specific, matching how Claude Code layers
    them. `settings.local.json` is listed because it is where "don't ask
    again" writes — omitting it would leave the detector blind to the
    exact click it exists to catch.
    """
    return [
        home / ".claude" / "settings.json",
        home / ".claude" / "settings.local.json",
        project_dir / ".claude" / "settings.json",
        project_dir / ".claude" / "settings.local.json",
    ]


def _rule_command(rule: str) -> str | None:
    """The command a `Bash(...)` permission rule matches, or None.

    Returns `"*"` for a rule that covers every command (`Bash`,
    `Bash(*)`, `Bash(:*)`), the command's base name otherwise. A
    non-Bash rule (`Read(...)`, `WebFetch`) returns None.
    """
    rule = rule.strip()
    if rule == "Bash":
        return "*"
    if not rule.startswith("Bash(") or not rule.endswith(")"):
        return None
    inner = rule[len("Bash("):-1].strip()
    if inner in ("*", ":*", ""):
        return "*"
    # `Bash(ssh:*)`, `Bash(ssh *)`, `Bash(/usr/bin/ssh -o Foo:*)` all
    # name their command first; everything after the first separator is
    # argument matching we do not need to understand.
    head = inner.replace(":", " ").split()[0] if inner.replace(":", " ").split() else ""
    if not head:
        return None
    return Path(head).name or None


def scan_permissions(paths: list[Path]) -> dict[str, object]:
    """Read the settings files and report what they allow.

    A file that does not exist, cannot be read, or does not parse is
    skipped rather than raised on: this is a diagnostic, and a broken
    settings file is a problem for the harness to report, not a reason
    to refuse to say anything about the cluster.
    """
    gated: list[tuple[str, Path]] = []
    queries: list[tuple[str, Path]] = []
    bypass: list[tuple[str, Path]] = []
    read = 0
    for path in paths:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict):
            continue
        read += 1
        perms = data.get("permissions")
        if not isinstance(perms, dict):
            continue
        mode = perms.get("defaultMode")
        if isinstance(mode, str) and mode in BYPASS_MODES:
            bypass.append((mode, path))
        allow = perms.get("allow")
        if not isinstance(allow, list):
            continue
        for rule in allow:
            if not isinstance(rule, str):
                continue
            cmd = _rule_command(rule)
            if cmd is None:
                continue
            if cmd == "*" or cmd in GATED_COMMANDS:
                gated.append((rule, path))
            elif cmd in QUERY_COMMANDS:
                queries.append((rule, path))
    return {
        "gated": gated,
        "queries": queries,
        "bypass": bypass,
        "files_read": read,
    }


def requested_level(
    flag: str | None,
    env_value: str | None,
    config_value: str,
) -> tuple[str, str, list[str]]:
    """`(level, source, warnings)` after applying the precedence chain.

    An unrecognised value in the environment or the config file is
    warned about and ignored rather than raised on. Falling back to
    `manual` is the safe direction: the cost of ignoring a typo'd `auto`
    is that the agent prints commands instead of running them.
    """
    warnings: list[str] = []
    if flag:
        return flag, "--automation flag", warnings

    env_value = (env_value or "").strip()
    if env_value:
        if env_value in LEVELS:
            return env_value, f"{ENV_VAR} environment variable", warnings
        warnings.append(
            f"{ENV_VAR}={env_value!r} is not one of "
            f"{'|'.join(LEVELS)} — ignoring it."
        )

    config_value = (config_value or "").strip()
    if config_value:
        if config_value in LEVELS:
            return config_value, "config.toml [cluster] automation", warnings
        warnings.append(
            f"config.toml [cluster] automation = {config_value!r} is not "
            f"one of {'|'.join(LEVELS)} — ignoring it."
        )

    return DEFAULT_LEVEL, "default (nothing configured)", warnings


def _fmt_rules(rules: list[tuple[str, Path]], home: Path) -> str:
    out = []
    for rule, path in rules:
        try:
            shown = "~/" + str(path.relative_to(home)).replace("\\", "/")
        except ValueError:
            shown = str(path)
        out.append(f"{rule} ({shown})")
    return ", ".join(out)


def effective_level(
    requested: str,
    perms: dict[str, object],
) -> tuple[str, list[str]]:
    """`(effective_level, notes)` — what will actually happen.

    Two directions, and both are surprises worth printing:

    - `confirm` with an allow rule in place confirms nothing. The user
      chose a level whose entire mechanism has been removed, usually by
      a single "don't ask again" click months ago.
    - `auto` with no allow rule prompts on every call. Interactively
      that is merely `confirm` under another name; headless, the run
      stalls on the first prompt and looks hung.

    `manual` is unaffected by permission state, because it never issues
    a gated command in the first place. That is precisely what makes it
    the safe default.
    """
    gated = perms["gated"]
    bypass = perms["bypass"]
    assert isinstance(gated, list) and isinstance(bypass, list)
    notes: list[str] = []

    if requested == "manual":
        if gated or bypass:
            notes.append(
                "Permission rules would allow remote commands, but "
                "`manual` does not issue any, so nothing changes."
            )
        return "manual", notes

    if bypass:
        mode, _ = bypass[0]
        notes.append(
            f"permissions.defaultMode is {mode!r} — nothing prompts in "
            f"this session, so the effective level is auto no matter "
            f"what is set here."
        )
        return "auto", notes

    if requested == "confirm" and gated:
        rule, _ = gated[0]
        notes.append(
            f"automation: confirm BUT {rule} is in permissions.allow — "
            f"effective level is auto. Nothing will prompt on a remote "
            f"call. Remove the rule to get the confirmation step back, "
            f"or set automation = \"auto\" so the config says what is "
            f"actually happening."
        )
        return "auto", notes

    if requested == "auto" and not gated:
        notes.append(
            "automation: auto BUT no allow rule covers "
            f"{'/'.join(GATED_COMMANDS)} — every remote call will prompt. "
            "Interactively that is `confirm`; in a headless session the "
            "run stops at the first prompt."
        )
        return "confirm", notes

    return requested, notes


def status_lines(
    requested: str,
    source: str,
    effective: str,
    perms: dict[str, object],
    home: Path,
) -> list[str]:
    """The status block, shared with `set_cluster_automation.py` so the
    two scripts cannot drift in what they report."""
    gated = perms["gated"]
    queries = perms["queries"]
    assert isinstance(gated, list) and isinstance(queries, list)
    lines = [
        f"automation: {requested}",
        f"source: {source}",
        f"effective: {effective}",
        f"allow_rules: {_fmt_rules(gated, home) if gated else 'none'}",
        f"query_rules: {_fmt_rules(queries, home) if queries else 'none'}",
        f"settings_files: {perms['files_read']} read",
    ]
    return lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--automation", choices=LEVELS, default=None,
        help="Level to assume for this run only, above the env var and "
             "the config file. Use it to ask what a level WOULD do, not "
             "to talk yourself into one.",
    )
    parser.add_argument(
        "--list", action="store_true",
        help="Describe the levels and exit.",
    )
    parser.add_argument(
        "--project-dir", default="",
        help="Project directory whose .claude/ settings are read "
             "(default: the current directory).",
    )
    args = parser.parse_args(argv)

    if args.list:
        print("Automation levels (default: manual):")
        for level in LEVELS:
            print(f"  {level:<8} {LEVEL_HELP[level]}")
        return 0

    home = Path.home()
    project_dir = Path(args.project_dir) if args.project_dir else Path.cwd()

    requested, source, warnings = requested_level(
        args.automation,
        # Read the two sources separately rather than through `get`'s
        # env-over-config precedence: the same value can arrive from
        # either place, and reporting WHICH one is most of the value of
        # this line — "I set it in config.toml and it is being ignored"
        # is the question the source line exists to answer.
        env_value=os.environ.get(ENV_VAR, ""),
        config_value=get("cluster", "automation"),
    )
    perms = scan_permissions(settings_paths(project_dir, home))
    effective, notes = effective_level(requested, perms)

    for line in status_lines(requested, source, effective, perms, home):
        print(line)
    for warning in warnings:
        print(f"WARNING: {warning}")
    for note in notes:
        print(f"NOTE: {note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
