"""Plan PDF retrieval: classify the queue by the route most likely to work.

`enrich_pdfs.py --plan-strategy` is the planning step of the
`retrieving-pdfs` skill (plan `pdf-retrieval.md` §1, "Plan with the
user, then execute"). It never fetches a PDF and never writes to Zotero;
it reads the library, the run and failure logs, the audit's key files and
the caches, and says — per item without a PDF — which retrieval mode is
the likely way in. The skill shows those counts to the user, agrees an
order of modes with them, and writes it down as the strategy
(`pdf_strategy.py`).

This module is the pure half: given one `ItemFacts` per item it decides
the routes, aggregates, formats the summary and writes the JSON the skill
reads. `enrich_pdfs` gathers the facts (it owns the Zotero client, the
resolver and the handler registry). Nothing here does I/O except
`write_outputs`, and nothing here imports the fetcher registry.

**Two routes per item.** `route` is where the item goes first under the
default order (API cascade → paid → browser / Connector → manual);
`fallback` is where it goes if the API sources (free and paid) fail it.
`fallback` is what the user decides the rest of the order with: "the API
will likely get 300; of everything it might miss, 120 are behind two
Cloudflare publishers and 80 have no automated route at all".

**Consent** (plan §4). Items on a browser-handler, EBSCOhost or
Connector route carry the site their download would come from and the
user's stored answer for it (`ItemFacts.consent_site` / `consent`, read
from `core/publisher_consent` — never asked here). The summary lists them
per site (`consent`, `consent_counts`) so the skill can ask consent along
with the counts; a site answered `deny` sends its items' `fallback` to
`manual` (`automated_route` keeps the route they would otherwise take).

**Three lookup depths** (`LOOKUPS`):

- `none` — caches only: the Crossref DOI cache, the link-resolver cache,
  the logs. No network beyond the Zotero read. Fast, so it is the MCP
  short tool (`pdf_plan`).
- `openalex` — adds one batched OpenAlex *metadata* query per 50 items,
  which answers two questions exactly: whether the paid Content API has
  the PDF (`has_content.pdf`) and whether a free open-access copy within
  the policy exists. **It cannot cost anything**: the request carries no
  API key, so nothing can be billed to the user's account, and no PDF is
  downloaded. Without it, paid reachability is reported as unknown with a
  cost ceiling, never guessed.
- `all` — adds a Crossref lookup for every DOI whose landing host is not
  cached, and the link-resolver coverage queries (~2 s per uncached item,
  priced before it starts). The answers land in the same caches the
  browser pass reads, so the real run later starts warm.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path

#: Where the plan is written, relative to the project directory.
PLAN_DIR = Path(".claude") / "pdf"
PLAN_JSON = PLAN_DIR / "plan.json"
#: `<stem>.<route>.keys`, one per non-empty route, for `--filter-keys-file`.
KEYS_STEM = PLAN_DIR / "plan"

PLAN_SCHEMA_VERSION = 1

LOOKUPS: tuple[str, ...] = ("none", "openalex", "all")

#: The paid OpenAlex Content API's price per PDF (USD).
PAID_COST_USD = 0.01
#: Resolver coverage queries are serial at roughly this pace.
RESOLVER_SECONDS_PER_ITEM = 2.0
#: Crossref lookups for DOIs whose landing host is not cached.
CROSSREF_SECONDS_PER_ITEM = 0.5
#: OpenAlex `filter=doi:a|b|…` accepts up to this many values per call.
OPENALEX_BATCH = 50

# Routes, in the default strategy order. `route` uses all of them;
# `fallback` never the first three.
API = "api"                    # an API source is likely to answer
API_UNTRIED = "api_untried"    # the free cascade has never asked about it
PAID = "paid_openalex"         # OpenAlex holds the publisher PDF ($0.01)
BROWSER = "browser"            # a publisher browser handler
EBSCO = "ebsco"                # library resolver → EBSCOhost (no solve)
CONNECTOR = "connector"        # library resolver / doi.org → Zotero Connector
OTHER_LIBRARY = "other_library"  # routes only at a library not active now
MANUAL = "manual"              # no automated route left: the user downloads
NO_DOI = "no_doi"              # cannot be fetched or opened until it has a DOI

ROUTES: tuple[str, ...] = (
    API, API_UNTRIED, PAID, BROWSER, EBSCO, CONNECTOR, OTHER_LIBRARY, MANUAL, NO_DOI,
)
FALLBACK_ROUTES: tuple[str, ...] = (BROWSER, EBSCO, CONNECTOR, OTHER_LIBRARY, MANUAL, NO_DOI)

ROUTE_LABELS: dict[str, str] = {
    API: "API cascade, likely (publisher API or a free copy within policy)",
    API_UNTRIED: "API cascade, never tried on these",
    PAID: "paid OpenAlex Content API",
    BROWSER: "publisher browser handler",
    EBSCO: "library route to EBSCOhost (browser pass, no challenge)",
    CONNECTOR: "library route / doi.org via the Zotero Connector",
    OTHER_LIBRARY: "routes only at another library (its network needed)",
    MANUAL: "no automated route left — manual download",
    NO_DOI: "no DOI — find one first (enrich_dois)",
}

#: Strategy mode each route is executed by (see `pdf_strategy.MODES`).
ROUTE_MODE: dict[str, str] = {
    API: "api", API_UNTRIED: "api", PAID: "paid", BROWSER: "browser",
    EBSCO: "browser", CONNECTOR: "connector", OTHER_LIBRARY: "browser",
    MANUAL: "manual", NO_DOI: "",
}

# Resolver answers for one item (`ItemFacts.resolver`).
R_EBSCO = "ebsco"
R_ROUTE = "route"
R_NONE = "none"
R_DEFERRED = "deferred"
R_UNKNOWN = "unknown"
R_UNCONFIGURED = "unconfigured"

#: `classify_direct_route` verdicts that send an item to its handler.
_HANDLER_CASES = ("", "3-in-coverage", "1a-unknown")


@dataclass
class ItemFacts:
    """What is known about one in-scope item without a PDF.

    `None` on a tri-state field means "not looked up" — never "no".
    """

    key: str
    doi: str = ""
    title: str = ""
    #: A free or paid API source has been asked about it (run / failure log).
    api_tried: bool = False
    #: Publisher API source (`sciencedirect`, `springer`, `wiley`) whose
    #: prefix or landing host matches and whose credential is configured.
    publisher_api: str = ""
    #: Same match, but the credential is missing — a setup hint.
    publisher_api_missing_key: str = ""
    #: OpenAlex lists a free PDF the policy would accept.
    open_access: bool | None = None
    #: OpenAlex's Content API holds the PDF (`has_content.pdf`).
    paid: bool | None = None
    #: Browser handler covering the DOI (after `[library] no_access`).
    handler: str = ""
    handler_display: str = ""
    handler_needs_solve: bool = False
    #: `classify_direct_route` verdict, "" when the resolver was not asked.
    direct_case: str = ""
    #: One of the `R_*` values.
    resolver: str = R_UNKNOWN
    #: The browser pass (handler, EBSCO or Connector) has answered for it.
    browser_tried: bool = False
    #: Latest failure-log cause (`pdf_fetch_log.latest_per_item`).
    last_cause: str = ""
    #: Audit key files listing it (`retry.manual`, `true_negative`, …).
    audit_sets: tuple[str, ...] = ()
    #: A copy below `[pdf] min_version` was found and set aside.
    below_policy: bool = False
    #: The site an automated download would come from on the item's
    #: browser / EBSCO / Connector route (`fetchers.browser.consent`),
    #: "" when it has no such route or the site is not known.
    consent_site: str = ""
    #: That site's stored answer — `allow`, `deny` or `ask` (also for
    #: no entry at all); "" without a site.
    consent: str = ""


#: Routes that download automatically and so need consent (modes 1, 2).
CONSENT_ROUTES: tuple[str, ...] = (BROWSER, EBSCO, CONNECTOR)


def automated_route(f: ItemFacts) -> str:
    """`fallback_route` before consent: the automated route the item
    would take if every site allowed it. The planner uses it to say which
    site is asked about; `fallback_route` is what actually happens."""
    if not f.doi:
        return NO_DOI
    if f.browser_tried:
        return MANUAL
    if f.handler and f.direct_case in _HANDLER_CASES:
        return BROWSER
    if f.direct_case == "4-other-library" or f.resolver == R_DEFERRED:
        return OTHER_LIBRARY
    if f.resolver == R_EBSCO:
        return EBSCO
    if f.resolver == R_NONE:
        return MANUAL
    # A route, an unanswered question, or no resolver at all: the
    # Connector pass fails open to doi.org, so it still gets a try.
    return CONNECTOR


def fallback_route(f: ItemFacts) -> str:
    """Where the item goes if every API source fails it. A site the user
    denied automated download for sends the item to manual download —
    the browser pass would skip it (CONSENT_DENIED). `ask` keeps the
    route: the planning conversation is where that question is put."""
    route = automated_route(f)
    if route in CONSENT_ROUTES and f.consent == "deny":
        return MANUAL
    return route


def route_for(f: ItemFacts) -> str:
    """The first route likely to attach a PDF, in the default order."""
    if not f.doi:
        return NO_DOI
    if not f.api_tried:
        return API if (f.publisher_api or f.open_access) else API_UNTRIED
    if f.paid:
        return PAID
    return fallback_route(f)


# ---------------------------------------------------------------------------
# OpenAlex metadata (lookups = openalex | all)
# ---------------------------------------------------------------------------

def openalex_batches(dois: Iterable[str], size: int = OPENALEX_BATCH) -> list[list[str]]:
    """DOIs in `filter=doi:` batches. A DOI holding `|` or `,` would split
    the filter, so it is left out (and stays unknown)."""
    clean = sorted({d for d in dois if d and "|" not in d and "," not in d})
    return [clean[i:i + size] for i in range(0, len(clean), size)]


def openalex_params(batch: list[str], mailto: str = "") -> dict:
    """Query parameters for one batch. **No `api_key`**: a keyless
    metadata query cannot be billed to anyone."""
    params = {
        "filter": "doi:" + "|".join(batch),
        "select": "doi,has_content,locations",
        "per-page": str(len(batch)),
    }
    if mailto:
        params["mailto"] = mailto
    return params


def oa_and_paid(work: dict, acceptable: Callable[[dict, str], bool]) -> tuple[bool, bool]:
    """(free copy within policy, Content API has the PDF) for one work.

    `acceptable(location, pdf_url)` is the policy gate, applied to the
    version `fetchers.pdf_version` would settle for that location.
    """
    paid = bool((work.get("has_content") or {}).get("pdf", False))
    oa = False
    for loc in work.get("locations") or []:
        url = (loc or {}).get("pdf_url") or ""
        if url and (loc or {}).get("is_oa", True) and acceptable(loc, url):
            oa = True
            break
    return oa, paid


# ---------------------------------------------------------------------------
# Aggregation
# ---------------------------------------------------------------------------

@dataclass
class Plan:
    library: str
    lookups: str
    policy: dict
    scope_items: int
    with_pdf: int
    facts: list[ItemFacts] = field(default_factory=list)
    #: Paid-reachability source: "openalex" (exact) or "" (unknown).
    paid_known: bool = False
    #: Counts of what was *not* asked, for the "unknown" section.
    unknown: dict = field(default_factory=dict)
    credentials: dict = field(default_factory=dict)
    resolver_configured: bool = False
    notes: list[str] = field(default_factory=list)
    #: site -> {"display", "terms_url", "automated", "confirmed"} for every
    #: `ItemFacts.consent_site` in the queue (gathered by enrich_pdfs).
    consent_sites: dict = field(default_factory=dict)


def _count(values: Iterable[str], order: tuple[str, ...]) -> dict[str, int]:
    out = {k: 0 for k in order}
    for v in values:
        out[v] = out.get(v, 0) + 1
    return {k: n for k, n in out.items() if n}


def summarise(plan: Plan) -> dict:
    """The machine-readable plan (`plan.json`), minus per-item rows."""
    facts = plan.facts
    routes = {f.key: route_for(f) for f in facts}
    fallbacks = {f.key: fallback_route(f) for f in facts}
    with_doi = [f for f in facts if f.doi]

    automated = {f.key: automated_route(f) for f in facts}
    publishers: dict[str, dict] = {}
    for f in facts:
        # By the route before consent, so a denied publisher is still
        # listed — with its answer — rather than vanishing into `manual`.
        if automated[f.key] == BROWSER:
            site = plan.consent_sites.get(f.consent_site or f.handler, {})
            entry = publishers.setdefault(f.handler, {
                "display": f.handler_display or f.handler,
                "items": 0, "needs_solve": f.handler_needs_solve,
                "consent": f.consent or "ask",
                "confirmed": site.get("confirmed", ""),
                "terms_url": site.get("terms_url", ""),
            })
            entry["items"] += 1

    consent: dict[str, dict] = {}
    for f in facts:
        route = automated[f.key]
        if route not in CONSENT_ROUTES or not f.consent_site:
            continue
        site = plan.consent_sites.get(f.consent_site, {})
        entry = consent.setdefault(f.consent_site, {
            "display": site.get("display", f.consent_site),
            "automated": f.consent or "ask",
            "confirmed": site.get("confirmed", ""),
            "terms_url": site.get("terms_url", ""),
            "modes": [], "items": 0,
        })
        entry["items"] += 1
        mode = ROUTE_MODE[route] if route != EBSCO else "browser (EBSCOhost)"
        if mode not in entry["modes"]:
            entry["modes"].append(mode)

    paid_items = sum(1 for f in with_doi if f.paid)
    paid_after_api = sum(1 for f in facts if routes[f.key] == PAID)
    unchecked = [f for f in with_doi if f.paid is None]
    paid: dict = {
        "known": plan.paid_known and not unchecked,
        "policy": plan.policy.get("paid", ""),
        "items": paid_items,
        "cost_usd": round(paid_items * PAID_COST_USD, 2),
        "items_api_already_tried": paid_after_api,
    }
    if unchecked:
        # Unknown is said as unknown: a ceiling, not an estimate dressed
        # as a count.
        paid["unchecked"] = len(unchecked)
        paid["cost_ceiling_usd"] = round(
            (paid_items + len(unchecked)) * PAID_COST_USD, 2)
        paid["note"] = (
            "Not looked up — re-plan with lookups=openalex for the exact "
            "count (a keyless metadata query: free). The ceiling assumes "
            "OpenAlex holds every unchecked item, which it does not."
        )

    audit: dict[str, int] = {}
    for f in facts:
        for label in f.audit_sets:
            audit[label] = audit.get(label, 0) + 1

    oa_known = [f for f in with_doi if f.open_access is not None]
    return {
        "version": PLAN_SCHEMA_VERSION,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "library": plan.library,
        "lookups": plan.lookups,
        "policy": plan.policy,
        "scope": {
            "items": plan.scope_items,
            "with_pdf": plan.with_pdf,
            "queue": len(facts),
            "with_doi": len(with_doi),
            "no_doi": len(facts) - len(with_doi),
        },
        "routes": _count(routes.values(), ROUTES),
        "fallback_routes": _count(fallbacks.values(), FALLBACK_ROUTES),
        "browser_publishers": dict(sorted(
            publishers.items(), key=lambda kv: (-kv[1]["items"], kv[0]))),
        # Consent for automated download, per site on a browser / EBSCO /
        # Connector route (modes 1, 2): what the conversation asks with
        # the counts. `automated` is allow | deny | ask (ask = no answer).
        "consent": dict(sorted(
            consent.items(), key=lambda kv: (-kv[1]["items"], kv[0]))),
        "consent_counts": {
            "ask": sum(e["items"] for e in consent.values() if e["automated"] == "ask"),
            "deny": sum(e["items"] for e in consent.values() if e["automated"] == "deny"),
            "allow": sum(e["items"] for e in consent.values()
                         if e["automated"] == "allow"),
            "sites_to_ask": sum(1 for e in consent.values() if e["automated"] == "ask"),
        },
        "signals": {
            "api_tried": sum(f.api_tried for f in facts),
            "publisher_api": sum(bool(f.publisher_api) for f in facts),
            "publisher_api_missing_key": _count(
                (f.publisher_api_missing_key for f in facts
                 if f.publisher_api_missing_key), ()),
            "open_access": (sum(bool(f.open_access) for f in oa_known)
                            if oa_known else None),
            "browser_tried": sum(f.browser_tried for f in facts),
            "no_licensed_route": sum(f.resolver == R_NONE for f in facts),
        },
        "paid_openalex": paid,
        "below_policy": sum(f.below_policy for f in facts),
        "audit": dict(sorted(audit.items())),
        # Step 3's definition: in scope, no PDF, has a DOI.
        "manual_queue": len(with_doi),
        "unknown": plan.unknown,
        "credentials": plan.credentials,
        "resolver_configured": plan.resolver_configured,
        "notes": plan.notes,
        "keys_files": {},
    }


def keys_by_route(facts: Iterable[ItemFacts]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for f in facts:
        out.setdefault(route_for(f), []).append(f.key)
    return {r: sorted(keys) for r, keys in out.items()}


def write_outputs(plan: Plan, project: Path = Path(".")) -> tuple[dict, Path]:
    """Write `plan.json` (with per-item rows) and one keys file per route.

    Stale keys files from an earlier plan are removed, so a route that
    emptied does not keep pointing at last week's items.
    """
    summary = summarise(plan)
    out_dir = project / PLAN_DIR
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = project / KEYS_STEM
    for old in out_dir.glob(stem.name + ".*.keys"):
        old.unlink(missing_ok=True)
    for route, keys in keys_by_route(plan.facts).items():
        path = Path(f"{stem}.{route}.keys")
        path.write_text("\n".join(keys) + "\n", encoding="utf-8")
        summary["keys_files"][route] = Path(f"{KEYS_STEM}.{route}.keys").as_posix()
    full = {**summary, "items": [
        {"key": f.key, "doi": f.doi, "route": route_for(f),
         "fallback": fallback_route(f),
         **({"automated_route": automated_route(f)}
            if automated_route(f) != fallback_route(f) else {}),
         **{k: v for k, v in asdict(f).items()
            if k not in ("key", "doi", "title") and v not in ("", None, (), False)}}
        for f in plan.facts
    ]}
    path = project / PLAN_JSON
    path.write_text(json.dumps(full, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    return summary, path


# ---------------------------------------------------------------------------
# The human summary
# ---------------------------------------------------------------------------

def _consent_word(entry: dict) -> str:
    answer = entry.get("consent") or entry.get("automated") or "ask"
    if answer == "allow":
        return "allowed" + (f" {entry['confirmed']}" if entry.get("confirmed") else "")
    if answer == "deny":
        return "denied → manual"
    return "not asked yet"


def _n(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def format_summary(summary: dict) -> str:
    """The plan for a person. The last line is the fixed, parseable
    `PLAN: key=value …` line the MCP tool summarises."""
    s = summary["scope"]
    pol = summary["policy"]
    lines = [
        f"PDF retrieval plan — {summary['library']} "
        f"(lookups: {summary['lookups']})",
        f"  {s['items']} items in scope · {s['with_pdf']} have a PDF · "
        f"{s['queue']} without",
        f"  policy: min_version={pol.get('min_version')} "
        f"({pol.get('sources', {}).get('min_version', 'default')}) · "
        f"reconstructed={str(pol.get('reconstructed')).lower()} · "
        f"paid={pol.get('paid')} ({pol.get('sources', {}).get('paid', 'default')})",
        "",
        "First likely route (default order: API → paid → browser → Connector → manual):",
    ]
    for route, n in summary["routes"].items():
        lines.append(f"  {n:>6}  {ROUTE_LABELS[route]}")
    lines += ["", "If the API sources fail them, where each item would go next:"]
    for route, n in summary["fallback_routes"].items():
        lines.append(f"  {n:>6}  {ROUTE_LABELS[route]}")
    pubs = summary["browser_publishers"]
    if pubs:
        lines.append("")
        lines.append("  Browser publishers (items · needs a Cloudflare / SSO solve):")
        for p in pubs.values():
            lines.append(f"    {p['display']}: {p['items']}"
                         + ("  [solve]" if p["needs_solve"] else "")
                         + f"  [consent: {_consent_word(p)}]")
    consent = summary.get("consent") or {}
    if consent:
        lines.append("")
        lines.append("Consent for automated download (browser handlers and the "
                     "Connector; per site):")
        for site, c in consent.items():
            terms = f" — terms: {c['terms_url']}" if c.get("terms_url") else ""
            lines.append(f"  {c['display']} [{site}]: {_n(c['items'], 'item')} via "
                         f"{', '.join(c['modes'])} · {_consent_word(c)}{terms}")
        counts = summary.get("consent_counts") or {}
        if counts.get("sites_to_ask"):
            lines.append(
                f"  {counts['sites_to_ask']} site(s) have no answer: a browser or "
                f"Connector run that cannot ask skips their {counts['ask']} item(s) "
                f"as CONSENT_NEEDED. Ask the user now and record it "
                f"(pdf_consent_set). Denied sites go to manual download.")
    paid = summary["paid_openalex"]
    lines.append("")
    if paid.get("unchecked"):
        lines.append(
            f"Paid OpenAlex: not looked up for {paid['unchecked']} item(s); "
            f"at most ${paid['cost_ceiling_usd']:.2f} at $0.01/PDF (a ceiling, "
            f"not an estimate). Re-plan with lookups=openalex for the count — "
            f"free: a keyless metadata query.")
    else:
        lines.append(
            f"Paid OpenAlex: holds {_n(paid['items'], 'item')} of the queue "
            f"(${paid['cost_usd']:.2f} at $0.01/PDF); "
            f"{paid['items_api_already_tried']} of them after the free API "
            f"sources already failed. Policy paid={paid['policy']}.")
    sig = summary["signals"]
    missing = sig.get("publisher_api_missing_key") or {}
    if missing:
        lines.append("  Publisher API without a credential: " + ", ".join(
            f"{k} {v}" for k, v in missing.items())
            + " — configure it with /setup.")
    if summary["below_policy"]:
        lines.append(f"Below policy: {summary['below_policy']} item(s) have only a "
                     f"copy below min_version (set aside, not attached).")
    if summary["audit"]:
        lines.append("Audit flags on queue items: " + ", ".join(
            f"{k} {v}" for k, v in summary["audit"].items()))
    unknown = summary.get("unknown") or {}
    if any(unknown.values()):
        lines.append("Not looked up (re-plan with a deeper lookups= to settle): "
                     + ", ".join(f"{k} {v}" for k, v in unknown.items() if v))
    for note in summary.get("notes") or []:
        lines.append(f"Note: {note}")
    lines.append(f"Manual queue (in scope, no PDF, has a DOI): {summary['manual_queue']}")
    fields = {
        "queue": s["queue"], "with_pdf": s["with_pdf"], "no_doi": s["no_doi"],
        **{f"route_{k}": v for k, v in summary["routes"].items()},
        **{f"fallback_{k}": v for k, v in summary["fallback_routes"].items()},
        "paid_items": paid["items"],
        "paid_known": int(bool(paid["known"])),
        "below_policy": summary["below_policy"],
        "consent_ask": (summary.get("consent_counts") or {}).get("ask", 0),
        "consent_deny": (summary.get("consent_counts") or {}).get("deny", 0),
        "lookups": summary["lookups"],
    }
    lines.append("PLAN: " + " ".join(f"{k}={v}" for k, v in fields.items()))
    return "\n".join(lines)


def estimate_seconds(*, resolver_uncached: int, crossref_uncached: int,
                     openalex_batches: int = 0) -> float:
    """Wall-clock estimate for the lookups a `lookups=all` plan makes."""
    return (resolver_uncached * RESOLVER_SECONDS_PER_ITEM
            + crossref_uncached * CROSSREF_SECONDS_PER_ITEM
            + openalex_batches * 1.0)
