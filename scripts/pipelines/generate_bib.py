#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
generate_bib.py — Scan thesis Markdown chapters for citation keys;
                   fetch each entry from Zotero via Better BibTeX JSON-RPC;
                   write references.bib.

Searches both the personal library and all group libraries.

Requirements:
    Zotero must be running with the Better BibTeX plugin installed.
    No extra Python packages required (uses only the standard library).

Usage:
    python3 generate_bib.py <thesis_dir>

    <thesis_dir> can be an absolute path or a path relative to the current
    working directory. The directory must contain a 'chapters/' subdirectory.

    Examples:
        python3 generate_bib.py thesis_AQ1_profit_warnings
        python3 generate_bib.py /Users/me/projects/my_thesis
"""

import json
import re
import sys
import urllib.error as urlerr
import urllib.request as urlreq
from pathlib import Path

BBT_URL = "http://localhost:23119/better-bibtex/json-rpc"
TRANSLATOR = "Better BibLaTeX"

# Match [@Key], [@Key1; @Key2], [@Key, p. 5], etc.
CITEKEY_RE = re.compile(r'@([\w][\w:.#$%&\-+?<>~/]*)')

# Match the opening line of a BibTeX entry to extract the cite key
ENTRY_KEY_RE = re.compile(r'^@\w+\{([^,]+),', re.MULTILINE)


# ---------------------------------------------------------------------------
# BBT JSON-RPC helpers
# ---------------------------------------------------------------------------

def bbt_call(method: str, params: dict) -> dict:
    """Make a Better BibTeX JSON-RPC call. Returns the parsed response body."""
    payload = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
        "id": 1,
    }).encode("utf-8")

    req = urlreq.Request(
        BBT_URL,
        data=payload,
        headers={"Content-Type": "application/json"},
    )

    try:
        with urlreq.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urlerr.URLError as exc:
        print(
            f"ERROR: Could not reach Zotero at {BBT_URL}\n"
            f"       Make sure Zotero is running with Better BibTeX installed.\n"
            f"       Details: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)


def extract_bibtex(body: dict) -> str:
    """Extract the BibTeX string from a BBT JSON-RPC response."""
    result = body.get("result", "")
    if isinstance(result, dict):
        return result.get("data", "")
    return result if isinstance(result, str) else ""


def export_from_library(citekeys: list[str], library_id: int) -> tuple[str, list[str]]:
    """
    Try to export citekeys from a specific Zotero library.
    Returns (bibtex_string, list_of_missing_keys).
    """
    body = bbt_call("item.export", {
        "citekeys": citekeys,
        "translator": TRANSLATOR,
        "libraryID": library_id,
    })

    if "error" in body:
        msg = body["error"].get("message", str(body["error"]))
        # BBT reports missing keys as: "not found: key1, key2"
        if msg.startswith("not found:"):
            missing_str = msg[len("not found:"):].strip()
            missing = [k.strip() for k in missing_str.split(",")]
            found_keys = [k for k in citekeys if k not in missing]
            if not found_keys:
                return "", missing
            # Re-export only the found keys
            body2 = bbt_call("item.export", {
                "citekeys": found_keys,
                "translator": TRANSLATOR,
                "libraryID": library_id,
            })
            if "error" in body2:
                return "", missing
            return extract_bibtex(body2), missing
        else:
            # Unrecognised error; treat all keys as missing
            return "", citekeys

    return extract_bibtex(body), []


def get_group_library_ids() -> list[int]:
    """Return Zotero group library IDs via the BBT user.groups method."""
    body = bbt_call("user.groups", {})
    if "error" in body or "result" not in body:
        return []
    groups = body["result"]
    if isinstance(groups, list):
        return [g["id"] for g in groups if isinstance(g, dict) and "id" in g]
    return []


# ---------------------------------------------------------------------------
# BibTeX capitalization post-processing
# ---------------------------------------------------------------------------

def _is_all_caps(value: str) -> bool:
    """True if value has ≥2 letters and every letter is uppercase."""
    letters = re.sub(r'[^a-zA-Z]', '', value)
    return len(letters) >= 2 and letters == letters.upper()


def _titlecase_name(value: str) -> str:
    """Title-case each word-run in value (handles 'P. JOAKIM' → 'P. Joakim')."""
    return re.sub(r'[A-Za-z]+', lambda m: m.group(0).capitalize(), value)


def _fix_author_names(field_content: str) -> str:
    """Fix ALL-CAPS tokens in a biblatex author/editor field string.

    Handles two formats:
    1. BBT extended: family=Surname, given=Firstname, given-i=F
    2. Raw BibTeX: SURNAME, FIRSTNAME M.  (no family=/given= prefixes)
    """
    def fix_one_author(author_str: str) -> str:
        # First try the BBT extended format (family=..., given=...)
        has_extended = re.search(r'(?:family|given|given-i)\s*=', author_str)
        if has_extended:
            def replacer(m):
                prefix, value = m.group(1), m.group(2)
                return prefix + (_titlecase_name(value) if _is_all_caps(value) else value)
            return re.sub(
                r'((?:family|given|given-i)\s*=\s*)([^,{}\n]+)',
                replacer,
                author_str,
            )
        # Raw BibTeX format: fix each word-run if the whole name part is ALL CAPS
        # e.g. "FAMA, EUGENE F." → "Fama, Eugene F."
        if _is_all_caps(author_str):
            return _titlecase_name(author_str)
        return author_str

    # Split on ' and ' (BibLaTeX name-list separator) so each author is processed
    # independently — prevents the greedy group from spanning the separator.
    parts = re.split(r'\s+and\s+', field_content)
    return ' and '.join(fix_one_author(p) for p in parts)


def _process_author_fields(bib_text: str) -> str:
    def fix_field(m):
        return m.group(1) + _fix_author_names(m.group(2)) + m.group(3)
    return re.sub(
        r'(\s*(?:author|editor)\s*=\s*\{)([^}]*)(\})',
        fix_field,
        bib_text,
        flags=re.IGNORECASE,
    )


def _to_sentence_case(content: str) -> str:
    """Convert unbraced text to sentence case; content inside {…} is left verbatim.
    LaTeX commands (\\word) are preserved as-is."""
    result, depth, cap_next, i = [], 0, True, 0
    while i < len(content):
        c = content[i]
        if c == '{':
            depth += 1
            result.append(c)
            i += 1
            continue
        if c == '}':
            depth = max(depth - 1, 0)
            result.append(c)
            i += 1
            continue
        if depth > 0:
            result.append(c)
            i += 1
            continue
        # depth == 0: check for LaTeX command (\word)
        if c == '\\' and i + 1 < len(content) and content[i + 1].isalpha():
            # Copy the entire command verbatim
            result.append(c)
            i += 1
            while i < len(content) and content[i].isalpha():
                result.append(content[i])
                i += 1
            continue
        # Normal character at depth 0
        if c.isalpha():
            result.append(c.upper() if cap_next else c.lower())
            cap_next = False
        else:
            result.append(c)
            if c in (':', '\u2013', '\u2014'):   # colon, en-dash, em-dash
                cap_next = True
        i += 1
    return ''.join(result)


def _strip_bbt_double_braces(title: str) -> str:
    """Strip BBT's protective double braces {{word}} → word in title text.

    BBT wraps every significant word in {{...}} to preserve its original
    capitalisation.  We strip these so that _to_sentence_case can apply
    uniform sentence case.  Single braces {WORD} are kept — they indicate
    intentional case protection (acronyms, proper nouns).

    Short all-caps tokens (≤3 letters like EU, CSR, GRI) are re-wrapped
    in single braces so sentence case preserves them.
    """
    def replacer(m):
        inner = m.group(1)
        # Keep short acronyms protected with single braces
        letters = re.sub(r'[^a-zA-Z]', '', inner)
        if letters and len(letters) <= 3 and letters == letters.upper():
            return '{' + inner + '}'
        return inner

    return re.sub(r'\{\{([^{}]*?)\}\}', replacer, title)


def _process_title_fields(bib_text: str) -> str:
    """Apply sentence case to title fields (BBT puts each field on one line)."""
    def fix_line(line: str) -> str:
        m = re.match(r'^(\s*title\s*=\s*\{)(.*?)(\},?\s*)$', line)
        if m:
            stripped = _strip_bbt_double_braces(m.group(2))
            return m.group(1) + _to_sentence_case(stripped) + m.group(3)
        return line
    return '\n'.join(fix_line(ln) for ln in bib_text.split('\n'))


def _fix_biblatex_commands(bib_text: str) -> str:
    """Replace BibLaTeX-specific commands with standard LaTeX equivalents."""
    # \Mkbibemph{...} and \mkbibemph{...} → \emph{...}
    bib_text = re.sub(r'\\[Mm]kbibemph\{', r'\\emph{', bib_text)
    # \mkbibquote{...} → ``...'' (or just leave as text)
    bib_text = re.sub(r'\\mkbibquote\{([^}]*)\}', r"``\1''", bib_text)
    return bib_text


def fix_bib_capitalization(bib_text: str) -> str:
    """Fix ALL-CAPS author names, title-case titles, and BibLaTeX commands."""
    bib_text = _fix_biblatex_commands(bib_text)
    bib_text = _process_author_fields(bib_text)
    bib_text = _process_title_fields(bib_text)
    return bib_text


# ---------------------------------------------------------------------------
# Step 1: extract citation keys from Markdown chapter files
# ---------------------------------------------------------------------------

def extract_keys(chapters_dir: Path) -> list[str]:
    """Find all @Key tokens in markdown files under chapters_dir."""
    keys: set[str] = set()
    for md in sorted(list(chapters_dir.glob('*.md')) + list(chapters_dir.glob('*.qmd'))):
        text = md.read_text(encoding='utf-8')
        for m in CITEKEY_RE.finditer(text):
            key = m.group(1)
            if key[0].isalpha() and not key.startswith(('fig-', 'tbl-', 'sec-', 'eq-', 'lst-')):
                keys.add(key)
    return sorted(keys)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import argparse
    parser = argparse.ArgumentParser(
        description="Generate references.bib from citation keys found in Markdown / Quarto sources.")
    parser.add_argument("project_dir",
                        help="Project root directory. Scans {project_dir}/chapters/ by default for @citekeys.")
    parser.add_argument("--chapters-dir", default=None,
                        help="Override source directory to scan (relative to project_dir, "
                             "or absolute). Default: chapters/")
    parser.add_argument("--bib-out", default=None,
                        help="Override output path for references.bib. Default: {project_dir}/references.bib")
    args = parser.parse_args()

    thesis_arg = Path(args.project_dir)
    thesis_dir = thesis_arg if thesis_arg.is_absolute() else Path.cwd() / thesis_arg
    thesis_name = thesis_dir.name

    if args.chapters_dir:
        ca = Path(args.chapters_dir)
        chapters_dir = ca if ca.is_absolute() else thesis_dir / ca
    else:
        chapters_dir = thesis_dir / 'chapters'

    if args.bib_out:
        ba = Path(args.bib_out)
        bib_path = ba if ba.is_absolute() else thesis_dir / ba
    else:
        bib_path = thesis_dir / 'references.bib'

    if not chapters_dir.is_dir():
        print(f"ERROR: source directory not found: {chapters_dir}", flush=True)
        sys.exit(1)

    # --- Extract keys ---
    all_keys = extract_keys(chapters_dir)
    if not all_keys:
        print("No citation keys found in Markdown files.", flush=True)
        sys.exit(0)
    print(f"Citation keys found in Markdown ({len(all_keys)}):", flush=True)
    for k in all_keys:
        print(f"  @{k}", flush=True)
    print(flush=True)

    # --- Personal library (ID 1) ---
    print(f"Fetching from personal library (translator: '{TRANSLATOR}') ...", flush=True)
    bibtex_parts: list[str] = []
    remaining = list(all_keys)

    bibtex, missing = export_from_library(remaining, library_id=1)
    if bibtex:
        bibtex_parts.append(bibtex)
    remaining = missing

    # --- Group libraries ---
    if remaining:
        print(f"  {len(remaining)} key(s) not in personal library; checking group libraries ...", flush=True)
        group_ids = get_group_library_ids()
        if not group_ids:
            print("  (No group libraries found.)", flush=True)
        for gid in group_ids:
            if not remaining:
                break
            print(f"  Checking group library {gid} for {len(remaining)} remaining key(s) ...", flush=True)
            bibtex, still_missing = export_from_library(remaining, library_id=gid)
            if bibtex:
                bibtex_parts.append(bibtex)
            remaining = still_missing

    # --- Summary ---
    found_count = len(all_keys) - len(remaining)
    print(flush=True)
    for k in all_keys:
        if k not in remaining:
            print(f"  ✅  {k}", flush=True)
        else:
            print(f"  ❌  {k}  — not found in Zotero", flush=True)

    if not bibtex_parts:
        print("\nERROR: No entries were exported. references.bib not modified.",
              file=sys.stderr, flush=True)
        sys.exit(1)

    # --- Write references.bib ---
    header = (
        "% Auto-generated — do not edit by hand.\n"
        f"% Source: Zotero / Better BibTeX  ({thesis_name})\n"
        "% Regenerate: python3 generate_bib.py " + thesis_name + "\n\n"
    )
    combined = fix_bib_capitalization("\n".join(bibtex_parts))
    bib_path.write_text(header + combined.strip() + "\n", encoding="utf-8")

    print(f"\nWrote {found_count} entr{'y' if found_count == 1 else 'ies'} → {bib_path}",
          flush=True)

    if remaining:
        print(f"\nWARNING — {len(remaining)} key(s) not resolved:", flush=True)
        for k in remaining:
            print(f"  {k}", flush=True)
        print("Add these papers to Zotero before compiling the thesis.", flush=True)
        sys.exit(2)   # non-zero so scripts can detect an incomplete bibliography


if __name__ == '__main__':
    main()
