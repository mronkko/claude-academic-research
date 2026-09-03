"""The recovered-PDF cache must be validated and version-checked before it is served.

`fetch_pdf` short-circuited on `<doi>-tdm-recovered.pdf` with no checks
at all, while the non-recovered branch three lines below it validated —
and that branch's own comment says why: "an entry written by an earlier,
unvalidated run may be truncated, and returning it unchecked made the
corruption permanent."

The recovered branch has the same failure mode plus one the other branch
does not. The XML→PDF transformation *changed* in 0.15.0: files made
before it carry no front matter (no title, no authors, no abstract, no
DOI) and have footnotes spliced into the middle of sentences. Every
recovery ever made carries the same `-tdm-recovered` suffix, so the file
name cannot tell the two apart. Serving one unchecked turns a deliberate
re-fetch into a silent no-op that reports success — which is exactly what
happened downstream: a corpus of 530 recovered PDFs, 131 of them written
by a pre-0.15.0 build, all of which would have come back from `cache://`
unchanged had the run not passed a scratch cache directory by luck.

The stamp `_recovery_note` writes into the file is what makes the two
distinguishable, so these tests pin that it is actually read back.

One thing deliberately NOT tested here, because it must not be
implemented: a size floor. Short recovered articles are real — Lancet
correspondence and conference abstracts come out at 3-4KB and are
legitimate, in one downstream corpus larger than the originals they
replaced. Validity is about structure and provenance, never length.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from fetchers.sciencedirect import (
    _CURRENT_RECOVERY_VERSION,
    ScienceDirectSource,
    _document_flow,
    _parse_version,
    _plugin_version,
    _recovery_note,
    _render_text_pdf,
    _stale_recovery_reason,
    stamped_recovery_version,
)

DOI = "10.1016/j.cached.2020.01.001"
CACHE_NAME = "10.1016_j.cached.2020.01.001-tdm-recovered.pdf"


def _make_source(api_key: str = "fake-key") -> ScienceDirectSource:
    cfg = MagicMock()
    cfg.elsevier_api_key = api_key
    src = ScienceDirectSource(config=cfg)
    src.http = MagicMock()
    return src


def _fake_pdf(marker: bytes = b"body") -> bytes:
    """Structurally valid enough for `_pdf_validate`, with a plain-text marker."""
    return b"%PDF-1.4\n" + marker + b"\n" + b"0" * 2000 + b"\n%%EOF\n"


def _render_recovered(path: Path, *, n_notes: int = 1) -> None:
    """Write a real recovered PDF the way `_fetch_xml_fallback` does."""
    blocks = [("h1", "Introduction"), ("p", "Some body text. " * 40)]
    notes = ["A footnote moved out of the body. " * 3] * n_notes
    _render_text_pdf(
        _document_flow(blocks, notes),
        path,
        note=_recovery_note(len(notes)),
        meta={"title": "A Title", "journal": "J", "doi": DOI},
    )


# ---------------------------------------------------------------------------
# stamped_recovery_version — reading the stamp back out of a PDF
# ---------------------------------------------------------------------------


def test_stamp_round_trips_through_a_real_rendered_pdf(tmp_path: Path) -> None:
    """The writer and the reader have to agree. reportlab compresses page
    content streams (ASCII85 + Flate), so a naive byte search for the
    stamp finds nothing — the reader has to decode."""
    path = tmp_path / CACHE_NAME
    _render_recovered(path)
    assert stamped_recovery_version(path) == _parse_version(_plugin_version())
    assert stamped_recovery_version(path) >= _CURRENT_RECOVERY_VERSION


def test_stamp_round_trips_when_the_article_had_no_footnotes(tmp_path: Path) -> None:
    """`_recovery_note` has two wordings; both carry the version."""
    path = tmp_path / CACHE_NAME
    _render_recovered(path, n_notes=0)
    assert stamped_recovery_version(path) == _parse_version(_plugin_version())


def test_unstamped_pdf_has_no_version(tmp_path: Path) -> None:
    path = tmp_path / CACHE_NAME
    path.write_bytes(_fake_pdf(b"pre-0.15.0 recovery, no stamp anywhere"))
    assert stamped_recovery_version(path) is None


def test_stamp_is_read_from_plain_bytes_when_uncompressed(tmp_path: Path) -> None:
    """The version also goes into the PDF Info dictionary, which reportlab
    writes uncompressed — the cheap path, and the one that survives a
    change in how reportlab encodes content streams."""
    path = tmp_path / CACHE_NAME
    path.write_bytes(_fake_pdf(b"claude-academic-research 0.14.2"))
    assert stamped_recovery_version(path) == (0, 14, 2)


def test_rendered_pdf_carries_the_stamp_in_its_info_dictionary(tmp_path: Path) -> None:
    """Not just recoverable by decoding streams — present as plain bytes."""
    path = tmp_path / CACHE_NAME
    _render_recovered(path)
    assert b"claude-academic-research" in path.read_bytes()


def _render_legacy_recovered(path: Path, version: str) -> None:
    """A 0.15.x recovery: the stamp on the page, and nowhere else.

    0.15.0 and 0.15.1 wrote the provenance line as a visible paragraph
    only — the Info-dictionary copy arrived with this check. Those files
    exist in real corpora (one downstream library holds 139 of them), and
    they are current: refusing them would re-fetch a few hundred articles
    through a publisher's rate-limited API to replace files that are
    already correct.
    """
    from reportlab.lib.pagesizes import letter
    from reportlab.lib.styles import getSampleStyleSheet
    from reportlab.platypus import Paragraph, SimpleDocTemplate

    styles = getSampleStyleSheet()
    note = (
        f"Text recovered from Elsevier full-text XML by "
        f"claude-academic-research {version}. No footnotes were present "
        f"in the body."
    )
    SimpleDocTemplate(str(path), pagesize=letter).build(
        [Paragraph(note, styles["Italic"]),
         Paragraph("Body text. " * 40, styles["BodyText"])]
    )


def test_stamp_is_read_from_compressed_page_text(tmp_path: Path) -> None:
    """The 0.15.x case: no Info-dictionary entry, so the only copy of the
    stamp is inside an ASCII85+Flate content stream. A plain byte search
    finds nothing there — assert it explicitly, because if it ever did,
    this test would pass without exercising the decoder at all."""
    path = tmp_path / CACHE_NAME
    _render_legacy_recovered(path, "0.15.1")
    assert b"claude-academic-research" not in path.read_bytes()
    assert stamped_recovery_version(path) == (0, 15, 1)
    assert _stale_recovery_reason(path) is None


def test_older_stamp_in_compressed_page_text_is_still_stale(tmp_path: Path) -> None:
    path = tmp_path / CACHE_NAME
    _render_legacy_recovered(path, "0.14.8")
    assert stamped_recovery_version(path) == (0, 14, 8)
    assert _stale_recovery_reason(path) is not None


def test_an_undecodable_pdf_reads_as_unstamped(tmp_path: Path) -> None:
    """The decoder's failure direction has to be re-fetch, not trust: a
    wasted fetch costs quota, a wrongly-trusted entry costs correctness."""
    path = tmp_path / CACHE_NAME
    path.write_bytes(
        b"%PDF-1.4\nstream\n" + bytes(range(256)) * 8 + b"\nendstream\n"
        + b"0" * 2000 + b"\n%%EOF\n"
    )
    assert stamped_recovery_version(path) is None
    assert _stale_recovery_reason(path) is not None


# ---------------------------------------------------------------------------
# _stale_recovery_reason — the staleness verdict
# ---------------------------------------------------------------------------


def test_no_stamp_is_stale(tmp_path: Path) -> None:
    path = tmp_path / CACHE_NAME
    path.write_bytes(_fake_pdf(b"no stamp"))
    reason = _stale_recovery_reason(path)
    assert reason is not None
    assert "no version stamp" in reason


def test_older_stamp_is_stale(tmp_path: Path) -> None:
    path = tmp_path / CACHE_NAME
    path.write_bytes(_fake_pdf(b"claude-academic-research 0.14.9"))
    reason = _stale_recovery_reason(path)
    assert reason is not None
    assert "0.14.9" in reason


def test_current_stamp_is_not_stale(tmp_path: Path) -> None:
    path = tmp_path / CACHE_NAME
    _render_recovered(path)
    assert _stale_recovery_reason(path) is None


def test_a_later_version_is_not_stale(tmp_path: Path) -> None:
    """The floor is a minimum, not an equality — a cache entry written by
    a newer build than the one reading it is still current."""
    path = tmp_path / CACHE_NAME
    path.write_bytes(_fake_pdf(b"claude-academic-research 99.0.0"))
    assert _stale_recovery_reason(path) is None


def test_a_short_recovered_pdf_is_not_stale_for_being_short(tmp_path: Path) -> None:
    """Genuinely short articles exist — Lancet correspondence, conference
    abstracts. A flat byte floor flagged four such files downstream, all
    of them correct. Length is not evidence of anything."""
    path = tmp_path / CACHE_NAME
    _render_text_pdf(
        [("p", "A one-paragraph correspondence item.")],
        path,
        note=_recovery_note(0),
        meta={"title": "Short", "journal": "The Lancet", "doi": DOI},
    )
    assert path.stat().st_size < 5000
    assert _stale_recovery_reason(path) is None


# ---------------------------------------------------------------------------
# fetch_pdf — the branch that was serving these unchecked
# ---------------------------------------------------------------------------


def test_fetch_pdf_serves_a_current_recovered_cache_without_network(
    tmp_path: Path,
) -> None:
    src = _make_source()
    cached = tmp_path / CACHE_NAME
    _render_recovered(cached)
    src.http.get.side_effect = AssertionError("should not be called on cache hit")

    result = src.fetch_pdf(DOI, cache_dir=tmp_path)
    assert result is not None
    path, source = result
    assert path == cached
    assert source.startswith("cache://")


def test_fetch_pdf_does_not_serve_an_unstamped_recovered_cache(
    tmp_path: Path,
) -> None:
    """The 131-file case. A pre-0.15.0 entry must not satisfy a re-fetch."""
    src = _make_source()
    cached = tmp_path / CACHE_NAME
    cached.write_bytes(_fake_pdf(b"pre-0.15.0 recovery"))
    src.http.get.return_value = MagicMock(
        status_code=404, content=b"", headers={},
    )

    result = src.fetch_pdf(DOI, cache_dir=tmp_path)
    assert result is None, "stale recovered cache was served as a fresh result"
    assert src.http.get.called, "a stale cache entry must fall through to the network"


def test_fetch_pdf_does_not_serve_a_recovered_cache_from_an_older_build(
    tmp_path: Path,
) -> None:
    src = _make_source()
    cached = tmp_path / CACHE_NAME
    cached.write_bytes(_fake_pdf(b"claude-academic-research 0.14.9"))
    src.http.get.return_value = MagicMock(
        status_code=404, content=b"", headers={},
    )

    assert src.fetch_pdf(DOI, cache_dir=tmp_path) is None
    assert src.http.get.called


def test_fetch_pdf_leaves_a_stale_recovered_file_on_disk(tmp_path: Path) -> None:
    """Refusing to serve it is not a reason to destroy it.

    The file holds real text, just badly shaped; if the re-fetch cannot
    reach the publisher, deleting it would take away the only copy the
    user has. Not-serving is what makes the failure visible — deleting is
    what would make it irreversible.
    """
    src = _make_source()
    cached = tmp_path / CACHE_NAME
    cached.write_bytes(_fake_pdf(b"pre-0.15.0 recovery"))
    src.http.get.return_value = MagicMock(
        status_code=404, content=b"", headers={},
    )

    src.fetch_pdf(DOI, cache_dir=tmp_path)
    assert cached.exists(), "a stale but readable recovery was deleted"


def test_fetch_pdf_discards_a_corrupt_recovered_cache(tmp_path: Path) -> None:
    """A truncated entry is worthless, not merely outdated — that is the
    case the non-recovered branch already deletes, for the same reason."""
    src = _make_source()
    cached = tmp_path / CACHE_NAME
    cached.write_bytes(b"%PDF-1.4\n" + b"0" * 2000)  # no %%EOF trailer
    src.http.get.return_value = MagicMock(
        status_code=404, content=b"", headers={},
    )

    src.fetch_pdf(DOI, cache_dir=tmp_path)
    assert not cached.exists(), "a corrupt cache entry was kept"


def test_fetch_pdf_reports_the_stale_version_in_the_log(
    tmp_path: Path, caplog
) -> None:
    """The failure was silent before; the log line is what makes a
    re-fetch auditable after the fact."""
    src = _make_source()
    cached = tmp_path / CACHE_NAME
    cached.write_bytes(_fake_pdf(b"claude-academic-research 0.14.9"))
    src.http.get.return_value = MagicMock(
        status_code=404, content=b"", headers={},
    )

    with caplog.at_level("WARNING"):
        src.fetch_pdf(DOI, cache_dir=tmp_path)
    assert any("0.14.9" in r.getMessage() for r in caplog.records)
