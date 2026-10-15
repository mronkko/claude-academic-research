#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Show and record consent for automated PDF download, per site.

    pdf_consent.py show                      # every known site + stored answers
    pdf_consent.py show --site sage --site jstor
    pdf_consent.py set --site sage --automated allow     # the user's answer

The answers live in the user config (`[publisher_consent.<site>]`,
`core/publisher_consent.py`) and govern only modes 1 and 2 — browser
handlers and the Zotero Connector. `set` records an answer **the user
gave**: the `retrieving-pdfs` skill asks in the planning conversation,
with the plan's counts and the terms link (`show`), and relays the reply
through this script (or the MCP tools `pdf_consent_show` /
`pdf_consent_set`, which run it). Never a default, never a guess: a
browser run that finds no answer skips the site (CONSENT_NEEDED).

`show` lists the handlers' sites and the Connector-only platforms, every
stored answer, and — when `.claude/pdf/plan.json` exists — the sites the
current plan depends on with their item counts. The last line is
`CONSENT_JSON: {…}` for the MCP summary.

Stdlib only: `fetchers.browser.consent` and the handler registry import
without Playwright or any third-party package.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
SCRIPTS_ROOT = SCRIPT_DIR.parent
for _p in (SCRIPTS_ROOT, SCRIPT_DIR):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from core import publisher_consent as pc  # noqa: E402
from fetchers.browser import consent  # noqa: E402

PLAN_PATH = Path(".claude") / "pdf" / "plan.json"


def _plan_sites(path: Path = PLAN_PATH) -> dict[str, dict]:
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    sites = plan.get("consent") if isinstance(plan, dict) else None
    return sites if isinstance(sites, dict) else {}


def _row(site: str, stored: dict[str, pc.Consent], plan: dict[str, dict]) -> dict:
    info = consent.site_info(site)
    c = stored.get(site, pc.Consent(site=site))
    row = {
        "site": site, "display": info.display,
        "automated": c.automated, "confirmed": c.as_dict()["confirmed"],
        "stored": c.stored, "terms_url": info.terms_url, "homepage": info.homepage,
    }
    if c.invalid:
        row["invalid"] = c.invalid
    if site in plan:
        row["plan_items"] = int(plan[site].get("items", 0) or 0)
        row["plan_modes"] = list(plan[site].get("modes") or [])
    return row


def _show(args: argparse.Namespace) -> int:
    stored = pc.read_all()
    plan = _plan_sites()
    if args.site:
        try:
            sites = [pc.normalise_site(s) for s in args.site]
        except pc.ConsentError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
    else:
        sites = [s.site for s in consent.known_sites()]
        sites += sorted(set(stored) - set(sites))
        sites += sorted(set(plan) - set(sites))
    rows = [_row(s, stored, plan) for s in sites]
    print("Consent for automated download (browser handlers, Zotero Connector):")
    for r in rows:
        when = f" {r['confirmed']}" if r["confirmed"] else ""
        answer = r["automated"] + when + ("" if r["stored"] else " (not answered)")
        plan_part = (f" · plan: {r['plan_items']} item(s) via "
                     f"{', '.join(r['plan_modes'])}" if "plan_items" in r else "")
        terms = r["terms_url"] or (f"no link recorded — see {r['homepage']}"
                                   if r["homepage"] else "no link recorded")
        print(f"  {r['display']} [{r['site']}]: {answer}{plan_part}\n      terms: {terms}")
    to_ask = [r["site"] for r in rows if r["automated"] == pc.ASK and "plan_items" in r]
    if to_ask:
        print(f"Not answered yet for this plan: {', '.join(to_ask)}.")
    print("CONSENT_JSON: " + json.dumps(
        {"sites": rows, "plan_sites_to_ask": to_ask}, ensure_ascii=False))
    return 0


def _set(args: argparse.Namespace) -> int:
    try:
        got = pc.record(args.site, args.automated)
    except pc.ConsentError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    info = consent.site_info(got.site)
    print(f"Recorded: {info.display} [{got.site}] automated = {got.automated!r} "
          f"(confirmed {got.confirmed.isoformat() if got.confirmed else ''}) in "
          f"config.toml [publisher_consent.{got.site}].")
    if got.automated == pc.DENY:
        print("  Its items go to manual download (enrich_pdfs.py --manual next); "
              "browser runs will skip the site (CONSENT_DENIED).")
    elif got.automated == pc.ASK:
        print("  A terminal browser run will ask; any other run skips the site "
              "(CONSENT_NEEDED).")
    print("CONSENT_JSON: " + json.dumps(
        {"recorded": _row(got.site, pc.read_all(), _plan_sites())}, ensure_ascii=False))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("show", help="List sites and their stored answers.")
    show.add_argument("--site", action="append", default=[],
                      help="Only this site key (repeatable).")
    put = sub.add_parser("set", help="Record the user's answer for one site.")
    put.add_argument("--site", required=True,
                     help="Site key: a handler name (sage, ebsco, …), a platform "
                          "(jstor, proquest) or a host (degruyter.com) — as `show` "
                          "and the plan name it.")
    # A literal, not `pc.ANSWERS`, so the MCP schema-sync guard can read
    # it; `test_pdf_consent_cli` keeps the two equal.
    put.add_argument("--automated", required=True, choices=("allow", "deny", "ask"),
                     help="allow | deny | ask — what the user said.")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    return _show(args) if args.command == "show" else _set(args)


if __name__ == "__main__":
    from core.console import enable_utf8_output
    enable_utf8_output()
    sys.exit(main())
