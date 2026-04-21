#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic>=0.40",
#     "pyzotero>=1.6",
#     "pdfplumber>=0.10",
#     "pypdf>=4.0",
# ]
# ///
"""LLM-driven full-text screening + structured coding for an SLR.

Reads items from a Zotero collection (typically those marked `include`
or `borderline` at the abstract stage), locates each paper's PDF
attachment, extracts the full text (pdfplumber with pypdf fallback),
then passes title + full text to Claude Sonnet for a single decision
(`include` / `exclude`) plus extraction of the coding fields declared
in the project's `screening_config.py`.

Writes an append-only CSV with the dynamically-sized schema derived
from `FULLTEXT_CODING_FIELDS`. Resumable: re-running skips items whose
last logged decision is `include` or `exclude`; `--rerun` reprocesses
`error` rows; `--full-recode` backs up the log and rebuilds.

The prompt, model, and coding schema all come from the per-project
config — the plugin's copy of this script is deliberately generic.

Usage:
    uv run fulltext_code.py --group 6015547 --collection ABCDE1234 \\
        --config ./screening_config.py --pdf-dir ./pdfs \\
        --output screening/fulltext_screening.csv

Common flags: --dry-run (print first prompt, no API calls),
--limit N, --only-keys K1,K2,..., --workers N, --rerun, --full-recode.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import importlib.util
import os
import shutil
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from core.config_loader import require  # noqa: E402
from core.llm import (  # noqa: E402
    extract_json_from_response,
    extract_pdf_text,
)

try:
    import anthropic
    from pyzotero import zotero
except ImportError:
    sys.exit(
        "ERROR: dependencies not available. Run via `uv run`; the PEP 723 "
        "block at the top declares anthropic + pyzotero + pdfplumber + pypdf."
    )


# Soft cap on full-text chars sent to Sonnet (~180k tokens at 4 chars/token;
# leaves headroom for prompt + response in Sonnet's 200k context).
SOFT_FULLTEXT_CHAR_CAP = 720_000
PLACEHOLDER = "{coding_fields_json_placeholder}"


def _load_screening_config(path: str):
    spec = importlib.util.spec_from_file_location("screening_config", path)
    assert spec is not None and spec.loader is not None, (
        f"cannot load screening config: {path}"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    for attr in ("FULLTEXT_CODING_SYSTEM_PROMPT", "FULLTEXT_CODING_FIELDS"):
        if not hasattr(mod, attr):
            sys.exit(f"ERROR: {path} is missing `{attr}`.")
    if not isinstance(mod.FULLTEXT_CODING_FIELDS, list):
        sys.exit("ERROR: FULLTEXT_CODING_FIELDS must be a list of dicts.")
    for field in mod.FULLTEXT_CODING_FIELDS:
        if "name" not in field:
            sys.exit("ERROR: every FULLTEXT_CODING_FIELDS entry needs `name`.")
    return (
        mod.FULLTEXT_CODING_SYSTEM_PROMPT,
        mod.FULLTEXT_CODING_FIELDS,
        getattr(mod, "FULLTEXT_CODING_MODEL", "claude-sonnet-4-6"),
        getattr(mod, "FULLTEXT_CODING_PROMPT_VERSION", ""),
    )


def _render_prompt(template: str, fields: list[dict]) -> str:
    """Substitute the coding-fields JSON placeholder into the prompt template."""
    if PLACEHOLDER not in template:
        return template
    # Build the JSON-schema fragment Claude should return
    lines = [f'  "{f["name"]}": "<...>"' + ("," if i + 1 < len(fields) else "")
             for i, f in enumerate(fields)]
    json_block = "\n".join(lines)
    # Also render a brief "fields with descriptions" guide at the end
    guide_lines = []
    for f in fields:
        desc = f.get("description", "").strip().replace("\n", " ")
        guide_lines.append(f"- **{f['name']}**: {desc}")
    guide = "\n".join(guide_lines)
    return template.replace(PLACEHOLDER, json_block) + (
        f"\n\nField descriptions:\n{guide}" if guide_lines else ""
    )


# ---------------------------------------------------------------------------
# CSV schema
# ---------------------------------------------------------------------------


def _csv_columns(coding_fields: list[dict]) -> list[str]:
    base = [
        "timestamp", "item_key", "doi", "title", "year", "journal",
        "pdf_path", "fulltext_chars", "truncated",
        "decision", "exclusion_code", "reason",
    ]
    return base + [f["name"] for f in coding_fields] + [
        "model", "prompt_version",
    ]


def _load_last_decisions(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    last: dict[str, str] = {}
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            k = row.get("item_key")
            if k:
                last[k] = row.get("decision", "")
    return last


# ---------------------------------------------------------------------------
# Zotero helpers
# ---------------------------------------------------------------------------


def _find_pdf_path(item: dict, attachments_by_parent: dict[str, list[dict]],
                  pdf_dir: Path) -> Path | None:
    """Find the best PDF path for an item: attachment path or DOI-named file."""
    key = item["key"]
    d = item.get("data", {})
    doi = (d.get("DOI") or "").strip()
    atts = attachments_by_parent.get(key, [])
    pdfs = [a for a in atts if a.get("data", {}).get("contentType") == "application/pdf"
            and a.get("data", {}).get("md5")]
    if pdfs:
        # pyzotero stores attachment files under the profile; resolve to local path
        att_data = pdfs[0].get("data", {})
        filename = att_data.get("filename", "")
        # Zotero's local storage convention: look under pdf_dir for matching file
        if filename:
            candidate = pdf_dir / filename
            if candidate.exists():
                return candidate
    # Fallback: look for DOI-named PDF in pdf_dir
    if doi:
        candidate = pdf_dir / (doi.replace("/", "_") + ".pdf")
        if candidate.exists():
            return candidate
    return None


# ---------------------------------------------------------------------------
# Code one paper
# ---------------------------------------------------------------------------


def _code_one(item: dict, pdf_path: Path, client, model: str, prompt: str,
              fields: list[dict]) -> dict:
    d = item.get("data", {})
    title = (d.get("title") or "").strip()
    fulltext = extract_pdf_text(str(pdf_path))
    truncated = len(fulltext) > SOFT_FULLTEXT_CHAR_CAP
    if truncated:
        fulltext = fulltext[:SOFT_FULLTEXT_CHAR_CAP]
    user_msg = f"TITLE: {title}\n\nFULL TEXT:\n{fulltext}"

    row: dict = {
        "item_key": d.get("key", item.get("key", "")),
        "doi": (d.get("DOI") or "").strip(),
        "title": title[:200],
        "year": (d.get("date") or "")[:4],
        "journal": d.get("publicationTitle", "") or "",
        "pdf_path": str(pdf_path),
        "fulltext_chars": len(fulltext),
        "truncated": "true" if truncated else "false",
        "model": model,
    }
    for f in fields:
        row[f["name"]] = ""

    try:
        resp = client.messages.create(
            model=model,
            max_tokens=3500,
            temperature=0,
            system=prompt,
            messages=[{"role": "user", "content": user_msg}],
        )
        text = resp.content[0].text.strip()
        parsed = extract_json_from_response(text)
        if not parsed:
            row["decision"] = "error"
            row["reason"] = f"JSON PARSE ERROR — raw: {text[:300]}"
            return row
        row["decision"] = str(parsed.get("decision", "")).strip().lower()
        row["exclusion_code"] = str(parsed.get("exclusion_code", "")).strip()
        row["reason"] = str(parsed.get("reason", "")).strip()
        for f in fields:
            val = parsed.get(f["name"], "")
            if isinstance(val, (list, dict)):
                import json as _json
                val = _json.dumps(val, ensure_ascii=False)
            row[f["name"]] = str(val).strip()
        if row["decision"] not in ("include", "exclude"):
            row["decision"] = "error"
            row["reason"] = f"invalid decision value: {parsed.get('decision')!r}"
    except Exception as e:
        row["decision"] = "error"
        row["reason"] = str(e)[:300]

    return row


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="./screening_config.py",
                        help="Path to screening_config.py (default: "
                             "./screening_config.py).")
    parser.add_argument("--group", default=os.environ.get("ZOTERO_GROUP", ""),
                        help="Zotero group ID (default: $ZOTERO_GROUP).")
    parser.add_argument("--collection", required=True,
                        help="Zotero collection key whose items to code.")
    parser.add_argument("--pdf-dir", required=True,
                        help="Directory containing the PDFs for this project.")
    parser.add_argument("--output", default="screening/fulltext_screening.csv",
                        help="Append-only log path.")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print first rendered prompt; no API calls.")
    parser.add_argument("--limit", type=int, default=0,
                        help="Process at most N items (0 = all).")
    parser.add_argument("--only-keys", default="",
                        help="Comma-separated Zotero item keys to process "
                             "(overrides collection enumeration).")
    parser.add_argument("--rerun", action="store_true",
                        help="Re-process items whose last logged decision is "
                             "`error`.")
    parser.add_argument("--full-recode", action="store_true",
                        help="Back up the log file and re-code everything.")
    parser.add_argument("--workers", type=int, default=5,
                        help="Parallel API workers (default: 5; Sonnet has "
                             "tighter rate limits than Haiku).")
    args = parser.parse_args()

    if not args.group:
        sys.exit("ERROR: --group required (or set ZOTERO_GROUP).")

    prompt_template, fields, model, prompt_version = _load_screening_config(
        args.config)
    rendered_prompt = _render_prompt(prompt_template, fields)
    csv_columns = _csv_columns(fields)

    api_key = "" if args.dry_run else require("zotero", "api_key",
                                              env="ZOTERO_API_KEY")
    if not args.dry_run:
        require("anthropic", "api_key", env="ANTHROPIC_API_KEY")

    pdf_dir = Path(args.pdf_dir)
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.full_recode and output_path.exists():
        backup = output_path.with_suffix(".bak-" + datetime.now(
            UTC).strftime("%Y%m%dT%H%M%SZ"))
        shutil.copy2(output_path, backup)
        output_path.unlink()
        print(f"Backed up existing log to {backup}; rebuilding.", flush=True)

    print(f"Fetching items from Zotero (group={args.group}, "
          f"collection={args.collection})...", flush=True)
    local = zotero.Zotero(args.group, "group", api_key or "dummy", local=True)
    items = local.everything(
        local.collection_items(args.collection, itemType="journalArticle")
    )
    attachments = local.everything(local.items(itemType="attachment"))
    atts_by_parent: dict[str, list[dict]] = {}
    for a in attachments:
        p = a.get("data", {}).get("parentItem")
        if p:
            atts_by_parent.setdefault(p, []).append(a)
    print(f"  {len(items)} items, {len(attachments)} attachments", flush=True)

    if args.only_keys:
        wanted = {k.strip() for k in args.only_keys.split(",") if k.strip()}
        items = [it for it in items if it["key"] in wanted]

    last = _load_last_decisions(output_path)
    to_code: list[dict] = []
    for it in items:
        last_decision = last.get(it["key"], "")
        if last_decision in ("include", "exclude") and not args.full_recode:
            continue
        if last_decision == "error" and not (args.rerun or args.full_recode):
            continue
        to_code.append(it)

    if args.limit and args.limit < len(to_code):
        to_code = to_code[:args.limit]

    print(f"  To code: {len(to_code)} items", flush=True)
    if not to_code:
        print("Nothing to code.", flush=True)
        return 0

    if args.dry_run:
        first = to_code[0].get("data", {})
        print("\n=== RENDERED SYSTEM PROMPT ===")
        print(rendered_prompt)
        print("\n=== USER MESSAGE TEMPLATE ===")
        print(f"TITLE: {first.get('title', '')}\n\nFULL TEXT: <{pdf_dir}/...>")
        print(f"\n[DRY RUN] Would code {len(to_code)} items with {model}",
              flush=True)
        return 0

    client = anthropic.Anthropic()
    log_fh = output_path.open("a", newline="", encoding="utf-8")
    writer = csv.DictWriter(log_fh, fieldnames=csv_columns, extrasaction="ignore")
    if output_path.stat().st_size == 0:
        writer.writeheader()
    log_lock = threading.Lock()

    counts = {"include": 0, "exclude": 0, "error": 0, "no_pdf": 0}
    done_count = 0
    total = len(to_code)

    def worker(item: dict) -> dict:
        pdf_path = _find_pdf_path(item, atts_by_parent, pdf_dir)
        if pdf_path is None:
            d = item.get("data", {})
            return {
                "item_key": d.get("key", item.get("key", "")),
                "doi": (d.get("DOI") or "").strip(),
                "title": (d.get("title") or "")[:200],
                "year": (d.get("date") or "")[:4],
                "journal": d.get("publicationTitle", "") or "",
                "pdf_path": "",
                "fulltext_chars": 0,
                "truncated": "false",
                "decision": "error",
                "exclusion_code": "",
                "reason": "no PDF attachment found",
                "model": model,
                **{f["name"]: "" for f in fields},
            }
        return _code_one(item, pdf_path, client, model, rendered_prompt, fields)

    print(f"Coding with {args.workers} parallel workers (model={model})...",
          flush=True)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(worker, it): it for it in to_code}
        for fut in concurrent.futures.as_completed(futures):
            row = fut.result()
            done_count += 1
            decision = row.get("decision", "error")
            if row.get("reason") == "no PDF attachment found":
                counts["no_pdf"] += 1
            else:
                counts[decision] = counts.get(decision, 0) + 1

            row["timestamp"] = datetime.now(UTC).isoformat()
            row["prompt_version"] = prompt_version
            with log_lock:
                writer.writerow(row)
                log_fh.flush()

            title = row.get("title", "")[:60]
            print(f"[{done_count}/{total}] {title:<60} → {decision}",
                  flush=True)

    log_fh.close()

    print(f"\n{'=' * 60}")
    print(f"Done. Coded {total} items.")
    for k in ("include", "exclude", "error", "no_pdf"):
        print(f"  {k}: {counts.get(k, 0)}")
    print(f"Log: {output_path}")
    print("\nDeferred to a later plugin release: automatic write-back of")
    print("tags (`fulltext:include` / `fulltext:exclude`) and coded-field")
    print("child notes to Zotero. Use `export_coded_includes.py` to build")
    print("the manuscript-facing view of the include rows.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
