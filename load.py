"""Недельная нагрузка по двум осям (#66).

Слой домена по раскладке #36: только чистые вычисления, в GCS модуль не ходит
и о `storage` не знает.

Нагрузка считается по двум осям:

- **внешняя** — что сделано: rTSS по темпу с поправкой на уклон относительно
  порогового темпа, как в TrainingPeaks. 100 = час на пороге;
- **внутренняя** — чего это стоило: TRIMP по пульсу. Банистер, если в профиле
  есть пульс покоя и пол, иначе Эдвардс по пульсовым зонам.

Посекундные данные FIT приходят сюда гистограммами: сколько секунд пульс
держался на каждом значении и сколько секунд скорость (уже с поправкой на
уклон) провела в каждой корзине. Профиль в гистограммах не участвует — зоны,
пульс покоя и порог применяются на лету. Поэтому правка профиля пересчитывает
всю историю, и недели остаются сопоставимыми между собой.

Оценка недели — отношение к обычной нагрузке: к среднему за четыре
предыдущие недели (сама неделя в среднее не входит). Это ориентир, а не
диагноз: пороги взяты из командных видов спорта, и подписи нарочно
нейтральные.
"""
import math
from collections import deque
from datetime import timedelta

from compliance import to_date
from domain import DIST_LABEL_KM, parse_time_to_sec

# Правила построения гистограмм. Поменялись — кэш собирается заново.
HIST_VERSION = 1

# Сэмпл «держит» время до следующего. Разрыв длиннее — часы стояли на паузе,
# и записывать его в зону последнего пульса нельзя.
ZONE_GAP_CAP_SEC = 30

# Пульс вне коридора — сбой датчика, а не тренировка. Коридор заодно
# ограничивает гистограмму: больше 201 ключа в ней не бывает.
HR_MIN, HR_MAX = 30, 230
# Выше максимального пульса на столько — тоже сбой: оптика на запястье
# «цепляется» за каденс и показывает 190 на трусце.
HR_OVER_MAX = 10

SPEED_BIN_CM = 5           # корзина скорости, см/с
SPEED_MAX_MPS = 7.0        # быстрее 2:23/км бегом не бывает — это выброс GPS
ADJUSTED_MAX_MPS = 14.0    # потолок скорости с поправкой на крутой подъём
SMOOTH_SEC = 30            # сглаживание скорости перед четвёртой степенью, как у NGP
GRADE_WINDOW_SEC = 30      # уклон считается на отрезке не короче этого
GRADE_MIN_DIST_M = 20      # и не короче этого: на коротком шум высоты больше подъёма
GRADE_CLIP = 0.30

# Пороговый темп: столько держится примерно час. Коридор — и для ручного
# ввода в профиле, и для того, что считается порогом здесь.
THRESHOLD_PACE_MIN, THRESHOLD_PACE_MAX = 150, 600      # 2:30–10:00 на км
RIEGEL_EXP = 1.06
THRESHOLD_RACE_DAYS = 365
# Забег с темпом вне коридора — опечатка во времени, а не результат.
RACE_PACE_MIN, RACE_PACE_MAX = 150, 900

# Коэффициенты Банистера: a·e^(b·HRR)
BANISTER = {"m": (0.64, 1.92), "f": (0.86, 1.67)}

WEEKS = 12
BASE_WEEKS = 4              # «обычная» нагрузка — среднее за столько недель до
HISTORY_DAYS = 7 * BASE_WEEKS
MIN_BASE_RUNS = 4           # меньше пробежек в базе — сравнивать не с чем
AEROBIC_TYPES = ("easy", "long", "recovery")
COST_MIN_WEEK_RUNS = 2
COST_MIN_BASE_RUNS = 4
COST_BAND_PCT = 10
RUN_DURATION_MAX_SEC = 2 * 86400


def _num(value):
    """Конечное число или None. bool числом не считается, целое больше
    float (10**400 из JSON) — тоже: math.isfinite на нём падает."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except OverflowError:
        return None


def _float(value):
    """Число из записи пробежки: старые записи хранят его и строкой."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) else None


# ── Гистограммы из сэмплов FIT ───────────────────────────────────────────────

def _columns(details, *names):
    """Списки сэмплов одной длины или None."""
    samples = details.get("samples") if isinstance(details, dict) else None
    if not isinstance(samples, dict):
        return None
    columns = [samples.get(name) for name in names]
    if not all(isinstance(column, list) for column in columns):
        return None
    if len({len(column) for column in columns}) != 1 or len(columns[0]) < 2:
        return None
    return columns


def hr_histogram(details):
    """{уд/мин: секунды} по сэмплам. {} — считать не из чего."""
    columns = _columns(details, "t_offset_sec", "hr")
    hist = {}
    if not columns:
        return hist
    times, pulses = columns
    for i in range(len(times) - 1):
        start, end, pulse = _num(times[i]), _num(times[i + 1]), _num(pulses[i])
        if start is None or end is None or pulse is None:
            continue
        span = min(end - start, ZONE_GAP_CAP_SEC)
        bpm = int(round(pulse))
        if span > 0 and HR_MIN <= bpm <= HR_MAX:
            hist[bpm] = hist.get(bpm, 0) + span
    return hist


def _minetti(grade):
    """Энергозатраты бега, Дж/кг/м, по уклону (Minetti, 2002)."""
    i = max(-GRADE_CLIP, min(GRADE_CLIP, grade))
    return 155.4 * i ** 5 - 30.4 * i ** 4 - 43.3 * i ** 3 + 46.3 * i ** 2 + 19.5 * i + 3.6


FLAT_COST = _minetti(0.0)


def _speed_key(speed):
    return int(round(speed * 100 / SPEED_BIN_CM)) * SPEED_BIN_CM


def speed_histogram(details):
    """{см/с: секунды} скорости с поправкой на уклон, сглаженной за 30 с.

    Время считается только в движении: остановка (темпа нет) и выброс GPS
    в гистограмму не попадают. {} — скорости в сэмплах нет (дорожка без
    датчика, старые файлы).
    """
    columns = _columns(details, "t_offset_sec", "pace_sec_per_km")
    if not columns:
        return {}
    times, paces = columns
    heights = details["samples"].get("altitude_m")
    if not isinstance(heights, list) or len(heights) != len(times):
        heights = [None] * len(times)

    points = []                                     # (момент, скорость, высота)
    for moment, pace, height in zip(times, paces, heights):
        moment, pace = _num(moment), _num(pace)
        if moment is None:
            continue
        speed = 1000 / pace if pace and pace > 0 else None
        if speed is not None and speed > SPEED_MAX_MPS:
            speed = None
        points.append((moment, speed, _num(height)))

    # Отрезок k — от точки k до k + 1, со скоростью точки k.
    spans = [max(0.0, min(points[k + 1][0] - points[k][0], ZONE_GAP_CAP_SEC))
             for k in range(len(points) - 1)]
    clock, path = [0.0], [0.0]
    for k, span in enumerate(spans):
        clock.append(clock[-1] + span)
        path.append(path[-1] + (points[k][1] or 0.0) * span)

    hist = {}
    window, window_time, window_sum = deque(), 0.0, 0.0
    ahead = 0
    for k, span in enumerate(spans):
        speed = points[k][1]
        if not speed or span <= 0:
            continue
        ahead = max(ahead, k + 1)
        while ahead < len(points) - 1 and clock[ahead] - clock[k] < GRADE_WINDOW_SEC:
            ahead += 1
        grade = 0.0
        rise_from, rise_to = points[k][2], points[ahead][2]
        run = path[ahead] - path[k]
        if rise_from is not None and rise_to is not None and run >= GRADE_MIN_DIST_M:
            grade = (rise_to - rise_from) / run
        adjusted = min(speed * _minetti(grade) / FLAT_COST, ADJUSTED_MAX_MPS)

        window.append((span, adjusted))
        window_time += span
        window_sum += span * adjusted
        while len(window) > 1 and window_time - window[0][0] >= SMOOTH_SEC:
            old_span, old_speed = window.popleft()
            window_time -= old_span
            window_sum -= old_span * old_speed
        key = _speed_key(window_sum / window_time)
        if key > 0:
            hist[key] = hist.get(key, 0) + span
    return hist


def run_hist_entry(details):
    """Запись кэша для пробежки: обе гистограммы, ключи — строки (JSON)."""
    return {"hr": _dump(hr_histogram(details)), "spd": _dump(speed_histogram(details))}


def _dump(hist):
    return {str(key): round(sec, 1) for key, sec in sorted(hist.items())}


def _clean_hist(raw, low, high):
    hist = {}
    if not isinstance(raw, dict):
        return hist
    for key, sec in raw.items():
        try:
            key = int(key)
        except (TypeError, ValueError):
            continue
        sec = _num(sec)
        if low <= key <= high and sec is not None and 0 < sec <= RUN_DURATION_MAX_SEC:
            hist[key] = hist.get(key, 0) + sec
    return hist


def clean_hist_entry(raw):
    """Запись кэша → {"hr": {int: сек}, "spd": {int: сек}}. Мусор отбрасывается:
    кэш пишет только сервер, но он переживает смены формата."""
    raw = raw if isinstance(raw, dict) else {}
    return {"hr": _clean_hist(raw.get("hr"), HR_MIN, HR_MAX),
            "spd": _clean_hist(raw.get("spd"), SPEED_BIN_CM, int(ADJUSTED_MAX_MPS * 100))}


# ── Профиль: зоны, метод пульсовой оси, порог ────────────────────────────────

def format_duration(seconds):
    seconds = int(round(seconds))
    if seconds >= 3600:
        return f"{seconds // 3600}:{seconds % 3600 // 60:02d}:{seconds % 60:02d}"
    return f"{seconds // 60}:{seconds % 60:02d}"


def hour_pace(km, seconds):
    """Темп, сек/км, который по формуле Ригеля держится час."""
    hour_km = km * (3600 / seconds) ** (1 / RIEGEL_EXP)
    return 3600 / hour_km


def estimate_threshold(races, today):
    """Пороговый темп по «Стартам» или None.

    Берётся лучший прогноз среди забегов за последний год; если их нет — за
    всё время, с пометкой stale. Время в основании собрано заново из секунд,
    а не взято строкой забега: в промпт ИИ уходит только то, что разобралось.
    """
    recent = best = None
    for race in races or []:
        if not isinstance(race, dict) or race.get("deleted"):
            continue
        label = race.get("dist_label")
        km = DIST_LABEL_KM.get(label) if isinstance(label, str) else None
        seconds = parse_time_to_sec(race.get("time"))
        day = to_date(race.get("date"))
        if not km or not seconds or day is None or day > today:
            continue
        if not RACE_PACE_MIN <= seconds / km <= RACE_PACE_MAX:
            continue
        candidate = (hour_pace(km, seconds), label, seconds, day)
        if best is None or candidate[0] < best[0]:
            best = candidate
        if (today - day).days <= THRESHOLD_RACE_DAYS and (recent is None or candidate[0] < recent[0]):
            recent = candidate
    pick = recent or best
    if pick is None:
        return None
    pace, label, seconds, day = pick
    return {"pace_sec": int(round(pace)),
            "basis": {"dist_label": label, "time": format_duration(seconds),
                      "date": day.isoformat(), "stale": recent is None}}


def valid_threshold(value):
    value = _num(value)
    return value if value is not None and THRESHOLD_PACE_MIN <= value <= THRESHOLD_PACE_MAX else None


def threshold_for(profile, races, today):
    """Действующий пороговый темп: ручной из профиля, иначе оценка по забегам."""
    estimate = estimate_threshold(races, today)
    manual = valid_threshold(profile.get("threshold_pace_sec"))
    if manual is not None:
        pace, source, basis = int(manual), "manual", None
    elif estimate:
        pace, source, basis = estimate["pace_sec"], "races", estimate["basis"]
    else:
        pace = source = basis = None
    return {"threshold_pace_sec": pace, "threshold_source": source,
            "threshold_basis": basis, "threshold_auto": estimate}


def axes(profile, derived, races, today):
    """Чем считать нагрузку этого спортсмена."""
    zones = [z for z in (derived.get("hr_zones") or [])
             if isinstance(z, dict) and _num(z.get("from")) is not None]
    hr_max = _num(derived.get("hr_max_effective"))
    hr_rest = _num(profile.get("hr_rest"))
    sex = profile.get("sex")
    if hr_max and hr_rest and sex in BANISTER and hr_max > hr_rest:
        method = "banister"
    elif zones:
        method = "edwards"
    else:
        method = None
    return {"zones": zones,
            "zones_source": derived.get("hr_zones_source") if zones else None,
            "hr_max": hr_max, "hr_rest": hr_rest, "sex": sex, "internal": method,
            **threshold_for(profile, races, today)}


# ── Нагрузка одной пробежки ──────────────────────────────────────────────────

def zone_seconds(hist, zones):
    """Секунды по зонам: [ниже первой, Z1, …]. zones — по возрастанию from."""
    seconds = [0.0] * (len(zones) + 1)
    for bpm, sec in hist.items():
        seconds[sum(1 for zone in zones if bpm >= zone["from"])] += sec
    return seconds


def _without_dropouts(hist, hr_max):
    if not hr_max:
        return hist
    return {bpm: sec for bpm, sec in hist.items() if bpm <= hr_max + HR_OVER_MAX}


def internal_load(hist, ctx):
    """TRIMP: Банистер или Эдвардс — как решил axes(). None — нечем считать."""
    method = ctx.get("internal")
    hist = _without_dropouts(hist, ctx.get("hr_max"))
    if not hist or method is None:
        return None
    if method == "banister":
        a, b = BANISTER[ctx["sex"]]
        rest, top = ctx["hr_rest"], ctx["hr_max"]
        total = 0.0
        for bpm, sec in hist.items():
            reserve = min(max((bpm - rest) / (top - rest), 0.0), 1.0)
            total += sec / 60 * reserve * a * math.exp(b * reserve)
        return total
    # Эдвардс: вес зоны — её номер, ниже первой зоны — ноль.
    return sum(sec / 60 * slot for slot, sec in enumerate(zone_seconds(hist, ctx["zones"])))


def external_load(hist, threshold_pace_sec):
    """rTSS = часы × IF² × 100, IF = NGS / пороговая скорость. None — нечем."""
    if not hist or not threshold_pace_sec:
        return None
    total = sum(hist.values())
    if total <= 0:
        return None
    threshold = 1000 / threshold_pace_sec
    ngs = (sum(sec * (key / 100) ** 4 for key, sec in hist.items()) / total) ** 0.25
    return total / 3600 * (ngs / threshold) ** 2 * 100


def run_duration(run):
    """Длительность пробежки, сек: время, иначе темп × дистанция."""
    seconds = parse_time_to_sec(run.get("time"))
    if not seconds:
        pace, dist = parse_time_to_sec(run.get("pace")), _float(run.get("dist"))
        seconds = pace * dist if pace and dist and dist > 0 else None
    return seconds if seconds and 0 < seconds <= RUN_DURATION_MAX_SEC else None


def _run_speed(run, duration):
    pace = parse_time_to_sec(run.get("pace"))
    if pace:
        speed = 1000 / pace
    else:
        dist = _float(run.get("dist"))
        speed = dist * 1000 / duration if dist and dist > 0 and duration else None
    return speed if speed and speed <= SPEED_MAX_MPS else None


def run_load(run, entry, ctx):
    """Нагрузка пробежки по обеим осям. Без гистограммы (нет FIT, нет в нём
    пульса или скорости) — оценка по среднему пульсу и темпу, флаг *_est."""
    entry = entry or {}
    hr_hist, spd_hist = entry.get("hr") or {}, entry.get("spd") or {}
    int_est = ext_est = False
    duration = None
    if not hr_hist:
        duration = run_duration(run)
        pulse = _float(run.get("hr"))
        if duration and pulse and HR_MIN <= round(pulse) <= HR_MAX:
            hr_hist, int_est = {int(round(pulse)): duration}, True
    if not spd_hist:
        duration = duration or run_duration(run)
        speed = _run_speed(run, duration)
        if duration and speed:
            spd_hist, ext_est = {_speed_key(speed): duration}, True
    internal = internal_load(hr_hist, ctx)
    external = external_load(spd_hist, ctx.get("threshold_pace_sec"))
    zones = ctx.get("zones") or []
    split = None
    if hr_hist and len(zones) == 5:
        split = zone_seconds(_without_dropouts(hr_hist, ctx.get("hr_max")), zones)
    return {"int": internal, "int_est": int_est and internal is not None,
            "ext": external, "ext_est": ext_est and external is not None,
            "zones": split, "aerobic": run.get("type") in AEROBIC_TYPES}


# ── Недели ───────────────────────────────────────────────────────────────────

def load_band(ratio):
    """Оценка к обычной нагрузке. Подписи нейтральные: это ориентир."""
    if ratio is None:
        return None
    if ratio < 0.8:
        return "low"
    if ratio <= 1.3:
        return "ok"
    if ratio <= 1.5:
        return "high"
    return "spike"


def cost_band(dev_pct):
    if dev_pct is None:
        return None
    if dev_pct > COST_BAND_PCT:
        return "higher"
    if dev_pct < -COST_BAND_PCT:
        return "lower"
    return "usual"


def _tally(loads, start, end):
    t = {"runs": 0, "int": 0.0, "int_known": 0, "int_unknown": 0,
         "ext": 0.0, "ext_known": 0, "ext_unknown": 0,
         "zones": [0.0] * 6, "cost_int": 0.0, "cost_ext": 0.0, "cost_runs": 0}
    for day, load in loads:
        if not start <= day <= end:
            continue
        t["runs"] += 1
        for axis in ("int", "ext"):
            if load[axis] is None:
                t[axis + "_unknown"] += 1
            else:
                t[axis] += load[axis]
                t[axis + "_known"] += 1
        if load["zones"]:
            t["zones"] = [a + b for a, b in zip(t["zones"], load["zones"])]
        if load["aerobic"] and load["int"] is not None and load["ext"]:
            t["cost_int"] += load["int"]
            t["cost_ext"] += load["ext"]
            t["cost_runs"] += 1
    return t


def weekly_load(runs, entries, ctx, today, weeks=WEEKS):
    """Недели пн–вс, скользящие 7 дней и нагрузка по пробежкам.

    entries — кэш гистограмм {str(run_id): {"hr", "spd"}} после clean_hist_entry.
    Пробежка без записи в кэше считается по средним — до тех пор, пока кэш
    не догонит.
    """
    loads, by_id = [], {}
    for run in runs or []:
        if not isinstance(run, dict) or run.get("deleted"):
            continue
        day = to_date(run.get("date"))
        if day is None:
            continue
        load = run_load(run, entries.get(str(run.get("id"))), ctx)
        loads.append((day, load))
        by_id[str(run.get("id"))] = {
            "int": round(load["int"]) if load["int"] is not None else None,
            "int_est": load["int_est"],
            "ext": round(load["ext"]) if load["ext"] is not None else None,
            "ext_est": load["ext_est"],
        }

    latest = max((day for day, _ in loads), default=None)
    if latest and latest > today:
        # Пробежка, записанная после местной полуночи, не выпадает из окна.
        today = min(latest, today + timedelta(days=1))
    first = min((day for day, _ in loads), default=None)
    has = {"int": ctx.get("internal") is not None,
           "ext": bool(ctx.get("threshold_pace_sec"))}
    split = len(ctx.get("zones") or []) == 5

    def axis(key, cur, base, start, rated):
        if not has[key]:
            return None
        chronic = None
        if (first is not None and first <= start - timedelta(days=HISTORY_DAYS)
                and base[key + "_known"] >= MIN_BASE_RUNS and base[key] > 0):
            chronic = base[key] / BASE_WEEKS
        ratio = cur[key] / chronic if chronic and rated else None
        return {"load": round(cur[key]), "unknown": cur[key + "_unknown"],
                "chronic": round(chronic) if chronic else None,
                "ratio": round(ratio, 2) if ratio is not None else None,
                "band": load_band(ratio)}

    def cost(cur, base):
        if not (has["int"] and has["ext"]):
            return None
        if (cur["cost_runs"] < COST_MIN_WEEK_RUNS or base["cost_runs"] < COST_MIN_BASE_RUNS
                or cur["cost_ext"] <= 0 or base["cost_ext"] <= 0 or base["cost_int"] <= 0):
            return None
        dev = ((cur["cost_int"] / cur["cost_ext"]) / (base["cost_int"] / base["cost_ext"]) - 1) * 100
        return {"dev_pct": int(round(dev)), "band": cost_band(dev)}

    def minutes(cur):
        if not split:
            return {"easy": None, "moderate": None, "hard": None}
        z = cur["zones"]
        return {"easy": round(sum(z[:3]) / 60), "moderate": round(z[3] / 60),
                "hard": round((z[4] + z[5]) / 60)}

    monday = today - timedelta(days=today.weekday())
    rows = []
    for back in range(weeks - 1, -1, -1):
        start = monday - timedelta(weeks=back)
        end = start + timedelta(days=6)
        cur = _tally(loads, start, end)
        base = _tally(loads, start - timedelta(days=HISTORY_DAYS), start - timedelta(days=1))
        partial = back == 0
        rows.append({"start": start.isoformat(), "end": end.isoformat(),
                     "partial": partial, "runs": cur["runs"],
                     "int": axis("int", cur, base, start, not partial),
                     "ext": axis("ext", cur, base, start, not partial),
                     **minutes(cur), "cost": cost(cur, base)})

    start = today - timedelta(days=6)
    cur = _tally(loads, start, today)
    base = _tally(loads, start - timedelta(days=HISTORY_DAYS), start - timedelta(days=1))
    rolling = {"start": start.isoformat(), "end": today.isoformat(), "runs": cur["runs"],
               "int": axis("int", cur, base, start, True),
               "ext": axis("ext", cur, base, start, True),
               **minutes(cur), "cost": cost(cur, base)}

    return {"today": today.isoformat(), "weeks": rows, "rolling": rolling, "runs": by_id}
