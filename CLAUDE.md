# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Deferred development ideas — things consciously not done yet but worth revisiting — live in [BACKLOG.md](BACKLOG.md). Consult it before starting non-trivial work; the current item may already be captured there with context for why it was deferred.

## What this repo is

A Claude Code **plugin** — not an application. It ships skills (prose rule-books), pipeline scripts, and templates for academic-research workflows. End users install via `/plugin marketplace add mronkko/claude-academic-research`. Anything you change here is consumed by downstream Claude Code instances in user projects.

## Common commands

```bash
# Default test run — unit tests only; live tests are deselected by marker.
pytest tests/ -q

# Single test file or test.
pytest tests/unit/test_zotero_io.py -q
pytest tests/unit/test_zotero_io.py::test_attach_pdf_raises_on_failure -q

# Live tests (real network, API keys required — opt in explicitly).
pytest -m live tests/live/
pytest -m live_browser tests/live/test_browser_publishers.py

# Lint (CI blocker).
ruff check scripts tests

# Lint with auto-fix for I001/UP037/F401/F541 etc.
ruff check scripts tests --fix
```

CI (`.github/workflows/ci.yml`) runs `ruff check scripts tests` then `pytest tests -v` on Python 3.11, 3.12, 3.13. Lint is a hard gate — a single error fails the whole matrix.

## Architecture

### Plugin surface (what users consume)

- **`skills/<name>/SKILL.md`** — each has YAML frontmatter (`name`, `description`) + a markdown body. The `description` is what the Claude Code harness matches on to decide whether to load the skill. Every procedural skill in this plugin follows the same shape: "Use when …" + `Trigger phrases: …` + a "Do NOT use for X — use Y instead" delegation rule. Breaking that shape causes the wrong skill to fire.
- **`templates/`** — copied into downstream user projects (`manuscript.qmd`, `manuscript_tables.py`, `manuscript_stats.py`, `test_citations.py`, `test_empirical_integrity.py`, `test_systematic_review.py`, `test_common.py`, `search_config.py`, `screening_config.py`, `sr_claude_md.md`, `manuscript_claude_md.md`). Changes here affect what a fresh project looks like.
- **`.claude-plugin/plugin.json`** — carries the version string. Bump only on user-visible releases, not on lint or CI fixes.

### Pipeline scripts

`scripts/pipelines/` contains the full systematic-review pipeline — one orchestrator script per stage, roughly in dependency order: `search.py` (plus four `search_<db>.py` single-DB wrappers for piloting) → `import_to_zotero.py` → enrichment (`enrich_abstracts.py`, `enrich_pdfs.py`, `enrich_dois.py`) → `abstract_screen.py` → `fulltext_code.py` → `audit_zotero_library.py` → `export_coded_includes.py` → `generate_bib.py`. The three `enrich_*` scripts replaced the pre-v0.3.0 `attach_pdfs.py` / `fetch_*.py` monolith (now under `legacy/`). All of these orchestrators invoke:

- `scripts/pipelines/fetchers/` — per-provider classes implementing `AbstractFetcher` / `PdfFetcher` ABCs in `fetchers/base.py`. Crossref / OpenAlex / ScienceDirect inherit both. `fetchers/browser/` hosts Playwright handlers for Cloudflare-gated publishers and requires `library_resolver.py` for SFX/OpenURL pre-flight.
- `scripts/pipelines/searchers/` — per-database ABC implementations (Scopus, WoS, OpenAlex, Semantic Scholar) with a similar base-class pattern.
- `scripts/pipelines/zotero_io.py` — `ZoteroClient` wrapping `pyzotero`. Every script that touches Zotero routes through it; `update_abstract` auto-retries on HTTP 412 (version conflict) via `tenacity`.
- `scripts/pipelines/http_client.py` — shared `requests.Session` with `urllib3.Retry` + `tenacity` wrappers.
- `scripts/pipelines/legacy/` — the pre-v0.3.0 orchestrators (`attach_pdfs.py`, `fetch_abstracts.py`, `fetch_pdfs_browser.py`, `fetch_pdfs_wiley_tdm.py`) kept as a rollback path. Skills and docs must point at the `enrich_*` orchestrators, not `legacy/`.

### Runtime model users see

- Scripts run via `uv run` with PEP 723 inline dependency declarations (no venv, no `requirements.txt`).
- Secrets live in `~/.config/academic-research/config.toml` (mode 0600) or env vars; env takes precedence.
- A `permissions.deny` rule blocks the Read tool from the config file so API keys never enter a conversation.
- Zotero writes go through the Zotero Web API; reads prefer the local HTTP server at `localhost:23119` (Better BibTeX must be enabled in Zotero desktop).

### Cross-platform notes

The plugin runs on Windows, macOS, and Linux. CI verifies all three
(`.github/workflows/ci.yml` matrix is `ubuntu + windows + macos ×
Python 3.11/3.12/3.13`). A few conventions that keep it that way:

- **Config path**: `Path.home() / ".config" / "academic-research" / "config.toml"` on every OS. The literal string `~/.config/` appears in prose only; never write `open("~/x")` in code (use `Path.home()`).
- **Project-local artefacts**: scripts and skills write run-outputs under `.claude/<scope>/` in the user's project (e.g. `.claude/critic-loop/`, `.claude/audit/`, `.claude/fact-check/`). The setup wizard adds `.claude/` to the project `.gitignore` if one exists.
- **`os.chmod`**: always guard with `if sys.platform != "win32":`. Python's chmod on Windows only toggles the read-only bit; NTFS per-user ACLs already protect paths under `C:\Users\<user>\`.
- **Skill pre-flight and bootstrap helpers**: when a skill needs to probe config / scaffold / deny-rules, create a project-local directory, or copy templates into a project, invoke the cross-platform scripts in `scripts/setup/` (`check_configured.py`, `check_project_scaffold.py FILE...`, `check_deny_rules.py RULE...`, `ensure_dir.py DIR...`, `install_templates.py BASENAME:DEST...`). Do not use POSIX `test -f` / `mkdir -p`, shell `cp` chains, or inline `python -c`. None of those are covered by the wizard's `Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)` allow rule, so they trigger a permission prompt at skill load time; the script paths are covered.
- **Legacy scripts** (`scripts/pipelines/legacy/*.py`) are POSIX-only (one of them reads `/dev/tty`). Users on Windows use the refactored `enrich_*.py` scripts. Do not add new work to `legacy/`.

### Test suite shape

- `tests/conftest.py` inserts both `scripts/` and `scripts/pipelines/` on `sys.path`, so unit tests can `import zotero_io` and `import http_client` directly without the sys.path gymnastics the scripts do at runtime.
- Default run deselects `live` and `live_browser` markers — those require real API keys and are opt-in per `pyproject.toml`.
- Live tests live under `tests/live/` and each publisher / source / API key MUST have a matching live test. The `test_live_coverage.py` guard enforces this at CI time.

## Reference projects

When designing a new skill, pipeline module, or workflow, check these first — both for prior-art ideas and for code that can be lifted or adapted (with attribution):

- **[Imbad0202/academic-research-skills](https://github.com/Imbad0202/academic-research-skills)** — a similar Claude Code plugin targeting academic research. Useful as a sanity check on skill decomposition, description patterns, and scope boundaries. *Reference only*, not a dependency — lifting code requires license/attribution review.
- **[54yyyu/zotero-mcp](https://github.com/54yyyu/zotero-mcp)** — the Zotero MCP server this plugin depends on at runtime. Its source is a good reference when extending our Zotero handling: look here before building a new pyzotero helper or re-implementing a Zotero API call locally.
- **[openags/paper-search-mcp](https://github.com/openags/paper-search-mcp)** — the multi-database paper-search MCP server this plugin depends on at runtime (Scopus, WoS, Google Scholar, Semantic Scholar, arXiv, bioRxiv, medRxiv, PubMed, Crossref, sci-hub). Registered by `scripts/setup/wizard.py`. Its source is the reference when adding a new search provider or extending our `scripts/pipelines/searchers/` with a pattern that already exists upstream.
- **[Dianel555/paper-search-mcp-nodejs](https://github.com/Dianel555/paper-search-mcp-nodejs)** — a Node.js companion to `openags/paper-search-mcp` with broader publisher coverage (adds Wiley, Springer, ScienceDirect, IACR, Web of Science, Scopus on top of the arXiv / bioRxiv / medRxiv / PubMed / Google Scholar / Semantic Scholar / Crossref / sci-hub set). *Reference only* today — not registered by `scripts/setup/wizard.py`. Worth consulting when a paper-search gap the Python server doesn't cover maps to an endpoint this one does, and when considering whether to add it as a second runtime MCP alongside the Python server.
