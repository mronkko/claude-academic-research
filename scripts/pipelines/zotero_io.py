"""Single entry point for Zotero I/O across the pipeline scripts.

Every pipeline script (both read-only and read-write) routes through
`ZoteroClient`. The class wraps pyzotero — it does not reimplement the
Zotero REST API.

Design notes:
    - Local pyzotero client for reads (localhost:23119, requires Zotero
      desktop + Better BibTeX). Falls back to the cloud client if the
      local server is unreachable.
    - Cloud pyzotero client for writes. pyzotero's `attachment_simple`
      runs the 3-step S3 upload internally, and `update_item` sends
      `If-Unmodified-Since-Version` automatically, so the custom code
      that used to live in attach_pdfs.py / fetch_abstracts.py is gone.
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
import sys
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

import httpx
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


def _is_retryable_upload_error(exc: BaseException) -> bool:
    """True for transient failures of the 3-step S3 attachment upload.

    The upload is three network round-trips (register → auth → PUT), so
    a transport blip or a 503 anywhere in the chain aborts the whole
    thing while the local PDF is perfectly good. Those are worth
    retrying. A `RuntimeError` from `attach_pdf` is NOT — it means the
    Zotero API accepted the request and explicitly reported the file in
    its `failure` bucket, which retrying reproduces verbatim.
    """
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_UPLOAD_STATUSES
    return isinstance(exc, httpx.TransportError)


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

    def __init__(
        self,
        api_key: str,
        group_id: str,
        *,
        library_type: str = "group",
        prefer_local: bool = True,
    ):
        """api_key / group_id — standard Zotero credentials. For a user
        library pass `library_type="user"` and `group_id=<user_id>`."""
        self.api_key = api_key
        self.group_id = group_id
        self.library_type = library_type
        self.prefer_local = prefer_local
        self._local: zotero.Zotero | None = None
        self._cloud: zotero.Zotero | None = None

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
            self._local = zotero.Zotero(
                lib_id, self.library_type, self.api_key, local=True,
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
        return z.everything(z.items(itemType="journalArticle"))

    def abstractable_items(
        self, item_types: Sequence[str] | None = None,
    ) -> list[dict]:
        """Top-level items of every type that can carry an abstract.

        The Zotero API's `itemType` accepts `a || b` for a union, so this
        is one paginated sweep rather than one per type.
        """
        types = tuple(item_types or self.ABSTRACTABLE_ITEM_TYPES)
        z = self._read_client()
        return z.everything(z.items(itemType=" || ".join(types)))

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
            out.extend(z.everything(z.items(itemKey=",".join(batch))))
        return out

    def top_items(self) -> list[dict]:
        """All top-level items (includes non-article types: book, report, etc.)."""
        z = self._read_client()
        return z.everything(z.top())

    def all_attachments(self) -> list[dict]:
        """All attachment items in the library."""
        z = self._read_client()
        return z.everything(z.items(itemType="attachment"))

    def collection_items(self, collection_key: str, *,
                         item_type: str = "journalArticle") -> list[dict]:
        """Items in a specific collection, filtered by type."""
        z = self._read_client()
        return z.everything(z.collection_items(collection_key, itemType=item_type))

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

        return {k: (bool(real), [s["key"] for s in stubs])
                for k, (real, stubs) in by_parent.items()}

    def get_item(self, item_key: str) -> dict:
        """Fetch a single item's current payload (used for version refresh)."""
        return self.cloud.item(item_key)

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
    # Writes (cloud client; pyzotero handles the 3-step S3 upload and
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
        pyzotero's `@backoff_check` decorator on `update_item` already
        raises `httpx.HTTPStatusError` on any non-2xx.
        """
        current = self.get_item(item_key)
        payload = {
            "key": item_key,
            "version": current["version"],
            "abstractNote": abstract,
        }
        try:
            return bool(self.cloud.update_item(payload))
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 412:
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
            self.cloud.update_item(payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 412:
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
            fetched = self.cloud.items(itemKey=",".join(keys))
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

            if not hasattr(self.cloud, "update_items"):
                # Fallback for older pyzotero without multi-item PATCH.
                for p in payloads:
                    try:
                        self.cloud.update_item(p)
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
            resp = self.cloud.update_items(payloads)
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
        for child in self.cloud.children(parent_key):
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
            resp = self.cloud.create_items([payload])
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
            self.cloud.update_item(payload)
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 412:
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
        the new attachment key on success, None if the file was already
        attached.

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
            self.cloud, [template], item_key, basedir=path.parent,
        ).upload()

        success = result.get("success") or []
        if success:
            first = success[0]
            if isinstance(first, dict):
                return first.get("key") or first.get("data", {}).get("key")
            return str(first)

        unchanged = result.get("unchanged") or []
        if unchanged:
            logger.info("attach_pdf: %s already has this file attached", item_key)
            return None

        failure = result.get("failure") or []
        raise RuntimeError(f"attach_pdf failed for {item_key}: {failure!r}")

    def update_item(self, payload: dict) -> bool:
        """Generic item PATCH (used by import_to_zotero for bulk field updates).

        Caller must include `key` and `version` in the payload. pyzotero
        handles the If-Unmodified-Since-Version header and raises on
        non-2xx (via `@backoff_check`).
        """
        return bool(self.cloud.update_item(payload))

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
        resp = self.cloud.create_collections(payload)
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

    def delete_collection(self, collection_key: str) -> bool:
        """Delete a collection (used by mini_slr.py's teardown stage).

        Fetches the current version first, matching `delete_item`'s
        pattern for the If-Unmodified-Since-Version header.
        """
        try:
            current = self.cloud.collection(collection_key)
        except Exception:
            return False
        return bool(
            self.cloud.delete_collection(
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
            self.cloud.delete_item(current, last_modified=current["version"])
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
        body = self.bbt_json_rpc("item.citationkey", {"item_keys": list(item_keys)})
        result = body.get("result")
        if not isinstance(result, dict):
            return {}
        # BBT returns {"<zotero_key>": "<bbt_key>", ...}; filter empty values.
        return {k: v for k, v in result.items() if isinstance(v, str) and v}

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

    def merge_duplicate_item(
        self,
        target_key: str,
        duplicate_key: str,
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

        Safety guard: refuses to merge when the two items carry
        different non-empty DOIs, since a mismatched merge permanently
        entangles two separate papers' metadata. Raises ValueError.
        """
        target = self.get_item(target_key)
        duplicate = self.get_item(duplicate_key)

        target_data = target.get("data", {})
        dup_data = duplicate.get("data", {})
        target_doi = (target_data.get("DOI") or "").strip().lower()
        dup_doi = (dup_data.get("DOI") or "").strip().lower()
        if target_doi and dup_doi and target_doi != dup_doi:
            raise ValueError(
                f"Refusing to merge: target DOI {target_doi!r} != "
                f"duplicate DOI {dup_doi!r}",
            )

        target_children = self.cloud.children(target_key)
        dup_children = self.cloud.children(duplicate_key)

        # Step 1: tag union.
        existing_tags = {t.get("tag", "")
                         for t in target_data.get("tags", [])}
        dup_tags = {t.get("tag", "")
                    for t in dup_data.get("tags", [])}
        new_tags = (dup_tags - existing_tags) - {""}
        if new_tags:
            target_data["tags"] = [
                {"tag": t} for t in sorted(existing_tags | new_tags)
            ]
            self.cloud.update_item(target)
            target = self.get_item(target_key)          # refresh version

        # Step 2: collection union.
        existing_collections = set(target.get("data", {}).get("collections", []))
        dup_collections = set(dup_data.get("collections", []))
        new_collections = dup_collections - existing_collections
        for coll_key in new_collections:
            self.cloud.addto_collection(coll_key, target)
            target = self.get_item(target_key)          # refresh version

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
        skipped_dupes: list[str] = []
        for child in dup_children:
            child_key = child.get("key", "")
            fresh = self.cloud.item(child_key)
            fd = fresh.get("data", {})
            if fd.get("itemType") == "attachment":
                sig = (
                    fd.get("contentType", ""),
                    fd.get("filename", ""),
                    fd.get("md5", ""),
                    fd.get("url", ""),
                )
                if sig in keeper_sigs:
                    skipped_dupes.append(child_key)
                    continue
            fd["parentItem"] = target_key
            self.cloud.update_item(fresh)
            moved.append(child_key)

        # Step 4: trash the duplicate with PATCH {"deleted": 1}.
        # pyzotero's `delete_item` permanently destroys; we want
        # Zotero's Trash (recoverable in the UI).
        trashed: list[str] = []
        try:
            from pyzotero.zotero import build_url
            latest = self.get_item(duplicate_key)
            url = build_url(
                self.cloud.endpoint,
                f"/{self.cloud.library_type}/{self.cloud.library_id}"
                f"/items/{duplicate_key}",
            )
            headers = {
                "If-Unmodified-Since-Version": str(latest["version"]),
                "Zotero-API-Key": self.api_key,
                "Zotero-API-Version": "3",
                "Content-Type": "application/json",
            }
            # pyzotero's httpx client is lazily typed as Optional but
            # is always created in Zotero.__init__; access it once we've
            # issued a read against the same instance.
            http = self.cloud.client
            if http is None:
                raise RuntimeError("pyzotero client is not initialised")
            resp = http.patch(
                url=url,
                headers=headers,
                content=json.dumps({"deleted": 1}),
            )
            if resp.status_code in (200, 204):
                trashed.append(duplicate_key)
            else:
                logger.warning(
                    "merge_duplicate_item: trash PATCH returned HTTP %d for %s",
                    resp.status_code, duplicate_key,
                )
        except Exception as e:
            logger.warning(
                "merge_duplicate_item: trash PATCH failed for %s: %s",
                duplicate_key, e,
            )

        return {
            "moved": len(moved),
            "skipped_dupe_attachments": len(skipped_dupes),
            "tags_added": len(new_tags),
            "collections_added": len(new_collections),
            "trashed": trashed,
        }
