"""A DOI made safe to splice into a URL path.

Fetchers interpolated DOIs raw. A SICI DOI's `<>` and `#`, or a `?`,
then changed what the URL *means*: `#` starts a fragment the server never
sees, `?` starts a query, so the request asked about a truncated DOI and
its "not found" was logged as an answer about the real one.

Only characters that are illegal in a path segment or that change URL
structure are encoded. `/`, `:`, `;`, `(`, `)` and the other RFC 3986
sub-delimiters stay literal, so every ordinary DOI — and every URL this
plugin has been building — is byte-for-byte unchanged. Stdlib only.
"""

from __future__ import annotations

from urllib.parse import quote

#: RFC 3986 `pchar` minus the unreserved set (which `quote` never
#: touches), plus `/` so a DOI keeps its prefix/suffix separator.
_SAFE = "/:@!$&'()*+,;="


def doi_path(doi: str) -> str:
    """`doi` for use inside a URL path."""
    return quote(doi or "", safe=_SAFE)
