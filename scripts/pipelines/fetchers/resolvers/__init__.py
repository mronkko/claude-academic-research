"""Link-resolver registry.

`resolver_for()` picks the dialect for a configured endpoint. Selection
is by explicit config first (`[library] resolver = sfx | alma`) and
autodetection second, so an institution whose endpoint this package
guesses wrong can always override it.

**`RESOLVERS` order is load-bearing.** `SfxResolver.matches()` accepts
any non-empty endpoint because plain OpenURL has no distinguishing
marker, so every more specific flavour must be offered first. Append new
flavours *before* `SfxResolver`, never after.

`load_from_config` deliberately lives in `library_resolver.py` rather
than here: it builds the cache and HTTP session, which are that module's
concerns, and importing them here would close an import cycle.
"""

from __future__ import annotations

from .alma import AlmaResolver
from .base import (
    FULLTEXT_SERVICE_TYPE,
    OPENURL_CONTEXT_PARAMS,
    PLATFORM_PRIORITY,
    FulltextTarget,
    LibraryResolver,
    Platform,
    ResolverRequest,
    effective_host,
    host_matches_domains,
    local_name,
    platform_priority_from_keys,
)
from .sfx import SfxResolver

#: Most specific first; the SFX fallback must stay last. See module docstring.
RESOLVERS: tuple[type[LibraryResolver], ...] = (AlmaResolver, SfxResolver)


def resolver_for(
    openurl_base: str, override: str = "",
) -> LibraryResolver | None:
    """Resolver instance for `openurl_base`, or None when unconfigured.

    `override` is the `[library] resolver` value: a flavour name, or
    `"auto"`/`""` for autodetection. An unrecognised override falls
    through to autodetection rather than raising — a typo in config
    should degrade to the previous behaviour, not stop a run that would
    otherwise work.
    """
    base = (openurl_base or "").strip()
    if not base:
        return None

    wanted = (override or "").strip().lower()
    if wanted and wanted != "auto":
        for cls in RESOLVERS:
            if cls.flavour == wanted:
                return cls(base)

    for cls in RESOLVERS:
        if cls.matches(base):
            return cls(base)
    return None


__all__ = [
    "AlmaResolver",
    "FULLTEXT_SERVICE_TYPE",
    "FulltextTarget",
    "LibraryResolver",
    "OPENURL_CONTEXT_PARAMS",
    "PLATFORM_PRIORITY",
    "Platform",
    "RESOLVERS",
    "ResolverRequest",
    "SfxResolver",
    "effective_host",
    "host_matches_domains",
    "local_name",
    "platform_priority_from_keys",
    "resolver_for",
]
