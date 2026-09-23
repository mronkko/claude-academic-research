"""Bulk listings retry a slow page instead of dying.

Live 2026-09-23: enrich_pdfs died at startup on `httpx2.ReadTimeout`
while listing 23,995 attachments over the local API, with Zotero Desktop
busy with another session's bulk trash.
"""

from __future__ import annotations

import httpx2
import pytest
import zotero_io


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(zotero_io, "_PAGE_ATTEMPTS", 4)
    monkeypatch.setattr(zotero_io, "_page_sleep", lambda s: None)


class _Pager:
    """Minimal pyzotero surface: first page, then `follow()` via links."""

    def __init__(self, pages: list[list[int]], fail: dict[int, list]) -> None:
        self.pages = pages
        self.fail = fail            # page index -> exceptions to raise first
        self.links: dict = {}
        self.calls: list[int] = []

    def _serve(self, i: int) -> list[int]:
        self.calls.append(i)
        if self.fail.get(i):
            raise self.fail[i].pop(0)
        self.links = {"next": f"p{i + 1}"} if i + 1 < len(self.pages) else {}
        return self.pages[i]

    def first(self) -> list[int]:
        return self._serve(0)

    def follow(self) -> list[int]:
        return self._serve(int(self.links["next"][1:]))

    def everything(self, query: list[int]) -> list[int]:
        """pyzotero's paging loop, as in `Zotero.everything`."""
        items = list(query)
        while self.links.get("next"):
            items.extend(self.follow())
        return items


def test_a_timed_out_page_is_retried_alone() -> None:
    z = _Pager([[1, 2], [3, 4], [5]],
               {1: [httpx2.ReadTimeout("timed out")]})
    assert zotero_io._everything(z, z.first) == [1, 2, 3, 4, 5]
    assert z.calls == [0, 1, 1, 2]
    assert z.follow.__func__ is _Pager.follow, "the wrapper is removed after"


def test_the_first_page_is_retried_too() -> None:
    z = _Pager([[1]], {0: [httpx2.ReadTimeout("t"), httpx2.ReadTimeout("t")]})
    assert zotero_io._everything(z, z.first) == [1]


def test_it_gives_up_after_the_attempt_budget() -> None:
    z = _Pager([[1], [2]], {1: [httpx2.ReadTimeout("t")] * 4})
    with pytest.raises(httpx2.ReadTimeout):
        zotero_io._everything(z, z.first)
    assert z.calls.count(1) == 4


def test_nothing_listening_fails_fast() -> None:
    """A ConnectError means Zotero is not running; backing off will not
    start it."""
    z = _Pager([[1]], {0: [httpx2.ConnectError("refused")]})
    with pytest.raises(httpx2.ConnectError):
        zotero_io._everything(z, z.first)
    assert z.calls == [0]


def test_other_errors_are_not_retried() -> None:
    z = _Pager([[1]], {0: [ValueError("bad")]})
    with pytest.raises(ValueError):
        zotero_io._everything(z, z.first)
    assert z.calls == [0]
