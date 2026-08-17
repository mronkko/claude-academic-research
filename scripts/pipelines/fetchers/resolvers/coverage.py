"""Parse a resolver's per-package coverage statement into a year window.

Why this exists
---------------
A link resolver reporting a platform for a journal does **not** mean it
can serve *this article*. Alma's answer is journal-level: for a 1988
article it happily reports SpringerLink alongside EBSCOhost, and the
coverage strings are what distinguish them:

    Springer  : 'Available from 01.01.1997 volume: 16 issue: 1.<br>'
    EBSCOhost : 'Available from 01.02.1982.<br>Most recent 1 year(s) not available.<br>'

The 1988 article is inside EBSCOhost's holding and outside Springer's.
Without reading these, the pipeline sent a 1988 paper to the Springer
handler, hit the paywall, and burned a 30-second download timeout — three
times in one 97-item run.

It also fixes the opposite error, which platform ranking made *more*
likely rather than less. `PLATFORM_PRIORITY` puts EBSCOhost first, and
EBSCOhost here carries a one-year moving wall. So for a 2026 article the
preferred route is precisely the one that cannot serve it, while
SpringerLink can. Ranking only started biting once Alma target matching
worked at all, so this became a live risk on the same day the matching
was fixed.

Grammar
-------
Sampled from a real Alma tenant across six DOIs and 23 distinct strings.
Segments are `<br>`-separated; one holding may carry several ranges, and
a moving wall applies to the whole holding:

    Available from 01.11.1987 volume: 6 issue: 8.<br>Most recent 1 year(s) not available.<br>
    Available from 01.05.1982 volume: 1 issue: 2 until 31.12.1985 volume: 4 issue: 6.<br>
    Available from 1996 until 2007.<br>Available from 2007.<br>Most recent 1 year(s) not available.<br>
    Available from 1999 volume: 23 issue: 3.<br>

Dates come as `DD.MM.YYYY` or a bare `YYYY`; `until` may carry its own
volume/issue, which is ignored. Only years are used — an article's
publication month is often unknown or wrong in Zotero metadata, and
month precision would turn a soft miss into a hard skip for no gain.

Unparseable is not "not covered"
--------------------------------
`parse_coverage` returns None when it finds no range, and callers must
treat None as *unknown* and proceed. SFX targets carry no coverage
statement at all, so on SFX every answer is None and behaviour is
unchanged. A parser that guessed False here would silently stop
retrieving from any platform whose wording we had not anticipated, which
is a far worse failure than one wasted timeout.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

# `from <date>` and the optional `until <date>`; the date is either
# DD.MM.YYYY or a bare year. Volume/issue may follow either and is
# ignored, so the `until` lookup must not run past the next segment.
_FROM_RE = re.compile(r"\bfrom\s+(\d{1,2}\.\d{1,2}\.(\d{4})|(\d{4}))", re.I)
_UNTIL_RE = re.compile(r"\buntil\s+(\d{1,2}\.\d{1,2}\.(\d{4})|(\d{4}))", re.I)
# "Most recent 1 year(s) not available" — a moving wall on the newest
# content, expressed in whole years.
_EMBARGO_RE = re.compile(
    r"most\s+recent\s+(\d+)\s*year", re.I,
)


def _year(match: re.Match[str]) -> int | None:
    """Year out of a `DD.MM.YYYY`-or-`YYYY` match."""
    return int(match.group(2) or match.group(3))


@dataclass(frozen=True)
class CoverageWindow:
    """What a package holds, in whole years.

    `ranges` is a tuple of `(start_year, end_year_or_None)`; None means
    the holding is open-ended. `embargo_years` is the moving wall, 0 when
    there is none.
    """

    ranges: tuple[tuple[int, int | None], ...]
    embargo_years: int = 0

    def covers_year(
        self, year: int, *, today_year: int | None = None,
    ) -> bool | None:
        """Whether an article published in `year` is inside this holding.

        Tri-state, because a moving wall is measured in months while a
        Zotero `date` field often yields only a year. With a one-year wall
        and today in 2026: 2026 content is certainly inside the wall,
        2024 is certainly outside it, and 2025 straddles it — some months
        released, some not. Returning None for that boundary year is the
        honest answer, and it is also the safe one: callers treat None as
        unknown and attempt the fetch, so an ambiguous case costs one
        wasted try rather than a wrongly skipped article.

        `today_year` is injectable so tests do not drift as the calendar
        moves — an embargo assertion pinned to a literal year would start
        failing on 1 January otherwise.
        """
        current = today_year if today_year is not None else _dt.date.today().year
        if self.embargo_years:
            wall_start = current - self.embargo_years
            if year > wall_start:
                return False
            if year == wall_start:
                return None
        return any(
            start <= year and (end is None or year <= end)
            for start, end in self.ranges
        )


def parse_coverage(text: str) -> CoverageWindow | None:
    """Parse a coverage statement, or None when nothing is parseable.

    None is *unknown*, never "not covered" — see the module docstring.
    """
    if not text or not text.strip():
        return None

    embargo = 0
    if (m := _EMBARGO_RE.search(text)) is not None:
        embargo = int(m.group(1))

    ranges: list[tuple[int, int | None]] = []
    # Split on <br> so a second "Available from" starts a second range
    # rather than being read as the first range's `until`.
    for segment in re.split(r"<br\s*/?>", text, flags=re.I):
        start_m = _FROM_RE.search(segment)
        if start_m is None:
            continue
        start = _year(start_m)
        if start is None:
            continue
        end: int | None = None
        until_m = _UNTIL_RE.search(segment, start_m.end())
        if until_m is not None:
            end = _year(until_m)
        ranges.append((start, end))

    if not ranges:
        return None
    return CoverageWindow(ranges=tuple(ranges), embargo_years=embargo)


def covers_year(
    coverage: str, year: int | str | None, *, today_year: int | None = None,
) -> bool | None:
    """Tri-state: True / False / None (unknown).

    Accepts `year` as an int or a string, because the value threaded
    through the pipeline comes from Zotero's free-text `date` field via
    `_year_from_zotero_date` and arrives as a string or None.
    """
    if year is None:
        return None
    try:
        y = int(str(year).strip()[:4])
    except (ValueError, TypeError):
        return None
    window = parse_coverage(coverage)
    if window is None:
        return None
    return window.covers_year(y, today_year=today_year)


__all__ = ["CoverageWindow", "covers_year", "parse_coverage"]
