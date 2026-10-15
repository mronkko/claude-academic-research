"""Pytest fixtures and path setup for the claude-academic-research test suite.

Besides the `sys.path` setup, this file makes every test hermetic with
respect to the developer's machine (`_isolate_from_user_environment`) and
keeps a forgotten marker from turning a live test into a default-run one
(`pytest_collection_modifyitems`).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS_ROOT = REPO_ROOT / "scripts"
PIPELINES_ROOT = SCRIPTS_ROOT / "pipelines"
LIVE_ROOT = REPO_ROOT / "tests" / "live"

# Make `sources` and `core` importable without the sys.path
# tricks the pipeline scripts do at runtime. Also add `scripts/pipelines`
# so test modules can `import zotero_io` / `import http_client` directly,
# mirroring the runtime path layout.
for _p in (SCRIPTS_ROOT, PIPELINES_ROOT):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core import config_loader, config_writer  # noqa: E402 — needs the sys.path setup above

# Held directly: tests routinely monkeypatch `config_loader.load_config` with
# a plain lambda, which has no `cache_clear`, and this fixture's teardown
# runs before that patch is undone.
_REAL_LOAD_CONFIG = config_loader.load_config


# ---------------------------------------------------------------------------
# Environment isolation
# ---------------------------------------------------------------------------

#: The opt-in markers that keep a test out of the default run. `addopts` in
#: pyproject.toml deselects exactly these.
LIVE_MARKERS = ("live", "live_browser", "live_slr", "live_cluster")

#: Environment variables the plugin reads, matched by shape rather than
#: listed one by one so a new key is covered the day it is added.
#: `tests/unit/test_env_isolation.py` greps `scripts/` for every variable
#: name the code reads and fails if one escapes these rules.
ISOLATED_ENV_PREFIXES = ("ZOTERO_", "ACADEMIC_RESEARCH_", "LIBRARY_", "SLURM", "PDF_")
ISOLATED_ENV_SUFFIXES = ("_API_KEY", "_API_KEY_EXTENDED", "_TOKEN", "_BASE_URL", "_MAILTO")
ISOLATED_ENV_NAMES = frozenset({
    "ABSTRACT_EXCLUDE_SOURCES",
    "ELSEVIER_RENDER_XML_TO_PDF",
    "OPENALEX_USE_PAID_CONTENT_API",
    "OPENURL_CONTEXT_PARAMS",
    "DISCOVERY_KEYWORD",
})

#: Variables pointed at the per-test temporary home rather than removed.
#: `Path.home()` reads HOME on POSIX and USERPROFILE on Windows; the
#: AppData pair is where the wizard and the Zotero-connector lookup search
#: on Windows.
REDIRECTED_HOME_VARS = ("HOME", "USERPROFILE")
REDIRECTED_APPDATA_VARS = ("APPDATA", "LOCALAPPDATA")


def is_isolated_env_name(name: str) -> bool:
    """True if the suite-wide fixture clears `name` for every test."""
    return (
        name in ISOLATED_ENV_NAMES
        or name.startswith(ISOLATED_ENV_PREFIXES)
        or name.endswith(ISOLATED_ENV_SUFFIXES)
    )


@pytest.fixture(autouse=True)
def _isolate_from_user_environment(request: pytest.FixtureRequest,
                                   tmp_path_factory: pytest.TempPathFactory,
                                   monkeypatch: pytest.MonkeyPatch):
    """Run every test as if on a fresh machine with nothing configured.

    Without this, a test's outcome depended on the machine running it:
    `config_loader` read the developer's real `config.toml` and env vars,
    so `test_get_provider_routing` failed with `ACADEMIC_RESEARCH_PROVIDER`
    set, and a test that forgot to stub `ZOTERO_USER_ID` made a real
    group-listing request on one machine and not on another. Its
    `lru_cache` also carried whatever the first test loaded into every
    later one, which made config hermeticity order-dependent.

    So, for every test:
    - HOME / USERPROFILE (and the Windows AppData pair) point at an empty
      temporary directory, and `config_loader.CONFIG_PATH` — computed at
      import time from the *real* home — is re-pointed into it;
    - every plugin-relevant env var is removed (see `is_isolated_env_name`);
    - the `load_config` cache is cleared before and after.

    A test that wants configuration sets it explicitly, with
    `monkeypatch.setenv` or by writing the temporary `config.toml`.

    Tests under `tests/live/` are exempt: reading the real keys is what
    they are for.
    """
    if is_under_live_root(Path(str(request.node.fspath))):
        yield None
        return
    home = tmp_path_factory.mktemp("home")
    for var in REDIRECTED_HOME_VARS:
        monkeypatch.setenv(var, str(home))
    for var in REDIRECTED_APPDATA_VARS:
        monkeypatch.setenv(var, str(home / "AppData" / var))
    for name in list(os.environ):
        if is_isolated_env_name(name):
            monkeypatch.delenv(name, raising=False)
    config_path = home / ".config" / "academic-research" / "config.toml"
    # `config_writer` binds its own copy of the name at import, so it has
    # to be re-pointed separately — or an unpatched write would land in
    # the developer's real config.
    monkeypatch.setattr(config_loader, "CONFIG_PATH", config_path)
    monkeypatch.setattr(config_writer, "CONFIG_PATH", config_path)
    _REAL_LOAD_CONFIG.cache_clear()
    yield home
    _REAL_LOAD_CONFIG.cache_clear()


# ---------------------------------------------------------------------------
# Live tests: deselected by default even when a marker is forgotten
# ---------------------------------------------------------------------------


def is_under_live_root(path: Path) -> bool:
    try:
        Path(path).resolve().relative_to(LIVE_ROOT)
    except ValueError:
        return False
    return True


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    """Mark any unmarked test under `tests/live/` as `live`.

    The default run's `-m 'not live and not …'` only excludes tests that
    carry one of the live markers. A live test whose author forgot the
    marker therefore ran in the default suite — against real services,
    with real keys. Adding `live` here (before the `-m` filter runs, hence
    `tryfirst`) makes location, not discipline, decide it.
    """
    for item in items:
        if not is_under_live_root(Path(str(item.fspath))):
            continue
        if any(item.get_closest_marker(m) for m in LIVE_MARKERS):
            continue
        item.add_marker(pytest.mark.live)
