"""Skills call the plugin's MCP tools, not hand-composed commands (P21 3c).

The plugin's own MCP server (`mcp/server.py`) runs every pipeline step a
skill needs as a tool. Skills therefore name tools, and give the shipped
script only as a *terminal equivalent* — the fallback when the tools are
not available, and what a user can paste into their own terminal. Three
things keep that honest:

- every tool name a skill mentions is one the server offers, so a renamed
  or misspelt tool cannot leave a skill pointing at nothing (this
  generalises the retrieving-pdfs check in test_retrieval_doctrine_sync);
- a skill that names a tool says, once, where to find it — the bare name
  differs from the host's full name — and what to do without it;
- no skill hands the agent a script invocation that a tool covers unless
  it is marked as the terminal equivalent. A mode no tool exposes
  (`TOOLLESS_FLAGS`) is a legitimate Bash invocation.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MCP_DIR = ROOT / "mcp"
if str(MCP_DIR) not in sys.path:
    sys.path.insert(0, str(MCP_DIR))

import tool_registry as reg  # noqa: E402

SKILLS_DIR = ROOT / "skills"
SKILL_FILES = sorted(SKILLS_DIR.glob("*/SKILL.md"))
GLOSSARY = SKILLS_DIR / "_glossary.md"
ALL_FILES = (*SKILL_FILES, GLOSSARY)

TOOL_NAMES = set(reg.TOOLS_BY_NAME)

#: A backticked identifier shaped like one of the server's tool names.
#: Broad on purpose — `start_abstract_screening` must be caught as a
#: typo, not skipped as "not a tool".
_TOOLISH = re.compile(
    r"`((?:start|job|pdf|enrich_pdfs|cancel|list)_[a-z_]+"
    r"|check_setup|scaffold_project|audit_library|generate_bib"
    r"|export_coded_includes|screening_report)`"
)
#: Backticked names of that shape that are not tools: tool parameters
#: (`list_decision`), job fields, and a few vocabulary words.
NOT_TOOLS = {p.name for t in reg.TOOLS for p in t.params} | {
    "job_id",                   # what a start_* tool returns
    "pdf_fetch_log",            # a module (the failure-cause vocabulary)
    "pdf_unreadable",           # a cluster-screening skip reason
}

#: Flags selecting a script mode no tool exposes. An invocation carrying
#: one is a legitimate Bash command even though the script has a tool.
TOOLLESS_FLAGS = (
    "--replace",                # enrich_pdfs: library repair (zotero-operations)
    "--control-file",           # the attended browser pass, driven over Bash
    "--update-fields",          # fulltext_code: merge named fields only
    "--emit-manifest",          # cluster-screening round trip
    "--apply-responses",
    "--keyword-databases",      # search.py per-stream database selection
    "--citation-databases",
    "--citation-journal-scope",
)

_INVOCATION = re.compile(
    r"(?:uv run|python3?|py)\s+\"?\$\{CLAUDE_PLUGIN_ROOT\}/scripts/"
    r"(?:[\w-]+/)*([\w-]+\.py)\"?"
)
_MARK = "terminal equivalent"


def _tool_scripts() -> dict[str, set[str]]:
    by_script: dict[str, set[str]] = {}
    for tool in reg.TOOLS:
        for script in tool.scripts():
            by_script.setdefault(Path(script).name, set()).add(tool.name)
    return by_script


TOOL_SCRIPTS = _tool_scripts()


def _blocks(text: str) -> list[tuple[str, str]]:
    """(block, context) pairs: a prose paragraph, a table row, or a fenced
    code block. The context is what may carry the terminal-equivalent
    mark — the paragraph itself; a row plus its table's header; a fence
    plus the paragraph just before it."""
    out: list[tuple[str, str]] = []
    lines = text.splitlines()
    i = 0
    last_para = ""
    header = ""
    while i < len(lines):
        line = lines[i]
        if line.lstrip().startswith("```"):
            j = i + 1
            while j < len(lines) and not lines[j].lstrip().startswith("```"):
                j += 1
            body = "\n".join(lines[i + 1:j])
            out.append((body, last_para + "\n" + body))
            i = j + 1
            continue
        if line.lstrip().startswith("|"):
            if not header:
                header = line
            out.append((line, header + "\n" + line))
            i += 1
            continue
        header = ""
        if not line.strip():
            i += 1
            continue
        j = i
        para: list[str] = []
        while (j < len(lines) and lines[j].strip()
               and not lines[j].lstrip().startswith(("```", "|"))):
            para.append(lines[j])
            j += 1
        last_para = "\n".join(para)
        out.append((last_para, last_para))
        i = j
    return out


def _command_after(block: str, end: int) -> str:
    """The rest of one command: to the closing backtick, the table cell's
    end, or the first line break not continued with a backslash."""
    rest = block[end:]
    m = re.search(r"`|\||(?<!\\)\n", rest)
    return rest[: m.start()] if m else rest


def _uncovered_invocations(path: Path) -> list[str]:
    bad = []
    for block, context in _blocks(path.read_text(encoding="utf-8")):
        for m in _INVOCATION.finditer(block):
            script = m.group(1)
            tools = TOOL_SCRIPTS.get(script)
            if not tools:
                continue                   # no tool runs this script
            command = _command_after(block, m.end())
            if any(flag in command for flag in TOOLLESS_FLAGS):
                continue                   # a mode no tool exposes
            if _MARK not in " ".join(context.lower().split()):
                bad.append(f"{script} (tools: {sorted(tools)}): "
                           f"{block.strip()[:120]!r}")
                continue
            if block.lstrip().startswith("|") and not _TOOLISH.search(block):
                bad.append(f"{script}: a terminal-equivalent row that names "
                           f"no tool: {block.strip()[:120]!r}")
    return bad


# ---------------------------------------------------------------------------


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.parent.name
                         if p.name == "SKILL.md" else p.name)
def test_every_tool_a_skill_names_exists(path: Path) -> None:
    named = set(_TOOLISH.findall(path.read_text(encoding="utf-8"))) - NOT_TOOLS
    unknown = sorted(named - TOOL_NAMES)
    assert not unknown, (
        f"{path.relative_to(ROOT)} names tools the MCP server does not offer: "
        f"{unknown} (mcp/tool_registry.py is the list)"
    )


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.parent.name)
def test_a_skill_naming_tools_says_where_to_find_them(path: Path) -> None:
    text = path.read_text(encoding="utf-8")
    named = set(_TOOLISH.findall(text)) - NOT_TOOLS
    if not named:
        return
    assert "Plugin tools" in text, (
        f"{path.relative_to(ROOT)} names {sorted(named)[:3]}… but has no "
        "'Plugin tools' note saying they belong to the academic-research "
        "MCP server and what to do when they are missing"
    )
    assert "academic-research" in text and "MCP server" in text
    assert _MARK in " ".join(text.lower().split()), (
        "no terminal-equivalent fallback named")


@pytest.mark.parametrize("path", ALL_FILES, ids=lambda p: p.parent.name
                         if p.name == "SKILL.md" else p.name)
def test_no_skill_scripts_a_step_a_tool_covers(path: Path) -> None:
    bad = _uncovered_invocations(path)
    assert not bad, (
        f"{path.relative_to(ROOT)} gives a script command where a tool "
        "exists. Call the tool, and keep the command only as the "
        "\"terminal equivalent\" (or add the mode's flag to TOOLLESS_FLAGS "
        "if no tool exposes it):\n  " + "\n  ".join(bad)
    )


def test_the_glossary_defines_the_tool_terms() -> None:
    text = GLOSSARY.read_text(encoding="utf-8")
    for term in ("**Plugin tools**", "**Job**", "**Terminal equivalent**"):
        assert term in text, term
    assert "mcp__plugin_academic-research_academic-research__" in text
    assert "never answer it yourself" in text


def test_the_guard_sees_an_uncovered_invocation(tmp_path: Path) -> None:
    """The scanner itself: an unmarked tool-covered command fails, the
    marked one and a tool-less mode pass."""
    sample = tmp_path / "SKILL.md"
    sample.write_text(
        "Run it:\n\n```bash\nuv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/"
        "audit_zotero_library.py --group 1\n```\n\n"
        "Call `audit_library` (terminal equivalent: `uv run "
        "${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/audit_zotero_library.py`).\n\n"
        "`uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py "
        "--replace --filter-keys-file k`\n\n"
        "| Step | Tool | Terminal equivalent |\n|---|---|---|\n"
        "| Audit | — | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/"
        "audit_zotero_library.py` |\n",
        encoding="utf-8",
    )
    bad = _uncovered_invocations(sample)
    assert len(bad) == 2, bad
    assert "Run it" not in bad[0] and "--group 1" in bad[0]
    assert "names no tool" in bad[1]


def test_toolless_flags_are_not_tool_arguments() -> None:
    """A flag listed as tool-less must really be absent from every tool's
    argv — otherwise the allow-list would hide a command the tool covers."""
    emitted = set()
    for tool in reg.TOOLS:
        emitted.update(t for t in tool.fixed if t.startswith("--"))
        for p in tool.params:
            if p.flag.startswith("--"):
                emitted.add(p.flag)
            for choice in p.choices.values():
                emitted.update(t for t in choice if t.startswith("--"))
    assert not emitted & set(TOOLLESS_FLAGS), emitted & set(TOOLLESS_FLAGS)
