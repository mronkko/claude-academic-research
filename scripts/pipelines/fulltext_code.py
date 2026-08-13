#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "anthropic>=0.40",
#     "pyzotero>=1.6",
#     "pdfplumber>=0.10",
#     "pypdf>=4.0",
#     "tenacity>=8.0",
#     "httpx>=0.25",
#     "google-genai",
#     "openai>=1.0",
# ]
# ///
"""LLM-driven full-text screening + structured coding for an SLR.

Reads items from a Zotero collection (typically those marked
`abstract:include` or `abstract:borderline` at the abstract stage),
locates each paper's PDF attachment, extracts the full text
(pdfplumber with pypdf fallback), then passes title + full text to
Claude Sonnet for a single decision (`include` / `exclude`) plus
extraction of the coding fields declared in the project's
`screening_config.py`.

Writes the decision in two places:

1. As a `fulltext:include` / `fulltext:exclude` Zotero tag on the
   item — the authoritative state (per the `systematic-review`
   skill's Zotero-as-ground-truth principle). Error rows are NOT
   tagged, so a re-run naturally retries them.
2. As an append-only row in `screening/fulltext_screening.csv` — the
   run-history for provenance.

Resumable: on start, reads the collection's items, skips any that
already carry `fulltext:include` / `fulltext:exclude`, and processes
the rest. `--full-recode` removes the stage tag first so every item
is re-coded.

The prompt, model, and coding schema all come from the per-project
config — the plugin's copy of this script is deliberately generic.

Usage:
    uv run fulltext_code.py --group 6015547 --collection ABCDE1234 \\
        --config ./screening_config.py --pdf-dir ./pdfs \\
        --output screening/fulltext_screening.csv

Common flags: --dry-run (print first prompt, no API calls),
--limit N, --only-keys K1,K2,..., --workers N, --rerun, --full-recode,
--csv-backfill (one-time migration: apply fulltext:* tags from CSV
decisions, no LLM calls; exits after).
"""

from __future__ import annotations

import argparse
import concurrent.futures
import csv
import os
import re
import shutil
import sys
import threading
from datetime import UTC, datetime
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

import csv_io  # noqa: E402
import pdf_text_cache  # noqa: E402
import screening_common  # noqa: E402
import zotero_io  # noqa: E402
from core import llm_provider  # noqa: E402
from core.config_loader import require  # noqa: E402
from core.llm import (  # noqa: E402
    extract_json_from_response,
    extract_pdf_text,
)
from core.models import (  # noqa: E402
    default_for_stage,
    effective_model,
    model_flag_help,
)
from log_schemas import fulltext_screening_fields  # noqa: E402

# Soft cap on full-text chars sent to Sonnet (~180k tokens at 4 chars/token;
# leaves headroom for prompt + response in Sonnet's 200k context).
SOFT_FULLTEXT_CHAR_CAP = 720_000
PLACEHOLDER = "{coding_fields_json_placeholder}"

# Marker at the top of the SLR Coding child note — used to find and
# overwrite our own note among an item's children without touching
# any user-authored notes.
SLR_CODING_NOTE_MARKER = "<h1>SLR Coding</h1>"


def _build_slr_coding_note_html(
    row: dict,
    fields: list[dict],
    prompt_version: str,
) -> str:
    """Render a coded row as an HTML note body for Zotero.

    Two layers in one note:
    - The visible HTML above (h2 headings + paragraphs) is the
      adjudicator's view in Zotero Desktop.
    - A trailing `<!-- SLR_CODING_DATA: {json} -->` comment carries the
      same data in machine-parseable JSON. `export_coded_includes.py`
      reads the JSON block, not the HTML, so presentation changes don't
      break the export pipeline.

    Skips fields whose value is empty (the coder had nothing to say).
    """
    import json
    from html import escape

    parts = [SLR_CODING_NOTE_MARKER]
    decision = row.get("decision", "")
    reason = row.get("reason", "")
    exclusion_code = row.get("exclusion_code", "")

    parts.append(f"<p><strong>Decision:</strong> {escape(decision)}</p>")
    if exclusion_code:
        parts.append(
            f"<p><strong>Exclusion code:</strong> {escape(exclusion_code)}</p>"
        )
    if reason:
        parts.append(f"<p><strong>Reason:</strong> {escape(reason)}</p>")

    for f in fields:
        name = f.get("name", "")
        if not name:
            continue
        value = (row.get(name) or "").strip()
        if not value:
            continue
        # Human-readable label: snake_case → Title Case.
        label = name.replace("_", " ").title()
        parts.append(f"<h2>{escape(label)}</h2>")
        # Preserve paragraph breaks; don't blow up on HTML inside values.
        for para in value.split("\n\n"):
            para = para.strip()
            if para:
                parts.append(f"<p>{escape(para)}</p>")

    parts.append(
        f"<hr/><p><em>Produced by fulltext_code.py — "
        f"model={escape(str(row.get('model', '')))}; "
        f"prompt_version={escape(str(prompt_version))}; "
        f"timestamp={escape(str(row.get('timestamp', '')))}</em></p>"
    )

    # Machine-parseable JSON block (an HTML comment, hidden from Zotero's
    # note renderer). `export_coded_includes.py` extracts this rather
    # than parsing the HTML above.
    data_payload: dict = {
        "decision": decision,
        "exclusion_code": exclusion_code,
        "reason": reason,
        "model": row.get("model", ""),
        "prompt_version": prompt_version,
        "timestamp": row.get("timestamp", ""),
        "fields": {f["name"]: row.get(f["name"], "")
                   for f in fields if f.get("name")},
    }
    parts.append(
        f"<!-- SLR_CODING_DATA: "
        f"{json.dumps(data_payload, ensure_ascii=False)} -->"
    )
    return "\n".join(parts)


def _merge_fields_into_payload(
    existing_payload: dict,
    new_row: dict,
    target_fields: set[str],
) -> dict:
    """Merge target_fields from new_row into existing_payload.

    Only the named field values (inside payload['fields']) are updated.
    Decision, reason, exclusion_code, model, prompt_version, and timestamp
    are not touched — the screening decision is authoritative and update
    mode must not silently flip it.

    Returns a new dict; existing_payload is not mutated.
    """
    result = dict(existing_payload)
    result["fields"] = dict(existing_payload.get("fields") or {})
    for fname in target_fields:
        new_val = new_row.get(fname)
        if new_val is not None:
            result["fields"][fname] = new_val
    return result


def _items_for_update_mode(
    items: list[dict],
    only_keys: set[str] | None,
) -> list[dict]:
    """Return items eligible for --update-fields: those already tagged
    `fulltext:include`. Optional `only_keys` narrows further."""
    eligible = [
        it for it in items
        if any(
            t.get("tag", "") == "fulltext:include"
            for t in it.get("data", {}).get("tags", [])
        )
    ]
    if only_keys is not None:
        eligible = [it for it in eligible if it["key"] in only_keys]
    return eligible


def _fetch_existing_payload(
    zot: zotero_io.ZoteroClient,
    item_key: str,
) -> dict | None:
    """Fetch the SLR Coding child note for item_key and return its parsed
    JSON payload, or None if the item has no SLR Coding note yet."""
    children = zot.cloud.children(item_key)
    for child in children:
        cdata = child.get("data", {})
        if cdata.get("itemType") != "note":
            continue
        body = (cdata.get("note") or "").lstrip()
        if "SLR_CODING_DATA" not in body:
            continue
        payload = zotero_io.parse_slr_coding_note(body)
        if payload is not None:
            return payload
    return None


def _load_screening_config(path: str):
    mod = screening_common.load_config_module(
        path,
        "screening_config",
        required=("FULLTEXT_CODING_SYSTEM_PROMPT", "FULLTEXT_CODING_FIELDS"),
    )
    if not isinstance(mod.FULLTEXT_CODING_FIELDS, list):
        sys.exit("ERROR: FULLTEXT_CODING_FIELDS must be a list of dicts.")
    for field in mod.FULLTEXT_CODING_FIELDS:
        if "name" not in field:
            sys.exit("ERROR: every FULLTEXT_CODING_FIELDS entry needs `name`.")
    return (
        mod.FULLTEXT_CODING_SYSTEM_PROMPT,
        mod.FULLTEXT_CODING_FIELDS,
        getattr(mod, "FULLTEXT_CODING_MODEL", "") or default_for_stage("fulltext_coding"),
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
    """Compose the canonical full-text screening column list.

    Delegates to `log_schemas.fulltext_screening_fields` so the column
    order stays in sync with what `abstract_screen.py` and any manual
    adjudication path use. Project-specific fields come from the user's
    `screening_config.FULLTEXT_CODING_FIELDS` (each dict's `name`).
    """
    return fulltext_screening_fields([f["name"] for f in coding_fields])


STAGE_TAG_PREFIX = "fulltext:"
STAGE_TAG_VALUES = ("include", "exclude")


def _already_tagged(items: list[dict]) -> set[str]:
    """Items that already have `fulltext:include` or `fulltext:exclude`
    in Zotero — these are 'done' for resume purposes. Canonical source.

    Exact-value match, unlike abstract screening's prefix match: any other
    `fulltext:*` tag a user has added by hand does not count as coded."""
    return screening_common.items_with_stage_tag(
        items, prefix=STAGE_TAG_PREFIX, values=STAGE_TAG_VALUES,
    )


def _load_last_decisions(path: Path) -> dict[str, str]:
    """Last CSV decision per key. Used for the `--rerun` path (retry
    `error` rows) and for `--csv-backfill`, NOT for resume decisions.

    Unfiltered on purpose — `--rerun` needs to see the `error` rows. The
    backfill path filters afterwards, which is why it can drop an item
    whose last row is an error even though an earlier row decided it."""
    return screening_common.last_decisions_by_key(path)


def _run_csv_backfill(
    zot: zotero_io.ZoteroClient,
    coll_items: list[dict],
    output_path: Path,
) -> int:
    """One-time migration: apply fulltext:* tags from CSV decisions for
    items that have a CSV decision but no Zotero tag yet. No LLM calls."""
    csv_decisions = {
        k: d for k, d in _load_last_decisions(output_path).items()
        if d in STAGE_TAG_VALUES
    }
    return screening_common.run_csv_backfill(
        zot,
        coll_items,
        csv_decisions,
        prefix=STAGE_TAG_PREFIX,
        values=STAGE_TAG_VALUES,
        label="fulltext:*",
    )


# ---------------------------------------------------------------------------
# Zotero helpers
# ---------------------------------------------------------------------------


def _find_pdf_path(
    item: dict,
    attachments_by_parent: dict[str, list[dict]],
    pdf_dir: Path | None = None,
    zotero_storage: Path | None = None,
) -> Path | None:
    """Resolve an item's PDF path, preferring Zotero's own storage tree.

    Resolution order:
      1. **Linked-file attachments** (`linkMode == "linked_file"`):
         use `data.path` directly. Zotero stores absolute paths or
         the literal sentinel `attachments:<filename>` for entries
         relative to the data dir.
      2. **Stored attachments**: `<zotero_storage>/storage/<attachment_key>/<filename>`
         is Zotero's convention for items the user dragged into the
         library. The attachment item's `key` field gives the directory.
      3. **Legacy project-local pdfs/ dir**: `<pdf_dir>/<filename>` —
         covers users who symlinked Zotero PDFs into a project-local
         directory before this fix landed (the `link_zotero_pdfs.py`
         workaround). Optional.
      4. **DOI-named fallback** in `pdf_dir`: covers PDFs renamed by
         the Elsevier-TDM remediation (P11) or hand-placed by the user.

    Returns the first existing path or None.
    """
    key = item["key"]
    d = item.get("data", {})
    doi = (d.get("DOI") or "").strip()
    atts = attachments_by_parent.get(key, [])
    pdfs = [
        a for a in atts
        if a.get("data", {}).get("contentType") == "application/pdf"
        and a.get("data", {}).get("md5")
    ]

    for att in pdfs:
        att_data = att.get("data", {})
        att_key = att.get("key", "") or att_data.get("key", "")
        filename = att_data.get("filename", "") or ""
        link_mode = att_data.get("linkMode", "")
        att_path = att_data.get("path", "") or ""

        # 1. Linked-file attachment.
        if link_mode == "linked_file" and att_path:
            if att_path.startswith("attachments:") and zotero_storage:
                rel = att_path.split(":", 1)[1]
                candidate = zotero_storage / rel
                if candidate.exists():
                    return candidate
            else:
                candidate = Path(att_path)
                if candidate.exists():
                    return candidate

        # 2. Stored attachment under Zotero's storage tree.
        if zotero_storage and att_key and filename:
            candidate = zotero_storage / "storage" / att_key / filename
            if candidate.exists():
                return candidate

        # 3. Legacy project-local pdfs/ dir.
        if pdf_dir and filename:
            candidate = pdf_dir / filename
            if candidate.exists():
                return candidate

    # 4. DOI-named fallback in the project-local pdf_dir.
    if pdf_dir and doi:
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
    item_key = d.get("key", item.get("key", ""))
    # Route through pdf_text_cache so re-codes / audits / re-runs reuse
    # the prior extraction. The cache is keyed by content hash, so an
    # Elsevier-TDM PDF replacement (P11) auto-invalidates the prior
    # entry. Falls back to direct extraction when the cache helper is
    # unavailable (e.g. pdftotext missing) — preserves the old contract.
    try:
        fulltext = pdf_text_cache.get_text(item_key, pdf_path)
    except FileNotFoundError:
        # pdftotext binary missing — let the existing extractor try
        # (it has multiple internal fallbacks: pypdf, pdfplumber).
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
        text = client.generate(
            model=model,
            # Scale output budget with the coding schema: a fixed 3500
            # overflowed once configs grew past the 12-field template
            # (truncated JSON -> parse error; in --update-fields mode this
            # surfaced as silently empty fields at temperature 0).
            # ~400 tokens/field of headroom, floor 4000 for small schemas.
            max_tokens=max(4000, 2000 + 400 * len(fields)),
            temperature=0.0,
            system=prompt,
            prompt=user_msg,
        )
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
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(line_buffering=True)
        except Exception:
            pass
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="./screening_config.py",
                        help="Path to screening_config.py (default: "
                             "./screening_config.py).")
    zotero_io.add_library_args(parser)
    parser.add_argument("--collection", required=True,
                        help="Zotero collection key whose items to code.")
    parser.add_argument("--pdf-dir", default="",
                        help="Optional fallback directory for project-local PDFs "
                             "(legacy `./pdfs/` convention). PDFs are normally "
                             "resolved from the Zotero attachment's storage path.")
    parser.add_argument("--zotero-storage", default="",
                        help="Override path to the Zotero data directory "
                             "(contains the `storage/` subtree). Default: "
                             "$ZOTERO_DATA_DIR or ~/Zotero.")
    parser.add_argument("--output", default="screening/fulltext_screening.csv",
                        help="Append-only log path.")
    parser.add_argument("--model", default="",
                        help=model_flag_help(
                            "FULLTEXT_CODING_MODEL from screening_config.py, "
                            "else the configured provider's balanced tier"))
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
    parser.add_argument("--csv-backfill", action="store_true",
                        help="One-time migration from pre-Zotero-as-truth "
                             "deployments: read CSV decisions and apply "
                             "matching fulltext:* tags for items that don't "
                             "have one yet. Makes no LLM calls; exits after.")
    parser.add_argument("--update-fields", default="",
                        help="Comma-separated field names to selectively update "
                             "in existing SLR Coding notes without changing "
                             "screening decisions. Targets items already tagged "
                             "fulltext:include. Other fields in each note are "
                             "preserved. Use --only-keys to restrict to a subset.")
    args = parser.parse_args()

    prompt_template, fields, config_model, prompt_version = _load_screening_config(
        args.config)
    # Resolve before the provider pre-flight below — that branches on the
    # model name to decide which API key to require.
    model = effective_model(
        args.model, config_model, stage="FULLTEXT_CODING_MODEL",
    )
    rendered_prompt = _render_prompt(prompt_template, fields)
    csv_columns = _csv_columns(fields)

    api_key = "" if args.dry_run else require("zotero", "api_key",
                                              env="ZOTERO_API_KEY")
    if not args.dry_run:
        llm_provider.require_credentials(model)


    pdf_dir = Path(args.pdf_dir) if args.pdf_dir else None
    # Resolve Zotero data directory: --zotero-storage flag → $ZOTERO_DATA_DIR
    # → ~/Zotero (Zotero's cross-platform default). Stored attachments
    # live at <zotero_storage>/storage/<attachment_key>/<filename>; the
    # _find_pdf_path resolver checks that path before any project-local
    # symlink convention.
    storage_candidate = Path(
        args.zotero_storage
        or os.environ.get("ZOTERO_DATA_DIR")
        or (Path.home() / "Zotero")
    )
    zotero_storage: Path | None
    if storage_candidate.is_dir():
        zotero_storage = storage_candidate
    else:
        # Don't fail outright — `_find_pdf_path` falls back to pdf_dir.
        # But surface the miss so the user knows why their Zotero PDFs
        # aren't being picked up.
        print(
            f"  warning: Zotero storage dir {storage_candidate} not found; "
            f"falling back to --pdf-dir-only resolution.",
            flush=True,
        )
        zotero_storage = None
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    if args.full_recode and output_path.exists():
        backup = output_path.with_suffix(".bak-" + datetime.now(
            UTC).strftime("%Y%m%dT%H%M%SZ"))
        shutil.copy2(output_path, backup)
        output_path.unlink()
        print(f"Backed up existing log to {backup}; rebuilding.", flush=True)

    # Pre-flight schema validation/migration before any API calls are made.
    if output_path.exists() and output_path.stat().st_size > 0:
        try:
            with output_path.open(newline="", encoding="utf-8") as fh:
                reader = csv.DictReader(fh)
                actual_header = list(reader.fieldnames or [])
                if actual_header != csv_columns:
                    if set(actual_header).issubset(set(csv_columns)) and [c for c in csv_columns if c in actual_header] == actual_header:
                        print(
                            f"  Migrating CSV schema at {output_path} (widening from "
                            f"{len(actual_header)} to {len(csv_columns)} columns)...",
                            flush=True,
                        )
                        existing_rows = list(reader)
                        import tempfile
                        tmp_fd, tmp_path = tempfile.mkstemp(
                            prefix=f".{output_path.name}.", suffix=".tmp", dir=str(output_path.parent)
                        )
                        try:
                            with os.fdopen(tmp_fd, "w", newline="", encoding="utf-8") as out_fh:
                                writer = csv.DictWriter(out_fh, fieldnames=csv_columns, extrasaction="ignore")
                                writer.writeheader()
                                for r in existing_rows:
                                    writer.writerow({col: r.get(col, "") for col in csv_columns})
                            os.replace(tmp_path, output_path)
                        except Exception:
                            try:
                                os.unlink(tmp_path)
                            except OSError:
                                pass
                            raise
                    else:
                        sys.exit(
                            f"ERROR: CSV schema mismatch at {output_path}.\n"
                            f"  Expected: {csv_columns}\n"
                            f"  Actual:   {actual_header}"
                        )
        except Exception as e:
            sys.exit(f"ERROR: Failed to validate/migrate CSV schema: {e}")

    zot = zotero_io.ZoteroClient.from_args(args, api_key=api_key or "dummy")
    print(f"Fetching items from Zotero ({zot.describe_library()}, "
          f"collection={args.collection})...", flush=True)
    items = zot.collection_items(args.collection, item_type="journalArticle")

    if args.csv_backfill:
        return _run_csv_backfill(zot, items, output_path)

    attachments = zot.all_attachments()
    atts_by_parent: dict[str, list[dict]] = {}
    for a in attachments:
        p = a.get("data", {}).get("parentItem")
        if p:
            atts_by_parent.setdefault(p, []).append(a)
    print(f"  {len(items)} items, {len(attachments)} attachments", flush=True)

    if args.only_keys:
        wanted = {k.strip() for k in args.only_keys.split(",") if k.strip()}
        items = [it for it in items if it["key"] in wanted]

    # --full-recode removes the fulltext:* tag from every targeted item,
    # forcing re-processing. The CSV backup already happened above.
    if args.full_recode:
        print("--full-recode: clearing fulltext:* tags on all targeted items",
              flush=True)
        for it in items:
            try:
                zot.update_tags(it["key"], remove_prefixed=[STAGE_TAG_PREFIX])
            except Exception as e:  # noqa: BLE001
                print(f"  WARN: could not clear tag on {it['key']}: {e}",
                      flush=True)
        # Refresh items to reflect the tag clearing.
        items = zot.collection_items(
            args.collection, item_type="journalArticle",
        )
        if args.only_keys:
            wanted = {k.strip() for k in args.only_keys.split(",") if k.strip()}
            items = [it for it in items if it["key"] in wanted]

    if not args.update_fields:
        # Resume: skip items already carrying fulltext:include / fulltext:exclude.
        tagged = _already_tagged(items)
        last = _load_last_decisions(output_path)
        to_code: list[dict] = []
        for it in items:
            if it["key"] in tagged:
                continue
            last_decision = last.get(it["key"], "")
            # CSV-only: an 'error' row not yet tagged — usually the
            # screening-time tag write failed OR pre-Zotero-as-truth state.
            # Only retry if --rerun.
            if last_decision == "error" and not args.rerun:
                continue
            to_code.append(it)

        # Warn on tag/CSV drift (CSV decision exists but no tag yet).
        drift_count = sum(
            1 for k, d in last.items()
            if d in STAGE_TAG_VALUES and k not in tagged
        )
        if drift_count:
            print(
                f"  WARNING: {drift_count} item(s) in CSV log lack fulltext:* "
                f"tags in Zotero. Run with --csv-backfill to apply tags from "
                f"CSV decisions.",
                flush=True,
            )

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

    client = llm_provider.get_provider(model)
    # Schema-stable + idempotent writes via csv_io.upsert_by_item_key.
    # Re-running on the same item replaces the prior row instead of
    # appending; recovers cleanly from partial / interrupted runs.
    log_lock = threading.Lock()

    # --update-fields mode: re-code specific fields on fulltext:include items.
    if args.update_fields:
        target_fields = {
            f.strip() for f in args.update_fields.split(",") if f.strip()
        }
        known_names = {f["name"] for f in fields}
        unknown = target_fields - known_names
        if unknown:
            sys.exit(
                f"ERROR: --update-fields names unknown field(s): {unknown}. "
                f"Known fields from config: {known_names}"
            )
        only_keys: set[str] | None = None
        if args.only_keys:
            only_keys = {k.strip() for k in args.only_keys.split(",") if k.strip()}
        to_update = _items_for_update_mode(items, only_keys)
        if args.limit and args.limit < len(to_update):
            to_update = to_update[:args.limit]
        print(
            f"  --update-fields mode: updating field(s) {sorted(target_fields)} "
            f"on {len(to_update)} fulltext:include item(s).",
            flush=True,
        )
        print(
            "  WARNING: adjudicator edits to these fields in Zotero will be "
            "overwritten. All other fields are preserved.",
            flush=True,
        )
        if not to_update:
            print("Nothing to update.", flush=True)
            return 0

        # Update mode targets items whose include decision is already final
        # (often via human adjudication), and the merge preserves that
        # decision. The prompt must therefore not re-adjudicate: without
        # this override, the LLM may re-decide "exclude" and — per the
        # output rules — return empty strings for every coding field,
        # silently blanking the targeted fields while the decision stays
        # "include". Same override pattern as adjudicated re-coding.
        update_prompt = rendered_prompt + (
            "\n\nUPDATE-MODE OVERRIDE: This paper has ALREADY passed "
            "full-text screening and its include decision is final — do "
            "NOT re-adjudicate it. Always output decision=include with an "
            "empty exclusion_code, and provide substantive content for "
            "EVERY coding field."
        )

        if args.dry_run:
            first = to_update[0].get("data", {}) if to_update else {}
            print("\n=== RENDERED SYSTEM PROMPT ===")
            print(update_prompt)
            print("\n=== USER MESSAGE TEMPLATE ===")
            print(f"TITLE: {first.get('title', '')}\n\nFULL TEXT: <{pdf_dir}/...>")
            print(f"\n[DRY RUN] Would update {len(to_update)} item(s) with {model}",
                  flush=True)
            return 0

        counts: dict[str, int] = {"updated": 0, "no_pdf": 0, "error": 0}
        done_count = 0
        total = len(to_update)

        def update_worker(item: dict) -> dict:
            pdf_path = _find_pdf_path(
                item, atts_by_parent,
                pdf_dir=pdf_dir,
                zotero_storage=zotero_storage,
            )
            d = item.get("data", {})
            item_key = d.get("key", item.get("key", ""))
            base: dict = {
                "item_key": item_key,
                "doi": (d.get("DOI") or "").strip(),
                "title": (d.get("title") or "")[:200],
                "year": (d.get("date") or "")[:4],
                "journal": d.get("publicationTitle", "") or "",
                "model": model,
            }
            if pdf_path is None:
                return {**base, "pdf_path": "", "fulltext_chars": 0,
                        "truncated": "false", "decision": "error",
                        "exclusion_code": "", "reason": "no PDF attachment found",
                        **{f["name"]: "" for f in fields}}
            row = _code_one(item, pdf_path, client, model, update_prompt, fields)
            if row.get("decision") == "error":
                # Surface the failure instead of merging an empty update
                # into the note (which would destroy the error reason and
                # poison the note with blank fields). The note is left
                # untouched so a re-run retries cleanly.
                return row
            try:
                existing = _fetch_existing_payload(zot, item_key)
            except Exception as e:  # noqa: BLE001
                row["decision"] = "error"
                row["reason"] = f"fetch_children failed: {e}"[:300]
                return row
            if existing is not None:
                merged_payload = _merge_fields_into_payload(
                    existing, row, target_fields,
                )
                merged_row = dict(row)
                merged_row.update(merged_payload.get("fields", {}))
                merged_row["decision"] = existing["decision"]
                merged_row["exclusion_code"] = existing.get("exclusion_code", "")
                existing_reason = existing.get("reason", "")
                prefix = f"[UPDATE-FIELDS:{','.join(sorted(target_fields))}] "
                cleaned_reason = re.sub(r'^(?:\[UPDATE-FIELDS:[^\]]*\]\s*)+', '', existing_reason)
                merged_row["reason"] = (prefix + cleaned_reason)[:500]
                merged_row["timestamp"] = datetime.now(UTC).isoformat()
                note_html = _build_slr_coding_note_html(
                    merged_row, fields, prompt_version,
                )
                try:
                    zot.upsert_child_note(
                        item_key,
                        marker=SLR_CODING_NOTE_MARKER,
                        note_html=note_html,
                    )
                except Exception as e:  # noqa: BLE001
                    merged_row["reason"] = (
                        merged_row.get("reason", "") +
                        f" [NOTE UPDATE FAILED: {e}]"
                    )[:500]
                row = merged_row
            else:
                # No existing note: create fresh (same as normal include path).
                row["timestamp"] = datetime.now(UTC).isoformat()
                note_html = _build_slr_coding_note_html(row, fields, prompt_version)
                try:
                    zot.upsert_child_note(
                        item_key,
                        marker=SLR_CODING_NOTE_MARKER,
                        note_html=note_html,
                    )
                except Exception as e:  # noqa: BLE001
                    row["reason"] = (
                        row.get("reason", "") + f" [NOTE WRITE FAILED: {e}]"
                    )[:500]
            return row

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.workers
        ) as pool:
            futures = {pool.submit(update_worker, it): it for it in to_update}
            for fut in concurrent.futures.as_completed(futures):
                row = fut.result()
                done_count += 1
                decision = row.get("decision", "error")
                if "no PDF attachment found" in row.get("reason", ""):
                    counts["no_pdf"] += 1
                elif decision == "error":
                    counts["error"] += 1
                else:
                    counts["updated"] += 1

                row["timestamp"] = datetime.now(UTC).isoformat()
                row["prompt_version"] = prompt_version
                with log_lock:
                    csv_io.upsert_by_item_key(output_path, row, csv_columns)

                title = row.get("title", "")[:60]
                outcome = "updated" if decision not in ("error",) else decision
                print(f"[{done_count}/{total}] {title:<60} → {outcome}",
                      flush=True)

        print(f"\n{'=' * 60}")
        print(f"Done. Updated {total} item(s).")
        for k in ("updated", "error", "no_pdf"):
            if counts.get(k, 0):
                print(f"  {k}: {counts[k]}")
        print(f"Log: {output_path}")
        return 0

    counts = {"include": 0, "exclude": 0, "error": 0, "no_pdf": 0}
    done_count = 0
    total = len(to_code)

    def worker(item: dict) -> dict:
        pdf_path = _find_pdf_path(
            item, atts_by_parent,
            pdf_dir=pdf_dir,
            zotero_storage=zotero_storage,
        )
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

            # Apply stage tag. Only include / exclude get tagged;
            # error / no_pdf stay untagged so a re-run picks them up.
            row["timestamp"] = datetime.now(UTC).isoformat()
            row["prompt_version"] = prompt_version
            if decision in STAGE_TAG_VALUES:
                item_key = row.get("item_key", "")
                if item_key:
                    try:
                        zot.update_tags(
                            item_key,
                            add=[f"{STAGE_TAG_PREFIX}{decision}"],
                            remove_prefixed=[STAGE_TAG_PREFIX],
                        )
                    except Exception as tag_exc:  # noqa: BLE001
                        existing_reason = row.get("reason", "")
                        row["reason"] = (
                            f"{existing_reason} "
                            f"[TAG WRITE FAILED: {tag_exc}]"
                        )[:500]

                # Write the SLR Coding child note for includes only.
                # Excludes don't get a note — the tag plus the CSV row
                # is enough provenance, and excluded papers typically
                # have empty / placeholder coding fields.
                if decision == "include":
                    try:
                        note_html = _build_slr_coding_note_html(
                            row, fields, prompt_version,
                        )
                        zot.upsert_child_note(
                            item_key,
                            marker=SLR_CODING_NOTE_MARKER,
                            note_html=note_html,
                        )
                    except Exception as note_exc:  # noqa: BLE001
                        existing_reason = row.get("reason", "")
                        row["reason"] = (
                            f"{existing_reason} "
                            f"[NOTE WRITE FAILED: {note_exc}]"
                        )[:500]
            with log_lock:
                csv_io.upsert_by_item_key(output_path, row, csv_columns)

            title = row.get("title", "")[:60]
            print(f"[{done_count}/{total}] {title:<60} → {decision}",
                  flush=True)

    print(f"\n{'=' * 60}")
    print(f"Done. Coded {total} items.")
    for k in ("include", "exclude", "error", "no_pdf"):
        print(f"  {k}: {counts.get(k, 0)}")
    print(f"Log: {output_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
