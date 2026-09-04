"""Citation keys come from Zotero's native field now, not from `extra`.

Better BibTeX used to write `Citation Key: foo2020bar` into an item's
`extra` field, and that string was the only place this package looked.
Zotero exposes a native `citationKey` on the item's `data` instead, and
BBT no longer populates `extra` — confirmed live on Zotero 10.0.1 with
BBT 9.0.63, where `data["citationKey"]` is `'meurisTaskInterdependence…'`
and `data["extra"]` is `''`.

The consequence was total and silent: every exported row carried an
empty `bibtex_key`. A downstream review found 140 of 140 included papers
exported that way, and nothing failed — the column was simply blank, so
the manuscript's `references.bib` had nothing to match against.

`get_bbt_keys` failed the same way for a different reason. It asks BBT's
`item.citationkey` with bare item keys, which BBT resolves only against
the personal library; for a group it answers `null` for every key. BBT
wants the *local* library id, which is not the cloud group id — measured
on the same install, `41:SQR3R8QW` resolves while `6658025:SQR3R8QW` and
a bare `SQR3R8QW` both return null. Since the native field is now
authoritative and needs no such mapping, that is what this reads first.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import export_coded_includes as eci
import zotero_io


def _data(**kw) -> dict:
    return {"key": "K1", "title": "T", "extra": "", **kw}


# ---------------------------------------------------------------------------
# export_coded_includes
# ---------------------------------------------------------------------------


def test_the_native_field_is_used() -> None:
    assert eci._citation_key(_data(citationKey="smithStudy2020")) == "smithStudy2020"


def test_extra_is_still_read_when_there_is_no_native_field() -> None:
    """Older Zotero/BBT installs, and items imported before the change."""
    assert eci._citation_key(
        _data(extra="Citation Key: legacyKey2011")
    ) == "legacyKey2011"


def test_the_native_field_wins_over_extra() -> None:
    """A stale `extra` line left behind by an older BBT must not shadow
    the field Zotero itself maintains."""
    assert eci._citation_key(
        _data(citationKey="fresh2026", extra="Citation Key: stale2011")
    ) == "fresh2026"


def test_no_key_anywhere_is_empty_not_an_error() -> None:
    assert eci._citation_key(_data()) == ""
    assert eci._citation_key({}) == ""


def test_extra_parsing_tolerates_other_lines_and_case() -> None:
    assert eci._citation_key(_data(
        extra="tex.ids: x\nCITATION KEY:  spacedKey2020 \nfoo: bar",
    )) == "spacedKey2020"


def test_a_blank_native_field_falls_through_to_extra() -> None:
    """Zotero returns `""` rather than omitting the field on items BBT
    has not keyed; that must not mask a usable legacy value."""
    assert eci._citation_key(
        _data(citationKey="", extra="Citation Key: legacy2011")
    ) == "legacy2011"


def test_the_exported_row_carries_the_native_key() -> None:
    """End to end through the row builder — the 140-of-140 column."""
    item = {"key": "K1", "data": _data(citationKey="smithStudy2020",
                                       DOI="10.1/x", title="A paper")}
    row = eci._row_from_item(item, {})
    assert row["bibtex_key"] == "smithStudy2020"


# ---------------------------------------------------------------------------
# zotero_io.get_bbt_keys
# ---------------------------------------------------------------------------


def _client(items: list[dict]) -> zotero_io.ZoteroClient:
    zc = zotero_io.ZoteroClient(api_key="k", group_id="6658025")
    zc.items_by_keys = MagicMock(return_value=items)  # type: ignore[method-assign]
    zc.bbt_json_rpc = MagicMock(  # type: ignore[method-assign]
        return_value={"result": {}},
    )
    return zc


def test_native_keys_are_read_without_calling_bbt() -> None:
    """BBT cannot answer for a group library without a local-library id
    mapping this client does not have, so the native field is not merely
    preferred — it is the only route that works there."""
    zc = _client([{"key": "A", "data": {"citationKey": "alpha2020"}},
                  {"key": "B", "data": {"citationKey": "beta2021"}}])
    assert zc.get_bbt_keys(["A", "B"]) == {"A": "alpha2020", "B": "beta2021"}
    zc.bbt_json_rpc.assert_not_called()


def test_bbt_is_consulted_only_for_items_the_field_does_not_cover() -> None:
    zc = _client([{"key": "A", "data": {"citationKey": "alpha2020"}},
                  {"key": "B", "data": {"citationKey": ""}}])
    zc.bbt_json_rpc.return_value = {"result": {"B": "beta2021"}}
    assert zc.get_bbt_keys(["A", "B"]) == {"A": "alpha2020", "B": "beta2021"}
    sent = zc.bbt_json_rpc.call_args[0][1]["item_keys"]
    assert sent == ["B"], "only the uncovered key should reach BBT"


def test_an_empty_request_does_no_work() -> None:
    zc = _client([])
    assert zc.get_bbt_keys([]) == {}
    zc.items_by_keys.assert_not_called()


def test_a_bbt_outage_does_not_lose_the_native_keys() -> None:
    """The fallback is best-effort: BBT being unreachable must not
    discard keys Zotero already gave us."""
    zc = _client([{"key": "A", "data": {"citationKey": "alpha2020"}},
                  {"key": "B", "data": {}}])
    zc.bbt_json_rpc.side_effect = RuntimeError("BBT offline")
    assert zc.get_bbt_keys(["A", "B"]) == {"A": "alpha2020"}
