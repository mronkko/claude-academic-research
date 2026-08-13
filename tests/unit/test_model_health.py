"""The model-connection probe, and the one distinction it exists to make.

Reconstructed from a real incident. A full-text classification run over
244 papers produced no output for ~22 minutes and was diagnosed as a
network hang; the agent started adding a SIGALRM timeout backstop. The
actual response body said:

    429: You exceeded your current quota, please check your plan and
    billing details.

The retry ladder (4 attempts, 10/20/40/60s) turned a one-second
diagnosis into a 131-second-per-item silence. So these tests pin two
behaviours: a 429 that mentions quota is NOT retryable and must not be
confused with a burst limit, and every non-OK result explains itself
well enough to act on.
"""

from __future__ import annotations

import json

import pytest
from core import providers
from core.model_health import (
    ConnectionResult,
    ConnectionStatus,
    check_connection,
    classify,
    classify_exception,
)

# The exact body from the incident, trimmed.
GEMINI_QUOTA_BODY = json.dumps({
    "error": {
        "code": 429,
        "message": (
            "You exceeded your current quota, please check your plan and "
            "billing details. For more information on this error, head to: "
            "https://ai.google.dev/gemini-api/docs/rate-limits."
        ),
        "status": "RESOURCE_EXHAUSTED",
    }
})


# ---------------------------------------------------------------------------
# The 429 fork
# ---------------------------------------------------------------------------


def test_the_incident_body_classifies_as_quota_not_rate_limit() -> None:
    """The regression this module exists for.

    Same status code, opposite advice: waiting clears a burst limit and
    does nothing at all for a spent quota.
    """
    assert classify(429, GEMINI_QUOTA_BODY) is ConnectionStatus.QUOTA_EXHAUSTED


def test_a_bare_429_is_a_rate_limit() -> None:
    assert classify(429, "Too Many Requests") is ConnectionStatus.RATE_LIMITED


def test_quota_exhaustion_is_not_retryable_and_says_so() -> None:
    result = ConnectionResult(
        status=ConnectionStatus.QUOTA_EXHAUSTED,
        provider="google", model="gemini-2.5-flash",
    )
    assert not result.retryable
    assert "retrying will not fix this" in result.format().lower()


def test_a_rate_limit_is_retryable() -> None:
    result = ConnectionResult(
        status=ConnectionStatus.RATE_LIMITED, provider="google", model="m",
    )
    assert result.retryable
    assert "retrying will not fix this" not in result.format().lower()


@pytest.mark.parametrize("body", [
    '{"error": {"message": "insufficient_quota"}}',
    '{"error": {"message": "You have exceeded your current quota"}}',
    '{"error": {"message": "Your credit balance is too low"}}',
    '{"error": {"message": "check your plan and billing details"}}',
])
def test_provider_quota_wordings_all_land_on_quota_exhausted(body) -> None:
    """Providers phrase it differently; none of them phrases a burst
    limit this way."""
    assert classify(429, body) is ConnectionStatus.QUOTA_EXHAUSTED


# ---------------------------------------------------------------------------
# The other statuses
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", [401, 403])
def test_auth_codes_are_auth_failures(code) -> None:
    assert classify(code, "") is ConnectionStatus.AUTH_FAILED


def test_402_is_a_spent_balance() -> None:
    assert classify(402, "") is ConnectionStatus.QUOTA_EXHAUSTED


def test_404_is_a_missing_model() -> None:
    assert classify(404, "") is ConnectionStatus.MODEL_NOT_FOUND


def test_400_naming_a_model_is_a_missing_model() -> None:
    """Google returns 400, not 404, for a model ID it does not serve —
    which is exactly what a typo'd pin produces."""
    body = '{"error": {"message": "models/gemma-4-31b-it is not found"}}'
    assert classify(400, body) is ConnectionStatus.MODEL_NOT_FOUND


def test_400_with_insufficient_quota_is_quota() -> None:
    body = '{"error": {"type": "insufficient_quota"}}'
    assert classify(400, body) is ConnectionStatus.QUOTA_EXHAUSTED


def test_a_plain_400_is_a_bad_request() -> None:
    assert classify(400, "malformed body") is ConnectionStatus.BAD_REQUEST


def test_5xx_is_unreachable() -> None:
    assert classify(503, "") is ConnectionStatus.UNREACHABLE


def test_no_status_at_all_is_unreachable() -> None:
    assert classify(None, "") is ConnectionStatus.UNREACHABLE


def test_success_is_ok() -> None:
    assert classify(200, "") is ConnectionStatus.OK


# ---------------------------------------------------------------------------
# SDK exceptions — the pipelines hold real clients, not the stdlib probe
# ---------------------------------------------------------------------------


class _SdkError(Exception):
    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


def test_exception_with_status_code_attribute() -> None:
    exc = _SdkError("quota exceeded, check your plan", status_code=429)
    status, detail, code = classify_exception(exc)
    assert status is ConnectionStatus.QUOTA_EXHAUSTED
    assert code == 429
    assert "quota" in detail


def test_exception_with_status_only_in_the_message() -> None:
    """Several SDKs stringify as `Error code: 429 - {...}` with no
    structured attribute to read."""
    exc = Exception(
        "Error code: 429 - {'error': {'message': 'insufficient_quota'}}"
    )
    status, _detail, code = classify_exception(exc)
    assert code == 429
    assert status is ConnectionStatus.QUOTA_EXHAUSTED


def test_a_connection_error_reads_as_unreachable() -> None:
    status, _d, code = classify_exception(
        Exception("Connection refused: localhost:1234")
    )
    assert status is ConnectionStatus.UNREACHABLE
    assert code is None


def test_an_unrecognisable_exception_is_unknown_not_ok() -> None:
    """Fail closed: an unclassifiable error must never read as healthy."""
    status, _d, _c = classify_exception(Exception("something odd"))
    assert status is ConnectionStatus.UNKNOWN
    assert status is not ConnectionStatus.OK


# ---------------------------------------------------------------------------
# Output the agent has to act on
# ---------------------------------------------------------------------------


def test_failure_output_names_status_provider_model_and_remedy() -> None:
    result = ConnectionResult(
        status=ConnectionStatus.AUTH_FAILED,
        provider="google", model="gemini-2.5-flash",
        detail="API key not valid", http_status=401,
    )
    text = result.format()
    assert "AUTH_FAILED" in text
    assert "google" in text
    assert "gemini-2.5-flash" in text
    assert "API key not valid" in text          # the provider's own words
    assert "/setup" in text                     # the next step


def test_every_status_has_a_remedy_except_ok() -> None:
    for status in ConnectionStatus:
        result = ConnectionResult(status=status, provider="p", model="m")
        if status is ConnectionStatus.OK:
            assert result.ok
        else:
            assert result.remedy, f"{status} has no remedy text"


def test_ok_output_is_a_single_reassuring_line() -> None:
    result = ConnectionResult(
        status=ConnectionStatus.OK, provider="anthropic", model="claude-x",
    )
    assert result.ok
    assert "\n" not in result.format()


# ---------------------------------------------------------------------------
# The probe itself
# ---------------------------------------------------------------------------


def test_no_pinned_model_is_reported_rather_than_probed() -> None:
    """An empty pin must not become a request with an empty model ID."""
    spec = providers.require("anthropic")
    result = check_connection(spec, "", api_key="k")
    assert result.status is ConnectionStatus.MODEL_NOT_FOUND
    assert "No model is pinned" in result.detail


def test_probe_never_raises_even_when_the_transport_explodes(monkeypatch) -> None:
    """A pre-flight check that can itself crash the run is worse than
    no pre-flight check."""
    import core.model_health as mh

    def boom(*_a, **_kw):
        raise ValueError("kaboom")

    monkeypatch.setattr(mh.urllib.request, "urlopen", boom)
    result = check_connection(providers.require("openai"), "gpt-x", api_key="k")
    assert result.status is ConnectionStatus.UNKNOWN
    assert "kaboom" in result.detail


def test_probe_targets_the_right_endpoint_per_transport(monkeypatch) -> None:
    """Each transport has its own chat URL; a wrong one would make every
    check fail with a misleading 404."""
    import core.model_health as mh

    seen: list[str] = []

    def capture(request, timeout=None):
        seen.append(request.full_url)
        raise ValueError("stop here")

    monkeypatch.setattr(mh.urllib.request, "urlopen", capture)

    check_connection(providers.require("anthropic"), "claude-x", api_key="k")
    check_connection(providers.require("google"), "gemini-x", api_key="k")
    check_connection(providers.require("openai"), "gpt-x", api_key="k")

    assert seen[0].endswith("/v1/messages")
    assert "/v1beta/models/gemini-x:generateContent" in seen[1]
    assert seen[2].endswith("/v1/chat/completions")


def test_google_model_id_is_normalised_before_the_url(monkeypatch) -> None:
    """Pins are sometimes written `models/gemini-…`; the REST path already
    contains `models/`, so leaving the prefix on yields `models/models/…`."""
    import core.model_health as mh

    seen: list[str] = []

    def capture(request, timeout=None):
        seen.append(request.full_url)
        raise ValueError("stop")

    monkeypatch.setattr(mh.urllib.request, "urlopen", capture)
    check_connection(
        providers.require("google"), "models/gemini-2.5-flash", api_key="k",
    )
    assert "models/models/" not in seen[0]
