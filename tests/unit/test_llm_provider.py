"""LLM provider routing and the Gemini adapter, against stubbed SDKs.

The `anthropic` and `google.genai` SDKs are replaced in `sys.modules` for
the duration of each test only. They used to be installed at import time
and never removed, so every test collected after this module saw a
MagicMock where `anthropic` should be — whether or not the real SDK was
installed.
"""

import sys
from unittest.mock import MagicMock, patch

import pytest
from core import llm_provider, providers


class MockGenerateContentConfig:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


@pytest.fixture(autouse=True)
def _stub_sdks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub the vendor SDKs; `llm_provider` imports them lazily, so the
    stubs only need to be in place while a test runs."""
    mock_genai = MagicMock()
    mock_genai.types.GenerateContentConfig = MockGenerateContentConfig
    mock_google = MagicMock()
    mock_google.genai = mock_genai
    monkeypatch.setitem(sys.modules, "anthropic", MagicMock())
    monkeypatch.setitem(sys.modules, "google", mock_google)
    monkeypatch.setitem(sys.modules, "google.genai", mock_genai)
    monkeypatch.setitem(sys.modules, "google.genai.types", mock_genai.types)


def test_get_provider_routing():
    """Verify that get_provider routes to the correct subclass by model name prefix."""
    # Claude models
    with patch("core.llm_provider.require", return_value="dummy-key"):
        with patch("anthropic.Anthropic"):
            provider = llm_provider.get_provider("claude-3-5-sonnet-20241022")
            assert isinstance(provider, llm_provider.AnthropicProvider)

    # Gemini models
    with patch("core.llm_provider.require", return_value="dummy-key"):
        provider = llm_provider.get_provider("gemini-2.5-flash")
        assert isinstance(provider, llm_provider.GeminiProvider)

    # Default fallback
    with patch("core.llm_provider.require", return_value="dummy-key"):
        with patch("anthropic.Anthropic"):
            provider = llm_provider.get_provider("unknown-model-prefix")
            assert isinstance(provider, llm_provider.AnthropicProvider)


def test_gemini_provider_generate_success():
    """Verify GeminiProvider calls generate_content with correct configurations and returns text."""
    mock_client = MagicMock()
    mock_response = MagicMock()
    mock_response.text = "DECISION: include\nREASON: Standard include"
    mock_client.models.generate_content.return_value = mock_response

    with patch("google.genai.Client", return_value=mock_client):
        provider = llm_provider.GeminiProvider(api_key="mock-api-key")

        response_text = provider.generate(
            model="gemini-2.5-flash",
            system="System Instructions Here",
            prompt="User Prompt Here",
            temperature=0.0,
            max_tokens=200,
        )

        assert response_text == "DECISION: include\nREASON: Standard include"

        # Verify generate_content parameters
        called_args = mock_client.models.generate_content.call_args
        assert called_args[1]["model"] == "gemini-2.5-flash"
        assert called_args[1]["contents"] == "User Prompt Here"

        config = called_args[1]["config"]
        assert config.system_instruction == "System Instructions Here"
        assert config.temperature == 0.0
        assert config.max_output_tokens == 200


def test_gemini_provider_generate_error():
    """Verify GeminiProvider raises RuntimeError when SDK call fails."""
    mock_client = MagicMock()
    mock_client.models.generate_content.side_effect = Exception("API key expired")

    with patch("google.genai.Client", return_value=mock_client):
        provider = llm_provider.GeminiProvider(api_key="mock-api-key")

        with pytest.raises(RuntimeError) as exc_info:
            provider.generate(
                model="gemini-2.5-flash",
                system="",
                prompt="Hello",
            )

        assert "Gemini API returned error" in str(exc_info.value)
        assert "API key expired" in str(exc_info.value)



# --- temperature on the Anthropic transport ------------------------------
# anthropic SDK 2.x dropped the `temperature` keyword and Claude 5+ models
# answer 400 "`temperature` is deprecated for this model".


def _anthropic_provider():
    with patch("core.llm_provider._api_key_for", return_value="dummy-key"):
        return llm_provider.AnthropicProvider()


def _reply(text="ok"):
    r = MagicMock()
    r.content = [MagicMock(text=text)]
    return r


@pytest.mark.parametrize(
    "model,accepts",
    [
        ("claude-haiku-5-5", False),
        ("claude-opus-5-5", False),
        ("claude-fable-5", False),
        ("claude-haiku-4-5-20251001", True),
        ("claude-3-5-sonnet-20241022", True),
        ("claude-sonnet-4-6", True),
    ],
)
def test_anthropic_accepts_temperature_by_major_version(model, accepts):
    assert providers.anthropic_accepts_temperature(model) is accepts


def test_anthropic_generate_omits_temperature_for_claude_5():
    p = _anthropic_provider()
    p.client.messages.create.return_value = _reply()
    p.generate("claude-haiku-5-5", "s", "p", temperature=0.0)
    kwargs = p.client.messages.create.call_args.kwargs
    assert "temperature" not in kwargs
    assert "extra_body" not in kwargs


def test_anthropic_generate_sends_temperature_via_extra_body_otherwise():
    p = _anthropic_provider()
    p.client.messages.create.return_value = _reply()
    p.generate("claude-sonnet-4-6", "s", "p", temperature=0.0)
    kwargs = p.client.messages.create.call_args.kwargs
    assert "temperature" not in kwargs  # not a keyword on SDK 2.x
    assert kwargs["extra_body"] == {"temperature": 0.0}


def test_anthropic_generate_retries_without_temperature_on_rejection():
    class Rejected(Exception):
        status_code = 400

    p = _anthropic_provider()
    p.client.messages.create.side_effect = [
        Rejected("`temperature` is deprecated for this model."),
        _reply("done"),
    ]
    assert p.generate("claude-sonnet-4-6", "s", "p") == "done"
    assert "extra_body" not in p.client.messages.create.call_args.kwargs


def test_anthropic_generate_does_not_swallow_other_errors():
    class Rejected(Exception):
        status_code = 400

    p = _anthropic_provider()
    p.client.messages.create.side_effect = Rejected("max_tokens too large")
    with pytest.raises(Rejected):
        p.generate("claude-sonnet-4-6", "s", "p")
