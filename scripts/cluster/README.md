# Running a screening manifest on a GPU cluster

Screening and coding normally call an LLM per item and wait. That needs a
provider answering synchronously, which rules out the cheapest compute many
universities have: a GPU node behind a batch scheduler, where a job is
submitted and collected minutes or hours later.

The batch path splits the run in two so that machine can do the work:

    emit    abstract_screen.py --emit-manifest requests.jsonl
            ... the manifest is executed anywhere ...
    apply   abstract_screen.py --apply-responses responses.jsonl

This directory is the "anywhere = a GPU node" case. Two files:

| file             | what it is                                              |
|------------------|---------------------------------------------------------|
| `run_batch.py`   | the runner. Stdlib + `vllm`, no plugin imports at all.  |
| `run_batch.sbatch` | a SLURM wrapper. No site-specific value in it.        |

## The transfer

**Three files out, two files back.** Copy `run_batch.py`,
`run_batch.sbatch` and your manifest to the cluster; copy the responses
file and its `.run.json` back.

**Do not clone this repository onto the cluster.** The runner is
deliberately import-free of the plugin so that it does not need to be, and
a checkout there creates a packaging problem that has already cost one
project a day: `scripts` is an ordinary directory name, the site's own
software stack very likely has one, and `PYTHONPATH` does not reliably
decide who wins. That is also why the sbatch wrapper invokes the runner
**by file path** and never as `python3 -m scripts.cluster.run_batch`. A
path has no package to resolve and therefore nothing to lose.

## `SITE_ENV`: the one file you write

Everything site-specific lives in a shell snippet you supply, and nothing
in this repository guesses at its contents:

```bash
# ~/llm-site-env.sh — sourced by run_batch.sbatch before anything else
module load <your site's python/GPU toolchain>
module load <your site's model cache, if it has one>
export HF_HOME=<where your site keeps model weights>
```

Then:

```bash
mkdir -p logs                      # before the first submit, not after
MANIFEST=$PWD/requests.jsonl \
MODELS=<org/model-id> \
SITE_ENV=~/llm-site-env.sh \
  sbatch --array=0 run_batch.sbatch
```

`logs/` has to exist **before** you submit. The scheduler opens the
`--output` path itself, before the first line of the script runs, so the
`mkdir -p logs` inside the wrapper is too late to help the very first
job — which then fails with no log to explain why.

`MODELS` is colon-separated and indexed by `$SLURM_ARRAY_TASK_ID`, so
`MODELS=<org/a>:<org/b> sbatch --array=0-1` runs two models over the same
manifest as one array job. **One array job, not a flood of individual
jobs** — schedulers and their operators both prefer it, and it is one job
ID to poll rather than twenty.

Check the pre-flight on a login node first. It imports no GPU stack:

```bash
python3 run_batch.py --manifest requests.jsonl --model <org/model-id> --dry-run
```

## Pilot before you commit a corpus

Emit a ~10-item manifest and run that first, especially for full-text
coding. An open-weight model's compliance with a strict JSON schema is
worth measuring on ten papers rather than discovering on 240 — every
parse failure otherwise costs a whole GPU pass to repeat. `--limit N`
runs the first N requests of a larger manifest for the same purpose.

## Polling discipline

At most one `squeue -j <jobid>` per check, never in a loop. A scheduler is
a shared service and a polling loop is measurable load on it. Scratch
filesystems are usually not backed up: copy the responses file and the run
record off the cluster rather than leaving them there.

Size the next run from the run record rather than from guesswork. It
reports `model_load_s` separately from `generate_s` on purpose — loading
weights takes minutes and generation for a small batch takes seconds, so
the ratio is what tells you how big to make the next manifest.

## What the runner refuses to do

- **It does not retry, and it never writes placeholder text.** A failed
  generation is recorded as a failed generation with its error; the item
  stays untagged and a re-run picks it up.
- **It does not send a request that cannot be answered.** A prompt leaving
  no room for an answer inside `--max-model-len` is recorded as an error
  against its own item, with both numbers, and the rest of the batch runs.
- **It flags a degenerate run.** If the mean output is at or below two
  tokens, the run record says so and `--apply-responses` refuses it. That
  is the failure mode that looks healthy: requests in, responses out,
  every row `call_status=ok`, and nothing in Zotero to show the run was
  worthless.

## Symptoms and fixes

**`import vllm` fails with a `GLIBCXX_...` version error.** A site module
ships a wheel built against a newer `libstdc++` than the operating
system's. The usual fix is to put the module's own lib directory ahead of
the system one, in `SITE_ENV`:

```bash
export LD_LIBRARY_PATH="$(python3 -c 'import sys; print(sys.prefix)')/lib:${LD_LIBRARY_PATH:-}"
```

It is not shipped in `run_batch.sbatch` because it is wrong on any site
that does not have this problem.

**The job hangs instead of failing on a missing model.** The wrapper
exports `HF_HUB_OFFLINE=1` for exactly this: without it a compute node
with no outbound network waits on a download that cannot happen, and the
allocation expires. If you unset it, expect the hang.

**Every response is empty and `call_status` reads `ok` throughout.** Read
`prompt_format` in the responses file. `raw_completion` means the runner
found no usable chat template and warned loudly in the job log; an
instruction-tuned model handed an unframed prompt answers with a single
end-of-turn token. The run record's degeneracy flag catches this, and the
applier refuses the run.
