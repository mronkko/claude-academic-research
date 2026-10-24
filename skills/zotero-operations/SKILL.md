---
name: zotero-operations
description: Use when the user asks to work with a Zotero library — adding abstracts, enriching metadata, importing items, deduplicating, fixing BBT (Better BibTeX) citation keys, or writing child notes. Trigger phrases "add abstracts to Zotero", "enrich Zotero library", "fix citation keys", "find duplicates in Zotero", "update Zotero items". Do NOT use for finding or attaching PDFs — use `retrieving-pdfs`; for a full PRISMA-style systematic review — use `systematic-review`.
---

# zotero-operations

> **Glossary:** unfamiliar with **MCP**, **BBT**, **DOI**, **ISSN**?
> See [skills/_glossary.md](../_glossary.md) for one-line definitions
> of every acronym this skill uses.

**Plugin tools.** Steps name the tools of the plugin's own
**academic-research** MCP server by bare name (`check_setup`,
`audit_library`, `start_enrich_abstracts`, …); in Claude Code they appear
as `mcp__plugin_academic-research_academic-research__<name>`, on other
hosts with that host's prefix. `start_*` tools are **jobs**: follow one
with `job_status`; when it is `awaiting_user`, show the user its `prompt`
and pass *their* answer to `job_answer` — never answer it yourself; stop
it with `cancel_job`. If the tools are not available, run the *terminal
equivalent* given beside each with Bash instead, and tell the user once
how to enable the server (see **Terminal equivalent** in the glossary).

## Pre-flight (ALWAYS run first)

Before any step below, verify the plugin has been configured: call
**`check_setup`** (terminal equivalent:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_configured.py"`).

If it reports `configured: false` (`NOT CONFIGURED`), stop immediately and tell the user:

> The academic-research project has not been set up on this machine
> yet. Run the setup skill (`/setup`) first to configure API keys and
> MCP servers. Do not attempt Zotero operations before that.

Do not call MCP tools, run scripts, or proceed with the procedure.
Running the setup skill/wizard is the required first step.

If it reports `configured: true`, proceed.

The same `check_setup` result also gives the `zotero-mcp-server` version
the MCP configuration actually launches (`zotero_mcp_server`) — an
install below the floor (0.11) silently loses tool names this skill
documents and adds identifiers one at a time instead of in batches
(terminal equivalent:
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_zotero_mcp_version.py"`).

A `zotero_mcp_warnings` entry (a `WARNING` line) means the user should upgrade before relying on
`mcp__zotero__*` tools; it is informational, not a hard gate — proceed
if `zotero-cli` and `zotero_io.py` still cover what you need.

---

## Relationship to `systematic-review` — who owns enrichment?

Both this skill and `systematic-review` list the enrichment scripts
(`enrich_abstracts.py`, `enrich_pdfs.py`, `enrich_dois.py`,
`audit_zotero_library.py`). **The scripts are the same; the operational
context differs.** The decision is simple:

- **Use `systematic-review`** when enrichment is part of a PRISMA-style
  pipeline that will flow into abstract screening and full-text coding.
  Stage tags (`<prefix>/abstract:*`, `<prefix>/fulltext:*`, namespaced
  under the project's `TAG_PREFIX`), the screening-config round-trip, QA
  evaluator agents, and export to `coded_papers.csv` are all in scope. The audit report drives which items need
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

**PDF retrieval belongs to neither.** Finding and attaching PDFs —
planning the retrieval with the user, the API cascade, paid OpenAlex, the
browser and Connector passes, manual download, and diagnosing what is
left — is the `retrieving-pdfs` skill, whichever of the two calls it.

**Overlap is not redundancy.** The same script (`enrich_pdfs.py`)
behaves identically whether called from SR context or ad-hoc
context — the scripts don't know which skill invoked them. What
differs is **what comes next**: SR context expects
`abstract_screen.py` to read the enriched library; ad-hoc context
stops after enrichment.

## Pipeline steps — direct path, no probing

Do **not** list the plugin's `scripts/pipelines/` directory to figure
out what is available. The mapping below is authoritative: call the
tool; the script column is the terminal equivalent. The rows without a
tool have none yet — run their script with Bash.

| User intent | Tool | Terminal equivalent |
|---|---|---|
| Audit a library for items missing abstracts / PDFs / empty stubs | `audit_library` (`group` or `user`) | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/audit_zotero_library.py --group <id>` |
| Add missing abstracts to items | `start_enrich_abstracts` (job; `filter_keys_file: ".claude/audit/audit.missing_abstract.keys"`) | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_abstracts.py --filter-keys-file .claude/audit/audit.missing_abstract.keys` |
| Validate DOIs / find missing ones | `start_enrich_dois` (job; `filter_keys_file: ".claude/audit/audit.missing_doi.keys"`; a 2/3 match is asked through `job_status` — relay it) | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_dois.py --all --filter-keys-file <keys>` |
| Repair stored abstracts (copyright notices, HTML entities, "Abstract" headings) | — | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/clean_abstracts.py` (dry run; read the CSV it writes, then add `--write` with the user's OK) |
| Attach missing PDFs (any route: API, paid, browser, Connector, manual) | **REQUIRED SUB-SKILL: retrieving-pdfs** — it plans with the user, then runs each mode; scope it with `.claude/audit/audit.missing_pdf.keys` | — |
| Replace PDFs an item already has (repair a bad or superseded copy) | — | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py --replace --filter-keys-file <keys>` |
| Generate `references.bib` from a manuscript's citation keys | `generate_bib` (scans `manuscript/`, writes `manuscript/references.bib`; `chapters_dir` / `bib_out` for other layouts) | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/generate_bib.py` from the project root |
| Tag supplements, front matter and notices (and relate supplements to their article) | — | `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/tag_doctypes.py --group <id> [--collection <key>]` (dry run; add `--apply` with the user's OK) |

**`doctype:*` tags mark records that are typed `journal-article` but are
not research articles**: `doctype:supplement` (APA supplemental material,
DOI `<article DOI>.supp`), `doctype:front-matter` ("Editorial Board",
"Issue Information", "Featured Cover", "Acknowledgment of Ad Hoc
Reviewers", author-less "Call for Papers: …"), `doctype:notice` (SAGE
"Publication Notice" retraction/withdrawal notices). They are global, not
review-namespaced — a fact about the record, like `predatory:flag`.
`import_to_zotero.py` applies them on import and relates each supplement
to its article (Zotero "Related", both directions) when the article is in
the batch or library. Keep these items; never delete them to "clean up".
Screening skips them and counts them for PRISMA.

**An item that already holds a PDF never reaches a fetcher.** That is
usually right — it is what stops every run re-downloading a whole
library — but it also meant the only route to a *better* copy was to
delete the one you have and hope a replacement arrives. `--replace`
re-admits those items and swaps on success only: the new file is fetched
and attached first, and the old attachment goes to Zotero's trash after that (recoverable, never deleted outright). A
retry that finds nothing leaves the library exactly as it was, so the
flag is safe to run across a whole corpus rather than one cautious item
at a time. Use it when an attached file is textless, truncated, or was
generated by a superseded version of the pipeline. It ignores the resume
log by design, and costs one extra pass over the library's attachments.

TDM-recovered PDFs (`pdf:tdm-recovered`) made before 0.24.1 have **no
tables or figure captions**: Elsevier keeps those outside the article
body, and the XML transformation never rendered them. The prose still
cites "Table 2", so the gap is easy to miss. Tell the user before anyone
codes data from those tables. The repair is `--replace --filter-keys-file
<audit-stem>.tdm_recovered.keys`. Recovery itself is off by default
(`[pdf] reconstructed`; the older `[elsevier] render_xml_to_pdf` still
works as a fallback), and while it is off that run swaps in a
real PDF wherever another source has one, and leaves the rest untouched.
Only with the option on does it re-render them (one Elsevier API call per
item; older cached recoveries count as stale and are not reused).

The audit script writes both a JSON report and five `.keys` files
(`.claude/audit/audit.{missing_abstract,missing_pdf,missing_doi,
empty_stubs,tdm_recovered}.keys`), plus a set of `retry.*` /
`true_negative` key files when a PDF-fetch failure log is present
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

- PDF retrieval — `retrieving-pdfs` announces the browser window, the
  manual-download tabs and long runs itself, step by step.
- The first run of any plugin tool or `uv run` command installs Python
  dependencies (~1–20 s). Mention it if noticeable.

### Canonical workflow for "add missing abstracts and PDFs to a library"

1. Identify the Zotero library the user means (ask if ambiguous). Use
   `mcp__zotero__zotero_list_libraries` if you need to see what is
   available. Never guess the group ID.
2. Call `audit_library` with the library (`group` / `user`). Read the
   summary counts. The audit writes `.claude/audit/audit.{missing_abstract,missing_pdf,
   missing_doi,empty_stubs,tdm_recovered,preprint_version}.keys`
   alongside the JSON report (project-local).
3. Report counts to the user and ask which to fix (missing abstracts,
   missing PDFs, empty stubs, or all).
4. Run the stage(s) the user chose — `start_enrich_abstracts`,
   `start_enrich_dois` — passing the matching `.keys` file as
   `filter_keys_file`; missing PDFs go to step 6 instead. (The audit's
   "Next steps" output prints the same steps as terminal commands — the
   fallback when the tools are not available.)
5. Re-run the audit to confirm counts dropped.
6. **Missing PDFs: REQUIRED SUB-SKILL: retrieving-pdfs.** Load it with
   the Skill tool and follow it rather than running `enrich_pdfs.py`
   here: it plans the retrieval with the user, runs the agreed modes one
   at a time, re-runs the audit after each, and works the residue down
   the failure causes (`BROWSER_REQUIRED`, `ACCESS_BLOCKED`, …) before
   anything is reported as unavailable.

### Optional: retraction check

Retracted papers in a Zotero library are a silent data-quality
problem — citing a retracted paper is a fact-check failure mode the
author almost certainly wants to catch. Scite exposes a free
retraction-watch endpoint that the Zotero MCP server wraps as
`mcp__zotero__scite_check_retractions` (no Scite account required).
The tool exists only when the Zotero MCP server runs with `scite` in its
`ZOTERO_MCP_TOOLSETS` environment variable; the setup wizard registers it
that way. If the tool is not in your tool list, the server was
registered some other way: tell the user and suggest re-running `/setup`
rather than silently skipping the step.

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
it breaks the security model (API keys flow through your context),
is code the user has to audit on the spot instead of once, and is
untested on the platforms the shipped scripts run on.

## IRON RULE — Zotero access goes through the plugin's surface

When you need to talk to the user's Zotero library, the access
hierarchy is:

1. **Registered MCP tools** (`mcp__zotero__zotero_get_item_metadata`,
   `mcp__zotero__zotero_get_item_children`, `mcp__zotero__zotero_search_items`,
   `mcp__zotero__zotero_get_item_fulltext`, …). These cover most
   *reads* — item metadata, children, attachments, items lists,
   fulltext, annotations. `mcp__zotero__zotero_add_item` (`source_type=
   "doi"`/`"url"`/`"isbn"`) is the *write* that belongs here, and it
   is a first-class route for adding papers by identifier at any scale,
   screening-scale included. Pass the whole list in **one call** (a
   list, or a comma/newline-separated string) rather than one call per
   DOI: from zotero-mcp-server 0.11 (this plugin's floor) a batch
   resolves Crossref metadata in one request per 50 DOIs, creates items
   50 per POST, resolves the collection once, and reports each
   identifier separately (added / reused / failed), so one bad DOI does
   not sink the rest. Set `attach_mode="none"` unless the user wants
   PDFs now — it skips the PDF download-and-upload entirely, and PDF
   retrieval is a separate pipeline step anyway. One call per DOI in a
   loop gives all of that back, so batch.

   **Which route for bulk adds:**
   - **A list of DOIs/URLs/ISBNs** — reference-list snowballing,
     papers the user names, a handful of late additions to a review:
     batched `zotero_add_item`. Metadata comes from Crossref, which is
     what you want when you hold only identifiers.
   - **A search-results CSV from `search.py`** (the systematic-review
     pipeline): the `start_import` job, which runs
     `scripts/pipelines/import_to_zotero.py` (tier 3), is still the
     better tool. It builds most items from the search
     databases' own metadata with no Crossref call, keeps the
     database's abstract, imports rows that have no DOI, dedups within
     the batch and against the library by DOI *and* title+first-author,
     and layers on what the review needs: `search:` provenance tags per
     query, ISSN/journal canonicalisation, the predatory flag,
     `doctype:*` tags with supplements related to their article. It
     tags under the review's namespace, so run it from a project whose
     `screening_config.py` declares `TAG_PREFIX`, or pass
     `--tag-prefix <name>` for a standalone import; it refuses to run
     without one rather than writing provenance tags that belong to no
     review.

   If `check_setup` reports a `zotero-mcp-server` below the floor,
   the batching is not there — that install pays a Crossref lookup, a
   template GET and a create POST per identifier, serialised — so
   upgrade first, or use `import_to_zotero.py` for anything beyond a
   handful of DOIs.
2. **`zotero-cli`** for one-off writes MCP doesn't expose — `zotero-cli
   edit <key> --abstract/--add-tags/--doi/...`, `zotero-cli duplicates
   find|merge`, `zotero-cli notes create|update`, `zotero-cli add
   doi|bibtex|isbn|file`, `zotero-cli collections`, `zotero-cli tags`.
   It ships with the `zotero-mcp-server` package the setup wizard
   already installs — no separate install step. Use it for a single
   interactive mutation, or from a subagent that has no MCP tools
   registered. **Do not use it inside a pipeline script or any loop
   over more than a handful of items** — each invocation is a fresh
   process (~1–2 s startup), it has no batch-by-keys mode, no `--json`
   output a script can parse reliably, and no retry/backoff on Zotero's
   HTTP 412 version-conflict response. That is what tier 3 is for.
3. **`scripts/pipelines/zotero_io.py` and `scripts/pipelines/bbt_client.py`**
   for bulk/pipeline work — Better BibTeX endpoints
   (`get_bibtex_export`, `bbt_json_rpc`, `get_bbt_keys`,
   `populate_missing_bbt_keys`), keyed transactional writes with 412
   retry (`batch_update_tags`, `upsert_child_note`, `update_abstract`),
   the Connector dedup path (`merge_duplicate_item`, called from
   `scripts/pipelines/fetchers/browser/connector.py` — keep this one
   in `zotero_io.py` rather than shelling out, since the pipeline
   consumes its structured `{moved, skipped_dupe_attachments,
   tags_added, collections_added}` stats), and any other custom
   Zotero operation a headless `uv run` script needs. MCP tools and
   `zotero-cli` are both unusable here — MCP tools aren't reachable
   from a headless process at all, and `zotero-cli`'s per-call
   process cost and lack of batching make it unfit for hundreds or
   thousands of items.
4. **Direct HTTP** to `http://127.0.0.1:23119/...` is **not** a fourth
   option. It is a defect signal. If you find yourself writing
   `urllib.request.urlopen("http://127.0.0.1:23119/...")` or
   `curl localhost:23119`, that means the plugin is missing a helper.

**Stop, name the gap to the user, and propose adding a method to
`zotero_io.py` (or `bbt_client.py` for BBT) — do not work around it
inline.**

A direct-HTTP call by the agent bypasses retries, schema versioning,
cross-project reuse, and the one-line definition-of-Zotero-shape
that other consumers rely on. Inline urllib also drives the agent back
into improvising pipeline code, which the standing rule forbids.

## IRON RULE — never compose item metadata yourself

The rule above says *how* to reach Zotero. This one says where an
item's **content** may come from, and it has exactly two answers:

1. **A pipeline script importing database-retrieved data.**
   `import_to_zotero.py` maps rows that a search database returned
   (OpenAlex, Scopus, WoS, Semantic Scholar) and fills gaps from
   Crossref. Every field traces to an API response.
2. **An identifier-based add.** `mcp__zotero__zotero_add_item` with
   `source_type="doi"`/`"isbn"`/`"url"`, or `zotero-cli add
   doi|isbn|url|bibtex|csl-json`. You supply the identifier; the
   authority supplies the metadata.

**Never write an item from metadata you wrote yourself.** No
hand-assembled `{"title": ..., "creators": [...]}` dict, no inline
Python building item payloads, no typing a citation out of a PDF you
just read, no "I know this paper, I'll fill in the fields". Not for one
item, not to fix a record that imported badly, not because the
identifier lookup failed.

The reason is not tidiness. Metadata you compose is metadata with no
provenance: nothing downstream can tell it from an authority record,
it will be cited in a manuscript, and its errors are invisible —
a plausible-looking wrong year or a wrong page range does not announce
itself. An identifier-based add is checkable; a typed one is not.

**When neither path works, that is a defect signal**, and the response
is the same as for direct HTTP: stop, name the gap to the user, and
propose the missing capability — a searcher that carries the field, a
fix to the fill path, a new `zotero_io.py` helper. Do not fill the gap
by hand.

If an item genuinely has no identifier anywhere (grey literature, an
unpublished working paper), say so explicitly and ask the user how they
want it recorded, rather than quietly typing it in.

## Which Zotero surface a call uses

One surface only: **Zotero Desktop's local API at `localhost:23119`**,
for reads, writes, file uploads and merges alike. Zotero 10 must be
running. The plugin has no zotero.org Web API path and needs no
zotero.org key; Desktop syncs to zotero.org on its own.

Writes need Desktop's own write key, `[zotero] local_api_key`, which
`/setup` obtains through Zotero's consent dialog and proves with a test
write. If it is revoked later, the next write asks Zotero for a new one
(the dialog again — the user chooses **Always Allow**), saves it, and
carries on. `zotero_io.ZoteroClient` does all of this; do not hand-roll
pyzotero clients against the local API, which also has no item
templates (`item_template()` raises there).

This replaced a two-surface design (local reads, Web API writes and
uploads) whose failures were all one gap: a script that wrote through
zotero.org and read back from Desktop saw nothing until Desktop synced,
and reported it as "already done".

## Citation keys (Better BibTeX)

- BBT keys are auto-generated from author/year/title (e.g.,
  `brownUsingDailyStock1985a`).
- Generate the project's bibliography with the `generate_bib` tool
  (terminal equivalent:
  `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/generate_bib.py`, run
  from the project root).
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
   add to the target collection and backfill the abstract if empty. The
   title fallback never matches two records that both carry DOIs and the
   DOIs differ — those are different papers.

2. **Within the import batch itself.** As the loop processes rows, keep
   growing sets of `batch_doi_seen` and `batch_title_seen`. A second row
   for the same paper (e.g. Scopus + WoS where only one has a DOI) must
   merge into the already-queued item, not create a new one.
   `import_to_zotero.py` exits non-zero when any create or patch fails;
   treat that as an incomplete import, not a warning.

3. **Post-import.** Always run `mcp__zotero__zotero_find_duplicates` at
   the end of the import. (It and `zotero_merge_duplicates` exist only
   when `duplicates` is in the server's `ZOTERO_MCP_TOOLSETS`, as the
   setup wizard registers it; if they are missing, say so and fall back
   to `zotero-cli duplicates find` rather than skipping the check.) Pre-existing library items with incomplete
   metadata can slip past the first two checks; the post-check is the
   safety net. To merge what it finds, use `mcp__zotero__zotero_merge_duplicates`
   or, equivalently, `zotero-cli duplicates find` / `zotero-cli
   duplicates merge` from Bash — either is fine for this agent-driven,
   one-item-at-a-time flow. This is separate from
   `zotero_io.merge_duplicate_item`, which stays pipeline-only (see the
   IRON RULE above) because `scripts/pipelines/fetchers/browser/connector.py`
   depends on its structured return stats.

**Fix the data, don't work around it.** If post-import surfaces duplicates,
audit the upstream source first (search-API field mapping, manual
entries, out-of-scope items), fix them, re-run. Only add new fallback
matching after confirming the missing metadata is legitimate.

## Surface structured data in Zotero

When a pipeline writes decisions or structured extractions back to
Zotero (e.g. LLM screening decisions, coded fields), make them
reviewable in Zotero itself:

- **Tag** every processed item with the decision, namespaced under the
  project's `TAG_PREFIX` (e.g. `<prefix>/fulltext:include` /
  `<prefix>/fulltext:exclude`). Never write a bare `fulltext:include`:
  one library commonly holds several reviews, and an unprefixed tag
  belongs to none of them, so no pipeline script will ever see it.
- **Child note** with structured fields as HTML on includes, titled
  `SLR Coding: <prefix>`. The title carries the namespace for the same
  reason — the writer finds and overwrites its own note by that marker,
  so a shared marker means one review silently replaces another's
  coding. The local Zotero client reads item version + existing
  tags; the remote API writes PATCH and the child note.
- On `--full-recode`, delete prior named child notes before re-writing
  so re-runs don't accumulate stale notes.

## Adding to Zotero (one-off)

- Use `mcp__zotero__zotero_add_item` with `source_type="doi"` when a DOI
  exists (preferred), or `source_type="url"` only when no DOI exists.
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
