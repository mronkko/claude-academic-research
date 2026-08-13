"""Structural validation for downloaded PDF bytes.

Every HTTP fetcher used to accept a response as a PDF on two conditions:
`status_code == 200` and `content[:4] == b"%PDF"`. That catches a
publisher serving an HTML paywall page, and nothing else. In particular
it cannot see a **truncated** download, because the first four bytes of
half a PDF are still `%PDF`.

That gap produced a real, expensive failure. Five articles in a live
review were attached from OpenAlex with files that opened as valid PDFs
by every check the pipeline made, but would not open in Preview and
yielded zero extractable text. One of them declared its cross-reference
table at byte offset 1,744,085 while the file itself was 1,608,714 bytes
— the last ~135KB, containing the page tree, was simply missing. The
pipeline had recorded all five as clean successes, and they were
initially misdiagnosed as scanned documents needing OCR.

Two properties of that incident shape this module:

1. **Truncation is detectable without parsing.** A PDF ends with an
   `%%EOF` marker and declares its xref offset in the trailer; both
   checks are pure byte arithmetic and neither can false-positive on a
   well-formed file.
2. **Retrying the same source does not help.** The re-fetch produced
   byte-identical files — OpenAlex's stored copy was itself corrupt. So
   a rejected PDF must let the cascade fall through to the next source,
   which is why the fetchers return None rather than raising.

Deliberately dependency-free (no PyMuPDF, no poppler): this runs inside
every fetcher, and a validator that needs an optional binary would
silently no-op on the machines that need it most. The checks below are
conservative — they reject only what is provably broken, because a false
rejection discards a good PDF the user may have no other route to.
"""

from __future__ import annotations

import re

# A PDF's trailer must appear near the end of the file. 4KB is generous:
# the spec allows arbitrary trailing whitespace, and some producers pad.
_TAIL_BYTES = 4096

_STARTXREF_RE = re.compile(rb"startxref\s+(\d+)\s*(?:%%EOF)?\s*$", re.DOTALL)

# Below this, nothing is a real article PDF — matches the threshold
# `fetchers.browser.base.is_cached` already used for cache entries.
MIN_PDF_BYTES = 1000


def pdf_defect(data: bytes, *, expected_length: int | None = None) -> str | None:
    """Return a short reason why `data` is not a usable PDF, or None if it is.

    `expected_length` is the response's `Content-Length` when known; a
    mismatch is the most direct possible evidence of a short read.

    The reason string is written to the run log's `detail` column, so it
    is phrased for someone reading a CSV, not a stack trace.
    """
    if not data:
        return "empty response"
    if len(data) < MIN_PDF_BYTES:
        return f"too small to be an article PDF ({len(data)} bytes)"
    if data[:5] != b"%PDF-":
        return "not a PDF (missing %PDF- header)"

    if expected_length is not None and len(data) != expected_length:
        return (
            f"truncated download: got {len(data)} of "
            f"{expected_length} bytes"
        )

    tail = data[-_TAIL_BYTES:]
    if b"%%EOF" not in tail:
        return "truncated download: no %%EOF trailer"

    # The trailer names the byte offset of the cross-reference table. An
    # offset past the end of the file is the signature of the OpenAlex
    # incident: header and content intact, structure cut off.
    match = _STARTXREF_RE.search(tail)
    if match:
        offset = int(match.group(1))
        if offset >= len(data):
            return (
                f"truncated download: xref offset {offset} is past the "
                f"end of a {len(data)}-byte file"
            )

    return None


def is_valid_pdf(data: bytes, *, expected_length: int | None = None) -> bool:
    """Boolean form of `pdf_defect`."""
    return pdf_defect(data, expected_length=expected_length) is None


def response_defect(resp) -> str | None:
    """Validate a `requests`-style response as a PDF download.

    Wraps `pdf_defect` with the HTTP-level checks every fetcher was
    already doing, plus the `Content-Length` comparison none of them
    were, so all seven call sites can collapse to one line.
    """
    status = getattr(resp, "status_code", None)
    if status != 200:
        return f"HTTP {status}"

    declared = None
    raw = (getattr(resp, "headers", None) or {}).get("Content-Length")
    if raw is not None:
        try:
            declared = int(raw)
        except (TypeError, ValueError):
            declared = None
        else:
            # A gzipped transfer reports the compressed size, which
            # legitimately differs from the decoded body length.
            encoding = (resp.headers or {}).get("Content-Encoding", "")
            if encoding and encoding.lower() != "identity":
                declared = None

    return pdf_defect(resp.content, expected_length=declared)


def file_defect(path) -> str | None:
    """Validate a PDF already on disk. Unreadable file counts as a defect."""
    try:
        with open(path, "rb") as fh:
            return pdf_defect(fh.read())
    except OSError as exc:
        return f"unreadable file: {exc}"
