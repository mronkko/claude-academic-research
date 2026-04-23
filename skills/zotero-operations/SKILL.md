---
name: zotero-operations
description: Use when the user asks to work with a Zotero library — adding missing abstracts, attaching missing PDFs, enriching metadata, importing items, deduplicating, fixing BBT (Better BibTeX) citation keys, or writing structured child notes. Common trigger phrases the harness should match on: "add abstracts to Zotero", "attach PDFs", "enrich my Zotero library", "fix citation keys", "find duplicates in Zotero", "update Zotero items". Do NOT use for a full PRISMA-style systematic review — use the `systematic-review` skill instead.
---

# zotero-operations

## Pre-flight (ALWAYS run first)

Before any step below, verify the plugin has been configured:

```bash
python -c "from pathlib import Path; print('configured' if (Path.home()/'.config'/'academic-research'/'config.toml').is_file() else 'NOT CONFIGURED')"
```

If the result is `NOT CONFIGURED`, stop immediately and tell the user:

> The academic-research plugin has not been set up on this machine
> yet. Run `/setup` first to configure API keys, MCP servers, and
> permission rules. Do not attempt Zotero operations before that.

Do not call MCP tools, run scripts, or proceed with the procedure.
`/setup` is the required first step.

If the result is `configured`, proceed.

---

## Relationship to `systematic-review` — who owns enrichment?

Both this skill and `systematic-review` list the enrichment scripts
(`enrich_abstracts.py`, `enrich_pdfs.py`, `enrich_dois.py`,
`audit_zotero_library.py`). **The scripts are the same; the operational
context differs.** The decision is simple:

- **Use `systematic-review`** when enrichment is part of a PRISMA-style
  pipeline that will flow into abstract screening and full-text coding.
  Stage tags (`abstract:*`, `fulltext:*`), the screening-config
  round-trip, QA evaluator agents, and export to `coded_papers.csv`
  are all in scope. The audit report drives which items need
  enrichment *before screening can start*.
- **Use this skill** when the work is **standalone library
  housekeeping** — the user has a Zotero collection (SLR or not) and
  wants missing abstracts filled, missing PDFs attached, BBT keys
  fixed, duplicates found, or a one-off Zotero query answered. No
  downstream screening / coding step is planned.

**Signal for the harness.** If the user's prompt mentions PRISMA,
systematic review, screening, inclusion criteria, coding, QA
evaluators, adjudication, or anything that implies a full-text
review pipeline — route to `systematic-review`. If it's
"just add abstracts / PDFs / tags to my Zotero library", stay here.
A half-SLR library that also needs housekeeping is still SR work:
delegate to `systematic-review` and note the housekeeping step is
a sub-task of that pipeline, not an independent operation.

**Overlap is not redundancy.** The same script (`enrich_pdfs.py`)
behaves identically whether called from SR context or ad-hoc
context — the scripts don't know which skill invoked them. What
differs is **what comes next**: SR context expects
`abstract_screen.py` to read the enriched library; ad-hoc context
stops after enrichment.

## Pipeline scripts — direct path, no probing

Do **not** list the plugin's `scripts/pipelines/` directory to figure
out what is available. The mapping below is authoritative; use the
exact invocation.

| User intent | Script | Invocation |
|---|---|---|
| Audit a library for items missing abstracts / PDFs / empty stubs | `audit_zotero_library.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/audit_zotero_library.py --group <id>` |
| Add missing abstracts to items | `enrich_abstracts.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_abstracts.py --filter-keys-file .claude/audit/audit.missing_abstract.keys` |
| Attach missing PDFs (fast HTTP cascade) | `enrich_pdfs.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py --filter-keys-file .claude/audit/audit.missing_pdf.keys` |
| Attach PDFs from Wiley journals (TDM token route) | `enrich_pdfs.py --sources wiley` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py --sources wiley --filter-keys-file .claude/audit/audit.missing_pdf.keys` |
| Attach PDFs from Cloudflare-gated publishers (Sage, APA, T&F, Emerald, …) | `enrich_pdfs.py --sources browser` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py --sources browser --filter-keys-file .claude/audit/audit.missing_pdf.keys` |
| Generate `references.bib` from a manuscript's citation keys | `generate_bib.py` | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/generate_bib.py <project_dir>` |

The audit script writes both a JSON report and three `.keys` files
(`.claude/audit/audit.{missing_abstract,missing_pdf,empty_stubs}.keys`)
— feed them straight to the next stage's `--filter-keys-file` flag.
**Do not improvise a `jq` step to extract keys**; the script wrote them
for you.

Each script reads API keys from `~/.config/academic-research/config.toml`
(the `/setup` wizard writes it) inside its own process via
`core.config_loader`. **The keys never pass through your tool layer.**

### Narrate before surprising the user

Some pipeline stages do things the user may find startling if
unannounced. **Always tell the user what is about to happen before
running these stages:**

- `enrich_pdfs.py --sources browser` — opens a visible Chromium window
  on their desktop; they may need to solve a Cloudflare challenge or
  sign in via institutional SSO. Tell them *before* launching:
  *"Next step: browser-based PDF fetcher. A Chromium window will
  open on your desktop. For each publisher you may need to click
  through a Cloudflare challenge once. Ready?"* and wait for
  acknowledgement.
- `enrich_pdfs.py` on a large library — can take 5–15 minutes with
  the default multi-source cascade. Warn if > 20 items.
- `enrich_pdfs.py --sources wiley` — silent HTTP via the Wiley TDM
  token, no warning needed.
- First run of any `uv run` command installs Python dependencies
  (~1–20 s). Mention it if noticeable.

### Canonical workflow for "add missing abstracts and PDFs to a library"

1. Identify the Zotero library the user means (ask if ambiguous). Use
   `mcp__zotero__zotero_list_libraries` if you need to see what is
   available. Never guess the group ID.
2. Run `audit_zotero_library.py --group <id>`. Read the summary counts.
   The script writes `.claude/audit/audit.{missing_abstract,missing_pdf,
   empty_stubs}.keys` alongside the JSON report (project-local).
3. Report counts to the user and ask which to fix (missing abstracts,
   missing PDFs, empty stubs, or all).
4. Run the stage(s) the user chose, passing the matching `.keys` file
   to `--filter-keys-file`. The audit script prints the exact commands
   in its "Next steps" output — use those verbatim.
5. Re-run the audit to confirm counts dropped.

### Optional: retraction check

Retracted papers in a Zotero library are a silent data-quality
problem — citing a retracted paper is a fact-check failure mode the
author almost certainly wants to catch. Scite exposes a free
retraction-watch endpoint that the Zotero MCP server wraps as
`mcp__zotero__scite_check_retractions` (no Scite account required).

**Offer the check as a post-audit step** when any of the following
is true: the library is being prepared for submission, the user
mentions bibliography hygiene / citation integrity, or the audit
report shows a mature library (no stubs, few missing abstracts). The
check queries each DOI in the collection against the retraction
registry and reports matches.

Invocation (agent-mediated — the pipeline script can't call MCP tools
directly):

```
mcp__zotero__scite_check_retractions(
    group_id=<group>,
    collection_key=<collection>,
)
```

Report any retracted items to the user with the matching citation
key; ask whether to tag them (`retracted:flag` is the convention)
and/or remove them from the collection. **Flag, don't auto-remove** —
the author decides. For SLR projects where retraction screening is
part of PRISMA quality assessment, the `systematic-review` skill
has the equivalent step inside its pipeline.

### Do not improvise

If the user's request does not clearly map to one of the rows above,
**ask before acting**. Specifically:

- Do **not** probe the plugin directory with `ls` to see what scripts
  exist (they are listed here — this is authoritative).
- Do **not** write a Bash heredoc or a Python script to read
  Zotero / config / library data yourself. Use the shipped scripts.
- Do **not** extract values from `~/.config/academic-research/config.toml`
  under any circumstance — scripts read it internally.

If you truly need an operation the table above does not cover, tell
the user which operation is missing and propose adding a new shipped
script to the plugin. A one-off improvised script has no place here —
it breaks the security model (API keys flow through your context)
and sidesteps pre-approved permissions.

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
- You are about to read `~/.config/academic-research/config.toml` via
  `cat`, `head`, `tail`, `grep`, `less`, `more`, `awk`, `sed`, a
  Python script, or any other command. **NEVER read that file.** It
  holds API keys. Pipeline scripts read it via Python's `open()`
  outside your tool layer; you have no legitimate reason to inspect
  it. If you feel like you need to debug by looking inside, you are
  on the wrong track — ask the user to re-run `/setup` instead.
- You are about to write a Bash heredoc or an inline Python script to
  do Zotero work. **Never improvise.** Use the shipped scripts in
  the intent-to-script table above. If nothing fits, ask the user
  whether to add a new shipped script — don't write a one-off.
