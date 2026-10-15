"""Manual download mode: the user downloads, the plugin opens and attaches.

`enrich_pdfs.py --manual next` runs one cycle:

1. **Open the next batch** (25 by default; `--batch-size` /
   `[pdf] manual_batch_size`) in the user's default browser through the
   stdlib `webbrowser` module — not Playwright, so the user's own logins,
   VPN and extensions apply. Each tab is the active library's resolver
   route (EZproxy prefix kept) or else `https://doi.org/<doi>`. When a
   route needs an interactive sign-in (`needs_interactive_login`) and no
   sign-in tab was opened in the last `LOGIN_FRESH_S`, only that one tab
   opens; the rest follow on the next call.
2. **Collect the previous batch**: PDFs that appeared in the Downloads
   folder since that batch was opened (a name/size/mtime snapshot is
   stored with the batch) move to `output/pdf_staging/batch-N/`.
   Unfinished downloads (`.crdownload`, `.part`, …) stay for the next
   cycle.
3. **Match and attach** every file in staging not yet settled, against
   every item still without a PDF — not only the current batch, so a late
   download lands correctly. Order: structural check → DOI on the first
   page → title on the first page → file-name hints (Elsevier
   `1-s2.0-<PII>-main.pdf`, DOI-shaped names). A byte-identical copy
   (`file (1).pdf`) is skipped by hash. A file that matches nothing, or
   more than one item, or whose signals disagree, is **never attached**:
   it is listed, written to `output/pdf_staging/unmatched.json`, and
   waits for `--manual assign --assign-file F --assign-item KEY` (or
   `--manual ignore --assign-file F`).

`--manual collect` does steps 2–3 without opening anything.

No terminal interaction and no background watcher: an agent runs the
next command when the user says "done" in chat. State lives in
`.claude/pdf/manual_state.json`, so cycles resume across sessions.

Pure orchestration over callbacks: `enrich_pdfs` supplies the Zotero
items, the attach path (`_attach_and_log`, so the `[pdf] min_version`
gate and the run log apply unchanged) and the resolver lookup. Nothing
here talks to the network or to Zotero directly.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import time
import webbrowser
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from pathlib import Path

import doi_utils
from fetchers import _pdf_validate, _title_match, pdf_version
from fetchers.resolvers.base import needs_interactive_login

#: Global tag (a fact about the attachment, outside `TAG_PREFIX`).
MANUAL_TAG = "pdf:manual"
#: `source` column of the run log and the version sidecar.
SOURCE = "manual"

DEFAULT_BATCH_SIZE = 25
STATE_PATH = Path(".claude") / "pdf" / "manual_state.json"
STAGING_DIR = Path("output") / "pdf_staging"
UNMATCHED_REPORT = STAGING_DIR / "unmatched.json"

#: Suffixes browsers give a download still in progress.
PARTIAL_SUFFIXES = (".crdownload", ".part", ".partial", ".download", ".opdownload")

#: A sign-in tab opened this recently is taken to have left a live
#: session, so the next batch opens in full. EZproxy sessions commonly
#: last a working day; four hours is the conservative end.
LOGIN_FRESH_S = 4 * 3600

#: Pause between tabs: some browsers drop tabs opened in a tight loop.
TAB_DELAY_S = 0.3

#: Pseudo item key namespace for `pdf_text_cache` (keyed by item).
_TEXT_KEY_PREFIX = "manual-"

#: How many folded title characters must appear on the first page. Long
#: enough that a short generic title ("Introduction") cannot match.
_TITLE_MIN = 20
_TITLE_NEEDLE = 60

# File statuses. Settled ones are never looked at again; open ones are
# re-matched every cycle (the open-item set changes as items get PDFs).
ATTACHED = "attached"
DUPLICATE = "duplicate"
REJECTED = "rejected"
BELOW_POLICY = "below_policy"
IGNORED = "ignored"
NOT_NEEDED = "not_needed"
UNMATCHED = "unmatched"
AMBIGUOUS = "ambiguous"
FAILED = "failed"
SETTLED = frozenset({ATTACHED, DUPLICATE, REJECTED, BELOW_POLICY, IGNORED, NOT_NEEDED})

#: Audit retry sets whose items are put at the front of the queue: each
#: is an item a person is known to be the next step for. Items denied
#: automated download (`retry.consent_denied`, build step 5) are here: a
#: "deny" falls through to this mode, never to nothing. So are the items
#: an unattended run deferred for a person (`retry.deferred`, step 6:
#: DEFERRED_NEEDS_USER) — the run's summary hands them to this mode.
#: `retry.consent_needed` is deliberately absent — those wait for an
#: answer, and an `allow` sends them back to the browser pass.
PRIORITY_RETRY_SETS: tuple[str, ...] = (
    "retry.manual", "retry.below_policy", "retry.consent_denied",
    "retry.deferred",
)

#: Run-log statuses whose items also go to the front of the queue.
PRIORITY_LOG_STATUSES = frozenset({
    "skipped_below_policy", "skipped_consent_denied", "deferred_needs_user",
})


# ---------------------------------------------------------------------------
# Items
# ---------------------------------------------------------------------------

@dataclass
class Item:
    key: str
    doi: str
    title: str
    data: dict = field(default_factory=dict)

    @property
    def doi_key(self) -> str:
        return doi_utils.doi_key(self.doi) if self.doi else ""


def to_item(raw: dict) -> Item:
    data = raw.get("data", {}) or {}
    return Item(
        key=raw.get("key", ""),
        doi=(data.get("DOI") or "").strip(),
        title=(data.get("title") or "").strip(),
        data=data,
    )


def priority_keys(audit_stem: str, log_csv: str = "") -> set[str]:
    """Keys from `PRIORITY_RETRY_SETS` key files and from run-log rows
    with a `PRIORITY_LOG_STATUSES` status."""
    keys: set[str] = set()
    for label in PRIORITY_RETRY_SETS:
        path = Path(f"{audit_stem}.{label}.keys")
        if path.is_file():
            keys |= {ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()
                     if ln.strip()}
    if log_csv and os.path.isfile(log_csv):
        import csv
        with open(log_csv, newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                if (row.get("status") or "") in PRIORITY_LOG_STATUSES:
                    keys.add((row.get("item_key") or "").strip())
    keys.discard("")
    return keys


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def load_state(path: Path = STATE_PATH) -> dict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    state.setdefault("version", 1)
    state.setdefault("library", "")
    state.setdefault("batches", [])
    state.setdefault("items", {})
    state.setdefault("files", {})
    state.setdefault("login_prompted_at", 0.0)
    return state


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)


def opened_keys(state: dict) -> set[str]:
    return {k for b in state["batches"] for k in b.get("keys", [])}


# ---------------------------------------------------------------------------
# Downloads folder
# ---------------------------------------------------------------------------

def is_partial(name: str) -> bool:
    return name.lower().endswith(PARTIAL_SUFFIXES)


def snapshot(folder: Path) -> dict[str, list]:
    """{name: [size, mtime]} of the files directly in `folder`."""
    snap: dict[str, list] = {}
    try:
        entries = list(os.scandir(folder))
    except OSError:
        return snap
    for entry in entries:
        try:
            if entry.is_file():
                st = entry.stat()
                snap[entry.name] = [st.st_size, st.st_mtime]
        except OSError:
            continue
    return snap


def new_since(folder: Path, snap: dict) -> tuple[list[Path], list[str]]:
    """(new PDFs, names of unfinished downloads) since `snap`. A file is
    new when its name is not in the snapshot or its size/mtime changed."""
    pdfs: list[Path] = []
    partials: list[str] = []
    for name, meta in sorted(snapshot(folder).items()):
        if snap.get(name) == meta:
            continue
        if is_partial(name):
            partials.append(name)
        elif name.lower().endswith(".pdf"):
            pdfs.append(folder / name)
    return pdfs, partials


def _free_name(dest_dir: Path, name: str) -> Path:
    target = dest_dir / name
    stem, suffix = os.path.splitext(name)
    n = 1
    while target.exists():
        target = dest_dir / f"{stem}__{n}{suffix}"
        n += 1
    return target


def move_into(files: Iterable[Path], dest_dir: Path) -> list[Path]:
    dest_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for src in files:
        target = _free_name(dest_dir, src.name)
        try:
            shutil.move(str(src), str(target))
        except OSError as e:
            print(f"  could not move {src.name}: {e}", flush=True)
            continue
        moved.append(target)
    return moved


# ---------------------------------------------------------------------------
# Opening a batch
# ---------------------------------------------------------------------------

def doi_link(doi: str) -> str:
    return f"https://doi.org/{doi}"


RouteLookup = Callable[[str, dict], tuple]


def link_for(item: Item, route_lookup: RouteLookup | None) -> tuple[str, str]:
    """`(url, version)` for one item: the library route, else doi.org.

    The version is what a download from that page is (step 1's
    `for_connector_target` rule, which the Connector already uses for
    the same routes): published for a platform route or the DOI landing
    page, accepted for anything else, submitted on a preprint host.
    """
    url, target = None, None
    if route_lookup is not None:
        try:
            url, target = route_lookup(item.doi, item.data)
        except Exception as e:  # noqa: BLE001 — a failed ask falls open to doi.org
            print(f"  resolver lookup failed for {item.key}: {e}", flush=True)
            url, target = None, None
    if not url:
        url, target = doi_link(item.doi), None
    return url, pdf_version.for_connector_target(target, url, item.doi)


def _login_fresh(state: dict, now: float) -> bool:
    return now - float(state.get("login_prompted_at") or 0) < LOGIN_FRESH_S


def open_batch(
    state: dict,
    queue: list[Item],
    *,
    size: int,
    downloads: Path,
    route_lookup: RouteLookup | None,
    opener: Callable[[str], bool] | None = None,
    now: float | None = None,
) -> dict | None:
    """Open the next batch and record it in `state`. None when the queue
    is empty. The Downloads snapshot is taken before any tab opens."""
    if not queue or size < 1:
        return None
    opener = opener or webbrowser.open_new_tab
    now = time.time() if now is None else now
    picked = queue[:size]
    links = {it.key: link_for(it, route_lookup) for it in picked}

    login_first = None
    if not _login_fresh(state, now):
        login_first = next(
            (it for it in picked if needs_interactive_login(links[it.key][0])), None,
        )
    if login_first is not None:
        picked = [login_first]
        state["login_prompted_at"] = now

    n = max((b["n"] for b in state["batches"]), default=0) + 1
    batch = {
        "n": n,
        "opened_at": now,
        "keys": [it.key for it in picked],
        "links": {it.key: links[it.key][0] for it in picked},
        "snapshot": snapshot(downloads),
        "login_first": login_first is not None,
        "failed_to_open": [],
    }
    batch_dir = STAGING_DIR / f"batch-{n}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / "links.txt").write_text(
        "".join(f"{it.key}\t{links[it.key][0]}\t{it.title}\n" for it in picked),
        encoding="utf-8",
    )
    for i, it in enumerate(picked):
        url, version = links[it.key]
        state["items"][it.key] = {"link": url, "route_version": version, "batch": n}
        try:
            ok = opener(url)
        except Exception:  # noqa: BLE001 — no browser is a reportable outcome
            ok = False
        if ok is False:
            batch["failed_to_open"].append(it.key)
        if TAB_DELAY_S and i < len(picked) - 1:
            time.sleep(TAB_DELAY_S)
    state["batches"].append(batch)
    return batch


# ---------------------------------------------------------------------------
# Reading a downloaded file
# ---------------------------------------------------------------------------

_DOI_IN_TEXT_RE = re.compile(r"10\.\d{4,9}/[^\s\"'<>,;]+", re.IGNORECASE)
_PII_NAME_RE = re.compile(r"^1-s2\.0-(S[0-9X]{15,17})-main", re.IGNORECASE)
_PII_RE = re.compile(r"\b(S[0-9X]{4}-?[0-9X]{4}\(?\d{2}\)?[0-9X]{5}-?[0-9X])\b|pii/(S[0-9X]{15,17})",
                     re.IGNORECASE)

#: First-page phrases that mark an author manuscript / preprint. Checked
#: only to lower a version — a typeset PDF almost never says these, and
#: if one does, under-claiming is the safe direction.
_SUBMITTED_PHRASES = (
    "not peer reviewed", "not been peer reviewed", "not been peer-reviewed",
    "not peer-reviewed", "not certified by peer review", "this is a preprint",
    "preprint submitted to", "working paper series",
)
_ACCEPTED_PHRASES = (
    "accepted manuscript", "author accepted", "accepted version",
    "author's accepted", "authors accepted", "not the version of record",
    "not the final published version", "has not been through the copyediting",
    "accepted article", "post-print", "postprint", "author manuscript",
)
_ARXIV_BANNER_RE = re.compile(r"arxiv:\d{4}\.\d{4,5}(v\d+)?", re.IGNORECASE)


def file_hash(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def default_text_extractor(path: Path, digest: str) -> str:
    """The file's text via `pdf_text_cache` (pdftotext). '' when it cannot
    be read — matching then falls back to file-name hints."""
    import pdf_text_cache

    try:
        return pdf_text_cache.get_text(_TEXT_KEY_PREFIX + digest[:16], path)
    except Exception as e:  # noqa: BLE001
        print(f"  (no text from {path.name}: {str(e)[:80]})", flush=True)
        return ""


def first_page(text: str, pages: int = 1) -> str:
    return "\f".join(text.split("\f")[:pages])


def dois_in_text(text: str) -> set[str]:
    found = set()
    for m in _DOI_IN_TEXT_RE.finditer(text or ""):
        raw = m.group(0).rstrip(".)]}")
        key = doi_utils.doi_key(raw)
        if key:
            found.add(key)
    return found


def content_version(page_text: str) -> str | None:
    """A version the first page itself declares, or None."""
    low = " ".join((page_text or "").lower().split())
    if any(p in low for p in _SUBMITTED_PHRASES) or _ARXIV_BANNER_RE.search(low):
        return pdf_version.SUBMITTED
    if any(p in low for p in _ACCEPTED_PHRASES):
        return pdf_version.ACCEPTED
    return None


def lower_version(*versions: str | None) -> str:
    """The lowest of the given versions (published > accepted > submitted)."""
    known = [v for v in versions if v in pdf_version.VERSIONS]
    if not known:
        return pdf_version.ACCEPTED
    return max(known, key=pdf_version.VERSIONS.index)


def _norm_pii(raw: str) -> str:
    return re.sub(r"[^0-9A-Z]", "", raw.upper())


def pii_from_filename(name: str) -> str:
    m = _PII_NAME_RE.match(name)
    return _norm_pii(m.group(1)) if m else ""


def item_piis(item: Item, extra_urls: Iterable[str] = ()) -> set[str]:
    """Elsevier PIIs named in the item's metadata (url, extra) or in
    already-cached resolution URLs (Crossref's linkinghub URL)."""
    texts = [str(item.data.get(k) or "") for k in ("url", "extra")] + list(extra_urls)
    piis = set()
    for text in texts:
        for m in _PII_RE.finditer(text):
            raw = m.group(1) or m.group(2)
            if raw:
                piis.add(_norm_pii(raw))
    return piis


def _name_fold(name: str) -> str:
    stem = name[:-4] if name.lower().endswith(".pdf") else name
    return re.sub(r"[^a-z0-9]", "", stem.lower())


def doi_from_filename(name: str, items: Iterable[Item]) -> set[str]:
    """Keys of items whose DOI (or DOI suffix, ≥ 6 characters) the file
    name spells, with `/` written as `_`, `-` or nothing."""
    folded = _name_fold(name)
    if not folded:
        return set()
    hits = set()
    for it in items:
        if not it.doi_key:
            continue
        full = re.sub(r"[^a-z0-9]", "", it.doi_key)
        suffix = re.sub(r"[^a-z0-9]", "", it.doi_key.split("/", 1)[-1])
        if folded == full or (len(suffix) >= 6 and folded == suffix):
            hits.add(it.key)
    return hits


# ---------------------------------------------------------------------------
# Matching
# ---------------------------------------------------------------------------

@dataclass
class Match:
    key: str | None
    status: str          # ATTACHED-to-be ("matched"), UNMATCHED, AMBIGUOUS, NOT_NEEDED
    reason: str
    candidates: list[str] = field(default_factory=list)


def match_file(
    name: str,
    page_text: str,
    open_items: list[Item],
    closed_items: list[Item],
    piis: dict[str, set[str]],
) -> Match:
    """Which open item `name` is, from its first page and its name.

    Signals in order: DOI on the page, title on the page, file-name hints
    (Elsevier PII, DOI-shaped name). The first signal naming exactly one
    item decides — unless a later signal names exactly one *other* item,
    which makes the file ambiguous. Nothing is ever guessed.
    """
    by_doi = {it.doi_key: it.key for it in open_items if it.doi_key}
    closed_by_doi = {it.doi_key: it.key for it in closed_items if it.doi_key}
    page_dois = dois_in_text(page_text)

    signals: list[tuple[str, set[str]]] = []
    signals.append(("DOI on the first page", {by_doi[d] for d in page_dois if d in by_doi}))
    folded = _title_match.fold(page_text)
    title_hits = set()
    if folded:
        for it in open_items:
            needle = _title_match.fold(it.title)[:_TITLE_NEEDLE]
            if len(needle) >= _TITLE_MIN and needle in folded:
                title_hits.add(it.key)
    signals.append(("title on the first page", title_hits))
    name_hits = set()
    pii = pii_from_filename(name)
    if pii:
        name_hits |= {k for k, s in piis.items() if pii in s}
    name_hits |= doi_from_filename(name, open_items)
    signals.append(("file name", name_hits))

    decided: tuple[str, str] | None = None
    candidates: set[str] = set()
    for label, hits in signals:
        candidates |= hits
        if not hits:
            continue
        if decided is None:
            if len(hits) == 1:
                decided = (next(iter(hits)), label)
            continue
        if len(hits) == 1 and decided[0] not in hits:
            return Match(None, AMBIGUOUS,
                         f"{decided[1]} names {decided[0]} but {label} names "
                         f"{next(iter(hits))}", sorted(candidates))

    closed_hits = {closed_by_doi[d] for d in page_dois if d in closed_by_doi}
    if decided is not None:
        key, label = decided
        if label != "DOI on the first page" and closed_hits:
            return Match(None, AMBIGUOUS,
                         f"{label} names {key} but the page carries the DOI of "
                         f"{', '.join(sorted(closed_hits))}",
                         sorted(candidates | closed_hits))
        return Match(key, "matched", f"by {label}", [key])
    if candidates:
        return Match(None, AMBIGUOUS,
                     f"{len(candidates)} items fit equally", sorted(candidates))
    if closed_hits:
        return Match(None, NOT_NEEDED,
                     f"the page carries the DOI of {', '.join(sorted(closed_hits))}, "
                     f"which already has a PDF", sorted(closed_hits))
    reason = "no DOI, title or file-name hint matches an item without a PDF"
    if not page_text.strip():
        reason += " (no text could be read from the file)"
    return Match(None, UNMATCHED, reason)


# ---------------------------------------------------------------------------
# One cycle
# ---------------------------------------------------------------------------

#: attach(item, pdf_path, version) -> "attached" | "below_policy" | "failed"
AttachFn = Callable[[Item, Path], str]


@dataclass
class Context:
    """What a cycle needs from `enrich_pdfs`."""

    library: str
    scope_items: list[dict]
    has_pdf: Callable[[str], bool]
    attach: AttachFn
    downloads: Path
    batch_size: int = DEFAULT_BATCH_SIZE
    route_lookup: RouteLookup | None = None
    priority: set[str] = field(default_factory=set)
    #: doi -> cached resolution URL (Crossref), for Elsevier PIIs.
    cached_url: Callable[[str], str] = lambda doi: ""
    text_extractor: Callable[[Path, str], str] = default_text_extractor
    opener: Callable[[str], bool] | None = None
    dry_run: bool = False
    state_path: Path = STATE_PATH


@dataclass
class Summary:
    opened: int = 0
    batch: int | None = None
    login_first: bool = False
    failed_to_open: list[str] = field(default_factory=list)
    collected: list[str] = field(default_factory=list)
    in_progress: list[str] = field(default_factory=list)
    attached: list[tuple[str, str]] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    rejected: list[tuple[str, str]] = field(default_factory=list)
    below_policy: list[tuple[str, str]] = field(default_factory=list)
    not_needed: list[tuple[str, str]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    unmatched: list[dict] = field(default_factory=list)
    remaining: int = 0
    unopened: int = 0
    queue_empty: bool = False


def _split_items(ctx: Context) -> tuple[list[Item], list[Item]]:
    items = [to_item(r) for r in ctx.scope_items]
    open_items = [it for it in items if not ctx.has_pdf(it.key)]
    closed = [it for it in items if ctx.has_pdf(it.key)]
    return open_items, closed


def build_queue(ctx: Context, state: dict, open_items: list[Item]) -> list[Item]:
    """Items to open, priority sets first, each opened at most once."""
    done = opened_keys(state)
    pending = [it for it in open_items if it.doi and it.key not in done]
    return ([it for it in pending if it.key in ctx.priority]
            + [it for it in pending if it.key not in ctx.priority])


def _check_library(state: dict, library: str) -> str | None:
    if state["library"] and state["library"] != library:
        return (f"{STATE_PATH} belongs to library {state['library']}, not "
                f"{library}. Finish that library's cycle (or move the file "
                f"aside) before starting another.")
    state["library"] = library
    return None


def _rel(path: Path) -> str:
    """State key for a file: project-relative POSIX path when possible."""
    if not path.is_absolute():
        return path.as_posix()
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return str(path)


def _version_for(state: dict, item: Item, page_text: str) -> str:
    route = state["items"].get(item.key, {}).get("route_version")
    # Never opened by this mode: a file of unknown provenance.
    base = route if route in pdf_version.VERSIONS else pdf_version.ACCEPTED
    return lower_version(base, content_version(page_text))


def _settle(
    ctx: Context, state: dict, summary: Summary, entry: dict, path: Path,
    item: Item, page_text: str,
) -> None:
    version = _version_for(state, item, page_text)
    if ctx.dry_run:
        print(f"  {path.name} → {item.key} ({version}; dry run, not attached)",
              flush=True)
        summary.attached.append((path.name, item.key))
        return
    pdf_version.record(path, version, source=SOURCE)
    print(f"  {path.name} → {item.key} ({version}) ", end="", flush=True)
    outcome = ctx.attach(item, path)
    entry["item_key"] = item.key
    if outcome == ATTACHED:
        entry["status"] = ATTACHED
        summary.attached.append((path.name, item.key))
    elif outcome == BELOW_POLICY:
        entry["status"] = BELOW_POLICY
        summary.below_policy.append((path.name, item.key))
    else:
        entry["status"] = FAILED
        summary.failed.append(path.name)


def process_staging(
    ctx: Context, state: dict, summary: Summary, extra_files: Iterable[Path] = (),
) -> None:
    """Match and attach every unsettled file under the staging folder."""
    open_items, closed = _split_items(ctx)
    open_by_key = {it.key: it for it in open_items}
    piis = {
        it.key: item_piis(it, [ctx.cached_url(it.doi)] if it.doi else [])
        for it in open_items
    }
    seen_hashes = {
        e["sha256"]: rel for rel, e in state["files"].items()
        if e.get("sha256") and e.get("status") != IGNORED
    }
    files = sorted(p for p in STAGING_DIR.glob("batch-*/*.pdf") if p.is_file())
    files += list(extra_files)
    for path in files:
        rel = _rel(path)
        entry = state["files"].get(rel)
        if entry and entry.get("status") in SETTLED:
            continue
        digest = file_hash(path)
        if entry is None:
            entry = {"sha256": digest, "status": UNMATCHED}
            first = seen_hashes.get(digest)
            if first and first != rel:
                entry.update(status=DUPLICATE, reason=f"same bytes as {first}")
                if not ctx.dry_run:
                    state["files"][rel] = entry
                summary.duplicates.append(path.name)
                continue
            seen_hashes[digest] = rel
            if not ctx.dry_run:
                state["files"][rel] = entry
        defect = _pdf_validate.file_defect(path)
        if defect:
            entry.update(status=REJECTED, reason=defect)
            summary.rejected.append((path.name, defect))
            continue
        page = first_page(ctx.text_extractor(path, digest))
        m = match_file(path.name, page, open_items, closed, piis)
        if m.key is None:
            entry.update(status=m.status, reason=m.reason, candidates=m.candidates)
            if m.status == NOT_NEEDED:
                summary.not_needed.append((path.name, m.reason))
            else:
                summary.unmatched.append(_unmatched_row(rel, path, m, ctx))
            continue
        item = open_by_key[m.key]
        entry["reason"] = m.reason
        _settle(ctx, state, summary, entry, path, item, page)
        if entry.get("status") == ATTACHED or ctx.dry_run:
            # Settled: no second file may claim it this cycle.
            open_items = [it for it in open_items if it.key != item.key]
            closed.append(item)
            open_by_key.pop(item.key, None)


def _unmatched_row(rel: str, path: Path, m: Match, ctx: Context) -> dict:
    titles = {to_item(r).key: to_item(r) for r in ctx.scope_items}
    return {
        "file": rel,
        "name": path.name,
        "status": m.status,
        "reason": m.reason,
        "candidates": [
            {"item_key": k, "title": titles[k].title if k in titles else "",
             "doi": titles[k].doi if k in titles else ""}
            for k in m.candidates
        ],
    }


def _write_unmatched(summary: Summary) -> None:
    STAGING_DIR.mkdir(parents=True, exist_ok=True)
    UNMATCHED_REPORT.write_text(json.dumps({
        "how_to_assign": (
            "enrich_pdfs.py --manual assign --assign-file <file> --assign-item "
            "<item_key>   |   --manual ignore --assign-file <file>"
        ),
        "files": summary.unmatched,
    }, indent=1), encoding="utf-8")


def collect(ctx: Context, state: dict, summary: Summary, batch: dict | None) -> list[Path]:
    """Move the files that appeared since `batch` was opened into its
    staging folder. Under dry-run nothing moves: the new files are
    returned for matching in place."""
    if batch is None:
        return []
    pdfs, partials = new_since(ctx.downloads, batch.get("snapshot", {}))
    summary.in_progress = partials
    if ctx.dry_run:
        summary.collected = [p.name for p in pdfs]
        return pdfs
    moved = move_into(pdfs, STAGING_DIR / f"batch-{batch['n']}")
    summary.collected = [p.name for p in moved]
    batch["collected_at"] = time.time()
    return []


def run_cycle(ctx: Context, action: str) -> tuple[int, Summary]:
    """`next` or `collect`. Returns (exit code, summary)."""
    summary = Summary()
    if not ctx.downloads.is_dir():
        print(f"ERROR: downloads folder not found: {ctx.downloads}\n"
              f"  Set [pdf] downloads_dir in config.toml or pass --downloads-dir.",
              flush=True)
        return 2, summary
    state = load_state(ctx.state_path)
    err = _check_library(state, ctx.library)
    if err:
        print(f"ERROR: {err}", flush=True)
        return 2, summary
    previous = state["batches"][-1] if state["batches"] else None

    open_items, _ = _split_items(ctx)
    if action == "next":
        queue = build_queue(ctx, state, open_items)
        if ctx.dry_run:
            for it in queue[:ctx.batch_size]:
                url, version = link_for(it, ctx.route_lookup)
                print(f"  would open {it.key}: {url} ({version})", flush=True)
            summary.opened = min(len(queue), ctx.batch_size)
        else:
            batch = open_batch(
                state, queue, size=ctx.batch_size, downloads=ctx.downloads,
                route_lookup=ctx.route_lookup, opener=ctx.opener,
            )
            if batch is None:
                summary.queue_empty = True
            else:
                summary.opened = len(batch["keys"])
                summary.batch = batch["n"]
                summary.login_first = batch["login_first"]
                summary.failed_to_open = batch["failed_to_open"]
                if batch["login_first"]:
                    url = batch["links"][batch["keys"][0]]
                    print(f"\nSIGN IN FIRST: opened one tab ({url}). Sign in to "
                          f"the library proxy there, download that article, then "
                          f"say 'done' — the rest of the batch opens next cycle.",
                          flush=True)
        # Save before collecting: the tabs are open whatever happens next.
        if not ctx.dry_run:
            save_state(state, ctx.state_path)

    extra = collect(ctx, state, summary, previous)
    process_staging(ctx, state, summary, extra)

    open_after, _ = _split_items(ctx)
    attached_keys = {k for _, k in summary.attached}
    summary.remaining = len([it for it in open_after if it.key not in attached_keys])
    summary.unopened = len([
        it for it in build_queue(ctx, state, open_after) if it.key not in attached_keys
    ])
    if not ctx.dry_run:
        save_state(state, ctx.state_path)
        _write_unmatched(summary)
    print_summary(summary, action)
    return 0, summary


def _find_staged(state: dict, ref: str) -> str | None:
    """A staged file by relative path, absolute path or unique base name."""
    p = Path(ref)
    if p.is_file():
        return _rel(p)
    names = [rel for rel in state["files"] if Path(rel).name == ref]
    if len(names) == 1:
        return names[0]
    return None


def assign(ctx: Context, file_ref: str, item_key: str) -> int:
    """Attach one staged file to the item the user named."""
    state = load_state(ctx.state_path)
    err = _check_library(state, ctx.library)
    if err:
        print(f"ERROR: {err}", flush=True)
        return 2
    rel = _find_staged(state, file_ref)
    if rel is None:
        print(f"ERROR: no staged file {file_ref!r} (see {UNMATCHED_REPORT}).",
              flush=True)
        return 2
    open_items, closed = _split_items(ctx)
    item = next((it for it in open_items if it.key == item_key), None)
    if item is None:
        why = ("already has a PDF" if any(it.key == item_key for it in closed)
               else "is not in this run's scope")
        print(f"ERROR: item {item_key} {why}.", flush=True)
        return 2
    path = Path(rel)
    digest = file_hash(path)
    entry = state["files"].setdefault(rel, {"sha256": digest})
    if entry.get("status") == ATTACHED:
        print(f"ERROR: {rel} is already attached to {entry.get('item_key')}.",
              flush=True)
        return 2
    defect = _pdf_validate.file_defect(path)
    if defect:
        entry.update(status=REJECTED, reason=defect)
        save_state(state, ctx.state_path)
        print(f"ERROR: {path.name} is not a usable PDF: {defect}", flush=True)
        return 1
    summary = Summary()
    entry["reason"] = "assigned by the user"
    page = first_page(ctx.text_extractor(path, digest))
    _settle(ctx, state, summary, entry, path, item, page)
    save_state(state, ctx.state_path)
    print_summary(summary, "assign")
    return 0 if summary.attached else 1


def ignore(ctx: Context, file_ref: str) -> int:
    state = load_state(ctx.state_path)
    rel = _find_staged(state, file_ref)
    if rel is None:
        print(f"ERROR: no staged file {file_ref!r}.", flush=True)
        return 2
    entry = state["files"].setdefault(rel, {"sha256": file_hash(Path(rel))})
    entry.update(status=IGNORED, reason="ignored by the user")
    save_state(state, ctx.state_path)
    print(f"Ignored {rel}; it stays in staging and is not matched again.",
          flush=True)
    return 0


def print_summary(s: Summary, action: str) -> None:
    lines = [f"\nManual download — {action}:"]
    if action == "next":
        if s.queue_empty:
            lines.append("  Nothing left to open: every item without a PDF has "
                         "had its tab. Run --manual collect after the last downloads.")
        elif s.batch is not None:
            lines.append(f"  Opened {s.opened} tab(s) as batch {s.batch}.")
        if s.failed_to_open:
            lines.append(f"  {len(s.failed_to_open)} tab(s) did not open — the links "
                         f"are in output/pdf_staging/batch-{s.batch}/links.txt.")
    lines.append(f"  Collected {len(s.collected)} file(s) from Downloads.")
    if s.in_progress:
        lines.append(f"  {len(s.in_progress)} download(s) still in progress, left "
                     f"for the next cycle: {', '.join(s.in_progress)}")
    lines.append(f"  Attached {len(s.attached)}.")
    for label, rows in (("Duplicate (skipped)", [(n, "") for n in s.duplicates]),
                        ("Not a usable PDF", s.rejected),
                        ("Below [pdf] min_version (not attached)", s.below_policy),
                        ("Not needed", s.not_needed),
                        ("Upload failed (retried next cycle)", [(n, "") for n in s.failed])):
        if rows:
            lines.append(f"  {label}: " + "; ".join(
                f"{n}{' — ' + d if d else ''}" for n, d in rows))
    if s.unmatched:
        lines.append(f"  Unmatched {len(s.unmatched)} — NOT attached; tell me which "
                     f"item each is (listed in {UNMATCHED_REPORT}):")
        for row in s.unmatched:
            cands = ", ".join(c["item_key"] for c in row["candidates"])
            lines.append(f"    {row['name']}: {row['reason']}"
                         + (f" [candidates: {cands}]" if cands else ""))
    lines.append(f"  Remaining without a PDF: {s.remaining} "
                 f"({s.unopened} not opened yet).")
    print("\n".join(lines), flush=True)
    # One fixed-shape line for agents and the MCP summariser.
    print(
        f"MANUAL: action={action} opened={s.opened} batch={s.batch or 0} "
        f"login_first={int(s.login_first)} collected={len(s.collected)} "
        f"in_progress={len(s.in_progress)} attached={len(s.attached)} "
        f"duplicates={len(s.duplicates)} rejected={len(s.rejected)} "
        f"below_policy={len(s.below_policy)} unmatched={len(s.unmatched)} "
        f"remaining={s.remaining} unopened={s.unopened}",
        flush=True,
    )
