"""Unified LLM provider layer for the academic-research plugin.

Supports Anthropic (Claude) and Google Gemini (Antigravity) models through
a unified client interface. Extensible to other providers (e.g. OpenAI)
by registering new subclasses of LLMProvider.
"""

from __future__ import annotations

import os
import sys
import httpx
from pathlib import Path

# Add parent directory to path to enable core imports
SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from core.config_loader import require, get  # noqa: E402


class LLMProvider:
    """Base class for unified LLM providers."""

    def generate(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 1000,
    ) -> str:
        """Generate a response from the LLM."""
        raise NotImplementedError("Subclasses must implement generate().")


class AnthropicProvider(LLMProvider):
    """Provider for Anthropic (Claude) models."""

    def __init__(self, api_key: str | None = None):
        if not api_key:
            api_key = require("anthropic", "api_key", env="ANTHROPIC_API_KEY")
        
        try:
            import anthropic
        except ImportError:
            raise ImportError(
                "Anthropic Python SDK is required for Claude models. "
                "Ensure 'anthropic' is installed."
            )
        self.client = anthropic.Anthropic(api_key=api_key)

    def generate(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 1000,
    ) -> str:
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            temperature=temperature,
            system=system,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text.strip()


class GeminiProvider(LLMProvider):
    """Provider for Google Gemini models using the official Google GenAI Python SDK."""

    def __init__(self, api_key: str | None = None):
        if not api_key:
            # Try to get GEMINI_API_KEY first, fallback to checking config.toml under gemini.api_key
            api_key = get("gemini", "api_key", env="GEMINI_API_KEY")
            if not api_key:
                api_key = require("gemini", "api_key", env="GEMINI_API_KEY")
        
        try:
            from google import genai
        except ImportError:
            raise ImportError(
                "Google GenAI SDK is required for Gemini models. "
                "Ensure 'google-genai' is installed."
            )
        self.client = genai.Client(api_key=api_key)

    def generate(
        self,
        model: str,
        system: str,
        prompt: str,
        temperature: float = 0.0,
        max_tokens: int = 1000,
    ) -> str:
        # Standardize Gemini model names (remove 'models/' prefix if user includes it)
        clean_model = model
        if clean_model.startswith("models/"):
            clean_model = clean_model.replace("models/", "")

        from google.genai import types

        config = types.GenerateContentConfig(
            temperature=temperature,
        )
        if system:
            config.system_instruction = system
        if max_tokens:
            config.max_output_tokens = max_tokens

        try:
            response = self.client.models.generate_content(
                model=clean_model,
                contents=prompt,
                config=config,
            )
            return response.text.strip()
        except Exception as e:
            raise RuntimeError(f"Gemini API returned error: {e}")


def get_provider(model_name: str) -> LLMProvider:
    """Factory function to get the appropriate LLMProvider for a given model."""
    name_lower = model_name.lower()
    if name_lower.startswith("claude-"):
        return AnthropicProvider()
    elif name_lower.startswith("gemini-"):
        return GeminiProvider()
    else:
        # Extensible default: fallback to Anthropic but raise a helpful warning
        print(
            f"Warning: Unknown model prefix for '{model_name}'. Defaulting to Anthropic.",
            file=sys.stderr,
        )
        return AnthropicProvider()
