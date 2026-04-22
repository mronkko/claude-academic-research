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

import requests
import urllib3
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = 30


def build_session(mailto: str | None = None) -> requests.Session:
    """requests.Session wired for academic APIs.

    mailto — if provided, goes into the User-Agent. Crossref uses this to
    tier requests into the polite pool. Safe to omit.
    """
    session = requests.Session()

    retry_policy = Retry(
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


@retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, max=30),
    retry=retry_if_exception_type(_RETRYABLE),
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


def silence_insecure_warnings() -> None:
    """Disable urllib3's InsecureRequestWarning.

    Some academic endpoints serve valid content behind mis-configured
    TLS chains (wrong intermediate certs, etc.). Source modules that
    knowingly call `session.get(url, verify=False)` should call this
    once at import time to keep logs readable. The default `build_session`
    keeps `verify=True` — the global ssl.CERT_NONE context used by the
    legacy urllib code is intentionally NOT carried forward.
    """
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
