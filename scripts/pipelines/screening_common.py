"""Shared machinery for the two screening-stage orchestrators.

`abstract_screen.py` and `fulltext_code.py` run the same shape of job at
different stages: load a per-project config module by path, read Zotero
stage tags to decide what is already done, record decisions as
`<stage>:<decision>` tags, and offer a one-time `--csv-backfill` migration
for deployments that predate Zotero-as-source-of-truth. Each had grown its
own copy of that machinery, differing only in the stage prefix, the
decision vocabulary, and message wording. `search.py` carried a third copy
of the config-loading half.

This module holds the logic; each orchestrator keeps a thin private wrapper
that binds its own constants. That split is deliberate — it keeps the call
sites and their tests unchanged, and it keeps each stage's vocabulary
visible in the stage's own file rather than buried in a shared default.

**One difference is not abstracted away.** The two stages filter CSV
decisions at different points, and it changes the result:

- `abstract_screen` filters *while reading*, so a later `error` row does not
  displace an earlier valid decision for the same item.
- `fulltext_code` takes the genuinely-last decision and filters *after*, so
  a trailing `error` row drops the item from the backfill entirely.

`last_decisions_by_key` therefore takes an optional `valid` set for the
first behaviour, and `run_csv_backfill` accepts an already-computed
decisions mapping rather than a path — so the callers keep their own
semantics instead of a flag hiding the difference.
"""

from __future__ import annotations

import csv
import importlib.util
import sys
from collections.abc import Iterable, Sequence
from pathlib import Path
from types import ModuleType

# ---------------------------------------------------------------------------
# Per-project config modules loaded by path
# ---------------------------------------------------------------------------


def load_config_module(
    path: str,
    module_name: str,
    required: Sequence[str] = (),
) -> ModuleType:
    """Import a per-project config file by path and check required attrs.

    `module_name` is both the name the module is registered under and the
    label used in the failure message (`"screening_config"` →
    "cannot load screening config: ..."), matching what the orchestrators
    printed before this was shared.

    Exits via `sys.exit` with an actionable message when a required
    attribute is missing — these are user-authored project files, and a
    traceback is the wrong response to a typo in one.
    """
    spec = importlib.util.spec_from_file_location(module_name, path)
    label = module_name.replace("_", " ")
    assert spec is not None and spec.loader is not None, (
        f"cannot load {label}: {path}"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for attr in required:
        if not hasattr(mod, attr):
            sys.exit(f"ERROR: {path} is missing `{attr}`.")
    return mod


# ---------------------------------------------------------------------------
# Zotero stage tags — the resume source of truth
# ---------------------------------------------------------------------------


def items_with_stage_tag(
    items: list[dict],
    *,
    prefix: str,
    values: Iterable[str] | None = None,
) -> set[str]:
    """Keys of items already carrying a stage tag — i.e. 'done' on resume.

    With `values`, only the exact tags `{prefix}{value}` count
    (`fulltext:include` / `fulltext:exclude`). Without it, any tag starting
    with `prefix` counts — which is how abstract screening treats
    `abstract:borderline` as decided rather than pending.
    """
    exact = {f"{prefix}{v}" for v in values} if values is not None else None
    done: set[str] = set()
    for item in items:
        tags = {
            t.get("tag", "")
            for t in item.get("data", {}).get("tags", [])
        }
        if exact is not None:
            hit = bool(tags & exact)
        else:
            hit = any(t.startswith(prefix) for t in tags)
        if hit:
            done.add(item["key"])
    return done


def stage_tag_op(prefix: str, decision: str) -> dict:
    """The `update_tags` / `batch_update_tags` op recording one decision:
    add `{prefix}{decision}`, clearing any prior tag under `prefix` in the
    same PATCH so a re-screen replaces rather than accumulates."""
    return {
        "add": [f"{prefix}{decision}"],
        "remove_prefixed": [prefix],
    }


# ---------------------------------------------------------------------------
# CSV decision logs
# ---------------------------------------------------------------------------


def last_decisions_by_key(
    path: Path,
    *,
    valid: Iterable[str] | None = None,
) -> dict[str, str]:
    """Last decision per `item_key` from an append-only screening log.

    With `valid`, rows carrying any other decision are skipped entirely, so
    they do not displace an earlier valid decision for the same item.
    Without it, the genuinely-last row wins whatever it says.

    Used for `--csv-backfill` migration and `--rerun`, never for resume
    decisions — Zotero tags are authoritative for those.
    """
    if not path.exists():
        return {}
    allowed = set(valid) if valid is not None else None
    last: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            key = row.get("item_key")
            if not key:
                continue
            decision = row.get("decision", "")
            if allowed is not None and decision not in allowed:
                continue
            last[key] = decision
    return last


def run_csv_backfill(
    zot,
    coll_items: list[dict],
    decisions: dict[str, str],
    *,
    prefix: str,
    values: Iterable[str] | None = None,
    label: str,
) -> int:
    """Apply stage tags for items that have a CSV decision but no tag yet.

    A one-time migration for deployments that predate Zotero-as-source-of-
    truth. Makes no LLM calls. `decisions` is supplied by the caller (see
    the module docstring on why it is not read from a path here).

    Returns a process exit code: 0 on success, 1 if any tag write failed.
    """
    tagged = items_with_stage_tag(coll_items, prefix=prefix, values=values)
    drift = {k: d for k, d in decisions.items() if k not in tagged}

    if not drift:
        print(f"Nothing to backfill — all CSV-decided items already have "
              f"{label} tags in Zotero.", flush=True)
        return 0

    print(f"Backfilling {label} tags for {len(drift)} item(s) "
          f"(batched)...", flush=True)
    updates = [
        (key, stage_tag_op(prefix, decision))
        for key, decision in drift.items()
    ]
    stats = zot.batch_update_tags(updates)
    print(
        f"Backfill complete: {stats['applied']} tagged, "
        f"{stats['unchanged']} unchanged, {stats['failed']} failed.",
        flush=True,
    )
    return 0 if stats["failed"] == 0 else 1
