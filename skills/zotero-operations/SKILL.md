---
name: zotero-operations
description: Use when the user asks to work with a Zotero library — adding abstracts, attaching PDFs, enriching metadata, importing items, deduplicating, fixing BBT (Better BibTeX) citation keys, or writing child notes. Trigger phrases "add abstracts to Zotero", "attach PDFs", "enrich Zotero library", "fix citation keys", "find duplicates in Zotero", "update Zotero items". Do NOT use for a full PRISMA-style systematic review — use `systematic-review` instead.
---

# zotero-operations

> **Glossary:** unfamiliar with **MCP**, **BBT**, **DOI**, **ISSN**?
> See [skills/_glossary.md](../_glossary.md) for one-line definitions
> of every acronym this skill uses.

## Pre-flight (ALWAYS run first)

Before any step below, verify the plugin has been configured:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-.}/scripts/setup/check_configured.py"
```

If the result is `NOT CONFIGURED`, stop immediately and tell the user:

> The academic-research project has not been set up on this machine
> yet. Run the setup skill or setup wizard first to configure API keys, MCP servers, and
> permission rules. Do not attempt Zotero operations before that.

Do not call MCP tools, run scripts, or proceed with the procedure.
Running the setup skill/wizard is the required first step.

If the result is `configured`, proceed.

Also check the installed `zotero-mcp-server` version — a stale pre-0.9
install silently loses the tool names this skill documents:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-.}/scripts/setup/check_zotero_mcp_version.py"
```

A `WARNING` line means the user should upgrade before relying on
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
| Audit a library for items missing abstracts / PDFs / empty stubs | `audit_zotero_library.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/audit_zotero_library.py --group <id>` |
| Add missing abstracts to items | `enrich_abstracts.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/enrich_abstracts.py --filter-keys-file .claude/audit/audit.missing_abstract.keys` |
| Attach missing PDFs (fast HTTP cascade) | `enrich_pdfs.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/enrich_pdfs.py --filter-keys-file .claude/audit/audit.missing_pdf.keys` |
| Attach PDFs from Wiley journals (TDM token route) | `enrich_pdfs.py --sources wiley` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/enrich_pdfs.py --sources wiley --filter-keys-file .claude/audit/audit.missing_pdf.keys` |
| Attach PDFs from Cloudflare-gated publishers (Sage, APA, T&F, Emerald, …) | `enrich_pdfs.py --sources browser` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/enrich_pdfs.py --sources browser --filter-keys-file .claude/audit/audit.missing_pdf.keys` |
| Generate `references.bib` from a manuscript's citation keys | `generate_bib.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/generate_bib.py <project_dir>` |

The browser route (`--sources browser`) needs a one-time Playwright
browser install before first use: `uvx playwright install chromium`
(the setup wizard pre-approves this command).

**Exhaust the API cascade before reaching for the browser.** APIs are
much faster and far less error prone — nothing hinges on a page layout,
a Cloudflare challenge, or the user sitting at the keyboard. The default
`enrich_pdfs.py` run is ranked by version quality first and cost second:
free version of record (ScienceDirect, Springer, Crossref TDM, PMC) →
paid version of record (OpenAlex Content API, $0.01/PDF, opt-in) → open
access that is often the author's manuscript (OpenAlex OA, Unpaywall,
Semantic Scholar, CORE) → browser handlers → Zotero Connector. The
OpenAlex Content API is the only per-item cost anywhere in that
sequence; it outranks the free open-access tiers because it serves the
correctly paginated published article rather than an author manuscript,
and it is switched off with `[openalex] use_paid_content_api = false`.
The authoritative ordering lives in `fetchers.pdf_sources`'s docstring —
read it there rather than restating it from memory.

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
   missing_doi,empty_stubs,tdm_recovered,preprint_version}.keys`
   alongside the JSON report (project-local).
3. Report counts to the user and ask which to fix (missing abstracts,
   missing PDFs, empty stubs, or all).
4. Run the stage(s) the user chose, passing the matching `.keys` file
   to `--filter-keys-file`. The audit script prints the exact commands
   in its "Next steps" output — use those verbatim.
5. Re-run the audit to confirm counts dropped — **and to read the
   retrieval report for whatever did not drop.** A count that stayed
   put is not the end of the workflow; it is the start of step 6.
6. **Escalate the residuals rather than reporting them as failures.**
   The audit's per-publisher table names the cause for each item, and
   the causes map to rungs on a ladder. Work down it, offering each
   rung to the user:

   | Cause in the report | Next rung |
   |---|---|
   | `BROWSER_REQUIRED` | `enrich_pdfs.py --sources browser --filter-keys-file <retry.browser.keys>` — a visible Chromium opens; the user solves one Cloudflare challenge per publisher. Narrow to one publisher with `retry.browser.<publisher>.keys`. Rows that name no publisher belong here too: the link resolver and the Zotero Connector key on the item rather than the DOI prefix, and the same pass reaches both. |
   | `ACCESS_BLOCKED` (Wiley prefix, no token) | Configure `WILEY_TDM_TOKEN` via `/setup`, then `--sources wiley` |
   | `ACCESS_BLOCKED` (anything else) | Hand the user `retry.ill.keys` as an interlibrary-loan list |
   | `NETWORK_ERROR` | Re-run the same stage; the cause is transient |
   | `CORRUPT_DOWNLOAD` | The source served a broken file (usually truncated). Re-running the *same* source returns the same bad bytes — escalate to a different one: the publisher TDM route, or `--sources browser`. |
   | `UPLOAD_FAILED` | The PDF is already in the local cache and only the Zotero attach failed. Re-run `enrich_pdfs.py`; it attaches from cache with no new download. Cheapest rung on the ladder — always offer it first. |
   | `OUT_OF_SCOPE` | A book chapter, thesis, or preprint. No rung applies — the item is excluded on its type, not on retrieval, and chasing a PDF for it wastes the user's time. |
   | `UNAVAILABLE` | Genuinely unreachable *as published* — every route was tried, so this cannot appear before a browser pass has run. One route remains — see below — and only after the user declines it is "not available" the honest report. |

   **A publisher the browser pass skips is a finding, not a gap.** The
   run prints, for example, "16 items skipped the publisher's own site —
   the link resolver lists a licensed route for them, but not via that
   publisher (Academy of Management 7, Taylor & Francis 5, …)". Those
   items are *not* lost: they went to the resolver's own route in the
   same pass. Report the line rather than treating it as a failure, and
   only act on it if the user says they can reach one of those
   publishers by other means — a society membership, a login at a second
   institution — in which case add that handler name to
   `[library] direct_access` in config.toml and re-run. Many society
   publishers (Academy of Management, INFORMS) sell membership rather
   than institutional access, so this skip is usually correct.

   **The last rung is a different paper, so it is offered, never taken
   silently.** `enrich_pdfs.py --allow-preprints` looks for a copy on
   arXiv / SSRN / RePEc. What it finds is the manuscript before peer
   review: hypotheses, samples and findings all move between a working
   paper and the published article, and nothing downstream can tell the
   two apart. Name that when you offer it. Attachments carry
   `pdf:preprint-version`, `fulltext_code.py` lists those items before
   coding them, and the audit reports them under `preprint_version`.

   **Say how many items each rung would recover before proposing it.**
   "76 of these 110 are behind Cloudflare at two publishers and one
   browser pass gets them" is a decision the user can make; "110 items
   failed" is not.

7. **Run the browser pass yourself — do not send the user to a
   terminal.** You have no controlling TTY, but that no longer matters:
   `--control-file` moves the prompts into a file you poll, while the
   Chromium window still opens on the user's screen and the user still
   solves every challenge.

   ```bash
   uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py \
       --sources browser --auto-publishers \
       --browser-workers 4 \
       --control-file .claude/audit/browser.json \
       --progress-json .claude/audit/browser-progress.jsonl
   ```

   **`--browser-workers N` is worth passing on any large queue.** N tabs
   share one Chromium profile, so one Cloudflare / SSO solve covers them
   all — an unattended EBSCOhost run of 400 items drops from roughly two
   hours to well under one. Each publisher caps it at its own
   `concurrency`, which is 1 for every direct publisher handler and 4 for
   EBSCOhost, so raising the number cannot get a bot-protected publisher
   throttled; a request that gets capped is printed rather than silently
   reduced. Do not raise a handler's `concurrency` to make the flag bite
   harder — those 1s are measured limits, and the cost of exceeding one
   is the publisher for the whole run plus the shared profile's
   clearance.

   Launch it with `run_in_background: true`, then loop:

   - read `.claude/audit/browser.json`;
   - when `state` is `awaiting_user`, **relay `prompt` to the user
     verbatim** — it is the text they would have seen on a terminal, and
     it names the publisher whose challenge is on screen;
   - write their answer to `.claude/audit/browser.json.reply` as
     `{"seq": <the seq you just read>, "answer": "<their answer>"}`;
   - carry on until `state` stays `running` and the process exits.

   Always echo back the `seq` you read. A reply carrying an old `seq` is
   ignored by design, which is what stops a stale answer from silently
   clearing a challenge nobody looked at.

   **Expect long stretches with no question.** The run only asks when
   there is something for the user to solve: it opens each publisher's
   first page and waits for the Cloudflare challenge to clear on its own,
   which it usually does — the browser profile persists between runs, and
   non-interactive challenges pass in seconds. Silence is the normal case,
   not a hang. Read `--progress-json` to tell the two apart: it is one
   JSON object per line (`publisher_start`, `item`, `publisher_done`,
   `run_done`), so the last line tells you where the run is without
   parsing stdout. Report progress from there rather than guessing.

   `--auto-publishers` takes the item list from the audit's
   `retry.browser.keys`, so do not assemble a key list by hand. If it
   reports no retry set, the audit in step 5 has not been run — run it
   rather than falling back to a full-library pass.

   Use `--no-prompt` only for genuinely unattended runs. It answers every
   challenge with "skip", so it is not a substitute for the control file
   when the user is present.

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

## IRON RULE — Zotero access goes through the plugin's surface

When you need to talk to the user's Zotero library, the access
hierarchy is:

1. **Registered MCP tools** (`mcp__zotero__zotero_get_item_metadata`,
   `mcp__zotero__zotero_get_item_children`, `mcp__zotero__zotero_search_items`,
   `mcp__zotero__zotero_get_item_fulltext`, …). These cover most
   *reads* — item metadata, children, attachments, items lists,
   fulltext, annotations. `mcp__zotero__zotero_add_item` (`source_type=
   "doi"`/`"url"`/`"isbn"`, batchable) is the one *write* that belongs
   here, but know its cost before reaching for it on more than a
   handful of DOIs: each identifier pays one Crossref/metadata lookup,
   one item-template GET, one create POST, and — as currently
   published — a PDF download-and-upload too, all serialized under
   zotero-mcp's process-wide API lock. `attach_mode="none"` does not
   yet suppress that PDF step outside the arXiv path, so a
   screening-scale batch (dozens to hundreds of DOIs) can wedge the
   lock and crash Zotero Desktop's local server — this is exactly what
   happened before this sentence existed. A handful of DOIs is fine;
   anything screening-scale goes through `import_to_zotero.py`
   (tier 3) instead. (A fix that batches creates, narrows the lock,
   and makes `attach_mode="none"` actually skip the PDF step is in
   progress upstream — once it ships and the wizard's version floor
   moves past it, revisit these numbers.)
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

**Implementation note for plugin contributors.** The CI guard at
`tests/unit/test_no_direct_localhost_zotero.py` greps every file
under `scripts/pipelines/` for `127.0.0.1:23119` or `localhost:23119`
and fails the build on a match outside `zotero_io.py` and
`bbt_client.py`. New code must route through those modules.

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
  `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/generate_bib.py <project_dir>`.
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

- **Tag** every processed item with the decision (e.g.
  `fulltext:include` / `fulltext:exclude`).
- **Child note** with structured fields as HTML on includes (e.g.
  `SLR Coding`). The local Zotero client reads item version + existing
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
