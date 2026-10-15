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
follows the code instead of being a second list to keep in sync. It used
to cover only `zotero_io` (and, in test_searcher_backoff.py, `http_client`
for `search*.py` alone), and matched dependency names by substring — so
`"httpx2"` would have satisfied `httpx`. Now, for every script carrying a
PEP 723 block, it follows the script's local imports transitively and
requires each third-party module those local modules import at top level
to appear, by exact normalised distribution name, in the block's
`dependencies`. That is how `import_to_zotero.py` was found to reach
`http_client` (and so `urllib3`) without declaring it.
"""

from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS = REPO / "scripts"
PIPELINES = SCRIPTS / "pipelines"
TEMPLATES = REPO / "templates"
ZOTERO_IO = PIPELINES / "zotero_io.py"
HTTP_CLIENT = PIPELINES / "http_client.py"

#: Directories a script may import local modules from — the pipeline
#: scripts put `scripts/` and `scripts/pipelines/` on sys.path, and a
#: template imports its sibling `test_common.py` once copied into a project.
_IMPORT_ROOTS = (SCRIPTS, PIPELINES, TEMPLATES)

#: Every tree a `uv run`-able script can live in.
_SCRIPT_TREES = (SCRIPTS, TEMPLATES, REPO / "editorial-tools")

#: Import name -> PyPI distribution name, for the cases where they differ.
_DISTRIBUTION_NAME: dict[str, str] = {
    "zotero_mcp": "zotero-mcp-server",
    "google": "google-genai",
    "yaml": "pyyaml",
    "fitz": "pymupdf",
    "bs4": "beautifulsoup4",
    "dateutil": "python-dateutil",
}


def _normalise(dist: str) -> str:
    """PEP 503 normalisation, so `Wiley_TDM` and `wiley-tdm` compare equal."""
    return re.sub(r"[-_.]+", "-", dist).lower()


def _pep723_dependencies(text: str) -> set[str] | None:
    """Normalised distribution names in the script's PEP 723 block.

    None when the script has no block. Parsed as TOML, so a dependency is
    one entry of `dependencies` — not any substring of the block.
    """
    match = re.search(
        r"^# /// script\s*$(?P<body>.*?)^# ///\s*$", text, re.MULTILINE | re.DOTALL,
    )
    if match is None:
        return None
    body = "\n".join(
        line[2:] if line.startswith("# ") else line[1:]
        for line in match.group("body").splitlines()
    )
    names: set[str] = set()
    for req in tomllib.loads(body).get("dependencies", []):
        m = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", req)
        assert m, f"unparseable dependency {req!r}"
        names.add(_normalise(m.group(1)))
    return names


def _local_index() -> dict[str, Path]:
    """Dotted module name -> file, for everything importable from the repo."""
    index: dict[str, Path] = {}
    for root in _IMPORT_ROOTS:
        for path in root.rglob("*.py"):
            parts = list(path.relative_to(root).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts = parts[:-1]
            if parts:
                index.setdefault(".".join(parts), path)
    return index


_INDEX = _local_index()
_LOCAL_TOPS = {name.split(".")[0] for name in _INDEX}


def _module_name(path: Path) -> tuple[str, bool]:
    """(dotted name, is_package) for `path`, relative to the innermost root."""
    for root in sorted(_IMPORT_ROOTS, key=lambda r: len(r.parts), reverse=True):
        if path.is_relative_to(root):
            parts = list(path.relative_to(root).with_suffix("").parts)
            is_pkg = parts[-1] == "__init__"
            if is_pkg:
                parts = parts[:-1]
            return ".".join(parts), is_pkg
    return path.stem, False


def _optional_import_lines(tree: ast.AST) -> set[int]:
    """Line numbers of imports inside a `try` that catches ImportError.

    Such an import is optional by construction (`whenever` in zotero_io,
    `zotero_io` in the browser connector) and must not become a hard
    requirement on every caller.
    """
    guarded: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Try):
            continue
        caught = {
            ast.unparse(h.type) if h.type is not None else "BaseException"
            for h in node.handlers
        }
        if not any(name in c for c in caught for name in
                   ("ImportError", "ModuleNotFoundError", "Exception")):
            continue
        for stmt in node.body:
            for inner in ast.walk(stmt):
                if isinstance(inner, (ast.Import, ast.ImportFrom)):
                    guarded.add(inner.lineno)
    return guarded


def _imported_names(path: Path) -> set[str]:
    """Every module name `path` imports, anywhere — lazily inside a
    function too, since a lazy import still has to resolve when that code
    path runs — except optional (try/ImportError-guarded) ones."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    optional = _optional_import_lines(tree)
    me, is_pkg = _module_name(path)
    found: set[str] = set()
    for node in ast.walk(tree):
        if getattr(node, "lineno", None) in optional:
            continue
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = me.split(".")
                if not is_pkg:
                    base = base[:-1]
                base = base[: len(base) - (node.level - 1)]
                module = ".".join([*base, node.module] if node.module else base)
            else:
                module = node.module or ""
            found.add(module)
            # `from fetchers import browser` imports a submodule.
            found.update(f"{module}.{a.name}" for a in node.names)
    return found


def _local_closure(script: Path) -> set[Path]:
    """`script` plus every repo module it reaches through imports."""
    seen: set[Path] = set()
    stack = [script]
    while stack:
        path = stack.pop()
        if path in seen:
            continue
        seen.add(path)
        for name in _imported_names(path):
            parts = name.split(".")
            for i in range(1, len(parts) + 1):
                local = _INDEX.get(".".join(parts[:i]))
                if local is not None and local not in seen:
                    stack.append(local)
    return seen


def _toplevel_third_party(path: Path) -> set[str]:
    """Third-party import names at module scope, minus optional ones."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            found.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            found.add(node.module.split(".")[0])
    return {
        m for m in found
        if m not in sys.stdlib_module_names and m not in _LOCAL_TOPS and m != "__future__"
    }


def _uv_scripts() -> list[Path]:
    return sorted(
        path
        for tree in _SCRIPT_TREES
        for path in tree.rglob("*.py")
        if "# /// script" in path.read_text(encoding="utf-8")
    )


def _missing_for(script: Path) -> list[str]:
    declared = _pep723_dependencies(script.read_text(encoding="utf-8")) or set()
    missing: set[str] = set()
    for module_path in _local_closure(script):
        for module in _toplevel_third_party(module_path):
            dist = _normalise(_DISTRIBUTION_NAME.get(module, module))
            if dist not in declared:
                via = _module_name(module_path)[0]
                missing.add(f"{dist} (imported by {via})")
    return sorted(missing)


# ---------------------------------------------------------------------------
# The parser and the walk are themselves checked, so a regression in either
# cannot make the guard pass by seeing nothing.
# ---------------------------------------------------------------------------


def test_the_block_parser_matches_names_exactly() -> None:
    text = (
        '# /// script\n# requires-python = ">=3.11"\n# dependencies = [\n'
        '#     "httpx2>=0.1",\n#     "Wiley_TDM>=0.2",\n#     "requests[socks]>=2",\n'
        '# ]\n# ///\n'
    )
    deps = _pep723_dependencies(text)
    assert deps is not None
    assert deps =={"httpx2", "wiley-tdm", "requests"}
    assert "httpx" not in deps, "a substring match is what this replaced"
    assert _pep723_dependencies("print('no block')") is None


def test_the_walk_sees_known_dependencies() -> None:
    scripts = _uv_scripts()
    assert len(scripts) >= 20, f"expected the pipeline scripts; found {scripts}"
    assert _toplevel_third_party(ZOTERO_IO) >= {"pyzotero"}, (
        "expected zotero_io to import pyzotero at module scope; the parse "
        "has stopped seeing its imports"
    )
    assert _toplevel_third_party(HTTP_CLIENT) >= {"urllib3", "tenacity", "requests"}
    assert HTTP_CLIENT in _local_closure(PIPELINES / "search.py"), (
        "search.py reaches http_client through the searchers; the import "
        "walk has stopped following local imports"
    )


def test_every_uv_script_declares_what_its_imports_import() -> None:
    """Covers `zotero_io` consumers, `http_client` consumers (urllib3 and
    tenacity — the check that used to live in test_searcher_backoff.py for
    `search*.py` only), and anything else a script reaches."""
    missing = {
        str(script.relative_to(REPO)): gaps
        for script in _uv_scripts()
        if (gaps := _missing_for(script))
    }
    assert not missing, (
        "These scripts' PEP 723 blocks omit a dependency of code they "
        "import, so `uv run <script>` fails with ModuleNotFoundError on a "
        "fresh machine:\n  "
        + "\n  ".join(f"{s}: {', '.join(g)}" for s, g in sorted(missing.items()))
    )
