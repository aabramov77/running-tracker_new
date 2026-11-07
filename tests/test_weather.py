"""Прогноз погоды и советы к тренировке (#72).

Сети нет: ответ Open-Meteo собирается здесь той же формы, что настоящий
(проверено на живом ответе), а _http_get подменён в conftest.
"""
from datetime import date, datetime, timedelta

import pytest

import compliance as c
import weather as w

# 2026-11-07 08:00 по Москве (UTC+3). Прогноз начинается вчерашним днём:
# past_days=1 нужен, чтобы видеть осадки прошлой ночи.
NOW = datetime(2026, 11, 7, 5, 0)
TODAY = date(2026, 11, 7)
FIRST = TODAY - timedelta(days=1)

BASE_HOUR = {"temperature_2m": 10, "apparent_temperature": 10, "precipitation": 0,
             "precipitation_probability": 0, "weather_code": 3, "wind_speed_10m": 2,
             "wind_gusts_10m": 4, "uv_index": 1}


def raw_forecast(days=8, offset=10800, sunrise="07:48", sunset="16:37"):
    dates = [(FIRST + timedelta(days=i)).isoformat() for i in range(days)]
    times = [f"{d}T{h:02d}:00" for d in dates for h in range(24)]
    hourly = {"time": times, **{k: [v] * len(times) for k, v in BASE_HOUR.items()}}
    daily = {"time": dates, "weather_code": [3] * days,
             "temperature_2m_min": [5] * days, "temperature_2m_max": [12] * days,
             "precipitation_sum": [0] * days,
             "sunrise": [f"{d}T{sunrise}" for d in dates],
             "sunset": [f"{d}T{sunset}" for d in dates]}
    return {"utc_offset_seconds": offset, "timezone": "Europe/Moscow",
            "hourly": hourly, "daily": daily}


def set_hours(raw, when, **values):
    """Значения на час «2026-11-07T08:00» (или на список часов)."""
    for stamp in ([when] if isinstance(when, str) else when):
        i = raw["hourly"]["time"].index(stamp)
        for key, value in values.items():
            raw["hourly"][key][i] = value
    return raw


def forecast(raw=None):
    return w.parse_forecast(raw or raw_forecast())


def at_eight(**values):
    """Погода дня TODAY в 08:00 с заданными значениями."""
    raw = set_hours(raw_forecast(), f"{TODAY}T08:00", **values)
    return w.day_weather(forecast(raw), TODAY, (8, 0))


# ── Тип тренировки ────────────────────────────────────────────────────────────

# Ячейки — из docs/samples/plan-example.csv
@pytest.mark.parametrize("text,kind", [
    ("", c.WORKOUT_REST),
    ("Отдых", c.WORKOUT_REST),
    ("6–8 км легко", c.WORKOUT_EASY),
    ("8–10 км средний", c.WORKOUT_EASY),
    ("4 км восстановительный", c.WORKOUT_EASY),
    ("14–16 км легко", c.WORKOUT_EASY),
    ("18 км легко", c.WORKOUT_LONG),
    ("Длительная 25 км, последние 3 в темпе", c.WORKOUT_LONG),
    ("3×7 мин по 4:35–4:40", c.WORKOUT_QUALITY),
    ("4×2 км по 4:48–4:50", c.WORKOUT_QUALITY),
    ("8 км по 5:05–5:15", c.WORKOUT_QUALITY),
    ("Темповый 6 км", c.WORKOUT_QUALITY),
    ("Фартлек 40 мин", c.WORKOUT_QUALITY),
    ("10 км в комфортном темпе", c.WORKOUT_EASY),
    ("СТАРТ 10 км", c.WORKOUT_RACE),
])
def test_workout_kind_from_plan_cell(text, kind):
    assert c.workout_kind(text) == kind


# ── Ответ провайдера ──────────────────────────────────────────────────────────

def test_parse_forecast_indexes_hours_and_days():
    fc = forecast()
    assert fc["utc_offset"] == 10800 and fc["timezone"] == "Europe/Moscow"
    assert fc["hours"][fc["index"][f"{TODAY}T08:00"]]["time"] == f"{TODAY}T08:00"
    assert set(fc["days"]) >= {TODAY.isoformat(), FIRST.isoformat()}


@pytest.mark.parametrize("raw", [None, {}, {"hourly": {"time": []}, "daily": {}},
                                 {"hourly": {"time": ["2026-11-07T00:00"]}}])
def test_parse_forecast_rejects_garbage(raw):
    assert w.parse_forecast(raw) is None


def test_today_is_local_to_the_place_not_utc():
    """22:00 UTC 6-го — в Москве уже 7-е: «сегодня» по месту бега."""
    assert w.local_today(forecast(), datetime(2026, 11, 6, 22, 0)) == TODAY


@pytest.mark.parametrize("text,expected", [
    ("07:30", (7, 30)), ("19:05", (19, 5)), ("", (8, 0)), (None, (8, 0)),
    ("25:00", (8, 0)), ("7:30", (8, 0)),
])
def test_run_time_falls_back_to_default(text, expected):
    assert w.parse_run_time(text) == expected


def test_values_are_taken_at_the_nearest_hour():
    raw = set_hours(raw_forecast(), f"{TODAY}T08:00", apparent_temperature=-4)
    raw = set_hours(raw, f"{TODAY}T07:00", apparent_temperature=20)
    wx = w.day_weather(forecast(raw), TODAY, (7, 30))
    assert wx["public"]["at_run"]["feels"] == -4
    assert wx["public"]["at_run"]["time"] == "07:30"


def test_day_outside_forecast_is_none():
    assert w.day_weather(forecast(), TODAY + timedelta(days=30), (8, 0)) is None


# ── Оценка погоды ─────────────────────────────────────────────────────────────

@pytest.mark.parametrize("values,level,why", [
    ({}, w.OK, ""),
    ({"weather_code": 95}, w.DANGER, "гроза"),
    ({"weather_code": 66}, w.DANGER, "ледяной дождь"),
    ({"apparent_temperature": -25}, w.DANGER, "мороз −25°"),
    ({"apparent_temperature": -16}, w.HARD, "мороз −16°"),
    ({"apparent_temperature": -14}, w.OK, ""),
    ({"apparent_temperature": 33}, w.DANGER, "жара +33°"),
    ({"apparent_temperature": 28}, w.HARD, "жара +28°"),
    ({"wind_gusts_10m": 21}, w.DANGER, "шквал 21 м/с"),
    ({"wind_gusts_10m": 15}, w.HARD, "порывы 15 м/с"),
    ({"precipitation": 2.5, "weather_code": 65}, w.HARD, "ливень"),
    ({"weather_code": 75}, w.HARD, "сильный снег"),
])
def test_level_and_reason(values, level, why):
    wx = at_eight(**values)
    assert (wx["level"], wx["why"]) == (level, why)


def test_slippery_needs_both_zero_and_recent_precipitation():
    dry = at_eight(temperature_2m=0)
    assert dry["level"] == w.OK
    raw = set_hours(raw_forecast(), f"{TODAY}T08:00", temperature_2m=0)
    raw = set_hours(raw, f"{TODAY}T02:00", precipitation=0.5)    # ночью шёл дождь
    wx = w.day_weather(forecast(raw), TODAY, (8, 0))
    assert (wx["level"], wx["why"]) == (w.HARD, "скользко")


def test_night_precipitation_of_yesterday_counts():
    """Ради этого прогноз берётся с past_days=1: ранним утром «12 часов
    назад» — это вчерашний вечер."""
    raw = set_hours(raw_forecast(), f"{TODAY}T06:00", temperature_2m=-1)
    raw = set_hours(raw, f"{FIRST}T22:00", precipitation=1.0)
    assert "slippery" in w.day_weather(forecast(raw), TODAY, (6, 0))["flags"]


@pytest.mark.parametrize("run,dark", [((7, 0), True), ((8, 0), False),
                                      ((16, 30), False), ((17, 0), True)])
def test_dark_by_exact_time_against_sunrise_and_sunset(run, dark):
    wx = w.day_weather(forecast(), TODAY, run)
    assert ("dark" in wx["flags"]) is dark


def test_warm_day_suggests_the_coolest_hour():
    raw = raw_forecast()
    day = [f"{TODAY}T{h:02d}:00" for h in range(24)]
    raw = set_hours(raw, day, apparent_temperature=26)
    raw = set_hours(raw, f"{TODAY}T06:00", apparent_temperature=19)
    raw = set_hours(raw, f"{TODAY}T03:00", apparent_temperature=15)   # ночь не в счёт
    wx = w.day_weather(forecast(raw), TODAY, (18, 0))
    assert wx["cooler"] == {"time": "06:00", "feels": 19}


# ── Место ─────────────────────────────────────────────────────────────────────

MOVE_TO = date(2026, 11, 12)    # четверг


@pytest.mark.parametrize("level,kind,venues,move_to,code", [
    (w.OK, c.WORKOUT_QUALITY, set(), None, "outdoor"),
    (w.HARD, c.WORKOUT_EASY, set(), None, "outdoor"),
    (w.HARD, c.WORKOUT_LONG, {"treadmill"}, None, "outdoor"),
    (w.HARD, c.WORKOUT_QUALITY, {"treadmill", "manege"}, None, "manege"),
    (w.HARD, c.WORKOUT_QUALITY, {"treadmill"}, None, "treadmill"),
    (w.DANGER, c.WORKOUT_EASY, {"treadmill", "manege"}, None, "treadmill"),
    (w.DANGER, c.WORKOUT_LONG, {"manege"}, None, "manege"),
    (w.DANGER, c.WORKOUT_EASY, set(), MOVE_TO, "move"),
    (w.DANGER, c.WORKOUT_EASY, set(), None, "move"),
    (w.HARD, c.WORKOUT_QUALITY, set(), MOVE_TO, "move"),
    (w.HARD, c.WORKOUT_QUALITY, set(), None, "outdoor"),
])
def test_venue(level, kind, venues, move_to, code):
    assert w.choose_venue(level, kind, venues, "гололёд", move_to)["code"] == code


def test_venue_reason_and_labels():
    hard_easy = w.choose_venue(w.HARD, c.WORKOUT_EASY, set(), "скользко")
    assert (hard_easy["label"], hard_easy["why"]) == ("Улица", "скользко")
    assert w.choose_venue(w.OK, c.WORKOUT_EASY, set(), "")["why"] == ""
    moved = w.choose_venue(w.DANGER, c.WORKOUT_QUALITY, set(), "гроза", MOVE_TO)
    assert moved["label"] == "Перенести на чт 12.11" and moved["date"] == "2026-11-12"
    assert w.choose_venue(w.HARD, c.WORKOUT_QUALITY, set(), "скользко")["label"] \
        == "Улица — без ускорений"


@pytest.mark.parametrize("kind", [None, c.WORKOUT_REST, c.WORKOUT_RACE])
def test_no_venue_for_rest_race_and_days_outside_plan(kind):
    assert w.choose_venue(w.DANGER, kind, {"manege"}, "гроза") is None


# ── Одежда ────────────────────────────────────────────────────────────────────

def wear(feels, kind=c.WORKOUT_EASY, sensitivity="", venue=None, **values):
    wx = at_eight(apparent_temperature=feels, **values)
    return w.clothing(wx, kind, sensitivity, venue)


@pytest.mark.parametrize("feels,top", [
    (22, "майка"), (21.9, "футболка"), (15, "футболка"),
    (9, "футболка или лонгслив"), (3, "лонгслив"), (2.9, "термобельё + ветровка"),
    (-3, "термобельё + ветровка"), (-10, "термобельё + утеплённый верх + ветровка"),
    (-10.1, "термобельё + флис + ветрозащитная куртка"),
])
def test_clothing_ladder_boundaries(feels, top):
    assert wear(feels)["top"] == top


def test_cold_sensitivity_shifts_clothing_three_degrees():
    assert wear(10)["top"] == "футболка или лонгслив"
    assert wear(10, sensitivity="cold")["top"] == "лонгслив"          # как при +7°
    assert wear(7, sensitivity="warm")["top"] == "футболка или лонгслив"  # как при +10°


def test_intensity_dresses_four_degrees_lighter():
    assert wear(5)["top"] == "лонгслив"
    assert wear(5, kind=c.WORKOUT_QUALITY)["top"] == "футболка или лонгслив"
    assert wear(5, kind=c.WORKOUT_RACE)["top"] == "футболка или лонгслив"


def test_sensitivity_changes_clothing_but_not_danger():
    raw = set_hours(raw_forecast(), f"{TODAY}T08:00", apparent_temperature=-16)
    fc = forecast(raw)
    plain = w.build_weather(fc, {"location": {}}, now=NOW)[0]
    cold = w.build_weather(fc, {"cold_sensitivity": "cold"}, now=NOW)[0]
    assert plain["level"] == cold["level"] == w.HARD
    assert plain["clothing"]["top"] == cold["clothing"]["top"]          # оба в нижней ступени
    mild = forecast(set_hours(raw_forecast(), f"{TODAY}T08:00", apparent_temperature=4))
    assert w.build_weather(mild, {"cold_sensitivity": "cold"}, now=NOW)[0]["clothing"]["top"] \
        != w.build_weather(mild, {}, now=NOW)[0]["clothing"]["top"]


def test_rain_makes_the_shell_waterproof():
    assert wear(0, precipitation=0.5)["top"] == "термобельё + непромокаемая ветровка"
    warmish = wear(12, precipitation=0.5)
    assert "непромокаемая ветровка" in warmish["extra"]
    assert warmish["head"] == "кепка с козырьком"
    assert "непромокаемая ветровка" not in wear(18, precipitation=0.5)["extra"]


def test_feet_on_slippery_and_snow():
    assert wear(5)["feet"] == "кроссовки"
    assert "агрессивным протектором" in wear(-2, weather_code=73)["feet"]
    raw = set_hours(raw_forecast(), f"{TODAY}T08:00", temperature_2m=0, precipitation=0.4)
    wx = w.day_weather(forecast(raw), TODAY, (8, 0))
    assert "шипами" in w.clothing(wx, c.WORKOUT_EASY, "")["feet"]


def test_extras_for_wind_uv_dark_and_warm():
    assert "ветрозащитный верх" in wear(10, wind_speed_10m=9)["extra"]
    assert "ветрозащитный верх" not in wear(0, wind_speed_10m=9)["extra"]   # уже ветровка
    assert "солнцезащитные очки, SPF" in wear(20, uv_index=7)["extra"]
    assert "вода" in wear(23)["extra"]
    assert "вода" in wear(16, kind=c.WORKOUT_LONG)["extra"]
    wx = w.day_weather(forecast(), TODAY, (7, 0))
    assert "светоотражатели, фонарь" in w.clothing(wx, c.WORKOUT_EASY, "")["extra"]


def test_indoor_clothing_ignores_the_weather():
    assert wear(-20, venue="manege")["top"] == "майка или футболка"
    assert wear(-20, venue="treadmill")["hands"] == ""


# ── Неделя целиком ────────────────────────────────────────────────────────────

# Строка плана пн 02.11 – вс 08.11 и следующая
WEEKS = [
    {"start": "02.11", "end": "08.11", "mon": "8 км легко", "tue": "6×800 по 3:50",
     "wed": "", "thu": "10 км легко", "fri": "", "sat": "6×1 км по 4:30", "sun": "18 км легко"},
    {"start": "09.11", "end": "15.11", "mon": "8 км легко", "tue": "", "wed": "Отдых",
     "thu": "Темповый 6 км", "fri": "", "sat": "", "sun": "СТАРТ 10 км"},
]
PROFILE = {"location": {"name": "Москва", "lat": 55.75, "lon": 37.62},
           "run_time": "08:00", "venues": ["manege"], "cold_sensitivity": ""}


def test_week_starts_today_and_maps_plan_days():
    days = w.build_weather(forecast(), PROFILE, WEEKS, "2026-11-02", now=NOW)
    assert [d["date"] for d in days] == [(TODAY + timedelta(days=i)).isoformat() for i in range(7)]
    first = days[0]
    assert (first["field"], first["week_idx"], first["workout"], first["kind"]) == \
        ("sat", 0, "6×1 км по 4:30", c.WORKOUT_QUALITY)
    assert days[1]["kind"] == c.WORKOUT_LONG and days[1]["week_idx"] == 0
    assert days[2]["week_idx"] == 1                               # следующая строка плана


def test_rest_day_has_weather_but_no_advice():
    days = w.build_weather(forecast(), PROFILE, WEEKS, "2026-11-02", now=NOW)
    wed = next(d for d in days if d["date"] == "2026-11-11")
    assert wed["kind"] == c.WORKOUT_REST
    assert wed["weather"]["at_run"]["t"] == 10
    assert (wed["venue"], wed["clothing"], wed["notes"]) == (None, None, [])


def test_without_plan_days_get_weather_and_clothing_only():
    days = w.build_weather(forecast(), PROFILE, now=NOW)
    assert all(d["kind"] is None and d["venue"] is None for d in days)
    assert all(d["clothing"]["top"] for d in days)


def test_undated_plan_gets_no_workouts():
    """Как в план/факте: подставить тренировку с угаданного дня хуже, чем ничего."""
    undated = [{k: v for k, v in WEEKS[0].items() if k not in ("start", "end")}]
    days = w.build_weather(forecast(), PROFILE, undated, None, now=NOW)
    assert all(d["kind"] is None for d in days)


def test_bad_day_without_venues_moves_to_next_good_day():
    raw = set_hours(raw_forecast(), f"{TODAY}T08:00", weather_code=95)
    raw = set_hours(raw, f"{TODAY + timedelta(days=1)}T08:00", weather_code=95)
    days = w.build_weather(forecast(raw), {**PROFILE, "venues": []}, WEEKS, "2026-11-02", now=NOW)
    assert days[0]["venue"]["code"] == "move"
    assert days[0]["venue"]["date"] == (TODAY + timedelta(days=2)).isoformat()


def test_quality_on_slippery_day_goes_to_manege_and_easy_stays_out():
    raw = raw_forecast()
    for i in range(2):
        day = TODAY + timedelta(days=i)
        raw = set_hours(raw, f"{day}T08:00", temperature_2m=0, apparent_temperature=-3)
        raw = set_hours(raw, f"{day}T05:00", precipitation=1.0)
    days = w.build_weather(forecast(raw), PROFILE, WEEKS, "2026-11-02", now=NOW)
    sat, sun = days[0], days[1]
    assert (sat["venue"]["code"], sat["venue"]["why"]) == ("manege", "скользко")
    assert sat["clothing"]["top"] == "майка или футболка"
    assert sun["venue"]["code"] == "outdoor"
    assert any(n.startswith("Скользко") for n in sun["notes"])
    assert any(n.startswith("Тяжёлая погода") for n in sun["notes"])


# ── Отчёт для недели ──────────────────────────────────────────────────────────

def test_report_without_location():
    report = w.weather_report({"run_time": "07:30"})
    assert (report["status"], report["days"], report["run_time"]) == ("no_location", [], "07:30")
    assert report["source"] == "Open-Meteo.com"


def test_report_when_provider_is_down():
    assert w.weather_report(PROFILE)["status"] == "unavailable"     # сеть выключена в conftest


def test_report_ok():
    report = w.weather_report(PROFILE, WEEKS, "2026-11-02", now=NOW,
                              fetch=lambda lat, lon: forecast())
    assert report["status"] == "ok" and report["today"] == TODAY.isoformat()
    assert len(report["days"]) == 7 and report["location"]["name"] == "Москва"


# ── Кэш и геокодер ────────────────────────────────────────────────────────────

class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


def test_forecast_is_cached_and_refreshed_after_ttl(monkeypatch):
    calls = []

    def fake_get(url, params):
        calls.append(params)
        return raw_forecast()

    monkeypatch.setattr(w, "_http_get", fake_get)
    clock = Clock()
    assert w.fetch_forecast(55.751, 37.618, clock) is not None
    assert w.fetch_forecast(55.749, 37.621, clock) is not None      # те же 0,01°
    assert len(calls) == 1
    assert calls[0]["latitude"] == 55.75 and calls[0]["past_days"] == 1
    assert calls[0]["wind_speed_unit"] == "ms" and calls[0]["timezone"] == "auto"
    clock.now += w.WEATHER_TTL_SEC + 1
    w.fetch_forecast(55.75, 37.62, clock)
    assert len(calls) == 2


def test_provider_failure_is_remembered_briefly(monkeypatch):
    calls = []

    def failing(url, params):
        calls.append(url)
        raise TimeoutError("нет ответа")

    monkeypatch.setattr(w, "_http_get", failing)
    clock = Clock()
    assert w.fetch_forecast(55.75, 37.62, clock) is None
    assert w.fetch_forecast(55.75, 37.62, clock) is None
    assert len(calls) == 1
    clock.now += w.WEATHER_FAIL_TTL_SEC + 1
    w.fetch_forecast(55.75, 37.62, clock)
    assert len(calls) == 2


def test_geocode_rounds_and_caches(monkeypatch):
    calls = []

    def fake_get(url, params):
        calls.append(params)
        return {"results": [
            {"name": "Москва", "latitude": 55.75204, "longitude": 37.61781,
             "country": "Россия", "admin1": "Москва"},
            {"name": "Без координат"},
        ]}

    monkeypatch.setattr(w, "_http_get", fake_get)
    found = w.geocode("Москва")
    assert found == [{"name": "Москва", "admin1": "Москва", "country": "Россия",
                      "lat": 55.75, "lon": 37.62}]
    assert calls[0]["language"] == "ru" and calls[0]["count"] == 5
    w.geocode("москва")
    assert len(calls) == 1


def test_geocode_nothing_found_and_failure(monkeypatch):
    monkeypatch.setattr(w, "_http_get", lambda url, params: {"generationtime_ms": 0.1})
    assert w.geocode("zzqqxx") == []
    monkeypatch.setattr(w, "_http_get", lambda url, params: (_ for _ in ()).throw(OSError()))
    assert w.geocode("Тверь") is None


@pytest.mark.parametrize("query,ok", [("М", False), ("  Мо  ", True), ("x" * 101, False),
                                      ("Санкт   Петербург", True), (None, False)])
def test_geocode_query_validation(query, ok):
    text, error = w.clean_geocode_query(query)
    assert (error is None) is ok
    if ok:
        assert "  " not in text
