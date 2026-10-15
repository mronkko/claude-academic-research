"""Protocol-level tests for the plugin's MCP server (`mcp/server.py`).

Each test spawns the server as a subprocess and talks JSON-RPC to it over
stdio, the way an MCP host does. The `uv run` launcher is replaced through
`ACADEMIC_RESEARCH_MCP_LAUNCHER` with a stub that records the argv and cwd
it was given and prints canned output, so no pipeline script runs and
nothing touches Zotero or the network. Two tests point the launcher at
plain `python` instead and run a real stdlib-only script end to end, which
is what proves the summarisers parse the scripts' actual output.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
SERVER = REPO / "mcp" / "server.py"

STUB = r'''
import json, os, sys
record = os.environ["MCP_STUB_RECORD"]
with open(record, "a", encoding="utf-8") as f:
    f.write(json.dumps({"argv": sys.argv[1:], "cwd": os.getcwd()}) + "\n")
sys.stdout.write(os.environ.get("MCP_STUB_OUTPUT", ""))
sys.exit(int(os.environ.get("MCP_STUB_EXIT", "0")))
'''


@pytest.fixture
def project(tmp_path: Path) -> Path:
    p = tmp_path / "project"
    p.mkdir()
    return p


@pytest.fixture
def stub(tmp_path: Path) -> Path:
    path = tmp_path / "stub_launcher.py"
    path.write_text(STUB, encoding="utf-8")
    return path


def _env(project: Path | None, launcher: list[str], record: Path,
         output: str = "", exit_code: int = 0) -> dict:
    env = dict(os.environ)
    env.pop("CLAUDE_PROJECT_DIR", None)
    if project is not None:
        env["CLAUDE_PROJECT_DIR"] = str(project)
    env["ACADEMIC_RESEARCH_MCP_LAUNCHER"] = json.dumps(launcher)
    env["MCP_STUB_RECORD"] = str(record)
    env["MCP_STUB_OUTPUT"] = output
    env["MCP_STUB_EXIT"] = str(exit_code)
    return env


def _session(messages: list, env: dict, cwd: Path | None = None) -> list[dict]:
    """Send `messages` (dicts, or raw strings sent verbatim) and return the
    replies in order."""
    lines = [m if isinstance(m, str) else json.dumps(m) for m in messages]
    proc = subprocess.run(
        [sys.executable, str(SERVER)],
        input="\n".join(lines) + "\n",
        capture_output=True, text=True, encoding="utf-8",
        env=env, cwd=cwd, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]


def _init(id_: int = 0, version: str = "2025-06-18") -> dict:
    return {"jsonrpc": "2.0", "id": id_, "method": "initialize",
            "params": {"protocolVersion": version, "capabilities": {},
                       "clientInfo": {"name": "test", "version": "0"}}}


def _call(id_: int, name: str, arguments: dict | None = None) -> dict:
    params: dict = {"name": name}
    if arguments is not None:
        params["arguments"] = arguments
    return {"jsonrpc": "2.0", "id": id_, "method": "tools/call", "params": params}


def _payload(reply: dict) -> dict:
    result = reply["result"]
    payload = json.loads(result["content"][0]["text"])
    assert result["isError"] is (not payload["ok"])
    return payload


def _records(record: Path) -> list[dict]:
    if not record.exists():
        return []
    return [json.loads(line) for line in record.read_text(encoding="utf-8").splitlines()]


# ---------------------------------------------------------------------------
# Handshake and discovery
# ---------------------------------------------------------------------------


def test_initialize_then_tools_list(project, stub, tmp_path) -> None:
    replies = _session([
        _init(1),
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {"jsonrpc": "2.0", "id": 3, "method": "ping"},
    ], _env(project, [sys.executable, str(stub)], tmp_path / "rec"))

    # The notification gets no reply; the three requests do, in order.
    assert [r["id"] for r in replies] == [1, 2, 3]
    init = replies[0]["result"]
    assert init["protocolVersion"] == "2025-06-18"
    assert init["capabilities"] == {"tools": {"listChanged": False}}
    assert init["serverInfo"]["name"] == "academic-research"
    assert replies[2]["result"] == {}

    tools = replies[1]["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == [
        "check_setup", "scaffold_project", "pdf_plan", "pdf_strategy_show",
        "pdf_strategy_write", "pdf_strategy_update", "pdf_policy_write",
        "pdf_consent_show", "pdf_consent_set", "enrich_pdfs_plan",
        "enrich_pdfs_report", "pdf_manual_next", "pdf_manual_collect",
        "pdf_manual_assign", "pdf_manual_ignore", "screening_report", "audit_library",
        "export_coded_includes", "generate_bib",
        "start_pdf_plan", "start_enrich_pdfs", "start_enrich_pdfs_paid",
        "start_pdf_unattended_setup", "start_pdf_unattended_run",
        "start_search", "start_import", "start_enrich_abstracts",
        "start_enrich_dois", "start_abstract_screen", "start_fulltext_code",
        "job_status", "job_answer", "cancel_job", "list_jobs",
    ]
    for t in tools:
        assert t["description"].startswith((
            "[read-only] ", "[writes project files] ", "[writes user config] ",
            "[writes Zotero] ", "[costs LLM credits] ", "[costs money] ",
        )), t["name"]
        assert t["inputSchema"]["type"] == "object"
        assert t["inputSchema"]["additionalProperties"] is False
        assert set(t["annotations"]) >= {
            "readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint",
        }
        # Decided in the P21 spec: no forced per-call prompt, so users can
        # choose "always allow" per tool.
        assert "_meta" not in t
    assert _records(tmp_path / "rec") == [], "listing tools must run nothing"


def test_unknown_protocol_version_gets_the_newest(project, stub, tmp_path) -> None:
    [reply] = _session([_init(1, "1999-01-01")],
                       _env(project, [sys.executable, str(stub)], tmp_path / "rec"))
    assert reply["result"]["protocolVersion"] == "2025-06-18"


def test_protocol_errors(project, stub, tmp_path) -> None:
    replies = _session([
        "{not json",
        {"jsonrpc": "2.0", "id": 1, "method": "resources/list"},
        _call(2, "no_such_tool", {}),
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": []},
    ], _env(project, [sys.executable, str(stub)], tmp_path / "rec"))
    codes = [r["error"]["code"] for r in replies]
    assert codes == [-32700, -32601, -32602, -32602]


# ---------------------------------------------------------------------------
# tools/call through the stubbed launcher
# ---------------------------------------------------------------------------


def test_call_builds_argv_and_runs_in_project(project, stub, tmp_path) -> None:
    (project / "screening").mkdir()
    (project / "screening" / "a.csv").write_text("item_key\n", encoding="utf-8")
    record = tmp_path / "rec"
    output = ("Log: x\n  Rows in file:       7\n  Unique items:       5\n\n"
              "  Latest decisions (last-row-wins):\n    include     2\n"
              "    borderline  1\n    exclude     2\n    error       0\n")
    [reply] = _session(
        [_call(1, "screening_report",
               {"log_csv": "screening/a.csv", "list_decision": "include"})],
        _env(project, [sys.executable, str(stub)], record, output=output),
    )
    payload = _payload(reply)
    assert payload["ok"] is True and payload["exit_code"] == 0
    assert payload["summary"] == {
        "rows_in_file": 7, "unique_items": 5,
        "latest_decisions": {"include": 2, "borderline": 1, "exclude": 2, "error": 0},
    }
    assert payload["log_path"] == ".claude/mcp/logs/screening_report.log"
    assert (project / payload["log_path"]).read_text(encoding="utf-8").endswith(output)

    [run] = _records(record)
    script, *args = run["argv"]
    assert Path(script) == REPO / "scripts" / "pipelines" / "screening_report.py"
    assert args == [str((project / "screening" / "a.csv").resolve()), "--list", "include"]
    assert Path(run["cwd"]).resolve() == project.resolve()


def test_map_parameter_and_fixed_flags(project, stub, tmp_path) -> None:
    record = tmp_path / "rec"
    _session([_call(1, "enrich_pdfs_plan", {"mode": "all", "group": "123",
                                            "institution": ["aalto", "jyu"]})],
             _env(project, [sys.executable, str(stub)], record))
    [run] = _records(record)
    assert run["argv"][0].endswith("enrich_pdfs.py")
    assert run["argv"][1:] == ["--plan", "--all", "--group", "123",
                               "--library", "aalto", "--library", "jyu"]


def test_default_mode_and_project_default(project, stub, tmp_path) -> None:
    record = tmp_path / "rec"
    _session([_call(1, "enrich_pdfs_plan", {}),
              _call(2, "scaffold_project", {}),
              _call(3, "generate_bib", {})],
             _env(project, [sys.executable, str(stub)], record))
    plan, scaffold, bib = _records(record)
    assert plan["argv"][1:] == ["--plan", "--sources", "browser"]
    assert scaffold["argv"][1:] == ["--kind", "sr", "--target", str(project.resolve())]
    assert bib["argv"][1:] == [str(project.resolve())]


def test_nonzero_exit_is_not_ok_and_tail_is_bounded(project, stub, tmp_path) -> None:
    output = "".join(f"line {i}\n" for i in range(500))
    [reply] = _session([_call(1, "enrich_pdfs_report", {})],
                       _env(project, [sys.executable, str(stub)], tmp_path / "rec",
                            output=output, exit_code=3))
    payload = _payload(reply)
    assert payload["ok"] is False and payload["exit_code"] == 3
    assert payload["log_tail"].splitlines()[-1] == "line 499"
    assert len(payload["log_tail"].splitlines()) <= 40
    assert "line 0\n" not in payload["log_tail"]


def test_check_setup_stops_when_not_configured(project, stub, tmp_path) -> None:
    record = tmp_path / "rec"
    [reply] = _session([_call(1, "check_setup")],
                       _env(project, [sys.executable, str(stub)], record,
                            output="NOT CONFIGURED\n"))
    payload = _payload(reply)
    assert payload["summary"]["configured"] is False
    assert "terminal" in payload["summary"]["next_step"]
    assert len(_records(record)) == 1, "no database probe without a config"


def test_check_setup_reports_databases(project, stub, tmp_path) -> None:
    record = tmp_path / "rec"
    [reply] = _session([_call(1, "check_setup")],
                       _env(project, [sys.executable, str(stub)], record,
                            output="configured\nscopus: available\n"
                                   "zotero-mcp-server: 0.9.1 — Claude Code (user scope)\n"
                                   "WARNING: below floor\n"))
    payload = _payload(reply)
    assert payload["summary"]["configured"] is True
    assert payload["summary"]["databases"]["scopus"] == "available"
    assert payload["summary"]["zotero_mcp_server"] == ["0.9.1 — Claude Code (user scope)"]
    assert payload["summary"]["zotero_mcp_warnings"] == ["WARNING: below floor"]
    scripts = [Path(r["argv"][0]).name for r in _records(record)]
    assert scripts == ["check_configured.py", "check_database_access.py",
                       "check_zotero_mcp_version.py"]


def test_project_dir_falls_back_to_cwd(project, stub, tmp_path) -> None:
    record = tmp_path / "rec"
    _session([_call(1, "generate_bib", {})],
             _env(None, [sys.executable, str(stub)], record), cwd=project)
    [run] = _records(record)
    assert run["argv"][1:] == [str(project.resolve())]


# ---------------------------------------------------------------------------
# Argument validation: refused before anything runs
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name,arguments,needle", [
    ("screening_report", {"log_csv": "../outside.csv"}, "outside the project"),
    ("screening_report", {"log_csv": str(Path("/").resolve() / "etc" / "passwd")},
     "outside the project"),
    ("screening_report", {}, "log_csv is required"),
    ("screening_report", {"log_csv": "a.csv", "bogus": 1}, "unknown argument"),
    ("screening_report", {"log_csv": "a.csv", "list_decision": "maybe"}, "one of"),
    ("screening_report", {"log_csv": "a.csv", "list_rescreened": "yes"}, "boolean"),
    ("enrich_pdfs_plan", {"publisher": "--all"}, "may not start with '-'"),
    ("enrich_pdfs_plan", {"institution": ["-x"]}, "may not start with '-'"),
    ("enrich_pdfs_plan", {"group": "12; rm -rf /"}, "does not match"),
    ("enrich_pdfs_plan", {"group": "1", "user": True}, "cannot be combined"),
    ("audit_library", {}, "one of group, user is required"),
    ("export_coded_includes", {"collection": "ABCD1234", "out": "../x.csv"},
     "outside the project"),
    ("check_setup", {"anything": True}, "unknown argument"),
])
def test_invalid_arguments_are_refused(project, stub, tmp_path, name, arguments,
                                       needle) -> None:
    record = tmp_path / "rec"
    [reply] = _session([_call(1, name, arguments)],
                       _env(project, [sys.executable, str(stub)], record))
    payload = _payload(reply)
    assert payload["ok"] is False
    assert needle in payload["summary"]["error"]
    assert _records(record) == [], "nothing may run after a refused argument"


def test_symlink_out_of_project_is_refused(project, stub, tmp_path) -> None:
    outside = tmp_path / "outside.csv"
    outside.write_text("item_key\n", encoding="utf-8")
    link = project / "link.csv"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("symlinks unavailable here")
    record = tmp_path / "rec"
    [reply] = _session([_call(1, "screening_report", {"log_csv": "link.csv"})],
                       _env(project, [sys.executable, str(stub)], record))
    assert "outside the project" in _payload(reply)["summary"]["error"]
    assert _records(record) == []


# ---------------------------------------------------------------------------
# Real stdlib scripts, end to end (launcher = this Python, no uv)
# ---------------------------------------------------------------------------


def test_real_screening_report(project, tmp_path) -> None:
    log = project / "screening.csv"
    log.write_text(
        "item_key,decision,title\nA,include,Foo\nB,exclude,Bar\n"
        "A,exclude,Foo\nC,borderline,Baz\n",
        encoding="utf-8",
    )
    [reply] = _session(
        [_call(1, "screening_report", {"log_csv": "screening.csv",
                                       "list_rescreened": True})],
        _env(project, [sys.executable], tmp_path / "rec"),
    )
    payload = _payload(reply)
    assert payload["summary"] == {
        "rows_in_file": 4, "unique_items": 3,
        "latest_decisions": {"include": 0, "borderline": 1, "exclude": 2, "error": 0},
    }
    assert "A  [2 rows" in payload["log_tail"]


def test_real_scaffold_project(project, tmp_path) -> None:
    env = _env(project, [sys.executable], tmp_path / "rec")
    [first, second] = _session(
        [_call(1, "scaffold_project", {"kind": "manuscript"}),
         _call(2, "scaffold_project", {"kind": "manuscript"})],
        env,
    )
    p1, p2 = _payload(first), _payload(second)
    assert "manuscript/" in p1["summary"]["created_dirs"]
    assert "CLAUDE.md" in p1["summary"]["copied_templates"]
    assert p1["artifacts"] == p1["summary"]["copied_templates"]
    assert (project / "manuscript" / "manuscript.qmd").is_file()
    # Re-running is a no-op that overwrites nothing.
    assert p2["summary"]["copied_templates"] == []
    assert p2["summary"]["kept_existing"] == len(p1["summary"]["copied_templates"])


# --- manual PDF download tools ----------------------------------------------

MANUAL_OUTPUT = (
    "Manual download — next:\n"
    "  Unmatched 1 — NOT attached; tell me which item each is:\n"
    "    random.pdf: no DOI, title or file-name hint matches\n"
    "MANUAL: action=next opened=25 batch=3 login_first=0 collected=7 "
    "in_progress=1 attached=5 duplicates=1 rejected=0 below_policy=0 "
    "unmatched=1 remaining=120 unopened=95\n"
)


def test_pdf_manual_next_argv_and_summary(project, stub, tmp_path) -> None:
    record = tmp_path / "rec"
    [reply] = _session(
        [_call(1, "pdf_manual_next", {"group": "123", "batch_size": "10",
                                      "institution": ["aalto"]})],
        _env(project, [sys.executable, str(stub)], record, output=MANUAL_OUTPUT))
    [run] = _records(record)
    assert run["argv"][1:] == ["--manual", "next", "--group", "123",
                               "--library", "aalto", "--batch-size", "10"]
    payload = _payload(reply)
    assert payload["summary"]["attached"] == 5
    assert payload["summary"]["unmatched_files"] == ["random.pdf"]
    assert "output/pdf_staging/unmatched.json" in payload["artifacts"]


def test_pdf_manual_assign_confines_the_file(project, stub, tmp_path) -> None:
    record = tmp_path / "rec"
    staged = project / "output" / "pdf_staging" / "batch-1"
    staged.mkdir(parents=True)
    (staged / "x.pdf").write_bytes(b"%PDF")
    _session([_call(1, "pdf_manual_assign", {
        "file": "output/pdf_staging/batch-1/x.pdf", "item_key": "ABCD1234"})],
        _env(project, [sys.executable, str(stub)], record))
    [run] = _records(record)
    assert run["argv"][1:] == [
        "--manual", "assign", "--assign-file",
        str((staged / "x.pdf").resolve()), "--assign-item", "ABCD1234"]
    [reply] = _session([_call(2, "pdf_manual_assign", {
        "file": "/etc/passwd", "item_key": "ABCD1234"})],
        _env(project, [sys.executable, str(stub)], record))
    assert "outside the project" in json.dumps(reply)


def test_pdf_manual_batch_size_must_be_a_number(project, stub, tmp_path) -> None:
    [reply] = _session([_call(1, "pdf_manual_next", {"batch_size": "lots"})],
                       _env(project, [sys.executable, str(stub)], tmp_path / "rec"))
    assert "does not match" in json.dumps(reply)


# --- PDF retrieval planning and strategy (retrieving-pdfs skill) ------------

PLAN_OUTPUT = (
    "PDF retrieval plan — group:123 (lookups: none)\n"
    "PLAN: queue=40 with_pdf=60 no_doi=2 route_api=10 route_api_untried=5 "
    "route_browser=12 route_manual=11 route_no_doi=2 fallback_browser=20 "
    "fallback_manual=18 fallback_no_doi=2 paid_items=0 paid_known=0 "
    "below_policy=1 lookups=none\n"
)


def test_pdf_plan_argv_and_summary(project, stub, tmp_path) -> None:
    record = tmp_path / "rec"
    plan = project / ".claude" / "pdf" / "plan.json"
    plan.parent.mkdir(parents=True)
    plan.write_text(json.dumps({
        "browser_publishers": {"sage": {"display": "Sage", "items": 12,
                                        "needs_solve": True}},
        "paid_openalex": {"known": False, "unchecked": 38},
        "manual_queue": 38,
        "keys_files": {"browser": ".claude/pdf/plan.browser.keys"},
        "items": [{"key": "X"}],
    }), encoding="utf-8")
    [reply] = _session(
        [_call(1, "pdf_plan", {"group": "123", "lookups": "openalex"})],
        _env(project, [sys.executable, str(stub)], record, output=PLAN_OUTPUT))
    [run] = _records(record)
    assert run["argv"][0].endswith("enrich_pdfs.py")
    assert run["argv"][1:] == ["--plan-strategy", "--plan-lookups", "openalex",
                               "--group", "123"]
    summary = _payload(reply)["summary"]
    assert summary["routes"] == {"api": 10, "api_untried": 5, "browser": 12,
                                 "manual": 11, "no_doi": 2}
    assert summary["fallback_routes"]["manual"] == 18
    assert summary["queue"] == 40 and summary["lookups"] == "none"
    assert summary["browser_publishers"]["sage"]["items"] == 12
    assert "items" not in summary, "per-item rows stay in the file"
    assert _payload(reply)["artifacts"] == [".claude/pdf/plan.json",
                                           ".claude/pdf/plan.browser.keys"]


def test_pdf_plan_refuses_the_resolver_sweep(project, stub, tmp_path) -> None:
    """The short tool has a 10-minute limit; `all` is start_pdf_plan's."""
    [reply] = _session([_call(1, "pdf_plan", {"lookups": "all"})],
                       _env(project, [sys.executable, str(stub)], tmp_path / "rec"))
    assert "one of" in _payload(reply)["summary"]["error"]


def test_real_pdf_strategy_round_trip(project, tmp_path) -> None:
    """The stdlib strategy helper end to end: write, update, show."""
    (project / "screening_config.py").write_text(
        '# PDF_MIN_VERSION = "accepted"\n', encoding="utf-8")
    env = _env(project, [sys.executable], tmp_path / "rec")
    replies = _session([
        _call(1, "pdf_strategy_show", {}),
        _call(2, "pdf_strategy_write", {"order": "api,paid,manual",
                                        "present": "yes", "library": "group:5"}),
        _call(3, "pdf_strategy_update", {"mode": "api", "status": "done",
                                         "result": "attached 7"}),
        _call(4, "pdf_policy_write", {"min_version": "published"}),
        _call(5, "pdf_strategy_show", {}),
        _call(6, "pdf_strategy_write", {"order": "manual", "present": "no"}),
    ], env)
    first, written, updated, policy, shown, again = map(_payload, replies)
    assert first["summary"] == {"exists": False}
    assert written["summary"]["next"]["mode"] == "api"
    assert written["artifacts"] == [".claude/pdf/strategy.toml"]
    assert updated["summary"]["next"]["mode"] == "paid"
    assert policy["summary"]["written"] == ['PDF_MIN_VERSION = "published"']
    assert shown["summary"]["policy"]["where"] == "project"
    assert shown["summary"]["steps"][0] == {"mode": "api", "status": "done",
                                            "result": "attached 7"}
    assert again["ok"] is False   # an existing strategy is not replaced silently
    assert (project / ".claude" / "pdf" / "strategy.toml").is_file()


def test_pdf_strategy_write_validates_the_order(project, stub, tmp_path) -> None:
    [reply] = _session([_call(1, "pdf_strategy_write",
                              {"order": "api;rm", "present": "yes"})],
                       _env(project, [sys.executable, str(stub)], tmp_path / "rec"))
    assert "does not match" in _payload(reply)["summary"]["error"]


def test_real_pdf_consent_round_trip(project, tmp_path) -> None:
    """The stdlib consent helper end to end: show, set, show. The suite's
    HOME redirect reaches the subprocess, so the write lands in a scratch
    config.toml, never the real one."""
    import tomllib
    home = Path(os.environ["HOME"])
    env = _env(project, [sys.executable], tmp_path / "rec")
    replies = _session([
        _call(1, "pdf_consent_show", {"site": ["sage"]}),
        _call(2, "pdf_consent_set", {"site": "sage", "automated": "deny"}),
        _call(3, "pdf_consent_show", {"site": ["sage"]}),
        _call(4, "pdf_consent_set", {"site": "Sage Journals", "automated": "allow"}),
        _call(5, "pdf_consent_set", {"site": "sage", "automated": "yes"}),
    ], env)
    before, recorded, after, bad_site, bad_answer = map(_payload, replies)
    assert before["summary"]["sites"][0]["automated"] == "ask"
    assert before["summary"]["sites"][0]["stored"] is False
    assert recorded["summary"]["recorded"]["automated"] == "deny"
    assert after["summary"]["sites"][0]["automated"] == "deny"
    assert after["summary"]["sites"][0]["confirmed"]
    assert bad_site["ok"] is False and bad_answer["ok"] is False
    cfg = tomllib.loads(
        (home / ".config" / "academic-research" / "config.toml").read_text())
    assert cfg["publisher_consent"]["sage"]["automated"] == "deny"
