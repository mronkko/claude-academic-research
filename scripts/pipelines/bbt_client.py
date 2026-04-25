"""Standard-library-only client for Better BibTeX's local endpoints.

BBT (the Zotero plugin) serves two HTTP endpoints on the user's
machine when Zotero desktop is running:

- `http://127.0.0.1:23119/better-bibtex/json-rpc` — JSON-RPC 2.0
  for citation-key lookups, item exports, group enumeration, etc.
- `http://127.0.0.1:23119/better-bibtex/library/<library_id>/library.bibtex`
  — full BibTeX export of a library.

This module is pure stdlib so light-weight uv-run scripts (notably
`generate_bib.py`, which declares `dependencies = []`) can talk to BBT
without pulling in the pyzotero stack. `zotero_io.py` re-exports these
on `ZoteroClient` for the heavier consumers.

Direct urllib / curl against `localhost:23119/better-bibtex/...`
from anywhere outside this module and `zotero_io.py` is a defect —
see the IRON RULE in `skills/zotero-operations/SKILL.md` and the CI
guard at `tests/unit/test_no_direct_localhost_zotero.py`.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

BBT_BASE = "http://127.0.0.1:23119/better-bibtex"

# Translator name for full-text BibLaTeX export. BBT also supports
# "Better BibTeX" (BibTeX-classic). Callers can override per-call.
DEFAULT_BIBLATEX_TRANSLATOR = "Better BibLaTeX"


class BBTUnreachableError(RuntimeError):
    """BBT's local endpoint did not respond — Zotero or BBT plugin offline."""


def bbt_json_rpc(method: str, params: dict | None = None, *, timeout: int = 30) -> dict:
    """Call a Better BibTeX JSON-RPC method.

    Returns the decoded response body (a dict with `result` and/or
    `error` keys per JSON-RPC 2.0). Raises BBTUnreachableError if BBT
    is not running.
    """
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params or {},
        "id": 1,
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{BBT_BASE}/json-rpc",
        data=payload,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.URLError as exc:
        raise BBTUnreachableError(
            f"BBT JSON-RPC unreachable at {BBT_BASE}/json-rpc — "
            f"is Zotero running with Better BibTeX installed? ({exc})"
        ) from exc


def get_bibtex_export(library_id: int | str, *, timeout: int = 60) -> str:
    """Fetch a Zotero library's full BibTeX export from BBT.

    `library_id` is BBT's numeric library identifier — `1` for the
    user's personal library, or the group ID for a group library.

    Returns the BibTeX as a single string. Raises BBTUnreachableError
    on transport failure.
    """
    url = f"{BBT_BASE}/library/{library_id}/library.bibtex"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise BBTUnreachableError(
            f"BBT library export unreachable at {url} — is Zotero "
            f"running with Better BibTeX installed? ({exc})"
        ) from exc


def get_group_library_ids() -> list[int]:
    """Enumerate Zotero group library IDs via BBT's `user.groups`.

    Returns an empty list on any error (BBT offline, no groups,
    malformed response). Used by `generate_bib.py` to fan a citation
    key search out across every group library the user can see.
    """
    try:
        body = bbt_json_rpc("user.groups", {})
    except BBTUnreachableError:
        return []
    if "error" in body or "result" not in body:
        return []
    groups = body["result"]
    if isinstance(groups, list):
        return [g["id"] for g in groups if isinstance(g, dict) and "id" in g]
    return []
