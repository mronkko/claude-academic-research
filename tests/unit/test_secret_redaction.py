"""G1: an API key never appears in a URL or in error text.

The Gemini key used to ride in the query string. A key pasted with a
stray space or newline then made `urllib` raise an error quoting the
whole URL, and that text went into `ConnectionResult.detail` /
`DiscoveryError`, which are printed and returned to the assistant.

These tests use a dummy key with embedded whitespace and real
`urllib`/`http.client` code paths that fail *before* any connection is
made (a header value containing a newline is rejected locally), so no
network is touched.
"""

from __future__ import annotations

import importlib.util
import sys
import urllib.request
from pathlib import Path

import pytest
from core import config_loader, model_discovery, model_health, providers

DUMMY_KEY = "  AIzaDUMMY-secret\nvalue-0123456789  "
CORE = DUMMY_KEY.strip()
FRAGMENTS = ("AIzaDUMMY-secret", "value-0123456789")

# Port 9 (discard) on loopback; never reached because the request fails
# while its headers are still being assembled.
UNREACHABLE_BASE = "http://127.0.0.1:9"


def _assert_clean(text: str) -> None:
    for frag in FRAGMENTS:
        assert frag not in text, f"key fragment {frag!r} leaked into: {text!r}"


def test_redact_masks_plain_quoted_and_url_forms() -> None:
    text = (
        f"plain {CORE} | repr {CORE!r} | bytes {CORE.encode()!r} | "
        f"url https://h/x?key=whatever&pageSize=2"
    )
    out = providers.redact(text, CORE)
    _assert_clean(out)
    assert "whatever" not in out
    assert "pageSize=2" in out


def test_redact_ignores_empty_and_tiny_secrets() -> None:
    assert providers.redact("a b c", "", "a") == "a b c"


def test_google_listing_url_has_no_key_placeholder() -> None:
    spec = providers.require("google")
    assert "{key}" not in spec.list_models_url
    assert "key=" not in spec.list_models_url


def test_health_probe_sends_google_key_in_header(monkeypatch) -> None:
    seen = {}

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["header"] = req.get_header("X-goog-api-key")
        raise ValueError(f"boom while sending {req.full_url} with {CORE!r}")

    monkeypatch.setattr(model_health.urllib.request, "urlopen", fake_urlopen)
    result = model_health.check_connection(
        providers.require("google"), "gemini-x", api_key=DUMMY_KEY,
    )
    assert "key=" not in seen["url"]
    assert CORE not in seen["url"]
    assert seen["header"] == CORE
    _assert_clean(result.detail)
    _assert_clean(result.format())


def test_health_probe_real_header_rejection_does_not_leak() -> None:
    """The genuine `http.client` error for a newline in a header."""
    result = model_health.check_connection(
        providers.require("google"), "gemini-x",
        api_key=DUMMY_KEY, base_url=UNREACHABLE_BASE,
    )
    assert not result.ok
    _assert_clean(result.detail)
    _assert_clean(result.format())


@pytest.mark.parametrize("provider", ["google", "openai", "anthropic"])
def test_discovery_error_never_quotes_the_key(monkeypatch, provider) -> None:
    monkeypatch.setattr(model_discovery.time, "sleep", lambda _s: None)
    with pytest.raises(model_discovery.DiscoveryError) as exc:
        model_discovery.list_models(
            providers.require(provider), api_key=DUMMY_KEY, base_url=UNREACHABLE_BASE,
        )
    _assert_clean(str(exc.value))
    _assert_clean(repr(exc.value.__cause__ or ""))


def test_discovery_sends_google_key_in_header(monkeypatch) -> None:
    seen = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"models": [{"name": "models/gemini-x"}]}'

    def fake_urlopen(req, timeout=None):
        seen["url"] = req.full_url
        seen["header"] = req.get_header("X-goog-api-key")
        return _Resp()

    monkeypatch.setattr(model_discovery.urllib.request, "urlopen", fake_urlopen)
    models = model_discovery.list_models(providers.require("google"), api_key=DUMMY_KEY)
    assert [m.id for m in models] == ["gemini-x"]
    assert CORE not in seen["url"] and "key=" not in seen["url"]
    assert seen["header"] == CORE


def _load_wizard():
    path = Path(__file__).resolve().parents[2] / "scripts" / "setup" / "wizard.py"
    spec = importlib.util.spec_from_file_location("wizard_redaction_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("verifier", ["_verify_gemini", "_verify_openai"])
def test_wizard_verification_message_never_quotes_the_key(monkeypatch, verifier) -> None:
    wizard = _load_wizard()
    monkeypatch.setattr(wizard.time, "sleep", lambda _s: None)
    seen = []
    real_urlopen = urllib.request.urlopen

    def spy(req, timeout=None):
        seen.append(req.full_url)
        return real_urlopen(req, timeout=timeout)  # fails on the header, offline

    monkeypatch.setattr(wizard.urllib.request, "urlopen", spy)
    ok, msg, _extras = getattr(wizard, verifier)(CORE)
    assert not ok
    _assert_clean(msg)
    for url in seen:
        _assert_clean(url)


def test_config_values_are_stripped_like_env_values(tmp_path, monkeypatch) -> None:
    cfg = tmp_path / "config.toml"
    cfg.write_text('[gemini]\napi_key = "  padded-key \\n"\nflag = true\n', encoding="utf-8")
    monkeypatch.setattr(config_loader, "CONFIG_PATH", cfg)
    config_loader.load_config.cache_clear()
    try:
        assert config_loader.get("gemini", "api_key") == "padded-key"
        assert config_loader.get("gemini", "flag") is True
    finally:
        config_loader.load_config.cache_clear()
