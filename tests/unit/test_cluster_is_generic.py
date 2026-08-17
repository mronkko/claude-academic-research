"""Nothing shipped for the cluster path may name one institution's cluster.

This is the guard that keeps a design decision from eroding. The cluster
support in this plugin was written against one university's facility, and
every single detail of that facility — its hostname, its partitions, its
module names, the model IDs sitting in its shared weight cache — is
useless or actively misleading to the next user. A shipped guess does not
merely fail; it fails at 3am inside a queued job, having spent an
allocation, with an error that reads as the plugin being broken.

The pressure to erode is constant and reasonable-sounding. Every live
test run, every debugging session, every "let me just put the working
value in as the default" happens against a real cluster, and the working
value is right there. So the rule is enforced rather than remembered:
everything site-specific is a variable with a documented default and no
institution's name, and the one genuinely unshippable part — the
`module load` lines — is supplied by the user as `SITE_ENV`.

Two tiers, because they are different risks:

- **Site terms** (hostnames, partitions, scheduler flags, scratch
  variables) are scanned across everything shipped under
  `scripts/cluster/`, including the Python. An institution's name in a
  docstring is the leak, whether or not it changes behaviour.
- **Model terms** (specific weight IDs) are scanned in the operator-facing
  files only. `run_batch.py` legitimately matches on model *families* —
  a reasoning model needs a larger token budget, and knowing which
  families reason is knowledge about models, not about a cluster.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
CLUSTER_DIR = REPO / "scripts" / "cluster"

#: Files an operator reads or edits: no model may be named in them,
#: because a named model reads as a recommendation and the right model is
#: whatever the user's site actually has in its cache.
OPERATOR_FILES = sorted(CLUSTER_DIR.glob("*.sbatch")) + sorted(CLUSTER_DIR.glob("*.md"))

#: Everything shipped for this path, including the skill once B3 lands.
SKILL = REPO / "skills" / "cluster-screening" / "SKILL.md"
ALL_SHIPPED = sorted(p for p in CLUSTER_DIR.iterdir() if p.is_file()) + (
    [SKILL] if SKILL.exists() else []
)

#: Case-insensitive substrings that name one institution's facility, its
#: scheduler configuration, or its software stack.
SITE_TERMS = (
    "aalto",
    "triton",
    "scicomp",
    "wrkdir",
    "min-vram",
    "gpu-debug",
    "--partition=",
    "model-huggingface",
    "seff-gpu",
)

#: A hostname in a national TLD is the most direct leak of all: it is
#: someone's actual login node.
HOSTNAME_RE = re.compile(r"\b[a-z0-9][a-z0-9.-]*\.(fi|edu|ac\.uk|de|se|no)\b", re.I)

#: Weight identifiers. Vendor prefixes catch the common cases by name;
#: the parameter-count pattern catches `<anything>/<anything>-31B-...`,
#: which is the shape of nearly every open-weight release.
MODEL_VENDORS = (
    "redhatai/",
    "meta-llama/",
    "mistralai/",
    "deepseek-ai/",
    "qwen/",
    "google/gemma",
    "openai/gpt-oss",
    "nvidia/",
    "microsoft/phi",
)
MODEL_ID_RE = re.compile(r"\b[\w.-]+/[\w.-]*\d+[bB]\b")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_there_is_something_to_check() -> None:
    """A glob that silently matches nothing is a guard that passes vacuously."""
    assert OPERATOR_FILES, "no .sbatch or README under scripts/cluster/"
    assert len(ALL_SHIPPED) >= 3


@pytest.mark.parametrize("path", ALL_SHIPPED, ids=lambda p: p.name)
def test_no_institution_is_named(path: Path) -> None:
    lowered = _text(path).lower()
    found = [term for term in SITE_TERMS if term in lowered]
    assert not found, (
        f"{path.relative_to(REPO)} names {found}. Everything site-specific "
        f"belongs in the user's SITE_ENV snippet or in a documented "
        f"environment variable — a shipped value fails inside someone "
        f"else's queued job."
    )


@pytest.mark.parametrize("path", ALL_SHIPPED, ids=lambda p: p.name)
def test_no_real_hostname_is_shipped(path: Path) -> None:
    hits = [
        m.group(0) for m in HOSTNAME_RE.finditer(_text(path))
        # Ordinary prose ("etc.")  and file suffixes are not hostnames.
        if "." in m.group(0)[:-3]
    ]
    assert not hits, f"{path.relative_to(REPO)} ships hostname(s) {hits}"


@pytest.mark.parametrize("path", OPERATOR_FILES, ids=lambda p: p.name)
def test_no_model_is_named_to_the_operator(path: Path) -> None:
    """`MODELS=<org/model-id>` is the placeholder; a real ID is not.

    Naming one turns an example into a recommendation, and the right
    model is whichever one the user's site already holds — downloading a
    different one is usually impossible from a compute node anyway.
    """
    text = _text(path)
    lowered = text.lower()
    named = [v for v in MODEL_VENDORS if v in lowered]
    named += [m.group(0) for m in MODEL_ID_RE.finditer(text)]
    assert not named, (
        f"{path.relative_to(REPO)} names model(s) {named}. Use a "
        f"placeholder such as <org/model-id>."
    )


def test_the_sbatch_hardcodes_no_partition() -> None:
    """Many sites auto-select a partition; a wrong name never starts.

    `--partition=` is on the denylist above, so this only has to check
    that the omission is explained — otherwise the next reader adds one
    back believing it was forgotten.
    """
    text = _text(CLUSTER_DIR / "run_batch.sbatch")
    assert "#SBATCH --partition" not in text
    assert "deliberately not set" in text


def test_every_site_specific_value_is_an_environment_variable() -> None:
    """The variables the wrapper documents must be the ones it reads.

    A documented variable the script ignores is worse than an undocumented
    one: the user sets it, sees no effect, and concludes the whole path is
    broken.
    """
    text = _text(CLUSTER_DIR / "run_batch.sbatch")
    for var in (
        "MANIFEST", "MODELS", "SITE_ENV", "RUNNER", "OUT_DIR",
        "MAX_MODEL_LEN", "GPU_MEMORY_UTILIZATION", "TEMPERATURE", "SEED",
    ):
        assert f"${{{var}" in text, f"{var} is documented but never read"


def test_the_wrapper_fails_fast_on_a_missing_or_empty_manifest() -> None:
    """An empty manifest is not an empty result.

    Run, it produces a run record describing a clean pass over nothing,
    which is indistinguishable from a corpus that had no eligible items.
    """
    text = _text(CLUSTER_DIR / "run_batch.sbatch")
    assert ': "${MANIFEST:?' in text
    assert "not an empty result" in text
    assert "-s \"$MANIFEST\"" in text


def test_offline_mode_is_forced_so_a_missing_model_fails_rather_than_hangs() -> None:
    """Without it a compute node waits on a download it cannot perform.

    The allocation then expires with no output at all, which is the most
    expensive way to learn that a model is not in the site's cache.
    """
    text = _text(CLUSTER_DIR / "run_batch.sbatch")
    assert "HF_HUB_OFFLINE" in text
    assert "TOKENIZERS_PARALLELISM" in text


def test_the_glibcxx_workaround_is_documented_not_shipped() -> None:
    """It is the right fix on one kind of site and wrong everywhere else.

    Exported unconditionally, it puts a module's lib directory ahead of
    the system's for every user, including the ones whose stack was fine.
    """
    assert "LD_LIBRARY_PATH" not in _text(CLUSTER_DIR / "run_batch.sbatch")
    readme = _text(CLUSTER_DIR / "README.md")
    assert "GLIBCXX" in readme and "LD_LIBRARY_PATH" in readme


def test_vllm_is_not_a_dependency_of_this_repository() -> None:
    """It is a cluster-side runtime, not a test-suite dependency.

    Adding it to the dev group would pull a multi-gigabyte GPU stack into
    every CI job on three Python versions and three operating systems, to
    test a file whose whole design is that it never imports this
    repository. The hermetic fake in `test_batch_roundtrip.py` is what
    covers it instead.
    """
    text = _text(REPO / "pyproject.toml")
    assert "vllm" not in text.lower()
