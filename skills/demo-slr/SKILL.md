---
name: demo-slr
description: Use when the user wants to demo or try the plugin's whole systematic-review workflow in 5-10 minutes on a topic they choose, ending with a highlighted Zotero PDF. Trigger phrases "/demo-slr", "demo the SLR", "show me a mini systematic review", "run a demo review". Do NOT use for a real review — use `systematic-review`. Do NOT use to retrieve PDFs for a real library — use `retrieving-pdfs`.
---

# demo-slr

> **Glossary:** unfamiliar with **MCP**, **DOI**, **PRISMA**?
> See [skills/_glossary.md](../_glossary.md).

A guided mini systematic review for an audience that has **never seen how
this works**. The presenter gives a topic; the run searches the literature,
imports it into Zotero, has a model screen it, fetches the full texts it can,
has a model read one paper in depth, writes a short report, and ends with a
PDF open in Zotero at a highlighted quote. **Everything it writes carries a
`demo-` tag prefix and lives in a throwaway collection in the presenter's
own library (My Library)**, so `demo_cleanup` removes it without touching the
rest of the library.

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

- **Explain as you go.** Assume the audience knows nothing. Before each step
  say, in plain words and two or three sentences: what is about to happen,
  why a literature review does it at this point, and what to look at in the
  result. Define a term the first time it appears (database, Zotero
  collection, screening, full text, coding). Never paste log lines at the
  audience: turn them into one sentence (for example, "publishers often
  refuse automated downloads, so the demo tries about a dozen legal sources
  in turn and takes whichever answers").
- **Ask only about a choice or about money.** Use AskUserQuestion, default
  first ("Recommended"). The only questions are: the topic, query and
  journals (step 1); which database to take records from (step 3); an LLM
  step whose dry-run cost floor is above **$0.50**; any paid OpenAlex PDF
  purchase; the critic round (step 8); and cleanup (step 11). Everything
  else just runs: do not ask to approve configs, an import, or a cost under
  $0.50 (print the cost line and go on).
- **Counts, not lists.** Report how many records a step found, imported or
  kept. Do not list titles back unless the step is about the individual
  papers (screening decisions, the coded paper).
- **Keep the clock.** Note the start time (Bash `date`) and print the elapsed
  time after each step. Target 10 minutes. If a step overruns, offer to
  shrink the sample (fewer records, fewer coded papers) rather than skip a step.
- **My Library, no prompt.** Every Zotero-touching tool is called with
  `user=true` (the presenter's personal library). Never ask which library or
  collection: the collection is named `<DEMO_ID>`.
- **No browser, no babysitting.** PDFs come from APIs only. Never run the
  browser, Connector or manual-download routes.
- **No improvised code.** Use the tools and the shipped scripts named here.
  Do not read `~/.config/academic-research/config.toml`; never ask for keys.
- **Cost honesty.** Show each LLM step's `dry_run` cost line before running
  it. Models that think by default bill thinking tokens as output, so quote the
  estimate as a floor and say so.

## Pre-flight (ALWAYS run first)

Tell the audience in one sentence that this checks the machine is ready.

1. **`check_setup`** (terminal equivalent:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_configured.py"`). If it
   is not configured, stop and tell the user to run `/setup` first.
2. Zotero Desktop 10 must be running with local writes authorised (`/setup`
   verifies this). An LLM provider key must be configured. `pdftotext`
   (poppler) must be on the PATH. Report what is missing and stop. Say that
   the demo writes to **My Library**, in one new collection.
3. Which search databases are configured:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_database_access.py"`.
   Keep the list for step 3.
4. The LLM configuration. First
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_llm_provider.py"`: print
   its lines to the presenter as they are (provider, whether it was selected
   or is the registry default, whether a credential is configured, base
   URL; it never prints a key). If `credential:` says `missing`, stop and
   tell them to run `/setup`; if `selected:` says `no (registry default)`,
   say so and confirm that provider is the one they want. Then which models
   the demo will screen and code with:
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_demo_models.py"`. Show
   its lines as they are too: the provider, and the model behind
   `--model fast` (abstract screening, step 5) and `--model balanced`
   (full-text coding, step 7). If a line says "not resolved", tell the
   presenter that step's model is unknown and offer to stop and pin one
   (`resolve_models.py`, see `setup`) before starting.

## Steps

### 1. Topic, query and journals (ask)

Explain: a review starts from a question and a written search recipe, so
that someone else could repeat it. Ask the presenter for a topic in a
sentence. Propose, from their words: a one-sentence research question, two
inclusion criteria, a keyword query with two concept blocks (A AND B), a year
window (default: the last three years), and **5 to 10 journals** that
publish on the topic. Limiting the search to journals is what keeps the first
page of results on topic.

Resolve every journal against OpenAlex (the `search_sources` and
`check_venue_quality` tools of the OpenAlex MCP server) and show each as
title + ISSN. Never type an ISSN from memory; drop a journal OpenAlex cannot
confirm. Show all of it; let the presenter edit. Approve once.

### 2. Scaffold

Explain in a sentence: this creates a scratch project folder for the demo,
with the search recipe and the screening instructions the model will follow.

Set `DEMO_ID` = `demo-` + today's date and time as `YYYYMMDD-HHMM`.

- Create the directory: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/ensure_dir.py" output/demo/<DEMO_ID>`.
- **`scaffold_project`** with `kind=sr`, `target=output/demo/<DEMO_ID>`,
  `claude_md=skip` (terminal equivalent:
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/scaffold_project.py" --kind sr --target output/demo/<DEMO_ID> --no-claude-md`).
- Install the demo configs over the scaffolded ones:
  `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/install_templates.py" --force demo_search_config.py:output/demo/<DEMO_ID>/search_config.py demo_screening_config.py:output/demo/<DEMO_ID>/screening_config.py`.
- Fill every `<...>` placeholder in those two files with the approved topic
  using Edit, and set `TAG_PREFIX = "<DEMO_ID>"`. Leave the model pins empty.
  In `search_config.py` set `JOURNALS = {"<ISSN>": (0, "<title>"), …}` from the
  verified list (the title is needed: Semantic Scholar returns no ISSN). Put
  combined phrases that already join both concepts in `BLOCK_A_TERMS` and
  leave `BLOCK_B_TERMS` empty, because OpenAlex and Semantic Scholar run the
  two blocks as separate queries, not as A AND B.

Do not ask for approval: the presenter already approved the content in step 1.

### 3. Pilot the databases, then search one

Explain: a **database** is a searchable index of the research literature
(Web of Science, Scopus, OpenAlex, Semantic Scholar). A real review first
*pilots*: it tries the query in each database to see how many records match
before committing to a full search. Here a pilot is one page of results per
database, which takes seconds.

**Pilot.** Run **`start_search`** with `databases` = every configured
database from the pre-flight, `streams=keyword`, `max_records=25`,
`config=output/demo/<DEMO_ID>/search_config.py`,
`output_dir=output/demo/<DEMO_ID>/pilot` and
`metadata_dir=output/demo/<DEMO_ID>/pilot`. Each database stops after its
first page and reports how many records the query matches in total. Read
those totals from `per_database_api_totals` in
`output/demo/<DEMO_ID>/pilot/search_metadata.json` (the fetched rows are
only a page and say nothing about volume) and show them as a table, one row
per database. A database that failed or was skipped appears in the run's
`summary`; report it rather than hiding it. Say that the Boolean databases
(Web of Science, Scopus) and the phrase-based ones (OpenAlex, Semantic
Scholar) run the query differently, so the totals are not like-for-like.

**Suggest one database and ask.** Recommend, in this order, the first that is
configured: **Web of Science**, then **Scopus**, then OpenAlex, then
Semantic Scholar. Give the reason in a sentence: Web of Science and Scopus
are faster to import (creators arrive split and bibliographic detail is
complete, so no per-record Crossref lookup) and more complete (abstracts
arrive with the search). Ask which to take the records from.

**Search the chosen database.** Run **`start_search`** again with
`databases=<chosen>`, `streams=keyword`, `max_records=25`, the same
`config`, `output_dir=output/demo/<DEMO_ID>/analysis/raw` and
`metadata_dir=output/demo/<DEMO_ID>`. 25 is the most Scopus returns on one
page. Report the count and how many have an abstract. Say once that a capped
run is a pilot and not a formal search; `search_metadata.json` records the
cap.

### 4. Import

Explain: the records now go into **Zotero**, the reference manager where a
review keeps everything: one new collection named `<DEMO_ID>` in My Library.

**`start_import`** with `user=true`, `collection=<DEMO_ID>`,
`input=output/demo/<DEMO_ID>/analysis/raw/search_results.csv`,
`config=output/demo/<DEMO_ID>/screening_config.py` and
`created_keys_out=output/demo/<DEMO_ID>/created.keys` (terminal equivalent:
`import_to_zotero.py --user ... --created-keys-out output/demo/<DEMO_ID>/created.keys`;
the keys file is what `demo_cleanup` later deletes by). Then
**`start_enrich_abstracts`** with `user=true` and
`filter_keys_file=output/demo/<DEMO_ID>/created.keys`: it fills in any abstract
the database did not supply. Report the counts. If fewer items sit in the
collection than the import reported creating, say so.

### 5. Abstract screening

Explain: **screening** decides which records belong in the review. A model
reads each title and abstract against the two inclusion criteria and answers
include, borderline or exclude, with a one-sentence reason. It is cheap and
fast, and a real review checks a sample of its decisions by hand.

**`start_abstract_screen`** with `user=true`, the collection key, `config`,
`model=fast` and `dry_run=true` first; print the cost line, and run it for
real without asking unless the floor is above $0.50. Report counts per
decision, then a short table of the included and borderline papers with the
reason; give the excluded ones as counts per exclusion code.

### 6. PDFs — APIs only

Explain: to read a paper in depth the model needs its **full text**, the PDF.
The plugin tries about a dozen legal sources in turn (publisher agreements,
open-access archives, Unpaywall, OpenAlex and others). Many publishers block
automated downloads, so expect only some papers to come through; that is
normal and is exactly why real reviews budget time for this step.

Write the included items' keys to `output/demo/<DEMO_ID>/include.keys`, then
run **`start_enrich_pdfs`** with `user=true` and that `filter_keys_file` (free
API cascade). This can take a few minutes. For items still without a PDF,
run **`pdf_plan`** (or `enrich_pdfs_plan`) with `lookups=openalex` to count
what paid OpenAlex Content could fetch. If the count is above zero, **ask**
(it costs money) and show the count and the per-PDF price;
`start_enrich_pdfs_paid` is the default answer. If it is zero, say so and move
on. If no included item ends with a PDF, say so and let the presenter pick
another topic or item. Do not switch to the browser or manual routes; say in
a sentence which route would have been next, so the audience knows it exists.

### 7. Full-text coding

Explain: **coding** means reading a paper and recording what it says in
fixed fields (sample, method, findings). The model reads the whole paper and
quotes its evidence word for word, which step 9 can then check.

Pick the included items that have a PDF, at most 3. Run
**`start_fulltext_code`** with `user=true`, `only_keys=<their keys>`,
`dry_run=true`, print the cost line, then run it with `limit=3` and the
balanced tier (`model=balanced`), asking only if the floor is above $0.50.
Show each paper's coded fields and its evidence quotes.

### 8. Short report and one quick critic round

Explain: the coded data is exported to a table, and a short report is
written from it.

Export the coded data with **`export_coded_includes`** (`user=true`, to
`output/demo/<DEMO_ID>/coded_papers.csv`). Write
`output/demo/<DEMO_ID>/report/report.md`: about one page — the question, the
method (databases, search date, counts from the tool results), the number
screened/included/coded, and 2-3 findings from the coded fields. **Every
number must be copied from a tool result or the CSV**, never typed from
memory (`empirical-integrity`); cite only the coded papers. Follow
`academic-style` for the prose.

Then **ask** whether to run `critic-loop` on it in quick mode (it starts
several reviewer agents in parallel and spends credits; explain that in a
sentence). If yes, run exactly:
`/critic-loop output/demo/<DEMO_ID>/report/report.md --quick --render-cmd "true" --rendered-path output/demo/<DEMO_ID>/report/report.md`
(a Markdown report needs no render step). Show the verdicts, apply the MAJOR
fixes visibly, and stop after one round.

### 9. Highlights

Explain: the coder's quotes are now marked in the PDF itself, and each
quote is checked against the PDF text, which catches a model inventing words.

Run **`highlight_quotes`** with `user=true`, `dry_run=true`, then for real,
for the demo collection and tag prefix. Report the status counts: `created`,
`not_found` (the model's quote is not in the PDF — a faithfulness check, worth
showing), `ambiguous`.

### 10. Finale — open the highlighted PDF

Find the first highlighted item: the attachment key and page are in
`output/highlight_log.csv` (status `created`). Open it in Zotero on macOS
with `open "zotero://open-pdf/library/items/<attachmentKey>?page=<page>"`
(Windows: `start ""`, Linux: `xdg-open`), and print the link as well. Tell
the presenter what to look for: colour-coded highlights, each with the
field and coded value as its comment.

### 11. Cleanup (ask)

Ask whether to keep or clear the demo. Default: **keep until the presenter
says to clear**. To clear, run **`demo_cleanup`** with `user=true`,
`dry_run=true`, show the counts, then for real (`tag_prefix=<DEMO_ID>`,
`collection`, `keys_file`). It deletes only the items the import created and
the demo collection; then delete the scratch directory
`output/demo/<DEMO_ID>` with the user's approval.

## If something fails

Stop at the failing step, show the tool's `summary` and `log_path`, and offer
to retry, shrink the sample, or end the demo. Do not improvise around a
failure on stage.
