---
name: setup
description: Use when the user invokes `/setup`, asks to configure the academic-research plugin for the first time, wants to add or rotate API keys (Zotero, Elsevier, WoS, Anthropic, Semantic Scholar, Wiley TDM, OpenAlex), register MCP servers, or patch permission rules. Also fires when any other academic-research procedural skill (zotero-operations, systematic-review, fact-check, critic-loop) reports `NOT CONFIGURED` on its pre-flight check. Launches a terminal wizard that collects API keys with hidden input, writes the config file, and patches settings.json. API keys entered in the wizard never pass through Claude's context.
---

# setup

Setup runs as a terminal wizard (`scripts/setup/wizard.py`). The wizard
collects API keys with hidden input and writes configuration files
directly. **Keys entered in the wizard never pass through Claude's
context.** The skill's job is to launch the wizard (CLI) or walk the
user through running it in a terminal (GUI / Desktop).

## Pre-flight

```bash
test -f ~/.config/academic-research/config.toml && echo "config exists" || echo "no config"
ls -d ~/.claude/plugins/cache/mronkko/academic-research/*/ 2>/dev/null | head -1 || echo "plugin not installed"
```

Read the plugin path from the `ls` output — that is `$CLAUDE_PLUGIN_ROOT`
for this session. If config already exists, ask whether the user wants
to re-run the wizard (to update or add keys) or skip setup.

## Step 1 — Decide the launch path

**CRITICAL:** never ask the user to paste API keys into the Claude chat.
Any text typed into the chat is transmitted to Anthropic. The wizard
exists specifically so keys stay local.

Detect the environment:

```bash
test -t 0 && echo "tty" || echo "no-tty"
```

### If `tty` (CLI Claude Code in a terminal)

Claude can launch the wizard directly in-process — the user types into
the same terminal window, and their keystrokes go to the wizard via
`getpass`, not through Claude. Tell the user:

> I'll launch the setup wizard now. When it asks for a key, type it at
> the prompt in this terminal — the keystrokes go directly to the
> wizard, not to me. I can't see what you type.

Then run:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/wizard.py
```

When the wizard exits, it prints a one-line summary Claude can read to
confirm success.

### If `no-tty` (Desktop app, Positron native, VSCode extension, headless)

Claude cannot take interactive input. Tell the user exactly how to open
a terminal on their OS, paste one command, and run it.

**Copy-paste this to the user** (adapt to the detected OS — read
`uname` if unsure):

> I can't launch an interactive wizard from this chat interface, so you'll
> run it yourself in a terminal window. Don't worry — it's one command.
>
> **macOS:**
>   1. Press ⌘-Space, type *Terminal*, press Enter.
>   2. Paste the command below and press Enter.
>
> **Windows:**
>   1. Press Windows key, type *PowerShell*, press Enter.
>   2. Paste the command below and press Enter.
>
> **Linux:**
>   1. Open your terminal (Ctrl-Alt-T on most distros).
>   2. Paste the command below and press Enter.
>
> ```
> python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/wizard.py
> ```
>
> (On Windows, if `python3` is not found, try `python` instead.)
>
> The wizard will walk you through each API key. When it says "Setup
> complete", come back here and tell me.

After the user says they are done, verify:

```bash
test -f ~/.config/academic-research/config.toml && stat -f "%Sp" ~/.config/academic-research/config.toml 2>/dev/null || stat -c "%A" ~/.config/academic-research/config.toml
```

The mode should be `-rw-------` (0600). If it does not exist or has
looser permissions, something went wrong — ask the user to paste the
wizard's output.

## Step 2 — MCP server verification

The plugin expects these MCP servers to be registered with Claude Code:

- `openalex`
- `semantic-scholar`
- `zotero`
- `paper-search` (or equivalent)

The wizard's final report lists any missing servers. If any are
missing, suggest registration commands like
`claude mcp add openalex <command>` and let the user run them — the
plugin does not bundle MCP server code because there are multiple
competing implementations.

## Step 3 — Playwright (optional, for CF-gated PDF fetching)

For publisher sites behind Cloudflare (Sage, Emerald, APA PsycNET), the
plugin uses Playwright's Chromium. If the user plans to run the PDF
pipeline against those publishers, they should install it:

```bash
playwright install chromium
playwright install-deps   # Linux only; skip on macOS/Windows
```

Ask first — the install downloads ~100 MB. Skip if they say no.

## Step 4 — Onboarding

After everything verifies, show the user this menu:

> **Done.** You now have:
>
> - `mcp-research`, `empirical-integrity`, `academic-writing` — eager
>   rule-books that fire automatically on relevant work.
> - `/critic-loop <doc>` — parallel-critic manuscript revision.
> - `systematic-review` — PRISMA-style SLR pipeline (say "run a systematic
>   review on X").
> - `zotero-operations` — Zotero enrichment outside an SLR (say "add
>   abstracts and PDFs to my Zotero library").
> - `fact-check` — one-shot citation/claim audit.
>
> Suggested first step: try `fact-check` on a short draft you have, or
> tell me what you want to work on.

## Red flags

- You are about to ask the user to paste a key into the chat. **Never.**
  The wizard is the only acceptable path for keys.
- You are about to run the wizard on a headless machine where no
  terminal is available. In that case, give the user the command and
  wait for them to run it themselves.
- The wizard wrote `config.toml` but not mode 0600. Re-run with correct
  `chmod` or tell the user the wizard has a bug.
- You are about to log, echo, or repeat a key the user typed in any
  form. Never — the wizard hides input for this exact reason.
