"""The academic-research MCP server's tools, declared as data.

Each `Tool` names the plugin script it runs, the parameters it exposes and
how each parameter becomes argv, so `server.py` can stay a generic
protocol loop and `tests/unit/test_mcp_tool_schema_sync.py` can check every
flag named here against the target script's argparse without running it.

Rules every tool follows (BACKLOG P21):

- The description starts with the effect class — `[read-only]`,
  `[writes project files]`, `[writes user config]`, `[writes Zotero]` or
  `[costs LLM credits]` —
  because that, not the MCP annotations, is what a Claude Code user sees
  when deciding whether to "always allow" a tool.
- Parameters are the user's decisions only (which library, which
  collection, which layout). Tuning flags stay on the command line.
- Path parameters are confined to the project directory by the server.
- Summaries are small: counts parsed from the script's own output, never
  the output itself. The full output goes to a log file whose path is
  returned, so nothing is lost and nothing floods the context window.
- Long runs are jobs (`job=True`): the server starts them detached and
  appends `--progress-json` (and `--control-file` when `control=True`)
  to the script's argv, so every job script must accept those flags —
  the schema-sync test checks it. `job_status` and friends are
  `internal` tools the server answers itself, with no script.

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
#: `~/.config/academic-research/config.toml` — outside the project, and
#: shared by every project, so it is said separately from project files.
WRITES_USER_CONFIG = "writes user config"
WRITES_ZOTERO = "writes Zotero"
COSTS_LLM = "costs LLM credits"
EFFECT_CLASSES = (READ_ONLY, WRITES_PROJECT, WRITES_USER_CONFIG, WRITES_ZOTERO,
                  COSTS_LLM)

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
    #: A job: started detached, returns a job_id at once (see job_runner).
    #: The server appends `--progress-json` to the script's argv.
    job: bool = False
    #: The job's script can ask the user a question through
    #: `--control-file`; the server appends it and `job_answer` replies.
    control: bool = False
    #: Project-relative files a job writes, reported by `job_status` once
    #: they exist and are newer than the job's start.
    artifacts: tuple[str, ...] = ()
    #: A server-side tool (job_status, …) with no script of its own.
    internal: bool = False

    def full_description(self) -> str:
        return f"[{self.effect}] {self.description}"

    def scripts(self) -> tuple[str, ...]:
        if self.internal:
            return ()
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


def summarise_pdf_manual(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    """The fixed `MANUAL: key=value …` line `manual_pdfs` prints, plus the
    names of files waiting for the user to say which item they are."""
    m = re.search(r"^MANUAL: (.+)$", out, re.MULTILINE)
    summary: dict = {}
    if m:
        for pair in m.group(1).split():
            key, _, value = pair.partition("=")
            summary[key] = int(value) if value.isdigit() else value
    unmatched = re.findall(r"^    (\S.*?\.pdf): ", out, re.MULTILINE)
    if unmatched:
        summary["unmatched_files"] = unmatched
    artifacts = []
    if summary.get("unmatched"):
        artifacts.append("output/pdf_staging/unmatched.json")
    return summary, artifacts


PLAN_JSON = ".claude/pdf/plan.json"
STRATEGY_TOML = ".claude/pdf/strategy.toml"


def _key_values(line: str) -> dict:
    out: dict = {}
    for pair in line.split():
        key, _, value = pair.partition("=")
        out[key] = int(value) if value.isdigit() else value
    return out


def summarise_retrieval_plan(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    """The fixed `PLAN: key=value …` line `pdf_plan.format_summary` ends
    with, regrouped, plus the plan's publisher and paid sections from
    plan.json (both small). The per-item rows stay in the file."""
    m = re.search(r"^PLAN: (.+)$", out, re.MULTILINE)
    if not m:
        return {}, []
    flat = _key_values(m.group(1))
    summary: dict = {
        "routes": {k[len("route_"):]: v for k, v in flat.items()
                   if k.startswith("route_")},
        "fallback_routes": {k[len("fallback_"):]: v for k, v in flat.items()
                            if k.startswith("fallback_")},
        **{k: v for k, v in flat.items()
           if not k.startswith(("route_", "fallback_"))},
    }
    artifacts: list[str] = []
    path = project / PLAN_JSON
    if rc == 0 and path.is_file():
        try:
            plan = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            plan = {}
        for key in ("browser_publishers", "consent", "consent_counts",
                    "paid_openalex", "policy", "audit", "unknown", "manual_queue"):
            if key in plan:
                summary[key] = plan[key]
        artifacts.append(PLAN_JSON)
        artifacts += sorted((plan.get("keys_files") or {}).values())
    return summary, artifacts


def summarise_strategy(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    """The `STRATEGY_JSON: {…}` line `pdf_strategy.py` prints last."""
    lines = re.findall(r"^STRATEGY_JSON: (.+)$", out, re.MULTILINE)
    if not lines:
        return {}, []
    try:
        summary = json.loads(lines[-1])
    except ValueError:
        return {}, []
    artifacts = [STRATEGY_TOML] if (project / STRATEGY_TOML).is_file() else []
    return summary, artifacts


def summarise_policy_write(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    written = re.findall(r"^  \S+: (PDF_[A-Z_]+ = .+)$", out, re.MULTILINE)
    return ({"written": written} if written else {}), []


# ---------------------------------------------------------------------------
# The tools
# ---------------------------------------------------------------------------

CHECK_SETUP = Tool(
    name="check_setup",
    title="Check plugin setup",
    effect=READ_ONLY,
    description=(
        "Reports whether the plugin is configured (the setup wizard has "
        "written its config file), which citation databases that config "
        "unlocks, and the zotero-mcp-server version the MCP configuration "
        "launches, with a warning when it is below the plugin's floor. "
        "Never shows keys. If it reports not configured, setup is "
        "done in a terminal with the wizard — do not ask for keys in chat."
    ),
    script="scripts/setup/check_configured.py",
    extra_scripts=("scripts/setup/check_database_access.py",
                   "scripts/setup/check_zotero_mcp_version.py"),
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
        "Dry plan of the browser PDF pass only — run it just before that "
        "step; for the overall retrieval plan use pdf_plan. Classifies items "
        "without a PDF and lists the publisher queue, including which "
        "publishers will need an interactive Cloudflare / SSO solve, and how "
        "many go to EBSCOhost or the Connector. Fetches and attaches nothing. "
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

PDF_PLAN = Tool(
    name="pdf_plan",
    title="Plan PDF retrieval",
    effect=READ_ONLY,
    description=(
        "The planning step of PDF retrieval: for every item without a PDF, "
        "the route most likely to get it — API cascade, paid OpenAlex (with "
        "the cost), a publisher browser handler, the library route to "
        "EBSCOhost or the Zotero Connector, or manual download — plus where "
        "each would go if the API sources fail it, items below the [pdf] "
        "policy and audit flags. Never writes to Zotero; writes "
        ".claude/pdf/plan.json and a keys file per route. lookups=none uses "
        "caches only (seconds); lookups=openalex adds a keyless OpenAlex "
        "metadata query per 50 items for the exact paid count (free — no "
        "key is sent). For the link-resolver lookups use start_pdf_plan."
    ),
    script="scripts/pipelines/enrich_pdfs.py",
    params=(
        Param("lookups", "`none` (default; caches and logs only) or "
              "`openalex` (+ keyless OpenAlex metadata).", kind="option",
              flag="--plan-lookups", enum=("none", "openalex"), default="none"),
        *LIBRARY_ARGS,
        FILTER_KEYS,
        Param("institution", "Follow only this institution's link-resolver "
              "routes (a label from the run banner); repeatable. Maps to "
              "--library.", kind="repeat", flag="--library"),
    ),
    fixed=("--plan-strategy",),
    timeout=600,
    open_world=True,
    exclusive=(("group", "user"),),
    summarise=summarise_retrieval_plan,
)

STRATEGY_MODES = r"(api|paid|browser|connector|manual)"
YES_NO = ("yes", "no")

PDF_STRATEGY_SHOW = Tool(
    name="pdf_strategy_show",
    title="Show the PDF retrieval strategy",
    effect=READ_ONLY,
    description=(
        "Reads .claude/pdf/strategy.toml: the agreed order of retrieval "
        "modes, each step's status and result, whether the user is present, "
        "the policy, and the next step with its exact command or MCP tool "
        "(exists=false when there is no strategy yet). Use it to resume."
    ),
    script="scripts/pipelines/pdf_strategy.py",
    fixed=("show",),
    summarise=summarise_strategy,
)

PDF_STRATEGY_WRITE = Tool(
    name="pdf_strategy_write",
    title="Write the PDF retrieval strategy",
    effect=WRITES_PROJECT,
    description=(
        "Records the retrieval strategy agreed with the user in "
        ".claude/pdf/strategy.toml: the mode order, whether they are present "
        "now, whether an unattended run is possible, and the policy (taken "
        "from the last pdf_plan unless given). Only what the user agreed to. "
        "Refuses to replace an existing strategy unless force is set."
    ),
    script="scripts/pipelines/pdf_strategy.py",
    params=(
        Param("order", "Comma-separated modes in order, from api, paid, "
              "browser, connector, manual — e.g. `api,paid,manual`.",
              kind="option", flag="--order", required=True,
              pattern=rf"^{STRATEGY_MODES}(,{STRATEGY_MODES})*$"),
        Param("present", "Is the user at the keyboard now.", kind="option",
              flag="--present", enum=YES_NO, required=True),
        Param("unattended", "Could a run be left going unattended.",
              kind="option", flag="--unattended", enum=YES_NO),
        Param("library", "`user` or `group:<id>` (default: the plan's).",
              kind="option", flag="--library", pattern=r"^(user|group:[0-9]+)$"),
        Param("institution", "Link-resolver library label every browser / "
              "manual step follows.", kind="option", flag="--institution"),
        Param("scope_keys_file", "Project-relative keys file every step is "
              "restricted to.", kind="option", flag="--scope-keys-file",
              path=True),
        Param("min_version", "Agreed min_version.", kind="option",
              flag="--min-version", enum=("published", "accepted", "preprint")),
        Param("reconstructed", "Agreed reconstructed.", kind="option",
              flag="--reconstructed", enum=YES_NO),
        Param("paid", "Agreed paid position.", kind="option", flag="--paid",
              enum=("off", "prefer", "last_resort")),
        Param("policy_where", "Where runs read the policy: project, global, "
              "default, or strategy (this retrieval only; steps pass it as "
              "flags).", kind="option", flag="--policy-where",
              enum=("project", "global", "default", "strategy")),
        Param("notes", "Anything the next session should know.",
              kind="option", flag="--notes"),
        Param("force", "Replace an existing strategy.", kind="switch",
              flag="--force", type="boolean"),
    ),
    fixed=("init",),
    read_only=False,
    idempotent=False,
    summarise=summarise_strategy,
)

PDF_STRATEGY_UPDATE = Tool(
    name="pdf_strategy_update",
    title="Update the PDF retrieval strategy",
    effect=WRITES_PROJECT,
    description=(
        "Records a step's status (running when it starts; done, skipped or "
        "failed when it ends, with a one-line result) or whether the user is "
        "present, in .claude/pdf/strategy.toml. Returns the next step."
    ),
    script="scripts/pipelines/pdf_strategy.py",
    params=(
        Param("mode", "The step.", kind="option", flag="--mode",
              enum=("api", "paid", "browser", "connector", "manual")),
        Param("status", "Its new status.", kind="option", flag="--status",
              enum=("pending", "running", "done", "skipped", "failed")),
        Param("result", "One-line outcome, e.g. `attached 212, 40 left`.",
              kind="option", flag="--result"),
        Param("present", "Is the user at the keyboard now.", kind="option",
              flag="--present", enum=YES_NO),
        Param("unattended", "Could a run be left going unattended.",
              kind="option", flag="--unattended", enum=YES_NO),
    ),
    fixed=("update",),
    read_only=False,
    idempotent=False,
    summarise=summarise_strategy,
)

PDF_POLICY_WRITE = Tool(
    name="pdf_policy_write",
    title="Write the project's PDF policy",
    effect=WRITES_PROJECT,
    description=(
        "Writes the agreed [pdf] policy into the project's "
        "screening_config.py as PDF_MIN_VERSION / PDF_RECONSTRUCTED / "
        "PDF_PAID (replacing the template's commented lines), which every "
        "enrich_pdfs run in the project then reads. Only with the user's "
        "agreement. Fails when the project has no screening_config.py: the "
        "library-wide default is set with /setup in a terminal instead."
    ),
    script="scripts/pipelines/pdf_strategy.py",
    params=(
        Param("min_version", "published, accepted or preprint.", kind="option",
              flag="--min-version", enum=("published", "accepted", "preprint")),
        Param("reconstructed", "Attach text-only PDFs rebuilt from publisher "
              "XML.", kind="option", flag="--reconstructed", enum=YES_NO),
        Param("paid", "off, prefer or last_resort.", kind="option",
              flag="--paid", enum=("off", "prefer", "last_resort")),
        Param("config", "Project-relative config file (default: "
              "screening_config.py).", kind="option", flag="--config",
              path=True),
    ),
    fixed=("policy",),
    read_only=False,
    idempotent=True,
    summarise=summarise_policy_write,
)

def summarise_consent(out: str, rc: int, project: Path) -> tuple[dict, list[str]]:
    """The `CONSENT_JSON: {…}` line `pdf_consent.py` prints last."""
    lines = re.findall(r"^CONSENT_JSON: (.+)$", out, re.MULTILINE)
    if not lines:
        return {}, []
    try:
        return json.loads(lines[-1]), []
    except ValueError:
        return {}, []


CONSENT_SITE = Param(
    "site", "Site key as pdf_plan / pdf_consent_show name it: a handler "
    "(sage, tandf, ebsco, …), a platform (jstor, proquest) or a host "
    "(degruyter.com).", kind="option", flag="--site", required=True,
    pattern=r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$",
)

PDF_CONSENT_SHOW = Tool(
    name="pdf_consent_show",
    title="Show automated-download consent",
    effect=READ_ONLY,
    description=(
        "Lists, per site, whether the user has allowed or denied automated "
        "PDF download by the browser handlers and the Zotero Connector "
        "(allow / deny / ask = not answered), the date of the answer and the "
        "site's terms link, plus the sites the current plan depends on with "
        "their item counts. Publisher APIs, open-access sources and manual "
        "download need no consent. Use it to put the question to the user "
        "alongside the plan's counts."
    ),
    script="scripts/pipelines/pdf_consent.py",
    params=(Param("site", "Only these site keys.", kind="repeat", flag="--site"),),
    fixed=("show",),
    summarise=summarise_consent,
)

PDF_CONSENT_SET = Tool(
    name="pdf_consent_set",
    title="Record automated-download consent",
    effect=WRITES_USER_CONFIG,
    description=(
        "Records the user's own answer — allow, deny or ask — for automated "
        "PDF download from one site, dated, in the user config "
        "([publisher_consent.<site>]; it applies to every project, since it "
        "depends on the institution's licence). Only what the user said, "
        "after they have seen the site's terms link: never a default or a "
        "guess. deny sends the site's items to manual download; a browser or "
        "Connector run that cannot ask skips any site without allow."
    ),
    script="scripts/pipelines/pdf_consent.py",
    params=(
        CONSENT_SITE,
        Param("automated", "The user's answer.", kind="option",
              flag="--automated", enum=("allow", "deny", "ask"), required=True),
    ),
    fixed=("set",),
    read_only=False,
    idempotent=True,
    summarise=summarise_consent,
)

MANUAL_NOTE = (
    " The user downloads in their own browser; nothing is guessed: a file "
    "that matches no item, or several, is listed (unmatched_files, and "
    "output/pdf_staging/unmatched.json) for the user to assign with "
    "pdf_manual_assign."
)
INSTITUTION = Param(
    "institution", "Follow only this institution's link-resolver routes (a "
    "label from the run banner, e.g. 'aalto'); repeatable. Maps to --library.",
    kind="repeat", flag="--library",
)

PDF_MANUAL_NEXT = Tool(
    name="pdf_manual_next",
    title="Manual PDF download: next batch",
    effect=WRITES_ZOTERO,
    description=(
        "Opens browser tabs. One manual-download cycle: opens the next batch "
        "of articles still without a PDF in the user's default browser "
        "(library route, else doi.org), then moves the PDFs the user "
        "downloaded for the previous batch out of their Downloads folder, "
        "matches them to items and attaches them (tag pdf:manual). Run it "
        "when the user says they are done with the open tabs. If the "
        "summary says login_first=1, only a sign-in tab opened; the rest "
        "open on the next call." + MANUAL_NOTE
    ),
    script="scripts/pipelines/enrich_pdfs.py",
    params=(
        *LIBRARY_ARGS, FILTER_KEYS, INSTITUTION,
        Param("batch_size", "Tabs to open (default: [pdf] manual_batch_size, "
              "else 25).", kind="option", flag="--batch-size",
              pattern=r"^[1-9][0-9]{0,2}$"),
    ),
    fixed=("--manual", "next"),
    timeout=600,
    read_only=False,
    idempotent=False,
    open_world=True,
    exclusive=(("group", "user"),),
    summarise=summarise_pdf_manual,
)

PDF_MANUAL_COLLECT = Tool(
    name="pdf_manual_collect",
    title="Manual PDF download: collect",
    effect=WRITES_ZOTERO,
    description=(
        "Finishes a manual-download session without opening more tabs: "
        "moves the PDFs downloaded since the last batch opened out of the "
        "Downloads folder, matches them to items and attaches them (tag "
        "pdf:manual)." + MANUAL_NOTE
    ),
    script="scripts/pipelines/enrich_pdfs.py",
    params=(*LIBRARY_ARGS, FILTER_KEYS),
    fixed=("--manual", "collect"),
    timeout=600,
    read_only=False,
    idempotent=False,
    open_world=True,
    exclusive=(("group", "user"),),
    summarise=summarise_pdf_manual,
)

PDF_MANUAL_ASSIGN = Tool(
    name="pdf_manual_assign",
    title="Manual PDF download: assign a file",
    effect=WRITES_ZOTERO,
    description=(
        "Attaches one downloaded file that the manual-download cycle could "
        "not match, to the item the user names. Only on the user's say-so — "
        "never pick the item yourself."
    ),
    script="scripts/pipelines/enrich_pdfs.py",
    params=(
        Param("file", "The staged file, as listed in unmatched_files or "
              "output/pdf_staging/unmatched.json.", kind="option",
              flag="--assign-file", required=True, path=True),
        Param("item_key", "The Zotero item key the user says the file is.",
              kind="option", flag="--assign-item", required=True,
              pattern=r"^[A-Z0-9]{8}$"),
        *LIBRARY_ARGS,
    ),
    fixed=("--manual", "assign"),
    timeout=300,
    read_only=False,
    idempotent=False,
    open_world=True,
    exclusive=(("group", "user"),),
    summarise=summarise_pdf_manual,
)

PDF_MANUAL_IGNORE = Tool(
    name="pdf_manual_ignore",
    title="Manual PDF download: ignore a file",
    effect=WRITES_PROJECT,
    description=(
        "Marks a downloaded file the user says belongs to no item, so the "
        "manual-download cycle stops listing it. The file stays in "
        "output/pdf_staging/."
    ),
    script="scripts/pipelines/enrich_pdfs.py",
    params=(
        Param("file", "The staged file, as listed in unmatched_files.",
              kind="option", flag="--assign-file", required=True, path=True),
        *LIBRARY_ARGS,
    ),
    fixed=("--manual", "ignore"),
    read_only=False,
    open_world=True,
    exclusive=(("group", "user"),),
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

# ---------------------------------------------------------------------------
# Jobs — long runs, started detached; follow them with job_status
# ---------------------------------------------------------------------------

#: Appended to every job description: how a job is driven.
JOB_NOTE = (
    " Runs as a background job: returns a job_id at once; follow it with "
    "job_status, relay any question it asks with job_answer, stop it with "
    "cancel_job."
)

CONFIG = Param(
    "config", "Project-relative screening config (default: "
    "./screening_config.py).", kind="option", flag="--config", path=True,
)
TAG_PREFIX = Param(
    "tag_prefix", "This review's tag namespace, overriding TAG_PREFIX in "
    "screening_config.py.", kind="option", flag="--tag-prefix",
)
COLLECTION_KEY = Param(
    "collection", "Zotero collection key of the review.", kind="option",
    flag="--collection", required=True, pattern=r"^[A-Z0-9]{8}$",
)
MODEL = Param(
    "model", "Model alias (haiku, sonnet, opus, flash, pro, …) or a full "
    "model ID. Default: the stage's model in screening_config.py.",
    kind="option", flag="--model", pattern=r"^[A-Za-z0-9][A-Za-z0-9._:/@-]*$",
)
DRY_RUN_LLM = Param(
    "dry_run", "Print the first prompt and a cost estimate; no model calls, "
    "no Zotero writes.", kind="switch", flag="--dry-run", type="boolean",
)
LIBRARY_PAIR = (("group", "user"),)

START_SEARCH = Tool(
    name="start_search",
    title="Run the systematic search",
    effect=WRITES_PROJECT,
    description=(
        "Runs the formal search from search_config.py across the citation "
        "databases (network; uses their API quotas) and writes the raw and "
        "deduplicated result CSVs plus search_metadata.json and "
        "search_run.json. Changes nothing in Zotero." + JOB_NOTE
    ),
    script="scripts/pipelines/search.py",
    params=(
        Param("config", "Project-relative search config (default: "
              "./search_config.py).", kind="option", flag="--config",
              path=True),
        Param("databases", "Comma-separated databases to search (scopus, "
              "wos, openalex, semantic_scholar). Default: every database "
              "with usable credentials.", kind="option", flag="--databases",
              pattern=r"^[a-z_]+(,[a-z_]+)*$"),
        Param("streams", "`keyword,citation` (default), `keyword` or "
              "`citation` (forward snowballing from CITATION_SEEDS only — "
              "a cheap pilot of a seed).", kind="option", flag="--streams",
              enum=("keyword,citation", "keyword", "citation")),
        Param("search_fields", "`all` (default; OpenAlex full text) or "
              "`title_abstract` (comparable across databases).",
              kind="option", flag="--search-fields",
              enum=("all", "title_abstract")),
    ),
    job=True,
    read_only=False,
    idempotent=False,
    open_world=True,
    artifacts=("analysis/raw/search_results.csv",
               "analysis/raw/search_results_raw.csv",
               "analysis/raw/search_results_raw.partial.csv",
               "search_metadata.json", "search_run.json"),
)

START_IMPORT = Tool(
    name="start_import",
    title="Import search results into Zotero",
    effect=WRITES_ZOTERO,
    description=(
        "Imports a deduplicated search-results CSV into the Zotero library: "
        "creates new items (Crossref metadata), patches existing ones "
        "(missing abstract, collection, doctype tag), adds them to the "
        "collection. Use dry_run first to preview." + JOB_NOTE
    ),
    script="scripts/pipelines/import_to_zotero.py",
    params=(
        GROUP, USER,
        Param("input", "Project-relative search-results CSV.",
              kind="option", flag="--input", path=True,
              default="analysis/raw/search_results.csv"),
        Param("collection", "Collection to add items to: an 8-character "
              "key or a display name (created if missing).", kind="option",
              flag="--collection"),
        Param("no_create_collection", "Fail instead of creating a missing "
              "collection.", kind="switch", flag="--no-create-collection",
              type="boolean"),
        CONFIG, TAG_PREFIX,
        Param("dry_run", "Parse and report; write nothing to Zotero.",
              kind="switch", flag="--dry-run", type="boolean"),
    ),
    job=True,
    read_only=False,
    idempotent=False,
    open_world=True,
    exclusive=LIBRARY_PAIR,
)

START_ENRICH_ABSTRACTS = Tool(
    name="start_enrich_abstracts",
    title="Fetch missing abstracts",
    effect=WRITES_ZOTERO,
    description=(
        "Looks up abstracts for items that have a DOI but no abstract "
        "(Crossref, Semantic Scholar, Scopus, … in priority order) and "
        "writes them into Zotero. Resumes past items already done per its "
        "log." + JOB_NOTE
    ),
    script="scripts/pipelines/enrich_abstracts.py",
    params=(
        *LIBRARY_ARGS, FILTER_KEYS,
        Param("sources", "Comma-separated fetchers to ask, replacing the "
              "default cascade (e.g. to opt into wos).", kind="option",
              flag="--sources", pattern=r"^[a-z_]+(,[a-z_]+)*$"),
        Param("dry_run", "Fetch abstracts; do not write them to Zotero.",
              kind="switch", flag="--dry-run", type="boolean"),
    ),
    job=True,
    read_only=False,
    open_world=True,
    exclusive=LIBRARY_PAIR,
    artifacts=("output/abstract_fetch_log.csv",),
)

START_ENRICH_DOIS = Tool(
    name="start_enrich_dois",
    title="Validate and find DOIs",
    effect=WRITES_ZOTERO,
    description=(
        "Validates existing DOIs against Crossref and searches Crossref for "
        "items without one. Applies 3/3 matches; for a 2/3 match it asks "
        "(job_status shows the question; answer y or n with job_answer) "
        "unless no_prompt is set." + JOB_NOTE
    ),
    script="scripts/pipelines/enrich_dois.py",
    params=(
        Param("mode", "`all` (default): validate, then find missing; "
              "`validate` or `find_missing` for one pass.", kind="map",
              default="all",
              choices={"all": ("--all",), "validate": ("--validate",),
                       "find_missing": ("--find-missing",)}),
        *LIBRARY_ARGS, FILTER_KEYS,
        Param("dry_run", "Never write to Zotero; report proposals only.",
              kind="switch", flag="--dry-run", type="boolean"),
        Param("no_prompt", "Do not ask about 2/3 matches; log them as "
              "proposed_not_applied.", kind="switch", flag="--no-prompt",
              type="boolean"),
        Param("fix_malformed", "Rewrite DOIs stored with URL / doi: "
              "prefixes or whitespace to the clean form.", kind="switch",
              flag="--fix-malformed", type="boolean"),
        Param("replace_invalid", "Overwrite DOIs that fail validation with "
              "a 3/3 Crossref match.", kind="switch",
              flag="--replace-invalid", type="boolean"),
    ),
    job=True,
    control=True,
    read_only=False,
    idempotent=False,
    open_world=True,
    exclusive=LIBRARY_PAIR,
    artifacts=("output/doi_enrich_log.csv",),
)

START_ABSTRACT_SCREEN = Tool(
    name="start_abstract_screen",
    title="Screen abstracts with an LLM",
    effect=COSTS_LLM,
    description=(
        "Screens each not-yet-screened item in the collection against the "
        "review's criteria with the configured model, logs every decision "
        "to the screening CSV and writes this review's abstract:* tags to "
        "Zotero. Run with dry_run first for the cost estimate." + JOB_NOTE
    ),
    script="scripts/pipelines/abstract_screen.py",
    params=(
        COLLECTION_KEY, *LIBRARY_ARGS, CONFIG, TAG_PREFIX, MODEL,
        Param("sample", "Screen a random sample of N items (0 = all).",
              kind="option", flag="--sample", pattern=r"^[0-9]+$"),
        Param("search_csv", "Project-relative search_results.csv, for "
              "query provenance in the log.", kind="option",
              flag="--search-csv", path=True),
        DRY_RUN_LLM,
    ),
    job=True,
    read_only=False,
    idempotent=False,
    open_world=True,
    exclusive=LIBRARY_PAIR,
    artifacts=("screening/abstract_screening.csv",),
)

START_FULLTEXT_CODE = Tool(
    name="start_fulltext_code",
    title="Code full texts with an LLM",
    effect=COSTS_LLM,
    description=(
        "Codes each eligible item's PDF with the configured model: a "
        "full-text decision plus the coding fields, logged to the CSV and "
        "written to Zotero as fulltext:* tags and an SLR Coding note. "
        "full_recode clears this review's coding tags first. Run with "
        "dry_run first for the cost estimate." + JOB_NOTE
    ),
    script="scripts/pipelines/fulltext_code.py",
    params=(
        COLLECTION_KEY, *LIBRARY_ARGS, CONFIG, TAG_PREFIX, MODEL,
        Param("limit", "Code at most N items (0 = all).", kind="option",
              flag="--limit", pattern=r"^[0-9]+$"),
        Param("only_keys", "Comma-separated Zotero item keys to code.",
              kind="option", flag="--only-keys",
              pattern=r"^[A-Z0-9]{8}(,[A-Z0-9]{8})*$"),
        Param("rerun", "Retry only items whose last decision was `error`.",
              kind="switch", flag="--rerun", type="boolean"),
        Param("full_recode", "Back up the log, clear this review's "
              "fulltext:* and coding tags, and re-code everything.",
              kind="switch", flag="--full-recode", type="boolean"),
        DRY_RUN_LLM,
    ),
    job=True,
    read_only=False,
    destructive=True,
    idempotent=False,
    open_world=True,
    exclusive=(*LIBRARY_PAIR, ("rerun", "full_recode")),
    artifacts=("screening/fulltext_screening.csv",),
)

START_PDF_PLAN = Tool(
    name="start_pdf_plan",
    title="Plan PDF retrieval (full lookups)",
    effect=READ_ONLY,
    description=(
        "pdf_plan with every lookup: also asks Crossref and the library link "
        "resolver about each uncached item (~2 s each; the estimate is the "
        "first lines of the log), so the browser / EBSCOhost / Connector / "
        "no-route split is exact. Never writes to Zotero; the answers are "
        "cached, so the browser pass afterwards starts warm. Writes "
        ".claude/pdf/plan.json." + JOB_NOTE
    ),
    script="scripts/pipelines/enrich_pdfs.py",
    params=(
        Param("lookups", "`all` (default) or `openalex`.", kind="option",
              flag="--plan-lookups", enum=("all", "openalex"), default="all"),
        *LIBRARY_ARGS,
        FILTER_KEYS,
        INSTITUTION,
    ),
    fixed=("--plan-strategy",),
    job=True,
    # enrich_pdfs takes --control-file; the planning mode never asks.
    control=True,
    open_world=True,
    exclusive=LIBRARY_PAIR,
    artifacts=(PLAN_JSON,),
)

JOB_ID = Param(
    "job_id", "The job_id a start_* tool returned (see list_jobs).",
    kind="option", required=True, pattern=r"^[a-z_]+-[0-9]{8}-[0-9]{6}-[0-9a-f]{4}$",
)

JOB_STATUS = Tool(
    name="job_status",
    title="Job status",
    effect=READ_ONLY,
    description=(
        "Reports a job's status (running, awaiting_user, succeeded, failed, "
        "cancelled), a short progress summary, the question it is waiting "
        "on if any, the tail of its log and the files it wrote. When it is "
        "awaiting_user, show the prompt to the user and pass their answer "
        "to job_answer — never answer on their behalf."
    ),
    script="",
    params=(JOB_ID,),
    internal=True,
)

JOB_ANSWER = Tool(
    name="job_answer",
    title="Answer a job's question",
    effect=WRITES_PROJECT,
    description=(
        "Delivers the user's answer to the question a job is waiting on "
        "(status awaiting_user). The job then acts on it — e.g. applies a "
        "proposed DOI — so pass only what the user said."
    ),
    script="",
    params=(JOB_ID, Param("answer", "The user's answer, verbatim.",
                          kind="option", required=True)),
    read_only=False,
    idempotent=False,
    internal=True,
)

CANCEL_JOB = Tool(
    name="cancel_job",
    title="Cancel a job",
    effect=WRITES_PROJECT,
    description=(
        "Stops a running job: interrupts it first so it saves what it has "
        "done (CSV logs, pending Zotero tags), then terminates it if it "
        "does not stop. Returns at once; job_status shows when it has "
        "ended. Work already written stays written."
    ),
    script="",
    params=(JOB_ID,),
    read_only=False,
    internal=True,
)

LIST_JOBS = Tool(
    name="list_jobs",
    title="List jobs",
    effect=READ_ONLY,
    description="Lists this project's recent jobs, newest first, with "
                "their status.",
    script="",
    internal=True,
)

JOB_TOOLS: tuple[Tool, ...] = (
    START_PDF_PLAN,
    START_SEARCH,
    START_IMPORT,
    START_ENRICH_ABSTRACTS,
    START_ENRICH_DOIS,
    START_ABSTRACT_SCREEN,
    START_FULLTEXT_CODE,
)
JOB_CONTROL_TOOLS: tuple[Tool, ...] = (JOB_STATUS, JOB_ANSWER, CANCEL_JOB, LIST_JOBS)

TOOLS: tuple[Tool, ...] = (
    CHECK_SETUP,
    SCAFFOLD_PROJECT,
    PDF_PLAN,
    PDF_STRATEGY_SHOW,
    PDF_STRATEGY_WRITE,
    PDF_STRATEGY_UPDATE,
    PDF_POLICY_WRITE,
    PDF_CONSENT_SHOW,
    PDF_CONSENT_SET,
    ENRICH_PDFS_PLAN,
    ENRICH_PDFS_REPORT,
    PDF_MANUAL_NEXT,
    PDF_MANUAL_COLLECT,
    PDF_MANUAL_ASSIGN,
    PDF_MANUAL_IGNORE,
    SCREENING_REPORT,
    AUDIT_LIBRARY,
    EXPORT_CODED_INCLUDES,
    GENERATE_BIB,
    *JOB_TOOLS,
    *JOB_CONTROL_TOOLS,
)
TOOLS_BY_NAME = {t.name: t for t in TOOLS}
