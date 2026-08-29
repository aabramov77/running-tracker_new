"""План против факта: границы недель и разбор планового дня (#41).

Модуль чистый — ни GCS, ни сети, поэтому импортируется напрямую.
"""
from datetime import date

import pytest

import compliance as c


# ── Границы недель (#40) ──────────────────────────────────────────────────────

@pytest.mark.parametrize("start,expected", [
    ("2026-05-04", date(2026, 5, 4)),   # понедельник — сам себе начало
    ("2026-05-05", date(2026, 5, 4)),   # вторник
    ("2026-05-06", date(2026, 5, 4)),   # среда
    ("2026-05-07", date(2026, 5, 4)),   # четверг
    ("2026-05-08", date(2026, 5, 4)),   # пятница
    ("2026-05-09", date(2026, 5, 4)),   # суббота
    ("2026-05-10", date(2026, 5, 4)),   # воскресенье
])
def test_week_zero_is_monday_for_any_start_weekday(start, expected):
    """plan_start может быть любым днём, недели плана — всегда пн→вс."""
    assert c.plan_week_zero(start) == expected


def test_sunday_belongs_to_the_week_that_is_ending():
    """Регрессия #40: 23.08.2026 — воскресенье, и это конец недели 17–23.08,
    а не начало следующей. Старая арифметика подсвечивала неделю с 24.08."""
    idx = c.current_week_idx("2026-05-10", 20, "2026-08-23")
    assert c.plan_week_range("2026-05-10", idx) == (date(2026, 8, 17), date(2026, 8, 23))


# ── Якорь по подписи первой недели (#40) ──────────────────────────────────────

# Реальная конфигурация из #40: plan_start остался воскресным с прежних
# времён, а строки плана человек подписал по понедельникам (после #23).
LABELLED = [{"start": "11.05", "end": "17.05"}] + [
    {"start": "", "end": ""} for _ in range(19)]


def test_label_anchor_beats_a_stale_plan_start():
    """Ровно тот случай, ради которого заведён #40: отсчёт от plan_start
    подсвечивал строку 24.08, когда сегодня ещё 23.08."""
    idx = c.current_week_idx("2026-05-10", 20, "2026-08-23", weeks=LABELLED)
    assert idx == 14
    assert c.plan_week_range("2026-05-10", idx, weeks=LABELLED) == \
        (date(2026, 8, 17), date(2026, 8, 23))


def test_stale_plan_start_alone_would_point_at_the_next_row():
    """Без подписей исправить рассогласование нечем — фиксируем разницу,
    чтобы правка не выглядела бессмысленной."""
    assert c.current_week_idx("2026-05-10", 20, "2026-08-23") == 15


def test_plan_start_is_used_when_labels_are_empty():
    blank = [{"start": "", "end": ""} for _ in range(20)]
    assert c.current_week_idx("2026-05-10", 20, "2026-08-23", weeks=blank) == \
        c.current_week_idx("2026-05-10", 20, "2026-08-23")


@pytest.mark.parametrize("label", ["11.05", "11.05.2026", "11.05.26", "2026-05-11", "11/05"])
def test_label_formats(label):
    assert c.label_to_date(label, "2026-05-10") == date(2026, 5, 11)


def test_label_year_is_picked_closest_to_plan_start():
    """План через Новый год: «05.01» при старте в ноябре — это январь следующего."""
    assert c.label_to_date("05.01", "2026-11-30") == date(2027, 1, 5)
    assert c.label_to_date("20.12", "2027-01-10") == date(2026, 12, 20)


@pytest.mark.parametrize("label", ["", None, "неделя 1", "32.13", "—"])
def test_broken_labels_fall_back_to_plan_start(label):
    weeks = [{"start": label}]
    assert c.plan_week_zero("2026-05-10", weeks) == c.plan_week_zero("2026-05-10")


def test_label_anchor_survives_a_non_monday_label():
    """Подпись «10.05» (вс) означает неделю 04–10.05, а не 11–17.05."""
    weeks = [{"start": "10.05"}]
    assert c.plan_week_zero("2026-05-10", weeks) == date(2026, 5, 4)


def test_monday_starts_the_next_week():
    sunday = c.current_week_idx("2026-05-10", 20, "2026-08-23")
    monday = c.current_week_idx("2026-05-10", 20, "2026-08-24")
    assert monday == sunday + 1


@pytest.mark.parametrize("day", [
    "2026-08-17", "2026-08-18", "2026-08-19", "2026-08-20",
    "2026-08-21", "2026-08-22", "2026-08-23",
])
def test_every_day_of_a_week_maps_to_the_same_index(day):
    assert c.current_week_idx("2026-05-10", 20, day) == 15


def test_index_is_clamped_to_plan_bounds():
    assert c.current_week_idx("2026-05-10", 13, "2026-01-01") == 0    # до старта
    assert c.current_week_idx("2026-05-10", 13, "2027-01-01") == 12   # после конца


def test_missing_plan_start_falls_back_to_historic_default():
    assert c.plan_week_zero(None) == c.plan_week_zero(c.DEFAULT_PLAN_START)
    assert c.plan_week_zero("не дата") == c.plan_week_zero(None)


def test_week_range_is_seven_days_monday_to_sunday():
    start, end = c.plan_week_range("2026-05-04", 3)
    assert start.weekday() == 0 and end.weekday() == 6
    assert (end - start).days == 6


# ── Разбор планового дня ──────────────────────────────────────────────────────

@pytest.mark.parametrize("text,km", [
    ("10 км лёгкий", 10.0),
    ("10км", 10.0),
    ("10 KM", 10.0),
    ("10,5 км", 10.5),
    ("10.5 км", 10.5),
    ("длительный 21 км", 21.0),
])
def test_single_distance_is_parsed(text, km):
    assert c.parse_planned_day(text) == {"kind": c.KM, "km": km}


@pytest.mark.parametrize("text", ["", None, "   ", "отдых", "Отдых", "выходной", "rest", "-", "—"])
def test_rest_days(text):
    assert c.parse_planned_day(text)["kind"] == c.REST


@pytest.mark.parametrize("text", [
    "интервалы 6×800м",      # кириллическая × (знак умножения)
    "6x800",                 # латинская x
    "6х800 через 200",       # кириллическая х
    "45 мин лёгкий",         # длительность, не расстояние
    "1 ч кросс",
    "фартлек",               # вообще без чисел
    "разминка 2 км + 6x800 + заминка 2 км",
    "10 км (5 км в темпе)",  # два числа: 10, а не 15 — не угадываем
])
def test_unparseable_days_are_not_guessed(text):
    """Правдоподобная, но неверная цифра хуже честного «не знаю»."""
    assert c.parse_planned_day(text) == {"kind": c.UNPARSED, "km": None}


def test_interval_notation_is_not_mistaken_for_distance():
    """«6х800м» не должно дать «00 м» или 800 км."""
    assert c.parse_planned_day("6х800м")["km"] is None


# ── Сопоставление недели ──────────────────────────────────────────────────────

MONDAY = date(2026, 8, 17)

FULL_WEEK = {
    "mon": "10 км лёгкий", "tue": "отдых", "wed": "8 км", "thu": "отдых",
    "fri": "6 км", "sat": "отдых", "sun": "16 км длительный",
}


def _runs(*pairs):
    return [{"date": d, "dist": km, "plan_id": "p1"} for d, km in pairs]


def test_week_sums_plan_and_fact():
    result = c.week_compliance(
        FULL_WEEK, MONDAY,
        c._runs_by_date(_runs(("2026-08-17", 10), ("2026-08-19", 8),
                              ("2026-08-21", 6), ("2026-08-23", 16))))
    assert result["planned_km"] == 40.0
    assert result["actual_km"] == 40.0
    assert result["pct"] == 100
    assert result["complete"] is True
    assert result["missed"] == 0 and result["extra"] == 0


def test_missed_and_extra_days():
    result = c.week_compliance(
        FULL_WEEK, MONDAY,
        c._runs_by_date(_runs(("2026-08-17", 10), ("2026-08-18", 5))))
    by_field = {d["field"]: d for d in result["days"]}
    assert by_field["mon"]["status"] == c.DONE
    assert by_field["wed"]["status"] == c.MISSED       # запланировано, не сделано
    assert by_field["tue"]["status"] == c.EXTRA        # день отдыха, но бежал
    assert by_field["thu"]["status"] == c.EMPTY
    assert result["missed"] == 3 and result["extra"] == 1


def test_several_runs_in_one_day_are_summed():
    result = c.week_compliance(
        {"mon": "10 км"}, MONDAY,
        c._runs_by_date(_runs(("2026-08-17", 6), ("2026-08-17", 4))))
    day = result["days"][0]
    assert day["actual_km"] == 10.0 and day["runs"] == 2


def test_unparsed_cell_hides_percentage():
    """Неполный плановый объём нельзя выдавать за полный."""
    week = {**FULL_WEEK, "wed": "интервалы 6×800м"}
    result = c.week_compliance(week, MONDAY, c._runs_by_date(_runs(("2026-08-19", 9))))
    assert result["complete"] is False
    assert result["unparsed"] == 1
    assert result["pct"] is None
    assert result["delta_km"] is None
    assert result["planned_km"] == 32.0          # без нераспознанной ячейки
    assert result["days"][2]["status"] == c.DONE  # но день всё равно выполнен


def test_unparsed_day_still_counts_as_a_planned_session():
    result = c.week_compliance({"mon": "6x800"}, MONDAY, {})
    assert result["planned_sessions"] == 1
    assert result["days"][0]["status"] == c.MISSED


# ── План целиком ──────────────────────────────────────────────────────────────

WEEKS = [{"mon": "10 км"}, {"mon": "12 км"}, {"mon": "14 км"}]


def test_runs_land_in_the_right_week():
    result = c.plan_compliance(
        WEEKS, _runs(("2026-08-17", 10), ("2026-08-24", 12), ("2026-08-31", 14)),
        plan_start="2026-08-17")
    assert [w["actual_km"] for w in result["weeks"]] == [10.0, 12.0, 14.0]
    assert result["totals"]["pct"] == 100


def test_runs_outside_the_plan_are_ignored():
    result = c.plan_compliance(
        WEEKS, _runs(("2026-08-17", 10), ("2026-12-25", 99)),
        plan_start="2026-08-17")
    assert result["totals"]["actual_km"] == 10.0


def test_deleted_runs_are_excluded():
    runs = _runs(("2026-08-17", 10))
    runs.append({"date": "2026-08-17", "dist": 99, "plan_id": "p1", "deleted": True})
    result = c.plan_compliance(WEEKS, runs, plan_start="2026-08-17")
    assert result["weeks"][0]["actual_km"] == 10.0


def test_runs_of_another_plan_are_excluded():
    runs = _runs(("2026-08-17", 10))
    runs.append({"date": "2026-08-17", "dist": 99, "plan_id": "other"})
    result = c.plan_compliance(WEEKS, runs, plan_start="2026-08-17", plan_id="p1")
    assert result["weeks"][0]["actual_km"] == 10.0


def test_runs_with_broken_dates_do_not_crash():
    runs = _runs(("2026-08-17", 10)) + [{"date": "", "dist": 5, "plan_id": "p1"},
                                        {"date": "вчера", "dist": 5, "plan_id": "p1"}]
    result = c.plan_compliance(WEEKS, runs, plan_start="2026-08-17")
    assert result["totals"]["actual_km"] == 10.0


def test_one_unparsed_week_makes_the_total_incomplete():
    weeks = [{"mon": "10 км"}, {"mon": "6x800"}]
    result = c.plan_compliance(weeks, [], plan_start="2026-08-17")
    assert result["totals"]["complete"] is False
    assert result["totals"]["pct"] is None
    assert result["totals"]["unparsed"] == 1


def test_empty_plan_does_not_crash():
    result = c.plan_compliance([], [], plan_start="2026-08-17")
    assert result["weeks"] == []
    assert result["totals"]["weeks_total"] == 0


# ── Совместимость с прежней точкой входа ──────────────────────────────────────

def test_storage_delegates_to_compliance(storage_module):
    """storage.current_plan_week_idx остаётся публичным именем (#36)."""
    assert storage_module.current_plan_week_idx("2026-05-10", 13) == \
        c.current_week_idx("2026-05-10", 13)
