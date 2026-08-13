"""Live tests for every authenticated service's auth workflow.

Reuses the `_verify_*` helpers from `scripts/setup/wizard.py` so the
tests exercise exactly the same code path the wizard uses at setup
time. Each test fetches the real key via `core.config_loader` (env var
precedence), calls the matching verifier, and asserts `ok=True`.

Opt in with `pytest -m live`. Each test skips cleanly if its key is
not configured.

When a new `KeySpec` is added to `wizard.py:KEYS`, add a matching test
here. The unit-level guard at `tests/unit/test_live_coverage.py`
enforces this.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.live.conftest import http_get, require_config

pytestmark = pytest.mark.live

WIZARD_PATH = Path(__file__).resolve().parents[2] / "scripts" / "setup" / "wizard.py"


def _wizard():
    """Load the wizard module by path, since it's not in a package."""
    spec = importlib.util.spec_from_file_location("wizard", WIZARD_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["wizard"] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# Each test follows the same shape: load key → call verifier → assert ok.
# ---------------------------------------------------------------------------


def test_auth_zotero() -> None:
    key = require_config("zotero", "api_key", env="ZOTERO_API_KEY")
    ok, msg, extras = _wizard()._verify_zotero(key)
    assert ok, f"Zotero auth failed: {msg}"
    assert extras.get("user_id"), f"Zotero verify missing user_id: {extras}"


def test_auth_anthropic() -> None:
    key = require_config("anthropic", "api_key", env="ANTHROPIC_API_KEY")
    ok, msg, _ = _wizard()._verify_anthropic(key)
    assert ok, f"Anthropic auth failed: {msg}"


def test_auth_wos_extended() -> None:
    key = require_config("wos", "expanded_key", env="WOS_API_KEY_EXTENDED")
    ok, msg, _ = _wizard()._verify_wos_extended(key)
    assert ok, f"WoS Expanded auth failed: {msg}"


def test_auth_wos_starter() -> None:
    key = require_config("wos", "starter_key", env="WOS_API_KEY")
    ok, msg, _ = _wizard()._verify_wos_starter(key)
    assert ok, f"WoS Starter auth failed: {msg}"


def test_auth_elsevier() -> None:
    key = require_config("elsevier", "api_key", env="ELSEVIER_API_KEY")
    ok, msg, _ = _wizard()._verify_elsevier(key)
    assert ok, f"Elsevier auth failed: {msg}"


def test_auth_scopus() -> None:
    key = require_config("scopus", "api_key", env="SCOPUS_API_KEY")
    ok, msg, _ = _wizard()._verify_scopus(key)
    assert ok, f"Scopus auth failed: {msg}"


def test_auth_semantic_scholar() -> None:
    key = require_config("semantic_scholar", "api_key",
                         env="SEMANTIC_SCHOLAR_API_KEY")
    ok, msg, _ = _wizard()._verify_semantic_scholar(key)
    assert ok, f"Semantic Scholar auth failed: {msg}"


def test_auth_core() -> None:
    key = require_config("core", "api_key", env="CORE_API_KEY")
    ok, msg, _ = _wizard()._verify_core(key)
    assert ok, f"CORE auth failed: {msg}"


def test_auth_crossref_mailto() -> None:
    mailto = require_config("crossref", "mailto", env="CROSSREF_MAILTO")
    ok, msg, _ = _wizard()._verify_crossref_mailto(mailto)
    assert ok, f"Crossref mailto format invalid: {msg}"


def test_auth_wiley_tdm_placeholder() -> None:
    """Wiley TDM has no cheap auth-only probe (see wizard rationale).

    We still emit a test so the coverage guard is satisfied. It skips
    with an explanatory reason; the real check happens in
    `test_pdf_endpoints.py::test_wiley_tdm_downloads_pdf`, which
    actually exercises the token.
    """
    require_config("wiley", "tdm_token", env="WILEY_TDM_TOKEN")
    pytest.skip(
        "Wiley TDM has no cheap auth-only probe; actual auth verified "
        "by test_wiley_tdm_downloads_pdf in test_pdf_endpoints.py."
    )


def test_auth_openalex_placeholder() -> None:
    """OpenAlex paid tier auth can't be probed cheaply (see wizard rationale).

    Same pattern as the Wiley test — skips with reason; the real check
    is `test_openalex_content_api_returns_pdf_bytes`.
    """
    require_config("openalex", "api_key", env="OPENALEX_API_KEY")
    pytest.skip(
        "OpenAlex paid key has no auth-only endpoint; actual auth "
        "verified by test_openalex_content_api_returns_pdf_bytes."
    )


def test_auth_gemini() -> None:
    key = require_config("gemini", "api_key", env="GEMINI_API_KEY")
    ok, msg, _ = _wizard()._verify_gemini(key)
    assert ok, f"Gemini auth failed: {msg}"


def test_auth_anthropic_base_url() -> None:
    """ANTHROPIC_BASE_URL is a plain URL, not a credential — there is no
    auth to verify. This confirms the endpoint the user configured is
    reachable and speaks the Anthropic Messages API well enough to answer.

    Skips when unset, which is the normal case: blank means "use
    api.anthropic.com", already covered by test_auth_anthropic.
    """
    base = require_config("anthropic", "base_url", env="ANTHROPIC_BASE_URL")
    status, _body, _headers = http_get(base.rstrip("/") + "/v1/models")
    assert status != 0, f"{base} unreachable (network/DNS error)"
    # 401/404 are acceptable — they prove something is listening and
    # routing. Only a transport failure (0) means the URL is wrong.


def test_auth_openai() -> None:
    key = require_config("openai", "api_key", env="OPENAI_API_KEY")
    ok, msg, _ = _wizard()._verify_openai(key)
    assert ok, f"OpenAI auth failed: {msg}"


def test_auth_openrouter() -> None:
    key = require_config("openrouter", "api_key", env="OPENROUTER_API_KEY")
    ok, msg, _ = _wizard()._verify_openrouter(key)
    assert ok, f"OpenRouter auth failed: {msg}"


def test_auth_openai_base_url() -> None:
    """OPENAI_BASE_URL is a plain URL, not a credential — same shape as
    the Anthropic one above. Confirms the gateway the user configured is
    listening; whether it accepts their key is what test_auth_openai
    covers.

    Skips when unset, which is the normal case: blank means
    api.openai.com.
    """
    base = require_config("openai", "base_url", env="OPENAI_BASE_URL")
    status, _body, _headers = http_get(base.rstrip("/") + "/v1/models")
    assert status != 0, f"{base} unreachable (network/DNS error)"


# ---------------------------------------------------------------------------
# Local providers.
#
# No key exists to verify, so reachability is the whole test — and the
# endpoint only exists while the user has the app running. These skip
# rather than fail when nothing answers: a laptop with LM Studio closed
# is not a broken configuration, and a live suite that goes red for that
# reason stops being read.
# ---------------------------------------------------------------------------


def _local_base(provider: str, env: str) -> str:
    """Configured base URL for a local provider, else its default."""
    from core import providers
    from core.config_loader import get

    spec = providers.require(provider)
    return providers.base_url_for(spec, get(provider, "base_url", env=env))


def test_auth_ollama_base_url() -> None:
    """OLLAMA_BASE_URL points at a local Ollama server (no API key)."""
    base = _local_base("ollama", "OLLAMA_BASE_URL")
    ok, msg, _ = _wizard()._verify_ollama_base_url(base)
    if not ok and "nothing answered" in msg:
        pytest.skip(f"no Ollama server at {base}; skipping ({msg})")
    assert ok, f"Ollama endpoint check failed: {msg}"


def test_auth_lmstudio_base_url() -> None:
    """LMSTUDIO_BASE_URL points at a local LM Studio server (no API key)."""
    base = _local_base("lmstudio", "LMSTUDIO_BASE_URL")
    ok, msg, _ = _wizard()._verify_lmstudio_base_url(base)
    if not ok and "nothing answered" in msg:
        pytest.skip(f"no LM Studio server at {base}; skipping ({msg})")
    assert ok, f"LM Studio endpoint check failed: {msg}"


def test_auth_library_openurl_base() -> None:
    """openurl_base is a plain URL, not a credential — there's no auth
    to verify. This confirms whatever endpoint the user configured
    actually responds; the real getFullTxt behavior (SFX vs. Alma
    shape, real coverage answers) is exercised institution-specifically
    by test_library_resolver_alma.py, since every library's holdings
    and endpoint flavor differ.
    """
    base = require_config("library", "openurl_base", env="LIBRARY_OPENURL_BASE")
    status, _body, _headers = http_get(base)
    assert status != 0, f"{base} unreachable (network/DNS error)"

