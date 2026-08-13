# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

Deferred development ideas — things consciously not done yet but worth revisiting — live in [BACKLOG.md](BACKLOG.md). Consult it before starting non-trivial work; the current item may already be captured there with context for why it was deferred.

## What this repo is

An academic research **plugin** for Claude Code and Antigravity — not an application. It ships skills (prose rule-books), pipeline scripts, and templates for academic-research workflows. Claude Code users install via `/plugin marketplace add mronkko/claude-academic-research`, while Antigravity users install via `agy plugin install <url>`. Anything you change here is consumed by downstream agentic instances in user projects.

**The repo is a marketplace hosting more than one plugin.** `.claude-plugin/marketplace.json` lists them. The main plugin, `academic-research`, is sourced at the repo root (`./`) and is what most of this CLAUDE.md describes. A second, smaller plugin, `editorial-tools`, lives under `editorial-tools/` with its own `.claude-plugin/plugin.json` and `skills/` — it ships the `suggesting-reviewers` skill (peer-reviewer suggestion for journal editors/AEs, with a bundled ORM editorial-board roster). The two are independent installs: the root plugin only scans `./skills/`, so `editorial-tools/` does not leak into it, and users who install only `academic-research` never load the editorial skill. Do not assume one-plugin-per-repo.

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

# Set up / refresh the dev environment.
uv sync --group dev
```

Run tests via `.venv/bin/pytest` or `uv run pytest`, never a bare `pytest` on `PATH` — see BACKLOG S11 for why.

CI (`.github/workflows/ci.yml`) runs `ruff check scripts tests` then `pytest tests -v` on Python 3.11, 3.12, 3.13. Lint is a hard gate — a single error fails the whole matrix.

**Test dependencies live in exactly one place:** `[dependency-groups] dev` in `pyproject.toml`. CI installs it with `pip install --group dev` (PEP 735). Do not add a hand-written package list to `ci.yml` — that second source of truth already drifted once and left `uv sync` unable to collect 14 test modules while CI stayed green. `tests/unit/test_ci_dependencies.py` guards this.

## Parallel sessions

Multiple agent instances work this repo concurrently. Start any non-trivial task in a **git worktree**, never in a working tree another instance is already editing — two agents in one checkout clobber each other's edits and run tests against half-applied changes. Git enforces the useful half of this itself: the same branch cannot be checked out in two worktrees.

After creating a worktree, run `uv sync --group dev` before anything else. `uv.lock` and `.venv/` are both gitignored, so a fresh worktree has no environment and every test fails until you sync. `.claude/` is gitignored too, which means project-local permission settings do not follow a worktree either — symlink `.claude/settings.local.json` from the primary checkout rather than copying it, so approvals stay in one place.

The default test run is hermetic — `addopts` in `pyproject.toml` deselects the `live` and `live_browser` markers — so `pytest tests/ -q` and `ruff check scripts tests` are safe to run from any number of worktrees at once.

**Live work is single-lane.** `pytest -m live`, `pytest -m live_browser`, anything under `scripts/pipelines/`, and the `/setup` wizard all contend for resources that exist exactly once on the machine: one Zotero desktop holding `localhost:23119` and one Web API library (concurrent writers interleave badly, and the HTTP 412 retry in `zotero_io.py` masks rather than resolves it), one shared API quota per publisher key, one Playwright Chromium profile, and one `~/.config/academic-research/config.toml`. Confirm no other instance holds that lane before starting.

High-collision files across branches are this file, `BACKLOG.md`, and `.claude-plugin/plugin.json`. Since the version string moves only on user-visible releases, bump it once at merge time in a single branch — bumping it per-branch guarantees a conflict on that line in every subsequent merge.

### Handing off to a new session

**Whenever you suggest continuing in a fresh session, end your message with a ready-to-paste prompt that starts it.** Not a description of what the next session should do — the literal text to paste, in a fenced block, written in second person to that session.

The reason is that a new session begins with nothing: no conversation, no working memory of which of four plausible branches is live, no idea that a decision was already argued and settled. Left to reconstruct that from the repo alone it will re-read files you already read, re-derive conclusions you already reached, and quite possibly re-open a question you closed. The handoff prompt is what stops that, and writing it is the outgoing session's job because only the outgoing session knows what was in its head.

A usable prompt names, at minimum: the branch to work on and how it relates to `main`; where the detailed plan lives (a path under `~/.claude/plans/`, or the relevant `BACKLOG.md` entry); the specific next task, not the whole remaining backlog; the verification command that should pass before and after; and any decision already taken that must not be re-litigated. If a guard test is expected to fail as part of the work, say so — otherwise the next session will treat a deliberate red test as a bug it introduced.

Two habits that make these prompts cheap to write: keep a `## STATE` section at the end of the plan file, updated as work lands, so the prompt can point at it rather than repeat it; and put the *why* in commit messages rather than in chat, since commits are the one artefact a cold session reliably reads.

## Architecture

### Plugin surface (what users consume)

- **`skills/<name>/SKILL.md`** — each has YAML frontmatter (`name`, `description`) + a markdown body. The `description` is what the Claude Code harness matches on to decide whether to load the skill. Every procedural skill in this plugin follows the same shape: "Use when …" + `Trigger phrases: …` + a "Do NOT use for X — use Y instead" delegation rule. Breaking that shape causes the wrong skill to fire. Description bodies are kept under ~500 chars and contain no workflow summary — workflow summaries cause Claude to follow the description instead of reading the skill body. CSO doctrine is in `superpowers:writing-skills`.
- **`REQUIRED SUB-SKILL: <name>` contract.** When a skill body or per-subagent prompt names a sub-skill that way, the receiver is expected to **load the named skill via the `Skill` tool before proceeding** — never to inline the sub-skill's content into the prompt verbatim. This applies symmetrically: a main agent dispatching N parallel subagents (e.g. fact-check's per-citation `Agent` calls) tells each one which sub-skill to load; each subagent then loads it independently. The caller does *not* paste the sub-skill body into the prompt — that would re-introduce the duplication the sub-skill extraction was meant to eliminate, and force every prompt rewrite to ripple across callers. Current sub-skills used this way: `verifying-citations` (loaded by `fact-check` and by `critic-loop`'s evidence critic), `superpowers:dispatching-parallel-agents` (loaded by `fact-check` and `critic-loop` for the parallel-Agent dispatch pattern).
- **`templates/`** — copied into downstream user projects (`manuscript.qmd`, `manuscript_tables.py`, `manuscript_stats.py`, `test_citations.py`, `test_empirical_integrity.py`, `test_systematic_review.py`, `test_common.py`, `search_config.py`, `screening_config.py`, `sr_claude_md.md`, `manuscript_claude_md.md`). Changes here affect what a fresh project looks like.
- **`.claude-plugin/plugin.json`** — carries the version string. Bump only on user-visible releases, not on lint or CI fixes.

### Pipeline scripts

`scripts/pipelines/` contains the full systematic-review pipeline — one orchestrator script per stage, roughly in dependency order: `search.py` (plus four `search_<db>.py` single-DB wrappers for piloting) → `import_to_zotero.py` → enrichment (`enrich_abstracts.py`, `enrich_pdfs.py`, `enrich_dois.py`) → `abstract_screen.py` → `fulltext_code.py` → `audit_zotero_library.py` → `export_coded_includes.py` → `generate_bib.py`. The three `enrich_*` scripts replaced the pre-v0.3.0 `attach_pdfs.py` / `fetch_*.py` monolith (removed in v0.6.0). All of these orchestrators invoke:

- `scripts/pipelines/fetchers/` — per-provider classes implementing `AbstractFetcher` / `PdfFetcher` ABCs in `fetchers/base.py`. Crossref / OpenAlex / ScienceDirect inherit both. `fetchers/browser/` hosts Playwright handlers for Cloudflare-gated publishers and requires `library_resolver.py` for SFX/OpenURL pre-flight.
- `scripts/pipelines/searchers/` — per-database ABC implementations (Scopus, WoS, OpenAlex, Semantic Scholar) with a similar base-class pattern.
- `scripts/pipelines/zotero_io.py` — `ZoteroClient` wrapping `pyzotero`. Every script that touches Zotero routes through it; `update_abstract` auto-retries on HTTP 412 (version conflict) via `tenacity`.
- `scripts/pipelines/http_client.py` — shared `requests.Session` with `urllib3.Retry` + `tenacity` wrappers. **Every outbound HTTP call in `scripts/pipelines/` goes through a session from `build_session()`** — either the `get_json` / `get_bytes` helpers or `session.<verb>(...)` directly. A bare `requests.get` has no retry adapter, so it dies on the first transient 429/5xx and invites a hand-rolled `sleep`-and-retry loop; `searchers/semantic_scholar.py` had exactly that, an unbounded `while True` on 429. `tests/unit/test_searcher_backoff.py` fails the build on a new bare call (AST-based, so prose mentioning `requests.patch()` is fine). Two consequences: searchers reach the session via `SearchContext.http()`, and any `uv run` script that touches `http_client` must declare `urllib3` **and** `tenacity` in its PEP 723 block. `scripts/setup/` is exempt and must stay stdlib-only — see the cross-platform note below.
- `scripts/pipelines/doi_utils.py` — the one DOI normaliser. Stdlib-only, because `fetchers/doi_resolver.py` sits below the orchestrators and `enrich_dois.py` has no `zotero-mcp-server` in its PEP 723 block. Exposes a **strict** form (`normalize_doi` / `doi_key`, returns nothing for a malformed DOI — right for dedup identity) and a **lenient** one (`strip_doi_prefixes` / `doi_cache_key` — right for cache keys and for the `--fix-malformed` repair path). The split is load-bearing; the module docstring says why.
- `scripts/pipelines/screening_common.py` — config-module loading, stage-tag matching, CSV decision reading, and `--csv-backfill`, shared by `abstract_screen.py` / `fulltext_code.py` (and the config loader by `search.py`). Each orchestrator keeps a thin private wrapper binding its own stage constants.
- `scripts/core/models.py` — model defaults and `--model` aliases (`haiku`, `sonnet`, …) for the screening stages. `templates/screening_config.py` cannot import it (it is copied into user projects), so `tests/unit/test_model_defaults.py` keeps the literals equal.

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
- **Project-local artefacts**: scripts and skills write run-outputs under `.claude/<scope>/` for transient internals (e.g. `.claude/audit/`) and under top-level visible directories for outputs the user is expected to find and review (e.g. `critic-reviews/` for critic-loop iteration reports, `fact-check-reports/` for fact-check audits). The setup wizard adds `.claude/` to the project `.gitignore` if one exists; visible review directories like `critic-reviews/` and `fact-check-reports/` are deliberately *not* gitignored — users may want to commit them as part of their manuscript history.
- **`os.chmod`**: always guard with `if sys.platform != "win32":`. Python's chmod on Windows only toggles the read-only bit; NTFS per-user ACLs already protect paths under `C:\Users\<user>\`.
- **Skill pre-flight and bootstrap helpers**: when a skill needs to probe config / scaffold / deny-rules / database access, create a project-local directory, or copy templates into a project, invoke the cross-platform scripts in `scripts/setup/` (`check_configured.py`, `check_project_scaffold.py FILE...`, `check_deny_rules.py RULE...`, `check_database_access.py`, `ensure_dir.py DIR...`, `install_templates.py BASENAME:DEST...`). Do not use POSIX `test -f` / `mkdir -p`, shell `cp` chains, or inline `python -c`. None of those are covered by the wizard's `Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)` allow rule, so they trigger a permission prompt at skill load time; the script paths are covered. `check_database_access.py` in particular is the out-of-process way to inspect `~/.config/academic-research/config.toml` — the Read tool is denied on that path to protect keys, but a subprocess script that emits only yes/no status is fine.

### Test suite shape

- `tests/conftest.py` inserts both `scripts/` and `scripts/pipelines/` on `sys.path`, so unit tests can `import zotero_io` and `import http_client` directly without the sys.path gymnastics the scripts do at runtime.
- Default run deselects `live` and `live_browser` markers — those require real API keys and are opt-in per `pyproject.toml`.
- Live tests live under `tests/live/` and each publisher / source / API key MUST have a matching live test. The `test_live_coverage.py` guard enforces this at CI time.

## Real-session logs (`logs/`)

`logs/` holds JSONL session transcripts copied from `~/.claude/projects/<encoded-cwd>/` — full per-session captures of real downstream usage of this plugin (every tool call and every raw tool result, not just the visible chat). They surface bugs, missed warnings, and friction points the user hit while running the plugin end-to-end. They are gitignored — keep them local, never commit.

Use them as primary input when designing fixes: grep for warnings the pipeline silently swallowed (e.g. publisher API entitlement messages), retry storms, permission prompts, or improvised inline scripts. The condensed text exports a user might paste into chat are lossy; the JSONL is authoritative.

## Reference projects

When designing a new skill, pipeline module, or workflow, check these first — both for prior-art ideas and for code that can be lifted or adapted (with attribution):

- **[Imbad0202/academic-research-skills](https://github.com/Imbad0202/academic-research-skills)** — a similar Claude Code plugin targeting academic research. Useful as a sanity check on skill decomposition, description patterns, and scope boundaries. *Reference only*, not a dependency — lifting code requires license/attribution review.
- **[mronkko/zotero-mcp](https://github.com/mronkko/zotero-mcp)** — our fork of [54yyyu/zotero-mcp](https://github.com/54yyyu/zotero-mcp), the Zotero MCP server this plugin depends on at runtime and now the fork the wizard's `homepage` points at. `origin/main` must be kept current with `upstream/main` (fast-forward only — see the fork's own commit history for the merge/rebase discipline); a stale fork means the setup wizard's version-floor checks and PyPI install commands drift from what `origin/main` actually contains. Its source is a good reference when extending our Zotero handling: look here before building a new pyzotero helper or re-implementing a Zotero API call locally. `tests/unit/test_zotero_mcp_sync.py` guards the seam between this package's live tool registry and everything in this repo that names an `mcp__zotero__*` tool or a `zotero-cli` subcommand — see that file's docstring before changing either side.

  **Importing from it is not free.** Through 0.9.1, `zotero_mcp/__init__.py` imports the MCP server eagerly, so `from zotero_mcp.schema import valid_fields` costs ~1.73 s and ~1480 modules (FastMCP, pydantic, pyzotero, …) for what is an 8 µs stdlib-only lookup — and forces the whole server dependency tree into the PEP 723 block of any `uv run` script that wants it. Only `import_to_zotero.py` pays this today, for `valid_fields`. Upstream PR [54yyyu/zotero-mcp#445](https://github.com/54yyyu/zotero-mcp/pull/445) makes `mcp` lazy and adds a stdlib-only `zotero_mcp.identifiers.normalize_doi`; until it merges *and* ships to PyPI, prefer a local stdlib helper over a new `zotero_mcp` import in pipeline code, and never import a private symbol (`zotero_mcp.tools._helpers._*`) — that seam has no stability guarantee.
- **[openags/paper-search-mcp](https://github.com/openags/paper-search-mcp)** — the multi-database paper-search MCP server this plugin depends on at runtime (Scopus, WoS, Google Scholar, Semantic Scholar, arXiv, bioRxiv, medRxiv, PubMed, Crossref, sci-hub). Registered by `scripts/setup/wizard.py`. Its source is the reference when adding a new search provider or extending our `scripts/pipelines/searchers/` with a pattern that already exists upstream.
- **[Dianel555/paper-search-mcp-nodejs](https://github.com/Dianel555/paper-search-mcp-nodejs)** — a Node.js companion to `openags/paper-search-mcp` with broader publisher coverage (adds Wiley, Springer, ScienceDirect, IACR, Web of Science, Scopus on top of the arXiv / bioRxiv / medRxiv / PubMed / Google Scholar / Semantic Scholar / Crossref / sci-hub set). *Reference only* today — not registered by `scripts/setup/wizard.py`. Worth consulting when a paper-search gap the Python server doesn't cover maps to an endpoint this one does, and when considering whether to add it as a second runtime MCP alongside the Python server.
