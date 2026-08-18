"""The run report must not make one Zotero request per missing item.

Reported from a live run as "the script did not terminate but does not
seem to progress either". It had terminated its actual work — every PDF
was attached and logged — and was then making 1,133 sequential
`get_item` calls to decorate the list of what was still missing, with no
output while it did.

This is the third place in this pipeline where a serial loop over a
large queue printed nothing (see the resolver pre-flight and `--plan`),
and the one where the work was avoidable rather than merely quiet:
`ZoteroClient.items_by_keys` batches 50 keys per request.
"""

from __future__ import annotations

import argparse

import enrich_pdfs


class _Zot:
    def __init__(self) -> None:
        self.batch_calls = 0
        self.single_calls = 0

    def items_by_keys(self, keys):
        self.batch_calls += 1
        return [
            {"key": k, "data": {
                "title": f"T{k}", "date": "2020",
                "publicationTitle": "J", "creators": [
                    {"lastName": "Smith"}, {"lastName": "Jones"},
                ],
            }}
            for k in keys
        ]

    def get_item(self, key):
        self.single_calls += 1
        raise AssertionError("per-item lookup in the report")


def _log(tmp_path, n: int):
    path = tmp_path / "pdf_attach_log.csv"
    lines = ["run_date,item_key,doi,title,status,source,detail"]
    for i in range(n):
        lines.append(f"2026-08-18,K{i},10.1/{i},T{i},skipped_no_pdf,,")
    # One success, which must NOT be fetched: the report never itemises
    # resolved items, so looking them up is pure cost.
    lines.append("2026-08-18,OK1,10.1/ok,Done,attached,crossref,")
    path.write_text("\n".join(lines) + "\n")
    return path


def test_the_report_fetches_metadata_in_one_batch(tmp_path, capsys) -> None:
    log = _log(tmp_path, 120)
    zot = _Zot()
    args = argparse.Namespace(log_csv=str(log))

    enrich_pdfs._print_run_report(args, zot, None)

    assert zot.single_calls == 0, "fell back to one request per item"
    assert zot.batch_calls == 1, f"{zot.batch_calls} batch calls, expected 1"
    out = capsys.readouterr().out
    assert "120 unresolved items" in out, out[:400]
    assert "Smith & Jones" in out


def test_resolved_items_are_not_looked_up(tmp_path) -> None:
    """The report itemises only what is still missing."""
    log = _log(tmp_path, 3)
    seen: list[list[str]] = []

    class _Z(_Zot):
        def items_by_keys(self, keys):
            seen.append(list(keys))
            return super().items_by_keys(keys)

    enrich_pdfs._print_run_report(
        argparse.Namespace(log_csv=str(log)), _Z(), None,
    )
    assert seen and "OK1" not in seen[0], seen


def test_a_metadata_failure_still_prints_a_report(tmp_path, capsys) -> None:
    """A worse report beats no report — the statuses and DOIs are the
    part that matters, and they come from the log, not from Zotero."""
    log = _log(tmp_path, 5)

    class _Broken(_Zot):
        def items_by_keys(self, keys):
            raise RuntimeError("zotero down")

    enrich_pdfs._print_run_report(
        argparse.Namespace(log_csv=str(log)), _Broken(), None,
    )
    out = capsys.readouterr().out
    assert "skipped_no_pdf" in out
    assert "metadata lookup failed" in out


def test_citation_fields_abbreviates_long_author_lists() -> None:
    got = enrich_pdfs._citation_fields({
        "creators": [{"lastName": n} for n in ("A", "B", "C", "D")],
        "date": "2019-04", "publicationTitle": "J", "title": "T",
    })
    assert got["authors"] == "A et al."
    assert got["year"] == "2019"
