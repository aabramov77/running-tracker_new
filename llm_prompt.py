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

SYSTEM_PROMPT = """Ты опытный беговой тренер. Анализируешь данные тренировок бегуна, готовящегося к целевому старту (дистанция и цель указаны в данных).

Если в данных есть профиль спортсмена — учитывай возраст, пульсовые показатели, ограничения по здоровью и дни, доступные для тренировок. Не предлагай тренировки в недоступные дни. Оценочные значения помечены явно — не выдавай их за измеренные.

Дай рекомендации СТРОГО в JSON-формате без лишнего текста до или после:
{
  "assessment": "1-2 предложения общей оценки прогресса",
  "adjustments": [
    {"day": "среда", "change": "конкретное предложение по корректировке"}
  ],
  "warnings": ["предупреждение если есть риски"]
}

Если корректировок не нужно — пустой массив adjustments. Если предупреждений нет — пустой массив warnings.
Отвечай на русском языке."""
