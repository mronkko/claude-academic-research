"""pdf_columns: choosing, per page, between pdftotext's -layout and
reading-order text. Synthetic pages; no pdftotext."""

from __future__ import annotations

import pdf_columns as pc

LEFT = [
    "The sample consisted of employees in a large firm who",
    "completed three surveys over a period of six months and",
    "reported their job satisfaction and turnover intentions",
    "on established scales with acceptable reliability values",
    "across every measurement occasion in the present study.",
    "Missing data were handled with full information methods",
]
RIGHT = [
    "We estimated a random intercept cross-lagged panel model",
    "to separate stable differences between persons from the",
    "dynamic within-person effects that the theory predicts and",
    "compared it with a traditional cross-lagged panel model",
    "using likelihood ratio tests and information criteria in",
    "line with recent recommendations for panel data analysis.",
]


def _two_column(indent: int = 0, rows: list[str] = ()) -> str:
    lines = [" " * indent + f"{a:<58}    {b}" for a, b in zip(LEFT, RIGHT, strict=True)]
    return "\n".join(["", *lines, "", *rows, "", *lines])


def _reading() -> str:
    return "\n".join(LEFT + RIGHT + LEFT + RIGHT)


TABLE = [
    "Variable            Model 1        Model 2        Model 3",
    "Age                 0.012*         0.011*         0.010",
    "                    (0.004)        (0.004)        (0.005)",
    "Tenure              0.204**        0.199**        0.180**",
    "                    (0.051)        (0.050)        (0.049)",
    "Gender              0.031          0.029          0.027",
]


def test_a_two_column_page_reads_in_order():
    page = _two_column()
    assert pc.gutter(page) is not None
    assert pc.merge_page(page, _reading()) == _reading()


def test_a_single_column_page_keeps_layout():
    page = "\n".join(LEFT + RIGHT + LEFT)
    assert pc.gutter(page) is None
    assert pc.merge_page(page, "anything else") == page


def test_a_table_page_keeps_layout():
    page = "\n".join(["Table 2", "Regression results", "", *TABLE, "", *TABLE])
    assert pc.gutter(page) is None
    assert pc.merge_page(page, "flattened") == page


def test_a_word_table_is_not_taken_for_two_columns():
    # Cells holding words: each row has a cell-sized gap inside a side.
    rows = [f"{'Founding team size':<20} {a[:34]:<36} {b[:40]}" for a, b in zip(LEFT, RIGHT, strict=True)]
    page = "\n".join(rows * 2)
    assert pc.gutter(page) is None


def test_a_margin_watermark_does_not_hide_the_columns():
    # pdftotext puts a rotated margin notice at the far left of the page.
    page = _two_column(indent=110).replace(
        "\n\n", "\n   This document is copyrighted by the publisher.\n", 1)
    assert pc.gutter(page) is not None


def test_a_two_column_page_with_a_table_gets_reading_order_and_the_aligned_table():
    page = _two_column(rows=TABLE)
    out = pc.merge_page(page, _reading())
    assert out.startswith(_reading())
    body = out.split(pc.TABLE_START, 1)[1].split(pc.TABLE_END, 1)[0]
    assert all(row in body for row in TABLE)            # verbatim, aligned


def test_pages_are_merged_one_for_one_and_a_count_mismatch_keeps_layout():
    single = "\n".join(LEFT + RIGHT + LEFT)
    layout = "\f".join([single, _two_column()])
    reading = "\f".join(["single, reading order", _reading()])
    assert pc.merge(layout, reading) == "\f".join([single, _reading()])
    assert pc.merge(layout, "only one page") == layout
