"""Direct tests for `core.llm.extract_json_from_response` (D8, F4).

Every full-text coding reply goes through it, and it had no direct tests.
The old hand-rolled walker missed the JSON after prose with an odd number
of double quotes or a stray closing brace.
"""

from __future__ import annotations

import pytest
from core.llm import extract_json_from_response

OBJ = {"decision": "include", "reason": "fits"}
JSON = '{"decision": "include", "reason": "fits"}'


@pytest.mark.parametrize("text", [
    JSON,
    f"```json\n{JSON}\n```",
    f"```\n{JSON}\n```",
    f"Looking at this paper, I need to weigh it.\n{JSON}",
    f"{JSON}\nThat is my answer.",
    f"Some prose.\n```json\n{JSON}\n```\nMore prose.",
])
def test_the_usual_shapes(text) -> None:
    assert extract_json_from_response(text) == OBJ


def test_prose_with_an_odd_number_of_quotes_before_the_json() -> None:
    text = f'The authors call it "self-efficacy. Overall:\n{JSON}'
    assert extract_json_from_response(text) == OBJ


def test_a_stray_closing_brace_before_the_json() -> None:
    text = "Criteria met } mostly.\n" + JSON
    assert extract_json_from_response(text) == OBJ


def test_a_malformed_brace_block_before_the_real_one() -> None:
    text = f"Template: {{decision: ...}}\n{JSON}"
    assert extract_json_from_response(text) == OBJ


def test_nested_objects_and_braces_inside_strings() -> None:
    text = ('Answer: {"decision": "include", "reason": "uses {x} notation",'
            ' "fields": {"sample": "N=10"}}')
    assert extract_json_from_response(text) == {
        "decision": "include", "reason": "uses {x} notation",
        "fields": {"sample": "N=10"},
    }


@pytest.mark.parametrize("text", ["", None, "no json here", "[1, 2, 3]",
                                  '{"decision": "include", "reason": '])
def test_nothing_to_find_is_none(text) -> None:
    assert extract_json_from_response(text) is None
