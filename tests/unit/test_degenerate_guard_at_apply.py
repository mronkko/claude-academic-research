"""The degenerate-run guard runs even without a `.run.json` (D1).

Only `run_manifest.py` and the cluster runner write the run record. A
responses file from any other executor — a gateway script, a notebook —
arrived without one, and apply skipped the guard entirely: a run of empty
replies tagged the whole corpus `borderline`.
"""

from __future__ import annotations

import abstract_screen
import batch_manifest as bm
import pytest
import tag_prefix

NS = tag_prefix.namespace("test-review")


def _items(n: int) -> list[dict]:
    return [
        {"key": f"K{i:07d}", "data": {"key": f"K{i:07d}", "title": f"T{i}",
                                      "abstractNote": "A", "DOI": f"10.1/{i}",
                                      "publicationTitle": "J"}}
        for i in range(n)
    ]


def _run(tmp_path, replies: list[dict]):
    run_id = bm.new_run_id(bm.STAGE_ABSTRACT)
    rows, _ = abstract_screen.build_manifest_rows(
        _items(len(replies)), run_id=run_id, system_prompt="S", model="m",
        prompt_version="v", doi_to_query={}, library={"kind": "user", "id": "1"},
        collection="", ns=NS,
    )
    manifest = bm.write_manifest(tmp_path / "m.jsonl", rows)
    responses = [
        {"schema_version": bm.SCHEMA_VERSION, "run_id": run_id,
         "request_id": r["request_id"], "item_key": r["item_key"],
         "model": "m", "generated_at": "2026-01-01T00:00:00+00:00", **reply}
        for r, reply in zip(rows, replies, strict=True)
    ]
    path = bm._write_jsonl(tmp_path / "r.jsonl", responses)
    assert not bm.run_record_path(path).exists()
    return manifest, path


class _Zot:
    def __init__(self):
        self.writes: list = []

    def collection_items(self, *a, **k):
        return []

    def batch_update_tags(self, updates):
        self.writes.extend(updates)
        return {"applied": len(updates), "unchanged": 0, "failed": 0}


def _apply(tmp_path, manifest, responses, zot, force=False):
    return abstract_screen.apply_responses(
        zot, manifest_path=manifest, responses_path=responses,
        output_path=tmp_path / "log.csv", tag_batch_size=50, force=force,
        ns=NS,
    )


def test_a_degenerate_run_without_a_run_record_is_refused(tmp_path) -> None:
    # Third-party executor: empty answers, no token counts, no .run.json.
    manifest, responses = _run(tmp_path, [
        {"call_status": "ok", "response_text": ""} for _ in range(5)
    ])
    zot = _Zot()
    with pytest.raises(SystemExit, match="REFUSING"):
        _apply(tmp_path, manifest, responses, zot)
    assert zot.writes == []
    assert not (tmp_path / "log.csv").exists()


def test_force_still_overrides_the_computed_verdict(tmp_path) -> None:
    manifest, responses = _run(tmp_path, [
        {"call_status": "empty", "response_text": ""} for _ in range(3)
    ])
    assert _apply(tmp_path, manifest, responses, _Zot(), force=True) == 0


def test_a_healthy_run_without_token_counts_is_not_refused(tmp_path) -> None:
    """Missing usage is estimated from the text, not read as zero —
    otherwise every executor that reports no usage would be refused."""
    manifest, responses = _run(tmp_path, [
        {"call_status": "ok",
         "response_text": "DECISION: include\nREASON: meets all criteria"}
        for _ in range(3)
    ])
    zot = _Zot()
    assert _apply(tmp_path, manifest, responses, zot) == 0
    assert len(zot.writes) == 3


def test_a_run_of_only_failed_calls_is_not_called_degenerate() -> None:
    """All-error is a provider problem, reported by status_counts; its
    rows are untagged `error`, so the guard has nothing to protect."""
    rec = bm.summarise_run("r", stage="s", model="m", responses=[
        {"call_status": "error", "output_tokens": 0} for _ in range(4)
    ])
    assert "degenerate_output" not in rec

