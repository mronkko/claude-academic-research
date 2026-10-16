# /// script
# requires-python = ">=3.11"
# dependencies = [
#     # Not used here directly: the optional validator below runs under this
#     # interpreter (sys.executable), and it imports both.
#     "lxml>=5.0",
#     "defusedxml>=0.7",
# ]
# ///
"""Re-zip an unpacked .docx and run safety checks.

Usage: uv run check_docx.py UNPACKED_DIR OUT.docx --original ORIG.docx --author "Surname, Given"
           [--preview preview.md] [--validator PATH/validate.py]

Checks:
1. Zips UNPACKED_DIR into OUT.docx ([Content_Types].xml first, symlinks skipped).
2. Field balance: counts of fldChar begin/separate/end must be equal.
3. If Anthropic's docx skill is installed, runs its validate.py with
   --original and --author (verifies every change is tracked under that author).
   Otherwise prints a warning and skips: the docx skill is a soft dependency,
   and its validator is under a different licence, so it is discovered, never
   vendored. --validator or $DOCX_VALIDATE_PY overrides discovery.
4. Optional: writes an accepted-changes Markdown preview with pandoc.

Exit code 1 if field balance fails, or the validator reports a problem the
original does not have: a new schema/relationship error, or a change not
tracked under --author. Originals often fail validate.py on their own (e.g. an
unreferenced "[trash]/0000.dat" part, which Word ignores), so when the edited
file fails, the original is validated alone and its error lines become a
baseline; only lines outside it count. A validator that exits non-zero without
printing any error line (a crash) counts as a failure.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path


def zip_dir(src, out):
    if os.path.exists(out):
        os.remove(out)
    with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
        ct = os.path.join(src, '[Content_Types].xml')
        z.write(ct, '[Content_Types].xml')
        for root, _, files in os.walk(src):
            for f in files:
                full = os.path.join(root, f)
                rel = os.path.relpath(full, src).replace(os.sep, '/')
                if rel != '[Content_Types].xml' and not os.path.islink(full):
                    z.write(full, rel)


def find_validator(home=None):
    """Newest docx-skill validate.py under ~/.claude, ignoring staging copies.

    The skill sync keeps half-installed copies under `.staging/`; picking one
    of those would validate against an outdated schema set.
    """
    base = Path(home) if home else Path.home()
    hits = [p for p in (base / '.claude').glob('**/docx/scripts/office/validate.py')
            if '.staging' not in p.parts]
    return max(hits, key=lambda p: p.stat().st_mtime) if hits else None


def field_counts(xml):
    return [xml.count(f'w:fldCharType="{k}"') for k in ('begin', 'separate', 'end')]


def duplicate_citation_ids(xml):
    """citationIDs used by more than one Zotero field.

    Zotero tells citations apart by citationID; copies with the same ID
    (e.g. a paragraph duplicated by hand) confuse its refresh. Reported as a
    warning, because the original may already contain duplicates.
    """
    ids = []
    for m in re.finditer(r'"citationID"\s*:\s*("(?:[^"\\]|\\.)*")', xml):
        ids.append(json.loads(m.group(1)))
    return sorted({i for i in ids if ids.count(i) > 1})


#: Validator output lines that are status, not errors.
_NOT_ERRORS = ('PASSED', 'All validations PASSED', 'Paragraphs:', 'Note:')


def error_lines(stdout):
    """The validator's error lines; it prints nothing for passing checks unless verbose."""
    return {ln.strip() for ln in stdout.splitlines()
            if ln.strip() and not ln.strip().startswith(_NOT_ERRORS)}


def run_validator(validator, out, original, author):
    """(ok, report) for the edited file, judged against the original's own errors."""
    r = subprocess.run([sys.executable, str(validator), out, '--original', original,
                        '--author', author], capture_output=True, text=True)
    report = [r.stdout[-4000:], r.stderr[-2000:]]
    if r.returncode == 0:
        return True, report
    errors = error_lines(r.stdout)
    if not errors:
        report.append(f'validator exited {r.returncode} without reporting an error (crashed?)')
        return False, report
    b = subprocess.run([sys.executable, str(validator), original],
                       capture_output=True, text=True)
    baseline = error_lines(b.stdout)
    new = sorted(errors - baseline)
    ignored = sorted(errors & baseline)
    if ignored:
        report.append('ignored (also reported for the original):\n  ' + '\n  '.join(ignored))
    if new:
        report.append('NEW relative to the original:\n  ' + '\n  '.join(new))
    return not new, report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('unpacked')
    ap.add_argument('out')
    ap.add_argument('--original', required=True)
    ap.add_argument('--author', required=True)
    ap.add_argument('--preview')
    ap.add_argument('--validator', default=os.environ.get('DOCX_VALIDATE_PY'))
    a = ap.parse_args()
    zip_dir(a.unpacked, a.out)
    xml = Path(a.unpacked, 'word', 'document.xml').read_text(encoding='utf8')
    counts = field_counts(xml)
    ok = len(set(counts)) == 1
    print('fields begin/separate/end:', *counts, '-> OK' if ok else '-> UNBALANCED')
    dups = duplicate_citation_ids(xml)
    if dups:
        print('WARNING: citationID shared by several Zotero fields:', ', '.join(dups),
              '(compare with the original; new duplicates confuse Zotero refresh)')
    v = a.validator or find_validator()
    if v:
        print('validator:', v)
        valid, report = run_validator(v, a.out, a.original, a.author)
        print('\n'.join(x for x in report if x.strip()))
        ok = ok and valid
    else:
        print('WARNING: docx skill validator not found; tracked-change validation skipped.')
    if a.preview:
        if shutil.which('pandoc') is None:
            print('WARNING: pandoc not found; preview not written.')
        else:
            subprocess.run(['pandoc', '--track-changes=accept', a.out, '-t', 'gfm',
                            '--wrap=none', '-o', a.preview], check=False)
            print('preview written:', a.preview,
                  '(pandoc artefacts: empty bullets/headings are deleted paragraphs, and a'
                  ' paragraph whose runs mix text and field characters may be missing even'
                  ' if untouched; check Word or the XML before "fixing")')
    sys.exit(0 if ok else 1)


if __name__ == '__main__':
    main()
