"""Abstract base class for bibliographic search sources.

A `SearchSource` discovers DOIs matching a query against one
academic database. Sources differ in:

- whether they filter by journal ISSN at the API level (Scopus, WoS
  do; Semantic Scholar does not — filtering happens client-side)
- whether they take Boolean-expression queries (Scopus, WoS) or
  block-term lists OR'd together (OpenAlex, Semantic Scholar)
- credentials required (none, env var, institutional key)

`run()` returns rows in the common `search_results.csv` schema
(see SEARCH_ROW_FIELDS below) — the orchestrator (`search.py`)
merges and deduplicates across sources.
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import requests

# Credential requirement modes for `resolve_credential`.
CREDENTIAL_REQUIRED = "required"   # missing key → hard error, source can't run
CREDENTIAL_OPTIONAL = "optional"   # key raises quota, but anon calls still work


def resolve_credential(
    env_var: str,
    *,
    mode: str = CREDENTIAL_REQUIRED,
    label: str = "",
    hint: str = "",
) -> tuple[str, str | None]:
    """Resolve an API credential from the environment, uniformly.

    Returns `(value, error)`:

    - The credential is read from `os.environ[env_var]` and stripped.
    - If present, returns `(value, None)`.
    - If absent and `mode == CREDENTIAL_OPTIONAL`, returns `("", None)` —
      the caller proceeds unauthenticated (lower quota).
    - If absent and `mode == CREDENTIAL_REQUIRED`, returns `("", message)`
      where `message` names `label` (default: the env var), the missing
      env var, and any `hint`.

    This replaces the per-searcher hand-rolled checks — `wos.py`'s bare
    `os.environ[...]` KeyError, `semantic_scholar.py`'s silent
    `get(..., "")` — with one regime that `credentials_error()` and
    `run()` can both call.
    """
    value = os.environ.get(env_var, "").strip()
    if value:
        return value, None
    if mode == CREDENTIAL_OPTIONAL:
        return "", None
    label = label or env_var
    message = f"{label}: {env_var} env var not set."
    if hint:
        message = f"{message} {hint}"
    return "", message

# Common row schema every source emits. Fields not applicable to a
# source must still be present as empty strings / zero; downstream CSV
# writers use DictWriter with a fixed fieldnames list.
SEARCH_ROW_FIELDS = (
    "db",              # "scopus" | "wos" | "openalex" | "semantic_scholar"
    "query",           # label of the query that produced the row
    "doi",
    "title",
    "authors",         # "Last, First; Last, First" convention
    "year",            # "YYYY" string
    "source",          # journal / venue name
    "issn",
    "cited_by",        # int
    "abstract",
    # per-source identifiers (empty when not applicable)
    "scopus_id",
    "wos_id",
    "openalex_id",
    "s2_paper_id",
    # OA metadata (populated by OpenAlex and Semantic Scholar when available)
    "oa_status",
    "oa_url",
)


def empty_row() -> dict:
    """Return a new row dict with every field initialised to an empty value.

    Callers fill in what they have; downstream CSV writes every column
    whether populated or not, so the header stays stable.
    """
    row: dict = {k: "" for k in SEARCH_ROW_FIELDS}
    row["cited_by"] = 0
    return row


@dataclass
class SearchContext:
    """State shared across all sources in a search run.

    - `from_year` / `to_year`: inclusive year bounds from `search_config.py`.
    - `issns`: flat list of ISSNs (sources that filter server-side use
      this; sources that don't use it for client-side post-filtering).
    - `mailto`: `CROSSREF_MAILTO` value if set; OpenAlex uses it for
      polite-pool identification.
    - `session`: the shared `requests.Session` every HTTP-based source
      must use — see `http()`.
    """
    from_year: int
    to_year: int
    issns: list[str]
    mailto: str = ""
    session: Any = field(default=None, repr=False)

    def http(self) -> requests.Session:
        """The run's shared `requests.Session`, built on first use.

        Every HTTP-based source routes through this rather than calling
        `requests.get` directly, so the whole search stage inherits
        `http_client`'s retry policy: exponential backoff on 429 / 5xx,
        `Retry-After` honoured, and a bounded attempt count.

        Built lazily rather than in `__post_init__` because a
        `SearchContext` is cheap to construct in tests and in
        `credentials_error()` pre-flight, neither of which makes a
        request. The import is deferred for the same reason — nothing
        that only inspects a context should have to import `requests`.
        """
        if self.session is None:
            import http_client

            self.session = http_client.build_session(mailto=self.mailto or None)
        return self.session


class SearchSource(ABC):
    """One database's search interface.

    Implementations set class attributes `name` and the `supports_*`
    flags, and implement `run()` and optionally `credentials_error()`.
    """

    # Short stable identifier used in CLI flags, CSV `db` column, and
    # the registry. Lower_snake_case.
    name: str = ""

    # True if the source can restrict results to specific ISSNs at the
    # API level. Informational; influences the orchestrator's messaging
    # about what scope filtering actually happens.
    supports_journal_scope: bool = False

    # True if the source's native query language is a block-term list
    # (OpenAlex, Semantic Scholar). False if it is a Boolean expression
    # (Scopus, WoS). Informational; the source reads its own query
    # shape from `config` directly.
    supports_block_queries: bool = False

    @abstractmethod
    def run(self, config, ctx: SearchContext) -> list[dict]:
        """Run every query this source can derive from `config`.

        `config` is the user's loaded `search_config.py` module. The
        source knows which attributes to read (`QUERY_DEFS`,
        `BLOCK_A_TERMS`, `BLOCK_B_TERMS`, etc.). Returns a list of rows
        in the SEARCH_ROW_FIELDS schema.
        """

    def credentials_error(self, ctx: SearchContext) -> str | None:
        """Return None if the source is ready to run; otherwise an
        error message explaining which credential is missing.

        The orchestrator calls this before `run()` and skips / errors
        based on the result, so `run()` can assume credentials are
        present.
        """
        return None
