"""Every bare `requests.Session()` in enrich_pdfs says why it is bare.

A bare session has no retry adapter, so one transient 429/5xx is a lost
answer. `http_client.build_session()` is the default; the two exceptions
left (the localhost Zotero ping and the serial resolver pre-flight) must
carry the reason beside them, so a reader can tell a decision from an
oversight.
"""

from __future__ import annotations

from pathlib import Path

import enrich_pdfs


def test_bare_sessions_document_why_they_skip_build_session() -> None:
    lines = Path(enrich_pdfs.__file__).read_text(encoding="utf-8").splitlines()
    bare = [i for i, line in enumerate(lines) if "_requests.Session()" in line]
    assert bare, "expected the documented exceptions to still exist"
    for i in bare:
        context = "\n".join(lines[max(0, i - 12):i])
        assert "build_session" in context, (
            f"enrich_pdfs.py:{i + 1} creates a bare Session with no reason "
            f"given; use http_client.build_session() or say why not"
        )
