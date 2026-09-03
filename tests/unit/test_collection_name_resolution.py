"""`--collection <name>` has to work on the remote read path too.

`collection_items` passed its argument straight to the Zotero API as a
collection key. Locally that happened to work — Zotero's own HTTP server
tolerates a name — so `abstract_screen.py --collection SLR` ran fine
until `--remote` was added, at which point api.zotero.org was asked for
`/collections/SLR/items` and returned 404 "Collection not found".

Two things make that worth fixing rather than documenting. It is
inconsistent with `import_to_zotero.py`, which resolves keys and names on
either path and says so. And the same class of failure has already cost
this project once: the note on `find_collection` records a run where a
name reached the API as a key, every item in the batch failed with an
opaque 400, and the agent driving it improvised a fix that duplicated the
whole corpus.

Resolution is deliberately skipped for anything already shaped like a key
(8 upper-case alphanumerics), so the common path costs no extra request
and a caller passing a real key is unaffected.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
import zotero_io


def _client(collections: list[dict]) -> zotero_io.ZoteroClient:
    zc = zotero_io.ZoteroClient(api_key="k", group_id="1")
    read = MagicMock()
    read.everything.side_effect = lambda arg: arg
    read.collections.return_value = collections
    read.collection_items.side_effect = (
        lambda key, **kw: [{"key": "I1", "_collection": key}]
    )
    zc._read_client = MagicMock(return_value=read)  # type: ignore[method-assign]
    zc._read = read
    return zc


def _coll(key: str, name: str) -> dict:
    return {"key": key, "data": {"name": name}}


def test_a_name_resolves_to_its_key() -> None:
    zc = _client([_coll("QCN49MPI", "SLR")])
    items = zc.collection_items("SLR")
    assert items[0]["_collection"] == "QCN49MPI"


def test_a_key_is_used_verbatim_without_a_lookup() -> None:
    """The common path stays one request."""
    zc = _client([_coll("QCN49MPI", "SLR")])
    items = zc.collection_items("QCN49MPI")
    assert items[0]["_collection"] == "QCN49MPI"
    zc._read.collections.assert_not_called()


def test_an_unknown_name_raises_with_an_actionable_message() -> None:
    """A 404 from the API said "Collection not found" and named nothing
    the user had typed."""
    zc = _client([_coll("QCN49MPI", "SLR")])
    with pytest.raises(ValueError) as exc:
        zc.collection_items("Screening")
    message = str(exc.value)
    assert "Screening" in message
    assert "SLR" in message, "the message should name what does exist"


def test_an_ambiguous_name_refuses_rather_than_guessing() -> None:
    """Same rule as `find_collection`: never pick one of two."""
    zc = _client([_coll("AAAAAAAA", "SLR"), _coll("BBBBBBBB", "SLR")])
    with pytest.raises(ValueError, match="Ambiguous"):
        zc.collection_items("SLR")


def test_a_name_with_spaces_resolves() -> None:
    zc = _client([_coll("QCN49MPI", "Systematic Review 2026")])
    assert zc.collection_items("Systematic Review 2026")[0][
        "_collection"] == "QCN49MPI"


def test_surrounding_whitespace_is_tolerated() -> None:
    zc = _client([_coll("QCN49MPI", "SLR")])
    assert zc.collection_items("  SLR  ")[0]["_collection"] == "QCN49MPI"


def test_an_empty_collection_argument_is_rejected() -> None:
    zc = _client([_coll("QCN49MPI", "SLR")])
    with pytest.raises(ValueError):
        zc.collection_items("")


def test_resolution_uses_the_read_client_not_the_cloud() -> None:
    """A local run must not acquire a cloud dependency it did not have.
    `find_collection` reads the cloud deliberately, for read-your-writes
    on a collection this pipeline just created; this path only reads."""
    zc = _client([_coll("QCN49MPI", "SLR")])
    zc._cloud = MagicMock(side_effect=AssertionError("must not touch cloud"))
    zc.collection_items("SLR")
    zc._read.collections.assert_called_once()
