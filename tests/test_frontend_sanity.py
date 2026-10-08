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


def test_plan_table_columns_match_colspan():
    """Шапка таблицы плана и PLAN_COLSPAN обязаны совпадать.

    Расхождение ломает пустое состояние и строку «+ Неделя» молча — ячейка
    с colspan просто встаёт не на всю ширину, ошибок в консоли нет.
    """
    html = INDEX.read_text(encoding="utf-8")
    thead = re.search(r'<table class="plan">\s*<thead><tr>(.*?)</tr>', html, re.S)
    assert thead, "не найдена шапка таблицы плана"
    columns = len(re.findall(r"<th[ >]", thead.group(1)))

    js = APP_JS.read_text(encoding="utf-8")
    fixed = re.search(r"const PLAN_COLSPAN\s*=\s*(\d+)\s*\+\s*PLAN_DAYS\.length", js)
    assert fixed, "PLAN_COLSPAN не найден или записан иначе"
    days_decl = re.search(r"const PLAN_DAYS = \[(.*?)\];", js, re.S)
    assert days_decl, "PLAN_DAYS не найден"
    days = days_decl.group(1).count("[")

    assert int(fixed.group(1)) + days == columns, (
        f"в шапке {columns} колонок, PLAN_COLSPAN даёт {int(fixed.group(1)) + days}")


def test_frontend_reads_the_statuses_backend_emits():
    """Статусы дня — контракт между compliance.py и отрисовкой плана (#41).
    Переименование на бэкенде без правки фронта убрало бы все отметки разом.
    """
    import compliance

    js = APP_JS.read_text(encoding="utf-8")
    for status in (compliance.DONE, compliance.MISSED, compliance.EXTRA):
        assert f"'{status}'" in js, f"фронт не знает статуса {status!r}"


# ── Тренер (#44) ──────────────────────────────────────────────────────────────

def test_inline_handlers_point_at_existing_functions():
    """onclick/onchange в index.html зовут функции по имени. Опечатка или
    переименование в app.js ломает кнопку молча — до первого клика."""
    html = INDEX.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    defined = set(re.findall(r"^(?:async\s+)?function\s+(\w+)\s*\(", js, re.M))
    called = set()
    for handler in re.findall(r'\bon(?:click|change|input|keydown)="([^"]*)"', html):
        called.update(re.findall(r"(?<![.\w])([A-Za-z_]\w*)\s*\(", handler))
    missing = called - defined
    assert not missing, f"в app.js нет функций: {sorted(missing)}"


def test_coach_tab_is_wired():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="tab-coach"' in html
    assert "showTab('coach',this)" in html


def _js_section(js, start_marker, end_marker):
    start = js.index(start_marker)
    return js[start:js.index(end_marker, start)]


# ── ИИ-тренер (#46) ───────────────────────────────────────────────────────────

def test_ai_coach_tab_is_wired():
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="tab-aicoach"' in html
    assert "showTab('aicoach',this)" in html
    assert "if(name==='aicoach')openAiCoachTab()" in APP_JS.read_text(encoding="utf-8")


def test_old_adjust_tab_is_gone_everywhere():
    """Вкладку заменили целиком: забытая ссылка на её элементы падает в
    рантайме на getElementById(...).classList."""
    for path in (APP_JS, INDEX):
        text = path.read_text(encoding="utf-8")
        for leftover in ("tab-adjust", "renderAdjust", "llm-advice", "requestLlmAdvice"):
            assert leftover not in text, f"{leftover} остался в {path.name}"


def test_ai_coach_generated_handlers_exist():
    """Кнопки разборов и подсказок рисует app.js — проверка inline-обработчиков
    из index.html их не видит."""
    js = APP_JS.read_text(encoding="utf-8")
    section = _js_section(js, "// ── ИИ-тренер (#46)", "function showTab(")
    defined = set(re.findall(r"^(?:async\s+)?function\s+(\w+)\s*\(", js, re.M))
    called = set()
    for handler in re.findall(r'onclick="([^"]*)"', section):
        handler = re.sub(r"\$\{[^}]*\}", "", handler)     # подстановки — не вызовы обработчика
        called.update(re.findall(r"(?<![.\w])([A-Za-z_]\w*)\s*\(", handler))
    assert {"aiOpenThread", "aiArchiveThread", "aiStarter", "showRunDetail",
            "aiApplyProposal"} <= called
    assert not (called - defined), sorted(called - defined)


def test_plan_is_changed_only_by_the_apply_button():
    """ИИ предлагает, применяет спортсмен: раздел ИИ-тренера сам план не
    пишет — ни напрямую в PLAN, ни через сохранение недель. После применения
    он перечитывает план с сервера."""
    js = APP_JS.read_text(encoding="utf-8")
    section = _js_section(js, "// ── ИИ-тренер (#46)", "function showTab(")
    assert not re.search(r"(?<![.\w])PLAN\s*=[^=]", section)
    assert "postPlanWeeks" not in section and "savePlanEdits" not in section
    apply = _js_section(section, "async function aiApplyProposal(", "\n}\n")
    assert "confirm(" in apply and "planEditMode" in apply
    assert apply.index("/apply`") < apply.index("await loadPlan()")


def test_run_review_entry_points_are_wired():
    html = INDEX.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    assert 'id="rd-ai-btn"' in html and 'id="ai-run"' in html
    assert "getElementById('rd-ai-btn')" in js
    assert "onAiReview: () => aiReviewRun(id)" in js
    assert "reviewable: true" in js


def test_coach_screen_offers_no_ai_review_of_an_athletes_run():
    """Разбор идёт по данным и лимиту самого пользователя — пробежка
    спортсмена на экране тренера в него попасть не должна."""
    js = APP_JS.read_text(encoding="utf-8")
    section = _js_section(js, "const COACH = {", "function renderAll()")
    for leak in ("aiReviewRun", "onAiReview", "reviewable"):
        assert leak not in section, leak


def test_ai_coach_answers_are_escaped_before_markup():
    """Ответ модели — недоверенный текст: разметка накладывается только
    поверх экранированной строки."""
    js = APP_JS.read_text(encoding="utf-8")
    body = _js_section(js, "function mdLite(", "\n}\n")
    assert body.index("escapeHtml(text)") < body.index(".replace(")
    section = _js_section(js, "// ── ИИ-тренер (#46)", "function showTab(")
    assert "localStorage" not in section


# ── Данные с сервера в разметке (#53) ─────────────────────────────────────────
# Пробежки, старты и план тренеру показывает спортсмен, а записать в них он
# мог что угодно. Поле, вставленное в разметку как есть, исполняется в сессии
# того, кто на него смотрит.

def js_templates(src):
    """Шаблонные литералы: [(свой текст, [свои подстановки], номер строки)].

    Подстановка отдаётся без вложенных шаблонов (на их месте ``; сами они идут
    отдельными записями), без комментариев, со строками, сведёнными к ''.
    """
    found, stack = [], []    # кадр — шаблон {"text", "exprs", "line"} или код {"code", "depth"}
    recent = ""              # хвост кода: по нему отличаем regex от деления
    i, n = 0, len(src)

    def emit(piece):
        nonlocal recent
        if stack and "code" in stack[-1]:
            stack[-1]["code"].append(piece)
        recent = (recent + piece)[-16:]

    while i < n:
        ch = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        top = stack[-1] if stack else None

        if top is not None and "text" in top:               # внутри `...`
            if ch == "\\":
                top["text"].append(src[i:i + 2])
                i += 2
            elif ch == "`":
                stack.pop()
                found.append(("".join(top["text"]), top["exprs"], top["line"]))
                emit("``")
                i += 1
            elif ch == "$" and nxt == "{":
                stack.append({"code": [], "depth": 0})
                recent = "{"
                i += 2
            else:
                top["text"].append(ch)
                i += 1
            continue

        if ch == "/" and nxt == "/":                        # комментарии
            j = src.find("\n", i)
            i = n if j == -1 else j
            continue
        if ch == "/" and nxt == "*":
            j = src.find("*/", i + 2)
            i = n if j == -1 else j + 2
            continue
        if ch in "\"'":                                     # строка
            j = i + 1
            while j < n and src[j] != ch:
                j += 2 if src[j] == "\\" else 1
            emit("''")
            i = j + 1
            continue
        if ch == "`":
            stack.append({"text": [], "exprs": [], "line": src.count("\n", 0, i) + 1})
            i += 1
            continue
        if ch == "/":                                       # regex-литерал?
            tail = recent.rstrip()
            if not tail or tail[-1] in _REGEX_PREV_PUNCT or tail.endswith(_REGEX_PREV_WORDS):
                j, in_class, closed = i + 1, False, False
                while j < n and src[j] != "\n":
                    if src[j] == "\\":
                        j += 2
                        continue
                    if src[j] == "[":
                        in_class = True
                    elif src[j] == "]":
                        in_class = False
                    elif src[j] == "/" and not in_class:
                        closed = True
                        break
                    j += 1
                if closed:
                    j += 1
                    while j < n and src[j].isalpha():
                        j += 1
                    emit("/re/")
                    i = j
                    continue

        if top is not None:                                 # код подстановки
            if ch == "{":
                top["depth"] += 1
            elif ch == "}":
                if top["depth"] == 0:
                    stack.pop()
                    stack[-1]["exprs"].append("".join(top["code"]).strip())
                    recent = "x"
                    i += 1
                    continue
                top["depth"] -= 1
        emit(ch)
        i += 1

    assert not stack, "шаблонный литерал не закрыт — сканер сбился"
    return found


# Что считается обезвреженным: экранирование, приведение к числу и функции,
# которые сами собирают разметку или текст из своих аргументов.
_SAFE_CALLS = ("escapeHtml", "Number", "km1", "chatTime", "mdLite", "ddmm", "secondsToTime",
               "plannedKmLabel", "dayFactHtml", "aiRunChipHtml", "aiProposalHtml",
               "runItemHtml", "kmCell", "dayCell", "inp", "typeSel", "hero")
# Словари подписей в коде: значение берётся из них, а не из данных.
_SAFE_MAPS = r"(?:[A-Z][A-Z_0-9]*|label|feelEmoji|typeLabels|statusLabel)"
_MARKUP = re.compile(r"</?[a-zA-Z]")
_DATA_FIELD = re.compile(r"(?<![\w.$])[a-z_]\w*(?:\?\.|\.)(?!length\b)[A-Za-z_]\w*(?!\w*\s*\()"
                         r"|(?<![\w.$])[a-z_]\w*\[")


def _drop_calls(expr, names):
    """Убирает вызовы names(...) целиком, со сбалансированными скобками."""
    pattern = re.compile(r"(?<![\w.$])(?:%s)\s*\(" % "|".join(names))
    while True:
        m = pattern.search(expr)
        if not m:
            return expr
        depth, j = 1, m.end()
        while j < len(expr) and depth:
            depth += {"(": 1, ")": -1}.get(expr[j], 0)
            j += 1
        expr = expr[:m.start()] + expr[j:]


def raw_field_in(expr):
    """Поле данных, которое подстановка выводит как есть; None — такого нет."""
    emitted = re.split(r"(?<!\?)\?(?![.?])", expr, maxsplit=1)[-1]   # условие тернарника не выводится
    emitted = _drop_calls(emitted, _SAFE_CALLS)
    emitted = re.sub(_SAFE_MAPS + r"\[[^\]]*\]", "", emitted)
    m = _DATA_FIELD.search(emitted)
    return m.group(0) if m else None


def unescaped_interpolations(js):
    """Подстановки в шаблонах с разметкой, выводящие поле данных как есть."""
    return [f"строка {line}: ${{{expr}}}"
            for text, exprs, line in js_templates(js) if _MARKUP.search(text)
            for expr in exprs if raw_field_in(expr)]


def test_markup_scanner_tells_raw_fields_from_escaped_ones():
    """Страховка для самой проверки — иначе она молча пропустит что угодно."""
    def found(js):
        return [raw_field_in(e) for text, exprs, _ in js_templates(js)
                if _MARKUP.search(text) for e in exprs]

    # так выглядели дыры до #53
    assert found("x = `<b>${r.hr?r.hr+' уд/мин':''}</b>`;") == ["r.hr"]
    assert found("x = `<td>${r.w ?? i+1}</td>`;") == ["r.w"]
    assert found("x = `<span>${r.date.slice(0,4)}</span>`;") == ["r.date"]
    assert found("x = `<button onclick=\"deleteRun(${r.id})\">`;") == ["r.id"]
    assert found("x = `<td>${r[f]}</td>`;") == ["r["]
    # а так — обезврежено или вовсе не данные
    for safe in ("`<b>${r.hr?escapeHtml(r.hr)+' уд/мин':''}</b>`",
                 "`<td>${escapeHtml(r.w ?? i+1)}</td>`",
                 "`<button onclick=\"deleteRun(${Number(r.id)})\">`",
                 "`<tr class=\"${r.type==='race'?'race-week':''}\">`",
                 "`<span class=\"${PLAN_BADGE[r.type]||''}\">${label[d.field]}</span>`",
                 "`<i>${rows.map(r => `<b>${escapeHtml(r.name)}</b>`).join('')} из ${plan.length}</i>`"):
        assert found("x = " + safe + ";") in ([None], [None, None], [None, None, None]), safe
    # вложенный шаблон проверяется сам по себе, а не прячется во внешнем
    assert found("x = `<i>${list.map(r => `<b>${r.name}</b>`).join('')}</i>`;") == ["r.name", None]
    # шаблон без разметки (текст для textContent, адрес) не трогаем
    assert found("x = `HTTP ${res.status}`; y = `${API_URL}plans/${plan.id}/weeks`;") == []
    # кавычки и скобки внутри regex и строк сканер не сбивают
    assert found("a = /[\",`]/.test(s) ? '`' : \"${\"; x = `<b>${r.id}</b>`;") == ["r.id"]


def test_server_values_are_escaped_before_markup():
    """Ни одна подстановка в разметке не выводит поле данных как есть.

    Если тест упал на новой строке — оберните значение в escapeHtml (или в
    Number, если это число). Функцию, которая сама собирает разметку из
    аргументов, допишите в _SAFE_CALLS, а её шаблоны проверятся отдельно.
    """
    leaks = unescaped_interpolations(APP_JS.read_text(encoding="utf-8"))
    assert not leaks, "поле данных в разметке без экранирования:\n" + "\n".join(leaks)


def test_run_and_race_ids_reach_inline_handlers_only_as_numbers():
    """Внутри onclick escapeHtml не спасает: строка там исполняется как код.
    Поэтому id пробежки и старта попадает в обработчик только через Number()."""
    js = APP_JS.read_text(encoding="utf-8")
    calls = re.findall(r"(?:showRunDetail|showCoachRunDetail|deleteRun|deleteRace|aiReviewRun)"
                       r"\(\$\{([^}]*)\}\)", js)
    assert len(calls) >= 7, "обработчики с id найдены не все — проверка устарела"
    for arg in calls:
        assert arg == "id" or re.fullmatch(r"Number\((?:r\.id|runId)\)", arg), arg
    # «id» — локальная переменная строки журнала, уже приведённая к числу
    assert "const id = Number(r.id);" in _js_section(js, "function runItemHtml(", "\n}\n")
    assert not re.search(r"\$\{\s*r\.id\s*\}", js)


def test_run_card_and_plan_escape_what_the_athlete_typed():
    """Карточка пробежки собирает строки массивом, мимо шаблона с разметкой —
    общая проверка её не видит, поэтому поля названы поимённо."""
    js = APP_JS.read_text(encoding="utf-8")
    card = _js_section(js, "function openRunDetail(", "\n}\n")
    assert "escapeHtml(run.hr)" in card and "${run.hr}" not in card
    assert "feelLabels[run.feel] || escapeHtml(run.feel)" in card
    row = _js_section(js, "function runItemHtml(", "\n}\n")
    assert "escapeHtml(r.hr)" in row
    for body in (_js_section(js, "function planViewRowsHtml(", "\n}\n"),
                 _js_section(js, "function planWeekHtml(", "\n}\n")):
        assert "escapeHtml(r.w ??" in body and "${r.w" not in body


def test_a_date_that_is_not_a_string_does_not_break_rendering():
    """У записи, сохранённой до проверки полей, дата может быть числом или
    null: r.date.slice тогда роняет отрисовку всего списка."""
    js = APP_JS.read_text(encoding="utf-8")
    assert "r.date.slice" not in js and ".date.localeCompare(" not in js
    for name in ("runItemHtml", "renderRaces"):
        assert "String(r.date ?? '')" in _js_section(js, f"function {name}(", "\n}\n"), name


def test_coach_screen_keeps_out_of_own_data():
    """Экран тренера держит чужие данные в COACH. Запись в свои runs/PLAN или
    в localStorage отправила бы чужие пробежки в кэш и офлайн-синхронизацию."""
    js = APP_JS.read_text(encoding="utf-8")
    start = js.index("const COACH = {")
    section = js[start:js.index("function renderAll()", start)]
    assert "showCoachRunDetail" in section, "секция тренера найдена не целиком"
    assert "localStorage" not in section
    for own in ("runs", "PLAN", "PLANS", "COMPLIANCE", "ACTIVE_PLAN", "races"):
        assert not re.search(rf"(?<![.\w]){own}\s*=[^=]", section), f"запись в {own}"


# ── Мобильная раскладка (#48) ─────────────────────────────────────────────────

def _tab_groups(js):
    literal = re.search(r"const TAB_GROUP = \{(.*?)\};", js, re.S)
    assert literal, "TAB_GROUP не найден"
    return dict(re.findall(r"(\w+):\s*'(\w+)'", literal.group(1)))


def test_mobile_shell_is_wired():
    html = INDEX.read_text(encoding="utf-8")
    for element in ('id="tabbar"', 'id="tab-more"', 'id="add-sheet"',
                    'id="progress-switch"', 'id="more-back"'):
        assert element in html, element
    assert "viewport-fit=cover" in html, "без него env(safe-area-inset-*) равны нулю"


def test_every_tab_belongs_to_a_tabbar_item():
    """Вкладка без группы на телефоне не подсветит ни одного пункта панели и
    не покажет возврат в «Ещё» — из неё останется выходить наугад."""
    html = INDEX.read_text(encoding="utf-8")
    groups = _tab_groups(APP_JS.read_text(encoding="utf-8"))
    tabs = set(re.findall(r'id="tab-(\w+)"', html))
    assert tabs == set(groups), f"расхождение: {sorted(tabs ^ set(groups))}"
    buttons = set(re.findall(r'class="tabbar-btn[^"]*" data-group="(\w+)"', html))
    assert set(groups.values()) == buttons, sorted(set(groups.values()) ^ buttons)


def test_show_tab_targets_exist():
    """showTab берёт вкладку по имени: опечатка падает на getElementById(...)
    только при клике."""
    html = INDEX.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    tabs = set(re.findall(r'id="tab-(\w+)"', html))
    called = set(re.findall(r"showTab\('(\w+)'", html + js))
    assert called <= tabs, f"нет вкладок: {sorted(called - tabs)}"


def test_gated_menu_items_are_handled_by_apply_role():
    """Пункты «Тренер» и «Пользователи» продублированы в «Ещё». Видимость
    обоих экземпляров ставит applyRole по data-gate — пункт с неизвестным
    значением остался бы скрыт навсегда."""
    html = INDEX.read_text(encoding="utf-8")
    js = APP_JS.read_text(encoding="utf-8")
    body = _js_section(js, "function applyRole(", "\n}\n")
    gates = set(re.findall(r'data-gate="(\w+)"', html))
    assert gates == {"admin", "coach"}
    for gate in gates:
        assert f"gate('{gate}'" in body, gate
        assert html.count(f'data-gate="{gate}"') == 2, "пункт должен быть и в меню, и в «Ещё»"


def test_plan_week_list_sits_right_before_the_table():
    """Таблицу на телефоне прячет соседний селектор «.plan-week:not(:empty) +
    .plan-table-wrap». Элемент между ними вернул бы таблицу под список."""
    html = INDEX.read_text(encoding="utf-8")
    assert re.search(r'<div id="plan-week" class="plan-week"></div>\s*<div class="plan-table-wrap"', html)
    assert ".plan-week:not(:empty) + .plan-table-wrap" in STYLE.read_text(encoding="utf-8")
    assert 'id="tab-today"' in html and 'id="today-body"' in html


def test_render_plan_refreshes_every_view_of_the_plan():
    """Таблица, неделя списком и «Сегодня» показывают один и тот же план —
    обновление по отдельности оставило бы на экране два разных."""
    js = APP_JS.read_text(encoding="utf-8")
    body = _js_section(js, "function renderPlan() {", "\n}\n")
    for view in ("renderPlanTable()", "renderPlanWeek()", "renderToday()"):
        assert view in body, view


def test_mobile_plan_views_call_existing_handlers():
    """Кнопки недели и экрана «Сегодня» рисует app.js — проверка
    inline-обработчиков из index.html их не видит."""
    js = APP_JS.read_text(encoding="utf-8")
    section = _js_section(js, "// ── План неделей (#48)", "function togglePlanEdit(")
    defined = set(re.findall(r"^(?:async\s+)?function\s+(\w+)\s*\(", js, re.M))
    called = set()
    for handler in re.findall(r'onclick="([^"]*)"', section):
        handler = re.sub(r"\$\{[^}]*\}", "", handler)
        called.update(re.findall(r"(?<![.\w])([A-Za-z_]\w*)\s*\(", handler))
    assert {"planWeekStep", "planWeekToday", "openDaySheet", "openAddSheet", "showTab"} <= called
    assert not (called - defined), sorted(called - defined)


def test_today_screen_does_not_guess_for_an_undated_plan():
    """У плана без дат недели отсчитываются от даты по умолчанию. Перевести
    такую неделю на «сегодня» значит показать чужую тренировку как свою."""
    js = APP_JS.read_text(encoding="utf-8")
    body = _js_section(js, "function renderToday() {", "\n}\n")
    assert body.index("planIsDated()") < body.index("getCurrentWeek()")


def test_week_boundaries_come_from_one_place():
    """Окно строки плана считает только weekWindows — зеркало week_windows
    бэкенда. Своя «+6 дней» в другом месте снова разойдётся с таблицей на
    строке короче недели: текущей окажется не та неделя."""
    js = APP_JS.read_text(encoding="utf-8")
    for name in ("getCurrentWeek", "getWeekLabel", "weekBuckets", "renderToday"):
        body = _js_section(js, f"function {name}(", "\n}\n")
        assert "weekWindows()" in body, name
    assert "getDate() + 6" not in js
    assert "planDateWarnings()" in _js_section(js, "function applyImportedPlan(", "\n}\n")


def test_plan_writes_carry_the_version_they_were_based_on():
    """#51: версия приходит вместе с неделями и уходит обратно с правкой;
    отказ сервера — это перечитанный план и ошибка с кодом, а не «HTTP 409»."""
    js = APP_JS.read_text(encoding="utf-8")
    load = _js_section(js, "async function loadPlan() {", "\n}\n")
    assert "'plan?meta=1'" in load and "rememberPlan(" in load
    post = _js_section(js, "async function postPlanWeeks(", "\n}\n")
    assert "base_version: base.version" in post and "base_plan_id: base.plan_id" in post
    stale, reload, raised = (post.index(s) for s in (
        "res.status === 409", "await loadPlan();", "code: 'plan_stale'"))
    assert stale < reload < raised
    # недели и их версия попадают в кэш только вместе
    assert js.count("localStorage.setItem(planCacheKey()") == 2
    assert _js_section(js, "function rememberPlan(", "\n}\n").count("localStorage.setItem(") == 2


def test_day_edit_writes_a_new_plan_version_and_leaves_staleness_to_the_server():
    """Правка дня на телефоне — та же версионная запись плана, что и из
    конструктора. Версию сверяет сервер (#51): изменившийся план — это
    plan_stale, после которого лист показывает, что теперь стоит в этом дне."""
    html = INDEX.read_text(encoding="utf-8")
    assert 'id="day-sheet"' in html and 'onclick="saveDayEdit()"' in html
    js = APP_JS.read_text(encoding="utf-8")
    body = _js_section(js, "async function saveDayEdit() {", "\n}\n")
    assert "planEditMode" in body, "открытый конструктор записал бы план без этой правки"
    write, reload, stale = (body.index(s) for s in (
        "await postPlanWeeks(", "await loadPlan();         //", "e.code === 'plan_stale'"))
    assert write < reload < stale
    assert "edit.was = " in body[stale:], "после отказа лист должен знать новое содержимое дня"
    # на запись уходит копия недель с одной ячейкой; локальный PLAN не трогаем
    assert "PLAN.map(" in body
    assert not re.search(r"(?<![.\w])PLAN\s*(\[[^\]]*\]\s*)*(\.\w+\s*)?=[^=]", body)
    assert "postPlanWeeks(PLAN" not in body
