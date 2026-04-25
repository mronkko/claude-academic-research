"""Regression guard: critic-loop revision history must stay discoverable.

The critic-loop skill writes per-iteration revision history (rendered
snapshots, four critic outputs, adjudication decisions) to a
project-local directory. Originally these landed under
`.claude/critic-loop/`, which is hidden by default on macOS / Linux
finders and easy for non-technical co-authors to miss. The
`critic-reviews/` directory is the visible replacement.

This test pins the location so a future edit doesn't silently move
the reports back into a hidden directory. If the directory name needs
to change, update both the skill prose and this test deliberately.
"""

from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CRITIC_LOOP_SKILL = REPO_ROOT / "skills" / "critic-loop" / "SKILL.md"
FACT_CHECK_SKILL = REPO_ROOT / "skills" / "fact-check" / "SKILL.md"


def test_critic_loop_skill_writes_to_visible_critic_reviews_dir() -> None:
    text = CRITIC_LOOP_SKILL.read_text(encoding="utf-8")
    # Old hidden path must not appear anywhere in the skill prose.
    assert ".claude/critic-loop" not in text, (
        "skills/critic-loop/SKILL.md still references the hidden "
        "`.claude/critic-loop/` path. critic-loop reports were "
        "deliberately moved to the visible `critic-reviews/` directory "
        "so non-technical co-authors can find them in Finder / Explorer."
    )
    # Visible path must be present at least once (sanity-check that the
    # skill still names a working directory at all).
    assert "critic-reviews/" in text, (
        "skills/critic-loop/SKILL.md no longer references the visible "
        "`critic-reviews/` directory. The skill must name where its "
        "iteration reports are written so reviewers can find them."
    )


def test_fact_check_skill_uses_visible_path_for_critic_loop_pointer() -> None:
    """fact-check's `critic-loop in progress?` heuristic checks for
    iteration directories. Must match the new visible path."""
    text = FACT_CHECK_SKILL.read_text(encoding="utf-8")
    assert ".claude/critic-loop" not in text, (
        "fact-check skill still references the legacy "
        "`.claude/critic-loop/` path when describing how to detect a "
        "critic-loop in progress."
    )
