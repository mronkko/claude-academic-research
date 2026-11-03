---
name: demo-slr
description: Use when the user wants to demo or try the plugin's whole systematic-review workflow in 5-10 minutes on a topic they choose, ending with a highlighted Zotero PDF. Trigger phrases "/demo-slr", "demo the SLR", "show me a mini systematic review", "run a demo review". Do NOT use for a real review — use `systematic-review`. Do NOT use to retrieve PDFs for a real library — use `retrieving-pdfs`.
---

# demo-slr

> **Glossary:** unfamiliar with **MCP**, **DOI**, **PRISMA**?
> See [skills/_glossary.md](../_glossary.md).

A guided mini systematic review for a live audience. The presenter proposes a
topic; every step has a default and waits for their approval; the run ends
with a PDF open in Zotero at a highlighted quote. **Everything it writes
carries a `demo-` tag prefix and lives in a throwaway collection**, so
`demo_cleanup` removes it without touching the rest of the library.

**Plugin tools.** Steps name the tools of the plugin's own
**academic-research** MCP server by bare name (`check_setup`,
`start_search`, …); in Claude Code they appear as
`mcp__plugin_academic-research_academic-research__<name>`, on other hosts
with that host's prefix. `start_*` tools are **jobs**: follow one with
`job_status`; when it is `awaiting_user`, show the user its `prompt` and
pass *their* answer to `job_answer` — never answer it yourself; stop it with
`cancel_job`. If the tools are not available, run the *terminal equivalent*
given beside each with Bash instead, and tell the user once how to enable
the server (see **Terminal equivalent** in the glossary).

## Rules of the demo

- **One approval per step.** Use AskUserQuestion with the default as the
  first option, ("Recommended"), then run the step. Never chain two steps on
  one approval. Say what the step will do and cost in one sentence first.
- **Keep the clock.** Note the start time (Bash `date`) and print the elapsed
  time after each step. Target 10 minutes. If a step overruns, offer to
  shrink the sample (fewer records, fewer coded papers) rather than skip a step.
- **No browser, no babysitting.** PDFs come from APIs only. Never run the
  browser, Connector or manual-download routes.
- **No improvised code.** Use the tools and the shipped scripts named here.
  Do not read `~/.config/academic-research/config.toml`; never ask for keys.
- **Cost honesty.** Show each LLM step's `dry_run` cost line before running
  it. Models that think by default bill thinking tokens as output, so quote the
  estimate as a floor and say so.

## Pre-flight (ALWAYS run first)

1. **`check_setup`** (terminal equivalent:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_configured.py"`). If it
   is not configured, stop and tell the user to run `/setup` first.
2. Zotero Desktop 10 must be running with local writes authorised (`/setup`
   verifies this). An LLM provider key must be configured. `pdftotext`
   (poppler) must be on the PATH. Report what is missing and stop.
3. Which search databases are configured:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_database_access.py"`.
   Keep the list for step 3.
4. Which models the demo will screen and code with:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_demo_models.py"`. Show
   the presenter its lines as they are: the provider, and the model behind
   `--model fast` (abstract screening, step 5) and `--model balanced`
   (full-text coding, step 7). If a line says "not resolved", tell the
   presenter that step's model is unknown and offer to stop and pin one
   (`resolve_models.py`, see `setup`) before starting.

## Steps

### 1. Topic (default: ask)

Ask the presenter for a topic in a sentence. Propose, from their words:
a one-sentence research question, two inclusion criteria, a keyword query
with two concept blocks (A AND B), and a year window (default: the last three
years). Show all of it; let them edit. Approve.

### 2. Scaffold

Set `DEMO_ID` = `demo-` + today's date and time as `YYYYMMDD-HHMM`.

- Create the directory: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/ensure_dir.py" output/demo/<DEMO_ID>`.
- **`scaffold_project`** with `kind=sr`, `target=output/demo/<DEMO_ID>`,
  `claude_md=skip` (terminal equivalent:
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/scaffold_project.py" --kind sr --target output/demo/<DEMO_ID> --no-claude-md`).
- Install the demo configs over the scaffolded ones:
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/install_templates.py" --force demo_search_config.py:output/demo/<DEMO_ID>/search_config.py demo_screening_config.py:output/demo/<DEMO_ID>/screening_config.py`.
- Fill every `<...>` placeholder in those two files with the approved topic
  using Edit, and set `TAG_PREFIX = "<DEMO_ID>"`. Leave the model pins empty.

Approve the filled configs (show the two files' key lines, not the whole text).

### 3. Pilot the databases, then search one

The demo pilots first, as a real review does, but cheaply: one page of
results per database, not the whole literature.

**Pilot.** Run **`start_search`** with `databases` = every configured
database from the pre-flight, `streams=keyword`, `max_records=15`,
`config=output/demo/<DEMO_ID>/search_config.py`,
`output_dir=output/demo/<DEMO_ID>/pilot` and
`metadata_dir=output/demo/<DEMO_ID>/pilot`. Each database stops after its
first page and reports how many records the query matches in total. Read
those totals from `per_database_api_totals` in
`output/demo/<DEMO_ID>/pilot/search_metadata.json` (the fetched rows are
only a page and say nothing about volume) and show them as a table, one
row per database. A database that failed or was skipped appears in the
run's `summary`; report it rather than hiding it. Time the pilot out loud:
this step should take seconds.

**Suggest one database.** Recommend, in this order, the first that is
configured: **Web of Science**, then **Scopus**, then OpenAlex, then
Semantic Scholar. Give the reason in a sentence: Web of Science and Scopus
are faster to import (creators arrive split and bibliographic detail is
complete, so no per-record Crossref lookup) and more complete (abstracts
arrive with the search). Say that the totals above are what each database
holds for this query, and that a real review would pick on coverage too.
Let the presenter choose another.

**Search the chosen database.** Run **`start_search`** again with
`databases=<chosen>`, `streams=keyword`, `max_records=15`, the same
`config`, `output_dir=output/demo/<DEMO_ID>/analysis/raw` and
`metadata_dir=output/demo/<DEMO_ID>`. This first page is the demo's
records; show the titles. Scopus returns at most 25 records per page,
which is why the cap is 15. Say once that a capped run is a pilot and not
a formal search; `search_metadata.json` records the cap. Approve.

### 4. Import

Create the collection named `<DEMO_ID>` and import the first-page records:
**`start_import`** with `collection=<DEMO_ID>`, `input=output/demo/<DEMO_ID>/analysis/raw/search_results.csv`
and the created-keys file
(`output/demo/<DEMO_ID>/created.keys`; terminal equivalent:
`import_to_zotero.py ... --created-keys-out`). Then **`start_enrich_abstracts`**
for the collection. Approve before the import.

### 5. Abstract screening

**`start_abstract_screen`** with `dry_run=true` first; show the cost line; on
approval run it with the fast tier (`model=fast`). Show the decisions and the
reasons in a table. Approve the cost before the real run.

### 6. PDFs — APIs only

Write the included items' keys to `output/demo/<DEMO_ID>/include.keys`, then
run **`start_enrich_pdfs`** with that `filter_keys_file` (free API cascade). For
items still without a PDF, run **`pdf_plan`** (or `enrich_pdfs_plan`) to count
what paid OpenAlex Content could fetch and show the count and per-PDF
price. **Ask for confirmation**; paid OpenAlex (`start_enrich_pdfs_paid`) is
the default but it costs money. If no included item ends with a PDF, say so
and let the presenter pick another topic or item. Do not switch to the
browser or manual routes.

### 7. Full-text coding

Pick the included items that have a PDF, at most 3. Run
**`start_fulltext_code`** with `dry_run=true`, show the cost, then run it with
`limit=3` and the balanced tier (`model=balanced`). Show each paper's coded
fields and its evidence quotes.

### 8. Short report and one quick critic round

Export the coded data with **`export_coded_includes`** (to
`output/demo/<DEMO_ID>/coded_papers.csv`). Write
`output/demo/<DEMO_ID>/report/report.md`: about one page — the question, the
method (databases, search date, counts from the tool results), the number
screened/included/coded, and 2-3 findings from the coded fields. **Every
number must be copied from a tool result or the CSV**, never typed from
memory (`empirical-integrity`); cite only the coded papers. Follow
`academic-style` for the prose.

Then run `critic-loop` on it in quick mode, exactly:
`/critic-loop output/demo/<DEMO_ID>/report/report.md --quick --render-cmd "true" --rendered-path output/demo/<DEMO_ID>/report/report.md`
(a Markdown report needs no render step). Show the verdicts, apply the MAJOR
fixes visibly, and stop after one round. Approve before starting the critics.

### 9. Highlights

Run **`highlight_quotes`** with `dry_run=true`, then for real, for the demo
collection and tag prefix. Report the status counts: `created`, `not_found`
(the model's quote is not in the PDF — a faithfulness check, worth showing),
`ambiguous`. Approve.

### 10. Finale — open the highlighted PDF

Find the first highlighted item: the attachment key and page are in
`output/highlight_log.csv` (status `created`). Open it in Zotero on macOS
with `open "zotero://open-pdf/library/items/<attachmentKey>?page=<page>"`
(Windows: `start ""`, Linux: `xdg-open`), and print the link as well. Tell
the presenter what to look for: colour-coded highlights, each with the
field and coded value as its comment.

### 11. Cleanup (ask)

Ask whether to keep or clear the demo. Default: **keep until the presenter
says to clear**. To clear, run **`demo_cleanup`** with `dry_run=true`, show the
counts, then for real (`tag_prefix=<DEMO_ID>`, `collection`, `keys_file`).
It deletes only the items the import created and the demo collection; then
delete the scratch directory `output/demo/<DEMO_ID>` with the user's
approval.

## If something fails

Stop at the failing step, show the tool's `summary` and `log_path`, and offer
to retry, shrink the sample, or end the demo. Do not improvise around a
failure on stage.
