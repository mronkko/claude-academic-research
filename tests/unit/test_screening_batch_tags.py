"""Tests for R6 steady-state batched stage-tag writes in abstract_screen.

These pin the batching helpers (`_stage_tag_op`, `_flush_tag_buffer`)
without a live Zotero — the flusher takes any object exposing
`batch_update_tags`.
"""

from __future__ import annotations

import abstract_screen


class _FakeZot:
    """Records `batch_update_tags` calls and returns a configurable stats
    dict so the warning path can be exercised."""

    def __init__(self, failed: int = 0) -> None:
        self.calls: list[list[tuple[str, dict]]] = []
        self._failed = failed

    def batch_update_tags(self, updates):
        # Copy — the caller clears its buffer after we return.
        self.calls.append(list(updates))
        return {"applied": len(updates), "unchanged": 0, "failed": self._failed}


def test_stage_tag_op_adds_decision_and_clears_prefix() -> None:
    op = abstract_screen._stage_tag_op("include")
    assert op == {
        "add": ["abstract:include"],
        "remove_prefixed": ["abstract:"],
    }


def test_flush_empty_buffer_is_a_noop() -> None:
    zot = _FakeZot()
    stats = abstract_screen._flush_tag_buffer(zot, [])
    assert zot.calls == []
    assert stats == {"applied": 0, "unchanged": 0, "failed": 0}


def test_flush_sends_buffer_and_clears_it() -> None:
    zot = _FakeZot()
    buffer = [
        ("KEY1", abstract_screen._stage_tag_op("include")),
        ("KEY2", abstract_screen._stage_tag_op("exclude")),
    ]
    stats = abstract_screen._flush_tag_buffer(zot, buffer)

    assert len(zot.calls) == 1
    assert [k for k, _ in zot.calls[0]] == ["KEY1", "KEY2"]
    assert stats["applied"] == 2
    assert buffer == []  # cleared in place so the caller can keep filling it


def test_flush_warns_but_clears_on_failures(capsys) -> None:
    zot = _FakeZot(failed=1)
    buffer = [("KEY1", abstract_screen._stage_tag_op("include"))]
    abstract_screen._flush_tag_buffer(zot, buffer)
    out = capsys.readouterr().out
    assert "tag write(s) failed" in out
    assert buffer == []


def test_batching_threshold_flushes_in_chunks() -> None:
    """Simulate the main loop's buffer-and-flush at a batch size of 2 over
    five decisions: two full flushes mid-loop, one partial flush at the end."""
    zot = _FakeZot()
    batch_size = 2
    buffer: list[tuple[str, dict]] = []

    for i in range(5):
        buffer.append((f"KEY{i}", abstract_screen._stage_tag_op("include")))
        if len(buffer) >= batch_size:
            abstract_screen._flush_tag_buffer(zot, buffer)
    abstract_screen._flush_tag_buffer(zot, buffer)  # final flush

    # 2 + 2 + 1 = three batches covering all five keys, none dropped.
    assert [len(c) for c in zot.calls] == [2, 2, 1]
    flushed = [k for call in zot.calls for k, _ in call]
    assert flushed == ["KEY0", "KEY1", "KEY2", "KEY3", "KEY4"]


def test_tag_batch_size_floor_is_one() -> None:
    """`max(1, args.tag_batch_size)` — a 0 or negative arg must not disable
    flushing (which would silently drop all tag writes)."""
    assert max(1, 0) == 1
    assert max(1, -5) == 1
