"""The `[pdf]` retrieval policy: which copies a PDF run may attach, and
where the paid OpenAlex Content API sits.

    [pdf]
    min_version   = "accepted"   # published | accepted | preprint
    reconstructed = false        # generated text-only PDFs (Elsevier XML)
    paid          = "prefer"     # off | prefer | last_resort

Settled with the user on 2026-10-15 (`~/.claude/plans/pdf-retrieval.md`,
"Retrieval policy").

**Where a value comes from**, highest first:

1. **flag** — `enrich_pdfs.py --min-version / --paid`, and
   `--allow-preprints` (= `--min-version preprint` for one run).
2. **project** — `PDF_MIN_VERSION`, `PDF_RECONSTRUCTED`, `PDF_PAID` in the
   project's `screening_config.py`. Read with `ast`, never executed: the
   library-wide `enrich_pdfs` must not need a review's config to load
   (no `TAG_PREFIX` check, no LLM imports), it only needs three literals.
3. **global** — `[pdf]` in `~/.config/academic-research/config.toml`.
4. **legacy** — the keys `[pdf]` supersedes: `[elsevier]
   render_xml_to_pdf` (env `ELSEVIER_RENDER_XML_TO_PDF`) for
   `reconstructed`, and `[openalex] use_paid_content_api` (env
   `OPENALEX_USE_PAID_CONTENT_API`) for `paid`, where false means `off`
   and true means `prefer`. A `[pdf]` value always beats them, env or not.
5. **default** — `accepted`, `false`, `prefer`.

A value that is present but unrecognised is an error, never the default:
`min_version = "publisehd"` silently meaning `accepted` would attach the
manuscripts the user asked to keep out.

**What `min_version` compares.** The version `fetchers.pdf_version`
settled for the file — never a raw API field. `preprint` is the policy
word for the `submitted` version. An item whose own DOI is a preprint
server's (an arXiv-native paper) has no better version than the preprint,
so its floor is always `submitted`: the gate would otherwise refuse the
work itself.

Stdlib only, and in `scripts/core/` rather than `scripts/pipelines/`
so the stdlib-only setup wizard can import it — one list of allowed
values for the question and for the run. No `fetchers` import (that
package imports every source, Playwright included); the version names are the same strings as
`fetchers.pdf_version.VERSIONS`, which `tests/unit/test_pdf_policy.py`
keeps equal.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path

MIN_VERSIONS: tuple[str, ...] = ("published", "accepted", "preprint")
PAID_MODES: tuple[str, ...] = ("off", "prefer", "last_resort")

DEFAULTS: dict[str, object] = {
    "min_version": "accepted",
    "reconstructed": False,
    "paid": "prefer",
}

#: `screening_config.py` constant for each policy field.
PROJECT_CONSTANTS: dict[str, str] = {
    "min_version": "PDF_MIN_VERSION",
    "reconstructed": "PDF_RECONSTRUCTED",
    "paid": "PDF_PAID",
}

#: `pdf_version` names, best first. Same strings as
#: `fetchers.pdf_version.VERSIONS`.
VERSION_RANK: tuple[str, ...] = ("published", "accepted", "submitted")

#: Policy word → the lowest `pdf_version` it admits.
_FLOOR: dict[str, str] = {
    "published": "published",
    "accepted": "accepted",
    "preprint": "submitted",
}

_TRUE = ("1", "true", "yes", "on")
_FALSE = ("0", "false", "no", "off")


class PolicyError(ValueError):
    """A policy value that is present but not one the policy knows."""


def floor_version(min_version: str) -> str:
    """The lowest `pdf_version` version `min_version` admits."""
    try:
        return _FLOOR[min_version]
    except KeyError:
        raise PolicyError(f"unknown min_version {min_version!r}") from None


def allows(min_version: str, version: str, *, item_is_preprint: bool = False) -> bool:
    """Whether a file of `version` may be attached under `min_version`.

    `item_is_preprint` — the item's own DOI is a preprint server's — lowers
    the floor to `submitted`: for such an item the preprint *is* the work.
    An unknown `version` is refused rather than guessed at.
    """
    floor = "submitted" if item_is_preprint else floor_version(min_version)
    if version not in VERSION_RANK:
        return False
    return VERSION_RANK.index(version) <= VERSION_RANK.index(floor)


def coerce(name: str, value: object, where: str = "") -> object:
    """`value` as a valid `name` value; raises `PolicyError` otherwise."""
    where = f"{where}: " if where else ""
    if name == "reconstructed":
        if isinstance(value, bool):
            return value
        text = str(value).strip().lower()
        if text in _TRUE:
            return True
        if text in _FALSE:
            return False
        raise PolicyError(
            f"{where}reconstructed = {value!r} is not true or false",
        )
    text = str(value).strip().lower() if isinstance(value, str) else value
    allowed = MIN_VERSIONS if name == "min_version" else PAID_MODES
    if text not in allowed:
        raise PolicyError(
            f"{where}{name} = {value!r} is not one of {', '.join(allowed)}",
        )
    return text


def _blank(value: object) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


@dataclass(frozen=True)
class PdfPolicy:
    min_version: str = "accepted"
    reconstructed: bool = False
    paid: str = "prefer"
    #: Field → where its value came from (`default`, `global`,
    #: `legacy [section] key`, `project (path)`, `flag (--x)`).
    sources: dict[str, str] = field(default_factory=dict)

    def allows(self, version: str, *, item_is_preprint: bool = False) -> bool:
        return allows(self.min_version, version, item_is_preprint=item_is_preprint)

    def banner(self) -> str:
        """One line for the run banner: every value and its source."""
        def part(name: str) -> str:
            val = getattr(self, name)
            shown = str(val).lower() if isinstance(val, bool) else val
            return f"{name}={shown} ({self.sources.get(name, 'default')})"
        return "PDF policy: " + " · ".join(
            part(n) for n in ("min_version", "reconstructed", "paid")
        )


def read_project_constants(path: str | Path) -> dict[str, object]:
    """The policy constants assigned at top level in `path`, as literals.

    Parsed, not imported — see the module docstring. A constant assigned
    something other than a literal is an error (the run cannot know its
    value without executing the file). Missing constants are simply
    absent from the result.
    """
    path = Path(path)
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except SyntaxError as e:
        raise PolicyError(f"{path}: cannot parse ({e.msg}, line {e.lineno})") from None
    wanted = {const: name for name, const in PROJECT_CONSTANTS.items()}
    out: dict[str, object] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        else:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id in wanted:
                try:
                    out[wanted[target.id]] = ast.literal_eval(value)
                except ValueError:
                    raise PolicyError(
                        f"{path}: {target.id} must be a plain literal "
                        f"(a string or True/False)",
                    ) from None
    return out


def resolve(
    *,
    global_cfg: dict | None = None,
    legacy: dict[str, tuple[object, str]] | None = None,
    project: dict[str, object] | None = None,
    project_label: str = "project",
    flags: dict[str, tuple[object, str]] | None = None,
) -> PdfPolicy:
    """Combine the tiers into one policy (precedence in the module docstring).

    `global_cfg` is the `[pdf]` table; `legacy` maps a field to
    `(already-mapped value, label)`; `project` is
    `read_project_constants`'s result; `flags` maps a field to
    `(value, label)` for the flags that were given.
    """
    tiers: list[dict[str, tuple[object, str]]] = [
        {k: (v, f"flag ({lbl})") for k, (v, lbl) in (flags or {}).items()},
        {k: (v, f"project ({project_label})") for k, v in (project or {}).items()},
        {k: (v, "global") for k, v in (global_cfg or {}).items()},
        {k: (v, f"legacy {lbl}") for k, (v, lbl) in (legacy or {}).items()},
    ]
    values: dict[str, object] = {}
    sources: dict[str, str] = {}
    for name in DEFAULTS:
        for tier in tiers:
            if name in tier and not _blank(tier[name][0]):
                raw, label = tier[name]
                values[name] = coerce(name, raw, label)
                sources[name] = label
                break
        else:
            values[name] = DEFAULTS[name]
            sources[name] = "default"
    return PdfPolicy(
        min_version=str(values["min_version"]),
        reconstructed=bool(values["reconstructed"]),
        paid=str(values["paid"]),
        sources=sources,
    )


def legacy_paid(value: object) -> str | None:
    """`[openalex] use_paid_content_api` as a `paid` mode: false → `off`,
    true → `prefer`, unset or unrecognised → None (no answer)."""
    if isinstance(value, bool):
        return "prefer" if value else "off"
    text = str(value if value is not None else "").strip().lower()
    if text in _FALSE:
        return "off"
    if text in _TRUE:
        return "prefer"
    return None


def legacy_reconstructed(value: object) -> bool | None:
    """`[elsevier] render_xml_to_pdf` as `reconstructed`, or None when unset.
    Only an explicit true token enables, as it always did."""
    if isinstance(value, bool):
        return value
    text = str(value if value is not None else "").strip().lower()
    if not text:
        return None
    return text in _TRUE
