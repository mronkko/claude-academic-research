import sys
import pytest
from unittest.mock import MagicMock, patch
# Mock anthropic module so tests can run without having it installed
mock_anthropic_module = MagicMock()
sys.modules["anthropic"] = mock_anthropic_module

import json
import httpx
from core import llm_provider



def test_get_provider_routing():
    """Verify that get_provider routes to the correct subclass by model name prefix."""
    # Claude models
    with patch("core.llm_provider.require", return_value="dummy-key"):
        with patch("anthropic.Anthropic") as mock_anthropic:
            provider = llm_provider.get_provider("claude-3-5-sonnet-20241022")
            assert isinstance(provider, llm_provider.AnthropicProvider)

    # Gemini models
    with patch("core.llm_provider.require", return_value="dummy-key"):
        provider = llm_provider.get_provider("gemini-2.5-flash")
        assert isinstance(provider, llm_provider.GeminiProvider)

    # Default fallback
    with patch("core.llm_provider.require", return_value="dummy-key"):
        with patch("anthropic.Anthropic") as mock_anthropic:
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

