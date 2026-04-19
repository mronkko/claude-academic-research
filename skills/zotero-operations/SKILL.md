---
name: zotero-operations
description: Use when performing Zotero reference-management operations outside a systematic review — importing items, deduplication, attaching PDFs, maintaining BBT citation keys, enriching metadata, or writing structured child notes. Covers the patterns for mixing the local Zotero client (fast bulk reads) with the remote API (writes), and the three-check dedup protocol.
---

# zotero-operations

## Pre-flight (ALWAYS run first)

Before any step below, verify the plugin has been configured:

```bash
test -f ~/.config/academic-research/config.toml && echo "configured" || echo "NOT CONFIGURED"
```

If the result is `NOT CONFIGURED`, stop immediately and tell the user:

> The academic-research plugin has not been set up on this machine
> yet. Run `/setup` first to configure API keys, MCP servers, and
> permission rules. Do not attempt Zotero operations before that.

Do not call MCP tools, run scripts, or proceed with the procedure.
`/setup` is the required first step.

If the result is `configured`, proceed.

---

For SLR-specific operations (bulk screening, coding, QA tags), use the
`systematic-review` skill. This skill covers general Zotero patterns
that apply outside an SLR context.

## Local client for reads, remote for writes

`pyzotero.zotero.Zotero(group, "group", key, local=True)` reads from
`localhost:23119` (Zotero must be running). Much faster than the remote
API for bulk operations — a library of a few thousand items that would
time out on `api.zotero.org` returns in milliseconds from the local
client.

Use the remote API (`api.zotero.org`) for writes: PATCH, new items,
child notes, tag updates.

## Citation keys (Better BibTeX)

- BBT keys are auto-generated from author/year/title (e.g.,
  `brownUsingDailyStock1985a`).
- Generate the project's bibliography with
  `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/generate_bib.py <project_dir>`.
- Never hand-craft keys like `Smith2019`.
- Never write to the Zotero `Extra` field to override or pin BBT keys.
- BBT keys resolve via the local JSON-RPC endpoint:
  `http://localhost:23119/better-bibtex/json-rpc`.

## Bulk attachment map

For operations that need to classify every item's attachment state,
fetch all attachments in one pass:

```python
attachments = local.everything(local.items(itemType="attachment"))
by_parent = {}
for a in attachments:
    parent = a["data"].get("parentItem")
    if parent:
        by_parent.setdefault(parent, []).append(a)
```

Classify into real files (has `md5`) vs. empty stubs (no `md5`). Avoids
N+1 remote queries. Delete empty PDF stubs before processing — Zotero
creates these when a PDF import fails.

## PDF upload (3-step protocol)

1. POST to `/items/{key}/file` with `md5`, `filename`, `filesize`,
   `mtime` → get S3 upload authorization.
2. POST to S3 URL with `prefix + pdf_bytes + suffix` from the
   authorization response.
3. POST to `/items/{key}/file` with `upload={uploadKey}` to register.

Validate PDFs before upload: `%PDF` magic bytes AND parse-test (some
downloaders save HTML-with-200 or corrupted PDFs that pass magic-bytes
but fail to parse).

## Import dedup — three checks in order

Creating duplicates has three distinct failure modes. Any import script
must handle all three:

1. **Against the existing Zotero library.** Match each input row by DOI,
   falling back to `normalised_title|first_author_lastname`. If matched,
   add to the target collection and backfill the abstract if empty.

2. **Within the import batch itself.** As the loop processes rows, keep
   growing sets of `batch_doi_seen` and `batch_title_seen`. A second row
   for the same paper (e.g. Scopus + WoS where only one has a DOI) must
   merge into the already-queued item, not create a new one.

3. **Post-import.** Always run `mcp__zotero__zotero_find_duplicates` at
   the end of the import. Pre-existing library items with incomplete
   metadata can slip past the first two checks; the post-check is the
   safety net.

**Fix the data, don't work around it.** If post-import surfaces duplicates,
audit the upstream source first (search-API field mapping, manual
entries, out-of-scope items), fix them, re-run. Only add new fallback
matching after confirming the missing metadata is legitimate.

## Surface structured data in Zotero

When a pipeline writes decisions or structured extractions back to
Zotero (e.g. LLM screening decisions, coded fields), make them
reviewable in Zotero itself:

- **Tag** every processed item with the decision (e.g.
  `fulltext:include` / `fulltext:exclude`).
- **Child note** with structured fields as HTML on includes (e.g.
  `SLR Coding`). The local Zotero client reads item version + existing
  tags; the remote API writes PATCH and the child note.
- On `--full-recode`, delete prior named child notes before re-writing
  so re-runs don't accumulate stale notes.

## Adding to Zotero (one-off)

- Use `mcp__zotero__zotero_add_by_doi` when a DOI exists (preferred).
- Use `mcp__zotero__zotero_add_by_url` only when no DOI exists.
- After adding, retrieve the BBT key via
  `mcp__zotero__zotero_get_item_metadata` with `format="bibtex"`. The
  key is the first argument of the BibTeX entry.

## Red flags

- You are using the remote API for bulk reads (will time out on
  libraries > 1000 items).
- You are hand-crafting a citation key.
- You are writing to the Zotero `Extra` field to pin a citation key.
- You are uploading a PDF without magic-byte + parse validation.
- You are adding an import-dedup fallback (fuzzy match, author+year
  heuristic) without first surfacing the DOI-less records.
- You are letting the local client do a write (use remote API).
- You are re-running a pipeline with `--full-recode` but not deleting
  prior child notes first.
