# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.1.3] — 2026-04-20

### UX polish after first real-pipeline run

- **Audit script writes `.keys` files directly.** After
  `audit_zotero_library.py` runs, `/tmp/zotero_audit.missing_abstract.keys`,
  `.missing_pdf.keys`, and `.empty_stubs.keys` land next to the JSON —
  feedable straight to the next pipeline stage's `--filter-keys-file`
  flag. Eliminates the `jq` extraction step (which triggered a
  permission prompt and invited the `empty_stub` vs `empty_stubs`
  singular/plural typo). The script's "Next steps" output now shows
  the exact command to run for each non-empty category.
- **Browser fetcher announces itself.** `fetch_pdfs_browser.py` prints
  a 20-line banner before launching Chromium: what is about to happen,
  which publishers are queued with counts, what the user may be asked
  to do (solve CF challenge, sign in via SSO). No more surprise
  browser windows.
- **Skill-level narration rule.** `zotero-operations` now instructs
  Claude to announce potentially startling stages to the user *before*
  running them (browser fetches, long attach_pdfs runs, first-run uv
  installs).
- **Canonical workflow prose updated** — the skill's "add missing
  abstracts and PDFs" walkthrough drops the `jq` step and references
  the `.keys` files directly.

## [0.1.2] — 2026-04-20

### Security hardening

- **Canonical scripts replace improvised pipeline code.** When Claude
  was asked to "add missing abstracts and PDFs" it composed a Python
  heredoc that read `config.toml` to extract the API key and run a
  library audit. That approach leaks keys through Claude's tool
  context. The fix ships a real audit script
  (`scripts/pipelines/audit_zotero_library.py`) and hardens the
  `zotero-operations` skill to forbid improvisation.
- **Shared config reader** (`scripts/core/config_loader.py`) — all
  pipeline scripts now have a single canonical path to read
  `~/.config/academic-research/config.toml`. Env vars take precedence;
  `require()` raises a clear error if a required value is missing.
- **Broader `permissions.deny` patterns** — wizard now writes deny
  entries for `cat`/`head`/`tail`/`grep`/`less`/`more`/`awk`/`sed`/
  `od`/`xxd`/`strings`/`bat` against both the absolute and tilde-prefix
  form of the config file. Not exhaustive (Python heredocs still
  slip through), so skill-level red flags are the primary defence.
- **Explicit "never read config" red flag** added to every procedural
  skill (`systematic-review`, `zotero-operations`, `fact-check`,
  `critic-loop`).
- **Explicit "never improvise a pipeline script" red flag** added to
  `zotero-operations` and `systematic-review`.

### New functionality

- `scripts/pipelines/audit_zotero_library.py` — classify a library's
  items into have-PDF / missing-PDF / empty-stub / missing-abstract
  categories. Prints summary, writes JSON. Intended to drive
  `fetch_abstracts.py` and `attach_pdfs.py` via their
  `--filter-keys-file` argument.

### uv + PEP 723 inline dependencies

- All pipeline scripts now declare their runtime deps in a PEP 723
  header. `uv run <script>` auto-installs into an ephemeral venv on
  first run — no more `pip install` before use, no system-Python
  pollution.

### Skill updates

- `zotero-operations` — added a canonical "intent → script" table, a
  step-by-step workflow for "add abstracts and PDFs", and forbids
  directory probing or improvised scripts.
- Systematic-review, fact-check, critic-loop, zotero-operations —
  now each have the two new hard-rule red flags above.

## [0.1.1] — 2026-04-20

### Security fix (breaking UX change)

- **`/setup` now launches a terminal wizard** (`scripts/setup/wizard.py`)
  instead of collecting API keys in chat. The previous design asked
  the user to paste API keys into the Claude chat, which would have
  transmitted them to Anthropic's API as part of the user message.
  The wizard reads keys with `getpass` in the user's terminal — keys
  never enter Claude's context.
- The setup skill now detects TTY and either launches the wizard
  in-process (CLI Claude Code) or instructs the user to open a
  terminal (Desktop / Positron / VSCode / headless).
- Wizard is idempotent; re-run to update or add keys.
- Wizard patches `~/.claude/settings.json` with the plugin's
  permission rules (allow `Bash(... ${CLAUDE_PLUGIN_ROOT}/scripts/**)`,
  deny `Read` on the config file). Backs settings.json up before
  mutating.

## [Unreleased]

### critic-loop extensions (deferred from 2026-04-19 prior-art review)

- **Devil's Advocate** as a 5th parallel critic (forces construction of the
  strongest case *against* the manuscript's position). Revisit after seeing
  4-critic loop performance.
- **Traceability matrix** for iteration 2+ — feed each critic a diff since
  its prior iteration plus its own prior unresolved issues, to verify
  substantive fixes rather than cosmetic rewrites.

### Potential improvements (deferred prior-art)

- **Marker** (GPL-3.0) — LLM-assisted PDF extraction for CID-font garbling.
  Integrate via subprocess CLI only (not import) to preserve MIT licensing.
  Candidate fallback in `scripts/core/pdf_extract.py` when both pdfplumber and
  pypdf fail the quality score.
- **paperscraper** (MIT) — Wiley + Elsevier TDM + bioRxiv + PMC BioC-XML.
  Partial overlap with `scripts/pipelines/attach_pdfs.py`; integration would
  require rewriting the orchestration layer. Defer until we have evidence the
  simplification is worth the churn.
- **grobid-client-python**, **semanticscholar** PyPI, **Europe PMC** — minor
  code-quality wins.
- **`/add-publisher`** scaffold skill — generate `publishers/<name>.py` stub
  from DOI prefix + login-required + CF-required inputs.

## [0.1.0] — TBD

Initial public release. See README for the full feature set.
