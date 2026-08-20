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

A run can also be split in two, with a JSONL manifest as the seam, so
that the generation step can happen where the compute is rather than
where Zotero is:

    --emit-manifest    assemble the requests, write them, exit
    ... executed anywhere: a gateway, a laptop, a GPU node ...
    --apply-responses  write CSV rows, fulltext:* tags and coding notes

See `batch_manifest.py` for the contract and `run_manifest.py` for the
reference executor. Coding is the expensive stage, so the split matters
more here than at the abstract stage — and the manifest carries its own
`coding_fields`, because a field added to `screening_config.py` between
emit and apply would otherwise change the CSV's shape for a run whose
model was never asked about it.

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
import json
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

import batch_manifest  # noqa: E402
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
    cost_estimate_line,
    default_for_stage,
    effective_model,
    model_flag_help,
    paid_run_banner,
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


def merge_update_into_note(
    zot: zotero_io.ZoteroClient,
    row: dict,
    *,
    target_fields: set[str],
    fields: list[dict],
    prompt_version: str,
    timestamp: str,
) -> dict:
    """Fold an update-mode row into the item's existing SLR Coding note.

    Returns the row as it should be logged: the *existing* decision,
    exclusion code and reason, with only the targeted field values
    replaced. Update mode targets items whose include decision is
    already final — often by human adjudication — so the merge must not
    let the model flip it.

    Shared by the synchronous update worker and the batch applier, which
    reach this point from different directions (one has just generated
    the answer, the other read it out of a file days later) but owe the
    note and the log exactly the same content.
    """
    item_key = row.get("item_key", "")
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
        cleaned_reason = re.sub(
            r'^(?:\[UPDATE-FIELDS:[^\]]*\]\s*)+', '', existing_reason,
        )
        merged_row["reason"] = (prefix + cleaned_reason)[:500]
        merged_row["timestamp"] = timestamp
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
                merged_row.get("reason", "") + f" [NOTE UPDATE FAILED: {e}]"
            )[:500]
        return merged_row
    # No existing note: create fresh (same as the normal include path).
    row["timestamp"] = timestamp
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

# "No PDF to read" is not the same failure as "coding blew up", and the
# distinction drives what you do next: find the PDF, versus re-run the
# model. Both were logged as `decision=error` and told apart afterwards by
# string-matching `reason`, so the CSV disagreed with the tally printed
# beside it (`error: 0, no_pdf: 2` over two rows that both said `error`),
# and verify reported "2 items still in error state" for items whose only
# problem was a missing file. Neither is a *final* decision — both stay
# untagged so a re-run picks them up — but they are different states.
NO_PDF_DECISION = "no_pdf"
NO_PDF_REASON = "no PDF attachment found"
UNRESOLVED_DECISIONS = ("error", NO_PDF_DECISION)


def no_pdf_row(base: dict, fields: list[dict]) -> dict:
    """The log row for an item with no PDF to read.

    Built identically by the coding and `--rerun` update paths; shared so
    the two cannot drift into disagreeing about what a missing PDF looks
    like in the log. `base` supplies the item's identity columns
    (item_key, doi, title, year, journal, model).
    """
    return {
        **base,
        "pdf_path": "",
        "fulltext_chars": 0,
        "truncated": "false",
        "decision": NO_PDF_DECISION,
        "exclusion_code": "",
        "reason": NO_PDF_REASON,
        **{f["name"]: "" for f in fields},
    }


def _already_tagged(items: list[dict]) -> set[str]:
    """Items that already have `fulltext:include` or `fulltext:exclude`
    in Zotero — these are 'done' for resume purposes. Canonical source.

    Exact-value match, unlike abstract screening's prefix match: any other
    `fulltext:*` tag a user has added by hand does not count as coded."""
    return screening_common.items_with_stage_tag(
        items, prefix=STAGE_TAG_PREFIX, values=STAGE_TAG_VALUES,
    )


# Canonical definition: fetchers.preprint.PREPRINT_VERSION_TAG. Repeated
# rather than imported because this module's PEP 723 block carries no
# fetcher dependencies, and a tag string is not worth acquiring them.
PREPRINT_VERSION_TAG = "pdf:preprint-version"


def _warn_on_preprint_attachments(items: list[dict]) -> None:
    """Say out loud which items are about to be coded from a preprint.

    `enrich_pdfs.py --allow-preprints` can attach the manuscript that
    preceded peer review. Nothing downstream of this point can tell the
    difference: the coding note, the CSV row and the export all read
    identically whether the model read the working paper or the
    published article. So the one place the distinction still exists —
    the tag — has to be surfaced here, before the LLM call, while the
    user can still decide to fetch the real thing instead.
    """
    flagged = [
        it for it in items
        if any(
            t.get("tag") == PREPRINT_VERSION_TAG
            for t in it.get("data", {}).get("tags", [])
        )
    ]
    if not flagged:
        return
    print(
        f"\n  WARNING: {len(flagged)} of these items have a PREPRINT "
        f"attached, not the published article (tagged "
        f"'{PREPRINT_VERSION_TAG}').\n"
        f"  Coding a working paper as the published paper misreports what "
        f"the journal\n"
        f"  published — hypotheses, samples and findings all move between "
        f"the two.\n"
        f"  Review these before trusting their coded rows:",
        flush=True,
    )
    for it in flagged[:10]:
        title = (it.get("data", {}).get("title") or "")[:70]
        print(f"    {it['key']}  {title}", flush=True)
    if len(flagged) > 10:
        print(f"    … and {len(flagged) - 10} more (audit_zotero_library.py "
              f"writes the full list to audit.preprint_version.keys)",
              flush=True)


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


def output_budget(fields: list[dict]) -> int:
    """The output-token budget a coding call needs for this schema.

    Scales with the number of fields: a fixed 3500 overflowed once
    configs grew past the 12-field template (truncated JSON → parse
    error; in `--update-fields` mode that surfaced as silently empty
    fields at temperature 0). ~400 tokens/field of headroom, floor 4000
    for small schemas.

    Shared with the batch path, which writes the number into the
    manifest so that whatever executes the run reserves the same budget.
    A manifest executed with a smaller one produces truncated JSON for
    exactly the configs this formula exists to protect.
    """
    return max(4000, 2000 + 400 * len(fields))


def build_coding_request(
    item: dict,
    pdf_path: Path,
    *,
    model: str,
    fields: list[dict],
) -> tuple[dict, str]:
    """`(base_row, user_message)` — everything one coding call needs.

    The base row is the identity and provenance half of the CSV row:
    what was read, from where, and how much of it. The user message is
    the prompt half. Split out of `_code_one` so the batch path can
    assemble exactly the same request with no LLM in sight — a manifest
    row *is* these two values, serialised.
    """
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
        "item_key": item_key,
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
    return row, user_msg


def parse_coding_response(text: str, fields: list[dict]) -> dict:
    """The verdict half of a coded row, read out of the model's answer.

    Returns `decision` / `exclusion_code` / `reason` plus one entry per
    coding field, ready to merge into a base row. Never raises: an
    answer this cannot read becomes `decision=error` carrying the raw
    text, which leaves the item untagged and re-runnable.

    Shared by the synchronous path and the batch applier. A second copy
    is how the two would come to disagree about what a given model
    output means — and because both write the same columns, the
    disagreement would surface not as an error but as a review whose
    codes depend on which path ran it.
    """
    parsed = extract_json_from_response(text)
    if not parsed:
        return {
            "decision": "error",
            "reason": f"JSON PARSE ERROR — raw: {(text or '')[:300]}",
        }
    out: dict = {
        "decision": str(parsed.get("decision", "")).strip().lower(),
        "exclusion_code": str(parsed.get("exclusion_code", "")).strip(),
        "reason": str(parsed.get("reason", "")).strip(),
    }
    for f in fields:
        val = parsed.get(f["name"], "")
        if isinstance(val, (list, dict)):
            val = json.dumps(val, ensure_ascii=False)
        out[f["name"]] = str(val).strip()
    if out["decision"] not in ("include", "exclude"):
        out["decision"] = "error"
        out["reason"] = f"invalid decision value: {parsed.get('decision')!r}"
    return out


def apply_coded_row(
    zot,
    row: dict,
    *,
    fields: list[dict],
    prompt_version: str,
    output_path: Path,
    csv_columns: list[str],
    log_lock,
    timestamp: str = "",
) -> str:
    """Write one coded row's consequences: tag, coding note, CSV row.

    Returns the decision. Only `include` / `exclude` get tagged; `error`
    and `no_pdf` stay untagged so a re-run picks them up. A failed tag or
    note write is appended to the row's reason rather than raised — the
    CSV row is still worth having, and the missing tag is what makes the
    item come back next run.

    `timestamp` defaults to now; the batch applier passes the response's
    own `generated_at`, because the log records when a decision was
    *made* and that can be days before it is filed.
    """
    decision = row.get("decision", "error")
    row["timestamp"] = timestamp or datetime.now(UTC).isoformat()
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
                    f"{existing_reason} [TAG WRITE FAILED: {tag_exc}]"
                )[:500]

            # The SLR Coding child note is written for includes only.
            # Excludes don't get one — the tag plus the CSV row is enough
            # provenance, and excluded papers typically have empty /
            # placeholder coding fields.
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
                        f"{existing_reason} [NOTE WRITE FAILED: {note_exc}]"
                    )[:500]
    with log_lock:
        csv_io.upsert_by_item_key(output_path, row, csv_columns)
    return decision


def _code_one(item: dict, pdf_path: Path, client, model: str, prompt: str,
              fields: list[dict]) -> dict:
    row, user_msg = build_coding_request(
        item, pdf_path, model=model, fields=fields,
    )
    try:
        text = client.generate(
            model=model,
            max_tokens=output_budget(fields),
            temperature=0.0,
            system=prompt,
            prompt=user_msg,
        )
    except Exception as e:
        # Classified, not stringified — the caller needs to know whether
        # every remaining paper will fail the same way. Coding runs are
        # the expensive stage, so grinding through a whole corpus against
        # a spent quota is the costliest version of this mistake.
        verdict = llm_provider.classify_failure(e)
        row["decision"] = "error"
        row["reason"] = f"{verdict.status.value}: {verdict.detail}"[:300]
        row["_fatal"] = "" if verdict.retryable else verdict.format()
        return row
    row.update(parse_coding_response(text, fields))
    return row


# ---------------------------------------------------------------------------
# Batch path: emit a manifest here, execute it anywhere, apply it back
# ---------------------------------------------------------------------------


def build_manifest_rows(
    items: list[dict],
    *,
    run_id: str,
    system_prompt: str,
    model: str,
    prompt_version: str,
    fields: list[dict],
    library: dict,
    collection: str,
    attachments_by_parent: dict[str, list[dict]],
    pdf_dir: Path | None = None,
    zotero_storage: Path | None = None,
    max_input_chars: int = 0,
    mode: str = "code",
    target_fields: list[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    """`(manifest_rows, skipped)` for a set of items to code.

    Each row carries its own rendered prompt and its own copy of the
    coding schema, so whatever executes the manifest needs no Zotero, no
    `screening_config.py` and no part of this plugin.

    Three ways an item leaves here without a request, all of them into
    the sidecar rather than into silence:

    - **no_pdf** — nothing to read. Becomes a `no_pdf` CSV row on apply,
      identical to the one the synchronous path writes.
    - **pdf_unreadable** — extraction raised, or produced nothing. A GPU
      pass over an empty document is a wasted pass, and the answer it
      returns would be about no paper at all. (This is the one place the
      batch path deliberately decides something the synchronous path
      does not: there, an empty extraction is sent and the model's
      confusion becomes the verdict.)
    - **too_long_for_context** — over `--max-input-chars`. The 720k-char
      cap here is ~180k tokens, and a self-hosted model typically serves
      8k–128k; over-long requests fail *inside* the executor in ways that
      read as model failures. Skipping is visible, truncating is not.
    """
    rows: list[dict] = []
    skipped: list[dict] = []
    budget = output_budget(fields)
    # Frozen into every row: the schema the model is actually being
    # asked for. Editing `screening_config.py` between emit and apply
    # would otherwise change the CSV's columns for a run whose model was
    # never asked about them.
    coding_fields = [
        {"name": f["name"], "description": f.get("description", "")}
        for f in fields
    ]
    for item in items:
        d = item.get("data", {})
        identity = {
            "item_key": d.get("key", item.get("key", "")),
            "doi": (d.get("DOI") or "").strip(),
            "title": (d.get("title") or "")[:200],
            "year": (d.get("date") or "")[:4],
            "journal": d.get("publicationTitle", "") or "",
        }
        pdf_path = _find_pdf_path(
            item, attachments_by_parent,
            pdf_dir=pdf_dir, zotero_storage=zotero_storage,
        )
        if pdf_path is None:
            skipped.append({
                **identity, "skip_reason": "no_pdf", "detail": NO_PDF_REASON,
            })
            continue
        try:
            base, user_msg = build_coding_request(
                item, pdf_path, model=model, fields=fields,
            )
        except Exception as e:  # noqa: BLE001
            skipped.append({
                **identity,
                "skip_reason": "pdf_unreadable",
                "detail": f"{type(e).__name__}: {e}"[:300],
            })
            continue
        if not base["fulltext_chars"]:
            skipped.append({
                **identity,
                "skip_reason": "pdf_unreadable",
                "detail": f"extracted no text from {pdf_path}",
            })
            continue
        if max_input_chars and len(user_msg) > max_input_chars:
            skipped.append({
                **identity,
                "skip_reason": "too_long_for_context",
                "detail": (
                    f"{len(user_msg)} chars exceeds --max-input-chars "
                    f"{max_input_chars}"
                ),
            })
            continue
        rows.append({
            "schema_version": batch_manifest.SCHEMA_VERSION,
            "run_id": run_id,
            "request_id": batch_manifest.request_id(
                run_id, identity["item_key"],
            ),
            "ordinal": 0,
            "stage": batch_manifest.STAGE_FULLTEXT,
            "mode": mode,
            "target_fields": sorted(target_fields or []),
            "library": library,
            "collection": collection,
            **identity,
            "prompt_version": prompt_version,
            "model_hint": model,
            "coding_fields": coding_fields,
            "system": system_prompt,
            "user": user_msg,
            "system_sha256": batch_manifest.sha256(system_prompt),
            "user_sha256": batch_manifest.sha256(user_msg),
            "temperature": 0.0,
            "max_output_tokens": budget,
            "input_chars": len(user_msg),
            "fulltext_chars": base["fulltext_chars"],
            "truncated": base["truncated"],
            "pdf_path": base["pdf_path"],
            "pdf_sha256": batch_manifest.file_sha256(pdf_path),
        })
    return rows, skipped


def coding_fields_from_manifest(rows: list[dict]) -> list[dict]:
    """The coding schema a manifest was emitted with.

    Rows that disagree mean two runs' requests in one file, which
    `read_manifest` already refuses on `run_id` — this catches the
    hand-edited case as well, because the schema decides the CSV's
    columns and half a schema is worse than none.
    """
    shapes = {
        json.dumps(r.get("coding_fields") or [], sort_keys=True) for r in rows
    }
    if len(shapes) != 1:
        raise batch_manifest.ManifestError(
            "manifest rows disagree about `coding_fields`. The coding "
            "schema decides the CSV's columns, so one file cannot carry "
            "two of them."
        )
    return list(rows[0].get("coding_fields") or [])


def check_coding_fields_match(
    manifest_fields: list[dict],
    config_fields: list[dict],
    *,
    force: bool,
) -> None:
    """Refuse to apply a manifest whose schema the config has moved past.

    The manifest is the reproducibility record: it says which fields the
    model was asked for. If `screening_config.py` has since gained,
    lost or renamed one, applying against the current config would write
    a CSV whose columns no model ever answered — silently, since a
    missing key just renders as an empty cell.
    """
    m = [f.get("name", "") for f in manifest_fields]
    c = [f.get("name", "") for f in config_fields]
    if m == c:
        return
    added = [n for n in c if n not in m]
    removed = [n for n in m if n not in c]
    detail = (
        f"the manifest was emitted with {len(m)} coding field(s) and "
        f"screening_config.py now declares {len(c)}"
        + (f"; added since: {added}" if added else "")
        + (f"; no longer in the config: {removed}" if removed else "")
    )
    if force:
        print(
            f"  WARNING: {detail}.\n"
            f"           --force-apply was given, so the manifest's schema "
            f"wins — those are the fields the model was actually asked for.",
            flush=True,
        )
        return
    raise SystemExit(
        f"REFUSING to apply: {detail}.\n"
        f"  The manifest records what the model was asked for; the config "
        f"records what you want now. Applying the manifest under the new "
        f"config would write columns no model answered.\n"
        f"  Restore the config to re-apply this run, re-emit the manifest "
        f"to use the new schema, or pass --force-apply to log the run "
        f"under the schema it was emitted with."
    )


def _base_row_from_manifest_row(
    req: dict, *, model: str, fields: list[dict],
) -> dict:
    """Rebuild what `build_coding_request` produced, from the manifest.

    The manifest carries these columns precisely so applying needs
    neither Zotero nor the PDF: the log should describe the paper as it
    was when the model read it, and by apply time the item may have been
    edited and the PDF replaced.
    """
    row = {
        "item_key": req.get("item_key", ""),
        "doi": req.get("doi", ""),
        "title": req.get("title", ""),
        "year": req.get("year", ""),
        "journal": req.get("journal", ""),
        "pdf_path": req.get("pdf_path", ""),
        "fulltext_chars": req.get("fulltext_chars", 0),
        "truncated": req.get("truncated", "false"),
        "model": model,
    }
    for f in fields:
        row[f["name"]] = ""
    return row


def _row_for_skipped_unit(skip: dict, *, model: str, fields: list[dict]) -> dict:
    """The CSV row a unit that never reached the model still owes the log."""
    base = {
        "item_key": skip.get("item_key", ""),
        "doi": skip.get("doi", ""),
        "title": skip.get("title", ""),
        "year": skip.get("year", ""),
        "journal": skip.get("journal", ""),
        "model": model,
    }
    row = no_pdf_row(base, fields)
    if skip.get("skip_reason") != "no_pdf":
        row["decision"] = "error"
        row["reason"] = (
            f"{skip.get('skip_reason')}: {skip.get('detail', '')}"
        )[:300]
    return row


def apply_responses(
    zot,
    *,
    manifest_path: Path,
    manifest_rows: list[dict],
    responses_path: Path,
    output_path: Path,
    items: list[dict],
    fields: list[dict],
    csv_columns: list[str],
    force: bool,
    skip_already_tagged: bool,
) -> int:
    """Apply an executed manifest: CSV rows, fulltext:* tags, coding notes.

    No LLM is called. Everything written here is derived from the two
    files, which is what lets the generation step happen on a machine
    this one never talks to.
    """
    _, responses = batch_manifest.read_responses(responses_path)
    paired, unanswered, orphaned = batch_manifest.join_responses(
        manifest_rows, responses,
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

    first = manifest_rows[0]
    mode = first.get("mode", "code")
    prompt_version = first.get("prompt_version", "")
    target_fields = set(first.get("target_fields") or [])
    run_model = next(
        (r.get("model", "") for _, r in paired if r.get("model")),
        first.get("model_hint", ""),
    )

    # A manifest and its application can be days apart. Anything coded
    # in between was decided by something else, and overwriting it
    # silently would lose that decision. Update mode is exempt: it
    # targets already-tagged items by definition.
    tagged_since = set() if mode == "update_fields" else _already_tagged(items)
    clashes = [r for r, _ in paired if r["item_key"] in tagged_since]
    if clashes:
        verb = "skipping" if skip_already_tagged else "overwriting"
        print(
            f"  WARNING: {len(clashes)} of {len(paired)} item(s) have been "
            f"tagged fulltext:* since this manifest was emitted; {verb} "
            f"them.",
            flush=True,
        )

    log_lock = threading.Lock()
    counts: dict[str, int] = {}
    total = len(paired)
    for n, (req, resp) in enumerate(paired, start=1):
        if skip_already_tagged and req["item_key"] in tagged_since:
            counts["skipped"] = counts.get("skipped", 0) + 1
            continue
        model = resp.get("model", "") or req.get("model_hint", "")
        row = _base_row_from_manifest_row(req, model=model, fields=fields)
        status = resp.get("call_status", "error")
        if status == "ok":
            _, answer = batch_manifest.split_reasoning(
                resp.get("response_text") or "",
            )
            row.update(parse_coding_response(answer, fields))
        elif status == "truncated":
            # A cut-off answer is a run defect, not a verdict: the item
            # stays untagged and re-runnable. Note this does not touch
            # the row's `truncated` column, which records whether the
            # *input* hit the 720k cap — a different fact about a
            # different end of the call.
            row["decision"] = "error"
            row["reason"] = (
                f"truncated: output hit the "
                f"{req.get('max_output_tokens')}-token budget"
            )
        else:
            row["decision"] = "error"
            row["reason"] = str(
                resp.get("error") or f"call_status={status}"
            )[:300]

        # When the decision was made, not when it was filed.
        timestamp = resp.get("generated_at", "")
        if mode == "update_fields":
            if row["decision"] != "error":
                row = merge_update_into_note(
                    zot, row,
                    target_fields=target_fields,
                    fields=fields,
                    prompt_version=prompt_version,
                    timestamp=timestamp or datetime.now(UTC).isoformat(),
                )
            row["timestamp"] = timestamp or datetime.now(UTC).isoformat()
            row["prompt_version"] = prompt_version
            with log_lock:
                csv_io.upsert_by_item_key(output_path, row, csv_columns)
            decision = row.get("decision", "error")
            outcome = decision if decision in UNRESOLVED_DECISIONS else "updated"
        else:
            outcome = apply_coded_row(
                zot, row,
                fields=fields,
                prompt_version=prompt_version,
                output_path=output_path,
                csv_columns=csv_columns,
                log_lock=log_lock,
                timestamp=timestamp,
            )
        counts[outcome] = counts.get(outcome, 0) + 1
        print(f"[{n}/{total}] {row.get('title', '')[:60]:<60} → {outcome}",
              flush=True)

    # Units that never reached the model still owe the log a row.
    sidecar = batch_manifest.read_skipped(
        batch_manifest.skipped_path(manifest_path),
    )
    for skip in sidecar.get("skipped", []):
        if skip.get("skip_reason") not in batch_manifest.SKIP_REASONS_WITH_ROWS:
            continue
        row = _row_for_skipped_unit(skip, model=run_model, fields=fields)
        apply_coded_row(
            zot, row,
            fields=fields,
            prompt_version=prompt_version,
            output_path=output_path,
            csv_columns=csv_columns,
            log_lock=log_lock,
        )
        key = skip["skip_reason"]
        counts[key] = counts.get(key, 0) + 1

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


def emit_manifest(
    items: list[dict],
    *,
    path: Path,
    system_prompt: str,
    model: str,
    prompt_version: str,
    fields: list[dict],
    library: dict,
    collection: str,
    attachments_by_parent: dict[str, list[dict]],
    pdf_dir: Path | None,
    zotero_storage: Path | None,
    max_input_chars: int,
    mode: str,
    target_fields: list[str] | None,
    selection: dict,
) -> int:
    """Write the manifest and its sidecar, say what happened, exit 0."""
    run_id = batch_manifest.new_run_id(batch_manifest.STAGE_FULLTEXT)
    print(
        f"Extracting full text for {len(items)} item(s) — this is the slow "
        f"part, and it happens here rather than wherever the model runs.",
        flush=True,
    )
    rows, skipped = build_manifest_rows(
        items,
        run_id=run_id,
        system_prompt=system_prompt,
        model=model,
        prompt_version=prompt_version,
        fields=fields,
        library=library,
        collection=collection,
        attachments_by_parent=attachments_by_parent,
        pdf_dir=pdf_dir,
        zotero_storage=zotero_storage,
        max_input_chars=max_input_chars,
        mode=mode,
        target_fields=target_fields,
    )
    sidecar_path = batch_manifest.skipped_path(path)
    if not rows:
        batch_manifest.write_skipped(
            sidecar_path,
            run_id=run_id, stage=batch_manifest.STAGE_FULLTEXT,
            skipped=skipped, n_requested=0, selection=selection,
        )
        sys.exit(
            "ERROR: every candidate was skipped; nothing to emit. See "
            f"{sidecar_path} for why."
        )
    batch_manifest.write_manifest(path, rows)
    batch_manifest.write_skipped(
        sidecar_path,
        run_id=run_id,
        stage=batch_manifest.STAGE_FULLTEXT,
        skipped=skipped,
        n_requested=len(rows),
        selection=selection,
    )
    biggest = max(r["input_chars"] for r in rows)
    print(f"\nWrote {len(rows)} request(s) to {path}")
    print(f"Skipped {len(skipped)}; reasons in {sidecar_path}")
    print(f"run_id: {run_id}")
    print(
        f"Largest request: {biggest:,} chars (~{biggest // 4:,} tokens). "
        f"Check that against the context window of whatever will run it — "
        f"--max-input-chars sends over-long items to the sidecar instead."
    )
    print(
        "\nExecute it wherever the compute is, then:\n"
        f"  --apply-responses <responses.jsonl> --manifest {path}",
    )
    return 0


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
    parser.add_argument("--skip-preflight", action="store_true",
                        help="Skip the one-request check that the provider "
                             "answers for this model. The check costs ~4 "
                             "tokens and catches a spent quota or a dead key "
                             "before the run; skip it only if you have "
                             "already run check_model_connection.py.")
    parser.add_argument("--csv-backfill", action="store_true",
                        help="One-time migration from pre-Zotero-as-truth "
                             "deployments: read CSV decisions and apply "
                             "matching fulltext:* tags for items that don't "
                             "have one yet. Makes no LLM calls; exits after.")
    parser.add_argument("--emit-manifest", default="",
                        help="Assemble the requests this run would send "
                             "(full text included) and write them to PATH as "
                             "JSONL, plus a .skipped.json sidecar, then exit. "
                             "Makes no LLM calls. Execute the manifest "
                             "anywhere, then feed the responses back with "
                             "--apply-responses. A .gz suffix compresses it, "
                             "which matters here: a 240-paper manifest is "
                             "~170 MB uncompressed.")
    parser.add_argument("--apply-responses", default="",
                        help="Apply an executed manifest's responses: write "
                             "CSV rows, fulltext:* tags and SLR Coding notes. "
                             "Requires --manifest too. Makes no LLM calls.")
    parser.add_argument("--manifest", default="",
                        help="The manifest that --apply-responses's file "
                             "answers. Needed because the responses carry "
                             "only answers; the manifest carries which item "
                             "each answer belongs to and which coding fields "
                             "were asked for.")
    parser.add_argument("--max-input-chars", type=int, default=0,
                        help="Emit no request whose user message exceeds N "
                             "characters; over-long items go to the skipped "
                             "sidecar rather than being silently truncated "
                             "(0 = no limit). The built-in 720k cap is ~180k "
                             "tokens — far past what a self-hosted model "
                             "usually serves, so set this to about 4x the "
                             "executor's context window in tokens.")
    parser.add_argument("--force-apply", action="store_true",
                        help="Apply responses even when the run record marks "
                             "the run degenerate, or when the config's coding "
                             "fields have moved on from the manifest's. "
                             "Prints why that is a bad idea first.")
    parser.add_argument("--skip-already-tagged", action="store_true",
                        help="On apply, skip items tagged fulltext:* in "
                             "Zotero since the manifest was emitted, rather "
                             "than overwriting their decision.")
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

    # The manifest's own coding schema wins over the config's, and the
    # CSV columns follow it — read here, before the schema validation
    # below, so a widened config cannot quietly add columns to a run
    # whose model was never asked about them.
    manifest_rows: list[dict] = []
    if args.apply_responses:
        if not args.manifest:
            sys.exit(
                "ERROR: --apply-responses needs --manifest too. The "
                "responses carry answers; the manifest carries which item "
                "each answer belongs to and which fields were asked for."
            )
        _, manifest_rows = batch_manifest.read_manifest(Path(args.manifest))
        manifest_fields = coding_fields_from_manifest(manifest_rows)
        check_coding_fields_match(
            manifest_fields, fields, force=args.force_apply,
        )
        fields = manifest_fields
    csv_columns = _csv_columns(fields)

    # `--dry-run` writes nothing, but `--remote` reads still go to
    # api.zotero.org, which 403s on an empty key. See abstract_screen.py.
    needs_key = not args.dry_run or getattr(args, "remote", False)
    api_key = require("zotero", "api_key",
                      env="ZOTERO_API_KEY") if needs_key else ""
    # Emit and apply call no model, so they must not demand a credential:
    # a user whose compute is elsewhere may not have one at all.
    batch_mode = bool(args.emit_manifest or args.apply_responses)
    if not (args.dry_run or batch_mode):
        llm_provider.require_credentials(model)
        # See abstract_screen.py: a present key is not a working one, and
        # this stage is the expensive one to discover that on item 200.
        if not args.skip_preflight:
            llm_provider.preflight_or_exit(model)


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

    if args.apply_responses:
        return apply_responses(
            zot,
            manifest_path=Path(args.manifest),
            manifest_rows=manifest_rows,
            responses_path=Path(args.apply_responses),
            output_path=output_path,
            items=items,
            fields=fields,
            csv_columns=csv_columns,
            force=args.force_apply,
            skip_already_tagged=args.skip_already_tagged,
        )

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
        _warn_on_preprint_attachments(to_code)
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
            print(cost_estimate_line(
                model, stage="fulltext_coding", n_items=len(to_code),
            ), flush=True)
            return 0

        if args.emit_manifest:
            return emit_manifest(
                to_code,
                path=Path(args.emit_manifest),
                system_prompt=rendered_prompt,
                model=model,
                prompt_version=prompt_version,
                fields=fields,
                library=zot.library_ref(),
                collection=args.collection,
                attachments_by_parent=atts_by_parent,
                pdf_dir=pdf_dir,
                zotero_storage=zotero_storage,
                max_input_chars=args.max_input_chars,
                mode="code",
                target_fields=None,
                selection={
                    "collection": args.collection,
                    "library": zot.library_ref(),
                    "already_tagged": len(tagged),
                    "limit": args.limit,
                    "only_keys": args.only_keys,
                    "rerun": args.rerun,
                    "max_input_chars": args.max_input_chars,
                    "model_hint": model,
                    "prompt_version": prompt_version,
                },
            )

    # Emit and apply never generate, so they never need a provider — and
    # constructing one would demand a credential the user may not have.
    client = None if batch_mode else llm_provider.get_provider(model)
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
            print(cost_estimate_line(
                model, stage="fulltext_coding", n_items=len(to_update),
            ), flush=True)
            return 0

        if args.emit_manifest:
            return emit_manifest(
                to_update,
                path=Path(args.emit_manifest),
                system_prompt=update_prompt,
                model=model,
                prompt_version=prompt_version,
                fields=fields,
                library=zot.library_ref(),
                collection=args.collection,
                attachments_by_parent=atts_by_parent,
                pdf_dir=pdf_dir,
                zotero_storage=zotero_storage,
                max_input_chars=args.max_input_chars,
                mode="update_fields",
                target_fields=sorted(target_fields),
                selection={
                    "collection": args.collection,
                    "library": zot.library_ref(),
                    "update_fields": sorted(target_fields),
                    "limit": args.limit,
                    "only_keys": args.only_keys,
                    "max_input_chars": args.max_input_chars,
                    "model_hint": model,
                    "prompt_version": prompt_version,
                },
            )

        print(paid_run_banner(
            model, stage="fulltext_coding", n_items=len(to_update),
        ), flush=True)

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
                return no_pdf_row(base, fields)
            row = _code_one(item, pdf_path, client, model, update_prompt, fields)
            if row.get("decision") == "error":
                # Surface the failure instead of merging an empty update
                # into the note (which would destroy the error reason and
                # poison the note with blank fields). The note is left
                # untouched so a re-run retries cleanly.
                return row
            return merge_update_into_note(
                zot, row,
                target_fields=target_fields,
                fields=fields,
                prompt_version=prompt_version,
                timestamp=datetime.now(UTC).isoformat(),
            )

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=args.workers
        ) as pool:
            futures = {pool.submit(update_worker, it): it for it in to_update}
            update_fatal: list[str] = []
            for fut in concurrent.futures.as_completed(futures):
                row = fut.result()
                verdict = row.pop("_fatal", "")
                if verdict:
                    update_fatal.append(verdict)
                done_count += 1
                decision = row.get("decision", "error")
                if decision == NO_PDF_DECISION:
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
                outcome = (decision if decision in UNRESOLVED_DECISIONS
                           else "updated")
                print(f"[{done_count}/{total}] {title:<60} → {outcome}",
                      flush=True)

                if update_fatal:
                    for pending in futures:
                        pending.cancel()
                    print("", flush=True)
                    print(update_fatal[0], file=sys.stderr, flush=True)
                    break

        print(f"\n{'=' * 60}")
        print(f"Done. Updated {done_count} of {total} item(s).")
        for k in ("updated", "error", "no_pdf"):
            if counts.get(k, 0):
                print(f"  {k}: {counts[k]}")
        print(f"Log: {output_path}")
        return 1 if update_fatal else 0

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
            return no_pdf_row({
                "item_key": d.get("key", item.get("key", "")),
                "doi": (d.get("DOI") or "").strip(),
                "title": (d.get("title") or "")[:200],
                "year": (d.get("date") or "")[:4],
                "journal": d.get("publicationTitle", "") or "",
                "model": model,
            }, fields)
        return _code_one(item, pdf_path, client, model, rendered_prompt, fields)

    if not batch_mode:
        print(paid_run_banner(
            model, stage="fulltext_coding", n_items=len(to_code),
        ), flush=True)
    print(f"Coding with {args.workers} parallel workers (model={model})...",
          flush=True)

    fatal: list[str] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(worker, it): it for it in to_code}
        for fut in concurrent.futures.as_completed(futures):
            row = fut.result()
            # Carried out of the worker on the row because that is the
            # only channel back; popped before the row reaches the CSV,
            # which has a fixed schema.
            verdict = row.pop("_fatal", "")
            if verdict:
                fatal.append(verdict)
            done_count += 1
            decision = row.get("decision", "error")
            counts[decision] = counts.get(decision, 0) + 1

            apply_coded_row(
                zot, row,
                fields=fields,
                prompt_version=prompt_version,
                output_path=output_path,
                csv_columns=csv_columns,
                log_lock=log_lock,
            )

            title = row.get("title", "")[:60]
            print(f"[{done_count}/{total}] {title:<60} → {decision}",
                  flush=True)

            if fatal:
                for pending in futures:
                    pending.cancel()
                print("", flush=True)
                print(fatal[0], file=sys.stderr, flush=True)
                print(
                    f"\nStopped after {done_count} of {total} papers. Coded "
                    f"rows so far are in {output_path} and tagged in Zotero — "
                    f"re-run after fixing the above and coding resumes from "
                    f"there.",
                    file=sys.stderr, flush=True,
                )
                break

    print(f"\n{'=' * 60}")
    print(f"Done. Coded {done_count} of {total} items.")
    for k in ("include", "exclude", "error", "no_pdf"):
        print(f"  {k}: {counts.get(k, 0)}")
    print(f"Log: {output_path}")
    return 1 if fatal else 0


if __name__ == "__main__":
    sys.exit(main())
