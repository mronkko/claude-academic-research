---
name: setup
description: Use when the user invokes `/setup` (or asks to "configure setup", "run setup wizard", "configure the academic-research project/plugin"), asks to add or rotate API keys (Zotero, Elsevier, WoS, Semantic Scholar, Wiley TDM, OpenAlex, Gemini, OpenAI), switch LLM provider, register MCP servers, or patch permission rules. Also fires when another academic-research skill (zotero-operations, systematic-review, fact-check, critic-loop) reports `NOT CONFIGURED` on its pre-flight check.
---

# setup

> **Quick definitions** (T4-5; full glossary at [skills/_glossary.md](../_glossary.md)):
> - A **plugin** is the code bundle this setup is configuring — a
>   downloadable package shipped with skills, pipeline scripts, and
>   templates that the agentic environment uses for academic-research work.
> - A **skill** is a prose rule-book the agent loads when your request
>   matches its trigger phrases. Skills tell the agent *how* to approach
>   a task; they don't run code on their own. `setup`, `zotero-
>   operations`, `systematic-review`, etc. are all skills in this
>   plugin.
> - An **MCP server** is a small helper program the agent talks to in
>   the background. Example: the **Zotero MCP server** lets the agent
>   read and update your Zotero library directly. The wizard checks
>   five MCP servers (Zotero, Scopus, Semantic Scholar, OpenAlex,
>   paper-search) and offers to register the missing ones.

Setup runs as a terminal wizard the **user** executes. The agent's role is
only to give them the command and confirm when they are done. Do not
run any tool calls — no Bash, no Read, no probes. All the information
needed is already known:

- **Wizard path:** `${CLAUDE_PLUGIN_ROOT}/scripts/setup/wizard.py` (or project-relative `./scripts/setup/wizard.py` if not running inside Claude Code or Antigravity)
  — The active plugin version's absolute path (or project-relative fallback if `${CLAUDE_PLUGIN_ROOT}` is not defined) is used, so the user has a concrete path to run.
- **Config written to:** `~/.config/academic-research/config.toml` (mode 0600).
- **Settings patched:** `~/.claude/settings.json` (backed up as `.bak-wizard`, bypassed silently if not running under Claude).
- **Antigravity MCP config:** if Antigravity (`agy`) is detected, the same MCP servers are also registered into `~/.gemini/config/mcp_config.json` (backed up as `.bak-wizard`). Permission rules are not patched for Antigravity — only Claude Code's `~/.claude/settings.json` is.
- **Wizard is idempotent:** re-running updates or adds keys without
  clobbering existing ones.

## Procedure

**CRITICAL:** never ask the user to paste API keys into the chat. Any text typed into the chat is transmitted to the model provider. The wizard exists so keys stay local.

Paste the following message to the user (no tool calls needed — just
text):

> I'll hand you the setup wizard. It runs in your terminal. It first
> asks **which LLM provider** should run the screening pipelines —
> Anthropic, Google, OpenAI, OpenRouter, your institution's own
> OpenAI-compatible gateway, or a local server (Ollama or LM Studio,
> which need no API key at all) — and then only asks for that
> provider's credential rather than all of them. It prompts for
> each API key with hidden input (keystrokes don't appear), then checks
> five MCP (Model Context Protocol) servers and offers to register any
> that are missing: **Zotero** (required — every citation skill uses
> it), at least one of **Scopus / Semantic Scholar / OpenAlex**
> (required for literature search to work), and **paper-search**
> (optional — for ArXiv / PubMed PDF retrieval). It then writes your
> config file locally. **Your keys never pass through the chat.**
>
> Run the setup wizard in your terminal. If the `${CLAUDE_PLUGIN_ROOT}` environment variable is defined:
>
> ```
> python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/wizard.py
> ```
>
> Otherwise, run the project-relative command from the root of your workspace:
>
> ```
> python3 ./scripts/setup/wizard.py
> ```
>
> **How to open a terminal** if you are not already in one:
> - **macOS:** ⌘-Space → type *Terminal* → Enter.
> - **Windows:** Windows key → type *PowerShell* → Enter. If `python3`
>   is not recognised, try `python` instead.
> - **Linux:** Ctrl-Alt-T (or your distro's terminal app).
>
> Already running the agent in a terminal? Either open a new tab and run
> it there, or temporarily exit this session, run the wizard, and then resume this conversation.
>
> When the wizard prints "Setup complete", return here and say "done"
> (or similar). I'll confirm and we'll continue.

After the user says they finished the wizard, respond with a short
confirmation ("Setup done. Ready for the next task.") and let the next
conversational turn drive the work. Do not run a verification Bash
call — if something went wrong with the wizard, the user's next
invocation of `zotero-operations` / `systematic-review` / etc. will
hit its own pre-flight check and bounce here again.

## If the wizard reports errors

The wizard prints to stdout. If the user pastes output showing a
problem:

- **Python missing**: tell them to install Python 3.11+ — macOS can use
  Homebrew (`brew install python`), Windows can use python.org's
  installer (check "Add Python to PATH"), Linux uses the distro's
  package manager.
- **Tkinter not required** — the wizard is terminal-only. Any Python
  3.11+ install works.
- **Permission denied writing config**: user's home directory has
  unusual permissions. Unlikely on a single-user machine.
- **Can't parse existing settings.json**: the file is malformed. The
  wizard backs up to `.bak-wizard` before touching; restore from
  there, fix manually, or delete and re-run.
- **MCP register fails with "command not found"**: the underlying MCP
  binary is not installed. The wizard prints the project's homepage and
  the exact install command (`uv tool install
  "zotero-mcp-server[scite,semantic]"` — the extras add Scite
  retraction checks and semantic search; `uv tool install scopus-mcp`,
  or "requires Node.js + npm" for the
  npx-based servers). Install it, then re-run the wizard — it's
  idempotent and picks up where it left off.
- **`zotero-cli` missing or `zotero-mcp` found but not `zotero-cli`**:
  installing `zotero-mcp-server` also puts the standalone `zotero-cli`
  on PATH — no separate step. If the wizard's summary shows `zotero-mcp`
  present but `zotero-cli` absent, the stale PyPI package `zotero-mcp`
  (0.1.6, pre-dates the CLI) is shadowing it: `uv tool uninstall
  zotero-mcp` then reinstall `zotero-mcp-server[scite,semantic]` as
  above. `zotero-cli` is optional — every skill degrades to MCP tools
  and `zotero_io.py` without it — so this never blocks setup.
- **Wizard exits with code 4**: Zotero MCP is not connected. No
  academic-research skill works without it. The wizard's summary lists
  the install and registration commands; run them and re-run the
  wizard.

## Switching LLM provider (the one path that is not a wizard hand-off)

"Switch me to OpenAI", "use a local model for screening", "which model
provider am I on?" — these do not involve a key, so they do not need
the wizard. Two scripts cover it, and they are the **only** Bash calls
this skill may make:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_llm_provider.py
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/set_llm_provider.py <name>
```

`<name>` is one of `anthropic`, `google`, `openai`, `openrouter`,
`gateway`, `ollama`, `lmstudio`. The check script prints the provider,
whether one was ever chosen, and whether its credential is present —
never a key value. Do NOT try to read
`~/.config/academic-research/config.toml` yourself; a permission rule
denies it precisely so keys cannot reach a transcript.

`gateway` is the only provider the plugin ships no address for, so it
needs **two** answers — an endpoint URL as well as a key — and it is the
only provider with **no environment variable**. Both live in
`config.toml` under `[gateway]`, because every other provider's variable
is an ecosystem convention its own SDK reads, while any name invented
for a gateway would just collide with whatever the user already exports.
Until `[gateway] base_url` is set, every check reports `UNREACHABLE` and
says so.

A user who would rather keep the key out of the file can point at a
variable they already have, by name:

```toml
[gateway]
base_url = "https://llm.example.edu/api"
api_key_env = "MY_EXISTING_LLM_KEY"
test_model = "org/model-id"   # only used by `pytest -m live`
```

**Gateways often load a model on first request.** Asking for one that
is not resident gets `503 Model not available yet, try again in a few
minutes` — retryable, not broken. Pin stages to models the gateway
keeps warm, and point `test_model` at one too, or the live suite pays a
cold start it did not need.

**Three settings can point at a self-hosted model, and they are not
interchangeable.** Pick by the endpoint's wire protocol, not by who
runs it:

- `gateway` — an OpenAI-compatible endpoint your institution runs. This
  is the one to reach for; it gets its own config section, its own tier
  hints for open-weight model names, and an honest **unknown** rather
  than a fabricated price.
- `OPENAI_BASE_URL` — only to redirect OpenAI itself, e.g. through a
  proxy. A gateway configured here reports as `openai` and borrows
  OpenAI's list prices, which makes the cost estimate wrong.
- `ANTHROPIC_BASE_URL` — only for an endpoint speaking the Anthropic
  Messages API, not the OpenAI one.

If `set_llm_provider.py` reports a missing credential, **hand off to
the wizard for that key** — do not ask for it in the chat. Then re-pin
the project's models, since the old pins name models the new provider
does not serve. Run this from the project directory to see what it
does serve:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/resolve_models.py
```

It lists models and writes nothing. **Propose one per stage to the user
and get confirmation before pinning** — the listing includes variants
that are not ordinary chat models (`:batch` queues, `-image` / `-tts`
endpoints, `deep-research`), and the `tier?` column is a guess from the
model's name, not a recommendation. Then write each confirmed choice:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/resolve_models.py \
    --stage abstract_screening --model <id>
```

Pinning ends with an automatic ~4-token test request, and the script
exits non-zero when the model does not answer. **A pin is not done
until that check passes** — the written line records an intention; the
check is what proves the model ID, the provider, and the credential
agree. To re-check later without re-pinning:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_model_connection.py
```

Report the status word verbatim. `QUOTA_EXHAUSTED` and `RATE_LIMITED`
are both HTTP 429 and mean opposite things: the first says the
allowance is spent and **retrying is useless** until billing or the
quota period changes; the second is a passing throttle. `AUTH_FAILED`
means rotate the key **through the wizard**, never through the chat.

## Red flags

- You are about to run a `Bash` tool call in this skill for anything
  other than the three provider scripts above. **Don't.** This skill has
  no Bash probes by design — they cause permission prompts for no
  benefit. The wizard handles everything else.
- A model-connection check returned `QUOTA_EXHAUSTED` and you are about
  to suggest waiting, retrying, or raising a timeout. **Don't.** That
  status means the quota is spent; only billing or a provider switch
  clears it. Retrying it once cost a real user ~22 minutes of silence.
- You are about to ask the user to paste a key into the chat.
  **Never.** The wizard is the only acceptable path for keys.
- You are about to log, echo, or repeat a key the user typed in any
  form. The wizard hides input for this exact reason; don't
  accidentally capture it in follow-up questions.
