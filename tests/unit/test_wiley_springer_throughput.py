"""Two sources that failed quietly, both found by watching a live run.

Neither crashed. Both produced exactly the output a genuinely
unavailable article produces, which is why a 1,895-item pass reported
them as misses and nobody looked again.

- **Wiley TDM lost more than half its yield to its own rate limiter.**
  A fresh `TDMClient` per DOI meant one limiter per worker thread and no
  view of the aggregate; Wiley throttled, the client raised, and the
  handler swallowed it at debug level. Same token, same Wiley-prefix
  items: 47 PDFs at `--workers 4`, 110 one at a time.

- **Springer asked 144 times and was refused 144 times.** The Imperva
  challenge is byte-identical, arrives as HTTP 200 before entitlement is
  evaluated, and every one was logged at warning level.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

# --- Wiley: one client, one caller ------------------------------------


def test_wiley_reuses_one_client_across_calls(tmp_path, monkeypatch) -> None:
    """A client per DOI is a rate limiter per DOI, which is no limiter."""
    import fetchers.wiley as wmod

    monkeypatch.setattr(wmod, "_CLIENT", None, raising=False)
    monkeypatch.setattr(wmod, "_CLIENT_KEY", None, raising=False)

    built: list[int] = []

    class _Client:
        def __init__(self, **kw) -> None:
            built.append(1)

        def download_pdfs(self, dois):
            return []

    import sys
    sys.modules["wiley_tdm"] = SimpleNamespace(TDMClient=_Client)
    sys.modules["wiley_tdm.download_status"] = SimpleNamespace(
        DownloadStatus=SimpleNamespace(SUCCESS="ok"),
    )

    src = wmod.WileySource(None, SimpleNamespace(wiley_tdm_token="t"))
    for doi in ("10.1111/a", "10.1111/b", "10.1002/c"):
        src.fetch_pdf(doi, cache_dir=str(tmp_path))

    assert len(built) == 1, f"built {len(built)} clients for 3 DOIs"


def test_a_wiley_failure_is_no_longer_whispered(tmp_path, monkeypatch, caplog) -> None:
    """`logger.debug` is why throttling read as "article unavailable".
    A failure to *ask* is not an answer and must be visible."""
    import logging

    import fetchers.wiley as wmod

    monkeypatch.setattr(wmod, "_CLIENT", None, raising=False)
    monkeypatch.setattr(wmod, "_CLIENT_KEY", None, raising=False)

    class _Client:
        def __init__(self, **kw) -> None:
            pass

        def download_pdfs(self, dois):
            raise RuntimeError("429 Too Many Requests")

    import sys
    sys.modules["wiley_tdm"] = SimpleNamespace(TDMClient=_Client)
    sys.modules["wiley_tdm.download_status"] = SimpleNamespace(
        DownloadStatus=SimpleNamespace(SUCCESS="ok"),
    )

    src = wmod.WileySource(None, SimpleNamespace(wiley_tdm_token="t"))
    with caplog.at_level(logging.WARNING):
        assert src.fetch_pdf("10.1111/x", cache_dir=str(tmp_path)) is None
    assert any("429" in r.getMessage() for r in caplog.records), caplog.text


# --- Springer: stop asking once the wall is confirmed -----------------


_CHALLENGE = (
    b"<html><head><title>Client Challenge</title></head>"
    b"<body>_Incapsula_Resource</body></html>"
)


def _resp(body: bytes, status: int = 200):
    return SimpleNamespace(content=body, status_code=status, text=body.decode())


@pytest.fixture(autouse=True)
def _reset_springer(monkeypatch):
    import fetchers.springer as smod
    monkeypatch.setattr(smod, "_consecutive_challenges", 0, raising=False)
    monkeypatch.setattr(smod, "_gave_up_announced", False, raising=False)
    return smod


def test_springer_stops_asking_after_repeated_challenges(
    _reset_springer, tmp_path,
) -> None:
    """144 identical refusals in one run is not a slow patch; it is a
    wall, and asking the 145th time cannot inform anything."""
    smod = _reset_springer
    calls: list[str] = []

    class _Http:
        def get(self, url, **kw):
            calls.append(url)
            return _resp(_CHALLENGE)

    src = smod.SpringerSource(_Http(), None)
    for i in range(20):
        assert src.fetch_pdf(f"10.1007/x{i}", cache_dir=str(tmp_path)) is None

    assert len(calls) == smod._CHALLENGE_LIMIT, (
        f"kept asking: {len(calls)} requests for 20 items"
    )


def test_a_real_answer_resets_the_springer_breaker(
    _reset_springer, tmp_path,
) -> None:
    """The breaker must not latch on a transient. Only the challenge
    counts toward it, and anything else clears it — that is what keeps
    "the block may be relaxed at any time" true."""
    smod = _reset_springer
    bodies = [_CHALLENGE, _CHALLENGE, b"<html>503 upstream</html>", _CHALLENGE]
    calls: list[str] = []

    class _Http:
        def get(self, url, **kw):
            calls.append(url)
            return _resp(bodies[min(len(calls) - 1, len(bodies) - 1)])

    src = smod.SpringerSource(_Http(), None)
    for i in range(4):
        src.fetch_pdf(f"10.1007/y{i}", cache_dir=str(tmp_path))

    # Two challenges, then a non-challenge reset, so it never latched.
    assert len(calls) == 4
