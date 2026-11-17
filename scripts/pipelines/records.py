"""Coding fields that hold a LIST of records, one per measure/finding/etc.

A plain coding field is one string per paper. Some reviews need several
structured records per paper — e.g. every measure a study uses, each with a
concept from a closed list and the verbatim passage that shows it. Declare
such a field in `screening_config.py` with a `records` spec::

    {
        "name": "measures",
        "description": "Every measure of X the paper uses.",
        "records": {
            "keys": ["id", "measure", "concept", "context", "quote"],
            "values": {"concept": ["trust", "commitment", "not covered"]},
            "quote": "quote",                      # key holding the verbatim quote
            "comment": "{measure} - {concept}",    # highlight comment template
        },
    }

`keys` are all required in every record. `values` closes the vocabulary of
named keys. `quote` (default ``"quote"``) names the key whose text must occur
in the PDF. `comment` (default: every non-quote key joined with " ·") is what
`highlight_quotes.py` writes on the highlight, so the model need not repeat
measure and concept in a separate `evidence_quotes` entry.

The model answers with a JSON list of objects; the coded value stays that
JSON string in the CSV row and the coding note, like any list-valued field.

Stdlib-only: it sits below both `fulltext_code.py` and `highlight_quotes.py`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable

DEFAULT_QUOTE_KEY = "quote"


def spec_of(field: dict) -> dict | None:
    spec = field.get("records")
    return spec if isinstance(spec, dict) else None


def quote_key(spec: dict) -> str:
    return spec.get("quote") or DEFAULT_QUOTE_KEY


def validate_spec(name: str, field: dict) -> list[str]:
    """Problems with a field's `records` spec; [] when sound (or absent)."""
    if "records" not in field:
        return []
    spec = field["records"]
    where = f"FULLTEXT_CODING_FIELDS[{name!r}].records"
    if not isinstance(spec, dict):
        return [f"{where} must be a dict"]
    problems: list[str] = []
    keys = spec.get("keys")
    if (not isinstance(keys, (list, tuple)) or not keys
            or any(not isinstance(k, str) or not k.strip() for k in keys)
            or len(set(keys)) != len(keys)):
        return [f"{where}.keys must be a non-empty list of unique strings"]
    if quote_key(spec) not in keys:
        problems.append(f"{where}.keys must include the quote key "
                        f"{quote_key(spec)!r}")
    if field.get("values") is not None:
        problems.append(f"{name!r} cannot have both `values` and `records`")
    for k, vals in (spec.get("values") or {}).items():
        if k not in keys:
            problems.append(f"{where}.values names {k!r}, not in keys")
        elif (not isinstance(vals, (list, tuple)) or not vals
              or any(not isinstance(v, str) or not v.strip() for v in vals)):
            problems.append(f"{where}.values[{k!r}] must be a non-empty "
                            f"list of strings")
    template = spec.get("comment")
    if template is not None:
        if not isinstance(template, str):
            problems.append(f"{where}.comment must be a string")
        else:
            for ph in re.findall(r"{(\w*)}", template):
                if ph not in keys:
                    problems.append(f"{where}.comment uses {{{ph}}}, "
                                    f"not in keys")
    return problems


def prompt_slot(spec: dict) -> str:
    """The inline JSON example for the schema block of the prompt."""
    closed = spec.get("values") or {}
    parts = []
    for k in spec["keys"]:
        slot = " | ".join(f'"{v}"' for v in closed[k]) if k in closed else '"<...>"'
        parts.append(f'"{k}": {slot}')
    return "[{" + ", ".join(parts) + "}, ...]"


def prompt_guide(spec: dict) -> str:
    qk = quote_key(spec)
    text = (f" Answer with a JSON list, one object per record, each with exactly "
            f"the keys {', '.join(spec['keys'])}. `{qk}` must be copied "
            f"verbatim from the paper's text. Use [] if there are none.")
    for k, vals in (spec.get("values") or {}).items():
        text += f" `{k}` must be exactly one of: {', '.join(vals)}."
    return text


def parse(raw) -> list[dict] | None:
    """The coded value → list of dict records; None if it is not one."""
    if isinstance(raw, list):
        data = raw
    elif isinstance(raw, str) and raw.strip():
        try:
            data = json.loads(raw, strict=False)
        except json.JSONDecodeError:
            return None
    else:
        return []
    if not isinstance(data, list) or any(not isinstance(r, dict) for r in data):
        return None
    return data


def render_comment(rec: dict, spec: dict) -> str:
    """The highlight comment for one record."""
    qk = quote_key(spec)
    template = spec.get("comment")
    if template:
        return re.sub(r"{(\w+)}", lambda m: str(rec.get(m.group(1), "")).strip(),
                      template).strip()
    return " · ".join(str(rec[k]).strip() for k in spec["keys"]
                      if k != qk and str(rec.get(k, "")).strip())


def check(raw, spec: dict, quote_found: Callable[[str], bool],
          ) -> tuple[list[dict], list[str], list[str]]:
    """`(verified, dropped_reasons, vocab_warnings)` for one coded value.

    A record is dropped when it lacks a key, has no quote, or its quote is
    not found by `quote_found`. An out-of-vocabulary value is kept but
    reported, as for a single closed field: the note keeps what the model
    said.
    """
    records = parse(raw)
    if records is None:
        return [], ["not a JSON list of objects"], []
    qk = quote_key(spec)
    closed = spec.get("values") or {}
    verified, dropped, vocab = [], [], []
    for i, rec in enumerate(records, 1):
        label = str(rec.get("id") or i)
        missing = [k for k in spec["keys"] if k not in rec]
        if missing:
            dropped.append(f"record {label}: missing {', '.join(missing)}")
            continue
        quote = str(rec.get(qk, "")).strip()
        if not quote:
            dropped.append(f"record {label}: empty {qk}")
            continue
        if not quote_found(quote):
            dropped.append(f"record {label}: quote not in PDF text")
            continue
        for k, vals in closed.items():
            if str(rec.get(k, "")).strip() not in vals:
                vocab.append(f"record {label}: {k}={rec.get(k)!r}")
        verified.append(rec)
    return verified, dropped, vocab
