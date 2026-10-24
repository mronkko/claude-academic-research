"""Guard test: every publisher / KeySpec / source has a matching live test.

Zotero Desktop's local API, whose write key is not a KeySpec, is pinned
by `ZOTERO_LOCAL_LIVE_TESTS`.

Runs on every `pytest` invocation (no marker). If a new browser
handler is added to `fetchers.browser.all_handlers()`, a new `KeySpec`
added to `scripts/setup/wizard.py:KEYS`, a new `AbstractFetcher` /
`PdfFetcher` subclass added under `scripts/pipelines/fetchers/`, or a new
search database added to `searchers.ALL_SOURCE_CLASSES` without a
matching live test being added at the same time, this test fails with an
actionable message.

The policy is documented in the project memory at
`feedback_every_source_has_a_test.md`:

> Every publisher / source / API key has a live test — adding a new
> registry entry, KeySpec, or source module must ship with a matching
> live test; a default-run guard test enforces the invariant at PR
> time.

Matching is by exact name. A test function must exist under the expected
name (found by parsing the file, so a docstring or comment that merely
mentions the name does not count — the old substring check accepted
`test_x` wherever `test_x_old` appeared), a KNOWN_DOIS key must be a
literal key of that dict, and an env var must appear as a whole word
inside a `test_auth_*` function. Each check also asserts the registry it
walks is non-empty, so a broken import cannot make the guard pass
vacuously.
"""

from __future__ import annotations

import ast
import importlib.util
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO / "scripts"
LIVE = REPO / "tests" / "live"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))


def _leaf_sources() -> list[type]:
    """Every concrete fetcher class registered via `fetchers/__init__.py`.

    Walks `Source.__subclasses__()` recursively (importing `fetchers`
    registers every source module); a class counts as a leaf when it
    declares `name` in its own `__dict__`. Interactive sources
    (BrowserSource) are covered by the browser-publisher guard instead.

    Deliberately NOT based on `fetchers.pdf_sources()` — that function
    default-excludes `wiley` and `browser`, and a coverage guard must
    see every source, selected or not.
    """
    import fetchers  # noqa: F401 — importing registers all source modules
    from fetchers.base import Source

    seen: set[type] = set()
    stack = list(Source.__subclasses__())
    while stack:
        cls = stack.pop()
        if cls in seen:
            continue  # dual-ABC classes are reached via both parents
        seen.add(cls)
        stack.extend(cls.__subclasses__())
    return [
        cls for cls in seen
        if cls.__dict__.get("name") and not cls.interactive
    ]


def _load_wizard():
    spec = importlib.util.spec_from_file_location(
        "wizard", SCRIPTS_ROOT / "setup" / "wizard.py",
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _test_functions(path: Path) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    """Every `test_*` function defined in `path`, by exact name."""
    tree = ast.parse(_read(path))
    return {
        node.name: node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test_")
    }


def _known_doi_keys() -> set[str]:
    """The literal keys of `KNOWN_DOIS` in tests/live/conftest.py.

    Parsed rather than imported (the module is not importable outside a
    pytest session) and rather than grepped: a regex over the file also
    matched keys of `APA_REGRESSION_DOIS` and anything in a comment.
    """
    tree = ast.parse(_read(LIVE / "conftest.py"))
    for node in tree.body:
        if isinstance(node, ast.AnnAssign):
            target = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        else:
            continue
        if isinstance(target, ast.Name) and target.id == "KNOWN_DOIS":
            assert isinstance(node.value, ast.Dict), "KNOWN_DOIS is no longer a dict literal"
            return {
                k.value for k in node.value.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, str)
            }
    raise AssertionError("KNOWN_DOIS not found in tests/live/conftest.py")


def _missing_tests(mapping: dict[str, list[str]], names: list[str],
                   live_file: Path, label: str) -> list[str]:
    """Names without a mapping entry, or whose mapped test does not exist
    in `live_file` under exactly that name."""
    defined = _test_functions(live_file)
    missing: list[str] = []
    for name in names:
        expected = mapping.get(name)
        if expected is None:
            missing.append(f"{name} (no entry in {label} — add one plus a live test)")
            continue
        missing.extend(f"{name} → expected {t}" for t in expected if t not in defined)
    return missing


# ---------------------------------------------------------------------------
# Publishers ↔ test_browser_publishers.py
# ---------------------------------------------------------------------------


def test_every_publisher_in_registry_has_a_known_doi() -> None:
    """test_browser_publishers.py is parametrized from the handler
    registry; the corresponding DOI must exist in KNOWN_DOIS."""
    from fetchers.browser import all_handlers

    handlers = all_handlers()
    assert handlers, "fetchers.browser.all_handlers() returned nothing"
    known_keys = _known_doi_keys()

    missing = [h.name for h in handlers if h.name not in known_keys]
    assert not missing, (
        f"Browser handlers without a KNOWN_DOIS entry: {missing}. "
        f"Add DOIs to tests/live/conftest.py so test_browser_publishers.py "
        f"can exercise them."
    )


def test_the_browser_live_test_is_parametrized_from_the_registry() -> None:
    """A KNOWN_DOIS entry is only coverage if a live test iterates the
    registry. Pin that the per-publisher test exists and takes its
    parameters from `all_handlers()`, so deleting or hard-coding it
    cannot leave the DOI check above passing on its own."""
    path = LIVE / "test_browser_publishers.py"
    test = _test_functions(path).get("test_browser_publisher_downloads_pdf")
    assert test is not None, f"{path.name}: test_browser_publisher_downloads_pdf is gone"
    parametrize_args = [
        ast.unparse(arg)
        for dec in test.decorator_list
        if isinstance(dec, ast.Call) and ast.unparse(dec.func).endswith("parametrize")
        for arg in dec.args
    ]
    assert "_handlers()" in parametrize_args, (
        f"{path.name}: the publisher test is no longer parametrized by "
        f"_handlers(); found {parametrize_args}"
    )
    helper = next(
        (n for n in ast.parse(_read(path)).body
         if isinstance(n, ast.FunctionDef) and n.name == "_handlers"),
        None,
    )
    assert helper is not None and "all_handlers()" in ast.unparse(helper), (
        f"{path.name}: _handlers() no longer returns fetchers.browser.all_handlers()"
    )


# ---------------------------------------------------------------------------
# KeySpecs ↔ test_auth_workflows.py
# ---------------------------------------------------------------------------


def test_every_keyspec_has_an_auth_test() -> None:
    """Every KeySpec in wizard.py:KEYS has a test in test_auth_workflows.py."""
    wizard = _load_wizard()
    assert wizard.KEYS, "wizard.KEYS is empty"
    path = LIVE / "test_auth_workflows.py"
    source = _read(path)
    # Only text inside a `test_auth_*` function (code or docstring)
    # counts — a mention in a module comment or a helper is not a test.
    auth_bodies = [
        ast.get_source_segment(source, fn) or ""
        for name, fn in _test_functions(path).items()
        if name.startswith("test_auth_")
    ]
    assert auth_bodies, f"no test_auth_* functions found in {path.name}"

    def covered(needle: str) -> bool:
        # Whole-word: `WOS_API_KEY` must not be satisfied by
        # `WOS_API_KEY_EXTENDED`.
        pattern = re.compile(rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])")
        return any(pattern.search(body) for body in auth_bodies)

    missing = []
    for spec in wizard.KEYS:
        if spec.env_var:
            needle = spec.env_var
        else:
            # A config-only spec has no variable to search for, and
            # `"" in text` is vacuously true — which would silently
            # exempt the specs least likely to be covered.
            #
            # Match a declared marker rather than a `require_config(...)`
            # call: how a test fetches the value is an implementation
            # detail that legitimately changes (the gateway key moved to
            # `llm_provider._api_key_for` so it would follow the
            # `api_key_env` indirection), and a guard that breaks on a
            # refactor of the thing it guards teaches people to edit the
            # guard. The marker states the intent instead.
            needle = f"covers: [{spec.toml_section}].{spec.toml_key}"
        if not covered(needle):
            missing.append(spec.env_var or f"[{spec.toml_section}].{spec.toml_key}")
    assert not missing, (
        f"KeySpecs without a matching test in test_auth_workflows.py: "
        f"{missing}. Add a test_auth_{{name}} function that calls the "
        f"matching _verify_* helper. A config-only spec (no env var) is "
        f"matched by a `covers: [section].key` marker in that test's "
        f"docstring, since it has no variable name to grep for."
    )


# ---------------------------------------------------------------------------
# Zotero Desktop's local API ↔ tests/live
# ---------------------------------------------------------------------------


# The plugin's only Zotero surface is Desktop's local API, and its one
# credential is the local write key `/setup` obtains through Zotero's
# consent dialog. That key is not a `KeySpec` (no one pastes it in), so
# the KeySpec guard above cannot see it; it is pinned here by name.
# `ZOTERO_LOCAL_API_KEY` must appear in the auth test, and the write,
# upload and key re-grant paths each keep a live test.
ZOTERO_LOCAL_LIVE_TESTS: dict[str, list[str]] = {
    "test_auth_workflows.py": [
        "test_auth_zotero_local_read", "test_auth_zotero_local_api_key",
    ],
    "test_zotero_local_writes.py": [
        "test_a_local_write_is_visible_to_a_local_read_immediately",
        "test_a_rejected_key_is_regranted_and_the_write_retried",
    ],
    "test_pdf_attach_roundtrip.py": [
        "test_attached_pdf_actually_lands_with_bytes",
        "test_pdf_map_agrees_the_item_has_a_real_pdf",
    ],
}


def test_the_zotero_local_api_has_live_tests() -> None:
    missing = [
        f"{filename}::{name}"
        for filename, names in ZOTERO_LOCAL_LIVE_TESTS.items()
        for name in names
        if name not in _test_functions(LIVE / filename)
    ]
    assert not missing, f"Zotero local-API live tests missing: {missing}"

    path = LIVE / "test_auth_workflows.py"
    source = _read(path)
    fn = _test_functions(path)["test_auth_zotero_local_api_key"]
    assert re.search(
        r"(?<![A-Za-z0-9_])ZOTERO_LOCAL_API_KEY(?![A-Za-z0-9_])",
        ast.get_source_segment(source, fn) or "",
    ), "test_auth_zotero_local_api_key no longer names ZOTERO_LOCAL_API_KEY"


def test_no_live_test_uses_the_zotero_web_api() -> None:
    """The Web API key and `--remote` are gone; a live test still reading
    them would skip forever and pass silently."""
    banned = re.compile(
        r"ZOTERO_API_KEY(?![A-Za-z0-9_])|ZOTERO_USER_ID|\"api_key\", env=\"ZOTERO"
        r"|prefer_local|--remote|\.cloud\b"
    )
    offenders = [
        f"{path.relative_to(REPO)}:{lineno}"
        for path in sorted(LIVE.rglob("*.py"))
        for lineno, line in enumerate(_read(path).splitlines(), 1)
        if banned.search(line)
    ]
    assert not offenders, f"live tests still use the Zotero Web API: {offenders}"


# ---------------------------------------------------------------------------
# Abstract sources ↔ test_abstract_endpoints.py
# ---------------------------------------------------------------------------


# Source `name` → live-test function names in test_abstract_endpoints.py.
# Update this mapping when you add or rename a source or its test.
ABSTRACT_LIVE_TESTS: dict[str, list[str]] = {
    "crossref": ["test_crossref_abstract"],
    "semantic_scholar": ["test_semantic_scholar_abstract"],
    "scopus": ["test_scopus_abstract"],
    "wos": ["test_wos_abstract_direct_doi", "test_wos_title_fallback_on_doi_alias"],
    "sciencedirect": ["test_sciencedirect_abstract"],
    "openalex": ["test_openalex_grobid_abstract"],
}


def test_every_abstract_source_has_a_live_test() -> None:
    """Each `AbstractFetcher` subclass has a matching live test."""
    from fetchers.base import AbstractFetcher

    sources = sorted(
        cls.name for cls in _leaf_sources() if issubclass(cls, AbstractFetcher)
    )
    assert sources, "no AbstractFetcher sources found — the registry walk is broken"
    missing = _missing_tests(
        ABSTRACT_LIVE_TESTS, sources, LIVE / "test_abstract_endpoints.py",
        "ABSTRACT_LIVE_TESTS",
    )
    assert not missing, (
        f"Abstract sources without a matching live test: {missing}. "
        f"Add a corresponding test to tests/live/test_abstract_endpoints.py."
    )


# ---------------------------------------------------------------------------
# PDF sources ↔ test_pdf_endpoints.py
# ---------------------------------------------------------------------------


# Source `name` → live-test function names in test_pdf_endpoints.py.
# The two OpenAlex endpoints are two separate sources: `openalex_content`
# is the paid Content API (version of record, stage 2 of the cascade) and
# `openalex` is the free OA-metadata tier (stage 3). They were a single
# class with one pair of live tests until the tiers were split apart, so
# the two existing tests carry over one apiece.
PDF_LIVE_TESTS: dict[str, list[str]] = {
    "sciencedirect": ["test_elsevier_sciencedirect_reachable"],
    "springer": ["test_springer_reachable"],
    "crossref": ["test_crossref_tdm_link_present"],
    "pubmed_central": ["test_pmc_doi_to_pmcid_resolves"],
    "openalex": ["test_openalex_oa_url_present"],
    "openalex_content": ["test_openalex_content_api_returns_pdf_bytes"],
    "unpaywall": ["test_unpaywall_returns_pdf_url"],
    "arxiv": ["test_arxiv_serves_the_pdf_named_by_the_doi"],
    "semantic_scholar": ["test_semantic_scholar_open_access_pdf_url"],
    "core": ["test_core_search_returns_a_download_url"],
    "openaire": ["test_openaire_search_returns_a_record"],
    "base": ["test_base_search_is_reachable_or_ip_gated"],
    "preprint": ["test_preprint_discovery_finds_a_preprint_hosted_pdf"],
    "wiley": ["test_wiley_tdm_downloads_pdf"],
}


def test_every_pdf_source_has_a_live_test() -> None:
    """Each non-interactive `PdfFetcher` subclass has a matching live test.

    `BrowserSource` (interactive) is excluded here; its per-publisher
    handlers are covered by `test_every_publisher_in_registry_has_a_known_doi`.
    """
    from fetchers.base import PdfFetcher

    sources = sorted(
        cls.name for cls in _leaf_sources() if issubclass(cls, PdfFetcher)
    )
    assert sources, "no PdfFetcher sources found — the registry walk is broken"
    missing = _missing_tests(
        PDF_LIVE_TESTS, sources, LIVE / "test_pdf_endpoints.py", "PDF_LIVE_TESTS",
    )
    assert not missing, (
        f"PDF sources without a matching live test: {missing}. "
        f"Add a corresponding test to tests/live/test_pdf_endpoints.py."
    )


# ---------------------------------------------------------------------------
# Search databases ↔ tests/live
# ---------------------------------------------------------------------------


# Search source `name` → (live test file, test function names).
# Scopus and WoS have no dedicated live search test today: their only live
# coverage is the end-to-end mini SLR, whose fixed search_config runs all
# four databases (tests/live/e2e/search_config.py says so). Pinned here so
# that coverage cannot vanish silently and a fifth database cannot be
# added without a line in this table.
SEARCH_LIVE_TESTS: dict[str, tuple[str, list[str]]] = {
    "openalex": ("test_citation_search.py", ["test_openalex_lists_works_citing_the_seed"]),
    "semantic_scholar": (
        "test_s2_journal_scope.py", ["test_semantic_scholar_returns_rows_under_journal_scope"],
    ),
    "scopus": ("test_search_scopus_wos.py", ["test_scopus_search_returns_rows_in_scope"]),
    "wos": ("test_search_scopus_wos.py", ["test_wos_search_returns_rows_in_scope"]),
}


def test_every_search_source_has_a_live_test() -> None:
    """Each class in `searchers.ALL_SOURCE_CLASSES` has a live test."""
    from searchers import ALL_SOURCE_CLASSES

    names = sorted(cls.name for cls in ALL_SOURCE_CLASSES)
    assert names, "searchers.ALL_SOURCE_CLASSES is empty"
    missing: list[str] = []
    for name in names:
        entry = SEARCH_LIVE_TESTS.get(name)
        if entry is None:
            missing.append(f"{name} (no entry in SEARCH_LIVE_TESTS — add one plus a live test)")
            continue
        filename, expected = entry
        defined = _test_functions(LIVE / filename)
        missing.extend(f"{name} → expected {filename}::{t}" for t in expected if t not in defined)
    assert not missing, f"Search sources without a matching live test: {missing}."


# ---------------------------------------------------------------------------
# tests/live never runs by default
# ---------------------------------------------------------------------------


class _FakeItem:
    """Just enough of `pytest.Item` for the collection hook."""

    def __init__(self, path: Path, markers: list[str]) -> None:
        self.fspath = path
        self.markers = list(markers)

    def get_closest_marker(self, name: str):
        return name if name in self.markers else None

    def add_marker(self, mark) -> None:
        self.markers.append(mark.name)


def test_a_live_test_without_a_marker_is_marked_live() -> None:
    """tests/conftest.py marks every unmarked test under tests/live/ as
    `live`, so a forgotten marker cannot put a real-network test into the
    default run. Exercised on stand-in items so the check does not depend
    on some real live test happening to lack a marker."""
    from tests.conftest import LIVE_ROOT, pytest_collection_modifyitems

    unmarked = _FakeItem(LIVE_ROOT / "test_new_publisher.py", [])
    browser = _FakeItem(LIVE_ROOT / "test_browser_publishers.py", ["live_browser"])
    unit = _FakeItem(REPO / "tests" / "unit" / "test_x.py", [])
    pytest_collection_modifyitems([unmarked, browser, unit])  # type: ignore[list-item]

    assert unmarked.markers == ["live"]
    assert browser.markers == ["live_browser"], "an explicit live marker is kept as is"
    assert unit.markers == [], "unit tests are not touched"


def test_the_default_run_selects_nothing_under_tests_live() -> None:
    """End to end: collect tests/live with the default addopts and confirm
    no item survives the `-m` filter. `--collect-only -q` prints one
    `path::name` line per selected item."""
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--collect-only", "-q",
         "-p", "no:cacheprovider", str(LIVE)],
        capture_output=True, text=True, cwd=REPO, timeout=300,
    )
    selected = [line for line in result.stdout.splitlines() if "::" in line]
    assert "deselected" in result.stdout, result.stdout[-2000:] + result.stderr[-2000:]
    assert not selected, (
        "these tests under tests/live/ would run in the default suite:\n  "
        + "\n  ".join(selected)
    )
