"""Model defaults, aliases, and the `--model` override.

`templates/screening_config.py` is copied into user projects and must stand
alone, so it cannot import `core.models`. It keeps literals; these tests are
what keep them equal to the constants the orchestrators fall back to.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from core import models

REPO = Path(__file__).resolve().parents[2]
TEMPLATE = REPO / "templates" / "screening_config.py"


def _template_module():
    """Execute `templates/screening_config.py` and hand back the module.

    Bytecode writing is suppressed for the duration: without that, importing
    the template drops a `.pyc` into `templates/__pycache__/`, and
    `test_zotero_mcp_sync.py` — which scans `templates/**/*` as UTF-8 text —
    then dies on the binary file.
    """
    spec = importlib.util.spec_from_file_location("screening_config_tpl", TEMPLATE)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["screening_config_tpl"] = mod
    previous = sys.dont_write_bytecode
    sys.dont_write_bytecode = True
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = previous
    return mod


# ---------------------------------------------------------------------------
# Template <-> constants
# ---------------------------------------------------------------------------


def test_template_abstract_model_matches_constant() -> None:
    assert _template_module().ABSTRACT_SCREENING_MODEL == (
        models.DEFAULT_ABSTRACT_SCREENING_MODEL
    ), (
        "templates/screening_config.py and core/models.py disagree on the "
        "abstract-screening default. The template cannot import the module "
        "(it is copied into user projects), so they are kept equal here."
    )


def test_template_fulltext_model_matches_constant() -> None:
    assert _template_module().FULLTEXT_CODING_MODEL == (
        models.DEFAULT_FULLTEXT_CODING_MODEL
    ), (
        "templates/screening_config.py and core/models.py disagree on the "
        "full-text coding default."
    )


def test_orchestrators_do_not_hardcode_a_model_id() -> None:
    """The `getattr(..., "claude-...")` fallbacks were the other half of the
    duplication; they must read from core.models now."""
    offenders = []
    for name in ("abstract_screen.py", "fulltext_code.py"):
        path = REPO / "scripts" / "pipelines" / name
        for lineno, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if re.search(r'getattr\([^)]*MODEL[^)]*"(claude|gemini)-', line):
                offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert not offenders, (
        "Orchestrator hard-codes a model ID as a getattr fallback; use the "
        "DEFAULT_* constants from core.models:\n  " + "\n  ".join(offenders)
    )


# ---------------------------------------------------------------------------
# resolve_model
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", sorted(models.ALIASES))
def test_every_alias_resolves_to_a_full_id(alias: str) -> None:
    resolved = models.resolve_model(alias)
    assert resolved == models.ALIASES[alias]
    assert "-" in resolved, f"alias {alias!r} should map to a full model ID"


def test_resolve_model_is_case_and_space_insensitive() -> None:
    assert models.resolve_model("  Haiku ") == models.ALIASES["haiku"]
    assert models.resolve_model("SONNET") == models.ALIASES["sonnet"]


def test_resolve_model_passes_unknown_names_through() -> None:
    """Explicit model IDs and locally-served names must keep working — the
    alias table is a convenience, not a whitelist."""
    assert models.resolve_model("claude-opus-4-1-20250805") == (
        "claude-opus-4-1-20250805"
    )
    assert models.resolve_model("qwen3-30b") == "qwen3-30b"


def test_resolve_model_empty_returns_empty() -> None:
    assert models.resolve_model("") == ""


def test_haiku_alias_matches_the_abstract_default() -> None:
    assert models.resolve_model("haiku") == models.DEFAULT_ABSTRACT_SCREENING_MODEL


# ---------------------------------------------------------------------------
# effective_model — precedence and the override banner
# ---------------------------------------------------------------------------


def test_effective_model_falls_back_to_config_when_flag_absent(capsys) -> None:
    assert models.effective_model("", "claude-sonnet-4-6", stage="X") == (
        "claude-sonnet-4-6"
    )
    assert capsys.readouterr().out == ""


def test_effective_model_flag_wins_and_announces(capsys) -> None:
    out = models.effective_model(
        "haiku", "claude-sonnet-4-6", stage="FULLTEXT_CODING_MODEL",
    )
    assert out == models.ALIASES["haiku"]
    printed = capsys.readouterr().out
    assert "FULLTEXT_CODING_MODEL" in printed
    assert "claude-sonnet-4-6" in printed
    assert models.ALIASES["haiku"] in printed
    assert "screening_config.py is unchanged" in printed


def test_effective_model_silent_when_flag_matches_config(capsys) -> None:
    """No banner when the flag names what the config already says — the
    banner is about divergence, not about the flag being present."""
    out = models.effective_model("haiku", models.ALIASES["haiku"], stage="X")
    assert out == models.ALIASES["haiku"]
    assert capsys.readouterr().out == ""


def test_model_flag_help_lists_every_alias() -> None:
    help_text = models.model_flag_help("some default")
    for alias in models.ALIASES:
        assert alias in help_text
    assert "some default" in help_text
