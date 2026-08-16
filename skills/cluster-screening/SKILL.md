---
name: cluster-screening
description: Use when screening or coding papers on a GPU cluster or batch scheduler instead of an LLM API — emitting a request manifest, submitting a SLURM job, collecting responses, applying them to Zotero. Trigger phrases "screen on the cluster", "run this on SLURM", "sbatch", "GPU node", "emit a manifest", "apply the responses", "batch screening". Do NOT use for ordinary API-based screening — use `systematic-review`.
---

# cluster-screening

> **Glossary:** unfamiliar with **PRISMA**, **MCP**, **BBT**, **stage
> tag**, or **DOI**? See [skills/_glossary.md](../_glossary.md).

This skill covers one thing: running the screening or coding LLM pass
**somewhere the plugin cannot reach**, and bringing the answers back.
Everything before it (search, import, enrichment, the screening
protocol) and everything after it (audit, export, the manuscript) is
`systematic-review`'s. Load that skill for those; load this one for the
round trip.

## Pre-flight (ALWAYS run first, both of them, in this order)

```bash
python3 "${CLAUDE_PLUGIN_ROOT:-.}/scripts/setup/check_configured.py"
python3 "${CLAUDE_PLUGIN_ROOT:-.}/scripts/setup/check_cluster_config.py"
```

If the first says `NOT CONFIGURED`, stop and hand off to `/setup`.

The second prints the automation level. **Read the `effective:` line and
obey it. The level is not yours to choose, raise, or work around.** It
is a statement about how much of a shared facility account — somebody
else's allocation, on hardware the user is accountable for — the user
has agreed to hand over. If the level blocks what you were asked to do,
say so and print the commands; do not set a level to get past a
permission prompt, and do not suggest `--automation auto` as a fix for
one.

The block looks like this:

```
automation: confirm
source: config.toml [cluster] automation
effective: auto
allow_rules: Bash(ssh:*) (~/.claude/settings.local.json)
query_rules: none
settings_files: 2 read
```

`automation:` is what was asked for; `effective:` is what will actually
happen; any `NOTE:` line explains a difference between them. **Relay a
`NOTE:` to the user in full.** Both directions matter: `confirm` with an
allow rule confirms nothing (usually one "don't ask again" click, long
forgotten), and `auto` without one prompts on every call, which in a
headless session reads as a hang.

To change it, the user decides and this writes it:

```bash
python3 ${CLAUDE_PLUGIN_ROOT}/scripts/setup/set_cluster_automation.py <level>
```

### What each level permits you to do

| Level | You may | You may not |
|---|---|---|
| `manual` (default) | emit, apply, read local files, print commands | run `ssh`, `scp`, `rsync`, `sbatch`, `squeue`, `sacct` at all |
| `confirm` | run them, one permission prompt per call | click past a prompt on the user's behalf, or batch calls to reduce prompts |
| `auto` | run the loop unattended | resubmit silently, apply a refused run, or submit a second array job per request |

`manual` is the default and is often the only level that *works*:
reaching a cluster commonly needs a VPN, 2FA or Kerberos, none of which
an agent can do. Under `manual`, produce a numbered command block with
every path already filled in, and stop. That block is the deliverable —
make it copy-pasteable, not illustrative.

Under `confirm`, the permission prompt **is** the approval step. The
plugin never allow-lists `ssh`/`scp`/`rsync`/`sbatch` (pinned by
`tests/unit/test_cluster_automation.py`), so each call surfaces to the
user. Do not try to reduce the number of prompts by chaining commands
into one `ssh` invocation — that converts several approvals into one and
is exactly the erosion the level exists to prevent.

---

## The round trip

Five steps. The LLM runs only in step 3, and steps 1 and 5 need no LLM
at all.

```
  1 emit      abstract_screen.py --emit-manifest requests.jsonl
  2 transfer  three files out
  3 run       run_batch.py --execute      (on the GPU node)
  4 collect   two files back
  5 apply     abstract_screen.py --apply-responses responses.jsonl \
                                 --manifest requests.jsonl
```

### 1. Emit

Same selection flags as a normal run — the screening protocol,
`screening_config.py` and the prompt-placeholder self-check in
`systematic-review` all apply unchanged, because the manifest freezes
the prompt at this moment.

```bash
uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/abstract_screen.py \
    --group <id> --collection <key> --config ./screening_config.py \
    --emit-manifest .claude/batch/abstract_requests.jsonl
```

Full-text coding is the same shape, plus `--pdf-dir`:

```bash
uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/fulltext_code.py \
    --group <id> --collection <key> --config ./screening_config.py \
    --pdf-dir ./pdfs \
    --emit-manifest .claude/batch/fulltext_requests.jsonl
```

Emit writes **two** files and prints a `run_id`. Keep both:

- the manifest — every request, with the system prompt, its SHA-256,
  the prompt version and (for coding) the frozen `coding_fields`;
- a `.skipped.json` sidecar — every item *not* in the manifest and why
  (`no_abstract`, `no_pdf`, `pdf_unreadable`, `too_long_for_context`).
  **Read it and report the counts.** A run that screened 180 of 240
  papers is not a run that screened 240.

Report the `run_id` to the user; it is what ties the responses back.

**Size the requests against the model's context window before you
transfer anything.** Coding emit prints the largest request in
characters and estimated tokens. A request that does not fit fails
*inside* the serving stack in ways that read as model failure, after the
allocation has been spent. `--max-input-chars N` sends over-long items
to the sidecar as `too_long_for_context` instead of letting them
through; a reasonable starting point is four times the context window
you plan to serve, in tokens.

A `.jsonl.gz` suffix compresses the manifest. Full-text manifests reach
hundreds of megabytes, and the runner detects the suffix.

### 2. Transfer — three files out

`run_batch.py`, `run_batch.sbatch` and the manifest. Nothing else.

**Do not clone this repository onto the cluster**, and do not run the
runner as `python3 -m scripts.cluster.run_batch`. The runner imports
nothing from the plugin precisely so that it does not need a checkout,
and `scripts` is an ordinary directory name that a site's own software
stack very likely also has — `PYTHONPATH` does not reliably decide who
wins. Invoke it **by file path**. `scripts/cluster/README.md` has the
full rationale; hand that file to the user, it is written for them.

### 3. Run

The user writes one file the plugin never guesses at: a `SITE_ENV`
shell snippet holding their `module load` lines and model-cache
location. Then, from the login node, the pre-flight — it imports no GPU
stack and touches no allocation:

```bash
python3 run_batch.py --manifest requests.jsonl --model <org/model-id> --dry-run
```

Then one array job:

```bash
MANIFEST=$PWD/requests.jsonl \
MODELS=<org/model-id> \
SITE_ENV=~/llm-site-env.sh \
  sbatch --array=0 run_batch.sbatch
```

`MODELS` is colon-separated and indexed by the array task ID, so two
models over the same manifest is `--array=0-1` and **one** job, not two
submissions. Do not name a partition unless the user's site requires
one; many sites select it themselves and a wrong name never starts.

**Pilot first.** Emit ~10 items, or run a larger manifest with
`--limit 10`, and apply nothing until you have looked at the output.
This matters most for full-text coding, where an open-weight model must
return strict JSON: measure its compliance on ten papers rather than
discovering it on 240, because every parse failure otherwise costs a
whole GPU pass to repeat.

### 4. Polling discipline — read this before you write a loop

- **At most one `squeue -j <jobid>` per turn. Never in a loop, never in
  a `while`, never with `watch`.** A scheduler is a shared service and
  polling it is measurable load on other people's work.
- One array job over a flood of individual jobs, always.
- Long waits are normal. Queue time is hours at some sites. Tell the
  user the job ID and stop; do not idle-poll to look busy.
- **Scratch filesystems are usually not backed up.** Copy the responses
  file and its `.run.json` off the cluster rather than leaving them
  there.
- Size the next run from the run record, not from guesswork. It reports
  `model_load_s` separately from `generate_s` on purpose: loading
  weights takes minutes while generating a small batch takes seconds, so
  the ratio tells you how large the next manifest should be.

### 5. Apply

```bash
uv run ${CLAUDE_PLUGIN_ROOT:-.}/scripts/pipelines/abstract_screen.py \
    --group <id> --collection <key> --config ./screening_config.py \
    --apply-responses .claude/batch/abstract_responses.jsonl \
    --manifest .claude/batch/abstract_requests.jsonl
```

`--manifest` is required, not optional: the responses carry answers, the
manifest carries what was asked. Apply writes the CSV log rows, the
stage tags and (for coding) the child notes — the same artefacts a
synchronous run produces, from the same code.

**Apply is a write path with no LLM in it, often run days later.** That
is what makes it worth slowing down for:

- `--skip-already-tagged` skips items tagged since the manifest was
  emitted. Without it, apply overwrites decisions made in between. The
  applier warns when it finds any; **relay the count before writing.**
- A coding manifest freezes `coding_fields`. If `screening_config.py`
  has since gained or lost a field, apply refuses and names the
  difference. The fix is normally to re-emit, not to force. Do not reach
  for `--force-apply` on the user's behalf — it logs the run under the
  schema it was emitted with, which is a choice with consequences for
  the CSV a reviewer will read.

---

## Four hard stops

Stop and report. Do not work around any of these.

1. **A degenerate run.** If mean output is at or below two tokens the
   run record flags it and apply refuses. This is the failure mode that
   looks healthy: requests in, responses out, every row `call_status=ok`
   and nothing meaningful in any of them. The usual cause is a missing
   chat template — check `prompt_format` in the responses; `raw_completion`
   means the runner found no usable template and said so in the job log.
2. **A `run_id` mismatch** between manifest and responses. You have two
   different runs. Find the right pair; never apply across them.
3. **An unknown `schema_version`.** The runner on the cluster is a
   different version from the plugin here. Copy the current
   `run_batch.py` over and re-run; do not hand-edit either file.
4. **An error rate the user has not seen.** Report `call_status` counts
   before applying, not after.

The runner itself never retries and never writes placeholder text. A
failed generation is recorded as a failure with its error, the item
stays untagged, and a later run picks it up. Preserve that property:
if you find yourself about to synthesise a decision for a failed item,
stop.

---

## When the batch path is the wrong answer

Emit/apply exists because a scheduler cannot answer synchronously. If a
provider *can*, use `systematic-review`'s ordinary path — it is one
command instead of five, and it cannot drift between emit and apply.
Reach for this skill when the compute is a queued job, and for the
same emit/apply commands when the compute is anything else the plugin
cannot call directly.

`--workers` does not apply here. It parallelises a per-item API loop;
the batch runner hands the whole manifest to the serving engine at once
and lets it schedule the batch, which is where nearly all of the
throughput comes from.

Symptoms and their fixes — a `GLIBCXX` error on `import vllm`, a job
that hangs instead of failing on a missing model, empty responses with
`call_status=ok` — are in
[scripts/cluster/README.md](../../scripts/cluster/README.md). It is
written for the person at the terminal; hand it over rather than
paraphrasing it.
