"""Every SKILL.md, in both plugins, has valid YAML frontmatter whose
`description` follows the contract in CLAUDE.md.

The frontmatter is parsed with a real YAML parser. The hand-rolled
`key: value` reader this replaced accepted `verifying-citations`'
description, which a YAML loader rejects (a `: ` inside a plain scalar),
so a host with a strict loader could drop the skill while CI stayed
green. It also only looked at `skills/`, never at `editorial-tools/`.

The description contract (CLAUDE.md, "Plugin surface"):
"Use when …" + `Trigger phrases …` + a "Do NOT use for X — use Y" rule
naming the skill to use instead, kept to at most 500 characters. The
description is what the harness matches on, so a broken shape makes the
wrong skill fire.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import yaml

REPO = Path(__file__).resolve().parents[2]
SKILL_ROOTS = (REPO / "skills", REPO / "editorial-tools" / "skills")

EXPECTED_SKILLS = {
    "grounded-citations",
    "empirical-integrity",
    "manuscript-revision",
    "academic-style",
    "systematic-review",
    "zotero-operations",
    "fact-check",
    "critic-loop",
    "setup",
    "cluster-screening",
    "verifying-citations",
    "suggesting-reviewers",
}

MAX_DESCRIPTION_CHARS = 500

FRONTMATTER_RE = re.compile(r"\A---\n(.*?)\n---\n", re.DOTALL)
# A `Do NOT use …` clause runs to the next `Do NOT use` or the end; it
# must name at least one alternative skill in backticks.
DO_NOT_CLAUSE_RE = re.compile(r"Do NOT use\b(.*?)(?=Do NOT use\b|\Z)", re.DOTALL)
SKILL_REF_RE = re.compile(r"`(?:academic-research:)?/?[a-z][a-z-]+`")


def _skill_files() -> list[Path]:
    return sorted(p for root in SKILL_ROOTS for p in root.glob("*/SKILL.md"))


def _frontmatter(path: Path) -> dict:
    m = FRONTMATTER_RE.match(path.read_text(encoding="utf-8"))
    assert m, f"{path}: SKILL.md must begin with --- YAML frontmatter ---"
    data = yaml.safe_load(m.group(1))
    assert isinstance(data, dict), f"{path}: frontmatter is not a YAML mapping"
    return data


SKILLS = _skill_files()
IDS = [p.parent.name for p in SKILLS]


def test_all_expected_skills_present() -> None:
    found = set(IDS)
    missing = EXPECTED_SKILLS - found
    assert not missing, f"missing SKILL.md files for: {sorted(missing)}"
    unexpected = found - EXPECTED_SKILLS
    assert not unexpected, (
        f"new skill(s) {sorted(unexpected)}: add them to EXPECTED_SKILLS and to "
        "the README's skill table"
    )


@pytest.mark.parametrize("skill_md", SKILLS, ids=IDS)
def test_frontmatter_is_valid_yaml_with_name(skill_md: Path) -> None:
    fm = _frontmatter(skill_md)
    assert fm.get("name") == skill_md.parent.name, (
        f"{skill_md.parent.name}: frontmatter name {fm.get('name')!r} does not "
        "match the directory name"
    )
    assert isinstance(fm.get("description"), str), (
        f"{skill_md.parent.name}: `description` must be a YAML string"
    )


@pytest.mark.parametrize("skill_md", SKILLS, ids=IDS)
def test_description_follows_the_contract(skill_md: Path) -> None:
    name = skill_md.parent.name
    desc = _frontmatter(skill_md)["description"].strip()
    assert desc.startswith("Use when") or desc.startswith("Use only when"), (
        f"{name}: description must start with 'Use when …'"
    )
    assert "Trigger phrases" in desc, f"{name}: description lacks 'Trigger phrases …'"
    clauses = DO_NOT_CLAUSE_RE.findall(desc)
    assert clauses, f"{name}: description lacks a 'Do NOT use … — use `other-skill`' rule"
    for clause in clauses:
        assert SKILL_REF_RE.search(clause), (
            f"{name}: 'Do NOT use{clause.strip()[:60]}…' names no skill to use "
            "instead (in backticks)"
        )
    assert len(desc) <= MAX_DESCRIPTION_CHARS, (
        f"{name}: description is {len(desc)} chars (max {MAX_DESCRIPTION_CHARS}). "
        "Long descriptions turn into workflow summaries the model follows "
        "instead of reading the skill body."
    )


def test_skill_descriptions_unique() -> None:
    seen: dict[str, str] = {}
    for skill_md in SKILLS:
        desc = _frontmatter(skill_md)["description"]
        assert desc not in seen, (
            f"duplicate description in {skill_md.parent.name} and {seen[desc]}"
        )
        seen[desc] = skill_md.parent.name
