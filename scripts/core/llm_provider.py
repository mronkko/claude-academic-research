"""Unified LLM provider layer for the academic-research plugin.

Supports Anthropic (Claude) and Google Gemini (Antigravity) models through
a unified client interface. Extensible to other providers (e.g. OpenAI)
by registering new subclasses of LLMProvider.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Add parent directory to path to enable core imports
SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from core.config_loader import get, require  # noqa: E402


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


def anthropic_base_url() -> str:
    """Optional Anthropic-compatible endpoint, or `""` for the real API.

    Set `[anthropic] base_url` in config.toml or `$ANTHROPIC_BASE_URL` to
    point the screening pipelines at a local server. Open WebUI and LM
    Studio both expose Anthropic-compatible `/v1/messages` endpoints, which
    makes local models a workable alternative for high-volume abstract
    screening (issue #1).
    """
    return get("anthropic", "base_url", env="ANTHROPIC_BASE_URL") or ""


class AnthropicProvider(LLMProvider):
    """Provider for Anthropic (Claude) models, or any Anthropic-compatible
    endpoint named by `ANTHROPIC_BASE_URL` / `[anthropic] base_url`."""

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        if base_url is None:
            base_url = anthropic_base_url()

        if not api_key:
            if base_url:
                # Local servers generally ignore the key but the SDK still
                # requires one. Fall back to a placeholder rather than
                # forcing users to invent an Anthropic key they won't use.
                api_key = get(
                    "anthropic", "api_key", env="ANTHROPIC_API_KEY",
                ) or "not-required-for-local-endpoint"
            else:
                api_key = require("anthropic", "api_key", env="ANTHROPIC_API_KEY")

        try:
            import anthropic
        except ImportError as e:
            raise ImportError(
                "Anthropic Python SDK is required for Claude models. "
                "Ensure 'anthropic' is installed."
            ) from e
        kwargs: dict = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = anthropic.Anthropic(**kwargs)

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
        except ImportError as e:
            raise ImportError(
                "Google GenAI SDK is required for Gemini models. "
                "Ensure 'google-genai' is installed."
            ) from e
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
            raise RuntimeError(f"Gemini API returned error: {e}") from e


def require_credentials(model_name: str) -> None:
    """Pre-flight: fail fast if the credential this model needs is missing.

    Called before a run starts so a missing key surfaces immediately rather
    than after the first item has been fetched. Skips the Anthropic key
    check when `ANTHROPIC_BASE_URL` points at a local endpoint — those
    generally have no key to check.
    """
    if model_name.lower().startswith("gemini-"):
        require("gemini", "api_key", env="GEMINI_API_KEY")
    elif not anthropic_base_url():
        require("anthropic", "api_key", env="ANTHROPIC_API_KEY")


def get_provider(model_name: str) -> LLMProvider:
    """Factory function to get the appropriate LLMProvider for a given model."""
    name_lower = model_name.lower()
    if name_lower.startswith("claude-"):
        return AnthropicProvider()
    elif name_lower.startswith("gemini-"):
        return GeminiProvider()
    else:
        # Extensible default: fall back to the Anthropic client shape.
        #
        # Only warn when talking to the real Anthropic API — there an
        # unrecognised prefix is almost certainly a typo. With a custom
        # base_url configured, a name like `qwen3-30b` or `llama-3.3-70b` is
        # the expected case, and warning on every single screening call
        # would bury the run's real output.
        if not anthropic_base_url():
            print(
                f"Warning: Unknown model prefix for '{model_name}'. "
                f"Defaulting to Anthropic.",
                file=sys.stderr,
            )
        return AnthropicProvider()
