"""Abstract-stage batch apply keeps decisions made since emit (D5).

A manifest and its application can be days apart. Overwriting an item
tagged in between used to be the default, with skipping opt-in; now
skipping is the default and `--overwrite-tagged` is the opt-in.
"""

from __future__ import annotations

import abstract_screen
import batch_manifest as bm
import tag_prefix

NS = tag_prefix.namespace("test-review")
ITEM = {"key": "K1", "data": {"key": "K1", "title": "T", "abstractNote": "A",
                              "DOI": "10.1/x", "publicationTitle": "J"}}


def _one_item_run(tmp_path, reply: str):
    run_id = bm.new_run_id(bm.STAGE_ABSTRACT)
    rows, _ = abstract_screen.build_manifest_rows(
        [ITEM], run_id=run_id, system_prompt="S", model="m",
        prompt_version="v", doi_to_query={}, library={"kind": "user", "id": "1"},
        collection="C", ns=NS,
    )
    manifest = bm.write_manifest(tmp_path / "m.jsonl", rows)
    resp = {
        "schema_version": bm.SCHEMA_VERSION, "run_id": run_id,
        "request_id": rows[0]["request_id"], "item_key": "K1", "model": "m",
        "call_status": "ok", "response_text": reply,
        "output_tokens": 40, "generated_at": "2026-01-01T00:00:00+00:00",
    }
    responses = bm._write_jsonl(tmp_path / "r.jsonl", [resp])
    return manifest, responses


class _TaggedZot:
    """The item was tagged include by a human after the manifest was made."""

    def __init__(self):
        self.tagged = {**ITEM, "data": {**ITEM["data"], "tags": [
            {"tag": f"{tag_prefix.family(NS, 'abstract')}include"},
        ]}}
        self.writes: list = []

    def collection_items(self, *a, **k):
        return [self.tagged]

    def batch_update_tags(self, updates):
        self.writes.extend(updates)
        return {"applied": len(updates), "unchanged": 0, "failed": 0}


def test_batch_apply_skips_items_tagged_since_emit_by_default(
    tmp_path, capsys,
) -> None:
    manifest, responses = _one_item_run(tmp_path, "DECISION: exclude\nREASON: x")
    zot = _TaggedZot()
    abstract_screen.apply_responses(
        zot, manifest_path=manifest, responses_path=responses,
        output_path=tmp_path / "log.csv", tag_batch_size=50, force=False,
        ns=NS,
    )
    assert zot.writes == []
    assert not (tmp_path / "log.csv").exists()
    assert "skipping them" in capsys.readouterr().out


def test_batch_apply_overwrites_only_with_the_flag(tmp_path) -> None:
    manifest, responses = _one_item_run(tmp_path, "DECISION: exclude\nREASON: x")
    zot = _TaggedZot()
    abstract_screen.apply_responses(
        zot, manifest_path=manifest, responses_path=responses,
        output_path=tmp_path / "log.csv", tag_batch_size=50, force=False,
        overwrite_tagged=True, ns=NS,
    )
    assert [k for k, _ in zot.writes] == ["K1"]
