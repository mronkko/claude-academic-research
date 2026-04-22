# claude-academic-research

Claude Code plugin for academic research: MCP-grounded citations, empirical
integrity, systematic reviews, Zotero operations, and parallel-critic manuscript
revision.

## Install

Inside the Claude Code chat (Desktop or CLI):

```
/plugin marketplace add mronkko/claude-academic-research
/plugin install academic-research@mronkko
```

After install, run `/setup` once to configure API keys, MCP servers, and
permission rules. The wizard is chat-driven — no terminal required.

## What's in the plugin

Eight skills:

| Skill | Mode | Purpose |
|---|---|---|
| `grounded-citations` | rule-book (eager) | Every citation = Zotero BBT key + externalised source consultation; drop claims the source doesn't support. |
| `empirical-integrity` | rule-book (eager) | Every number in prose must come from an inline expression reading `analysis/results/`. |
| `manuscript-revision` | rule-book (eager) | Parallel-critic revision loop is the default revision protocol — delegates to `/critic-loop`. |
| `academic-style` | rule-book (eager) | House-style conventions at drafting time — APA citations, voice, tense, hedging, synthesis-over-enumeration, terminology. |
| `systematic-review` | procedure (explicit) | End-to-end SLR pipeline from search → screening → coding → export. |
| `zotero-operations` | procedure (explicit) | Import, dedup, enrich, attach PDFs, maintain BBT keys. |
| `fact-check` | procedure (explicit) | Verify citations and quantitative claims against sources. |
| `critic-loop` | procedure (explicit) | Run 4 parallel critics (evidence / method / argument / expert) until no MAJOR issues remain. |
| `setup` | procedure (explicit) | Chat-driven configuration wizard for first-time install. |

## Runtime model

- Plugin scripts run with `uv` and use PEP 723 inline dependency declarations
  — no venv, no `requirements.txt`, no `pip install`.
- Secrets live in `~/.config/academic-research/config.toml` (mode 0600) or
  environment variables. Environment variables take precedence.
- A `permissions.deny` rule blocks Claude's Read tool from accessing the
  config file, so API keys never enter a conversation context.
- Reference management goes through Zotero via the Better BibTeX local
  JSON-RPC endpoint (`localhost:23119`).

## Repo layout

```
.claude-plugin/
  plugin.json              # plugin manifest
  marketplace.json         # self-hosted marketplace catalog
skills/                    # SKILL.md per skill
scripts/
  core/                    # llm, http, pdf, zotero primitives
  sources/                 # abstract/metadata sources (Crossref, Semantic Scholar, Scopus, ...)
  publishers/              # per-publisher PDF retrieval (Wiley, Elsevier, ...)
  pipelines/               # enrich_pdfs, enrich_abstracts, generate_bib, search, ...
  setup/                   # first-run configuration helpers
tests/unit/                # pytest + responses mocks
.github/workflows/ci.yml   # pytest + ruff on push/PR
```

## License

MIT. See [LICENSE](LICENSE).
