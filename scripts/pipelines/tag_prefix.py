"""Per-review namespacing for the Zotero tags the pipeline writes.

Every tag that records *this review's judgement* — screening decisions, QA
verdicts, adjudications, search provenance, coded categorical values — is
written as ``<prefix>/<family>:<value>``, e.g. ``agentic-ai/abstract:include``.
The prefix comes from ``TAG_PREFIX`` in the project's ``screening_config.py``
and is mandatory.

**Why a namespace at all.** Two problems, both hit on a real library:

- A library with hundreds of personal tags gives the Zotero tag selector no
  way to show only one review's tags. ``abstract:`` and ``fulltext:`` are the
  *plugin's* vocabulary, so they do not distinguish one review from another.
  Typing the prefix does.
- One library commonly holds several reviews. Without a namespace two reviews
  write into the same ``abstract:*`` family, and because
  :meth:`zotero_io.ZoteroClient.merge_duplicate_item` unions tag sets, a
  later dedup can leave one item carrying two reviews' contradictory
  decisions.

**What is deliberately NOT namespaced.** Tags that state a fact about the
*item* rather than a judgement about it — ``predatory:flag``,
``retracted:flag``, ``pdf:tdm-recovered``, ``pdf:preprint-version``,
``pdf:repository-copy``. Those are true regardless of who is reviewing the
paper, so two reviews sharing an item should share them rather than each
stamping a private copy. The practical corollary is that ``enrich_pdfs.py``
and ``enrich_dois.py`` stay library-wide and need no prefix at all.

**The separator is load-bearing.** ``/`` divides namespace from family, ``:``
divides family from value. A prefix may contain neither, which is what keeps
``family()`` unambiguous and lets ``update_tags(remove_prefixed=[...])`` — a
plain ``str.startswith`` (see ``zotero_io.py``) — clear exactly one family of
one review without reaching into another's.

Stdlib-only, and it must stay that way: it sits below the orchestrators, and
``scripts/setup/set_tag_prefix.py`` mirrors its validation rules for a wizard
that may not import from ``scripts/pipelines/`` at all.
"""

from __future__ import annotations

import re
import sys
from types import ModuleType

#: Divides the review namespace from the tag family: ``agentic-ai/abstract:``.
NAMESPACE_SEP = "/"

#: The config attribute every project declares.
CONFIG_ATTR = "TAG_PREFIX"

#: Longest accepted prefix. Zotero itself does not care, but the prefix is
#: repeated on every tag in the selector, and a long one defeats the point.
MAX_PREFIX_LEN = 32

#: A well-formed prefix: lowercase alphanumerics and interior hyphens.
#: Excluding ``/`` and ``:`` is what keeps namespace and family parsing
#: unambiguous; excluding uppercase and spaces keeps the selector sortable.
PREFIX_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")

#: Shown verbatim by every caller that rejects a prefix, so the rules are
#: stated once and the user reads the same sentence wherever they trip.
RULES = (
    f"A tag prefix must be 1-{MAX_PREFIX_LEN} characters of lowercase letters, "
    "digits and interior hyphens (e.g. `agentic-ai`). No spaces, no `/`, no "
    "`:`, and it may not start or end with a hyphen."
)


def validate(raw: object) -> str:
    """Return the canonical bare prefix, or raise :class:`ValueError`.

    Callers that front a user-authored config file should use :func:`resolve`
    instead — it turns the error into an actionable ``sys.exit`` rather than a
    traceback, which is the right response to a typo in a project file.
    """
    if not isinstance(raw, str):
        raise ValueError(f"{CONFIG_ATTR} must be a string, not {type(raw).__name__}. {RULES}")
    prefix = raw.strip()
    if not prefix:
        raise ValueError(
            f"{CONFIG_ATTR} is empty. Every review must declare one so its tags "
            f"can be told apart from other reviews' in the same library. {RULES}"
        )
    if len(prefix) > MAX_PREFIX_LEN:
        raise ValueError(f"{CONFIG_ATTR} is {len(prefix)} characters. {RULES}")
    if not PREFIX_RE.match(prefix):
        raise ValueError(f"{CONFIG_ATTR} `{raw}` is not well-formed. {RULES}")
    return prefix


def namespace(raw: object) -> str:
    """``"agentic-ai"`` -> ``"agentic-ai/"``. Validates on the way through."""
    return validate(raw) + NAMESPACE_SEP


def family(ns: str, name: str) -> str:
    """The full removable prefix for one tag family: ``agentic-ai/abstract:``.

    Pass the result straight to ``screening_common.items_with_stage_tag`` or
    ``stage_tag_op`` — both already take a full prefix string, which is why
    namespacing needed no change to that module.
    """
    return f"{ns}{name}:"


def slug(text: str) -> str:
    """Coding-field and value names as they appear inside a tag.

    ``"research_design"`` -> ``"research-design"``; ``"Case study"`` ->
    ``"case-study"``. Colons become hyphens too, so a coded value that happens
    to contain one cannot forge a second family level.
    """
    lowered = re.sub(r"[\s_:/]+", "-", str(text).strip().lower())
    cleaned = re.sub(r"[^a-z0-9-]+", "", lowered)
    return re.sub(r"-{2,}", "-", cleaned).strip("-")


def coding_family(ns: str, field_name: str) -> str:
    """The removable prefix for one coded variable: ``agentic-ai/research-design:``."""
    return family(ns, slug(field_name))


def coding_tag(ns: str, field_name: str, value: str) -> str:
    """One coded value as a tag: ``agentic-ai/research-design:panel``."""
    return f"{coding_family(ns, field_name)}{slug(value)}"


def from_config(mod: ModuleType, path: str) -> str:
    """Read and validate ``TAG_PREFIX`` off an already-loaded config module.

    Exits with an actionable message rather than raising — this is a
    user-authored file and a traceback is the wrong response to a typo.
    """
    if not hasattr(mod, CONFIG_ATTR):
        sys.exit(
            f"ERROR: {path} is missing `{CONFIG_ATTR}`.\n"
            f"       Every review namespaces its Zotero tags so they can be "
            f"filtered in the tag\n"
            f"       selector and kept apart from other reviews in the same "
            f"library.\n"
            f"       Add a line such as `{CONFIG_ATTR} = \"agentic-ai\"`, or run:\n"
            f"         python3 ${{CLAUDE_PLUGIN_ROOT:-.}}/scripts/setup/"
            f"set_tag_prefix.py --prefix <prefix>"
        )
    try:
        return namespace(getattr(mod, CONFIG_ATTR))
    except ValueError as exc:
        sys.exit(f"ERROR: in {path}: {exc}")


def resolve(override: object, mod: ModuleType | None, path: str) -> str:
    """The one resolution order every script uses.

    ``--tag-prefix`` beats the config file; the config file is the normal
    source; neither is a hard exit. Returns the namespace *with* its trailing
    separator, ready for :func:`family`.
    """
    if override:
        try:
            return namespace(override)
        except ValueError as exc:
            sys.exit(f"ERROR: --tag-prefix: {exc}")
    if mod is None:
        sys.exit(
            f"ERROR: no tag prefix. Pass `--tag-prefix <prefix>`, or run from a "
            f"project whose\n       {path} declares `{CONFIG_ATTR}`."
        )
    return from_config(mod, path)


def add_argument(parser) -> None:
    """Register the shared ``--tag-prefix`` override on an argparse parser."""
    parser.add_argument(
        "--tag-prefix",
        default="",
        help=(
            "Namespace for this review's Zotero tags, overriding "
            f"`{CONFIG_ATTR}` in the screening config. {RULES}"
        ),
    )
