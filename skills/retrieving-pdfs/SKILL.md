---
name: retrieving-pdfs
description: Use when PDFs must be found and attached to Zotero items — planning retrieval with the user, then running the API cascade, paid OpenAlex, the browser or Connector pass, or manual downloads, and diagnosing items still without a PDF. Trigger phrases "attach PDFs", "get the PDFs", "missing PDFs", "download full texts", "fetch PDFs", "manual download". Do NOT use for abstracts, DOIs or other library housekeeping — use `zotero-operations`; for screening or coding — use `systematic-review`.
---

# retrieving-pdfs

> **Glossary:** unfamiliar with **MCP**, **DOI**, **SFX**, **TDM**,
> **FE-code**? See [skills/_glossary.md](../_glossary.md).

This skill owns PDF retrieval end to end: **plan with the user, then
execute**. There is no fixed order of retrieval modes. You classify the
queue, show the user what each mode would get, agree an order with them,
write it down as the **strategy**, and execute it step by step — and a
later session resumes it from the file.

`systematic-review` and `zotero-operations` load this skill (REQUIRED
SUB-SKILL) whenever PDFs are to be retrieved. Everything about retrieval
lives here; those skills only decide *when*.

## Pre-flight (ALWAYS run first)

1. The plugin is configured: the `check_setup` tool, or
   `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_configured.py"`.
   `NOT CONFIGURED` → stop and send the user to `/setup` (in a terminal;
   never ask for keys in chat).
2. You know the Zotero library: a group ID or the personal library. Ask
   if it is ambiguous; never guess the group ID. Every tool and command
   below takes it (`group` / `user`, `--group` / `--user`).
3. **Resume before planning.** Call `pdf_strategy_show`
   (`uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/pdf_strategy.py show`).
   If a strategy exists and is not complete, tell the user where it
   stopped (steps done, their results, the next step) and ask whether to
   carry on, re-plan, or start over. A running step means a session ended
   mid-step: check that step's output (the audit, `list_jobs`, the
   browser run's `--progress-json`) before re-running it.

## Step 1 — Plan (read-only)

Call **`pdf_plan`** with `lookups: "openalex"` — the default
recommendation. It never writes to Zotero; it reads the library, the run
and failure logs (`output/pdf_attach_log.csv`, `output/pdf_fetch_log.csv`),
the audit's key files and the caches, and asks OpenAlex one **keyless**
metadata question per 50 items. Keyless means it cannot be billed to
anyone and downloads no PDF; what it buys is the *exact* number of items
the paid Content API holds (`has_content.pdf`) and which ones have a free
copy within the `[pdf]` policy.

- `lookups: "none"` — caches and logs only, no network beyond the Zotero
  read; seconds. Paid reachability is then reported as **unknown**, with
  a cost *ceiling*. Never present that ceiling as an estimate of the
  cost.
- **`start_pdf_plan`** (a background job; follow it with `job_status`) —
  also asks Crossref and the library link resolver about every uncached
  item, so the browser / EBSCOhost / Connector / no-route split is exact.
  Roughly 2 s per uncached resolver query; the estimate is in the first
  lines of the job's log — read it out before the user waits on it. The
  answers are cached, so the browser pass afterwards starts warm. Worth it
  when a browser or Connector step is likely and the queue is not tiny.

Terminal equivalent: `uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py
--plan-strategy --plan-lookups openalex --group <id>` (`--filter-keys-file`
narrows the scope, `--library <name>` picks the institution). Output:
`.claude/pdf/plan.json` and `.claude/pdf/plan.<route>.keys`.

## Step 2 — Show the counts

Read the summary back in plain words. The plan gives each item without a
PDF two routes:

- **`routes`** — where it goes *first* under the default order:
  `api` (a publisher API with a configured key, or a free copy within
  policy), `api_untried` (the free cascade has never asked about it — it
  always runs first, it is free), `paid_openalex`, `browser` (a publisher
  handler; `browser_publishers` says which ones need a Cloudflare / SSO
  solve), `ebsco` (the library route to EBSCOhost — browser pass, but no
  challenge), `connector` (library route or doi.org via the Zotero
  Connector), `other_library` (routes only at an institution whose
  network is not up now), `manual` (no automated route left), `no_doi`.
- **`fallback_routes`** — where each item would go **if the API sources
  fail it**. This is the number the user decides the rest of the order
  with: "the API will likely get 300; of what it may miss, 120 sit behind
  two Cloudflare publishers and 80 have no automated route at all".

Also say: the paid count and its cost (`$0.01` per PDF), items `below
policy` (a copy exists but below `min_version`), the audit flags on the
queue (`retry.manual`, `retry.below_policy`, …), publisher APIs with a
missing credential (`signals.publisher_api_missing_key` — `/setup`
fixes that, and it is the cheapest win there is), and `no_doi` items
(their DOIs have to be found first — `start_enrich_dois` /
`zotero-operations` — or they can only be found by hand).

## Step 3 — Agree the strategy with the user

Ask, in one message, and give your recommendation with each:

1. **The order of the modes**: `api`, `paid`, `browser`, `connector`,
   `manual`. The API cascade first, always — it is fast and nothing in it
   depends on a page layout, a challenge or the user. After it:
   - **Recommend paying before spending the user's time.** At $0.01 a PDF
     most researchers prefer paying to downloading by hand; say what the
     plan says it costs. Paid runs inside the API step under the policy
     (`prefer` before the free open-access sources, `last_resort` after
     them). Put `paid` in the order as its own step only to move it
     later — e.g. after the browser pass but before manual downloads
     (the `api` step then runs with `--paid off`).
   - **Encourage manual download** for what is left. It is usually the
     fastest route through a few hundred items: the user downloads in
     their own browser — their logins, VPN and extensions; no Cloudflare
     automation; no terms-of-use question, since they are the one
     clicking — and the plugin opens the tabs, collects and attaches.
   - Offer the **browser pass** where it pays: many items behind one or
     two Cloudflare publishers (one solve covers a whole publisher), or a
     large `ebsco` count (no challenge at all). Offer `connector` alone
     only when the resolver routes most of the residue to platforms
     without a handler.
2. **Is the user present now?** Manual download always needs them; so do
   the browser and Connector passes — a Chromium window opens on their
   screen and they solve challenges. Steps that need nobody (`api`,
   `paid`) can run while they are away.
3. **Could an unattended run be left going?** Record the answer; it is
   for the unattended run, which is not built yet. Until it is, describe
   the browser and Connector modes as needing the user present — never
   start one on the strength of this answer.
4. **The policy, if none is stored** (`policy.stored` is false in the
   plan: every value came from the defaults): `min_version`
   (`published` | `accepted` | `preprint`), `reconstructed`, `paid`. See
   *Retrieval policy* below for what each means. Recommend `accepted` /
   `false` / `prefer` unless the review needs page-exact quotation
   (`published`).

Then write it:

- **`pdf_strategy_write`** — `order` (e.g. `api,paid,manual`), `present`,
  `unattended`, and the policy values if they were agreed. It refuses to
  replace an existing strategy unless `force`; only replace one the user
  agreed to replace.
- **Where the policy is stored** — pick and say why:
  - a review project (it has `screening_config.py`): **`pdf_policy_write`**
    writes `PDF_MIN_VERSION` / `PDF_RECONSTRUCTED` / `PDF_PAID` into it.
    This is the right home: the version a review codes from is a
    methodological decision of *that* review, it is recorded with the
    review, and it is the only tier you may write;
  - otherwise a library-wide default belongs in the global `[pdf]`
    settings, which only `/setup` writes — ask the user to run it in a
    terminal (never read or edit `~/.config/academic-research/config.toml`);
  - or, if the user wants it for this retrieval only, pass
    `policy_where: "strategy"`: every step then carries `--min-version`
    / `--paid` itself (`reconstructed` has no per-run flag and stays as
    stored).

Terminal equivalents: `pdf_strategy.py init --order … --present yes|no
[--unattended yes|no]`, `pdf_strategy.py policy --min-version …`. Never
edit `.claude/pdf/strategy.toml` by hand.

**Autonomous runs.** When the calling skill runs autonomously, fold these
questions into its single up-front proposal. With no answer, run the
`api` step under the stored policy and stop with the plan on screen —
the other modes need the user.

## Step 4 — Execute, one step at a time

Loop until `pdf_strategy_show` says the strategy is complete:

1. `pdf_strategy_show` gives the next step, whether it needs the user
   (`blocked_until_user_present`), and its exact command or MCP tool. Use
   that command verbatim; do not compose your own.
2. `pdf_strategy_update` with `status: "running"`.
3. Run it (per mode, below).
4. Run the audit and **diagnose** (next section) — after every step.
5. `pdf_strategy_update` with `done` (or `failed` / `skipped`) and a
   one-line `result` ("attached 212, 88 left; 61 BROWSER_REQUIRED").
   If the user is leaving, update `present: "no"` so the next session
   knows.

Re-plan (`pdf_plan`, `lookups: "none"` is enough) when a step changed the
picture a lot — the counts for the remaining steps are what the user
agreed to.

### `api` and `paid`

No MCP job tool for `enrich_pdfs` exists yet (BACKLOG P21 adds them), so
run the command from `pdf_strategy_show` with Bash and
`run_in_background: true` — a large library takes 5–15 minutes. The run
prints the effective `[pdf]` policy, with where each value came from, at
the top: check it says what was agreed.

Journal articles only unless `--item-types journalArticle,bookSection,book`
admits more; an admitted type is never logged `OUT_OF_SCOPE`.

### `browser` and `connector`

Tell the user before launching: *"Next: the browser pass. A Chromium
window will open on your desktop; for some publishers you may need to
click through a Cloudflare challenge or sign in once. Ready?"* — and wait.
First use needs a one-time `uvx playwright@latest install chromium` (the
setup wizard pre-approves it).

Just before it, **`enrich_pdfs_plan`** shows the publisher queue in run
order and which publishers will ask for an interactive solve (`mode:
"connector"` for a Connector-only step).

**Run the pass yourself — do not send the user to a terminal.** You have
no controlling TTY, but `--control-file` moves the prompts into a file
you poll, while the Chromium window opens on the user's screen and they
solve every challenge. The strategy's command already carries the flags:

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py \
    --sources browser --browser-workers 4 \
    --control-file .claude/pdf/browser.json \
    --progress-json .claude/pdf/browser-progress.jsonl --group <id>
```

Start it with `run_in_background: true`, then loop:

- read `.claude/pdf/browser.json`;
- when `state` is `awaiting_user`, **relay `prompt` to the user
  verbatim** — it names the publisher whose challenge is on screen;
- write their answer to `.claude/pdf/browser.json.reply` as
  `{"seq": <the seq you just read>, "answer": "<their answer>"}`;
- carry on until `state` stays `running` and the process exits.

Always echo back the `seq` you read: a reply carrying an old `seq` is
ignored by design, which stops a stale answer from clearing a challenge
nobody looked at.

**Expect long silences.** The run asks only when something needs a
human: it opens each publisher's first page and waits for the challenge
to clear on its own, which it usually does (the browser profile
persists). Read `--progress-json` to tell a working run from a stuck one:
one JSON object per line (`publisher_start`, `item`, `publisher_done`,
`routes_start` / `routes_progress` / `routes_done` for the library-route
lookups before the Connector pass, `run_done`); report progress from it.

- `--browser-workers N` drives each publisher on N tabs of **one**
  Chromium profile, so one Cloudflare / SSO solve covers them all. Each
  handler caps it at its own `concurrency` — 1 for every direct publisher
  handler, 4 for EBSCOhost, always 1 for the Zotero Connector — and says
  so when it does. Do not raise a handler's `concurrency`: those 1s are
  measured limits, and exceeding one costs the publisher for the whole
  run plus the shared profile's clearance.
- To re-run only what the audit says the browser can still recover,
  replace the scope with `--auto-publishers` (the audit's
  `retry.browser.keys`); one publisher alone with
  `--sources browser --publisher <name> --filter-keys-file
  .claude/audit/audit.retry.browser.<name>.keys`.
- `--no-prompt` answers every challenge with "skip" — only for a run
  nobody is watching, never instead of the control file when the user is
  there.
- **Two libraries, one network at a time.** With several
  `[library] openurl_base` entries, pass `--library <name>` for the
  institution whose VPN or login is up now (the run banner lists the
  names). Every library is still consulted, but only that one's links are
  opened; items whose only route is at the other library are deferred,
  not logged — re-run with the other `--library` on its network. Switch
  this way, never by editing config.toml. A warning about cache entries
  that "predate per-library keys" means run once with
  `--adopt-legacy-resolver-cache <name>`, naming the library that was
  *first* in the list when that cache was built; ask the user if you do
  not know.
- **A publisher the browser pass skips is a finding, not a gap.** "16
  items skipped the publisher's own site — the link resolver lists a
  licensed route for them, but not via that publisher (Academy of
  Management 7, …)" means they went to the resolver's own route in the
  same pass. Act only if the user can reach that publisher another way (a
  society membership, a second institution's login): add the handler
  name to `[library] direct_access` in their config.toml (the user does
  that, not you) and re-run.
- The Connector step needs Zotero desktop running with the Connector in
  the Chromium profile, and the institution's OpenURL base configured.

### `manual`

One call per cycle — **`pdf_manual_next`** (`enrich_pdfs.py --manual
next`):

1. It opens the next batch (25 tabs by default; `batch_size`,
   `[pdf] manual_batch_size`) in the user's own browser — the library
   route, else doi.org. If a route needs a proxy sign-in, only that tab
   opens: tell the user to sign in and download it; the rest open on the
   next call.
2. It moves the PDFs downloaded since the *previous* batch opened from
   the Downloads folder (`[pdf] downloads_dir`, default `~/Downloads`)
   into `output/pdf_staging/batch-N/`; unfinished downloads wait.
3. It matches each file to an item still without a PDF (DOI or title on
   the first page, Elsevier / DOI file names) and attaches it
   (`pdf:manual`, version-tagged, `[pdf] min_version` applies).

Tell the user to download what they can from the open tabs and say
"done"; then run the next cycle. Relay the summary line (opened,
attached, unmatched, remaining). After the last batch call
`pdf_manual_collect`. Items in `retry.manual` and `retry.below_policy`
open first.

**Never assign a file yourself.** An unmatched or ambiguous file is
listed with its candidates — ask the user which item it is, then
`pdf_manual_assign` with the key *they* name (or `pdf_manual_ignore` for
a stray file). Not your best guess, not the likeliest candidate. State is
in `.claude/pdf/manual_state.json`, so a later session resumes where this
one stopped.

## Diagnose before you exclude — mandatory, after every step

Never go from "the cascade failed" to "tag it unavailable". Most of what
the API cascade fails on is *not* unreachable — it is reachable by a
route not yet tried. On a real 244-item run, 76 of 119 apparent failures
were Sage and Academy of Management articles that one browser pass
recovered; the pipeline had reported all 119 as "no fulltext available".

After **every** step, run the audit — `audit_library` (`uv run
${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/audit_zotero_library.py --group
<id>`) — and read its retrieval report:

```
PDF retrieval: 125/244 attached · 110 still missing

  publisher                 n  cause              next step
  Sage                     48  BROWSER_REQUIRED   --sources browser --publisher sage
  Academy of Management    28  BROWSER_REQUIRED   --sources browser --publisher aom
  IEEE                     16  BROWSER_REQUIRED   Not an exclusion — run enrich_pdfs.py --sources browser
  Wiley                     8  ACCESS_BLOCKED     Flag for ILL — paywall, full text exists

  102 of the 110 are recoverable — they have not been through every route yet.
```

**Report this to the user and say how many items each next step would
recover** — "76 of these 110 are behind Cloudflare at two publishers and
one browser pass gets them" is a decision; "110 items failed" is not. If
a later step of the strategy already covers them, say so and carry on.

A `BROWSER_REQUIRED` row with no `--publisher` (IEEE above) means no
per-publisher handler covers the DOI — **not** that nothing can be done:
the link resolver's platforms (EBSCOhost, JSTOR, ProQuest) and the
Connector key on the item, and the plain browser pass reaches both. After
an API-only run expect *no* `UNAVAILABLE` rows at all.

Then act by cause:

| Cause | Meaning | What to do |
|---|---|---|
| `BROWSER_REQUIRED` | A route the plain-HTTP cascade cannot take is still untried — a Cloudflare-gated publisher whose handler has not run, or no handler but the browser pass has not run, leaving the link resolver and the Connector ahead of the item | The `browser` step (`retry.browser.keys`, or `retry.browser.<publisher>.keys` for one), or `manual`. **Not an exclusion.** |
| `ACCESS_BLOCKED` | Paywalled; the full text exists. With a Wiley prefix and no token, configure `WILEY_TDM_TOKEN` via `/setup` and re-run `api` | `manual` (the user's own access may differ), else the ILL list (`retry.ill.keys`). **Not an exclusion.** |
| `NETWORK_ERROR` | Transport failure | Re-run the step (`retry.network.keys`). **Not an exclusion.** |
| `CORRUPT_DOWNLOAD` | A source served bytes that are not a usable PDF — usually truncated | A *different* source: the same one returns the same bytes. The publisher route, `browser`, or `manual`. **Not an exclusion.** |
| `NO_PDF_OFFERED` | The Connector opened the library's route and its translator finished without a PDF — usually an HTML-only galley; nobody refused access | `manual` (`retry.manual.keys` opens first): save the HTML full text or find the PDF link. Not ILL. **Not an exclusion.** |
| `VERSION_BELOW_POLICY` | A copy below `[pdf] min_version` was found and set aside in `<cache>/below_policy/`, not attached | Get the published version another way (`retry.below_policy.keys`: `paid`, `browser`, `manual`), or ask whether to lower `PDF_MIN_VERSION`. **Not an exclusion.** |
| `UPLOAD_FAILED` | Fetched, but the Zotero attach failed | Re-run `api`: it attaches from the local cache, no new download (`retry.reattach.keys`). Cheapest of all. **Not an exclusion.** |
| `OUT_OF_SCOPE` | Book chapter, thesis, preprint | FE2 / FE3 — excluded on item type, not on retrieval; chasing a PDF wastes the user's time |
| `UNAVAILABLE` | Every route tried, nothing found | FE6 — the only cause that justifies a full-text-unavailable exclusion, and only after the preprint offer below |

**Before accepting `UNAVAILABLE`, there is one more route — and it
changes what the item is.** `enrich_pdfs.py --allow-preprints` looks for
a copy on arXiv / SSRN / RePEc. It is off by default because what it
finds is the manuscript *before* peer review: hypotheses, samples and
findings all move between a working paper and the published article, and
nothing downstream can tell the two apart. Offer it explicitly, say that
is what it does, and let the user decide. Every attachment it produces is
tagged `pdf:preprint-version` (and `pdf:version:submitted`);
`fulltext_code.py` names those items before coding them, and the audit
lists them under `preprint_version`.

**The hard rule: an item may not be tagged `<prefix>/fulltext:unavailable` until
its cause in the retrieval report is `UNAVAILABLE`.** If you have not run
the audit, you do not know the cause, and you may not tag. The audit
writes the retry sets as key files (`retry.browser[.<publisher>]`,
`retry.ill`, `retry.network`, `retry.reattach`, `retry.manual`,
`retry.below_policy`, `true_negative`, `out_of_scope`) under
`.claude/audit/` — feed them straight to `--filter-keys-file`; never
assemble key lists by hand or with `jq`.

The pipeline enforces the same rule from its side: `pdf_fetch_log` will
not classify an item `UNAVAILABLE` while the browser pass is still ahead
of it. An empty `http_status` on such a row means not one source ever
answered "not found" — it is evidence of nothing. One live run produced
227 of them; three that had been written into `true_negative` opened on
the first click through EBSCOhost.

**Never silently drop items.** A paper with no PDF after every step is a
data-quality signal to report, not a failure to hide.

## The API cascade — what to expect from it

The default `enrich_pdfs.py` run is ranked by **version quality first,
cost second**; within a quality tier free sources precede paid ones:

```
Stage 1 — free version of record     ScienceDirect (Elsevier) → Springer
                                     → Wiley TDM → Crossref TDM → PMC
Stage 2 — paid version of record     OpenAlex Content API ($0.01/PDF)
Stage 3 — open access, often the     arXiv → OpenAlex OA → Unpaywall
          author's manuscript        → Semantic Scholar → CORE → OpenAIRE
                                     → BASE → [preprint, min_version=preprint]
```

The authoritative order is `fetchers.pdf_sources`'s docstring (and
`enrich_pdfs.py --help`); read it there rather than restating it from
memory. The paid tier outranks the free stage-3 sources because it serves
the publisher's own, correctly paginated file, while those often hold an
author manuscript whose page numbers do not match; CORE attachments carry
`pdf:repository-copy` for the same reason.

**Three stage-1 sources fail in ways that look like "article
unavailable" and are not.** Check these before concluding anything:

- **Springer is bot-blocked, not network-gated.**
  `link.springer.com/content/pdf/<doi>.pdf` answers any HTTP client with
  an identical ~3 KB `Client Challenge` page — an Imperva JavaScript
  interstitial — regardless of DOI, entitlement or campus network, and a
  full browser header set does not change it. Springer DOIs reliably fall
  through stage 1 and are recovered by the browser pass (or by hand). A
  VPN does not help; the planner does not count them API-likely.
- **Elsevier may return a one-page preview with HTTP 200.** The TDM
  endpoint serves `application/pdf` of plausible size while
  `X-ELS-Status` reads `WARNING - Response limited to first page because
  requestor not entitled to resource`. The fetcher refuses to attach it —
  but the signal is logged at info level, so a run can produce nothing
  from Elsevier while looking healthy. If Elsevier yields no PDFs, suspect
  the key's full-text entitlement rather than the articles.
- **Wiley TDM answers for current content only.** On a live 1,895-item
  pass the token's hit rate was ~20% for 1990-onward articles and **zero**
  for anything older — a back-file entitlement gap, not a failure.
  Pre-1990 Wiley articles are a browser / EBSCOhost or manual job. Wiley
  TDM is in the default cascade; `--sources wiley` is only for a
  Wiley-only run.

## Retrieval policy

The `[pdf]` policy is asked in `/setup` and printed, with each value's
source, at the top of every `enrich_pdfs.py` run:

- `min_version` — `published` (version of record only: open-access copies
  count only when the publisher hosts them; repository copies are refused
  even when they are the typeset PDF), `accepted` (default), `preprint`
  (also preprints — see the hazard above).
- `reconstructed` — attach generated, text-only PDFs rebuilt from
  Elsevier XML when only a preview is served (off by default; the older
  `[elsevier] render_xml_to_pdf` still works).
- `paid` — `off`, `prefer` (default; before the free open-access
  sources) or `last_resort` (only when nothing free has a copy).

A project overrides it with `PDF_MIN_VERSION` / `PDF_RECONSTRUCTED` /
`PDF_PAID` in its `screening_config.py`; one run with `--min-version`,
`--paid` or `--allow-preprints`. A copy below `min_version` is refused,
set aside in `<cache>/below_policy/`, and the cascade asks the next
source. Every attached PDF carries `pdf:version:published`,
`pdf:version:accepted` or `pdf:version:submitted`.

## Cross-cutting rules

- **Pilot the browser modes on a small batch** before a long run —
  challenges and Connector state are session-scoped; find a
  misconfiguration after 10 items, not 500.
- Every downloaded file is checked for `%PDF` magic bytes and parsed
  before it is cached or attached; a "PDF" that is HTML with HTTP 200 is a
  Cloudflare or login page, not an article.
- If the browser pass regresses, file an issue with the run log
  (`--log-csv`) so the failure can be reproduced.
- Replacing a PDF an item already has (`--replace`) is library repair, in
  `zotero-operations`.

## Red flags

- You started a mode without an agreed strategy, or ran a step the
  strategy does not contain.
- You presented the paid cost ceiling from a `lookups: "none"` plan as
  an estimate.
- You started a browser or Connector pass while the user said they are
  away, or because "an unattended run is possible".
- You assigned a downloaded file to an item the user did not name.
- You tagged anything `<prefix>/fulltext:unavailable` without an audit
  saying `UNAVAILABLE`.
- You edited `.claude/pdf/strategy.toml` or `screening_config.py` by hand
  instead of through the tools.
- You read `~/.config/academic-research/config.toml`. Never: it holds API
  keys; the scripts read it themselves.
