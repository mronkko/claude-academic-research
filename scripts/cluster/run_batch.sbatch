#!/bin/bash -l
#SBATCH --job-name=llm-batch
#SBATCH --time=01:00:00
#SBATCH --cpus-per-task=8
#SBATCH --mem=64G
#SBATCH --gpus=1
#SBATCH --output=logs/%x-%A_%a.out
#SBATCH --error=logs/%x-%A_%a.err
#
# Execute a screening manifest on a GPU node: one model per array task,
# one GPU each.
#
# Nothing site-specific is written into this file, because there is no
# honest value to write. Every cluster names its partitions, its module
# system and its scratch differently, and a shipped guess is worse than a
# required variable: the guess fails at 3am inside a job, the variable
# fails immediately with a message naming what to set.
#
#   MANIFEST=/path/to/requests.jsonl \
#   MODELS=<org/model-id> \
#   SITE_ENV=~/llm-site-env.sh \
#     sbatch --array=0 run_batch.sbatch
#
# Variables (all but the first two have defaults):
#
#   MANIFEST     required. The .jsonl (or .jsonl.gz) emitted by
#                `--emit-manifest`.
#   MODELS       required. Colon-separated model IDs, indexed by
#                $SLURM_ARRAY_TASK_ID, so `--array=0-2` runs three models
#                over the same manifest. A model ID never contains a
#                colon; it usually contains a slash, which is why the
#                separator is not a slash.
#   SITE_ENV     path to a shell snippet sourced before anything else.
#                This is where `module load ...` goes — the single most
#                site-specific thing about running any of this, and
#                therefore supplied rather than shipped. See README.md.
#   RUNNER       path to run_batch.py (default: beside this file).
#   OUT_DIR      where responses and the run record land
#                (default: beside the manifest).
#   MAX_MODEL_LEN          context window to serve (default 32768).
#   GPU_MEMORY_UTILIZATION fraction of VRAM vLLM may claim (default 0.90).
#   TEMPERATURE  sampling temperature (default 0.0).
#   SEED         sampling seed (default 42).
#
# `--partition` is deliberately not set: many sites auto-select one from
# the time/memory/GPU request, and a wrong partition name is a job that
# never starts. If yours needs one, pass it on the sbatch command line.
# Likewise if your site expresses VRAM as a GRES, add that flag there —
# what a model needs is a property of the model, not of this script.
#
# Size the next run from measurement rather than guesswork: the run
# record separates model load from generation, and load can dominate a
# small batch several times over.

set -euo pipefail
mkdir -p logs

# Site setup first, so it can put python3, vllm and the model cache on
# PATH before anything below looks for them.
if [ -n "${SITE_ENV:-}" ]; then
  if [ ! -r "$SITE_ENV" ]; then
    echo "SITE_ENV=$SITE_ENV is not readable." >&2
    exit 2
  fi
  # shellcheck disable=SC1090
  . "$SITE_ENV"
fi

# Compute nodes usually have no outbound network. Without this a missing
# model *hangs* on a download that cannot happen, burning the allocation;
# with it, the job fails immediately and says what is missing.
export HF_HUB_OFFLINE="${HF_HUB_OFFLINE:-1}"
export TOKENIZERS_PARALLELISM="${TOKENIZERS_PARALLELISM:-false}"

: "${MANIFEST:?set MANIFEST=/path/to/requests.jsonl}"
if [ ! -s "$MANIFEST" ]; then
  echo "MANIFEST $MANIFEST is missing or empty." >&2
  echo "An empty manifest is not an empty result: re-emit it, and read" >&2
  echo "the .skipped.json sidecar to see why every unit was dropped." >&2
  exit 2
fi

: "${MODELS:?set MODELS=<org/model-id>[:<org/model-id>...], indexed by --array}"
IFS=':' read -r -a MODEL_LIST <<< "$MODELS"
TASK_ID="${SLURM_ARRAY_TASK_ID:-0}"
if [ "$TASK_ID" -ge "${#MODEL_LIST[@]}" ]; then
  echo "array task $TASK_ID has no entry in MODELS (${#MODEL_LIST[@]} model(s))." >&2
  echo "Use --array=0-$(( ${#MODEL_LIST[@]} - 1 ))." >&2
  exit 2
fi
MODEL="${MODEL_LIST[$TASK_ID]}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# By file path, never `python3 -m`. `scripts` is a common package name and
# on a cluster it is very likely already taken by the site's own software
# stack; a path has no package to resolve and so has nothing to lose.
RUNNER="${RUNNER:-$SCRIPT_DIR/run_batch.py}"
if [ ! -r "$RUNNER" ]; then
  echo "RUNNER $RUNNER is not readable. Copy run_batch.py next to this" >&2
  echo "script, or set RUNNER=/path/to/run_batch.py." >&2
  exit 2
fi

OUT_DIR="${OUT_DIR:-$(dirname "$MANIFEST")}"
mkdir -p "$OUT_DIR"

echo "=== node=${SLURMD_NODENAME:-?} job=${SLURM_JOB_ID:-?} task=$TASK_ID"
echo "=== model=$MODEL max_model_len=${MAX_MODEL_LEN:-32768}"
echo "=== manifest=$MANIFEST"
echo "=== out_dir=$OUT_DIR"
nvidia-smi --query-gpu=name,memory.total,driver_version \
           --format=csv,noheader || true

# --confirm carries the approval given by submitting this job; the
# pre-flight summary is still printed into this log before the model is
# loaded, so the log records what was sent.
srun python3 "$RUNNER" \
     --manifest "$MANIFEST" \
     --model "$MODEL" \
     --out-dir "$OUT_DIR" \
     --max-model-len "${MAX_MODEL_LEN:-32768}" \
     --gpu-memory-utilization "${GPU_MEMORY_UTILIZATION:-0.90}" \
     --temperature "${TEMPERATURE:-0.0}" \
     --seed "${SEED:-42}" \
     --execute --confirm

echo "=== responses and run record under $OUT_DIR"
