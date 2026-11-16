"""Новый план с ИИ (#69): бриф, календарь, оценка цели, каркас и черновик.

Слой домена по раскладке #36: только чистые вычисления — без GCS и HTTP.
Модель пишет содержание плана, а всё, что можно посчитать, считается здесь:
даты недель, их число, неделя и день старта, проверки и предупреждения.
Так ошибка модели в датах или в числе недель не доходит до плана.
"""
from datetime import timedelta

from compliance import DAY_FIELDS, KM, REST, parse_planned_day, to_date
from config import AI_PLAN_GENERAL_WEEKS_MAX, AI_PLAN_WEEKS_MAX
from domain import DIST_LABEL_KM, PLAN_DAYS, PLAN_PHASE_LABELS, parse_time_to_sec
from load import (RACE_PACE_MAX, RACE_PACE_MIN, RIEGEL_EXP, estimate_threshold,
                  format_duration)

BRIEF_TYPES = ("race", "general")
BRIEF_TITLE_MAX = 200
BRIEF_NOTES_MAX = 500
BRIEF_DISTANCE_KM = (1, 250)
BRIEF_SESSIONS = (1, 14)

# Сколько недель подготовки к дистанции — разумный минимум. Меньше — не
# запрет, а предупреждение и повод для модели сказать об этом прямо.
MIN_WEEKS_BY_KM = ((5, 4), (10, 6), (21.1, 8))
MIN_WEEKS_LONGER = 12

# Подписи недель «ДД.ММ» читаются с годом, ближайшим к началу плана
# (compliance.label_to_date): дальше полугода от начала год угадается
# неверно. Длинный план подписывается с годом.
SHORT_LABEL_DAYS = 180


def _one_line(value, limit):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def _int(value):
    """Целое из числа или строки «12»; bool и дробные — None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _km(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(",", "."))
        except ValueError:
            return None
    return None


def _target_seconds(value, km):
    """Цель по времени → секунды или None.

    «1:45» по формату — минуты и секунды, но к полумарафону это явно часы и
    минуты: темп вне коридора забегов значит, что прочитано не тем порядком.
    """
    seconds = parse_time_to_sec(value) if isinstance(value, str) else None
    if not seconds:
        return None
    if RACE_PACE_MIN <= seconds / km <= RACE_PACE_MAX:
        return seconds
    if value.count(":") == 1 and RACE_PACE_MIN <= seconds * 60 / km <= RACE_PACE_MAX:
        return seconds * 60
    return None


def target_label(target_time):
    """«1:45:00» → «1:45»: так цель пишут в плане; иначе как есть."""
    seconds = parse_time_to_sec(target_time)
    if seconds and seconds >= 3600 and seconds % 60 == 0:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}"
    return target_time


def next_monday(today):
    """Ближайший понедельник после сегодня — начало плана по умолчанию."""
    return today + timedelta(days=7 - today.weekday())


def min_weeks_for(km):
    for limit, weeks in MIN_WEEKS_BY_KM:
        if km <= limit:
            return weeks
    return MIN_WEEKS_LONGER


# ── Бриф ─────────────────────────────────────────────────────────────────────

def clean_brief(raw, today):
    """Бриф от модели → (бриф, None) или (None, причина отказа).

    Причина нужна журналу и тестам; спортсмен видит только, что бриф не
    принят. Календарь считается тут же: бриф, по которому не построить
    план разумной длины, не принимается.
    """
    if not isinstance(raw, dict):
        return None, "not_an_object"
    kind = raw.get("type")
    if kind not in BRIEF_TYPES:
        return None, "type"
    title = _one_line(raw.get("title"), BRIEF_TITLE_MAX)
    if not title:
        return None, "title"

    if raw.get("start_date") in (None, ""):
        start = next_monday(today)
    else:
        start = to_date(raw.get("start_date"))
        if start is None or start < today:
            return None, "start_date"

    sessions = _int(raw.get("sessions_per_week"))
    if sessions is None or not BRIEF_SESSIONS[0] <= sessions <= BRIEF_SESSIONS[1]:
        return None, "sessions_per_week"

    days = raw.get("days") or []
    if not isinstance(days, list) or any(day not in DAY_FIELDS for day in days):
        return None, "days"
    days = [day for day in DAY_FIELDS if day in days]
    long_day = raw.get("long_run_day") or ""
    if long_day and (long_day not in DAY_FIELDS or (days and long_day not in days)):
        return None, "long_run_day"

    brief = {"type": kind, "title": title, "start_date": start.isoformat(),
             "sessions_per_week": sessions, "days": days, "long_run_day": long_day,
             "notes": _one_line(raw.get("notes"), BRIEF_NOTES_MAX)}

    if kind == "race":
        race_date = to_date(raw.get("race_date"))
        if race_date is None or race_date < start:
            return None, "race_date"
        km = _km(raw.get("distance_km"))
        if km is None or not BRIEF_DISTANCE_KM[0] <= km <= BRIEF_DISTANCE_KM[1]:
            return None, "distance_km"
        target = raw.get("target_time")
        if target in (None, ""):
            target = ""
        else:
            seconds = _target_seconds(target, km)
            if not seconds:
                return None, "target_time"
            target = format_duration(seconds)
        brief.update(race_date=race_date.isoformat(), distance_km=round(km, 3),
                     target_time=target)
    else:
        weeks = _int(raw.get("weeks"))
        if weeks is None or not 1 <= weeks <= AI_PLAN_GENERAL_WEEKS_MAX:
            return None, "weeks"
        brief["weeks"] = weeks

    calendar = brief_calendar(brief)
    if len(calendar) > AI_PLAN_WEEKS_MAX:
        return None, "too_long"
    brief["weeks"] = len(calendar)
    return brief, None


# ── Календарь ────────────────────────────────────────────────────────────────

def brief_calendar(brief):
    """Недели плана по брифу: [{w, start_date, end_date, start, end}].

    Первая неделя — от даты начала до воскресенья (короче, если начало не в
    понедельник), остальные — пн–вс. У плана к старту последняя неделя —
    та, в которую попадает старт; у общего — weeks штук. start/end — подписи
    недели в том виде, в каком их хранит план.
    """
    start = to_date(brief["start_date"])
    monday = start - timedelta(days=start.weekday())
    if brief["type"] == "race":
        race = to_date(brief["race_date"])
        count = (race - timedelta(days=race.weekday()) - monday).days // 7 + 1
    else:
        count = brief["weeks"]
    last_end = monday + timedelta(days=7 * count - 1)
    full = (last_end - start).days > SHORT_LABEL_DAYS
    fmt = "%d.%m.%Y" if full else "%d.%m"

    weeks = []
    for idx in range(count):
        first = max(start, monday + timedelta(days=7 * idx))
        last = monday + timedelta(days=7 * idx + 6)
        weeks.append({"w": idx + 1, "start_date": first.isoformat(),
                      "end_date": last.isoformat(),
                      "start": first.strftime(fmt), "end": last.strftime(fmt)})
    return weeks


def race_day(brief):
    """(номер недели, код дня) старта или None у общего плана."""
    if brief.get("type") != "race":
        return None
    race = to_date(brief["race_date"])
    for week in brief_calendar(brief):
        if to_date(week["start_date"]) <= race <= to_date(week["end_date"]):
            return week["w"], DAY_FIELDS[race.weekday()]
    return None


def race_cell(brief):
    """Текст ячейки дня старта: «СТАРТ 21.1 км, цель 1:45:00»."""
    text = f"СТАРТ {brief['distance_km']:g} км"
    target = brief.get("target_time")
    return text + (f", цель {target_label(target)}" if target else "")


# ── Оценка цели ──────────────────────────────────────────────────────────────

def goal_assessment(brief, races, today):
    """Факты для разговора о реалистичности цели; None — у общего плана.

    Прогноз — по формуле Ригеля от того же забега, по которому оценивается
    пороговый темп (лучший часовой темп за год). Забега за год нет — прогноза
    тоже: по старому результату он ввёл бы в заблуждение.
    """
    if not brief or brief.get("type") != "race":
        return None
    km = brief["distance_km"]
    result = {"weeks": brief["weeks"], "min_weeks": min_weeks_for(km),
              "distance_km": km, "prediction": None,
              "target_time": brief.get("target_time") or "", "gap_sec": None}
    estimate = estimate_threshold(races, today)
    basis = (estimate or {}).get("basis") or {}
    if basis and not basis.get("stale"):
        base_km = DIST_LABEL_KM.get(basis["dist_label"])
        base_sec = parse_time_to_sec(basis["time"])
        if base_km and base_sec:
            seconds = int(round(base_sec * (km / base_km) ** RIEGEL_EXP))
            result["prediction"] = {"seconds": seconds, "time": format_duration(seconds),
                                    "basis": basis}
            target = parse_time_to_sec(result["target_time"])
            if target:
                result["gap_sec"] = target - seconds    # > 0 — цель медленнее прогноза
    return result


# ── Каркас ───────────────────────────────────────────────────────────────────

OUTLINE_TEXT_MAX = 200
OUTLINE_KM = (0, 300)
OUTLINE_PACES_MAX = 8
OUTLINE_PACE_TEXT_MAX = 60
WEEK_TYPES = tuple(PLAN_PHASE_LABELS)       # dev, peak, taper, load, race

GROWTH_LIMIT = 1.15         # рост объёма к прошлой неделе — больше уже скачок
FIRST_WEEK_LIMIT = 1.3      # первая неделя к обычному объёму спортсмена


def clean_outline(raw, calendar):
    """Каркас от модели → (каркас, None) или (None, причина отказа).

    Недель должно быть ровно столько, сколько в календаре брифа, с номерами
    1..N: неделя, которой нет в календаре, или пропущенная неделя значат, что
    модель строила каркас не под этот бриф.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("weeks"), list):
        return None, "not_an_object"
    by_number = {}
    for item in raw["weeks"]:
        if not isinstance(item, dict):
            return None, "week"
        number = _int(item.get("week"))
        if number is None or number in by_number:
            return None, "week"
        kind = item.get("type")
        if kind not in WEEK_TYPES:
            return None, "type"
        km = _km(item.get("km"))
        if km is None or not OUTLINE_KM[0] <= km <= OUTLINE_KM[1]:
            return None, "km"
        by_number[number] = {"week": number, "type": kind, "km": round(km, 1),
                             "accent": _one_line(item.get("accent"), OUTLINE_TEXT_MAX),
                             "key": _one_line(item.get("key"), OUTLINE_TEXT_MAX)}
    if sorted(by_number) != [week["w"] for week in calendar]:
        return None, "weeks"

    paces = []
    for item in raw.get("paces") or []:
        if not isinstance(item, dict):
            continue
        name = _one_line(item.get("name"), OUTLINE_PACE_TEXT_MAX)
        value = _one_line(item.get("value"), OUTLINE_PACE_TEXT_MAX)
        if name and value:
            paces.append({"name": name, "value": value})
    return {"weeks": [by_number[n] for n in sorted(by_number)],
            "paces": paces[:OUTLINE_PACES_MAX]}, None


def _pct(value):
    return f"{value:.0f}".replace(".", ",")


def outline_warnings(outline, usual_km=None, assessment=None):
    """Предупреждения к каркасу: сохранению не мешают, спортсмен их видит.

    Рост после разгрузочной недели не скачок: объём возвращается к прежнему.
    """
    warnings = []
    weeks = outline["weeks"]
    if usual_km and weeks and weeks[0]["km"] > FIRST_WEEK_LIMIT * usual_km:
        warnings.append(f"Неделя 1: {weeks[0]['km']:g} км — заметно больше обычного "
                        f"объёма ({usual_km:g} км в неделю)")
    for prev, week in zip(weeks, weeks[1:]):
        if prev["type"] == "load" or prev["km"] <= 0:
            continue
        if week["km"] > GROWTH_LIMIT * prev["km"]:
            growth = (week["km"] / prev["km"] - 1) * 100
            warnings.append(f"Неделя {week['week']}: объём {prev['km']:g} → {week['km']:g} км "
                            f"(+{_pct(growth)} %) — рост больше 15 % за неделю")
    if assessment and assessment["weeks"] < assessment["min_weeks"]:
        warnings.append(f"До старта {assessment['weeks']} нед. — для {assessment['distance_km']:g} км "
                        f"разумно не меньше {assessment['min_weeks']}")
    return warnings


# ── Генерация недель по каркасу ──────────────────────────────────────────────

CELL_TEXT_MAX = 200
DRAFT_VOLUME_LIMIT = 1.15   # нижняя граница объёма по ячейкам к цели каркаса


def chunks(calendar, size):
    """Номера недель календаря кусками по size: [[1..4], [5..8], …]."""
    numbers = [week["w"] for week in calendar]
    return [numbers[i:i + size] for i in range(0, len(numbers), size)]


def _day_statuses(brief, week, race):
    """Дни строки календаря пн→вс с датой и статусом для модели."""
    monday = to_date(week["end_date"]) - timedelta(days=6)
    first = to_date(week["start_date"])
    days = []
    for offset, field in enumerate(DAY_FIELDS):
        day = monday + timedelta(days=offset)
        if day < first:
            status = "outside"
        elif race and race == (week["w"], field):
            status = "race"
        elif brief["days"] and field not in brief["days"]:
            status = "unavailable"
        elif field == brief.get("long_run_day"):
            status = "long"
        else:
            status = ""
        days.append({"field": field, "date": day.isoformat(), "status": status})
    return days


def chunk_rows(brief, calendar, outline, numbers):
    """Недели куска для таблицы «Расписать недели»: календарь + каркас + дни."""
    by_w = {week["w"]: week for week in calendar}
    plan = {week["week"]: week for week in outline["weeks"]}
    race = race_day(brief)
    return [{"week": n, "start_date": by_w[n]["start_date"], "end_date": by_w[n]["end_date"],
             "type": plan[n]["type"], "accent": plan[n]["accent"], "km": plan[n]["km"],
             "key": plan[n]["key"], "days": _day_statuses(brief, by_w[n], race)}
            for n in numbers]


def clean_chunk(raw, numbers):
    """Ответ модели на кусок → ({неделя: {день: текст}}, недостающие номера).

    Неделя не из куска или повтор недели не принимаются. День, которого нет
    в ответе, — отдых: пустая ячейка валидна.
    """
    cells = {}
    items = raw.get("weeks") if isinstance(raw, dict) else None
    for item in items if isinstance(items, list) else []:
        if not isinstance(item, dict):
            continue
        number = _int(item.get("week"))
        if number not in numbers or number in cells:
            continue
        cells[number] = {field: _one_line(item.get(field), CELL_TEXT_MAX)
                         if isinstance(item.get(field), str) else ""
                         for field in DAY_FIELDS}
    return cells, [n for n in numbers if n not in cells]


def assemble_weeks(brief, calendar, outline, cells):
    """Недели плана из календаря, каркаса и ячеек модели.

    Номер, подписи дат, тип и акцент — из календаря и каркаса; дни вне окна
    первой недели пустые; ячейку дня старта ставит сервер.
    """
    plan = {week["week"]: week for week in outline["weeks"]}
    race = race_day(brief)
    weeks = []
    for week in calendar:
        row = {"w": week["w"], "start": week["start"], "end": week["end"],
               "type": plan[week["w"]]["type"], "accent": plan[week["w"]]["accent"]}
        for day in _day_statuses(brief, week, race):
            if day["status"] == "outside":
                row[day["field"]] = ""
            elif day["status"] == "race":
                row[day["field"]] = race_cell(brief)
            else:
                row[day["field"]] = cells[week["w"]][day["field"]]
        weeks.append(row)
    return weeks


def draft_warnings(brief, calendar, outline, weeks):
    """Предупреждения черновика: созданию плана не мешают."""
    day_ru = dict(PLAN_DAYS)
    target = {week["week"]: week["km"] for week in outline["weeks"]}
    race = race_day(brief)
    warnings = []
    for cal, row in zip(calendar, weeks):
        n = row["w"]
        busy = [f for f in DAY_FIELDS
                if parse_planned_day(row.get(f))["kind"] != REST]
        off = [f for f in busy if brief["days"] and f not in brief["days"]
               and (n, f) != race]
        if off:
            warnings.append(f"Неделя {n}: тренировка в {', '.join(day_ru[f] for f in off)} — "
                            "не из доступных дней")
        if len(busy) > brief["sessions_per_week"]:
            warnings.append(f"Неделя {n}: тренировок {len(busy)} — больше, чем в брифе "
                            f"({brief['sessions_per_week']})")
        low = sum(parsed["km"] for parsed in (parse_planned_day(row.get(f)) for f in DAY_FIELDS)
                  if parsed["kind"] == KM)
        if target[n] and low > DRAFT_VOLUME_LIMIT * target[n]:
            warnings.append(f"Неделя {n}: по ячейкам не меньше {low:g} км при цели каркаса "
                            f"{target[n]:g} км")
    return warnings


def draft_summary(brief, weeks, outline, warnings):
    """Текст ответа с черновиком — его составляет сервер, а не модель."""
    kms = [week["km"] for week in outline["weeks"]]
    first, last = weeks[0], weeks[-1]
    text = (f"План расписан по неделям: {len(weeks)} нед., {first['start']}–{last['end']}, "
            f"объём {min(kms):g}–{max(kms):g} км в неделю.")
    if warnings:
        text += f" Предупреждений: {len(warnings)} — они в карточке черновика."
    return text + (" Проверьте черновик: план создаётся только по вашей команде — "
                   "«Создать план…» или правка в конструкторе.")
