"""Consent records for automated PDF download from a publisher's site.

    [publisher_consent.sage]
    automated = "allow"      # allow | deny | ask (the default)
    confirmed = 2026-10-15   # the day the user gave the answer

Settled with the user on 2026-10-15 (`~/.claude/plans/pdf-retrieval.md`,
§4 "Consent"). **What it covers:** the two modes in which the plugin
itself drives a website to download — mode 1, a browser handler
(`fetchers/browser/<publisher>.py`, EBSCOhost included), and mode 2, the
Zotero Connector saving from the page the library's link resolver opens.
**What it does not:** publisher APIs (Elsevier, Wiley TDM, Crossref TDM)
and open-access sources are channels the publisher offers for exactly
this, so they never ask; manual download (mode 3) never asks either,
because there the user is the one clicking download.

**User level, not project level.** Whether an institution's licence
permits automated download is a fact about the user's library, not about
one review, so the record lives in `~/.config/academic-research/
config.toml` beside the other access settings (`[library] no_access`,
`direct_access`).

**One entry per site the download comes from** — the key is the site,
not the journal or the item. A browser handler's key is its name
(`sage`, `tandf`, `ebsco` — one entry for all of EBSCOhost). A Connector
save is keyed by the platform it saves from, worked out from the resolver
route (`fetchers/browser/consent.py`, `site_for_target`): the same keys
where the platform is known (`jstor`, `proquest`, `sage`, …), else the
host's registrable domain (`"degruyter.com"`). A Sage article reached
through the Connector and through the Sage handler is therefore one
question, which is what "per site" means.

**A missing answer never means yes.** `resolve` returns `NEEDED` — skip
the site and log `CONSENT_NEEDED` — whenever the record is anything but
`allow` and nobody can be asked. An unreadable value (`automated =
"alow"`) reads as `ask`, never as `allow`; unlike `[pdf]` (`pdf_policy`)
it is not an error that stops the run, because the safe reading is
available and costs nothing but a skipped site.

Stdlib only and in `scripts/core/` so the setup wizard (stdlib-only by
rule) can list and change the stored answers with the same validation.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass

SECTION = "publisher_consent"

ALLOW = "allow"
DENY = "deny"
ASK = "ask"
ANSWERS: tuple[str, ...] = (ALLOW, DENY, ASK)
DEFAULT = ASK

#: `resolve` verdicts.
PROCEED = "proceed"          # recorded allow: download
DENIED = "denied"            # recorded deny: fall through to manual download
ASK_USER = "ask_user"        # no usable answer, but someone can be asked now
NEEDED = "needed"            # no usable answer, nobody to ask: skip, CONSENT_NEEDED
VERDICTS: tuple[str, ...] = (PROCEED, DENIED, ASK_USER, NEEDED)

#: A site key: lower-case, a TOML-quotable host or slug. Dots are allowed
#: (`"degruyter.com"`); the config writer quotes such keys.
_SITE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,62}[a-z0-9])?$")


class ConsentError(ValueError):
    """A site key or an answer this module cannot store."""


def normalise_site(site: str) -> str:
    """`site` as a stored key, or `ConsentError`."""
    key = str(site or "").strip().lower()
    if not _SITE_RE.fullmatch(key):
        raise ConsentError(
            f"{site!r} is not a site key (lower-case letters, digits, '.', '-')",
        )
    return key


def normalise_answer(answer: str) -> str:
    value = str(answer or "").strip().lower()
    if value not in ANSWERS:
        raise ConsentError(f"{answer!r} is not one of {', '.join(ANSWERS)}")
    return value


@dataclass(frozen=True)
class Consent:
    site: str
    automated: str = DEFAULT
    confirmed: _dt.date | None = None
    #: An entry exists in config.toml (an `ask` may be stored explicitly).
    stored: bool = False
    #: The raw `automated` value when it was unreadable (read as `ask`).
    invalid: str = ""

    def as_dict(self) -> dict:
        return {
            "site": self.site,
            "automated": self.automated,
            "confirmed": self.confirmed.isoformat() if self.confirmed else "",
            "stored": self.stored,
            **({"invalid": self.invalid} if self.invalid else {}),
        }


def _as_date(value: object) -> _dt.date | None:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    if isinstance(value, str) and value.strip():
        try:
            return _dt.date.fromisoformat(value.strip()[:10])
        except ValueError:
            return None
    return None


def from_table(site: str, table: object) -> Consent:
    """One stored entry read safely: anything unreadable is `ask`."""
    if not isinstance(table, dict):
        return Consent(site=site, stored=True, invalid=repr(table))
    raw = table.get("automated", DEFAULT)
    try:
        automated, invalid = normalise_answer(raw), ""
    except ConsentError:
        automated, invalid = ASK, str(raw)
    return Consent(site=site, automated=automated,
                   confirmed=_as_date(table.get("confirmed")),
                   stored=True, invalid=invalid)


def _config(cfg: dict | None) -> dict:
    if cfg is not None:
        return cfg
    from core.config_loader import load_config
    return load_config()


def read_all(cfg: dict | None = None) -> dict[str, Consent]:
    """Every stored entry, by site. `cfg` defaults to config.toml."""
    section = _config(cfg).get(SECTION, {}) or {}
    if not isinstance(section, dict):
        return {}
    out: dict[str, Consent] = {}
    for site, table in section.items():
        try:
            key = normalise_site(site)
        except ConsentError:
            continue
        out[key] = from_table(key, table)
    return out


def lookup(site: str, cfg: dict | None = None) -> Consent:
    """The record for `site`; an unstored site is `ask`."""
    key = normalise_site(site)
    return read_all(cfg).get(key, Consent(site=key))


def resolve(consent: Consent, *, can_ask: bool) -> str:
    """What a run does about this site (one of `VERDICTS`).

    Only a recorded `allow` proceeds without a question. `ask`, a missing
    entry or an unreadable one becomes a question when the user can be
    asked now, and `NEEDED` otherwise — never `PROCEED`.
    """
    if consent.automated == ALLOW:
        return PROCEED
    if consent.automated == DENY:
        return DENIED
    return ASK_USER if can_ask else NEEDED


def record(site: str, automated: str, *, today: _dt.date | None = None) -> Consent:
    """Store the user's answer for `site`, dated, keeping the rest of the
    file (`config_writer.set_table`). Only ever called with an answer the
    user gave: from the run's own prompt, `/setup`, or `pdf_consent.py
    set` relaying what they said in the conversation."""
    from core import config_writer

    key = normalise_site(site)
    value = normalise_answer(automated)
    when = today or _dt.date.today()
    config_writer.set_table((SECTION, key), {"automated": value, "confirmed": when})
    return Consent(site=key, automated=value, confirmed=when, stored=True)
