"""The academic-research MCP server's tools, declared as data.

Each `Tool` names the plugin script it runs, the parameters it exposes and
how each parameter becomes argv, so `server.py` can stay a generic
protocol loop and `tests/unit/test_mcp_tool_schema_sync.py` can check every
flag named here against the target script's argparse without running it.

Rules every tool follows (BACKLOG P21):

- The description starts with the effect class — `[read-only]`,
  `[writes project files]`, `[writes Zotero]` or `[costs LLM credits]` —
  because that, not the MCP annotations, is what a Claude Code user sees
  when deciding whether to "always allow" a tool.
- Parameters are the user's decisions only (which library, which
  collection, which layout). Tuning flags stay on the command line.
- Path parameters are confined to the project directory by the server.
- Summaries are small: counts parsed from the script's own output, never
  the output itself. The full output goes to a log file whose path is
  returned, so nothing is lost and nothing floods the context window.

Stdlib only, and imports nothing from `scripts/`: the server never loads
pipeline code into its own process.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

READ_ONLY = "read-only"
WRITES_PROJECT = "writes project files"
WRITES_ZOTERO = "writes Zotero"
COSTS_LLM = "costs LLM credits"
EFFECT_CLASSES = (READ_ONLY, WRITES_PROJECT, WRITES_ZOTERO, COSTS_LLM)

#: Placeholder in `fixed` tokens and parameter defaults, replaced with the
#: resolved project directory when argv is built.
PROJECT = "{project}"


@dataclass(frozen=True)
class Param:
    """One tool parameter and the argv it becomes.

    kind:
      - "option":     `[flag, value]`
      - "switch":     `[flag]` when true, nothing when false
      - "repeat":     `[flag, v]` for each string in a list
      - "positional": `[value]`; `flag` holds the argparse dest name
      - "map":        the tokens `choices[value]`
    """

    name: str
    description: str
    kind: str
    flag: str = ""
    type: str = "string"            # JSON-schema type of the value
    enum: tuple[str, ...] = ()
    choices: dict[str, tuple[str, ...]] = field(default_factory=dict)
    required: bool = False
    default: object = None          # applied when absent; may be PROJECT
    path: bool = False              # confine to the project directory
    pattern: str = ""               # regex the string value must match

    def schema(self) -> dict:
        if self.kind == "repeat":
            s: dict = {"type": "array", "items": {"type": "string"}}
        else:
            s = {"type": self.type}
        if self.enum:
            s["enum"] = list(self.enum)
        elif self.kind == "map":
            s["enum"] = list(self.choices)
        if self.pattern:
            s["pattern"] = self.pattern
        if self.default is not None and self.default != PROJECT:
            s["default"] = self.default
        s["description"] = self.description
        return s


@dataclass(frozen=True)
class Tool:
    name: str
    title: str
    effect: str
    description: str
    script: str                     # relative to the plugin root
    params: tuple[Param, ...] = ()
    fixed: tuple[str, ...] = ()     # argv tokens always passed
    timeout: int = 120
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = False        # talks to Zotero or the internet
    #: Pairs of parameter names that cannot both be set.
    exclusive: tuple[tuple[str, ...], ...] = ()
    #: Groups of parameter names of which exactly one must be set.
    one_of: tuple[tuple[str, ...], ...] = ()
    #: (combined output, exit code, project dir) -> (summary, artifacts)
    summarise: Callable[[str, int, Path], tuple[dict, list[str]]] | None = None
    #: Extra scripts the tool runs (checked to exist by the guard test).
    extra_scripts: tuple[str, ...] = ()

    def full_description(self) -> str:
        return f"[{self.effect}] {self.description}"

    def scripts(self) -> tuple[str, ...]:
        return (self.script, *self.extra_scripts)

    def input_schema(self) -> dict:
        return {
            "type": "object",
            "properties": {p.name: p.schema() for p in self.params},
            "required": [p.name for p in self.params if p.required],
            "additionalProperties": False,
        }

    def annotations(self) -> dict:
        return {
            "title": self.title,
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world,
        }

    def to_mcp(self) -> dict:
        return {
            "name": self.name,
            "title": self.title,
            "description": self.full_description(),
            "inputSchema": self.input_schema(),
            "annotations": self.annotations(),
        }


# ---------------------------------------------------------------------------
# Shared parameters
# ---------------------------------------------------------------------------

GROUP = Param(
    "group", "Zotero group library ID (numeric). Omit with `user` unset to "
    "use the script's default (ZOTERO_GROUP or the only group you have).",
    kind="option", flag="--group", pattern=r"^[0-9]+$",
)
USER = Param(
    "user", "Use your personal library (My Library) instead of a group.",
    kind="switch", flag="--user", type="boolean",
)
REMOTE = Param(
    "remote", "Read through api.zotero.org instead of Zotero desktop — use "
    "when items were just written via the Web API and desktop has not "
    "synced yet.",
    kind="switch", flag="--remote", type="boolean",
)
FILTER_KEYS = Param(
    "filter_keys_file", "Project-relative path to a file of Zotero item "
    "keys (one per line) to restrict the run to, e.g. an audit .keys file.",
    kind="option", flag="--filter-keys-file", path=True,
)
LIBRARY_ARGS = (GROUP, USER, REMOTE)


# ---------------------------------------------------------------------------
# Summarisers — conservative parsing of each script's human output.
# A line that does not match is ignored, never guessed at.
# ---------------------------------------------------------------------------

def _int(pattern: str, text: str) -> int | None:
    m = re.search(pattern, text, re.MULTILINE)
    return int(m.group(1)) if m else None


def _drop_none(d: dict) -> dict:
    return {k: v for k, v in d.items() if v is not None}


def _rel(path: Path, project: Path) -> str:
    try:
        return path.resolve().relative_to(project).as_posix()
    except ValueError:
        return str(path)


def summarise_scaffold(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    created_dirs, copied, kept, gitignore = [], [], [], []
    section = "dirs"
    for line in out.splitlines():
        if line.startswith("Copying templates"):
            section = "templates"
        elif line.startswith(".gitignore updated"):
            section = "gitignore"
        m = re.match(r"^  ([+=]) (\S.*?)(?: \(already exists; not overwritten\))?$", line)
        if not m:
            continue
        mark, name = m.groups()
        if section == "dirs" and mark == "+":
            created_dirs.append(name)
        elif section == "templates":
            (copied if mark == "+" else kept).append(name)
        elif section == "gitignore" and mark == "+":
            gitignore.append(name)
    summary = {
        "created_dirs": created_dirs,
        "copied_templates": copied,
        "kept_existing": len(kept),
        "gitignore_added": gitignore,
    }
    return summary, list(copied)


def summarise_pdf_report(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    buckets = {
        m.group(1): int(m.group(2))
        for m in re.finditer(r"^([a-z_]+)  \((\d+)\)$", out, re.MULTILINE)
    }
    summary = _drop_none({
        "items": _int(r"PDF run report — (\d+) items", out),
        "attached": _int(r"^\s+(\d+) with a PDF attached", out),
        "still_without": _int(r"(\d+) still without\s*$", out),
        "by_status": buckets or None,
    })
    if "no log rows found" in out:
        summary["items"] = 0
    return summary, []


def summarise_pdf_plan(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    # The plan's publisher queue is meant for a person; pass its tail
    # through log_tail and pull out only the lines whose shape is fixed.
    solve = re.search(
        r"solve a Cloudflare / SSO challenge for \d+ publishers?, in order: (.+)$",
        out, re.MULTILINE,
    )
    summary = _drop_none({
        "items_without_pdf": _int(r"^(\d+) items without real PDF", out),
        "solve_publishers": (
            [p.strip() for p in solve.group(1).split(",")] if solve else None
        ),
        "nothing_to_do": True if "Nothing to do via the browser path" in out else None,
    })
    return summary, []


def summarise_screening(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    decisions = {}
    for d in ("include", "borderline", "exclude", "error"):
        n = _int(rf"^\s+{d}\s+(\d+)$", out)
        if n is not None:
            decisions[d] = n
    summary = _drop_none({
        "rows_in_file": _int(r"Rows in file:\s+(\d+)", out),
        "unique_items": _int(r"Unique items:\s+(\d+)", out),
        "latest_decisions": decisions or None,
        "listed": _int(r"Items with latest decision = '[^']*' \((\d+)\)", out),
    })
    return summary, []


AUDIT_JSON = ".claude/audit/audit.json"


def summarise_audit(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    report_path = project / AUDIT_JSON
    summary: dict = {}
    artifacts: list[str] = []
    if rc == 0 and report_path.is_file():
        try:
            report = json.loads(report_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            report = {}
        for key, value in report.items():
            if isinstance(value, int) and not isinstance(value, bool):
                summary[key] = value
        artifacts.append(AUDIT_JSON)
        stem = report_path.with_suffix("")
        for keys_file in sorted(stem.parent.glob(stem.name + ".*.keys")):
            try:
                if keys_file.stat().st_size:
                    artifacts.append(_rel(keys_file, project))
            except OSError:
                continue
    return summary, artifacts


def summarise_export(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    summary = _drop_none({
        "tagged_items": _int(r"(\d+) item\(s\) carry tag", out),
        "rows_built": _int(r"Rows built: (\d+)", out),
        "rows_written": _int(r"^Wrote (\d+) rows", out),
        "incomplete_items": _int(r"ERROR: (\d+) item\(s\) tagged", out),
        "dry_run": True if "[DRY RUN]" in out else None,
    })
    artifacts = []
    m = re.search(r"^Wrote \d+ rows to (.+)$", out, re.MULTILINE)
    if m:
        artifacts.append(_rel(Path(m.group(1).strip()), project))
    return summary, artifacts


def summarise_bib(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    summary = _drop_none({
        "citation_keys": _int(r"Citation keys found in Markdown \((\d+)\)", out),
        "written": _int(r"Wrote (\d+) entr", out),
        "not_found": len(re.findall(r"not found in Zotero", out)) or None,
        "no_keys": True if "No citation keys found" in out else None,
    })
    artifacts = []
    m = re.search(r"Wrote \d+ entr(?:y|ies) → (.+)$", out, re.MULTILINE)
    if m:
        artifacts.append(_rel(Path(m.group(1).strip()), project))
    return summary, artifacts


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

CHECK_SETUP = Tool(
    name="check_setup",
    title="Check plugin setup",
    effect=READ_ONLY,
    description=(
        "Reports whether the plugin is configured (the setup wizard has "
        "written its config file) and which citation databases that config "
        "unlocks. Never shows keys. If it reports not configured, setup is "
        "done in a terminal with the wizard — do not ask for keys in chat."
    ),
    script="scripts/setup/check_configured.py",
    extra_scripts=("scripts/setup/check_database_access.py",),
)

SCAFFOLD_PROJECT = Tool(
    name="scaffold_project",
    title="Scaffold a project",
    effect=WRITES_PROJECT,
    description=(
        "Creates the standard project directories and copies the plugin's "
        "templates into the project. Never overwrites an existing file, so "
        "re-running is safe; adds .claude/ and output/ to .gitignore inside "
        "a git repo."
    ),
    script="scripts/setup/scaffold_project.py",
    params=(
        Param("kind", "`sr` for a full systematic-review layout, "
              "`manuscript` for a manuscript-only one.",
              kind="option", flag="--kind", enum=("sr", "manuscript"),
              default="sr"),
        Param("target", "Project-relative directory to scaffold. Default: "
              "the project root.", kind="option", flag="--target",
              path=True, default=PROJECT),
    ),
    read_only=False,
    summarise=summarise_scaffold,
)

ENRICH_PDFS_PLAN = Tool(
    name="enrich_pdfs_plan",
    title="Plan the browser PDF pass",
    effect=READ_ONLY,
    description=(
        "Dry plan of the browser PDF pass: classifies items without a PDF and "
        "lists the publisher queue, including which publishers will need an "
        "interactive Cloudflare / SSO solve. Fetches and attaches nothing. "
        "Queries the library link resolver (~2 s per uncached item), so a "
        "large uncached library can exceed this tool's 10-minute limit; run "
        "it from a terminal then."
    ),
    script="scripts/pipelines/enrich_pdfs.py",
    params=(
        Param("mode", "Which browser pass to plan: `browser` (direct "
              "publisher handlers + Connector fallback), `connector` "
              "(Zotero Connector only) or `all` (API cascade then browser; "
              "plans the browser half over every item).",
              kind="map", default="browser",
              choices={"browser": ("--sources", "browser"),
                       "connector": ("--sources", "connector"),
                       "all": ("--all",)}),
        *LIBRARY_ARGS,
        FILTER_KEYS,
        Param("publisher", "Restrict the plan to one publisher key.",
              kind="option", flag="--publisher"),
        Param("institution", "Follow only this institution's link-resolver "
              "routes (a label from the run banner, e.g. 'aalto'); "
              "repeatable. Maps to --library.",
              kind="repeat", flag="--library"),
    ),
    fixed=("--plan",),
    timeout=600,
    open_world=True,
    exclusive=(("group", "user"),),
    summarise=summarise_pdf_plan,
)

ENRICH_PDFS_REPORT = Tool(
    name="enrich_pdfs_report",
    title="PDF run report",
    effect=READ_ONLY,
    description=(
        "Summarises an existing PDF run log: how many items have a PDF, how "
        "many are still missing, grouped by cause, with the next lever for "
        "each cause. Reads the log only; no Zotero or network access. The "
        "full report (with citations) is in the returned log file."
    ),
    script="scripts/pipelines/enrich_pdfs.py",
    params=(
        Param("log_csv", "Project-relative PDF run log. Default: "
              "output/pdf_attach_log.csv.", kind="option", flag="--log-csv",
              path=True),
        FILTER_KEYS,
    ),
    fixed=("--report",),
    summarise=summarise_pdf_report,
)

SCREENING_REPORT = Tool(
    name="screening_report",
    title="Screening report",
    effect=READ_ONLY,
    description=(
        "Counts the latest decision per item (include / borderline / exclude "
        "/ error) in an abstract-screening or full-text-coding CSV log, "
        "last row wins. Optional listings go to the returned log file."
    ),
    script="scripts/pipelines/screening_report.py",
    params=(
        Param("log_csv", "Project-relative screening or coding log, e.g. "
              "screening/abstract_screening.csv.", kind="positional",
              flag="log_csv", path=True, required=True),
        Param("list_decision", "Also list every item whose latest decision "
              "is this one.", kind="option", flag="--list",
              enum=("include", "borderline", "exclude", "error")),
        Param("list_rescreened", "Also list items screened more than once.",
              kind="switch", flag="--list-rescreened", type="boolean"),
    ),
    summarise=summarise_screening,
)

AUDIT_LIBRARY = Tool(
    name="audit_library",
    title="Audit Zotero library",
    effect=READ_ONLY,
    description=(
        "Audits a Zotero library for items missing a PDF, abstract or DOI "
        "(plus empty PDF stubs, XML-recovered and preprint PDFs). Changes "
        "nothing in Zotero; writes its report and one .keys file per "
        f"category under {Path(AUDIT_JSON).parent.as_posix()}/ for the "
        "enrichment steps' filter_keys_file."
    ),
    script="scripts/pipelines/audit_zotero_library.py",
    params=LIBRARY_ARGS,
    timeout=300,
    open_world=True,
    exclusive=(("group", "user"),),
    one_of=(("group", "user"),),
    summarise=summarise_audit,
)

EXPORT_CODED_INCLUDES = Tool(
    name="export_coded_includes",
    title="Export coded includes",
    effect=WRITES_PROJECT,
    description=(
        "Writes the manuscript-facing CSV of this review's full-text "
        "includes, joining each item's coding note with its bibliographic "
        "data. Reads Zotero; changes nothing there. Fails (ok=false) if an "
        "included item has no readable coding note — those need re-coding."
    ),
    script="scripts/pipelines/export_coded_includes.py",
    params=(
        *LIBRARY_ARGS,
        Param("collection", "Zotero collection key of the review.",
              kind="option", flag="--collection", required=True,
              pattern=r"^[A-Z0-9]{8}$"),
        Param("out", "Project-relative output CSV.", kind="option",
              flag="--out", path=True,
              default="analysis/results/coded_papers.csv"),
        Param("tag_prefix", "This review's tag namespace, overriding "
              "TAG_PREFIX in screening_config.py.", kind="option",
              flag="--tag-prefix"),
        Param("dry_run", "Count only; write nothing.", kind="switch",
              flag="--dry-run", type="boolean"),
    ),
    read_only=False,
    open_world=True,
    exclusive=(("group", "user"),),
    summarise=summarise_export,
)

GENERATE_BIB = Tool(
    name="generate_bib",
    title="Generate references.bib",
    effect=WRITES_PROJECT,
    description=(
        "Scans the manuscript sources for @citation keys and writes "
        "references.bib from Zotero via Better BibTeX (Zotero desktop must "
        "be running). Replaces the existing references.bib."
    ),
    script="scripts/pipelines/generate_bib.py",
    params=(
        Param("chapters_dir", "Project-relative directory to scan. Default: "
              "manuscript/ (chapters/ for the older layout).",
              kind="option", flag="--chapters-dir", path=True),
        Param("bib_out", "Project-relative output path. Default: "
              "manuscript/references.bib.", kind="option", flag="--bib-out",
              path=True),
    ),
    fixed=(PROJECT,),
    read_only=False,
    destructive=True,
    open_world=True,
    summarise=summarise_bib,
)

TOOLS: tuple[Tool, ...] = (
    CHECK_SETUP,
    SCAFFOLD_PROJECT,
    ENRICH_PDFS_PLAN,
    ENRICH_PDFS_REPORT,
    SCREENING_REPORT,
    AUDIT_LIBRARY,
    EXPORT_CODED_INCLUDES,
    GENERATE_BIB,
)
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
