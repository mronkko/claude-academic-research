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


def _anthropic_provider(request_params=None):
    with patch("core.llm_provider._api_key_for", return_value="dummy-key"):
        return llm_provider.AnthropicProvider(request_params=request_params)


def _reply(text="ok", *, thinking=False, stop_reason="end_turn"):
    r = MagicMock()
    r.stop_reason = stop_reason
    blocks = [MagicMock(type="text", text=text)] if text is not None else []
    if thinking:
        # A real ThinkingBlock has no `.text` attribute.
        blocks.insert(0, MagicMock(type="thinking", spec=["type", "thinking"]))
    r.content = blocks
    return r


def _answers(p, *replies):
    """Serve `replies` (or exceptions) to both the create and stream paths."""
    seq = iter(replies)

    def nxt(*a, **k):
        r = next(seq)
        if isinstance(r, Exception):
            raise r
        return r
    p.client.messages.create.side_effect = nxt
    ctx = p.client.messages.stream.return_value.__enter__.return_value
    ctx.get_final_message.side_effect = nxt

    def stream(**kw):
        p.streamed.append(kw)
        return p.client.messages.stream.return_value
    p.streamed = []
    p.client.messages.stream.side_effect = stream


def _sent(p):
    """The keyword arguments of the last request, however it was sent."""
    created = p.client.messages.create.call_args
    return p.streamed[-1] if p.streamed else created.kwargs


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


def test_claude_5_gets_the_output_cap_and_medium_effort_and_no_temperature():
    p = _anthropic_provider()
    _answers(p, _reply())
    p.generate("claude-haiku-5-5", "s", "p", temperature=0.0, max_tokens=200)
    kw = _sent(p)
    assert kw["max_tokens"] == 128_000          # the caller's 200 is for older models
    assert kw["extra_body"] == {"output_config": {"effort": "medium"}}
    assert "temperature" not in kw and p.client.messages.create.call_count == 0


def test_older_models_keep_the_callers_budget_and_temperature():
    p = _anthropic_provider()
    _answers(p, _reply())
    p.generate("claude-sonnet-4-6", "s", "p", temperature=0.0, max_tokens=200)
    kw = _sent(p)
    assert kw["max_tokens"] == 200
    assert "temperature" not in kw  # not a keyword on SDK 2.x
    assert kw["extra_body"] == {"temperature": 0.0}
    assert p.streamed == []                      # small budget: not streamed


def test_review_params_merge_over_the_defaults_and_none_removes_one():
    p = _anthropic_provider({"output_config": {"effort": "low"},
                             "extra_headers": {"anthropic-beta": "x-2026"}})
    _answers(p, _reply(), _reply())
    p.generate("claude-opus-5-5", "s", "p",
               request_params={"max_tokens": 20_000, "top_k": 5})
    kw = _sent(p)
    assert kw["max_tokens"] == 20_000
    assert kw["extra_headers"] == {"anthropic-beta": "x-2026"}
    assert kw["extra_body"] == {"output_config": {"effort": "low"}, "top_k": 5}
    p.generate("claude-opus-5-5", "s", "p", request_params={"output_config": None})
    assert "extra_body" not in _sent(p)


@pytest.mark.parametrize("bad", [{"model": "x"}, {"messages": []}, "effort=low",
                                 {"extra_headers": ["a"]}])
def test_malformed_request_params_are_refused(bad):
    with pytest.raises(ValueError):
        _anthropic_provider(bad)


def test_merge_request_params_recurses_and_removes():
    base = {"a": {"b": 1, "c": 2}, "d": 3}
    assert providers.merge_request_params(base, {"a": {"b": 9, "c": None}, "d": None}) == {
        "a": {"b": 9}}
    assert base == {"a": {"b": 1, "c": 2}, "d": 3}   # not mutated


def test_anthropic_generate_retries_without_temperature_on_rejection():
    class Rejected(Exception):
        status_code = 400

    p = _anthropic_provider()
    _answers(p, Rejected("`temperature` is deprecated for this model."), _reply("done"))
    assert p.generate("claude-sonnet-4-6", "s", "p") == "done"
    assert "extra_body" not in _sent(p)


def test_anthropic_generate_does_not_swallow_other_errors():
    class Rejected(Exception):
        status_code = 400

    p = _anthropic_provider()
    _answers(p, Rejected("max_tokens too large"))
    with pytest.raises(Rejected):
        p.generate("claude-sonnet-4-6", "s", "p")


def test_anthropic_generate_skips_leading_thinking_block():
    p = _anthropic_provider()
    _answers(p, _reply("INCLUDE", thinking=True))
    assert p.generate("claude-haiku-5-5", "s", "p") == "INCLUDE"


def test_anthropic_generate_joins_text_blocks():
    p = _anthropic_provider()
    r = _reply("a ", thinking=True)
    r.content.append(MagicMock(type="text", text="b"))
    _answers(p, r)
    assert p.generate("claude-haiku-5-5", "s", "p") == "a b"


def test_anthropic_generate_names_stop_reason_when_no_text_and_does_not_retry():
    p = _anthropic_provider()
    _answers(p, _reply(None, thinking=True, stop_reason="max_tokens"), _reply())
    with pytest.raises(ValueError, match="max_tokens"):
        p.generate("claude-haiku-5-5", "s", "p")
    assert len(p.streamed) == 1


def test_get_provider_passes_params_to_claude_and_warns_for_others(capsys):
    with patch("core.llm_provider._api_key_for", return_value="dummy-key"):
        p = llm_provider.get_provider("claude-opus-5-5", request_params={"top_k": 3})
    assert p.request_params == {"top_k": 3}
    with patch("core.llm_provider.OpenAICompatProvider") as other, \
            patch("core.llm_provider.resolve_provider",
                  return_value=MagicMock(transport="openai")):
        llm_provider.get_provider("gpt-5", request_params={"top_k": 3})
    assert other.called and "Anthropic models only" in capsys.readouterr().out
