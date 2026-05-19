#!/usr/bin/env python3
"""Idempotently add deny rules to the project's .claude/settings.json.

Usage:
    python3 add_deny_rules.py RULE [RULE ...]

Each RULE is one entry to append to `permissions.deny` in
./.claude/settings.json if not already present. Creates the file
(and `.claude/` directory) when missing. Prints a summary of what
was added vs already present.

Companion to `check_deny_rules.py` (read-only). Kept as a shipped
script (not an inline `python -c` or shell heredoc) so the wizard's
existing `Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)` allow rule
covers it — and so skills don't have to improvise file-mutating
code at load time.
"""
import json
import sys
from pathlib import Path

settings = Path(".claude/settings.json")
settings.parent.mkdir(parents=True, exist_ok=True)
data = json.loads(settings.read_text()) if settings.is_file() else {}
deny = data.setdefault("permissions", {}).setdefault("deny", [])

added = []
already = []
for rule in sys.argv[1:]:
    if rule in deny:
        already.append(rule)
    else:
        deny.append(rule)
        added.append(rule)

settings.write_text(json.dumps(data, indent=2) + "\n")

if added:
    print("added: " + ", ".join(added))
if already:
    print("already present: " + ", ".join(already))
if not added and not already:
    print("ok (no rules requested)")
