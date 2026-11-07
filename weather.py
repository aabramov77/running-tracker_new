"""Прогноз погоды и советы к тренировке: где бежать и что надеть (#72).

Слой домена по раскладке #36: в GCS модуль не ходит и о `storage` не знает.
Единственный внешний вызов — Open-Meteo. Погода — не бизнес-данные: она нигде
не хранится, живёт в памяти тёплого инстанса, а при сбое провайдера советов
просто нет — план рисуется как раньше.

Советы считаются правилами, а не моделью: мгновенно, бесплатно и одинаково
при каждом открытии плана. Правила смотрят на погоду в час тренировки (поле
профиля «Обычно бегаю в») и на тип тренировки из текста дня:

- **место** — опасная погода (гроза, ледяной дождь, сильный мороз, жара,
  шквал) уводит в помещение любую тренировку, тяжёлая (скользко, ливень,
  мороз, жара, сильные порывы) — только работу на скорости;
- **одежда** — по ощущаемой температуре, по зонам тела. Поправки: личная
  («мёрзну» −3°, «жарко» +3°) и на интенсивность (интервалы и старт +4°).
  Поправки меняют одежду, но не оценку опасности: мороз для всех один.

Пороги — ориентир для бегуна-любителя, а не медицинская норма.
"""
import logging
import re
import time
from datetime import datetime, timedelta

import httpx

from compliance import (DAY_FIELDS, WORKOUT_LONG, WORKOUT_QUALITY, WORKOUT_RACE,
                        WORKOUT_REST, planned_for_date, workout_kind)
from config import (GEOCODE_TTL_SEC, WEATHER_DEFAULT_RUN_TIME, WEATHER_FAIL_TTL_SEC,
                    WEATHER_FORECAST_URL, WEATHER_GEOCODE_URL, WEATHER_TIMEOUT_SEC,
                    WEATHER_TTL_SEC)
from domain import PLAN_DAYS

SOURCE = "Open-Meteo.com"
DAYS_AHEAD = 7

# Поля профиля (#72). Улица доступна всегда, поэтому в списке её нет.
VENUES = ("treadmill", "manege")
COLD_SENSITIVITY = ("", "cold", "warm")
LOCATION_NAME_MAX = 80
GEOCODE_QUERY_MIN, GEOCODE_QUERY_MAX = 2, 100

HOURLY = ("temperature_2m", "apparent_temperature", "precipitation",
          "precipitation_probability", "weather_code", "wind_speed_10m",
          "wind_gusts_10m", "uv_index")
DAILY = ("weather_code", "temperature_2m_min", "temperature_2m_max",
         "precipitation_sum", "sunrise", "sunset")

# Код погоды WMO → (значок, подпись)
WMO = {
    0: ("☀", "ясно"), 1: ("🌤", "малооблачно"), 2: ("⛅", "переменная облачность"),
    3: ("☁", "пасмурно"), 45: ("🌫", "туман"), 48: ("🌫", "туман с изморозью"),
    51: ("🌦", "слабая морось"), 53: ("🌦", "морось"), 55: ("🌦", "сильная морось"),
    56: ("🧊", "ледяная морось"), 57: ("🧊", "ледяная морось"),
    61: ("🌧", "небольшой дождь"), 63: ("🌧", "дождь"), 65: ("🌧", "сильный дождь"),
    66: ("🧊", "ледяной дождь"), 67: ("🧊", "сильный ледяной дождь"),
    71: ("🌨", "небольшой снег"), 73: ("🌨", "снег"), 75: ("❄", "сильный снег"),
    77: ("🌨", "снежная крупа"),
    80: ("🌦", "ливень"), 81: ("🌧", "ливень"), 82: ("🌧", "сильный ливень"),
    85: ("🌨", "снегопад"), 86: ("❄", "сильный снегопад"),
    95: ("⛈", "гроза"), 96: ("⛈", "гроза с градом"), 99: ("⛈", "гроза с градом"),
}
STORM_CODES = {95, 96, 99}
FREEZING_CODES = {56, 57, 66, 67}
SNOW_CODES = {71, 73, 75, 77, 85, 86}
HEAVY_SNOW_CODES = {75, 86}

# Пороги — по ощущаемой температуре и по часу тренировки
DANGER_FEELS_COLD, DANGER_FEELS_HEAT = -25, 33
HARD_FEELS_COLD, HARD_FEELS_HEAT = -15, 27
DANGER_GUSTS, HARD_GUSTS = 20, 14          # м/с
HARD_PRECIP_MM = 2.0                       # мм за час — ливень
SLIPPERY_T_MIN, SLIPPERY_T_MAX = -3, 1     # около нуля после осадок — скользко
SLIPPERY_LOOKBACK_H = 12
SLIPPERY_PRECIP_MM = 0.2
RAIN_MM, RAIN_PROB = 0.3, 60
WIND_MS = 8
UV_HIGH = 6
WARM_FEELS = 22                            # с этой — вода и подсказка о прохладном часе
COOLER_BY = 3                              # насколько прохладнее, чтобы советовать другой час
COOL_HOURS = range(5, 22)

OK, HARD, DANGER = "ok", "hard", "danger"

# Расчётная температура одежды: личная поправка и поправка на интенсивность
SENSITIVITY_SHIFT = {"cold": -3, "warm": 3}
INTENSITY_SHIFT = 4

# (от какой расчётной температуры, голова, верх, низ, руки); последняя — всё, что ниже
CLOTHING_LADDER = [
    (22, "кепка", "майка", "шорты", ""),
    (15, "", "футболка", "шорты", ""),
    (9, "", "футболка или лонгслив", "шорты", ""),
    (3, "", "лонгслив", "тайтсы", "тонкие перчатки"),
    (-3, "повязка", "термобельё + ветровка", "тайтсы", "перчатки"),
    (-10, "шапка, бафф", "термобельё + утеплённый верх + ветровка",
     "утеплённые тайтсы", "плотные перчатки"),
    (None, "шапка, балаклава", "термобельё + флис + ветрозащитная куртка",
     "утеплённые тайтсы + ветрозащитные брюки", "варежки"),
]
INDOOR_CLOTHING = {"head": "", "top": "майка или футболка", "bottom": "шорты",
                   "hands": "", "feet": "кроссовки", "extra": ["вода", "полотенце"]}

VENUE_LABELS = {"outdoor": "Улица", "manege": "Манеж", "treadmill": "Дорожка"}
INDOOR = {"manege", "treadmill"}

_DAY_RU = dict(PLAN_DAYS)
_RUN_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


# ── Провайдер и кэш ──────────────────────────────────────────────────────────

_forecast_cache = {}     # (lat, lon) → (свеж до, прогноз или None)
_geocode_cache = {}      # запрос → (свеж до, варианты)
CACHE_MAX = 256


def _http_get(url, params):
    res = httpx.get(url, params=params, timeout=WEATHER_TIMEOUT_SEC)
    res.raise_for_status()
    return res.json()


def _remember(cache, key, expires, value):
    if len(cache) >= CACHE_MAX:
        cache.pop(next(iter(cache)))         # самый старый
    cache[key] = (expires, value)


def _at(values, i):
    return values[i] if isinstance(values, list) and i < len(values) else None


def parse_forecast(raw):
    """Ответ Open-Meteo → {utc_offset, timezone, hours, index, days}; мусор → None."""
    hourly = (raw or {}).get("hourly") or {}
    daily = (raw or {}).get("daily") or {}
    times = hourly.get("time") or []
    hours = [{"time": t, **{k: _at(hourly.get(k), i) for k in HOURLY}}
             for i, t in enumerate(times)]
    days = {d: {k: _at(daily.get(k), i) for k in DAILY}
            for i, d in enumerate(daily.get("time") or [])}
    if not hours or not days:
        return None
    return {"utc_offset": int(raw.get("utc_offset_seconds") or 0),
            "timezone": raw.get("timezone") or "",
            "hours": hours,
            "index": {h["time"]: i for i, h in enumerate(hours)},
            "days": days}


def fetch_forecast(lat, lon, clock=time.monotonic):
    """Прогноз на 7 дней (и вчерашний день — для «скользко») или None.

    Ответ живёт в памяти WEATHER_TTL_SEC; отказ провайдера — WEATHER_FAIL_TTL_SEC,
    чтобы при его сбое каждый запрос не ждал таймаута заново.
    """
    key = (round(float(lat), 2), round(float(lon), 2))
    now = clock()
    cached = _forecast_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    try:
        forecast = parse_forecast(_http_get(WEATHER_FORECAST_URL, {
            "latitude": key[0], "longitude": key[1], "timezone": "auto",
            "forecast_days": DAYS_AHEAD, "past_days": 1, "wind_speed_unit": "ms",
            "hourly": ",".join(HOURLY), "daily": ",".join(DAILY)}))
    except Exception:
        logging.warning("weather: прогноз не получен", exc_info=True)
        forecast = None
    _remember(_forecast_cache, key,
              now + (WEATHER_TTL_SEC if forecast else WEATHER_FAIL_TTL_SEC), forecast)
    return forecast


def clean_geocode_query(query):
    """Запрос к геокодеру: пробелы схлопнуты; (запрос, ошибка)."""
    text = " ".join(str(query or "").split())
    if not GEOCODE_QUERY_MIN <= len(text) <= GEOCODE_QUERY_MAX:
        return None, f"от {GEOCODE_QUERY_MIN} до {GEOCODE_QUERY_MAX} символов"
    return text, None


def geocode(query, clock=time.monotonic):
    """До пяти мест по названию; None — геокодер не ответил."""
    key = query.lower()
    now = clock()
    cached = _geocode_cache.get(key)
    if cached and cached[0] > now:
        return cached[1]
    try:
        raw = _http_get(WEATHER_GEOCODE_URL, {"name": query, "count": 5,
                                              "language": "ru", "format": "json"})
    except Exception:
        logging.warning("weather: геокодер не ответил", exc_info=True)
        return None
    found = []
    for item in raw.get("results") or []:
        lat, lon = item.get("latitude"), item.get("longitude")
        if not isinstance(lat, (int, float)) or not isinstance(lon, (int, float)):
            continue
        found.append({"name": str(item.get("name") or "")[:LOCATION_NAME_MAX],
                      "admin1": str(item.get("admin1") or ""),
                      "country": str(item.get("country") or ""),
                      "lat": round(lat, 2), "lon": round(lon, 2)})
    _remember(_geocode_cache, key, now + GEOCODE_TTL_SEC, found)
    return found


# ── Погода в час тренировки ──────────────────────────────────────────────────

def parse_run_time(text):
    """«07:30» → (7, 30); пусто или мусор → время по умолчанию."""
    match = _RUN_TIME_RE.match(str(text or "")) or _RUN_TIME_RE.match(WEATHER_DEFAULT_RUN_TIME)
    return int(match.group(1)), int(match.group(2))


def local_today(forecast, now=None):
    """Сегодня в часовом поясе места, а не по UTC сервера."""
    return ((now or datetime.utcnow()) + timedelta(seconds=forecast["utc_offset"])).date()


def deg(value):
    """+3°, −1°, 0° — со знаком, как в прогнозах."""
    value = int(round(value))
    return f"+{value}°" if value > 0 else f"−{-value}°" if value < 0 else "0°"


def _num(value, default=0.0):
    return float(value) if isinstance(value, (int, float)) else default


def _hhmm(iso):
    return str(iso or "")[11:16]


def day_weather(forecast, day, run_time):
    """Погода дня и в час тренировки + оценка; None — дня нет в прогнозе.

    Значения берутся на ближайший целый час: 07:30 → 08:00. Темно или светло —
    по точному времени.
    """
    hh, mm = run_time
    hour = min(23, hh + (1 if mm >= 30 else 0))
    idx = forecast["index"].get(f"{day.isoformat()}T{hour:02d}:00")
    daily = forecast["days"].get(day.isoformat())
    if idx is None or not daily:
        return None
    h = forecast["hours"][idx]
    code = int(_num(h.get("weather_code"), _num(daily.get("weather_code"))))
    t, feels = _num(h.get("temperature_2m")), _num(h.get("apparent_temperature"))
    precip, prob = _num(h.get("precipitation")), _num(h.get("precipitation_probability"))
    wind, gusts, uv = _num(h.get("wind_speed_10m")), _num(h.get("wind_gusts_10m")), _num(h.get("uv_index"))
    before = forecast["hours"][max(0, idx - SLIPPERY_LOOKBACK_H):idx + 1]
    precip_before = sum(_num(p.get("precipitation")) for p in before)
    clock = f"{hh:02d}:{mm:02d}"
    sunrise, sunset = _hhmm(daily.get("sunrise")), _hhmm(daily.get("sunset"))

    danger, hard, flags = [], [], []
    if code in STORM_CODES:
        danger.append("гроза"); flags.append("storm")
    if code in FREEZING_CODES:
        danger.append("ледяной дождь"); flags.append("freezing_rain")
    if feels <= DANGER_FEELS_COLD:
        danger.append(f"мороз {deg(feels)}"); flags.append("extreme_cold")
    elif feels <= HARD_FEELS_COLD:
        hard.append(f"мороз {deg(feels)}"); flags.append("cold")
    if feels >= DANGER_FEELS_HEAT:
        danger.append(f"жара {deg(feels)}"); flags.append("extreme_heat")
    elif feels >= HARD_FEELS_HEAT:
        hard.append(f"жара {deg(feels)}"); flags.append("heat")
    if gusts >= DANGER_GUSTS:
        danger.append(f"шквал {round(gusts)} м/с"); flags.append("gale")
    elif gusts >= HARD_GUSTS:
        hard.append(f"порывы {round(gusts)} м/с"); flags.append("gusts")
    if SLIPPERY_T_MIN <= t <= SLIPPERY_T_MAX and precip_before >= SLIPPERY_PRECIP_MM:
        hard.append("скользко"); flags.append("slippery")
    if code in HEAVY_SNOW_CODES:
        hard.append("сильный снег"); flags.append("heavy_snow")
    elif precip >= HARD_PRECIP_MM:
        hard.append("ливень"); flags.append("downpour")
    if code in SNOW_CODES:
        flags.append("snow")
    elif precip >= RAIN_MM or prob >= RAIN_PROB:
        flags.append("rain")
    if wind >= WIND_MS:
        flags.append("wind")
    if uv >= UV_HIGH:
        flags.append("uv")
    if sunrise and sunset and (clock < sunrise or clock >= sunset):
        flags.append("dark")
    if feels >= WARM_FEELS:
        flags.append("warm")

    level = DANGER if danger else HARD if hard else OK
    icon, summary = WMO.get(code, ("☁", "без осадков"))
    cooler = None
    if feels >= WARM_FEELS:
        candidates = [(_num(x.get("apparent_temperature")), x["time"])
                      for x in forecast["hours"]
                      if x["time"][:10] == day.isoformat() and int(x["time"][11:13]) in COOL_HOURS
                      and isinstance(x.get("apparent_temperature"), (int, float))]
        if candidates:
            coolest = min(candidates)
            if feels - coolest[0] >= COOLER_BY:
                cooler = {"time": _hhmm(coolest[1]), "feels": round(coolest[0])}
    return {
        "public": {
            "icon": icon, "summary": summary,
            "t_min": round(_num(daily.get("temperature_2m_min"))),
            "t_max": round(_num(daily.get("temperature_2m_max"))),
            "sunrise": sunrise, "sunset": sunset,
            "at_run": {"time": clock, "t": round(t), "feels": round(feels),
                       "precip_mm": round(precip, 1), "precip_prob": round(prob),
                       "wind": round(wind), "gusts": round(gusts), "uv": round(uv)},
        },
        "level": level,
        "why": (danger or hard or [""])[0],
        "flags": flags,
        "feels": feels,
        "t": t,
        "cooler": cooler,
    }


# ── Место и одежда ───────────────────────────────────────────────────────────

def choose_venue(level, kind, venues, why, move_to=None):
    """Где бежать: {code, label, why} или None (отдых, старт, дня нет в плане).

    В помещение уводит опасная погода — любую тренировку, тяжёлая — только
    работу на скорости. Работе нужен манеж (на дорожке не разогнаться и не
    сделать отрезки), лёгкому бегу и длительной — дорожка (в манеже круги по
    200 м). Если ни того, ни другого нет — перенос на ближайший день без
    плохой погоды.
    """
    if kind in (None, WORKOUT_REST, WORKOUT_RACE):
        return None
    indoor = level == DANGER or (level == HARD and kind == WORKOUT_QUALITY)
    if not indoor:
        return {"code": "outdoor", "label": VENUE_LABELS["outdoor"],
                "why": why if level == HARD else ""}
    order = ("manege", "treadmill") if kind == WORKOUT_QUALITY else ("treadmill", "manege")
    for code in order:
        if code in venues:
            return {"code": code, "label": VENUE_LABELS[code], "why": why}
    if move_to:
        return {"code": "move", "date": move_to.isoformat(), "why": why,
                "label": f"Перенести на {_DAY_RU[DAY_FIELDS[move_to.weekday()]]} "
                         f"{move_to.strftime('%d.%m')}"}
    if level == DANGER:
        return {"code": "move", "date": "", "why": why, "label": "Перенести или отменить"}
    return {"code": "outdoor", "label": "Улица — без ускорений", "why": why}


def design_temperature(feels, kind, sensitivity):
    """Температура, на которую подбирается одежда."""
    shift = SENSITIVITY_SHIFT.get(sensitivity, 0)
    if kind in (WORKOUT_QUALITY, WORKOUT_RACE):
        shift += INTENSITY_SHIFT
    return feels + shift


def clothing(wx, kind, sensitivity, venue_code=None):
    """Одежда по зонам: {head, top, bottom, hands, feet, extra}."""
    if venue_code in INDOOR:
        return {**INDOOR_CLOTHING, "extra": list(INDOOR_CLOTHING["extra"])}
    design = design_temperature(wx["feels"], kind, sensitivity)
    for low, head, top, bottom, hands in CLOTHING_LADDER:
        if low is None or design >= low:
            break
    flags = set(wx["flags"])
    feet = "кроссовки"
    if "slippery" in flags or "freezing_rain" in flags:
        feet = "кроссовки с шипами или агрессивным протектором"
    elif "snow" in flags:
        feet = "кроссовки с агрессивным протектором"
    extra = []
    if "rain" in flags or "downpour" in flags:
        if "ветровка" in top:
            top = top.replace("ветровка", "непромокаемая ветровка")
        elif design < 15:
            extra.append("непромокаемая ветровка")
        if not head:
            head = "кепка с козырьком"
    if "wind" in flags and design < 15 and not re.search(r"ветровка|куртка", top + " ".join(extra)):
        extra.append("ветрозащитный верх")
    if "uv" in flags:
        extra.append("солнцезащитные очки, SPF")
    if "dark" in flags:
        extra.append("светоотражатели, фонарь")
    if "warm" in flags or (kind == WORKOUT_LONG and wx["feels"] >= 15):
        extra.append("вода")
    return {"head": head, "top": top, "bottom": bottom, "hands": hands,
            "feet": feet, "extra": extra}


def advice_notes(wx, kind, venue):
    """Короткие пометки к дню — только то, что меняет поведение на тренировке."""
    if kind == WORKOUT_REST:
        return []
    outdoor = not venue or venue["code"] == "outdoor"
    flags = set(wx["flags"])
    notes = []
    if outdoor and wx["level"] == HARD and kind not in (WORKOUT_QUALITY, WORKOUT_RACE, None):
        notes.append(f"Тяжёлая погода ({wx['why']}): можно сократить или сбавить темп")
    if outdoor and "slippery" in flags:
        notes.append("Скользко: короче шаг, ускорения — только на чистом покрытии")
    if outdoor and "wind" in flags:
        notes.append("Ветер: первую половину — против ветра, обратно — по ветру")
    if outdoor and "dark" in flags:
        sun = wx["public"]["sunrise"] if wx["public"]["at_run"]["time"] < "12:00" \
            else wx["public"]["sunset"]
        notes.append(f"Темно (рассвет/закат в {sun}): светлая одежда, светоотражатели")
    if outdoor and wx["cooler"]:
        notes.append(f"Прохладнее всего в {wx['cooler']['time']}: "
                     f"ощущается {deg(wx['cooler']['feels'])}")
    if kind == WORKOUT_RACE and wx["feels"] < 9:
        notes.append("Перед стартом — лишний слой, сбросить на старте")
    return notes


# ── Сборка недели ────────────────────────────────────────────────────────────

def build_weather(forecast, profile, weeks=None, plan_start=None, now=None):
    """Дни «сегодня…+6» в часовом поясе места: погода, место, одежда, пометки.

    Тренировка дня берётся из плана по дате (`planned_for_date`): план без дат
    тренировок не получает, как и в план/факте. День вне плана — только погода
    и одежда для лёгкого бега.
    """
    today = local_today(forecast, now)
    run_time = parse_run_time(profile.get("run_time"))
    venues = set(profile.get("venues") or [])
    sensitivity = profile.get("cold_sensitivity") or ""

    rows = []
    for offset in range(DAYS_AHEAD):
        day = today + timedelta(days=offset)
        wx = day_weather(forecast, day, run_time)
        if wx is None:
            continue
        hit = planned_for_date(weeks, day, plan_start) if weeks else None
        text = hit[2] if hit else ""
        rows.append((day, wx, hit, text, workout_kind(text) if hit else None))

    days = []
    for i, (day, wx, hit, text, kind) in enumerate(rows):
        move_to = next((d for d, w, *_ in rows[i + 1:] if w["level"] == OK), None)
        venue = choose_venue(wx["level"], kind, venues, wx["why"], move_to)
        wear = None if kind == WORKOUT_REST else \
            clothing(wx, kind, sensitivity, venue["code"] if venue else None)
        days.append({
            "date": day.isoformat(),
            "field": DAY_FIELDS[day.weekday()],
            "week_idx": hit[0] if hit else None,
            "workout": text,
            "kind": kind,
            "weather": wx["public"],
            "level": wx["level"],
            "why": wx["why"],
            "flags": wx["flags"],
            "venue": venue,
            "clothing": wear,
            "notes": advice_notes(wx, kind, venue),
        })
    return days


def weather_report(profile, weeks=None, plan_start=None, now=None, fetch=None):
    """Ответ для недели плана: status ok | no_location | unavailable.

    Всегда словарь, а не исключение: план без погоды остаётся планом.
    """
    hh, mm = parse_run_time(profile.get("run_time"))
    location = profile.get("location")
    report = {"source": SOURCE, "run_time": f"{hh:02d}:{mm:02d}",
              "location": location, "days": []}
    if not location:
        return {**report, "status": "no_location"}
    forecast = (fetch or fetch_forecast)(location["lat"], location["lon"])
    if forecast is None:
        return {**report, "status": "unavailable"}
    return {**report, "status": "ok", "timezone": forecast["timezone"],
            "today": local_today(forecast, now).isoformat(),
            "days": build_weather(forecast, profile, weeks, plan_start, now)}
