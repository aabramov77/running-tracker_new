"""Долгая история для ИИ-тренера (#81): месяцы за год и прошлые планы.

Модуль ничего не читает из GCS: на вход — пробежки, их нагрузка, реестр
планов, недели планов и старты, на выходе — сводка для промпта. Ближайшие
недели модель видит в блоках нагрузки и выполнения плана; здесь — фон:
тренд за год, перерывы и чем закончились прошлые подготовки.
"""
from datetime import timedelta

from compliance import plan_compliance, to_date
from domain import parse_time_to_sec

HISTORY_MONTHS = 12
PAST_PLANS_MAX = 3
RACE_MATCH_DAYS = 3             # старт в «Стартах» ищется в стольких днях от даты гонки плана
EASY_TYPES = ("easy",)          # по ним — тренд аэробной формы: темп и пульс на лёгких


def _month(day):
    return f"{day.year:04d}-{day.month:02d}"


def _months_back(day, count):
    """Месяц, отстоящий от месяца day на count назад, — первым числом."""
    index = day.year * 12 + day.month - 1 - count
    return day.replace(year=index // 12, month=index % 12 + 1, day=1)


def _number(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _pace_sec(run, dist):
    """Темп пробежки в сек/км: из поля темпа, иначе из времени и дистанции."""
    pace = parse_time_to_sec(run.get("pace"))
    if pace:
        return pace
    total = parse_time_to_sec(run.get("time"))
    return total / dist if total and dist else None


def monthly_summary(runs, run_loads, today, months=HISTORY_MONTHS):
    """Месяцы за год, от старых к текущему: объём, лёгкие, нагрузка, тяжёлые.

    run_loads — нагрузка по пробежкам из build_training_load: {str(id):
    {"int", "ext"}}. Месяцы до первой пробежки не выводятся — это не
    перерыв, а время до приложения. Месяц без пробежек после неё
    выводится: перерыв модели важен. Ось нагрузки, которой нет ни у одной
    пробежки (нет порога или зон), в сводку не попадает.
    """
    dated = [(to_date(r.get("date")), r) for r in runs or []
             if isinstance(r, dict) and not r.get("deleted")]
    dated = [(day, r) for day, r in dated if day and day <= today + timedelta(days=1)]
    if not dated:
        return []
    first = max(min(day for day, _ in dated).replace(day=1), _months_back(today, months - 1))
    current = _month(today)

    rows, keys = {}, []
    cursor = first
    while _month(cursor) <= current:
        key = _month(cursor)
        keys.append(key)
        rows[key] = {"month": key, "partial": key == current, "runs": 0, "km": 0.0,
                     "longest_km": 0.0, "hard": 0,
                     "_easy_km": 0.0, "_easy_pace": 0.0, "_hr_km": 0.0, "_hr": 0.0,
                     "ext": 0, "ext_unknown": 0, "int": 0, "int_unknown": 0}
        cursor = _months_back(cursor, -1)

    loads = run_loads or {}
    axes = {axis: any((loads.get(str(r.get("id"))) or {}).get(axis) is not None for _, r in dated)
            for axis in ("ext", "int")}
    for day, run in dated:
        row = rows.get(_month(day))
        if row is None:
            continue
        dist = _number(run.get("dist")) or 0.0
        row["runs"] += 1
        row["km"] += dist
        row["longest_km"] = max(row["longest_km"], dist)
        row["hard"] += run.get("feel") in ("hard", "bad")
        load = loads.get(str(run.get("id"))) or {}
        for axis in ("ext", "int"):
            if load.get(axis) is None:
                row[axis + "_unknown"] += 1
            else:
                row[axis] += load[axis]
        if run.get("type") in EASY_TYPES and dist:
            pace = _pace_sec(run, dist)
            if pace:
                row["_easy_km"] += dist
                row["_easy_pace"] += pace * dist
            hr = _number(run.get("hr"))
            if hr:
                row["_hr_km"] += dist
                row["_hr"] += hr * dist

    result = []
    for key in keys:
        row = rows[key]
        easy_km, hr_km = row.pop("_easy_km"), row.pop("_hr_km")
        easy_pace, hr = row.pop("_easy_pace"), row.pop("_hr")
        row["km"] = round(row["km"], 1)
        row["longest_km"] = round(row["longest_km"], 1)
        row["easy_pace_sec"] = round(easy_pace / easy_km) if easy_km else None
        row["easy_hr"] = round(hr / hr_km) if hr_km else None
        for axis in ("ext", "int"):
            if not axes[axis]:
                row[axis] = row[axis + "_unknown"] = None
            else:
                row[axis] = round(row[axis])
        result.append(row)
    return result


def past_plan_candidates(index, today, limit=PAST_PLANS_MAX):
    """Прошлые планы: не скрытые, не активный и с гонкой в прошлом —
    последние limit, от свежих к старым."""
    active = index.get("active_plan_id")
    past = [(to_date(p.get("race_date")), p) for p in index.get("plans", [])
            if isinstance(p, dict) and not p.get("archived") and p.get("id") != active]
    past = [(day, p) for day, p in past if day and day < today]
    past.sort(key=lambda item: item[0], reverse=True)
    return [p for _, p in past[:limit]]


def race_result(races, race_date, days=RACE_MATCH_DAYS):
    """Старт из «Стартов» ближе всего к дате гонки, не дальше days; None — нет.

    Время отдаётся секундами: в промпт оно собирается заново, текст из поля
    времени туда не попадает — как и у порогового темпа (#66).
    """
    target = to_date(race_date)
    if not target:
        return None
    near = [(abs((to_date(r.get("date")) - target).days), r) for r in races or []
            if isinstance(r, dict) and not r.get("deleted") and to_date(r.get("date"))]
    near = [(gap, r) for gap, r in near if gap <= days]
    if not near:
        return None
    _, race = min(near, key=lambda item: item[0])
    return {"date": to_date(race.get("date")).isoformat(), "dist_label": race.get("dist_label"),
            "seconds": parse_time_to_sec(race.get("time"))}


def past_plans(plans, weeks_by_plan, runs, races):
    """Итоги прошлых планов: цель, выполнение план/факт и результат старта.

    plans — из past_plan_candidates, weeks_by_plan — {id плана: недели}.
    Выполнение считается тем же plan_compliance, что и у активного плана.
    """
    result = []
    for plan in plans:
        weeks = weeks_by_plan.get(plan["id"]) or []
        totals = None
        if weeks:
            full = plan_compliance(weeks, runs, plan.get("plan_start"), plan["id"])
            totals = full["totals"] if full["dated"] else None
        result.append({
            "race_name": plan.get("race_name") or "",
            "race_date": plan.get("race_date") or "",
            "target_time": plan.get("target_time") or "",
            "weeks": len(weeks),
            "compliance": totals,
            "result": race_result(races, plan.get("race_date")),
        })
    return result
