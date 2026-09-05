"""A scoped abstract run must fetch what it asked for, not the library.

`enrich_abstracts.py` accepted `--filter-keys-file` and then applied it
*after* `abstractable_items()` had already walked the entire library:
the filter reduced what got enriched but not what got fetched. Against
a 27,000-item library, a run scoped to a few hundred keys spent most of
its wall time paginating through items it was about to discard, and
every backoff retry paid for the sweep again.

`enrich_pdfs.py` already had the fix — `ZoteroClient.items_by_keys`
asks for exactly the keys wanted, 50 per request. This puts the two
scripts on the same footing.

One wrinkle the shared helper brings with it: `items_by_keys` answers
with the requested items *and* their attachment children, so the
item-type filter has to be applied to the response rather than assumed
away by the query. See `test_filter_keys_selection.py` for the count
this got wrong the first time.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import enrich_abstracts


def _item(key: str, item_type: str = "journalArticle") -> dict:
    return {"key": key, "data": {"itemType": item_type, "key": key}}


def test_a_scoped_run_asks_for_its_keys_and_never_walks_the_library(tmp_path):
    keys_file = tmp_path / "keys.txt"
    keys_file.write_text("AAAA1111\nBBBB2222\n")

    zot = MagicMock()
    zot.items_by_keys.return_value = [_item("AAAA1111"), _item("BBBB2222")]

    got = enrich_abstracts._select_items(zot, ["journalArticle"], str(keys_file))

    assert {i["key"] for i in got} == {"AAAA1111", "BBBB2222"}
    zot.items_by_keys.assert_called_once()
    assert sorted(zot.items_by_keys.call_args[0][0]) == ["AAAA1111", "BBBB2222"]
    zot.abstractable_items.assert_not_called()


def test_a_scoped_run_still_honours_item_types(tmp_path):
    """`items_by_keys` does no type filtering, and returns children too."""
    keys_file = tmp_path / "keys.txt"
    keys_file.write_text("AAAA1111\nBBBB2222\n")

    zot = MagicMock()
    zot.items_by_keys.return_value = [
        _item("AAAA1111", "journalArticle"),
        _item("BBBB2222", "book"),
        _item("CHILD001", "attachment"),
    ]

    got = enrich_abstracts._select_items(zot, ["journalArticle"], str(keys_file))

    assert [i["key"] for i in got] == ["AAAA1111"]


def test_an_unscoped_run_is_unchanged(tmp_path):
    """No keys file, no behaviour change: the sweep is still the right call."""
    zot = MagicMock()
    zot.abstractable_items.return_value = [_item("AAAA1111")]

    got = enrich_abstracts._select_items(zot, ["journalArticle"], None)

    assert [i["key"] for i in got] == ["AAAA1111"]
    zot.abstractable_items.assert_called_once_with(["journalArticle"])
    zot.items_by_keys.assert_not_called()


def test_blank_lines_and_whitespace_in_the_keys_file_are_ignored(tmp_path):
    keys_file = tmp_path / "keys.txt"
    keys_file.write_text("  AAAA1111  \n\n\nBBBB2222\n\n")

    zot = MagicMock()
    zot.items_by_keys.return_value = [_item("AAAA1111"), _item("BBBB2222")]

    enrich_abstracts._select_items(zot, ["journalArticle"], str(keys_file))

    assert sorted(zot.items_by_keys.call_args[0][0]) == ["AAAA1111", "BBBB2222"]
