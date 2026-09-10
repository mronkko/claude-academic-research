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
```

After install, run `/setup` once to configure API keys, MCP servers, and
permission rules. The wizard is chat-driven — no terminal required.

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

Nine user-invocable skills:

| Skill | Mode | Purpose |
|---|---|---|
| `grounded-citations` | rule-book (eager) | Every citation = Zotero BBT key + externalised source consultation; drop claims the source doesn't support. |
| `empirical-integrity` | rule-book (eager) | Every number in prose must come from an inline expression reading `analysis/results/`. |
| `manuscript-revision` | rule-book (eager) | Parallel-critic revision loop is the default revision protocol — delegates to `/critic-loop`. |
| `academic-style` | rule-book (eager) | House-style conventions at drafting time — APA citations, voice, tense, hedging, synthesis-over-enumeration, terminology. |
| `systematic-review` | procedure (explicit) | End-to-end SLR pipeline from search → screening → coding → export. |
| `zotero-operations` | procedure (explicit) | Import, dedup, enrich, attach PDFs, maintain BBT keys. |
| `dblp-bibformat` | procedure (explicit) | Normalise `.bib` entries to canonical DBLP BibTeX — DBLP keys, DBLP field set, fetched from dblp.org. |
| `fact-check` | procedure (explicit) | Verify citations and quantitative claims against sources. |
| `critic-loop` | procedure (explicit) | Run 4 parallel critics (evidence / method / argument / expert) until no MAJOR issues remain. |
| `setup` | procedure (explicit) | Chat-driven configuration wizard for first-time install. |

Plus one sub-skill, `verifying-citations` — not invoked directly by
users; loaded by `fact-check` and `critic-loop`'s evidence critic to
share one citation-verification rule-book between the two callers.

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
  core/                    # config loader/writer, llm provider primitives
  sources/                 # predatory-journal (Beall's list) checks
  pipelines/               # orchestrator scripts (search, enrich_*, abstract_screen,
                            #   fulltext_code, generate_bib, ...) plus:
    fetchers/               #   per-provider abstract/PDF fetchers (Crossref,
                            #     OpenAlex, ScienceDirect, browser/ for
                            #     Cloudflare-gated publishers)
    searchers/              #   per-database search backends (Scopus, WoS,
                            #     OpenAlex, Semantic Scholar)
  setup/                   # first-run configuration wizard + scaffold helpers
editorial-tools/           # second, independently-versioned plugin (peer-reviewer
                            #   suggestion skill) — see Install and Load above
tests/unit/                # pytest + responses mocks
.github/workflows/ci.yml   # pytest + ruff on push/PR
```

## License

MIT. See [LICENSE](LICENSE).
