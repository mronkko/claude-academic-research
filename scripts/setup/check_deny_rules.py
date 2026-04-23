#!/usr/bin/env python3
"""Check that the project's .claude/settings.json denies a set of rules.

Usage:
    python3 check_deny_rules.py RULE [RULE ...]

Each RULE is one entry expected under permissions.deny in
./.claude/settings.json. Prints 'ok' if every rule is present,
otherwise 'missing: <comma-separated>'. A missing settings.json is
treated as having no rules at all.

Kept as a script (not an inline `python -c`) so the wizard's existing
`Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)` allow rule covers it.
"""
import json
import sys
from pathlib import Path

settings = Path(".claude/settings.json")
data = json.loads(settings.read_text()) if settings.is_file() else {}
deny = data.get("permissions", {}).get("deny", [])
missing = [rule for rule in sys.argv[1:] if rule not in deny]
print("ok" if not missing else "missing: " + ", ".join(missing))
