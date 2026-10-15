"""Abstract-screening reply parsing and the bounded re-ask (D6).

`borderline` on an unparseable reply is a deliberate bias, but it is also
permanent: the tag marks the item done. Before this, `**DECISION:** include`
and `Decision: Include.` — formatting noise, not indecision — landed there.
"""

from __future__ import annotations

import abstract_screen
import pytest


@pytest.mark.parametrize("text, expected", [
    ("DECISION: include\nREASON: fits", ("include", "fits")),
    ("**DECISION:** include\n**REASON:** fits", ("include", "fits")),
    ("**Decision**: Include.\nReason: fits", ("include", "fits")),
    ("Decision: Exclude!\nReason: a review", ("exclude", "a review")),
    ("  - DECISION: **borderline**\n  - REASON: unclear", ("borderline", "unclear")),
    ("decision: INCLUDE (meets all three)\nreason: ok", ("include", "ok")),
    ('DECISION: "exclude"\nREASON: x', ("exclude", "x")),
    ("## Decision: include\nReason: x", ("include", "x")),
])
def test_formatting_noise_does_not_change_the_decision(text, expected) -> None:
    assert abstract_screen.parse_decision(text) == expected


@pytest.mark.parametrize("text", [
    "DECISION: inc",
    "I think this paper is relevant.",
    "",
    "DECISION: maybe\nREASON: x",
])
def test_unparseable_still_falls_back_to_borderline(text) -> None:
    decision, reason = abstract_screen.parse_decision(text)
    assert decision == "borderline"
    assert abstract_screen.is_parse_fallback(reason)
    assert abstract_screen.try_parse_decision(text) is None


class _Client:
    """Replies from a script, recording each prompt it was sent."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def generate(self, *, model, max_tokens, temperature, system, prompt):
        self.prompts.append(prompt)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


ITEM = {"key": "K1", "data": {"key": "K1", "title": "T", "abstractNote": "A",
                              "DOI": "10.1/x", "publicationTitle": "J"}}


def _screen(client, **kw):
    return abstract_screen.screen_item(
        ITEM, client=client, model="m", system_prompt="S", doi_to_query={}, **kw,
    )


def test_an_unparseable_reply_is_re_asked_before_falling_back() -> None:
    client = _Client(["blah", "DECISION: exclude\nREASON: off-topic"])
    res = _screen(client)
    assert (res.decision, res.reason) == ("exclude", "off-topic")
    assert res.reasks == 1
    assert not res.parse_fallback
    # The re-ask restates the question and reminds the model of the format.
    assert client.prompts[1].startswith(client.prompts[0])
    assert abstract_screen.REASK_SUFFIX in client.prompts[1]


def test_re_asking_is_bounded_then_borderline() -> None:
    client = _Client(["nope"] * 10)
    res = _screen(client)
    assert res.decision == "borderline"
    assert res.parse_fallback
    assert res.reasks == abstract_screen.PARSE_REASK_LIMIT
    assert len(client.prompts) == abstract_screen.PARSE_REASK_LIMIT + 1


def test_a_parseable_first_reply_costs_one_call() -> None:
    client = _Client(["DECISION: include\nREASON: ok"])
    res = _screen(client)
    assert (res.decision, res.reasks, res.parse_fallback) == ("include", 0, False)
    assert len(client.prompts) == 1


def test_a_provider_failure_is_an_error_not_a_reask(monkeypatch) -> None:
    class _Verdict:
        retryable = False
        detail = "quota"

        class status:  # noqa: N801
            value = "quota_exhausted"

    monkeypatch.setattr(
        abstract_screen.llm_provider, "classify_failure", lambda e: _Verdict(),
    )
    client = _Client([RuntimeError("429")])
    res = _screen(client)
    assert res.decision == "error"
    assert res.fatal is not None
    assert len(client.prompts) == 1


def test_the_batch_applier_counts_parse_fallbacks(tmp_path, capsys) -> None:
    """The batch path cannot re-ask, so it falls back at once — and says
    how often, in the same summary a reader checks for errors."""
    import batch_manifest as bm
    import tag_prefix

    ns = tag_prefix.namespace("test-review")
    run_id = bm.new_run_id(bm.STAGE_ABSTRACT)
    rows, _ = abstract_screen.build_manifest_rows(
        [ITEM], run_id=run_id, system_prompt="S", model="m",
        prompt_version="v", doi_to_query={}, library={"kind": "user", "id": "1"},
        collection="C", ns=ns,
    )
    manifest = bm.write_manifest(tmp_path / "m.jsonl", rows)
    resp = {
        "schema_version": bm.SCHEMA_VERSION, "run_id": run_id,
        "request_id": rows[0]["request_id"], "item_key": "K1", "model": "m",
        "call_status": "ok", "response_text": "no format at all",
        "output_tokens": 40, "generated_at": "2026-01-01T00:00:00+00:00",
    }
    responses = bm._write_jsonl(tmp_path / "r.jsonl", [resp])

    class _Zot:
        def collection_items(self, *a, **k):
            return []

        def batch_update_tags(self, updates):
            return {"applied": len(updates), "unchanged": 0, "failed": 0}

    abstract_screen.apply_responses(
        _Zot(), manifest_path=manifest, responses_path=responses,
        output_path=tmp_path / "log.csv", tag_batch_size=50, force=False,
        overwrite_tagged=False, ns=ns,
    )
    assert "unparseable reply, tagged borderline: 1" in capsys.readouterr().out
