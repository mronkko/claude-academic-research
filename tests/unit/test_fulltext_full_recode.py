"""`--full-recode` must code what it just untagged (BACKLOG S13).

It cleared every targeted item's stage tag through the Web API, then
re-read the collection through the local API, which had not synced. The
stale read still showed `fulltext:include`, so nothing was selected: the
run printed "Nothing to code." and exited 0 with the tags already gone.
"""

from __future__ import annotations

import fulltext_code as fc
import tag_prefix

NS = tag_prefix.namespace("test-review")
STAGE = tag_prefix.family(NS, "fulltext")
DESIGN = tag_prefix.coding_family(NS, "design")


def _item(key: str, *tags: str) -> dict:
    return {"key": key, "data": {"key": key, "title": key,
                                 "tags": [{"tag": t} for t in tags]}}


class _StaleZot:
    """Writes land; reads return the pre-write state, like an unsynced
    local API. Any read during the recode is a failure of this fix."""

    def __init__(self, fail_keys=()):
        self.cleared: list[str] = []
        self.fail_keys = set(fail_keys)
        self.reads = 0

    def update_tags(self, key, add=(), remove_prefixed=()):
        if key in self.fail_keys:
            raise RuntimeError("HTTP 503")
        self.cleared.append(key)

    def collection_items(self, *a, **k):
        self.reads += 1
        raise AssertionError("re-read after the clear: that read is stale")


ITEMS = [
    _item("A", f"{STAGE}include", f"{DESIGN}panel", "keep:me"),
    _item("B", f"{STAGE}exclude"),
    _item("C", f"{STAGE}include"),
]


def test_cleared_items_are_selected_without_a_re_read() -> None:
    zot = _StaleZot()
    after = fc.clear_tags_for_recode(zot, ITEMS, [STAGE, DESIGN])
    assert zot.reads == 0
    assert zot.cleared == ["A", "B", "C"]
    assert fc._already_tagged(after, STAGE) == set()
    to_code = fc._select_to_code(
        after, tagged=fc._already_tagged(after, STAGE),
        last_decisions={}, rerun=False,
    )
    assert [it["key"] for it in to_code] == ["A", "B", "C"]
    # Only the recode families went; an unrelated tag survives.
    assert [t["tag"] for t in after[0]["data"]["tags"]] == ["keep:me"]
    # The caller's items were not mutated.
    assert len(ITEMS[0]["data"]["tags"]) == 3


def test_an_item_whose_clear_failed_still_counts_as_coded() -> None:
    zot = _StaleZot(fail_keys={"B"})
    after = fc.clear_tags_for_recode(zot, ITEMS, [STAGE])
    assert fc._already_tagged(after, STAGE) == {"B"}


def test_a_dry_run_writes_nothing_but_selects_the_same() -> None:
    zot = _StaleZot()
    after = fc.clear_tags_for_recode(zot, ITEMS, [STAGE], write=False)
    assert zot.cleared == []
    assert fc._already_tagged(after, STAGE) == set()


def test_main_no_longer_re_reads_the_collection_after_clearing() -> None:
    src = (fc.Path(fc.__file__)).read_text(encoding="utf-8")
    body = src[src.index("    if args.full_recode:\n        # Coding-value"):]
    body = body[:body.index("    if not args.update_fields:")]
    assert "collection_items" not in body
    assert "clear_tags_for_recode" in body
