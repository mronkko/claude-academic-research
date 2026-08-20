"""Shared HTTP client for pipeline source modules.

`build_session()` returns a `requests.Session` configured with:
  - A `urllib3.Retry` transport that retries on 429 / 5xx with
    exponential backoff, honouring `Retry-After` headers.
  - A sane default User-Agent, including an optional Crossref mailto so
    the plugin lands in Crossref's polite pool.
  - A shared cookie jar — PMC's proof-of-work flow needs cookies to
    persist between the challenge and the download, which `requests`
    handles for free.

`get_json` / `get_bytes` are thin convenience wrappers with a
`tenacity`-based application-level retry on top of the transport-level
retry (so a connection reset on attempt 1 doesn't bleed its full backoff
into the caller).

Source modules accept a `requests.Session` in their constructor — no
module-level globals.
"""

from __future__ import annotations

import sys
from urllib.parse import urlsplit

import requests
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = 30

#: Don't narrate a wait shorter than this. The first 429 retry is a
#: one-second blip nobody needs told about; a thirty-second one looks
#: exactly like a hung process from the outside.
ANNOUNCE_BACKOFF_OVER = 3.0


class VerboseRetry(Retry):
    """`urllib3.Retry` that says out loud when it is about to wait.

    The retry policy sleeps inside the transport adapter, below every
    print statement the pipeline owns. From the operator's seat a
    rate-limited Semantic Scholar search is therefore indistinguishable
    from a hang: the searcher printed `Semantic Scholar block_a: ` and
    then nothing, for minutes. That silence is what sent one agent into
    the searcher's source to work out whether it was stuck — so the fix
    belongs here, where the waiting actually happens, and not in one
    searcher's error path.

    `Retry.new()` rebuilds the object on every attempt via
    `type(self)(**params)`, which preserves the subclass, so overriding
    `sleep()` alone is enough — no extra state to carry across.
    """

    def sleep(self, response=None) -> None:  # noqa: ANN001 — urllib3's signature
        delay = None
        if self.respect_retry_after_header and response is not None:
            delay = self.get_retry_after(response)
        if delay is None:
            delay = self.get_backoff_time()
        if delay and delay > ANNOUNCE_BACKOFF_OVER:
            print(self._wait_message(delay), file=sys.stderr, flush=True)
        super().sleep(response)

    def _wait_message(self, delay: float) -> str:
        """`  HTTP 429 from api.semanticscholar.org — waiting 30s …`.

        `history` is appended by `increment()` immediately before
        `sleep()` is called on the incremented object, so its last entry
        describes the request that is being retried.
        """
        host = "the server"
        status = ""
        if self.history:
            last = self.history[-1]
            host = urlsplit(getattr(last, "url", "") or "").netloc or host
            status = f"HTTP {last.status} " if getattr(last, "status", None) else ""
        attempts_left = self.total if isinstance(self.total, int) else 0
        return (
            f"  {status}from {host} — waiting {delay:.0f}s before retrying "
            f"({attempts_left} attempt(s) left). Long waits are normal on "
            f"free API tiers; the run has not stalled."
        )


def build_session(mailto: str | None = None) -> requests.Session:
    """requests.Session wired for academic APIs.

    mailto — if provided, goes into the User-Agent. Crossref uses this to
    tier requests into the polite pool. Safe to omit.
    """
    session = requests.Session()

    retry_policy = VerboseRetry(
        total=5,
        backoff_factor=1.0,          # 1s, 2s, 4s, 8s, 16s
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=frozenset(["GET", "POST", "HEAD", "PUT", "PATCH"]),
        respect_retry_after_header=True,
        raise_on_status=False,
    )
    adapter = requests.adapters.HTTPAdapter(max_retries=retry_policy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)

    ua = "claude-academic-research/1.0 (https://github.com/mronkko/claude-academic-research)"
    if mailto:
        ua += f"; mailto:{mailto}"
    session.headers.update({"User-Agent": ua, "Accept": "*/*"})

    return session


_RETRYABLE = (requests.Timeout, requests.ConnectionError)


def _announce_network_retry(state) -> None:  # noqa: ANN001 — tenacity's RetryCallState
    """Same reasoning as `VerboseRetry`, one layer up.

    The transport-level policy narrates status-code waits; this one
    narrates the connection-level ones, which can reach 30 s and are
    just as silent.
    """
    delay = getattr(state.next_action, "sleep", 0) or 0
    if delay <= ANNOUNCE_BACKOFF_OVER:
        return
    exc = state.outcome.exception() if state.outcome else None
    print(
        f"  network error ({type(exc).__name__ if exc else 'unknown'}) — "
        f"retrying in {delay:.0f}s (attempt {state.attempt_number + 1} of 3).",
        file=sys.stderr, flush=True,
    )


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=30),
    retry=retry_if_exception_type(_RETRYABLE),
    before_sleep=_announce_network_retry,
    reraise=True,
)
def get_json(
    session: requests.Session,
    url: str,
    *,
    headers: dict | None = None,
    params: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict | None:
    """GET a JSON resource. Returns parsed dict or None on 4xx client error.

    Raises on network errors (after tenacity retries) and on 5xx (which
    urllib3.Retry should have already exhausted, so reaching this means
    the server stayed down).
    """
    response = session.get(url, headers=headers, params=params, timeout=timeout)
    if 400 <= response.status_code < 500:
        return None
    response.raise_for_status()
    try:
        return response.json()
    except ValueError:
        return None


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=30),
    retry=retry_if_exception_type(_RETRYABLE),
    before_sleep=_announce_network_retry,
    reraise=True,
)
def get_bytes(
    session: requests.Session,
    url: str,
    *,
    headers: dict | None = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> tuple[bytes, str] | None:
    """GET raw bytes. Returns (body, content_type) or None on 4xx."""
    response = session.get(url, headers=headers, timeout=timeout, stream=False)
    if 400 <= response.status_code < 500:
        return None
    response.raise_for_status()
    return response.content, response.headers.get("Content-Type", "")
