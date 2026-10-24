"""Shared stubs for tests that drive `wizard.main()`.

The wizard's first step is the Zotero Desktop gate (a local read test and
a create-and-delete write test against localhost:23119). The unit network
guard blocks those sockets, so every test that runs `main()` for something
else replaces the gate with a pass — and the Better BibTeX probe that runs
once the gate has passed, which talks to the same port. The gate itself is
tested in `test_wizard_zotero_gate.py`.

The wizard is loaded by path under several module names, so this takes
the loaded module rather than patching one import.
"""

from __future__ import annotations

#: The key the stubbed gate reports as granted.
GATE_KEY = "k"


def stub_zotero_gate(mod, monkeypatch, key: str = GATE_KEY) -> None:
    """Make `mod._zotero_local_gate` pass with `key` as the local write
    key, and Better BibTeX report as installed."""
    monkeypatch.setattr(
        mod, "_zotero_local_gate",
        lambda interactive, existing: (True, {"local_api_key": key}),
    )
    monkeypatch.setattr(
        mod, "_check_zotero_bbt", lambda *a, **k: (mod.ZOTERO_BBT_STATUS_OK, "stub"),
    )
