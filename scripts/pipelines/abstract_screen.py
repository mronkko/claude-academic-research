#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic>=0.40",
#     "pyzotero>=1.6",
#     "tenacity>=8.0",
#     "httpx>=0.25",
#     "google-genai",
#     "openai>=1.0",
# ]
# ///
"""LLM-driven title+abstract screening for a systematic review.

Reads items from a Zotero collection, screens each title+abstract via
Claude Haiku (configurable; temperature=0), and writes the decision in
two places:

1. As an `abstract:include` / `abstract:exclude` / `abstract:borderline`
   Zotero tag on the item — this is the authoritative state per the
   `systematic-review` skill's Zotero-as-ground-truth principle.
   Downstream stages (`fulltext_code.py`, `export_coded_includes.py`)
   filter by this tag.
2. As an append-only row in `screening/abstract_screening.csv` — this
   is the run-history for provenance (who decided what, when, with
   which model and prompt version).

Resumable: re-running reads the collection's items, skips any that
already carry an `abstract:*` tag, and processes the rest. The CSV log
is not consulted for resume decisions.

Reads the screening prompt from a per-project `screening_config.py`
(see `${CLAUDE_PLUGIN_ROOT}/templates/screening_config.py`) so the
inclusion criteria, research question, and exclusion codes stay with
the project, not with the plugin. The script is deliberately generic.

Usage:
    uv run abstract_screen.py --group 6015547 --collection ABCDE1234
    uv run abstract_screen.py --group 6015547 --collection ABCDE1234 \\
        --config ./screening_config.py \\
        --search-csv analysis/raw/search_results.csv \\
        --output screening/abstract_screening.csv

Flags: --dry-run (print prompt, no API calls), --sample N (random
subset), --workers N (parallel API calls; default 8),
--csv-backfill (read the CSV log and apply tags for any item with a
decision but no Zotero tag — one-time migration from pre-Zotero-as-truth
deployments; no LLM calls made).
"""

from __future__ import annotations

import argparse
import csv
import random
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import batch_manifest  # noqa: E402
import csv_io  # noqa: E402
import screening_common  # noqa: E402
import zotero_io  # noqa: E402
from core import llm_provider  # noqa: E402
from core.config_loader import require  # noqa: E402
from core.models import (  # noqa: E402
    cost_estimate_line,
    default_for_stage,
    effective_model,
    model_flag_help,
    paid_run_banner,
)
from log_schemas import ABSTRACT_SCREENING_FIELDS  # noqa: E402

# Re-export under the legacy name so any external consumer (or test
# fixture) that imports `abstract_screen.LOG_FIELDS` keeps working.
LOG_FIELDS = ABSTRACT_SCREENING_FIELDS

VALID_DECISIONS = ("include", "borderline", "exclude")


def _load_screening_config(path: str):
    mod = screening_common.load_config_module(
        path, "screening_config", required=("ABSTRACT_SCREENING_SYSTEM_PROMPT",),
    )
    return (
        mod.ABSTRACT_SCREENING_SYSTEM_PROMPT,
        getattr(mod, "ABSTRACT_SCREENING_MODEL", "") or default_for_stage("abstract_screening"),
        getattr(mod, "ABSTRACT_SCREENING_PROMPT_VERSION", ""),
    )


def _format_user_message(title: str, abstract: str, source: str,
                         query: str) -> str:
    parts = [f"TITLE: {title}"]
    parts.append(f"ABSTRACT: {abstract}" if abstract
                 else "ABSTRACT: [not available]")
    parts.append(f"JOURNAL: {source}")
    if query:
        parts.append(f"SEARCH QUERY: {query}")
    return "\n\n".join(parts)


STAGE_TAG_PREFIX = "abstract:"


def parse_decision(text: str) -> tuple[str, str]:
    """`(decision, reason)` from the model's two-line reply.

    **Unparseable output becomes `borderline`, not `error`.** That is a
    deliberate bias, not a fallback: this stage exists to decide what a
    human reads at full text, and a paper the screener could not speak
    about clearly belongs in the read pile. The raw text is kept as the
    reason so the failure is still visible in the log.

    Shared by the synchronous path and the batch applier on purpose. It
    is the single most consequential rule in this file, and a second
    copy is exactly how the two paths would come to disagree about what
    a given model output means.
    """
    decision = "error"
    reason = text
    for line in (text or "").splitlines():
        if line.upper().startswith("DECISION:"):
            decision = line.split(":", 1)[1].strip().lower()
        if line.upper().startswith("REASON:"):
            reason = line.split(":", 1)[1].strip()
    if decision not in VALID_DECISIONS:
        decision = "borderline"
        reason = f"PARSE ERROR — raw: {(text or '')[:200]}"
    return decision, reason


def screening_row(
    item: dict,
    *,
    decision: str,
    reason: str,
    model: str,
    prompt_version: str,
    query: str = "",
    timestamp: str = "",
) -> dict:
    """One CSV row. The only place the log's shape is decided.

    `timestamp` defaults to now, but the batch applier passes the
    response's own `generated_at`: the log records when a decision was
    *made*, not when it was filed, and those can be days apart once a
    manifest is executed elsewhere.
    """
    d = item.get("data", {})
    return {
        "timestamp": timestamp or datetime.now(UTC).isoformat(),
        "item_key": d.get("key", item.get("key", "")),
        "doi": (d.get("DOI") or "").strip(),
        "title": (d.get("title") or "")[:100],
        "source": d.get("publicationTitle", "") or "",
        "query": query,
        "decision": decision,
        "reason": reason,
        "model": model,
        "prompt_version": prompt_version,
    }


def _item_from_manifest_row(row: dict) -> dict:
    """Rebuild the slice of a Zotero item that `screening_row` reads.

    The manifest carries the identity columns precisely so applying does
    not have to re-fetch the library: between emit and apply an item may
    have been edited or moved, and the log should describe the paper as
    it was when the model read it. Zotero is still consulted for tag
    writes — that is state, not provenance.
    """
    return {
        "key": row.get("item_key", ""),
        "data": {
            "key": row.get("item_key", ""),
            "DOI": row.get("doi", ""),
            "title": row.get("title", ""),
            "publicationTitle": row.get("journal", ""),
        },
    }


def build_manifest_rows(
    items: list[dict],
    *,
    run_id: str,
    system_prompt: str,
    model: str,
    prompt_version: str,
    doi_to_query: dict[str, str],
    library: dict,
    collection: str,
    max_input_chars: int = 0,
) -> tuple[list[dict], list[dict]]:
    """`(manifest_rows, skipped)` for a set of items to screen.

    Each row carries its own rendered prompt, so whatever executes the
    manifest needs no access to `screening_config.py`, Zotero, or this
    plugin. `system_sha256` lets the applier notice that the prompt was
    edited between emit and apply — which would otherwise silently make
    the log describe a run that did not happen.
    """
    rows: list[dict] = []
    skipped: list[dict] = []
    for item in items:
        d = item.get("data", {})
        doi = (d.get("DOI") or "").strip()
        user = _format_user_message(
            d.get("title", ""), d.get("abstractNote", "") or "",
            d.get("publicationTitle", "") or "",
            doi_to_query.get(doi.lower(), ""),
        )
        if max_input_chars and len(user) > max_input_chars:
            skipped.append({
                "item_key": item["key"],
                "doi": doi,
                "title": (d.get("title") or "")[:100],
                "skip_reason": "too_long_for_context",
                "detail": (
                    f"{len(user)} chars exceeds --max-input-chars "
                    f"{max_input_chars}"
                ),
            })
            continue
        rows.append({
            "schema_version": batch_manifest.SCHEMA_VERSION,
            "run_id": run_id,
            "request_id": batch_manifest.request_id(run_id, item["key"]),
            "ordinal": 0,
            "item_key": item["key"],
            "stage": batch_manifest.STAGE_ABSTRACT,
            "mode": "screen",
            "library": library,
            "collection": collection,
            "doi": doi,
            "title": (d.get("title") or "")[:100],
            "journal": d.get("publicationTitle", "") or "",
            "year": (d.get("date") or "")[:4],
            "query": doi_to_query.get(doi.lower(), ""),
            "prompt_version": prompt_version,
            "model_hint": model,
            "system": system_prompt,
            "user": user,
            "system_sha256": batch_manifest.sha256(system_prompt),
            "user_sha256": batch_manifest.sha256(user),
            "temperature": 0.0,
            "max_output_tokens": 200,
            "input_chars": len(user),
        })
    return rows, skipped


def _already_tagged(items: list[dict]) -> set[str]:
    """Items that already have any `abstract:*` tag in Zotero — these are
    'done' for resume purposes. Canonical source of truth.

    Prefix match, not an exact-value match: `abstract:borderline` counts as
    decided, so a re-run does not re-screen it."""
    return screening_common.items_with_stage_tag(items, prefix=STAGE_TAG_PREFIX)


def _csv_decisions(path: Path) -> dict[str, str]:
    """Last-decision-per-key map from the CSV log. Used ONLY for
    `--csv-backfill` migration, not for resume decisions.

    Filters *while reading*: a trailing `error` row for an item does not
    displace an earlier valid decision. `fulltext_code` deliberately
    filters after — see the note in `screening_common`."""
    return screening_common.last_decisions_by_key(path, valid=VALID_DECISIONS)


def _run_csv_backfill(
    zot: zotero_io.ZoteroClient,
    coll_items: list[dict],
    output_path: Path,
) -> int:
    """One-time migration: apply abstract:* tags from CSV decisions for
    items that have a CSV decision but no Zotero tag yet. No LLM calls.
    Exits with 0 on success, 1 on partial failure."""
    return screening_common.run_csv_backfill(
        zot,
        coll_items,
        _csv_decisions(output_path),
        prefix=STAGE_TAG_PREFIX,
        label="abstract:*",
    )


def apply_responses(
    zot,
    *,
    manifest_path: Path,
    responses_path: Path,
    output_path: Path,
    tag_batch_size: int,
    force: bool,
    skip_already_tagged: bool,
) -> int:
    """Apply an executed manifest: CSV rows, then Zotero stage tags.

    No LLM is called. Everything written here is derived from the two
    files, which is what lets the generation step happen on a machine
    this one never talks to.
    """
    _, requests = batch_manifest.read_manifest(manifest_path)
    _, responses = batch_manifest.read_responses(responses_path)
    paired, unanswered, orphaned = batch_manifest.join_responses(
        requests, responses,
    )

    record = batch_manifest.load_run_record(
        batch_manifest.run_record_path(responses_path),
    )
    if record:
        batch_manifest.refuse_if_degenerate(record, force=force)

    if orphaned:
        print(
            f"  WARNING: {len(orphaned)} response(s) name requests absent "
            f"from this manifest; ignoring them. Check you paired the "
            f"right two files.",
            flush=True,
        )
    if unanswered:
        print(
            f"  {len(unanswered)} request(s) got no response — left "
            f"untagged so a re-run picks them up.",
            flush=True,
        )

    # A manifest and its application can be days apart. Anything tagged
    # in between was decided by something else, and overwriting it
    # silently would lose that decision.
    tagged_since = _already_tagged(
        zot.collection_items(
            requests[0].get("collection", ""), item_type="journalArticle",
        )
    ) if requests[0].get("collection") else set()
    clashes = [r for r, _ in paired if r["item_key"] in tagged_since]
    if clashes:
        verb = "skipping" if skip_already_tagged else "overwriting"
        print(
            f"  WARNING: {len(clashes)} of {len(paired)} item(s) have been "
            f"tagged since this manifest was emitted; {verb} them.",
            flush=True,
        )

    counts: dict[str, int] = {}
    tag_buffer: list[tuple[str, dict]] = []
    model_seen = ""
    for req, resp in paired:
        if skip_already_tagged and req["item_key"] in tagged_since:
            counts["skipped"] = counts.get("skipped", 0) + 1
            continue
        model_seen = resp.get("model", "") or model_seen
        status = resp.get("call_status", "error")
        if status == "ok":
            _, answer = batch_manifest.split_reasoning(
                resp.get("response_text") or "",
            )
            decision, reason = parse_decision(answer)
        elif status == "truncated":
            # A cut-off answer is a run defect, not a verdict. Recording
            # it as `error` keeps the item untagged and re-runnable.
            decision = "error"
            reason = (
                f"truncated: output hit the "
                f"{req.get('max_output_tokens')}-token budget"
            )
        else:
            decision = "error"
            reason = str(resp.get("error") or f"call_status={status}")[:200]

        row = screening_row(
            _item_from_manifest_row(req),
            decision=decision,
            reason=reason,
            model=resp.get("model", "") or req.get("model_hint", ""),
            prompt_version=req.get("prompt_version", ""),
            query=req.get("query", ""),
            # When the decision was made, not when it was filed.
            timestamp=resp.get("generated_at", ""),
        )
        csv_io.upsert_by_item_key(output_path, row, ABSTRACT_SCREENING_FIELDS)
        counts[decision] = counts.get(decision, 0) + 1
        if decision in VALID_DECISIONS:
            tag_buffer.append((req["item_key"], _stage_tag_op(decision)))
            if len(tag_buffer) >= max(1, tag_batch_size):
                _flush_tag_buffer(zot, tag_buffer)
    _flush_tag_buffer(zot, tag_buffer)

    # Units that never reached the model still owe the log a row.
    sidecar = batch_manifest.read_skipped(
        batch_manifest.skipped_path(manifest_path),
    )
    for s in sidecar.get("skipped", []):
        if s.get("skip_reason") not in batch_manifest.SKIP_REASONS_WITH_ROWS:
            continue
        row = screening_row(
            _item_from_manifest_row(s),
            decision="error",
            reason=f"{s['skip_reason']}: {s.get('detail', '')}"[:200],
            model=model_seen,
            prompt_version="",
        )
        csv_io.upsert_by_item_key(output_path, row, ABSTRACT_SCREENING_FIELDS)
        counts[s["skip_reason"]] = counts.get(s["skip_reason"], 0) + 1

    print(f"\n{'=' * 60}")
    # Not `len(paired)`: with --skip-already-tagged that number can be
    # every response in the file while nothing was written at all.
    print(
        f"Applied {len(paired) - counts.get('skipped', 0)} of "
        f"{len(paired)} response(s) from {responses_path.name}."
    )
    for k in sorted(counts):
        print(f"  {k}: {counts[k]}")
    if unanswered:
        print(f"  unanswered: {len(unanswered)}")
    print(f"Log: {output_path}")
    return 0


def _stage_tag_op(decision: str) -> dict:
    """The `batch_update_tags` / `update_tags` op that records a stage
    decision: add `abstract:<decision>`, clearing any prior `abstract:*`."""
    return screening_common.stage_tag_op(STAGE_TAG_PREFIX, decision)


def _flush_tag_buffer(zot, buffer: list[tuple[str, dict]]) -> dict[str, int]:
    """Apply and clear a buffer of `(item_key, op)` stage-tag writes via a
    single multi-item PATCH (R6 steady-state batching).

    Failed writes leave those items untagged — Zotero tags are the resume
    source of truth, so a re-run re-screens any item whose tag did not
    land. A warning is printed at batch granularity since per-item failure
    annotation isn't available on the batch path.
    """
    if not buffer:
        return {"applied": 0, "unchanged": 0, "failed": 0}
    stats = zot.batch_update_tags(buffer)
    if stats.get("failed"):
        print(
            f"  WARNING: {stats['failed']} tag write(s) failed this batch; "
            f"those items stay untagged and a re-run will re-screen them.",
            flush=True,
        )
    buffer.clear()
    return stats


def _load_doi_to_query(search_csv: Path | None) -> dict[str, str]:
    if not search_csv or not search_csv.exists():
        return {}
    doi_to_query: dict[str, str] = {}
    with search_csv.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            doi = (row.get("doi") or "").strip().lower()
            if doi:
                doi_to_query[doi] = row.get("query", "")
    return doi_to_query


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="./screening_config.py",
                        help="Path to screening_config.py (default: "
                             "./screening_config.py).")
    zotero_io.add_library_args(parser)
    parser.add_argument("--collection", required=True,
                        help="Zotero collection key to screen.")
    parser.add_argument("--search-csv", default="",
                        help="Optional: search_results.csv for query provenance.")
    parser.add_argument("--output", default="screening/abstract_screening.csv",
                        help="Append-only log path "
                             "(default: screening/abstract_screening.csv).")
    parser.add_argument("--model", default="",
                        help=model_flag_help(
                            "ABSTRACT_SCREENING_MODEL from screening_config.py, "
                            "else the configured provider's fast tier"))
    parser.add_argument("--dry-run", action="store_true",
                        help="Print first item's prompt; no API calls.")
    parser.add_argument("--sample", type=int, default=0,
                        help="Screen a random sample of N items (0 = all).")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel API workers (default: 8).")
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip the one-request check that the provider "
                             "answers for this model. The check costs ~4 "
                             "tokens and catches a spent quota or a dead key "
                             "before the run; skip it only if you have "
                             "already run check_model_connection.py.")
    parser.add_argument("--tag-batch-size", type=int, default=50,
                        help="Write abstract:* tags to Zotero in batches of "
                             "this many decisions via one multi-item PATCH "
                             "(default: 50) — fewer API calls and less 412 "
                             "pressure than per-item writes. Use 1 for strict "
                             "per-item writes. Failed tag writes leave items "
                             "untagged; a re-run re-screens them.")
    parser.add_argument("--emit-manifest", default="",
                        help="Assemble the requests this run would send and "
                             "write them to PATH as JSONL (plus a "
                             ".skipped.json sidecar), then exit. Makes no LLM "
                             "calls. Execute the manifest anywhere, then feed "
                             "the responses back with --apply-responses. "
                             "A .gz suffix compresses it.")
    parser.add_argument("--apply-responses", default="",
                        help="Apply an executed manifest's responses: write "
                             "CSV rows and Zotero abstract:* tags. Requires "
                             "--emit-manifest's file too, via --manifest. "
                             "Makes no LLM calls.")
    parser.add_argument("--manifest", default="",
                        help="The manifest that --apply-responses's file "
                             "answers. Needed because the responses carry "
                             "only decisions; the requests carry identity.")
    parser.add_argument("--max-input-chars", type=int, default=0,
                        help="Send no request whose user message exceeds N "
                             "characters; over-long items go to the skipped "
                             "sidecar rather than being silently truncated "
                             "(0 = no limit).")
    parser.add_argument("--force-apply", action="store_true",
                        help="Apply responses even when the run record marks "
                             "the run degenerate. Prints why that is a bad "
                             "idea first.")
    parser.add_argument("--skip-already-tagged", action="store_true",
                        help="On apply, skip items tagged in Zotero since the "
                             "manifest was emitted, rather than overwriting "
                             "their decision.")
    parser.add_argument("--csv-backfill", action="store_true",
                        help="One-time migration from pre-Zotero-as-truth "
                             "deployments: read CSV decisions and apply "
                             "matching abstract:* tags for items that don't "
                             "have one yet. Makes no LLM calls; exits after.")
    args = parser.parse_args()

    system_prompt, config_model, prompt_version = _load_screening_config(args.config)
    # Resolve before the provider pre-flight below — that branches on the
    # model name to decide which API key to require.
    model = effective_model(
        args.model, config_model, stage="ABSTRACT_SCREENING_MODEL",
    )

    # A dry run needs no Zotero credential to *write* with — but it
    # still has to read the collection, and `--remote` reads go to
    # api.zotero.org, which rejects an empty key with 403. That
    # combination died before printing anything, so the cost quote the
    # dry run exists to produce never appeared and the run went ahead
    # unpriced. Load the key whenever the reads are remote.
    needs_key = not args.dry_run or getattr(args, "remote", False)
    api_key = require("zotero", "api_key",
                      env="ZOTERO_API_KEY") if needs_key else ""
    batch_mode = bool(args.emit_manifest or args.apply_responses)
    if not (args.dry_run or args.csv_backfill or batch_mode):
        llm_provider.require_credentials(model)
        # A present key is not a working one. One ~4-token request here
        # separates "valid key, spent quota" from "bad key" from "model
        # ID the provider does not serve" — in about a second, before
        # any per-item spend.
        if not args.skip_preflight:
            llm_provider.preflight_or_exit(model)


    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    search_csv = Path(args.search_csv) if args.search_csv else None
    doi_to_query = _load_doi_to_query(search_csv)

    zot = zotero_io.ZoteroClient.from_args(args, api_key=api_key or "dummy")
    print(f"Fetching items from Zotero ({zot.describe_library()}, "
          f"collection={args.collection})...", flush=True)
    coll_items = zot.collection_items(args.collection, item_type="journalArticle")
    print(f"  {len(coll_items)} items in collection", flush=True)

    if args.csv_backfill:
        return _run_csv_backfill(zot, coll_items, output_path)

    if args.apply_responses:
        if not args.manifest:
            sys.exit(
                "ERROR: --apply-responses needs --manifest too. The "
                "responses carry decisions; the manifest carries which "
                "item each decision belongs to."
            )
        return apply_responses(
            zot,
            manifest_path=Path(args.manifest),
            responses_path=Path(args.apply_responses),
            output_path=output_path,
            tag_batch_size=args.tag_batch_size,
            force=args.force_apply,
            skip_already_tagged=args.skip_already_tagged,
        )

    tagged = _already_tagged(coll_items)
    to_screen = [it for it in coll_items if it["key"] not in tagged]
    print(f"  Already tagged (abstract:*): {len(tagged)}, remaining: "
          f"{len(to_screen)}", flush=True)

    # Warn on tag/CSV drift: items with CSV decisions but no matching tag.
    csv_done = set(_csv_decisions(output_path).keys())
    drift = csv_done - tagged
    if drift:
        print(
            f"  WARNING: {len(drift)} item(s) in CSV log lack "
            f"abstract:* tags in Zotero. Run with --csv-backfill to "
            f"apply tags from CSV decisions.",
            flush=True,
        )

    if args.sample and args.sample < len(to_screen):
        to_screen = random.sample(to_screen, args.sample)
        print(f"  Sampling {args.sample} items", flush=True)

    if not to_screen:
        print("Nothing to screen.", flush=True)
        return 0

    if args.emit_manifest:
        run_id = batch_manifest.new_run_id(batch_manifest.STAGE_ABSTRACT)
        manifest_path = Path(args.emit_manifest)
        rows, skipped = build_manifest_rows(
            to_screen,
            run_id=run_id,
            system_prompt=system_prompt,
            model=model,
            prompt_version=prompt_version,
            doi_to_query=doi_to_query,
            library=zot.library_ref(),
            collection=args.collection,
            max_input_chars=args.max_input_chars,
        )
        if not rows:
            sys.exit(
                "ERROR: every candidate was skipped; nothing to emit. See "
                f"{batch_manifest.skipped_path(manifest_path)} for why."
            )
        batch_manifest.write_manifest(manifest_path, rows)
        sidecar = batch_manifest.write_skipped(
            batch_manifest.skipped_path(manifest_path),
            run_id=run_id,
            stage=batch_manifest.STAGE_ABSTRACT,
            skipped=skipped,
            n_requested=len(rows),
            selection={
                "collection": args.collection,
                "library": zot.library_ref(),
                "already_tagged": len(tagged),
                "sample": args.sample,
                "max_input_chars": args.max_input_chars,
                "model_hint": model,
                "prompt_version": prompt_version,
            },
        )
        print(f"\nWrote {len(rows)} request(s) to {manifest_path}")
        print(f"Skipped {len(skipped)}; reasons in {sidecar}")
        print(f"run_id: {run_id}")
        print(
            "\nExecute it wherever the compute is, then:\n"
            f"  --apply-responses <responses.jsonl> --manifest {manifest_path}",
        )
        return 0

    if args.dry_run:
        d = to_screen[0].get("data", {})
        msg = _format_user_message(
            d.get("title", ""), d.get("abstractNote", ""),
            d.get("publicationTitle", ""),
            doi_to_query.get((d.get("DOI") or "").lower(), ""),
        )
        print("\n=== SYSTEM PROMPT ===")
        print(system_prompt)
        print("\n=== USER MESSAGE (first item) ===")
        print(msg)
        print(f"\n[DRY RUN] Would screen {len(to_screen)} items with {model}",
              flush=True)
        print(cost_estimate_line(
            model, stage="abstract_screening", n_items=len(to_screen),
        ), flush=True)
        return 0

    print(paid_run_banner(
        model, stage="abstract_screening", n_items=len(to_screen),
    ), flush=True)

    client = llm_provider.get_provider(model)
    # Schema-stable + idempotent writes via csv_io.upsert_by_item_key.
    # Re-running on the same item replaces the prior row instead of
    # appending, so partial-then-resumed screening passes don't double
    # up. Lock guards file rewrite (upsert reads → mutates → renames).
    log_lock = threading.Lock()

    counts: dict[str, int] = {k: 0 for k in (*VALID_DECISIONS, "error")}
    done_count = 0
    total = len(to_screen)
    #: Non-retryable verdicts seen by the workers. A spent quota or a
    #: rejected key fails every remaining item identically, so the run
    #: stops and says so once instead of grinding out N error rows.
    fatal: list = []

    def screen_one(item: dict) -> tuple[str, str, str, str, str, str, str]:
        d = item.get("data", {})
        key = d.get("key", item.get("key", ""))
        doi = (d.get("DOI") or "").strip()
        title = (d.get("title") or "")[:100]
        source = d.get("publicationTitle", "") or ""
        abstract = d.get("abstractNote", "") or ""
        query = doi_to_query.get(doi.lower(), "")

        msg = _format_user_message(d.get("title", ""), abstract, source, query)

        try:
            text = client.generate(
                model=model,
                max_tokens=200,
                temperature=0.0,
                system=system_prompt,
                prompt=msg,
            )
            decision, reason = parse_decision(text)
        except Exception as e:
            # Classify rather than stringify. A run that hits a spent
            # quota fails every remaining item the same way, and the
            # useful output is one clear diagnosis, not N copies of an
            # SDK repr.
            verdict = llm_provider.classify_failure(e)
            decision = "error"
            reason = f"{verdict.status.value}: {verdict.detail}"[:200]
            if not verdict.retryable:
                fatal.append(verdict)

        # The stage tag is applied by the main loop, batched (R6). The
        # CSV row is written first there, so a tag write that never lands
        # is recoverable: Zotero tags are the resume source of truth and a
        # re-run re-screens any item left untagged.
        return key, doi, title, source, query, decision, reason

    print(f"Screening with {args.workers} parallel workers (model={model})...",
          flush=True)

    # `screening_row` builds a row from the item, so the loop needs the
    # item back from the key its worker returned.
    by_key = {it["key"]: it for it in to_screen}

    # Stage-tag writes are buffered and flushed via one multi-item PATCH
    # every `tag_batch_size` decisions, instead of one PATCH per item.
    tag_batch_size = max(1, args.tag_batch_size)
    tag_buffer: list[tuple[str, dict]] = []

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(screen_one, item): item for item in to_screen}
        try:
            for future in as_completed(futures):
                key, doi, title, source, query, decision, reason = future.result()
                done_count += 1
                counts[decision] = counts.get(decision, 0) + 1

                row = screening_row(
                    by_key[key], decision=decision, reason=reason,
                    model=model, prompt_version=prompt_version, query=query,
                )
                # CSV first (source of truth), then enqueue the tag write.
                with log_lock:
                    csv_io.upsert_by_item_key(
                        output_path, row, ABSTRACT_SCREENING_FIELDS)

                if decision in VALID_DECISIONS:
                    tag_buffer.append((key, _stage_tag_op(decision)))
                    if len(tag_buffer) >= tag_batch_size:
                        _flush_tag_buffer(zot, tag_buffer)

                print(f"[{done_count}/{total}] {title[:70]:<70} → {decision}",
                      flush=True)

                if fatal:
                    # Cancel what has not started. Items already in
                    # flight finish and get logged; nothing new is sent
                    # to a provider that has told us it will refuse.
                    for pending in futures:
                        pending.cancel()
                    print("", flush=True)
                    print(fatal[0].format(), file=sys.stderr, flush=True)
                    print(
                        f"\nStopped after {done_count} of {total} items. "
                        f"Decisions already made are in {output_path} and "
                        f"tagged in Zotero — re-run after fixing the above "
                        f"and screening resumes from there.",
                        file=sys.stderr, flush=True,
                    )
                    break
        finally:
            # Flush whatever is left, including on Ctrl+C — partial progress
            # gets tagged so a resume doesn't re-screen already-decided items.
            _flush_tag_buffer(zot, tag_buffer)

    print(f"\n{'=' * 60}")
    print(f"Done. Screened {done_count} of {total} items.")
    for k in (*VALID_DECISIONS, "error"):
        print(f"  {k}: {counts.get(k, 0)}")
    print(f"Log: {output_path}")
    # A run cut short by a dead provider must not exit 0 — the caller is
    # usually a skill, and a zero here reads as "screening complete".
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
