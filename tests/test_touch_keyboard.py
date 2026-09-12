"""Touch text editing must preserve caret, selection and valid Unicode text."""

import json
from pathlib import Path
import subprocess

import pytest


KEYBOARD = Path(__file__).resolve().parents[1] / "static" / "touch-keyboard.js"


def _edit(value, start, end, key, max_length=120):
    arguments = json.dumps([value, start, end, key, max_length], ensure_ascii=False)
    script = "\n".join((
        f"const {{editTouchText}} = require({json.dumps(str(KEYBOARD))});",
        f"process.stdout.write(JSON.stringify(editTouchText(...{arguments})));",
    ))
    result = subprocess.run(
        ["node", "-e", script], capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout)


@pytest.mark.parametrize("value,start,end,key,expected", [
    ("abcd", 2, 2, "x", {"value": "abxcd", "caret": 3}),
    ("abcdef", 2, 4, "x", {"value": "abxef", "caret": 3}),
    ("ab", 1, 1, " ", {"value": "a b", "caret": 2}),
    ("ab", None, None, "c", {"value": "abc", "caret": 3}),
])
def test_insertion_replaces_selected_text_and_preserves_caret(value, start, end, key, expected):
    assert _edit(value, start, end, key) == expected


@pytest.mark.parametrize("value,start,end,expected", [
    ("abc", 0, 0, {"value": "abc", "caret": 0}),
    ("abc", 2, 2, {"value": "ac", "caret": 1}),
    ("abcdef", 1, 4, {"value": "aef", "caret": 1}),
    ("A😀B", 3, 3, {"value": "AB", "caret": 1}),
    ("A😀B", 1, 3, {"value": "AB", "caret": 1}),
    ("😀", 2, 2, {"value": "", "caret": 0}),
    ("", 0, 0, {"value": "", "caret": 0}),
])
def test_backspace_removes_selection_or_previous_whole_character(value, start, end, expected):
    assert _edit(value, start, end, "Backspace") == expected


def test_full_fields_allow_replacement_but_never_split_a_surrogate_pair():
    assert _edit("abcde", 5, 5, "x", 5) == {"value": "abcde", "caret": 5}
    assert _edit("abcde", 2, 4, "x", 5) == {"value": "abxe", "caret": 3}
    assert _edit("abcde", 1, 3, "😀", 5) == {"value": "a😀de", "caret": 3}
    assert _edit("abcd", 4, 4, "😀", 5) == {"value": "abcd", "caret": 4}


def test_clear_empties_the_entire_field_independently_of_selection():
    assert _edit("A😀BC", 1, 3, "Clear") == {"value": "", "caret": 0}


def test_numeric_draft_can_be_composed_without_a_physical_keyboard():
    value = ""
    caret = 0
    for key in "-0.25":
        result = _edit(value, caret, caret, key)
        value, caret = result["value"], result["caret"]
    assert value == "-0.25"
    assert caret == 5
