"""What a `--filter-keys-file` fetch counts as a skipped key.

`ZoteroClient.items_by_keys` answers with the requested items *and*
their attachment children, so the size of the response tracks how many
of those items have PDFs rather than how many keys were asked for. A
live 38-key EBSCO batch came back as 55 items — 38 articles plus the 17
attachments that earlier passes had just created — and the run
announced "11 key(s) resolved to non-journalArticle items and were
skipped" immediately after attaching 11 PDFs. Nothing had been skipped.

The failure mode is the one this pipeline keeps re-learning: a
diagnostic asserting something it never established. Here it was
anti-correlated with the truth, since every successful attach made the
claim larger.
"""

from __future__ import annotations

import enrich_pdfs


def _article(key: str) -> dict:
    return {"key": key, "data": {"itemType": "journalArticle", "key": key}}


def _attachment(key: str, parent: str) -> dict:
    return {
        "key": key,
        "data": {"itemType": "attachment", "key": key, "parentItem": parent},
    }


def test_attachment_children_are_not_skipped_keys() -> None:
    """The real 38-key shape: children inflate the response, not the count."""
    requested = {f"K{i:02d}" for i in range(38)}
    fetched = [_article(k) for k in sorted(requested)]
    # 17 of them have a PDF, so the API returns 17 extra child items.
    fetched += [
        _attachment(f"A{i:02d}", f"K{i:02d}") for i in range(17)
    ]
    assert len(fetched) == 55

    articles, not_articles = enrich_pdfs.select_requested_articles(
        fetched, requested)

    assert len(articles) == 38
    assert not_articles == 0


def test_count_does_not_grow_as_pdfs_are_attached() -> None:
    """The regression proper — the claim must not track retrieval success."""
    requested = {"K1", "K2", "K3"}
    base = [_article(k) for k in sorted(requested)]

    counts = []
    for attached in range(len(requested) + 1):
        fetched = base + [
            _attachment(f"A{i}", f"K{i + 1}") for i in range(attached)
        ]
        counts.append(
            enrich_pdfs.select_requested_articles(fetched, requested)[1])

    assert counts == [0, 0, 0, 0]


def test_a_requested_non_article_is_still_reported() -> None:
    """A book chapter that was asked for is a scope decision, and stays one."""
    requested = {"K1", "K2", "K3"}
    fetched = [
        _article("K1"),
        _article("K2"),
        {"key": "K3", "data": {"itemType": "bookSection", "key": "K3"}},
        _attachment("A1", "K1"),
    ]

    articles, not_articles = enrich_pdfs.select_requested_articles(
        fetched, requested)

    assert [it["key"] for it in articles] == ["K1", "K2"]
    assert not_articles == 1


def test_only_requested_items_are_returned() -> None:
    """An unrequested article in the response is not work this run claimed."""
    requested = {"K1"}
    fetched = [_article("K1"), _article("STRAY")]

    articles, not_articles = enrich_pdfs.select_requested_articles(
        fetched, requested)

    assert [it["key"] for it in articles] == ["K1"]
    assert not_articles == 0
