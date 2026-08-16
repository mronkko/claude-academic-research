---
name: systematic-review
description: Use when running a full systematic literature review (SLR) — PRISMA-style search, screening, coding, and export. Trigger phrases "systematic review", "SLR", "PRISMA", "screen papers", "code papers", "full-text screening". Do NOT use for isolated Zotero enrichment without a screening pipeline — use `zotero-operations`. Targets social sciences; medical-SLR instruments (RoB 2, ROBINS-I, evidence hierarchies, PRISMA-P) are out of scope.
---

# systematic-review

> **Glossary:** unfamiliar with **PRISMA**, **MCP**, **BBT**, **ABS**,
> **DOI**, **ISSN**, **SFX**, **TDM**, **CSL**, **stage tag**, or
> **FE-code**? See [skills/_glossary.md](../_glossary.md) for one-line
> definitions of every acronym this skill uses.

## Pre-flight (ALWAYS run first)

Before any step below, verify the plugin has been configured:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-.}/scripts/setup/check_configured.py"
```

If the result is `NOT CONFIGURED`, stop immediately and tell the user:

> The academic-research project has not been set up on this machine
> yet. Run the setup skill or setup wizard first to configure API keys (Zotero, Elsevier,
> WoS, Anthropic, Gemini, Semantic Scholar), MCP servers, and permission
> rules. Do not attempt an SLR before that.

Do not call MCP tools, run pipeline scripts, or proceed with any stage
of the procedure. Running the setup skill/wizard is the required first step.

If the result is `configured`, proceed.

---

## Bootstrap (first run in this project)

An SR project needs (a) the canonical directory scaffold, (b) four
regression-test files, and (c) pipeline-stage config templates. Run
the three setup helpers below in order. They are all idempotent —
re-running skips anything already in place. Do not use shell
`mkdir -p` (prompts the user, bash-only) or chained `cp` calls
(prompts the user for every chain) for the same work.

Create the directory scaffold:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-.}/scripts/setup/ensure_dir.py" \
    scripts screening pdfs analysis analysis/results manuscript
```

Check what's already present:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-.}/scripts/setup/check_project_scaffold.py" \
    scripts/test_common.py scripts/test_citations.py \
    scripts/test_empirical_integrity.py scripts/test_systematic_review.py \
    search_config.py screening_config.py \
    analysis/manuscript_stats.py manuscript/manuscript_tables.py \
    manuscript/manuscript.qmd
```

If any are missing, install them (one call, skip-if-exists for the
rest):

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-.}/scripts/setup/install_templates.py" \
    test_common.py:scripts/test_common.py \
    test_citations.py:scripts/test_citations.py \
    test_empirical_integrity.py:scripts/test_empirical_integrity.py \
    test_systematic_review.py:scripts/test_systematic_review.py \
    search_config.py:search_config.py \
    screening_config.py:screening_config.py \
    manuscript_stats.py:analysis/manuscript_stats.py \
    manuscript_tables.py:manuscript/manuscript_tables.py \
    manuscript.qmd:manuscript/manuscript.qmd
```

Tell the user which files were installed and flag that the top of
each `test_*.py` has project-specific paths, `test_empirical_integrity.py`
has a `FORBIDDEN_LITERALS` tuple, and `search_config.py` /
`screening_config.py` / `manuscript_stats.py` all need customisation
before use.

If the project has no `CLAUDE.md` yet, suggest using
`${CLAUDE_PLUGIN_ROOT:-.}/templates/sr_claude_md.md` as a starting
point — but don't write it without the user's say-so. CLAUDE.md is
user-owned. To install once the user confirms:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-.}/scripts/setup/install_templates.py" \
    sr_claude_md.md:CLAUDE.md
```

---

## Zotero access — see the `zotero-operations` skill's IRON RULE

Before any Zotero work in this skill: when reading or writing the
user's library, the access hierarchy is **(1) MCP `mcp__zotero__*`
tools for reads → (2) `zotero-cli` for one-off writes outside a
pipeline script → (3) `scripts/pipelines/zotero_io.py` and
`scripts/pipelines/bbt_client.py` for bulk/pipeline reads and writes
→ (4) never direct HTTP**. `zotero-cli` is not a substitute for
`zotero_io.py` inside enrichment/screening/coding scripts — no
batching, no `--json`, no 412 retry, ~1–2 s per-call startup. A
direct `urllib.request.urlopen("http://127.0.0.1:23119/...")` or
`curl localhost:23119` is a defect signal — propose adding the
missing helper to `zotero_io.py` rather than working around it
inline. The full rule lives in [skills/zotero-operations/SKILL.md](../zotero-operations/SKILL.md)
under "IRON RULE — Zotero access goes through the plugin's surface".

The CI guard at `tests/unit/test_no_direct_localhost_zotero.py`
fails the build if a direct-HTTP call slips into a pipeline file
that isn't `zotero_io.py` or `bbt_client.py`.

## Zotero library selection (required before any Zotero write)

Run this **first**, right after bootstrap. Pin down which Zotero
library will hold this review's bibliography before starting the
scope conversation — the choice is independent of scope, takes a
single question to resolve, and unblocks every later step that
touches Zotero. Running it first also means the project's
`CLAUDE.md` carries the library reference from the outset, so a
future session opening the project sees it immediately.

The choice is stored in the project's `CLAUDE.md` and passed
explicitly to every pipeline script as either `--group <id>` (a
group library) or `--user` (your personal / My Library), plus
`--collection <key>` where supported. It is NOT set via the
`ZOTERO_GROUP` env var — env vars are per-shell, easily lost on a
new terminal, and invisible to future sessions that read the
project's `CLAUDE.md` to orient themselves.

**Procedure:**

1. List available libraries:

   ```
   mcp__zotero__zotero_list_libraries()
   ```

   Show the user the personal library (type `user`) and each group
   (type `group`, with numeric IDs). Ask which to use. Group
   libraries are the usual choice for SRs — shared with
   collaborators, higher upload quota, cleaner archival than mixing
   into personal — but pipeline scripts fully support My Library via
   `--user`, so either works.

2. Optional: scope to a collection within the chosen library. Ask
   the user whether this SR's items should go into an existing
   collection, or a fresh one. For an existing one:

   ```
   mcp__zotero__zotero_get_collections(library_id=<id>)
   ```

   For a fresh collection, note the intended name — `import_to_zotero.py`
   creates it on first use when `--collection <name>` is passed and
   no matching key exists.

3. Write the choice into the project's `CLAUDE.md` under a
   `## Zotero library` heading (create or extend the file as
   needed). Ask the user to confirm the edit before saving. Shape
   depends on group vs personal:

   ```markdown
   ## Zotero library

   - **Library:** group (or `user` for personal)
   - **Group ID:** `<numeric id>`   (omit if `Library: user`)
   - **Collection key:** `<8-char Zotero key>`   (omit if creating
     fresh at import time)

   All pipeline scripts take `--group <id>` (group library) or
   `--user` (personal library) and, where supported,
   `--collection <key>`. Do not set `ZOTERO_GROUP` as an env var —
   the canonical record is here.
   ```

**Self-check before every Zotero write:** does the project's
`CLAUDE.md` have a `## Zotero library` section with a group ID? If
not, STOP and run the procedure.

---

## Scope lock-in (required before any search)

Before calling ANY search tool — MCP (`mcp__scopus__search_scopus`,
`mcp__openalex__search_*`, `mcp__semantic-scholar__*-search*`,
`mcp__paper-search*__search_*`) or script
(`scripts/pipelines/search*.py`) — **including piloting and volume
probes** — the scope brief must exist at
`.claude/systematic-review/scope.md` AND the user must have
explicitly confirmed it in the current session ("proceed", "looks
good", "confirmed", or equivalent). Silence is not confirmation, and
"experiment with X" is not confirmation of the surrounding scope.

The gate exists because "just a pilot search" shapes the methods:
keyword combinations get baked into the user's mental model, volume
numbers anchor downstream inclusion calls, and reframing after a
pilot is more expensive than reframing on paper. Pin down scope on
paper, get explicit sign-off, then search.

**Brief contents (every section required before asking for
confirmation):**

1. **Focal construct / phenomenon scope** — what is the central
   topic, and at what breadth? Give the breadth as a narrow / medium
   / broad choice with a concrete definition of each, and justify
   the choice. (E.g. a review on "remote work" could go narrow
   = post-2020 pandemic-induced remote work, medium = any scheduled
   telework since 2000, broad = all spatially distributed work
   arrangements.)
2. **Population / unit of analysis / context** — what units are in
   scope (individuals / teams / firms / ventures / SMEs / industries
   / countries / etc.)? Geographic / sectoral / temporal-era
   restrictions? If multiple units appear in the scope, name the
   synthesis strategy (separate strands? single framework?).
3. **Research question(s)** — one or more focal questions the review
   will answer. If the synthesis will map multiple streams of the
   literature — e.g. X-as-antecedent vs X-as-outcome, or phenomenon
   used as a tool vs studied as a domain vs applied as a research
   method — name the streams. Flag whether streams are a narrative
   device only, or whether a per-paper `research_stream` coding
   field should extend `FULLTEXT_CODING_FIELDS` in
   `screening_config.py` (this is a proposal, not a prescription —
   the default template does not include one).
4. **Time window** — start year (inclusive), end year (inclusive),
   and the reason for the start year (a pivot paper, a technology
   event, a round number with a defence).
5. **Journal set** — tier list (AJG/ABS 2024 / FT50 / ABDC), which
   field codes within it, and whether ISSN-filtering will be used
   (requires WoS Expanded).
6. **Database access** — which databases the formal search will
   use. Do NOT ask the user blind. First run the probe (it reads
   `~/.config/academic-research/config.toml` out-of-process and
   emits only yes/no status — no keys):

   ```bash
   python3 "${CLAUDE_PLUGIN_ROOT:-.}/scripts/setup/check_database_access.py"
   ```

   Then present the detected set and ask which subset to use. For a
   formal SR, two or three databases are typical; prefer WoS
   Expanded + Scopus when both are available, OpenAlex + Semantic
   Scholar as free fallbacks. Record both the chosen set and any
   available-but-excluded databases (with the user's reason).
7. **Exclusion criteria** — language restriction? editorials / book
   reviews / proceedings? conference papers? predatory-listed
   journals?
8. **Search keyword blocks** — the literal term lists for each
   Boolean block of the query (typically one block per scope
   dimension identified in items 1–2). These lists go verbatim into
   Scopus / WoS / OpenAlex queries. Present them block-by-block,
   with wildcards where stemming is needed (WoS does not stem
   phrases; Scopus does), and ask the user to approve each block.
   This is the level of detail that actually goes into
   `search_config.py` — do not commit without explicit approval,
   because small term choices (e.g. `"firm growth"` alone vs
   `"firm growth" OR "venture growth" OR "business growth*"`)
   drastically change recall.

Draft the brief in conversation, ask the user to confirm, then write
`.claude/systematic-review/scope.md`. Create the directory first:

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-.}/scripts/setup/ensure_dir.py" .claude/systematic-review
```

If the user changes scope mid-run, update `scope.md` and any
affected `search_config.py` together before further searches.

**Self-check before every search call:** has `scope.md` been written?
Has the user said "proceed" (or equivalent) since the brief was
finalised? If either answer is no, STOP and finish the interview.

**Self-check before any Write / Edit on `search_config.py`:** is the
keyword list in the current draft of `search_config.py` identical
(up to formatting) to the block-by-block keyword list the user
approved as item 8 of the scope brief? If the agent revised
keywords after a pilot or reviewer feedback, update `scope.md`
first, get fresh user confirmation on the revised blocks, then
write `search_config.py`. Never silently expand keyword coverage
between scope.md and search_config.py.

---

## Choosing the screening model

Model choice has two layers. **The provider** is a machine-wide setting
— which company's (or which local server's) API the pipelines call.
**The tier** is a per-stage capability level: `fast` for screening
thousands of abstracts, `balanced` for coding full texts. No model ID
is hardcoded anywhere in the plugin; you ask the provider what it
serves today and pin a choice.

`screening_config.py` records the result (`ABSTRACT_SCREENING_MODEL`,
`FULLTEXT_CODING_MODEL`) with a provenance comment. That pin is the
project's committed default and belongs in git with the prompts.

**Which provider is configured** — ask, don't guess, and never try to
read `config.toml` (the Read tool is denied on it):

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_llm_provider.py
```

It reports the provider, whether the user ever chose one, and whether
its credential is present — without printing any key.

**Pinning the models** (bootstrap, and any time the provider changes)
is a two-step, and the middle step is the user's. First see what is on
offer — this writes nothing:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/resolve_models.py
```

**Then propose one model per stage and get the user's confirmation
before pinning anything.** The script deliberately does not choose:
provider listings are full of things that are not ordinary synchronous
chat models — `:batch` IDs are asynchronous queue endpoints, and
`-image`, `-tts`, `-audio`, `deep-research` and `customtools` variants
appear right alongside the models you want. The `tier?` column is a
guess from the model's name and is labelled as one. Reading past that
is your job, not the script's.

Then write each confirmed choice, one call per stage, from the project
directory:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/resolve_models.py \
    --stage abstract_screening --model <id>
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/resolve_models.py \
    --stage fulltext_coding --model <id>
```

It rewrites only that stage's `*_MODEL` line, leaving the prompts
untouched, and stamps a provenance comment whose `tier=` label is
inferred from the model you pinned (override with `--tier`). Add
`--dry-run` to see the line without writing it. If the listing step
reports a **catalogue fallback**, say so to the user: the menu came
from a file shipped with the plugin rather than from the provider, and
may name superseded models.

Each pin is followed automatically by a ~4-token test request, and
**`resolve_models.py` exits non-zero if the model does not answer.** Do
not treat a pin as done until that check passes — a written pin only
records an intention, and the check is what proves the model ID, the
provider, and the credential agree.

### Check the connection before any batch run — mandatory

Before `abstract_screen.py` or `fulltext_code.py` runs over a real
corpus:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_model_connection.py
```

Exit `0` means every pinned model answered; `1` means one did not; `2`
means the project is not pinned or configured yet. Both orchestrators
also run this check themselves at startup and refuse to begin when it
fails, so a run that dies here has cost nothing.

**Read the status word, because two of them look alike and are not.**

| Status | What it means | What to do |
|---|---|---|
| `QUOTA_EXHAUSTED` | The key is valid; its allowance is spent | **Retrying will not help.** Tell the user to check billing, or switch provider. Do not restart the run. |
| `RATE_LIMITED` | Throttled this second; the quota is intact | Re-run, or lower `--workers`. |
| `AUTH_FAILED` | The key was rejected | Hand off to `/setup` to rotate it. Never ask for a key in chat. |
| `MODEL_NOT_FOUND` | The provider does not serve this ID | Re-list and re-pin. Usually a typo or a superseded model. |
| `UNREACHABLE` | Nothing answered at the endpoint | For `ollama` / `lmstudio`, the local server is not running. For `gateway`, either `[gateway] base_url` is unset — the detail line says so — or the endpoint needs the institution's network or VPN. |

The first row is why this section exists. In a real run, an exhausted
Gemini quota was diagnosed as a network hang: the per-item retry ladder
spent 131 seconds per paper failing, progress printed only every tenth
paper, and ~22 minutes passed with no output and no cause. The response
body had said "check your plan and billing details" from the first
request. **When you see `QUOTA_EXHAUSTED`, report it and stop — do not
add timeouts, do not retry, do not diagnose the network.**

**Switching provider** — "use OpenAI instead", "screen this locally":

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/set_llm_provider.py openai
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/resolve_models.py
```

then confirm and pin as above — the old pins name models the new
provider does not serve.

Providers: `anthropic`, `google`, `openai`, `openrouter`, `gateway`,
and the two local ones, `ollama` and `lmstudio`, which need no API key
and no per-paper spend. If the new provider's credential is missing, the
script says which variable is needed — **hand that to `/setup`, never
ask the user to paste a key into the conversation.**

**`gateway`** is an OpenAI-compatible endpoint the user's institution
runs — often free to the researcher and serving open-weight models.
Two differences matter when screening against one:

- It needs an **address as well as a key**, and unlike every other
  provider it has **no environment variable** — both live in
  `config.toml` under `[gateway]`. Everything reports `UNREACHABLE`
  until `base_url` is set. A user who already exports a key under their
  own name can point at it with `[gateway] api_key_env = "THEIR_NAME"`.
- `resolve_models.py` lists and pins against it normally, but there is
  **no list price on file**, so `--dry-run` reports the cost as
  **unknown**. Report that word. Do not translate it to "free" — an
  institution may recharge internally, and you cannot see that.

Do not reach for `OPENAI_BASE_URL` to do this. It exists to redirect
OpenAI itself; a gateway configured through it reports as `openai` and
inherits OpenAI's list prices, which makes the cost estimate wrong.
`ANTHROPIC_BASE_URL` is for Anthropic-Messages-shaped endpoints only.

**A one-off model change** — "screen these with the cheap one", "use a
stronger model for the coding pass" — is a `--model` flag, not an edit:

```bash
uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/abstract_screen.py \
    --group <id> --collection <key> --config ./screening_config.py \
    --model fast
```

`--model` accepts a tier (`fast`, `balanced`, `deep`), a short alias
that names a tier (`cheap`, `haiku`, `flash`, `mini`, `sonnet`, `opus`,
`pro`, `best`, …), or a full model ID. Aliases resolve against the
configured provider, so `--model fast` means Haiku for an Anthropic
user and Flash for a Gemini one. Unknown names pass through untouched,
so an explicit ID or a locally-served model name both work.

**Do NOT edit `screening_config.py` to satisfy a one-off request.** The
config is what a reviewer reads to reconstruct the review; rewriting it
per run destroys that record. The flag prints a banner when it overrides
the config, and the effective model is written to the `model` column of
every CSV log row — **that column, not the config file, is what the
manuscript should cite** (see `empirical-integrity`).

If the user wants the change to be permanent — "always code full texts
with the strong model" — re-pin it rather than hand-editing the
constant, so the provenance comment stays true:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/resolve_models.py \
    --stage fulltext_coding --model <id>
```

Run the script bare first if you need to see what the provider serves,
and confirm the specific model with the user before writing. Then say
that you did, and that it changed the committed default.

**Cost before spending.** `--dry-run` on either screening script prints
a projected cost for the real item count. Quote it before a full run,
especially when the user is about to move to a stronger tier — `deep`
is roughly double `balanced` per paper. An "unknown" estimate means no
list price is on file for that model; report that rather than implying
the run is free.

---

## Screening protocol (required before `abstract_screen.py`)

The abstract-screening system prompt lives in `screening_config.py`
(`ABSTRACT_SCREENING_SYSTEM_PROMPT`) and is **the** record of what
got in and what got out at stage 1. Reviewers will read it to judge
whether the review is reproducible. PRISMA expects it to be fixed
before screening starts, not tuned while decisions accumulate.

**Procedure** (run the first time the agent is about to call
`abstract_screen.py`):

1. Open `screening_config.py` and locate every `<INSERT …>` / `<…>`
   placeholder in `ABSTRACT_SCREENING_SYSTEM_PROMPT`. There are
   typically four:
   - **Research question.** Copy verbatim from `.claude/systematic-review/scope.md` item 3.
   - **Inclusion criteria (population / construct / outcome).**
     Translate scope-brief items 1–2 into decision rules an LLM can
     apply to a title-and-abstract pair in one pass. Each criterion
     needs an **examples** list (what passes) and a **NOT relevant**
     list (what fails — common false-positives).
   - **Exclusion codes E1…E5.** One code per criterion missing + a
     catch-all. Keep them short (≤ 40 chars) so they fit in tag
     labels and QA summaries.
   - **Bias line.** Default "be liberal; missing a relevant paper
     is more costly than reading one extra full text" is correct
     for most SRs; override only if the user has a defensible
     reason (tight inclusion domain, pre-registered precision
     target).

2. Draft each replacement in conversation. Show the user the
   resulting prompt in full (prefer a read-back / inline code block
   over just "here's the diff"). Ask them to approve criterion by
   criterion — criteria are the review's spine.

3. Bump `ABSTRACT_SCREENING_PROMPT_VERSION` to a fresh string (e.g.
   `vN-YYYY-MM-DD`). The version goes into every log row; reviewers
   use it to distinguish a re-run under the same protocol from a
   re-run under a revised protocol.

4. Write the revised file. Record a one-line summary of the
   protocol in `.claude/systematic-review/scope.md` (append, don't
   replace) so the scope brief and the screening config stay in
   lockstep.

**Self-check before every `abstract_screen.py` call:** does
`ABSTRACT_SCREENING_SYSTEM_PROMPT` contain any `<INSERT` /
`<CRITERION` / bare `<…>` placeholders? Has the user approved the
prompt in the current session? If either answer is no, STOP and run
the procedure.

**Revision during screening.** If the user wants to tighten a
criterion after seeing real decisions: bump
`ABSTRACT_SCREENING_PROMPT_VERSION`, have the user re-approve, and
re-run with `--rerun` so the new version replaces prior decisions
on the affected items. The append-only log preserves the original
decisions under the old version for audit.

---

## Coding protocol (required before `fulltext_code.py`)

The full-text coding schema — `FULLTEXT_CODING_FIELDS` plus
`FULLTEXT_CODING_SYSTEM_PROMPT` in `screening_config.py` — is the
record of what data the review extracts from each included paper.
Fields added or reworded after coding starts create inconsistent
columns in `coded_papers.csv`; PRISMA expects the schema to be
fixed before coding starts.

**Procedure** (run the first time the agent is about to call
`fulltext_code.py`):

1. Draft the coding schema. The template defaults are
   `key_findings`, `sample`, `method`; these are safe starters for
   nearly any SR. Propose additions based on the scope brief's
   research questions — common add-ons for social-sciences SRs
   include `theories_and_references`, `direction_of_relationship`,
   `moderators_boundary_conditions`, `causal_inference_strength`,
   `future_research`, and (if the scope brief named streams) a
   `research_stream` enum field. 5–15 fields total is typical;
   each field needs a `name`, a `description` written for an LLM
   reader, and ideally an `example`.

2. Fill in `FULLTEXT_CODING_SYSTEM_PROMPT` placeholders: research
   question (same as stage 1), stage-2 criteria (what the full
   text must show that the abstract could not), and exclusion
   codes `FE1…FE5`.

3. Show the user the full schema (every field) and the prompt.
   Ask them to approve field-by-field — adding a field mid-run
   costs a re-code of every already-coded paper.

4. Bump `FULLTEXT_CODING_PROMPT_VERSION` to a fresh string.

5. Write the revised file. Append a one-line summary to
   `.claude/systematic-review/scope.md`.

**Self-check before every `fulltext_code.py` call:** does
`FULLTEXT_CODING_SYSTEM_PROMPT` contain any `<INSERT` /
unpopulated placeholders? Does `FULLTEXT_CODING_FIELDS` still hold
the template's three starter entries unmodified? Has the user
approved the schema in the current session? If any answer is no or
yes-still-template, STOP and run the procedure. If only 1–3 fields were revised and prior adjudicator edits on other fields must be preserved, use `--update-fields` rather than a full re-code — see *Revision during coding* below.

**Revision during coding.** Two revision paths exist — choose based
on scope:

- **Add or revise specific fields (`--update-fields`)** — preferred for
  additive schema changes (new field) or guideline rewrites for 1–3 fields.
  This mode selects items already tagged `fulltext:include`, calls the LLM for
  all fields (using the updated config), then merges only the named fields into
  the existing `SLR Coding` note without touching any other field values or the
  screening decision. Adjudicator edits to the *targeted* fields are
  overwritten (warn the user); adjudicator edits to all other fields are
  preserved. Bump `FULLTEXT_CODING_PROMPT_VERSION` before invoking so the log
  records which config version produced the update.

  ```bash
  uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/fulltext_code.py \
      --group <id> --collection <key> --config ./screening_config.py \
      --pdf-dir ./pdfs --update-fields method,theories_and_references
  ```

  Combine with `--only-keys K1,K2,...` to limit to a specific subset.

- **Full schema overhaul (`--full-recode`)** — for major version changes
  where every field needs a fresh extraction under the new prompt. This
  removes all `fulltext:*` tags, backs up the CSV log, and re-codes from
  scratch. `SLR Coding` notes for items that re-include are overwritten unconditionally; adjudicator edits to those notes are lost. Notes on items that re-exclude are left untouched. Treat as a `v1 → v2` bump and ask the user to confirm they accept
  the re-coding cost. Bump `FULLTEXT_CODING_PROMPT_VERSION` before invoking.

Field **reordering** in `FULLTEXT_CODING_FIELDS` is free (it only affects
column order in the export CSV and note rendering). Field **renaming** needs
a data migration: the old name stays in existing notes' JSON payloads; use
`--update-fields <new_name>` as a one-pass migration that populates the new
field name, then rename it in config and regenerate.

---

## No improvised pipeline-style code (hard rule)

Before writing ANY of the following, STOP:

- A Bash heredoc that runs Python (`python3 <<'EOF' ... EOF` or
  `python3 - <<'PY' ... PY`).
- An inline `python -c "..."` for anything beyond a single-line
  probe (the four shipped probes under `scripts/setup/check_*.py`
  and `ensure_dir.py` cover all the legitimate single-line cases).
- A multi-line shell pipeline that munges Zotero / search /
  screening / coding state.

If the task is pipeline-shaped (enumerate Zotero items, summarise a
screening CSV, mutate tags, fetch abstracts, filter a search CSV,
compute counts), one of two things must be true:

1. **A shipped script covers it.** Invoke that script with explicit
   flags. The Pipeline-scripts table below is the canonical list.
2. **No shipped script covers it.** Tell the user:

   > There is no shipped script for *<task>*. I can either (a) add a
   > new script under `scripts/pipelines/` (recommended — keeps the
   > work auditable and reusable across sessions), or (b) use the
   > Zotero / OpenAlex / etc. MCP tools directly for this one task.
   > Which do you prefer?

   Wait for their answer. Do **not** write the heredoc.

**Why this rule is hard:**

- Heredoc invocations are not covered by the wizard's allow rules
  (`Bash(python3 ${CLAUDE_PLUGIN_ROOT:-.}/scripts/**)` matches paths,
  not heredocs). Every heredoc triggers a permission prompt.
- Improvised code is composed mid-session — the user has to read
  and approve fresh logic in real time, instead of pre-audited
  shipped code.
- Without a shipped script, every session writes its own variant
  of "filter / count / summarise". The plugin's value is the
  shared library; one-offs erode it.

**Common gaps that surface as "I'll just write a quick script":**

| Task | Right move |
|------|------------|
| Filter / trim a search CSV (top-N by year, year range) | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/filter_search_results.py --input <csv> --output <csv> [--year-min Y] [--year-max Y] [--top-n N]` |
| Summarise screening decisions across passes (last-row-wins, decision counts, list re-screened items) | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/screening_report.py <log.csv> [--list <decision>] [--list-rescreened]` |
| Inspect tag state for one item | Use `mcp__zotero__zotero_get_item_metadata` directly — single MCP call, no Python needed. |
| Read a CSV row count | `wc -l <path>` — already a one-liner. |

**Self-check before any Bash heredoc / inline `python -c`
exceeding one line:** is this task in the Pipeline-scripts table or
covered by a `scripts/setup/` helper? If the answer is "no, but I
could write a quick one", STOP and propose adding the shipped
script instead.

---

## Core architecture

Every systematic review runs through the same stages:

```
search → import to Zotero → fetch abstracts → attach PDFs →
abstract screening → full-text screening/coding → QA with evaluator agents →
human adjudication → export results → test suite → manuscript
```

Principles:

- **Scripted searches only.** Main searches run as Python scripts querying
  APIs directly (Scopus, WoS Expanded, OpenAlex). MCP tools may be used for
  piloting (keyword tests, volume estimates), never for the formal search.
- **Zotero is the ground truth.** Every screening decision, coding field,
  and adjudication outcome lives on the Zotero item — as a tag (for
  decisions and stage membership) or as a child note (for structured
  coding fields). See *Zotero tag conventions* and *Child notes* below for
  the vocabulary. Scripts never delete items from Zotero. See the
  `zotero-operations` skill for lower-level Zotero patterns.
- **CSV logs are run-history, not source of truth.** Screening and coding
  scripts append a row per decision to `screening/*.csv` for provenance
  and debugging (who decided what, when, with which model and prompt
  version). But "what is the current decision on item X?" is answered
  by Zotero, not the CSV. Adjudicator flips happen in Zotero directly;
  re-runs read Zotero tags to decide what to skip.
- **Fix the data, don't work around it.** When a script hits records
  missing a DOI / ISSN / abstract, pause and surface the items. Missing
  DOIs are usually a data-capture bug (search-API field not mapped, manual
  entry, non-journal item). Do not add silent title-match fallbacks until
  the user confirms the data is genuinely unfixable.
- **Resumable stages.** Every stage is Ctrl+C-safe and resume-idempotent.
  On start, scripts read the project's Zotero collection, build a "done"
  set from the stage tags (`abstract:include` / `abstract:exclude` /
  `abstract:borderline` for abstract screening; `fulltext:include` /
  `fulltext:exclude` for full-text coding), and skip items already
  tagged. The CSV log is written in parallel for provenance but is not
  consulted for resume decisions.
- **Progress the user can follow.** Pipeline scripts use `flush=True` on
  every print; emit `[N/total]` counters; invoke via `| tee` to a log
  file. Never pipe to `/dev/null`.
- **Walk the user through the process.** At every milestone, explain the
  user the process. State the stages, explain where we are now and what the 
  user needs to do and what the agent does in this stage. Also summarize what
  we have accomplished this far and what work remains. The user is likely a
  doctoral student or a professional researcher but not an engineer. Assume the
  user knows the basic principles of systematic review but not the details of
  our tooling.  
- **Filterable.** Every stage accepts some filter-keys mechanism —
  `--filter-keys-file <path>` for enrichment / audit / export scripts,
  `--only-keys <k1,k2,…>` for screening scripts. Either way, the next
  stage drives from the previous stage's Zotero tag state (queried via
  MCP or pyzotero); the file / CLI filter is a way to narrow further.

## Environment variables

| Variable | Used by | Purpose |
|----------|---------|---------|
| `ZOTERO_API_KEY` | All scripts | Zotero API authentication (required) |
| `ANTHROPIC_API_KEY` | Screening scripts | Claude API — required only while `anthropic` is the selected provider. Each provider has its own variable; `check_llm_provider.py` reports which one this machine needs. |
| *(none — `[gateway]` in config.toml)* | Screening scripts | The institutional gateway is config-only: `base_url` and `api_key`, plus optional `api_key_env` / `base_url_env` naming variables the user already exports. |
| `ELSEVIER_API_KEY` | `enrich_pdfs.py`, `enrich_abstracts.py` | Elsevier/ScienceDirect full-text retrieval |
| `SCOPUS_API_KEY` | Search scripts | Scopus API (often same as `ELSEVIER_API_KEY`; some institutions issue separately) |
| `WILEY_TDM_TOKEN` | `enrich_pdfs.py --sources wiley` | Wiley TDM UUID token |
| `OPENALEX_API_KEY` | `enrich_pdfs.py`, `enrich_abstracts.py` | OpenAlex Content API ($0.01/download, paid) |
| `SEMANTIC_SCHOLAR_API_KEY` | `enrich_abstracts.py` | Semantic Scholar (higher rate limit with key) |
| `CROSSREF_MAILTO` | All scripts | Crossref polite pool (any email) |
| `WOS_API_KEY_EXTENDED` | Search scripts | WoS Expanded (full Boolean, `IS=` works) — **prefer this** |
| `WOS_API_KEY` | Search scripts | WoS Starter (field-limited, no `IS=`) — piloting only |

The `/setup` skill writes these to `~/.config/academic-research/config.toml`
(mode 0600) on first run. Environment variables take precedence over the
file.

Project-level Zotero selection (group ID, collection key) is **not**
an env var — it lives in the project's `CLAUDE.md` per the *Zotero
library selection* section above, and is passed to every pipeline
script as `--group <id>` (and `--collection <key>` where supported).
Scripts fall back to `$ZOTERO_GROUP` only as a convenience for
command-line invocations; skill agents pass the flag explicitly.

## Zotero tag and note conventions

Zotero is the ground truth for screening decisions, coding fields, and
adjudication outcomes (see the *Core architecture* principles above).
This section is the canonical catalogue of every tag and child note
the pipeline reads or writes. Scripts and skills reference these
conventions; the table below is the single source of truth.

### Stage tags (set by screening scripts)

Tell you where each item is in the pipeline. Mutually exclusive within
each stage — an item has at most one `abstract:*` tag and at most one
`fulltext:*` tag at any given time. Scripts apply these at decision
time via the Zotero API and remove prior stage tags on flip.

| Tag | Applied by | Meaning |
|---|---|---|
| `abstract:include` | `abstract_screen.py` | Passes title-abstract screening — proceeds to full-text |
| `abstract:exclude` | `abstract_screen.py` | Excluded at title-abstract stage |
| `abstract:borderline` | `abstract_screen.py` | Kept for full-text review (missing abstract, or LLM uncertain) |
| `fulltext:include` | `fulltext_code.py` | Passes full-text screening; has `SLR Coding` child note |
| `fulltext:exclude` | `fulltext_code.py` | Excluded at full-text stage |

### Pre-screening and quality-flag tags

Set outside the main screening loop — by preflight checks (predatory)
or post-screening quality audits (retraction). Both are warnings the
adjudicator sees in Zotero, not automatic exclusions.

| Tag | Applied by | Meaning |
|---|---|---|
| `predatory:flag` | Preflight journal check against Beall's list (`import_to_zotero.py`) | **Warning, not exclusion.** Author decides during full-text review whether to keep each flagged paper. |
| `retracted:flag` | Post-coding retraction check via `mcp__zotero__scite_check_retractions` (see *Retraction check* in *Key methodological rules*) | **Warning, not exclusion.** Cited paper has been retracted per Scite's retraction-watch data. Adjudicator decides whether to keep (with a discussion note), replace the citation, or drop the paper. |
| `pdf:tdm-recovered` | `enrich_pdfs.py`, when Elsevier's TDM API returns only a 1-page preview and the fetcher falls back to the XML endpoint | **Warning, not exclusion.** The attached "PDF" is text reconstructed from XML, not the publisher's native PDF — may be less complete or lose figures/tables. `audit_zotero_library.py` lists these under `tdm_recovered`; review before/during full-text coding. |
| `pdf:preprint-version` | `enrich_pdfs.py --allow-preprints`, when the only copy found is on arXiv / SSRN / RePEc | **Warning, not exclusion — and the most consequential of these.** The attached PDF is the manuscript *before* peer review. Hypotheses, samples and findings all move between a working paper and the published article, and nothing downstream can tell the difference: the coding note and the CSV row read identically either way. `fulltext_code.py` names these items before it codes them and `audit_zotero_library.py` lists them under `preprint_version`. Verify each coded finding against the published article, or fetch the real one, before the numbers reach a manuscript. Do **not** confuse this with `OUT_OF_SCOPE`, which is about the *item's* type being a preprint; this is a journal article with a preprint file attached. |
| `fulltext:unavailable` | Applied by the agent **only** after `audit_zotero_library.py` reports the item's cause as `UNAVAILABLE` | The full text could not be obtained by any route the plugin has. Note this is a `fulltext:*` tag, so it is mutually exclusive with `fulltext:include` / `fulltext:exclude`. **Do not invent a spelling for this** — there is exactly one, and it is this one. **Do not apply it on a failed enrichment run alone:** a `BROWSER_REQUIRED` or `ACCESS_BLOCKED` item is reachable and this tag would be false. See *Phase 4 — diagnose before you exclude*. |

### QA and adjudication tags

Applied during the post-screening QA evaluator pass and the human
adjudication loop (see *Post-screening QA* below).

| Tag | Applied by | Meaning | Removed when |
|---|---|---|---|
| `qa-flag` | Main agent after any evaluator flags an item | Sentinel for filtering in Zotero | After human adjudication (replaced by `qa-adjudicated-*`) |
| `qa-hard` | Main agent from a HARD evaluator flag | Clear violation of a named inclusion / exclusion criterion | After adjudication |
| `qa-soft-include` | Main agent from an inclusion-validator SOFT flag | Borderline inclusion | After adjudication |
| `qa-soft-exclude` | Main agent from an exclusion-validator SOFT flag | Borderline exclusion | After adjudication |
| `qa-wrong-code` | Main agent from an exclusion-validator `WRONG_CODE` flag | Exclusion stands but the code is wrong | After the exclusion code is corrected on the item |
| `qa-adjudicated-include` | Human after reviewing flag | Final decision: INCLUDE | Never (permanent adjudication record) |
| `qa-adjudicated-exclude` | Human after reviewing flag | Final decision: EXCLUDE | Never |

### Flip semantics under adjudication

If the human adjudicator flips an automated decision, the Zotero tag
set is updated atomically:

- Remove the screener's `fulltext:*` tag → add the opposite one.
- Remove the `qa-*` severity tag → add the matching `qa-adjudicated-*`.
- Optionally append a row to `screening/fulltext_screening.csv` for
  provenance (who flipped, when, why). The CSV is run-history; the
  tag is the current state.

### Child notes

| Note title | Attached to | Written by | Purpose |
|---|---|---|---|
| `SLR Coding` | Every item with `fulltext:include` | `fulltext_code.py` after each coding decision | Structured coding fields (constructs, method, findings — see `screening_config.py:FULLTEXT_CODING_FIELDS`). The adjudicator reads this note directly in Zotero; the CSV row is parallel provenance. Overwritten on `--full-recode`; selectively updated on `--update-fields`. |

A `SLR Coding` note is **created on first code**, **overwritten on
re-code** (via `--full-recode`), and **never deleted automatically**.
If the adjudicator edits a field inline in Zotero, the edit is
authoritative — subsequent `fulltext_code.py` runs skip that item
unless `--full-recode` is passed.

### How scripts use these conventions

- **Resume is tag-driven.** Each script queries Zotero for items
  already carrying the stage tag it writes, and skips them. The CSV
  log is not consulted for resume decisions. `--only-keys` / `--rerun`
  / `--full-recode` flags are the escape hatches for re-processing
  specific items.
- **Filtering downstream stages.** `fulltext_code.py` processes items
  tagged `abstract:include` OR `abstract:borderline`.
  `export_coded_includes.py` reads items tagged `fulltext:include`
  (adjudication flips propagate automatically because tags are
  authoritative).
- **Never hand-craft tags in a manuscript chunk or stats script.**
  Tags come from Zotero; if a stat needs a count of `fulltext:include`
  items, `manuscript_stats.py` queries Zotero, not the CSV.

## Pipeline scripts

All scripts live under `${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/`. Invoke
with `uv run`; first-run `uv` installs declared deps into an ephemeral
venv automatically. Invocations below show the most common form; run
each script with `--help` to see the full flag surface (every script
has additional options for re-processing, parallelism, caching, and
single-item debugging).

| Stage | Script | Invocation |
|---|---|---|
| Multi-database formal search | `search.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/search.py --config ./search_config.py [--databases scopus,wos,openalex,semantic_scholar]` |
| Single-database piloting (Scopus) | `search_scopus.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/search_scopus.py --config ./search_config.py` |
| Single-database piloting (Web of Science) | `search_wos.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/search_wos.py --config ./search_config.py` |
| Single-database piloting (OpenAlex, free) | `search_openalex.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/search_openalex.py --config ./search_config.py` |
| Single-database piloting (Semantic Scholar) | `search_semantic_scholar.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/search_semantic_scholar.py --config ./search_config.py` |
| Summarise a pilot CSV — year-cutoff distribution | `pilot_analyze.py year-cutoff` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/pilot_analyze.py year-cutoff --csv pilot/search_results.csv` |
| Summarise a pilot CSV — cross-DB DOI overlap | `pilot_analyze.py db-overlap` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/pilot_analyze.py db-overlap --csv pilot/search_results.csv` |
| Summarise a pilot CSV — journal coverage (top-N) | `pilot_analyze.py journal-coverage` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/pilot_analyze.py journal-coverage --csv pilot/search_results.csv --top 25` |
| Summarise a pilot CSV — hits by journals.json field code | `pilot_analyze.py field-breakdown` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/pilot_analyze.py field-breakdown --csv pilot/search_results.csv --journals journals.json` |
| Filter / trim a search CSV (top-N by year, year range) | `filter_search_results.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/filter_search_results.py --input <csv> --output <csv> [--year-min Y] [--year-max Y] [--top-n N]` |
| Import deduplicated search CSV into Zotero | `import_to_zotero.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/import_to_zotero.py --group <id> --input <search.csv> [--collection <key>]` |
| Abstract screening (Claude Haiku on title+abstract) | `abstract_screen.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/abstract_screen.py --group <id> --collection <key> --config ./screening_config.py [--model <alias>]` |
| Full-text screening + structured coding (Claude Sonnet) | `fulltext_code.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/fulltext_code.py --group <id> --collection <key> --config ./screening_config.py --pdf-dir ./pdfs [--model <alias>]` |
| Update specific coding fields on already-coded items | `fulltext_code.py --update-fields` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/fulltext_code.py --group <id> --collection <key> --config ./screening_config.py --pdf-dir ./pdfs --update-fields FIELD1,FIELD2` |
| Summarise screening / coding decisions across passes | `screening_report.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/screening_report.py <log.csv> [--list <decision>] [--list-rescreened]` |
| Fetch missing abstracts (multi-source cascade) | `enrich_abstracts.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/enrich_abstracts.py --filter-keys-file <keys>` |
| Attach missing PDFs (multi-source cascade) | `enrich_pdfs.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/enrich_pdfs.py --filter-keys-file <keys>` |
| Attach Wiley PDFs only (TDM token) | `enrich_pdfs.py --sources wiley` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/enrich_pdfs.py --sources wiley --filter-keys-file <keys>` |
| Attach Cloudflare-gated PDFs (Sage, APA, T&F, Emerald, …) | `enrich_pdfs.py --sources browser` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/enrich_pdfs.py --sources browser --filter-keys-file <keys>` |
| Audit library (missing abstracts / PDFs / stubs) | `audit_zotero_library.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/audit_zotero_library.py --group <id>` |
| Export includes-only coded view | `export_coded_includes.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/export_coded_includes.py --log-csv <screening.csv> --out <coded.csv>` |
| Generate `references.bib` from manuscript keys | `generate_bib.py` | `uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/generate_bib.py <project_dir>` |

Additional templates shipped with the plugin:

- **`${CLAUDE_PLUGIN_ROOT:-.}/templates/search_config.py`** — journal
  list, query definitions, year window. Read by `search.py` and
  `search_openalex.py`.
- **`${CLAUDE_PLUGIN_ROOT:-.}/templates/screening_config.py`** — system
  prompts for abstract screening and full-text coding, plus the
  `FULLTEXT_CODING_FIELDS` list that drives the coding schema.
- **Test templates** (copy all three plus `test_common.py` into your
  project's `scripts/` directory). One file per skill so failures map
  back cleanly to the rule-book the regression violated:
    - `${CLAUDE_PLUGIN_ROOT:-.}/templates/test_systematic_review.py` —
      this skill's 11 pipeline invariants (PRISMA arithmetic,
      `search_run.json` integrity, decision-state whitelists,
      `temperature=0`, `screening_config` round-trip, ghost handling).
    - `${CLAUDE_PLUGIN_ROOT:-.}/templates/test_citations.py` — `@citekey`
      resolution, bare `Author (YYYY)` detection, BBT-key uniqueness.
      Owned by the `grounded-citations` / `fact-check` skills.
    - `${CLAUDE_PLUGIN_ROOT:-.}/templates/test_empirical_integrity.py` —
      forbidden-literal grep, label uniqueness, inline `s['…']` key
      resolution against the live `build_stats()` dict, figure-file
      existence, `manuscript_stats.json` ↔ `build_stats()` content
      check. Owned by the `empirical-integrity` skill.
    - `${CLAUDE_PLUGIN_ROOT:-.}/templates/test_common.py` — shared
      `TestRunner` infra the three test files import.
- **`${CLAUDE_PLUGIN_ROOT:-.}/templates/manuscript_stats.py`** —
  flat-dict builder that reads every pipeline output and returns keys
  like `screen.n_included`, `search.unique_dois`, etc. for inline
  lookup in the manuscript. Copy into the project's
  `analysis/manuscript_stats.py`; extend as the manuscript needs new
  facts. Output: `analysis/results/manuscript_stats.json` (written by
  the script's CLI mode; never hand-edited).
- **`${CLAUDE_PLUGIN_ROOT:-.}/templates/manuscript_tables.py`** —
  pandas-based table functions (methods, regions, exclusion reasons,
  construct families) for Quarto code chunks. Keeps prose readable.
  Copy into the project's `manuscript/manuscript_tables.py` so the
  `.qmd` can `from manuscript_tables import ...`.
- **`${CLAUDE_PLUGIN_ROOT:-.}/templates/manuscript.qmd`** — Quarto
  scaffold with setup chunk importing `build_stats()`, placeholder
  sections, and example inline expressions showing every methodology
  number wired to `s['key']` rather than hand-typed.

A project CLAUDE.md template for new SLR projects lives at
`${CLAUDE_PLUGIN_ROOT:-.}/templates/sr_claude_md.md`. A
manuscript-only variant (no SLR-pipeline scaffolding, for research-report
editing projects) lives at
`${CLAUDE_PLUGIN_ROOT:-.}/templates/manuscript_claude_md.md`.

## Key methodological rules

### Search

**Pilot before the formal run.** Before committing to the formal search
parameters, probe each candidate database with a handful of keyword
combinations to surface volume estimates and construct-coverage gaps.
Per the *Scripted searches only* principle above, MCP tools are
acceptable for piloting (they are fast and session-scoped), and are
the only way to probe Scopus / OpenAlex / Semantic Scholar without
first spinning up the full scripted-search machinery. The formal run
then uses the scripted searchers under `scripts/pipelines/`.

Once a pilot search has produced a CSV (from any of the single-database
piloting scripts above, or a trimmed formal-search export), run
`pilot_analyze.py` on it instead of writing an ad-hoc `python3 -c
"import csv; ..."` snippet — its four subcommands (`year-cutoff`,
`db-overlap`, `journal-coverage`, `field-breakdown`) cover the standard
questions a year cutoff, a single-vs-multi-DB decision, and a
journal-scope sanity check need answered. See the table above for
invocations; `--help` lists the full flag surface including `--plot`.

**Source preference ordering.** Which databases to include depends on
what the user's institution provides. Degrade gracefully rather than
blocking on a missing subscription:

| Preference | Source | Access | Notes |
|---|---|---|---|
| 1 (preferred) | **Web of Science Expanded** | Script only, via `WOS_API_KEY_EXTENDED`. No MCP. | Strongest field coverage for social-sciences SR. Use `WOS_API_KEY_EXTENDED`, not `WOS_API_KEY` — Starter's `IS=` ISSN filter returns 0 results and blocks journal-list filtering. |
| 2 | **Scopus** | Script + MCP (`mcp__scopus__*`). Requires `ELSEVIER_API_KEY` or `SCOPUS_API_KEY`. | Strong alternative when WoS is unavailable. Covers the same journal set as WoS with different dedup patterns. |
| 3 | **OpenAlex** | Script + MCP (`mcp__openalex__*`). Free, no subscription. | Open-access baseline; always usable. Weaker field-precision for niche social-sciences topics, but improves year over year. |
| 4 | **Semantic Scholar** | Script + MCP (`mcp__semantic-scholar__*`). Free tier available. | Good for recent work and preprints; complementary to the above. |

A formal SR typically combines **two or three** sources from this list
— the exact mix depends on access. A user without WoS or Scopus can
still run a defensible SR using OpenAlex + Semantic Scholar, provided
the coverage gaps are disclosed in the methods section (pulled from
`search_metadata.json` via the stats dictionary; never typed in prose).

**Technical tips for search design:**

- **Wildcard multi-word phrases for WoS.** Scopus stems phrases; WoS does
  not. `TS="growth aspiration"` misses plural "aspirations". Always
  write `TS=("growth aspir*" OR ...)`.
- **Merge abstracts during dedup.** Same DOI from Scopus and WoS → keep
  the record with the non-empty abstract. Blindly-first-wins drops data.
- **Second-pass dedup by title+first-author.** DOI-only dedup misses the
  common case where Scopus has a DOI and WoS does not (or vice versa).
  Normalise title, first-author lastname, merge.

### Abstract retrieval cascade

Cascade in order: Crossref → Semantic Scholar (DOI) → Semantic Scholar
(title) → Scopus → ScienceDirect → OpenAlex GROBID.

- **Do NOT use OpenAlex `abstract_inverted_index`.** Often reconstructed
  from GROBID full-text parsing — returns body-text fragments, not
  abstracts. See <https://bmkramer.github.io/SesameOpenScience_site/thought/202411_open_abstracts/>.
- The GROBID TEI XML `<abstract>` element is the acceptable last-resort
  OpenAlex source; still verify length > 60 chars and sense-check.

### PDF retrieval

A four-phase cascade that `enrich_pdfs.py` runs automatically. Each
phase handles a class of item the previous phase can't; nothing is
ever silently dropped.

**Phase 1 — API cascade (`enrich_pdfs.py` default mode).** Works for
most open-access and publisher-TDM-enabled items:

```
publisher TDM API (Elsevier, Wiley)  →  Crossref TDM  →  PMC
  →  OpenAlex Content  →  Unpaywall  →  OpenAlex OA metadata
```

Elsevier and Wiley TDM require `ELSEVIER_API_KEY` and
`WILEY_TDM_TOKEN`. OpenAlex Content is paid ($0.01 per download, gated
on `OPENALEX_API_KEY`).

**Phase 2 — browser cascade for Cloudflare-gated publishers**
(`enrich_pdfs.py --sources browser`). HTTP clients cannot solve the
Cloudflare JS challenge, so for Sage, OUP, Taylor & Francis, Emerald,
and similar CF-gated publishers, a Playwright-driven Chromium opens
visibly. The user passes the Cloudflare challenge once per publisher;
the authenticated session then captures subsequent downloads
automatically. First-time use needs a one-time browser install:
`uvx playwright install chromium` (the setup wizard pre-approves
this command). If the browser cascade regresses, file an issue and
attach the run log (`--log-csv`) so the failure can be reproduced.

> **The agent runs this pass; the user only solves the challenges.**
> The Playwright window opens on the user's screen either way. What the
> agent lacks is a controlling TTY, so a bare invocation from the Bash
> tool exits with a paste-in command rather than hanging on the first
> prompt — pass `--control-file` and the prompts travel through a file
> and the conversation instead. See *Phase 4* below for the handshake.
> Most publishers now ask nothing at all: the script waits for the
> Cloudflare challenge to clear on its own first, and a persistent
> browser profile usually means there is nothing left to solve. For
> genuinely unattended runs (cron, no user present) pass `--no-prompt`
> — it answers every challenge with "skip" and records which publishers
> were bypassed in the run log.

**Phase 3 — Zotero Connector + institutional SFX/OpenURL**
(`enrich_pdfs.py` with Connector handlers). For items the browser
cascade can't reach directly — typically paywalled content accessed via
library proxy — the script launches Zotero Desktop's Connector
extension and routes requests through the institution's SFX/OpenURL
resolver (`scripts/pipelines/fetchers/library_resolver.py`). Requires:
Zotero Desktop running locally, Zotero Connector installed in the
Chromium profile, and the institution's OpenURL base URL configured.

**Phase 4 — diagnose before you exclude. This step is mandatory.**

Never go from "the cascade failed" to "tag it unavailable". Most of what
Phase 1 fails on is *not* unreachable — it is reachable by a route that
has not been tried yet. On a real 244-item run, 76 of 119 apparent
failures were Sage and Academy of Management articles that one browser
pass recovered; the pipeline had reported all 119 as "no fulltext
available".

After **every** enrichment run, run the audit and read the retrieval
report:

```bash
uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/audit_zotero_library.py \
    --group <id>
```

It prints a per-publisher × cause table and, crucially, a count of how
many residuals are *recoverable*:

```
PDF retrieval: 125/244 attached · 110 still missing

  publisher                 n  cause              next step
  Sage                     48  BROWSER_REQUIRED   --sources browser --publisher sage
  Academy of Management    28  BROWSER_REQUIRED   --sources browser --publisher aom
  Wiley                     8  ACCESS_BLOCKED     Flag for ILL — paywall, full text exists
  Springer                 15  UNAVAILABLE        FE6 (no fulltext available)

  86 of the 110 are recoverable — they have not been through every route yet.
```

**Report this table to the user and offer the next step before
proposing any exclusion.** Say how many items the browser pass would
recover — that number, not the raw failure count, is what the user needs
to decide with.

Then act by cause:

| Cause | Meaning | What to do |
|---|---|---|
| `BROWSER_REQUIRED` | A Cloudflare-gated publisher this plugin has a handler for, not yet run | Offer the browser pass. **Not an exclusion.** |
| `ACCESS_BLOCKED` | Paywalled; the full text exists | Offer the ILL list. **Not an exclusion.** |
| `NETWORK_ERROR` | Transport failure | Re-run. **Not an exclusion.** |
| `CORRUPT_DOWNLOAD` | A source served bytes that are not a usable PDF — usually a truncated download | Retry via a *different* source, not the same one. **Not an exclusion.** |
| `UPLOAD_FAILED` | The PDF was fetched but the Zotero attach failed | The file is already in the local cache; re-run `enrich_pdfs.py` and it attaches without re-downloading. **Not an exclusion.** |
| `OUT_OF_SCOPE` | Book chapter, thesis, preprint | FE2 / FE3 — exclude on item type, not on retrieval |
| `UNAVAILABLE` | Every route tried, nothing found | FE6 — the only cause that justifies a full-text-unavailable exclusion |

**Before accepting `UNAVAILABLE`, there is one more route — and it
changes what the item is.** `enrich_pdfs.py --allow-preprints` looks for
a copy on arXiv / SSRN / RePEc. It is off by default because what it
finds is the manuscript *before* peer review: coding a working paper as
the published article misreports what the journal published, and no
later stage can detect the substitution. Offer it explicitly, say that
is what it does, and let the user decide. Every attachment it produces
is tagged `pdf:preprint-version`; `fulltext_code.py` names those items
before coding them, and each coded finding must be checked against the
published article before it reaches a manuscript. A preprint copy is
better than a hole in the review only if the review says which rows rest
on one.

**The hard rule: an item may not be tagged `fulltext:unavailable` until
its cause in the retrieval report is `UNAVAILABLE`.** If you have not run
the audit, you do not know the cause, and you may not tag. The audit
writes the retry sets for you as key files
(`retry.browser[.<publisher>]`, `retry.ill`, `retry.network`,
`true_negative`, `out_of_scope`) — feed them straight to
`--filter-keys-file`; do not assemble key lists by hand.

**Run the browser pass yourself.** Having no controlling terminal is no
longer a reason to hand the user a command to paste — `--control-file`
puts the prompts in a file you poll, while the Chromium window opens on
the user's screen and the user still solves each challenge:

```bash
uv run ${CLAUDE_PLUGIN_ROOT}/scripts/pipelines/enrich_pdfs.py \
    --sources browser --auto-publishers \
    --control-file .claude/audit/browser.json \
    --progress-json .claude/audit/browser-progress.jsonl
```

Start it with `run_in_background: true`. When the file's `state` becomes
`awaiting_user`, relay its `prompt` to the user verbatim and write their
answer back as `{"seq": <seq from the file>, "answer": "..."}` to
`.claude/audit/browser.json.reply`. Echo the `seq` you read — a reply
with a stale `seq` is ignored, which is what stops an old answer from
clearing a challenge nobody looked at. The full procedure is in
`zotero-operations`, step 7 of the canonical workflow.

Long silences are expected — the run asks only when a challenge needs a
human, and most do not. `--progress-json` is how you tell a working run
from a stuck one: one JSON object per line, newest last.

**Never silently drop items** — a paper with no attached PDF after all
phases is a data-quality signal, not a failure to hide.

**Cross-cutting tips** (apply at every phase):

- **Always validate `%PDF` magic bytes** *and* parse-test the PDF
  before caching. Some downloaders save HTML-with-200 or corrupted PDFs.
- **Disable Chromium's built-in PDF viewer** via a `user_data_dir`
  with `plugins.always_open_pdf_externally=true` in Preferences —
  otherwise PDFs open inline and neither `expect_download` nor
  `expect_response` captures the bytes.
- **Pilot the browser phases on a small batch** before a full run —
  Cloudflare challenges and Connector state are session-scoped; if
  something's misconfigured you want to know after 10 items, not 500.

### Screening

- **Temperature=0 always.** The test suite must grep `"temperature": 0`
  in screening scripts.
- **Haiku / Gemini Flash for abstract screening** (fast, cheap, sufficient for
  include/borderline/exclude).
- **Sonnet / Gemini Pro for full-text screening and coding** (needs reasoning
  capacity for structured extraction).
- **Items without abstracts → borderline.** Retain for full-text review;
  never auto-exclude.
- **Append-only logs.** Last-row-wins per Zotero key allows overrides
  without losing history. Abstract becoming available for a previously-
  borderline item does not require editing earlier rows — append a new
  decision.
- **Parallelise with `ThreadPoolExecutor` + `threading.Lock` on the
  CSV log.** Default 8 workers for Haiku / Gemini Flash, 5 for Sonnet / Gemini Pro.
- **Resilient JSON parsing.** Even with "JSON only" system prompts,
  the LLM sometimes emits chain-of-thought before the object. Use
  `llm_helpers.extract_json_from_response()` which walks for the first
  balanced `{...}`. Errored rows write `decision=error` with truncated
  response in `reason`; `--rerun` retries only those.

### Predatory journal flag

Before screening, query a predatory-journal list (Beall's archive at
<https://beallslist.net/> or equivalent) for each journal ISSN. Papers
from listed journals get a `predatory:flag` tag in Zotero. This is a
**warning, not an exclusion** — the author decides during full-text
review whether to keep each flagged paper. Transparent flagging
(not silent removal) is the rule.

### Retraction check

PRISMA quality assessment should catch **retracted papers** in the
included set — citing a retracted paper is a fact-check failure mode.
Run this check **after full-text coding is complete and before
exporting `coded_papers.csv`**, so retractions don't slip into the
manuscript's bibliography.

The mechanism (`mcp__zotero__scite_check_retractions`, the
`retracted:flag` tag convention, "flag — don't silently drop")
lives in `zotero-operations` — see its *Optional: retraction check*
section. The SR-specific twist is **scope** and **timing**:

- **Scope.** Narrow the check to items already tagged
  `fulltext:include` so it runs against papers that matter for the
  synthesis, not the full library.
- **Timing.** Run before `export_coded_includes.py`; the
  adjudicator decides whether to keep retracted items (with a
  prominent discussion note), replace the citation, or exclude.

### Post-screening QA

After every automated full-text screening / coding run (and after
every re-run following prompt changes), launch **three parallel
evaluator agents**, then run a **human adjudication** loop on
whatever they flag. Abstract screening is typically not re-QAed — its
errors surface at Stage 2 anyway — but the pattern works identically
if you want to.

#### The three evaluators

Launch in a **single message, multiple `Agent` tool calls** so they
run in parallel (≈max-of-three latency instead of sum-of-three).
Every evaluator flags items; **no evaluator ever re-decides**.

- **Inclusion validator.** Input: every row decided `include`, with
  the automated reason and the key coding fields. Prompt asks it to
  flag **false positives** — papers that slipped through despite
  failing one of the inclusion criteria. Each flag marks severity
  **HARD** (clearly fails a named criterion) or **SOFT** (borderline,
  defensible). Returns a bulleted list, one per flagged item, with
  `item_key`, severity, and a one-sentence reason.
- **Exclusion validator.** Input: a **stratified sample** across
  exclusion codes — 6–8 items per code. Rationale: each exclusion
  code is a potential source of systematic false negatives, so
  sample across codes rather than uniformly across all exclusions.
  The prompt asks the agent to flag items the screener excluded that
  *should* have been included (false negatives) — HARD if clearly so,
  SOFT if borderline. Also flags a separate category `WRONG_CODE`:
  the exclusion stands but the code is wrong (e.g. exclusion E3 when
  the real reason is E1).
- **Coding-quality validator.** Input: a **random sample** of ≈20 %
  of included papers with **every coding field shown in full** (not
  truncated). The prompt checks each field for: bare labels (should
  be prose), missing citations where theories are named, fabrication
  risk (a claim that sounds too specific to have come from the
  paper), inconsistency across fields, and thin/vague entries. Ends
  with a single-word ship-it verdict and per-paper notes.

The 20 % threshold for coding-quality spot-checks is the plugin's
default. Smaller corpora (< 40 includes) warrant 100 % review;
larger corpora (> 200 includes) can drop to 10 % with a quality
audit built in.

#### Applying QA tags

Evaluators run as `Agent` calls in the main session — they cannot
write to Zotero themselves. The main agent takes each flag the
evaluators return and applies the appropriate `qa-*` tag via
`mcp__zotero__zotero_update_item` (with an `add_tags` parameter) or
`mcp__zotero__zotero_batch_update` (with `item_keys` + `add_tags`) for
the bulk case.

See *Zotero tag and note conventions* above for the full tag
vocabulary — `qa-flag`, `qa-hard`, `qa-soft-include`,
`qa-soft-exclude`, `qa-wrong-code`, and the two post-adjudication
`qa-adjudicated-*` tags.

**Required: each evaluator emits a `decisions.json` alongside the
markdown report.** The markdown is for human review; the JSON is the
machine-actionable input that `apply_qa_adjudications.py` consumes
once the human has reviewed and (optionally) overridden the
evaluator's calls. Schema:

```json
[
  {
    "item_key": "ABCD0001",
    "verdict": "include" | "exclude" | "borderline",
    "reason": "(optional) free-text rationale, written to apply log",
    "flip_fulltext": false
  }
]
```

`flip_fulltext` is `true` only when the adjudication **overrides**
the screener's `fulltext:*` tag — leave `false` for confirmations of
the original decision. The evaluator subagent prompt must instruct
the model to emit one JSON entry per flagged item.

#### Human adjudication loop

The human opens Zotero, filters the collection by `qa-flag`, and for
each flagged item:

1. Reads the attached PDF and the `SLR Coding` child note.
2. Decides: **keep** the automated decision, or **flip** it.
3. Updates the Zotero tag set atomically:
   - Removes the severity tag (`qa-hard` / `qa-soft-*` /
     `qa-wrong-code`) and `qa-flag`; adds `qa-adjudicated-include` or
     `qa-adjudicated-exclude`.
   - **If flipping the decision**, also removes the screener's
     `fulltext:*` tag and adds the opposite one. Tags are the
     authoritative state — a flip that doesn't update the `fulltext:*`
     tag leaves Zotero inconsistent with the adjudication.
   - **If correcting an exclusion code without flipping**, updates
     the coding field in the `SLR Coding` child note and removes
     `qa-wrong-code`.
4. Optionally appends a provenance row to
   `screening/fulltext_screening.csv` (who flipped, when, why). The
   CSV is run-history; the Zotero tag is the current state. Downstream
   scripts (`export_coded_includes.py`, `manuscript_stats.py`,
   `test_systematic_review.py`) read from Zotero, not from the CSV.
5. Writes one line to `screening/qa_review.md` recording the decision
   (format below).

**Bulk-applying many adjudications:** when the human-curated decision
list is non-trivial (more than ~5 items), use the shipped pipeline
script rather than per-item MCP tag calls:

```bash
uv run "${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/apply_qa_adjudications.py" \
    --group <id> --decisions .claude/qa/decisions.json
```

The script consumes `decisions.json` (the schema in *Applying QA
tags* above), routes through `zotero_io.batch_update_tags` —
constructing the full PATCH payload with the right `version` field
per item — and writes an audit log to `output/qa_adjudications_log.csv`.
Replaces the user's earlier ad-hoc adjudication script that hit a
silent-write pyzotero footgun (calling `add_tags()` with a stub item
dict drops the write).

#### `screening/qa_review.md` structure

A single markdown file in the project's `screening/` directory with
two sections.

**Scope clarifications.** Protocol-level decisions the adjudicator
made while working through flags. These apply **going forward** and
propagate back into the screening prompt version for any future
re-run. Format:

> 1. **\<one-line rule\>** — \<paragraph rationale\>. *(YYYY-MM-DD)*

Example: *"Cross-country GEM studies at country-year level are in
scope. Rationale: the GEM cluster is a coherent strand; fragmenting
it weakens synthesis."*

**Adjudication log.** One line per flagged item, in processing order.
Format:

> `{item_key}` **{short citation}** — **{kept DECISION / flipped to
> DECISION [EXCLUSION_CODE]}** — \<one-to-two-sentence rationale\>.
> *(YYYY-MM-DD)*

Group related flips onto one line when the rationale is identical
(e.g. "10 GEM studies — all kept INCLUDE — see scope clarification
1"). Individual contentious flips get their own line.

This file **is** the methods-section evidence for the manuscript's
QA paragraph. Without it, the adjudication is not reproducible.

#### Red flag

You are about to **silently drop a `qa-flag`ed item** — remove the
flag without recording a disposition in the adjudication log. Never.
Every flagged item gets one line in `qa_review.md`, even if the
decision is "kept without change". Silent drops break the
reproducibility invariant that makes the QA step worth the effort.

## Data integrity

These rules supplement the `empirical-integrity` skill with SR-specific
patterns:

- **Auto-extract script constants** into `search_metadata.json`. Never
  import scripts (side effects); parse with
  `re.search(r'CONSTANT\s*=\s*"([^"]+)"', source)`. Keywords, year
  bounds, model names, prompt versions all live in the metadata file.
  `analysis/manuscript_stats.py` then ingests `search_metadata.json` and exposes
  each field under `s['search.*']` or `s['provenance.*']` in the
  manuscript's stats dictionary — the manuscript never reads
  `search_metadata.json` directly.
- **Forbidden methodology literals.** The project's test suite must
  grep the manuscript for hand-typed search dates, model names
  (`claude-haiku`, `claude-sonnet`), keyword strings, year bounds.
  These must use inline expressions from the stats dictionary
  (`s['search.databases']`, `s['provenance.fulltext.model']`, …).
- **PRISMA arithmetic test.** `include + borderline + exclude = total
  screened`; `coded include + exclude = total coded`. Catches missing
  items or pipeline drops.
- **Search integrity gatekeeper.** `search_run.json` records the
  canonical count of unique DOIs from the scripted search. Post-import
  invariant: Zotero DOIs == search DOIs. Abort if extras exist (items
  added outside the pipeline).

## Test suite

See `empirical-integrity` for the overall approach and file layout.
SR-specific invariants live in
`${CLAUDE_PLUGIN_ROOT:-.}/templates/test_systematic_review.py` (copy into
the project's `scripts/`). The file ships 14 active tests:

| Test | What it catches |
|---|---|
| Pipeline artefacts exist and non-empty | Pipeline didn't run |
| `search_run.json` marker matches dedup CSV | Stale or missing integrity gatekeeper |
| `search_metadata.json` has required fields | Export bug |
| No duplicate DOIs in dedup CSV | Dedup gap |
| Abstract log uses allowed decision states | Pipeline emitted an unexpected abstract-stage decision |
| Fulltext log final decisions | Non-final (`error`) decision left at the end of the fulltext log |
| PRISMA arithmetic | Screening funnel inconsistency |
| Coded count == fulltext includes | Export/coding drift |
| `temperature=0` pinned in Claude calls | Reproducibility regression |
| `screening_config` constants match logs | Config changed without a re-run |
| No `decision=error` left in fulltext log | Unresolved screening errors |
| No ghost keys (fulltext log ⊆ Zotero) | Items removed or renamed outside the pipeline |
| **Fulltext tags consistent with CSV log** | Zotero tag state diverges from CSV decisions — tag write-back failed, or an out-of-band CSV edit wasn't mirrored in Zotero |
| **Every `fulltext:include` item has an SLR Coding note** | Include-tag set without a coded note — export script has nothing to read for that paper |

BBT-key uniqueness and `coded_papers.csv` → `references.bib` resolution
live in `test_citations.py` (citation concerns). Manuscript-prose
invariants — forbidden literals, label uniqueness, inline `s['…']`
resolution, figure-file existence — live in
`test_empirical_integrity.py`. Zotero-collection dedup checks are run
via `mcp__zotero__zotero_find_duplicates`, not as a static test.

**Grow the suite with the pipeline.** When you find a new SR-pipeline
regression a static check could catch — a new metadata field that
should round-trip, a new PRISMA edge case, a new Zotero-drift pattern —
add the test to `scripts/test_systematic_review.py` before closing
out the task. The failure becomes the sentinel so the same class of
mistake can't silently return across runs.

## Scope note

This skill targets **social-sciences systematic reviews** (management,
entrepreneurship, IS, organizational behavior). Medical / clinical SLR
instruments — evidence hierarchies (I–VII), RoB 2, ROBINS-I, PRISMA-P
preregistration — are **out of scope** for v0.1. A medical-SLR variant
would need those plugged in; forcing them into social-science reviews
is domain-inappropriate.

## Red flags

- You are about to hardcode an API key in a reusable script (use env vars).
- Temperature is not pinned to 0 in a screening or coding API call.
- An OpenAlex abstract is being used directly without cross-checking
  against Crossref or the GROBID `<abstract>` element.
- A manual count appears in manuscript prose instead of an inline
  expression.
- A downloaded file is assumed to be a PDF without checking `%PDF`
  magic bytes.
- Zotero contains items not in the current search scope (extras from
  prior runs or manual additions).
- A PDF download returned HTTP 200 but the response is HTML (Cloudflare
  challenge page).
- You are adding a non-DOI fallback (title fuzzy match, author-based
  dedup) without first surfacing the DOI-less records to the user and
  asking whether the source data should be fixed instead.
- A predatory-journal flagged paper is being silently excluded instead
  of surfaced to the author for decision.
- You are about to read `~/.config/academic-research/config.toml` via
  `cat`, `head`, `tail`, `grep`, `less`, `more`, `awk`, `sed`, a
  Python script, or any other command. **NEVER read that file.** It
  holds API keys. Pipeline scripts read it via Python's `open()`
  outside your tool layer; you have no legitimate reason to inspect
  it. If debugging feels like it needs a look inside the file, ask
  the user to re-run `/setup` — that's the reset path.
- You are about to write a Bash heredoc or an inline Python script to
  run a pipeline-style task (enumerate a library, compute stats,
  mutate Zotero, fetch abstracts, etc.). **Never improvise.** If a
  shipped script under `scripts/pipelines/` covers the task, invoke
  it. If none does, tell the user which task is missing and propose
  adding a shipped script — do not write a one-off. Improvised
  scripts leak keys through your context and sidestep pre-approved
  permissions.
