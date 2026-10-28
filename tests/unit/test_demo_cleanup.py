"""demo_cleanup deletes only what a demo run created."""

from __future__ import annotations

import demo_cleanup
import pytest


class _Zot:
    def __init__(self, items, collection_name="demo-x"):
        self.items = items            # key -> tag list
        self.deleted: list[str] = []
        self.coll_deleted = False
        self.name = collection_name

    def get_item(self, key):
        if key not in self.items:
            raise KeyError(key)
        return {"data": {"tags": [{"tag": t} for t in self.items[key]]}}

    def delete_item(self, key):
        self.deleted.append(key)
        return True

    def _client(self):
        z = self
        class C:
            def collection(self, key):
                return {"data": {"name": z.name}}
        return C()

    def delete_collection(self, key):
        self.coll_deleted = True
        return True


def test_refuses_a_non_demo_prefix():
    with pytest.raises(SystemExit):
        demo_cleanup.cleanup(_Zot({}), collection="", prefix="my-review",
                             keys=["A"], dry_run=False)


def test_deletes_only_tagged_listed_items_and_demo_collection():
    zot = _Zot({"A": ["demo-x/abstract:include"], "B": ["other/tag"]})
    res = demo_cleanup.cleanup(zot, collection="COLL1234", prefix="demo-x",
                               keys=["A", "B", "GONE"], dry_run=False)
    assert zot.deleted == ["A"]
    assert res == {"deleted_items": 1, "skipped_items": 2,
                   "deleted_collection": True}


def test_collection_not_named_demo_is_kept():
    zot = _Zot({}, collection_name="My real collection")
    res = demo_cleanup.cleanup(zot, collection="COLL1234", prefix="demo-x",
                               keys=[], dry_run=False)
    assert not zot.coll_deleted and res["deleted_collection"] is False


def test_dry_run_deletes_nothing():
    zot = _Zot({"A": ["demo-x/t"]})
    demo_cleanup.cleanup(zot, collection="COLL1234", prefix="demo-x",
                         keys=["A"], dry_run=True)
    assert zot.deleted == [] and not zot.coll_deleted
