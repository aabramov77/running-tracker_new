"""План против факта: границы недель и разбор планового дня (#41).

Модуль чистый — ни GCS, ни сети, поэтому импортируется напрямую.
"""
from datetime import date

import pytest

import compliance as c


# ── Границы недель (#40) ──────────────────────────────────────────────────────

# Разметка недель бывает разной: у одних планов строки идут пн→вс, у других
# вс→сб. Ни к какому дню недели даты не притягиваются — окно строки берётся
# из её подписи, иначе в строке «24.05–30.05» показался бы объём другой недели.

SUNDAY_PLAN = [{"start": d} for d in
               ["10.05", "17.05", "24.05", "31.05", "07.06", "14.06"]]
MONDAY_PLAN = [{"start": d} for d in
               ["11.05", "18.05", "25.05", "01.06", "08.06", "15.06"]]


@pytest.mark.parametrize("weeks,idx,expected", [
    (SUNDAY_PLAN, 0, (date(2026, 5, 10), date(2026, 5, 16))),
    (SUNDAY_PLAN, 2, (date(2026, 5, 24), date(2026, 5, 30))),
    (MONDAY_PLAN, 0, (date(2026, 5, 11), date(2026, 5, 17))),
    (MONDAY_PLAN, 2, (date(2026, 5, 25), date(2026, 5, 31))),
])
def test_window_equals_the_label_it_is_shown_under(weeks, idx, expected):
    """Подпись строки и окно расчёта обязаны совпадать по построению."""
    assert c.week_window(weeks, idx, "2026-05-10") == expected


def test_sunday_labelled_row_covers_its_own_seven_days():
    """Строка «24.05–30.05» — воскресенье→суббота. Прежний код брал
    понедельник её недели и считал объём за 18–24.05."""
    start, end = c.week_window(SUNDAY_PLAN, 2, "2026-05-10")
    assert (start.weekday(), end.weekday()) == (6, 5)   # вс → сб


@pytest.mark.parametrize("field,expected", [
    ("sun", date(2026, 5, 24)),   # первый день окна вс→сб
    ("mon", date(2026, 5, 25)),
    ("sat", date(2026, 5, 30)),   # последний
])
def test_day_columns_map_inside_the_window(field, expected):
    """В семи подряд идущих днях каждый день недели ровно один — колонки
    раскладываются однозначно при любом дне начала строки."""
    start, _ = c.week_window(SUNDAY_PLAN, 2, "2026-05-10")
    assert c.day_date(start, field) == expected


@pytest.mark.parametrize("weeks", [SUNDAY_PLAN, MONDAY_PLAN])
def test_day_columns_stay_within_the_window(weeks):
    start, end = c.week_window(weeks, 1, "2026-05-10")
    for field in c.DAY_FIELDS:
        assert start <= c.day_date(start, field) <= end


def test_week_zero_is_the_plan_start_itself():
    """Никакого притягивания к понедельнику: план начинается тогда,
    когда сказано."""
    assert c.plan_week_zero("2026-05-10") == date(2026, 5, 10)
    assert c.plan_week_zero("2026-05-13") == date(2026, 5, 13)


@pytest.mark.parametrize("day,expected", [
    ("2026-05-24", 2),   # первый день строки 3
    ("2026-05-27", 2),   # середина
    ("2026-05-30", 2),   # последний день
    ("2026-05-31", 3),   # уже следующая
    ("2026-05-23", 1),   # ещё предыдущая
])
def test_current_week_follows_the_labelled_windows(day, expected):
    assert c.current_week_idx("2026-05-10", 6, day, weeks=SUNDAY_PLAN) == expected


def test_day_before_the_plan_gives_the_first_row():
    assert c.current_week_idx("2026-05-10", 6, "2026-01-01", weeks=SUNDAY_PLAN) == 0


def test_day_after_the_plan_gives_the_last_row():
    assert c.current_week_idx("2026-05-10", 6, "2026-12-31", weeks=SUNDAY_PLAN) == 5


def test_irregular_labels_do_not_shift_later_rows():
    """Вставленная неделя или опечатка в дате раньше уводила все строки
    ниже — теперь каждая строка стоит на своей подписи."""
    weeks = [{"start": "10.05"}, {"start": "17.05"},
             {"start": "24.05"}, {"start": "07.06"}]   # разрыв: 31.05 пропущена
    assert c.week_window(weeks, 3, "2026-05-10")[0] == date(2026, 6, 7)
    assert c.current_week_idx("2026-05-10", 4, "2026-06-08", weeks=weeks) == 3
    # день в разрыве относится к последней начавшейся строке
    assert c.current_week_idx("2026-05-10", 4, "2026-06-03", weeks=weeks) == 2


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
    assert c.plan_week_range("2026-05-10", idx, weeks=LABELLED) ==         (date(2026, 8, 17), date(2026, 8, 23))


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


@pytest.mark.parametrize("plan_start,weeks,expected", [
    ("2026-05-10", [{"start": "11.05"}], c.LABEL),
    ("2026-05-10", [{"start": ""}],      c.PLAN_START),
    ("2026-05-10", None,                 c.PLAN_START),
    (None,         [{"start": "11.05"}], c.LABEL),
    (None,         [{"start": ""}],      c.DEFAULT),
    (None,         None,                 c.DEFAULT),
])
def test_anchor_source_is_reported(plan_start, weeks, expected):
    """`default` значит, что недели разложены наугад — интерфейсу нужно
    показать не цифры, а объяснение, чего не хватает."""
    assert c.anchor_source(plan_start, weeks) == expected


def test_undated_plan_is_marked_not_dated():
    result = c.plan_compliance([{"mon": "10 км"}], [])
    assert result["anchor_source"] == c.DEFAULT
    assert result["dated"] is False


def test_dated_plan_carries_its_anchor():
    result = c.plan_compliance([{"start": "17.08", "mon": "10 км"}], [],
                               plan_start="2026-08-17")
    assert result["anchor"] == "2026-08-17"
    assert result["dated"] is True


def test_windows_without_labels_step_by_seven_from_plan_start():
    """Без подписей отсчитываем по семь дней от plan_start — как есть,
    не притягивая к понедельнику."""
    assert c.plan_week_range("2026-05-10", 0) == (date(2026, 5, 10), date(2026, 5, 16))
    assert c.plan_week_range("2026-05-10", 2) == (date(2026, 5, 24), date(2026, 5, 30))


def test_index_is_clamped_to_plan_bounds():
    assert c.current_week_idx("2026-05-10", 13, "2026-01-01") == 0    # до старта
    assert c.current_week_idx("2026-05-10", 13, "2027-01-01") == 12   # после конца


def test_missing_plan_start_falls_back_to_historic_default():
    assert c.plan_week_zero(None) == c.plan_week_zero(c.DEFAULT_PLAN_START)
    assert c.plan_week_zero("не дата") == c.plan_week_zero(None)


def test_week_range_is_always_seven_days():
    for start_date in ("2026-05-04", "2026-05-10", "2026-05-13"):
        start, end = c.plan_week_range(start_date, 3)
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
    assert c.parse_planned_day(text) == {"kind": c.KM, "km": km, "exact": True}


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
    assert c.parse_planned_day(text) == {"kind": c.UNPARSED, "km": None, "exact": False}


@pytest.mark.parametrize("text,low", [
    ("14–16 км легко", 14.0),      # en dash
    ("14—16 км", 14.0),            # em dash
    ("6-8 км очень легко", 6.0),   # hyphen
    ("10,5–12 км", 10.5),
])
def test_range_takes_the_lower_bound_and_is_marked_inexact(text, low):
    """«14–16 км» — это не 16. Округление вверх выглядит точным числом,
    не будучи им; берём нижнюю границу и помечаем как неточную."""
    assert c.parse_planned_day(text) == {"kind": c.KM, "km": low, "exact": False}


def test_range_with_intervals_stays_unparsed():
    """«6–8 км + 4×80» — интервалы съедают любую попытку посчитать объём."""
    assert c.parse_planned_day("6–8 км очень легко + 4×80")["kind"] == c.UNPARSED


def test_reversed_range_is_not_trusted():
    assert c.parse_planned_day("16–14 км")["kind"] == c.UNPARSED


def test_inexact_week_reports_a_lower_bound_without_percentage():
    """Строка из реального плана: два интервальных дня и один диапазон."""
    week = {"mon": "8 км легко", "tue": "3×1 км по 4:30–4:35",
            "wed": "6–8 км очень легко + 4×80", "thu": "СТАРТ 10 км",
            "sat": "14–16 км легко, пульс до 150"}
    result = c.week_compliance(week, date(2026, 5, 25), {})
    assert result["planned_km"] == 32.0      # 8 + 10 + 14 (нижняя граница)
    assert result["unparsed"] == 2
    assert result["approx"] == 1
    assert result["complete"] is False
    assert result["pct"] is None and result["delta_km"] is None


def test_interval_notation_is_not_mistaken_for_distance():
    """«6х800м» не должно дать «00 м» или 800 км."""
    assert c.parse_planned_day("6х800м")["km"] is None


def test_rest_is_exact():
    """День отдыха — точный ноль, а не неизвестность."""
    assert c.parse_planned_day("отдых")["exact"] is True


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


# ── Что стояло в плане на дату (#46) ──────────────────────────────────────────

PLANNED = [{"start": "17.08", "mon": "10 км", "wed": " интервалы 6×800м ", "sun": "16 км"},
           {"start": "24.08", "mon": "12 км"}]


@pytest.mark.parametrize("day,expected", [
    ("2026-08-17", (0, "mon", "10 км")),
    ("2026-08-19", (0, "wed", "интервалы 6×800м")),      # пробелы по краям срезаны
    ("2026-08-18", (0, "tue", "")),                       # день в плане, тренировки нет
    ("2026-08-24", (1, "mon", "12 км")),
    (date(2026, 8, 23), (0, "sun", "16 км")),
])
def test_planned_for_date_finds_the_cell(day, expected):
    assert c.planned_for_date(PLANNED, day, "2026-08-17") == expected


@pytest.mark.parametrize("day", ["2026-08-16", "2026-08-31", "не дата", None, ""])
def test_planned_for_date_outside_the_plan_is_none(day):
    assert c.planned_for_date(PLANNED, day, "2026-08-17") is None


def test_planned_for_date_follows_the_row_label_not_the_weekday():
    """Строка вс→сб: воскресенье — первый день окна, а не последний."""
    weeks = [{"start": "10.05", "sun": "длительная 18 км", "mon": "отдых"}]
    assert c.planned_for_date(weeks, "2026-05-10", "2026-05-10") == (0, "sun", "длительная 18 км")
    assert c.planned_for_date(weeks, "2026-05-17", "2026-05-10") is None


def test_planned_for_date_refuses_to_guess_for_an_undated_plan():
    """Ни подписей, ни plan_start: недели легли бы на даты по умолчанию."""
    assert c.planned_for_date([{"mon": "10 км"}], c.DEFAULT_PLAN_START) is None
    assert c.planned_for_date([], "2026-08-17", "2026-08-17") is None


# ── Совместимость с прежней точкой входа ──────────────────────────────────────

def test_storage_delegates_to_compliance(storage_module):
    """storage.current_plan_week_idx остаётся публичным именем (#36)."""
    assert storage_module.current_plan_week_idx("2026-05-10", 13) == \
        c.current_week_idx("2026-05-10", 13)
