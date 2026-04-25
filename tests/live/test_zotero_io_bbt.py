"""Live tests for the Better BibTeX helpers.

Opt in with `pytest -m live`. Skips cleanly when Zotero / BBT are
not running locally — these tests need:

- Zotero desktop running with the Better BibTeX plugin installed.
- The local server enabled (Zotero → Edit → Preferences → Advanced →
  Allow other applications on this computer to communicate with Zotero).

Pass criterion: `bbt_json_rpc('user.groups')` returns either a result
or an error body without raising. The other tests probe each helper
once, smoke-test only.
"""

from __future__ import annotations

import pytest
from bbt_client import (
    BBTUnreachableError,
    bbt_json_rpc,
    get_bibtex_export,
    get_group_library_ids,
)

pytestmark = pytest.mark.live


def _bbt_or_skip() -> None:
    """Skip the test if BBT's local endpoint isn't reachable."""
    try:
        bbt_json_rpc("user.groups", {})
    except BBTUnreachableError as exc:
        pytest.skip(f"BBT unreachable — Zotero + Better BibTeX not running locally? ({exc})")


def test_bbt_json_rpc_user_groups_returns_jsonrpc_envelope() -> None:
    _bbt_or_skip()
    body = bbt_json_rpc("user.groups", {})
    # JSON-RPC 2.0 response always carries `jsonrpc: "2.0"` and either
    # `result` or `error`. We don't assert on `result` shape — that
    # depends on the user's group memberships — only on the envelope.
    assert body.get("jsonrpc") == "2.0"
    assert "result" in body or "error" in body


def test_get_group_library_ids_returns_list() -> None:
    _bbt_or_skip()
    ids = get_group_library_ids()
    # User may have zero groups — empty list is a valid pass.
    assert isinstance(ids, list)
    assert all(isinstance(i, int) for i in ids)


def test_get_bibtex_export_personal_library_returns_string() -> None:
    """BBT serves the personal library at library_id=1. The export may
    be empty (fresh install) but must be a string, not raise."""
    _bbt_or_skip()
    out = get_bibtex_export(library_id=1)
    assert isinstance(out, str)
