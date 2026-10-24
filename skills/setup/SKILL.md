---
name: setup
description: Use when the user invokes `/setup`, asks to configure the academic-research plugin, add or rotate an API key (Elsevier, WoS, Semantic Scholar, Wiley TDM, OpenAlex, an LLM provider), switch LLM provider or register MCP servers, or when another skill's pre-flight reports `NOT CONFIGURED`. Trigger phrases "/setup", "run setup wizard", "configure the plugin", "add my API key", "switch to OpenAI". Do NOT use for Zotero library work — use `zotero-operations`.
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

Setup runs as a terminal wizard the **user** executes. The agent's job
is to explain why, hand over one command that works as pasted, and
confirm when the user is done.

**Why a terminal program, in one sentence for the user:** anything typed
into this chat — API keys included — is sent to the model provider and
kept in the conversation transcript, so keys are entered in a terminal
program that hides them as they are typed and writes them only to a file
on the user's own computer.

What the wizard touches:

- **Config written to:** `~/.config/academic-research/config.toml`
  (mode 0600 on macOS/Linux).
- **MCP servers:** registered with Claude Code (`claude mcp add`) and,
  if Antigravity (`agy`) is detected, also in
  `~/.gemini/config/mcp_config.json` (backed up as `.bak-wizard`).
- **The plugin's own `academic-research` MCP server** (the pipeline
  steps as tools): Claude Code loads it from the plugin itself, needing
  only `uv`; for Antigravity, and Codex (untested), the wizard offers to
  register it.
- **No permission rules.** The wizard does not edit
  `~/.claude/settings.json`; which tool calls to allow is the user's
  own decision.
- **Idempotent:** re-running updates or adds keys without clobbering
  existing ones. It is also the way to rotate a single key.

## Procedure

**CRITICAL:** never ask the user to paste API keys into the chat.

### 1. Resolve the wizard's absolute path (the one tool call)

The user's terminal has no plugin-root environment variable and their
project has no `scripts/` directory, so the command must carry the
plugin's real, absolute path. Work it out, then confirm it:

- The wizard is `${CLAUDE_PLUGIN_ROOT}/scripts/setup/wizard.py`. In
  Claude Code that variable is normally already replaced by an absolute
  path in the text you are reading.
- If that path does not start with `/` or a drive letter — it still
  begins with a `$` variable (other hosts, such as Antigravity) — use this skill's own location instead: this file
  is `<plugin root>/skills/setup/SKILL.md` (Claude Code reports it as
  "Base directory for this skill"), so the wizard is
  `<plugin root>/scripts/setup/wizard.py`. Write the path out in full —
  no `..`, no `~`, no `*`.
- **Confirm it with exactly one tool call:** a `Read` of the first few
  lines of that file. This is the only tool call the hand-off makes —
  no Bash, no config probes. If the file is not there, tell the user
  you could not locate the plugin and ask where it is installed;
  never hand over a command you have not confirmed.

### 2. Hand over one command

Pick the interpreter from the user's operating system (your
environment information names the platform):

- macOS / Linux: `python3 "<absolute path>"`
- Windows: `py "<absolute path>"` — the Python launcher installed by
  python.org's installer. If PowerShell says `py` is not recognised,
  the same line with `python` instead.

Always quote the path (Windows user folders and macOS
`Application Support` contain spaces). Then send the user this message,
with the one resolved command in the fenced block — not a template, not
two alternatives:

> Setup runs in your terminal rather than in this chat, because anything
> typed here — API keys included — is sent to the model provider and
> kept in the chat transcript. The wizard asks for keys with hidden
> input and stores them only in a file on your computer.
>
> **Have Zotero 10 open first.** The wizard starts by reading from and
> writing to Zotero on your computer (it creates one test note in My
> Library and deletes it again), and stops there if either fails. The
> first time, Zotero shows a permission dialog — choose **Always
> Allow**. No zotero.org key is needed.
>
> It then asks **which LLM provider** should run the screening
> pipelines — Anthropic, Google, OpenAI, OpenRouter, your institution's
> own OpenAI-compatible gateway, or a local server (Ollama or LM Studio,
> which need no key) — and asks only for that provider's credential. It
> also asks three questions about PDF retrieval: the lowest article
> version to attach (published / accepted manuscript / preprint), whether
> to attach generated text-only PDFs rebuilt from publisher XML, and — if
> you have an OpenAlex key — whether to use OpenAlex's paid Content API
> (about $0.01 a PDF) before the free sources, only as a last resort, or
> never. Our recommendation is to pay before spending your own time on
> manual downloads. Re-running the wizard keeps your earlier answers as
> the defaults. It then checks five MCP (Model Context Protocol) servers and offers to
> register any that are missing: **Zotero** (required — every citation
> skill uses it), at least one of **Scopus / Semantic Scholar /
> OpenAlex** (required for literature search), and **paper-search**
> (optional — ArXiv / PubMed PDF retrieval).
>
> Copy this line into a terminal and press Enter:
>
> ```
> python3 "<absolute path to wizard.py>"
> ```
>
> **Opening a terminal:** macOS — ⌘-Space, type *Terminal*, Enter.
> Windows — Windows key, type *PowerShell*, Enter. Linux — Ctrl-Alt-T.
> If you are running me in a terminal already, open a new tab or window
> for the wizard.
>
> When the wizard prints "Setup complete", come back and say "done".

Replace the whole line in that block with the command from above —
interpreter for the user's OS plus the path confirmed in step 1.

After the user says they finished the wizard, respond with a short
confirmation ("Setup done. Ready for the next task.") and let the next
conversational turn drive the work. Do not run a verification call — if
something went wrong, the next skill's own pre-flight check will route
back here.

## If the wizard reports errors

The wizard prints to stdout. If the user pastes output showing a
problem:

- **Python missing** (`command not found`, or on Windows a Microsoft
  Store window opening instead): tell them to install Python 3.11+ —
  macOS can use Homebrew (`brew install python`), Windows python.org's
  installer (it installs the `py` launcher; tick "Add Python to PATH"),
  Linux the distro's package manager.
- **Tkinter not required** — the wizard is terminal-only. Any Python
  3.11+ install works.
- **Permission denied writing config**: user's home directory has
  unusual permissions. Unlikely on a single-user machine.
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
- **Wizard exits with code 3**: the Zotero read or write test failed
  and setup stopped before asking anything else. The lines above the
  stop say which: Zotero not running, the "Allow other applications …
  to communicate with Zotero" setting off (Settings → Advanced →
  General), a Zotero older than 10, or the permission dialog answered
  Deny or one-time Allow. Fix that and re-run; choose **Always Allow**.
- **Wizard exits with code 4**: Zotero MCP is not connected. No
  academic-research skill works without it. The wizard's summary lists
  the install and registration commands; run them and re-run the
  wizard.

## Switching LLM provider (the one path that is not a wizard hand-off)

"Switch me to OpenAI", "use a local model for screening", "which model
provider am I on?" — these do not involve a key, so they do not need
the wizard. Four scripts cover it, and they are the **only** Bash calls
this skill makes (the wizard hand-off above makes none): the two
below, plus `resolve_models.py` and `check_model_connection.py` further
down.

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_llm_provider.py"
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/set_llm_provider.py" <name>
```

`<name>` is one of `anthropic`, `google`, `openai`, `openrouter`,
`gateway`, `ollama`, `lmstudio`. The check script prints the provider,
whether one was ever chosen, and whether its credential is present —
never a key value. Do NOT read
`~/.config/academic-research/config.toml` yourself: it holds the keys,
and anything you read lands in the transcript. The scripts exist so you
never have to.

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
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/resolve_models.py"
```

It lists models and writes nothing. **Propose one per stage to the user
and get confirmation before pinning** — the listing includes variants
that are not ordinary chat models (`:batch` queues, `-image` / `-tts`
endpoints, `deep-research`), and the `tier?` column is a guess from the
model's name, not a recommendation. Then write each confirmed choice:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/resolve_models.py" \
    --stage abstract_screening --model <id>
```

Pinning ends with an automatic ~4-token test request, and the script
exits non-zero when the model does not answer. **A pin is not done
until that check passes** — the written line records an intention; the
check is what proves the model ID, the provider, and the credential
agree. To re-check later without re-pinning:

```bash
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup/check_model_connection.py"
```

Report the status word verbatim. `QUOTA_EXHAUSTED` and `RATE_LIMITED`
are both HTTP 429 and mean opposite things: the first says the
allowance is spent and **retrying is useless** until billing or the
quota period changes; the second is a passing throttle. `AUTH_FAILED`
means rotate the key **through the wizard**, never through the chat.

## Red flags

- You are about to run a `Bash` tool call in this skill for anything
  other than the four provider scripts above, or more than the single
  `Read` that confirms the wizard path. **Don't.** The wizard handles
  everything else.
- You are about to hand the user a command containing
  a `$` variable, `./scripts/`, `~/.claude/plugins/...`, a `*`, or
  two alternatives to choose from. **Don't.** Their terminal
  cannot resolve any of those; give the one confirmed absolute path.
- A model-connection check returned `QUOTA_EXHAUSTED` and you are about
  to suggest waiting, retrying, or raising a timeout. **Don't.** That
  status means the quota is spent; only billing or a provider switch
  clears it. Retrying it once cost a real user ~22 minutes of silence.
- You are about to ask the user to paste a key into the chat.
  **Never.** The wizard is the only acceptable path for keys.
- You are about to log, echo, or repeat a key the user typed in any
  form. The wizard hides input for this exact reason; don't
  accidentally capture it in follow-up questions.
