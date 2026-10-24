"""Single entry point for Zotero I/O across the pipeline scripts.

Every pipeline script (both read-only and read-write) routes through
`ZoteroClient`. The class wraps pyzotero — it does not reimplement the
Zotero REST API.

Design notes:
    - **Zotero Desktop's local API (localhost:23119) is the only
      surface.** Reads, writes, file uploads and merges all go there;
      the plugin has no Web API path, no zotero.org key and no
      fallback. Zotero 10 is required: it is the first release that
      accepts local writes and uploads. Desktop syncs to zotero.org on
      its own, which is no concern of this module.
    - The zotero.org Web API was dropped on 2026-10-24. Two surfaces
      kept unrelated version counters, so a version read from one and
      sent to the other was rejected 412 every time; writes through the
      Web API were invisible to local reads until Desktop synced, which
      a re-run read as "already done" and duplicated an import; and an
      upload landed in the cloud minutes before Desktop saw it, racing
      every local re-parent. With one surface none of that can happen.
    - **Local writes need Desktop's own key** (`[zotero] local_api_key`,
      unrelated to any zotero.org credential), granted once through
      Zotero's consent dialog. `/setup` grants it and proves a write
      works; `_LocalZotero` re-grants it on the write that finds it
      missing or revoked, saves the new one, and resends that write
      once. See `_LocalZotero`.
    - No item templates: the local API does not serve `/items/new`, so
      pyzotero's `item_template()` and the `attachment_*` helpers built
      on it raise there. `attach_pdf` builds its own (see
      `_IMPORTED_FILE_TEMPLATE`).
    - tenacity wraps `update_abstract` to retry on version conflicts
      (HTTP 412): we re-fetch the item, re-apply the abstract, and
      re-PATCH.
    - No module-level key read. Callers instantiate via
      `ZoteroClient.from_config()` / `from_args()`, which go through
      `core.config_loader`.

Attribution:
    `merge_duplicate_item` is a port of the `merge_duplicates` function
    from zotero-mcp (MIT-licensed) at
    `src/zotero_mcp/tools/write.py` — specifically the execute path
    (tag union, collection union, child re-parenting with attachment-
    signature dedup, and trash-via-PATCH). Adapted to our single-keeper
    single-duplicate signature, our logger in place of FastMCP's
    `Context`, and to raise on failure rather than return a diagnostic
    string.
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import sys
import time
import warnings
from collections import defaultdict
from collections.abc import Iterable, Sequence
from pathlib import Path

# pyzotero ≤1.11 uses a deprecated `whenever.ZonedDateTime.py_datetime()`
# API that spams WheneverDeprecationWarning on every write. The warning
# is benign — `py_datetime` still works — but it buries real output.
# Remove this filter once pyzotero releases a fix.
try:
    import whenever
    warnings.filterwarnings(
        "ignore", category=whenever.WheneverDeprecationWarning,
    )
except Exception:
    pass

from pyzotero import errors as _pyzotero_errors
from pyzotero import zotero
from tenacity import (
    retry,
    retry_if_exception,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)

# HTTP statuses worth a second attempt on the attachment upload path:
# rate-limiting and server-side faults. Anything else (401/403/404/413,
# a malformed request) will fail identically on retry.
_RETRYABLE_UPLOAD_STATUSES = frozenset({429, 500, 502, 503, 504})


#: Status codes pyzotero gives a dedicated exception class, inverted so
#: an exception's TYPE yields its status. Built from pyzotero's own map
#: rather than restated, so a class it adds later is picked up free.
_PYZOTERO_ERROR_STATUS: dict[type, int] = {
    cls: code for code, cls in _pyzotero_errors.ERROR_CODES.items()
}

#: `Code: 503` — the first line of pyzotero's formatted error message.
_PYZOTERO_CODE_RE = re.compile(r"\bCode:\s*(\d{3})\b")


def _http_status_of(exc: BaseException) -> int | None:
    """The HTTP status behind an exception, across two pyzotero eras.

    Through 1.14, pyzotero let httpx's `HTTPStatusError` out of any
    non-2xx call and the status was on `.response.status_code`. 1.15
    raises its own typed errors, which are not httpx subclasses and
    carry no response at all — so the old `except httpx.HTTPStatusError`
    matched nothing and every 412 retry here quietly became dead code.

    Three sources, most reliable first: the httpx response if there is
    one; the exception's type, for the statuses pyzotero classifies; and
    finally the `Code: NNN` line its message formatter always writes,
    which is the only signal for a 5xx — those map to the generic
    `HTTPError` with no class of their own, and the upload retry needs
    to tell a 503 from a 404.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status

    for cls, code in _PYZOTERO_ERROR_STATUS.items():
        if type(exc) is cls:
            return code
    if isinstance(exc, _pyzotero_errors.PyZoteroError):
        for cls, code in _PYZOTERO_ERROR_STATUS.items():
            if isinstance(exc, cls):
                return code
        match = _PYZOTERO_CODE_RE.search(str(exc))
        if match:
            return int(match.group(1))
    return None


def _is_transient_write_error(exc: BaseException) -> bool:
    """True for transient failures of a Zotero write: worth another try.

    The attachment upload is three network round-trips (register → auth
    → PUT), so a transport blip or a 503 anywhere in the chain aborts
    the whole thing while the local PDF is perfectly good; a batched tag
    PATCH dies the same way. Those are worth retrying. A `RuntimeError`
    from `attach_pdf` is NOT — it means the Zotero API accepted the
    request and explicitly reported the file in its `failure` bucket,
    which retrying reproduces verbatim.

    pyzotero's `UploadError` is transient by construction: 1.16 raises it
    in exactly two places, both in the S3 PUT, for `httpx2.ConnectError`
    and `httpx2.TimeoutException` — re-raised `from None`, so there is no
    status and no httpx class left in the chain to recognise. Before it
    was named here the upload retry never fired for the very failure it
    was written for.
    """
    upload_error = getattr(_pyzotero_errors, "UploadError", None)
    if upload_error is not None and isinstance(exc, upload_error):
        return True
    status = _http_status_of(exc)
    if status is not None:
        return status in _RETRYABLE_UPLOAD_STATUSES
    return _is_httpx_error(exc, "TransportError")


#: The upload path's original name for the same test.
_is_retryable_upload_error = _is_transient_write_error


def _is_version_conflict(exc: BaseException) -> bool:
    """A 412 that a fresh read of the item's version can cure.

    `ServerIDMismatchError` is a 412 subclass, but it means the local
    API now answers for a different Zotero database: no re-read of a
    version fixes that, so retrying it three times only delays the
    error the user needs to see.
    """
    if _http_status_of(exc) != 412:
        return False
    mismatch = getattr(_pyzotero_errors, "ServerIDMismatchError", None)
    return not (mismatch is not None and isinstance(exc, mismatch))


def _is_httpx_error(exc: BaseException, *names: str) -> bool:
    """Whether `exc` is one of httpx's exception classes `names`, or a
    subclass — from **either** `httpx` or `httpx2`.

    pyzotero 1.15 moved to `httpx2`, a separate distribution whose
    classes share httpx's names but not its hierarchy:
    `issubclass(httpx2.ReadTimeout, httpx.TransportError)` is False. An
    `isinstance(exc, httpx.TransportError)` here therefore stopped
    matching anything pyzotero raised, and the upload retry quietly
    stopped retrying transport errors. Matching by class name across
    the MRO covers both, without importing a package this module's
    PEP 723 users may not have.
    """
    return any(
        c.__name__ in names and c.__module__.split(".")[0] in ("httpx", "httpx2")
        for c in type(exc).__mro__
    )


def _is_slow_local_read(exc: BaseException) -> bool:
    """A listing page that failed because the server was slow or dropped
    the connection mid-response — Zotero Desktop busy with another
    session's bulk write. Not `ConnectError`: nothing listening is not
    going to start listening within a backoff, and should fail fast."""
    return _is_httpx_error(
        exc, "TimeoutException", "ReadError", "RemoteProtocolError",
    )


#: Attempts per listing page. The whole listing is not restarted: a
#: 24k-attachment library is ~240 pages, and one busy moment should cost
#: a page, not the two minutes already spent.
_PAGE_ATTEMPTS = 4


#: Indirection so tests can skip the backoff: tenacity binds its own
#: `sleep` when `retry` is defined, out of reach of a monkeypatch.
_page_sleep = time.sleep

#: The same indirection for the write paths' backoff (batched tag
#: writes, attachment uploads).
_write_sleep = time.sleep

#: Attempts for one batched tag write that fails transiently (429/5xx,
#: transport). Each attempt re-reads the items, so a retry after a write
#: that landed but whose answer was lost finds them already tagged and
#: counts them unchanged rather than failing them on a stale version.
_TAG_BATCH_ATTEMPTS = 4

#: Passes over objects Zotero rejected one by one with a 412 (another
#: writer touched the item between our read and our write). Each pass
#: re-reads just those items.
_TAG_CONFLICT_ROUNDS = 3


def _post_items(z, payloads: list[dict]) -> dict:
    """POST `payloads` to the library's `/items` and return Zotero's
    per-object result map: `{"success": {idx: key}, "unchanged": {idx:
    key}, "failed": {idx: {"key", "code", "message"}}}`.

    Not pyzotero's `update_items`. Through 1.16 that discards the
    response body and returns `True` whenever the HTTP status is 2xx
    (`_batch_update`), and Zotero answers a multi-object write with 200
    *even when every object in it was rejected* — the rejections are in
    the body's `failed` map. Every per-object failure was therefore
    counted as applied. It also returns normally on a 429 that carries a
    Backoff header, having written nothing.

    Raises pyzotero's typed error for a non-2xx answer (via its own
    error handler, so `_http_status_of` reads it like any other), and
    `TooManyRequestsError` for that 429.
    """
    from pyzotero.zotero import build_url
    check_backoff = getattr(z, "_check_backoff", None)
    if callable(check_backoff):
        check_backoff()
    # Trailing slash as pyzotero's own `_batch_update` sends it.
    url = build_url(z.endpoint, f"/{z.library_type}/{z.library_id}/items/")
    resp = z._write("POST", url=url, json=payloads)
    if resp.status_code >= 400:
        # Raises for everything but a 429 with a Backoff header, which
        # it records on `z` so the next attempt's `_check_backoff` waits.
        _pyzotero_errors.error_handler(z, resp)
        raise _pyzotero_errors.TooManyRequestsError(
            f"\nCode: {resp.status_code}\nURL: {url}\nMethod: POST\n"
            f"Response: {resp.text}"
        )
    body = resp.json()
    if not isinstance(body, dict):
        raise ValueError(f"unexpected multi-object write response: {body!r}")
    return body


def _read_page(fetch):
    """`fetch()` with retries on `_is_slow_local_read`, backing off."""
    return retry(
        stop=stop_after_attempt(_PAGE_ATTEMPTS),
        retry=retry_if_exception(_is_slow_local_read),
        wait=wait_exponential(multiplier=2, max=30),
        sleep=lambda s: _page_sleep(s),
        reraise=True,
        before_sleep=lambda rs: logger.warning(
            "Zotero listing page failed (%s); retrying (attempt %d of %d)",
            type(rs.outcome.exception()).__name__,
            rs.attempt_number + 1, _PAGE_ATTEMPTS,
        ),
    )(fetch)()


def _everything(z: zotero.Zotero, first) -> list:
    """`z.everything(first())`, retrying each page rather than the lot.

    Live 2026-09-23: enrich_pdfs died at startup on `httpx2.ReadTimeout`
    one page into listing 23,995 attachments over the local API, while
    another session's 1,265-item trash kept Zotero Desktop busy.
    pyzotero only retries 429s.

    pyzotero's own `everything` still does the paging; only `follow` is
    wrapped for the duration, so the paging logic stays pyzotero's.
    Retrying `follow()` is safe because pyzotero replaces `z.links` only
    after a response has arrived, so a failed page leaves the same
    `next` link to ask for again.
    """
    page = _read_page(first)
    follow = getattr(z, "follow", None)
    if follow is None:              # a test double with no paging
        return z.everything(page)
    z.follow = lambda: _read_page(follow)
    try:
        return z.everything(page)
    finally:
        z.follow = follow



def slr_coding_marker(ns: str) -> str:
    """The `<h1>` that identifies one review's SLR Coding note.

    Namespaced, because `upsert_child_note` finds the plugin's own note by
    this marker and overwrites it. With a single fixed marker, two reviews
    coding the same paper in a shared library silently overwrote each
    other's coding — the second review's note replaced the first's, with no
    warning and no way to tell afterwards.

    `ns` is a namespace with its trailing separator (`"agentic-ai/"`), as
    returned by `tag_prefix.namespace`. The separator is stripped here so
    the heading reads as a title rather than a path.
    """
    return f"<h1>SLR Coding: {ns.rstrip('/')}</h1>"


def parse_slr_coding_note(note_html: str) -> dict | None:
    """Extract the machine-readable JSON payload from an SLR Coding
    note written by `fulltext_code._build_slr_coding_note_html`.

    Returns the decoded payload dict or `None` if no `SLR_CODING_DATA`
    comment is present or the JSON is malformed. Used by
    `export_coded_includes.py` to read coded fields from Zotero
    authoritatively, bypassing the CSV log entirely.
    """
    import json
    import re

    match = re.search(
        r"<!--\s*SLR_CODING_DATA:\s*(\{.*\})\s*-->",
        note_html,
        flags=re.DOTALL,
    )
    if not match:
        return None
    try:
        parsed = json.loads(match.group(1))
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        return None


class MergeRefused(ValueError):
    """`merge_duplicate_item` will not merge these two items, ever.

    Its own type so a caller can tell this permanent answer from a
    transient failure: a queued Connector merge that retried a DOI
    mismatch on every sweep kept its keeper out of the pass indefinitely
    (a figshare supplement saved for its article, 2026-09-25).
    """


class VersionConflictError(RuntimeError):
    """Raised by update_abstract when pyzotero returns HTTP 412.

    tenacity's @retry catches this and re-invokes the wrapped method,
    which re-fetches the item's current version before re-applying the
    patch.
    """


class GroupSelectionRequired(RuntimeError):
    """Raised by `ZoteroClient.from_config()` when the user hasn't picked
    a group and more than one (or zero) is accessible.

    Carries the list of accessible groups (as returned by the local
    API's /users/0/groups endpoint) so orchestrators can print an
    actionable menu to the user.
    """

    def __init__(self, groups: list[dict]):
        super().__init__(
            f"Zotero group selection required: {len(groups)} accessible groups"
        )
        self.groups = groups


def _personal_library() -> zotero.Zotero:
    """A read-only local client on the personal library, `users/0`.

    The local API serves the personal library only under `0` ("the
    logged-in user"), and answers the groups listing there too.
    """
    return zotero.Zotero("0", "user", None, local=True)


def _list_accessible_groups() -> list[dict]:
    """`{id, name}` for every group Zotero Desktop holds.

    The groups Desktop has synced, which are the ones the user belongs
    to; their IDs are the real group IDs, which the local API serves
    them under. Returns an empty list on any failure (Desktop not
    running, malformed response). The caller handles both "empty because
    failure" and "empty because user has no group memberships" the same
    way — prompting them to specify --group explicitly.
    """
    try:
        z = _personal_library()
        data = _everything(z, lambda: z.groups())
    except Exception:
        return []
    out: list[dict] = []
    for g in data if isinstance(data, list) else []:
        gid = g.get("id")
        gdata = g.get("data", {}) or {}
        name = gdata.get("name") or f"group {gid}"
        if gid is not None:
            out.append({"id": gid, "name": name})
    return out


#: The zotero.org user ID in the `alternate` link the local API returns
#: with every listing: `<https://www.zotero.org/users/5591/items>`.
_ALTERNATE_USER_RE = re.compile(
    r"<https?://(?:www\.)?zotero\.org/users/(\d+)/[^>]*>\s*;\s*rel=\"alternate\"",
)


def local_user_id() -> str:
    """The zotero.org user ID Desktop is signed in as, or "" if none.

    The local API names the personal library `users/0`, which is no
    good in a relation URI: Desktop stores relations as
    `http://zotero.org/users/<id>/items/<key>`. Desktop gives the real
    ID away in the `alternate` link of any listing, so it is read from
    there rather than asked of the user. An install never signed in to
    zotero.org has no such link and gets "".
    """
    try:
        z = _personal_library()
        z.items(limit=1)
        link = z.request.headers.get("link", "")
    except Exception:
        return ""
    match = _ALTERNATE_USER_RE.search(link)
    return match.group(1) if match else ""


def find_group_by_name(name: str) -> dict | None:
    """Look up a Zotero group by exact display-name match.

    Queries the groups Desktop holds (`_list_accessible_groups`) and
    returns the `{"id", "name"}` dict for the unique exact match, or
    `None` if no group has that name. Raises `ValueError` if more than
    one group shares the name — ambiguous, never guess.
    """
    groups = _list_accessible_groups()
    matches = [g for g in groups if g.get("name") == name]
    if len(matches) > 1:
        raise ValueError(
            f"Ambiguous Zotero group name {name!r}: {len(matches)} matches "
            f"(ids {[g['id'] for g in matches]}). Rename one of the groups "
            f"or resolve by id instead."
        )
    return matches[0] if matches else None


def format_group_selection_error(groups: list[dict]) -> str:
    """Render the help message shown when GroupSelectionRequired fires.

    Kept as a module-level helper so orchestrators can share wording.
    """
    lines = ["ERROR: Zotero group not specified."]
    if groups:
        lines.append("")
        lines.append("Your accessible Zotero groups:")
        for g in groups:
            lines.append(f"  {g['id']:<12} {g.get('name', '?')}")
        lines.append("")
        lines.append("Either:")
        lines.append("  • pass --group <id> on the command line, or")
        lines.append("  • export ZOTERO_GROUP=<id> in your shell.")
    else:
        lines.append(
            "Zotero Desktop listed no groups (it is not running, or this "
            "account belongs to none). Start Zotero, or pass --group <id> "
            "directly, or --user for your personal library."
        )
    return "\n".join(lines)


def add_library_args(parser: argparse.ArgumentParser) -> None:
    """Add `--group` / `--user` (mutually exclusive).

    Pipeline scripts call this once on their argparse parser, then
    pass the parsed namespace to `ZoteroClient.from_args(args)`. This
    keeps the user-vs-group choice in one place rather than repeated
    across every script that touches Zotero.
    """
    import os
    target = parser.add_mutually_exclusive_group()
    target.add_argument(
        "--group", default=os.environ.get("ZOTERO_GROUP", ""),
        help="Zotero group library ID (numeric). Default: $ZOTERO_GROUP.",
    )
    target.add_argument(
        "--user", action="store_true",
        help="Use your personal (My Library) instead of a group library.",
    )


#: Zotero's `imported_file` attachment template, written out rather than
#: fetched.
#:
#: The local API has no `/items/new`, so there is nothing to fetch it
#: from. Before that mattered it used to call the cloud client's
#: `_attachment_template("imported_file")`, pyzotero's private wrapper
#: around `GET /items/new?itemType=attachment&linkMode=imported_file`,
#: and two reasons had already made that go, one of them live:
#:
#: 1. **It breaks on long runs.** `item_template` caches the response,
#:    and on a cache entry older than an hour `_updated()` re-validates
#:    by issuing `GET /items/new` *with no query parameters at all*
#:    (pyzotero 1.13.7, `_client.py:381`). The API answers
#:    `400 'itemType' not provided`, so every upload fails. Seen live:
#:    a Springer block downloaded 110 PDFs and attached none of them,
#:    each one logged `UnsupportedParamsError ... 'itemType' not
#:    provided`. Short runs never hit it; a run that has been going for
#:    an hour fails every remaining attachment.
#: 2. It is a private symbol, and this repo already has a rule against
#:    depending on those across a package boundary.
#:
#: The shape is Zotero's documented attachment schema and is stable —
#: `Zupload` only requires `filename`, and fills `contentType` itself
#: when it is empty.
_IMPORTED_FILE_TEMPLATE: dict = {
    "itemType": "attachment",
    "linkMode": "imported_file",
    "title": "",
    "accessDate": "",
    "url": "",
    "note": "",
    "tags": [],
    "relations": {},
    "contentType": "",
    "charset": "",
    "filename": "",
}


#: Attachment link modes whose bytes Zotero stores (and hashes). Only
#: these can be "stubs"; see `ZoteroClient.pdf_map`.
_STORED_LINK_MODES = frozenset({"imported_file", "imported_url"})


#: The name Zotero shows in its consent dialog. The same one `/setup`
#: asks under, so the user sees one app in Zotero's key list, not two.
LOCAL_APP_NAME = "academic-research"

#: How long a key grant may wait. The dialog waits for a person, which
#: pyzotero's 30 s default does not allow for.
_AUTHORIZE_TIMEOUT_S = 180

#: The local API's two bodies for a write without a usable key
#: (pyzotero maps both to `LocalAPIKeyRequiredError`).
_KEY_REJECTED_BODIES = ("API key required", "Invalid or expired API key")


class LocalKeyRefused(RuntimeError):
    """Zotero would not grant a local write key: Deny, a one-time
    "Allow", the rate limit, or no answer in time. Not retryable — the
    message says what the user has to do."""


def _save_local_api_key(key: str) -> None:
    """Persist a freshly granted key to `[zotero] local_api_key`.

    A failure to save is logged, never raised: the write that asked for
    the key can still go ahead with it, and the next run asks again.
    """
    import os
    if os.environ.get("ZOTERO_LOCAL_API_KEY"):
        logger.warning(
            "ZOTERO_LOCAL_API_KEY is set and overrides config.toml, so the "
            "new Zotero key saved there will not be used until it is unset.",
        )
    try:
        from core import config_writer
        config_writer.set_value("zotero", "local_api_key", key)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Could not save the new Zotero local key: %s", exc)


class _LocalZotero(zotero.Zotero):
    """pyzotero's local client, granting itself a write key on demand.

    Every write pyzotero makes — its own methods, `_post_items`,
    `_set_deleted`, `Zupload`'s item creation — goes through `_write`,
    so this is the one place a missing or revoked key can be caught. On
    the local API's 401 for a key it asks Zotero for a new one (its
    consent dialog; the user picks "Always Allow"), saves it, and sends
    the write again, once. A second 401 in the same process is not
    answered with a second dialog: that is a key Zotero keeps revoking,
    or an "Allow" that only covered one write, and looping on it would
    hit the endpoint's rate limit.

    `on_new_key` is how the key reaches config; tests replace it.
    """

    def __init__(self, *args, on_new_key=_save_local_api_key, **kwargs):
        super().__init__(*args, **kwargs)
        self._on_new_key = on_new_key
        self._regranted = False

    def _write(self, method: str, url: str, **kwargs):
        resp = super()._write(method, url, **dict(kwargs))
        if not self._key_rejected(resp) or self._regranted:
            return resp
        self._regranted = True
        logger.warning(
            "Zotero refused the local write key (%s). Asking Zotero for a "
            "new one: answer the dialog in Zotero with 'Always Allow'.",
            "none configured" if not self.local_api_key else "revoked",
        )
        self.local_api_key = self._grant_key()
        self._on_new_key(self.local_api_key)
        return super()._write(method, url, **dict(kwargs))

    @staticmethod
    def _key_rejected(resp) -> bool:
        if getattr(resp, "status_code", None) != 401:
            return False
        text = getattr(resp, "text", "") or ""
        return any(body in text for body in _KEY_REJECTED_BODIES)

    def _grant_key(self) -> str:
        """Run Zotero's consent dialog and return a key worth storing.

        pyzotero's `authorize_local`, but with a timeout long enough for
        a person to answer, and refusing a one-time "Allow": that key is
        spent by the write it was asked for, and the next write would
        open the dialog again.
        """
        from pyzotero.zotero import build_url
        resp = self._send(
            "POST",
            url=build_url(self.endpoint, "/local/authorize"),
            headers={"Zotero-Server-ID": self._ensure_server_id()},
            json={"appName": LOCAL_APP_NAME},
            timeout=_AUTHORIZE_TIMEOUT_S,
        )
        if resp.status_code == 403:
            raise LocalKeyRefused(
                "Zotero write access was denied in its dialog. Run the "
                "command again and choose 'Always Allow'.",
            )
        if resp.status_code == 429:
            raise LocalKeyRefused(
                "Zotero is rate-limiting key requests; wait a minute and "
                "run the command again.",
            )
        if resp.status_code >= 400:
            _pyzotero_errors.error_handler(self, resp)
        grant = resp.json()
        key = (grant.get("key") or "").strip()
        if not key or not grant.get("remember"):
            raise LocalKeyRefused(
                "Zotero granted one-time access ('Allow'), which covers a "
                "single write. Run the command again and choose 'Always "
                "Allow'.",
            )
        return key


class ZoteroClient:
    """Thin pyzotero wrapper used by every pipeline script.

    Usage:
        zot = ZoteroClient.from_config()
        for item in zot.journal_articles():
            ...
        zot.update_abstract(item_key, abstract_text)
        zot.attach_pdf(item_key, "/path/to/file.pdf")
    """

    #: Class-level default. Several tests build a client with `__new__`
    #: to exercise one method against a stub, bypassing `__init__`.
    local_api_key: str | None = None

    def __init__(
        self,
        group_id: str,
        *,
        library_type: str = "group",
        local_api_key: str | None = None,
    ):
        """`group_id` is the group's ID, or for `library_type="user"`
        anything at all: the local API serves the personal library only
        as `users/0`, and that is what is used.

        `local_api_key` is Zotero Desktop's own write key. None is fine:
        the first write then asks Zotero for one (see `_LocalZotero`).
        The alternate constructors read it from config, so it is explicit
        here and unit tests that build a client directly touch no config.
        """
        self.group_id = "0" if library_type == "user" else str(group_id)
        self.library_type = library_type
        self.local_api_key = local_api_key or None
        self._local: zotero.Zotero | None = None

    @staticmethod
    def _configured_local_api_key() -> str:
        from core.config_loader import get
        return get("zotero", "local_api_key", env="ZOTERO_LOCAL_API_KEY")

    @classmethod
    def from_config(cls, group_id: str | None = None) -> ZoteroClient:
        """Instantiate from ~/.config/academic-research/config.toml.

        `group_id` is per-project (set by the caller from a --group CLI
        flag or $ZOTERO_GROUP) and is NOT stored in the global config.
        See the convention note in tests/unit/test_setup_wizard.py:40-42.

        When group_id is not provided, asks Zotero Desktop which groups
        it holds. If exactly one exists, uses it automatically.
        Otherwise raises a GroupSelectionRequired exception carrying the
        list of groups so orchestrators can print an actionable error.
        """
        import os

        if not group_id:
            group_id = os.environ.get("ZOTERO_GROUP", "").strip()
        if not group_id:
            # User didn't specify a group — ask Zotero which ones they
            # can access, auto-pick if there's only one.
            groups = _list_accessible_groups()
            if len(groups) == 1:
                group_id = str(groups[0]["id"])
                print(
                    f"ZoteroClient: auto-selected sole accessible group "
                    f"{group_id} ('{groups[0].get('name', '?')}').",
                    file=sys.stderr,
                )
            else:
                raise GroupSelectionRequired(groups)
        return cls(group_id, local_api_key=cls._configured_local_api_key())

    @classmethod
    def for_user_library(cls) -> ZoteroClient:
        """Alternate constructor for the personal (user) library."""
        return cls(
            "0", library_type="user",
            local_api_key=cls._configured_local_api_key(),
        )

    @classmethod
    def from_args(cls, args: argparse.Namespace) -> ZoteroClient:
        """Build a client from parsed `--group` / `--user` argparse args.

        Expects the parser was set up with `add_library_args(parser)`.
        Returns a client pointing at either a group library (numeric
        `--group <id>`) or the user's personal library (`--user`).
        Raises SystemExit with an actionable message if neither is
        set (and `$ZOTERO_GROUP` isn't populated).
        """
        import os

        is_user = bool(getattr(args, "user", False))
        group = getattr(args, "group", "") or os.environ.get("ZOTERO_GROUP", "")
        if not is_user and not group:
            raise SystemExit(
                "ERROR: --group <id> or --user is required "
                "(or set $ZOTERO_GROUP).",
            )
        if is_user:
            return cls.for_user_library()
        return cls(group, local_api_key=cls._configured_local_api_key())

    # -----------------------------------------------------------------
    # Internal — the pyzotero client. Lazily created so ZoteroClient()
    # in a unit test doesn't touch the network.
    # -----------------------------------------------------------------

    @property
    def local(self) -> zotero.Zotero:
        if self._local is None:
            # pyzotero >= 1.15.1 sends `local_api_key` as the
            # `Zotero-API-Key` header on local writes, and discovers the
            # companion `Zotero-Server-ID` from a response header itself.
            self._local = _LocalZotero(
                self.group_id, self.library_type, None,
                local=True, local_api_key=self.local_api_key,
            )
        return self._local

    def _client(self) -> zotero.Zotero:
        return self.local

    # -----------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------

    #: Item types that can plausibly carry an abstract, and that a
    #: systematic review screens. `journalArticle` alone is too narrow for
    #: screening work: a review's included set routinely holds book
    #: chapters, reports and preprints, and those records need abstracts
    #: for exactly the same reason articles do. Excluding them silently
    #: shrinks the screening frame rather than reporting a gap.
    ABSTRACTABLE_ITEM_TYPES: tuple[str, ...] = (
        "journalArticle", "bookSection", "book", "report",
        "conferencePaper", "preprint", "thesis", "manuscript", "document",
    )

    def journal_articles(self) -> list[dict]:
        """All journalArticle items in the library.

        Narrow by design; see `abstractable_items()` for the screening
        frame, which is what most callers actually want.
        """
        z = self._client()
        return _everything(z, lambda: z.items(itemType="journalArticle"))

    def recent_items(self, limit: int = 50) -> list[dict]:
        """The `limit` most recently added items, newest first, any type.

        For polling "did something just arrive": listing every journal
        article instead took 95 s per poll on a 25,703-article library,
        this takes about half a second.
        """
        z = self._client()
        return z.items(sort="dateAdded", direction="desc", limit=limit)

    def abstractable_items(
        self, item_types: Sequence[str] | None = None,
    ) -> list[dict]:
        """Top-level items of every type that can carry an abstract.

        The Zotero API's `itemType` accepts `a || b` for a union, so this
        is one paginated sweep rather than one per type.
        """
        types = tuple(item_types or self.ABSTRACTABLE_ITEM_TYPES)
        z = self._client()
        return _everything(z, lambda: z.items(itemType=" || ".join(types)))

    #: Zotero's `itemKey` filter takes a comma-separated list; 50 is the
    #: documented ceiling per request.
    ITEM_KEY_BATCH = 50

    def items_by_keys(self, keys: Iterable[str]) -> list[dict]:
        """Fetch exactly these items, by key, in batches.

        The alternative — walk the whole library and filter in Python — is
        what the pipeline did whenever it was handed a
        `--filter-keys-file`, and it does not scale: on a ~10,000-item
        library that is a full paginated sweep per invocation, repeated on
        every backoff retry, to arrive at a few hundred items. A live run
        tripped Zotero's rate limiter during that enumeration and could
        not get past it, so retrieval made no progress however patiently
        it retried.

        Asking for 2,229 keys costs 45 requests here against roughly a
        hundred for the sweep, and — the part that matters under a rate
        limiter — the cost tracks what was requested rather than the size
        of the library.

        Missing keys are simply absent from the result: the caller knows
        what it asked for and is better placed to say what a gap means.
        """
        wanted = [k.strip() for k in keys if k and k.strip()]
        if not wanted:
            return []
        z = self._client()
        out: list[dict] = []
        for i in range(0, len(wanted), self.ITEM_KEY_BATCH):
            batch = wanted[i:i + self.ITEM_KEY_BATCH]
            out.extend(_everything(
                z, lambda batch=batch: z.items(itemKey=",".join(batch)),
            ))
        return out

    def top_items(self) -> list[dict]:
        """All top-level items (includes non-article types: book, report, etc.)."""
        z = self._client()
        return _everything(z, lambda: z.top())

    def all_attachments(self) -> list[dict]:
        """All attachment items in the library."""
        z = self._client()
        return _everything(z, lambda: z.items(itemType="attachment"))

    def collection_items(self, collection: str, *,
                         item_type: str = "journalArticle") -> list[dict]:
        """Items in a collection, named by key **or** display name, by type.

        Accepts a name because the CLI flags that feed this are written
        `--collection SLR` in the skills and by users. The argument used
        to go straight to the API as a key, which worked only by accident:
        Zotero's local HTTP server tolerates a name, which the Web API
        answered with a 404 naming nothing the user had typed. Resolving
        here keeps the behaviour from depending on that tolerance.
        """
        z = self._client()
        key = self._resolve_collection_for_read(collection, z)
        return _everything(z, lambda: z.collection_items(key, itemType=item_type))

    def _resolve_collection_for_read(self, collection: str, z) -> str:
        """A collection key for `collection`, resolving a name if needed.

        Anything already shaped like a key is returned untouched, so the
        common path costs no extra request.

        Resolution reads through `z`, the client the caller is already
        listing with.
        """
        wanted = (collection or "").strip()
        if not wanted:
            raise ValueError(
                "collection_items: no collection given (empty name or key)."
            )
        if self._COLLECTION_KEY_RE.match(wanted):
            return wanted

        collections = _everything(z, lambda: z.collections())
        matches = [
            c for c in collections
            if (c.get("data", {}) or {}).get("name", "") == wanted
        ]
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous collection name {wanted!r}: "
                f"{len(matches)} collections share it "
                f"(keys {[c.get('key') for c in matches]}). Pass the key "
                f"instead, or rename one of them."
            )
        if matches:
            return matches[0].get("key", "")

        # Name what exists: the API's own 404 says "Collection not found"
        # and repeats nothing the user typed, which is how a typo turns
        # into a hunt through the Zotero UI.
        names = sorted(
            (c.get("data", {}) or {}).get("name", "")
            for c in collections
            if (c.get("data", {}) or {}).get("name")
        )
        listed = ", ".join(names[:20]) + (" …" if len(names) > 20 else "")
        raise ValueError(
            f"No collection named {wanted!r} in {self.describe_library()}, "
            f"and it is not a collection key either. Collections here: "
            f"{listed or '(none)'}"
        )

    def real_pdf_map(
        self, *, stub_grace_seconds: int = 3600,
    ) -> dict[str, list[str]]:
        """{parent_key: [attachment_key, ...]} for PDF attachments holding bytes.

        The keys `pdf_map` reduces to a bare `has_real_pdf` boolean. Only
        `enrich_pdfs --replace` needs them — to delete the attachment a
        newly fetched PDF replaces — and it is a second pass over
        `all_attachments()`, so it stays a separate call rather than
        widening `pdf_map`'s return shape for every caller that does not.
        """
        by_parent = self._pdf_attachments_by_parent(
            stub_grace_seconds=stub_grace_seconds,
        )
        return {
            parent: [a["key"] for a in real]
            for parent, (real, _stubs, _linked) in by_parent.items() if real
        }

    def real_pdf_md5_map(
        self, *, stub_grace_seconds: int = 3600,
    ) -> dict[str, dict[str, str]]:
        """{parent_key: {attachment_key: md5}} for PDFs holding bytes.

        `real_pdf_map` plus each file's hash, from the same single walk:
        `--replace` needs both, the keys to delete and the hashes to tell
        a fetched copy of the same file from a real replacement.
        """
        by_parent = self._pdf_attachments_by_parent(
            stub_grace_seconds=stub_grace_seconds,
        )
        return {
            parent: {a["key"]: a["data"].get("md5") or "" for a in real}
            for parent, (real, _stubs, _linked) in by_parent.items() if real
        }

    def pdf_map(
        self, *, stub_grace_seconds: int = 3600,
    ) -> dict[str, tuple[bool, list[str]]]:
        """{parent_key: (has_real_pdf, [stub_keys])} across the whole library.

        A "real" PDF has a non-empty `md5`. A "stub" is a
        metadata-only attachment with no bytes (left behind by
        earlier failed uploads).

        Only *stored* attachments (`linkMode` imported_file /
        imported_url) can be stubs. A linked file lives on the user's
        disk and never carries an md5 in the API, so judging it by md5
        made every linked PDF older than an hour a "stub" — and
        enrich_pdfs deleted them, annotations and all. A linked-file PDF
        counts as the parent having a PDF here; it is never in a stub
        list, nor in `real_pdf_map`, whose keys `--replace` deletes. A
        linked URL is neither: there are no bytes in the library.

        Grace window: attachments whose `dateAdded` is within the
        last `stub_grace_seconds` (default 1h) are NOT classified
        as stubs — their md5 may just not have populated yet because
        Zotero Desktop is still uploading the file bytes. Deleting
        those prematurely would destroy an in-flight upload. After
        the grace window expires, a missing md5 genuinely indicates
        a failed upload.
        """
        return {
            k: (bool(real or linked), [s["key"] for s in stubs])
            for k, (real, stubs, linked) in self._pdf_attachments_by_parent(
                stub_grace_seconds=stub_grace_seconds,
            ).items()
        }

    def _pdf_attachments_by_parent(
        self, *, stub_grace_seconds: int = 3600,
    ) -> dict[str, tuple[list, list, list]]:
        """{parent_key: ([real], [stubs], [linked files])}, full dicts.

        The shared walk behind `pdf_map` and `real_pdf_map`; see
        `pdf_map` for what separates a real attachment from a stub, why
        only stored attachments can be either, and why the grace window
        exists. Linked URLs are left out altogether.
        """
        import datetime
        pdfs = [a for a in self.all_attachments()
                if a["data"].get("contentType") == "application/pdf"
                and a["data"].get("parentItem")]

        now = datetime.datetime.now(datetime.UTC)
        grace = datetime.timedelta(seconds=stub_grace_seconds)

        by_parent: dict[str, tuple[list, list, list]] = defaultdict(
            lambda: ([], [], []),
        )
        for pdf in pdfs:
            parent = pdf["data"]["parentItem"]
            link_mode = pdf["data"].get("linkMode")
            if link_mode == "linked_file":
                by_parent[parent][2].append(pdf)
                continue
            if pdf["data"].get("md5"):
                by_parent[parent][0].append(pdf)
                continue
            if link_mode not in _STORED_LINK_MODES:
                # linked_url, or no/unknown link mode: no bytes in the
                # library to be missing, so never a stub of ours.
                continue
            # No md5 — might be a stub, OR an in-flight upload.
            # Check dateAdded: if the attachment was added within
            # the grace window, treat as "real" (don't delete).
            added_raw = pdf["data"].get("dateAdded") or ""
            is_recent = False
            try:
                added = datetime.datetime.fromisoformat(
                    added_raw.replace("Z", "+00:00")
                )
                is_recent = (now - added) < grace
            except Exception:
                # Unparseable timestamp — err on the side of
                # preserving the attachment.
                is_recent = True
            if is_recent:
                by_parent[parent][0].append(pdf)
            else:
                by_parent[parent][1].append(pdf)

        return dict(by_parent)

    def children(self, item_key: str) -> list[dict]:
        """`item_key`'s child notes and attachments."""
        return self._client().children(item_key)

    def get_item(self, item_key: str) -> dict:
        """Fetch a single item's current payload (used for version refresh)."""
        return self._client().item(item_key)

    def library_ref(self) -> dict[str, str]:
        """Machine-readable library identity, for provenance records.

        `describe_library` is prose for a log line; this is the version
        a batch manifest carries so that applying it somewhere else can
        confirm it is about to write to the library it was emitted from.
        Tagging the right keys in the wrong library is a quiet, ugly
        failure — the keys exist in both, so nothing errors.
        """
        return {"kind": self.library_type, "id": str(self.group_id)}

    def describe_library(self) -> str:
        """Human-readable summary for log lines.

        Examples:
            "group 6015547 'AI in entrepreneurship'"
            "personal library"
        """
        if self.library_type == "user":
            return "personal library"
        name = self.group_name() or "?"
        return f"group {self.group_id} ({name!r})"

    def selected_local_library(self) -> dict | None:
        """Return the library currently highlighted in Zotero Desktop's
        left pane (i.e. where Connector saves would land).

        Queries Zotero Desktop's `/connector/getSelectedCollection`
        endpoint — separate from the `/api/*` surface that pyzotero
        wraps, so we call it over plain HTTP. Response shape:
            {
              "libraryID":   <local numeric ID>,
              "libraryName": "<human-readable name>",
              "libraryEditable": true,
              ...                       # more fields when a collection is selected
            }
        Returns None on any error (Desktop not running, endpoint
        missing on old Zotero, parse failure). Callers must tolerate
        None.
        """
        import json as _json
        import urllib.request
        url = "http://127.0.0.1:23119/connector/getSelectedCollection"
        req = urllib.request.Request(
            url, method="POST", data=b"{}",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=3) as resp:
                if resp.status != 200:
                    return None
                return _json.loads(resp.read())
        except Exception:
            return None

    def group_name(self) -> str | None:
        """The group's display name, as Zotero Desktop holds it.

        Used by the Connector pre-flight to compare against Zotero
        Desktop's currently-selected library name — lets us tell the
        user "matches" vs "mismatch" definitively rather than
        hedging with "is this the right library?". Returns None on
        any error; callers must tolerate the None case.

        Only applicable when `library_type == 'group'`; user libraries
        don't have a group endpoint.
        """
        if self.library_type != "group":
            return None
        for g in _list_accessible_groups():
            if str(g["id"]) == self.group_id:
                return g["name"]
        return None

    # -----------------------------------------------------------------
    # Writes (pyzotero handles the upload handshake and the
    # If-Unmodified-Since-Version headers).
    # -----------------------------------------------------------------

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(VersionConflictError),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    def update_abstract(self, item_key: str, abstract: str) -> bool:
        """Patch an item's abstractNote. Retries on HTTP 412 by
        re-fetching the item's latest version.

        Returns True on success. Raises on non-retryable errors —
        pyzotero raises on any non-2xx, as `httpx.HTTPStatusError`
        through 1.14 and as its own typed errors from 1.15; both are
        read by `_http_status_of`.
        """
        current = self.get_item(item_key)
        payload = {
            "key": item_key,
            "version": current["version"],
            "abstractNote": abstract,
        }
        try:
            return bool(self._client().update_item(payload))
        except Exception as exc:
            # pyzotero >= 1.15 raises its own typed errors, which are
            # not httpx subclasses and carry no response; see
            # `_http_status_of`.
            if _is_version_conflict(exc):
                raise VersionConflictError(
                    f"{item_key}: version {current['version']} was stale"
                ) from exc
            raise

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(VersionConflictError),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    def update_tags(
        self,
        item_key: str,
        *,
        add: Iterable[str] = (),
        remove: Iterable[str] = (),
        remove_prefixed: Iterable[str] = (),
    ) -> int:
        """Atomically add / remove tags on an item in a single PATCH.

        The backbone of Zotero-as-ground-truth: screening scripts use
        this to record decisions as stage tags (`abstract:include`,
        `fulltext:exclude`, …) that resume logic then reads.

        `add`: exact tags to add (no-op if already present).
        `remove`: exact tags to remove (no-op if not present).
        `remove_prefixed`: tags whose first `:`-segment matches any of
          the given prefixes are removed. Use for atomic stage-tag
          replacement — to flip `abstract:borderline` → `abstract:include`
          in one write, pass `add=['abstract:include']` and
          `remove_prefixed=['abstract:']`.

        Returns the number of tags that changed (additions + removals).
        Returns 0 without writing when the computed target tag set
        equals the current set.

        Retries up to 3 times on HTTP 412 by re-fetching the item's
        current version, same pattern as `update_abstract`.
        """
        current = self.get_item(item_key)
        data = current.get("data", {})
        existing = {
            t.get("tag", "")
            for t in data.get("tags", [])
            if t.get("tag")
        }

        add_set = {t for t in add if t}
        remove_set = {t for t in remove if t}
        prefix_tuple = tuple(p for p in remove_prefixed if p)

        def _matches_prefix(tag: str) -> bool:
            return any(tag.startswith(p) for p in prefix_tuple)

        target = {
            t for t in existing
            if t not in remove_set and not _matches_prefix(t)
        } | add_set

        if target == existing:
            return 0

        payload = {
            "key": item_key,
            "version": current["version"],
            "tags": [{"tag": t} for t in sorted(target)],
        }
        try:
            self._client().update_item(payload)
        except Exception as exc:
            # pyzotero >= 1.15 raises its own typed errors, which are
            # not httpx subclasses and carry no response; see
            # `_http_status_of`.
            if _is_version_conflict(exc):
                raise VersionConflictError(
                    f"{item_key}: version {current['version']} was stale "
                    f"during tag update"
                ) from exc
            raise

        added = len(target - existing)
        removed = len(existing - target)
        return added + removed

    #: Zotero's predicate for "Related" items.
    RELATION_PREDICATE = "dc:relation"

    def item_uri(self, item_key: str) -> str:
        """The URI Zotero stores in `relations` to point at an item.

        Always the `http://zotero.org/...` form: it is an identifier,
        not a URL anything fetches, and Zotero Desktop writes the same
        form. For the personal library that needs the zotero.org user ID
        (`local_user_id`), since the local API's `0` means nothing in a
        stored relation; raises if Desktop has never been signed in.
        """
        library_id = self.group_id
        if self.library_type == "user":
            library_id = local_user_id()
            if not library_id:
                raise RuntimeError(
                    "Cannot build a relation in the personal library: Zotero "
                    "Desktop is not signed in to a zotero.org account, so it "
                    "reports no user ID to name the item by.",
                )
        return (f"http://zotero.org/{self.library_type}s/{library_id}"
                f"/items/{item_key}")

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_version_conflict),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    def add_related(
        self, item_key: str, related_keys: Iterable[str],
    ) -> int:
        """Add `dc:relation` links from `item_key` to each related key.

        One direction only — call it for both items to get the two-way
        link Zotero Desktop makes when you relate items by hand. Existing
        relations (of any predicate) are kept; returns how many links
        were added, 0 without writing when all were already there.
        """
        client = self._client()
        current = client.item(item_key)
        relations = dict(current.get("data", {}).get("relations") or {})
        existing = relations.get(self.RELATION_PREDICATE) or []
        if isinstance(existing, str):
            existing = [existing]
        wanted = [self.item_uri(k) for k in related_keys if k != item_key]
        added = [u for u in dict.fromkeys(wanted) if u not in existing]
        if not added:
            return 0
        relations[self.RELATION_PREDICATE] = list(existing) + added
        payload = {
            "key": item_key,
            "version": current["version"],
            "relations": relations,
        }
        # Version read just above; a 412 re-reads it (the retry), as
        # `update_fields` does.
        client.update_item(payload)
        return len(added)

    def get_tags(self, item_key: str) -> set[str]:
        """Return the current set of tags on an item (for resume checks)."""
        item = self.get_item(item_key)
        return {
            t.get("tag", "")
            for t in item.get("data", {}).get("tags", [])
            if t.get("tag")
        }

    def batch_update_tags(
        self,
        updates: list[tuple[str, dict]],
        *,
        batch_size: int = 50,
    ) -> dict[str, int]:
        """Apply tag changes to many items, one multi-object write per
        batch. The bulk path behind `--csv-backfill`, abstract
        screening's steady-state tag flush, `manage_tags --apply` and
        `apply_qa_adjudications`; single writes use `update_tags()`.

        Each entry in `updates` is `(item_key, op)` where `op` is a
        dict with any of `add`, `remove`, `remove_prefixed` (same
        semantics as `update_tags`). Items are fetched in one call
        per batch, new tag sets are computed,
        and the batch is sent as one POST. `batch_size` is capped at
        50, Zotero's per-request limit.

        Returns `{applied, unchanged, failed}` counts across all
        batches, read from Zotero's per-object answer — see
        `_post_items` for why pyzotero's `update_items` cannot be used
        to get them. An item counts as failed when the bulk read did not
        return it, when Zotero rejected it, or when its write still
        could not land after the retries below; failures are logged with
        Zotero's reason.

        Retries, so one bad moment does not kill a long screening run:
        - a transient failure of the whole request (429, 5xx,
          transport) is retried up to `_TAG_BATCH_ATTEMPTS` times,
          re-reading the batch each time;
        - objects Zotero rejected individually with 412 (edited since
          our read) are re-read and re-sent, up to
          `_TAG_CONFLICT_ROUNDS` passes.
        If transient failures outlast the attempts, that batch's
        pending items are counted failed and the run moves on, since the
        callers treat a failed item as "re-run will redo it". Any other
        error (403, a bad key) raises: it would fail every batch alike.
        """
        stats = {"applied": 0, "unchanged": 0, "failed": 0}
        if not updates:
            return stats
        batch_size = max(1, min(batch_size, 50))

        for i in range(0, len(updates), batch_size):
            pending = updates[i:i + batch_size]
            for _ in range(_TAG_CONFLICT_ROUNDS):
                try:
                    chunk_stats, pending = self._apply_tag_batch(pending)
                except Exception as exc:
                    if not _is_transient_write_error(exc):
                        raise
                    logger.warning(
                        "zotero_io: %d tag write(s) not applied after %d "
                        "attempts (%s); counted as failed",
                        len(pending), _TAG_BATCH_ATTEMPTS, exc,
                    )
                    stats["failed"] += len(pending)
                    pending = []
                    break
                for k, v in chunk_stats.items():
                    stats[k] += v
                if not pending:
                    break
            if pending:
                logger.warning(
                    "zotero_io: %d tag write(s) still in version conflict "
                    "after %d passes: %s",
                    len(pending), _TAG_CONFLICT_ROUNDS,
                    ", ".join(k for k, _ in pending),
                )
                stats["failed"] += len(pending)

        return stats

    def _apply_tag_batch(
        self, chunk: list[tuple[str, dict]],
    ) -> tuple[dict[str, int], list[tuple[str, dict]]]:
        """One read-compute-write pass over ≤50 tag ops, retried whole on
        transient failure. Returns this pass's counts and the ops Zotero
        rejected with a per-object 412, for the caller to re-run."""
        return retry(
            stop=stop_after_attempt(_TAG_BATCH_ATTEMPTS),
            retry=retry_if_exception(_is_transient_write_error),
            wait=wait_exponential(multiplier=1, max=10),
            sleep=lambda s: _write_sleep(s),
            reraise=True,
            before_sleep=lambda rs: logger.warning(
                "zotero_io: batched tag write failed (%s); retrying "
                "(attempt %d of %d)",
                rs.outcome.exception(), rs.attempt_number + 1,
                _TAG_BATCH_ATTEMPTS,
            ),
        )(self._apply_tag_batch_once)(chunk)

    def _apply_tag_batch_once(
        self, chunk: list[tuple[str, dict]],
    ) -> tuple[dict[str, int], list[tuple[str, dict]]]:
        stats = {"applied": 0, "unchanged": 0, "failed": 0}
        z = self._client()
        keys = ",".join(k for k, _ in chunk)
        fetched = _read_page(lambda: z.items(itemKey=keys))
        fetched_by_key = {it.get("key"): it for it in fetched}

        payloads: list[dict] = []
        sent: list[tuple[str, dict]] = []
        for item_key, op in chunk:
            item = fetched_by_key.get(item_key)
            if item is None:
                logger.warning(
                    "zotero_io: %s not found for tag write; counted as failed",
                    item_key,
                )
                stats["failed"] += 1
                continue

            data = item.get("data", {})
            existing = {
                t.get("tag", "")
                for t in data.get("tags", [])
                if t.get("tag")
            }
            add_set = {t for t in op.get("add", ()) if t}
            remove_set = {t for t in op.get("remove", ()) if t}
            prefix_tuple = tuple(p for p in op.get("remove_prefixed", ()) if p)

            def _matches_prefix(tag: str, _pt=prefix_tuple) -> bool:
                return any(tag.startswith(p) for p in _pt)

            target = {
                t for t in existing
                if t not in remove_set and not _matches_prefix(t)
            } | add_set

            if target == existing:
                stats["unchanged"] += 1
                continue

            payloads.append({
                "key": item_key,
                "version": data.get("version", item.get("version", 0)),
                "tags": [{"tag": t} for t in sorted(target)],
            })
            sent.append((item_key, op))

        if not payloads:
            return stats, []

        body = _post_items(z, payloads)
        success = body.get("success") or {}
        unchanged = body.get("unchanged") or {}
        failed = body.get("failed") or {}
        conflicted: list[tuple[str, dict]] = []
        for idx, (item_key, op) in enumerate(sent):
            slot = str(idx)
            if slot in success:
                stats["applied"] += 1
            elif slot in unchanged:
                stats["unchanged"] += 1
            elif slot in failed:
                reason = failed[slot] if isinstance(failed[slot], dict) else {}
                if reason.get("code") == 412:
                    conflicted.append((item_key, op))
                    continue
                logger.warning(
                    "zotero_io: tag write for %s rejected: %s %s",
                    item_key, reason.get("code", "?"),
                    reason.get("message", failed[slot]),
                )
                stats["failed"] += 1
            else:
                # Absent from every bucket: nothing says it landed.
                logger.warning(
                    "zotero_io: no result for tag write on %s; counted as failed",
                    item_key,
                )
                stats["failed"] += 1
        return stats, conflicted

    def items_with_tag(
        self,
        collection_key: str,
        tag: str,
        *,
        item_type: str = "journalArticle",
    ) -> list[dict]:
        """All items in the collection whose tag set contains `tag`.

        Used by export / test scripts to read Zotero-authoritative state
        (e.g. `items_with_tag(coll, 'fulltext:include')` enumerates the
        included-paper set). Works against any tag vocabulary, not just
        stage tags.
        """
        items = self.collection_items(collection_key, item_type=item_type)
        return [
            it for it in items
            if any(
                t.get("tag") == tag
                for t in it.get("data", {}).get("tags", [])
            )
        ]

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception_type(VersionConflictError),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    def upsert_child_note(
        self,
        parent_key: str,
        marker: str,
        note_html: str,
    ) -> str:
        """Create or update a child note on `parent_key` identified by
        `marker`. The marker is a string the note's HTML content starts
        with (e.g. `<h1>SLR Coding</h1>`); this lets us find our own
        note among any other child notes the user may have added.

        If a note starting with `marker` already exists under the
        parent, its `note` field is overwritten with `note_html`. If no
        such note exists, a new one is created.

        Returns the Zotero key of the note (new or existing). Retries
        on HTTP 412 version conflicts.

        `note_html` must begin with `marker` for subsequent runs to
        find and update it rather than creating duplicates.
        """
        if not note_html.startswith(marker):
            raise ValueError(
                f"note_html must begin with the marker {marker!r} so the "
                f"next upsert can find and update it."
            )

        # Find existing note with the marker.
        existing: dict | None = None
        for child in self._client().children(parent_key):
            data = child.get("data", {})
            if data.get("itemType") != "note":
                continue
            if (data.get("note") or "").lstrip().startswith(marker):
                existing = child
                break

        if existing is None:
            # Create new note.
            payload = {
                "itemType": "note",
                "parentItem": parent_key,
                "note": note_html,
                "tags": [],
                "collections": [],
                "relations": {},
            }
            resp = self._client().create_items([payload])
            # pyzotero returns a dict with 'success' / 'failed' keys.
            success = resp.get("success") or resp.get("successful") or {}
            if isinstance(success, dict):
                keys = list(success.values())
                if keys:
                    first = keys[0]
                    if isinstance(first, dict):
                        return first.get("key") or first.get("data", {}).get("key", "")
                    return str(first)
            failed = resp.get("failed") or resp.get("failure") or {}
            raise RuntimeError(
                f"upsert_child_note: create_items did not return a key "
                f"for parent {parent_key}: success={success!r} failed={failed!r}"
            )

        # Update existing note.
        existing_data = existing.get("data", {})
        note_key = existing_data.get("key", existing.get("key", ""))
        note_version = existing_data.get("version", existing.get("version", 0))
        payload = {
            "key": note_key,
            "version": note_version,
            "note": note_html,
        }
        try:
            self._client().update_item(payload)
        except Exception as exc:
            # pyzotero >= 1.15 raises its own typed errors, which are
            # not httpx subclasses and carry no response; see
            # `_http_status_of`.
            if _is_version_conflict(exc):
                raise VersionConflictError(
                    f"{note_key}: version {note_version} was stale during "
                    f"child-note upsert"
                ) from exc
            raise

        return note_key

    #: Upload attempts per PDF, the first included.
    UPLOAD_ATTEMPTS = 3

    def attach_pdf(self, item_key: str, pdf_path: str | Path) -> str | None:
        """Upload a PDF as a child attachment of `item_key`.

        Runs pyzotero's full 3-step S3 upload — create attachment item →
        auth request → PUT bytes → register — via `Zupload`, and returns
        the key of the attachment item it created. Raises if the API
        rejected the file.

        **It never returns None on a path that changed the library**, and
        never reports "already attached": the attachment item is created
        before the server is asked whether it needs the bytes, so there is
        no outcome here that leaves the parent item as it was. See the
        comment on the return below for what the `unchanged` bucket
        actually means and the duplicate attachments that reading it as a
        no-op produced.

        **Not** `attachment_simple`, and the difference is load-bearing.
        That helper sets the attachment item's `filename` field to the
        path it was handed (`_client.py:1197`), and the Zotero API
        rejects any stored-file filename containing a directory
        separator:

            400 Stored-file filename '/abs/path/to/x.pdf' cannot
                contain a directory path

        So every upload from a cache directory fails at *item creation*,
        before a byte is sent. pyzotero then compounds it: the failing
        entry never gets a `key`, so `Zupload.upload` drops it in the
        `failure` bucket (`_upload.py:230`) while `_create_prelim`
        discards the server's `failed` map (`:109-112`) — the reason
        never reaches the caller, and the only symptom is a failure
        bucket containing the payload that was sent. A live run of 2,229
        items downloaded PDFs fine and attached zero of them, logging 38
        `upload_failed` rows whose detail was the echoed payload.

        The fix is to send the basename as `filename` and pass the
        directory as `Zupload`'s `basedir`, which is what that parameter
        exists for — the local file is still read from the full path,
        and the server gets the bare name it requires.

        pyzotero's return shape (from _upload.py:218-239):
            {"success": [item_dict, ...],
             "failure": [item_dict, ...],
             "unchanged": [item_dict, ...]}
        — all three values are lists of the attachment-item dicts that
        ended up in each bucket.

        Retries transient transport / 429 / 5xx failures (see
        `_is_transient_write_error`). A live run once lost 48
        successfully-downloaded PDFs here because a single unretried
        upload blip was terminal for the item; the bytes were still on
        disk, but nothing ever tried again.

        **A retry reuses the attachment item the first attempt created.**
        `Zupload` writes the new key into the template dict it was handed,
        and skips item creation when the template already carries one, so
        handing the same dict back resumes at the auth / PUT step. The
        retry used to start from a fresh template, creating one more
        file-less attachment per attempt; `pdf_map` read those as real
        for its one-hour grace window (so a rerun skipped the parent) and
        as stubs after it. If every attempt fails, the item that was
        created is checked: bytes that did land after all (an answer lost
        in transit) make it a success; otherwise it is moved to the trash
        so it cannot pose as a PDF, and the error is raised.
        """
        path = Path(pdf_path)
        # Refuse to upload a file that cannot be a PDF. Zotero accepts a
        # zero-byte upload happily, and the resulting attachment still
        # carries an md5 (of nothing) — which `pdf_map()` reads as "this
        # item has a real PDF", marking it permanently complete and
        # skipping it on every future run. Found by a live test.
        try:
            size = path.stat().st_size
        except OSError as exc:
            raise RuntimeError(f"attach_pdf: cannot read {path}: {exc}") from exc
        if size == 0:
            raise RuntimeError(f"attach_pdf: refusing to upload empty file {path}")

        from pyzotero._upload import Zupload

        template = dict(_IMPORTED_FILE_TEMPLATE)
        template["title"] = path.name
        template["filename"] = path.name
        z = self._client()
        attempt = 0
        while True:
            attempt += 1
            try:
                result = Zupload(
                    z, [template], item_key, basedir=path.parent,
                ).upload()
                break
            except Exception as exc:
                if (attempt < self.UPLOAD_ATTEMPTS
                        and _is_transient_write_error(exc)):
                    logger.warning(
                        "attach_pdf: %s upload failed (%s: %s); retrying "
                        "(attempt %d of %d)", item_key, type(exc).__name__,
                        exc, attempt + 1, self.UPLOAD_ATTEMPTS,
                    )
                    _write_sleep(min(2 ** (attempt - 1), 10))
                    continue
                created = template.get("key")
                if created and self._upload_landed(z, created):
                    logger.info(
                        "attach_pdf: %s — %s holds the bytes although the "
                        "upload reported %s", item_key, created,
                        type(exc).__name__,
                    )
                    return str(created)
                if created:
                    self._discard_fileless_attachment(z, created)
                raise

        # `unchanged` is NOT a no-op, and reading it as one misreports the
        # library. `Zupload` creates the attachment item first, on every
        # call, in `_create_prelim`; only afterwards does it ask for an
        # upload authorisation, and only there can the server answer
        # `exists` — meaning it already holds these bytes, so skip the
        # transfer (`_upload.py:299-302`). The child item it just created
        # stays. So that bucket separates "bytes sent" from "bytes already
        # there", never "attachment added" from "nothing done".
        #
        # Returning None here told callers nothing had happened while the
        # parent had in fact just gained a second attachment. A repair loop
        # downstream read those Nones as "already attached, skip" and
        # reported 137 swaps plus 2 items with no key; both figures were
        # wrong, and two items were left holding byte-identical duplicate
        # PDFs added 49 seconds apart. Since no path through this method
        # leaves the library unchanged, there is no no-op to report, and
        # the honest answer on every non-raising path is a key.
        for bucket in ("success", "unchanged"):
            entries = result.get(bucket) or []
            if not entries:
                continue
            if bucket == "unchanged":
                logger.info(
                    "attach_pdf: %s — the library already held these bytes; "
                    "a new attachment item was created for them anyway",
                    item_key,
                )
            first = entries[0]
            if not isinstance(first, dict):
                return str(first)
            key = first.get("key") or first.get("data", {}).get("key")
            if key:
                return str(key)

        failure = result.get("failure") or []
        raise RuntimeError(f"attach_pdf failed for {item_key}: {failure!r}")

    @staticmethod
    def _upload_landed(z, attachment_key: str) -> bool:
        """Whether `attachment_key` holds bytes (has an md5) on `z`."""
        try:
            item = z.item(attachment_key)
        except Exception:  # noqa: BLE001 — unknown is not "landed"
            return False
        return bool((item.get("data") or {}).get("md5"))

    def _discard_fileless_attachment(self, z, attachment_key: str) -> None:
        """Trash the attachment item a failed upload left without a file.

        Best effort: the upload error is what the caller needs to see, so
        a failure here is logged, never raised over it. Trash rather than
        delete, so nothing is unrecoverable if the guess is wrong.
        """
        try:
            status = self._set_deleted(attachment_key, 1, client=z)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "attach_pdf: could not trash file-less attachment %s: %s",
                attachment_key, exc,
            )
            return
        if status not in (200, 204):
            logger.warning(
                "attach_pdf: trashing file-less attachment %s returned "
                "HTTP %s", attachment_key, status,
            )

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_version_conflict),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
    def update_fields(self, item_key: str, fields: dict) -> bool:
        """PATCH `fields` onto `item_key` at its *current* version.

        `update_item` sends whatever version the caller read, which goes
        stale the moment anything else writes the item — including the
        caller's own earlier write in the same run (enrich_dois's
        malformed-DOI fix followed by its replacement write). This reads
        the version just before writing, and on a
        412 reads it again (three attempts).
        """
        current = self.get_item(item_key)
        payload = {"key": item_key, "version": current["version"], **fields}
        return bool(self._client().update_item(payload))

    def update_item(self, payload: dict) -> bool:
        """Generic item PATCH (used by import_to_zotero for bulk field updates).

        Caller must include `key` and `version` in the payload. pyzotero
        handles the If-Unmodified-Since-Version header and raises on
        non-2xx (via `@backoff_check`).
        """
        return bool(self._client().update_item(payload))

    def create_items(self, payloads: list[dict]) -> dict:
        """POST new items (at most 50); Zotero's per-object result map,
        `{"success": {idx: key}, "failed": {idx: {...}}, ...}` — see
        `_post_items` for why not pyzotero's `create_items`."""
        return _post_items(self._client(), payloads)

    def create_collection(
        self, name: str, *, parent_collection: str = "",
    ) -> str:
        """Create a new collection and return its key.

        Groups can only be created on zotero.org — the API creates
        items, collections and saved searches within an existing
        library — so this is the write-side counterpart to `find_group_by_name`
        for harnesses that need a scoped scratch collection inside a
        hand-created group.
        """
        payload = [{"name": name, "parentCollection": parent_collection}]
        resp = self._client().create_collections(payload)
        success = resp.get("success") or resp.get("successful") or {}
        if isinstance(success, dict) and success:
            first = next(iter(success.values()))
            if isinstance(first, dict):
                return first.get("key") or first.get("data", {}).get("key", "")
            return str(first)
        failed = resp.get("failed") or resp.get("failure") or {}
        raise RuntimeError(
            f"create_collection failed for {name!r}: {failed!r}"
        )

    #: A Zotero object key: 8 characters, upper-case letters and digits.
    #: Distinguishing a key from a collection *name* is a guess, but a
    #: safe one — a real collection called "AI_ETP_SR" is 9 characters,
    #: and any 8-character all-caps name that isn't a key simply falls
    #: through to the name lookup below and is found (or created) there.
    _COLLECTION_KEY_RE = re.compile(r"^[A-Z0-9]{8}$")

    def find_collection(
        self, name_or_key: str, *, create: bool = False,
    ) -> tuple[str, str]:
        """Resolve a collection key **or** display name. Returns `(key, how)`.

        `how` is `"key"`, `"name"` or `"created"`, so the caller can say
        out loud which happened — the failure this exists to prevent was
        silent. The skill documented `--collection <name>` and promised
        it would be created; the script accepted only an existing key,
        so a name was passed straight to the API as one and every single
        item in the batch failed with an opaque 400. The agent driving
        that run improvised inline Python to list and create collections
        and then re-ran the import, which duplicated the whole corpus.

        Lookup order: exact key match, then exact name match, then
        (with `create=True`) create it. Raises ValueError when two
        collections share the name — ambiguous, never guess, same rule
        as `find_group_by_name`.
        """
        wanted = (name_or_key or "").strip()
        if not wanted:
            return "", ""
        collections = self._client().everything(self._client().collections())
        by_key = {c.get("key", ""): c for c in collections}
        if self._COLLECTION_KEY_RE.match(wanted) and wanted in by_key:
            return wanted, "key"

        matches = [
            c for c in collections
            if (c.get("data", {}) or {}).get("name", "") == wanted
        ]
        if len(matches) > 1:
            raise ValueError(
                f"Ambiguous collection name {wanted!r}: "
                f"{len(matches)} collections share it "
                f"(keys {[c.get('key') for c in matches]}). Pass the key "
                f"instead, or rename one of them."
            )
        if matches:
            return matches[0].get("key", ""), "name"

        if not create:
            raise ValueError(
                f"No collection named {wanted!r} in "
                f"{self.describe_library()}, and it is not a collection "
                f"key either."
            )
        return self.create_collection(wanted), "created"

    def delete_collection(self, collection_key: str) -> bool:
        """Delete a collection (used by mini_slr.py's teardown stage).

        Fetches the current version first, matching `delete_item`'s
        pattern for the If-Unmodified-Since-Version header.
        """
        try:
            current = self._client().collection(collection_key)
        except Exception:
            return False
        return bool(
            self._client().delete_collection(
                current, last_modified=current["version"],
            )
        )

    def delete_item(self, item_key: str) -> bool:
        """Delete an item (used by enrich_pdfs.py to remove PDF stubs).

        pyzotero needs the current version for the If-Unmodified-Since
        header, so we fetch once before deleting.
        """
        try:
            current = self.get_item(item_key)
        except Exception:
            return False
        return bool(
            self._client().delete_item(current, last_modified=current["version"])
        )

    # -----------------------------------------------------------------
    # Better BibTeX (BBT) helpers.
    #
    # The stdlib-only transport (`bbt_json_rpc`, `get_bibtex_export`)
    # lives in `bbt_client.py` so light-weight scripts like
    # generate_bib.py can use it without importing pyzotero. The
    # methods on this class are thin instance wrappers plus the
    # higher-level ops (`get_bbt_keys`, `populate_missing_bbt_keys`)
    # that need pyzotero for item enumeration.
    #
    # Direct urllib / curl against `127.0.0.1:23119/better-bibtex/...`
    # from anywhere outside `zotero_io.py` and `bbt_client.py` is a
    # defect — see the IRON RULE in skills/zotero-operations/SKILL.md
    # and the CI guard at tests/unit/test_no_direct_localhost_zotero.py.
    # -----------------------------------------------------------------

    def bbt_json_rpc(self, method: str, params: dict | None = None) -> dict:
        """Call a Better BibTeX JSON-RPC method. See bbt_client.bbt_json_rpc."""
        from bbt_client import bbt_json_rpc as _rpc
        return _rpc(method, params)

    def get_bibtex_export(self, *, library_id: int | str | None = None) -> str:
        """Fetch the full BibTeX export for a Zotero library.

        Defaults to this client's library: its group ID, or BBT's `1`
        for the personal library. Pass any numeric ID to override.
        See bbt_client.get_bibtex_export.
        """
        from bbt_client import get_bibtex_export as _export
        if library_id is None:
            library_id = 1 if self.library_type == "user" else self.group_id
        lid = library_id
        return _export(lid)

    def get_bbt_keys(self, item_keys: list[str]) -> dict[str, str]:
        """Bulk-lookup BBT citation keys for a list of Zotero item keys.

        Calls BBT's `item.citationkey` JSON-RPC method. Returns a dict
        mapping `{zotero_item_key: bbt_citation_key}` for every item
        BBT has a key for. Items without a BBT key (BBT not yet
        synced, or item added without BBT running) are absent from
        the returned dict — callers can compute the missing set as
        `set(item_keys) - result.keys()`.

        The parameter name is `item_keys`, not `keys`: BBT's JSON-RPC
        handler validates named parameters against the method
        signature (`async citationkey(item_keys)`) and rejects
        anything else with `-32602 unsupported argument`. Because the
        error body carries no `result`, the wrong name fails *silently*
        — this method returns `{}` and `populate_missing_bbt_keys`
        reports every item as unkeyed. zotero-mcp hit the identical
        bug (its #293); `tests/live/test_zotero_io_bbt.py` is what
        pins it here, since a mocked transport cannot catch it.
        """
        if not item_keys:
            return {}

        # Zotero's own `citationKey` field first. It is authoritative,
        # it needs no BBT round-trip, and for a group library it is the
        # only route that works at all: BBT's `item.citationkey`
        # resolves bare keys against the personal library and answers
        # null for everything else. Measured on Zotero 10.0.1 with BBT
        # 9.0.63 — `41:SQR3R8QW` (the *local* library id) resolves,
        # while `6658025:SQR3R8QW` (the group id) and a bare `SQR3R8QW`
        # both return null. This client knows the group id and has no
        # mapping to Desktop's internal library id, so the prefixed form
        # is not available to it.
        out: dict[str, str] = {}
        for item in self.items_by_keys(item_keys):
            key = item.get("key", "")
            native = (item.get("data", {}).get("citationKey") or "").strip()
            if key and native:
                out[key] = native

        remaining = [k for k in item_keys if k not in out]
        if not remaining:
            return out

        # Legacy path: an older Zotero that does not expose the field, or
        # items BBT has keyed but Zotero has not surfaced yet. Best
        # effort — a BBT outage must not discard the keys already found.
        try:
            body = self.bbt_json_rpc("item.citationkey", {"item_keys": remaining})
        except Exception as exc:  # noqa: BLE001
            logger.debug("BBT citation-key fallback failed: %s", exc)
            return out
        result = body.get("result")
        if isinstance(result, dict):
            # BBT returns {"<zotero_key>": "<bbt_key>", ...}; skip empties.
            for key, value in result.items():
                if isinstance(value, str) and value:
                    out[key] = value
        return out

    def populate_missing_bbt_keys(
        self,
        item_keys: list[str] | None = None,
    ) -> dict[str, list[str]]:
        """Identify items missing a BBT citation key.

        Returns a dict with two lists:
          - `keyed`:   item keys that already have a BBT citation key.
          - `missing`: item keys for which BBT did not return a key.

        BBT auto-generates keys at item-add time when the plugin is
        running. Items added while BBT was off, or imported via paths
        that bypass BBT, end up without keys until the user runs
        "Generate BibTeX key" / "Refresh BibTeX key" in the Zotero
        right-click menu, or until BBT's auto-pin sweep runs.

        If `item_keys` is None, scans every top-level item in the
        library. The remediation step itself (regenerating the keys)
        is a Zotero-UI action; this method is intentionally read-only
        — it tells you what needs the user's attention without
        clobbering existing keys.
        """
        if item_keys is None:
            top = self.top_items()
            item_keys = [it.get("key", "") for it in top if it.get("key")]
        keyed_map = self.get_bbt_keys(item_keys)
        keyed = sorted(keyed_map.keys())
        missing = sorted(set(item_keys) - set(keyed))
        return {"keyed": keyed, "missing": missing}

    # -----------------------------------------------------------------
    # Duplicate merge — see module attribution. Ported from
    # zotero-mcp's merge_duplicates (MIT-licensed).
    # -----------------------------------------------------------------

    #: Fields Zotero's API *returns* but pyzotero's `check_items()`
    #: allowlist rejects on write. `lastRead` is set by Zotero's built-in
    #: PDF reader, so it appears on exactly the attachments this plugin
    #: creates and users then open — which made every merge involving a
    #: read PDF fail with `InvalidItemFieldsError`. Live case: merging a
    #: Connector-saved duplicate whose PDF the operator had opened.
    _UNWRITABLE_FIELDS = frozenset({"lastRead"})

    def _safe_update_item(self, item: dict, client=None) -> None:
        """`update_item` with server-only fields stripped.

        Two layers, because the denylist above is a snapshot and Zotero
        keeps adding reader state: drop the known fields first, then, if
        pyzotero still objects, take the field names out of its own error
        message and retry once. That keeps the fix working for fields
        that do not exist yet without silently discarding anything the
        API would have accepted.
        """
        client = client if client is not None else self._client()
        data = item.get("data", item)
        for field in self._UNWRITABLE_FIELDS:
            data.pop(field, None)
        try:
            client.update_item(item)
            return
        except Exception as e:
            if type(e).__name__ != "InvalidItemFieldsError":
                raise
            offenders = set(
                re.findall(r"Invalid keys present in item \d+: (.*)", str(e))
            )
            names = {n for group in offenders for n in group.split()}
            if not names:
                raise
            logger.warning(
                "zotero_io: stripping unwritable field(s) %s and retrying",
                ", ".join(sorted(names)),
            )
            for field in names:
                data.pop(field, None)
            client.update_item(item)

    def _set_deleted(self, item_key: str, deleted: int, *, client=None) -> int:
        """PATCH `{"deleted": 0|1}`; returns the HTTP status. Hand-built
        because pyzotero rejects `deleted` as an invalid field.

        Verified live on 2026-09-23: a standalone note trashed through
        Desktop's local API answered 204 and read back `deleted`. The
        local form has two requirements. It goes through pyzotero's
        `_write`, which adds the `Zotero-Server-ID` and local-key headers
        a local write needs (428/401 without them). It also needs an
        explicit `Content-Type`, since the local API answers a raw body
        without one with "400 Empty request body".
        """
        from pyzotero.zotero import build_url
        z = client if client is not None else self._client()
        latest = z.item(item_key)
        url = build_url(
            z.endpoint, f"/{z.library_type}/{z.library_id}/items/{item_key}",
        )
        headers = {
            "If-Unmodified-Since-Version": str(latest["version"]),
            "Content-Type": "application/json",
        }
        body = json.dumps({"deleted": deleted})
        return z._write("PATCH", url=url, headers=headers, content=body).status_code

    def parent_of(self, item_key: str) -> str:
        """`item_key`'s parent ("" for a top item).

        Where a merge re-parented it, so where its result is checked."""
        item = self._client().item(item_key)
        return (item.get("data", {}) or {}).get("parentItem", "") or ""

    def reparent(self, child_key: str, parent_key: str) -> None:
        """Move `child_key` under `parent_key`."""
        z = self._client()
        item = z.item(child_key)
        item["data"]["parentItem"] = parent_key
        self._safe_update_item(item, z)

    def trash_item(self, item_key: str) -> None:
        """Move `item_key` to Zotero's trash, recoverable
        from the Trash in the UI, unlike pyzotero's permanent `delete_item`.
        Raises on failure."""
        status = self._set_deleted(item_key, 1)
        if status not in (200, 204):
            raise RuntimeError(f"trash PATCH returned HTTP {status} for {item_key}")

    def restore_from_trash(self, item_key: str) -> bool:
        """Take `item_key` back out of Zotero's trash. True on
        success. For recovering a Connector save a merge trashed while
        its PDF was still under it."""
        return self._set_deleted(item_key, 0) in (200, 204)

    def _reparent_child(
        self, child_key: str, target_key: str, keeper_sigs: set,
        child_content_types: tuple[str, ...] | None, *, attempts: int = 4,
        client=None,
    ) -> str | None:
        """Move one child to `target_key`. Returns None when the child is
        filtered out, "dupe" when the keeper already has the same file,
        "pdf" or "other" when moved.

        Retries a 412, re-reading the child each time. Zotero Desktop can
        still be finishing a PDF upload when the Connector pass merges,
        and the md5/mtime update it writes bumps the attachment's version
        between our read and our PATCH; seen twice in one night.
        """
        z = client if client is not None else self._client()
        for attempt in range(attempts):
            fresh = z.item(child_key)
            fd = fresh.get("data", {})
            if child_content_types is not None and (
                fd.get("itemType") != "attachment"
                or fd.get("contentType", "") not in child_content_types
            ):
                return None
            if fd.get("itemType") == "attachment":
                sig = (
                    fd.get("contentType", ""),
                    fd.get("filename", ""),
                    fd.get("md5", ""),
                    fd.get("url", ""),
                )
                if sig in keeper_sigs:
                    return "dupe"
            fd["parentItem"] = target_key
            try:
                self._safe_update_item(fresh, z)
            except Exception as exc:
                if _http_status_of(exc) != 412 or attempt == attempts - 1:
                    raise
                time.sleep(2 ** attempt)
                continue
            return "pdf" if fd.get("contentType") == "application/pdf" else "other"
        return None  # unreachable: the last attempt raises

    def merge_duplicate_item(
        self,
        target_key: str,
        duplicate_key: str,
        *,
        union_tags: bool = True,
        child_content_types: tuple[str, ...] | None = None,
    ) -> dict[str, int | list[str]]:
        """Merge `duplicate_key` into `target_key` and trash the duplicate.

        Keeps the target item intact (preserves its Zotero item_key,
        Better BibTeX citation key, hand-curated metadata). From the
        duplicate:
          - Tags and collections are unioned into the target.
          - Each child (attachment / note / annotation) is re-parented
            to the target, EXCEPT attachments whose
            (contentType, filename, md5, url) signature already exists
            on the target — those are dropped to avoid duplicate PDFs.
          - Finally the duplicate is trashed via
            `PATCH {"deleted": 1}` (recoverable from Zotero's Trash),
            NOT pyzotero's permanent `delete_item`.

        Returns a stats dict with counts plus the key lists for logs:
            {
              "moved":        int,
              "skipped_dupe_attachments": int,
              "tags_added":   int,
              "collections_added": int,
              "trashed":      [target_key] on success, [] on failure,
            }

        `union_tags=False` leaves the target's tags alone, and
        `child_content_types` moves only attachments of those types (notes
        and other attachments stay on the duplicate and go to the trash
        with it). The Connector route uses both: its keeper's metadata
        came from elsewhere, and the translator's HTML snapshot and
        keyword tags are not wanted there.

        Safety guard: refuses to merge when the two items carry
        different non-empty DOIs, since a mismatched merge permanently
        entangles two separate papers' metadata. Raises ValueError.

        **Runs in Desktop, through the local API.** It used to run on
        the cloud, and Zotero Desktop did not always take the result. The
        Connector saves the item in Desktop, and Desktop may still be
        writing to that attachment (the md5/mtime of a finishing upload)
        when the cloud re-parents it. Desktop overwrote two re-parents
        that had looked successful, and on 2026-09-23 26 of 213 merged
        PDFs were still under the trashed item in Desktop hours later
        while the cloud had them under the keeper. A re-parent made in
        Desktop itself has nothing to be overwritten by.

        The trash is `_set_deleted`; see there.
        """
        z = self._client()
        target = z.item(target_key)
        duplicate = z.item(duplicate_key)

        target_data = target.get("data", {})
        dup_data = duplicate.get("data", {})
        target_doi = (target_data.get("DOI") or "").strip().lower()
        dup_doi = (dup_data.get("DOI") or "").strip().lower()
        if target_doi and dup_doi and target_doi != dup_doi:
            raise MergeRefused(
                f"Refusing to merge: target DOI {target_doi!r} != "
                f"duplicate DOI {dup_doi!r}",
            )

        target_children = z.children(target_key)
        dup_children = z.children(duplicate_key)

        # Step 1: tag union.
        existing_tags = {t.get("tag", "")
                         for t in target_data.get("tags", [])}
        dup_tags = {t.get("tag", "")
                    for t in dup_data.get("tags", [])}
        new_tags = (dup_tags - existing_tags) - {""} if union_tags else set()
        if new_tags:
            target_data["tags"] = [
                {"tag": t} for t in sorted(existing_tags | new_tags)
            ]
            self._safe_update_item(target, z)
            target = z.item(target_key)          # refresh version

        # Step 2: collection union.
        existing_collections = set(target.get("data", {}).get("collections", []))
        dup_collections = set(dup_data.get("collections", []))
        new_collections = dup_collections - existing_collections
        for coll_key in new_collections:
            z.addto_collection(coll_key, target)
            target = z.item(target_key)          # refresh version

        # Step 3: re-parent children, skipping duplicate attachments.
        keeper_sigs = {
            (
                c.get("data", {}).get("contentType", ""),
                c.get("data", {}).get("filename", ""),
                c.get("data", {}).get("md5", ""),
                c.get("data", {}).get("url", ""),
            )
            for c in target_children
            if c.get("data", {}).get("itemType") == "attachment"
        }
        moved: list[str] = []
        moved_pdfs: list[str] = []
        skipped_dupes: list[str] = []
        for child in dup_children:
            child_key = child.get("key", "")
            moved_one = self._reparent_child(
                child_key, target_key, keeper_sigs, child_content_types,
                client=z,
            )
            if moved_one is None:
                continue
            if moved_one == "dupe":
                skipped_dupes.append(child_key)
                continue
            moved.append(child_key)
            if moved_one == "pdf":
                moved_pdfs.append(child_key)

        # Step 4: trash the duplicate with PATCH {"deleted": 1}.
        # pyzotero's `delete_item` permanently destroys; we want
        # Zotero's Trash (recoverable in the UI).
        #
        # Unless it still holds a PDF this merge neither moved nor
        # skipped as a copy of the keeper's. Under upload load a PDF
        # child appeared after the listing above; the merge
        # moved nothing and then trashed the only item holding the real
        # PDF. Listed again here, as late as possible; a listing that
        # fails counts as "may hold one".
        trashed: list[str] = []
        accounted = set(moved) | set(skipped_dupes)
        try:
            late = [
                c for c in (z.children(duplicate_key) or [])
                if c.get("data", {}).get("contentType") == "application/pdf"
                and c.get("key") not in accounted
            ]
        except Exception:  # noqa: BLE001
            late = [None]
        if late:
            logger.warning(
                "merge_duplicate_item: %s still holds a PDF the merge did "
                "not move; not trashing it", duplicate_key,
            )
            return {
                "moved": len(moved), "moved_keys": moved,
                "moved_pdf_keys": moved_pdfs,
                "skipped_dupe_attachments": len(skipped_dupes),
                "tags_added": len(new_tags),
                "collections_added": len(new_collections),
                "trashed": [], "kept_unmoved_pdf": True,
            }
        try:
            status = self._set_deleted(duplicate_key, 1)
            if status in (200, 204):
                trashed.append(duplicate_key)
            else:
                logger.warning(
                    "merge_duplicate_item: trash PATCH returned HTTP %d for %s",
                    status, duplicate_key,
                )
        except Exception as e:
            logger.warning(
                "merge_duplicate_item: trash PATCH failed for %s: %s",
                duplicate_key, e,
            )

        return {
            "moved": len(moved),
            "moved_keys": moved,
            "moved_pdf_keys": moved_pdfs,
            "skipped_dupe_attachments": len(skipped_dupes),
            "tags_added": len(new_tags),
            "collections_added": len(new_collections),
            "trashed": trashed,
            "kept_unmoved_pdf": False,
        }
