"""Tests for the model-pinning bootstrap and the provider-switch scripts.

`resolve_models.py` rewrites two lines of a file the user owns and keeps
in git, so the tests care as much about what it leaves alone as about
what it writes: the prompts, the coding scheme, and the line endings all
have to survive untouched.

Discovery is monkeypatched throughout — these are unit tests and must
not call a provider. `tests/live/test_auth_workflows.py` covers the real
endpoints.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SETUP = Path(__file__).resolve().parents[2] / "scripts" / "setup"


def _load(name: str):
    spec = importlib.util.spec_from_file_location(name, SETUP / f"{name}.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def resolve():
    return _load("resolve_models")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A minimal screening_config.py, shaped like the real template."""
    path = tmp_path / "screening_config.py"
    path.write_text(
        '"""Docstring the user edited."""\n'
        "\n"
        "# A comment about the pin.\n"
        'ABSTRACT_SCREENING_MODEL = ""\n'
        'ABSTRACT_SCREENING_PROMPT_VERSION = "v1"\n'
        'ABSTRACT_SCREENING_SYSTEM_PROMPT = """Screen this."""\n'
        "\n"
        'FULLTEXT_CODING_MODEL = ""\n'
        'FULLTEXT_CODING_PROMPT_VERSION = "v1"\n',
        encoding="utf-8",
    )
    return path


def _fake_resolution(mod, model: str, source: str = "discovered", detail: str = ""):
    from core import model_discovery

    return lambda *a, **kw: model_discovery.Resolution(model, source, detail)


# ---------------------------------------------------------------------------
# Stage ↔ constant schema
# ---------------------------------------------------------------------------


def test_every_stage_with_a_tier_has_a_constant(resolve) -> None:
    """A stage that maps to a tier but not to a constant would resolve a
    model and then have nowhere to write it."""
    from core.models import TIER_FOR_STAGE

    assert set(resolve.CONSTANT_FOR_STAGE) == set(TIER_FOR_STAGE)


def test_constants_match_the_shipped_template(resolve) -> None:
    """The constants named here must exist in templates/screening_config.py
    — a rename on either side leaves the pin unwritable."""
    template = (
        Path(__file__).resolve().parents[2] / "templates" / "screening_config.py"
    ).read_text(encoding="utf-8")
    for constant in resolve.CONSTANT_FOR_STAGE.values():
        assert f"{constant} = " in template


# ---------------------------------------------------------------------------
# Line rewriting
# ---------------------------------------------------------------------------


def test_pin_line_carries_provenance(resolve) -> None:
    line = resolve.pin_line("X_MODEL", "claude-haiku-4-5", "anthropic", "fast",
                            today="2026-08-13")
    assert line.startswith('X_MODEL = "claude-haiku-4-5"')
    assert "provider=anthropic" in line
    assert "tier=fast" in line
    assert "resolved 2026-08-13" in line


def test_rewrite_replaces_only_the_target_line(resolve) -> None:
    text = 'A = ""\nABSTRACT_SCREENING_MODEL = ""\nB = "keep"\n'
    out, n = resolve.rewrite_pin(text, "ABSTRACT_SCREENING_MODEL", 'X = "1"')
    assert n == 1
    assert out == 'A = ""\nX = "1"\nB = "keep"\n'


def test_rewrite_does_not_touch_the_prompt_version_constant(resolve) -> None:
    """ABSTRACT_SCREENING_MODEL is a prefix of nothing, but
    ABSTRACT_SCREENING_PROMPT_VERSION starts the same way — the anchor
    has to be the whole assignment, not a prefix match."""
    text = (
        'ABSTRACT_SCREENING_MODEL = ""\n'
        'ABSTRACT_SCREENING_PROMPT_VERSION = "v1"\n'
    )
    out, n = resolve.rewrite_pin(text, "ABSTRACT_SCREENING_MODEL", 'PINNED = "x"')
    assert n == 1
    assert 'ABSTRACT_SCREENING_PROMPT_VERSION = "v1"' in out


def test_rewrite_reports_a_missing_constant(resolve) -> None:
    out, n = resolve.rewrite_pin("NOTHING = 1\n", "ABSTRACT_SCREENING_MODEL", "X")
    assert n == 0
    assert out == "NOTHING = 1\n"


def test_rewrite_replaces_an_existing_pin_without_stacking_comments(
    resolve,
) -> None:
    """Re-running must not accumulate provenance comments."""
    first = resolve.pin_line("M", "a-1", "anthropic", "fast", today="2026-01-01")
    second = resolve.pin_line("M", "a-2", "anthropic", "fast", today="2026-08-13")
    text, _ = resolve.rewrite_pin('M = ""\n', "M", first)
    text, n = resolve.rewrite_pin(text, "M", second)
    assert n == 1
    assert text.count("resolved") == 1
    assert "a-2" in text and "a-1" not in text


def test_rewrite_handles_a_model_id_with_a_backslash(resolve) -> None:
    """A regex-special replacement string must be inserted literally."""
    line = resolve.pin_line("M", r"weird\1model", "openrouter", "fast")
    out, n = resolve.rewrite_pin('M = ""\n', "M", line)
    assert n == 1
    assert r"weird\1model" in out


# ---------------------------------------------------------------------------
# main()
# ---------------------------------------------------------------------------


def test_main_writes_both_pins(resolve, project, monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        resolve.model_discovery, "resolve_tier", _fake_resolution(resolve, "m-1"),
    )
    rc = resolve.main(["--config", str(project), "--provider", "anthropic"])
    assert rc == 0

    written = project.read_text(encoding="utf-8")
    assert 'ABSTRACT_SCREENING_MODEL = "m-1"' in written
    assert 'FULLTEXT_CODING_MODEL = "m-1"' in written
    # Everything else survives.
    assert '"""Docstring the user edited."""' in written
    assert 'ABSTRACT_SCREENING_SYSTEM_PROMPT = """Screen this."""' in written
    assert 'ABSTRACT_SCREENING_PROMPT_VERSION = "v1"' in written


def test_main_uses_the_stage_default_tiers(resolve, project, monkeypatch) -> None:
    """Abstract screening asks for the fast tier and full-text coding for
    the balanced one — the cost decision pinned in TIER_FOR_STAGE."""
    seen: list[str] = []

    def spy(_spec, tier, **_kw):
        from core import model_discovery

        seen.append(tier)
        return model_discovery.Resolution(f"model-for-{tier}", "discovered")

    monkeypatch.setattr(resolve.model_discovery, "resolve_tier", spy)
    resolve.main(["--config", str(project), "--provider", "anthropic"])
    assert seen == ["fast", "balanced"]


def test_main_stage_and_tier_pins_one_line(resolve, project, monkeypatch) -> None:
    """The permanent-change path: --stage X --tier deep rewrites only X."""
    monkeypatch.setattr(
        resolve.model_discovery, "resolve_tier", _fake_resolution(resolve, "big-1"),
    )
    rc = resolve.main([
        "--config", str(project), "--provider", "anthropic",
        "--stage", "fulltext_coding", "--tier", "deep",
    ])
    assert rc == 0

    written = project.read_text(encoding="utf-8")
    assert 'FULLTEXT_CODING_MODEL = "big-1"' in written
    assert "tier=deep" in written
    assert 'ABSTRACT_SCREENING_MODEL = ""' in written


def test_main_rejects_tier_without_stage(resolve, project) -> None:
    with pytest.raises(SystemExit):
        resolve.main(["--config", str(project), "--tier", "deep"])


def test_main_dry_run_writes_nothing(resolve, project, monkeypatch, capsys) -> None:
    before = project.read_text(encoding="utf-8")
    monkeypatch.setattr(
        resolve.model_discovery, "resolve_tier", _fake_resolution(resolve, "m-1"),
    )
    rc = resolve.main(["--config", str(project), "--provider", "anthropic",
                       "--dry-run"])
    assert rc == 0
    assert project.read_text(encoding="utf-8") == before
    assert "m-1" in capsys.readouterr().out


def test_main_announces_a_catalogue_fallback(
    resolve, project, monkeypatch, capsys,
) -> None:
    """A pin that came from the shipped file, not the provider, must say
    so — silence there is how a project stays on a superseded model."""
    monkeypatch.setattr(
        resolve.model_discovery, "resolve_tier",
        _fake_resolution(resolve, "old-1", source="catalog", detail="HTTP 401"),
    )
    rc = resolve.main(["--config", str(project), "--provider", "anthropic"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "WARNING" in err
    assert "HTTP 401" in err


def test_main_fails_when_nothing_resolves(
    resolve, project, monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(
        resolve.model_discovery, "resolve_tier",
        _fake_resolution(resolve, "", source="none", detail="unreachable"),
    )
    before = project.read_text(encoding="utf-8")
    rc = resolve.main(["--config", str(project), "--provider", "anthropic"])
    assert rc == 1
    assert project.read_text(encoding="utf-8") == before
    assert "ERROR" in capsys.readouterr().err


def test_main_rejects_an_unknown_provider(resolve, project, capsys) -> None:
    rc = resolve.main(["--config", str(project), "--provider", "anthorpic"])
    assert rc == 2
    assert "anthorpic" in capsys.readouterr().err


def test_main_reports_a_missing_config_file(
    resolve, tmp_path, monkeypatch, capsys,
) -> None:
    monkeypatch.setattr(
        resolve.model_discovery, "resolve_tier", _fake_resolution(resolve, "m-1"),
    )
    rc = resolve.main([
        "--config", str(tmp_path / "nope.py"), "--provider", "anthropic",
    ])
    assert rc == 1
    assert "not found" in capsys.readouterr().err


def test_main_preserves_crlf_line_endings(
    resolve, tmp_path, monkeypatch,
) -> None:
    """The file is in the user's git repo. Flipping every line ending
    would bury the two lines that actually changed."""
    path = tmp_path / "screening_config.py"
    path.write_bytes(
        b'ABSTRACT_SCREENING_MODEL = ""\r\nFULLTEXT_CODING_MODEL = ""\r\n',
    )
    monkeypatch.setattr(
        resolve.model_discovery, "resolve_tier", _fake_resolution(resolve, "m-1"),
    )
    resolve.main(["--config", str(path), "--provider", "anthropic"])

    raw = path.read_bytes()
    assert b"\r\n" in raw
    assert b"\n" not in raw.replace(b"\r\n", b"")


# ---------------------------------------------------------------------------
# check_llm_provider.py / set_llm_provider.py
# ---------------------------------------------------------------------------


@pytest.fixture
def _redirect_config(tmp_path: Path, monkeypatch):
    """Point the loader and writer at an ephemeral config.toml."""
    from core import config_loader, config_writer, providers

    cfg = tmp_path / ".config" / "academic-research" / "config.toml"
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text("", encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", cfg)
    monkeypatch.setattr(config_writer, "CONFIG_PATH", cfg)
    config_loader.load_config.cache_clear()
    monkeypatch.delenv(providers.PROVIDER_ENV, raising=False)
    for spec in providers.PROVIDERS:
        for env in (spec.api_key_env, spec.base_url_env):
            if env:
                monkeypatch.delenv(env, raising=False)
    return cfg


def test_check_reports_the_default_as_unselected(_redirect_config) -> None:
    """"Never asked" and "chose Anthropic" must be distinguishable — a
    skill decides whether to suggest /setup on exactly that."""
    check = _load("check_llm_provider")
    name, selected = check.configured_name()
    from core import providers

    assert name == providers.DEFAULT_PROVIDER
    assert selected is False


def test_check_reports_a_configured_provider(_redirect_config) -> None:
    _redirect_config.write_text('[llm]\nprovider = "ollama"\n', encoding="utf-8")
    from core import config_loader

    config_loader.load_config.cache_clear()

    check = _load("check_llm_provider")
    assert check.configured_name() == ("ollama", True)


def test_check_ignores_an_unknown_configured_name(_redirect_config) -> None:
    _redirect_config.write_text('[llm]\nprovider = "bogus"\n', encoding="utf-8")
    from core import config_loader, providers

    config_loader.load_config.cache_clear()

    check = _load("check_llm_provider")
    assert check.configured_name() == (providers.DEFAULT_PROVIDER, False)


def test_check_status_never_prints_a_key(_redirect_config) -> None:
    _redirect_config.write_text(
        '[openai]\napi_key = "sk-secret-value"\n', encoding="utf-8",
    )
    from core import config_loader, providers

    config_loader.load_config.cache_clear()

    check = _load("check_llm_provider")
    lines = check.status_lines(providers.require("openai"), selected=True)
    blob = "\n".join(lines)
    assert "sk-secret-value" not in blob
    assert "credential: configured" in blob


def test_check_status_names_the_missing_variable(_redirect_config) -> None:
    from core import providers

    check = _load("check_llm_provider")
    blob = "\n".join(check.status_lines(providers.require("openai"), selected=True))
    assert "credential: missing (OPENAI_API_KEY)" in blob


def test_check_status_local_needs_no_credential(_redirect_config) -> None:
    from core import providers

    check = _load("check_llm_provider")
    blob = "\n".join(check.status_lines(providers.require("ollama"), selected=True))
    assert "credential: not required (local provider)" in blob
    assert "base_url: http://localhost:11434" in blob


def test_set_writes_the_provider(_redirect_config, capsys) -> None:
    import tomllib

    setter = _load("set_llm_provider")
    assert setter.main(["openrouter"]) == 0
    parsed = tomllib.loads(_redirect_config.read_text(encoding="utf-8"))
    assert parsed["llm"]["provider"] == "openrouter"


def test_set_preserves_other_config(_redirect_config) -> None:
    import tomllib

    _redirect_config.write_text(
        '[zotero]\napi_key = "z"\n\n[llm]\nmax_retries = "9"\n', encoding="utf-8",
    )
    from core import config_loader

    config_loader.load_config.cache_clear()

    setter = _load("set_llm_provider")
    assert setter.main(["google"]) == 0
    parsed = tomllib.loads(_redirect_config.read_text(encoding="utf-8"))
    assert parsed["zotero"]["api_key"] == "z"
    assert parsed["llm"]["max_retries"] == "9"
    assert parsed["llm"]["provider"] == "google"


def test_set_reports_a_missing_credential_without_prompting(
    _redirect_config, capsys, monkeypatch,
) -> None:
    """Adding a key is /setup's job — a key typed into a conversation is
    a key in a transcript."""
    def boom(*_a, **_kw):
        raise AssertionError("must not prompt for a credential")

    monkeypatch.setattr("builtins.input", boom)
    setter = _load("set_llm_provider")
    assert setter.main(["openai"]) == 0
    out = capsys.readouterr().out
    assert "OPENAI_API_KEY" in out
    assert "/setup" in out
    assert "resolve_models.py" in out


def test_set_rejects_an_unknown_provider(_redirect_config, capsys) -> None:
    setter = _load("set_llm_provider")
    assert setter.main(["chatgtp"]) == 2
    assert "chatgtp" in capsys.readouterr().err


def test_set_list_shows_every_provider(_redirect_config, capsys) -> None:
    from core import providers

    setter = _load("set_llm_provider")
    assert setter.main(["--list"]) == 0
    out = capsys.readouterr().out
    for spec in providers.PROVIDERS:
        assert spec.name in out


def test_wizard_and_runtime_agree_on_credential_status(_redirect_config) -> None:
    """The wizard checks pending values as well as the file, so it has
    its own implementation. It must reach the same verdict as the runtime
    for every provider once nothing is pending — otherwise /setup blesses
    a configuration that then fails on item 1."""
    from core import config_loader, llm_provider, providers

    # Both sides must read the same data: the wizard from the dict it
    # was handed, the runtime from config.toml.
    _redirect_config.write_text('[anthropic]\napi_key = "sk-x"\n', encoding="utf-8")
    config_loader.load_config.cache_clear()
    existing = {"anthropic": {"api_key": "sk-x"}}

    wizard = _load("wizard")
    for spec in providers.PROVIDERS:
        wizard_ok, _hint = wizard._llm_credential_present(spec.name, {}, existing)
        runtime_ok, _missing = llm_provider.credential_status(spec)
        assert wizard_ok == runtime_ok, f"disagreement on {spec.name}"
