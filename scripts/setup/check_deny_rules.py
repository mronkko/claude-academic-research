#!/usr/bin/env python3
"""Check that the project's .claude/settings.json denies a set of rules.

Usage:
    python3 check_deny_rules.py RULE [RULE ...]

Each RULE is one entry expected under permissions.deny in
./.claude/settings.json. Prints 'ok' if every rule is present,
otherwise 'missing: <comma-separated>'. A missing settings.json is
treated as having no rules at all. A settings.json that cannot be
read as JSON, or has the wrong shape, prints 'error: <why>' and exits
3 rather than crashing with a traceback.

Kept as a shipped script rather than an inline `python -c`: one
auditable, cross-platform command.
"""
import json
import sys
from pathlib import Path

SETTINGS = Path(".claude") / "settings.json"
EXIT_MALFORMED = 3


def main(argv: list[str]) -> int:
    deny: list | None = []
    if SETTINGS.is_file():
        try:
            data = json.loads(SETTINGS.read_text(encoding="utf-8-sig"))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            print(f"error: {SETTINGS} is not valid JSON ({e})")
            return EXIT_MALFORMED
        perms = data.get("permissions", {}) if isinstance(data, dict) else None
        deny = perms.get("deny", []) if isinstance(perms, dict) else None
        if not isinstance(deny, list):
            print(f"error: {SETTINGS} has no usable permissions.deny list")
            return EXIT_MALFORMED
    missing = [rule for rule in argv if rule not in deny]
    print("ok" if not missing else "missing: " + ", ".join(missing))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
