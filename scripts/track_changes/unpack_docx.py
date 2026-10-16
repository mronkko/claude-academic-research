# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""Snapshot a .docx and unpack it for editing.

Usage: uv run unpack_docx.py MANUSCRIPT.docx WORKDIR

Copies MANUSCRIPT.docx to WORKDIR/00_original.docx (the baseline that
check_docx.py --original compares against) and extracts it to
WORKDIR/unpacked/. Symlink entries and paths escaping the target are
skipped. Stdlib-only, so it replaces `cp` + `unzip`, which are not
available on every platform.
"""
import argparse
import shutil
import stat
import sys
import zipfile
from pathlib import Path


def unpack(src, workdir):
    workdir = Path(workdir)
    out = workdir / 'unpacked'
    if out.exists():
        sys.exit(f'{out} already exists; use a fresh WORKDIR so edits start from the snapshot')
    workdir.mkdir(parents=True, exist_ok=True)
    snapshot = workdir / '00_original.docx'
    shutil.copy2(src, snapshot)
    root = out.resolve()
    with zipfile.ZipFile(snapshot) as z:
        for info in z.infolist():
            if stat.S_ISLNK(info.external_attr >> 16):
                continue
            target = (out / info.filename).resolve()
            if not target.is_relative_to(root):
                continue
            z.extract(info, out)
    return snapshot, out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('docx')
    ap.add_argument('workdir')
    a = ap.parse_args()
    snapshot, out = unpack(a.docx, a.workdir)
    print('snapshot:', snapshot)
    print('unpacked:', out)
    print('document:', out / 'word' / 'document.xml')


if __name__ == '__main__':
    main()
