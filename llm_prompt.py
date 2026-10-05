"""Сборка текстового промпта для LLM из подготовленного контекста.

Выделено из main.py (#36, фаза 1). Модуль ничего не читает из GCS: на вход
приходит готовый словарь контекста, на выходе — текст. Это самая часто
изменяемая часть кода вокруг ИИ, и держать её отдельно дешевле.
"""
from datetime import datetime

from domain import (DIST_LABEL_KM, FEEL_LABELS, PLAN_DAYS, PLAN_PHASE_LABELS,
                    SEX_LABELS, TYPE_LABELS)

def plural_ru(number, one, few, many):
    """«1 тренировка», «3 тренировки», «5 тренировок» — промпт читает человек тоже."""
    tail = abs(int(number)) % 100
    if 11 <= tail <= 14:
        return many
    tail %= 10
    if tail == 1:
        return one
    if 2 <= tail <= 4:
        return few
    return many

def format_profile_block(profile, derived, bests):
    """Строки профиля для промпта. Незаполненное не печатаем — шум для модели."""
    lines = []
    day_ru = dict(PLAN_DAYS)

    who = []
    if profile.get("sex"):
        who.append(SEX_LABELS.get(profile["sex"], ""))
    if derived.get("age") is not None:
        who.append(f"{derived['age']} {plural_ru(derived['age'], 'год', 'года', 'лет')}")
    if profile.get("height_cm"):
        who.append(f"{profile['height_cm']} см")
    if profile.get("weight_kg"):
        weight = f"{profile['weight_kg']:g} кг"
        if derived.get("bmi"):
            weight += f" (ИМТ {derived['bmi']})"
        who.append(weight)
    if who:
        lines.append("Профиль: " + ", ".join(w for w in who if w))

    hr = []
    if profile.get("hr_max"):
        hr.append(f"макс {profile['hr_max']}")
    elif derived.get("hr_max_estimated"):
        hr.append(f"макс ~{derived['hr_max_estimated']} (оценка по возрасту, не измерялся)")
    if profile.get("hr_threshold"):
        hr.append(f"ПАНО {profile['hr_threshold']}")
    if profile.get("hr_rest"):
        hr.append(f"покой {profile['hr_rest']}")
    if hr:
        lines.append("Пульс: " + ", ".join(hr))
    if profile.get("vo2max"):
        lines.append(f"МПК: {profile['vo2max']:g}")

    # Ноль здесь — валидное и сильное значение (новичок без стажа), поэтому
    # сравниваем с None, а не проверяем на истинность.
    experience = []
    if profile.get("years_running") is not None:
        years = profile["years_running"]
        experience.append(f"стаж {years:g} {plural_ru(years, 'год', 'года', 'лет')}")
    if profile.get("weekly_km_typical") is not None:
        experience.append(f"обычный объём {profile['weekly_km_typical']:g} км/нед")
    if profile.get("sessions_per_week") is not None:
        sessions = profile["sessions_per_week"]
        experience.append(f"{sessions} {plural_ru(sessions, 'тренировка', 'тренировки', 'тренировок')} в неделю")
    if experience:
        lines.append("Опыт: " + ", ".join(experience))

    schedule = []
    if profile.get("available_days"):
        schedule.append("доступные дни — " + ", ".join(day_ru.get(d, d) for d in profile["available_days"]))
    if profile.get("long_run_day"):
        schedule.append("длительная — " + day_ru.get(profile["long_run_day"], profile["long_run_day"]))
    if schedule:
        lines.append("Расписание: " + "; ".join(schedule))

    if bests:
        lines.append("Личные рекорды: " + ", ".join(
            f"{b['km']:g} км {b['time']}" + (f" ({b['date']})" if b.get("date") else "")
            for b in bests))

    if profile.get("injuries"):
        lines.append(f"Ограничения: {profile['injuries']}")
    if profile.get("notes"):
        lines.append(f"От спортсмена: {profile['notes']}")

    return lines

def _week_days_str(week):
    """Строка тренировок недели по дням; пустые/отсутствующие дни пропускаются."""
    parts = [f"{label}={week.get(field)}" for field, label in PLAN_DAYS if week.get(field)]
    return "; ".join(parts) if parts else "(пусто)"

def format_compliance_block(compliance):
    """Выполнение плана по неделям (#41).

    Плановый объём подаётся как «не меньше N» там, где точнее из текста не
    вывести: ячейки с интервалами, временем или диапазоном. Модель не должна
    принимать нижнюю границу за план и делать вывод о недоборе, которого нет.
    """
    if not compliance or not compliance.get("weeks"):
        return []

    lines = ["Выполнение плана по неделям (план / факт):"]
    for week in compliance["weeks"]:
        planned = week["planned_km"]
        if week["complete"]:
            plan_str = f"план {planned:g} км"
            tail = f", {week['pct']}%" if week.get("pct") is not None else ""
        else:
            plan_str = (f"план не меньше {planned:g} км" if planned
                        else "плановый объём из текста не вывести")
            tail = ""
        parts = [f"  - неделя {week['idx'] + 1} ({week['start']}–{week['end']}): "
                 f"{plan_str}, факт {week['actual_km']:g} км{tail}"]
        marks = []
        if week["missed"]:
            marks.append(f"пропущено дней: {week['missed']}")
        if week["extra"]:
            marks.append(f"вне плана: {week['extra']}")
        if marks:
            parts.append("; " + ", ".join(marks))
        lines.append("".join(parts))

    if not all(w["complete"] for w in compliance["weeks"]):
        lines.append("  (там, где сказано «не меньше», точный плановый объём "
                     "из текста плана не выводится — не считай это недобором)")
    return lines


def format_context_for_llm(ctx):
    """Превращает контекст в текстовый user prompt."""
    lines = []
    profile_block = format_profile_block(ctx.get("profile") or {},
                                         ctx.get("profile_derived") or {},
                                         ctx.get("personal_bests") or [])
    if profile_block:
        lines.extend(profile_block)
        lines.append("")

    race = ctx.get("race") or {}
    goal_bits = [b for b in [race.get("race_name"), race.get("race_date")] if b]
    if race.get("target_time"):
        goal_bits.append(f"цель {race['target_time']}")
    lines.append("Цель: " + (", ".join(goal_bits) if goal_bits else "не задана"))
    lines.append(f"Сегодня: {datetime.utcnow().date().isoformat()}")
    total = ctx.get("weeks_total") or 0
    lines.append(f"Текущая неделя плана: {ctx['week_idx'] + 1}"
                 + (f" из {total}" if total else ""))

    cw = ctx.get("current_week")
    if cw:
        phase = PLAN_PHASE_LABELS.get(cw.get("type"), cw.get("type"))
        lines.append(f"Фаза: {phase} — {cw.get('accent', '')}")
        lines.append("План текущей недели:")
        lines.append("  " + _week_days_str(cw))
    nw = ctx.get("next_week")
    if nw:
        lines.append("План следующей недели:")
        lines.append("  " + _week_days_str(nw))

    compliance_block = format_compliance_block(ctx.get("compliance"))
    if compliance_block:
        lines.append("")
        lines.extend(compliance_block)

    lines.append("")
    lines.append("Последние 14 тренировок (сначала свежие):")
    for r in ctx["last_runs"]:
        t = TYPE_LABELS.get(r.get("type"), r.get("type", ""))
        feel = FEEL_LABELS.get(r.get("feel"), "")
        parts = [r.get("date", "?"), t, f"{r.get('dist', '?')}км"]
        if r.get("time"): parts.append(r["time"])
        if r.get("pace"): parts.append(f"темп {r['pace']}/км")
        if r.get("hr"): parts.append(f"пульс ср.{r['hr']}")
        if r.get("max_hr"): parts.append(f"макс {r['max_hr']}")
        if r.get("avg_cadence"): parts.append(f"каденс {r['avg_cadence']}")
        if r.get("total_ascent_m"): parts.append(f"набор {r['total_ascent_m']}м")
        if feel: parts.append(f"ощ:{feel}")
        line = "  - " + " ".join(parts)
        if r.get("_lap_paces"):
            line += f"\n    лапы: {r['_lap_paces']}"
        if r.get("_hr_drift_pct") is not None:
            d = r["_hr_drift_pct"]
            sign = "+" if d >= 0 else ""
            line += f"\n    HR-drift: {sign}{d}% (изменение среднего пульса 1-я→2-я половина)"
        if r.get("notes"):
            line += f"\n    заметки: {r['notes']}"
        lines.append(line)

    if ctx["last_races"]:
        lines.append("")
        lines.append("Последние забеги:")
        for race in ctx["last_races"]:
            label = race.get("dist_label", "")
            km = DIST_LABEL_KM.get(label, "?")
            lines.append(f"  - {race.get('date', '?')} {race.get('name', '?')} {km}км {race.get('time', '?')}")

    h = ctx["heuristics"]
    lines.append("")
    lines.append("Эвристики:")
    if h["avg_pace_min_per_km"] is not None:
        ap = h["avg_pace_min_per_km"]
        m = int(ap); s = round((ap - m) * 60)
        lines.append(f"  - средний темп за 14 тренировок: {m}:{s:02d}/км")
    lines.append(f"  - тяжёлых/плохих тренировок: {h['hard_or_bad_count']}")
    lines.append(f"  - суммарно: {h['total_km_last_14']} км")

    return "\n".join(lines)

# ── Диалог с ИИ-тренером (#46) ────────────────────────────────────────────────

COACH_CHAT_SYSTEM_PROMPT = """Ты опытный беговой тренер. Ведёшь диалог со спортсменом: разбираешь его тренировки, отвечаешь на вопросы и помогаешь корректировать план подготовки к целевому старту.

Правила:
- Опирайся на данные спортсмена ниже. Если для вывода данных не хватает — скажи об этом прямо и спроси недостающее, не додумывай цифры.
- Учитывай профиль: возраст, пульсовые показатели, ограничения по здоровью и дни, доступные для тренировок. Не предлагай тренировки в недоступные дни. Оценочные значения помечены явно — не выдавай их за измеренные.
- Ты не врач и диагнозов не ставишь. При жалобах на боль или тревожных симптомах советуй снизить нагрузку и обратиться к специалисту.
- Отвечай на заданный вопрос и конкретно: темпы, пульс, километры, дни недели. Не пересказывай данные, которые спортсмен и так видит.
- Разбирая тренировку, сравни её с тем, что стояло в плане на этот день, оцени раскладку темпа по кругам, дрейф пульса и время в зонах, и скажи, что из этого следует для ближайших тренировок. Если детальных данных по тренировке нет — разбирай по сводке и скажи, чего не хватает для точного вывода.
- Пиши по-русски и коротко: обычно 3–8 предложений или короткий список. Для выделения годятся **жирный** и списки с «- »."""

COACH_CHAT_REPLY_FORMAT = """Формат ответа — СТРОГО JSON без текста до или после:
{"reply": "текст ответа спортсмену"}"""

# Формат с правками плана. Используется, только когда модели показана таблица
# «План: что можно менять» — без неё адресовать правку нечем.
COACH_CHAT_PROPOSAL_FORMAT = """Формат ответа — СТРОГО JSON без текста до или после:
{"reply": "текст ответа спортсмену", "proposal": null}

Когда нужно изменить план, вместо null передай правки:
{"reply": "...", "proposal": {"summary": "что меняется и зачем, одной фразой", "changes": [{"week": 6, "day": "wed", "text": "новый текст ячейки", "reason": "почему"}]}}

Правила для proposal:
- Заполняй его, только когда спортсмен просит изменить план либо изменение явно необходимо (травма, перегруз, пропущена ключевая тренировка). На обычный вопрос или разбор тренировки — null.
- week — номер недели, day — код дня (mon, tue, wed, thu, fri, sat, sun) из таблицы «План: что можно менять». Менять можно только дни этой таблицы, не помеченные как прошедшие.
- text — новое содержимое ячейки целиком, в том же стиле, что остальной план. Пустая строка — день отдыха.
- Не больше 14 правок. В reply объясни их словами: спортсмен увидит карточку «было → стало» и сам решит, применять ли. Не пиши, что план уже изменён."""


def format_plan_window(window):
    """Таблица недель, которые модель может править (#46).

    На вход — список из storage.ai_plan_window. Дни идут по датам, а не
    пн→вс: у строки вс→сб воскресенье — первый день, и модель не должна
    считать его концом недели.
    """
    day_ru = dict(PLAN_DAYS)
    lines = ["=== План: что можно менять ==="]
    for week in window:
        phase = PLAN_PHASE_LABELS.get(week.get("phase"), week.get("phase") or "")
        head = f"Неделя {week['week']}" + (" (текущая)" if week.get("current") else "")
        head += f", {week['start']} – {week['end']}" + (f", {phase}" if phase else "")
        lines.append(head)
        for day in week["days"]:
            past = " (прошёл, менять нельзя)" if day["past"] else ""
            lines.append(f"  {day['field']} ({day_ru[day['field']]} {day['date']}): "
                         f"{day['text'] or '—'}{past}")
    return "\n".join(lines)

FOCUS_LAPS_MAX = 45           # марафон на километровом автокруге помещается целиком


def _mmss(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _lap_line(lap):
    parts = [f"{lap.get('dist_km', 0):g} км"]
    if lap.get("duration_sec"):
        parts.append(f"за {_mmss(lap['duration_sec'])}")
    if lap.get("pace"):
        parts.append(f"темп {lap['pace']}/км")
    if lap.get("avg_hr"):
        hr = f"пульс {lap['avg_hr']}"
        if lap.get("max_hr"):
            hr += f"/{lap['max_hr']}"
        parts.append(hr)
    if lap.get("cadence"):
        parts.append(f"каденс {lap['cadence']}")
    if lap.get("ascent_m"):
        parts.append(f"набор {lap['ascent_m']} м")
    return f"  {lap.get('lap', '?')}: " + ", ".join(parts)


def format_run_focus(focus):
    """Подробный блок одной пробежки для разбора (#46).

    На вход — словарь из storage.build_run_focus. Сырые посекундные ряды сюда
    не попадают: из них заранее посчитаны дрейф, половины и зоны.
    """
    run = focus["run"]
    kind = TYPE_LABELS.get(run.get("type"), run.get("type") or "тренировка")
    lines = [f"--- Тренировка {run.get('date', '?')}, {kind} ---"]

    summary = [f"{run.get('dist', '?')} км"]
    if run.get("time"):
        summary.append(f"за {run['time']}")
    if run.get("pace"):
        summary.append(f"темп {run['pace']}/км")
    if run.get("hr"):
        summary.append(f"пульс ср. {run['hr']}" + (f", макс {run['max_hr']}" if run.get("max_hr") else ""))
    if run.get("avg_cadence"):
        summary.append(f"каденс {run['avg_cadence']}")
    if run.get("total_ascent_m"):
        summary.append(f"набор {run['total_ascent_m']} м")
    lines.append("Сводка: " + ", ".join(summary))
    if FEEL_LABELS.get(run.get("feel")):
        lines.append(f"Ощущения спортсмена: {FEEL_LABELS[run['feel']]}")
    if run.get("notes"):
        lines.append(f"Заметки спортсмена: {run['notes']}")

    planned = focus.get("planned")
    if planned:
        phase = PLAN_PHASE_LABELS.get(planned.get("phase"), planned.get("phase") or "")
        where = f"неделя {planned['week']}" + (f", {phase}" if phase else "")
        lines.append(f"По плану в этот день ({where}): «{planned['text']}»" if planned["text"]
                     else f"По плану в этот день ({where}) тренировки не было")
    else:
        lines.append("Что стояло в плане на этот день — неизвестно (дата вне плана "
                     "или план без дат)")

    laps = focus.get("laps") or []
    if laps:
        lines.append("Круги (№: дистанция, время, темп, пульс ср./макс, каденс):")
        lines.extend(_lap_line(lap) for lap in laps[:FOCUS_LAPS_MAX])
        if len(laps) > FOCUS_LAPS_MAX:
            lines.append(f"  … и ещё {len(laps) - FOCUS_LAPS_MAX}")
    if focus.get("half_paces"):
        first, second = focus["half_paces"]
        lines.append(f"Темп по половинам (по кругам): {_mmss(first)} → {_mmss(second)}/км")
    if focus.get("hr_drift_pct") is not None:
        drift = focus["hr_drift_pct"]
        lines.append(f"HR-drift: {'+' if drift >= 0 else ''}{drift}% "
                     "(средний пульс второй половины к первой)")
    if focus.get("zones"):
        basis = f"макс. пульс {focus.get('hr_max')}" + (
            " — оценка по возрасту, не измерялся" if focus.get("hr_max_estimated") else "")
        lines.append(f"Время в пульсовых зонах ({basis}): " + "; ".join(
            f"{z['name']} — {z['min']:g} мин ({z['pct']}%)" for z in focus["zones"]))
    if not laps and not focus.get("zones"):
        lines.append("Детальных данных (кругов, пульса по времени) нет — только сводка.")
    return "\n".join(lines)


def coach_chat_instructions(with_proposals):
    """Неизменная часть системного промпта: роль, правила и формат ответа.

    Формат с правками плана даётся, только когда модели показана таблица
    недель, открытых для правок: без неё адресовать правку нечем.
    """
    return "\n\n".join([
        COACH_CHAT_SYSTEM_PROMPT,
        COACH_CHAT_PROPOSAL_FORMAT if with_proposals else COACH_CHAT_REPLY_FORMAT])


def coach_chat_data(context_text, focus_blocks=(), plan_window_text=""):
    """Данные спортсмена для системного промпта хода.

    Они идут в системную часть, а не репликой: так история диалога остаётся
    чистым чередованием вопросов и ответов, а контекст каждый ход
    подставляется актуальный, не тот, что был на момент первого вопроса.

    focus_blocks — подробные блоки пробежек, которые спортсмен выбрал для
    разбора; последняя в списке прикреплена позже всех.
    """
    parts = ["=== Данные спортсмена на момент сообщения ===\n" + context_text]
    if plan_window_text:
        parts.append(plan_window_text)
    if focus_blocks:
        parts.append(
            "=== Тренировки, выбранные спортсменом для разбора ===\n"
            "Разговор идёт о них; остальные данные — фон. Если выбрано несколько, "
            "последняя в списке прикреплена позже всех.\n\n" + "\n\n".join(focus_blocks))
    return "\n\n".join(parts)
