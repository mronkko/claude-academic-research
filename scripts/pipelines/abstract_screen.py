#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic>=0.40",
#     "pyzotero>=1.6",
#     "tenacity>=8.0",
#     "httpx>=0.25",
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
import importlib.util
import os
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

from core.config_loader import require  # noqa: E402

try:
    import anthropic
except ImportError:
    sys.exit(
        "ERROR: dependencies not available. Run via `uv run`; the PEP 723 "
        "block at the top declares anthropic + pyzotero."
    )

import csv_io  # noqa: E402
import zotero_io  # noqa: E402
from log_schemas import ABSTRACT_SCREENING_FIELDS  # noqa: E402

# Re-export under the legacy name so any external consumer (or test
# fixture) that imports `abstract_screen.LOG_FIELDS` keeps working.
LOG_FIELDS = ABSTRACT_SCREENING_FIELDS

VALID_DECISIONS = ("include", "borderline", "exclude")


def _load_screening_config(path: str):
    spec = importlib.util.spec_from_file_location("screening_config", path)
    assert spec is not None and spec.loader is not None, (
        f"cannot load screening config: {path}"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, "ABSTRACT_SCREENING_SYSTEM_PROMPT"):
        sys.exit(f"ERROR: {path} is missing `ABSTRACT_SCREENING_SYSTEM_PROMPT`.")
    return (
        mod.ABSTRACT_SCREENING_SYSTEM_PROMPT,
        getattr(mod, "ABSTRACT_SCREENING_MODEL", "claude-haiku-4-5-20251001"),
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


def _already_tagged(items: list[dict]) -> set[str]:
    """Items that already have any `abstract:*` tag in Zotero — these are
    'done' for resume purposes. Canonical source of truth."""
    done: set[str] = set()
    for it in items:
        tags = {
            t.get("tag", "")
            for t in it.get("data", {}).get("tags", [])
        }
        if any(t.startswith(STAGE_TAG_PREFIX) for t in tags):
            done.add(it["key"])
    return done


def _csv_decisions(path: Path) -> dict[str, str]:
    """Last-decision-per-key map from the CSV log. Used ONLY for
    `--csv-backfill` migration, not for resume decisions."""
    if not path.exists():
        return {}
    latest: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            k = row.get("item_key")
            d = row.get("decision", "")
            if k and d in VALID_DECISIONS:
                latest[k] = d
    return latest


def _run_csv_backfill(
    zot: zotero_io.ZoteroClient,
    coll_items: list[dict],
    output_path: Path,
) -> int:
    """One-time migration: apply abstract:* tags from CSV decisions for
    items that have a CSV decision but no Zotero tag yet. No LLM calls.
    Exits with 0 on success, 1 on partial failure."""
    tagged = _already_tagged(coll_items)
    csv_done = _csv_decisions(output_path)
    drift = {k: d for k, d in csv_done.items() if k not in tagged}

    if not drift:
        print("Nothing to backfill — all CSV-decided items already have "
              "abstract:* tags in Zotero.", flush=True)
        return 0

    print(f"Backfilling abstract:* tags for {len(drift)} item(s) "
          f"(batched)...", flush=True)
    updates = [
        (
            key,
            {
                "add": [f"{STAGE_TAG_PREFIX}{decision}"],
                "remove_prefixed": [STAGE_TAG_PREFIX],
            },
        )
        for key, decision in drift.items()
    ]
    stats = zot.batch_update_tags(updates)
    print(
        f"Backfill complete: {stats['applied']} tagged, "
        f"{stats['unchanged']} unchanged, {stats['failed']} failed.",
        flush=True,
    )
    return 0 if stats["failed"] == 0 else 1


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
    parser.add_argument("--dry-run", action="store_true",
                        help="Print first item's prompt; no API calls.")
    parser.add_argument("--sample", type=int, default=0,
                        help="Screen a random sample of N items (0 = all).")
    parser.add_argument("--workers", type=int, default=8,
                        help="Parallel API workers (default: 8).")
    parser.add_argument("--csv-backfill", action="store_true",
                        help="One-time migration from pre-Zotero-as-truth "
                             "deployments: read CSV decisions and apply "
                             "matching abstract:* tags for items that don't "
                             "have one yet. Makes no LLM calls; exits after.")
    args = parser.parse_args()

    api_key = "" if args.dry_run else require("zotero", "api_key",
                                              env="ZOTERO_API_KEY")
    if not args.dry_run and not args.csv_backfill and not os.environ.get("ANTHROPIC_API_KEY"):
        require("anthropic", "api_key", env="ANTHROPIC_API_KEY")
        # config_loader.require raised SystemExit already if missing

    system_prompt, model, prompt_version = _load_screening_config(args.config)

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
        return 0

    client = anthropic.Anthropic()
    # Schema-stable + idempotent writes via csv_io.upsert_by_item_key.
    # Re-running on the same item replaces the prior row instead of
    # appending, so partial-then-resumed screening passes don't double
    # up. Lock guards file rewrite (upsert reads → mutates → renames).
    log_lock = threading.Lock()

    counts: dict[str, int] = {k: 0 for k in (*VALID_DECISIONS, "error")}
    done_count = 0
    total = len(to_screen)

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
            resp = client.messages.create(
                model=model,
                max_tokens=200,
                temperature=0,
                system=system_prompt,
                messages=[{"role": "user", "content": msg}],
            )
            text = resp.content[0].text.strip()
            decision = "error"
            reason = text
            for line in text.splitlines():
                if line.upper().startswith("DECISION:"):
                    decision = line.split(":", 1)[1].strip().lower()
                if line.upper().startswith("REASON:"):
                    reason = line.split(":", 1)[1].strip()
            if decision not in VALID_DECISIONS:
                decision = "borderline"
                reason = f"PARSE ERROR — raw: {text[:200]}"
        except Exception as e:
            decision = "error"
            reason = str(e)[:200]

        # Apply stage tag to Zotero. Non-fatal — if tag write fails
        # (network, version conflict after retries), the decision still
        # lands in the CSV and a subsequent re-run will detect the
        # untagged item and re-screen it.
        if decision in VALID_DECISIONS:
            try:
                zot.update_tags(
                    key,
                    add=[f"{STAGE_TAG_PREFIX}{decision}"],
                    remove_prefixed=[STAGE_TAG_PREFIX],
                )
            except Exception as tag_exc:  # noqa: BLE001
                reason = f"{reason} [TAG WRITE FAILED: {tag_exc}]"[:400]

        return key, doi, title, source, query, decision, reason

    print(f"Screening with {args.workers} parallel workers (model={model})...",
          flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(screen_one, item): item for item in to_screen}
        for future in as_completed(futures):
            key, doi, title, source, query, decision, reason = future.result()
            done_count += 1
            counts[decision] = counts.get(decision, 0) + 1

            row = {
                "timestamp": datetime.now(UTC).isoformat(),
                "item_key": key,
                "doi": doi,
                "title": title,
                "source": source,
                "query": query,
                "decision": decision,
                "reason": reason,
                "model": model,
                "prompt_version": prompt_version,
            }
            with log_lock:
                csv_io.upsert_by_item_key(output_path, row, ABSTRACT_SCREENING_FIELDS)

            print(f"[{done_count}/{total}] {title[:70]:<70} → {decision}",
                  flush=True)

    print(f"\n{'=' * 60}")
    print(f"Done. Screened {total} items.")
    for k in (*VALID_DECISIONS, "error"):
        print(f"  {k}: {counts.get(k, 0)}")
    print(f"Log: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
