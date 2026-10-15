"""The `[pdf]` retrieval policy (`core/pdf_policy.py`) and how `enrich_pdfs`
enforces it: precedence, the `min_version` gate with reject-and-continue
in the cascade, the browser / Connector gates, the paid position, and the
generated `--sources` help.

No network: fetchers are fakes writing into a tmp cache.
"""

from __future__ import annotations

import argparse
import csv
import io
from pathlib import Path
from unittest.mock import MagicMock

import enrich_pdfs
import fetchers
import pdf_fetch_log
import pytest
from core import config_loader, pdf_policy
from fetchers import pdf_version
from log_schemas import PDF_FETCH_FIELDS

# ---------------------------------------------------------------------------
# Resolution and precedence
# ---------------------------------------------------------------------------


def test_defaults() -> None:
    p = pdf_policy.resolve()
    assert (p.min_version, p.reconstructed, p.paid) == ("accepted", False, "prefer")
    assert set(p.sources.values()) == {"default"}


def test_each_tier_beats_the_one_below() -> None:
    p = pdf_policy.resolve(
        global_cfg={"min_version": "accepted", "paid": "off", "reconstructed": True},
        legacy={"paid": ("prefer", "[openalex] use_paid_content_api")},
        project={"min_version": "published", "paid": "last_resort"},
        project_label="screening_config.py",
        flags={"paid": ("prefer", "--paid")},
    )
    assert p.min_version == "published"
    assert p.sources["min_version"] == "project (screening_config.py)"
    assert p.paid == "prefer" and p.sources["paid"] == "flag (--paid)"
    assert p.reconstructed is True and p.sources["reconstructed"] == "global"


def test_pdf_table_supersedes_the_legacy_keys() -> None:
    p = pdf_policy.resolve(
        global_cfg={"reconstructed": False, "paid": "prefer"},
        legacy={
            "reconstructed": (True, "[elsevier] render_xml_to_pdf"),
            "paid": ("off", "[openalex] use_paid_content_api"),
        },
    )
    assert p.reconstructed is False and p.paid == "prefer"


def test_legacy_keys_still_work_alone() -> None:
    p = pdf_policy.resolve(legacy={
        "reconstructed": (True, "[elsevier] render_xml_to_pdf"),
        "paid": ("off", "[openalex] use_paid_content_api"),
    })
    assert p.reconstructed is True and p.paid == "off"
    assert p.sources["paid"] == "legacy [openalex] use_paid_content_api"


@pytest.mark.parametrize("raw,expected", [
    (False, "off"), ("false", "off"), ("0", "off"),
    (True, "prefer"), ("yes", "prefer"),
    ("", None), (None, None), ("perhaps", None),
])
def test_legacy_paid_mapping(raw, expected) -> None:
    assert pdf_policy.legacy_paid(raw) == expected


@pytest.mark.parametrize("raw,expected", [
    (True, True), ("true", True), (False, False), ("no", False),
    ("perhaps", False), ("", None), (None, None),
])
def test_legacy_reconstructed_mapping(raw, expected) -> None:
    assert pdf_policy.legacy_reconstructed(raw) == expected


@pytest.mark.parametrize("tier", ["global_cfg", "project"])
def test_an_unknown_value_is_an_error_not_the_default(tier) -> None:
    """A typo silently meaning `accepted` would attach what the user
    asked to keep out."""
    with pytest.raises(pdf_policy.PolicyError, match="min_version"):
        pdf_policy.resolve(**{tier: {"min_version": "publisehd"}})
    with pytest.raises(pdf_policy.PolicyError, match="reconstructed"):
        pdf_policy.resolve(**{tier: {"reconstructed": "maybe"}})
    with pytest.raises(pdf_policy.PolicyError, match="paid"):
        pdf_policy.resolve(**{tier: {"paid": "always"}})


def test_banner_names_every_value_and_its_source() -> None:
    p = pdf_policy.resolve(
        global_cfg={"paid": "off"}, flags={"min_version": ("preprint", "--allow-preprints")},
    )
    line = p.banner()
    assert "min_version=preprint (flag (--allow-preprints))" in line
    assert "reconstructed=false (default)" in line
    assert "paid=off (global)" in line


def test_project_constants_are_parsed_not_executed(tmp_path: Path) -> None:
    """The review's config may import things the PDF run has no business
    loading; only the three literals are read."""
    cfg = tmp_path / "screening_config.py"
    cfg.write_text(
        "import module_that_does_not_exist\n"
        "raise SystemExit('executed')\n"
        'TAG_PREFIX = ""\n'
        'PDF_MIN_VERSION = "published"\n'
        "PDF_RECONSTRUCTED: bool = True\n"
        "# PDF_PAID = 'off'\n",
        encoding="utf-8",
    )
    assert pdf_policy.read_project_constants(cfg) == {
        "min_version": "published", "reconstructed": True,
    }


def test_a_non_literal_project_constant_is_an_error(tmp_path: Path) -> None:
    cfg = tmp_path / "screening_config.py"
    cfg.write_text('import os\nPDF_PAID = os.environ["X"]\n', encoding="utf-8")
    with pytest.raises(pdf_policy.PolicyError, match="PDF_PAID"):
        pdf_policy.read_project_constants(cfg)


def test_template_constants_are_commented_examples() -> None:
    """Shipped commented out, so the global answers apply until a
    project opts in — and each example is a valid value."""
    template = Path(__file__).resolve().parents[2] / "templates" / "screening_config.py"
    assert pdf_policy.read_project_constants(template) == {}
    text = template.read_text(encoding="utf-8")
    for const in pdf_policy.PROJECT_CONSTANTS.values():
        assert f"# {const} = " in text


def test_version_names_match_pdf_version() -> None:
    assert pdf_policy.VERSION_RANK == pdf_version.VERSIONS


@pytest.mark.parametrize("min_version,version,ok", [
    ("published", "published", True),
    ("published", "accepted", False),
    ("published", "submitted", False),
    ("accepted", "published", True),
    ("accepted", "accepted", True),
    ("accepted", "submitted", False),
    ("preprint", "submitted", True),
    ("accepted", "bogus", False),
])
def test_allows_matrix(min_version, version, ok) -> None:
    assert pdf_policy.allows(min_version, version) is ok


def test_a_preprint_native_item_always_admits_its_preprint() -> None:
    """For an arXiv-native item the preprint is the work itself."""
    assert pdf_policy.allows("published", "submitted", item_is_preprint=True)


# ---------------------------------------------------------------------------
# enrich_pdfs._resolve_pdf_policy: config.toml, project file, flags
# ---------------------------------------------------------------------------


def _ns(**kw) -> argparse.Namespace:
    base = dict(config=None, min_version=None, allow_preprints=False, paid=None)
    base.update(kw)
    return argparse.Namespace(**base)


@pytest.fixture
def cfg(monkeypatch, tmp_path):
    """A fake config.toml, and a cwd with no screening_config.py."""
    data: dict = {}
    monkeypatch.setattr(config_loader, "load_config", lambda: data)
    monkeypatch.chdir(tmp_path)
    return data


def test_resolve_reads_the_pdf_table(cfg) -> None:
    cfg["pdf"] = {"min_version": "published", "paid": "last_resort", "junk": 1}
    p = enrich_pdfs._resolve_pdf_policy(_ns())
    assert (p.min_version, p.paid) == ("published", "last_resort")
    assert p.sources["min_version"] == "global"


def test_resolve_falls_back_to_the_old_keys(cfg, monkeypatch) -> None:
    cfg["elsevier"] = {"render_xml_to_pdf": True}
    cfg["openalex"] = {"use_paid_content_api": False}
    p = enrich_pdfs._resolve_pdf_policy(_ns())
    assert p.reconstructed is True
    assert p.sources["reconstructed"] == "legacy [elsevier] render_xml_to_pdf"
    assert p.paid == "off"

    monkeypatch.setenv("OPENALEX_USE_PAID_CONTENT_API", "true")
    p = enrich_pdfs._resolve_pdf_policy(_ns())
    assert p.paid == "prefer"
    assert p.sources["paid"] == "legacy env OPENALEX_USE_PAID_CONTENT_API"


def test_resolve_picks_up_the_project_file_in_cwd(cfg, tmp_path) -> None:
    cfg["pdf"] = {"min_version": "accepted"}
    (tmp_path / "screening_config.py").write_text(
        'PDF_MIN_VERSION = "published"\n', encoding="utf-8",
    )
    p = enrich_pdfs._resolve_pdf_policy(_ns())
    assert p.min_version == "published"
    assert p.sources["min_version"] == "project (screening_config.py)"


def test_resolve_explicit_config_must_exist(cfg) -> None:
    with pytest.raises(pdf_policy.PolicyError, match="not found"):
        enrich_pdfs._resolve_pdf_policy(_ns(config="nope/screening_config.py"))


def test_allow_preprints_raises_the_policy_for_one_run(cfg, tmp_path) -> None:
    (tmp_path / "screening_config.py").write_text(
        'PDF_MIN_VERSION = "accepted"\n', encoding="utf-8",
    )
    p = enrich_pdfs._resolve_pdf_policy(_ns(allow_preprints=True))
    assert p.min_version == "preprint"
    assert p.sources["min_version"] == "flag (--allow-preprints)"


def test_flags_beat_everything(cfg) -> None:
    cfg["pdf"] = {"min_version": "accepted", "paid": "off"}
    p = enrich_pdfs._resolve_pdf_policy(_ns(min_version="published", paid="prefer"))
    assert (p.min_version, p.paid) == ("published", "prefer")


def test_policy_drives_the_fetcher_switches() -> None:
    c = enrich_pdfs.Config()
    enrich_pdfs._apply_policy_to_config(
        c, pdf_policy.PdfPolicy(reconstructed=True, paid="off"),
    )
    assert c.elsevier_render_xml_to_pdf is True
    assert c.openalex_use_paid_content_api is False


def test_main_rejects_contradictory_flags(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "sys.argv",
        ["enrich_pdfs.py", "--user", "--allow-preprints", "--min-version", "published"],
    )
    assert enrich_pdfs.main() == 2
    assert "contradicts" in capsys.readouterr().err


def test_main_rejects_a_bad_policy_value(cfg, monkeypatch, capsys) -> None:
    cfg["pdf"] = {"min_version": "vor"}
    monkeypatch.setattr("sys.argv", ["enrich_pdfs.py", "--user"])
    assert enrich_pdfs.main() == 2
    assert "PDF policy" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# min_version in the cascade: reject and continue
# ---------------------------------------------------------------------------


def _write_pdf(path: Path, marker: bytes = b"x") -> Path:
    path.write_bytes(b"%PDF-1.4\n" + marker + b"\n%%EOF\n")
    return path


class _Fake:
    """A fetcher that writes the shared `<doi>.pdf` slot and records the
    version it read off its "API", as the API-field sources do."""

    def __init__(self, name: str, version: str | None, cache: Path) -> None:
        self.name, self.version, self.cache = name, version, cache
        self.calls = 0

    def fetch_pdf(self, doi, *, cache_dir):
        self.calls += 1
        if self.version is None:
            return None
        path = Path(cache_dir) / "slot.pdf"
        if path.exists():
            return path, f"cache://{path}"
        _write_pdf(path, self.name.encode())
        pdf_version.record(path, self.version, source=self.name)
        return path, f"https://{self.name}.example/x.pdf"


def _item(doi="10.1234/abc") -> dict:
    return {"key": "K1", "data": {"DOI": doi, "itemType": "journalArticle"}}


def test_published_policy_refuses_and_moves_on(tmp_path) -> None:
    log = tmp_path / "fail.csv"
    first = _Fake("unpaywall", pdf_version.ACCEPTED, tmp_path)
    second = _Fake("semantic_scholar", pdf_version.PUBLISHED, tmp_path)
    refused: list = []
    res = enrich_pdfs._try_cascade(
        _item(), [first, second], str(tmp_path),
        failure_log_path=str(log), min_version="published", rejected=refused,
    )
    assert res is not None and res[1] == "semantic_scholar"
    assert refused == [("unpaywall", "accepted")]
    # The refused copy left the shared slot (else the next source would
    # have served it back as a cache hit) and kept its sidecar.
    aside = tmp_path / enrich_pdfs.BELOW_POLICY_DIR / "slot.pdf"
    assert aside.read_bytes().startswith(b"%PDF-1.4\nunpaywall")
    assert pdf_version.recorded(aside) == "accepted"
    rows = pdf_fetch_log.read_failures(log)
    assert [(r["source"], r["cause"]) for r in rows] == [
        ("unpaywall", "VERSION_BELOW_POLICY"),
    ]


def test_a_refused_cache_hit_gets_its_source_one_fresh_ask(tmp_path) -> None:
    """A cached file of unknown provenance reads `accepted`; the source
    that would download the publisher's own PDF must still get to."""
    slot = _write_pdf(tmp_path / "slot.pdf", b"legacy")    # no sidecar
    src = _Fake("sciencedirect", None, tmp_path)

    def fetch(doi, *, cache_dir):
        src.calls += 1
        if slot.exists():
            return slot, f"cache://{slot}"
        _write_pdf(slot, b"fresh")
        return slot, "https://api.elsevier.com/x"

    src.fetch_pdf = fetch
    res = enrich_pdfs._try_cascade(
        _item(), [src], str(tmp_path), min_version="published", rejected=[],
    )
    assert res is not None and src.calls == 2
    assert slot.read_bytes().startswith(b"%PDF-1.4\nfresh")


def test_default_policy_refuses_a_submitted_copy(tmp_path) -> None:
    refused: list = []
    res = enrich_pdfs._try_cascade(
        _item(), [_Fake("unpaywall", pdf_version.SUBMITTED, tmp_path)],
        str(tmp_path), min_version="accepted", rejected=refused,
    )
    assert res is None and refused == [("unpaywall", "submitted")]


def test_an_arxiv_native_item_keeps_its_own_pdf(tmp_path) -> None:
    res = enrich_pdfs._try_cascade(
        _item("10.48550/arxiv.2101.00001"),
        [_Fake("arxiv", pdf_version.SUBMITTED, tmp_path)],
        str(tmp_path), min_version="published",
    )
    assert res is not None


def test_no_policy_means_no_gate(tmp_path) -> None:
    res = enrich_pdfs._try_cascade(
        _item(), [_Fake("unpaywall", pdf_version.SUBMITTED, tmp_path)], str(tmp_path),
    )
    assert res is not None


class _Log:
    def __init__(self) -> None:
        self.rows: list[dict] = []
        self._w = csv.DictWriter(io.StringIO(), fieldnames=PDF_FETCH_FIELDS)

    def writerow(self, row: dict) -> None:
        self._w.writerow(row)
        self.rows.append(row)


def _run_args(tmp_path: Path, min_version: str) -> argparse.Namespace:
    return argparse.Namespace(
        workers=1, cache_dir=str(tmp_path), failure_log_csv="", dry_run=False,
        no_check_text=True, item_types=("journalArticle",),
        pdf_policy=pdf_policy.PdfPolicy(min_version=min_version),
    )


def test_api_cascade_logs_skipped_below_policy(tmp_path) -> None:
    log = _Log()
    src = _Fake("unpaywall", pdf_version.ACCEPTED, tmp_path)
    src.handles_doi = lambda doi: True
    counts = enrich_pdfs._run_api_cascade(
        [_item()], [src], _run_args(tmp_path, "published"), "2026-10-15",
        MagicMock(), log,
    )
    assert counts == (0, 1, 0)
    (row,) = log.rows
    assert row["status"] == "skipped_below_policy"
    assert row["source"] == "unpaywall"
    assert "accepted copy via unpaywall" in row["detail"]
    assert "skipped_below_policy" in enrich_pdfs.pdf_run_report.STATUS_INFO


# ---------------------------------------------------------------------------
# The attach path (browser handlers, Pass-2, cache recovery) and Pass 3
# ---------------------------------------------------------------------------


def test_attach_refuses_a_file_below_policy(tmp_path) -> None:
    path = _write_pdf(tmp_path / "x.pdf")
    pdf_version.record(path, pdf_version.ACCEPTED)
    zot, log = MagicMock(), _Log()
    ok = enrich_pdfs._attach_and_log(
        zot, log, run_date="d", item_key="K1", doi="10.1/x", title="T",
        source="sage", pdf_path=path, check_text=False, min_version="published",
    )
    assert ok is False
    zot.attach_pdf.assert_not_called()
    assert log.rows[0]["status"] == "skipped_below_policy"
    assert not path.exists()


def test_attach_admits_a_browser_handler_copy_under_published(tmp_path) -> None:
    """Browser handlers settle `published`, so the gate lets them through."""
    path = _write_pdf(tmp_path / "x.pdf")
    pdf_version.settle(
        path, source="sage", source_url="https://journals.sagepub.com/x",
        static=pdf_version.BROWSER_HANDLER_VERSION,
    )
    assert enrich_pdfs._version_allowed(
        "published", pdf_version.version_of(path), "10.1/x",
    )


def test_cache_recovery_leaves_a_below_policy_copy_to_the_cascade(tmp_path) -> None:
    from fetchers.browser.base import cache_path_for

    path = cache_path_for(str(tmp_path), "10.1/x")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"%PDF-1.4\n" + b"0" * 2000 + b"\n%%EOF\n")
    pdf_version.record(path, pdf_version.ACCEPTED)
    zot, log = MagicMock(), _Log()
    done = enrich_pdfs._attach_from_cache(
        [_item("10.1/x")], zot, log, _run_args(tmp_path, "published"), "d",
    )
    assert done == [] and log.rows == []
    zot.attach_pdf.assert_not_called()


@pytest.mark.parametrize("version,refused", [
    (pdf_version.ACCEPTED, True), (pdf_version.PUBLISHED, False),
])
def test_connector_route_gate(tmp_path, version, refused) -> None:
    log = _Log()
    entry = {
        "item_key": "K1", "doi": "10.1/x", "title": "T",
        "resolver_target_url": "https://repo.example/x", "pdf_version": version,
    }
    args = _run_args(tmp_path, "published")
    assert enrich_pdfs._route_below_policy(
        entry, via="connector", args=args, log_writer=log, run_date="d",
    ) is refused
    assert bool(log.rows) is refused
    if refused:
        assert log.rows[0]["status"] == "skipped_below_policy"


def test_below_policy_is_recoverable_and_bucketed() -> None:
    cause = pdf_fetch_log.FailureCause.VERSION_BELOW_POLICY.value
    assert cause in pdf_fetch_log.RECOVERABLE_CAUSES
    assert cause in pdf_fetch_log.SUGGESTED_FE_CODE
    prec = pdf_fetch_log.CAUSE_PRECEDENCE
    assert prec.index(cause) < prec.index(pdf_fetch_log.FailureCause.UNAVAILABLE.value)


# ---------------------------------------------------------------------------
# paid position, and the --sources help
# ---------------------------------------------------------------------------


def _names(**kw) -> list[str]:
    return [s.name for s in fetchers.pdf_sources(MagicMock(), None, **kw)]


def test_paid_prefer_is_todays_position() -> None:
    names = _names()
    assert names.index("openalex_content") < names.index("arxiv")
    assert names.index("openalex_content") > names.index("pubmed_central")


def test_paid_off_drops_it_even_when_named() -> None:
    assert "openalex_content" not in _names(paid="off")
    assert _names(paid="off", names=["openalex_content"]) == []


def test_paid_last_resort_follows_every_free_source() -> None:
    assert _names(paid="last_resort")[-1] == "openalex_content"
    assert _names(paid="last_resort", allow_preprints=True)[-1] == "openalex_content"


def test_unknown_paid_position_raises() -> None:
    with pytest.raises(ValueError):
        fetchers.pdf_sources(MagicMock(), None, paid="sometimes")


@pytest.mark.parametrize("paid", pdf_policy.PAID_MODES)
def test_static_name_list_matches_the_registry(paid) -> None:
    assert fetchers.default_pdf_source_names(paid=paid) == _names(paid=paid)


def test_policy_and_registry_agree_on_paid_modes() -> None:
    assert fetchers.PAID_POSITIONS == pdf_policy.PAID_MODES


def test_sources_help_is_generated_from_the_registry() -> None:
    parser = enrich_pdfs._build_parser()
    (action,) = [a for a in parser._actions if "--sources" in a.option_strings]
    assert " → ".join(fetchers.default_pdf_source_names()) in action.help
    assert "elsevier+springer+crossref" not in action.help
