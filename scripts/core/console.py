"""Make a script's own console output survive a Windows pipe.

Python picks stdout's encoding from the locale when stdout is not a
terminal. On Windows that is usually cp1252, which cannot encode most of
what this plugin prints: em dashes, arrows, the `──` section rules, the
`•` bullets in run reports. Printing any of them raises
`UnicodeEncodeError` and takes the whole script down mid-run.

It only bites when output is redirected — to a file, through a pipe, or
into a subprocess — because an attached Windows console is handled
separately and does support UTF-8. So it never showed up in ordinary use
or in the test suite until a test captured a script's stdout, and then it
surfaced as a crash inside a search that had already succeeded.

`errors="replace"` rather than a hard failure: a character that some
exotic target encoding still cannot represent should cost one glyph in a
progress line, never the run that was printing it.

Stdlib only, and safe to call more than once — `scripts/setup/` imports
it too, and that tree may not take a third-party dependency.
"""

from __future__ import annotations

import sys


def enable_utf8_output() -> None:
    """Reconfigure stdout/stderr to UTF-8 where the runtime allows it.

    A no-op on a stream that does not support reconfiguration (one
    already replaced by a test harness, for instance), and on platforms
    where the locale encoding was never a problem.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # Detached or already-closed stream; printing is the caller's
            # problem from here, but it must not be this call's.
            pass
