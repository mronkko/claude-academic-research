# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
