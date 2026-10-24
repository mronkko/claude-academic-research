"""Guard: the suite-wide isolation fixture in tests/conftest.py stays complete.

`_isolate_from_user_environment` clears plugin-relevant environment
variables by *shape* (prefixes and suffixes) rather than by a hand-kept
list. This module derives the real list from the code — every literal
variable name `scripts/` reads through `os.environ`, `os.getenv` or a
`config_loader` `env=` argument — and fails when a name escapes the rules,
so adding `FOO_SECRET` to a fetcher cannot silently re-open the leak that
made results depend on the developer's shell.
"""

from __future__ import annotations

import ast
import os
import re
from pathlib import Path

from core import config_loader, config_writer

from tests.conftest import is_isolated_env_name

REPO = Path(__file__).resolve().parents[2]
SCANNED = [REPO / "scripts", REPO / "editorial-tools"]

#: Names the code reads that describe the machine, not the user's
#: configuration — clearing them would only break the code reading them.
_NOT_CONFIGURATION = {
    "LOCALAPPDATA",   # redirected (not cleared) by the fixture
    "APPDATA",        # redirected (not cleared) by the fixture
    "HOME",
    "USERPROFILE",
    "PATH",
    "CLAUDE_PLUGIN_ROOT",  # set by the host; tests that need it set it
    "CLAUDE_PROJECT_DIR",  # set by the host; tests that need it set it
    "NO_COLOR",
    "TERM",
    "PYTHONIOENCODING",
}


def _env_names_read(tree: ast.AST) -> set[str]:
    """String literals used as environment-variable names in `tree`.

    Catches the direct reads (`os.environ[...]`, `.get`, `getenv`), the
    `env=` / `*_env=` / `env_var=` keyword the config helpers and
    `KeySpec`/`ProviderSpec` take, and `*_ENV = "..."` constants.
    """
    names: set[str] = set()

    def literal(node: ast.AST | None) -> str | None:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            attr = func.attr if isinstance(func, ast.Attribute) else (
                func.id if isinstance(func, ast.Name) else ""
            )
            # os.environ.get("X") / os.getenv("X") / os.environ.pop("X")
            if attr in {"getenv", "get", "pop", "setdefault"} and node.args:
                owner = func.value if isinstance(func, ast.Attribute) else None
                owner_src = ast.unparse(owner) if owner is not None else ""
                if attr == "getenv" or owner_src.endswith("environ"):
                    name = literal(node.args[0])
                    if name:
                        names.add(name)
            # config_loader.get(..., env="X") / require(..., env="X")
            for kw in node.keywords:
                if kw.arg and (kw.arg == "env" or kw.arg.endswith(("_env", "env_var"))):
                    name = literal(kw.value)
                    if name:
                        names.add(name)
        # PROVIDER_ENV = "X" — the name is held in a constant and read later
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(t, ast.Name) and t.id.upper().endswith(("_ENV", "ENV_VAR"))
                   for t in targets):
                name = literal(node.value)
                if name:
                    names.add(name)
        # os.environ["X"]
        elif isinstance(node, ast.Subscript) and ast.unparse(node.value).endswith("environ"):
            name = literal(node.slice)
            if name:
                names.add(name)
    return names


def _all_env_names() -> dict[str, list[str]]:
    found: dict[str, list[str]] = {}
    for root in SCANNED:
        for path in root.rglob("*.py"):
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except SyntaxError:
                continue
            for name in _env_names_read(tree):
                found.setdefault(name, []).append(str(path.relative_to(REPO)))
    return found


def test_the_scan_finds_the_variables_it_is_meant_to() -> None:
    """Without this, a broken scan would pass by finding nothing."""
    names = _all_env_names()
    for expected in ("ZOTERO_LOCAL_API_KEY", "ACADEMIC_RESEARCH_PROVIDER", "WILEY_TDM_TOKEN"):
        assert expected in names, f"scan no longer sees {expected}; found {sorted(names)}"


def test_every_variable_the_code_reads_is_cleared_for_tests() -> None:
    escaped = {
        name: where for name, where in _all_env_names().items()
        if re.fullmatch(r"[A-Z][A-Z0-9_]+", name)
        and name not in _NOT_CONFIGURATION
        and not is_isolated_env_name(name)
    }
    assert not escaped, (
        "These environment variables are read by the code but not cleared "
        "by `_isolate_from_user_environment` in tests/conftest.py, so a "
        "developer's shell can change test outcomes. Extend the "
        "ISOLATED_ENV_* rules there (or, if the variable describes the "
        "machine rather than configuration, add it to _NOT_CONFIGURATION "
        "here with a reason):\n  "
        + "\n  ".join(f"{n}  ({', '.join(sorted(set(w)))})" for n, w in sorted(escaped.items()))
    )


def test_home_and_config_point_into_a_temporary_directory(tmp_path) -> None:
    home = Path.home()
    assert home.is_relative_to(tmp_path.parent), "HOME is not a pytest temp dir"
    assert config_loader.CONFIG_PATH.is_relative_to(home)
    assert config_writer.CONFIG_PATH == config_loader.CONFIG_PATH
    assert not config_loader.CONFIG_PATH.exists()
    assert config_loader.load_config() == {}


def test_plugin_variables_are_absent() -> None:
    leaked = sorted(n for n in os.environ if is_isolated_env_name(n))
    assert not leaked, f"isolation fixture left these set: {leaked}"


def test_the_config_cache_does_not_carry_between_tests_part_one(monkeypatch) -> None:
    """Load a config into the cache; part two must not see it."""
    path = config_loader.CONFIG_PATH
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('[zotero]\napi_key = "from-part-one"\n', encoding="utf-8")
    assert config_loader.get("zotero", "api_key") == "from-part-one"


def test_the_config_cache_does_not_carry_between_tests_part_two() -> None:
    assert config_loader.get("zotero", "api_key") == ""
