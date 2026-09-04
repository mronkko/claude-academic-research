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


@pytest.mark.xfail(
    strict=True,
    reason="BBT 9.0.63 cannot resolve a library id for whole-library "
           "export; see get_bibtex_export's docstring. strict=True so an "
           "XPASS fails the run — that is how we learn BBT fixed it.",
)
def test_get_bibtex_export_personal_library_returns_string() -> None:
    """BBT serves the personal library at library_id=1. The export may
    be empty (fresh install) but must be a string, not raise.

    Currently xfail. BBT's `LibraryHandler` matches the URL and captures
    the id, then its own library lookup fails and it answers 404. Every
    id form was tried against a live install — with and without the
    leading slash, a local library id and the matching cloud group id —
    so there is no URL correction available on our side.

    Deliberately still exercising library_id=1 rather than a group: the
    point is the contract, and a passing run would mean BBT changed.
    """
    _bbt_or_skip()
    out = get_bibtex_export(library_id=1)
    assert isinstance(out, str)


# ---------------------------------------------------------------------------
# item.citationkey — regression pin for the `keys` vs `item_keys` defect.
#
# BBT validates named JSON-RPC parameters against the handler signature
# (`async citationkey(item_keys)`). The wrong name comes back as
# `-32602 unsupported argument`, and because that envelope carries no
# `result`, `ZoteroClient.get_bbt_keys` degraded to returning `{}` —
# making `populate_missing_bbt_keys` report every item as unkeyed. No
# mocked test can catch that; only a real BBT can.
# ---------------------------------------------------------------------------


def test_item_citationkey_accepts_the_item_keys_param_name() -> None:
    """Transport-level pin, no credentials and no real item needed: BBT
    must accept `item_keys` and echo the requested key back. A synthetic
    key resolves to `None` (no such item), which is a *result*, not an
    error — that distinction is exactly what the defect erased."""
    _bbt_or_skip()
    body = bbt_json_rpc("item.citationkey", {"item_keys": ["ZZZZZZZZ"]})
    assert "error" not in body, (
        f"BBT rejected the `item_keys` parameter: {body.get('error')!r}. "
        f"If the handler signature changed upstream, ZoteroClient."
        f"get_bbt_keys must change with it — it fails silently, not loudly."
    )
    assert isinstance(body.get("result"), dict)
    assert "ZZZZZZZZ" in body["result"]


def test_get_bbt_keys_resolves_real_items_from_the_local_library() -> None:
    """End-to-end through `ZoteroClient.get_bbt_keys` against real items.

    Reads item keys from Zotero Desktop's local API (`users/0`), which
    needs no cloud credentials — the API key is never checked locally —
    then asks BBT for their citation keys. Skips on an empty library.
    """
    _bbt_or_skip()
    import zotero_io

    zot = zotero_io.ZoteroClient.for_user_library(
        "0", api_key="local-no-auth-required", prefer_local=True,
    )
    try:
        top = zot.top_items()
    except Exception as exc:  # noqa: BLE001 — Desktop offline / local API disabled
        pytest.skip(f"Zotero local API unreachable: {exc}")

    item_keys = [it["key"] for it in top[:10] if it.get("key")]
    if not item_keys:
        pytest.skip("personal library is empty — nothing to resolve")

    resolved = zot.get_bbt_keys(item_keys)
    assert resolved, (
        f"BBT returned no citation keys for any of {len(item_keys)} real "
        f"items. Either BBT has not generated keys for this library, or "
        f"the item.citationkey call is malformed again."
    )
    assert set(resolved) <= set(item_keys)
    assert all(isinstance(v, str) and v for v in resolved.values())


def test_zotero_exposes_a_native_citation_key() -> None:
    """The premise of `_citation_key` and `get_bbt_keys`, asserted.

    BBT stopped writing `Citation Key:` into `extra`, and Zotero carries
    the key natively instead. Reading only `extra` silently produced an
    empty `bibtex_key` for every exported row — 140 of 140 in one live
    project. If this field ever goes away, that failure returns silently,
    so it is worth a live assertion rather than an assumption.
    """
    import zotero_io

    from tests.live.conftest import require_config

    api_key = require_config("zotero", "api_key", env="ZOTERO_API_KEY")
    group = zotero_io.find_group_by_name("academic-research-e2e",
                                         api_key=api_key, user_id=None)
    if group is None:
        pytest.skip("academic-research-e2e group not available")
    zc = zotero_io.ZoteroClient(api_key=api_key, group_id=str(group["id"]))
    items = [i for i in zc.cloud.top(limit=25)
             if i.get("data", {}).get("citationKey")]
    if not items:
        pytest.skip("no BBT-keyed items in the e2e group")
    data = items[0]["data"]
    assert isinstance(data["citationKey"], str) and data["citationKey"]
    # The whole point: the key is reachable without touching `extra`.
    assert zc.get_bbt_keys([items[0]["key"]]).get(items[0]["key"]) == \
        data["citationKey"]
