#!/usr/bin/env python3
"""
Helpers for SLR scripts that call LLMs and process academic PDFs.

Three utilities, all hard-won from running an LLM-based full-text screening
pass on a 224-paper systematic review:

  1. extract_json_from_response()
     Anthropic models (and others) sometimes precede the requested JSON
     object with prose analysis ("Looking at this paper, I need to..."),
     even when the system prompt insists "JSON only". Strict json.loads()
     fails. This function walks the response for the first balanced {...}
     block and parses it. Recovered ~17% of paper-coding rows that
     otherwise errored on a real run.

  2. extract_pdf_text()
     Tries pdfplumber and pypdf, picks the cleaner output via a
     word-length + whitespace-fraction quality heuristic. Catches the
     common pdfplumber CID-encoding double-letter bug ("JJoouurrnnaall")
     and the related missing-spaces bug ("Thisempiricalstudy") that
     leaves the text technically extracted but useless to an LLM.

  3. extract_year_from_date()
     Zotero `date` fields are freeform: "2016", "2016-04", "04/2016",
     "April 2016", "2014-2018". This pulls out the first 4-digit year.

Import from your project script with:
    import os, sys
    sys.path.insert(0, os.path.expanduser("~/.claude/scripts"))
    from llm_helpers import (
        extract_json_from_response,
        extract_pdf_text,
        extract_year_from_date,
    )
"""

from __future__ import annotations

import json
import re
import sys

# ── 1. Lenient JSON extraction from LLM responses ────────────────────────────

def extract_json_from_response(text: str) -> dict | None:
    """Parse a JSON object from arbitrary LLM response text.

    Strategy:
      a) Strip markdown fences and try the whole string.
      b) Walk the string for the first balanced {...} block (handling
         escaped quotes inside strings) and try parsing each candidate.

    Returns the parsed dict on success, None otherwise.

    Example responses this handles:
      '{"decision": "include", ...}'                           # already strict
      '```json\\n{"decision": "include", ...}\\n```'          # markdown fenced
      'Looking at this paper, ... \\n{"decision": "include"}'  # prose-then-JSON
    """
    if not text:
        return None
    stripped = re.sub(r"^```(?:json)?\s*", "", text.strip())
    stripped = re.sub(r"\s*```\s*$", "", stripped)
    try:
        obj = json.loads(stripped)
        if isinstance(obj, dict):
            return obj
    except json.JSONDecodeError:
        pass

    # Walk for the first balanced top-level {...} block.
    depth = 0
    start = -1
    in_str = False
    esc = False
    for i, ch in enumerate(stripped):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start >= 0:
                candidate = stripped[start:i + 1]
                try:
                    obj = json.loads(candidate)
                    if isinstance(obj, dict):
                        return obj
                except json.JSONDecodeError:
                    start = -1
                    continue
    return None


# ── 2. PDF text extraction with quality heuristic ────────────────────────────

def _extraction_quality(text: str) -> float:
    """Composite score (higher = cleaner prose). Accounts for:

      - mean word length: English prose ~4-5 chars; broken extraction
        ('Thisempiricalstudy...') gives 20+
      - whitespace fraction: English ~15-20%; missing-space PDFs ~5%

    Score ranges roughly [0.0, 1.0] — a clean academic PDF scores ~0.95+,
    a CID-encoded mess scores ~0.4.
    """
    if not text or len(text) < 200:
        return 0.0
    sample = text[:20_000]
    words = sample.split()
    if not words:
        return 0.0
    mean_word_len = sum(len(w) for w in words) / len(words)
    ws_frac = sum(1 for c in sample if c.isspace()) / len(sample)
    len_score = max(0.0, 1.0 - max(0.0, mean_word_len - 6) / 10.0)
    ws_score = min(1.0, ws_frac / 0.15)
    return 0.5 * len_score + 0.5 * ws_score


def extract_pdf_text(pdf_path: str, *, verbose: bool = False) -> str:
    """Extract text from a PDF, picking the cleaner of pdfplumber/pypdf.

    pdfplumber typically handles layout better but occasionally garbles
    output when a PDF uses CID fonts ('JJoouurrnnaall' bug) or strips
    spaces. pypdf is more conservative. We run both and pick whichever
    has the better quality score.

    Returns the extracted text (possibly empty if neither extractor works).
    """
    plumber_text = ""
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    parts.append(t)
        plumber_text = "\n".join(parts)
    except Exception as e:
        if verbose:
            print(f"    pdfplumber error: {e}", file=sys.stderr, flush=True)

    pypdf_text = ""
    try:
        from pypdf import PdfReader
        reader = PdfReader(pdf_path)
        pypdf_text = "\n".join(p.extract_text() or "" for p in reader.pages)
    except Exception as e:
        if verbose:
            print(f"    pypdf error: {e}", file=sys.stderr, flush=True)

    p_score = _extraction_quality(plumber_text)
    y_score = _extraction_quality(pypdf_text)
    pypdf_ok = len(pypdf_text) > 1000

    # Prefer pypdf when its quality is meaningfully higher.
    if plumber_text and pypdf_ok and y_score - p_score > 0.10:
        if verbose:
            print(f"    Using pypdf for {pdf_path} "
                  f"(quality {y_score:.2f} vs pdfplumber {p_score:.2f})",
                  flush=True)
        return pypdf_text
    return plumber_text or pypdf_text


def is_parseable_pdf(pdf_path: str) -> bool:
    """True if extract_pdf_text() yields >=200 non-whitespace chars.

    Use as a stronger cache-validity check than %PDF magic bytes alone:
    some downloaders save partial/corrupted PDFs that pass the magic check
    but fail to parse.
    """
    try:
        text = extract_pdf_text(pdf_path)
        return len(text.strip()) >= 200
    except Exception:
        return False


# ── 3. Year extraction from freeform Zotero date ─────────────────────────────

def extract_year_from_date(date_field: str) -> str:
    """Pull a 4-digit year from a Zotero date string.

    Zotero date format is freeform: '2016', '2016-04-12', '04/2016',
    'April 2016', '2014-2018'. Always returns either a 4-digit year or
    'n.d.'.
    """
    if not date_field:
        return "n.d."
    m = re.search(r"\b(19|20)\d{2}\b", date_field)
    return m.group(0) if m else "n.d."


# ── Self-test ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Quick smoke test
    cases = [
        ('{"decision": "include"}', "include"),
        ('```json\n{"decision": "exclude"}\n```', "exclude"),
        ('Looking at this paper... \n{"decision": "include", "x": 1}', "include"),
        ('No JSON here', None),
    ]
    for raw, expected in cases:
        got = extract_json_from_response(raw)
        got_dec = got.get("decision") if got else None
        ok = "✓" if got_dec == expected else "✗"
        print(f"  {ok}  extract_json_from_response({raw[:40]!r}) -> {got_dec!r}")

    for raw, expected in [("2016", "2016"), ("2016-04-12", "2016"),
                          ("04/2016", "2016"), ("April 2016", "2016"),
                          ("", "n.d."), ("forthcoming", "n.d.")]:
        got = extract_year_from_date(raw)
        ok = "✓" if got == expected else "✗"
        print(f"  {ok}  extract_year_from_date({raw!r}) -> {got!r}")
