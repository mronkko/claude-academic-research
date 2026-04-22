#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic>=0.40",
#     "pyzotero>=1.6",
# ]
# ///
"""LLM-driven title+abstract screening for a systematic review.

Reads items from a Zotero collection, screens each title+abstract via
Claude Haiku (configurable; temperature=0), and writes decisions to an
append-only CSV log. Resumable: re-running skips items already logged.

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
subset), --workers N (parallel API calls; default 8).
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

import zotero_io  # noqa: E402


LOG_FIELDS = [
    "timestamp", "item_key", "doi", "title", "source", "query",
    "decision", "reason", "model", "prompt_version",
]

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


def _load_already_screened(path: Path) -> set[str]:
    if not path.exists():
        return set()
    keys: set[str] = set()
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            k = row.get("item_key")
            if k:
                keys.add(k)
    return keys


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
    parser.add_argument("--group", default=os.environ.get("ZOTERO_GROUP", ""),
                        help="Zotero group ID (default: $ZOTERO_GROUP).")
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
    args = parser.parse_args()

    if not args.group:
        sys.exit("ERROR: --group required (or set ZOTERO_GROUP).")

    api_key = "" if args.dry_run else require("zotero", "api_key",
                                              env="ZOTERO_API_KEY")
    if not args.dry_run and not os.environ.get("ANTHROPIC_API_KEY"):
        require("anthropic", "api_key", env="ANTHROPIC_API_KEY")
        # config_loader.require raised SystemExit already if missing

    system_prompt, model, prompt_version = _load_screening_config(args.config)

    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    search_csv = Path(args.search_csv) if args.search_csv else None
    doi_to_query = _load_doi_to_query(search_csv)

    print(f"Fetching items from Zotero (group={args.group}, "
          f"collection={args.collection})...", flush=True)
    zot = zotero_io.ZoteroClient(api_key=api_key or "dummy", group_id=args.group)
    coll_items = zot.collection_items(args.collection, item_type="journalArticle")
    print(f"  {len(coll_items)} items in collection", flush=True)

    already = _load_already_screened(output_path)
    to_screen = [it for it in coll_items if it["key"] not in already]
    print(f"  Already screened: {len(already)}, remaining: {len(to_screen)}",
          flush=True)

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
    log_fh = output_path.open("a", newline="", encoding="utf-8")
    writer = csv.DictWriter(log_fh, fieldnames=LOG_FIELDS)
    if output_path.stat().st_size == 0:
        writer.writeheader()
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

        return key, doi, title, source, query, decision, reason

    print(f"Screening with {args.workers} parallel workers (model={model})...",
          flush=True)

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(screen_one, item): item for item in to_screen}
        for future in as_completed(futures):
            key, doi, title, source, query, decision, reason = future.result()
            done_count += 1
            counts[decision] = counts.get(decision, 0) + 1

            with log_lock:
                writer.writerow({
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
                })
                log_fh.flush()

            print(f"[{done_count}/{total}] {title[:70]:<70} → {decision}",
                  flush=True)

    log_fh.close()

    print(f"\n{'=' * 60}")
    print(f"Done. Screened {total} items.")
    for k in (*VALID_DECISIONS, "error"):
        print(f"  {k}: {counts.get(k, 0)}")
    print(f"Log: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
