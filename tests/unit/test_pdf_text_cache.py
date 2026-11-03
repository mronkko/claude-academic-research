"""Unit tests for `pdf_text_cache` — content-hash-keyed extracted-text cache.

Avoids invoking real `pdftotext` by patching `_run_pdftotext`. The
critical invariant is content-hash invalidation: replacing a PDF's
bytes flips the cache key and triggers a fresh extraction next call.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pdf_text_cache
import pytest


@pytest.fixture
def cache_dir(tmp_path: Path) -> Path:
    """Per-test cache dir — isolated from any project-local `.claude/pdf_text/`."""
    d = tmp_path / "pdf_text"
    return d


def _write_pdf(tmp_path: Path, name: str, contents: bytes) -> Path:
    """Create a fake PDF file with given bytes. We never actually parse it
    — `_run_pdftotext` is patched in tests."""
    p = tmp_path / name
    p.write_bytes(contents)
    return p


def test_first_call_runs_pdftotext_and_caches(tmp_path: Path, cache_dir: Path) -> None:
    pdf = _write_pdf(tmp_path, "paper.pdf", b"%PDF-1.4 hello world bytes")
    with patch.object(pdf_text_cache, "_run_pdftotext", return_value="extracted text") as mock:
        text = pdf_text_cache.get_text("ABCD0001", pdf, cache_dir=cache_dir)
    assert text == "extracted text"
    # Both pdftotext modes run once each; `pdf_columns` merges them.
    assert [c.args for c in mock.call_args_list] == [(pdf, "-layout"), (pdf,)]
    # Cache file exists and has expected name shape.
    cached = list(cache_dir.glob("ABCD0001-*.txt"))
    assert len(cached) == 1
    assert cached[0].read_text(encoding="utf-8") == "extracted text"


def test_an_interrupted_write_leaves_no_cache_entry(
    tmp_path: Path, cache_dir: Path, monkeypatch,
) -> None:
    """A plain write interrupted mid-way left a truncated file that the
    next call served forever as the PDF's text."""
    pdf = _write_pdf(tmp_path, "paper.pdf", b"%PDF-1.4 body")

    def boom(self, *a, **k):
        Path.write_bytes(self, b"trunc")   # partial bytes on disk...
        raise KeyboardInterrupt            # ...then the interrupt

    monkeypatch.setattr(Path, "write_text", boom)
    with patch.object(pdf_text_cache, "_run_pdftotext", return_value="full text"), \
            pytest.raises(KeyboardInterrupt):
        pdf_text_cache.get_text("ABCD0001", pdf, cache_dir=cache_dir)
    assert list(cache_dir.glob("ABCD0001-*.txt")) == []
    assert list(cache_dir.iterdir()) == []


def test_second_call_reads_from_cache_without_running_pdftotext(
    tmp_path: Path, cache_dir: Path,
) -> None:
    pdf = _write_pdf(tmp_path, "paper.pdf", b"%PDF-1.4 some bytes")
    # Prime the cache.
    with patch.object(pdf_text_cache, "_run_pdftotext", return_value="first run"):
        pdf_text_cache.get_text("XYZ0001", pdf, cache_dir=cache_dir)
    # Second call must NOT invoke pdftotext.
    with patch.object(
        pdf_text_cache,
        "_run_pdftotext",
        side_effect=AssertionError("pdftotext must not run on cache hit"),
    ):
        text = pdf_text_cache.get_text("XYZ0001", pdf, cache_dir=cache_dir)
    assert text == "first run"


def test_replacing_pdf_bytes_invalidates_cache(tmp_path: Path, cache_dir: Path) -> None:
    """Critical Elsevier-TDM-remediation contract: when the PDF bytes
    change (e.g. a preview is replaced with a TDM-recovered full-text
    PDF), the cache must invalidate automatically and re-extract."""
    pdf = _write_pdf(tmp_path, "paper.pdf", b"%PDF-1.4 PREVIEW-CONTENT")
    with patch.object(pdf_text_cache, "_run_pdftotext", return_value="preview text"):
        first = pdf_text_cache.get_text("REM0001", pdf, cache_dir=cache_dir)
    assert first == "preview text"

    # Replace PDF bytes — new content, new hash.
    pdf.write_bytes(b"%PDF-1.4 FULL-BODY-RECOVERED")
    with patch.object(pdf_text_cache, "_run_pdftotext", return_value="full body text") as mock:
        second = pdf_text_cache.get_text("REM0001", pdf, cache_dir=cache_dir)
    assert second == "full body text"
    assert mock.call_count == 2          # one extraction: both modes
    # Both versions cached side by side: original preview-cache survives,
    # new full-body cache lives next to it. The hash distinguishes them.
    cached = sorted(cache_dir.glob("REM0001-*.txt"))
    assert len(cached) == 2


def test_clear_item_removes_all_cached_versions(tmp_path: Path, cache_dir: Path) -> None:
    pdf = _write_pdf(tmp_path, "paper.pdf", b"%PDF-1.4 A")
    with patch.object(pdf_text_cache, "_run_pdftotext", return_value="A"):
        pdf_text_cache.get_text("CLR0001", pdf, cache_dir=cache_dir)
    pdf.write_bytes(b"%PDF-1.4 B")
    with patch.object(pdf_text_cache, "_run_pdftotext", return_value="B"):
        pdf_text_cache.get_text("CLR0001", pdf, cache_dir=cache_dir)

    assert len(list(cache_dir.glob("CLR0001-*.txt"))) == 2
    removed = pdf_text_cache.clear_item("CLR0001", cache_dir=cache_dir)
    assert removed == 2
    assert list(cache_dir.glob("CLR0001-*.txt")) == []


def test_clear_item_with_missing_cache_dir_returns_zero(tmp_path: Path) -> None:
    """clear_item is a no-op when the cache dir doesn't exist (fresh project)."""
    assert pdf_text_cache.clear_item("ANY", cache_dir=tmp_path / "does-not-exist") == 0


def test_get_text_raises_when_pdf_missing(tmp_path: Path, cache_dir: Path) -> None:
    with pytest.raises(FileNotFoundError):
        pdf_text_cache.get_text("MISS", tmp_path / "nope.pdf", cache_dir=cache_dir)


def test_get_text_requires_non_empty_item_key(tmp_path: Path, cache_dir: Path) -> None:
    pdf = _write_pdf(tmp_path, "paper.pdf", b"%PDF-1.4 X")
    with pytest.raises(ValueError):
        pdf_text_cache.get_text("", pdf, cache_dir=cache_dir)


def test_text_from_an_older_extractor_is_not_served(tmp_path: Path, cache_dir: Path) -> None:
    """Version 1 cached `-layout` text alone, under `<key>-<hash>.txt`.
    The merged extraction must not pick that file up."""
    pdf = _write_pdf(tmp_path, "paper.pdf", b"%PDF-1.4 bytes")
    cache_dir.mkdir(parents=True)
    old = cache_dir / f"OLD0001-{pdf_text_cache._content_hash(pdf)}.txt"
    old.write_text("interleaved layout text", encoding="utf-8")
    with patch.object(pdf_text_cache, "_run_pdftotext", return_value="new text"):
        assert pdf_text_cache.get_text("OLD0001", pdf, cache_dir=cache_dir) == "new text"
    assert (cache_dir / old.name.replace(".txt", f".v{pdf_text_cache.EXTRACTION_VERSION}.txt")).is_file()
    assert pdf_text_cache.clear_item("OLD0001", cache_dir=cache_dir) == 2   # both kinds removed


def test_the_two_modes_are_merged_page_by_page(tmp_path: Path, cache_dir: Path) -> None:
    pdf = _write_pdf(tmp_path, "paper.pdf", b"%PDF-1.4 bytes")
    outputs = {("-layout",): "LAYOUT", (): "READING"}
    with patch.object(pdf_text_cache, "_run_pdftotext",
                      side_effect=lambda p, *f: outputs[f]), \
            patch("pdf_columns.merge", side_effect=lambda lay, rd: f"{lay}+{rd}"):
        assert pdf_text_cache.get_text("MRG0001", pdf, cache_dir=cache_dir) == "LAYOUT+READING"


def test_extract_works_when_the_module_is_loaded_by_file_path(monkeypatch, tmp_path):
    # A caller that loads pdf_text_cache.py by path has no scripts/pipelines
    # on sys.path, so the sibling pdf_columns must be found next to the file.
    import importlib.util
    import sys

    src = Path(__file__).resolve().parents[2] / "scripts" / "pipelines"
    spec = importlib.util.spec_from_file_location(
        "pdf_text_cache_by_path", src / "pdf_text_cache.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.delitem(sys.modules, "pdf_columns", raising=False)
    monkeypatch.setattr(sys, "path", [p for p in sys.path if Path(p).resolve() != src])
    monkeypatch.setattr(module, "_run_pdftotext", lambda *a, **k: "one page")
    assert module._extract(tmp_path / "x.pdf") == "one page"
