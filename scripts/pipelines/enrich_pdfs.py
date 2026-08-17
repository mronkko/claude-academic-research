#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "pyzotero>=1.6",
#     "requests>=2.31",
#     "urllib3>=2.0",
#     "tenacity>=8.0",
#     "habanero>=1.2",
#     "pyalex>=0.15",
#     "pybliometrics>=3.6",
#     "wiley-tdm>=0.2",
#     "reportlab>=4.0",
#     "playwright>=1.40",
# ]
# ///
"""Enrich Zotero items by downloading missing PDFs and attaching them.

For each journal article in the Zotero library that does not already
have a PDF attached:

  1. Run the PDF-source cascade for the item's DOI (see
     `fetchers.pdf_sources`).
  2. Upload the first PDF found as a child attachment via
     `ZoteroClient.attach_pdf` (pyzotero's `attachment_simple`).
  3. Log the outcome to a CSV (`output/pdf_attach_log.csv`).

Source selection via `--sources`:

    enrich_pdfs.py                         # default automated cascade
    enrich_pdfs.py --sources wiley         # Wiley TDM only
    enrich_pdfs.py --sources browser       # Cloudflare-gated publishers
    enrich_pdfs.py --sources elsevier,pmc  # custom subset (aliases OK)
    enrich_pdfs.py --allow-preprints       # + arXiv / SSRN / RePEc copies

Fetcher names are `sciencedirect`, `springer`, `crossref`,
`pubmed_central`, `openalex`, `unpaywall`, `semantic_scholar`, `core`,
`preprint`, `wiley`; `elsevier` and `pmc` are accepted as aliases for
the first and fourth. `browser` and `connector` are separate passes and
cannot be combined with the others in one invocation — use `--all` for
cascade-then-browser.

`preprint` is the one source that is off unless asked for, via
`--allow-preprints` and not via `--sources`: what it attaches is the
manuscript before peer review, which is a different paper in every way
a systematic review cares about. See `fetchers/preprint.py`.

For `--sources browser`, this script drives the per-publisher
`fetchers.browser` handlers directly — a visible Chromium opens, you
solve Cloudflare once per publisher, and each handler's `download()`
method downloads its items using the shared session.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import copy
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date
from pathlib import Path

# Make `core`, `fetchers`, `zotero_io`, `http_client` importable without
# the PEP 723 runner touching the repo layout.
SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
for _p in (str(SCRIPT_DIR), str(SCRIPTS_ROOT)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import fetchers  # noqa: E402
import http_client  # noqa: E402
import pdf_fetch_log  # noqa: E402
import pdf_run_report  # noqa: E402
import shared_orchestrators  # noqa: E402
import zotero_io  # noqa: E402
from core.config_loader import get, require  # noqa: E402
from log_schemas import PDF_FETCH_FIELDS  # noqa: E402

DEFAULT_LOG_CSV = os.path.join("output", "pdf_attach_log.csv")
DEFAULT_CACHE_DIR = os.path.join("output", "pdf_cache")
# Structured failure log (T4-3). Sibling to the attach log above;
# audit_zotero_library reads it to group failures by cause and suggest
# FE codes. Same `output/` dir so users see both files together.
DEFAULT_FAILURE_LOG_CSV = os.path.join("output", "pdf_fetch_log.csv")

LOG_FIELDS = PDF_FETCH_FIELDS

#: Names the docs advertise that are not the fetchers' own `name`.
#: `--sources elsevier,pmc` is the example in this module's docstring
#: and in `--help`, and it exited 2 with "no PDF fetchers matched"
#: because the classes are called `sciencedirect` and `pubmed_central`.
#: Aliasing rather than renaming keeps the `source` column in existing
#: `pdf_attach_log.csv` files meaningful.
_SOURCE_ALIASES = {
    "elsevier": "sciencedirect",
    "sciencedirect": "sciencedirect",
    "pmc": "pubmed_central",
    "pubmed": "pubmed_central",
}

# `pdf_fetch_log` rewrites the whole failure CSV on every write (it
# upserts by (item_key, source)), and the API cascade calls it from a
# ThreadPoolExecutor. Without serialising, concurrent read-modify-write
# cycles drop each other's rows — see `csv_io.upsert_by_item_key`'s
# "callers must serialize externally" contract.
_FAILURE_LOG_LOCK = threading.Lock()

# `source` value for an API-cascade row that no fetcher answered: every
# source returned None without raising. The cascade failed as a unit and
# no individual member of it did anything worth recording, so the row
# says that instead of picking one. Also stable across runs, which keeps
# `pdf_fetch_log`'s (item_key, source) upsert key from accumulating a
# fresh row each time the cascade's ordering or membership shifts.
_API_CASCADE_SOURCE = "api_cascade"

# Consecutive network-layer failures that mean the machine is offline
# rather than the publisher being unhelpful. Low enough to stop the
# bleeding fast (the observed outage failed items at ~1.2 s each), high
# enough that a flaky link dropping the odd request rides through —
# `consecutive_transport` resets on any non-transport outcome.
_OUTAGE_THRESHOLD = 5


# --- Concurrent browser lanes ----------------------------------------------
#
# A "lane" is one Playwright page driving one handler instance. All the
# lanes for a publisher share a single persistent BrowserContext, which
# is the whole reason this is worth doing: the profile directory holds
# the Cloudflare clearance cookies and the institutional SSO / EZproxy
# session, Chromium locks that directory, and a second browser on a
# second profile would therefore need every one of those logins solved
# again. Tabs in one context inherit them for free, and Chromium already
# gives each tab its own renderer process, so the parallelism is real.
#
# What this does NOT parallelise: the Zotero Connector pass. It drives a
# single Zotero desktop through a single translator and asks a human to
# confirm each new host — `effective_lanes` pins it to 1 regardless.


def effective_lanes(handler, requested: int) -> int:
    """How many pages to drive `handler` with.

    Two ceilings, and the smaller wins. `--browser-workers` is the
    user's, for the whole run. Each handler's `concurrency` is the
    publisher's, and it is a finding rather than an unset default —
    `sage.py`'s module comment ("Keep concurrency at 1 and a 2.5-second
    delay between") records what a live run established about that
    platform's tolerance. Raising the flag must not be able to overrule
    it, or the flag becomes a way to get quietly rate-limited.

    A handler that attaches directly is pinned to one lane whatever
    either number says: that is the Connector, and there is one Zotero
    desktop.
    """
    if getattr(handler, "attaches_directly", False):
        return 1
    declared = int(getattr(handler, "concurrency", 1) or 1)
    return max(1, min(int(requested or 1), declared))


class LaneCoordinator:
    """Cross-lane state for one concurrent handler run.

    Three facts have to be shared by every lane driving a publisher, and
    each was a plain local in the serial loop:

    * `skip_remaining` — the user answered the Option-4 prompt with
      "skip". Every lane must honour that, not only the one that asked.
    * the outage breaker's count. "Consecutive" stops being literal
      under concurrency, but the fact it detects — this machine has no
      network — was never per-lane, and N lanes reach the threshold N
      times sooner, which is the direction you want when the alternative
      is shredding the queue at a second an item.
    * whether the prompt has already fired, so N simultaneous failures
      ask the human once.

    The gate has no serial counterpart. While a prompt is open every
    other lane parks *before* claiming its next item, so an answer of
    "skip the rest" cannot arrive after three more lanes have already
    opened pages against a publisher the user just declined. Claiming
    and checking are plain attribute access with no `await` between, so
    the event loop cannot interleave them and no lock is needed.
    """

    def __init__(self, *, outage_threshold: int = _OUTAGE_THRESHOLD) -> None:
        self._outage_threshold = outage_threshold
        self._gate = asyncio.Event()
        self._gate.set()
        self.skip_remaining = False
        self.prompt_fired = False
        self.consecutive_transport = 0
        #: Set once a lane raises `NetworkOutage`. Lanes stop claiming
        #: work rather than being cancelled, so nothing is abandoned
        #: mid-download and un-attempted items stay unlogged — which is
        #: what makes them re-runnable.
        self.outage: BaseException | None = None

    async def wait_until_open(self) -> None:
        await self._gate.wait()

    def note_transport_failure(self) -> bool:
        """True when this run of transport errors means we are offline."""
        self.consecutive_transport += 1
        return self.consecutive_transport >= self._outage_threshold

    def note_other_outcome(self) -> None:
        """Any non-transport outcome clears the breaker."""
        self.consecutive_transport = 0

    def claim_prompt(self) -> bool:
        """True for exactly one lane — the one that gets to ask."""
        if self.prompt_fired:
            return False
        self.prompt_fired = True
        return True

    @contextlib.asynccontextmanager
    async def prompting(self):
        """Hold every other lane at the gate while the human answers."""
        self._gate.clear()
        try:
            yield
        finally:
            self._gate.set()


class _SerialisedWriter:
    """`log_writer` guarded by a lock, for concurrent lanes.

    `_attach_and_log` runs in a worker thread when lanes > 1 — its
    Zotero upload is blocking `requests`, and left on the event loop it
    would stall every other lane for the duration of each upload,
    collapsing the concurrency to roughly nothing. That puts its log row
    off-loop while the lanes keep writing theirs on it. One lock over
    `writerow` covers it: every row is a single call against a shared
    file handle.
    """

    def __init__(self, writer) -> None:
        self._writer = writer
        self._lock = threading.Lock()

    def writerow(self, row) -> None:
        with self._lock:
            self._writer.writerow(row)


_PLAYWRIGHT_MISSING_MSG = (
    "ERROR: the playwright package is not installed.\n"
    "  - Invoke this script via `uv run` so its inline dependencies\n"
    "    (including playwright) are resolved automatically, or\n"
    "    `pip install playwright` into your environment.\n"
    "  - Then install the browser binary once:\n"
    "    `uvx playwright install chromium` (or `playwright install chromium`\n"
    "    if the CLI is on your PATH)."
)


def _as_bool(value, *, default: bool = False) -> bool:
    """Coerce a config value to bool.

    TOML gives a real `bool` for `key = true`; an environment variable
    gives a string; an unset key gives `""`. Only an explicit truthy
    token turns a feature on — anything unrecognised keeps the default,
    so a typo silently enabling a surprising behaviour is not possible.
    """
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if not text:
        return default
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off"):
        return False
    return default


@dataclass
class Config:
    elsevier_api_key: str = ""
    openalex_api_key: str = ""
    wiley_tdm_token: str = ""
    semantic_scholar_api_key: str = ""
    core_api_key: str = ""
    crossref_mailto: str = ""
    #: Opt-in. When an Elsevier PDF comes back as a first-page preview,
    #: render the entitled XML body to a text-only PDF and attach that.
    #: Off by default: it puts a *generated* file in the user's Zotero
    #: library, which is a surprising thing to find there unless it was
    #: asked for. See ScienceDirectSource._fetch_xml_fallback.
    elsevier_render_xml_to_pdf: bool = False
    #: Opt-in for the paid OpenAlex Content API ($0.01 per PDF), the one
    #: per-item cost in the cascade. Defaults to True because having set
    #: `OPENALEX_API_KEY` at all is itself an opt-in signal, and an
    #: existing setup must not silently lose a working tier on upgrade.
    #: The wizard asks outright, so new setups record a real answer.
    #: See fetchers/openalex.py's `_OpenAlexClient._paid_enabled`.
    openalex_use_paid_content_api: bool = True


def _load_config() -> Config:
    return Config(
        elsevier_api_key=get("elsevier", "api_key", env="ELSEVIER_API_KEY"),
        elsevier_render_xml_to_pdf=_as_bool(
            get("elsevier", "render_xml_to_pdf", env="ELSEVIER_RENDER_XML_TO_PDF"),
        ),
        openalex_api_key=get("openalex", "api_key", env="OPENALEX_API_KEY"),
        openalex_use_paid_content_api=fetchers.openalex.coerce_paid_opt_in(
            get(
                "openalex", "use_paid_content_api",
                env="OPENALEX_USE_PAID_CONTENT_API",
            ),
            default=True,
        ),
        wiley_tdm_token=get("wiley", "tdm_token", env="WILEY_TDM_TOKEN"),
        semantic_scholar_api_key=get(
            "semantic_scholar", "api_key", env="SEMANTIC_SCHOLAR_API_KEY",
        ),
        core_api_key=get("core", "api_key", env="CORE_API_KEY"),
        crossref_mailto=get("crossref", "mailto", env="CROSSREF_MAILTO"),
    )


def _open_log(path: str):
    return shared_orchestrators.open_log(path, LOG_FIELDS)


# Statuses that mean "this item has its PDF; don't fetch it again".
#
# `attached_no_text` is deliberately NOT here. It is tempting to call it
# done — the file is attached — but the live evidence says a PDF with no
# extractable text is usually a bad copy rather than a scan: all five
# textless files in the incident came back perfect from a different
# source (3 via Wiley TDM, 2 via the Sage browser handler; 19-44 real
# pages each). Zero were scans. Treating them as done would suppress the
# retry that actually works.
#
# In practice `pdf_map()` still gates these items — they carry an
# attachment, so they drop out before any fetch — which is why the run
# report tells the user to delete the attachment first. Keeping the
# status out of this tuple at least stops the run-log from asserting
# something the evidence contradicts.
#
# `attached_via_connector` IS here. It is a real attachment made by the
# Zotero Connector translator; leaving it out re-queued every Connector
# success on the next run, and the item only fell out later via the
# "already has a real PDF" attachment scan.
DONE_STATUSES = ("attached", "attached_via_connector")


def _load_done_items(path: str) -> set[str]:
    """Zotero item keys this log records as already carrying a PDF.

    **Keyed on the item, not the DOI** — the same correction made in
    `enrich_abstracts._already_done`, for the same reason. A library
    assembled by a systematic-review import holds duplicate records
    routinely; keying the resume set on the DOI meant attaching a PDF to
    one copy permanently barred every other copy from getting one, and a
    consumer that resolves the DOI to a different copy sees an item with
    no PDF and no way to ever acquire one.

    Nothing is lost by the change. The per-item gate that follows in
    `main()` — `pdf_map()`, read off the live library — is what actually
    establishes "this item is done", as the note on `DONE_STATUSES`
    above already observes. Re-fetching a sibling is near-free besides:
    `cache_path_for` keys the PDF cache on the DOI, so the second copy
    attaches from disk without a second download.
    """
    return shared_orchestrators.load_done_keys(
        path, statuses=DONE_STATUSES, key_field="item_key",
    )


# --- Publisher / browser-handler triage (no network) ------------------------
#
# Both lookups read only what is already on disk: the Crossref-resolved
# publisher and URL come from `doi_resolver_cache.json`, and the handler
# registry is a pure prefix/suffix match. This is what lets the API
# cascade say "Sage — a browser handler exists and has not run" instead
# of "unavailable, exclude as FE6".

#: Corporate suffixes that make otherwise-identical Crossref publisher
#: strings group separately in a report ("Springer Science and Business
#: Media LLC" vs "Springer Nature"). Trimmed for display only — the
#: untrimmed value is never needed downstream.
_PUBLISHER_NOISE = re.compile(
    r"\s*(,)?\s*\b(Ltd|Limited|Inc|Incorporated|LLC|GmbH|BV|B\.V\.|PLC|"
    r"Publications?|Publishing|Publishers?|Group|Media|Science and Business Media)"
    r"\b\.?",
    re.IGNORECASE,
)


def _tidy_publisher(name: str) -> str:
    """Collapse Crossref's corporate-suffix variants for grouping."""
    if not name:
        return ""
    tidied = _PUBLISHER_NOISE.sub("", name).strip(" ,.")
    return tidied or name.strip()


def _browser_handler_for(doi: str, resolved_url: str = ""):
    """The browser handler that covers this DOI, or None.

    Host first, DOI prefix second: a journal that migrated publishers
    keeps its old prefix, so `10.1111/etap.*` looks like Wiley but now
    resolves to journals.sagepub.com. Same precedence the browser
    pipeline's own Pass-1 classification uses.
    """
    from urllib.parse import urlparse

    from fetchers import browser as browser_registry

    handlers = browser_registry.all_handlers()
    if resolved_url:
        host = urlparse(resolved_url).netloc.lower()
        by_host = browser_registry.resolve_by_host(host, handlers)
        if by_host is not None:
            return by_host
    return browser_registry.resolve_by_doi(doi, handlers)


def _triage_context(doi: str, cache_dir: str) -> tuple[str, str]:
    """Return `(publisher, browser_handler_name)` for a failed DOI.

    Best-effort and never raises: triage metadata must not be able to
    break a fetch run. An empty handler name means no browser handler
    covers this publisher, which is what makes the difference between
    "try harder" and a genuine FE6.
    """
    from fetchers.doi_resolver import DoiResolverCache

    publisher = ""
    resolved_url = ""
    try:
        hit = DoiResolverCache(cache_dir).get(doi)
        if hit is not None:
            publisher = _tidy_publisher(hit.publisher)
            resolved_url = hit.url
    except Exception:  # noqa: BLE001 — cache is advisory, never load-bearing
        pass
    try:
        handler = _browser_handler_for(doi, resolved_url)
    except Exception:  # noqa: BLE001
        handler = None
    if handler is not None:
        # Prefer the handler's display name: Crossref spells one
        # publisher several ways, and the report groups on this.
        return (handler.display_name or publisher), handler.name
    return publisher, ""


def _report_outage(exc: Exception) -> None:
    """Explain a network-outage stop, and say what it does *not* mean.

    The important half is the disclaimer. Items not attempted get no log
    row at all, so a later audit sees them as untried rather than as
    articles that could not be found — which is the difference between
    re-running and excluding them from a review.
    """
    print(
        f"\nSTOPPED: the network went away mid-run ({exc}).\n"
        f"  Nothing here is a verdict about any article. Items already\n"
        f"  attempted during the outage are logged NETWORK_ERROR (retry,\n"
        f"  not an exclusion); items not yet reached are not logged at\n"
        f"  all. Re-run the same command once you are back online — the\n"
        f"  cache and the resume set mean it picks up where it left off.",
        file=sys.stderr, flush=True,
    )


def _pass3_target(item: dict, resolver_cfg, *, ignore_coverage: bool = False):
    """Choose the Pass-3 full-text target for one item.

    Returns `(url, query_ok, chosen_target)`. Extracted so the queue
    preview and Pass 3 itself cannot disagree about where an item is
    headed — they used to, and the preview was the one users read: it
    reported "Zotero Connector (upfront): 571 papers", implying 571
    manual saves needing Zotero desktop and a human, when two thirds of
    them resolve to EBSCOhost and are handled unattended by Pass 4a.

    Fail-open semantics are the caller's to apply, and are unchanged:
    `query_ok=False` means "could not ask", never "no access".
    """
    if resolver_cfg is None:
        return None, False, None
    if ignore_coverage:
        return None, True, None
    # Imported here rather than at module scope, matching how every
    # other resolver symbol in this file is reached: the browser/resolver
    # stack is heavy and most entry points never touch it.
    from fetchers.library_resolver import lookup_fulltext_target

    return lookup_fulltext_target(
        item["doi"], resolver_cfg,
        in_range_only=True,
        issn=item.get("issn"), pub_date=item.get("pub_date"),
        volume=item.get("volume"),
    )


#: Verdicts from `classify_direct_route`, mapped to whether the
#: publisher's own browser handler should be opened for the item.
DIRECT_ROUTE_CASES: dict[str, bool] = {
    "3-in-coverage": True,       # resolver: this platform holds this year
    "2-out-of-coverage": False,  # resolver: right platform, wrong year
    "1b-no-entitlement": False,  # resolver answered; no route via this platform
    "1a-unknown": True,          # resolver named nothing at all — fail open
}


def classify_direct_route(
    dual,
    domains: tuple[str, ...],
    resolver_cfg,
    *,
    pub_date: str | None = None,
    handler_name: str = "",
    direct_access: frozenset[str] | set[str] = frozenset(),
) -> str:
    """Should the publisher's own handler be opened? Returns a
    `DIRECT_ROUTE_CASES` key.

    The distinction that matters is between the two Case-1 outcomes,
    which used to be a single branch commented "try direct anyway":

    - **1a** — the resolver named *nothing*: unset, unreachable,
      unparseable, or simply a journal it does not know. That is silence,
      and silence is not evidence of missing access, so fail open. A
      failed attempt is a real answer; a skipped one is not.
    - **1b** — the resolver named one or more licensed routes and none of
      them is this publisher's platform. That is evidence, and opening
      the publisher can only fail.

    Collapsing 1b into 1a is expensive. On one 655-item corpus it queued
    60 items for a direct attempt at APA, Academy of Management, Emerald
    and AAA — none of them licensed by the institution, AoM in particular
    selling member access rather than institutional — each behind its own
    Cloudflare/SSO prompt, while the resolver was already naming
    EBSCOhost for every one of them.

    `direct_access` names publishers the user reaches by other means (a
    society membership, a second institution's login). It suppresses 1b,
    never Case 2: 1b is a claim about *entitlement*, which a private
    credential contradicts, whereas Case 2 is a claim about a platform's
    *holdings*, which no credential changes.
    """
    from fetchers.library_resolver import targets_match_domains

    if not domains:
        return "1a-unknown"
    if targets_match_domains(
        dual.in_range, domains, resolver_cfg, pub_date=pub_date,
    ):
        return "3-in-coverage"
    if targets_match_domains(dual.any_range, domains, resolver_cfg):
        return "2-out-of-coverage"
    if dual.any_range and handler_name not in direct_access:
        return "1b-no-entitlement"
    return "1a-unknown"


def _year_from_zotero_date(date_str: str) -> str | None:
    """Extract a 4-digit year from Zotero's free-text `date` field
    ("2024", "2024-05-15", "May 2024", ...) for the Alma ISSN-fallback
    query's `rft.date` (see library_resolver.py's `_query_targets`
    / BACKLOG.md P11). None when no year-shaped substring is found."""
    match = re.search(r"\b(1[89]\d{2}|20\d{2})\b", date_str or "")
    return match.group(1) if match else None


def _first_issn(issn_field: str) -> str | None:
    """Zotero's ISSN field may hold multiple values (print + electronic,
    comma-separated, e.g. "1042-2587, 1540-6520"); the Alma ISSN
    fallback query wants a single value, so take the first."""
    first = (issn_field or "").split(",")[0].strip()
    return first or None


def _log_browser_failure(
    args: argparse.Namespace,
    item: dict,
    *,
    source: str,
    publisher: str = "",
    cause: pdf_fetch_log.FailureCause | None = None,
) -> None:
    """Record a browser / Connector failure in the structured log.

    Until now only the API cascade wrote `pdf_fetch_log.csv`, so every
    browser and Connector outcome was invisible to the audit that reads
    it — exactly the outcomes a triage report most needs.

    Note what is *not* passed: `untried_browser_handler`. By the time
    this is called the handler has had its turn, so the item is no
    longer "try the browser" — it classifies on its own merits, and the
    composite `(item_key, source)` key means this row sits alongside the
    earlier API-cascade row rather than replacing it.

    Best-effort: a logging failure must never break a run.
    """
    log_path = getattr(args, "failure_log_csv", "")
    item_key = item.get("item_key") or ""
    if not log_path or not item_key:
        return
    try:
        pdf_fetch_log.log_failure(
            log_path,
            item_key=item_key,
            doi=item.get("doi", "") or "",
            item_type=item.get("item_type", "") or "journalArticle",
            attempt=2,
            source=source,
            publisher=publisher,
            cause=cause,
        )
    except Exception as e:  # noqa: BLE001
        print(f"    [pdf_fetch_log write failed: {e}]", flush=True)


# ---------------------------------------------------------------------
# Failure detail + the single attach path
# ---------------------------------------------------------------------

_DETAIL_MAX = 300


def _http_status_of(exc: BaseException) -> int | None:
    """Best-effort HTTP status from an exception, without importing httpx.

    pyzotero raises `httpx.HTTPStatusError`, which carries `.response.
    status_code`; requests-based fetchers raise something shaped the
    same way. Anything else yields None.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    return status if isinstance(status, int) else None


def _failure_detail(exc: BaseException) -> str:
    """One-line, CSV-safe reason string for a failed operation.

    Every non-success log row carries one of these. Before it existed,
    the reason for a failure was printed to stdout and then dropped, so
    diagnosing a run meant re-reading terminal scrollback that no longer
    existed.
    """
    parts = [type(exc).__name__]
    status = _http_status_of(exc)
    if status is not None:
        parts.append(f"HTTP {status}")
    message = " ".join(str(exc).split())
    if message:
        parts.append(message)
    detail = ": ".join(parts)
    return detail[:_DETAIL_MAX - 1] + "…" if len(detail) > _DETAIL_MAX else detail


def _pdf_has_text(pdf_path, item_key: str) -> bool | None:
    """True if `pdftotext` extracts any text from the PDF.

    None means "couldn't tell" — poppler missing or extraction blew up —
    which callers treat as "assume fine" rather than flagging a false
    positive. False means the file is structurally intact yet yields no
    text, which is a symptom, not a cause: on the evidence available it
    usually means a bad copy that another source will serve properly,
    and only rarely a genuine scan.

    Only meaningful *after* `_pdf_validate.file_defect` has passed. Zero
    extractable text has two very different causes, and a live run
    conflated them: five files that yielded no text were diagnosed as
    scans needing OCR, when they were actually truncated downloads whose
    page tree was missing entirely. Structure first, then text.

    Extraction is cached by `pdf_text_cache`, and `fulltext_code.py`
    reads that same cache, so this is prefetching rather than extra work.
    """
    try:
        import pdf_text_cache
        text = pdf_text_cache.get_text(item_key, Path(pdf_path))
    except FileNotFoundError:
        return None          # poppler not installed
    except Exception:
        return None
    return bool((text or "").strip())


def _attach_and_log(
    zot,
    log_writer,
    *,
    run_date: str,
    item_key: str,
    doi: str,
    title: str,
    source: str,
    pdf_path,
    failure_log_path: str = "",
    item_type: str = "",
    check_text: bool = True,
) -> bool:
    """Attach one fetched PDF to Zotero and log the outcome. True on success.

    The single upload path for every fetch route (browser handlers, the
    Pass-2 API retry, and the API cascade). Those were three divergent
    copies; the browser copy in particular wrote a bare `upload_failed`
    row, never touched the structured failure log, and had no retry —
    which is how a live run turned 48 good downloads into 48 dead ends.

    Ordering matters here: the attachment upload is the only step whose
    failure means "no PDF". Tagging is best-effort *after* it, because
    folding a tag PATCH into the same try-block records a fully
    successful attachment as `upload_failed`.
    """
    # Structural check before upload. Attaching a corrupt PDF is worse
    # than attaching nothing: `pdf_map()` then reports the item as
    # having a real PDF, so every later run skips it and the damage is
    # permanent. Rejecting here keeps the item in the retry population.
    from fetchers import _pdf_validate

    defect = _pdf_validate.file_defect(pdf_path)
    if defect is not None:
        print(f"→ rejected: {defect}", flush=True)
        log_writer.writerow({
            "run_date": run_date, "item_key": item_key, "doi": doi,
            "title": title, "status": "rejected_corrupt_pdf",
            "source": source, "detail": defect,
        })
        # Drop the bad bytes so the next run re-fetches instead of
        # rediscovering the same broken file in the cache.
        try:
            Path(pdf_path).unlink(missing_ok=True)
        except OSError:
            pass
        if failure_log_path and item_key:
            try:
                with _FAILURE_LOG_LOCK:
                    pdf_fetch_log.log_failure(
                        failure_log_path,
                        item_key=item_key, doi=doi, item_type=item_type,
                        source=source,
                        cause=pdf_fetch_log.FailureCause.CORRUPT_DOWNLOAD,
                    )
            except Exception:
                pass
        return False

    try:
        zot.attach_pdf(item_key, str(pdf_path))
    except Exception as exc:
        detail = _failure_detail(exc)
        print(f"→ upload failed: {detail}", flush=True)
        log_writer.writerow({
            "run_date": run_date, "item_key": item_key, "doi": doi,
            "title": title, "status": "upload_failed", "source": source,
            "detail": detail,
        })
        if failure_log_path and item_key:
            try:
                with _FAILURE_LOG_LOCK:
                    pdf_fetch_log.log_failure(
                        failure_log_path,
                        item_key=item_key, doi=doi, item_type=item_type,
                        source=source, http_status=_http_status_of(exc),
                        cause=pdf_fetch_log.FailureCause.UPLOAD_FAILED,
                    )
            except Exception:
                pass         # diagnostics must never sink the run
        return False

    # Attached. Everything below is best-effort annotation.
    provenance_tags = [
        tag for predicate, tag in (
            (fetchers.is_tdm_recovered_path, fetchers.TDM_RECOVERED_TAG),
            (fetchers.is_repository_copy_path, fetchers.REPOSITORY_COPY_TAG),
            (fetchers.is_preprint_path, fetchers.PREPRINT_VERSION_TAG),
        ) if predicate(pdf_path)
    ]
    if provenance_tags:
        try:
            zot.update_tags(item_key, add=provenance_tags)
        except Exception as exc:
            print(f"  WARN: attached, but tagging failed: {_failure_detail(exc)}",
                  flush=True)

    status, detail = "attached", ""
    if check_text and _pdf_has_text(pdf_path, item_key) is False:
        # Structure passed, so this is not truncation — but "no text"
        # is not the same as "scan". Every textless file in the live
        # incident turned out to be a bad copy that another source
        # served intact, so state the observation and let the report
        # carry the remediation rather than asserting a cause here.
        status = "attached_no_text"
        detail = "no extractable text — likely a bad copy; try another source"
        print("→ attached (no extractable text)", flush=True)
    else:
        print("→ attached", flush=True)

    log_writer.writerow({
        "run_date": run_date, "item_key": item_key, "doi": doi,
        "title": title, "status": status, "source": source, "detail": detail,
    })
    if failure_log_path and item_key:
        try:
            with _FAILURE_LOG_LOCK:
                pdf_fetch_log.clear_failure(failure_log_path, item_key)
        except Exception:
            pass
    return True


async def _drive_handler(
    handler,
    items: list[dict],
    zot,
    log_writer,
    args: argparse.Namespace,
    run_date: str,
    *,
    on_failure: str = "log",            # "log" | "retry_bucket"
    retry_bucket: list[dict] | None = None,
    prompt_on_first_failure: bool = False,
    on_always_skip=None,                # callable(handler_name) → None
) -> None:
    """Drive one publisher handler across its items.

    `on_failure="log"` keeps the v0.3.x behaviour: per-item failures
    are written as `skipped_no_pdf` CSV rows and the run continues.

    `on_failure="retry_bucket"` (new in v0.4.0) routes per-item
    failures into `retry_bucket` instead of writing a log row; the
    Connector pass later tries the same items via its own path, and
    its final status is the only row that ends up in the log. This
    cleaner chain matches the "only the final outcome is logged"
    design of the v0.4.0 routing model.

    `prompt_on_first_failure=True` fires the Option-4 prompt ONCE per
    handler per run on the first per-item failure. The user picks:
      * k — keep trying direct for remaining items
      * s — skip remaining direct attempts (default)
      * A — same as s, plus invoke `on_always_skip(handler.name)` so
            the caller can persist the publisher to
            `[library] no_access` in config.toml.

    When `on_failure="retry_bucket"` and the user answers `s`/`A`, the
    remaining un-attempted items go straight into the retry bucket
    without re-opening the page — saves 30s × N of timeouts.
    """
    from fetchers.browser import Counter, interaction, launch_context
    from fetchers.browser.base import (
        NetworkOutage,
        is_transport_error,
        normalise_setup_result,
    )

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(_PLAYWRIGHT_MISSING_MSG, file=sys.stderr)
        return

    display = handler.display_name or handler.name
    print(f"\n{'='*60}\nPublisher: {display} ({len(items)} PDFs)\n{'='*60}",
          flush=True)
    if not items:
        return
    interaction.report_progress({
        "event": "publisher_start", "publisher": handler.name,
        "queued": len(items),
    })

    os.makedirs(args.cache_dir, exist_ok=True)

    async with async_playwright() as p:
        ctx = await launch_context(p, args.cache_dir)
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        # `needs_interactive_solve = False` means what it says: no bot
        # wall, no institutional login, nothing for a human to clear — so
        # do not open a prompt nobody needs to answer. Until this check
        # existed the flag only changed the queue *message*, and the
        # EBSCOhost handler (which authenticates silently on
        # institutional IP) still blocked on a setup question, which would
        # stall an unattended run until the control-file timeout.
        if getattr(handler, "needs_interactive_solve", True):
            setup_result = normalise_setup_result(
                await handler.setup(page, items[0]["doi"])
            )
        else:
            setup_result = "proceed"
        if setup_result in ("skip", "always_skip"):
            # User bailed out before any item ran. "always_skip" also
            # persists the publisher to [library] no_access so future
            # runs don't bother asking.
            if setup_result == "always_skip" and on_always_skip is not None:
                try:
                    on_always_skip(handler.name)
                    print(
                        f"  {display}: persisted to [library] no_access; "
                        f"future runs will skip this handler.",
                        flush=True,
                    )
                except Exception as e:
                    print(
                        f"  WARN: could not persist [library] no_access "
                        f"+= {handler.name!r}: {e}",
                        flush=True,
                    )

            if on_failure == "retry_bucket" and retry_bucket is not None:
                print(
                    f"  Skipping {display}: routing {len(items)} items "
                    f"to the Connector retry bucket.",
                    flush=True,
                )
                retry_bucket.extend(items)
            else:
                print(
                    f"  Skipping {display}: logging {len(items)} items "
                    f"as skipped_no_access.",
                    flush=True,
                )
                for item in items:
                    log_writer.writerow({
                        "run_date": run_date, "item_key": item["item_key"],
                        "doi": item["doi"],
                        "title": (item.get("title") or "")[:70],
                        "status": "skipped_no_access", "source": handler.name,
                    })
                    # The user looked at the landing page and said they
                    # have no access. That is an ILL candidate, not a
                    # missing paper — ACCESS_BLOCKED says exactly that.
                    _log_browser_failure(
                        args, item,
                        source=handler.name, publisher=handler.display_name,
                        cause=pdf_fetch_log.FailureCause.ACCESS_BLOCKED,
                    )
            interaction.report_progress({
                "event": "publisher_skipped", "publisher": handler.name,
                "queued": len(items), "reason": setup_result,
            })
            await ctx.close()
            return

        counter = Counter()
        total = len(items)
        import time
        t_start = time.monotonic()

        # How many pages drive this publisher. `--browser-workers` is the
        # run-wide ceiling; `handler.concurrency` is the publisher's own,
        # and the smaller of the two wins. At 1 this behaves exactly as
        # the serial loop it replaces — a single lane claims every item
        # in order, on the page `setup()` was solved on.
        requested_lanes = int(getattr(args, "browser_workers", 1) or 1)
        lanes = effective_lanes(handler, requested_lanes)
        coord = LaneCoordinator()
        if requested_lanes > lanes:
            # Say so rather than quietly honouring a smaller number —
            # a user who asked for 10 and got 1 should be told which
            # ceiling bound it and where that ceiling lives.
            print(
                f"  {display} caps at {lanes} "
                f"{'tab' if lanes == 1 else 'parallel tabs'} "
                f"(--browser-workers {requested_lanes} requested); its "
                f"`concurrency` is set from what a live run against this "
                f"platform established.",
                flush=True,
            )
        if lanes > 1:
            print(f"  Driving {display} on {lanes} parallel tabs.", flush=True)
            # Every lane writes rows; `_attach_and_log` writes its from a
            # worker thread. One lock over the shared handle.
            log_writer = _SerialisedWriter(log_writer)

        cursor = 0

        def _claim() -> dict | None:
            """Hand out the next item, or None when the queue is spent.

            No `await` between the read and the write of `cursor`, so
            the event loop cannot hand the same item to two lanes. Also
            the one place the outage stops the run: lanes stop claiming
            rather than being cancelled, so un-attempted items are never
            logged and stay re-runnable.
            """
            nonlocal cursor
            if coord.outage is not None or cursor >= total:
                return None
            item = items[cursor]
            cursor += 1
            return item

        async def _process(lane_handler, lane_page, item: dict) -> None:
            if lane_handler.delay_s > 0:
                await asyncio.sleep(lane_handler.delay_s)
            result = await lane_handler.download(
                lane_page, ctx, item, args.cache_dir,
                counter=counter, total=total, t_start=t_start,
            )
            doi = item["doi"]
            title = (item.get("title") or "")[:70]
            # A heartbeat per item, so an agent following the run knows
            # it is alive and how far along without parsing stdout.
            # "downloaded" rather than "attached": the upload happens
            # below and has its own row in the run log.
            interaction.report_progress({
                "event": "item", "publisher": lane_handler.name, "doi": doi,
                "outcome": "failed" if result is None else "downloaded",
                "done": counter.done, "queued": total,
            })

            if result is None:
                # Per-item download failure. A network-layer error is a
                # different animal from "this publisher has nothing":
                # nothing was asked, so nothing was answered. Both arrive
                # here as `None`, which is why the handler carries the
                # reason out on `last_error` — and why each lane needs
                # its own handler instance, since that attribute would
                # otherwise be read across lanes.
                transport = is_transport_error(
                    getattr(lane_handler, "last_error", ""),
                )
                if transport:
                    # The machine's connection is gone, not this
                    # publisher's. Keep going and the queue is shredded at
                    # roughly a second an item — a live run lost the
                    # network for four minutes and burned 193 items that
                    # way, every one recorded as a failed fetch.
                    if coord.note_transport_failure():
                        raise NetworkOutage(
                            f"{coord.consecutive_transport} consecutive "
                            f"network errors on {display} "
                            f"(last: {lane_handler.last_error[:80]})"
                        )
                else:
                    coord.note_other_outcome()

                if prompt_on_first_failure and not transport and coord.claim_prompt():
                    # Hold the other lanes at the gate while the human
                    # decides. Without this, an answer of "skip the rest"
                    # would arrive after the remaining lanes had already
                    # opened pages against a publisher just declined.
                    async with coord.prompting():
                        remaining = max(total - cursor, 0)
                        answer = await asyncio.to_thread(
                            _prompt_on_first_failure,
                            lane_handler, remaining, args,
                        )
                        if answer == "always_skip":
                            coord.skip_remaining = True
                            if on_always_skip is not None:
                                try:
                                    on_always_skip(lane_handler.name)
                                except Exception as e:
                                    print(
                                        f"  WARN: could not persist "
                                        f"[library] no_access += "
                                        f"{lane_handler.name!r}: {e}",
                                        flush=True,
                                    )
                        elif answer == "skip":
                            coord.skip_remaining = True
                        # "keep" → keep looping, same as before.
                # Structured record either way: this handler was tried
                # and did not produce a PDF. That fact is true whether
                # or not the Connector gets a turn next, and the
                # composite key keeps both attempts.
                _log_browser_failure(
                    args, item,
                    source=lane_handler.name,
                    publisher=lane_handler.display_name,
                    # NETWORK_ERROR is recoverable and says "retry next
                    # run". Letting this default would classify it
                    # UNAVAILABLE — the one cause that licenses a
                    # full-text exclusion — for an article no server was
                    # ever asked about.
                    cause=(
                        pdf_fetch_log.FailureCause.NETWORK_ERROR
                        if transport else None
                    ),
                )
                if on_failure == "retry_bucket" and retry_bucket is not None:
                    retry_bucket.append(item)
                else:
                    log_writer.writerow({
                        "run_date": run_date, "item_key": item["item_key"],
                        "doi": doi, "title": title,
                        "status": "skipped_no_pdf", "source": lane_handler.name,
                    })
                return

            pdf_path, source_url = result
            if args.dry_run:
                log_writer.writerow({
                    "run_date": run_date, "item_key": item["item_key"],
                    "doi": doi, "title": title,
                    "status": "dry_run", "source": lane_handler.name,
                })
                return

            if not item["item_key"]:
                print(f"  [{doi}] no Zotero item key — skipping upload", flush=True)
                log_writer.writerow({
                    "run_date": run_date, "item_key": "",
                    "doi": doi, "title": title,
                    "status": "downloaded_no_item", "source": lane_handler.name,
                })
                return

            attach = dict(
                run_date=run_date, item_key=item["item_key"],
                doi=doi, title=title, source=lane_handler.name,
                pdf_path=pdf_path,
                failure_log_path=getattr(args, "failure_log_csv", "") or "",
                item_type=item.get("item_type", ""),
                check_text=not getattr(args, "no_check_text", False),
            )
            if lanes > 1:
                # A complete line rather than a prefix `_attach_and_log`
                # finishes: with lanes interleaving, the two halves would
                # not land next to each other.
                print(f"  [{doi}] attaching…", flush=True)
                # Blocking `requests` upload — off the loop, or it stalls
                # every other lane for its duration.
                await asyncio.to_thread(
                    _attach_and_log, zot, log_writer, **attach,
                )
            else:
                print(f"  [{doi}]", end=" ", flush=True)
                _attach_and_log(zot, log_writer, **attach)

        async def _lane(lane_handler, lane_page) -> None:
            """One tab, claiming items until the queue or the run ends."""
            while True:
                await coord.wait_until_open()
                item = _claim()
                if item is None:
                    return
                if coord.skip_remaining:
                    # User picked "skip remaining" for this publisher.
                    if on_failure == "retry_bucket" and retry_bucket is not None:
                        retry_bucket.append(item)
                    continue
                try:
                    await _process(lane_handler, lane_page, item)
                except NetworkOutage as e:
                    # Stop claiming rather than cancelling the siblings:
                    # a cancelled lane could abandon a download mid-flight,
                    # and items nobody attempted must stay unlogged so the
                    # next run picks them up.
                    coord.outage = e
                    return

        # Lane 0 reuses the page `setup()` was solved on. The extra lanes
        # are new tabs in the *same* context, so they inherit its cookies
        # — the Cloudflare clearance and the institutional session — and
        # need no second login. Each gets its own handler instance
        # because `last_error` is per-download instance state; copying
        # after `setup()` carries over anything setup established.
        lane_pages = [page]
        lane_handlers = [handler]
        for _ in range(lanes - 1):
            lane_pages.append(await ctx.new_page())
            lane_handlers.append(copy.copy(handler))
        try:
            await asyncio.gather(*(
                _lane(h, pg)
                for h, pg in zip(lane_handlers, lane_pages, strict=True)
            ))
        finally:
            for extra in lane_pages[1:]:
                with contextlib.suppress(Exception):
                    await extra.close()
        if coord.outage is not None:
            raise coord.outage

        print(
            f"\n  Total: {counter.ok} new, {counter.cached} cached, "
            f"{counter.failed} failed",
            flush=True,
        )
        interaction.report_progress({
            "event": "publisher_done", "publisher": handler.name,
            "queued": total, "ok": counter.ok, "cached": counter.cached,
            "failed": counter.failed,
        })
        await ctx.close()


def _prompt_on_first_failure(
    handler, remaining: int, args: argparse.Namespace,
) -> str:
    """Option-4 prompt. Returns one of 'keep' | 'skip' | 'always_skip'.

    `--on-first-failure=<value>` answers it without asking. Otherwise the
    question goes through the interaction channel, exactly like the
    "can you see the PDF?" prompt — `TtyChannel` for a real terminal,
    `ControlFileChannel` for an agent-driven run, `AutoSkipChannel` when
    nobody can be reached.

    **It used to test `sys.stdin.isatty()` directly and return 'skip'.**
    That conflated "can we ask a human" with "is stdin a terminal", which
    is the coupling `interaction` exists to undo: under `--control-file`
    the user is present and answering other prompts in the conversation,
    yet one failed item silently discarded every remaining item for that
    publisher. `reinert_2025_sgr` lost its second APA article that way —
    never attempted, never mentioned, indistinguishable in the log from
    an article nobody had a route to.
    """
    override = getattr(args, "on_first_failure", "")
    if override:
        return override
    # Imported here, not at module scope: `fetchers.browser` pulls in
    # Playwright, and the non-browser cascades must import this module
    # without it. Same pattern as the other call sites.
    from fetchers.browser import interaction

    channel = interaction.get_channel()
    if not channel.interactive:
        return "skip"
    display = handler.display_name or handler.name
    answer = channel.read_line(
        f"\n{display} failed to download the last item.\n"
        f"{remaining} more {display} item"
        f"{'s are' if remaining != 1 else ' is'} queued for this run. "
        f"What do you want to do?\n"
        f"  [k] Keep trying {display} direct for the remaining items\n"
        f"  [s] Skip remaining {display} items this run (default)\n"
        f"  [A] Always skip {display} direct — write to config so "
        f"future runs jump straight to the Connector fallback\n> "
    ).strip()
    if answer == "k" or answer.lower() == "keep":
        return "keep"
    if answer == "A" or answer.lower() in ("always", "always_skip"):
        return "always_skip"
    return "skip"                       # empty (Enter), "s", or anything else


def library_selection_matches(zot, selected: dict) -> tuple[bool, str]:
    """Does Zotero Desktop's selected library match the pipeline's target?

    Connector saves land in whichever library Desktop has highlighted, so
    a mismatch means every save goes to the wrong place. Returns
    `(matched, reason)`; `reason` names the evidence used.

    **A personal library must be judged differently from a group.** This
    compared only `groupID`, and a personal library never reports one —
    so under `--user` `matched` was always False and every run printed
    "every save will land in the wrong place" as a false alarm, on what
    is the most common configuration there is. Desktop signals that My
    Library is selected by reporting no group ID at all; when the target
    is a user library, that absence is the match, and a group being
    selected is the real mismatch.
    """
    lib_name = selected.get("libraryName") or "(unknown)"
    # Zotero Desktop may expose the cloud group ID under either
    # `groupID` or `groupId` depending on version — accept both.
    cloud_gid = selected.get("groupID") or selected.get("groupId")

    if zot.library_type == "user":
        return cloud_gid is None, "personal library (no group ID reported)"
    if cloud_gid is not None:
        return str(cloud_gid) == str(zot.group_id), f"group ID {cloud_gid}"
    target_name = zot.group_name()
    if target_name:
        return (
            lib_name == target_name,
            f"name-based comparison (target {target_name!r})",
        )
    return False, ""


async def _drive_connector(
    handler,
    items: list[dict],
    zot,
    log_writer,
    args: argparse.Namespace,
    run_date: str,
) -> None:
    """Drive the Zotero Connector handler across a single batch.

    Differs from `_drive_handler`:
      * loads the Connector extension into a separate Chromium profile,
      * waits for the extension's service worker,
      * pre-flight-pings Zotero Desktop (aborts cleanly if offline),
      * calls `handler.download_and_attach(...)` per item — the
        handler saves to Zotero itself (no local PDF upload step).
    """
    from fetchers.browser import (
        Counter,
        launch_context,
        ping_zotero_desktop,
        wait_for_service_worker,
    )
    from fetchers.browser.base import normalise_setup_result

    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print(_PLAYWRIGHT_MISSING_MSG, file=sys.stderr)
        return

    display = handler.display_name or handler.name
    print(f"\n{'='*60}\nPublisher: {display} ({len(items)} items)\n{'='*60}",
          flush=True)
    if not items:
        return

    # Zotero Desktop pre-flight.
    import requests as _requests
    if not ping_zotero_desktop(_requests.Session()):
        print(
            "  ERROR: Zotero Desktop is not running (or its connector\n"
            "  server on localhost:23119 is disabled).  Logging "
            f"{len(items)} items as connector_zotero_unavailable.",
            flush=True,
        )
        for it in items:
            log_writer.writerow({
                "run_date": run_date, "item_key": it["item_key"],
                "doi": it["doi"],
                "title": (it.get("title") or "")[:70],
                "status": "connector_zotero_unavailable",
                "source": handler.name,
            })
        return

    # Library-selection pre-flight. Connector saves go to whichever
    # library Zotero Desktop has selected in its left pane — if that
    # doesn't match our target group, every save lands in the wrong
    # library and our subsequent poll never finds the new item.
    # Compare by cloud group ID (unique); fall back to library name
    # if the response doesn't carry the group ID (older Zotero).
    selected = zot.selected_local_library()
    if selected is not None:
        lib_name = selected.get("libraryName") or "(unknown)"
        # Zotero Desktop may expose the cloud group ID under either
        # `groupID` or `groupId` depending on version — accept both.
        cloud_gid = selected.get("groupID") or selected.get("groupId")
        matched, match_reason = library_selection_matches(zot, selected)

        if matched:
            print(
                f"\n  Zotero Desktop has {lib_name!r} selected — "
                f"matches target {match_reason}. Saves will land here.",
                flush=True,
            )
        else:
            # `describe_library()` renders user and group targets in
            # their own vocabulary. Hardcoding "group <id>" here printed
            # "the pipeline is working on 'group 5591'" for a personal
            # library whose *user* id happened to be 5591.
            if zot.library_type == "user":
                detail = (
                    f"Desktop has a group library selected (group ID "
                    f"{cloud_gid}); the target is your personal library.\n"
                    "  Select 'My Library' in Zotero Desktop's left pane."
                )
            elif cloud_gid is not None:
                detail = (
                    f"Desktop reports group ID {cloud_gid}, target is "
                    f"{zot.group_id}."
                )
            else:
                detail = (
                    "Zotero Desktop did not report a group ID for the\n"
                    "  selected library; the pipeline could not match\n"
                    "  it against the target by ID."
                )
            print(
                f"\n  Zotero Desktop has {lib_name!r} selected, but the\n"
                f"  pipeline is working on {zot.describe_library()}.\n"
                f"  {detail}\n"
                f"  Connector saves go to the selected library, not the\n"
                f"  target — every save will land in the wrong place\n"
                f"  unless you fix this.",
                flush=True,
            )
            if sys.stdin.isatty():
                confirm = input(
                    f"  Save to {lib_name!r} anyway? [y/N] "
                ).strip().lower()
                if confirm not in ("y", "yes"):
                    where = (
                        "'My Library'" if zot.library_type == "user"
                        else repr(zot.group_name() or f"group {zot.group_id}")
                    )
                    print(
                        "  Aborting. In Zotero Desktop, click on\n"
                        f"  {where} in the left pane, then re-run.",
                        flush=True,
                    )
                    for it in items:
                        log_writer.writerow({
                            "run_date": run_date, "item_key": it["item_key"],
                            "doi": it["doi"],
                            "title": (it.get("title") or "")[:70],
                            "status": "connector_wrong_library",
                            "source": handler.name,
                        })
                    return
    else:
        print(
            "\n  WARN: could not determine Zotero Desktop's selected\n"
            "  library. Make sure your target group is selected in\n"
            "  Zotero Desktop's left pane before continuing.",
            flush=True,
        )

    # Extension pre-flight — surfaced in setup() too, but a clean
    # bail-out here avoids opening Chromium for nothing.
    if handler.extension_path is None:
        print(
            "  ERROR: Zotero Connector extension not found. Install "
            "from https://www.zotero.org/download/connectors/ in Chrome,\n"
            "  then re-run the setup wizard.",
            flush=True,
        )
        for it in items:
            log_writer.writerow({
                "run_date": run_date, "item_key": it["item_key"],
                "doi": it["doi"],
                "title": (it.get("title") or "")[:70],
                "status": "connector_extension_missing",
                "source": handler.name,
            })
        return

    os.makedirs(args.cache_dir, exist_ok=True)

    async with async_playwright() as p:
        ctx = await launch_context(
            p, args.cache_dir, extensions=[handler.extension_path],
        )
        page = ctx.pages[0] if ctx.pages else await ctx.new_page()

        setup_result = normalise_setup_result(
            await handler.setup(page, items[0]["doi"])
        )
        if setup_result != "proceed":
            for it in items:
                log_writer.writerow({
                    "run_date": run_date, "item_key": it["item_key"],
                    "doi": it["doi"],
                    "title": (it.get("title") or "")[:70],
                    "status": "connector_setup_failed",
                    "source": handler.name,
                })
            await ctx.close()
            return

        service_worker = await wait_for_service_worker(ctx, timeout_s=15)
        if service_worker is None:
            print(
                "  ERROR: Connector service worker did not start within 15s.",
                flush=True,
            )
            for it in items:
                log_writer.writerow({
                    "run_date": run_date, "item_key": it["item_key"],
                    "doi": it["doi"],
                    "title": (it.get("title") or "")[:70],
                    "status": "connector_sw_timeout",
                    "source": handler.name,
                })
            await ctx.close()
            return

        counter = Counter()
        total = len(items)
        import time
        t_start = time.monotonic()

        # Group items by effective host so reCAPTCHA / EZproxy logins
        # only need to be solved once per platform instead of once per
        # item. `effective_host` unwraps EZproxy URLs so jstor links
        # under ezproxy.jyu.fi cluster with jstor links that aren't.
        from fetchers.library_resolver import effective_host
        items_sorted = sorted(
            items,
            key=lambda it: effective_host(it.get("resolver_target_url", "")),
        )

        current_host = None
        for item in items_sorted:
            host = effective_host(item.get("resolver_target_url", ""))
            if host != current_host:
                current_host = host
                remaining_on_host = sum(
                    1 for it in items_sorted
                    if effective_host(it.get("resolver_target_url", "")) == host
                )
                print(
                    f"\n  ══ Batch: {host or '(unknown host)'} "
                    f"({remaining_on_host} "
                    f"item{'s' if remaining_on_host != 1 else ''}) ══\n"
                    f"  Solve any login / reCAPTCHA once for this host; "
                    f"subsequent items reuse the session.",
                    flush=True,
                )

            if handler.delay_s > 0:
                await asyncio.sleep(handler.delay_s)
            ok = await handler.download_and_attach(
                page, ctx, service_worker, item, zot,
                counter=counter, total=total, t_start=t_start,
            )
            doi = item["doi"]
            title = (item.get("title") or "")[:70]
            # Host-scoped skips (user pressed 's' at the first-item
            # prompt on this host) are a distinct status from "the
            # Connector tried to save but failed".
            item_host = effective_host(item.get("resolver_target_url", ""))
            skipped_by_user = item_host in getattr(
                handler, "_skipped_hosts", set(),
            )
            if ok:
                status = "attached_via_connector"
            elif skipped_by_user:
                status = "skipped_by_user"
            else:
                status = "connector_save_failed"
            log_writer.writerow({
                "run_date": run_date, "item_key": item["item_key"],
                "doi": doi, "title": title,
                "status": status, "source": handler.name,
            })
            if status == "connector_save_failed":
                # Last rung of the ladder: the library's own route was
                # opened in a real browser and still produced nothing.
                # So this is where an item finally earns UNAVAILABLE —
                # which is exactly why a dead network must not be allowed
                # to arrive here wearing that label. Same reasoning as the
                # publisher handlers above, and it matters more here,
                # because nothing downstream re-examines this verdict.
                from fetchers.browser.base import is_transport_error
                _log_browser_failure(
                    args, item, source="connector",
                    cause=(
                        pdf_fetch_log.FailureCause.NETWORK_ERROR
                        if is_transport_error(getattr(handler, "last_error", ""))
                        else None
                    ),
                )

        print(
            f"\n  Total: {counter.ok} new, {counter.failed} failed",
            flush=True,
        )
        await ctx.close()


#: Where `audit_zotero_library.py --pdf-fetch-log` writes its retry sets.
#: Mirrors that script's `--output` default (`.claude/audit/audit.json`,
#: stem `.claude/audit/audit`), which is the contract between the two.
AUDIT_KEYS_STEM = os.path.join(".claude", "audit", "audit")


def _auto_publisher_keys(stem: str = AUDIT_KEYS_STEM) -> tuple[str, list[str]]:
    """The audit's browser retry set, as `(keys_file, publishers)`.

    The audit already worked out which items a browser pass can recover
    and wrote them to `<stem>.retry.browser.keys`, plus one file per
    publisher. Re-deriving that here would be a second implementation of
    the same triage; reading it back means the two cannot disagree.

    Returns `("", [])` when the audit has not been run, which the caller
    turns into an instruction to run it rather than a silent full-library
    pass — the difference between 76 targeted items and 1,500.
    """
    combined = Path(f"{stem}.retry.browser.keys")
    if not combined.is_file():
        return "", []
    prefix = f"{Path(stem).name}.retry.browser."
    publishers = sorted(
        path.name[len(prefix):-len(".keys")]
        for path in Path(stem).parent.glob(f"{prefix}*.keys")
    )
    return str(combined), publishers


def _install_interaction_channel(args: argparse.Namespace) -> None:
    """Choose how this run talks to whoever is driving it.

    Two directions, and they are independent. Questions go out over the
    channel: an explicit `--control-file` wins, then `--no-prompt`, then
    the TTY default. `--control-file` is what makes the browser pass
    drivable from an agent's Bash subprocess — the human still solves
    every challenge, but the question travels through a file and the
    conversation instead of through a controlling terminal nobody has.

    Progress goes out over the sinks. The channel is always one of them;
    `--progress-json` adds a file that accumulates every event, which is
    what a run started in the background with no prompts has instead of
    a terminal to watch.
    """
    from fetchers.browser import interaction

    interaction.reset_progress_sinks()
    if getattr(args, "progress_json", ""):
        interaction.add_progress_sink(
            interaction.JsonlProgressFile(args.progress_json),
        )
        print(f"Progress events go to {args.progress_json} (one JSON object "
              f"per line).", flush=True)

    if getattr(args, "control_file", ""):
        interaction.set_channel(
            interaction.ControlFileChannel(
                args.control_file,
                timeout_s=float(getattr(args, "control_timeout", 1800) or 1800),
            )
        )
        print(
            f"Interactive prompts go to {args.control_file}; reply by writing "
            f'{{"seq": N, "answer": "..."}} to {args.control_file}.reply',
            flush=True,
        )
        return
    if getattr(args, "no_prompt", False):
        interaction.set_channel(interaction.AutoSkipChannel())


def _has_interactive_surface() -> bool:
    """True iff the script can prompt the user. Checks `/dev/tty`
    (POSIX, the canonical controlling terminal) first, then falls
    back to `sys.stdin.isatty()` so Windows interactive runs still
    pass when /dev/tty is absent.

    Used by the browser-cascade fail-fast guard (T4-2). The Bash tool
    subprocesses Claude Code spawns have neither a controlling TTY
    nor a TTY-shaped stdin, so this returns False there — and the
    caller exits fast with a copy-paste-friendly hint instead of
    silently hanging on the first `_wait_for_user()` prompt.

    A `--control-file` run bypasses this check entirely (see the call
    site): the file *is* the interactive surface, and requiring a TTY as
    well would defeat the point.
    """
    try:
        with open("/dev/tty"):
            return True
    except OSError:
        pass
    try:
        return sys.stdin.isatty()
    except Exception:  # noqa: BLE001 — stdin may be closed in odd subprocess wraps
        return False


def _exit_no_interactive_surface(args: argparse.Namespace) -> None:
    """Print a paste-in command for a fresh terminal and exit non-zero.

    Surfaces the "browser did not open / nothing happens" failure mode
    the user described in the session log (T4-2). Mentions `--no-prompt`
    so unattended runs (cron, agent loops) can opt in to skip-on-prompt
    rather than fail.
    """
    publisher_arg = (
        f" --publisher {args.publisher}" if getattr(args, "publisher", "") else ""
    )
    keys_arg = (
        f" --filter-keys-file {args.filter_keys_file}"
        if getattr(args, "filter_keys_file", "")
        else ""
    )
    library_arg = " --user" if getattr(args, "user", False) else (
        f" --group {args.group}" if getattr(args, "group", "") else ""
    )
    base = (
        f"uv run ${{CLAUDE_PLUGIN_ROOT}}/scripts/pipelines/enrich_pdfs.py"
        f" --sources browser{library_arg}{publisher_arg}{keys_arg}"
    )
    sys.exit(
        "ERROR: This run needs somewhere to ask you questions — the "
        "browser cascade prompts for Cloudflare / SSO / Yes-or-No "
        "confirmations on each publisher, and this process has no "
        "controlling terminal.\n"
        "\n"
        "  • If an agent is driving this: add `--control-file <path>`. The\n"
        "    Chromium window still opens on your screen and you still solve\n"
        "    each challenge; the questions travel through that file and the\n"
        "    conversation rather than through a terminal. Run it in the\n"
        "    background and watch the file:\n"
        f"      {base} --control-file .claude/audit/browser.json\n"
        "  • To run it yourself instead — macOS: ⌘-Space → Terminal → paste:\n"
        f"      {base}\n"
        "  • To see which publishers this will ask you to solve, without\n"
        "    opening a browser (safe to run anywhere, including here):\n"
        f"      {base} --plan\n"
        "  • For unattended runs (no prompts; auto-skip on first "
        "publisher failure), add `--no-prompt`.\n"
    )


def _run_browser_in_process(
    to_process: list[dict],
    zot,
    log_writer,
    args: argparse.Namespace,
    run_date: str,
    *,
    connector_only: bool = False,
    session=None,
    config=None,
) -> int:
    """Classify, drive direct handlers, then drive the Connector fallback.

    Four passes:

      Pass 1 — classify each item. SFX dual lookup tells us whether
               the library has any full-text route and whether the
               per-publisher handler's domain is in range. Three
               outcomes per item:
                 Case 3: direct domain in the date-filtered list →
                         `items_by_pub[handler.name]`.
                 Case 2: direct domain in the ignore-date list only →
                         skip direct (library has publisher but not
                         this year); route to `connector_upfront`.
                 Case 1: direct domain in neither (library has no
                         relationship with this publisher) → try
                         direct anyway (user might be a member).
               Items with no direct handler or in `library.no_access`
               go to `connector_upfront` directly.

      Pass 2 — drive each direct handler with the Option-4 failure
               prompt. Failures feed `connector_retry`.

      Pass 3 — assign an SFX target URL (date-filtered, highest-
               priority platform) to each Connector item. Items with
               no Query-B target are logged as `skipped_no_pdf` /
               `skipped_no_library_coverage` and dropped.

      Pass 4 — single Connector session for the remaining list.

    `connector_only=True` bypasses Pass 1/2 entirely: every DOI goes
    to the Connector upfront bucket. Used by `--sources connector`
    for targeted validation runs.
    """
    from collections import defaultdict
    # Fail fast if the script can't prompt — the browser cascade
    # depends on user interaction (Cloudflare challenges, SSO, per-
    # publisher Y/n confirmations). When neither /dev/tty nor a TTY-
    # shaped stdin is available (Bash tool subprocess, piped invocation),
    # exit immediately with a paste-in command for a fresh terminal
    # rather than starting and silently hanging on the first prompt.
    # Bypass via --no-prompt for unattended runs.
    # `--plan` never opens a browser or prompts, so the interactive-surface
    # requirement doesn't apply — that is the whole point of it: an agent
    # can run it non-interactively and tell the user what to expect.
    if (
        not getattr(args, "no_prompt", False)
        and not getattr(args, "plan", False)
        and not getattr(args, "control_file", "")
        and not _has_interactive_surface()
    ):
        _exit_no_interactive_surface(args)

    from urllib.parse import urlparse

    from core import config_writer
    from fetchers.browser import (
        EbscoHandler,
        ZoteroConnectorHandler,
        all_handlers,
        is_ebsco_target,
        resolve_by_doi,
        resolve_by_host,
    )
    from fetchers.browser.base import NetworkOutage
    from fetchers.doi_resolver import DoiResolverCache, resolve_doi
    from fetchers.library_resolver import (
        load_from_config,
        lookup_dual,
    )

    # Plain hostname test, used on URLs we synthesize ourselves from a
    # Crossref-resolved host. Distinct from `targets_match_domains`,
    # which matches resolver *targets* and must also consider platform
    # names because Alma's URLs never expose a publisher host.
    from fetchers.resolvers import host_matches_domains

    direct_handlers = all_handlers()
    handler_by_name = {h.name: h for h in direct_handlers}

    # DOI → canonical URL resolver via Crossref. Catches prefix
    # collisions: ETAP's 10.1111/etap.* DOIs route to Sage (not
    # Wiley) since the journal's migration circa 2021.
    from habanero import Crossref
    crossref_client = Crossref(
        mailto=get("crossref", "mailto", env="CROSSREF_MAILTO"),
    )
    doi_cache = DoiResolverCache(args.cache_dir)

    # Resolver session — no Crossref mailto, no tenacity retries
    # (competing timeouts lead to visible stalls).
    import requests as _requests
    resolver_session = _requests.Session()
    resolver_cfg = load_from_config(resolver_session, args.cache_dir)

    # [library] no_access → short-circuit these direct handlers
    # unconditionally. Populated at runtime by the failure prompt's
    # "Always skip" answer; editable via the setup wizard. Stored as
    # a TOML list, so we read via load_config() directly — get() only
    # returns strings.
    from core.config_loader import load_config

    def _handler_set(key: str) -> set[str]:
        raw = load_config().get("library", {}).get(key, [])
        if isinstance(raw, list):
            return {str(s).strip() for s in raw if s}
        if isinstance(raw, str):
            return {s.strip() for s in raw.split(",") if s.strip()}
        return set()

    no_access = _handler_set("no_access")
    # [library] direct_access → the opposite declaration: "I can reach
    # this publisher by means the link resolver cannot see." A personal
    # society membership, or a second institution's SSO — this user
    # reaches APA PsycNET through JYU while the configured resolver is
    # Aalto's, so Aalto's Alma legitimately lists no APA route and the
    # Case 1b divert below would be wrong for exactly this publisher.
    #
    # It suppresses Case 1b only, never Case 2. Case 1b is a claim about
    # *entitlement* ("the library has no route via this platform"), which
    # private access does contradict. Case 2 is a claim about the
    # platform's *holdings* ("that platform's run starts in 1996"), which
    # no credential changes.
    direct_access = _handler_set("direct_access")

    # Pass 2 API retry: the prefix-filtering API sources (Wiley TDM,
    # Elsevier, Springer). When Crossref resolution reveals a DOI
    # whose canonical host matches one of these sources, but whose
    # prefix Pass 1 wouldn't have matched, we call the source with
    # `bypass_prefix_filter=True` before resorting to the browser.
    # Skipped in connector_only mode (targeted validation).
    pass2_api_sources: list = []
    if not connector_only and session is not None and config is not None:
        try:
            pass2_api_sources = [
                s for s in fetchers.pdf_sources(session, config)
                if getattr(s, "direct_access_domains", ())
            ]
        except Exception as e:
            print(f"  WARN: Pass 2 API retry init failed: {e}", flush=True)
            pass2_api_sources = []

    # ------------------------------------------------------------------
    # Pass 1 — classify.
    # ------------------------------------------------------------------

    items_by_pub: dict[str, list[dict]] = defaultdict(list)
    connector_upfront: list[dict] = []
    no_handler_count = 0
    # Read once here rather than at Pass 3, because the queue preview
    # below now models the same routing and the two must agree.
    ignore_coverage = getattr(args, "ignore_library_coverage", False)
    # Case 1b diversions, per publisher, for the queue summary. Worth
    # naming rather than counting silently: the whole point is that the
    # user is *not* being asked to solve a challenge for these, and the
    # one case where that is wrong (private access the resolver cannot
    # see) is fixed by a config key they need to be told about.
    no_entitlement: dict[str, int] = {}

    if resolver_cfg is not None and not connector_only:
        print(
            f"\nChecking library access via {resolver_cfg.describe()}...",
            flush=True,
        )

    for zot_item in to_process:
        item_data = zot_item.get("data", {})
        doi = (item_data.get("DOI") or "").strip().lower()
        if not doi:
            continue
        # issn/pub_date/volume feed the Alma ISSN-fallback query in
        # library_resolver.py when a DOI-only lookup comes back empty
        # (BACKLOG.md P11) — harmless no-ops against SFX endpoints.
        entry = {
            "doi": doi,
            "item_key": zot_item.get("key", ""),
            "title": item_data.get("title", ""),
            "issn": _first_issn(item_data.get("ISSN", "")),
            "pub_date": _year_from_zotero_date(item_data.get("date", "")),
            "volume": (item_data.get("volume") or "").strip() or None,
        }

        if connector_only:
            connector_upfront.append(entry)
            continue

        # Route by DOI's canonical Crossref URL first — covers
        # migrated journals (e.g. ETAP moved Wiley→Sage; its
        # 10.1111/etap.* DOIs now resolve to journals.sagepub.com,
        # not Wiley). Fall back to DOI-prefix matching when Crossref
        # is unreachable or returns no URL.
        direct = None
        resolution = resolve_doi(
            doi, crossref=crossref_client, cache=doi_cache,
        )
        resolved_host = ""
        if resolution and resolution.url:
            resolved_host = urlparse(resolution.url).hostname or ""

        # Pass 2 API retry: if the resolved host matches a
        # prefix-filtering API source (Wiley TDM / Elsevier / Springer),
        # invoke it with `bypass_prefix_filter=True`. Catches DOIs
        # whose prefix Pass 1 didn't match but whose canonical host
        # does — e.g. a journal migrated onto Elsevier while keeping
        # its original non-10.1016 DOI prefix.
        if resolved_host and pass2_api_sources:
            for src in pass2_api_sources:
                if not host_matches_domains(
                    f"https://{resolved_host}/", src.direct_access_domains,
                ):
                    continue
                try:
                    retry_result = src.fetch_pdf(
                        doi, cache_dir=args.cache_dir,
                        bypass_prefix_filter=True,
                    )
                except Exception as e:
                    print(
                        f"  Pass 2 API retry via {src.name} errored: "
                        f"{str(e)[:80]}",
                        flush=True,
                    )
                    retry_result = None
                if retry_result is None:
                    continue
                pdf_path, _source_url = retry_result
                title70 = (entry.get("title") or "")[:70]
                if args.dry_run:
                    log_writer.writerow({
                        "run_date": run_date, "item_key": entry["item_key"],
                        "doi": doi, "title": title70,
                        "status": "dry_run", "source": src.name,
                    })
                    print(
                        f"  Pass 2 API retry hit {src.name} [dry-run] "
                        f"{title70}", flush=True,
                    )
                    break
                print(
                    f"  Pass 2 API retry via {src.name} {title70}",
                    end=" ", flush=True,
                )
                _attach_and_log(
                    zot, log_writer,
                    run_date=run_date, item_key=entry["item_key"],
                    doi=doi, title=title70, source=src.name,
                    pdf_path=pdf_path,
                    failure_log_path=getattr(args, "failure_log_csv", "") or "",
                    item_type=entry.get("item_type", ""),
                    check_text=not getattr(args, "no_check_text", False),
                )
                break
            else:
                retry_result = None

            # If any matching source attached (or hit dry-run), skip
            # further routing for this item.
            if retry_result is not None:
                continue

        if resolved_host:
            direct = resolve_by_host(resolved_host, direct_handlers)
        if direct is None:
            direct = resolve_by_doi(doi, direct_handlers)

        if direct and direct.name in no_access:
            direct = None

        if direct is None:
            no_handler_count += 1
            connector_upfront.append(entry)
            continue

        # Classify Case 1a / 1b / 2 / 3 via the resolver's coverage
        # queries. Case 2 works on both dialects by two mechanisms: SFX
        # answers it in the query (`sfx.ignore_date_threshold`), so
        # in_range and any_range are genuinely different requests, while
        # Alma cannot filter by date at all but reports per-package
        # coverage — so passing `pub_date` asks "does this platform hold
        # *this year*", and diffing against the unfiltered answer
        # reconstructs the same verdict from different evidence.
        #
        # This is what stops a pre-1997 article reaching the Springer
        # handler: Alma lists SpringerLink for the journal, but the
        # holding starts 1997, so in_range is False while in_any is True
        # — Case 2, divert, and no 30-second paywall timeout. Three items
        # in one 97-item run hit exactly that before this existed.
        #
        # See `classify_direct_route` for why Case 1 is split in two.
        if resolver_cfg is not None:
            dual = lookup_dual(
                doi, resolver_cfg,
                issn=entry["issn"], pub_date=entry["pub_date"],
                volume=entry["volume"],
            )
            case = classify_direct_route(
                dual, direct.direct_access_domains, resolver_cfg,
                pub_date=entry["pub_date"],
                handler_name=direct.name,
                direct_access=direct_access,
            )
            if not DIRECT_ROUTE_CASES[case]:
                if case == "1b-no-entitlement":
                    label = direct.display_name or direct.name
                    no_entitlement[label] = no_entitlement.get(label, 0) + 1
                connector_upfront.append(entry)
                continue

        items_by_pub[direct.name].append(entry)

    if args.publisher:
        items_by_pub = {
            k: v for k, v in items_by_pub.items() if k == args.publisher
        }
        # --publisher restricts direct; drop Connector items to avoid
        # surprising the caller with a second session.
        connector_upfront = []

    # Print the queue.
    total_direct = sum(len(v) for v in items_by_pub.values())
    if total_direct or connector_upfront:
        print("\nBrowser queue:", flush=True)
        solve_publishers: list[str] = []
        for name, pub_items in items_by_pub.items():
            handler = handler_by_name[name]
            display = handler.display_name or name
            needs_solve = getattr(handler, "needs_interactive_solve", True)
            if needs_solve:
                solve_publishers.append(display)
            print(
                f"  • {display} (direct): {len(pub_items)} "
                f"paper{'' if len(pub_items) == 1 else 's'}"
                + ("  [needs an interactive solve]" if needs_solve else ""),
                flush=True,
            )
        if connector_upfront:
            # Resolve where these actually go before calling them all
            # "Connector". Only in --plan: the lookups are the same ones
            # Pass 3 makes and they are cached, but on a cold cache they
            # are slow, and a normal run reaches Pass 4a's own banner
            # soon enough anyway. --plan exists to answer "what will this
            # cost me", so paying for the answer there is the trade.
            if getattr(args, "plan", False) and resolver_cfg is not None:
                print(
                    f"    (resolving targets for {len(connector_upfront)} "
                    f"items to split unattended from manual...)",
                    flush=True,
                )
                unattended = manual = no_route = 0
                for it in connector_upfront:
                    tgt, ok, chosen = _pass3_target(
                        it, resolver_cfg, ignore_coverage=ignore_coverage,
                    )
                    if tgt and is_ebsco_target(chosen):
                        unattended += 1
                    elif tgt or not ok or ignore_coverage:
                        manual += 1
                    else:
                        no_route += 1
                if unattended:
                    print(
                        f"  • EBSCOhost (resolver-routed): {unattended} "
                        f"paper{'' if unattended == 1 else 's'}"
                        f"  [no solve, no Zotero desktop]",
                        flush=True,
                    )
                if manual:
                    print(
                        f"  • Zotero Connector: {manual} "
                        f"paper{'' if manual == 1 else 's'}"
                        f"  [needs Zotero desktop + a human]",
                        flush=True,
                    )
                if no_route:
                    print(
                        f"  • No licensed route: {no_route} "
                        f"paper{'' if no_route == 1 else 's'}"
                        f"  [ILL candidates — not attempted]",
                        flush=True,
                    )
            else:
                print(
                    f"  • Zotero Connector (upfront): "
                    f"{len(connector_upfront)} "
                    f"paper{'' if len(connector_upfront) == 1 else 's'}",
                    flush=True,
                )

        if no_entitlement:
            total_nx = sum(no_entitlement.values())
            detail = ", ".join(
                f"{name} {n}"
                for name, n in sorted(
                    no_entitlement.items(), key=lambda kv: (-kv[1], kv[0])
                )
            )
            print(
                f"\n  {total_nx} item{'' if total_nx == 1 else 's'} skipped the "
                f"publisher's own site — the link resolver lists a licensed "
                f"route for {'it' if total_nx == 1 else 'them'}, but not via "
                f"that publisher ({detail}).",
                flush=True,
            )
            print(
                "  If you can reach one of these another way (society "
                "membership, a second institution's login), add it to "
                "`[library] direct_access` in config.toml and it will be "
                "tried directly again.",
                flush=True,
            )

        # State it as a single explicit instruction. The per-publisher
        # lines above are easy to skim past, and a user who solves only
        # the publishers they happened to notice leaves whole buckets
        # untried with no error anywhere.
        if solve_publishers:
            print(
                f"\n  You will be asked to solve a Cloudflare / SSO challenge "
                f"for {len(solve_publishers)} publisher"
                f"{'' if len(solve_publishers) == 1 else 's'}, in order: "
                + ", ".join(solve_publishers),
                flush=True,
            )
    else:
        print("\nNothing to do via the browser path.", flush=True)
        return 0

    if getattr(args, "plan", False):
        print(
            "\n--plan: stopping here without opening a browser. Re-run the "
            "same command without --plan to execute.",
            flush=True,
        )
        return 0

    # ------------------------------------------------------------------
    # Pass 2 — drive direct handlers, collect Connector retries.
    # ------------------------------------------------------------------

    connector_retry: list[dict] = []

    async def _run_direct() -> None:
        for name, pub_items in items_by_pub.items():
            handler = handler_by_name[name]
            await _drive_handler(
                handler, pub_items, zot, log_writer, args, run_date,
                on_failure="retry_bucket",
                retry_bucket=connector_retry,
                prompt_on_first_failure=True,
                on_always_skip=lambda n: config_writer.append_to_list(
                    "library", "no_access", n,
                ),
            )

    if items_by_pub and not connector_only:
        try:
            asyncio.run(_run_direct())
        except NetworkOutage as e:
            _report_outage(e)
            return 1

    # ------------------------------------------------------------------
    # Pass 3 — assign SFX target URLs to Connector items. Use Query B
    # (date-filtered) so we never hand the Connector a target that
    # SFX knows is out of coverage.
    # ------------------------------------------------------------------

    connector_items: list[dict] = []
    # Items whose resolver target is EBSCOhost. They get a dedicated
    # handler rather than the Connector: EBSCO's OpenURL link lands on a
    # multi-result page where the Zotero translator shows a picker (see
    # connector.py), whereas that page self-redirects to a single-article
    # PDF viewer we can drive directly. No Zotero desktop needed either.
    ebsco_items: list[dict] = []
    skipped_no_target = 0
    origins = (
        [(it, "upfront") for it in connector_upfront]
        + [(it, "retry") for it in connector_retry]
    )
    failed_open = 0
    for it, origin in origins:
        # Query B only (date-filtered). When Query B is empty, we do NOT
        # fall back to Query A. The cache data against JYU's SFX (see
        # resolver_cache.json) shows Query A commonly returns targets the
        # user genuinely can't access — the ignore-date list is "the
        # resolver knows the journal via these providers", not "you can
        # download this DOI now". Using it as a fallback wastes user time
        # on paywalls.
        #
        # Ranking comes from `resolver_cfg.priority`, which honours
        # `[library] platform_priority`.
        #
        # With no `[library] openurl_base` configured `query_ok` is
        # False, which fails open below. Gating on an unconfigured
        # resolver would drop every upfront item without a single
        # attempt — which is precisely what used to happen: it made the
        # entire Connector fallback unreachable while logging "no
        # library coverage".
        target, query_ok, chosen = _pass3_target(
            it, resolver_cfg, ignore_coverage=ignore_coverage,
        )

        if target or not query_ok or ignore_coverage:
            # Fail open. With no resolver answer, hand the Connector the
            # DOI resolver URL rather than nothing — it skips outright on
            # a missing target, so failing open without a URL would just
            # relabel the skip. Opening doi.org lands on the publisher
            # page, where the browser profile's existing institutional
            # session (IP range, EZproxy cookie, SSO) may well work.
            # A failed attempt is a real answer; a skipped one is not.
            if not target:
                if origin == "upfront":
                    failed_open += 1
                target = f"https://doi.org/{it['doi']}"
            entry_with_target = {**it, "resolver_target_url": target}
            if is_ebsco_target(chosen):
                ebsco_items.append(entry_with_target)
            else:
                connector_items.append(entry_with_target)
        else:
            status = (
                "skipped_no_library_coverage"
                if origin == "upfront" else "skipped_no_pdf"
            )
            log_writer.writerow({
                "run_date": run_date, "item_key": it["item_key"],
                "doi": it["doi"],
                "title": (it.get("title") or "")[:70],
                "status": status, "source": "connector",
                "detail": "link resolver returned no licensed full-text route",
            })
            # The library resolver reports no full-text route for this
            # item in its date range. The article exists; this reader
            # cannot reach it — which is the definition of an ILL
            # candidate, not of a missing paper.
            _log_browser_failure(
                args, it, source="library_resolver",
                cause=pdf_fetch_log.FailureCause.ACCESS_BLOCKED,
            )
            skipped_no_target += 1

    if skipped_no_target:
        print(
            f"\n  {skipped_no_target} item"
            f"{'s' if skipped_no_target != 1 else ''} had no Query-B "
            f"full-text target — logged without opening the Connector.",
            flush=True,
        )
    if failed_open:
        print(
            f"  {failed_open} item{'s' if failed_open != 1 else ''} had no "
            f"resolver answer (unset/unreachable/unparseable) — trying the "
            f"Connector anyway rather than assuming no access.",
            flush=True,
        )

    # ------------------------------------------------------------------
    # Pass 4a — EBSCOhost items, driven directly from their resolver
    # target. Runs before the Connector because it needs neither Zotero
    # desktop nor a human: EBSCO authenticates on institutional IP, and
    # the OpenURL page self-redirects to a single-article PDF viewer.
    # ------------------------------------------------------------------

    if ebsco_items:
        print(
            f"\n  • EBSCOhost (resolver-routed): {len(ebsco_items)} "
            f"paper{'' if len(ebsco_items) == 1 else 's'}",
            flush=True,
        )
        try:
            asyncio.run(_drive_handler(
                EbscoHandler(), ebsco_items, zot, log_writer, args, run_date,
                on_failure="retry_bucket",
                # A failure here is not evidence the article is
                # unreachable — hand it on to the Connector, which drives
                # the same platform through Zotero's own translator.
                retry_bucket=connector_items,
                prompt_on_first_failure=False,
            ))
        except NetworkOutage as e:
            _report_outage(e)
            return 1

    # ------------------------------------------------------------------
    # Pass 4b — single Connector session for whatever is left.
    # ------------------------------------------------------------------

    if connector_items:
        connector_handler = ZoteroConnectorHandler(
            extension_path=get(
                "zotero_connector", "extension_dir",
                env="ZOTERO_CONNECTOR_DIR",
            ) or None,
        )
        asyncio.run(_drive_connector(
            connector_handler, connector_items, zot, log_writer,
            args, run_date,
        ))

    _print_browser_summary(args, len(to_process))
    return 0


def _print_browser_summary(args: argparse.Namespace, queued: int) -> None:
    """End-of-run totals for `--sources browser` / `connector`.

    The browser path printed per-handler totals and then returned in
    silence, so a run that queued 119 items ended with no statement of
    how many were attached — the one number the user is waiting for.
    Read back from the run log rather than threading counters through
    four passes, which also means resumed and partial runs report the
    same way.
    """
    try:
        attached = shared_orchestrators.load_done_keys(
            args.log_csv,
            statuses=("attached", "attached_via_connector"),
            key_field="item_key",
        )
    except Exception:  # noqa: BLE001 — a summary must not fail a run
        return
    from fetchers.browser import interaction

    interaction.report_progress({
        "event": "run_done", "queued": queued, "attached": len(attached),
        "missing": max(queued - len(attached), 0),
    })
    print(
        f"\nDone. {len(attached)} of {queued} queued item"
        f"{'s' if queued != 1 else ''} now have a PDF attached.",
        flush=True,
    )
    remaining = queued - len(attached)
    if remaining > 0:
        print(
            f"  {remaining} still missing. Run "
            f"`audit_zotero_library.py --pdf-fetch-log` for the "
            f"per-publisher breakdown and what to try next.",
            flush=True,
        )


def _try_cascade(
    item: dict,
    sources: list,
    cache_dir: str,
    *,
    failure_log_path: str | None = None,
) -> tuple[Path, str] | None:
    """Try each PDF fetcher in priority order. Returns (path, source_name)
    on the first hit.

    On full-cascade failure (every source returns None or raises), the
    cause is classified via `pdf_fetch_log.classify_failure` and a row
    is appended to `failure_log_path` (when provided). Audits read that
    log to group items by cause and suggest FE codes — see T4-3.
    """
    d = item.get("data", {})
    # Lower-cased to match the browser path (`_run_browser_in_process`),
    # which normalises before building its cache filename. Without this
    # the same mixed-case DOI cached by one path is invisible to the
    # other, and a PDF already on disk gets re-fetched or declared
    # missing.
    doi = (d.get("DOI") or "").strip().lower()
    if not doi:
        return None
    item_type = d.get("itemType", "") or ""
    item_key = item.get("key", "") or d.get("key", "") or ""
    # The fetcher whose answer the logged row rests on — the one that
    # raised, preferring one that carried an HTTP status so `source` and
    # `http_status` always name the same event. Whichever fetcher merely
    # happened to be *last* in the cascade is not that: with the current
    # ordering it is always CORE, so every API-pass row in a 655-row
    # live log blamed a provider that had only been asked last. When
    # nobody answered, the failure belongs to the cascade as a whole and
    # naming any single member of it would be an invention.
    blamed_source = ""
    raised_exception = False
    last_status: int | None = None
    for src in sources:
        try:
            result = src.fetch_pdf(doi, cache_dir=cache_dir)
        except NotImplementedError:
            continue
        except Exception as e:
            print(f"    {src.name}: {e}", flush=True)
            raised_exception = True
            status = _http_status_of(e)
            if status is not None:
                last_status = status
                blamed_source = src.name
            elif not blamed_source:
                blamed_source = src.name
            continue
        if result is None:
            continue
        path, _ = result
        return path, src.name

    # Cascade exhausted. Classify and persist if a log path was given.
    if failure_log_path and item_key:
        publisher, browser_handler = _triage_context(doi, cache_dir)
        # Best-effort cause, but prefer letting `classify_failure` decide
        # from the evidence we collected. Out-of-scope item types resolve
        # regardless. Otherwise the HTTP status is what distinguishes a
        # 403 paywall (ACCESS_BLOCKED, "flag for ILL") from a genuine 404
        # — and an untried browser handler outranks both, because a
        # Cloudflare block surfaces as either an exception or a silent
        # miss and the browser pass recovers it in both cases.
        #
        # So NETWORK_ERROR is forced only when all three are true: it
        # raised, no status came back to classify on, and no handler
        # covers the publisher. Anything less specific would relabel a
        # recoverable item as a transport fault.
        cause: pdf_fetch_log.FailureCause | None = None
        if item_type in pdf_fetch_log.DEFAULT_OUT_OF_SCOPE_TYPES:
            cause = pdf_fetch_log.FailureCause.OUT_OF_SCOPE
        elif raised_exception and last_status is None and not browser_handler:
            cause = pdf_fetch_log.FailureCause.NETWORK_ERROR
        try:
            with _FAILURE_LOG_LOCK:
                pdf_fetch_log.log_failure(
                    failure_log_path,
                    item_key=item_key,
                    doi=doi,
                    item_type=item_type,
                    attempt=1,
                    source=blamed_source or _API_CASCADE_SOURCE,
                    publisher=publisher,
                    http_status=last_status,
                    cause=cause,
                    untried_browser_handler=browser_handler,
                    # This is the API cascade, and `main()` rejects
                    # --sources that mix the browser pass into it, so by
                    # construction nothing here has been through a
                    # browser handler, the link resolver, or the
                    # Connector. Saying so keeps UNAVAILABLE — the one
                    # cause that licenses an exclusion — off items that
                    # no route has yet actually refused.
                    browser_pass_untried=True,
                )
        except Exception as e:  # noqa: BLE001
            # Logging is best-effort — never let a CSV write break a
            # cascade run. Surface to stderr for diagnostics.
            print(f"    [pdf_fetch_log write failed: {e}]", flush=True)
    return None


def _run_api_cascade(
    to_process: list[dict],
    sources: list,
    args: argparse.Namespace,
    run_date: str,
    zot,
    log_writer,
) -> tuple[int, int, int]:
    """Run the API cascade (Pass 1) against `to_process`.

    Downloads in parallel, then uploads serially via
    `ZoteroClient.attach_pdf`. Writes one CSV row per item (status in
    {attached, skipped_no_pdf, upload_failed, dry_run}). Returns the
    counter triple `(attached, no_pdf, failed)` so the caller can
    print a summary — important for `--all` where this runs before
    Pass 2 and the Pass-1 summary must appear before Pass-2 banners.
    """
    attached = no_pdf = failed = 0

    print(f"\n  Downloading PDFs ({args.workers} threads)...", flush=True)
    results: list[tuple[dict, tuple[Path, str] | None]] = []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(
                _try_cascade, it, sources, args.cache_dir,
                failure_log_path=args.failure_log_csv,
            ): it
            for it in to_process
        }
        for fut in as_completed(futures):
            item = futures[fut]
            try:
                res = fut.result()
            except Exception as e:
                print(f"  [{item['key']}] cascade error: {e}", flush=True)
                res = None
            results.append((item, res))
            d = item["data"]
            title70 = (d.get("title") or "")[:70]
            if res is not None:
                path, src_name = res
                size_kb = path.stat().st_size // 1024
                print(
                    f"  [{len(results)}/{len(to_process)}] {title70:<70} "
                    f"({src_name}) {size_kb}KB",
                    flush=True,
                )
            else:
                print(
                    f"  [{len(results)}/{len(to_process)}] {title70:<70} "
                    f"no PDF",
                    flush=True,
                )

    found = [(item, r) for item, r in results if r is not None]
    not_found = [item for item, r in results if r is None]

    print(
        f"\n  Downloaded: {len(found)}, Not found: {len(not_found)}",
        flush=True,
    )

    for item in not_found:
        d = item["data"]
        log_writer.writerow({
            "run_date": run_date, "item_key": item["key"],
            "doi": d.get("DOI", ""),
            "title": (d.get("title") or "")[:70],
            "status": "skipped_no_pdf", "source": "",
        })
        no_pdf += 1

    if found and not args.dry_run:
        print(f"  Uploading {len(found)} PDFs to Zotero...\n", flush=True)

    for j, (item, res) in enumerate(found, 1):
        d = item["data"]
        key = item["key"]
        doi = d.get("DOI", "")
        title = (d.get("title") or "")[:70]
        path, src_name = res
        print(
            f"  [{j}/{len(found)}] {title:<70} ({src_name})",
            end=" ", flush=True,
        )
        if args.dry_run:
            print("[dry-run]", flush=True)
            log_writer.writerow({
                "run_date": run_date, "item_key": key, "doi": doi,
                "title": title, "status": "dry_run", "source": src_name,
            })
            attached += 1
            continue
        if _attach_and_log(
            zot, log_writer,
            run_date=run_date, item_key=key, doi=doi, title=title,
            source=src_name, pdf_path=path,
            failure_log_path=args.failure_log_csv,
            item_type=d.get("itemType", ""),
            check_text=not getattr(args, "no_check_text", False),
        ):
            attached += 1
        else:
            failed += 1

    return attached, no_pdf, failed


def _cached_pdf_for(doi: str, cache_dir: str) -> Path | None:
    """Return a usable cached PDF for `doi`, or None.

    Uses the full structural validator rather than a magic-byte check —
    a recovery pass that cheerfully re-attached a truncated file would
    re-create exactly the bug it exists to clean up.

    Tries the lower-cased DOI first (what every path writes now) and
    then the DOI as Zotero holds it, so caches written before the two
    paths agreed on case are still found.
    """
    from fetchers import _pdf_validate
    from fetchers.browser.base import cache_path_for

    doi = (doi or "").strip()
    seen: set[str] = set()
    for variant in (doi.lower(), doi):
        if not variant or variant in seen:
            continue
        seen.add(variant)
        path = cache_path_for(cache_dir, variant)
        if path.exists() and _pdf_validate.file_defect(path) is None:
            return path
    return None


def _attach_from_cache(
    to_process: list[dict],
    zot,
    log_writer,
    args: argparse.Namespace,
    run_date: str,
) -> list[dict]:
    """Attach PDFs already sitting in the cache. Returns the items done.

    Runs before any fetching. A previous run that downloaded a PDF but
    failed to upload it leaves the file on disk with nothing pointing
    back at it: the run-log says `upload_failed`, the item still has no
    attachment, and the next run re-enters the full cascade — which for
    a Cloudflare-gated publisher means it cannot be recovered at all
    without another interactive browser pass.

    That is not hypothetical. A live run downloaded 68 Sage PDFs behind
    a solved Cloudflare challenge, attached 20, and lost 48 that were
    still in `output/pdf_cache/` in perfect condition.
    """
    hits = [
        (it, path)
        for it in to_process
        if (path := _cached_pdf_for(
            it.get("data", {}).get("DOI") or "", args.cache_dir,
        )) is not None
    ]
    if not hits:
        return []

    print(
        f"\nRecovering {len(hits)} PDF(s) already in the cache "
        f"(previously downloaded but not attached)...",
        flush=True,
    )
    done: list[dict] = []
    for i, (item, path) in enumerate(hits, 1):
        d = item["data"]
        title = (d.get("title") or "")[:70]
        print(f"  [{i}/{len(hits)}] {title:<70} (cache)", end=" ", flush=True)
        if _attach_and_log(
            zot, log_writer,
            run_date=run_date, item_key=item["key"],
            doi=(d.get("DOI") or "").strip(), title=title, source="cache",
            pdf_path=path, failure_log_path=args.failure_log_csv,
            item_type=d.get("itemType", ""),
            check_text=not args.no_check_text,
        ):
            done.append(item)
    return done


def _print_run_report(
    args: argparse.Namespace, zot=None, item_keys: set[str] | None = None,
) -> None:
    """Print the end-of-run report, enriched with Zotero metadata if possible.

    Called from every exit path. Previously only the API-cascade branch
    printed anything at all, and only three counters — which is why
    answering "what is still missing?" meant hand-writing CSV forensics.

    `item_keys` scopes the report to the items this invocation
    considered. Without it a `--filter-keys-file` run would report on
    the whole accumulated log, burying the 20 items the user asked about
    under the 900 they didn't.
    """
    rows = pdf_run_report.read_log(args.log_csv)
    if item_keys is not None:
        rows = [r for r in rows if (r.get("item_key") or "").strip() in item_keys]
    if not rows:
        return

    lookup = None
    if zot is not None:
        cache: dict[str, dict[str, str]] = {}

        def lookup(item_key: str) -> dict[str, str]:      # noqa: F811
            if not item_key:
                return {}
            if item_key not in cache:
                try:
                    data = zot.get_item(item_key).get("data", {})
                except Exception:
                    cache[item_key] = {}
                    return {}
                creators = data.get("creators") or []
                names = [
                    c.get("lastName") or c.get("name") or ""
                    for c in creators if isinstance(c, dict)
                ]
                names = [n for n in names if n]
                if len(names) > 3:
                    authors = f"{names[0]} et al."
                elif names:
                    authors = " & ".join(names)
                else:
                    authors = ""
                cache[item_key] = {
                    "authors": authors,
                    "year": _year_from_zotero_date(data.get("date", "")) or "",
                    "journal": data.get("publicationTitle", "") or "",
                    "title": data.get("title", "") or "",
                }
            return cache[item_key]

    print(pdf_run_report.format_report(rows, metadata=lookup))


def select_requested_articles(
    fetched: list[dict], requested: set[str],
) -> tuple[list[dict], int]:
    """Split a `--filter-keys-file` fetch into articles and skipped keys.

    `ZoteroClient.items_by_keys` returns the items whose keys were asked
    for *and* their attachment children — a request for 38 keys comes
    back as 55 items once 17 of them have a PDF. Counting every
    non-`journalArticle` in that response as a skipped key therefore
    reported a number that grew as retrieval succeeded: a run that had
    just attached 11 PDFs announced "11 key(s) resolved to
    non-journalArticle items and were skipped" having skipped none of
    them, and the louder it got the better the run had gone.

    Only a key the caller actually asked for can be a scope decision, so
    the count is taken over those; the children are not an answer about
    anything and are ignored.
    """
    own = [it for it in fetched if it.get("key") in requested]
    articles = [
        it for it in own
        if it.get("data", {}).get("itemType") == "journalArticle"
    ]
    return articles, len(own) - len(articles)


def _build_parser() -> argparse.ArgumentParser:
    """Construct the CLI parser.

    Split out of `main()` so tests can check that the commands this
    script tells users to paste are actually commands it accepts.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sources", default="",
        help="Comma-separated fetcher names to use. Special values: "
             "'wiley' (Wiley TDM only), 'browser' (full browser pass: "
             "direct handlers + Connector fallback), 'connector' "
             "(Connector handler only; useful for targeted validation). "
             "Default: automated cascade "
             "(elsevier+springer+crossref+pmc+openalex+unpaywall).",
    )
    parser.add_argument(
        "--publisher",
        help="(browser mode only) Restrict to one publisher key.",
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="Download PDFs, do not upload to Zotero.")
    parser.add_argument("--log-csv", default=DEFAULT_LOG_CSV,
                        help=f"Path to log CSV (default: {DEFAULT_LOG_CSV}).")
    parser.add_argument(
        "--failure-log-csv", default=DEFAULT_FAILURE_LOG_CSV,
        help=(
            "Structured PDF-fetch failure log (default: "
            f"{DEFAULT_FAILURE_LOG_CSV}). audit_zotero_library reads this "
            "to group items by failure cause (out-of-scope, "
            "access-blocked, unavailable, network-error) and suggest "
            "FE codes during adjudication."
        ),
    )
    parser.add_argument("--cache-dir", default=DEFAULT_CACHE_DIR,
                        help=f"PDF cache directory (default: {DEFAULT_CACHE_DIR}).")
    parser.add_argument("--workers", type=int, default=6,
                        help="Parallel download threads (default: 6). "
                             "API cascade only — see --browser-workers for "
                             "the browser passes.")
    parser.add_argument(
        "--browser-workers", type=int, default=1, metavar="N",
        help="Parallel tabs per publisher in the browser passes "
             "(default: 1). All N share one Chromium profile, so one "
             "Cloudflare / SSO solve covers them all. Capped per "
             "publisher by that handler's own `concurrency`, and never "
             "applied to the Zotero Connector pass, which drives a "
             "single Zotero desktop.",
    )
    parser.add_argument(
        "--filter-keys-file",
        help="Path to a text file of Zotero item keys (one per line) "
             "to restrict processing to.",
    )
    zotero_io.add_library_args(parser)
    parser.add_argument(
        "--on-first-failure", default="",
        choices=("", "keep", "skip", "always_skip"),
        help="Answer for the per-publisher failure prompt in non-interactive "
             "runs. Default (empty) asks on a TTY and uses 'skip' when "
             "stdin is piped. 'always_skip' also writes the publisher to "
             "[library] no_access in config.toml.",
    )
    parser.add_argument(
        "--no-prompt", action="store_true",
        help="Bypass the interactive-surface check and auto-skip on first "
             "publisher failure. Useful for unattended runs (cron, agent "
             "loops). Equivalent to --on-first-failure=skip plus skipping "
             "the upfront 'no terminal detected' guard. Browser-cascade "
             "items that require live confirmation will be silently "
             "skipped — check the run log to see which publishers were "
             "bypassed.",
    )
    parser.add_argument(
        "--control-file", default="",
        help="Ask interactive questions through this JSON file instead of "
             "a terminal. The browser window still opens and you still "
             "solve each challenge yourself; the prompt is written here as "
             '{\"state\": \"awaiting_user\", \"prompt\": ..., \"seq\": N} '
             "and the run waits for "
             '{\"seq\": N, \"answer\": \"...\"} in <path>.reply. This is how '
             "an agent drives the browser pass from a background process "
             "with no controlling TTY.",
    )
    parser.add_argument(
        "--auto-publishers", action="store_true",
        help="Take the item list from the audit's browser retry set "
             f"({AUDIT_KEYS_STEM}.retry.browser.keys) instead of "
             "--filter-keys-file. Run `audit_zotero_library.py "
             "--pdf-fetch-log` first; this reuses its triage rather than "
             "re-deriving which items a browser pass can recover.",
    )
    parser.add_argument(
        "--allow-preprints", action="store_true",
        help="Also look for a preprint copy (arXiv / SSRN / RePEc) when "
             "every other source has failed. OFF by default: a preprint is "
             "the manuscript before peer review, and coding one as the "
             "published article misreports what the journal actually "
             "published. Every attachment this produces is tagged "
             "`pdf:preprint-version`, and the coding stage surfaces the tag "
             "— review those items before trusting their coded rows.",
    )
    parser.add_argument(
        "--progress-json", default="",
        help="Append one JSON object per line to this file as the browser "
             "pass progresses (publisher_start / item / publisher_done / "
             "run_done). Lets an agent driving a background run report "
             "progress without parsing stdout, which is written for a "
             "person. The file is truncated at start — one file per run.",
    )
    parser.add_argument(
        "--control-timeout", type=float, default=1800.0,
        help="Seconds to wait for a reply on --control-file before giving "
             "up (default: 1800). Generous by design — the wait is a human "
             "solving a Cloudflare challenge.",
    )
    parser.add_argument(
        "--all", action="store_true",
        help="Run Pass 1 (API cascade) and Pass 2 (browser + Connector) in "
             "one invocation. Pass 2 only processes items Pass 1 couldn't "
             "attach. Equivalent to running enrich_pdfs.py then "
             "enrich_pdfs.py --sources browser. Cannot be combined with "
             "--sources.",
    )
    parser.add_argument(
        "--report", action="store_true",
        help="Print the run report for an existing --log-csv and exit "
             "without fetching anything. Groups every item by outcome, "
             "gives citations for the ones still missing a PDF, and names "
             "the next lever for each failure bucket.",
    )
    parser.add_argument(
        "--plan", action="store_true",
        help="(browser mode) Classify items and print the publisher queue — "
             "including which publishers will need an interactive "
             "Cloudflare / SSO solve — then exit without opening a browser. "
             "Run this first so you know what the real run will ask of you.",
    )
    parser.add_argument(
        "--ignore-library-coverage", action="store_true",
        help="Don't let the library link-resolver pre-flight gate the "
             "Connector pass. Use when the resolver reports no coverage "
             "for journals your library actually subscribes to (it keys on "
             "DOI and misses aggregator-hosted holdings). To exempt a "
             "single publisher rather than disabling the gate wholesale — "
             "one you reach via a society membership or a second "
             "institution's login — list its handler name in "
             "[library] direct_access in config.toml.",
    )
    parser.add_argument(
        "--no-check-text", action="store_true",
        help="Skip the post-attach text check. By default each attached "
             "PDF is checked for extractable text, so a file that is "
             "structurally intact but yields nothing downstream is "
             "reported as `attached_no_text` instead of counting as a "
             "clean success.",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()

    if args.all and args.sources:
        print("ERROR: --all cannot be combined with --sources.",
              file=sys.stderr)
        return 2

    if args.report:
        rows = pdf_run_report.read_log(args.log_csv)
        if args.filter_keys_file:
            # A run-log accumulates across the whole library, so an
            # unscoped report can bury the 20 items the user asked about
            # under the 1,500 they didn't.
            with open(args.filter_keys_file) as f:
                keys = {line.strip() for line in f if line.strip()}
            rows = [r for r in rows if (r.get("item_key") or "").strip() in keys]
        print(pdf_run_report.format_report(rows))
        return 0

    # `--no-prompt` documents itself as "equivalent to
    # --on-first-failure=skip plus skipping the upfront guard", but only
    # ever did the second half: the flag was read once, at the guard, and
    # never reached `_prompt_on_first_failure`. On a real TTY an
    # unattended run would still block forever on a readline() the caller
    # had explicitly opted out of.
    if args.no_prompt and not args.on_first_failure:
        args.on_first_failure = "skip"

    source_names = [
        _SOURCE_ALIASES.get(s.strip().lower(), s.strip().lower())
        for s in args.sources.split(",") if s.strip()
    ]
    if "preprint" in source_names and not args.allow_preprints:
        # The flag is where the hazard is explained, so it has to be the
        # only door in. Letting `--sources preprint` do the same thing
        # silently would leave a user who never read that explanation
        # coding working papers as published articles.
        print(
            "ERROR: --sources preprint also needs --allow-preprints.\n"
            "  A preprint is the manuscript before peer review; coding one "
            "as the published\n"
            "  article misreports what the journal published. Pass "
            "--allow-preprints to\n"
            "  accept that, and expect every attachment to be tagged "
            "`pdf:preprint-version`.",
            file=sys.stderr,
        )
        return 2

    browser_modes = [s for s in source_names if s in ("browser", "connector")]
    if browser_modes and len(source_names) > 1:
        print(
            f"ERROR: --sources {args.sources!r} mixes the browser pass with "
            f"other fetchers. They are separate passes over the same items, "
            f"not a single cascade. Use --all to run the API cascade and "
            f"then the browser pass, or run them one at a time.",
            file=sys.stderr,
        )
        return 2

    os.makedirs(args.cache_dir, exist_ok=True)
    run_date = date.today().isoformat()
    done_items = _load_done_items(args.log_csv)

    if args.auto_publishers:
        if args.filter_keys_file:
            print(
                "ERROR: --auto-publishers and --filter-keys-file both choose "
                "the item list. Pass one.",
                file=sys.stderr,
            )
            return 2
        keys_file, publishers = _auto_publisher_keys()
        if not keys_file:
            print(
                f"ERROR: --auto-publishers found no retry set at "
                f"{AUDIT_KEYS_STEM}.retry.browser.keys.\n"
                f"  Run the triage that produces it first:\n"
                f"    uv run audit_zotero_library.py --pdf-fetch-log\n"
                f"  Without it there is nothing to scope this run to, and a "
                f"browser pass over the whole library is not what you want.",
                file=sys.stderr,
            )
            return 2
        args.filter_keys_file = keys_file
        print(
            f"--auto-publishers: using {keys_file}"
            + (f" (publishers: {', '.join(publishers)})" if publishers else ""),
            flush=True,
        )

    config = _load_config()
    session = http_client.build_session(mailto=config.crossref_mailto)

    # Validate Zotero config via require() — surfaces a clear error.
    require("zotero", "api_key", env="ZOTERO_API_KEY")
    if not getattr(args, "user", False) and not args.group:
        try:
            zot = zotero_io.ZoteroClient.from_config(group_id=None)
        except zotero_io.GroupSelectionRequired as e:
            print(zotero_io.format_group_selection_error(e.groups), file=sys.stderr)
            return 2
    else:
        zot = zotero_io.ZoteroClient.from_args(args)

    if args.filter_keys_file:
        # Fetch only what was asked for. Walking the whole library and
        # filtering in Python costs a full paginated sweep per invocation
        # — repeated on every backoff retry — and a live run against a
        # ~10,000-item library was rate-limited during that enumeration
        # and never reached the retrieval it was invoked for.
        if not os.path.isfile(args.filter_keys_file):
            print(f"ERROR: --filter-keys-file not found: {args.filter_keys_file}",
                  file=sys.stderr)
            return 2
        with open(args.filter_keys_file) as f:
            target = {line.strip() for line in f if line.strip()}
        print(f"Fetching {len(target)} Zotero items by key...", end=" ", flush=True)
        fetched = zot.items_by_keys(target)
        all_items, not_articles = select_requested_articles(fetched, target)
        print(f"{len(all_items)} journal articles.", flush=True)
        if not_articles:
            # Distinguished from "key not found": a book chapter that was
            # asked for and skipped is a scope decision, not a typo, and
            # reporting both as "matched no journal article" hid which.
            print(
                f"  NOTE: {not_articles} key(s) resolved to non-journalArticle "
                f"items and were skipped.",
                flush=True,
            )
        missing = target - {it["key"] for it in fetched}
        if missing:
            # Silence here used to hide typo'd keys and non-journalArticle
            # items, which then looked identical to "nothing to do".
            print(
                f"  WARN: {len(missing)} key(s) in the file do not exist in "
                f"this library: {', '.join(sorted(missing)[:5])}"
                + (" …" if len(missing) > 5 else ""),
                flush=True,
            )
    else:
        print("Fetching Zotero items...", end=" ", flush=True)
        all_items = zot.journal_articles()
        print(f"{len(all_items)} journal articles.", flush=True)

    # Everything this invocation is accountable for — the report is
    # scoped to these so a filtered run doesn't dump the whole log.
    scope_keys = {it["key"] for it in all_items}

    # Items with DOI that haven't already been attached
    candidates = [
        it for it in all_items
        if (it.get("data", {}).get("DOI") or "").strip()
        and it["key"].strip().lower() not in done_items
    ]
    print(f"Items not yet processed: {len(candidates)}", flush=True)

    print("Checking for existing PDF attachments...", end=" ", flush=True)
    pdf_map = zot.pdf_map()
    to_process: list[dict] = []
    stubs_deleted = 0
    for it in candidates:
        key = it["key"]
        has_real, stubs = pdf_map.get(key, (False, []))
        for stub_key in stubs:
            try:
                zot.delete_item(stub_key)
                stubs_deleted += 1
            except Exception as e:
                print(f"  stub delete {stub_key} failed: {e}", flush=True)
        if not has_real:
            to_process.append(it)
    print(
        f"{len(to_process)} items without real PDF"
        + (f" ({stubs_deleted} stubs deleted)" if stubs_deleted else "") + ".",
        flush=True,
    )
    if not to_process:
        _print_run_report(args, zot, scope_keys)
        return 0

    # Recover PDFs already on disk before fetching anything. A fetch that
    # succeeded but whose upload failed leaves a perfectly good file in
    # the cache; nothing used to go back for it, so a live run lost 48
    # PDFs it had already paid to download.
    if not args.dry_run:
        log_fh, log_writer = _open_log(args.log_csv)
        try:
            recovered = _attach_from_cache(
                to_process, zot, log_writer, args, run_date,
            )
        finally:
            log_fh.close()
        if recovered:
            done = {it["key"] for it in recovered}
            to_process = [it for it in to_process if it["key"] not in done]
            if not to_process:
                _print_run_report(args, zot, scope_keys)
                return 0

    # Browser path: drives fetchers.browser handlers in-process. The
    # `sources` list is ignored here — handlers are picked per-publisher.
    # `--sources connector` skips Pass 1/2 and sends every item
    # directly to the Connector (useful for targeted validation).
    if browser_modes:
        _install_interaction_channel(args)
        log_fh, log_writer = _open_log(args.log_csv)
        try:
            rc = _run_browser_in_process(
                to_process, zot, log_writer, args, run_date,
                connector_only=(browser_modes == ["connector"]),
                session=session, config=config,
            )
        finally:
            log_fh.close()
        if not args.plan:
            _print_run_report(args, zot, scope_keys)
        return rc

    # --all: run API cascade first, then re-read pdf_map for residuals
    # and run the browser pipeline.
    if args.all:
        print("\n--- Pass 1: API cascade ---", flush=True)
        sources = fetchers.pdf_sources(
            session, config, allow_preprints=args.allow_preprints,
        )
        print(f"Active fetchers: {[s.name for s in sources]}", flush=True)

        log_fh, log_writer = _open_log(args.log_csv)
        try:
            attached, no_pdf, failed = _run_api_cascade(
                to_process, sources, args, run_date, zot, log_writer,
            )
        finally:
            log_fh.close()
        print(
            f"\n  Pass 1 summary: attached={attached}, "
            f"no-pdf={no_pdf}, failed={failed}",
            flush=True,
        )

        # Pass 2 residuals — re-read pdf_map so items Pass 1 attached
        # drop out automatically.
        print("\n--- Pass 2: browser + Connector ---", flush=True)
        updated_pdf_map = zot.pdf_map()
        residuals = [
            it for it in to_process
            if not updated_pdf_map.get(it["key"], (False, []))[0]
        ]
        print(
            f"  {len(residuals)} items still missing PDF after Pass 1.",
            flush=True,
        )
        if not residuals:
            _print_run_report(args, zot, scope_keys)
            return 0

        _install_interaction_channel(args)
        log_fh, log_writer = _open_log(args.log_csv)
        try:
            rc = _run_browser_in_process(
                residuals, zot, log_writer, args, run_date,
                session=session, config=config,
            )
        finally:
            log_fh.close()
        if not args.plan:
            _print_run_report(args, zot, scope_keys)
        return rc

    # Default / explicit-sources path: API cascade only.
    sources = fetchers.pdf_sources(
        session, config, names=source_names if source_names else None,
        allow_preprints=args.allow_preprints,
    )
    if not sources:
        print(f"ERROR: no PDF fetchers matched --sources={args.sources!r}",
              file=sys.stderr)
        return 2
    print(f"Active fetchers: {[s.name for s in sources]}", flush=True)

    log_fh, log_writer = _open_log(args.log_csv)
    try:
        attached, no_pdf, failed = _run_api_cascade(
            to_process, sources, args, run_date, zot, log_writer,
        )
    finally:
        log_fh.close()
    print(
        f"\nDone. attached={attached}, no-pdf={no_pdf}, failed={failed}",
        flush=True,
    )
    _print_run_report(args, zot, scope_keys)
    return 0


if __name__ == "__main__":
    sys.exit(main())
