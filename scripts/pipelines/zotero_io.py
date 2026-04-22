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
"""

from __future__ import annotations

import logging
import sys
from collections import defaultdict
from pathlib import Path

import warnings

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
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = logging.getLogger(__name__)


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
    ) -> "ZoteroClient":
        """Instantiate from ~/.config/academic-research/config.toml.

        `group_id` is per-project (set by the caller from a --group CLI
        flag or $ZOTERO_GROUP) and is NOT stored in the global config.
        See the convention note in tests/unit/test_setup_wizard.py:40-42.

        When group_id is not provided, queries Zotero for the user's
        accessible groups. If exactly one exists, uses it automatically.
        Otherwise raises a GroupSelectionRequired exception carrying the
        list of groups so orchestrators can print an actionable error.
        """
        from core.config_loader import get, require
        import os
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
    ) -> "ZoteroClient":
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

    # -----------------------------------------------------------------
    # Internal — pyzotero client factories. Lazily created so
    # ZoteroClient() in a unit test doesn't touch the network.
    # -----------------------------------------------------------------

    @property
    def local(self) -> zotero.Zotero:
        if self._local is None:
            self._local = zotero.Zotero(
                self.group_id, "group", self.api_key, local=True,
            )
        return self._local

    @property
    def cloud(self) -> zotero.Zotero:
        if self._cloud is None:
            self._cloud = zotero.Zotero(
                self.group_id, "group", self.api_key,
            )
        return self._cloud

    def _read_client(self) -> zotero.Zotero:
        return self.local if self.prefer_local else self.cloud

    # -----------------------------------------------------------------
    # Reads
    # -----------------------------------------------------------------

    def journal_articles(self) -> list[dict]:
        """All journalArticle items in the library."""
        z = self._read_client()
        return z.everything(z.items(itemType="journalArticle"))

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

    def pdf_map(self) -> dict[str, tuple[bool, list[str]]]:
        """{parent_key: (has_real_pdf, [stub_keys])} across the whole library.

        A "real" PDF has a non-empty `md5`; a "stub" is a metadata-only
        attachment with no bytes (often left behind by earlier failed
        uploads). Matches the shape attach_pdfs.get_pdf_map used to return.
        """
        pdfs = [a for a in self.all_attachments()
                if a["data"].get("contentType") == "application/pdf"
                and a["data"].get("parentItem")]

        by_parent: dict[str, tuple[list, list]] = defaultdict(lambda: ([], []))
        for pdf in pdfs:
            parent = pdf["data"]["parentItem"]
            if pdf["data"].get("md5"):
                by_parent[parent][0].append(pdf)
            else:
                by_parent[parent][1].append(pdf)

        return {k: (bool(real), [s["key"] for s in stubs])
                for k, (real, stubs) in by_parent.items()}

    def get_item(self, item_key: str) -> dict:
        """Fetch a single item's current payload (used for version refresh)."""
        return self.cloud.item(item_key)

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

    def attach_pdf(self, item_key: str, pdf_path: str | Path) -> str | None:
        """Upload a PDF as a child attachment of `item_key`.

        Delegates to pyzotero.Zotero.attachment_simple which runs the
        full 3-step S3 upload: create attachment item → auth request
        → PUT bytes → register. Returns the new attachment key on
        success, None if pyzotero reports the file was already attached.

        pyzotero's return shape (from _upload.py:218-239):
            {"success": [item_dict, ...],
             "failure": [item_dict, ...],
             "unchanged": [item_dict, ...]}
        — all three values are lists of the attachment-item dicts that
        ended up in each bucket.
        """
        path_str = str(Path(pdf_path))
        result = self.cloud.attachment_simple([path_str], parentid=item_key)

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

    def delete_item(self, item_key: str) -> bool:
        """Delete an item (used by attach_pdfs to remove PDF stubs).

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
