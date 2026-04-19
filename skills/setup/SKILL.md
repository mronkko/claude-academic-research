---
name: setup
description: Use when the user invokes `/setup`, asks to configure the academic-research plugin for the first time, wants to add or rotate API keys (Zotero, Elsevier, WoS, Anthropic, Semantic Scholar, Wiley TDM, OpenAlex), register MCP servers, or patch permission rules. Also fires when any other academic-research procedural skill (zotero-operations, systematic-review, fact-check, critic-loop) reports `NOT CONFIGURED` on its pre-flight check. Walks the user through API-key entry, writes `~/.config/academic-research/config.toml` (mode 0600), patches `~/.claude/settings.json`, registers MCP servers, installs Playwright Chromium. Chat-driven — no terminal required.
---

# setup

The plugin does not function until `/setup` has run. Walk the user
through configuration interactively. Each step is chat-driven: ask,
confirm, write. The user does not need to open a terminal.

## Pre-flight

Before asking any questions, check what has already been done. Run:

```bash
test -f ~/.config/academic-research/config.toml && echo "config exists" || echo "no config"
test -L ~/.claude/settings.json && readlink ~/.claude/settings.json || echo "settings is a regular file or missing"
test -d ~/.claude/plugins/mronkko/academic-research && echo "plugin installed" || echo "plugin not installed"
```

Report what you find. If the plugin is not installed, stop and tell the
user the install commands (`/plugin marketplace add
mronkko/claude-academic-research` then `/plugin install
academic-research@mronkko`). If config already exists, ask whether the
user wants to reconfigure (replace) or update selected keys only.

## Step 1 — API keys

Collect the keys below. For each one, ask a single focused question
with a short explanation of what it is and whether it's required or
optional. Do NOT batch — ask one at a time. Never display or echo a key
after the user provides it.

**Required for core functionality (most workflows):**

- `ZOTERO_API_KEY` — Zotero write access. Generate at
  <https://www.zotero.org/settings/keys>. Required for all Zotero
  operations.
- `ZOTERO_GROUP` — Zotero group library ID (numeric). Visible in the
  group URL: `https://www.zotero.org/groups/<this-number>`. Required
  for group-library workflows; set to user library ID if personal.
- `ANTHROPIC_API_KEY` — Required for LLM screening/coding in
  `systematic-review`. Generate at
  <https://console.anthropic.com/settings/keys>.

**Required for systematic reviews:**

- `WOS_API_KEY_EXTENDED` — Web of Science Expanded API. Institutional
  access required. Prefer this over the Starter API.
- `ELSEVIER_API_KEY` — ScienceDirect full text + abstract retrieval.
  Institutional access required.
- `S2_API_KEY` — Semantic Scholar. Free tier works for low volume;
  request a key at
  <https://www.semanticscholar.org/product/api#api-key-form> to get
  higher rate limits.
- `CROSSREF_MAILTO` — an email address used as Crossref's polite-pool
  identifier. Any valid email.

**Optional:**

- `WILEY_TDM_TOKEN` — UUID issued under the institution's Wiley TDM
  agreement. Required only if pulling Wiley PDFs; ask your librarian.
- `OPENALEX_API_KEY` — paid tier for the OpenAlex Content API
  (\$0.01/PDF). Skip unless you need high-volume PDF retrieval.
- `WOS_API_KEY` — Web of Science Starter API (field-limited; useful
  only for piloting).

If the user doesn't have a key ready for an optional field, proceed
and note it as `""` in the config. They can rerun `/setup` later.

## Step 2 — Write the config file

Write `~/.config/academic-research/config.toml` with mode `0600`:

```bash
mkdir -p ~/.config/academic-research
chmod 700 ~/.config/academic-research
```

Then write the file with this structure (omit keys the user did not
provide — don't leave empty quoted strings for every optional key):

```toml
# Academic-research plugin configuration.
# File permissions: 0600. Not tracked in git. Not cloud-synced to
# non-encrypted providers. If this file is compromised, rotate every
# key listed here.

[zotero]
api_key = "..."
group_id = "..."

[anthropic]
api_key = "..."

[wos]
expanded_key = "..."
# starter_key = ""

[elsevier]
api_key = "..."

[semantic_scholar]
api_key = "..."

[crossref]
mailto = "user@example.com"

# [wiley]
# tdm_token = ""

# [openalex]
# api_key = ""
```

After writing, set file mode:

```bash
chmod 600 ~/.config/academic-research/config.toml
```

Confirm with `ls -l ~/.config/academic-research/config.toml` — mode
should show `-rw-------`.

## Step 3 — Patch settings.json (permissions)

Ask explicit consent before editing `~/.claude/settings.json`. Show the
diff you plan to apply. The user can say no and the setup continues
(without pre-approval they will see per-script permission prompts).

The patterns to add under `permissions.allow`:

```json
"Bash(uv run ${CLAUDE_PLUGIN_ROOT}/scripts/**)",
"Bash(uv run -s ${CLAUDE_PLUGIN_ROOT}/scripts/**)",
"Bash(uv run --script ${CLAUDE_PLUGIN_ROOT}/scripts/**)",
"Bash(python3 ${CLAUDE_PLUGIN_ROOT}/scripts/**)",
"Bash(${CLAUDE_PLUGIN_ROOT}/scripts/**.py:*)",
"Bash(playwright install chromium)",
"Bash(playwright install-deps)",
"Read(//Users/${USER}/.config/academic-research/)"
```

Under `permissions.deny` (create the array if absent):

```json
"Read(//Users/${USER}/.config/academic-research/config.toml)",
"Bash(cat //Users/${USER}/.config/academic-research/config.toml)",
"Bash(head //Users/${USER}/.config/academic-research/config.toml*)",
"Bash(tail //Users/${USER}/.config/academic-research/config.toml*)",
"Bash(grep*//Users/${USER}/.config/academic-research/config.toml*)"
```

Substitute `${USER}` with the actual username (read `echo $USER` or
`whoami`). The deny rules stop Claude from reading the config file —
API keys stay out of conversation context. Scripts read the file
directly via Python's `open()`, which is not mediated by Claude's tool
layer.

Explain the deny rules to the user: *"These prevent me from ever
reading your config file myself. Your scripts can read it because they
run outside my tool layer, but if I'm ever asked to 'just check the
config', the read will fail. This keeps API keys out of anything I
send to Anthropic."*

## Step 4 — Register MCP servers

The plugin relies on four MCP servers. The user must have each
installed. Check with:

```bash
claude mcp list
```

Expected entries (or equivalent):

- `openalex` — OpenAlex API (`mcp__openalex__*`).
- `semantic-scholar` — Semantic Scholar API.
- `zotero` — local Zotero library access.
- `paper-search` or `paper-search-nodejs` — multi-source paper search.

For each missing server, ask the user if they want it registered now.
Give them the `claude mcp add ...` command for their target server
implementation — the plugin does not bundle MCP server code, so
installation details depend on which implementation the user prefers.
Common published options as of 2026-04:

- openalex: `openalex-research-mcp` (npm)
- semantic-scholar: `@xbghc/semanticscholar-mcp` (npm)
- zotero: `zotero-mcp` (pyzotero-backed; requires local Zotero running)
- paper-search: `paper-search-nodejs` (npm)

Offer the user the command lines, let them run them (or copy them), and
verify after.

## Step 5 — Install Playwright Chromium

For CF-gated publisher PDF retrieval, run:

```bash
playwright install chromium
playwright install-deps   # Linux only; skip on macOS
```

Ask first — the install downloads ~100 MB. If the user declines,
browser-based PDF retrieval won't work but everything else will.

## Step 6 — Verification

Run a smoke test:

```bash
test -r ~/.config/academic-research/config.toml && echo "config readable"
grep -q "ZOTERO\|zotero" ~/.config/academic-research/config.toml && echo "zotero configured"
claude mcp list
```

If any fail, walk back to the failing step.

## Step 7 — Onboarding the user

After verification, show the user a short "what's next" menu:

> **Done.** You now have:
>
> - `/mcp-research` rules active for citation work (fires automatically).
> - `/empirical-integrity` rules active for manuscript edits
>   (fires automatically).
> - `/academic-writing` rules active for draft/revise/polish work
>   (fires automatically).
> - `/critic-loop <doc>` to revise a manuscript with 4 parallel critics.
> - `/systematic-review` for PRISMA-style SLRs.
> - `/zotero-operations` for Zotero-specific work outside an SLR.
> - `/fact-check <doc>` to audit citations against sources.
>
> Suggested first step: try `/critic-loop README.md --no-test` on a
> draft you have, or pick an SLR project to run the pipeline on.

## Red flags

- You are about to display an API key the user just typed. Never.
- You are about to commit `config.toml` to git, or copy it into a
  project folder. Never — it lives at `~/.config/academic-research/`
  only.
- You are skipping the 0600 chmod because "it'll probably be fine".
  Always set it.
- You are editing `~/.claude/settings.json` without showing the diff
  and asking for explicit consent.
- You are sending any key to Anthropic in a tool call argument, log
  line, or chat message.
- You are running `claude mcp add ...` yourself with a key baked into
  the command line. Have the user run it so the key never passes
  through your tool layer.
