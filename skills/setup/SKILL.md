---
name: setup
description: Use when the user invokes `/setup`, asks to configure the academic-research plugin for the first time, wants to add or rotate API keys (Zotero, Elsevier, WoS, Anthropic, Semantic Scholar, Wiley TDM, OpenAlex), register MCP servers, or patch permission rules. Also fires when any other academic-research procedural skill (zotero-operations, systematic-review, fact-check, critic-loop) reports `NOT CONFIGURED` on its pre-flight check. Hands the user a single terminal command that launches a setup wizard. The wizard reads keys with hidden input and writes configuration locally. API keys never pass through Claude's context.
---

# setup

Setup runs as a terminal wizard the **user** executes. Claude's role is
only to give them the command and confirm when they are done. Do not
run any tool calls — no Bash, no Read, no probes. All the information
needed is already known:

- **Wizard path:** `~/.claude/plugins/cache/mronkko/academic-research/*/scripts/setup/wizard.py`
  (the shell glob `*` matches whichever version is installed).
- **Config written to:** `~/.config/academic-research/config.toml` (mode 0600).
- **Settings patched:** `~/.claude/settings.json` (backed up as `.bak-wizard`).
- **Wizard is idempotent:** re-running updates or adds keys without
  clobbering existing ones.

## Procedure

**CRITICAL:** never ask the user to paste API keys into the Claude
chat. Any text typed into the chat is transmitted to Anthropic. The
wizard exists so keys stay local.

Paste the following message to the user (no tool calls needed — just
text):

> I'll hand you the setup wizard. It runs in your terminal, prompts for
> each API key with hidden input (keystrokes don't appear), then checks
> five MCP (Model Context Protocol) servers and offers to register any
> that are missing: **Zotero** (required — every citation skill uses
> it), at least one of **Scopus / Semantic Scholar / OpenAlex**
> (required for literature search to work), and **paper-search**
> (optional — for ArXiv / PubMed PDF retrieval). It then writes your
> config file and permission rules locally. **Your keys never pass
> through Claude's chat.**
>
> Paste this into a terminal and press Enter:
>
> ```
> python3 ~/.claude/plugins/cache/mronkko/academic-research/*/scripts/setup/wizard.py
> ```
>
> **How to open a terminal** if you are not already in one:
> - **macOS:** ⌘-Space → type *Terminal* → Enter.
> - **Windows:** Windows key → type *PowerShell* → Enter. If `python3`
>   is not recognised, try `python` instead.
> - **Linux:** Ctrl-Alt-T (or your distro's terminal app).
>
> Already running Claude in a terminal? Either open a new tab and run
> it there, or press Ctrl-C to exit this Claude session, run the
> wizard, then `claude -c` to resume this conversation.
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
  the exact install command (`uv tool install zotero-mcp-server`,
  `uv tool install scopus-mcp`, or "requires Node.js + npm" for the
  npx-based servers). Install it, then re-run the wizard — it's
  idempotent and picks up where it left off.
- **Wizard exits with code 4**: Zotero MCP is not connected. No
  academic-research skill works without it. The wizard's summary lists
  the install and registration commands; run them and re-run the
  wizard.

## Red flags

- You are about to run a `Bash` tool call in this skill. **Don't.**
  This skill has no Bash probes by design — they cause permission
  prompts for no benefit. The wizard handles everything.
- You are about to ask the user to paste a key into the chat.
  **Never.** The wizard is the only acceptable path for keys.
- You are about to log, echo, or repeat a key the user typed in any
  form. The wizard hides input for this exact reason; don't
  accidentally capture it in follow-up questions.
