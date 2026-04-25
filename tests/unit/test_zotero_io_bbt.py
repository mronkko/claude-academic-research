"""Unit tests for Better BibTeX helpers in bbt_client + zotero_io.

The transport (`bbt_json_rpc`, `get_bibtex_export`) lives in
`bbt_client.py` so light-weight uv-run scripts (`generate_bib.py`)
can import it without dragging in pyzotero. `zotero_io.ZoteroClient`
re-exports the same surface for callers that already have a client.

Tests mock `urllib.request.urlopen` so no real Zotero / BBT instance
is required.
"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

import bbt_client
import pytest
import zotero_io


def _fake_response(payload: bytes, status: int = 200) -> MagicMock:
    """Build a context-manager mock that mimics urllib.request.urlopen()."""
    cm = MagicMock()
    cm.__enter__.return_value = MagicMock(read=BytesIO(payload).read, status=status)
    cm.__exit__.return_value = False
    return cm


# ---------------------------------------------------------------------------
# bbt_client — stdlib-only transport
# ---------------------------------------------------------------------------


def test_bbt_json_rpc_serialises_request_and_decodes_response() -> None:
    response_body = json.dumps({"jsonrpc": "2.0", "id": 1, "result": "ok"}).encode("utf-8")
    with patch("urllib.request.urlopen", return_value=_fake_response(response_body)) as mock:
        body = bbt_client.bbt_json_rpc("item.export", {"keys": ["A", "B"]})
    assert body == {"jsonrpc": "2.0", "id": 1, "result": "ok"}
    request_arg = mock.call_args.args[0]
    sent = json.loads(request_arg.data.decode("utf-8"))
    assert sent["method"] == "item.export"
    assert sent["params"] == {"keys": ["A", "B"]}
    assert sent["jsonrpc"] == "2.0"


def test_bbt_json_rpc_raises_unreachable_on_url_error() -> None:
    import urllib.error
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        with pytest.raises(bbt_client.BBTUnreachableError) as exc_info:
            bbt_client.bbt_json_rpc("user.groups")
    assert "Better BibTeX" in str(exc_info.value)


def test_get_bibtex_export_uses_library_id_in_url() -> None:
    payload = b"@article{Smith2020, ...}"
    with patch("urllib.request.urlopen", return_value=_fake_response(payload)) as mock:
        body = bbt_client.get_bibtex_export(library_id=6015547)
    assert body == "@article{Smith2020, ...}"
    url = mock.call_args.args[0]
    assert url == "http://127.0.0.1:23119/better-bibtex/library/6015547/library.bibtex"


def test_get_group_library_ids_extracts_ids_from_user_groups_response() -> None:
    response_body = json.dumps({
        "jsonrpc": "2.0", "id": 1,
        "result": [
            {"id": 6015547, "name": "Group A"},
            {"id": 7890123, "name": "Group B"},
            {"name": "missing-id-skipped"},
        ],
    }).encode("utf-8")
    with patch("urllib.request.urlopen", return_value=_fake_response(response_body)):
        ids = bbt_client.get_group_library_ids()
    assert ids == [6015547, 7890123]


def test_get_group_library_ids_returns_empty_list_when_bbt_unreachable() -> None:
    import urllib.error
    with patch(
        "urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        # Should swallow the error and return [] — used as a "best-effort"
        # call by generate_bib.py, which already prints a friendly fallback.
        assert bbt_client.get_group_library_ids() == []


# ---------------------------------------------------------------------------
# zotero_io.ZoteroClient — instance-method wrappers + higher-level ops
# ---------------------------------------------------------------------------


def _client() -> zotero_io.ZoteroClient:
    return zotero_io.ZoteroClient(api_key="fake-key", group_id="6015547")


def test_zotero_client_bbt_json_rpc_delegates_to_bbt_client() -> None:
    zc = _client()
    with patch.object(bbt_client, "bbt_json_rpc", return_value={"result": "ok"}) as mock:
        body = zc.bbt_json_rpc("item.export", {"keys": ["X"]})
    assert body == {"result": "ok"}
    mock.assert_called_once_with("item.export", {"keys": ["X"]})


def test_zotero_client_get_bibtex_export_defaults_to_group_id() -> None:
    zc = _client()
    with patch.object(bbt_client, "get_bibtex_export", return_value="bibtex") as mock:
        text = zc.get_bibtex_export()
    assert text == "bibtex"
    mock.assert_called_once_with("6015547")


def test_zotero_client_get_bibtex_export_override_library_id() -> None:
    zc = _client()
    with patch.object(bbt_client, "get_bibtex_export", return_value="user-bib") as mock:
        text = zc.get_bibtex_export(library_id=1)
    assert text == "user-bib"
    mock.assert_called_once_with(1)


def test_get_bbt_keys_returns_only_non_empty_string_values() -> None:
    """item.citationkey may include null / empty entries for items whose
    BBT key isn't yet generated. Filter them out so callers can compute
    `set(item_keys) - result.keys()` to find missing items."""
    zc = _client()
    with patch.object(zc, "bbt_json_rpc", return_value={
        "result": {
            "ABCD0001": "smith2020Foo",
            "ABCD0002": "",          # empty string — should be filtered
            "ABCD0003": None,        # null — should be filtered
            "ABCD0004": "jones2021Bar",
        },
    }):
        out = zc.get_bbt_keys(["ABCD0001", "ABCD0002", "ABCD0003", "ABCD0004"])
    assert out == {"ABCD0001": "smith2020Foo", "ABCD0004": "jones2021Bar"}


def test_get_bbt_keys_empty_input_short_circuits() -> None:
    zc = _client()
    # Should not call bbt_json_rpc at all; ensure that by patching to
    # raise if invoked.
    with patch.object(zc, "bbt_json_rpc", side_effect=AssertionError("should not be called")):
        assert zc.get_bbt_keys([]) == {}


def test_get_bbt_keys_returns_empty_when_result_not_dict() -> None:
    zc = _client()
    with patch.object(zc, "bbt_json_rpc", return_value={"error": {"message": "bad"}}):
        assert zc.get_bbt_keys(["A"]) == {}


def test_populate_missing_bbt_keys_partitions_keyed_vs_missing() -> None:
    zc = _client()
    item_keys = ["AAAA0001", "AAAA0002", "AAAA0003"]
    with patch.object(zc, "get_bbt_keys", return_value={
        "AAAA0001": "key1", "AAAA0003": "key3",
    }):
        result = zc.populate_missing_bbt_keys(item_keys=item_keys)
    assert result == {
        "keyed": ["AAAA0001", "AAAA0003"],
        "missing": ["AAAA0002"],
    }


def test_populate_missing_bbt_keys_walks_top_items_when_no_input() -> None:
    """When called without item_keys, scans every top-level item via
    `top_items()`. Mock that out so the test doesn't touch pyzotero."""
    zc = _client()
    fake_items = [{"key": "AAAA0001"}, {"key": "AAAA0002"}, {"key": ""}]
    with patch.object(zc, "top_items", return_value=fake_items), \
         patch.object(zc, "get_bbt_keys", return_value={"AAAA0001": "k1"}) as mock_keys:
        result = zc.populate_missing_bbt_keys()
    # top_items entries with empty key are skipped before lookup.
    mock_keys.assert_called_once_with(["AAAA0001", "AAAA0002"])
    assert result == {"keyed": ["AAAA0001"], "missing": ["AAAA0002"]}
