"""Recognise an API's *daily* quota being spent, from response headers.

Stdlib-only, so both `http_client` (the transport retry) and
`fetchers.base` (the per-response verdict) can use it without either
importing the other.

A 429 means two different things. Per-second throttling clears within
the backoff `http_client` already applies. An exhausted daily quota does
not: live 2026-09-23, Web of Science Expanded answered 25 of 39 lookups
with 429, carrying `x-req-reqperday-remaining: 0` and no `Retry-After`,
because two sessions shared one key. Each of those items then sat
through five retries (~31 s) that could not succeed. Worse, a quota
never recovers mid-run, so every later item repeats the wait.
"""

from __future__ import annotations

from collections.abc import Mapping

#: Headers that count requests left *today*. Compared case-insensitively.
#: Web of Science sends the first; the others are the common spellings
#: of the same idea, listed so a new source is covered on sight.
DAILY_REMAINING_HEADERS: tuple[str, ...] = (
    "x-req-reqperday-remaining",
    "x-ratelimit-remaining-day",
    "x-ratelimit-remaining-daily",
)


def daily_quota_exhausted(headers: Mapping[str, str] | None) -> bool:
    """True when a response says no requests are left today."""
    if not headers:
        return False
    lowered = {str(k).lower(): v for k, v in headers.items()}
    for name in DAILY_REMAINING_HEADERS:
        value = lowered.get(name)
        if value is None:
            continue
        try:
            if int(str(value).strip()) <= 0:
                return True
        except ValueError:
            continue
    return False
