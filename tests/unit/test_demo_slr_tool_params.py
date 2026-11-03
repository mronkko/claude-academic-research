"""The parameters `skills/demo-slr/SKILL.md` passes exist on the tools it names.

The live run of 2026-11-03 found the demo calling `start_import` with a keys
file the tool could not take, so it fell back to the terminal. This pins the
contract: each (tool, parameter) pair below is both used in the skill text and
declared by the registry.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "mcp"))

import tool_registry as reg  # noqa: E402

SKILL = (REPO / "skills" / "demo-slr" / "SKILL.md").read_text(encoding="utf-8")

USED = {
    "start_search": ["max_records", "output_dir", "metadata_dir", "config",
                     "databases", "streams"],
    "start_import": ["created_keys_out", "collection", "input", "config", "user"],
    "start_enrich_abstracts": ["filter_keys_file", "user"],
    "start_abstract_screen": ["user", "model", "dry_run", "output"],
    "start_enrich_pdfs": ["user", "filter_keys_file"],
    "pdf_plan": ["lookups"],
    "start_fulltext_code": ["user", "only_keys", "limit", "model", "dry_run",
                            "output"],
    "export_coded_includes": ["user"],
    "highlight_quotes": ["user", "dry_run"],
    "demo_cleanup": ["user", "dry_run", "tag_prefix", "collection", "keys_file"],
}


@pytest.mark.parametrize(("tool", "param"), [
    (t, p) for t, ps in USED.items() for p in ps
])
def test_the_tool_declares_the_parameter_the_skill_passes(tool, param):
    declared = {p.name for p in reg.TOOLS_BY_NAME[tool].params}
    assert param in declared, f"{tool} has no parameter {param}"
    assert re.search(rf"\b{re.escape(param)}\b", SKILL), (
        f"demo-slr no longer mentions {param}; update USED")


def test_the_skill_requires_printing_what_the_presenter_is_asked_to_judge():
    """A question dialog shows one line; the live run asked to approve a
    recipe it had not printed (2026-11-03)."""
    assert "Show first, then ask" in SKILL
    assert "Print the whole proposal as text first" in SKILL
    assert "Print the key lines of both files" in SKILL
