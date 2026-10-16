# claude-academic-research

Academic research plugins for Claude Code and Antigravity: MCP-grounded citations, empirical
integrity, systematic reviews, Zotero operations, and parallel-critic manuscript
revision.

## Install and Load

### Claude Code

Inside the Claude Code chat (Desktop or CLI):

```
/plugin marketplace add mronkko/claude-academic-research
/plugin install academic-research@mronkko

# (Optional) the editorial-tools plugin — reviewer / AE suggestion
/plugin install editorial-tools@mronkko
```

After install, run `/setup` once to configure API keys and MCP servers.
The setup skill hands you one command to run in a terminal: keys typed
into the chat would reach the model provider and the transcript, so the
wizard asks for them in the terminal with hidden input and stores them
only in a local file. The wizard writes no permission rules; what to
allow is up to you.

### Antigravity

Inside the Antigravity session/CLI (or terminal):

```bash
# Install the main academic-research plugin
agy plugin install "https://github.com/mronkko/claude-academic-research.git"

# (Optional) Install the editorial-tools sub-plugin
agy plugin install "https://github.com/mronkko/claude-academic-research.git/editorial-tools"
```

Works on Windows, macOS, and Linux. Windows users do **not** need WSL or
Git Bash — native `cmd` and PowerShell are supported, and CI verifies
every commit against `windows-latest`.



## What's in the plugin

Twelve user-invocable skills:

| Skill | Mode | Purpose |
|---|---|---|
| `grounded-citations` | rule-book (eager) | Every citation = Zotero BBT key + externalised source consultation; drop claims the source doesn't support. |
| `empirical-integrity` | rule-book (eager) | Every number in prose must come from an inline expression reading `analysis/results/`. |
| `manuscript-revision` | rule-book (eager) | Parallel-critic revision loop is the default revision protocol — delegates to `/critic-loop`. |
| `academic-style` | rule-book (eager) | House-style conventions at drafting time — APA citations, voice, tense, hedging, synthesis-over-enumeration, terminology. |
| `systematic-review` | procedure (explicit) | End-to-end SLR pipeline from search → screening → coding → export. |
| `cluster-screening` | procedure (explicit) | Screening / coding on a GPU cluster or batch scheduler: emit a manifest, run it under SLURM, apply the responses. |
| `zotero-operations` | procedure (explicit) | Import, dedup, enrich abstracts and DOIs, maintain BBT keys. |
| `retrieving-pdfs` | procedure (explicit) | Find and attach PDFs: plan the retrieval with you (what the API cascade, paid OpenAlex, the browser / Connector passes — attended, or as a long unattended run after a short setup with you — and manual download would each get), record the agreed strategy, run it step by step, diagnose what is left. Loaded by `systematic-review` and `zotero-operations`. |
| `fact-check` | procedure (explicit) | Verify citations and quantitative claims against sources. |
| `word-zotero-track-changes` | procedure (explicit) | Edit a co-authored Word manuscript as tracked changes by one author, inserting live Zotero citation fields and preserving co-authors' pending changes. |
| `critic-loop` | procedure (explicit) | Run 4 parallel critics (evidence / method / argument / expert) until no MAJOR issues remain. |
| `setup` | procedure (explicit) | Hands you the terminal configuration wizard (API keys, LLM provider, MCP servers); switches LLM provider. |

Plus one sub-skill, `verifying-citations` — not invoked directly by
users; loaded by `fact-check` and `critic-loop`'s evidence critic to
share one citation-verification rule-book between the two callers.

The separate `editorial-tools` plugin ships one skill,
`suggesting-reviewers`, for journal editors and associate editors.

## Pipeline tools (MCP server)

The plugin bundles a small MCP server, `mcp/server.py`, that exposes the
pipeline steps as tools, so an agent can call them directly rather
than pasting long `uv run` commands for you to approve one by one. **The
skills call these tools by default**: every skill that runs a pipeline
step names the tool (by its bare name — in Claude Code the full name is
`mcp__plugin_academic-research_academic-research__<tool>`) and gives the
shipped script beside it as the *terminal equivalent*. An agent falls
back to that command only when the tools are not available (no `uv`, or
the server not registered on that host), and then says how to enable
them. A few steps have no tool yet — pilot analysis, tag pruning, LLM
provider and model pinning, the cluster round trip, the attended browser
pass, `--replace` — and the skills run those as scripts.

| Tool | Effect | Runs |
|---|---|---|
| `check_setup` | read-only | `scripts/setup/check_configured.py`, `check_database_access.py` |
| `scaffold_project` | writes project files | `scripts/setup/scaffold_project.py` (the template `CLAUDE.md` only with `claude_md: "install"`) |
| `pdf_plan` | read-only | `scripts/pipelines/enrich_pdfs.py --plan-strategy` (the retrieval plan; `start_pdf_plan` adds the resolver lookups as a job) |
| `pdf_strategy_show` | read-only | `scripts/pipelines/pdf_strategy.py show` |
| `pdf_strategy_write` / `pdf_strategy_update` | writes project files | `scripts/pipelines/pdf_strategy.py init` / `update` (`.claude/pdf/strategy.toml`) |
| `pdf_policy_write` | writes project files | `scripts/pipelines/pdf_strategy.py policy` (`PDF_*` in `screening_config.py`) |
| `pdf_consent_show` | read-only | `scripts/pipelines/pdf_consent.py show` (automated-download consent per site) |
| `pdf_consent_set` | writes user config | `scripts/pipelines/pdf_consent.py set` (`[publisher_consent.<site>]`) |
| `enrich_pdfs_plan` | read-only | `scripts/pipelines/enrich_pdfs.py --plan` (the browser pass's publisher queue) |
| `enrich_pdfs_report` | read-only | `scripts/pipelines/enrich_pdfs.py --report` |
| `pdf_manual_next` | writes Zotero, opens browser tabs | `scripts/pipelines/enrich_pdfs.py --manual next` |
| `pdf_manual_collect` | writes Zotero | `scripts/pipelines/enrich_pdfs.py --manual collect` |
| `pdf_manual_assign` | writes Zotero | `scripts/pipelines/enrich_pdfs.py --manual assign` |
| `pdf_manual_ignore` | writes project files | `scripts/pipelines/enrich_pdfs.py --manual ignore` |
| `screening_report` | read-only | `scripts/pipelines/screening_report.py` |
| `audit_library` | read-only | `scripts/pipelines/audit_zotero_library.py` |
| `export_coded_includes` | writes project files | `scripts/pipelines/export_coded_includes.py` |
| `generate_bib` | writes project files | `scripts/pipelines/generate_bib.py` |

Every tool's description starts with its effect class, so you can decide
per tool whether to "always allow" it. Each one runs the script listed
above, unchanged, in your project directory and returns a short summary;
the full output of its last run is kept in `.claude/mcp/logs/`. **Every
script remains runnable from a terminal** exactly as before — the tools
are a front end, not a replacement. Only `uv` is required.

The long steps run as **background jobs**: `start_pdf_plan` (read-only), `start_search`, `start_import`,
`start_enrich_abstracts`, `start_enrich_dois` (writes Zotero),
`start_abstract_screen` and `start_fulltext_code` (cost LLM credits, write
Zotero), and for PDFs `start_enrich_pdfs` (the free API cascade; writes
Zotero), `start_enrich_pdfs_paid` (costs money — the OpenAlex Content API
at $0.01 a PDF; writes Zotero), `start_pdf_unattended_setup` (opens a
browser for you to solve each publisher's challenge once; writes only the
setup record and, if you say "always skip", `[library] no_access`) and
`start_pdf_unattended_run` (the browser pass with no prompts — what needs
a person is deferred to manual download; writes Zotero) each return a job
id at once. One job at a time may write to Zotero, and one at a time may
drive the plugin's browser (the project's single Playwright Chromium
profile) — the server refuses a second. `job_status` reports progress, the
log tail and any question the job is waiting on (relayed to you and
answered with `job_answer`); `cancel_job` interrupts a job so it saves its
progress before stopping; `list_jobs` shows recent ones. A job lives in
`.claude/jobs/<id>/` and keeps running if the host restarts.

Claude Code picks the server up from the plugin automatically. For
Antigravity, and for Codex (untested), the setup wizard offers to
register it.

## Runtime model

- Plugin scripts run with `uv` and use PEP 723 inline dependency declarations
  — no venv, no `requirements.txt`, no `pip install`.
- Secrets live in `~/.config/academic-research/config.toml` (mode 0600) or
  environment variables. Environment variables take precedence.
- Skills never read the config file; pipeline scripts read it
  themselves, so API keys stay out of the conversation.
- Reference management goes through Zotero: the Zotero MCP server for
  ad-hoc reads, the pipeline's `zotero_io.py` (Zotero's local API at
  `localhost:23119`, or the Web API) for bulk work, and Better BibTeX
  for citation keys and `references.bib`.
- The manuscript template renders with Quarto plus a Python that has
  `jupyter`, `pandas` and `tabulate`; see the header of
  `templates/manuscript.qmd`.

## Repo layout

```
.claude-plugin/
  plugin.json              # Claude Code plugin manifest (carries the version)
  marketplace.json         # self-hosted marketplace catalog (both plugins)
plugin.json                # Antigravity plugin manifest
skills/                    # SKILL.md per skill, plus _glossary.md
templates/                 # files copied into user projects (manuscript.qmd,
                            #   apa.csl, configs, project test suites, CLAUDE.md);
                            #   model_catalog.toml is read in place, never copied
scripts/
  core/                    # config loader/writer, LLM provider primitives
  sources/                 # predatory-journal (Beall's list) checks
  pipelines/               # orchestrator scripts (search, enrich_*, abstract_screen,
                            #   fulltext_code, generate_bib, ...; tag_doctypes
                            #   backfills doctype:* tags on an existing library) plus:
    fetchers/               #   per-provider abstract/PDF fetchers (Crossref,
                            #     OpenAlex, ScienceDirect, browser/ for
                            #     Cloudflare-gated publishers)
    searchers/              #   per-database search backends (Scopus, WoS,
                            #     OpenAlex, Semantic Scholar)
  cluster/                 # batch runner + sbatch script for GPU-cluster screening
  setup/                   # configuration wizard + scaffold / pre-flight helpers
  dev/                     # developer tools (live mini-SLR driver, handler probe)
editorial-tools/           # second, independently-versioned plugin (peer-reviewer
                            #   suggestion skill) — see Install and Load above
mcp/                       # stdlib-only MCP server exposing pipeline scripts as tools
tests/unit/                # hermetic pytest suite (the default run)
tests/live/                # opt-in live tests against real APIs (pytest -m live ...)
.github/workflows/ci.yml   # ruff + pytest on Linux, macOS, Windows
```

## License

MIT. See [LICENSE](LICENSE).
