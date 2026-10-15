#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""The agreed PDF retrieval strategy: `.claude/pdf/strategy.toml`.

The `retrieving-pdfs` skill plans with the user (`enrich_pdfs.py
--plan-strategy`), agrees an order of retrieval modes, and records it
here so it is executed step by step and a later session can resume it
(plan `pdf-retrieval.md` §1). The skill never edits the TOML by hand;
it calls this script (or the MCP tools `pdf_strategy_show` /
`pdf_strategy_write` / `pdf_strategy_update` / `pdf_policy_write`,
which run it).

    pdf_strategy.py init --order api,paid,manual --present yes \\
        --unattended no --library group:12345
    pdf_strategy.py show            # the strategy and the next step
    pdf_strategy.py update --mode api --status done --result "attached 212"
    pdf_strategy.py update --present no
    pdf_strategy.py policy --min-version accepted --reconstructed false \\
        --paid prefer               # writes PDF_* into ./screening_config.py

File layout (version 1):

    version = 1
    created = "…"            updated = "…"
    library = "group:12345"  # or "user"; the steps' --group / --user
    institution = ""         # --library label to follow, if several
    scope_keys_file = ""     # --filter-keys-file for every step, if any
    plan = ".claude/pdf/plan.json"
    user_present = true      # is the user at the keyboard this session
    unattended_possible = false
    notes = ""

    [policy]                 # what was agreed, and where runs read it
    min_version = "accepted"
    reconstructed = false
    paid = "prefer"
    where = "project"        # project | global | default | strategy

    [[steps]]                # in order
    mode = "api"             # api | paid | browser | connector | unattended | manual
    status = "pending"       # pending | running | done | skipped | failed
    started = ""  finished = ""  result = ""

**Modes and what runs them.** `api` is the automated cascade (free
sources, plus the paid OpenAlex source *inside* it unless `paid` is a
step of its own, in which case `api` runs with `--paid off`); `paid` is
`--sources openalex_content --paid prefer` on its own, so the user can
put paying after the browser pass but before their own time; `browser`
is the browser pass (publisher handlers, the library route to EBSCOhost
and the Zotero Connector fallback), attended; `connector` is the
Connector pass alone; `unattended` is the same browser pass as a long
unattended run in two phases — `setup` with the user (consent, one
solve per publisher, Connector hosts confirmed) and then `run` with
nobody (`enrich_pdfs.py --unattended setup|run`); `manual` is the
manual-download cycle. `MODES[mode]` holds each one's command and MCP
tool, and `describe_step` the exact tool arguments, so the skill never
composes them.

**Who must be present.** `api` and `paid` run unattended. `browser`,
`connector` and `manual` need the user at the keyboard: the manual cycle
always (they download), the attended browser and Connector passes
because a Chromium window asks them to solve challenges. `unattended`
needs them for its setup phase only: once `.claude/pdf/unattended.json`
records a finished setup, the step's next phase is `run`, which needs
nobody — so `blocked_until_user_present` is then false. An `unattended`
step is only accepted when the user said an unattended run can be left
going (`unattended_possible`); that answer is what it rests on.

**Policy.** `policy` records the `[pdf]` values agreed. Where they live is
`where`: already stored (`global` from /setup, `project` from
`screening_config.py`), written to the project by `policy` (the only
tier this script may write — the global config is the setup wizard's),
or `strategy` — agreed for this retrieval only, so each step passes
`--min-version` / `--paid` itself. `reconstructed` has no per-run flag;
under `strategy` it is whatever the stored policy says.

Stdlib only (`tomllib` reads; the writer is the small one below, since
the layout is fixed).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from core import pdf_policy  # noqa: E402

STRATEGY_PATH = Path(".claude") / "pdf" / "strategy.toml"
PLAN_PATH = Path(".claude") / "pdf" / "plan.json"
DEFAULT_PROJECT_CONFIG = "screening_config.py"
SCHEMA_VERSION = 1

STATUSES: tuple[str, ...] = ("pending", "running", "done", "skipped", "failed")
#: Written by `enrich_pdfs.py --unattended setup` (`pdf_unattended`).
UNATTENDED_STATE = Path(".claude") / "pdf" / "unattended.json"
OPEN_STATUSES = ("pending", "running")
POLICY_WHERE: tuple[str, ...] = ("project", "global", "default", "strategy")

#: The plugin script every step runs, as the skill writes it.
ENRICH = "uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py"
CONTROL = ".claude/pdf/browser.json"
PROGRESS = ".claude/pdf/browser-progress.jsonl"
UNATTENDED_PROGRESS = ".claude/pdf/unattended-progress.jsonl"


@dataclass(frozen=True)
class Mode:
    name: str
    #: Needs the user at the keyboard (see the module docstring).
    needs_user: bool
    #: argv after the script, before scope / library / policy flags.
    args: tuple[str, ...]
    #: MCP tool(s) that run it; `describe_step` names the one for the
    #: next phase and the arguments to pass.
    tool: str
    #: Long-running: start it in the background.
    background: bool
    what: str


MODES: dict[str, Mode] = {m.name: m for m in (
    Mode("api", False, (), "start_enrich_pdfs, start_enrich_pdfs_paid", True,
         "API cascade: publisher APIs, Crossref TDM, PMC, open-access sources "
         "(and the paid OpenAlex source unless it is a step of its own)."),
    Mode("paid", False, ("--sources", "openalex_content", "--paid", "prefer"),
         "start_enrich_pdfs_paid", True,
         "Paid OpenAlex Content API only, $0.01 per PDF."),
    Mode("browser", True, ("--sources", "browser", "--browser-workers", "4",
                           "--control-file", CONTROL, "--progress-json", PROGRESS),
         "", True,
         "Browser pass: publisher handlers (a Cloudflare / SSO solve each), "
         "the library route to EBSCOhost, then the Zotero Connector. Attended: "
         "relay its prompts from the control file."),
    Mode("connector", True, ("--sources", "connector", "--control-file", CONTROL,
                             "--progress-json", PROGRESS),
         "", True, "Zotero Connector over the library link resolver only."),
    Mode("unattended", True, ("--unattended", "run", "--browser-workers", "4",
                              "--progress-json", UNATTENDED_PROGRESS),
         "start_pdf_unattended_setup, start_pdf_unattended_run", True,
         "The browser pass as a long unattended run: setup with the user "
         "(consent, one solve per publisher, Connector hosts confirmed), then "
         "the run with nobody — whatever needs a person is deferred to manual."),
    Mode("manual", True, ("--manual", "next"), "pdf_manual_next, pdf_manual_collect",
         False, "Manual download in batches: the user downloads, the plugin "
                "opens the tabs, collects and attaches."),
)}


class StrategyError(ValueError):
    """A value the strategy refuses."""


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Read / write
# ---------------------------------------------------------------------------

def _q(value: str) -> str:
    """A TOML basic string."""
    out = (str(value).replace("\\", "\\\\").replace('"', '\\"')
           .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t"))
    return f'"{out}"'


def _v(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return _q(str(value if value is not None else ""))


TOP_KEYS = ("version", "created", "updated", "library", "institution",
            "scope_keys_file", "plan", "user_present", "unattended_possible",
            "notes")
POLICY_KEYS = ("min_version", "reconstructed", "paid", "where")
STEP_KEYS = ("mode", "status", "started", "finished", "result")


def dumps(strategy: dict) -> str:
    lines = [
        "# PDF retrieval strategy, agreed with the user through the",
        "# retrieving-pdfs skill. Change it with pdf_strategy.py (or the",
        "# pdf_strategy_* MCP tools), not by hand.",
    ]
    lines += [f"{k} = {_v(strategy.get(k, ''))}" for k in TOP_KEYS]
    lines += ["", "[policy]"]
    policy = strategy.get("policy") or {}
    lines += [f"{k} = {_v(policy.get(k, ''))}" for k in POLICY_KEYS]
    for step in strategy.get("steps") or []:
        lines += ["", "[[steps]]"]
        lines += [f"{k} = {_v(step.get(k, ''))}" for k in STEP_KEYS]
    return "\n".join(lines) + "\n"


def load(path: Path = STRATEGY_PATH) -> dict | None:
    """The strategy, or None when there is none yet."""
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise StrategyError(f"{path}: not valid TOML ({e})") from None
    validate(data)
    return data


def save(strategy: dict, path: Path = STRATEGY_PATH) -> None:
    validate(strategy)
    strategy["updated"] = _now()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".toml.tmp")
    tmp.write_text(dumps(strategy), encoding="utf-8")
    tmp.replace(path)


def validate(strategy: dict) -> None:
    steps = strategy.get("steps") or []
    if not steps:
        raise StrategyError("a strategy needs at least one step")
    seen: set[str] = set()
    for step in steps:
        mode = step.get("mode")
        if mode not in MODES:
            raise StrategyError(f"unknown mode {mode!r}; choose from {', '.join(MODES)}")
        if mode in seen:
            raise StrategyError(f"mode {mode!r} appears twice in the order")
        seen.add(mode)
        if mode == "unattended" and not strategy.get("unattended_possible", False):
            raise StrategyError(
                "an unattended step needs the user's say-so that an unattended "
                "run can be left going (--unattended yes)")
        if step.get("status", "pending") not in STATUSES:
            raise StrategyError(f"{mode}: unknown status {step.get('status')!r}")
    policy = strategy.get("policy") or {}
    for name in ("min_version", "reconstructed", "paid"):
        if name in policy:
            pdf_policy.coerce(name, policy[name], "strategy [policy]")
    if policy.get("where", "default") not in POLICY_WHERE:
        raise StrategyError(f"[policy] where must be one of {', '.join(POLICY_WHERE)}")
    library = strategy.get("library", "")
    if library and not re.fullmatch(r"user|group:[0-9]+", library):
        raise StrategyError(f"library {library!r}: expected 'user' or 'group:<id>'")


# ---------------------------------------------------------------------------
# Building and updating
# ---------------------------------------------------------------------------

def parse_order(raw: str) -> list[str]:
    order = [m.strip().lower() for m in raw.split(",") if m.strip()]
    if not order:
        raise StrategyError("--order is empty")
    return order


def _plan_policy(plan_path: Path) -> dict:
    try:
        plan = json.loads(plan_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return plan.get("policy") or {}


def _plan_library(plan_path: Path) -> str:
    try:
        lib = json.loads(plan_path.read_text(encoding="utf-8")).get("library", "")
    except (OSError, ValueError):
        return ""
    if lib.startswith("user"):
        return "user"
    m = re.fullmatch(r"group:([0-9]+)", lib)
    return lib if m else ""


def _where_from_sources(sources: dict) -> str:
    vals = [str(v) for v in (sources or {}).values()]
    if any(v.startswith("project") for v in vals):
        return "project"
    if any(v == "global" or v.startswith("legacy") for v in vals):
        return "global"
    return "default"


def new_strategy(
    order: list[str], *, present: bool, unattended: bool,
    library: str = "", institution: str = "", scope_keys_file: str = "",
    plan: Path = PLAN_PATH, policy: dict | None = None, where: str = "",
    notes: str = "",
) -> dict:
    planned = _plan_policy(plan)
    pol = {
        "min_version": planned.get("min_version", pdf_policy.DEFAULTS["min_version"]),
        "reconstructed": bool(planned.get("reconstructed",
                                          pdf_policy.DEFAULTS["reconstructed"])),
        "paid": planned.get("paid", pdf_policy.DEFAULTS["paid"]),
    }
    pol.update({k: v for k, v in (policy or {}).items() if v is not None})
    pol = {k: pdf_policy.coerce(k, v) for k, v in pol.items()}
    pol["where"] = where or _where_from_sources(planned.get("sources") or {})
    now = _now()
    strategy = {
        "version": SCHEMA_VERSION, "created": now, "updated": now,
        "library": library or _plan_library(plan),
        "institution": institution, "scope_keys_file": scope_keys_file,
        "plan": plan.as_posix(), "user_present": present,
        "unattended_possible": unattended, "notes": notes,
        "policy": pol,
        "steps": [{"mode": m, "status": "pending", "started": "", "finished": "",
                   "result": ""} for m in order],
    }
    validate(strategy)
    return strategy


def update_step(strategy: dict, mode: str, status: str, result: str = "") -> dict:
    if status not in STATUSES:
        raise StrategyError(f"unknown status {status!r}; choose from {', '.join(STATUSES)}")
    for step in strategy["steps"]:
        if step["mode"] == mode:
            if status == "running" and not step.get("started"):
                step["started"] = _now()
            if status in ("done", "skipped", "failed"):
                step["finished"] = _now()
            elif status == "pending":
                step["finished"] = ""
            step["status"] = status
            if result:
                step["result"] = result
            return step
    raise StrategyError(
        f"mode {mode!r} is not in this strategy "
        f"({', '.join(s['mode'] for s in strategy['steps'])})")


def next_step(strategy: dict) -> dict | None:
    """The step to run now: a running one first (resume), else the first
    pending one."""
    for status in ("running", "pending"):
        for step in strategy["steps"]:
            if step.get("status") == status:
                return step
    return None


def unattended_phase(state_path: Path = UNATTENDED_STATE) -> str:
    """`setup` until `enrich_pdfs.py --unattended setup` has recorded a
    finished setup, then `run`."""
    try:
        state = json.loads(Path(state_path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "setup"
    finished = (state.get("setup") or {}).get("finished") if isinstance(state, dict) else ""
    return "run" if finished else "setup"


def command_for(strategy: dict, mode: str, *, phase: str = "") -> list[str]:
    """The terminal invocation of one step, as a token list. For
    `unattended`, `phase` picks setup or run (default: the next one)."""
    spec = MODES[mode]
    args = list(spec.args)
    if mode == "unattended" and (phase or unattended_phase()) == "setup":
        args = ["--unattended", "setup", "--control-file", CONTROL,
                "--progress-json", UNATTENDED_PROGRESS]
    modes = [s["mode"] for s in strategy["steps"]]
    policy = strategy.get("policy") or {}
    if mode == "api" and "paid" in modes:
        args += ["--paid", "off"]   # paying is a step of its own
    if policy.get("where") == "strategy" and mode not in ("paid",):
        args += ["--min-version", str(policy.get("min_version", "accepted"))]
        if mode == "api" and "paid" not in modes:
            args += ["--paid", str(policy.get("paid", "prefer"))]
    library = strategy.get("library", "")
    if library == "user":
        args.append("--user")
    elif library.startswith("group:"):
        args += ["--group", library.split(":", 1)[1]]
    if strategy.get("institution") and mode in ("browser", "connector", "unattended",
                                                 "manual"):
        args += ["--library", strategy["institution"]]
    if strategy.get("scope_keys_file"):
        args += ["--filter-keys-file", strategy["scope_keys_file"]]
    return [ENRICH, *args]


def tool_for(strategy: dict, mode: str, *, phase: str = "") -> tuple[str, dict]:
    """(MCP tool, its arguments) for one step — what the skill calls, so it
    never composes them. ("", {}) where no tool runs the mode (the attended
    browser and Connector passes: the agent relays their control file)."""
    modes = [s["mode"] for s in strategy["steps"]]
    policy = strategy.get("policy") or {}
    by_strategy = policy.get("where") == "strategy"
    out: dict = {}
    library = strategy.get("library", "")
    if library == "user":
        out["user"] = True
    elif library.startswith("group:"):
        out["group"] = library.split(":", 1)[1]
    if strategy.get("scope_keys_file"):
        out["filter_keys_file"] = strategy["scope_keys_file"]
    if mode == "api":
        if by_strategy:
            out["min_version"] = str(policy.get("min_version", "accepted"))
        # Paying inside the cascade is a cost the tool's effect class must
        # say; with `paid` a step of its own the cascade runs --paid off.
        paid_inside = "paid" not in modes and str(policy.get("paid", "prefer")) != "off"
        if not paid_inside:
            return "start_enrich_pdfs", out
        out["scope"] = (f"cascade_{policy.get('paid', 'prefer')}" if by_strategy
                        else "cascade")
        return "start_enrich_pdfs_paid", out
    if mode == "paid":
        out["scope"] = "paid_only"
        return "start_enrich_pdfs_paid", out
    if mode == "unattended":
        if by_strategy:
            out["min_version"] = str(policy.get("min_version", "accepted"))
        if strategy.get("institution"):
            out["institution"] = [strategy["institution"]]
        name = ("start_pdf_unattended_setup"
                if (phase or unattended_phase()) == "setup"
                else "start_pdf_unattended_run")
        return name, out
    if mode == "manual":
        return "pdf_manual_next", out
    return "", {}


def describe_step(strategy: dict, step: dict) -> dict:
    spec = MODES[step["mode"]]
    needs_user = spec.needs_user
    extra: dict = {}
    if step["mode"] == "unattended":
        # The setup phase needs the user; the run needs nobody.
        phase = unattended_phase()
        needs_user = phase == "setup"
        extra["phase"] = phase
    blocked = needs_user and not strategy.get("user_present", False)
    tool, tool_args = tool_for(strategy, step["mode"], phase=extra.get("phase", ""))
    return {
        **step,
        **extra,
        "needs_user": needs_user,
        "blocked_until_user_present": blocked,
        "tool": tool or spec.tool,
        "tool_arguments": tool_args,
        "background": spec.background,
        "what": spec.what,
        "command": " ".join(command_for(strategy, step["mode"],
                                        phase=extra.get("phase", ""))),
    }


def view(strategy: dict) -> dict:
    """What `show` reports: the strategy plus the next step, resolved."""
    nxt = next_step(strategy)
    return {
        "library": strategy.get("library", ""),
        "user_present": strategy.get("user_present", False),
        "unattended_possible": strategy.get("unattended_possible", False),
        "policy": strategy.get("policy", {}),
        "steps": [{"mode": s["mode"], "status": s["status"],
                   "result": s.get("result", "")} for s in strategy["steps"]],
        "next": describe_step(strategy, nxt) if nxt else None,
        "complete": nxt is None,
    }


# ---------------------------------------------------------------------------
# Project policy override (screening_config.py)
# ---------------------------------------------------------------------------

def write_project_policy(config_path: Path, policy: dict) -> list[str]:
    """Set `PDF_MIN_VERSION` / `PDF_RECONSTRUCTED` / `PDF_PAID` in the
    project's `screening_config.py`: an existing assignment (live or the
    template's commented-out one) is replaced in place, a missing one is
    appended. Returns the lines written. The file is read by
    `enrich_pdfs` with `ast`, so each value is a plain literal."""
    if not config_path.is_file():
        raise FileNotFoundError(config_path)
    text = config_path.read_text(encoding="utf-8")
    written = []
    for name, const in pdf_policy.PROJECT_CONSTANTS.items():
        if name not in policy or policy[name] is None:
            continue
        value = pdf_policy.coerce(name, policy[name], const)
        line = f"{const} = {value!r}" if isinstance(value, bool) else f'{const} = "{value}"'
        pattern = re.compile(rf"^[ \t]*#?[ \t]*{const}[ \t]*(?::[^=\n]*)?=.*$", re.MULTILINE)
        live = re.compile(rf"^{const}[ \t]*(?::[^=\n]*)?=.*$", re.MULTILINE)
        if live.search(text):
            text = live.sub(line, text, count=1)
        elif pattern.search(text):
            text = pattern.sub(line, text, count=1)
        else:
            text = text.rstrip("\n") + "\n\n# PDF retrieval policy (retrieving-pdfs skill)\n" + line + "\n"
        written.append(line)
    config_path.write_text(text, encoding="utf-8")
    pdf_policy.read_project_constants(config_path)  # still parses as literals
    return written


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _yes_no(raw: str) -> bool:
    text = raw.strip().lower()
    if text in ("yes", "y", "true", "1"):
        return True
    if text in ("no", "n", "false", "0"):
        return False
    raise argparse.ArgumentTypeError(f"expected yes or no, got {raw!r}")


def _print_view(v: dict) -> None:
    print(f"PDF retrieval strategy — library {v['library'] or '(default)'}; "
          f"user present: {'yes' if v['user_present'] else 'no'}; "
          f"unattended possible: {'yes' if v['unattended_possible'] else 'no'}")
    pol = v["policy"]
    print(f"  policy: min_version={pol.get('min_version')} "
          f"reconstructed={str(pol.get('reconstructed')).lower()} "
          f"paid={pol.get('paid')} (stored: {pol.get('where')})")
    for i, s in enumerate(v["steps"], 1):
        res = f" — {s['result']}" if s.get("result") else ""
        print(f"  {i}. {s['mode']:<9} {s['status']}{res}")
    nxt = v["next"]
    if nxt is None:
        print("  All steps finished. Run the audit and diagnose what is left.")
    else:
        phase = f" ({nxt['phase']} phase)" if nxt.get("phase") else ""
        print(f"  Next: {nxt['mode']}{phase} — {nxt['what']}")
        if nxt["blocked_until_user_present"]:
            print("  This step needs the user at the keyboard; they said they "
                  "are not. Ask before starting it.")
        if nxt["tool"]:
            print(f"  MCP tool: {nxt['tool']} "
                  f"{json.dumps(nxt.get('tool_arguments') or {})}")
        print(f"  Command: {nxt['command']}")
    print("STRATEGY_JSON: " + json.dumps(v, ensure_ascii=False))


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--file", default=str(STRATEGY_PATH),
                   help=f"Strategy file (default: {STRATEGY_PATH}).")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("show", help="Print the strategy and the next step.")

    init = sub.add_parser("init", help="Write a new strategy.")
    init.add_argument("--order", required=True,
                      help=f"Comma-separated modes in order: {', '.join(MODES)}.")
    init.add_argument("--present", type=_yes_no, required=True,
                      help="Is the user at the keyboard now (yes/no).")
    init.add_argument("--unattended", type=_yes_no, default=False,
                      help="Could an unattended run be left going (yes/no).")
    init.add_argument("--library", default="",
                      help="'user' or 'group:<id>' (default: the plan's).")
    init.add_argument("--institution", default="",
                      help="Link-resolver library label to follow (--library "
                           "of enrich_pdfs), with several configured.")
    init.add_argument("--scope-keys-file", default="",
                      help="Keys file every step is restricted to.")
    init.add_argument("--plan", default=str(PLAN_PATH),
                      help=f"The plan the strategy rests on (default: {PLAN_PATH}).")
    init.add_argument("--min-version", choices=pdf_policy.MIN_VERSIONS)
    init.add_argument("--reconstructed", type=_yes_no)
    init.add_argument("--paid", choices=pdf_policy.PAID_MODES)
    init.add_argument("--policy-where", choices=POLICY_WHERE, default="",
                      help="Where runs read the policy (default: from the plan).")
    init.add_argument("--notes", default="")
    init.add_argument("--force", action="store_true",
                      help="Replace an existing strategy.")

    upd = sub.add_parser("update", help="Record a step's status or the user's presence.")
    upd.add_argument("--mode", choices=tuple(MODES))
    upd.add_argument("--status", choices=STATUSES)
    upd.add_argument("--result", default="", help="One-line outcome of the step.")
    upd.add_argument("--present", type=_yes_no)
    upd.add_argument("--unattended", type=_yes_no)

    pol = sub.add_parser("policy", help="Write the PDF_* policy constants into "
                         "the project's screening_config.py.")
    pol.add_argument("--config", default=DEFAULT_PROJECT_CONFIG)
    pol.add_argument("--min-version", choices=pdf_policy.MIN_VERSIONS)
    pol.add_argument("--reconstructed", type=_yes_no)
    pol.add_argument("--paid", choices=pdf_policy.PAID_MODES)
    return p


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    path = Path(args.file)
    try:
        if args.cmd == "show":
            strategy = load(path)
            if strategy is None:
                print(f"No strategy yet ({path}). Plan first (enrich_pdfs.py "
                      "--plan-strategy), agree the order with the user, then init.")
                print("STRATEGY_JSON: " + json.dumps({"exists": False}))
                return 0
            _print_view({**view(strategy), "exists": True})
            return 0

        if args.cmd == "init":
            if path.exists() and not args.force:
                print(f"ERROR: {path} exists. Show it and resume, or pass --force "
                      "to replace it.", file=sys.stderr)
                return 2
            strategy = new_strategy(
                parse_order(args.order), present=args.present,
                unattended=args.unattended, library=args.library,
                institution=args.institution, scope_keys_file=args.scope_keys_file,
                plan=Path(args.plan),
                policy={"min_version": args.min_version,
                        "reconstructed": args.reconstructed, "paid": args.paid},
                where=args.policy_where, notes=args.notes,
            )
            save(strategy, path)
            print(f"Wrote {path}.")
            _print_view({**view(strategy), "exists": True})
            return 0

        if args.cmd == "update":
            strategy = load(path)
            if strategy is None:
                print(f"ERROR: no strategy at {path}; init one first.", file=sys.stderr)
                return 2
            if args.mode is None and args.present is None and args.unattended is None:
                print("ERROR: nothing to update: give --mode/--status, --present "
                      "or --unattended.", file=sys.stderr)
                return 2
            if (args.mode is None) != (args.status is None):
                print("ERROR: --mode and --status go together.", file=sys.stderr)
                return 2
            if args.mode:
                update_step(strategy, args.mode, args.status, args.result)
            if args.present is not None:
                strategy["user_present"] = args.present
            if args.unattended is not None:
                strategy["unattended_possible"] = args.unattended
            save(strategy, path)
            _print_view({**view(strategy), "exists": True})
            return 0

        if args.cmd == "policy":
            policy = {"min_version": args.min_version,
                      "reconstructed": args.reconstructed, "paid": args.paid}
            if all(v is None for v in policy.values()):
                print("ERROR: give at least one of --min-version, "
                      "--reconstructed, --paid.", file=sys.stderr)
                return 2
            config = Path(args.config)
            try:
                written = write_project_policy(config, policy)
            except FileNotFoundError:
                print(
                    f"ERROR: no {config} in this project, so there is no project "
                    "tier to write. For a library-wide default, the user runs "
                    "/setup (it asks the three [pdf] questions); for this "
                    "retrieval only, record the policy in the strategy with "
                    "--policy-where strategy.", file=sys.stderr)
                return 3
            for line in written:
                print(f"  {config}: {line}")
            strategy = load(path)
            if strategy is not None:
                pol = strategy["policy"]
                pol.update({k: pdf_policy.coerce(k, v) for k, v in policy.items()
                            if v is not None})
                pol["where"] = "project"
                save(strategy, path)
                print(f"  {path}: policy recorded as stored in the project.")
            return 0
    except (StrategyError, pdf_policy.PolicyError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    return 2  # pragma: no cover — argparse requires a subcommand


if __name__ == "__main__":
    sys.exit(main())
