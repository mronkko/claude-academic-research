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

from tests.live.conftest import http_get, require_config, require_zotero_local

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


# ---------------------------------------------------------------------------
# Zotero Desktop. The plugin's only Zotero surface is the local API, and
# its one credential is the write key `/setup` obtains through Zotero's
# consent dialog. It is not a KeySpec (nobody pastes it in), so the
# coverage guard pins these two tests by name.
# ---------------------------------------------------------------------------


def test_auth_zotero_local_read() -> None:
    """The wizard's read gate passes: Desktop serves the personal library."""
    require_zotero_local()
    ok, msg = _wizard()._zotero_read_test()
    assert ok, f"Zotero local read test failed: {msg}"


def test_auth_zotero_local_api_key() -> None:
    """ZOTERO_LOCAL_API_KEY (or [zotero] local_api_key) is accepted for a
    write: create one note and delete it, through the wizard's own write
    primitive.

    The wizard's `_zotero_write_test` writes to the personal library;
    this does the same create-then-delete in the disposable e2e group
    instead, so a live run never touches My Library. Skips without a key
    on file rather than requesting one — that would open Zotero's
    consent dialog.
    """
    from tests.live.conftest import e2e_group

    key = require_zotero_local()
    library = f"groups/{e2e_group()['id']}"
    wizard = _wizard()
    server_id = wizard._zotero_local_server_id()
    assert server_id, "Zotero sent no Zotero-Server-ID: too old for local writes"

    status, result = wizard._zotero_local_write(
        "POST", f"{library}/items", key, server_id,
        body=[{"itemType": "note", "note": wizard._WRITE_TEST_NOTE, "tags": []}],
    )
    assert status != 401, "Zotero rejected the configured local write key"
    created = (result or {}).get("successful", {}) if isinstance(result, dict) else {}
    assert status == 200 and created, f"local write failed (HTTP {status}): {result!r}"
    note = next(iter(created.values()))
    status, _ = wizard._zotero_local_write(
        "DELETE", f"{library}/items/{note['key']}", key, server_id,
        headers={"If-Unmodified-Since-Version": str(note["version"])},
    )
    assert status in (200, 204), (
        f"deleting test note {note['key']} failed (HTTP {status}); delete it "
        f"from the e2e group by hand"
    )


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
# Institutional gateway.
#
# Nothing here names an institution: the address and the key both come
# from config or the environment, so this file stays generic while
# whoever runs it points at whichever gateway they have. Many such
# gateways are reachable only from the institution's network, so a
# transport failure skips rather than fails — same reasoning as the
# local providers below.
# ---------------------------------------------------------------------------


def test_auth_gateway_base_url() -> None:
    """covers: [gateway].base_url — an OpenAI-compatible endpoint.

    Config-only: the gateway declares no environment variable, so this
    reads the file. A 401 counts as reachable — see the verifier.
    """
    base = require_config("gateway", "base_url")
    ok, msg, _ = _wizard()._verify_gateway_base_url(base)
    if not ok and "nothing answered" in msg:
        pytest.skip(f"no gateway at {base} (VPN down?); skipping ({msg})")
    assert ok, f"gateway endpoint check failed: {msg}"


def test_auth_gateway_key() -> None:
    """covers: [gateway].api_key — authenticates against that endpoint.

    Resolved through `llm_provider`, not `require_config`, so it follows
    the same two-step the runtime does: `[gateway] api_key`, or a
    variable the user named in `[gateway] api_key_env`. Reading the
    config key directly would silently skip for everyone who chose the
    indirection — which is the whole reason the indirection exists.

    Skips when the base URL is not on file: a key cannot be checked
    against nothing.
    """
    from core import llm_provider, providers

    spec = providers.require("gateway")
    key = llm_provider._api_key_for(spec, required=False)
    if not key:
        pytest.skip(
            "no gateway key on file (config [gateway].api_key, or the "
            "variable named by [gateway].api_key_env); skipping live test."
        )
    ok, msg, _ = _wizard()._verify_gateway_key(key)
    if not ok and ("no gateway base URL on file" in msg
                   or "could not reach" in msg):
        pytest.skip(f"gateway not reachable/configured; skipping ({msg})")
    assert ok, f"gateway auth failed: {msg}"


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

