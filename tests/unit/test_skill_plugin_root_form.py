"""Skills name plugin paths one way: `${CLAUDE_PLUGIN_ROOT}/...`.

They used to write `${CLAUDE_PLUGIN_ROOT:-.}`, and the glossary
recommended it. The `.` fallback is the user's project directory, which
has no `scripts/setup/` or `scripts/pipelines/`, so on any host that does
not set the variable the fallback turned a clear "variable not set" into
a misleading "file not found" inside the user's own project.
"""

from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
ROOTS = (REPO / "skills", REPO / "editorial-tools" / "skills", REPO / "templates")


def test_no_dot_fallback_for_plugin_root() -> None:
    offenders = [
        str(p.relative_to(REPO))
        for root in ROOTS
        for p in root.rglob("*")
        if p.is_file()
        and p.suffix in {".md", ".py", ".qmd"}
        and "CLAUDE_PLUGIN_ROOT:-" in p.read_text(encoding="utf-8")
    ]
    assert not offenders, (
        "use ${CLAUDE_PLUGIN_ROOT} without a ':-' fallback in: " + ", ".join(offenders)
    )
