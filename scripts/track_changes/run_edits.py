# /// script
# requires-python = ">=3.11"
# dependencies = ["lxml>=5.0"]
# ///
"""Run an edit script with `redline` and `zotero_fields` importable.

Usage: uv run run_edits.py EDITS.py

EDITS.py is a plain Python file the agent writes in its work directory:

    from redline import Doc
    from zotero_fields import Citer
    d = Doc('unpacked/word/document.xml', author='Surname, Given')
    ...
    d.save()

This wrapper exists so the edit script needs no PEP 723 block and no
sys.path manipulation pointing into the plugin directory: it puts this
directory on sys.path and executes EDITS.py as __main__.
"""
import runpy
import sys
from pathlib import Path


def main():
    if len(sys.argv) != 2:
        sys.exit('usage: run_edits.py EDITS.py')
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    script = sys.argv[1]
    sys.argv = [script]
    runpy.run_path(script, run_name='__main__')


if __name__ == '__main__':
    main()
