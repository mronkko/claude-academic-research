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

Ten user-invocable skills:

| Skill | Mode | Purpose |
|---|---|---|
| `grounded-citations` | rule-book (eager) | Every citation = Zotero BBT key + externalised source consultation; drop claims the source doesn't support. |
| `empirical-integrity` | rule-book (eager) | Every number in prose must come from an inline expression reading `analysis/results/`. |
| `manuscript-revision` | rule-book (eager) | Parallel-critic revision loop is the default revision protocol — delegates to `/critic-loop`. |
| `academic-style` | rule-book (eager) | House-style conventions at drafting time — APA citations, voice, tense, hedging, synthesis-over-enumeration, terminology. |
| `systematic-review` | procedure (explicit) | End-to-end SLR pipeline from search → screening → coding → export. |
| `cluster-screening` | procedure (explicit) | Screening / coding on a GPU cluster or batch scheduler: emit a manifest, run it under SLURM, apply the responses. |
| `zotero-operations` | procedure (explicit) | Import, dedup, enrich, attach PDFs, maintain BBT keys. |
| `fact-check` | procedure (explicit) | Verify citations and quantitative claims against sources. |
| `critic-loop` | procedure (explicit) | Run 4 parallel critics (evidence / method / argument / expert) until no MAJOR issues remain. |
| `setup` | procedure (explicit) | Hands you the terminal configuration wizard (API keys, LLM provider, MCP servers); switches LLM provider. |

Plus one sub-skill, `verifying-citations` — not invoked directly by
users; loaded by `fact-check` and `critic-loop`'s evidence critic to
share one citation-verification rule-book between the two callers.

The separate `editorial-tools` plugin ships one skill,
`suggesting-reviewers`, for journal editors and associate editors.

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
                            #   fulltext_code, generate_bib, ...) plus:
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
tests/unit/                # hermetic pytest suite (the default run)
tests/live/                # opt-in live tests against real APIs (pytest -m live ...)
.github/workflows/ci.yml   # ruff + pytest on Linux, macOS, Windows
```

## License

MIT. See [LICENSE](LICENSE).
