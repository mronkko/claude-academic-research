"""Guard: CI must install the `dev` dependency group, not a hand-written list.

`.github/workflows/ci.yml` used to carry its own `pip install pytest
responses ruff tenacity pyzotero httpx anthropic google-genai ...` line
alongside `[dependency-groups] dev` in pyproject.toml. Two sources of truth,
and they drifted: the dev group never declared `tenacity`, so a fresh
`uv sync` produced an environment where 14 test modules failed to collect
while CI stayed green. (It also installed `anthropic` and `google-genai`,
which the suite does not need at all.)

The fix is structural — CI installs `--group dev` — so this module guards
the structure rather than trying to keep two lists equal.

The first version only recognised `pip install` at the start of a line, so
`run: pip install foo`, `pip3 install`, `python3 -m pip`, `uv pip install`
and a `\\`-continued argument list all slipped past it, and a comment
mentioning `pip install --group dev` satisfied the positive check. Commands
are now read with YAML comments stripped and continuations joined, and
matched wherever they occur on the line.
"""

from __future__ import annotations

import re
import shlex
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO / "pyproject.toml"

#: Any spelling of a pip install: `pip`, `pip3`, `pip3.12`, `python -m pip`,
#: `python3.12 -m pip`, `py -m pip`, `uv pip`. Not anchored to the start of
#: the line, so the single-line `run: pip install x` form is seen too.
_PIP_INSTALL = re.compile(
    r"(?:^|[\s;&|:(])"
    r"(?:(?:python[\d.]*|py)\s+-m\s+|uv\s+)?"
    r"pip[\d.]*\s+install\b(?P<args>[^;&|]*)"
)


def _ci_commands() -> list[tuple[int, str]]:
    """(first line number, logical line) of ci.yml, comments removed and
    shell `\\` continuations joined."""
    out: list[tuple[int, str]] = []
    pending = ""
    start = 0
    for lineno, raw in enumerate(CI.read_text(encoding="utf-8").splitlines(), start=1):
        # YAML comments: a `#` at line start or after whitespace. ci.yml
        # has no `#` inside strings, and test_comments_are_not_commands
        # pins that a commented-out install is ignored.
        line = re.sub(r"(^|\s)#.*$", "", raw).rstrip()
        if not pending:
            start = lineno
        if line.endswith("\\"):
            pending += line[:-1] + " "
            continue
        out.append((start, pending + line))
        pending = ""
    if pending:
        out.append((start, pending))
    return out


def _dev_group() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["dependency-groups"]["dev"]


def _dist_name(spec: str) -> str:
    m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", spec)
    assert m, f"unparseable requirement {spec!r}"
    return re.sub(r"[-_.]+", "-", m.group(1)).lower()


# pip flags that consume the following token as their value, so that token
# is not a package name (`--group dev`, `-r requirements.txt`, ...).
_VALUE_FLAGS = {
    "--group", "-r", "--requirement", "-c", "--constraint",
    "-e", "--editable", "--index-url", "-i", "--extra-index-url",
    "--find-links", "-f", "--target", "-t", "--python", "--prefix",
}


def _named_packages(args: str) -> list[str]:
    """Package names in a `pip install` argument string, ignoring flags and
    the values they consume (`--group dev` and `--group=dev` alike).
    `--upgrade pip` is treated as naming `pip`, which callers exclude:
    bootstrapping pip itself is not a test dependency."""
    out: list[str] = []
    skip_next = False
    for tok in shlex.split(args):
        if skip_next:
            skip_next = False
            continue
        if tok.startswith("-"):
            if tok in _VALUE_FLAGS:
                skip_next = True
            continue
        if tok in {".", "..."}:
            continue
        out.append(tok)
    return out


def _installs(commands: list[tuple[int, str]]) -> list[tuple[int, str, list[str]]]:
    return [
        (lineno, m.group("args").strip(), _named_packages(m.group("args")))
        for lineno, line in commands
        for m in _PIP_INSTALL.finditer(line)
    ]


def test_ci_installs_the_dev_dependency_group() -> None:
    groups = [
        args for _, args, _ in _installs(_ci_commands())
        if re.search(r"--group(?:\s+|=)dev\b", args)
    ]
    assert groups, (
        "ci.yml must install dev dependencies with `pip install --group dev` "
        "(PEP 735) so pyproject.toml stays the only place test dependencies "
        "are declared."
    )


def test_ci_does_not_hand_list_test_packages() -> None:
    """A second `pip install <names...>` is exactly the drift this guard
    exists to prevent. `--group`/`-r`/`-e`/`.` forms are fine, as is
    bootstrapping pip itself."""
    offenders = [
        f"{CI.name}:{lineno}: pip install {args}  -> {packages}"
        for lineno, args, named in _installs(_ci_commands())
        if (packages := [p for p in named if p != "pip"])
    ]
    assert not offenders, (
        "ci.yml hand-lists packages instead of installing the dev dependency "
        "group; add them to [dependency-groups] dev in pyproject.toml "
        "instead:\n  " + "\n  ".join(offenders)
    )


def test_the_install_matcher_sees_every_spelling() -> None:
    """The bypasses the first version had, each pinned."""
    spellings = [
        "pip install foo",
        "run: pip install foo",
        "pip3 install foo",
        "pip3.12 install foo",
        "python -m pip install foo",
        "python3 -m pip install -U foo",
        "py -m pip install foo",
        "uv pip install foo",
        "pip install --group dev && pip install foo",
    ]
    for line in spellings:
        named = [p for _, _, n in _installs([(1, line)]) for p in n]
        assert "foo" in named, f"missed: {line!r}"


def test_continuations_and_comments(tmp_path, monkeypatch) -> None:
    """A `\\`-continued package list is seen; a commented-out install, or a
    comment that merely mentions `--group dev`, is not."""
    fake = tmp_path / "ci.yml"
    fake.write_text(
        "      # pip install --group dev\n"
        "      - run: |\n"
        "          pip install \\\n"
        "            foo bar\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(f"{__name__}.CI", fake)
    installs = _installs(_ci_commands())
    assert [n for _, _, n in installs] == [["foo", "bar"]]


def test_ci_lints_every_tracked_python_tree() -> None:
    """templates/ is copied into user projects and editorial-tools/ is a
    second plugin; both were outside the lint step while clean, which is
    when an exemption is cheapest to remove and easiest to forget."""
    lint = [line for _, line in _ci_commands() if re.search(r"\bruff\s+check\b", line)]
    assert lint, "ci.yml has no `ruff check` step"
    targets = set(lint[0].split("ruff check", 1)[1].split())
    # Top-level directories holding Python. Hidden ones (.venv, .git,
    # .claude worktrees) and the gitignored run-output dirs are not source.
    trees = {
        d.name for d in REPO.iterdir()
        if d.is_dir() and not d.name.startswith(".")
        and d.name not in {"logs", "output"}
        and next(d.rglob("*.py"), None) is not None
    }
    missing = sorted(trees - targets)
    assert not missing, f"ruff check in ci.yml does not cover: {missing}"


def test_dev_group_declares_tenacity() -> None:
    """The specific omission that caused the drift. zotero_io.py and
    http_client.py import tenacity at module scope, so without it 14 test
    modules fail at collection time."""
    assert "tenacity" in {_dist_name(s) for s in _dev_group()}, (
        "[dependency-groups] dev must declare tenacity — zotero_io.py and "
        "http_client.py import it at module scope."
    )


def test_dev_group_does_not_rely_on_transitive_runtime_deps() -> None:
    """pyzotero / httpx / requests reach the test env only through
    zotero-mcp-server's dependency tree unless declared here. That works
    until upstream drops one of them."""
    declared = {_dist_name(spec) for spec in _dev_group()}
    for pkg in ("pyzotero", "httpx", "requests"):
        assert pkg in declared, (
            f"[dependency-groups] dev should declare {pkg} explicitly rather "
            f"than inheriting it from zotero-mcp-server."
        )
