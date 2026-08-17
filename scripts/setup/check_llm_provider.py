#!/usr/bin/env python3
"""Report which LLM provider this machine is configured to screen with.

Usage:
    python3 check_llm_provider.py

Prints a short status block and exits 0:

    provider: anthropic
    label: Anthropic (Claude)
    selected: yes            # or "no (registry default)"
    local: no
    credential: configured   # or "missing (ANTHROPIC_API_KEY)"
                             # or "not required (local provider)"
    base_url: https://api.anthropic.com

Same purpose as `check_database_access.py`: skills need to know what is
configured, and the Read tool is denied on
`~/.config/academic-research/config.toml` so that keys cannot reach a
conversation. A subprocess that emits only yes/no status is the
supported way in. No key value is ever printed.

Network-free — it reports configuration, not reachability. Ask the
provider what it serves with `resolve_models.py`.
"""

from __future__ import annotations

import sys
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SCRIPTS_ROOT = _HERE.parent
if str(_SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS_ROOT))

from core import llm_provider, providers  # noqa: E402
from core.config_loader import get  # noqa: E402
from core.providers import ProviderSpec  # noqa: E402


def configured_name() -> tuple[str, bool]:
    """`(provider_name, was_selected)`.

    `was_selected` is False when nothing is configured and the registry
    default is standing in — the difference between "the user chose
    Anthropic" and "the user was never asked", which is exactly what a
    skill needs to decide whether to suggest `/setup`.
    """
    raw = get("llm", "provider", env=providers.PROVIDER_ENV).strip()
    if raw and providers.get(raw) is not None:
        return raw.lower(), True
    return providers.DEFAULT_PROVIDER, False


def status_lines(spec: ProviderSpec, selected: bool) -> list[str]:
    """The status block, shared with `set_llm_provider.py` so the two
    scripts cannot drift in what they report."""
    ok, missing = llm_provider.credential_status(spec)
    # `local`, not `api_key_env`: a bring-your-own gateway declares no
    # variable either, and reporting "not required" for a remote
    # authenticated endpoint is worse than saying nothing.
    if spec.local:
        credential = "not required (local provider)"
    elif ok:
        credential = "configured"
    else:
        credential = f"missing ({missing})"
    # A bring-your-own-endpoint provider has no default address, so a
    # blank here is a missing setting rather than "the default applies".
    # Saying which variable supplies it is the whole value of this line
    # for the one provider where it is not obvious.
    base = llm_provider.base_url_for(spec)
    if not base and spec.byo_endpoint:
        base = (
            f"missing — set "
            f"{providers.base_url_location(spec, llm_provider.base_url_env(spec))}; "
            f"no default is shipped"
        )
    return [
        f"provider: {spec.name}",
        f"label: {spec.label}",
        f"selected: {'yes' if selected else 'no (registry default)'}",
        f"local: {'yes' if spec.local else 'no'}",
        f"credential: {credential}",
        f"base_url: {base}",
    ]


def main() -> int:
    name, selected = configured_name()
    for line in status_lines(providers.require(name), selected):
        print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
