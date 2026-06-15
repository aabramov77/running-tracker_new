"""Lightweight static sanity checks for the frontend and source tree.

Node isn't available in this environment, so JS checks are heuristic:
bracket balance, no merge-conflict markers, and cache-buster wiring.
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
APP_JS = REPO / "app.js"
INDEX = REPO / "index.html"
STYLE = REPO / "style.css"
MAIN_PY = REPO / "main.py"


def _balanced(text, open_ch, close_ch):
    return text.count(open_ch) == text.count(close_ch)


def test_app_js_brackets_balanced():
    src = APP_JS.read_text(encoding="utf-8")
    assert _balanced(src, "{", "}"), "unbalanced {} in app.js"
    assert _balanced(src, "(", ")"), "unbalanced () in app.js"
    assert _balanced(src, "[", "]"), "unbalanced [] in app.js"


@pytest.mark.parametrize("path", [APP_JS, INDEX, STYLE, MAIN_PY])
def test_no_merge_conflict_markers(path):
    text = path.read_text(encoding="utf-8")
    for marker in ("<<<<<<<", ">>>>>>>"):
        assert marker not in text, f"merge conflict marker {marker!r} in {path.name}"
    # "=======" can appear legitimately in code comments/separators, so we only
    # flag it when paired with the other markers above (already checked).


def test_main_py_parses():
    ast.parse(MAIN_PY.read_text(encoding="utf-8"))


def test_index_references_cachebusted_assets():
    html = INDEX.read_text(encoding="utf-8")
    js = re.search(r"app\.js\?v=(\d+)", html)
    css = re.search(r"style\.css\?v=(\d+)", html)
    assert js, "index.html must reference app.js?v=<int>"
    assert css, "index.html must reference style.css?v=<int>"
    # versions are integers (sanity — they're parsed as such by the regex)
    assert int(js.group(1)) >= 1
    assert int(css.group(1)) >= 1
