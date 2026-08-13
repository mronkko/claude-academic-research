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
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
CI = REPO / ".github" / "workflows" / "ci.yml"
PYPROJECT = REPO / "pyproject.toml"


def _ci_text() -> str:
    return CI.read_text(encoding="utf-8")


def _dev_group() -> list[str]:
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    return data["dependency-groups"]["dev"]


def test_ci_installs_the_dev_dependency_group() -> None:
    assert re.search(r"pip install\s+--group\s+dev", _ci_text()), (
        "ci.yml must install dev dependencies with `pip install --group dev` "
        "(PEP 735) so pyproject.toml stays the only place test dependencies "
        "are declared."
    )


# pip flags that consume the following token as their value, so that token
# is not a package name (`--group dev`, `-r requirements.txt`, ...).
_VALUE_FLAGS = {
    "--group", "-r", "--requirement", "-c", "--constraint",
    "-e", "--editable", "--index-url", "-i", "--extra-index-url",
    "--find-links", "-f", "--target", "-t", "--python", "--prefix",
}


def _named_packages(args: str) -> list[str]:
    """Package names in a `pip install` argument string, ignoring flags and
    the values they consume. `--upgrade pip` is treated as naming `pip`,
    which is fine — bootstrapping pip itself is not a test dependency, so
    callers exclude that line separately."""
    tokens = args.split()
    out: list[str] = []
    skip_next = False
    for tok in tokens:
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


def test_ci_does_not_hand_list_test_packages() -> None:
    """A second `pip install <names...>` line is exactly the drift this
    guard exists to prevent. `--group`/`-r`/`-e`/`.` forms are fine, as is
    bootstrapping pip itself."""
    offenders = []
    for lineno, line in enumerate(_ci_text().splitlines(), start=1):
        stripped = line.strip()
        match = re.match(r"^(?:python -m )?pip install\s+(.*)$", stripped)
        if not match:
            continue
        named = [p for p in _named_packages(match.group(1)) if p != "pip"]
        if named:
            offenders.append(f"{CI.name}:{lineno}: {stripped}  -> {named}")
    assert not offenders, (
        "ci.yml hand-lists packages instead of installing the dev dependency "
        "group; add them to [dependency-groups] dev in pyproject.toml "
        "instead:\n  " + "\n  ".join(offenders)
    )


def test_dev_group_declares_tenacity() -> None:
    """The specific omission that caused the drift. zotero_io.py and
    http_client.py import tenacity at module scope, so without it 14 test
    modules fail at collection time."""
    assert any(spec.startswith("tenacity") for spec in _dev_group()), (
        "[dependency-groups] dev must declare tenacity — zotero_io.py and "
        "http_client.py import it at module scope."
    )


def test_dev_group_does_not_rely_on_transitive_runtime_deps() -> None:
    """pyzotero / httpx / requests reach the test env only through
    zotero-mcp-server's dependency tree unless declared here. That works
    until upstream drops one of them."""
    declared = {re.split(r"[><=\[]", spec, maxsplit=1)[0] for spec in _dev_group()}
    for pkg in ("pyzotero", "httpx", "requests"):
        assert pkg in declared, (
            f"[dependency-groups] dev should declare {pkg} explicitly rather "
            f"than inheriting it from zotero-mcp-server."
        )
