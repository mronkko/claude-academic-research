#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
# ]
# ///
"""Audit a Zotero library for items missing abstracts and / or PDF attachments.

Prints a one-line summary to stdout and writes a JSON file listing the
actionable item keys. Intended to drive subsequent pipeline stages
(fetch_abstracts.py, attach_pdfs.py) via their --filter-keys-file arg.

Usage:
    audit_zotero_library.py --group 6015547
    audit_zotero_library.py --group 6015547 --output analysis/raw/audit.json
    audit_zotero_library.py --user  # audit the personal library instead

The script reads the Zotero API key from the plugin config
(~/.config/academic-research/config.toml) via core.config_loader. The
API key never crosses into Claude's tool layer.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from core.config_loader import require  # noqa: E402

try:
    from pyzotero.zotero import Zotero
except ImportError:
    sys.exit(
        "ERROR: pyzotero not installed. Run via `uv run` — the PEP 723 "
        "block at the top of this script declares the dependency and "
        "uv will install it into an ephemeral venv automatically."
    )


def _classify(items: list[dict], attachments_by_parent: dict[str, list[dict]]) -> dict:
    missing_abstract: list[dict] = []
    missing_pdf: list[dict] = []
    empty_stubs: list[dict] = []
    have_pdf = 0

    for it in items:
        d = it.get("data", {})
        item_type = d.get("itemType", "")
        if item_type in ("attachment", "note", "annotation"):
            continue

        key = d.get("key") or it.get("key")
        if not key:
            continue

        title = (d.get("title") or "")[:80]
        doi = d.get("DOI") or ""
        identifier = {"key": key, "title": title, "doi": doi}

        if not (d.get("abstractNote") or "").strip():
            missing_abstract.append(identifier)

        atts = attachments_by_parent.get(key, [])
        pdfs = [a for a in atts if a.get("data", {}).get("contentType") == "application/pdf"]
        real = [a for a in pdfs if a.get("data", {}).get("md5")]
        stubs = [a for a in pdfs if not a.get("data", {}).get("md5")]

        if real:
            have_pdf += 1
        elif stubs:
            stub_keys = [s.get("data", {}).get("key") for s in stubs]
            empty_stubs.append({**identifier, "stub_keys": stub_keys})
        else:
            missing_pdf.append(identifier)

    return {
        "total_items": sum(
            1 for it in items
            if it.get("data", {}).get("itemType") not in ("attachment", "note", "annotation")
        ),
        "have_pdf": have_pdf,
        "missing_pdf_count": len(missing_pdf),
        "empty_stub_count": len(empty_stubs),
        "missing_abstract_count": len(missing_abstract),
        "missing_abstract": missing_abstract,
        "missing_pdf": missing_pdf,
        "empty_stubs": empty_stubs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--group", help="Zotero group library ID (numeric)")
    target.add_argument(
        "--user", action="store_true",
        help="Audit the personal (user) library instead of a group.",
    )
    parser.add_argument(
        "--output", default="/tmp/zotero_audit.json",
        help="Path to write JSON report (default: /tmp/zotero_audit.json).",
    )
    parser.add_argument(
        "--local", action="store_true", default=True,
        help="Use the local Zotero client (requires Zotero running). Default.",
    )
    parser.add_argument(
        "--remote", action="store_true",
        help="Use the remote api.zotero.org instead of the local client.",
    )
    args = parser.parse_args()

    api_key = require("zotero", "api_key", env="ZOTERO_API_KEY")
    if args.user:
        user_id = require("zotero", "user_id", env="ZOTERO_USER_ID")
        library_id, library_type = user_id, "user"
    else:
        library_id, library_type = args.group, "group"

    use_local = not args.remote
    print(f"Connecting to Zotero ({'local' if use_local else 'remote'}, "
          f"{library_type}={library_id})...", flush=True)
    zot = Zotero(library_id, library_type, api_key, local=use_local)

    print("Fetching top-level items...", end=" ", flush=True)
    items = zot.everything(zot.top())
    print(f"{len(items)} fetched.", flush=True)

    print("Fetching attachments...", end=" ", flush=True)
    attachments = zot.everything(zot.items(itemType="attachment"))
    print(f"{len(attachments)} fetched.", flush=True)

    by_parent: dict[str, list[dict]] = {}
    for a in attachments:
        parent = a.get("data", {}).get("parentItem")
        if parent:
            by_parent.setdefault(parent, []).append(a)

    report = _classify(items, by_parent)

    out_path = Path(args.output)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    # Write one-key-per-line files next to the JSON so downstream pipeline
    # stages can consume them via --filter-keys-file without any jq step.
    stem = out_path.with_suffix("")  # strip .json
    keys_files: dict[str, Path] = {}
    for category in ("missing_abstract", "missing_pdf", "empty_stubs"):
        keys_path = Path(f"{stem}.{category}.keys")
        keys_path.write_text(
            "\n".join(entry["key"] for entry in report.get(category, [])) + "\n"
            if report.get(category) else "",
            encoding="utf-8",
        )
        keys_files[category] = keys_path

    print()
    print(f"Library audit ({library_type} {library_id})")
    print(f"  Total items:                {report['total_items']}")
    print(f"  Have PDF:                   {report['have_pdf']}")
    print(f"  Missing PDF:                {report['missing_pdf_count']}")
    print(f"  Empty PDF stubs:            {report['empty_stub_count']}")
    print(f"  Missing abstract:           {report['missing_abstract_count']}")
    print(f"  Details written to:         {out_path}")
    print(f"  Keys files written to:      {stem}.{{missing_abstract,missing_pdf,empty_stubs}}.keys")
    print()
    print("Next steps — feed the .keys files directly into pipeline stages:")
    if report["missing_abstract_count"]:
        print(f"  uv run {SCRIPT_DIR}/fetch_abstracts.py "
              f"--filter-keys-file {keys_files['missing_abstract']}")
    if report["missing_pdf_count"]:
        print(f"  uv run {SCRIPT_DIR}/attach_pdfs.py "
              f"--filter-keys-file {keys_files['missing_pdf']}")
    if report["empty_stub_count"]:
        print(f"  # {report['empty_stub_count']} empty stubs to delete; see "
              f"{keys_files['empty_stubs']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
