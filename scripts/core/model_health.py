"""Can this project actually reach the model it is about to use?

Written after a real run lost an evening to a misdiagnosis. A full-text
classification pass over 244 items produced no output for ~22 minutes.
The agent driving it concluded "network hang" and started hardening the
script with a SIGALRM timeout backstop. The truth was in the response
body all along:

    429: You exceeded your current quota, please check your plan and
    billing details.

Not a hang. Not a network fault. Not a bad key. An exhausted quota — and
the reason it *looked* like a hang is that the per-item retry ladder
(4 attempts, 10/20/40/60s) spent 131 seconds per item failing, while
progress printed only every 10 items. Backoff turned a fast, legible
failure into a slow, silent one.

Two lessons are encoded here.

**A 429 is two different failures wearing one status code.** A burst
rate-limit clears if you wait; an exhausted quota does not clear until
the user changes their plan or the billing period rolls over. Retrying
the second is pure waste, and worse, it *hides* the cause behind minutes
of delay. `QUOTA_EXHAUSTED` and `RATE_LIMITED` are therefore separate
statuses with opposite advice, distinguished by the wording providers
put in the body.

**The check belongs before the run, not inside it.** One tiny request
answers "is this key live, does this model exist, is there quota left,
is the endpoint reachable" in under a second — before the user walks
away from a run that was never going to work.

Stdlib-only and hand-rolled, like `model_discovery` next door: this is
called from `scripts/setup/`, which runs under a bare `python3` with no
venv (see CLAUDE.md). It also means the preflight works before any
pipeline dependency is installed.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum

from core import providers
from core.providers import ProviderSpec

#: Deliberately short. This is a liveness probe, not a generation: one
#: token back is proof enough, and a long timeout here would reproduce
#: the very "silent wait" this module exists to prevent.
PROBE_TIMEOUT_S = 20
PROBE_MAX_TOKENS = 4
PROBE_PROMPT = "Reply with the single word: OK"


class ConnectionStatus(StrEnum):
    """Outcome of a probe. The value is what gets printed and logged."""

    OK = "OK"
    AUTH_FAILED = "AUTH_FAILED"
    QUOTA_EXHAUSTED = "QUOTA_EXHAUSTED"
    RATE_LIMITED = "RATE_LIMITED"
    MODEL_NOT_FOUND = "MODEL_NOT_FOUND"
    UNREACHABLE = "UNREACHABLE"
    BAD_REQUEST = "BAD_REQUEST"
    UNKNOWN = "UNKNOWN"


#: Statuses where trying again, unchanged, can succeed. Everything else
#: needs the user to change something first, and a pipeline that retries
#: them is burning wall-clock to arrive at the same answer — which is
#: exactly how the 22-minute "hang" happened.
RETRYABLE: frozenset[str] = frozenset({
    ConnectionStatus.RATE_LIMITED.value,
    ConnectionStatus.UNREACHABLE.value,
})

REMEDY: dict[str, str] = {
    ConnectionStatus.OK.value: "",
    ConnectionStatus.AUTH_FAILED.value: (
        "The API key was rejected. Rotate it with `/setup` — keys are "
        "entered there so they never enter the conversation."
    ),
    ConnectionStatus.QUOTA_EXHAUSTED.value: (
        "The key is valid but its quota is spent. Retrying will not help "
        "until the plan, billing, or quota period changes. Check the "
        "provider's billing page, or switch provider with "
        "`set_llm_provider.py --provider <name>`."
    ),
    ConnectionStatus.RATE_LIMITED.value: (
        "Throttled right now, but the quota itself is intact. Re-run, or "
        "lower --workers to reduce the request rate."
    ),
    ConnectionStatus.MODEL_NOT_FOUND.value: (
        "The provider does not serve this model ID. Run "
        "`resolve_models.py --list` to see what it does serve, then pin "
        "one with `--stage <stage> --model <id>`."
    ),
    ConnectionStatus.UNREACHABLE.value: (
        "No response from the endpoint. For a local provider (Ollama, LM "
        "Studio), confirm the server is running and the base URL is right."
    ),
    ConnectionStatus.BAD_REQUEST.value: (
        "The provider rejected the request shape. This is a plugin bug "
        "rather than a configuration problem — please report it."
    ),
    ConnectionStatus.UNKNOWN.value: (
        "Unrecognised failure. The provider's own message is above; it is "
        "the most reliable guide."
    ),
}

# Providers phrase exhaustion differently, but all of them say one of
# these words and none of them says any of them for a burst limit.
# Matched against the response body, which is where the distinction
# lives — the status code alone cannot carry it.
_QUOTA_WORDING = re.compile(
    r"quota|billing|plan and billing|insufficient[_ ]quota|"
    r"credit|exceeded your current|out of (?:credits|funds)|payment",
    re.I,
)

_MODEL_WORDING = re.compile(
    r"model.{0,30}(?:not found|does not exist|unknown|invalid|unsupported)|"
    r"(?:not found|unknown|invalid|unsupported).{0,30}model|no such model",
    re.I,
)

_AUTH_WORDING = re.compile(
    r"api[ _-]?key|unauthor|authentication|invalid[_ ]token|credential|"
    r"permission denied|forbidden",
    re.I,
)


@dataclass(frozen=True)
class ConnectionResult:
    """What one probe found. Printable as-is; that is the whole point."""

    status: ConnectionStatus
    provider: str
    model: str
    detail: str = ""
    http_status: int | None = None

    @property
    def ok(self) -> bool:
        return self.status is ConnectionStatus.OK

    @property
    def retryable(self) -> bool:
        return self.status.value in RETRYABLE

    @property
    def remedy(self) -> str:
        return REMEDY.get(self.status.value, "")

    def format(self) -> str:
        """A block an agent can act on and a user can read.

        States the verdict, the provider's own words, and the one next
        step — in that order, because an agent that reads only the first
        line still gets the actionable part.
        """
        if self.ok:
            return (
                f"Model connection OK — {self.provider} / {self.model} "
                f"answered a test request."
            )
        lines = [
            f"MODEL CONNECTION FAILED — {self.status.value}",
            f"  provider : {self.provider}",
            f"  model    : {self.model}",
        ]
        if self.http_status is not None:
            lines.append(f"  http     : {self.http_status}")
        if self.detail:
            lines.append(f"  provider says: {self.detail}")
        if self.remedy:
            lines.append(f"  what to do: {self.remedy}")
        if not self.retryable:
            lines.append(
                "  NOTE: retrying will not fix this. Do not start a "
                "screening or coding run until it is resolved."
            )
        return "\n".join(lines)


def classify(http_status: int | None, body: str = "") -> ConnectionStatus:
    """Map an HTTP status plus response body onto a status. Pure.

    The body matters as much as the code. A 429 carrying the word
    "quota" is a spent allowance (no amount of waiting inside one run
    fixes it); a bare 429 is a burst limit (waiting does). Getting this
    one distinction right is most of the value of this module.
    """
    text = body or ""
    if http_status is None:
        return ConnectionStatus.UNREACHABLE
    if http_status < 400:
        return ConnectionStatus.OK
    if http_status in (401, 403):
        return ConnectionStatus.AUTH_FAILED
    if http_status == 402:
        return ConnectionStatus.QUOTA_EXHAUSTED
    if http_status == 429:
        return (
            ConnectionStatus.QUOTA_EXHAUSTED if _QUOTA_WORDING.search(text)
            else ConnectionStatus.RATE_LIMITED
        )
    if http_status == 404:
        return ConnectionStatus.MODEL_NOT_FOUND
    if http_status == 400:
        # Providers overload 400. Google returns it for an unknown model,
        # OpenAI for a spent prepaid balance (`insufficient_quota`).
        if _MODEL_WORDING.search(text):
            return ConnectionStatus.MODEL_NOT_FOUND
        if _QUOTA_WORDING.search(text):
            return ConnectionStatus.QUOTA_EXHAUSTED
        if _AUTH_WORDING.search(text):
            return ConnectionStatus.AUTH_FAILED
        return ConnectionStatus.BAD_REQUEST
    if http_status >= 500:
        return ConnectionStatus.UNREACHABLE
    return ConnectionStatus.UNKNOWN


def classify_exception(exc: BaseException) -> tuple[ConnectionStatus, str, int | None]:
    """Classify an SDK exception. Returns `(status, detail, http_status)`.

    The pipelines hold real SDK clients (anthropic / google-genai /
    openai) rather than the stdlib probe, and every one of those raises
    its own exception type. Rather than import three SDKs to catch three
    hierarchies, read the two things they all expose: a status code
    somewhere on the object, and a string form that quotes the server.
    """
    status_code = None
    for attr in ("status_code", "code", "http_status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            status_code = value
            break
    if status_code is None:
        response = getattr(exc, "response", None)
        value = getattr(response, "status_code", None)
        if isinstance(value, int):
            status_code = value

    detail = " ".join(str(exc).split())
    if status_code is None:
        # Fall back to the message: SDKs stringify as "Error code: 429 - …".
        match = re.search(r"\b(4\d\d|5\d\d)\b", detail)
        if match:
            status_code = int(match.group(1))

    if status_code is None:
        lowered = detail.lower()
        if "connect" in lowered or "timeout" in lowered or "timed out" in lowered:
            return ConnectionStatus.UNREACHABLE, detail, None
        return ConnectionStatus.UNKNOWN, detail, None
    return classify(status_code, detail), detail, status_code


# ---------------------------------------------------------------------------
# The probe
# ---------------------------------------------------------------------------


def _chat_request(
    spec: ProviderSpec, base: str, model: str, api_key: str,
) -> tuple[str, dict, dict]:
    """`(url, headers, body)` for a minimal completion on this transport."""
    if spec.transport == "anthropic":
        return (
            f"{base}/v1/messages",
            {
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            {
                "model": model,
                "max_tokens": PROBE_MAX_TOKENS,
                "messages": [{"role": "user", "content": PROBE_PROMPT}],
            },
        )
    if spec.transport == "google":
        clean = model.removeprefix("models/")
        return (
            f"{base}/v1beta/models/{clean}:generateContent?key={api_key}",
            {"content-type": "application/json"},
            {
                "contents": [{"parts": [{"text": PROBE_PROMPT}]}],
                "generationConfig": {"maxOutputTokens": PROBE_MAX_TOKENS},
            },
        )
    headers = {"content-type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    return (
        f"{base}/v1/chat/completions",
        headers,
        {
            "model": model,
            "max_tokens": PROBE_MAX_TOKENS,
            "messages": [{"role": "user", "content": PROBE_PROMPT}],
        },
    )


def _extract_message(body: str) -> str:
    """The provider's own error sentence, if the body is JSON.

    Kept because the raw sentence is consistently more useful than
    anything this module could paraphrase — "check your plan and billing
    details" told the whole story in the incident, and no generic
    rendering of "429" would have.
    """
    try:
        data = json.loads(body)
    except (ValueError, TypeError):
        return " ".join((body or "").split())[:400]
    error = data.get("error") if isinstance(data, dict) else None
    if isinstance(error, dict):
        message = error.get("message") or error.get("type") or ""
    elif isinstance(error, str):
        message = error
    else:
        message = ""
    if not message and isinstance(data, dict):
        message = str(data.get("message") or "")
    return " ".join(str(message).split())[:400] or " ".join(body.split())[:400]


def check_connection(
    spec: ProviderSpec,
    model: str,
    api_key: str = "",
    base_url: str = "",
) -> ConnectionResult:
    """Send one tiny request and report what happened.

    No retries, on purpose. This runs *before* a batch to answer "will
    this work at all", and a retry ladder here would reintroduce the
    slow-silent-failure shape the module exists to prevent. The pipeline
    keeps its own retries for genuinely transient faults mid-run.
    """
    base = providers.base_url_for(spec, base_url)
    if not model:
        return ConnectionResult(
            status=ConnectionStatus.MODEL_NOT_FOUND,
            provider=spec.name,
            model="",
            detail="No model is pinned for this stage.",
        )

    url, headers, payload = _chat_request(spec, base, model, api_key)
    request = urllib.request.Request(  # noqa: S310 — provider URLs from the registry
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=PROBE_TIMEOUT_S) as resp:  # noqa: S310
            resp.read()
            return ConnectionResult(
                status=ConnectionStatus.OK,
                provider=spec.name,
                model=model,
                http_status=getattr(resp, "status", 200),
            )
    except urllib.error.HTTPError as e:
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001 — diagnostics must not raise
            body = ""
        return ConnectionResult(
            status=classify(e.code, body),
            provider=spec.name,
            model=model,
            detail=_extract_message(body),
            http_status=e.code,
        )
    except urllib.error.URLError as e:
        return ConnectionResult(
            status=ConnectionStatus.UNREACHABLE,
            provider=spec.name,
            model=model,
            detail=f"{base}: {e.reason}",
        )
    except Exception as e:  # noqa: BLE001 — a probe must never sink its caller
        return ConnectionResult(
            status=ConnectionStatus.UNKNOWN,
            provider=spec.name,
            model=model,
            detail=" ".join(str(e).split())[:400],
        )
