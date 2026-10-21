"""Недельная нагрузка по двум осям (#66): формулы, пороги, недели.

Модуль чистый — ни GCS, ни сети, поэтому импортируется напрямую. Данные —
короткие ручные сэмплы, где ответ считается в уме.
"""
import itertools
import math
from datetime import date, timedelta

import pytest

import load as L

ZONES = [{"name": "Z1", "from": 100, "to": 120}, {"name": "Z2", "from": 120, "to": 140},
         {"name": "Z3", "from": 140, "to": 160}, {"name": "Z4", "from": 160, "to": 175},
         {"name": "Z5", "from": 175, "to": 190}]

TODAY = date(2026, 10, 20)          # вторник: текущая неделя — с 19.10


def ctx(internal="edwards", threshold=250, zones=ZONES, hr_max=190, hr_rest=None, sex=None):
    return {"zones": zones, "zones_source": "auto", "hr_max": hr_max, "hr_rest": hr_rest,
            "sex": sex, "internal": internal, "threshold_pace_sec": threshold,
            "threshold_source": "manual" if threshold else None,
            "threshold_basis": None, "threshold_auto": None}


def samples(times, **columns):
    return {"samples": {"t_offset_sec": times, **columns}}


def steady(n=61, step=5, pace=250, alt=None, climb=0.0):
    """n сэмплов через step секунд с одним темпом и равномерным набором высоты."""
    times = [i * step for i in range(n)]
    columns = {"pace_sec_per_km": [pace] * n}
    if alt is not None:
        columns["altitude_m"] = [alt + climb * i for i in range(n)]
    return samples(times, **columns)


def mean_key(hist):
    return sum(k * s for k, s in hist.items()) / sum(hist.values())


_ids = itertools.count(1)


def run(day, hr=125, time="30:00", pace="4:10", type="easy", **extra):
    """Пробежка без FIT: 30 минут в Z2 на пороговом темпе 4:10.
    TRIMP по Эдвардсу 30 × 2 = 60, rTSS 0,5 ч × 1² × 100 = 50."""
    return {"id": next(_ids), "date": day.isoformat(), "dist": 7.2, "hr": hr,
            "time": time, "pace": pace, "type": type, **extra}


def weekly(runs, entries=None, c=None, today=TODAY):
    return L.weekly_load(runs, entries or {}, c or ctx(), today)


# ── Гистограммы ──────────────────────────────────────────────────────────────

def test_hr_histogram_counts_seconds_per_beat():
    hist = L.hr_histogram(samples([0, 10, 20, 30], hr=[120, 120, 150, 150]))
    assert hist == {120: 20, 150: 10}           # последний сэмпл времени не держит


def test_hr_histogram_does_not_count_a_watch_pause():
    hist = L.hr_histogram(samples([0, 10, 910, 920], hr=[120] * 4))
    assert hist == {120: 10 + L.ZONE_GAP_CAP_SEC + 10}


def test_hr_histogram_skips_missing_pulse():
    hist = L.hr_histogram(samples([0, 10, 20, 30, 40], hr=[None, 120, 20, 250, 120]))
    assert hist == {120: 10}                    # 20 и 250 — сбой датчика


def _old_zone_minutes(details, zones):
    """hr_zone_minutes до #66 — эталон: перенос на гистограмму его не меняет."""
    s = details["samples"]
    times, hrs = s["t_offset_sec"], s["hr"]
    seconds = [0.0] * (len(zones) + 1)
    for i in range(len(times) - 1):
        span = min(times[i + 1] - times[i], 30)
        if not hrs[i] or span <= 0:
            continue
        seconds[sum(1 for z in zones if hrs[i] >= z["from"])] += span
    return seconds


@pytest.mark.parametrize("zones", [
    ZONES,
    [{"name": "Z1", "from": 95, "to": 131}, {"name": "Z2", "from": 131, "to": 142},
     {"name": "Z3", "from": 142, "to": 155}, {"name": "Z4", "from": 155, "to": 168},
     {"name": "Z5", "from": 168, "to": None}],
])
def test_zone_seconds_match_hr_zone_minutes(zones):
    pulses = [90 + (i * 37) % 100 for i in range(200)]
    times = list(itertools.accumulate([0] + [3 + i % 9 for i in range(199)]))
    times[120:] = [t + 600 for t in times[120:]]            # пауза часов
    details = samples(times, hr=pulses)
    assert L.zone_seconds(L.hr_histogram(details), zones) == _old_zone_minutes(details, zones)


def test_speed_is_smoothed_over_30_seconds():
    details = steady(pace=333)
    details["samples"]["pace_sec_per_km"][30] = 154            # всплеск 6,5 м/с
    hist = L.speed_histogram(details)
    assert max(hist) < 400                                   # без сглаживания было бы 650
    assert sum(hist.values()) == 60 * 5


def test_gps_spike_above_7_mps_is_dropped():
    details = steady(pace=333)
    details["samples"]["pace_sec_per_km"][30] = 100            # 10 м/с
    hist = L.speed_histogram(details)
    assert max(hist) < 400
    assert sum(hist.values()) == 60 * 5 - 5                    # отрезок выброса не считается


def test_standing_with_gps_drift_is_not_moving_time():
    """Полчаса у светофора с часами (дрейф 0,2 м/с) не добавляют часов в rTSS:
    иначе час на пороге выглядел бы как 122."""
    details = steady(n=720, pace=250)                       # час на пороге 4:10
    details["samples"]["t_offset_sec"] += [3600 + 5 * i for i in range(360)]
    details["samples"]["pace_sec_per_km"] += [5000] * 360   # 0,2 м/с — стоим
    hist = L.speed_histogram(details)
    assert sum(hist.values()) == 3600
    assert L.external_load(hist, 250) == pytest.approx(100)


def test_uphill_counts_faster_and_downhill_slower():
    # 3 м/с, 15 м за сэмпл: ±0,75 м — уклон 5 %
    flat = L.speed_histogram(steady(pace=333, alt=100))
    up = L.speed_histogram(steady(pace=333, alt=100, climb=0.75))
    down = L.speed_histogram(steady(pace=333, alt=100, climb=-0.75))
    assert flat == {300: 300}
    assert mean_key(up) > 380 and mean_key(down) < 240


def test_grade_is_clipped_at_30_percent():
    at_30 = L.speed_histogram(steady(pace=333, alt=100, climb=4.5))
    at_50 = L.speed_histogram(steady(pace=333, alt=100, climb=7.5))
    assert at_30 == at_50


def test_flat_run_keeps_its_speed():
    assert L.speed_histogram(steady(pace=250)) == {400: 300}            # высоты нет
    assert L.speed_histogram(steady(pace=250, alt=150)) == {400: 300}   # дорожка


@pytest.mark.parametrize("details", [
    samples([0, 5, 10], pace_sec_per_km=[None, None, None]),
    samples([0, 5, 10], hr=[120, 120, 120]),
    samples([0, 5, 10], pace_sec_per_km=[250]),
    {},
])
def test_run_without_speed_samples_has_no_speed_histogram(details):
    assert L.speed_histogram(details) == {}


# ── Внутренняя ось ───────────────────────────────────────────────────────────

def test_edwards_weights_minutes_by_zone():
    assert L.internal_load({125: 600, 165: 300}, ctx()) == pytest.approx(10 * 2 + 5 * 4)


def test_time_below_first_zone_weighs_nothing():
    assert L.internal_load({90: 600}, ctx()) == 0


@pytest.mark.parametrize("sex,a,b", [("m", 0.64, 1.92), ("f", 0.86, 1.67)])
def test_banister_matches_a_hand_calculation(sex, a, b):
    c = ctx(internal="banister", hr_rest=50, hr_max=190, sex=sex)
    # 10 минут на пульсе 120: резерв (120 − 50) / (190 − 50) = 0,5
    assert L.internal_load({120: 600}, c) == pytest.approx(10 * 0.5 * a * math.exp(b * 0.5))


@pytest.mark.parametrize("profile,method", [
    ({"hr_rest": 50, "sex": "m"}, "banister"),
    ({"hr_rest": 50, "sex": "f"}, "banister"),
    ({"sex": "m"}, "edwards"),
    ({"hr_rest": 50, "sex": ""}, "edwards"),
    ({"hr_rest": 200, "sex": "m"}, "edwards"),          # покой выше максимума — не Банистер
])
def test_banister_falls_back_to_edwards_without_rest_pulse_or_sex(profile, method):
    derived = {"hr_zones": ZONES, "hr_max_effective": 190, "hr_zones_source": "auto"}
    assert L.axes(profile, derived, [], TODAY)["internal"] == method


def test_pulse_far_above_max_is_ignored():
    assert L.internal_load({125: 600, 215: 600}, ctx()) == L.internal_load({125: 600}, ctx())


def test_no_zones_means_no_internal_load():
    c = L.axes({}, {"hr_zones": [], "hr_max_effective": None}, [], TODAY)
    assert c["internal"] is None
    assert L.run_load(run(TODAY), {"hr": {125: 600}, "spd": {}}, c)["int"] is None


@pytest.mark.parametrize("fields", [
    {"time": "50:00"},
    {"time": "", "pace": "5:00", "dist": 10},
    {"time": "мусор", "pace": "5:00", "dist": "10"},
])
def test_internal_estimate_from_average_pulse(fields):
    load = L.run_load({"hr": 125, **fields}, None, ctx())
    assert load["int"] == pytest.approx(50 * 2) and load["int_est"] is True


# ── Внешняя ось ──────────────────────────────────────────────────────────────

def test_hour_at_threshold_is_100_and_half_hour_is_50():
    assert L.external_load({400: 3600}, 250) == pytest.approx(100)
    assert L.external_load({400: 1800}, 250) == pytest.approx(50)


def test_intervals_outweigh_a_steady_run_of_the_same_average():
    steady_run = L.external_load({400: 3600}, 250)
    intervals = L.external_load({450: 1800, 350: 1800}, 250)
    assert intervals > steady_run


def test_external_estimate_from_average_pace_without_fit():
    load = L.run_load({"pace": "4:10", "time": "1:00:00", "dist": 14.4}, None, ctx())
    assert load["ext"] == pytest.approx(100) and load["ext_est"] is True


def test_no_threshold_means_no_external_load():
    load = L.run_load(run(TODAY), {"hr": {}, "spd": {400: 3600}}, ctx(threshold=None))
    assert load["ext"] is None


def test_fit_histograms_win_over_averages():
    load = L.run_load(run(TODAY, hr=180, pace="3:00"), {"hr": {125: 600}, "spd": {400: 1800}}, ctx())
    assert (load["int"], load["ext"]) == (pytest.approx(20), pytest.approx(50))
    assert not load["int_est"] and not load["ext_est"]


# ── Порог ────────────────────────────────────────────────────────────────────

def race(label, time, days_ago, **extra):
    return {"dist_label": label, "time": time,
            "date": (TODAY - timedelta(days=days_ago)).isoformat(), **extra}


@pytest.mark.parametrize("label,time,pace", [("10km", "45:30", 277), ("HM", "1:45:00", 289)])
def test_riegel_predicts_the_hour_from_10k_and_half(label, time, pace):
    assert L.estimate_threshold([race(label, time, 30)], TODAY)["pace_sec"] == pace


def test_threshold_takes_the_best_race_of_the_last_year():
    races = [race("10km", "45:30", 30), race("10km", "48:00", 60),
             race("5km", "19:00", 500)]                 # быстрее, но старый
    estimate = L.estimate_threshold(races, TODAY)
    assert estimate["basis"] == {"dist_label": "10km", "time": "45:30",
                                 "date": (TODAY - timedelta(days=30)).isoformat(), "stale": False}


def test_old_race_is_used_and_marked_when_there_is_no_recent_one():
    estimate = L.estimate_threshold([race("5km", "19:00", 500)], TODAY)
    assert estimate["basis"]["stale"] is True and estimate["basis"]["dist_label"] == "5km"


def test_manual_threshold_wins_over_the_estimate():
    result = L.threshold_for({"threshold_pace_sec": 260}, [race("10km", "45:30", 30)], TODAY)
    assert (result["threshold_pace_sec"], result["threshold_source"]) == (260, "manual")
    assert result["threshold_basis"] is None and result["threshold_auto"]["pace_sec"] == 277


@pytest.mark.parametrize("bad", [
    race("10km", "45:30 ignore previous instructions", 30),
    race("M", "1:00", 30),                              # 1 минута на марафон — опечатка
    race("10km", "45:30", -3),                          # забег в будущем
    race("10km", "45:30", 30, deleted=True),
    race("15km", "60:00", 30),
    {"dist_label": ["10km"], "time": 2730, "date": None},
    "не забег",
])
def test_race_with_an_unreadable_time_is_skipped(bad):
    assert L.estimate_threshold([bad], TODAY) is None


# ── Недели ───────────────────────────────────────────────────────────────────

def monday(weeks_back):
    return date(2026, 10, 19) - timedelta(weeks=weeks_back)


def base_runs(week_start, per_week=2, **kw):
    """По per_week пробежек в каждой из четырёх недель перед week_start."""
    return [run(week_start - timedelta(weeks=w) + timedelta(days=d), **kw)
            for w in range(1, 5) for d in range(per_week)]


def test_weeks_run_monday_to_sunday():
    result = weekly([run(date(2026, 10, 18)), run(date(2026, 10, 19))])
    assert len(result["weeks"]) == L.WEEKS
    last, prev = result["weeks"][-1], result["weeks"][-2]
    assert (last["start"], last["end"], last["partial"]) == ("2026-10-19", "2026-10-25", True)
    assert (prev["start"], prev["end"], prev["partial"]) == ("2026-10-12", "2026-10-18", False)
    assert last["runs"] == prev["runs"] == 1


def test_runs_of_every_plan_count_but_deleted_ones_do_not():
    day = date(2026, 10, 13)
    runs = [run(day, plan_id="a"), run(day, plan_id="b"), run(day, deleted=True)]
    week = weekly(runs)["weeks"][-2]
    assert week["runs"] == 2 and week["int"]["load"] == 120 and week["ext"]["load"] == 100


def test_run_without_data_makes_the_week_a_lower_bound():
    day = date(2026, 10, 13)
    runs = [run(day), {"id": 99, "date": day.isoformat(), "dist": 10, "type": "easy"}]
    week = weekly(runs)["weeks"][-2]
    assert (week["int"]["load"], week["int"]["unknown"]) == (60, 1)
    assert (week["ext"]["load"], week["ext"]["unknown"]) == (50, 1)


def test_usual_load_does_not_include_the_week_itself():
    start = monday(1)
    runs = base_runs(start) + [run(start + timedelta(days=d)) for d in range(3)]
    week = weekly(runs)["weeks"][-2]
    assert week["int"]["chronic"] == 120                # 2 × 60 в каждой из 4 недель
    assert (week["int"]["ratio"], week["int"]["band"]) == (1.5, "high")
    assert (week["ext"]["ratio"], week["ext"]["band"]) == (1.5, "high")


@pytest.mark.parametrize("ratio,band", [
    (0.79, "low"), (0.8, "ok"), (1.0, "ok"), (1.3, "ok"), (1.31, "high"),
    (1.5, "high"), (1.51, "spike"), (None, None)])
def test_bands_switch_at_08_13_15(ratio, band):
    assert L.load_band(ratio) == band


def _without_first_base_week(start):
    return [r for r in base_runs(start) if date.fromisoformat(r["date"]) >= start - timedelta(weeks=3)]


@pytest.mark.parametrize("layout", [
    _without_first_base_week,                                       # история короче 4 недель
    lambda start: base_runs(start)[::3],                            # 3 пробежки в базе
    lambda start: [run(start - timedelta(days=60))],                # перерыв: база пуста
])
def test_too_little_history_gives_no_rating(layout):
    start = monday(1)
    week = weekly(layout(start) + [run(start)])["weeks"][-2]
    assert week["int"]["chronic"] is None and week["int"]["ratio"] is None
    assert week["int"]["band"] is None and week["ext"]["band"] is None


def test_current_week_is_partial_and_not_rated():
    start = monday(0)
    week = weekly(base_runs(start) + [run(start)])["weeks"][-1]
    assert week["partial"] and week["int"]["chronic"] == 120
    assert week["int"]["ratio"] is None and week["int"]["band"] is None


def test_rolling_week_compares_to_the_28_days_before():
    acute = [run(TODAY - timedelta(days=d)) for d in (0, 2, 5)]           # 14.10–20.10
    base = [run(TODAY - timedelta(days=7 + d)) for d in range(0, 28, 4)]  # 7 пробежек
    base.append(run(TODAY - timedelta(days=34)))                          # 16.09 — начало базы
    rolling = weekly(acute + base)["rolling"]
    assert (rolling["start"], rolling["end"]) == ("2026-10-14", "2026-10-20")
    assert rolling["int"]["load"] == 180 and rolling["int"]["chronic"] == 120
    assert (rolling["int"]["ratio"], rolling["int"]["band"]) == (1.5, "high")


def test_run_logged_after_local_midnight_moves_today():
    assert weekly([run(date(2026, 10, 21))])["today"] == "2026-10-21"
    assert weekly([run(date(2026, 10, 1))])["today"] == "2026-10-20"


def test_far_future_run_does_not_move_today():
    """Опечатка в дате (год не тот) не сдвигает окно у спортсмена навсегда."""
    assert weekly([run(date(2026, 10, 30))])["today"] == "2026-10-20"
    assert weekly([run(date(2027, 10, 19)), run(date(2026, 10, 18))])["today"] == "2026-10-20"


def test_band_follows_the_ratio_shown():
    """1 ч 18 мин 10 с при обычных 120 / 100: отношение 1,3028 показывается как
    ×1,30 — и оценка «в норме», как обещано в «Как считается»."""
    start = monday(1)
    week = weekly(base_runs(start) + [run(start, time="1:18:10")])["weeks"][-2]
    for key in ("int", "ext"):
        assert (week[key]["ratio"], week[key]["band"]) == (1.3, "ok"), key


def test_intensity_split_by_zone_groups():
    day = date(2026, 10, 13)
    fit_run = run(day)
    runs = [run(day), fit_run]                                            # 30 мин в Z2 по среднему
    entries = {str(fit_run["id"]): {"hr": {150: 600, 170: 300, 180: 120, 90: 60}, "spd": {}}}
    week = weekly(runs, entries)["weeks"][-2]
    assert (week["easy"], week["moderate"], week["hard"]) == (31, 10, 7)


def test_no_zones_means_no_intensity_split():
    week = weekly([run(date(2026, 10, 13))], c=ctx(internal=None, zones=[]))["weeks"][-2]
    assert week["easy"] is None and week["int"] is None


def test_runs_map_carries_every_run():
    fit_run, plain = run(date(2026, 10, 13)), run(date(2026, 10, 14))
    entries = {str(fit_run["id"]): {"hr": {125: 600}, "spd": {400: 1800}}}
    runs = weekly([fit_run, plain], entries)["runs"]
    assert runs[str(fit_run["id"])] == {"int": 20, "int_est": False, "ext": 50, "ext_est": False}
    assert runs[str(plain["id"])] == {"int": 60, "int_est": True, "ext": 50, "ext_est": True}


# ── Цена работы ──────────────────────────────────────────────────────────────

def test_cost_counts_only_aerobic_runs():
    start = monday(1)
    week_runs = [run(start, hr=145), run(start + timedelta(days=2), hr=145),     # Z3: 90 TRIMP
                 run(start + timedelta(days=4), hr=185, type="interval")]
    week = weekly(base_runs(start) + week_runs)["weeks"][-2]
    # база: 60 / 50 = 1,2; неделя: 90 / 50 = 1,8 → на 50 % дороже. Интервалы не в счёт.
    assert week["cost"] == {"dev_pct": 50, "band": "higher"}


@pytest.mark.parametrize("dev,band", [
    (10, "usual"), (10.5, "higher"), (-10, "usual"), (-10.5, "lower"), (0, "usual"), (None, None)])
def test_cost_bands_switch_at_10_percent(dev, band):
    assert L.cost_band(dev) == band


@pytest.mark.parametrize("layout", [
    lambda start: base_runs(start) + [run(start)],                           # одна в неделе
    lambda start: base_runs(start)[:3] + [run(start), run(start)],           # три в базе
    lambda start: base_runs(start, type="tempo") + [run(start), run(start)],  # база не аэробная
])
def test_cost_needs_two_runs_a_week_and_four_in_the_base(layout):
    start = monday(1)
    assert weekly(layout(start))["weeks"][-2]["cost"] is None


def test_cost_needs_both_axes():
    start = monday(1)
    runs = base_runs(start) + [run(start), run(start)]
    assert weekly(runs, c=ctx(threshold=None))["weeks"][-2]["cost"] is None
