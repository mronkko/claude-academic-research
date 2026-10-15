#!/usr/bin/env python3
"""Idempotently add deny rules to the project's .claude/settings.json.

Usage:
    python3 add_deny_rules.py RULE [RULE ...]

Each RULE is one entry to append to `permissions.deny` in
./.claude/settings.json if not already present. Creates the file
(and `.claude/` directory) when missing. Prints a summary of what
was added vs already present.

This is the one place the plugin writes a permission rule, and only
into the *project's* settings: `empirical-integrity` uses it to deny
edits to `analysis/results/**`, a data-integrity guard the user asked
for. The setup wizard writes no rules anywhere.

A settings.json that is not valid JSON, or whose `permissions` /
`deny` entries have the wrong shape, is reported and left untouched
(exit 3) — rewriting it would discard whatever the user had there.

Companion to `check_deny_rules.py` (read-only). Kept as a shipped
script rather than an inline `python -c` or shell heredoc: one
auditable, cross-platform command, so skills don't improvise
file-mutating code at load time.
"""
import json
import sys
from pathlib import Path

SETTINGS = Path(".claude") / "settings.json"
EXIT_MALFORMED = 3


def load_deny(settings: Path) -> tuple[dict, list]:
    """`(data, deny_list)` from `settings`; raises ValueError if malformed.

    `deny_list` is the live list inside `data`, created when absent.
    """
    if settings.is_file():
        try:
            data = json.loads(settings.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            raise ValueError(f"{settings} is not valid JSON ({e})") from None
    else:
        data = {}
    if not isinstance(data, dict):
        raise ValueError(f"{settings} does not hold a JSON object")
    perms = data.setdefault("permissions", {})
    if not isinstance(perms, dict):
        raise ValueError(f"{settings}: 'permissions' is not an object")
    deny = perms.setdefault("deny", [])
    if not isinstance(deny, list):
        raise ValueError(f"{settings}: 'permissions.deny' is not a list")
    return data, deny


def main(argv: list[str]) -> int:
    try:
        data, deny = load_deny(SETTINGS)
    except ValueError as e:
        print(f"error: {e}. Not modified; fix or move it, then re-run.", file=sys.stderr)
        return EXIT_MALFORMED

    added = []
    already = []
    for rule in argv:
        if rule in deny:
            already.append(rule)
        else:
            deny.append(rule)
            added.append(rule)

    if added:
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")

    if added:
        print("added: " + ", ".join(added))
    if already:
        print("already present: " + ", ".join(already))
    if not added and not already:
        print("ok (no rules requested)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
