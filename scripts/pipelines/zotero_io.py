"""Single entry point for Zotero I/O across the pipeline scripts.

Every pipeline script (both read-only and read-write) routes through
`ZoteroClient`. The class wraps pyzotero — it does not reimplement the
Zotero REST API.

Design notes:
    - Local pyzotero client for reads (localhost:23119, requires Zotero
      desktop + Better BibTeX). Falls back to the cloud client if the
      local server is unreachable.
    - Writes go local too when `[zotero] local_api_key` is configured,
      and to the cloud otherwise. Zotero 10.0.1 accepts local writes
      after a one-time consent dialog; `/setup` grants the key, because
      a dialog is not something an unattended `uv run` script can
      answer. Writing where we read removes the staleness gap that
      `cloud_journal_articles()` exists to work around — a script that
      wrote through the Web API and read back locally saw nothing until
      Desktop synced, and reported it as "already done".
    - **Whichever surface a write uses, the read that feeds it uses the
      same one.** `update_item` sends `If-Unmodified-Since-Version`, and
      local and cloud version counters are unrelated, so a version read
      from one surface and sent to the other is rejected 412 every time.
      `_write_client()` is therefore the read client for anything that
      is about to be written back.
    - **The surface is chosen by configuration, never by comparing
      library versions.** A downstream project gated "prefer local" on
      the local API's `Last-Modified-Version` matching the Web API's.
      Zotero 9 served the last synced server version there; Zotero 10
      serves `clientVersion`, a per-transaction local counter unrelated
      to Web API versions, so the check can never pass. Theirs returned
      "cannot determine" forever and fell back to the metered surface on
      every call with nothing said — 46 of 67 live tests skipped on that
      gate. A reachability probe would fail the same way, so there is
      none: if a key is configured the local API is used, and if it is
      down the write raises instead of quietly costing quota.
    - Uploads (`attach_pdf`) stay on the cloud client regardless.
      pyzotero's `attachment_simple` / `attachment_both` and the 3-step
      S3 handshake have no local equivalent (`upload_attachments()` is
      the local route, unimplemented here). An upload creates a *child*
      item and never bumps the parent's version, so it does not collide
      with metadata written locally.
    - tenacity wraps `update_abstract` to retry on version conflicts
      (HTTP 412): we re-fetch the item, re-apply the abstract, and
      re-PATCH.
    - No module-level ZOTERO_API_KEY read. Callers instantiate via
      `ZoteroClient.from_config()`, which goes through `core.config_loader`.

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


def _is_retryable_upload_error(exc: BaseException) -> bool:
    """True for transient failures of the 3-step S3 attachment upload.

    The upload is three network round-trips (register → auth → PUT), so
    a transport blip or a 503 anywhere in the chain aborts the whole
    thing while the local PDF is perfectly good. Those are worth
    retrying. A `RuntimeError` from `attach_pdf` is NOT — it means the
    Zotero API accepted the request and explicitly reported the file in
    its `failure` bucket, which retrying reproduces verbatim.
    """
    status = _http_status_of(exc)
    if status is not None:
        return status in _RETRYABLE_UPLOAD_STATUSES
    return _is_httpx_error(exc, "TransportError")


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


class VersionConflictError(RuntimeError):
    """Raised by update_abstract when pyzotero returns HTTP 412.

    tenacity's @retry catches this and re-invokes the wrapped method,
    which re-fetches the item's current version before re-applying the
    patch.
    """


class GroupSelectionRequired(RuntimeError):
    """Raised by `ZoteroClient.from_config()` when the user hasn't picked
    a group and more than one (or zero) is accessible.

    Carries the list of accessible groups (as returned by Zotero's
    /users/{id}/groups endpoint) so orchestrators can print an
    actionable menu to the user.
    """

    def __init__(self, groups: list[dict]):
        super().__init__(
            f"Zotero group selection required: {len(groups)} accessible groups"
        )
        self.groups = groups


def _list_accessible_groups(api_key: str, user_id: str) -> list[dict]:
    """Fetch `{id, name}` list for every Zotero group the user can access.

    Returns an empty list on any failure (network, auth, malformed
    response). The caller handles both "empty because failure" and
    "empty because user has no group memberships" the same way —
    prompting them to specify --group explicitly.
    """
    import urllib.request
    url = f"https://api.zotero.org/users/{user_id}/groups?v=3"
    req = urllib.request.Request(
        url, headers={"Zotero-API-Key": api_key, "Zotero-API-Version": "3"},
    )
    try:
        import json as _json
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = _json.loads(resp.read())
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


def find_group_by_name(
    name: str,
    *,
    api_key: str | None = None,
    user_id: str | None = None,
) -> dict | None:
    """Look up a Zotero group by exact display-name match.

    Queries the user's accessible groups (`_list_accessible_groups`) and
    returns the `{"id", "name"}` dict for the unique exact match, or
    `None` if no group has that name. Raises `ValueError` if more than
    one group shares the name — ambiguous, never guess.

    `api_key` / `user_id` default to the live config (same source
    `ZoteroClient.from_config` reads via `core.config_loader`).
    """
    if api_key is None or user_id is None:
        from core.config_loader import get, require
        if api_key is None:
            api_key = require("zotero", "api_key", env="ZOTERO_API_KEY")
        if user_id is None:
            user_id = get("zotero", "user_id", env="ZOTERO_USER_ID")
    if not user_id:
        return None
    groups = _list_accessible_groups(api_key, user_id)
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
            "Could not retrieve your accessible groups from Zotero "
            "(no user_id in config.toml, or network error, or the key "
            "lacks group access). Re-run `python3 scripts/setup/wizard.py` "
            "to refresh your Zotero profile, or pass --group <id> "
            "directly."
        )
    return "\n".join(lines)


def add_library_args(parser: argparse.ArgumentParser) -> None:
    """Add `--group` / `--user` (mutually exclusive) and `--remote`.

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
    # Reads default to Zotero Desktop's local API because it is far faster
    # and unmetered. That default is wrong in one specific, easy-to-hit
    # situation: items written through the Web API are invisible to the
    # local client until Zotero Desktop next syncs. A script that adds
    # items and then reads them back — or one handed a --filter-keys-file
    # of freshly created keys — sees nothing, and reports zero items to
    # process, which reads as "already done" rather than "cannot see them
    # yet". Added here rather than per script so every pipeline entry
    # point has the escape hatch.
    parser.add_argument(
        "--remote", action="store_true",
        help="Read via api.zotero.org instead of the local Zotero client. "
             "Use when items were just written through the Web API and the "
             "desktop client has not synced them down yet.",
    )


#: Zotero's `imported_file` attachment template, written out rather than
#: fetched.
#:
#: This used to call `self.cloud._attachment_template("imported_file")`,
#: which is pyzotero's private wrapper around
#: `GET /items/new?itemType=attachment&linkMode=imported_file`. Two
#: reasons that had to go, one of them live:
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


class ZoteroClient:
    """Thin pyzotero wrapper used by every pipeline script.

    Usage:
        zot = ZoteroClient.from_config()
        for item in zot.journal_articles():
            ...
        zot.update_abstract(item_key, abstract_text)
        zot.attach_pdf(item_key, "/path/to/file.pdf")
    """

    #: Class-level defaults for the two attributes that decide the write
    #: surface. Several tests build a client with `__new__` to exercise
    #: one method against a stub, bypassing `__init__` entirely; without
    #: these, asking any of them where a write goes raises
    #: AttributeError. The defaults are the conservative pair — cloud
    #: writes, which is what a client assembled that way already used.
    prefer_local: bool = True
    local_api_key: str | None = None

    def __init__(
        self,
        api_key: str,
        group_id: str,
        *,
        library_type: str = "group",
        prefer_local: bool = True,
        local_api_key: str | None = None,
    ):
        """api_key / group_id — standard Zotero credentials. For a user
        library pass `library_type="user"` and `group_id=<user_id>`.

        `local_api_key` is Zotero Desktop's own write key, granted by the
        user through Zotero's consent dialog and unrelated to the
        zotero.org `api_key`. Supplying one moves writes onto the local
        API; leaving it None keeps every existing install on the cloud
        write path it has always used. The alternate constructors read it
        from config, so it is explicit here and unit tests that build a
        client directly get the cloud default without touching config.
        """
        self.api_key = api_key
        self.group_id = group_id
        self.library_type = library_type
        self.prefer_local = prefer_local
        self.local_api_key = local_api_key or None
        self._local: zotero.Zotero | None = None
        self._cloud: zotero.Zotero | None = None

    #: Zotero Desktop's own write key, granted once through Zotero's
    #: consent dialog by `/setup` and stored in config. Unrelated to the
    #: zotero.org API key. Absent, writes go to the Web API exactly as
    #: they always have.
    @staticmethod
    def _configured_local_api_key() -> str:
        from core.config_loader import get
        return get("zotero", "local_api_key", env="ZOTERO_LOCAL_API_KEY")

    @classmethod
    def from_config(
        cls,
        group_id: str | None = None,
        *,
        prefer_local: bool = True,
    ) -> ZoteroClient:
        """Instantiate from ~/.config/academic-research/config.toml.

        `group_id` is per-project (set by the caller from a --group CLI
        flag or $ZOTERO_GROUP) and is NOT stored in the global config.
        See the convention note in tests/unit/test_setup_wizard.py:40-42.

        When group_id is not provided, queries Zotero for the user's
        accessible groups. If exactly one exists, uses it automatically.
        Otherwise raises a GroupSelectionRequired exception carrying the
        list of groups so orchestrators can print an actionable error.
        """
        import os

        from core.config_loader import get, require
        api_key = require("zotero", "api_key", env="ZOTERO_API_KEY")
        if not group_id:
            group_id = os.environ.get("ZOTERO_GROUP", "").strip()
        if not group_id:
            # User didn't specify a group — ask Zotero which ones they
            # can access, auto-pick if there's only one.
            user_id = get("zotero", "user_id", env="ZOTERO_USER_ID")
            groups = _list_accessible_groups(api_key, user_id) if user_id else []
            if len(groups) == 1:
                group_id = str(groups[0]["id"])
                print(
                    f"ZoteroClient: auto-selected sole accessible group "
                    f"{group_id} ('{groups[0].get('name', '?')}').",
                    file=sys.stderr,
                )
            else:
                raise GroupSelectionRequired(groups)
        return cls(
            api_key=api_key,
            group_id=group_id,
            prefer_local=prefer_local,
            local_api_key=cls._configured_local_api_key(),
        )

    @classmethod
    def for_user_library(
        cls,
        user_id: str,
        *,
        api_key: str | None = None,
        prefer_local: bool = True,
    ) -> ZoteroClient:
        """Alternate constructor for a personal (user) library.

        Used by audit_zotero_library.py when auditing the user's own
        library instead of a group.
        """
        if api_key is None:
            from core.config_loader import require
            api_key = require("zotero", "api_key", env="ZOTERO_API_KEY")
        return cls(
            api_key=api_key,
            group_id=user_id,
            library_type="user",
            prefer_local=prefer_local,
            local_api_key=cls._configured_local_api_key(),
        )

    @classmethod
    def from_args(
        cls,
        args: argparse.Namespace,
        *,
        prefer_local: bool = True,
        api_key: str | None = None,
    ) -> ZoteroClient:
        """Build a client from parsed `--group` / `--user` argparse args.

        Expects the parser was set up with `add_library_args(parser)`.
        Returns a client pointing at either a group library (numeric
        `--group <id>`) or the user's personal library (`--user`).
        Raises SystemExit with an actionable message if neither is
        set (and `$ZOTERO_GROUP` isn't populated).

        `--remote` forces reads through api.zotero.org. It overrides the
        `prefer_local` keyword rather than the other way round: the flag is
        an explicit statement by whoever is running the script that the
        local client cannot be trusted to have the items yet, which a
        caller's compiled-in default cannot know.
        """
        import os

        from core.config_loader import require

        if getattr(args, "remote", False):
            prefer_local = False

        # Validate library selection BEFORE requiring the API key —
        # a missing --group should produce the actionable
        # "specify --group or --user" message, not a confusing
        # "API key missing" RuntimeError. Order matters in CI / fresh
        # checkouts where neither is set.
        is_user = bool(getattr(args, "user", False))
        group = getattr(args, "group", "") or os.environ.get("ZOTERO_GROUP", "")
        if not is_user and not group:
            raise SystemExit(
                "ERROR: --group <id> or --user is required "
                "(or set $ZOTERO_GROUP).",
            )

        if api_key is None:
            api_key = require("zotero", "api_key", env="ZOTERO_API_KEY")

        if is_user:
            user_id = require("zotero", "user_id", env="ZOTERO_USER_ID")
            return cls.for_user_library(
                user_id,
                api_key=api_key,
                prefer_local=prefer_local,
            )
        return cls(
            api_key=api_key,
            group_id=group,
            library_type="group",
            prefer_local=prefer_local,
            local_api_key=cls._configured_local_api_key(),
        )

    # -----------------------------------------------------------------
    # Internal — pyzotero client factories. Lazily created so
    # ZoteroClient() in a unit test doesn't touch the network.
    # -----------------------------------------------------------------

    @property
    def local(self) -> zotero.Zotero:
        if self._local is None:
            # Zotero Desktop's local API serves the personal library
            # only as `users/0` ("the logged-in user") — the cloud
            # user ID gets a 400 locally. Group IDs pass through.
            lib_id = "0" if self.library_type == "user" else self.group_id
            kwargs: dict = {"local": True}
            if self.local_api_key:
                # pyzotero >= 1.15.1 sends this as the `Zotero-API-Key`
                # header on local writes, and discovers the companion
                # `Zotero-Server-ID` from a response header itself.
                kwargs["local_api_key"] = self.local_api_key
            self._local = zotero.Zotero(
                lib_id, self.library_type, self.api_key, **kwargs,
            )
        return self._local

    @property
    def cloud(self) -> zotero.Zotero:
        if self._cloud is None:
            self._cloud = zotero.Zotero(
                self.group_id, self.library_type, self.api_key,
            )
        return self._cloud

    def _read_client(self) -> zotero.Zotero:
        return self.local if self.prefer_local else self.cloud

    @property
    def local_writes_enabled(self) -> bool:
        """Whether writes go to Zotero Desktop rather than api.zotero.org.

        Two conditions, both configuration — deliberately no network
        probe. `--remote` (which clears `prefer_local`) is an explicit
        statement that the desktop client cannot be trusted for this run,
        and honouring it for reads while writing locally would split one
        run across two surfaces.
        """
        return bool(self.prefer_local and self.local_api_key)

    def _write_client(self) -> zotero.Zotero:
        """The surface writes go to — and reads that feed them.

        See the module docstring: a version read from one surface cannot
        be sent to the other.
        """
        return self.local if self.local_writes_enabled else self.cloud

    def _upload_client(self) -> zotero.Zotero:
        """File uploads, which have no local implementation here."""
        return self.cloud

    @property
    def write_surface(self) -> str:
        """`"local"` or `"cloud"` — where metadata writes land.

        For run-logs. The two surfaces keep unrelated version counters,
        so a reader reconciling a log against item versions afterwards
        has no way to tell which one produced a row, and choosing wrong
        makes every version in it look stale.
        """
        return "local" if self.local_writes_enabled else "cloud"

    @property
    def upload_surface(self) -> str:
        """Always `"cloud"` — see `_upload_client`.

        Separate from `write_surface` so a caller logging an attachment
        cannot accidentally claim it went local on a local-write client.
        """
        return "cloud"

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
        z = self._read_client()
        return _everything(z, lambda: z.items(itemType="journalArticle"))

    def recent_items(self, limit: int = 50) -> list[dict]:
        """The `limit` most recently added items, newest first, any type.

        For polling "did something just arrive": listing every journal
        article instead took 95 s per poll on a 25,703-article library,
        this takes about half a second.
        """
        z = self._read_client()
        return z.items(sort="dateAdded", direction="desc", limit=limit)

    def cloud_journal_articles(self) -> list[dict]:
        """`journal_articles()` forced through api.zotero.org.

        For callers that need to read back their own writes. Writes go
        to the Web API; Zotero Desktop's local database only learns
        about them at its next sync, so a caller that writes and then
        asks the local client gets a stale answer with no error — which
        is how one import created a full set of duplicates on a re-run.

        Deliberately not `prefer_local=False` on the whole client: the
        rest of a run is faster against the local server and is not
        reading its own writes.
        """
        z = self.cloud
        return _everything(z, lambda: z.items(itemType="journalArticle"))

    def abstractable_items(
        self, item_types: Sequence[str] | None = None,
    ) -> list[dict]:
        """Top-level items of every type that can carry an abstract.

        The Zotero API's `itemType` accepts `a || b` for a union, so this
        is one paginated sweep rather than one per type.
        """
        types = tuple(item_types or self.ABSTRACTABLE_ITEM_TYPES)
        z = self._read_client()
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
        z = self._read_client()
        out: list[dict] = []
        for i in range(0, len(wanted), self.ITEM_KEY_BATCH):
            batch = wanted[i:i + self.ITEM_KEY_BATCH]
            out.extend(_everything(
                z, lambda batch=batch: z.items(itemKey=",".join(batch)),
            ))
        return out

    def top_items(self) -> list[dict]:
        """All top-level items (includes non-article types: book, report, etc.)."""
        z = self._read_client()
        return _everything(z, lambda: z.top())

    def all_attachments(self) -> list[dict]:
        """All attachment items in the library."""
        z = self._read_client()
        return _everything(z, lambda: z.items(itemType="attachment"))

    def collection_items(self, collection: str, *,
                         item_type: str = "journalArticle") -> list[dict]:
        """Items in a collection, named by key **or** display name, by type.

        Accepts a name because the CLI flags that feed this are written
        `--collection SLR` in the skills and by users. The argument used
        to go straight to the API as a key, which worked only by accident:
        Zotero's local HTTP server tolerates a name, so a local run
        succeeded and the same command with `--remote` got a 404
        "Collection not found" from api.zotero.org naming nothing the user
        had typed. `import_to_zotero.py` resolved names on both paths and
        documented it, so the two disagreed.
        """
        z = self._read_client()
        key = self._resolve_collection_for_read(collection, z)
        return _everything(z, lambda: z.collection_items(key, itemType=item_type))

    def _resolve_collection_for_read(self, collection: str, z) -> str:
        """A collection key for `collection`, resolving a name if needed.

        Anything already shaped like a key is returned untouched, so the
        common path costs no extra request.

        Resolution reads through `z` — the caller's read client —
        rather than the write client. `find_collection` deliberately uses
        the write client, because it serves the write path and has to see
        a collection this pipeline just created; this one only reads, and
        routing it through the write surface would give a local run a
        credential requirement it did not have before.
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
            for parent, (real, _stubs) in by_parent.items() if real
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
            for parent, (real, _stubs) in by_parent.items() if real
        }

    def pdf_map(
        self, *, stub_grace_seconds: int = 3600,
    ) -> dict[str, tuple[bool, list[str]]]:
        """{parent_key: (has_real_pdf, [stub_keys])} across the whole library.

        A "real" PDF has a non-empty `md5`. A "stub" is a
        metadata-only attachment with no bytes (left behind by
        earlier failed uploads).

        Grace window: attachments whose `dateAdded` is within the
        last `stub_grace_seconds` (default 1h) are NOT classified
        as stubs — their md5 may just not have populated yet because
        Zotero Desktop is still uploading the file bytes. Deleting
        those prematurely would destroy an in-flight upload. After
        the grace window expires, a missing md5 genuinely indicates
        a failed upload.
        """
        return {
            k: (bool(real), [s["key"] for s in stubs])
            for k, (real, stubs) in self._pdf_attachments_by_parent(
                stub_grace_seconds=stub_grace_seconds,
            ).items()
        }

    def _pdf_attachments_by_parent(
        self, *, stub_grace_seconds: int = 3600,
    ) -> dict[str, tuple[list, list]]:
        """{parent_key: ([real attachments], [stub attachments])}, full dicts.

        The shared walk behind `pdf_map` and `real_pdf_map`; see
        `pdf_map` for what separates a real attachment from a stub and
        why the grace window exists.
        """
        import datetime
        pdfs = [a for a in self.all_attachments()
                if a["data"].get("contentType") == "application/pdf"
                and a["data"].get("parentItem")]

        now = datetime.datetime.now(datetime.UTC)
        grace = datetime.timedelta(seconds=stub_grace_seconds)

        by_parent: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
        for pdf in pdfs:
            parent = pdf["data"]["parentItem"]
            if pdf["data"].get("md5"):
                by_parent[parent][0].append(pdf)
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

    def get_item(self, item_key: str) -> dict:
        """Fetch a single item's current payload (used for version refresh)."""
        return self._write_client().item(item_key)

    def api_base_url(self) -> str:
        """Zotero REST API prefix for this library.

        Returns `https://api.zotero.org/groups/<id>` or
        `https://api.zotero.org/users/<id>` depending on `library_type`.
        Pipeline scripts that issue raw HTTP requests (outside pyzotero)
        use this instead of hard-coding `/groups/`.
        """
        return f"https://api.zotero.org/{self.library_type}s/{self.group_id}"

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
            "user 5591 (personal library)"
        """
        if self.library_type == "user":
            return f"user {self.group_id} (personal library)"
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
        """Fetch the group's display name from the Zotero cloud.

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
        import json as _json
        import urllib.request
        url = f"https://api.zotero.org/groups/{self.group_id}"
        req = urllib.request.Request(
            url,
            headers={
                "Zotero-API-Key": self.api_key,
                "Zotero-API-Version": "3",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = _json.loads(resp.read())
        except Exception:
            return None
        return (data or {}).get("data", {}).get("name")

    # -----------------------------------------------------------------
    # Writes (`_write_client()` — local when a local API key is
    # configured, else cloud; pyzotero handles the 3-step S3 upload and
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
            return bool(self._write_client().update_item(payload))
        except Exception as exc:
            # pyzotero >= 1.15 raises its own typed errors, which are
            # not httpx subclasses and carry no response; see
            # `_http_status_of`.
            if _http_status_of(exc) == 412:
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
            self._write_client().update_item(payload)
        except Exception as exc:
            # pyzotero >= 1.15 raises its own typed errors, which are
            # not httpx subclasses and carry no response; see
            # `_http_status_of`.
            if _http_status_of(exc) == 412:
                raise VersionConflictError(
                    f"{item_key}: version {current['version']} was stale "
                    f"during tag update"
                ) from exc
            raise

        added = len(target - existing)
        removed = len(existing - target)
        return added + removed

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
        """Apply tag changes to many items via pyzotero's multi-item
        PATCH. Intended for bulk paths like `--csv-backfill` where N
        tag writes over N separate PATCH calls would be slow and
        412-prone; the steady-state per-worker path continues to use
        `update_tags()`.

        Each entry in `updates` is `(item_key, op)` where `op` is a
        dict with any of `add`, `remove`, `remove_prefixed` (same
        semantics as `update_tags`). Items are fetched in one call
        per batch, new tag sets are computed, and the batch is sent
        as a single PATCH. `batch_size` caps per-PATCH size at 50
        (Zotero's per-request limit).

        Returns `{applied, unchanged, failed}` counts across all
        batches. When pyzotero's `update_items` returns a dict,
        partial-batch failures are surfaced individually via its
        success / failed buckets; when it returns a bool (some
        pyzotero versions report only whole-call success), the whole
        chunk is counted as applied or failed accordingly. This
        method does not retry on 412 (callers should re-invoke after
        fetching fresh state).
        """
        if not updates:
            return {"applied": 0, "unchanged": 0, "failed": 0}

        stats = {"applied": 0, "unchanged": 0, "failed": 0}

        for i in range(0, len(updates), batch_size):
            chunk = updates[i:i + batch_size]
            keys = [k for k, _ in chunk]
            # One bulk fetch per batch: `items` filtered by itemKey.
            fetched = self._write_client().items(itemKey=",".join(keys))
            fetched_by_key = {it.get("key"): it for it in fetched}

            payloads: list[dict] = []
            for item_key, op in chunk:
                item = fetched_by_key.get(item_key)
                if item is None:
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
                prefix_tuple = tuple(
                    p for p in op.get("remove_prefixed", ()) if p
                )

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
                    "version": data.get("version", 0),
                    "tags": [{"tag": t} for t in sorted(target)],
                })

            if not payloads:
                continue

            if not hasattr(self._write_client(), "update_items"):
                # Fallback for older pyzotero without multi-item PATCH.
                for p in payloads:
                    try:
                        self._write_client().update_item(p)
                        stats["applied"] += 1
                    except Exception:  # noqa: BLE001
                        stats["failed"] += 1
                continue

            # pyzotero's update_items return shape varies by version:
            # some releases return a dict `{success, unchanged, failed}`
            # each keyed by batch index, while others (e.g. 1.13.x,
            # which just POSTs each chunk and raises on any non-2xx
            # response) return a plain bool for the whole call. Branch
            # on the actual runtime type rather than assuming one
            # shape — a bare `.get()` on a bool blows up with
            # AttributeError mid-batch.
            resp = self._write_client().update_items(payloads)
            if isinstance(resp, dict):
                stats["applied"] += len(resp.get("success") or {})
                stats["unchanged"] += len(resp.get("unchanged") or {})
                failed = resp.get("failed") or resp.get("failure") or {}
                stats["failed"] += len(failed)
            else:
                # Bool (or any other non-dict) result: treat it as an
                # all-or-nothing signal for this chunk. pyzotero raises
                # on a failed PATCH rather than returning False, so a
                # truthy return here means the whole chunk of
                # `payloads` succeeded; a falsy return is handled
                # defensively as a whole-chunk failure.
                if resp:
                    stats["applied"] += len(payloads)
                else:
                    stats["failed"] += len(payloads)

        return stats

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
        for child in self._write_client().children(parent_key):
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
            resp = self._write_client().create_items([payload])
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
            self._write_client().update_item(payload)
        except Exception as exc:
            # pyzotero >= 1.15 raises its own typed errors, which are
            # not httpx subclasses and carry no response; see
            # `_http_status_of`.
            if _http_status_of(exc) == 412:
                raise VersionConflictError(
                    f"{note_key}: version {note_version} was stale during "
                    f"child-note upsert"
                ) from exc
            raise

        return note_key

    @retry(
        stop=stop_after_attempt(3),
        retry=retry_if_exception(_is_retryable_upload_error),
        wait=wait_exponential(multiplier=1, max=10),
        reraise=True,
    )
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
        `_is_retryable_upload_error`). A live run once lost 48
        successfully-downloaded PDFs here because a single unretried
        upload blip was terminal for the item; the bytes were still on
        disk, but nothing ever tried again.
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
        result = Zupload(
            self._upload_client(), [template], item_key, basedir=path.parent,
        ).upload()

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

    def update_item(self, payload: dict) -> bool:
        """Generic item PATCH (used by import_to_zotero for bulk field updates).

        Caller must include `key` and `version` in the payload. pyzotero
        handles the If-Unmodified-Since-Version header and raises on
        non-2xx (via `@backoff_check`).
        """
        return bool(self._write_client().update_item(payload))

    def create_collection(
        self, name: str, *, parent_collection: str = "",
    ) -> str:
        """Create a new collection and return its key.

        The Zotero Web API cannot create groups — only items,
        collections, and saved searches within an existing library —
        so this is the write-side counterpart to `find_group_by_name`
        for harnesses that need a scoped scratch collection inside a
        hand-created group.
        """
        payload = [{"name": name, "parentCollection": parent_collection}]
        resp = self._write_client().create_collections(payload)
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
        collections = self._write_client().everything(self._write_client().collections())
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
            current = self._write_client().collection(collection_key)
        except Exception:
            return False
        return bool(
            self._write_client().delete_collection(
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
            self._write_client().delete_item(current, last_modified=current["version"])
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

        Defaults to this client's `group_id`. Pass `library_id=1` for
        the user's personal library, or any numeric ID to override.
        See bbt_client.get_bibtex_export.
        """
        from bbt_client import get_bibtex_export as _export
        lid = library_id if library_id is not None else self.group_id
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
        # while `6658025:SQR3R8QW` (the cloud group id) and a bare
        # `SQR3R8QW` both return null. This client knows its cloud group
        # id and has no mapping to the local one, so the prefixed form is
        # not available to it.
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
        client = client if client is not None else self._write_client()
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

    def _set_deleted(self, item_key: str, deleted: int) -> int:
        """PATCH `{"deleted": 0|1}` on the write surface; returns the HTTP
        status. Hand-built because pyzotero rejects `deleted` as an
        invalid field.

        The version it sends is read from the same surface, so no version
        crosses surfaces. It was pinned to the cloud until the local form
        was verified live on 2026-09-23: a standalone note trashed through
        Desktop's local API answered 204, read back `deleted` locally, and
        showed `deleted: 1` on the Web API once Desktop synced. The
        local form has two requirements. It goes through pyzotero's
        `_write`, which adds the `Zotero-Server-ID` and local-key headers
        a local write needs (428/401 without them). It also needs an
        explicit `Content-Type`, since the local API answers a raw body
        without one with "400 Empty request body". The Web API tolerates
        the missing header, which is why it went unnoticed.
        """
        from pyzotero.zotero import build_url
        local = self.local_writes_enabled
        z = self._write_client()
        latest = z.item(item_key)
        url = build_url(
            z.endpoint, f"/{z.library_type}/{z.library_id}/items/{item_key}",
        )
        headers = {
            "If-Unmodified-Since-Version": str(latest["version"]),
            "Content-Type": "application/json",
        }
        body = json.dumps({"deleted": deleted})
        if local:
            return z._write("PATCH", url=url, headers=headers, content=body).status_code
        http = z.client
        if http is None:
            raise RuntimeError("pyzotero client is not initialised")
        resp = http.patch(
            url=url,
            headers={
                **headers,
                "Zotero-API-Key": self.api_key,
                "Zotero-API-Version": "3",
            },
            content=body,
        )
        return resp.status_code

    def parent_of(self, item_key: str) -> str:
        """`item_key`'s parent on the write surface ("" for a top item).

        Where a merge re-parented it, so where its result is checked."""
        item = self._write_client().item(item_key)
        return (item.get("data", {}) or {}).get("parentItem", "") or ""

    def reparent(self, child_key: str, parent_key: str) -> None:
        """Move `child_key` under `parent_key` on the write surface."""
        z = self._write_client()
        item = z.item(child_key)
        item["data"]["parentItem"] = parent_key
        self._safe_update_item(item, z)

    def trash_item(self, item_key: str) -> None:
        """Move `item_key` to Zotero's trash (write surface), recoverable
        from the Trash in the UI, unlike pyzotero's permanent `delete_item`.
        Raises on failure."""
        status = self._set_deleted(item_key, 1)
        if status not in (200, 204):
            raise RuntimeError(f"trash PATCH returned HTTP {status} for {item_key}")

    def restore_from_trash(self, item_key: str) -> bool:
        """Take `item_key` back out of Zotero's trash (write surface). True on
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
        z = client if client is not None else self.cloud
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

        **Runs on the write surface: Desktop's local API when a local
        key is configured, the cloud otherwise.** It used to be pinned
        to the cloud, and
        Zotero Desktop did not always take the result. The Connector
        saves the item in Desktop, and Desktop may still be writing to
        that attachment (the md5/mtime of a finishing upload) when the
        cloud re-parents it. Desktop overwrote two re-parents that had
        looked successful, and on 2026-09-23 26 of 213 merged PDFs were
        still under the trashed item in Desktop hours later while the
        cloud had them under the keeper. A re-parent made in Desktop
        itself has nothing to be overwritten by.

        The trash is `_set_deleted`, on the same surface; see there.
        """
        z = self._write_client()
        target = z.item(target_key)
        duplicate = z.item(duplicate_key)

        target_data = target.get("data", {})
        dup_data = duplicate.get("data", {})
        target_doi = (target_data.get("DOI") or "").strip().lower()
        dup_doi = (dup_data.get("DOI") or "").strip().lower()
        if target_doi and dup_doi and target_doi != dup_doi:
            raise ValueError(
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
        # child reached the cloud after the listing above; the merge
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
