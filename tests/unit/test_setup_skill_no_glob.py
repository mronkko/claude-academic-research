"""Regression guard: setup skill must not paste a shell glob (P12).

The user hit this in a real session — `~/.claude/plugins/cache/.../*/`
expands to multiple paths when two plugin versions are cached
side-by-side, and `python3 <multiple-paths>` aborts with "ambiguous
arguments". The fix replaced the glob with `${CLAUDE_PLUGIN_ROOT}`,
which Claude Code's harness substitutes to the active version's
absolute path before the model emits text.

This test pins that no shell glob ever sneaks back into a fenced
code block in the setup skill — ANY future edit must keep using
`${CLAUDE_PLUGIN_ROOT}`. Same guard for the runtime error path in
`config_loader.require()`.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SETUP_SKILL = REPO_ROOT / "skills" / "setup" / "SKILL.md"
CONFIG_LOADER = REPO_ROOT / "scripts" / "core" / "config_loader.py"


def _fenced_code_blocks(markdown: str) -> list[str]:
    """Return text inside ``` ``` fences. Skips inline code, since
    inline samples are usually descriptive, not paste-targets."""
    pattern = re.compile(r"```[a-zA-Z]*\n(.*?)```", re.DOTALL)
    return pattern.findall(markdown)


def test_setup_skill_paste_in_command_does_not_use_shell_glob() -> None:
    """The setup skill emits a paste-in shell command for the user.
    That command must not contain `*` — otherwise users with two
    plugin versions cached get an "ambiguous arguments" error."""
    content = SETUP_SKILL.read_text(encoding="utf-8")
    blocks = _fenced_code_blocks(content)
    assert blocks, "setup SKILL.md has no fenced code blocks — has the format changed?"
    for i, block in enumerate(blocks):
        # Allowed: regex / glob / etc inside an example string. Not
        # allowed: a literal `*` token in a path-like position. Heuristic:
        # if the block names `wizard.py`, then any `*` in it is broken.
        if "wizard.py" in block:
            assert "*" not in block, (
                f"Fenced code block #{i} in skills/setup/SKILL.md contains a "
                f"shell glob `*` next to wizard.py. Use ${{CLAUDE_PLUGIN_ROOT}} "
                f"instead — Claude Code resolves it to the active plugin "
                f"version's absolute path before the model emits text. The "
                f"glob breaks when two plugin versions are cached side-by-side.\n"
                f"Block was:\n{block}"
            )


def test_setup_skill_uses_claude_plugin_root_substitution() -> None:
    """Sanity-check that the skill DOES name the canonical substitution
    variable. If a future edit rewrites the wizard invocation but
    forgets to use ${CLAUDE_PLUGIN_ROOT}, this surfaces it."""
    content = SETUP_SKILL.read_text(encoding="utf-8")
    assert "${CLAUDE_PLUGIN_ROOT}" in content, (
        "skills/setup/SKILL.md no longer uses ${CLAUDE_PLUGIN_ROOT} for "
        "the wizard path. That substitution is the canonical pattern; "
        "anything else (literal hidden paths, shell globs) will break."
    )


def test_config_loader_error_path_is_glob_free() -> None:
    """`require()` raises a RuntimeError pointing at the wizard. That
    message used to embed the same `~/.claude/plugins/cache/.../*/`
    glob (P12). Replaced with `Path(__file__).resolve()...` so the
    path is single-valued by construction."""
    content = CONFIG_LOADER.read_text(encoding="utf-8")
    # Look for the legacy glob shape — that exact path fragment must
    # never appear (it's the antipattern this fix was designed to kill).
    assert "plugins/cache/mronkko/academic-research/*" not in content, (
        "scripts/core/config_loader.py still references the legacy glob "
        "wizard path. Use `Path(__file__).resolve().parent.parent / 'setup' / 'wizard.py'` "
        "instead so the error message names the running version, not a glob."
    )
