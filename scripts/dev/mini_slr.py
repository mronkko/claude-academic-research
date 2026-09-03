#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
#     "tenacity>=8.0",
#     "httpx>=0.25",
# ]
# ///
"""Resumable live end-to-end mini-SLR driver (BACKLOG.md "L1").

Drives the whole systematic-review pipeline — search, then a small
year-only-2019 corpus trim, Zotero import, sync, enrichment, audit,
abstract screening, full-text coding, export, and a verify pass — against
real APIs and a real, disposable Zotero group. Every pipeline stage is
invoked exactly as a user / the `systematic-review` skill would invoke it
(`uv run <script> ...` with its own PEP 723 deps), so whole-pipeline
defects that only appear when stages are chained together surface here
instead of in a user's project.

Target library: a hand-created Zotero GROUP named exactly
`academic-research-e2e`, resolved by name (never by env var, config
section, or --group flag) via `zotero_io.find_group_by_name`. See
BACKLOG.md's "live end-to-end SLR test" entry for why this must be a
group and not My Library (the personal library already carries live SLR
tags from a real review — importing into it would corrupt that record).
The Zotero Web API cannot create groups, so a missing group produces an
actionable error instead of trying to provision one.

Usage:
    uv run scripts/dev/mini_slr.py --stage all
    uv run scripts/dev/mini_slr.py --stage all --keep     # skip teardown
    uv run scripts/dev/mini_slr.py --stage search
    uv run scripts/dev/mini_slr.py --stage trim --run-id 20260812T140000Z
    uv run scripts/dev/mini_slr.py --stage teardown --run-id 20260812T140000Z

Stages (fixed order): search, trim, collection, import, sync, enrich,
audit, screen, code, export, verify, teardown.

Artefacts land under output/e2e/<run-id>/ (gitignored), which doubles as
the project root every pipeline-script subprocess runs in (cwd=<run-id>
dir) — so each script's own default output paths (analysis/raw/,
screening/, search_metadata.json, ...) land exactly where
templates/test_systematic_review.py expects them, with zero path
overrides needed for most stages.

Cost note: real per-token spend isn't visible to this driver — the
shared LLM provider layer (`core.llm_provider.LLMProvider.generate`)
returns plain text, not a token-usage object, and none of the pipeline
scripts log usage. Rather than claim a number this script can't measure,
it reports LLM *call counts* per stage (an honest proxy — abstract
screening cost scales with item count, full-text coding is capped via
FULLTEXT_LIMIT below) instead of a token tally.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import importlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPTS_ROOT = REPO_ROOT / "scripts"
PIPELINES_DIR = SCRIPTS_ROOT / "pipelines"
TEMPLATES_DIR = REPO_ROOT / "templates"
E2E_FIXTURES_DIR = REPO_ROOT / "tests" / "live" / "e2e"
OUTPUT_E2E_ROOT = REPO_ROOT / "output" / "e2e"

for _p in (str(SCRIPTS_ROOT), str(PIPELINES_DIR)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import zotero_io  # noqa: E402
from core.config_loader import require  # noqa: E402
from log_schemas import fulltext_screening_fields  # noqa: E402

GROUP_NAME = "academic-research-e2e"

# Sonnet full-text coding is the dominant per-run cost; cap it regardless
# of how many items pass abstract screening (see module docstring's cost
# note and BACKLOG.md's Cost/runtime section).
FULLTEXT_LIMIT = 3

SYNC_TIMEOUT_S = 180
SYNC_INTERVAL_S = 3

TEST_TEMPLATES = (
    "test_common.py",
    "test_citations.py",
    "test_empirical_integrity.py",
    "test_systematic_review.py",
)


@dataclass
class Ctx:
    run_dir: Path
    group_id: str
    state: dict


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


def _latest_run_id() -> str | None:
    if not OUTPUT_E2E_ROOT.is_dir():
        return None
    candidates = sorted(
        p.name for p in OUTPUT_E2E_ROOT.iterdir()
        if p.is_dir() and (p / ".mini_slr_state.json").exists()
    )
    return candidates[-1] if candidates else None


def _state_path(run_dir: Path) -> Path:
    return run_dir / ".mini_slr_state.json"


def _load_state(run_dir: Path) -> dict:
    p = _state_path(run_dir)
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    return {}


def _save_state(run_dir: Path, state: dict) -> None:
    _state_path(run_dir).write_text(
        json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8",
    )


def _run(cmd: list[str], *, cwd: Path, env: dict[str, str] | None = None,
          check: bool = True) -> subprocess.CompletedProcess:
    print(f"$ (cwd={cwd}) {' '.join(cmd)}", flush=True)
    full_env = os.environ.copy()
    if env:
        full_env.update(env)
    result = subprocess.run(cmd, cwd=cwd, env=full_env)
    if check and result.returncode != 0:
        sys.exit(
            f"ERROR: stage command failed (exit {result.returncode}): "
            f"{' '.join(cmd)}"
        )
    return result


def _uv_run(script: Path, args: list[str], *, cwd: Path,
            env: dict[str, str] | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
    return _run(["uv", "run", str(script), *args], cwd=cwd, env=env, check=check)


def _require_state(ctx: Ctx, key: str, earlier_stage: str) -> object:
    if key not in ctx.state:
        sys.exit(
            f"ERROR: missing {key!r} in run state — run --stage "
            f"{earlier_stage} first (or --stage all)."
        )
    return ctx.state[key]


def _resolve_group() -> tuple[str, str]:
    api_key = require("zotero", "api_key", env="ZOTERO_API_KEY")
    try:
        group = zotero_io.find_group_by_name(GROUP_NAME, api_key=api_key)
    except ValueError as e:
        sys.exit(f"ERROR: {e}")
    if group is None:
        sys.exit(
            f"ERROR: no Zotero group named {GROUP_NAME!r} is accessible "
            f"with the configured ZOTERO_API_KEY.\n"
            "The Zotero Web API cannot create groups, so this is a "
            "one-time manual step:\n"
            "  1. https://www.zotero.org/groups/new — create a group "
            f"named exactly {GROUP_NAME!r} (Private membership is fine).\n"
            "  2. Open Zotero Desktop and let it sync (Preferences > "
            "Sync, or just wait a minute) so the group appears locally.\n"
            "  3. Re-run this driver."
        )
    return str(group["id"]), group["name"]


def _local_client(group_id: str) -> zotero_io.ZoteroClient:
    return zotero_io.ZoteroClient(
        api_key=require("zotero", "api_key", env="ZOTERO_API_KEY"),
        group_id=group_id,
        prefer_local=True,
    )


def _cloud_client(group_id: str) -> zotero_io.ZoteroClient:
    return zotero_io.ZoteroClient.from_config(group_id=group_id)


def _tally(ctx: Ctx, stage: str, *, calls: int, model: str) -> None:
    tally = ctx.state.setdefault("llm_call_tally", {})
    tally[stage] = {"calls": calls, "model": model}
    print(f"  LLM calls this stage: {calls} ({model})", flush=True)


# ---------------------------------------------------------------------------
# Stages
# ---------------------------------------------------------------------------


def stage_search(ctx: Ctx) -> None:
    cfg_dst = ctx.run_dir / "search_config.py"
    if not cfg_dst.exists():
        shutil.copy2(E2E_FIXTURES_DIR / "search_config.py", cfg_dst)
    _uv_run(
        PIPELINES_DIR / "search.py",
        ["--config", "./search_config.py"],
        cwd=ctx.run_dir,
    )


def _recompute_search_run_marker(run_dir: Path) -> None:
    """Keep search_run.json / search_metadata.json consistent with the
    dedup CSV after `trim` shrinks it (search.py wrote them for the
    pre-trim corpus)."""
    csv_path = run_dir / "analysis" / "raw" / "search_results.csv"
    with csv_path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    dois = sorted(r["doi"].strip() for r in rows if r.get("doi", "").strip())
    doi_hash = hashlib.sha256("\n".join(dois).encode()).hexdigest()

    run_marker = run_dir / "search_run.json"
    marker = json.loads(run_marker.read_text(encoding="utf-8"))
    marker["unique_records"] = len(rows)
    marker["unique_dois"] = len(dois)
    marker["doi_sha256"] = doi_hash
    run_marker.write_text(
        json.dumps(marker, indent=2, ensure_ascii=False), encoding="utf-8",
    )

    meta_path = run_dir / "search_metadata.json"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["total_unique_records"] = len(rows)
    meta["records_without_doi"] = sum(
        1 for r in rows if not r.get("doi", "").strip()
    )
    meta_path.write_text(
        json.dumps(meta, indent=2, ensure_ascii=False), encoding="utf-8",
    )


def _normalise_issn(value: str) -> str:
    return value.replace("-", "").replace(" ", "").upper()


def _journal_of(row: dict, journals: dict[str, tuple[str, str]]) -> str | None:
    """Which configured journal a search row belongs to, or None.

    Matches on ISSN first — Zotero and the search APIs disagree about
    hyphenation, and a row may carry both print and electronic ISSNs in
    one comma-separated field — then falls back to the journal name,
    since not every database returns an ISSN on every row.
    """
    wanted = {_normalise_issn(k): k for k in journals}
    for part in (row.get("issn") or "").split(","):
        key = wanted.get(_normalise_issn(part))
        if key:
            return key

    name = (row.get("source") or "").strip().casefold()
    if name:
        for key, (_, journal_name) in journals.items():
            if name == journal_name.strip().casefold():
                return key
    return None


def _balanced_sample(
    rows: list[dict],
    journals: dict[str, tuple[str, str]],
    target_n: int,
) -> list[dict]:
    """Take `target_n` rows spread across all configured journals.

    `filter_search_results.py --top-n` sorts by year descending and
    keeps the first N. This corpus is a single closed year, so that sort
    is one giant tie and just preserves `search.py`'s dedup order — in
    the first live run all 8 trimmed rows were Small Business Economics
    (Springer), so the Elsevier and Wiley TDM routes were never
    exercised even though both keys were configured. The point of a
    three-publisher corpus is to exercise three publishers.

    Round-robin so the shortfall from a thin journal is absorbed by the
    others rather than truncating the sample. Ordering inside each
    journal is deterministic (year desc, then DOI) so reruns of the same
    corpus stay comparable.
    """
    def _sort_key(row: dict) -> tuple:
        year = row.get("year", "")
        return (-int(year) if year.isdigit() else 0, row.get("doi", ""))

    grouped: dict[str, list[dict]] = {key: [] for key in journals}
    unmatched: list[dict] = []
    for row in rows:
        key = _journal_of(row, journals)
        if key is None:
            unmatched.append(row)
        else:
            grouped[key].append(row)
    for key in grouped:
        grouped[key].sort(key=_sort_key)
    unmatched.sort(key=_sort_key)

    picked: list[dict] = []
    queues = [grouped[key] for key in journals]
    while len(picked) < target_n and any(queues):
        for queue in queues:
            if not queue:
                continue
            picked.append(queue.pop(0))
            if len(picked) >= target_n:
                break

    # Only top up from unmatched rows if the configured journals cannot
    # fill the quota — otherwise a stray row would displace the coverage
    # this sampling exists to guarantee.
    for row in unmatched:
        if len(picked) >= target_n:
            break
        picked.append(row)

    covered = sorted({
        key for key in journals
        if any(_journal_of(r, journals) == key for r in picked)
    })
    print(
        f"  trimmed to {len(picked)} rows across {len(covered)}/"
        f"{len(journals)} journals: {', '.join(covered) or '(none)'}",
        flush=True,
    )
    missing = [k for k in journals if k not in covered]
    if missing:
        # Not fatal: a journal can legitimately return nothing for a
        # single year. Say so loudly, because it means whichever
        # publisher route that journal exercises went untested.
        print(
            f"  WARN: no rows for {', '.join(missing)} — the PDF route for "
            f"{'that journal' if len(missing) == 1 else 'those journals'} "
            f"will not be exercised this run.",
            flush=True,
        )
    return picked


def stage_trim(ctx: Ctx, *, target_n: int = 8) -> None:
    csv_path = ctx.run_dir / "analysis" / "raw" / "search_results.csv"

    sys.path.insert(0, str(ctx.run_dir))
    try:
        import search_config
        importlib.reload(search_config)
        journals = dict(search_config.JOURNALS)
    finally:
        sys.path.remove(str(ctx.run_dir))

    with csv_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = list(reader)

    picked = _balanced_sample(rows, journals, target_n)

    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(picked)

    _recompute_search_run_marker(ctx.run_dir)


def stage_collection(ctx: Ctx) -> None:
    if ctx.state.get("collection_key"):
        print(f"  reusing existing collection {ctx.state['collection_key']}",
              flush=True)
        return
    zot = _cloud_client(ctx.group_id)
    name = f"e2e-{ctx.state['run_id']}"
    key = zot.create_collection(name)
    ctx.state["collection_key"] = key
    ctx.state["collection_name"] = name
    print(f"  created collection {key!r} ({name!r})", flush=True)


def stage_import(ctx: Ctx) -> None:
    coll_key = _require_state(ctx, "collection_key", "collection")
    csv_path = ctx.run_dir / "analysis" / "raw" / "search_results.csv"
    keys_out = ctx.run_dir / ".created_item_keys.txt"
    _uv_run(
        PIPELINES_DIR / "import_to_zotero.py",
        ["--group", ctx.group_id, "--collection", str(coll_key),
         "--input", str(csv_path), "--created-keys-out", str(keys_out)],
        cwd=ctx.run_dir,
    )
    new_keys = []
    if keys_out.exists():
        new_keys = [
            line.strip() for line in keys_out.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    existing = set(ctx.state.get("created_item_keys", []))
    ctx.state["created_item_keys"] = sorted(existing | set(new_keys))
    print(f"  {len(new_keys)} new item(s) created this pass "
          f"({len(ctx.state['created_item_keys'])} total tracked for teardown)",
          flush=True)


def stage_sync(ctx: Ctx) -> None:
    """Wait for the cloud writes `import` just made to reach the local
    Zotero client every downstream stage reads from (BACKLOG's
    "reads are local-only, no cloud fallback" finding)."""
    coll_key = _require_state(ctx, "collection_key", "collection")
    csv_path = ctx.run_dir / "analysis" / "raw" / "search_results.csv"
    with csv_path.open(newline="", encoding="utf-8") as f:
        expected = sum(1 for _ in csv.DictReader(f))

    zot_local = _local_client(ctx.group_id)
    deadline = time.monotonic() + SYNC_TIMEOUT_S
    items: list[dict] = []
    while True:
        try:
            items = zot_local.collection_items(str(coll_key), item_type="journalArticle")
        except Exception as e:  # noqa: BLE001
            print(f"  local read failed (will retry): {e}", flush=True)
            items = []
        if len(items) >= expected:
            break
        if time.monotonic() > deadline:
            sys.exit(
                f"ERROR: local Zotero sync timed out after {SYNC_TIMEOUT_S}s "
                f"({len(items)}/{expected} items visible locally in "
                f"collection {coll_key}). Is Zotero Desktop running and "
                "synced?"
            )
        print(f"  waiting for local sync... ({len(items)}/{expected})", flush=True)
        time.sleep(SYNC_INTERVAL_S)

    keys_path = ctx.run_dir / "keys" / "collection_items.keys"
    keys_path.parent.mkdir(parents=True, exist_ok=True)
    keys_path.write_text(
        "\n".join(it["key"] for it in items) + "\n", encoding="utf-8",
    )
    print(f"  synced: {len(items)} item(s) visible locally", flush=True)


def stage_enrich(ctx: Ctx) -> None:
    keys_path = ctx.run_dir / "keys" / "collection_items.keys"
    if not keys_path.exists():
        sys.exit("ERROR: keys/collection_items.keys not found — run --stage sync first.")
    common = ["--group", ctx.group_id, "--filter-keys-file", str(keys_path)]

    _uv_run(PIPELINES_DIR / "enrich_dois.py",
            [*common, "--all", "--no-prompt"], cwd=ctx.run_dir)
    _uv_run(PIPELINES_DIR / "enrich_abstracts.py", common, cwd=ctx.run_dir)
    _uv_run(PIPELINES_DIR / "enrich_pdfs.py",
            [*common, "--no-prompt"], cwd=ctx.run_dir)
    # Second pass: Wiley TDM only, for SEJ (1932-4391) — the default
    # cascade above doesn't include it. See BACKLOG.md's corpus note.
    _uv_run(PIPELINES_DIR / "enrich_pdfs.py",
            [*common, "--sources", "wiley", "--no-prompt"], cwd=ctx.run_dir)


def stage_audit(ctx: Ctx) -> None:
    out_path = ctx.run_dir / ".claude" / "audit" / "audit.json"
    _uv_run(
        PIPELINES_DIR / "audit_zotero_library.py",
        ["--group", ctx.group_id, "--output", str(out_path)],
        cwd=ctx.run_dir,
    )


def stage_screen(ctx: Ctx) -> None:
    coll_key = _require_state(ctx, "collection_key", "collection")
    cfg_dst = ctx.run_dir / "screening_config.py"
    if not cfg_dst.exists():
        shutil.copy2(E2E_FIXTURES_DIR / "screening_config.py", cfg_dst)

    search_csv = ctx.run_dir / "analysis" / "raw" / "search_results.csv"
    _uv_run(
        PIPELINES_DIR / "abstract_screen.py",
        ["--group", ctx.group_id, "--collection", str(coll_key),
         "--config", "./screening_config.py",
         "--search-csv", str(search_csv),
         "--workers", "4"],
        cwd=ctx.run_dir,
    )

    log_path = ctx.run_dir / "screening" / "abstract_screening.csv"
    n_screened = 0
    model = "?"
    if log_path.exists():
        with log_path.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        n_screened = len(rows)
        model = rows[0]["model"] if rows else "?"
    _tally(ctx, "screen", calls=n_screened, model=model)


def stage_code(ctx: Ctx) -> None:
    """Second sync wait (BACKLOG load-bearing finding): abstract_screen.py's
    tag writes go to the cloud; this driver reads locally like every
    pipeline script, so wait for `abstract:*` tags to sync down before
    computing which items pass to full-text coding."""
    coll_key = _require_state(ctx, "collection_key", "collection")
    keys_path = ctx.run_dir / "keys" / "collection_items.keys"
    if not keys_path.exists():
        sys.exit("ERROR: keys/collection_items.keys not found — run --stage sync first.")
    total_expected = len(
        [ln for ln in keys_path.read_text(encoding="utf-8").splitlines() if ln.strip()]
    )

    zot_local = _local_client(ctx.group_id)
    deadline = time.monotonic() + SYNC_TIMEOUT_S
    only_keys: list[str] = []
    while True:
        try:
            items = zot_local.collection_items(str(coll_key), item_type="journalArticle")
        except Exception as e:  # noqa: BLE001
            print(f"  local read failed (will retry): {e}", flush=True)
            items = []
        tagged = [
            it for it in items
            if any(t.get("tag", "").startswith("abstract:")
                   for t in it.get("data", {}).get("tags", []))
        ]
        if len(tagged) >= total_expected:
            only_keys = [
                it["key"] for it in tagged
                if any(t.get("tag") in ("abstract:include", "abstract:borderline")
                       for t in it["data"]["tags"])
            ]
            break
        if time.monotonic() > deadline:
            sys.exit(
                f"ERROR: local sync of abstract:* tags timed out after "
                f"{SYNC_TIMEOUT_S}s ({len(tagged)}/{total_expected})."
            )
        print(f"  waiting for local sync of abstract:* tags... "
              f"({len(tagged)}/{total_expected})", flush=True)
        time.sleep(SYNC_INTERVAL_S)

    print(f"  {len(only_keys)} item(s) passed abstract screening "
          f"(include/borderline)", flush=True)

    fulltext_log = ctx.run_dir / "screening" / "fulltext_screening.csv"
    if not only_keys:
        # Nothing to code — still produce a header-only log so the
        # verify stage's artefact-existence check has something to find.
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "screening_config", ctx.run_dir / "screening_config.py",
        )
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        field_names = [f["name"] for f in mod.FULLTEXT_CODING_FIELDS]
        fulltext_log.parent.mkdir(parents=True, exist_ok=True)
        with fulltext_log.open("w", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=fulltext_screening_fields(field_names)).writeheader()
        _tally(ctx, "code", calls=0, model="n/a")
        return

    _uv_run(
        PIPELINES_DIR / "fulltext_code.py",
        ["--group", ctx.group_id, "--collection", str(coll_key),
         "--config", "./screening_config.py",
         "--pdf-dir", "output/pdf_cache",
         "--only-keys", ",".join(only_keys),
         "--limit", str(FULLTEXT_LIMIT),
         "--workers", "3"],
        cwd=ctx.run_dir,
    )

    n_coded = 0
    model = "?"
    if fulltext_log.exists():
        with fulltext_log.open(newline="", encoding="utf-8") as f:
            rows = list(csv.DictReader(f))
        n_coded = len(rows)
        model = rows[0]["model"] if rows else "?"
    _tally(ctx, "code", calls=n_coded, model=model)


def _expected_fulltext_tags(ctx: Ctx) -> int:
    """How many items `fulltext_code.py` should have tagged `fulltext:*`.

    Only include/exclude get a stage tag — error and no_pdf stay untagged
    so a re-run picks them up — so the expected count is the number of
    include/exclude decisions in the log, not the number of coded items.
    Last row per item wins, matching how the verify stage reads the log.
    """
    log_path = ctx.run_dir / "screening" / "fulltext_screening.csv"
    if not log_path.is_file():
        return 0
    with log_path.open(newline="", encoding="utf-8") as f:
        last = {r["item_key"]: r for r in csv.DictReader(f) if r.get("item_key")}
    return sum(1 for r in last.values() if r.get("decision") in ("include", "exclude"))


def stage_export(ctx: Ctx) -> None:
    """Third sync wait, same cause as `stage_code`'s: `fulltext_code.py`
    writes stage tags to the cloud, but `export_coded_includes.py` selects
    on those tags through a client that prefers the local Zotero server.
    Exporting straight after coding races that sync and silently writes a
    short `coded_papers.csv` — 0 rows against 1 real include, in the run
    that prompted this."""
    coll_key = _require_state(ctx, "collection_key", "collection")
    total_expected = _expected_fulltext_tags(ctx)

    if total_expected:
        zot_local = _local_client(ctx.group_id)
        deadline = time.monotonic() + SYNC_TIMEOUT_S
        while True:
            try:
                items = zot_local.collection_items(
                    str(coll_key), item_type="journalArticle")
            except Exception as e:  # noqa: BLE001
                print(f"  local read failed (will retry): {e}", flush=True)
                items = []
            tagged = [
                it for it in items
                if any(t.get("tag", "").startswith("fulltext:")
                       for t in it.get("data", {}).get("tags", []))
            ]
            if len(tagged) >= total_expected:
                break
            if time.monotonic() > deadline:
                sys.exit(
                    f"ERROR: local sync of fulltext:* tags timed out after "
                    f"{SYNC_TIMEOUT_S}s ({len(tagged)}/{total_expected})."
                )
            print(f"  waiting for local sync of fulltext:* tags... "
                  f"({len(tagged)}/{total_expected})", flush=True)
            time.sleep(SYNC_INTERVAL_S)

    out_path = ctx.run_dir / "analysis" / "results" / "coded_papers.csv"
    _uv_run(
        PIPELINES_DIR / "export_coded_includes.py",
        ["--group", ctx.group_id, "--collection", str(coll_key),
         "--out", str(out_path)],
        cwd=ctx.run_dir,
    )


def _verify_fetch_attach_invariant(ctx: Ctx) -> None:
    """Every item the PDF run-log calls attached must really have a PDF.

    The check that would have caught the "48 lost PDFs" incident: the
    browser cascade downloaded 68 Sage PDFs and attached 20, and the run
    reported success. The 48 failures were visible only as
    `upload_failed` rows nobody read, while `pdf_map()` — the thing that
    decides whether an item still needs work — disagreed with the log
    entirely.

    Compares the two authorities directly, so a log that claims success
    while Zotero holds nothing is a hard failure rather than a number
    the operator has to notice.

    Reads the *cloud* library deliberately. `ZoteroClient` prefers the
    local Zotero server for reads, but uploads go to the Web API, so a
    local read races Zotero Desktop's sync and would fail this check on
    timing rather than on substance.
    """
    log_path = ctx.run_dir / "output" / "pdf_attach_log.csv"
    if not log_path.is_file():
        print("  (no pdf_attach_log.csv — nothing to cross-check)", flush=True)
        return

    sys.path.insert(0, str(PIPELINES_DIR))
    try:
        import pdf_run_report
        import shared_orchestrators
        from log_schemas import PDF_FETCH_FIELDS
    finally:
        sys.path.remove(str(PIPELINES_DIR))

    rows = pdf_run_report.latest_rows(
        shared_orchestrators.read_log_rows(str(log_path), PDF_FETCH_FIELDS)
    )
    claimed = {
        r["item_key"] for r in rows
        if r.get("status") in ("attached", "attached_via_connector")
        and r.get("item_key")
    }
    if not claimed:
        print("  (no items claimed as attached)", flush=True)
        return

    cloud_reader = zotero_io.ZoteroClient.from_config(
        group_id=ctx.group_id, prefer_local=False,
    )
    pdf_map = cloud_reader.pdf_map()
    missing = sorted(k for k in claimed if not pdf_map.get(k, (False, []))[0])
    if missing:
        raise SystemExit(
            f"fetch→attach invariant violated: {len(missing)} of "
            f"{len(claimed)} item(s) are logged as attached but carry no "
            f"real PDF in Zotero: {', '.join(missing[:10])}"
            + (" …" if len(missing) > 10 else "")
        )
    print(
        f"  fetch→attach invariant holds for {len(claimed)} attached item(s).",
        flush=True,
    )


def stage_verify(ctx: Ctx) -> None:
    _verify_fetch_attach_invariant(ctx)

    scripts_dir = ctx.run_dir / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)
    for name in TEST_TEMPLATES:
        shutil.copy2(TEMPLATES_DIR / name, scripts_dir / name)

    env = {
        "ZOTERO_GROUP": ctx.group_id,
        "ZOTERO_API_KEY": require("zotero", "api_key", env="ZOTERO_API_KEY"),
    }
    _uv_run(
        scripts_dir / "test_systematic_review.py", ["-v"],
        cwd=ctx.run_dir, env=env,
    )


def stage_teardown(ctx: Ctx) -> None:
    zot = _cloud_client(ctx.group_id)
    keys = ctx.state.get("created_item_keys", [])
    print(f"  deleting {len(keys)} item(s) this run created...", flush=True)
    failed = []
    for key in keys:
        try:
            ok = zot.delete_item(key)
            if not ok:
                failed.append(key)
        except Exception as e:  # noqa: BLE001
            print(f"  WARN: failed to delete {key}: {e}", flush=True)
            failed.append(key)
    if failed:
        print(f"  WARNING: {len(failed)} item(s) could not be deleted: "
              f"{failed}", flush=True)

    coll_key = ctx.state.get("collection_key")
    if coll_key:
        ok = zot.delete_collection(str(coll_key))
        print(f"  collection {coll_key} deleted: {ok}", flush=True)
    ctx.state["torn_down"] = True


STAGE_FUNCS = {
    "search": stage_search,
    "trim": stage_trim,
    "collection": stage_collection,
    "import": stage_import,
    "sync": stage_sync,
    "enrich": stage_enrich,
    "audit": stage_audit,
    "screen": stage_screen,
    "code": stage_code,
    "export": stage_export,
    "verify": stage_verify,
    "teardown": stage_teardown,
}
STAGE_ORDER = list(STAGE_FUNCS)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


STAGE_TAG_PREFIXES = ("abstract:", "fulltext:")


def _runs_owning(item_keys: set[str]) -> list[str]:
    """Run-ids whose teardown would remove `item_keys`, newest last.

    The tagged items carry no record of which run made them, but each
    run's state file lists what it created, so the intersection names the
    run to tear down. Runs already torn down are skipped.
    """
    owners: list[str] = []
    if not OUTPUT_E2E_ROOT.is_dir():
        return owners
    for run_dir in sorted(OUTPUT_E2E_ROOT.iterdir()):
        state_path = run_dir / ".mini_slr_state.json"
        if not state_path.is_file():
            continue
        try:
            state = json.loads(state_path.read_text(encoding="utf-8"))
        except Exception:  # noqa: BLE001
            continue
        if state.get("torn_down"):
            continue
        if set(state.get("created_item_keys", [])) & item_keys:
            owners.append(run_dir.name)
    return owners


def _preflight_clean_group(ctx: Ctx) -> None:
    """Refuse to start a new run in a group that still holds staged items.

    `import_to_zotero.py` deduplicates by DOI, so a second run against a
    dirty group creates no items at all — it re-uses the previous run's,
    stage tags and all. The damage is silent and downstream:
    `abstract_screen.py` sees every item already tagged and screens
    nothing (writing no `abstract_screening.csv`), a stale
    `fulltext:include` inflates the export, and the operator learns about
    it five `verify` failures later, none of which name the real cause.
    Run 20260813T184419Z lost a full pipeline that way.

    Reads the *cloud* library deliberately, for the same reason
    `_verify_fetch_attach_invariant` does: teardown deletes through the
    Web API, so the cloud is clean the moment it finishes, while Zotero
    Desktop may still be showing the items locally. Checking the local
    view would block a genuinely clean run and tell the operator to tear
    down a run that is already gone — a loop with no exit.

    Fails open: a group that cannot be read is not evidence of a dirty
    one, and blocking a run on a transient Zotero blip would be worse
    than the race this guards. An empty answer still counts as clean.
    """
    try:
        reader = zotero_io.ZoteroClient.from_config(
            group_id=ctx.group_id, prefer_local=False,
        )
        items = reader.journal_articles()
    except Exception as e:  # noqa: BLE001
        print(f"  WARNING: could not pre-check the group for stale items "
              f"({e}); continuing.", flush=True)
        return

    dirty = {
        it.get("key", "")
        for it in items
        if any(t.get("tag", "").startswith(STAGE_TAG_PREFIXES)
               for t in it.get("data", {}).get("tags", []))
    } - {""}
    if not dirty:
        return

    owners = _runs_owning(dirty)
    if owners:
        how = "\n".join(
            f"  uv run {Path(__file__).resolve()} --stage teardown --run-id {r}"
            for r in owners
        )
        hint = f"Tear the earlier run(s) down first:\n{how}"
    else:
        # Items nothing claims — a run whose state file was deleted, or
        # hand-added items. Teardown cannot help; say so rather than
        # printing a command that would delete nothing.
        hint = (
            "No run's state file claims these items, so --stage teardown "
            "will not remove them. Delete them in Zotero (or use a "
            "different group) before re-running."
        )

    sys.exit(
        f"ERROR: group {ctx.group_id} already holds {len(dirty)} item(s) "
        f"tagged abstract:*/fulltext:* from an earlier run.\n"
        f"import_to_zotero.py deduplicates by DOI, so this run would "
        f"re-use those items along with their stale tags, screen nothing, "
        f"and fail verify for reasons that point away from the cause.\n"
        f"{hint}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--stage", required=True, choices=[*STAGE_ORDER, "all"],
        help="Which stage to run, or 'all' to run every stage in order.",
    )
    parser.add_argument(
        "--run-id", default="",
        help="Resume/target a specific run under output/e2e/<run-id>/. "
             "Omit to start a fresh run (--stage search / all) or resume "
             "the most recently touched run (any other stage).",
    )
    parser.add_argument(
        "--keep", action="store_true",
        help="With --stage all: skip teardown, leaving the run's Zotero "
             "items and collection in place for inspection.",
    )
    args = parser.parse_args()

    fresh = args.stage in ("search", "all")
    if args.run_id:
        run_id = args.run_id
    elif fresh:
        run_id = _new_run_id()
    else:
        found = _latest_run_id()
        if not found:
            sys.exit(
                "ERROR: no existing e2e run found under output/e2e/. Pass "
                "--run-id, or start a run with --stage search or --stage all."
            )
        run_id = found

    run_dir = OUTPUT_E2E_ROOT / run_id
    run_dir.mkdir(parents=True, exist_ok=True)

    state = _load_state(run_dir)
    state.setdefault("run_id", run_id)
    if "group_id" not in state:
        group_id, group_name = _resolve_group()
        state["group_id"] = group_id
        state["group_name"] = group_name
        _save_state(run_dir, state)

    ctx = Ctx(run_dir=run_dir, group_id=state["group_id"], state=state)
    print(
        f"=== mini_slr run {run_id} "
        f"(group {ctx.group_id} {state.get('group_name', '')!r}) ===",
        flush=True,
    )

    # Only a brand-new run can be contaminated by an earlier one; a resumed
    # run is *supposed* to find its own stage tags in the group.
    if fresh and not args.run_id:
        _preflight_clean_group(ctx)

    stages = STAGE_ORDER if args.stage == "all" else [args.stage]
    stages_done = set(state.get("stages_completed", []))

    for stage in stages:
        if stage == "teardown" and args.stage == "all" and args.keep:
            print("\n--keep set: skipping teardown.", flush=True)
            continue
        if args.stage == "all" and stage in stages_done:
            print(f"\n--- {stage}: already completed, skipping (resume) ---",
                  flush=True)
            continue

        print(f"\n--- stage: {stage} ---", flush=True)
        t0 = time.monotonic()
        STAGE_FUNCS[stage](ctx)
        elapsed = time.monotonic() - t0
        print(f"--- stage {stage} done in {elapsed:.1f}s ---", flush=True)

        stages_done.add(stage)
        state["stages_completed"] = sorted(stages_done)
        _save_state(run_dir, state)

    tally = state.get("llm_call_tally", {})
    if tally:
        print("\nLLM call tally (proxy for spend — see module docstring):",
              flush=True)
        for stage_name, info in tally.items():
            print(f"  {stage_name:<8} {info['calls']:>3} call(s)  "
                  f"({info['model']})", flush=True)

    print(f"\n=== run {run_id} complete === (run dir: {run_dir})", flush=True)
    return 0


if __name__ == "__main__":
    # Windows takes stdout's encoding from the locale when output is
    # redirected — normally cp1252, which cannot encode the arrows, em
    # dashes and rules printed below. See scripts/core/console.py.
    from core.console import enable_utf8_output
    enable_utf8_output()
    sys.exit(main())
