"""Guard: a `uv run`-able script must declare what the modules it imports import.

A script with a PEP 723 block gets an isolated environment built from that
block alone — the project `.venv` is not consulted, so anything the block
omits is simply absent at runtime. `zotero_io` imports `httpx` at module
scope, but `enrich_dois.py`, `enrich_pdfs.py` and `enrich_abstracts.py` did
not declare it, so `uv run enrich_dois.py` died on `import zotero_io` with
`ModuleNotFoundError: No module named 'httpx'`.

Two things hid this for months. The unit suite runs against the dev `.venv`,
where `httpx` arrives via `zotero-mcp-server`, so every test passed. And uv
caches one environment per script and does not prune extraneous packages
from it: the `enrich_pdfs` cache still held an `httpx` installed back when
pyzotero depended on it, so that script kept working *on machines that had
run it before* while failing for anyone starting fresh. pyzotero 1.15.1
depends on `httpx2`, which does not provide the `httpx` import name.

The requirement is derived from the source rather than hard-coded, so it
follows `zotero_io` if it ever moves to `httpx2` instead of being a second
list to keep in sync.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
PIPELINES = SCRIPTS / "pipelines"
ZOTERO_IO = PIPELINES / "zotero_io.py"

#: Import name -> PyPI distribution name, for the cases where they differ.
#: Empty today; every third-party module `zotero_io` imports is published
#: under its own import name.
_DISTRIBUTION_NAME: dict[str, str] = {}


def _local_module_names() -> set[str]:
    """Every module/package name importable from the repo's own tree."""
    names: set[str] = set()
    for path in SCRIPTS.rglob("*.py"):
        names.add(path.stem)
        if path.name == "__init__.py":
            names.add(path.parent.name)
    return names


def _is_third_party(module: str, local: set[str]) -> bool:
    return module not in sys.stdlib_module_names and module not in local


def _unguarded_toplevel_imports(path: Path) -> set[str]:
    """Top-level import names, skipping anything inside a try/except.

    Only `ast.Module.body` is walked: an import nested in a `try` (as
    `whenever` is, for a warning filter) is optional by construction and
    must not become a hard requirement on every caller.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return found


def _pep723_block(text: str) -> str | None:
    """The inline metadata block, or None when the script has no PEP 723."""
    if "# /// script" not in text:
        return None
    return text.split("# ///")[1]


def _imports_module(tree: ast.AST, name: str) -> bool:
    """True if `name` is imported anywhere — lazily inside a function too.

    A lazy import still has to resolve when that code path runs, so it
    carries the same dependency requirement as a module-level one.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(a.name.split(".")[0] == name for a in node.names):
                return True
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module and node.module.split(".")[0] == name:
                return True
    return False


def test_zotero_io_consumers_declare_its_third_party_imports() -> None:
    local = _local_module_names()
    required = sorted(
        _DISTRIBUTION_NAME.get(module, module)
        for module in _unguarded_toplevel_imports(ZOTERO_IO)
        if _is_third_party(module, local)
    )
    assert "httpx" in required, (
        "expected zotero_io to import httpx at module scope; if it moved to "
        f"httpx2 this guard follows it automatically. Found: {required}"
    )

    missing: list[str] = []
    for script in sorted(PIPELINES.glob("*.py")):
        text = script.read_text(encoding="utf-8")
        block = _pep723_block(text)
        if block is None:
            continue
        if not _imports_module(ast.parse(text), "zotero_io"):
            continue
        for dist in required:
            if dist not in block:
                missing.append(f"{script.name}: {dist}")

    assert not missing, (
        "These scripts import `zotero_io` but their PEP 723 block omits a "
        "dependency it imports, so `uv run <script>` fails at import time "
        "with ModuleNotFoundError:\n  " + "\n  ".join(missing)
    )
