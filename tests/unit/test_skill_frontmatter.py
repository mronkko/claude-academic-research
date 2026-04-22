"""Every SKILL.md must have valid YAML frontmatter with `name` and `description`."""

from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SKILLS_DIR = REPO / "skills"

EXPECTED_SKILLS = {
    "grounded-citations",
    "empirical-integrity",
    "manuscript-revision",
    "systematic-review",
    "zotero-operations",
    "fact-check",
    "critic-loop",
    "setup",
}

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = FRONTMATTER_RE.match(text)
    assert m, "SKILL.md must begin with --- YAML frontmatter ---"
    body = m.group(1)
    out: dict[str, str] = {}
    current_key: str | None = None
    for line in body.splitlines():
        if not line.strip():
            continue
        # Very simple YAML: `key: value` at column 0; multi-line values
        # start with whitespace on continuation.
        if re.match(r"^[a-zA-Z_][\w-]*:", line):
            key, _, value = line.partition(":")
            out[key.strip()] = value.strip()
            current_key = key.strip()
        elif current_key and line.startswith(" "):
            out[current_key] += " " + line.strip()
    return out


def test_all_expected_skills_present() -> None:
    found = {p.parent.name for p in SKILLS_DIR.glob("*/SKILL.md")}
    missing = EXPECTED_SKILLS - found
    assert not missing, f"missing SKILL.md files for: {sorted(missing)}"


def test_skill_frontmatter_required_fields() -> None:
    for skill_md in SKILLS_DIR.glob("*/SKILL.md"):
        fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        assert fm.get("name"), f"{skill_md.parent.name}: missing `name` in frontmatter"
        assert fm["name"] == skill_md.parent.name, (
            f"{skill_md.parent.name}: frontmatter name '{fm['name']}' "
            f"does not match directory name '{skill_md.parent.name}'"
        )
        description = fm.get("description", "")
        assert len(description) > 60, (
            f"{skill_md.parent.name}: description must be at least 60 chars "
            f"(got {len(description)}: '{description[:60]}...') — descriptions "
            f"drive skill triggering, so keep them specific"
        )


def test_skill_descriptions_unique() -> None:
    seen: dict[str, str] = {}
    for skill_md in SKILLS_DIR.glob("*/SKILL.md"):
        fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8"))
        desc = fm["description"]
        assert desc not in seen, f"duplicate description in {skill_md.parent.name} and {seen[desc]}"
        seen[desc] = skill_md.parent.name
