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
BACKEND_PY = sorted(REPO.glob("*.py"))   # main, api, storage, domain, llm_prompt, config


_REGEX_PREV_PUNCT = set("(,=:[!&|?{};+-*%~^<>")
_REGEX_PREV_WORDS = ("return", "typeof", "case", "in", "of", "delete", "void", "instanceof")


def strip_js_literals(src):
    """Drops comments, string/template literals and regex literals.

    Bracket counting must reflect code structure, not characters inside text —
    e.g. the character class in /^\\s*[\\[{]/ is balanced code but looks like
    stray brackets to a naive counter.

    Template literals are handled with their ${...} interpolations: the code
    inside them is kept (it is real code, and may contain nested templates),
    only the surrounding text is dropped.
    """
    out = []
    i, n = 0, len(src)
    prev = ""            # last significant emitted char (regex-vs-division hint)
    mode = "code"
    brace_depth = 0
    interp_stack = []    # brace depth captured when each ${ was opened

    def after_keyword():
        tail = "".join(out).rstrip()
        return any(tail.endswith(w) for w in _REGEX_PREV_WORDS)

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""

        if mode == "tmpl":                                 # inside `...`
            if ch == "\\":
                i += 2
                continue
            if ch == "`":
                mode, prev, i = "code", "x", i + 1
                continue
            if ch == "$" and nxt == "{":                   # ${ → back to code
                interp_stack.append(brace_depth)
                brace_depth += 1
                out.append("{")                            # keep it balanced
                mode, prev, i = "code", "{", i + 2
                continue
            i += 1                                         # plain template text
            continue

        if ch == "/" and nxt == "/":                       # line comment
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "/" and nxt == "*":                       # block comment
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue

        if ch in "\"'":                                    # string literal
            quote, i = ch, i + 1
            while i < n:
                if src[i] == "\\":
                    i += 2
                    continue
                if src[i] == quote:
                    i += 1
                    break
                i += 1
            prev = "x"
            continue

        if ch == "`":                                      # template starts
            mode, i = "tmpl", i + 1
            continue

        if ch == "/" and (prev == "" or prev in _REGEX_PREV_PUNCT or after_keyword()):
            j, in_class, closed = i + 1, False, False      # regex literal
            while j < n:
                c = src[j]
                if c == "\\":
                    j += 2
                    continue
                if c == "\n":
                    break                                  # unterminated → not a regex
                if c == "[":
                    in_class = True
                elif c == "]":
                    in_class = False
                elif c == "/" and not in_class:
                    closed, j = True, j + 1
                    break
                j += 1
            if closed:
                i = j
                while i < n and src[i].isalpha():          # flags
                    i += 1
                prev = "x"
                continue

        if ch == "{":
            brace_depth += 1
        elif ch == "}":
            brace_depth -= 1
            if interp_stack and brace_depth == interp_stack[-1]:
                interp_stack.pop()                         # ${...} closed
                out.append("}")
                mode, prev, i = "tmpl", "x", i + 1
                continue

        out.append(ch)
        if not ch.isspace():
            prev = ch
        i += 1
    return "".join(out)


def _balanced(text, open_ch, close_ch):
    return text.count(open_ch) == text.count(close_ch)


def test_app_js_brackets_balanced():
    code = strip_js_literals(APP_JS.read_text(encoding="utf-8"))
    assert _balanced(code, "{", "}"), "unbalanced {} in app.js"
    assert _balanced(code, "(", ")"), "unbalanced () in app.js"
    assert _balanced(code, "[", "]"), "unbalanced [] in app.js"


def test_strip_js_literals_ignores_brackets_inside_literals():
    """Guard for the stripper itself — otherwise it could silently pass anything."""
    assert strip_js_literals("const a = '{[(';") .count("{") == 0
    assert strip_js_literals("const re = /^\\s*[\\[{]/;").count("{") == 0
    assert strip_js_literals("// комментарий {[(\nlet x = 1;").count("(") == 0
    assert strip_js_literals("const t = `текст [ ${a} ]`;").count("[") == 0
    # code inside ${...} is kept, and a nested template doesn't end the outer one
    nested = "const s = `<a>${list.map(x => `<b>${x}</b>`).join('')}</a>`;"
    assert _balanced(strip_js_literals(nested), "{", "}")
    assert _balanced(strip_js_literals(nested), "(", ")")
    assert "list.map" in strip_js_literals(nested)
    # real structure survives
    assert strip_js_literals("function f() { return [1]; }").count("{") == 1


@pytest.mark.parametrize("path", [APP_JS, INDEX, STYLE, *BACKEND_PY])
def test_no_merge_conflict_markers(path):
    text = path.read_text(encoding="utf-8")
    for marker in ("<<<<<<<", ">>>>>>>"):
        assert marker not in text, f"merge conflict marker {marker!r} in {path.name}"
    # "=======" can appear legitimately in code comments/separators, so we only
    # flag it when paired with the other markers above (already checked).


@pytest.mark.parametrize("path", BACKEND_PY, ids=lambda p: p.name)
def test_backend_module_parses(path):
    ast.parse(path.read_text(encoding="utf-8"))


def test_index_references_cachebusted_assets():
    html = INDEX.read_text(encoding="utf-8")
    js = re.search(r"app\.js\?v=(\d+)", html)
    css = re.search(r"style\.css\?v=(\d+)", html)
    assert js, "index.html must reference app.js?v=<int>"
    assert css, "index.html must reference style.css?v=<int>"
    # versions are integers (sanity — they're parsed as such by the regex)
    assert int(js.group(1)) >= 1
    assert int(css.group(1)) >= 1
