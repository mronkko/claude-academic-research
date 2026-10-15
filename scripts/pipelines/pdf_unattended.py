"""The long unattended browser run — modes 1–2 of the PDF retrieval plan
(`pdf-retrieval.md` §4, build step 6): the pure half.

`enrich_pdfs.py --unattended setup|run` drives it; this module holds what
needs neither a browser nor Zotero, so it can be tested on its own:

* **State** — `.claude/pdf/unattended.json`, written by the setup phase
  and read by the run: which publisher / platform sessions were solved,
  which Connector hosts the user confirmed, and the last runs' summaries.
* **Deferral reasons** — what the run sets aside for a person instead of
  blocking. Every deferral is one failure-log row with cause
  `DEFERRED_NEEDS_USER` and one run-log row `deferred_needs_user` whose
  `detail` starts with the reason key below.
* **The re-check interval** — how often a long publisher lane re-probes
  that its Cloudflare clearance / SSO / EZproxy session still holds.
* **The summary** — attached / deferred by reason / failed, split by the
  failure log's `network` column.

**No session lifetimes were measured.** The plan's step-6 measurement
(how long Cloudflare clearance and EZproxy / SSO sessions survive in the
Playwright profile) was skipped by the user's decision on 2026-10-15, so
the defaults here are conservative guesses, not findings: the session is
re-checked before every publisher and every `DEFAULT_RECHECK_MINUTES`
within one, an expired one defers that publisher's remaining items, and
requests are spaced `PACE_FACTOR` times wider than an attended run. One
thing *is* known (connector.py, 2026-09-23): an EZproxy login lives in a
browser-session cookie and does not survive a relaunch of the Chromium
profile — so proxied routes solved in the setup phase usually find a
login page in the run, and are deferred.

Stdlib only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

STATE_PATH = Path(".claude") / "pdf" / "unattended.json"
SCHEMA_VERSION = 1

#: Minutes between session re-checks inside one publisher lane. A guess
#: (see the module docstring): short enough that an expired clearance
#: costs a few items' worth of time, long enough that the probe — one
#: page load — stays a small fraction of the run.
DEFAULT_RECHECK_MINUTES = 15.0

#: Consecutive unexplained failures (no page verdict, no transport error,
#: the session probe still fine) after which a publisher's remaining items
#: are deferred. An attended run asks after the first; unattended, one
#: paywalled item must not cost the publisher, but a run of them is the
#: question a person should answer.
FAILURE_LIMIT = 3

#: Unattended pacing: each handler's `delay_s` times this. Nobody is
#: waiting, and a rate limit tripped at 3 a.m. costs the rest of the run.
PACE_FACTOR = 2.0

PHASES = ("setup", "run")

#: Run-log status of a deferral.
DEFERRED_STATUS = "deferred_needs_user"

CHALLENGE = "challenge"
LOGIN = "login"
REPEATED_FAILURES = "repeated_failures"
HOST_UNCONFIRMED = "host_unconfirmed"
ZOTERO_UNAVAILABLE = "zotero_unavailable"
ZOTERO_WRONG_LIBRARY = "zotero_wrong_library"

REASONS: dict[str, str] = {
    CHALLENGE: "a Cloudflare challenge is showing — the clearance solved in "
               "setup has expired",
    LOGIN: "a sign-in / SSO / EZproxy login page — the session solved in "
           "setup has expired (an EZproxy login never survives a browser "
           "relaunch)",
    REPEATED_FAILURES: f"{FAILURE_LIMIT} unexplained download failures in a "
                       f"row — an attended run would have asked whether to "
                       f"keep trying",
    HOST_UNCONFIRMED: "Connector host not confirmed in the setup phase",
    ZOTERO_UNAVAILABLE: "Zotero Desktop is not running (the Connector saves "
                        "through it)",
    ZOTERO_WRONG_LIBRARY: "Zotero Desktop has another library selected",
}

#: Setup outcomes per publisher / platform (`state["sites"]`).
SOLVED = "solved"                 # the user cleared it, or it was clear
NO_SOLVE_NEEDED = "no_solve_needed"
SKIPPED = "skipped"               # "no, I cannot reach the PDF here"
ALWAYS_SKIP = "always_skip"       # … and persisted to [library] no_access
CONSENT_NEEDED = "consent_needed"
CONSENT_DENIED = "consent_denied"
UNAVAILABLE = "unavailable"       # could not be set up (no extension, …)

#: Connector host outcomes (`state["connector_hosts"]`).
CONFIRMED = "confirmed"
HOST_SKIPPED = "skipped"

#: Last runs kept in the state file.
KEEP_RUNS = 5


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def detail(reason: str) -> str:
    """The run-log `detail` of a deferral: `<reason>: <explanation>`."""
    return f"{reason}: {REASONS.get(reason, reason)}"


def reason_of(detail_text: str) -> str:
    """The reason key back out of a deferral's `detail`."""
    head = (detail_text or "").split(":", 1)[0].strip()
    return head or "unknown"


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------

def new_state(library: str = "") -> dict:
    return {"version": SCHEMA_VERSION, "library": library, "setup": {},
            "sites": {}, "connector_hosts": {}, "runs": []}


def load_state(path: Path = STATE_PATH) -> dict:
    """The state, or a fresh one when there is none (or it is unreadable —
    a run without a setup record still runs, it just defers more)."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return new_state()
    if not isinstance(data, dict) or data.get("version") != SCHEMA_VERSION:
        return new_state()
    for key, empty in (("setup", {}), ("sites", {}), ("connector_hosts", {}),
                       ("runs", [])):
        if not isinstance(data.get(key), type(empty)):
            data[key] = empty
    return data


def save_state(state: dict, path: Path = STATE_PATH) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def record_site(state: dict, site: str, result: str, *, kind: str = "handler",
                items: int = 0) -> None:
    state.setdefault("sites", {})[site] = {
        "result": result, "kind": kind, "items": items, "at": now(),
    }


def record_host(state: dict, host: str, result: str, *, items: int = 0) -> None:
    state.setdefault("connector_hosts", {})[host] = {
        "result": result, "items": items, "at": now(),
    }


def site_result(state: dict, site: str) -> str:
    entry = (state.get("sites") or {}).get(site) or {}
    return str(entry.get("result", ""))


def confirmed_hosts(state: dict) -> set[str]:
    return {h for h, e in (state.get("connector_hosts") or {}).items()
            if isinstance(e, dict) and e.get("result") == CONFIRMED}


def setup_done(state: dict) -> bool:
    return bool((state.get("setup") or {}).get("finished"))


def add_run(state: dict, summary: dict) -> None:
    runs = list(state.get("runs") or [])
    runs.append(summary)
    state["runs"] = runs[-KEEP_RUNS:]


# ---------------------------------------------------------------------------
# The re-check interval
# ---------------------------------------------------------------------------

def recheck_seconds(flag_minutes: float | None, config_value: object) -> tuple[float, str]:
    """(seconds, where it came from). Flag > `[pdf] unattended_recheck_minutes`
    > default. 0 turns the periodic re-check off (the check before each
    publisher and after an unexplained failure stays). A value that is not
    a non-negative number is refused for the default, and said so."""
    if flag_minutes is not None:
        if flag_minutes < 0:
            raise ValueError("--recheck-minutes must be 0 or more")
        return float(flag_minutes) * 60.0, "flag"
    raw = str(config_value).strip() if config_value not in (None, "") else ""
    if raw:
        try:
            minutes = float(raw)
        except ValueError:
            minutes = -1.0
        if minutes >= 0:
            return minutes * 60.0, "config"
        return DEFAULT_RECHECK_MINUTES * 60.0, f"default (config value {raw!r} ignored)"
    return DEFAULT_RECHECK_MINUTES * 60.0, "default"


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------

ATTACHED_PREFIX = "attached"


def summarise(rows: list[dict], failures: list[dict], *, network: str,
              since: str) -> dict:
    """What one unattended run did, from the run-log rows it wrote and the
    failure-log rows (`pdf_fetch_log.read_failures`) written since `since`
    for those items.

    Failures are split by the failure log's `network` column: a 403 on
    the network the run was on is a finding about access *from there*, and
    one written on another network (or before the column existed) is not
    this run's evidence. Transport failures (NETWORK_ERROR) are their own
    line: they say nothing about access at all."""
    keys = {(r.get("item_key") or "").strip() for r in rows} - {""}
    attached: set[str] = set()
    deferred: dict[str, str] = {}
    consent: dict[str, set[str]] = {"needed": set(), "denied": set()}
    for r in rows:
        key = (r.get("item_key") or "").strip()
        status = r.get("status") or ""
        if not key:
            continue
        if status.startswith(ATTACHED_PREFIX):
            attached.add(key)
        elif status == DEFERRED_STATUS:
            deferred[key] = reason_of(r.get("detail", ""))
        elif status == "skipped_consent_needed":
            consent["needed"].add(key)
        elif status == "skipped_consent_denied":
            consent["denied"].add(key)
    by_reason: dict[str, int] = {}
    for reason in deferred.values():
        by_reason[reason] = by_reason.get(reason, 0) + 1

    failed: dict[str, dict[str, int]] = {}   # cause -> {network: n}
    failed_keys: set[str] = set()
    skip_causes = {"DEFERRED_NEEDS_USER", "CONSENT_NEEDED", "CONSENT_DENIED"}
    for f in failures:
        key = (f.get("item_key") or "").strip()
        if key not in keys or key in attached or key in deferred or (f.get("timestamp") or "") < since:
            continue
        cause = f.get("cause") or ""
        if cause in skip_causes:
            continue
        net = f.get("network") or ""
        failed.setdefault(cause, {})
        failed[cause][net] = failed[cause].get(net, 0) + 1
        failed_keys.add(key)
    network_errors = sum(failed.get("NETWORK_ERROR", {}).values())
    access_here = sum(n for c, nets in failed.items() if c != "NETWORK_ERROR"
                      for net, n in nets.items() if net == network)
    access_elsewhere = sum(n for c, nets in failed.items() if c != "NETWORK_ERROR"
                           for net, n in nets.items() if net != network)
    return {
        "phase": "run", "finished": now(), "since": since, "network": network,
        "items": len(keys), "attached": len(attached),
        "deferred": len(deferred), "deferred_by_reason": by_reason,
        "deferred_keys": sorted(deferred),
        "consent_needed": len(consent["needed"]),
        "consent_denied": len(consent["denied"]),
        "failed_items": len(failed_keys),
        "failures_by_cause": {c: dict(n) for c, n in sorted(failed.items())},
        "network_errors": network_errors,
        "access_failures_this_network": access_here,
        "access_failures_other_network": access_elsewhere,
    }


def summary_line(summary: dict) -> str:
    """The machine-readable last line: `UNATTENDED: k=v …`."""
    if summary.get("phase") == "setup":
        keys = ("sites", "solved", "skipped", "consent_needed", "consent_denied",
                "hosts_confirmed", "hosts_skipped")
    else:
        keys = ("items", "attached", "deferred", "consent_needed", "consent_denied",
                "failed_items", "network_errors")
    parts = [f"phase={summary.get('phase', 'run')}"]
    parts += [f"{k}={summary.get(k, 0)}" for k in keys]
    return "UNATTENDED: " + " ".join(parts)


def format_summary(summary: dict) -> str:
    """The human summary of a run."""
    lines = [
        "",
        "=" * 60,
        "Unattended run — summary",
        "=" * 60,
        f"  {summary['items']} item(s) went through the browser pass; "
        f"{summary['attached']} attached.",
    ]
    if summary["deferred"]:
        lines.append(f"  {summary['deferred']} deferred for a person "
                     f"(DEFERRED_NEEDS_USER):")
        for reason, n in sorted(summary["deferred_by_reason"].items(),
                                key=lambda kv: (-kv[1], kv[0])):
            lines.append(f"    {n:>4}  {REASONS.get(reason, reason)}")
        lines.append("  They are the next manual-download queue: `enrich_pdfs.py "
                     "--manual next` opens them first (the audit writes "
                     "retry.deferred.keys).")
    if summary["consent_needed"] or summary["consent_denied"]:
        lines.append(f"  {summary['consent_needed']} skipped for want of a consent "
                     f"answer (CONSENT_NEEDED), {summary['consent_denied']} denied "
                     f"(CONSENT_DENIED → manual).")
    if summary["failed_items"]:
        net = summary["network"] or "(no library network recorded)"
        lines.append(f"  {summary['failed_items']} item(s) failed:")
        if summary["network_errors"]:
            lines.append(f"    {summary['network_errors']:>4}  network / transport "
                         f"errors — say nothing about access; re-run")
        if summary["access_failures_this_network"]:
            lines.append(f"    {summary['access_failures_this_network']:>4}  on this "
                         f"run's network ({net}) — access findings from there")
        if summary["access_failures_other_network"]:
            lines.append(f"    {summary['access_failures_other_network']:>4}  recorded "
                         f"under another network — not this run's evidence")
    lines.append("  Session lifetimes were not measured: an expired clearance or "
                 "login defers a publisher's rest rather than blocking.")
    return "\n".join(lines)
