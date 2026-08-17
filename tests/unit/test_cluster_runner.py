"""The cluster runner's pure functions, and the two rules it exists under.

`scripts/cluster/run_batch.py` is the one file in this repository that
ships somewhere the rest of it cannot reach. It is copied to a GPU
cluster on its own, next to a manifest, and run by a scheduler. Two
consequences are load-bearing enough to be tested rather than documented:

**It must import nothing from this plugin.** An import that works here
resolves against a checkout that does not exist there.
`test_the_runner_imports_only_the_standard_library_and_vllm` walks the
AST rather than the text, so a mention in a docstring is fine and an
actual `import` is not.

**Its duplicated schema constants must agree with `batch_manifest`.**
Duplication is the price of the first rule; drift is what makes the price
too high. The run-record comparison is the sharp end: the applier reads
`degenerate_output` off a record this file writes, so the two must
compute it the same way.

The rest is the pure functions — reasoning splits, output budgets, the
context-window fit, the chat-template fallback ladder — each of which
encodes a failure the reference project hit on a real job.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
import types
from pathlib import Path

import batch_manifest as bm
import pytest

REPO = Path(__file__).resolve().parents[2]
RUNNER_PATH = REPO / "scripts" / "cluster" / "run_batch.py"


def _load_runner():
    """Load the runner by file path — the way the cluster is told to.

    Not `import scripts.cluster.run_batch`: that is precisely the
    invocation the module docstring forbids, and a test that used it
    would be exercising a path no user has.
    """
    spec = importlib.util.spec_from_file_location("cluster_run_batch", RUNNER_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


# ---------------------------------------------------------------------------
# The two rules
# ---------------------------------------------------------------------------

#: Everything the runner may import. `vllm` is the only third-party name,
#: and it is lazy — inside `execute()` — so `--dry-run` works on a login
#: node with no GPU stack installed.
ALLOWED_IMPORTS = {
    "argparse", "gzip", "hashlib", "json", "os", "platform", "re",
    "subprocess", "sys", "time", "datetime", "vllm", "__future__",
}


def test_the_runner_imports_only_the_standard_library_and_vllm() -> None:
    """No plugin import may creep in.

    The runner is copied to a cluster on its own. `import batch_manifest`
    would work in this repository and fail there, in a job, after the
    queue wait — which is the most expensive place to discover it.
    """
    tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                imported.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # a relative import needs a package: never valid here
                raise AssertionError(f"relative import in {RUNNER_PATH.name}")
            imported.add((node.module or "").split(".")[0])
    assert imported <= ALLOWED_IMPORTS, (
        f"{RUNNER_PATH.name} imports {sorted(imported - ALLOWED_IMPORTS)}, which "
        f"will not exist on the cluster it is copied to."
    )


def test_vllm_is_imported_lazily_so_dry_run_works_without_it() -> None:
    """`--dry-run` on a login node must not need the GPU stack.

    The pre-flight summary is what a user checks before spending a
    queue slot, so it has to run where they are, which is a login node
    with no vLLM installed.
    """
    tree = ast.parse(RUNNER_PATH.read_text(encoding="utf-8"))
    top_level = {
        alias.name.split(".")[0]
        for node in tree.body
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
    }
    assert "vllm" not in top_level


def test_the_runner_is_not_invoked_as_a_module_anywhere() -> None:
    """`python3 -m scripts.cluster.run_batch` must appear nowhere.

    `scripts` is an ordinary directory name and a cluster's own software
    stack very likely has one; the reference project lost the name and
    PYTHONPATH did not win it back. By file path there is no package to
    resolve.
    """
    for path in sorted((REPO / "scripts" / "cluster").iterdir()):
        if path.is_dir():
            continue
        # Prose names the forbidden form in order to forbid it, so only
        # what actually executes is checked for using it.
        code = _executable_lines(path)
        assert "-m scripts.cluster" not in code, f"{path.name} invokes the runner as a module"
    readme = (REPO / "scripts" / "cluster" / "README.md").read_text(encoding="utf-8")
    assert "by file path" in readme, "the README must state the rule it exists to carry"


def _executable_lines(path: Path) -> str:
    """`path` with comments, docstrings and Markdown prose removed."""
    if path.suffix == ".md":
        return ""
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".py":
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                node.value = ""
        return ast.unparse(tree)
    return "\n".join(
        line for line in text.splitlines() if not line.lstrip().startswith("#")
    )


def test_the_runner_ships_no_pep_723_header() -> None:
    """No `uv run`: a compute node has neither uv nor outbound network."""
    head = RUNNER_PATH.read_text(encoding="utf-8")[:2000]
    assert "/// script" not in head


def test_python_39_compatible_syntax() -> None:
    """Cluster interpreters are routinely older than this plugin's 3.11 floor.

    Compiling the source under 3.9's feature version catches the 3.10+
    syntax (`match`, PEP 604 unions outside annotations) that would fail
    at import on the node rather than here.
    """
    source = RUNNER_PATH.read_text(encoding="utf-8")
    ast.parse(source, filename=str(RUNNER_PATH), feature_version=(3, 9))


#: Runtime APIs newer than 3.9, which parse fine at any feature version
#: and then fail on the cluster. Every entry earned its place: a
#: `ruff check --fix --unsafe-fixes` pass over this file introduced
#: `datetime.UTC` and `zip(..., strict=False)`, and only running the
#: result on a real 3.9 interpreter found the second.
POST_39_APIS = (
    "datetime import UTC",
    "datetime.UTC",
    "tomllib",
    "strict=",          # zip(..., strict=) is 3.10+
    "itertools.pairwise",
    "importlib.resources.files",
)


def test_no_stdlib_api_newer_than_python_39() -> None:
    """Syntax is not the whole of compatibility.

    `test_python_39_compatible_syntax` parses the file at 3.9's feature
    version, which catches new *grammar* and nothing else. A keyword
    argument added to a builtin in 3.10 parses perfectly and raises
    `TypeError` at the call — inside a queued job, after the wait, with
    the allocation already spent. The rest of this repository uses these
    names freely and correctly, which is exactly why this one file needs
    the guard.
    """
    source = RUNNER_PATH.read_text(encoding="utf-8")
    # The prohibitions may be named in prose in order to prohibit them.
    code = _executable_lines(RUNNER_PATH)
    for name in POST_39_APIS:
        assert name not in code, (
            f"{name} is newer than Python 3.9 and would fail on the cluster"
        )
    assert "timezone.utc" in source, "the 3.9-safe UTC spelling should be in use"


# ---------------------------------------------------------------------------
# Agreement with batch_manifest
# ---------------------------------------------------------------------------


def test_schema_constants_match_the_plugin() -> None:
    assert runner.SCHEMA_VERSION == bm.SCHEMA_VERSION
    assert runner.DEGENERATE_OUTPUT_TOKENS == bm.DEGENERATE_OUTPUT_TOKENS
    assert runner.REASONING_MARKERS == bm.REASONING_MARKERS


@pytest.mark.parametrize(
    "text",
    [
        "",
        "DECISION: include",
        "thinking hard</think>DECISION: include",
        "analysisassistantfinalDECISION: exclude",
        "<|start|>assistant<|channel|>final<|message|>DECISION: include",
        "<|im_start|>assistantDECISION: borderline",
        "first</think>second</think>DECISION: include",
    ],
)
def test_split_reasoning_agrees_with_the_plugin(text: str) -> None:
    """Both copies must find the same answer in the same stream.

    The applier re-splits `response_text` with `batch_manifest`'s copy;
    the runner splits it here for the `reasoning_text` column. A
    disagreement would put one thing in the file and score another.
    """
    assert runner.split_reasoning(text) == bm.split_reasoning(text)


def test_the_run_record_matches_what_the_plugin_would_have_written() -> None:
    """The applier refuses on `degenerate_output` from a record this file writes.

    Comparing the whole record, not just the flag: the run record is also
    what a user reads to size the next manifest, and the two executors
    should describe the same run the same way.
    """
    responses = [
        {"call_status": "ok", "input_tokens": 300, "output_tokens": 40},
        {"call_status": "ok", "input_tokens": 320, "output_tokens": 44},
        {"call_status": "error", "input_tokens": 0, "output_tokens": 0},
    ]
    mine = runner.summarise("run-1", "abstract_screening", "org/m", responses)
    theirs = bm.summarise_run(
        "run-1", stage="abstract_screening", model="org/m", responses=responses,
    )
    mine.pop("recorded_at")
    theirs.pop("recorded_at")
    assert mine == theirs


def test_a_degenerate_run_is_flagged_identically_by_both() -> None:
    responses = [
        {"call_status": "ok", "input_tokens": 300, "output_tokens": 1},
        {"call_status": "ok", "input_tokens": 300, "output_tokens": 1},
    ]
    mine = runner.summarise("run-1", "abstract_screening", "org/m", responses)
    theirs = bm.summarise_run(
        "run-1", stage="abstract_screening", model="org/m", responses=responses,
    )
    assert mine["degenerate_output"] is True
    assert mine["degenerate_output_note"] == theirs["degenerate_output_note"]
    # And the applier, handed this record, stops.
    with pytest.raises(SystemExit):
        bm.refuse_if_degenerate(mine, force=False)


def test_an_all_empty_run_is_degenerate() -> None:
    """The classic unframed-prompt signature: answers, all of them nothing."""
    responses = [{"call_status": "empty", "output_tokens": 0} for _ in range(20)]
    record = runner.summarise("run-1", "abstract_screening", "org/m", responses)
    assert record["degenerate_output"] is True


def test_model_slug_matches_the_plugin() -> None:
    """The default responses filename is built from it on both sides."""
    for model in ("Org/Model-3-30B-A3B", "plain", "", "a/b/c"):
        assert runner.model_slug(model) == bm.model_slug(model)


# ---------------------------------------------------------------------------
# Output budgets
# ---------------------------------------------------------------------------


def test_output_budget_comes_from_the_row() -> None:
    """Per request, not one flat number for the batch.

    An abstract decision is 200 tokens and a full-text coding response
    over a 30-field schema is thousands; one flat budget either truncates
    the large ones or pays decode headroom on every small one.
    """
    assert runner.output_budget({"max_output_tokens": 200}) == 200
    assert runner.output_budget({"max_output_tokens": 4096}) == 4096


def test_output_budget_falls_back_when_the_row_says_nothing() -> None:
    assert runner.output_budget({}) == runner.DEFAULT_OUTPUT_BUDGET
    assert runner.output_budget({"max_output_tokens": None}) == runner.DEFAULT_OUTPUT_BUDGET
    assert runner.output_budget({"max_output_tokens": "junk"}) == runner.DEFAULT_OUTPUT_BUDGET


def test_output_budget_is_capped() -> None:
    assert runner.output_budget({"max_output_tokens": 10 ** 9}) == runner.MAX_OUTPUT_BUDGET


@pytest.mark.parametrize(
    "model,factor",
    [
        ("org/gpt-oss-20b", 3),
        ("org/DeepSeek-R1-Distill", 3),
        ("org/QwQ-32B", 3),
        ("org/model-thinking", 3),
        ("org/ordinary-instruct", 1),
        ("", 1),
    ],
)
def test_reasoning_models_get_a_larger_budget(model: str, factor: int) -> None:
    """A reasoning model writes its trace into the same budget as its answer.

    Sized for the answer alone, the answer is what gets truncated — and a
    truncated answer is unparseable, so it scores as a schema failure
    rather than as the budget failure it is.
    """
    assert runner.reasoning_factor(model) == factor
    assert runner.output_budget({"max_output_tokens": 200}, factor) == 200 * factor


# ---------------------------------------------------------------------------
# Fitting the context window
# ---------------------------------------------------------------------------


def test_a_request_that_fits_is_sent_unchanged() -> None:
    budget, note = runner.fit_budget(1000, 200, 32768)
    assert (budget, note) == (200, "")


def test_a_budget_that_overflows_is_trimmed_not_refused() -> None:
    """There is room for an answer, just less than asked. Say so and send it."""
    budget, note = runner.fit_budget(32000, 4096, 32768)
    assert budget == 768
    assert "reduced from 4096 to 768" in note


def test_a_prompt_longer_than_the_context_is_not_sent() -> None:
    """Refused here, where it is a fact about the manifest.

    Sent, it fails inside vLLM, where the traceback reads as a model
    problem and takes the rest of the batch with it.
    """
    budget, note = runner.fit_budget(40000, 200, 32768)
    assert budget is None
    assert "40000 tokens" in note and "32768" in note
    assert "--max-input-chars" in note


def test_a_prompt_leaving_no_room_for_an_answer_is_not_sent() -> None:
    """Twenty tokens of answer is an unparseable answer.

    It would be recorded as an error either way; refusing before
    generation just declines to spend the GPU time first.
    """
    budget, note = runner.fit_budget(32760, 200, 32768)
    assert budget is None
    assert f"below the {runner.MIN_OUTPUT_BUDGET}" in note


# ---------------------------------------------------------------------------
# --check-imports: the login-node question about the environment
#
# Added after live validation, where a broken module stack passed
# `--dry-run` (which imports nothing) and then died nine minutes into a
# real allocation, twice. Importing vLLM needs no GPU, so this is a
# question the login node can answer for free.
# ---------------------------------------------------------------------------


class _ImportExplodes:
    """A meta-path finder that fails `import vllm` the way a real site does.

    Modelled on an observed failure: a wheel built against a newer C++
    runtime than the OS provides, which raises `ImportError` naming a
    missing GLIBCXX symbol rather than saying anything about vLLM.
    """

    MESSAGE = "/lib64/libstdc++.so.6: version `GLIBCXX_3.4.31' not found"

    def find_spec(self, name, path=None, target=None):
        if name == "vllm":
            raise ImportError(self.MESSAGE)
        return None


def test_check_imports_reports_the_version_when_the_stack_loads(
    monkeypatch, capsys
) -> None:
    fake = types.ModuleType("vllm")
    fake.__version__ = "0.19.1"
    monkeypatch.setitem(sys.modules, "vllm", fake)

    assert runner.check_imports() == 0
    out = capsys.readouterr().out
    assert "0.19.1" in out
    assert "IMPORTS OK" in out


def test_check_imports_surfaces_the_real_error_and_exits_non_zero(
    monkeypatch, capsys
) -> None:
    """The exit code is the point: a wrapper has to be able to stop.

    And the message has to be the *underlying* one. "vLLM failed to
    import" sends someone to reinstall vLLM; the GLIBCXX line sends them
    to the library path, which is where the fix is.
    """
    monkeypatch.delitem(sys.modules, "vllm", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_ImportExplodes(), *sys.meta_path])

    assert runner.check_imports() == 1
    out = capsys.readouterr().out
    assert "FAILED" in out
    assert "GLIBCXX_3.4.31" in out, "the real cause must survive to the operator"
    assert "ImportError" in out, "the exception class is part of the diagnosis"
    assert "GLIBCXX" in runner.IMPORT_HELP


def test_check_imports_needs_no_manifest(monkeypatch, capsys) -> None:
    """The whole value is answering before a manifest exists.

    `--manifest` stopped being `required=True` for this; the guard below
    keeps that relaxation from leaking into the other two modes.
    """
    fake = types.ModuleType("vllm")
    fake.__version__ = "0.19.1"
    monkeypatch.setitem(sys.modules, "vllm", fake)

    assert runner.main(["--check-imports"]) == 0
    assert "IMPORTS OK" in capsys.readouterr().out


def test_dry_run_still_requires_a_manifest() -> None:
    """The relaxation of `required=True` must not reach --dry-run."""
    with pytest.raises(SystemExit) as excinfo:
        runner.main(["--dry-run"])
    assert "--manifest is required" in str(excinfo.value)


def test_execute_still_requires_a_manifest() -> None:
    with pytest.raises(SystemExit) as excinfo:
        runner.main(["--execute", "--confirm"])
    assert "--manifest is required" in str(excinfo.value)


def test_the_site_env_docs_tell_operators_about_the_check() -> None:
    """A free pre-flight nobody knows about is not a pre-flight.

    The README is the only place an operator setting up a new site is
    told what to run first.
    """
    readme = (REPO / "scripts" / "cluster" / "README.md").read_text(encoding="utf-8")
    assert "--check-imports" in readme


# ---------------------------------------------------------------------------
# The chat-template ladder
# ---------------------------------------------------------------------------


class _Tokenizer:
    """A tokenizer that accepts, refuses, or refuses everything.

    `refuse_system=True` reproduces the several widely-used open-weight
    instruction models whose templates raise on a system role outright.
    """

    def __init__(self, refuse_system: bool = False, refuse_all: bool = False) -> None:
        self.refuse_system = refuse_system
        self.refuse_all = refuse_all

    def apply_chat_template(self, messages, tokenize=False, add_generation_prompt=False):
        assert tokenize is False
        assert add_generation_prompt is True
        if self.refuse_all:
            raise ValueError("this model has no chat template")
        if self.refuse_system and any(m["role"] == "system" for m in messages):
            raise TemplateError("System role not supported")
        return "<bos>" + "".join(m["content"] for m in messages) + "<turn>"

    def encode(self, text):
        return [0] * (len(text) // 4)


class TemplateError(Exception):
    pass


ROWS = [{"system": "SYS", "user": "USER"}]


def test_a_model_that_takes_a_system_role_gets_two_turns() -> None:
    texts, fmt = render(_Tokenizer())
    assert fmt == "chat_template"
    assert texts == ["<bos>SYSUSER<turn>"]


def test_a_model_that_refuses_a_system_role_gets_a_merged_prompt() -> None:
    """Merging is the documented way to prompt those models, not a downgrade.

    What matters is that the model still receives a *framed* turn: the
    fallback below it produces one empty token per request while every
    row reads `call_status=ok`.
    """
    texts, fmt = render(_Tokenizer(refuse_system=True))
    assert fmt == "chat_template_merged_system"
    assert texts == ["<bos>SYS\n\nUSER<turn>"]


def test_no_usable_template_falls_back_raw_and_says_so_loudly() -> None:
    logged: list[str] = []
    texts, fmt = runner.render_prompts(_Tokenizer(refuse_all=True), ROWS, log=logged.append)
    assert fmt == "raw_completion"
    assert texts == ["SYS\n\nUSER"]
    warning = "\n".join(logged)
    assert "RAW COMPLETIONS" in warning
    assert "call_status=ok" in warning  # names the failure it looks like


def test_the_merged_fallback_is_announced_too() -> None:
    logged: list[str] = []
    runner.render_prompts(_Tokenizer(refuse_system=True), ROWS, log=logged.append)
    assert any("system role" in line for line in logged)


def test_a_row_without_a_system_prompt_is_a_single_user_turn() -> None:
    texts, fmt = render(_Tokenizer(), [{"user": "USER"}])
    assert fmt == "chat_template"
    assert texts == ["<bos>USER<turn>"]


def render(tokenizer, rows=None):
    return runner.render_prompts(tokenizer, rows or ROWS, log=lambda _msg: None)


# ---------------------------------------------------------------------------
# Manifest reading
# ---------------------------------------------------------------------------


def _manifest(tmp_path, rows, name="requests.jsonl"):
    path = tmp_path / name
    path.write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8",
    )
    return path


def _row(**over):
    row = {
        "schema_version": bm.SCHEMA_VERSION,
        "run_id": "abstract_screening-20260816T000000Z",
        "request_id": "abstract_screening-20260816T000000Z:AAAA1111:0",
        "item_key": "AAAA1111",
        "stage": "abstract_screening",
        "system": "SYS",
        "user": "USER",
        "max_output_tokens": 200,
        "input_chars": 4,
    }
    row.update(over)
    return row


def test_reading_a_manifest_derives_its_header(tmp_path) -> None:
    header, rows = runner.read_manifest(str(_manifest(tmp_path, [_row()])))
    assert header["run_id"] == "abstract_screening-20260816T000000Z"
    assert header["stage"] == "abstract_screening"
    assert header["n_requests"] == 1
    assert len(header["sha256"]) == 64
    assert len(rows) == 1


def test_an_empty_manifest_is_refused_with_the_reason(tmp_path) -> None:
    """An empty manifest is not an empty result.

    It is the shape of "everything was skipped and nobody looked at the
    sidecar", and running it would produce a run record showing a clean
    pass over nothing.
    """
    path = tmp_path / "requests.jsonl"
    path.write_text("", encoding="utf-8")
    with pytest.raises(runner.ManifestError, match="not an empty result"):
        runner.read_manifest(str(path))


def test_an_unknown_schema_version_is_refused(tmp_path) -> None:
    path = _manifest(tmp_path, [_row(schema_version=99)])
    with pytest.raises(runner.ManifestError, match="schema"):
        runner.read_manifest(str(path))


def test_two_runs_in_one_file_are_refused(tmp_path) -> None:
    rows = [_row(), _row(run_id="other-run", request_id="other-run:BBBB2222:0")]
    with pytest.raises(runner.ManifestError, match="disagree on run_id"):
        runner.read_manifest(str(_manifest(tmp_path, rows)))


def test_duplicate_request_ids_are_refused(tmp_path) -> None:
    with pytest.raises(runner.ManifestError, match="duplicate request_id"):
        runner.read_manifest(str(_manifest(tmp_path, [_row(), _row()])))


def test_a_gzipped_manifest_reads_the_same(tmp_path) -> None:
    """A full-text manifest is ~720 kB of prompt per paper.

    A few hundred papers is a file worth compressing before moving it to
    a cluster, and the runner has to detect that by suffix — the sbatch
    wrapper passes whatever path it was given.
    """
    import gzip

    path = tmp_path / "requests.jsonl.gz"
    with gzip.open(path, "wt", encoding="utf-8") as fh:
        fh.write(json.dumps(_row()) + "\n")
    header, rows = runner.read_manifest(str(path))
    assert header["n_requests"] == 1
    assert rows[0]["item_key"] == "AAAA1111"


def test_a_manifest_written_by_the_plugin_reads_here(tmp_path) -> None:
    """The seam itself: what emit writes is what the runner reads.

    Both sides validate independently — the plugin cannot import the
    runner and the runner cannot import the plugin — so nothing but a
    test connects the two halves of the contract.
    """
    import abstract_screen

    rows, _ = abstract_screen.build_manifest_rows(
        [{"key": "AAAA1111", "data": {"title": "T", "abstractNote": "A"}}],
        run_id=bm.new_run_id(bm.STAGE_ABSTRACT),
        system_prompt="SYSTEM",
        model="org/model-1",
        prompt_version="v1",
        doi_to_query={},
        library={"kind": "group", "id": "1"},
        collection="COLL0001",
    )
    path = tmp_path / "requests.jsonl"
    bm.write_manifest(path, rows)

    header, read = runner.read_manifest(str(path))
    assert header["stage"] == bm.STAGE_ABSTRACT
    assert read[0]["system"] == "SYSTEM"
    assert runner.output_budget(read[0]) == 200


# ---------------------------------------------------------------------------
# Provenance and the CLI
# ---------------------------------------------------------------------------


def test_hardware_provenance_records_absence_rather_than_guessing(monkeypatch) -> None:
    """No GPU here, and "unknown" is the honest answer.

    "An A100" typed into a methods section is not provenance; a recorded
    `gpu_query_error` at least says the run cannot support the claim.
    """
    monkeypatch.setenv("SLURM_JOB_ID", "12345")
    monkeypatch.setenv("SLURM_ARRAY_TASK_ID", "2")
    record = runner.hardware_provenance()
    assert record["job_id"] == "12345"
    assert record["array_task_id"] == "2"
    assert record["node"]
    # Either nvidia-smi is absent (error recorded) or it answered.
    assert "gpu_query_error" in record or record["gpu_name"] is not None


def test_dry_run_prints_the_preflight_and_generates_nothing(tmp_path, capsys) -> None:
    path = _manifest(tmp_path, [_row()])
    assert runner.main([
        "--manifest", str(path), "--model", "org/model-1", "--dry-run",
    ]) == 0
    out = capsys.readouterr().out
    assert "PRE-FLIGHT" in out
    assert "org/model-1" in out
    assert "DRY RUN" in out
    assert not list(tmp_path.glob("*responses*"))


def test_the_preflight_labels_its_token_figure_an_estimate(tmp_path, capsys) -> None:
    """Counting exactly needs the tokenizer, which needs the GPU stack.

    That is the thing `--dry-run` exists to avoid needing, so the number
    it prints is an estimate and has to say so — an estimate reported in
    a column called `tokens` is how a context-window overflow becomes a
    surprise inside a job.
    """
    path = _manifest(tmp_path, [_row()])
    runner.main(["--manifest", str(path), "--model", "org/m", "--dry-run"])
    assert "estimate" in capsys.readouterr().out


def test_execute_without_confirm_refuses(tmp_path) -> None:
    """A GPU allocation is a shared facility's resource."""
    path = _manifest(tmp_path, [_row()])
    with pytest.raises(SystemExit, match="needs --confirm"):
        runner.main(["--manifest", str(path), "--model", "org/m", "--execute"])


def test_neither_mode_is_refused(tmp_path) -> None:
    path = _manifest(tmp_path, [_row()])
    with pytest.raises(SystemExit, match="--dry-run, --execute or --check-imports"):
        runner.main(["--manifest", str(path), "--model", "org/m"])


def test_the_model_defaults_to_the_manifests_hint(tmp_path, capsys) -> None:
    path = _manifest(tmp_path, [_row(model_hint="org/hinted")])
    runner.main(["--manifest", str(path), "--dry-run"])
    assert "org/hinted" in capsys.readouterr().out


def test_no_model_anywhere_is_an_error(tmp_path) -> None:
    path = _manifest(tmp_path, [_row()])
    with pytest.raises(SystemExit, match="no model"):
        runner.main(["--manifest", str(path), "--dry-run"])


def test_limit_runs_a_pilot(tmp_path, capsys) -> None:
    """Ten papers to measure JSON compliance, not 240 to discover it."""
    rows = [
        _row(request_id=f"r:{i}", item_key=f"KEY{i}") for i in range(5)
    ]
    path = _manifest(tmp_path, rows)
    runner.main([
        "--manifest", str(path), "--model", "org/m", "--limit", "2", "--dry-run",
    ])
    assert "requests             : 2" in capsys.readouterr().out


def test_the_run_record_path_sits_beside_the_responses() -> None:
    """`--apply-responses` finds the record by this rule and no other."""
    for name in ("out.jsonl", "out.jsonl.gz"):
        expected = str(bm.run_record_path(Path("/tmp") / name))
        assert runner.run_record_path("/tmp/" + name) == expected
