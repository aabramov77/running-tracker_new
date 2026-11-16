"""Сборка текстового промпта для LLM из подготовленного контекста.

Выделено из main.py (#36, фаза 1). Модуль ничего не читает из GCS: на вход
приходит готовый словарь контекста, на выходе — текст. Это самая часто
изменяемая часть кода вокруг ИИ, и держать её отдельно дешевле.
"""
from datetime import datetime

from domain import (DIST_LABEL_KM, FEEL_LABELS, MEMORY_FACTS_MAX, MEMORY_KIND_LABELS,
                    MEMORY_OPS_MAX, MEMORY_TEXT_MAX, PLAN_DAYS, PLAN_PHASE_LABELS,
                    SEX_LABELS, TYPE_LABELS)
from weather import deg

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

def _mmss(seconds):
    seconds = int(round(seconds))
    return f"{seconds // 60}:{seconds % 60:02d}"


def _threshold_line(derived):
    """Пороговый темп (#66): заданный или оценка по забегу. Время забега
    собрано заново из секунд (load.estimate_threshold), а не взято строкой
    из «Стартов» — текст из поля времени в промпт не попадает."""
    pace = derived.get("threshold_pace_sec")
    if not pace:
        return None
    if derived.get("threshold_source") == "manual":
        return f"Пороговый темп: {_mmss(pace)}/км (задан спортсменом)"
    basis = derived.get("threshold_basis") or {}
    km = DIST_LABEL_KM.get(basis.get("dist_label"))
    if not km:
        return f"Пороговый темп: ~{_mmss(pace)}/км (оценка)"
    return (f"Пороговый темп: ~{_mmss(pace)}/км (оценка по забегу {km:g} км за "
            f"{basis.get('time')} от {basis.get('date')})")


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
    # Расчётные зоны не печатаем — в промпте они только именами. Ручные (#56)
    # спортсмен задал сам, и тренировать его надо по ним.
    if derived.get("hr_zones_source") == "manual":
        lines.append("Пульсовые зоны (заданы спортсменом): " + ", ".join(
            z["name"].split()[0] + " " + (f"{z['from']}–{z['to']}" if z.get("to") else f"от {z['from']}")
            for z in derived.get("hr_zones") or []))
    if profile.get("vo2max"):
        lines.append(f"МПК: {profile['vo2max']:g}")
    threshold = _threshold_line(derived)
    if threshold:
        lines.append(threshold)

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

    # Погода (#72): где ещё можно бегать и когда. Город и координаты в промпт
    # не идут — модели хватает прогноза.
    conditions = []
    venues = [VENUE_RU[v] for v in profile.get("venues") or [] if v in VENUE_RU]
    if venues:
        conditions.append("кроме улицы — " + ", ".join(venues))
    if profile.get("run_time"):
        conditions.append(f"обычно бегает в {profile['run_time']}")
    if profile.get("cold_sensitivity") in COLD_RU:
        conditions.append(COLD_RU[profile["cold_sensitivity"]])
    if conditions:
        lines.append("Условия: " + "; ".join(conditions))

    if bests:
        lines.append("Личные рекорды: " + ", ".join(
            f"{b['km']:g} км {b['time']}" + (f" ({b['date']})" if b.get("date") else "")
            for b in bests))

    if profile.get("injuries"):
        lines.append(f"Ограничения: {profile['injuries']}")
    if profile.get("notes"):
        lines.append(f"От спортсмена: {profile['notes']}")

    return lines

VENUE_RU = {"treadmill": "дорожка в зале", "manege": "манеж"}
COLD_RU = {"cold": "мёрзнет быстрее других", "warm": "быстро перегревается"}


def format_weather_block(weather):
    """Прогноз к дням плана (#72): погода в час тренировки и совет по правилам.

    Совет — не приказ модели: она видит, что предлагают правила, и может
    объяснить или поправить это в ответе. Без прогноза блока нет.
    """
    if not weather or weather.get("status") != "ok" or not weather.get("days"):
        return []
    day_ru = dict(PLAN_DAYS)
    lines = [f"Прогноз погоды (в {weather.get('run_time')}, по месту бега):"]
    for day in weather["days"]:
        w, r = day["weather"], day["weather"]["at_run"]
        parts = [f"{w['summary']}, {deg(r['t'])} (ощущается {deg(r['feels'])})",
                 f"ветер {r['wind']} м/с"]
        if r.get("precip_mm"):
            parts.append(f"осадки {r['precip_mm']:g} мм")
        when = f"{day_ru.get(day['field'], day['field'])} {day['date'][8:]}.{day['date'][5:7]}"
        line = f"  - {when}: " + ", ".join(parts)
        if day.get("workout"):
            line += f"; в плане «{day['workout']}»"
        venue = day.get("venue")
        if venue:
            line += f" → {venue['label']}" + (f" ({venue['why']})" if venue.get("why") else "")
        elif day.get("level") != "ok" and day.get("why"):
            line += f"; {day['why']}"
        lines.append(line)
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


LOAD_BAND_RU = {"low": "ниже обычного", "ok": "в норме", "high": "выше обычного",
                "spike": "резкий рост"}
COST_BAND_RU = {"usual": "как обычно",
                "higher": "дороже обычного: пульс выше при той же работе",
                "lower": "дешевле обычного: пульс ниже при той же работе"}
INTERNAL_METHOD_RU = {"banister": "по пульсу, метод Банистера",
                      "edwards": "по пульсу, по зонам (метод Эдвардса)"}
LOAD_BLOCK_WEEKS = 5            # 4 полные недели и текущая


def _ratio(ratio):
    return f"×{ratio:.2f}".replace(".", ",")


def _load_axis(name, axis, rated, first=True):
    if axis["unknown"]:
        count = axis["unknown"]
        text = (f"{name} не меньше {axis['load']} ({count} "
                f"{plural_ru(count, 'пробежка', 'пробежки', 'пробежек')} без данных не в счёт)")
    else:
        text = f"{name} {axis['load']}"
    if rated:
        if axis.get("ratio") is not None:
            base = " к средней за 4 недели" if first else ""
            text += f" — {_ratio(axis['ratio'])}{base} ({LOAD_BAND_RU[axis['band']]})"
        else:
            text += " — сравнивать не с чем: мало истории"
    return text


def _intensity(period):
    split = [period.get(key) for key in ("easy", "moderate", "hard")]
    total = sum(m or 0 for m in split)
    if None in split or total <= 0:
        return ""
    easy, moderate, hard = (round(m / total * 100) for m in split)
    return f"лёгкая {easy}% / средняя {moderate}% / высокая {hard}%"


def _load_period(head, period, rated):
    present = [(key, name) for key, name in (("ext", "rTSS"), ("int", "TRIMP")) if period.get(key)]
    axes = [_load_axis(name, period[key], rated, first=i == 0)
            for i, (key, name) in enumerate(present)]
    line = f"{head}: " + (", ".join(axes) if axes else "нет данных")
    split = _intensity(period)
    return line + (f"; {split}" if split else "")


def format_load_block(load):
    """Нагрузка по двум осям (#66): 4 полные недели, текущая и 7 дней.

    Только числа и подписи, которые собрал сервер. Модели прямо сказано, где
    число — нижняя граница, где сравнивать не с чем и какой оси нет: иначе
    она сделает вывод о недогрузе, которого нет.
    """
    if load is None:
        return ["Нагрузка: посчитать не удалось."]
    internal = (load.get("internal") or {}).get("method")
    external = (load.get("external") or {}).get("method")
    if not internal and not external:
        return ["Нагрузка: не считается — нет ни пульсовых зон, ни порогового темпа."]

    scales = []
    if external:
        scales.append("rTSS — по темпу с поправкой на уклон, 100 = час на пороге")
    if internal:
        scales.append("TRIMP — " + INTERNAL_METHOD_RU.get(internal, "по пульсу"))
    lines = [f"Нагрузка (все пробежки; {'; '.join(scales)}):"]
    if not external:
        lines.append("  (нагрузки по темпу нет: не задан пороговый темп и нет результатов забегов)")
    if not internal:
        lines.append("  (нагрузки по пульсу нет: нет пульсовых зон)")

    for week in (load.get("weeks") or [])[-LOAD_BLOCK_WEEKS:]:
        partial = week.get("partial")
        head = f"  - {week['start']}–{week['end']}" + (" (текущая, неполная)" if partial else "")
        lines.append(_load_period(head, week, rated=not partial))

    rolling = load.get("rolling")
    if rolling:
        lines.append(_load_period(f"  - последние 7 дней ({rolling['start']}–{rolling['end']})",
                                  rolling, rated=True))
        cost = rolling.get("cost")
        if cost:
            sign = "+" if cost["dev_pct"] > 0 else ""
            lines.append(f"  - цена работы на лёгких и длительных за 7 дней: {sign}{cost['dev_pct']}% "
                         f"к обычной ({COST_BAND_RU[cost['band']]})")
    if load.get("pending"):
        pending = load["pending"]
        lines.append(f"  (по {pending} {plural_ru(pending, 'пробежке', 'пробежкам', 'пробежкам')} "
                     "с файлом часов пока оценка по средним — посекундные данные ещё считаются)")
    return lines


def _hms(seconds):
    seconds = int(round(seconds))
    hours, rest = divmod(seconds, 3600)
    return f"{hours}:{rest // 60:02d}:{rest % 60:02d}" if hours else _mmss(seconds)


def _history_month(row):
    head = f"  - {row['month']}" + (" (текущий, неполный)" if row["partial"] else "")
    if not row["runs"]:
        return f"{head}: нет пробежек"
    runs = row["runs"]
    parts = [f"{row['km']:g} км, {runs} {plural_ru(runs, 'пробежка', 'пробежки', 'пробежек')}, "
             f"самая длинная {row['longest_km']:g} км"]
    easy = []
    if row["easy_pace_sec"]:
        easy.append(f"темп {_mmss(row['easy_pace_sec'])}/км")
    if row["easy_hr"]:
        easy.append(f"пульс {row['easy_hr']}")
    if easy:
        parts.append("лёгкие: " + ", ".join(easy))
    load = []
    for key, name in (("ext", "rTSS"), ("int", "TRIMP")):
        unknown = row[key + "_unknown"]
        if row[key] is None or unknown == runs:
            continue
        load.append(f"{name} {'не меньше ' if unknown else ''}{row[key]}")
    if load:
        parts.append(", ".join(load))
    if row["hard"]:
        parts.append(f"тяжело или плохо: {row['hard']}")
    return f"{head}: " + "; ".join(parts)


def _history_plan(plan):
    head = f"  - «{plan['race_name'] or 'без названия'}», старт {plan['race_date']}"
    if plan["target_time"]:
        head += f", цель {plan['target_time']}"
    totals = plan["compliance"]
    if totals is None:
        done = "выполнение не посчитать: план без дат" if plan["weeks"] else "недель в плане нет"
    elif totals["pct"] is not None:
        done = f"выполнение {totals['pct']}% ({totals['actual_km']:g} из {totals['planned_km']:g} км)"
    else:
        done = (f"факт {totals['actual_km']:g} км, план не меньше {totals['planned_km']:g} км "
                "(точнее из текста плана не вывести)")
    result = plan["result"]
    if result and result.get("seconds"):
        km = DIST_LABEL_KM.get(result.get("dist_label"))
        outcome = f"результат {_hms(result['seconds'])}" + (f" на {round(km, 1):g} км" if km else "") \
            + f" ({result['date']})"
    else:
        outcome = "результата в «Стартах» нет"
    return f"{head} — {done}; {outcome}"


def format_history_block(history):
    """Долгая история (#81): месяцы за год и итоги прошлых планов.

    Фон, а не основание для решений на неделю: ближайшие недели модель
    видит в блоках нагрузки и выполнения. Нагрузка месяца — «не меньше»,
    если у части пробежек её не посчитать, как в блоке нагрузки.
    """
    if history is None:
        return ["История: посчитать не удалось."]
    months = history.get("months") or []
    if not months:
        lines = ["История по месяцам: пробежек нет."]
    else:
        lines = ["История по месяцам (все пробежки, до 12 месяцев; месяцы до первой "
                 "пробежки в приложении не показаны):"]
        lines.extend(_history_month(row) for row in months)
    plans = history.get("plans") or []
    if plans:
        lines.append("Прошлые планы (последние, с гонкой в прошлом):")
        lines.extend(_history_plan(plan) for plan in plans)
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

    # Контекст, собранный до #72, ключа weather не знает — блока тогда нет.
    weather_block = format_weather_block(ctx.get("weather"))
    if weather_block:
        lines.append("")
        lines.extend(weather_block)

    compliance_block = format_compliance_block(ctx.get("compliance"))
    if compliance_block:
        lines.append("")
        lines.extend(compliance_block)

    # Контекст, собранный до #66, ключа load не знает — блока тогда нет вовсе.
    if "load" in ctx:
        lines.append("")
        lines.extend(format_load_block(ctx["load"]))
    # И история (#81) — так же: в старом контексте её нет вовсе.
    if "history" in ctx:
        lines.append("")
        lines.extend(format_history_block(ctx["history"]))

    lines.append("")
    lines.append("Последние 14 тренировок по всем планам и вне плана (сначала свежие, "
                 "в скобках — к какому плану относится):")
    for r in ctx["last_runs"]:
        t = TYPE_LABELS.get(r.get("type"), r.get("type", ""))
        feel = FEEL_LABELS.get(r.get("feel"), "")
        parts = [r.get("date", "?"), t, f"{r.get('dist', '?')}км"]
        if r.get("_plan"): parts.append(f"[{r['_plan']}]")
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
        lines.append("Забеги за 2 года (сначала свежие):")
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

# Инструкция общая для всех спортсменов: ничего личного здесь быть не должно,
# профиль, цель и план приходят блоком данных (coach_chat_data). Показатели
# тренировок перечислены те, что реально собирает build_llm_context, — иначе
# модель станет требовать HRV и прочее, чего в данных нет.
#
# Инструкция собрана из частей (#69): роль, стиль, подход, правила и память
# общие для обычного разбора и разбора нового плана, а задача, разделы о
# разборе тренировки и составлении плана и «Как писать» — свои у каждого.
COACH_ROLE = "Ты опытный беговой тренер, специалист по длинным дистанциям."

_COACH_CHAT_TASK = ("Ведёшь диалог со спортсменом: разбираешь его тренировки, отвечаешь на "
                    "вопросы и помогаешь корректировать план подготовки к целевому старту. "
                    "Дистанцию, цель и уровень спортсмена бери из его данных ниже.")

_COACH_CORE = """Стиль:
- Спокойный, уверенный, конкретный — как тренер, который понимает физиологию бега. Без мотивационной воды, сюсюканья и формальной похвалы.
- Тренировка выполнена плохо — объясни почему. Нагрузка слишком высокая или есть риск перегруза — скажи прямо. Прогресс хороший — отметь это без лишнего восторга.

Подход:
- Периодизация: база, развитие порога, МПК, специфическая подготовка, подводка к старту. Учитывай текущую фазу плана и сколько осталось до старта.
- Интенсивность: лёгкий и аэробный бег, темп, порог, интервалы, длительная.
- Смотри на общую картину: тренд, восстановление, соответствие цели. Не делай выводов по одному показателю.
- Главный приоритет — долгосрочный прогресс без травм. Риск травмы, накопленное утомление или неадекватная нагрузка важнее амбиций по результату.
- Нагрузку читай по двум осям: rTSS по темпу — сколько работы сделано, TRIMP по пульсу — чего она стоила организму. Отношение к обычной оценивай с учётом фазы плана: на разгрузке и подводке «ниже обычного» — норма, а резкий рост — повод для осторожности, а не для похвалы. Выросла цена работы (пульс выше при той же работе) — спроси про сон, жару, болезнь и самочувствие, а не добавляй нагрузку.
- История по месяцам и итоги прошлых планов — фон: по ним видно тренд объёма и аэробной формы, перерывы и как прошли прошлые подготовки. Решения на ближайшие недели принимай по нагрузке и выполнению плана, а не по годовым цифрам.

Правила:
- Опирайся на данные спортсмена ниже. Если для вывода данных не хватает — скажи об этом прямо и спроси недостающее, не додумывай цифры. То, что в данных уже есть, не переспрашивай.
- Учитывай профиль: возраст, пульсовые показатели, ограничения по здоровью и дни, доступные для тренировок. Не предлагай тренировки в недоступные дни. Оценочные значения помечены явно — не выдавай их за измеренные.
- По тренировкам у тебя есть темп, средний и максимальный пульс, каденс, набор высоты, круги, дрейф пульса, время в пульсовых зонах, недельная нагрузка по темпу (rTSS) и по пульсу (TRIMP) с отношением к обычной, цена работы, помесячная история за год, ощущения и заметки спортсмена. Других показателей часов (HRV, training effect, время восстановления, готовность к тренировке и т. п.) в данных нет: учитывай их, только если спортсмен сам о них написал, и не требуй их.
- Жару, ветер, недосып и стресс учитывай по заметкам спортсмена, рельеф — по набору высоты. Если тренировка вышла заметно хуже ожидаемого без видимой причины — спроси о них.
- Прогноз погоды на ближайшие дни, если он есть в данных, учитывай в советах о месте и времени тренировки. При опасной погоде (гроза, гололёд, сильный мороз или жара) предложи провести тренировку в помещении или переставить её на другой день — с учётом доступных дней.
- Ты не врач и диагнозов не ставишь. При жалобах на боль или тревожных симптомах советуй снизить нагрузку и обратиться к специалисту.
- Отвечай на заданный вопрос и конкретно: темпы, пульс, километры, дни недели. Без шаблонных фраз. Не пересказывай данные, которые спортсмен и так видит.
- Каждую корректировку объясняй: почему именно она. Сорванную или выполненную хуже плана тренировку не компенсируй дополнительной нагрузкой."""

_COACH_CHAT_SECTIONS = """Разбор тренировки:
- Сравни её с задачей дня по плану. Оцени раскладку темпа по кругам, стабильность отрезков, дрейф пульса, время в зонах и признаки усталости.
- Скажи, что получилось, а что нет, и что из этого следует для ближайших 3–7 дней.
- Если детальных данных нет — разбирай по сводке и скажи, чего не хватает для точного вывода.

Просьба составить новый план:
- Новый план целиком составляется в отдельном разборе «Новый план»: спортсмен открывает его кнопкой «🤖 Составить с ИИ» на вкладке «План» или подсказкой «Составить новый план» в ИИ-тренере. Там вы по шагам согласуете цель, каркас и недели, а план создаётся из готового черновика.
- Здесь коротко опиши подход — фазы, ключевые тренировки и их цель, на что обратить внимание — и предложи открыть разбор «Новый план». План по неделям в этом ответе не расписывай.
- Текущий план в этом диалоге не заменяется: правки возможны только в ближайших неделях и только так, как описано в формате ответа."""

_COACH_MEMORY = """Память:
- Блок «Что ты помнишь о спортсмене» в данных ниже — твоя память, общая для всех разборов. Опирайся на неё так же, как на остальные данные, и не переспрашивай то, что в ней уже есть.
- Запоминай то, что пригодится в следующих разговорах и чего нет в данных приложения: здоровье и самочувствие (боль, травмы, болезни), обстоятельства (отпуск, командировки, смена графика, где бегает), предпочтения (что любит и чего не любит, когда удобно тренироваться), ваши договорённости и решения.
- Не записывай то, что уже есть в данных: план, пробежки, профиль, рекорды. Не записывай оценок личности: спортсмен видит свою память и может её править.
- Пиши коротко, одной фразой, с датами, если они важны: «С 10 по 20 ноября в отпуске, бегать сможет только на дорожке».
- Факт изменился — уточни его, устарел или не подтвердился — забудь. Память почти заполнена — объединяй похожие факты.
- Говори, что запомнил, только если действительно передал это в memory.

Память о разговорах:
- В summary каждого ответа пиши краткое содержание всего этого разбора на сейчас, вместе с этим ответом: о чём спрашивал спортсмен, что ты посоветовал, о чём договорились, что осталось открытым. 3–6 предложений, до 800 символов, от третьего лица, с цифрами и датами. В других разборах ты увидишь этот разговор только по нему.
- Блок «Прошлые разборы» — краткое содержание недавних разговоров со спортсменом. Опирайся на него, когда тема продолжается или спортсмен ссылается на прошлый разговор. Нужных деталей там нет — скажи об этом прямо, не додумывай.
- Если ранние реплики этого разбора не показаны, их краткое содержание — в блоке «Начало этого разбора»."""

_COACH_CHAT_WRITING = """Как писать:
- По-русски.
- Разбор тренировки и корректировку плана строй по разделам: **Вывод** (1–2 предложения), **Что видно по данным** (выводы из цифр, а не их пересказ), **Главная проблема или сильная сторона**, **Что делать дальше**, **Корректировка плана**. Раздел, которому нечего сказать, пропускай.
- На простой вопрос отвечай коротко и без разделов: обычно 3–8 предложений.
- Для выделения годятся **жирный** и списки с «- »."""

COACH_CHAT_SYSTEM_PROMPT = "\n\n".join([COACH_ROLE + " " + _COACH_CHAT_TASK, _COACH_CORE,
                                        _COACH_CHAT_SECTIONS, _COACH_MEMORY,
                                        _COACH_CHAT_WRITING])

COACH_CHAT_REPLY_FORMAT = """Формат ответа — СТРОГО JSON без текста до или после:
{"reply": "текст ответа спортсмену", "summary": "краткое содержание всего разбора", "memory": null}"""

# Формат с правками плана. Используется, только когда модели показана таблица
# «План: что можно менять» — без неё адресовать правку нечем.
COACH_CHAT_PROPOSAL_FORMAT = """Формат ответа — СТРОГО JSON без текста до или после:
{"reply": "текст ответа спортсмену", "summary": "краткое содержание всего разбора", "proposal": null, "memory": null}

Когда нужно изменить план, вместо null передай правки:
{"reply": "...", "proposal": {"summary": "что меняется и зачем, одной фразой", "changes": [{"week": 6, "day": "wed", "text": "новый текст ячейки", "reason": "почему"}]}}

Правила для proposal:
- Заполняй его, только когда спортсмен просит изменить план либо изменение явно необходимо (травма, перегруз, пропущена ключевая тренировка). На обычный вопрос или разбор тренировки — null.
- week — номер недели, day — код дня (mon, tue, wed, thu, fri, sat, sun) из таблицы «План: что можно менять». Менять можно только дни этой таблицы, не помеченные как прошедшие.
- text — новое содержимое ячейки целиком, в том же стиле, что остальной план. Пустая строка — день отдыха.
- Не больше 14 правок. В reply объясни их словами: спортсмен увидит карточку «было → стало» и сам решит, применять ли. Не пиши, что план уже изменён."""

# Правки памяти (#80) — в обоих форматах: память есть всегда, даже пустая.
COACH_CHAT_MEMORY_FORMAT = """Когда нужно изменить память, вместо null в "memory" передай изменения — любые из трёх списков:
{"memory": {"add": [{"kind": "health", "text": "факт одной фразой"}], "edit": [{"id": "m3", "text": "новый текст факта целиком"}], "forget": [{"id": "m5"}]}}

Правила для memory:
- kind — вид факта: %(kinds)s.
- id — номер факта из блока «Что ты помнишь о спортсмене»; других фактов нет.
- Не больше %(ops)d изменений за ответ. В памяти помещается %(facts)d фактов, факт — до %(text)d символов.
- На большинство сообщений memory — null.""" % {
    "kinds": ", ".join(f"{kind} ({label})" for kind, label in MEMORY_KIND_LABELS.items()),
    "ops": MEMORY_OPS_MAX, "facts": MEMORY_FACTS_MAX, "text": MEMORY_TEXT_MAX}


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
    if focus.get("unplanned"):
        lines.append("Пробежка вне плана: спортсмен не привязал её ни к одному плану, "
                     "сравнивать с планом не нужно")
    elif planned:
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
        basis = "границы заданы спортсменом" if focus.get("zones_manual") else (
            f"макс. пульс {focus.get('hr_max')}" + (
                " — оценка по возрасту, не измерялся" if focus.get("hr_max_estimated") else ""))
        lines.append(f"Время в пульсовых зонах ({basis}): " + "; ".join(
            f"{z['name']} — {z['min']:g} мин ({z['pct']}%)" for z in focus["zones"]))
    if not laps and not focus.get("zones"):
        lines.append("Детальных данных (кругов, пульса по времени) нет — только сводка.")
    return "\n".join(lines)


def format_memory_block(facts):
    """Память тренера (#80): активные факты с id, на которые ссылаются правки.

    Дата — когда факт записан или последний раз уточнён: по ней модель
    решает, не устарел ли он. Факты сгруппированы по виду.
    """
    order = list(MEMORY_KIND_LABELS)
    lines = ["=== Что ты помнишь о спортсмене (общая память всех разборов) ==="]
    for fact in sorted(facts, key=lambda f: order.index(f["kind"])):
        when = str(fact.get("updated_at") or fact.get("created_at") or "")[:10]
        lines.append(f"[{fact['id']}] {when}, {MEMORY_KIND_LABELS[fact['kind']]}: {fact['text']}")
    lines.append(f"(фактов {len(facts)} из {MEMORY_FACTS_MAX})" if facts else "Память пока пуста.")
    return "\n".join(lines)


def format_past_threads(threads):
    """Прошлые разборы (#82): название, дата и краткое содержание, сначала
    свежие. Нет прошлых разборов — блока нет."""
    if not threads:
        return ""
    lines = ["=== Прошлые разборы (кратко, сначала свежие) ==="]
    for thread in threads:
        head = f"«{thread['title']}», {str(thread.get('last_ts') or '')[:10]}"
        lines.append(f"- {head}: {thread['summary'] or 'краткого содержания нет'}")
    return "\n".join(lines)


def format_thread_start(summary):
    """Начало длинного разбора (#82), не попавшее в переписку для модели."""
    return ("=== Начало этого разбора ===\n"
            "Ранние реплики этого разбора не показаны. "
            + (f"Кратко о разборе: {summary}" if summary else "Краткого содержания нет."))


def coach_chat_instructions(with_proposals):
    """Неизменная часть системного промпта: роль, правила и формат ответа.

    Формат с правками плана даётся, только когда модели показана таблица
    недель, открытых для правок: без неё адресовать правку нечем. Правки
    памяти (#80) возможны всегда.
    """
    return "\n\n".join([
        COACH_CHAT_SYSTEM_PROMPT,
        COACH_CHAT_PROPOSAL_FORMAT if with_proposals else COACH_CHAT_REPLY_FORMAT,
        COACH_CHAT_MEMORY_FORMAT])


def coach_chat_data(context_text, focus_blocks=(), plan_window_text="", memory_text="",
                    past_text="", thread_start_text=""):
    """Данные спортсмена для системного промпта хода.

    Они идут в системную часть, а не репликой: так история диалога остаётся
    чистым чередованием вопросов и ответов, а контекст каждый ход
    подставляется актуальный, не тот, что был на момент первого вопроса.

    memory_text — память тренера (#80), сразу за данными; за ней прошлые
    разборы и начало этого, если оно не уместилось в переписку (#82).
    focus_blocks — подробные блоки пробежек, которые спортсмен выбрал для
    разбора; последняя в списке прикреплена позже всех.
    """
    parts = ["=== Данные спортсмена на момент сообщения ===\n" + context_text]
    for text in (memory_text, past_text, thread_start_text):
        if text:
            parts.append(text)
    if plan_window_text:
        parts.append(plan_window_text)
    if focus_blocks:
        parts.append(
            "=== Тренировки, выбранные спортсменом для разбора ===\n"
            "Разговор идёт о них; остальные данные — фон. Если выбрано несколько, "
            "последняя в списке прикреплена позже всех.\n\n" + "\n\n".join(focus_blocks))
    return "\n\n".join(parts)


# ── Новый план с ИИ (#69) ─────────────────────────────────────────────────────
# Разбор вида «plan»: модель согласует со спортсменом бриф и каркас, а недели
# расписывает отдельный запрос по каркасу (PLAN_WEEKS_PROMPT). Даты, число
# недель и день старта считает сервер — модель получает их готовыми.

_PLAN_BUILDER_TASK = ("Сейчас ты составляешь со спортсменом новый план подготовки — в отдельном "
                      "разборе «Новый план». Данные спортсмена ниже: профиль, тренировки, нагрузка, "
                      "история и его текущий план, если он есть. Новый план к текущему не "
                      "относится и его не правит.")

_PLAN_BUILDER_SECTIONS = """Как идёт составление плана:
1. Бриф. Выясни цель и ограничения: нужен план к старту (дата, дистанция, цель по времени или просто финишировать) или общий без старта (поддержка формы, аэробная база, возвращение после перерыва — и на сколько недель); с какого дня начать; сколько тренировок в неделю, в какие дни, день длительной; травмы и ограничения. Объём, рекорды, доступные дни и здоровье бери из данных и памяти — спрашивай только то, чего там нет. Несколько вопросов можно задать в одном ответе. Когда данных хватает — верни бриф (поле brief) и попроси спортсмена его проверить.
2. Каркас. Только после того как спортсмен подтвердил бриф — написал, что всё верно, или нажал кнопку подтверждения, — верни каркас (поле outline): по каждой неделе из блока «Календарь плана» тип, акцент, целевой объём в км и ключевые тренировки, плюс ориентиры темпа. В reply объясни логику: фазы, как растёт объём, где разгрузки и подводка, почему так.
3. Недели. По дням план в ответе не расписывай: когда спортсмен согласен с каркасом, он нажимает «Расписать по неделям», и недели составляются по каркасу отдельно.
- Спортсмен просит изменить бриф — верни новый бриф целиком, без каркаса: каркас строится под подтверждённый бриф. Просит изменить каркас — верни новый каркас целиком.
- План создаёт только спортсмен из готового черновика. Не пиши, что план создан или сохранён.

Цель и сроки:
- В блоке «Оценка цели» — сколько недель до старта, разумный минимум для дистанции и прогноз по последнему забегу. Если цель по времени заметно быстрее прогноза или недель меньше минимума — скажи прямо и предложи реалистичную цель, более поздний старт или цель «финишировать». Решает спортсмен.
- План длиннее 30 недель не составляется: если старт дальше — предложи начать позже.

Принципы плана:
- Периодизация под цель: база → развитие → пик → подводка → старт. У общего плана подводки и старта нет — структура под цель словами (поддержка формы, аэробная база, возвращение после перерыва).
- Объём растёт не больше чем на 10–15 % в неделю; каждая 3–4-я неделя — разгрузочная (тип load).
- Подводка: 1 неделя к 5–10 км, 2 недели к полумарафону, 2–3 недели к марафону.
- Длительная — не больше трети недельного объёма. Бо́льшая часть объёма — лёгкий бег, интенсивных тренировок 1–2 в неделю.
- Первая неделя — близко к тому, что спортсмен бегает сейчас (нагрузка и история в данных). После перерыва или травмы начинай ниже.
- Учитывай профиль, доступные дни, день длительной, травмы и то, что ты помнишь о спортсмене."""

_PLAN_BUILDER_WRITING = """Как писать:
- По-русски, коротко и по делу.
- Бриф и каркас спортсмен увидит карточками — не пересказывай их в reply целиком, объясни решения.
- Для выделения годятся **жирный** и списки с «- »."""

PLAN_BUILDER_SYSTEM_PROMPT = "\n\n".join([COACH_ROLE + " " + _PLAN_BUILDER_TASK, _COACH_CORE,
                                          _PLAN_BUILDER_SECTIONS, _COACH_MEMORY,
                                          _PLAN_BUILDER_WRITING])

PLAN_BUILDER_REPLY_FORMAT = """Формат ответа — СТРОГО JSON без текста до или после:
{"reply": "текст ответа спортсмену", "summary": "краткое содержание всего разбора", "brief": null, "outline": null, "memory": null}

Бриф — когда данных для него хватает, иначе null:
{"brief": {"type": "race", "title": "Весенний полумарафон", "start_date": "2026-11-23", "race_date": "2027-03-14", "distance_km": 21.1, "target_time": "1:45:00", "sessions_per_week": 4, "days": ["tue", "thu", "sat", "sun"], "long_run_day": "sun", "notes": "ограничения и пожелания одной строкой"}}

Правила для brief:
- type: race — план к старту, general — общий без старта. У general вместо race_date, distance_km и target_time — "weeks": число недель от 1 до 16.
- title — название плана: старт или цель («Аэробная база»).
- target_time — ч:мм:сс или мм:сс; пустая строка — цель финишировать.
- start_date — с какого дня начать, не раньше сегодня, лучше с понедельника; null — с ближайшего понедельника.
- sessions_per_week — тренировок в неделю, от 1 до 14.
- days — доступные дни (mon, tue, wed, thu, fri, sat, sun); пустой список — без ограничений. long_run_day — один из них или пустая строка.

Каркас — только к подтверждённому брифу, иначе null:
{"outline": {"weeks": [{"week": 1, "type": "dev", "accent": "втягивание, аэробная база", "km": 32, "key": "длительная 12 км, 6×200 м"}], "paces": [{"name": "лёгкий", "value": "5:50–6:20"}]}}

Правила для outline:
- Ровно одна запись на каждую неделю из блока «Календарь плана»; week — её номер.
- type — dev (развитие), peak (пик формы), taper (подводка), load (разгрузка), race (неделя старта).
- km — целевой объём недели в км. accent и key — коротко, до 80 символов.
- paces — до 8 ориентиров темпа под этот план."""


def plan_builder_instructions():
    """Неизменная часть системного промпта разбора «Новый план»: правок
    текущего плана здесь нет, поэтому и формата proposal нет."""
    return "\n\n".join([PLAN_BUILDER_SYSTEM_PROMPT, PLAN_BUILDER_REPLY_FORMAT,
                        COACH_CHAT_MEMORY_FORMAT])


def _ddmm(iso):
    return f"{iso[8:10]}.{iso[5:7]}" if isinstance(iso, str) and len(iso) >= 10 else str(iso)


def _days_ru(days):
    names = dict(PLAN_DAYS)
    return ", ".join(names[d] for d in days)


def format_brief_block(brief, calendar):
    """Принятый бриф и календарь недель, посчитанный по нему сервером."""
    lines = ["=== Бриф нового плана (последний принятый) ===",
             "Тип: " + ("к старту" if brief["type"] == "race" else "общий, без старта"),
             f"Название: {brief['title']}"]
    if brief["type"] == "race":
        goal = f"цель {brief['target_time']}" if brief.get("target_time") else "цель — финишировать"
        lines.append(f"Старт: {brief['race_date']}, {brief['distance_km']:g} км, {goal}")
    lines.append(f"Начало: {brief['start_date']}")
    lines.append(f"Тренировок в неделю: {brief['sessions_per_week']}; дни: "
                 + (_days_ru(brief["days"]) if brief["days"] else "без ограничений")
                 + (f"; длительная: {dict(PLAN_DAYS)[brief['long_run_day']]}"
                    if brief.get("long_run_day") else ""))
    if brief.get("notes"):
        lines.append(f"Ограничения и пожелания: {brief['notes']}")
    lines.append(f"Календарь плана — {len(calendar)} "
                 f"{plural_ru(len(calendar), 'неделя', 'недели', 'недель')}:")
    race_date = brief.get("race_date")
    for week in calendar:
        line = f"  Неделя {week['w']}: {_ddmm(week['start_date'])}–{_ddmm(week['end_date'])}"
        if race_date and week["start_date"] <= race_date <= week["end_date"]:
            line += f" (старт {_ddmm(race_date)})"
        lines.append(line)
    return "\n".join(lines)


def _gap_ru(seconds):
    sign = "медленнее" if seconds > 0 else "быстрее"
    seconds = abs(seconds)
    text = (f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}" if seconds >= 3600
            else f"{seconds // 60}:{seconds % 60:02d}")
    return f"цель {sign} прогноза на {text}"


def format_goal_assessment(assessment):
    """Факты о реалистичности цели к старту; общий план — пустая строка."""
    if not assessment:
        return ""
    lines = ["=== Оценка цели ===",
             f"До старта {assessment['weeks']} "
             f"{plural_ru(assessment['weeks'], 'неделя', 'недели', 'недель')}; "
             f"для {assessment['distance_km']:g} км разумно не меньше {assessment['min_weeks']}."]
    prediction = assessment.get("prediction")
    if prediction:
        basis = prediction["basis"]
        line = (f"Прогноз по формуле Ригеля от забега {basis['dist_label']} за {basis['time']} "
                f"({basis['date']}): {prediction['time']}")
        if assessment.get("gap_sec") is not None:
            line += "; " + (_gap_ru(assessment["gap_sec"]) if assessment["gap_sec"]
                            else "цель совпадает с прогнозом")
        lines.append(line + ".")
    else:
        lines.append("Прогноза нет: забегов за последний год нет.")
    return "\n".join(lines)


def _outline_week_line(week):
    phase = PLAN_PHASE_LABELS.get(week["type"], week["type"])
    line = f"  Неделя {week['week']} ({phase}): {week['km']:g} км"
    if week.get("accent"):
        line += f" — {week['accent']}"
    if week.get("key"):
        line += f"; ключевые: {week['key']}"
    return line


def format_outline_block(outline, title="Последний каркас"):
    lines = [f"=== {title} ==="]
    lines.extend(_outline_week_line(week) for week in outline["weeks"])
    if outline.get("paces"):
        lines.append("Ориентиры темпа: " + "; ".join(
            f"{p['name']} {p['value']}" for p in outline["paces"]))
    return "\n".join(lines)


def plan_builder_data(brief_text="", assessment_text="", outline_text=""):
    """Блоки разбора «Новый план» одной строкой — идут на место окна правок."""
    return "\n\n".join(text for text in (brief_text, assessment_text, outline_text) if text)


# Генерация недель по каркасу (#69): отдельный вызов на кусок из нескольких
# недель. Модель пишет только ячейки дней — даты, тип, акцент и день старта
# сервер ставит сам, поэтому они даны ей как данные, а не как задача.
PLAN_WEEKS_PROMPT = """Ты опытный беговой тренер. Расписываешь по дням недели нового плана спортсмена по согласованному каркасу. Ниже — данные спортсмена, бриф, каркас всего плана и таблица «Расписать недели»: распиши только недели из этой таблицы.

Правила:
- Следуй каркасу: тип и акцент недели, целевой объём (сумма дней — около km недели), ключевые тренировки — в эту неделю.
- Тренировки — только в дни без пометок «недоступный день», длительная — в день длительной. Тренировок в неделю — не больше, чем в брифе. Пустая строка — отдых.
- День с пометкой «вне плана» и день старта оставь пустыми: их заполнит сервер.
- Ячейка — одна строка до 120 символов. Объём в км — в начале ячейки: «10 км легко», «16 км длительный, последние 3 км в темпе М», «9 км: разминка 2 км, 5×1 км по 4:30 через 2:00, заминка». Темпы бери из ориентиров каркаса.
- Без пояснений вне ячеек.

Формат ответа — СТРОГО JSON без текста до или после:
{"weeks": [{"week": 5, "mon": "", "tue": "8 км легко", "wed": "", "thu": "10 км: 3×2 км по 4:50", "fri": "", "sat": "6 км восстановительный", "sun": "16 км длительный"}]}
Одна запись на каждую неделю таблицы, week — её номер, все семь дней."""

_CHUNK_DAY_NOTES = {"outside": "вне плана — оставь пустым",
                    "race": "день старта — оставь пустым",
                    "unavailable": "недоступный день — отдых",
                    "long": "день длительной"}


def format_chunk_days(rows):
    """Таблица «Расписать недели»: недели куска с датами и пометками дней.

    rows — из plan_builder.chunk_rows: неделя каркаса и её дни со статусом.
    """
    day_ru = dict(PLAN_DAYS)
    lines = ["=== Расписать недели ==="]
    for row in rows:
        phase = PLAN_PHASE_LABELS.get(row["type"], row["type"])
        head = (f"Неделя {row['week']}, {_ddmm(row['start_date'])}–{_ddmm(row['end_date'])}, "
                f"{phase}: {row['km']:g} км")
        if row.get("accent"):
            head += f" — {row['accent']}"
        if row.get("key"):
            head += f"; ключевые: {row['key']}"
        lines.append(head)
        for day in row["days"]:
            note = _CHUNK_DAY_NOTES.get(day["status"])
            lines.append(f"  {day['field']} ({day_ru[day['field']]} {_ddmm(day['date'])})"
                         + (f": {note}" if note else ""))
    return "\n".join(lines)


def plan_weeks_data(profile_lines, brief_text, outline_text, days_text):
    """Данные для вызова на кусок: спортсмен, бриф, каркас, дни куска."""
    parts = []
    if profile_lines:
        parts.append("=== Спортсмен ===\n" + "\n".join(profile_lines))
    parts.extend([brief_text, outline_text, days_text])
    return "\n\n".join(parts)
