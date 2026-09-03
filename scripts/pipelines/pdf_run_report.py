"""End-of-run reporting for `enrich_pdfs.py`.

`enrich_pdfs.py` can end a run in four different places, and until this
module existed only one of them printed anything — three counters
(`attached / no-pdf / failed`) covering three of the fourteen statuses it
can actually produce. A `--sources browser` run printed no summary at
all.

The cost of that showed up in a real session: asked "why do we have so
many fulltexts not available?", the agent driving the pipeline had to
hand-write a series of throwaway `csv.DictReader` scripts to answer, and
still got it wrong twice — first reporting 110 items as unreachable
paywalls when 48 of them were fully-downloaded PDFs sitting in the local
cache, then reporting 16 more as "no library access" when the user's own
library listing showed otherwise. The information was all in the logs.
Nothing read it back.

So: one report, grouped by status, that says per bucket what happened,
what it means, and what the next lever is — plus per-item citations for
everything still missing, because the user's closing request in that
session was literally "give me the citations of the missing PDFs and I
will investigate".

Pure formatting over the run-log rows. No network, no Zotero, no
pipeline state — `enrich_pdfs.py` passes in an optional metadata lookup
when it has a live client, and the report degrades to DOI + title
without one.
"""

from __future__ import annotations

import os
from collections.abc import Callable, Iterable, Mapping

import shared_orchestrators
from log_schemas import PDF_FETCH_FIELDS

# --- Status taxonomy -------------------------------------------------
#
# Every status `enrich_pdfs.py` writes, in report order. `resolved`
# means the item ended the run with a PDF attached; those are counted
# but never itemised. `lever` is the concrete next action — the thing an
# agent reading this report should tell the user to do, or do itself.

SUCCESS_STATUSES = frozenset({"attached", "attached_via_connector", "dry_run"})

STATUS_INFO: dict[str, tuple[str, str]] = {
    # status: (one-line meaning, next lever)
    "attached": ("PDF attached", ""),
    "attached_via_connector": ("PDF attached via the Zotero Connector", ""),
    "dry_run": ("PDF downloaded, upload skipped (--dry-run)", ""),
    "attached_no_text": (
        "PDF attached and structurally intact, but no text can be extracted",
        "Most likely a bad copy, NOT a scan. Every textless file in the "
        "incident behind this check came back perfect from a different "
        "source (3 via Wiley TDM, 2 via the Sage browser handler, 19-44 "
        "real pages each); none needed OCR. Retry with `--replace` against "
        "the publisher's own route (`--sources wiley --replace` / "
        "`--sources elsevier --replace` / `--sources browser --replace`). "
        "The flag is needed because the item otherwise looks complete and "
        "every run skips it; it swaps the new file in only once that file "
        "is attached, so a retry that finds nothing leaves you the copy "
        "you already had. Only if a different source returns the "
        "same textless file is this plausibly a genuine scan, which "
        "would need OCR this pipeline does not do.",
    ),
    "rejected_corrupt_pdf": (
        "A source returned a broken file (usually truncated) — not attached",
        "Deliberately not attached: a corrupt PDF makes the item look "
        "done forever, since later runs see an attachment and skip it. "
        "Retry via a DIFFERENT source — a provider serving a truncated "
        "copy tends to keep serving it, byte for byte, while the "
        "publisher's own route returns the file intact. Try "
        "`--sources wiley` / `--sources elsevier` for that publisher, or "
        "`--sources browser`.",
    ),
    "upload_failed": (
        "PDF fetched successfully but could not be attached to Zotero",
        "NOT an access problem — the file is in the PDF cache. Re-run "
        "enrich_pdfs.py; the cache-recovery pass attaches it without "
        "re-fetching. If it keeps failing, check that Zotero is reachable "
        "and the API key has write access to this library.",
    ),
    "skipped_no_pdf": (
        "Every fetch route tried and came back empty",
        "For Cloudflare-gated publishers (Sage, APA, T&F, Emerald, AoM, "
        "INFORMS, OUP, Wiley, AAA) the automated cascade structurally "
        "cannot reach the PDF — re-run with `--sources browser` from a "
        "real terminal. Otherwise this is a genuine no-open-copy result.",
    ),
    "skipped_no_library_coverage": (
        "Library link-resolver reported no licensed full-text route",
        "Verify against your library's own journal listing before "
        "trusting this — the resolver keys on DOI and returns nothing "
        "for journals the library reaches via an aggregator. If your "
        "library does have access, re-run with "
        "`--ignore-library-coverage`.",
    ),
    "skipped_no_access": (
        "Publisher skipped at the setup prompt",
        "You answered [n]o / [A]lways-skip for this publisher. Clear it "
        "from `[library] no_access` in config.toml to try again.",
    ),
    "skipped_by_user": (
        "Host skipped at the Connector prompt",
        "You skipped this host during the Connector pass. Re-run to retry.",
    ),
    "downloaded_no_item": (
        "PDF downloaded but the row carried no Zotero item key",
        "The PDF is in the cache but has nowhere to go — usually means "
        "the item was deleted from Zotero mid-run.",
    ),
    "connector_zotero_unavailable": (
        "Zotero Desktop was not reachable during the Connector pass",
        "Start Zotero Desktop (the Connector saves through it) and re-run.",
    ),
    "connector_wrong_library": (
        "Connector was pointed at a different library",
        "Switch Zotero Desktop to the target library and re-run.",
    ),
    "connector_extension_missing": (
        "Zotero Connector browser extension was not found",
        # NOT `<cache-dir>/.chrome-profile-connector`, which this used to
        # say: that is the Playwright profile the extension is
        # --load-extension'd into, not somewhere a user installs
        # anything. It also contradicted the correct hint printed by
        # enrich_pdfs.py moments earlier.
        "Install the Zotero Connector in Google Chrome from "
        "https://www.zotero.org/download/connectors/ (not Chrome for "
        "Testing), then re-run the setup wizard so it is located.",
    ),
    "connector_setup_failed": (
        "Connector setup did not complete",
        "Re-run the Connector pass and complete the setup prompt.",
    ),
    "connector_sw_timeout": (
        "Connector's service worker did not come up in time",
        "Transient — re-run. If it persists, restart Chromium/Zotero.",
    ),
    "connector_save_failed": (
        "Connector opened the page but could not save the item",
        "Often a login/paywall wall behind the resolver link. Re-run and "
        "watch the browser window for what the page actually shows.",
    ),
}

UNKNOWN_STATUS = ("Unrecognised status", "")


def read_log(path: str | os.PathLike) -> list[dict[str, str]]:
    """Read a `pdf_attach_log.csv` into row dicts. Empty list if missing.

    Delegates to `shared_orchestrators.read_log_rows`, which tolerates a
    headerless file. That is not a hypothetical shape: `open_log` writes
    a header only when it creates the file, so real logs exist with
    thousands of rows and no header, and a plain `DictReader` would eat
    the first record and mislabel the rest.
    """
    if not os.path.isfile(path):
        return []
    return shared_orchestrators.read_log_rows(str(path), PDF_FETCH_FIELDS)


def latest_rows(rows: Iterable[Mapping[str, str]]) -> list[dict[str, str]]:
    """Collapse the append-only log to one row per item — the last one.

    The run-log is append-only and never deduped, so an item retried
    across runs carries a row per attempt. A report that counted those
    naively would double-count, and worse, would keep reporting an old
    `upload_failed` for an item that has since attached cleanly.
    """
    by_item: dict[str, dict[str, str]] = {}
    for row in rows:
        key = (row.get("item_key") or "").strip() or (row.get("doi") or "").strip()
        if not key:
            continue
        by_item[key] = dict(row)
    return list(by_item.values())


def group_by_status(
    rows: Iterable[Mapping[str, str]],
) -> dict[str, list[dict[str, str]]]:
    """Bucket rows by status, ordered by `STATUS_INFO` then alphabetically."""
    buckets: dict[str, list[dict[str, str]]] = {}
    for row in rows:
        buckets.setdefault((row.get("status") or "").strip(), []).append(dict(row))

    order = list(STATUS_INFO)
    return dict(
        sorted(
            buckets.items(),
            key=lambda kv: (
                order.index(kv[0]) if kv[0] in order else len(order),
                kv[0],
            ),
        )
    )


def _citation(row: Mapping[str, str], meta: Mapping[str, str] | None) -> str:
    """One-line citation for an unavailable item.

    Falls back through whatever is available: the run-log only carries
    DOI + a 70-char title, so a caller that can reach Zotero passes
    richer metadata in.
    """
    meta = meta or {}
    authors = (meta.get("authors") or "").strip()
    year = (meta.get("year") or "").strip()
    journal = (meta.get("journal") or "").strip()
    title = (meta.get("title") or row.get("title") or "").strip()
    doi = (row.get("doi") or "").strip()

    head = " ".join(p for p in (authors, f"({year})" if year else "") if p)
    parts = [p for p in (head, title, journal) if p]
    line = ". ".join(parts) if parts else "(no metadata)"
    return f"{line} https://doi.org/{doi}" if doi else line


def format_report(
    rows: Iterable[Mapping[str, str]],
    *,
    metadata: Callable[[str], Mapping[str, str]] | None = None,
    max_items_per_bucket: int | None = None,
) -> str:
    """Render the end-of-run report.

    `metadata` is an optional `item_key -> {authors, year, journal,
    title}` lookup used to upgrade the per-item lines into real
    citations; without it they degrade to title + DOI.
    """
    rows = latest_rows(rows)
    if not rows:
        return "PDF run report: no log rows found."

    buckets = group_by_status(rows)
    resolved = sum(len(v) for k, v in buckets.items() if k in SUCCESS_STATUSES)
    unresolved = len(rows) - resolved

    out: list[str] = []
    out.append("")
    out.append("=" * 68)
    out.append(f"PDF run report — {len(rows)} items")
    out.append(
        f"  {resolved} with a PDF attached, {unresolved} still without"
    )
    out.append("=" * 68)

    for status, items in buckets.items():
        meaning, lever = STATUS_INFO.get(status, UNKNOWN_STATUS)
        out.append("")
        out.append(f"{status}  ({len(items)})")
        out.append(f"  {meaning}.")
        if lever:
            out.append(f"  → {lever}")

        if status in SUCCESS_STATUSES:
            continue

        shown = items if max_items_per_bucket is None else items[:max_items_per_bucket]
        out.append("")
        for row in shown:
            meta = metadata(row.get("item_key", "")) if metadata else None
            out.append(f"    • {_citation(row, meta)}")
            detail = (row.get("detail") or "").strip()
            if detail:
                out.append(f"      {detail}")
        if len(shown) < len(items):
            out.append(f"    … and {len(items) - len(shown)} more")

    out.append("")
    return "\n".join(out)
