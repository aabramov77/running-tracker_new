"""Данные: GCS, версии, реестр пользователей, разбор FIT, контекст для LLM.

Выделено из main.py (#36, фаза 3). Модуль ничего не знает про HTTP — на вход
приходят bucket и sub, на выходе данные. Зависимости идут в одну сторону:
config → domain/llm_prompt → storage → api → main.
"""
import hashlib
import io
import json
import logging
import math
import re
import secrets as secrets_mod
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx
from fitparse import FitFile
from google.api_core.exceptions import NotFound, PreconditionFailed
from google.cloud import storage as gcs

from config import (ADMIN_EMAILS, BUCKET_NAME, LLM_CONFIG_MANIFEST,
                    LLM_DEFAULT_EFFORT, LLM_EFFORT_LEVELS, LLM_MAX_TOKENS,
                    MAX_PENDING, REGISTRY_TTL_SEC, USERS_REGISTRY)
from compliance import (DEFAULT as UNDATED, anchor_source,
                        current_week_idx, plan_compliance,
                        planned_for_date, to_date, week_days, week_window)
from domain import (DIST_LABEL_KM, FEEL_LABELS, HR_ZONE_BOUNDS, MEMORY_FACTS_MAX,
                    MEMORY_KIND_LABELS, MEMORY_OPS_MAX, MEMORY_TEXT_MAX, PLAN_DAYS,
                    TYPE_LABELS, parse_time_to_sec, personal_bests)
from history import monthly_summary, past_plan_candidates, past_plans
from load import (HIST_VERSION, THRESHOLD_PACE_MAX, THRESHOLD_PACE_MIN,
                  ZONE_GAP_CAP_SEC, axes, clean_hist_entry, format_duration,
                  hr_histogram, run_hist_entry, threshold_for, weekly_load,
                  zone_seconds)
from llm_prompt import (coach_chat_data, coach_chat_instructions,
                        format_context_for_llm, format_memory_block,
                        format_plan_window, format_run_focus)
from weather import (COLD_SENSITIVITY, LOCATION_NAME_MAX, VENUES, parse_run_time,
                     weather_report)

def get_storage_client():
    return gcs.Client()


# ── Per-user path builders (единственная точка построения путей) ──────────────
# Правило: ни одна data-функция не обращается к bucket без sub. Все пути — через p_*.

def upfx(sub):                 return f"users/{sub}/"
def p_runs(sub):               return f"{upfx(sub)}runs.json"
def p_races(sub):              return f"{upfx(sub)}races.json"
def p_plans_index(sub):        return f"{upfx(sub)}plans/index.json"
def p_plan_manifest(sub, pid): return f"{upfx(sub)}plans/{pid}/manifest.json"
def p_plan_ver(sub, pid, v):   return f"{upfx(sub)}plans/{pid}/v{v}/plan.json"
# Правка карточки гонки (#60): событие с прежними и новыми значениями
def p_plan_meta_prefix(sub, pid):     return f"{upfx(sub)}plans/{pid}/meta/"
def p_plan_meta_event(sub, pid, eid): return f"{p_plan_meta_prefix(sub, pid)}{eid}.json"
# Одиночный план до #25 — только для ленивой миграции
def p_singleplan_manifest(sub): return f"{upfx(sub)}plan/manifest.json"
def p_singleplan_ver(sub, v):   return f"{upfx(sub)}plan/v{v}/plan.json"
def p_advice_manifest(sub):    return f"{upfx(sub)}advice/manifest.json"
def p_advice_ver(sub, v):      return f"{upfx(sub)}advice/v{v}/recommendation.json"
def p_advice_usage(sub):       return f"{upfx(sub)}advice/usage.json"
def p_profile(sub):            return f"{upfx(sub)}profile.json"   # legacy: гонка до #25
def p_athlete_manifest(sub):   return f"{upfx(sub)}athlete/manifest.json"
def p_athlete_ver(sub, v):     return f"{upfx(sub)}athlete/v{v}/profile.json"
# Пробежка и старт — сущности с версиями (#75); runs.json и races.json —
# производный индекс их текущих версий. Манифест пробежки общий с деталями FIT.
def p_runs_dir(sub):           return f"{upfx(sub)}runs/"
def p_run_manifest(sub, rid):  return f"{p_runs_dir(sub)}{rid}/manifest.json"
def p_run_ver(sub, rid, v):    return f"{p_runs_dir(sub)}{rid}/v{v}/run.json"
def p_run_fit(sub, rid, v=1):     return f"{p_runs_dir(sub)}{rid}/v{v}/activity.fit"
def p_run_details(sub, rid, v=1): return f"{p_runs_dir(sub)}{rid}/v{v}/details.json"
def p_races_dir(sub):          return f"{upfx(sub)}races/"
def p_race_manifest(sub, rid): return f"{p_races_dir(sub)}{rid}/manifest.json"
def p_race_ver(sub, rid, v):   return f"{p_races_dir(sub)}{rid}/v{v}/race.json"
# Кэш гистограмм пульса и скорости по пробежкам (#66): производное от FIT,
# не бизнес-запись — восстанавливается целиком, поэтому перезаписывается.
def p_run_hist(sub):           return f"{upfx(sub)}derived/run_hist.json"
def p_tmp_fit(sub, token):     return f"tmp/{sub}/{token}/activity.fit"
def p_tmp_details(sub, token): return f"tmp/{sub}/{token}/details.json"
# Чат с тренером (#44) — в namespace спортсмена, ветка на каждого тренера
def p_chat_prefix(athlete, coach):      return f"{upfx(athlete)}coach_chat/{coach}/m/"
def p_chat_msg(athlete, coach, msg_id): return f"{p_chat_prefix(athlete, coach)}{msg_id}.json"
def p_chat_read(athlete, coach, role):  return f"{upfx(athlete)}coach_chat/{coach}/read/{role}.json"
# Разборы с ИИ-тренером (#46): ветка и её сообщения — в namespace спортсмена.
def p_ai_root(sub):                return f"{upfx(sub)}ai_coach/t/"
def p_ai_thread(sub, tid):         return f"{p_ai_root(sub)}{tid}/thread.json"
def p_ai_archived(sub, tid):       return f"{p_ai_root(sub)}{tid}/archived.json"
def p_ai_msg_prefix(sub, tid):     return f"{p_ai_root(sub)}{tid}/m/"
def p_ai_msg(sub, tid, msg_id):    return f"{p_ai_msg_prefix(sub, tid)}{msg_id}.json"
def p_ai_applied_prefix(sub, tid): return f"{p_ai_root(sub)}{tid}/applied/"
def p_ai_applied(sub, tid, msg_id): return f"{p_ai_applied_prefix(sub, tid)}{msg_id}.json"
# Память ИИ-тренера (#80): один документ с версиями, общий для всех разборов.
def p_ai_memory_manifest(sub):     return f"{upfx(sub)}ai_coach/memory/manifest.json"
def p_ai_memory_ver(sub, v):       return f"{upfx(sub)}ai_coach/memory/v{v}/memory.json"

# Legacy (глобальные, до multi-user) — только для миграции/ленивого fallback
LEGACY_RUNS = "runs.json"
LEGACY_RACES = "races.json"
LEGACY_PLAN_MANIFEST = "plan/manifest.json"
LEGACY_ADVICE_MANIFEST = "advice/manifest.json"
def legacy_run_manifest(rid): return f"runs/{rid}/manifest.json"
def legacy_run_fit(rid):      return f"runs/{rid}/v1/activity.fit"


# ── Запись в GCS (#35) ────────────────────────────────────────────────────────
#
# Каждая запись идёт с предусловием по поколению объекта (if_generation_match),
# поэтому одновременные запросы не затирают друг друга. Способов три:
#   create_json    — новый неизменяемый объект; если он уже есть, его не трогаем;
#   update_json    — изменяемый объект: «прочитал — поправил — записал», а если
#                    его успели изменить, то заново на свежих данных;
#   commit_version — новая версия сущности: объекты версии и манифест.
# Без предусловия пишутся только временные tmp/ — это проверяет
# tests/test_write_preconditions.py.

WRITE_ATTEMPTS = 8
VERSION_SLOT_ATTEMPTS = 16   # столько занятых номеров подряд пропускает commit_version


class StorageBusy(Exception):
    """Объект не удалось записать: его всё время меняют параллельные запросы."""


class VersionStale(Exception):
    """Правка сделана поверх версии, которая уже не текущая."""


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, indent=2, default=str)


def create_json(bucket, path, obj):
    """Пишет объект, только если его ещё нет. False — он уже есть: его не
    трогаем, а что это значит (повтор, чужая запись), решает вызывающий."""
    try:
        bucket.blob(path).upload_from_string(
            _dumps(obj), content_type="application/json", if_generation_match=0)
    except PreconditionFailed:
        return False
    return True


def create_unique_json(bucket, make):
    """create_json для объекта со случайной частью в имени. make() даёт
    (путь, объект); имя совпало с уже существующим — make() зовётся заново."""
    for _ in range(WRITE_ATTEMPTS):
        path, obj = make()
        if create_json(bucket, path, obj):
            return obj
    raise StorageBusy()


def copy_once(bucket, src_blob, dst_path):
    """Копия объекта, только если по dst_path ещё ничего нет. False — уже есть."""
    try:
        bucket.copy_blob(src_blob, bucket, dst_path, if_generation_match=0)
    except PreconditionFailed:
        return False
    return True


def update_json(bucket, path, change, empty, busy=StorageBusy):
    """Меняет изменяемый объект атомарно и возвращает результат change.

    change(data) правит data на месте и возвращает результат; None — писать
    нечего, тогда и update_json вернёт None. Объекта нет — change получает
    empty(). Если объект изменили между чтением и записью, change зовётся
    заново на свежих данных, поэтому побочных эффектов у него быть не должно:
    события аудита вызывающий пишет после успешной записи, один раз.
    Не удалось за WRITE_ATTEMPTS попыток — busy().
    """
    for _ in range(WRITE_ATTEMPTS):
        try:
            blob = bucket.get_blob(path)            # вместе с номером поколения
            data = json.loads(blob.download_as_text()) if blob else empty()
            result = change(data)
            if result is None:
                return None
            bucket.blob(path).upload_from_string(
                _dumps(data), content_type="application/json",
                if_generation_match=blob.generation if blob else 0)
            return result
        except (PreconditionFailed, NotFound):
            continue        # объект успели изменить — берём свежий
    raise busy()


def commit_version(bucket, manifest_path, claim, base_version=None, stale=VersionStale):
    """Выпускает новую версию сущности. Возвращает результат claim.

    claim(version, current) создаёт объекты версии version через create_json
    и возвращает (манифест, результат) либо None, если номер уже занят.
    current — версия, поверх которой пишем: её claim ставит в
    supersedes_version.

    1. Манифест читается вместе с поколением. base_version задан и не совпал
       с текущей версией → stale().
    2. Номер — первый свободный после текущей. Занятый номер значит, что его
       пишет параллельный запрос или он остался от сбоя.
    3. Манифест пишется с предусловием. Не прошло — версию успели выпустить
       параллельно: с base_version это stale(), без неё — всё заново.

    Объект версии, не попавший в манифест, остаётся в хранилище
    незафиксированным: он не удаляется и в историю не входит (version_chain).
    Поэтому номера растут, но могут идти с пропусками.
    """
    for _ in range(WRITE_ATTEMPTS):
        try:
            man_blob = bucket.get_blob(manifest_path)
            manifest = json.loads(man_blob.download_as_text()) if man_blob else None
        except NotFound:
            continue        # манифест сменился между чтением поколения и содержимого
        current = manifest["current_version"] if manifest else 0
        if base_version is not None and base_version != current:
            raise stale()
        for version in range(current + 1, current + 1 + VERSION_SLOT_ATTEMPTS):
            claimed = claim(version, current)
            if claimed:
                break
        else:
            raise StorageBusy()
        new_manifest, result = claimed
        try:
            bucket.blob(manifest_path).upload_from_string(
                _dumps(new_manifest), content_type="application/json",
                if_generation_match=man_blob.generation if man_blob else 0)
        except PreconditionFailed:
            if base_version is not None:
                raise stale()
            continue
        return result
    raise StorageBusy()


def version_chain(payloads, current):
    """Номера зафиксированных версий, от текущей к старым.

    payloads — {номер: payload} загруженных версий; нечитаемая версия — None.
    Цепочка идёт по supersedes_version от текущей, поэтому версия, не попавшая
    в манифест (commit_version), в неё не входит. Без supersedes_version
    (нечитаемая версия) предыдущей считается номер на единицу меньше; номер,
    объекта которого нет, пропускается. Цепочка обрывается там, где кончаются
    загруженные версии.
    """
    chain, version = [], current
    lowest = min(payloads, default=0)
    while version >= max(lowest, 1):
        if version not in payloads:
            version -= 1
            continue
        chain.append(version)
        payload = payloads[version]
        prev = payload.get("supersedes_version") if isinstance(payload, dict) else None
        version = prev if isinstance(prev, int) and 0 <= prev < version else version - 1
    return chain


# ── Runs helpers ──────────────────────────────────────────────────────────────

def read_runs(bucket, sub):
    """Индекс пробежек (#75): текущие версии, скрытые — с deleted."""
    blob = bucket.blob(p_runs(sub))
    if not blob.exists():
        return []
    return json.loads(blob.download_as_text())


# ── Проверка входных данных (#53) ─────────────────────────────────────────────
#
# Тело запроса — недоверенный JSON: любой тип, любая длина. Функции clean_*
# устроены как clean_athlete_profile ниже: возвращают (значение, {поле: что не
# так}). При непустых ошибках хендлер отвечает 400 validation_failed и ничего
# не пишет. Проверяется только то, что пишется сейчас: чтение к записям,
# сохранённым раньше, остаётся терпимым.

SHORT_TEXT_MAX = 32          # время, темп, даты и тип недели, даты гонки
TITLE_MAX = 200              # название старта и гонки, акцент недели
RUN_NOTES_MAX = 2000
RUN_DIST_MAX_KM = 1000
RUN_HR_MIN, RUN_HR_MAX = 30, 250
MAX_ID = 2 ** 53 - 1         # целые, которые JavaScript хранит без потерь
PLAN_WEEKS_MAX = 104         # два года по строке на неделю
PLAN_WEEK_NUMBER_MAX = 999
PLAN_DAY_TEXT_MAX = 500
CHANGE_REASON_MAX = 200
# Формат токена — тот, что выдаёт write_parsed_fit_to_tmp: токен уходит в путь.
_FIT_TOKEN_RE = re.compile(r"^[0-9]{1,12}-[0-9a-f]{8}$")
_ISO_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _clean_text(value, limit):
    """(строка, ошибка). None — пустая строка, число приводится к строке."""
    if value is None:
        return "", None
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return "", "ожидается строка"
    text = value if isinstance(value, str) else str(value)
    if len(text) > limit:
        return "", f"длиннее {limit} символов"
    return text, None


def _clean_number(value, low, high):
    """Конечное число из [low, high] или None. Строка с числом годится."""
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if math.isfinite(number) and low <= number <= high else None


def _clean_id(value):
    """Целый положительный id или None."""
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 < value <= MAX_ID else None


def _clean_date(value):
    """'ГГГГ-ММ-ДД' существующего дня или None."""
    if not isinstance(value, str) or not _ISO_DATE_RE.match(value):
        return None
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None
    return value


def _clean_choice(value, allowed, default=None):
    """Значение из словаря допустимых; None — default. Иначе None."""
    if value is None:
        return default
    return value if isinstance(value, str) and value in allowed else None


def _clean_entity_head(raw, entity, errors):
    """id и дата — общие для пробежки и старта. id может не прийти: тогда
    его выдаёт сервер."""
    if raw.get("id") is None:
        entity["id"] = None
    else:
        entity["id"] = _clean_id(raw["id"])
        if entity["id"] is None:
            errors["id"] = "ожидается целое положительное число"
    entity["date"] = _clean_date(raw.get("date"))
    if entity["date"] is None:
        errors["date"] = "ожидается формат ГГГГ-ММ-ДД"


def _clean_text_fields(raw, entity, errors, limits, prefix=""):
    for field, limit in limits.items():
        entity[field], error = _clean_text(raw.get(field), limit)
        if error:
            errors[prefix + field] = error


def clean_run(raw, plans_index):
    """Пробежка из тела запроса → (пробежка, ошибки).

    id, plan_id и fit_token в результате могут быть None: id тогда выдаёт
    сервер, пробежка уходит в активный план, FIT к ней не приложен. plan_id
    принимается только из своего реестра планов (plans_index).
    """
    run, errors = {}, {}
    _clean_entity_head(raw, run, errors)

    run["dist"] = _clean_number(raw.get("dist"), 0, RUN_DIST_MAX_KM)
    if not run["dist"]:
        errors["dist"] = f"ожидается число больше 0 и не больше {RUN_DIST_MAX_KM}"

    for field, allowed, default in (("type", TYPE_LABELS, "easy"),
                                    ("feel", FEEL_LABELS, "good")):
        run[field] = _clean_choice(raw.get(field), allowed, default)
        if run[field] is None:
            errors[field] = "допустимо: " + ", ".join(allowed)

    run["hr"] = None
    if raw.get("hr") not in (None, ""):
        pulse = _clean_number(raw["hr"], RUN_HR_MIN, RUN_HR_MAX)
        if pulse is None:
            errors["hr"] = f"ожидается целое от {RUN_HR_MIN} до {RUN_HR_MAX}"
        else:
            run["hr"] = int(round(pulse))

    _clean_text_fields(raw, run, errors, {"time": SHORT_TEXT_MAX, "pace": SHORT_TEXT_MAX,
                                          "notes": RUN_NOTES_MAX})

    plan_id = raw.get("plan_id")
    run["plan_id"] = None
    if plan_id not in (None, ""):
        if isinstance(plan_id, str) and find_plan(plans_index, plan_id):
            run["plan_id"] = plan_id
        else:
            errors["plan_id"] = "нет такого плана"

    token = raw.get("fit_token")
    run["fit_token"] = None
    if token not in (None, ""):
        if isinstance(token, str) and _FIT_TOKEN_RE.match(token):
            run["fit_token"] = token
        else:
            errors["fit_token"] = "неверный формат"
    return run, errors


def clean_race(raw):
    """Старт из тела запроса → (старт, ошибки). id может быть None."""
    race, errors = {}, {}
    _clean_entity_head(raw, race, errors)
    _clean_text_fields(raw, race, errors, {"name": TITLE_MAX, "time": SHORT_TEXT_MAX})
    for field in ("name", "time"):
        race[field] = race[field].strip()
        if not race[field]:
            errors.setdefault(field, "не заполнено")
    race["dist_label"] = _clean_choice(raw.get("dist_label"), DIST_LABEL_KM)
    if race["dist_label"] is None:
        errors["dist_label"] = "допустимо: " + ", ".join(DIST_LABEL_KM)
    return race, errors


def clean_change_reason(value):
    """Причина правки в одну строку. Это пометка в истории, а не данные:
    лишнее обрезается, не-строка даёт пустую причину."""
    return " ".join(value.split())[:CHANGE_REASON_MAX] if isinstance(value, str) else ""


# ── Legacy race profile (до #25 здесь жили гонка, цель и старт плана) ─────────
#
# Данные гонки переехали в план (#25). Объект остаётся источником для ленивой
# миграции пользователей, заведённых раньше, — новых записей в него не делаем,
# кроме сида при переносе legacy-данных админа.

PROFILE_DEFAULT = {"race_name": "", "race_date": "", "target_time": "", "plan_start": ""}
PROFILE_FIELDS = ("race_name", "race_date", "target_time", "plan_start")


def read_legacy_race_profile(bucket, sub):
    blob = bucket.blob(p_profile(sub))
    if not blob.exists():
        return dict(PROFILE_DEFAULT)
    data = json.loads(blob.download_as_text())
    return {**PROFILE_DEFAULT, **{k: data.get(k, "") for k in PROFILE_FIELDS}}


def write_legacy_race_profile(bucket, sub, profile):
    """Сид legacy-профиля (миграция). Уже есть — не трогаем: None."""
    clean = {k: (profile.get(k) or "") for k in PROFILE_FIELDS}
    return clean if create_json(bucket, p_profile(sub), clean) else None


# ── Athlete profile (#32) ─────────────────────────────────────────────────────
#
# Профиль спортсмена — источник контекста для LLM. Хранится версионно:
# users/{sub}/athlete/manifest.json + v{N}/profile.json. Каждое сохранение —
# новая версия, поэтому история веса и пульсовых показателей копится сама.


ATHLETE_TEXT_FIELDS = ("full_name", "birth_date", "sex", "long_run_day", "injuries", "notes")

# поле: (минимум, максимум, округлять до целого)
ATHLETE_NUM_FIELDS = {
    "height_cm":         (100, 250, True),
    "weight_kg":         (30, 200, False),
    "hr_max":            (100, 230, True),
    "hr_threshold":      (80, 220, True),
    "hr_rest":           (30, 120, True),
    "vo2max":            (20, 90, False),
    "years_running":     (0, 70, False),
    "weekly_km_typical": (0, 300, False),
    "sessions_per_week": (0, 14, True),
}

# Поля, по которым имеет смысл смотреть динамику между версиями
ATHLETE_HISTORY_FIELDS = ("weight_kg", "hr_max", "hr_threshold", "hr_rest", "vo2max",
                          "threshold_pace_sec")

# Ручные пульсовые зоны (#56) — нижние границы Z1…Z5 в уд/мин. Коридор тот же,
# что у пульса покоя снизу и максимального сверху.
HR_ZONE_START_MIN, HR_ZONE_START_MAX = 30, 230


def empty_athlete_profile():
    profile = {f: "" for f in ATHLETE_TEXT_FIELDS}
    profile.update({f: None for f in ATHLETE_NUM_FIELDS})
    profile["available_days"] = []
    profile["hr_zones_custom"] = None       # None — зоны считаются от HRmax
    profile["threshold_pace_sec"] = None    # None — порог оценивается по «Стартам» (#66)
    # Погода (#72): место прогноза, час тренировки, площадки и чувствительность к холоду
    profile["location"] = None
    profile["run_time"] = ""
    profile["venues"] = []
    profile["cold_sensitivity"] = ""
    return profile


# Поля, появившиеся в профиле после #32. Клиент, который о них не знает,
# присылает профиль без них, а POST заменяет профиль целиком: такое поле берётся
# из текущей версии (api.h_profile_post). Явный null или пустое значение сбрасывает.
LATE_PROFILE_FIELDS = ("threshold_pace_sec", "location", "run_time", "venues",
                       "cold_sensitivity")


def clean_location(value):
    """Место для прогноза (#72): ({name, lat, lon} или None, ошибка или None).

    Координаты округляются до 0,01° (~1 км): прогнозу точнее не нужно, а
    точка «Моё местоположение» не должна указывать на дом.
    """
    if value in (None, "", {}):
        return None, None
    if not isinstance(value, dict):
        return None, "ожидается объект {name, lat, lon}"
    name = value.get("name")
    name = " ".join(name.split()) if isinstance(name, str) else ""
    if not name:
        return None, "укажите название места"
    if len(name) > LOCATION_NAME_MAX:
        return None, f"название длиннее {LOCATION_NAME_MAX} символов"
    coords = []
    for key, limit in (("lat", 90), ("lon", 180)):
        number = value.get(key)
        # Сравнение отсекает NaN и бесконечность; bool — не координата.
        if isinstance(number, bool) or not isinstance(number, (int, float)) \
                or not -limit <= number <= limit:
            return None, "координаты вне допустимого диапазона"
        coords.append(round(number, 2))
    return {"name": name, "lat": coords[0], "lon": coords[1]}, None


def clean_threshold_pace(value):
    """Пороговый темп (#66): «м:сс» или секунды на км → (секунды или None, ошибка).

    Поле скалярное, поэтому список, объект и bool — ошибка, а не «пусто».
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return None, None
    if isinstance(value, str):
        seconds = parse_time_to_sec(value.strip())
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        seconds = value
    else:
        seconds = None
    if seconds is None:
        return None, "ожидается темп «м:сс»"
    # Сравнение отсекает и NaN, и бесконечность, и целое больше float —
    # math.isfinite на последнем падает.
    if not THRESHOLD_PACE_MIN <= seconds <= THRESHOLD_PACE_MAX:
        return None, (f"допустимо от {format_duration(THRESHOLD_PACE_MIN)} "
                      f"до {format_duration(THRESHOLD_PACE_MAX)} на км")
    return int(round(seconds)), None


def clean_hr_zone_starts(value, hr_max):
    """Ручные границы зон: (список целых или None, текст ошибки или None).

    Зоны задаются все разом. Смешать ручные границы с расчётными нельзя: при
    смене максимального пульса расчётные переехали бы через ручные.
    """
    if value in (None, "", []):
        return None, None
    count = len(HR_ZONE_BOUNDS)
    if not isinstance(value, list) or len(value) != count:
        return None, f"ожидается {count} границ — по одной на зону"
    starts = []
    for item in value:
        try:
            number = float(item)
        except (TypeError, ValueError, OverflowError):
            return None, "заполните все границы числами"
        if not (HR_ZONE_START_MIN <= number <= HR_ZONE_START_MAX):
            return None, f"допустимо от {HR_ZONE_START_MIN} до {HR_ZONE_START_MAX}"
        starts.append(int(round(number)))
    if any(low >= high for low, high in zip(starts, starts[1:])):
        return None, "границы должны идти по возрастанию"
    # С оценкой по возрасту не сверяем: зоны правят как раз те, у кого она врёт.
    if hr_max and starts[-1] >= hr_max:
        return None, "начало Z5 должно быть ниже максимального пульса"
    return starts, None


def clean_athlete_profile(raw):
    """Валидация и нормализация входных данных. Возвращает (профиль, ошибки)."""
    if not isinstance(raw, dict):
        return empty_athlete_profile(), {"profile": "ожидается объект"}
    errors = {}
    profile = {}
    day_codes = [code for code, _ in PLAN_DAYS]

    for field in ATHLETE_TEXT_FIELDS:
        value = raw.get(field, "")
        profile[field] = value.strip() if isinstance(value, str) else ""

    if profile["sex"] not in ("", "m", "f"):
        errors["sex"] = "допустимо: m, f или пусто"
        profile["sex"] = ""

    if profile["long_run_day"] and profile["long_run_day"] not in day_codes:
        errors["long_run_day"] = "неизвестный день недели"
        profile["long_run_day"] = ""

    raw_days = raw.get("available_days") or []
    if not isinstance(raw_days, list):
        errors["available_days"] = "ожидается список дней"
        raw_days = []
    unknown = [str(d) for d in raw_days if d not in day_codes]
    if unknown:
        errors["available_days"] = "неизвестные дни: " + ", ".join(unknown)
    profile["available_days"] = [d for d in day_codes if d in raw_days]

    if profile["birth_date"]:
        try:
            born = datetime.strptime(profile["birth_date"], "%Y-%m-%d").date()
            today = datetime.utcnow().date()
            if born > today:
                errors["birth_date"] = "дата рождения в будущем"
            elif (today - born).days > 100 * 366:
                errors["birth_date"] = "возраст больше 100 лет"
        except ValueError:
            errors["birth_date"] = "ожидается формат ГГГГ-ММ-ДД"

    for field, (low, high, as_int) in ATHLETE_NUM_FIELDS.items():
        value = raw.get(field)
        if value in (None, "", []):
            profile[field] = None
            continue
        try:
            number = float(value)
        except (TypeError, ValueError, OverflowError):
            errors[field] = "ожидается число"
            profile[field] = None
            continue
        if not (low <= number <= high):
            errors[field] = f"допустимо от {low} до {high}"
            profile[field] = None
            continue
        profile[field] = int(round(number)) if as_int else round(number, 1)

    # Пустой список доступных дней означает «ограничений нет» — проверяем только
    # заданное расписание, иначе промпт противоречит сам себе: «доступны пн, ср,
    # сб; длительная — вс» при запрете тренироваться в недоступные дни.
    if profile["available_days"] and profile["long_run_day"] \
            and profile["long_run_day"] not in profile["available_days"]:
        errors["long_run_day"] = "день длительной не входит в доступные дни"

    if profile.get("hr_max") and profile.get("hr_threshold") and profile["hr_threshold"] >= profile["hr_max"]:
        errors["hr_threshold"] = "ПАНО должен быть ниже максимального пульса"
    if profile.get("hr_max") and profile.get("hr_rest") and profile["hr_rest"] >= profile["hr_max"]:
        errors["hr_rest"] = "пульс покоя должен быть ниже максимального"

    profile["hr_zones_custom"], zones_error = clean_hr_zone_starts(
        raw.get("hr_zones_custom"), profile.get("hr_max"))
    if zones_error:
        errors["hr_zones_custom"] = zones_error

    profile["threshold_pace_sec"], pace_error = clean_threshold_pace(raw.get("threshold_pace_sec"))
    if pace_error:
        errors["threshold_pace_sec"] = pace_error

    profile["location"], location_error = clean_location(raw.get("location"))
    if location_error:
        errors["location"] = location_error

    run_time = raw.get("run_time")
    run_time = run_time.strip() if isinstance(run_time, str) else run_time
    profile["run_time"] = ""
    if run_time not in (None, ""):
        # parse_run_time молча подставляет время по умолчанию — здесь это ошибка
        hh, mm = parse_run_time(run_time)
        if not isinstance(run_time, str) or run_time != f"{hh:02d}:{mm:02d}":
            errors["run_time"] = "ожидается время ЧЧ:ММ"
        else:
            profile["run_time"] = run_time

    raw_venues = raw.get("venues") or []
    if not isinstance(raw_venues, list):
        errors["venues"] = "ожидается список"
        raw_venues = []
    unknown = [str(v) for v in raw_venues if v not in VENUES]
    if unknown:
        errors["venues"] = "неизвестные площадки: " + ", ".join(unknown)
    profile["venues"] = [v for v in VENUES if v in raw_venues]

    sensitivity = raw.get("cold_sensitivity") or ""
    if sensitivity not in COLD_SENSITIVITY:
        errors["cold_sensitivity"] = "допустимо: cold, warm или пусто"
        sensitivity = ""
    profile["cold_sensitivity"] = sensitivity

    return profile, errors


def athlete_age(birth_date, today=None):
    if not birth_date:
        return None
    try:
        born = datetime.strptime(birth_date, "%Y-%m-%d").date()
    except ValueError:
        return None
    today = today or datetime.utcnow().date()
    years = today.year - born.year - ((today.month, today.day) < (born.month, born.day))
    return years if 0 <= years <= 100 else None


def manual_hr_zones(profile, hr_max):
    """Зоны по ручным границам из профиля (#56); None — границы не заданы.

    Верх зоны — начало следующей, как и у расчётных. Верх последней — HRmax,
    если он известен и выше её начала: отдельный «верх Z5» был бы вторым HRmax.
    """
    starts = profile.get("hr_zones_custom")
    if not isinstance(starts, list) or len(starts) != len(HR_ZONE_BOUNDS) \
            or not all(isinstance(s, int) for s in starts):
        return None
    tops = starts[1:] + [hr_max if hr_max and hr_max > starts[-1] else None]
    return [{"name": name, "from": low, "to": high}
            for (name, _, _), low, high in zip(HR_ZONE_BOUNDS, starts, tops)]


def compute_athlete_derived(profile, today=None):
    """Возраст, ИМТ, оценка HRmax и пульсовые зоны. Не хранится — считается на лету.

    hr_zones — действующие зоны: ручные, если спортсмен их задал, иначе
    расчётные. hr_zones_auto — всегда расчётные: с них начинается ручная правка.
    """
    age = athlete_age(profile.get("birth_date"), today)
    derived = {"age": age, "bmi": None, "hr_max_estimated": None,
               "hr_max_effective": None, "hr_zones": [],
               "hr_zones_auto": [], "hr_zones_source": "auto"}

    height, weight = profile.get("height_cm"), profile.get("weight_kg")
    if height and weight:
        derived["bmi"] = round(weight / (height / 100) ** 2, 1)

    if not profile.get("hr_max") and age is not None:
        derived["hr_max_estimated"] = int(round(208 - 0.7 * age))   # формула Танаки

    effective = profile.get("hr_max") or derived["hr_max_estimated"]
    derived["hr_max_effective"] = effective
    if effective:
        derived["hr_zones_auto"] = [
            {"name": name,
             "from": int(round(effective * low / 100)),
             "to": int(round(effective * high / 100))}
            for name, low, high in HR_ZONE_BOUNDS
        ]
    derived["hr_zones"] = derived["hr_zones_auto"]

    manual = manual_hr_zones(profile, effective)
    if manual:
        derived["hr_zones"] = manual
        derived["hr_zones_source"] = "manual"
    return derived


def athlete_derived(profile, races, today=None):
    """compute_athlete_derived плюс пороговый темп (#66): ручной или оценка по
    «Стартам». Отдельной функцией, потому что порогу нужны забеги.

    threshold_pace_auto — оценка всегда, даже при ручном пороге: её профиль
    показывает подсказкой рядом с полем.
    """
    derived = compute_athlete_derived(profile, today)
    threshold = threshold_for(profile, races, today or datetime.utcnow().date())
    derived.update(threshold_pace_sec=threshold["threshold_pace_sec"],
                   threshold_source=threshold["threshold_source"],
                   threshold_basis=threshold["threshold_basis"],
                   threshold_pace_auto=threshold["threshold_auto"])
    return derived


def read_athlete_manifest(bucket, sub):
    blob = bucket.blob(p_athlete_manifest(sub))
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def read_athlete_profile(bucket, sub):
    """(профиль, версия, когда обновлён). Незаполненный профиль — версия 0."""
    manifest = read_athlete_manifest(bucket, sub)
    if not manifest:
        return empty_athlete_profile(), 0, None
    blob = bucket.blob(manifest["gcs_object_path"])
    if not blob.exists():
        return empty_athlete_profile(), 0, None
    data = json.loads(blob.download_as_text())
    profile = empty_athlete_profile()
    stored = data.get("profile") or {}
    profile.update({k: v for k, v in stored.items() if k in profile})
    return profile, data.get("version", 0), manifest.get("updated_at")


def write_athlete_version(bucket, sub, profile, change_reason="", created_by="api"):
    """Новая версия профиля (commit_version) — её payload."""
    def claim(version, current):
        object_path = p_athlete_ver(sub, version)
        now = datetime.utcnow().isoformat() + "Z"
        payload = {
            "version": version,
            "is_current": True,
            "created_at": now,
            "created_by": created_by,
            "change_reason": change_reason or "profile update",
            "supersedes_version": current or None,
            "profile": profile,
        }
        if not create_json(bucket, object_path, payload):
            return None
        return {"current_version": version, "gcs_object_path": object_path,
                "updated_at": now}, payload

    return commit_version(bucket, p_athlete_manifest(sub), claim)


ATHLETE_HISTORY_LIMIT = 50


def read_athlete_history(bucket, sub, limit=ATHLETE_HISTORY_LIMIT):
    """Сводка по последним версиям профиля — для динамики веса и пульса.

    Каждая версия — отдельный объект в GCS, поэтому читаем ограниченное окно:
    у пользователя с сотнями сохранений полный обход упёрся бы в таймаут.
    Версии, не попавшие в манифест (#35), в историю не входят.
    """
    manifest = read_athlete_manifest(bucket, sub)
    if not manifest:
        return []
    current = manifest["current_version"]
    numbers = list(range(max(1, current - limit + 1), current + 1))
    payloads = dict(zip(numbers, _load_json_objects(
        bucket, [p_athlete_ver(sub, v) for v in numbers], skip_broken=True)))
    history = []
    for version in reversed(version_chain(payloads, current)):
        data = payloads[version]
        if not isinstance(data, dict):
            continue
        stored = data.get("profile") or {}
        history.append({
            "version": data.get("version", version),
            "created_at": data.get("created_at"),
            "change_reason": data.get("change_reason", ""),
            **{f: stored.get(f) for f in ATHLETE_HISTORY_FIELDS},
        })
    return history


# ── Races helpers ─────────────────────────────────────────────────────────────

def read_races(bucket, sub):
    """Индекс стартов (#75), как read_runs."""
    blob = bucket.blob(p_races(sub))
    if not blob.exists():
        return []
    return json.loads(blob.download_as_text())


# ── FIT parsing + run details helpers ────────────────────────────────────────

def _spm(rec):
    """FIT хранит каденс как cycles/min (одна нога). Возвращаем шаги/мин."""
    base = rec.get("avg_running_cadence") or rec.get("avg_cadence")
    if base is None:
        return None
    frac = rec.get("avg_fractional_cadence") or 0
    return int(round((base + frac) * 2))


def _fmt_pace(sec_per_km):
    if not sec_per_km or sec_per_km <= 0:
        return None
    return f"{int(sec_per_km) // 60}:{int(sec_per_km) % 60:02d}"


def parse_fit_file(fit_bytes):
    """Парсит FIT и возвращает {date, summary, laps, samples}."""
    fit = FitFile(io.BytesIO(fit_bytes))
    session = None
    laps = []
    records = []
    for msg in fit.get_messages():
        name = msg.name
        if name == "session" and session is None:
            session = {f.name: f.value for f in msg}
        elif name == "lap":
            laps.append({f.name: f.value for f in msg})
        elif name == "record":
            records.append({f.name: f.value for f in msg})

    summary = {}
    if session:
        dist_m = session.get("total_distance") or 0
        dur_sec = session.get("total_elapsed_time") or 0
        summary = {
            "dist_km": round(dist_m / 1000, 2) if dist_m else 0,
            "duration_sec": int(dur_sec) if dur_sec else 0,
            "avg_hr": session.get("avg_heart_rate"),
            "max_hr": session.get("max_heart_rate"),
            "avg_cadence": _spm(session),
            "total_ascent_m": session.get("total_ascent"),
            "total_descent_m": session.get("total_descent"),
            "calories": session.get("total_calories"),
            "avg_power_w": session.get("avg_power"),
            "max_power_w": session.get("max_power"),
        }
        if summary["duration_sec"] and summary["dist_km"]:
            summary["avg_pace_sec_per_km"] = int(summary["duration_sec"] / summary["dist_km"])

    lap_list = []
    for i, lap in enumerate(laps, 1):
        dist_m = lap.get("total_distance") or 0
        dur_sec = lap.get("total_elapsed_time") or 0
        dist_km = round(dist_m / 1000, 3) if dist_m else 0
        pace_sec = int(dur_sec / dist_km) if (dist_km and dur_sec) else None
        lap_list.append({
            "lap": i,
            "dist_km": dist_km,
            "duration_sec": round(dur_sec, 1) if dur_sec else 0,
            "pace": _fmt_pace(pace_sec),
            "avg_hr": lap.get("avg_heart_rate"),
            "max_hr": lap.get("max_heart_rate"),
            "cadence": _spm(lap),
            "ascent_m": lap.get("total_ascent"),
        })

    samples = {"t_offset_sec": [], "hr": [], "pace_sec_per_km": [], "altitude_m": []}
    if records:
        first_ts = next((r.get("timestamp") for r in records if r.get("timestamp")), None)
        if first_ts:
            last_kept = -5.0
            for r in records:
                ts = r.get("timestamp")
                if ts is None:
                    continue
                t_offset = (ts - first_ts).total_seconds()
                if t_offset < last_kept + 5:
                    continue
                last_kept = t_offset
                samples["t_offset_sec"].append(int(t_offset))
                samples["hr"].append(r.get("heart_rate"))
                speed = r.get("enhanced_speed") or r.get("speed")  # м/с
                if speed and speed > 0.1:
                    samples["pace_sec_per_km"].append(int(1000 / speed))
                else:
                    samples["pace_sec_per_km"].append(None)
                alt = r.get("enhanced_altitude") or r.get("altitude")
                samples["altitude_m"].append(round(alt, 1) if alt is not None else None)

    # Дата активности
    start = None
    if session and session.get("start_time"):
        start = session["start_time"]
    elif records:
        start = next((r.get("timestamp") for r in records if r.get("timestamp")), None)
    date_str = start.date().isoformat() if start and hasattr(start, "date") else None

    return {
        "date": date_str,
        "summary": summary,
        "laps": lap_list,
        "samples": samples,
    }


def run_details_path(manifest):
    """Путь к деталям FIT текущей версии пробежки; None — FIT нет. До #75
    манифест указывал на сами детали, с #75 — на run.json, а детали —
    отдельным полем (они могут лежать в более ранней версии)."""
    if "details_object_path" in manifest:
        return manifest["details_object_path"]
    return manifest.get("gcs_object_path")


def read_run_details(bucket, sub, run_id):
    """Читает детали пробежки из namespace пользователя (sub — владелец данных).
    Ленивый fallback: если в per-user namespace деталей нет, но есть legacy
    (глобальные) — копирует их в namespace и возвращает. Вызывающий обязан
    заранее убедиться, что run_id принадлежит этому пользователю (есть в его
    runs.json).
    """
    man_blob = bucket.blob(p_run_manifest(sub, run_id))
    if man_blob.exists():
        path = run_details_path(json.loads(man_blob.download_as_text()))
        details_blob = bucket.blob(path) if path else None
        if details_blob is None or not details_blob.exists():
            return None
        return json.loads(details_blob.download_as_text())
    return _copy_legacy_run_details(bucket, sub, run_id)


def _copy_legacy_run_details(bucket, sub, run_id):
    """Детали из legacy (глобальных) runs/{id}/ — в namespace sub как v1 со
    своим манифестом. None — переносить нечего или не положено."""
    # Ленивый перенос legacy — только владельцу этих данных, то есть админу
    # (#53). Проверки «run_id есть в runs.json» мало: id пробежки задаёт
    # клиент, и совпадение с чужим legacy-id отдало бы чужую тренировку.
    # Тренер админа сюда приходит с sub админа и детали по-прежнему видит.
    if not _is_admin(bucket, sub):
        return None
    legacy_man = bucket.blob(legacy_run_manifest(run_id))
    if not legacy_man.exists():
        return None
    lman = json.loads(legacy_man.download_as_text())
    legacy_details = bucket.blob(lman["gcs_object_path"])
    if not legacy_details.exists():
        return None

    # Перенос идемпотентен: параллельный запрос мог успеть первым, тогда его
    # копии и манифест остаются как есть.
    legacy_fit = bucket.blob(legacy_run_fit(run_id))
    if legacy_fit.exists():
        copy_once(bucket, legacy_fit, p_run_fit(sub, run_id))
    copy_once(bucket, legacy_details, p_run_details(sub, run_id))
    create_json(bucket, p_run_manifest(sub, run_id), {
        "current_version": 1,
        "gcs_object_path": p_run_details(sub, run_id),
        "updated_at": datetime.utcnow().isoformat() + "Z",
    })
    return json.loads(bucket.blob(p_run_details(sub, run_id)).download_as_text())


def cleanup_old_tmp(bucket, sub, max_age_hours=24):
    """Удаляет tmp/{sub}/{token}/* старше max_age_hours (ephemeral temp data —
    удаление допустимо по CLAUDE.md). Ограничено namespace пользователя.
    """
    cutoff = int(datetime.utcnow().timestamp()) - max_age_hours * 3600
    deleted = 0
    for blob in bucket.list_blobs(prefix=f"tmp/{sub}/"):
        parts = blob.name.split("/")   # ["tmp", sub, token, filename]
        if len(parts) < 3:
            continue
        token = parts[2]
        try:
            ts = int(token.split("-")[0])
            if ts < cutoff:
                blob.delete()
                deleted += 1
        except (ValueError, IndexError):
            continue
    return deleted


def write_parsed_fit_to_tmp(bucket, sub, fit_bytes, parsed):
    """Кладёт FIT и parsed details во временный per-user префикс. Возвращает token."""
    token = f"{int(datetime.utcnow().timestamp())}-{secrets_mod.token_hex(4)}"
    bucket.blob(p_tmp_fit(sub, token)).upload_from_string(
        fit_bytes, content_type="application/octet-stream"
    )
    details_payload = {
        "source": "garmin_fit",
        "date": parsed.get("date"),
        "summary": parsed.get("summary", {}),
        "laps": parsed.get("laps", []),
        "samples": parsed.get("samples", {"t_offset_sec": [], "hr": [], "pace_sec_per_km": [], "altitude_m": []}),
    }
    bucket.blob(p_tmp_details(sub, token)).upload_from_string(
        json.dumps(details_payload, ensure_ascii=False, indent=2, default=str),
        content_type="application/json"
    )
    return token


# ── Пробежки и старты: версии (#75) ──────────────────────────────────────────
#
# Пробежка и старт — сущности с версиями, как план: манифест и неизменяемые
# v{N}/run.json (race.json). Создание, правка, скрытие и восстановление —
# новая версия через commit_version, скрытие — status DELETED. Манифест
# пробежки общий с деталями FIT: details.json и activity.fit лежат в той
# версии, где FIT приложили, а run.json и манифест ссылаются на текущие.
#
# runs.json и races.json — производный индекс текущих версий: после каждой
# версии в него ставится её запись, rebuild_index собирает его заново по
# манифестам. Формат прежний, плюс version, поэтому читатели не меняются.
# Записи, сделанные до #75, переносит в версии migrate_entity_history, а до
# миграции — первая же запись этой сущности (_ensure_versioned).

ACTIVE, DELETED = "ACTIVE", "DELETED"
RUN_FIELDS = ("date", "dist", "type", "time", "pace", "hr", "feel", "notes", "plan_id")
# Поля из FIT пишет сервер: правка пробежки их не трогает.
RUN_FIT_FIELDS = ("details_available", "max_hr", "avg_cadence", "total_ascent_m", "calories")
RACE_FIELDS = ("name", "date", "dist_label", "time")
ENTITY_HISTORY_LIMIT = 50
MIGRATION_AUTHOR = "auto-migrate"
_INDEX_META = ("id", "deleted", "deleted_at", "version")
_ENTITY_VER_RE = re.compile(r"/v(\d+)/(run|race|details)\.json$")


class RunStale(VersionStale):
    """Правка сделана поверх версии пробежки, которая уже не текущая."""


class RaceStale(VersionStale):
    """Правка сделана поверх версии старта, которая уже не текущая."""


class EntityDeleted(Exception):
    """Правка скрытой пробежки или старта: сначала её восстанавливают."""


class FitTokenInvalid(ValueError):
    """FIT по токену нет в tmp/: просрочен или не тот."""


class _Unchanged(Exception):
    """Новая версия ничего бы не поменяла: payload — текущая."""

    def __init__(self, payload):
        super().__init__()
        self.payload = payload


class EntityKind:
    """Пробежка или старт: пути, поля и исключение устаревшей правки."""

    def __init__(self, name, index, folder, manifest, version, fields, carried, links, stale):
        self.name = name            # ключ снимка полей в payload: run / race
        self.index = index
        self.folder = folder
        self.manifest = manifest
        self.version = version
        self.fields = fields        # поля, которые вводит пользователь
        self.carried = carried      # поля сервера в снимке: переходят из версии в версию
        self.links = links          # ссылки payload на объекты рядом (детали FIT)
        self.stale = stale


RUNS = EntityKind("run", p_runs, p_runs_dir, p_run_manifest, p_run_ver, RUN_FIELDS,
                  RUN_FIT_FIELDS, ("details_object_path", "fit_object_path"), RunStale)
RACES = EntityKind("race", p_races, p_races_dir, p_race_manifest, p_race_ver, RACE_FIELDS,
                   (), (), RaceStale)


def _read_json(bucket, path):
    blob = bucket.blob(path)
    return json.loads(blob.download_as_text()) if blob.exists() else None


def _is_versioned(kind, sub, eid, manifest):
    """Манифест указывает на версию сущности. До #75 манифест пробежки
    указывал на детали FIT — такая пробежка ещё не перенесена."""
    return isinstance(manifest, dict) and \
        manifest.get("gcs_object_path") == kind.version(sub, eid, manifest.get("current_version"))


def _links(kind, payload):
    return {key: (payload or {}).get(key) for key in kind.links}


def _index_entry(kind, payload):
    """Запись индекса по версии: снимок полей, deleted, version."""
    entry = {"id": payload["id"], **(payload.get(kind.name) or {}),
             "deleted": payload.get("status") == DELETED, "version": payload["version"]}
    if entry["deleted"]:
        entry["deleted_at"] = payload.get("deleted_at")
    return entry


def _put_index(bucket, kind, sub, payload):
    """Ставит в индекс запись версии и возвращает её. Монотонно: если в
    индексе уже версия новее, ничего не пишется, поэтому две версии, чьи
    записи пришли в обратном порядке, сходятся на текущей."""
    entry = _index_entry(kind, payload)

    def put(entries):
        at = next((i for i, e in enumerate(entries) if e.get("id") == entry["id"]), None)
        if at is None:
            entries.insert(0, entry)
        elif entries[at] == entry or (entries[at].get("version") or 0) > entry["version"]:
            return None
        else:
            entries[at] = entry
        return entry

    update_json(bucket, kind.index(sub), put, list)
    return entry


def _commit(bucket, kind, sub, eid, build, base_version=None, created_by="api",
            author="athlete", change_reason="", index=True):
    """Новая версия сущности → её payload (или текущий, если писать нечего).

    build(prev, version, current) по текущему payload (None — версий ещё
    нет) возвращает состояние новой версии: status, deleted_at, deleted_by,
    снимок полей под kind.name, ссылки kind.links и, по желанию,
    change_reason. Объекты рядом с payload (детали FIT) build создаёт сам;
    номер занят — None. Писать нечего — _Unchanged. build зовётся внутри
    claim, то есть по версии, которую манифест подтвердит предусловием, а
    при повторе — заново, на свежей.
    """
    def claim(version, current):
        prev = _read_json(bucket, kind.version(sub, eid, current)) if current else None
        state = build(prev, version, current)
        if state is None:
            return None
        now = datetime.utcnow().isoformat() + "Z"
        path = kind.version(sub, eid, version)
        payload = {"id": eid, "version": version, "is_current": True,
                   "created_at": now, "created_by": created_by, "author": author,
                   "change_reason": change_reason, "supersedes_version": current or None,
                   **state}
        # payload пишется последним: папка версии, где он есть, полная.
        if not create_json(bucket, path, payload):
            return None
        manifest = {"current_version": version, "gcs_object_path": path,
                    "status": payload["status"], "created_at": now, "created_by": created_by,
                    "change_reason": payload["change_reason"],
                    "checksum": hashlib.sha256(_dumps(payload).encode()).hexdigest()}
        if "details_object_path" in kind.links:
            manifest["details_object_path"] = payload["details_object_path"]
        return manifest, payload

    try:
        payload = commit_version(bucket, kind.manifest(sub, eid), claim,
                                 base_version=base_version, stale=kind.stale)
    except _Unchanged as same:
        payload = same.payload
    if index:
        _put_index(bucket, kind, sub, payload)
    return payload


def _sibling_fit(bucket, details_path):
    """activity.fit рядом с деталями — если он там есть."""
    if not details_path or not details_path.endswith("/details.json"):
        return None
    path = details_path[:-len("details.json")] + "activity.fit"
    return path if bucket.blob(path).exists() else None


def _details_before_versions(bucket, sub, run_id, manifest, legacy=True):
    """(ссылки, манифест): детали FIT пробежки, у которой ещё нет версий, — из
    манифеста деталей до #75, а если его нет — из legacy runs/{id}/ (только
    у админа, _copy_legacy_run_details; тогда манифест появляется). manifest
    — уже прочитанный или None."""
    if manifest is None and legacy:
        _copy_legacy_run_details(bucket, sub, run_id)
        manifest = _read_json(bucket, p_run_manifest(sub, run_id))
    details = run_details_path(manifest) if manifest else None
    links = {"details_object_path": details, "fit_object_path": _sibling_fit(bucket, details)}
    return links, manifest


def _migrate_entity(bucket, kind, sub, entry, manifest):
    """Базовая версия из записи индекса до #75 — снимок как есть. manifest —
    уже прочитанный манифест (у пробежки до #75 — деталей FIT) или None.
    False — базовую версию успел записать параллельный запрос."""
    eid = entry["id"]
    deleted = bool(entry.get("deleted"))
    state = {"status": DELETED if deleted else ACTIVE,
             "deleted_at": entry.get("deleted_at") if deleted else None, "deleted_by": None,
             kind.name: {k: v for k, v in entry.items() if k not in _INDEX_META}}
    if kind is RUNS:
        links, manifest = _details_before_versions(bucket, sub, eid, manifest,
                                                   legacy=bool(entry.get("details_available")))
        state.update(links)
    try:
        # base_version — увиденная текущая: вторая базовая версия не пройдёт.
        _commit(bucket, kind, sub, eid, lambda prev, version, current: state,
                base_version=manifest.get("current_version", 0) if manifest else 0,
                created_by=MIGRATION_AUTHOR, author="migration",
                change_reason=f"перенос из {kind.index(sub).rsplit('/', 1)[-1]} (#75)",
                index=False)
    except VersionStale:
        return False
    return True


def _ensure_versioned(bucket, kind, sub, eid):
    """Есть ли у сущности версии; запись индекса без них сначала переносится
    (_migrate_entity). Индекс тут не пишется: его поставит запись, ради
    которой переносили."""
    manifest = _read_json(bucket, kind.manifest(sub, eid))
    if _is_versioned(kind, sub, eid, manifest):
        return True
    entry = next((e for e in _read_index(bucket, kind, sub)
                  if isinstance(e, dict) and e.get("id") == eid), None)
    if entry is None:
        return False
    _migrate_entity(bucket, kind, sub, entry, manifest)
    return True


def _read_index(bucket, kind, sub):
    return _read_json(bucket, kind.index(sub)) or []


def _snapshot(kind, fields, old):
    """Снимок новой версии: поля пользователя — те, что пришли (из запроса
    приходят все), поля сервера — из текущей версии."""
    snapshot = {f: fields[f] for f in kind.fields if f in fields}
    snapshot.update({f: old[f] for f in kind.carried if f in old})
    return snapshot


def _editable(prev):
    if prev and prev.get("status") == DELETED:
        raise EntityDeleted()


def _active_state(kind, prev, snapshot, links, change_reason):
    """Активная версия со снимком snapshot; совпала с текущей — _Unchanged."""
    state = {"status": ACTIVE, "deleted_at": None, "deleted_by": None,
             kind.name: snapshot, **links}
    if prev and all(prev.get(k) == v for k, v in state.items()):
        raise _Unchanged(prev)
    if not change_reason:
        state["change_reason"] = "правка" if prev else "создание"
    return state


def _staged_fit(bucket, sub, fit_token):
    """FIT, разобранный в tmp/ (write_parsed_fit_to_tmp)."""
    tmp_fit = bucket.blob(p_tmp_fit(sub, fit_token))
    tmp_details = bucket.blob(p_tmp_details(sub, fit_token))
    if not tmp_fit.exists() or not tmp_details.exists():
        raise FitTokenInvalid(f"FIT token expired or invalid: {fit_token}")
    return {"fit": tmp_fit, "details": tmp_details,
            "raw": json.loads(tmp_details.download_as_text())}


def _place_fit(bucket, sub, run_id, version, current, staged):
    """details.json и activity.fit из tmp/ — в папку версии; ссылки на них.
    None — номер занят: объект уже есть (след сбоя или параллельной записи)."""
    raw = staged["raw"]
    fit_path, details_path = p_run_fit(sub, run_id, version), p_run_details(sub, run_id, version)
    details = {
        "version": version,
        "is_current": True,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "supersedes_version": current or None,
        "source": "garmin_fit",
        "fit_object_path": fit_path,
        "date": raw.get("date"),
        "summary": raw.get("summary", {}) or {},
        "laps": raw.get("laps", []),
        "samples": raw.get("samples", {}),
    }
    if not create_json(bucket, details_path, details) or \
            not copy_once(bucket, staged["fit"], fit_path):
        return None
    staged["placed"] = details
    return {"details_object_path": details_path, "fit_object_path": fit_path}


def _drop_tmp_fit(staged):
    """tmp/ — временные данные (CLAUDE.md): после переноса удаляются."""
    staged["fit"].delete()
    staged["details"].delete()


def save_run(bucket, sub, run, fit_token=None, base_version=None, change_reason="",
             created_by="api", author="athlete", default_plan_id=None):
    """Создание или правка пробежки → запись индекса.

    run — id и поля пользователя (RUN_FIELDS). Поля из FIT и детали
    переходят из текущей версии; с fit_token в новую версию ложатся свои
    details.json и activity.fit из tmp/ — FIT к той же пробежке ещё раз
    тоже новая версия. plan_id не пришёл: при правке остаётся прежний, у
    новой пробежки — default_plan_id (активный план). base_version не
    совпал с текущей → RunStale, скрытая пробежка → EntityDeleted.
    """
    run_id = run["id"]
    staged = _staged_fit(bucket, sub, fit_token) if fit_token else None
    inherited = {}
    if not _ensure_versioned(bucket, RUNS, sub, run_id):
        # Новая пробежка. Под её id уже могут быть детали без версий — legacy
        # у админа или зафиксированные до #75 без записи в runs.json: версия
        # ссылается на них.
        inherited, _ = _details_before_versions(
            bucket, sub, run_id, _read_json(bucket, p_run_manifest(sub, run_id)))

    def build(prev, version, current):
        _editable(prev)
        old = (prev or {}).get("run") or {}
        snapshot = _snapshot(RUNS, run, old)
        if snapshot.get("plan_id") is None:
            snapshot["plan_id"] = old.get("plan_id") if prev else default_plan_id
        links = _links(RUNS, prev) if prev else {**_links(RUNS, None), **inherited}
        if staged:
            links = _place_fit(bucket, sub, run_id, version, current, staged)
            if links is None:
                return None
            summary = staged["raw"].get("summary", {}) or {}
            snapshot.update(details_available=True, max_hr=summary.get("max_hr"),
                            avg_cadence=summary.get("avg_cadence"),
                            total_ascent_m=summary.get("total_ascent_m"),
                            calories=summary.get("calories"))
        return _active_state(RUNS, prev, snapshot, links, change_reason)

    payload = _commit(bucket, RUNS, sub, run_id, build, base_version=base_version,
                      created_by=created_by, author=author, change_reason=change_reason)
    if staged:
        _drop_tmp_fit(staged)
        # Гистограммы для нагрузки (#66) — сразу, чтобы новую пробежку не
        # пришлось дочитывать. Не вышло — пробежка всё равно сохраняется, а
        # гистограммы дочитает build_training_load.
        try:
            remember_run_hist(bucket, sub, run_id, staged["placed"])
        except Exception:
            logging.exception("run_hist: гистограммы пробежки %s не записаны", run_id)
    return _index_entry(RUNS, payload)


def save_race(bucket, sub, race, base_version=None, change_reason="", created_by="api"):
    """Создание или правка старта → запись индекса. race — id и RACE_FIELDS."""
    _ensure_versioned(bucket, RACES, sub, race["id"])

    def build(prev, version, current):
        _editable(prev)
        snapshot = _snapshot(RACES, race, (prev or {}).get("race") or {})
        return _active_state(RACES, prev, snapshot, {}, change_reason)

    payload = _commit(bucket, RACES, sub, race["id"], build, base_version=base_version,
                      created_by=created_by, change_reason=change_reason)
    return _index_entry(RACES, payload)


def _set_status(bucket, kind, sub, eid, status, by, change_reason):
    """Скрытие или восстановление — новая версия с тем же снимком полей.
    None — такой сущности нет. Уже в этом состоянии — без записи, поэтому
    повторное скрытие не сдвигает deleted_at."""
    if not _ensure_versioned(bucket, kind, sub, eid):
        return None

    def build(prev, version, current):
        if prev.get("status", ACTIVE) == status:
            raise _Unchanged(prev)
        deleted = status == DELETED
        return {"status": status,
                "deleted_at": datetime.utcnow().isoformat() + "Z" if deleted else None,
                "deleted_by": by if deleted else None,
                kind.name: prev.get(kind.name) or {}, **_links(kind, prev)}

    return _index_entry(kind, _commit(bucket, kind, sub, eid, build, created_by=by,
                                      change_reason=change_reason))


def delete_run(bucket, sub, run_id, deleted_by="api"):
    return _set_status(bucket, RUNS, sub, run_id, DELETED, deleted_by, "скрытие")


def restore_run(bucket, sub, run_id, restored_by="api"):
    return _set_status(bucket, RUNS, sub, run_id, ACTIVE, restored_by, "восстановление")


def delete_race(bucket, sub, race_id, deleted_by="api"):
    return _set_status(bucket, RACES, sub, race_id, DELETED, deleted_by, "скрытие")


def restore_race(bucket, sub, race_id, restored_by="api"):
    return _set_status(bucket, RACES, sub, race_id, ACTIVE, restored_by, "восстановление")


def _history_entry(kind, number, payload, path):
    if path.endswith("/details.json"):      # версия до #75: только детали FIT
        return {"version": number, "created_at": payload.get("created_at"),
                "author": "athlete", "change_reason": "детали FIT", "status": ACTIVE,
                "fit_attached": True, kind.name: None}
    entry = {"version": number, "created_at": payload.get("created_at"),
             "author": payload.get("author") or "athlete",
             "change_reason": payload.get("change_reason") or "",
             "status": payload.get("status") or ACTIVE, kind.name: payload.get(kind.name)}
    if "details_object_path" in kind.links:
        entry["fit_attached"] = (payload.get("details_object_path") or "").endswith(
            f"/v{number}/details.json")
    return entry


def read_entity_history(bucket, kind, sub, eid, limit=ENTITY_HISTORY_LIMIT):
    """Версии сущности от новых к старым; None — такой нет. Скрытая тоже
    отдаётся: это явное историческое чтение. Почты автора в истории нет,
    как и у плана, — только author (athlete / migration)."""
    manifest = _read_json(bucket, kind.manifest(sub, eid))
    if not isinstance(manifest, dict):
        return None
    current = manifest.get("current_version") or 0
    files = {}
    for blob in bucket.list_blobs(prefix=f"{kind.folder(sub)}{eid}/v"):
        m = _ENTITY_VER_RE.search(blob.name)
        if m and int(m.group(1)) <= current:
            files.setdefault(int(m.group(1)), {})[m.group(2)] = blob.name
    # У версии payload — run.json (race.json), у версии до #75 — details.json.
    paths = {n: names.get(kind.name) or names.get("details") for n, names in files.items()}
    numbers = sorted((n for n, path in paths.items() if path), reverse=True)
    page = numbers[:limit]
    payloads = dict(zip(page, _load_json_objects(
        bucket, [paths[n] for n in page], skip_broken=True)))
    versions = [_history_entry(kind, n, payloads[n], paths[n])
                for n in version_chain(payloads, current) if isinstance(payloads[n], dict)]
    return {f"{kind.name}_id": eid, "versions": versions, "has_more": len(numbers) > len(page)}


def read_run_history(bucket, sub, run_id, limit=ENTITY_HISTORY_LIMIT):
    return read_entity_history(bucket, RUNS, sub, run_id, limit)


def read_race_history(bucket, sub, race_id, limit=ENTITY_HISTORY_LIMIT):
    return read_entity_history(bucket, RACES, sub, race_id, limit)


def _entity_id(name, folder):
    """id сущности по имени манифеста folder/{id}/manifest.json; иначе None."""
    head, _, tail = name[len(folder):].partition("/")
    return int(head) if tail == "manifest.json" and head.isdigit() else None


def rebuild_index(bucket, kind, sub):
    """Индекс заново по манифестам → {"entries", "changed"}.

    Запись индекса, у сущности которой версий ещё нет (до миграции),
    остаётся как есть. Порядок — по id от новых к старым: id — время
    создания. Индекс совпал с собранным — ничего не пишется.
    """
    folder = kind.folder(sub)
    ids = sorted({eid for blob in bucket.list_blobs(prefix=folder)
                  if (eid := _entity_id(blob.name, folder)) is not None})
    manifests = _load_json_objects(bucket, [kind.manifest(sub, eid) for eid in ids],
                                   skip_broken=True)
    current = {eid: m for eid, m in zip(ids, manifests) if _is_versioned(kind, sub, eid, m)}
    # Нечитаемая версия записи не даёт: в индексе остаётся то, что было.
    payloads = _load_json_objects(bucket, [m["gcs_object_path"] for m in current.values()],
                                  skip_broken=True)
    built = {eid: _index_entry(kind, p) for eid, p in zip(current, payloads)
             if isinstance(p, dict)}
    seen = {}

    def rebuild(entries):
        merged = {**{e.get("id"): e for e in entries if isinstance(e, dict)}, **built}
        fresh = sorted(merged.values(), reverse=True,
                       key=lambda e: e.get("id") if isinstance(e.get("id"), int) else 0)
        seen["entries"] = len(fresh)
        if fresh == entries:
            return None
        entries[:] = fresh
        return True

    changed = update_json(bucket, kind.index(sub), rebuild, list)
    return {"entries": seen.get("entries", 0), "changed": bool(changed)}


def migrate_entity_history(bucket, sub):
    """Пробежки и старты sub — в версии: базовая версия для каждой записи
    индекса без версий, затем пересборка индекса. Идемпотентна: повторный
    вызов только сверяет индекс с версиями."""
    report = {}
    for kind in (RUNS, RACES):
        entries = [e for e in _read_index(bucket, kind, sub)
                   if isinstance(e, dict) and _clean_id(e.get("id"))]
        manifests = _load_json_objects(bucket, [kind.manifest(sub, e["id"]) for e in entries],
                                       skip_broken=True)
        todo = [(e, m) for e, m in zip(entries, manifests)
                if not _is_versioned(kind, sub, e["id"], m)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            migrated = sum(pool.map(lambda em: _migrate_entity(bucket, kind, sub, *em), todo))
        report[f"{kind.name}s"] = {"migrated": migrated, **rebuild_index(bucket, kind, sub)}
    return report


def migrate_all_entity_history(bucket):
    """migrate_entity_history по всем пользователям реестра (админ, #75):
    {sub: отчёт}. Сбой у одного пользователя не останавливает остальных."""
    report = {}
    for sub in sorted(_fresh_registry(bucket).get("users", {})):
        try:
            report[sub] = migrate_entity_history(bucket, sub)
        except Exception as e:
            logging.exception("migrate-history: %s", sub)
            report[sub] = {"error": str(e)[:120]}
    return report


# ── Plan helpers (per-plan weeks; #25) ────────────────────────────────────────

def read_plan_manifest(bucket, sub, plan_id):
    blob = bucket.blob(p_plan_manifest(sub, plan_id))
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def read_plan_version(bucket, object_path):
    blob = bucket.blob(object_path)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def read_plan_state(bucket, sub, plan_id):
    """Недели текущей версии плана вместе с её номером.

    version клиент возвращает как base_version при записи (см.
    save_plan_weeks); 0 — недель у плана ещё нет или нет самого плана.
    """
    manifest = read_plan_manifest(bucket, sub, plan_id) if plan_id else None
    data = read_plan_version(bucket, manifest["gcs_object_path"]) if manifest else None
    return {"plan_id": plan_id,
            "version": manifest["current_version"] if manifest else 0,
            "weeks": (data or {}).get("weeks", [])}


def read_plan_weeks(bucket, sub, plan_id):
    """Недели текущей версии плана; [] если плана/версии нет."""
    return read_plan_state(bucket, sub, plan_id)["weeks"]


class PlanStale(VersionStale):
    """Правка сделана поверх версии плана, которая уже не текущая."""


def save_plan_weeks(bucket, sub, plan_id, weeks, change_reason="", created_by="api",
                    base_version=None, author=None):
    """Пишет следующую версию недель плана (commit_version).

    base_version — версия, поверх которой сделана правка (0 — недель ещё не
    было). Текущая уже другая — или её успели сменить параллельно → PlanStale,
    в манифест ничего не попадает. None — без сверки: так пишет фронтенд,
    загруженный до #51.

    author — {role, sub}: кто правил план (#60). created_by — почта для
    аудита, author — для истории, которую видят спортсмен и тренер.
    """
    def claim(version, current):
        object_path = p_plan_ver(sub, plan_id, version)
        now = datetime.utcnow().isoformat() + "Z"
        payload = {
            "version": version,
            "is_current": True,
            "created_at": now,
            "created_by": created_by,
            "change_reason": change_reason or "",
            "supersedes_version": current or None,
            "weeks": weeks,
        }
        if author:
            payload["author"] = author
        if not create_json(bucket, object_path, payload):
            return None
        manifest = {
            "current_version": version,
            "gcs_object_path": object_path,
            "created_at": now,
            "created_by": created_by,
            "change_reason": payload["change_reason"],
            "checksum": hashlib.sha256(_dumps(payload).encode()).hexdigest(),
        }
        if author:
            manifest["author"] = author
        return manifest, {"version": version, "gcs_object_path": object_path}

    return commit_version(bucket, p_plan_manifest(sub, plan_id), claim,
                          base_version=base_version, stale=PlanStale)


# ── История версий плана (#60) ────────────────────────────────────────────────
# План теперь правят трое: спортсмен, тренер и ИИ-тренер. История отвечает,
# кто, когда и почему, — по объектам версий, которые и так не перезаписываются.

PLAN_AUTHOR_ROLES = ("athlete", "coach", "ai")
PLAN_HISTORY_LIMIT = 50
_PLAN_VER_RE = re.compile(r"/v(\d+)/plan\.json$")


def plan_author(role, sub):
    return {"role": role, "sub": sub}


def _version_author_role(payload):
    """Роль автора версии. До #60 автора не писали — выводим из того, что было:
    перенос одиночного плана, причина правки ИИ-тренера, иначе сам спортсмен."""
    role = (payload.get("author") or {}).get("role")
    if role in PLAN_AUTHOR_ROLES:
        return role
    if payload.get("created_by") == "auto-migrate":
        return "migration"
    if str(payload.get("change_reason") or "").startswith("ИИ-тренер:"):
        return "ai"
    return "athlete"


def read_plan_history(bucket, sub, plan_id, limit=PLAN_HISTORY_LIMIT):
    """Версии недель плана от новых к старым: номер, время, причина и автор.

    Автор — роль, sub и имя из реестра. Почта в историю не попадает, как и в
    карточку тренера: историю видит и вторая сторона пары.
    """
    manifest = read_plan_manifest(bucket, sub, plan_id)
    current = manifest["current_version"] if manifest else 0
    prefix = f"{upfx(sub)}plans/{plan_id}/v"
    # Номера выше текущего — версии, которые ещё пишутся или не попали в
    # манифест (#35); ниже текущего такие отсеивает version_chain.
    numbers = sorted({n for blob in bucket.list_blobs(prefix=prefix)
                      if (m := _PLAN_VER_RE.search(blob.name)) and (n := int(m.group(1))) <= current},
                     reverse=True)
    page = numbers[:limit]
    # Нечитаемая версия выпадает из истории, а не роняет её целиком.
    payloads = dict(zip(page, _load_json_objects(
        bucket, [p_plan_ver(sub, plan_id, v) for v in page], skip_broken=True)))
    users = read_registry(bucket).get("users", {})
    versions = []
    for number in version_chain(payloads, current):
        payload = payloads[number]
        if not isinstance(payload, dict):
            continue
        role = _version_author_role(payload)
        author_sub = (payload.get("author") or {}).get("sub")
        if author_sub is None and role in ("athlete", "ai"):
            author_sub = sub              # до #60 план писал только его владелец
        versions.append({
            "version": number,
            "created_at": payload.get("created_at"),
            "change_reason": payload.get("change_reason") or "",
            "author_role": role,
            "author_sub": author_sub,
            "author_name": (users.get(author_sub) or {}).get("name") or "",
        })
    return {"plan_id": plan_id, "versions": versions, "has_more": len(numbers) > len(page)}


# ── Plans registry (#25: несколько планов на пользователя) ────────────────────

PLAN_META_FIELDS = ("race_name", "race_date", "target_time", "plan_start")
PLAN_META_LIMITS = {"race_name": TITLE_MAX, "race_date": SHORT_TEXT_MAX,
                    "target_time": SHORT_TEXT_MAX, "plan_start": SHORT_TEXT_MAX}
PLAN_WEEK_LIMITS = {"start": SHORT_TEXT_MAX, "end": SHORT_TEXT_MAX,
                    "accent": TITLE_MAX, "type": SHORT_TEXT_MAX,
                    **{day: PLAN_DAY_TEXT_MAX for day, _ in PLAN_DAYS}}


def clean_plan_meta(raw):
    """Карточка плана из тела запроса → (поля, ошибки). В результате только
    те поля, что пришли: правка карточки меняет лишь их (#53)."""
    meta, errors = {}, {}
    present = {f: limit for f, limit in PLAN_META_LIMITS.items() if f in raw}
    _clean_text_fields(raw, meta, errors, present)
    return meta, errors


def _clean_week_number(value):
    if isinstance(value, str) and value.isascii() and value.isdigit():
        value = int(value)
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if 0 <= value <= PLAN_WEEK_NUMBER_MAX else None


def clean_plan_weeks(raw):
    """Недели плана из тела запроса → (недели, ошибки) (#53).

    В неделе остаются только известные поля, остальное отбрасывается. Чего в
    запросе не было, то и не появляется: планы до #23 хранятся без вт и чт,
    и отрисовка это понимает.
    """
    if not isinstance(raw, list):
        return [], {"weeks": "ожидается список недель"}
    if len(raw) > PLAN_WEEKS_MAX:
        return [], {"weeks": f"не больше {PLAN_WEEKS_MAX} недель"}
    weeks, errors = [], {}
    for i, item in enumerate(raw):
        where = f"weeks[{i}]"
        if not isinstance(item, dict):
            errors[where] = "ожидается объект недели"
            continue
        week = {}
        if item.get("w") is not None:
            week["w"] = _clean_week_number(item["w"])
            if week["w"] is None:
                errors[f"{where}.w"] = f"ожидается целое от 0 до {PLAN_WEEK_NUMBER_MAX}"
        present = {f: limit for f, limit in PLAN_WEEK_LIMITS.items() if f in item}
        _clean_text_fields(item, week, errors, present, prefix=where + ".")
        weeks.append(week)
    return weeks, errors


def _empty_plans_index():
    return {"active_plan_id": None, "plans": []}


def _new_plan_id():
    """Уникальный id плана. Случайный суффикс обязателен: два плана, созданных
    в одну миллисекунду, иначе получили бы один id и перезаписали данные друг друга."""
    return f"{int(datetime.now().timestamp() * 1000)}-{secrets_mod.token_hex(3)}"


def _read_plans_index_raw(bucket, sub):
    blob = bucket.blob(p_plans_index(sub))
    if not blob.exists():
        return None
    data = json.loads(blob.download_as_text())
    data.setdefault("plans", [])
    data.setdefault("active_plan_id", None)
    return data


def _create_plans_index(bucket, sub, index):
    """Пишет реестр, только если его ещё нет. None — его успел создать другой
    запрос (if_generation_match=0)."""
    return index if create_json(bucket, p_plans_index(sub), index) else None


def read_plans_index(bucket, sub):
    """Реестр планов. При отсутствии — запускает ленивую миграцию (см. migrate_single_plan)."""
    index = _read_plans_index_raw(bucket, sub)
    if index is None:
        index = migrate_single_plan(bucket, sub)
    return index


def active_plans(index):
    return [p for p in index.get("plans", []) if not p.get("archived")]


def find_plan(index, plan_id):
    return next((p for p in index.get("plans", []) if p.get("id") == plan_id), None)


def get_active_plan(bucket, sub):
    index = read_plans_index(bucket, sub)
    return find_plan(index, index.get("active_plan_id"))


# Реестр планов с #60 пишут двое — спортсмен и его тренер, и «прочитал,
# поправил, записал» из двух запросов разом теряло бы одну из правок. Запись
# идёт с предусловием, как у счётчика обращений к LLM: объект с момента
# чтения меняли — читаем заново и повторяем.
_PLAN_META_EVENT_RE = re.compile(r"^\d{8}T\d{12}-[a-z]+-[0-9a-f]{8}$")


class PlansIndexBusy(StorageBusy):
    """Реестр планов не удалось обновить: его всё время меняют параллельно."""


def _update_plans_index(bucket, sub, change):
    """change(index) правит реестр на месте и возвращает результат; None —
    записывать нечего. При повторе change зовётся заново на свежем реестре."""
    path = p_plans_index(sub)
    if bucket.get_blob(path) is None:
        read_plans_index(bucket, sub)               # ленивая миграция создаст реестр

    def normalized(index):
        index.setdefault("plans", [])
        index.setdefault("active_plan_id", None)
        return change(index)

    return update_json(bucket, path, normalized, _empty_plans_index, busy=PlansIndexBusy)


def create_plan(bucket, sub, meta, make_active=True, author=None):
    """Новый план в реестре. make_active=False — активным он станет, только
    если активного плана нет (так создаёт план тренер, #60)."""
    def add(index):
        plan = {
            "id": _new_plan_id(),
            "created_at": datetime.utcnow().isoformat() + "Z",
            "archived": False,
            **{f: (meta.get(f) or "") for f in PLAN_META_FIELDS},
        }
        if author:
            plan["created_by_role"], plan["created_by_sub"] = author["role"], author["sub"]
        index["plans"].append(plan)
        if make_active or not index.get("active_plan_id"):
            index["active_plan_id"] = plan["id"]
        return plan

    return _update_plans_index(bucket, sub, add)


def set_active_plan(bucket, sub, plan_id):
    def activate(index):
        plan = find_plan(index, plan_id)
        if not plan or plan.get("archived"):
            return None
        index["active_plan_id"] = plan_id
        return plan

    return _update_plans_index(bucket, sub, activate)


def plan_meta_unchanged(plan, meta):
    """Правка карточки ничего не меняет: формы шлют все поля разом (#60)."""
    return all((plan.get(f) or "") == (meta.get(f) or "") for f in PLAN_META_FIELDS if f in meta)


def update_plan_meta(bucket, sub, plan_id, meta, author=None):
    """Правит карточку гонки плана. Реестр перезаписывается, поэтому прежние
    значения уходят в неизменяемое событие рядом с планом (#60): после правки
    тренером спортсмен может узнать, что стояло раньше. Правка без изменений
    ничего не пишет и возвращает план как есть."""
    before, found = {}, {}

    def edit(index):
        found.clear()
        plan = find_plan(index, plan_id)
        if not plan:
            return None
        found["plan"] = plan
        if plan_meta_unchanged(plan, meta):
            return None                 # писать нечего — ни реестр, ни событие
        before.clear()
        before.update({f: plan.get(f, "") for f in PLAN_META_FIELDS})
        for f in PLAN_META_FIELDS:
            if f in meta:
                plan[f] = meta.get(f) or ""
        plan["updated_at"] = datetime.utcnow().isoformat() + "Z"
        if author:
            plan["updated_by_role"], plan["updated_by_sub"] = author["role"], author["sub"]
        return plan

    plan = _update_plans_index(bucket, sub, edit)
    if plan is None:
        return found.get("plan")        # плана нет — None, без изменений — он сам
    if plan:
        # Время в id строго растёт, как у сообщений чата: порядок событий —
        # порядок правок, даже если часы инстанса грубее двух правок подряд.
        ids = _message_ids(bucket, p_plan_meta_prefix(sub, plan_id), _PLAN_META_EVENT_RE)
        event = {"plan_id": plan_id, "ts": plan["updated_at"], "author": author,
                 "before": before, "after": {f: plan.get(f, "") for f in PLAN_META_FIELDS}}

        def make():
            event_id, _ = _next_message_id(ids, (author or {}).get("role") or "owner")
            return p_plan_meta_event(sub, plan_id, event_id), event

        create_unique_json(bucket, make)
    return plan


def archive_plan(bucket, sub, plan_id):
    """Логическое удаление: archived=true. Объекты плана не удаляются."""
    def archive(index):
        plan = find_plan(index, plan_id)
        if not plan:
            return None
        plan["archived"] = True
        plan["archived_at"] = datetime.utcnow().isoformat() + "Z"
        if index.get("active_plan_id") == plan_id:
            remaining = active_plans(index)
            index["active_plan_id"] = remaining[0]["id"] if remaining else None
        return plan

    return _update_plans_index(bucket, sub, archive)


def migrate_single_plan(bucket, sub):
    """Ленивая миграция одиночного плана (до #25) в реестр планов.

    Данные гонки берём из profile.json, текущие недели — из users/{sub}/plan/.
    Старые версии остаются по legacy-пути (append-only, ничего не удаляем):
    текущие недели переписываются как v1 нового плана.
    Активным пробежкам без плана проставляется plan_id.
    """
    index = _empty_plans_index()

    old_manifest_blob = bucket.blob(p_singleplan_manifest(sub))
    profile = read_legacy_race_profile(bucket, sub)
    has_profile = any(profile.get(f) for f in PLAN_META_FIELDS)

    if not old_manifest_blob.exists() and not has_profile:
        # Нечего мигрировать — пустой реестр
        return _create_plans_index(bucket, sub, index) or _read_plans_index_raw(bucket, sub)

    plan = {
        "id": _new_plan_id(),
        "created_at": datetime.utcnow().isoformat() + "Z",
        "archived": False,
        **{f: profile.get(f, "") for f in PLAN_META_FIELDS},
    }
    index["plans"].append(plan)
    index["active_plan_id"] = plan["id"]
    if _create_plans_index(bucket, sub, index) is None:
        # Параллельный запрос (тренер и спортсмен разом, #60) уже перенёс план:
        # второй перенос с другим id затёр бы первый и перепривязал пробежки.
        return _read_plans_index_raw(bucket, sub)

    # Текущие недели старого плана → v1 нового
    if old_manifest_blob.exists():
        old_manifest = json.loads(old_manifest_blob.download_as_text())
        old_data = read_plan_version(bucket, old_manifest.get("gcs_object_path", ""))
        weeks = (old_data or {}).get("weeks", [])
        if weeks:
            save_plan_weeks(bucket, sub, plan["id"], weeks,
                            "migrated from single plan (#25)", "auto-migrate", base_version=0)

    # Привязываем существующие пробежки к мигрированному плану — новой
    # версией каждой (#75): правку индекса стёрла бы его пересборка. Скрытые
    # не правятся, их и не показывают.
    for run in read_runs(bucket, sub):
        if isinstance(run, dict) and _clean_id(run.get("id")) and \
                not run.get("plan_id") and not run.get("deleted"):
            save_run(bucket, sub, {**run, "plan_id": plan["id"]},
                     change_reason="привязка к плану (#25)",
                     created_by=MIGRATION_AUTHOR, author="migration")
    return index


# ── LLM config helpers ────────────────────────────────────────────────────────

def mask_key(api_key):
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "***"
    return f"{api_key[:6]}***{api_key[-4:]}"


def read_llm_manifest(bucket):
    blob = bucket.blob(LLM_CONFIG_MANIFEST)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def read_llm_config_full(bucket):
    """Возвращает полный конфиг (с реальным ключом). Только для внутреннего использования."""
    manifest = read_llm_manifest(bucket)
    if not manifest:
        return None
    blob = bucket.blob(manifest["gcs_object_path"])
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def write_llm_config_version(bucket, provider, model, api_key, effort=None, created_by="aabramov77"):
    """Новая версия конфига LLM (commit_version)."""
    def claim(version, current):
        object_path = f"config/llm/v{version}/config.json"
        now = datetime.utcnow().isoformat() + "Z"
        payload = {
            "version": version,
            "is_current": True,
            "created_at": now,
            "created_by": created_by,
            "provider": provider,
            "model": model,
            "api_key": api_key,
            "effort": clean_effort(effort),
            "supersedes_version": current or None,
        }
        if not create_json(bucket, object_path, payload):
            return None
        manifest = {
            "current_version": version,
            "gcs_object_path": object_path,
            "updated_at": now,
            "provider": provider,
            "model": model,
        }
        return manifest, {"version": version, "provider": provider, "model": model,
                          "effort": payload["effort"]}

    return commit_version(bucket, LLM_CONFIG_MANIFEST, claim)


# ── LLM clients (Anthropic / OpenAI / Deepseek) ──────────────────────────────

def _turns(history, user_prompt):
    """Реплики диалога для провайдера: прошлые ходы (#46) и новый вопрос."""
    return [*(history or []), {"role": "user", "content": user_prompt}]


def _call_anthropic(model, api_key, system_prompt, user_prompt, max_tokens=1500,
                    history=None):
    res = httpx.post(
        "https://api.anthropic.com/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "system": system_prompt,
            "messages": _turns(history, user_prompt),
        },
        timeout=60.0,
    )
    res.raise_for_status()
    data = res.json()
    text = data["content"][0]["text"]
    return {
        "text": text,
        "input_tokens": data.get("usage", {}).get("input_tokens", 0),
        "output_tokens": data.get("usage", {}).get("output_tokens", 0),
    }


class LLMRefused(Exception):
    """Провайдер не стал отвечать: сработал фильтр или модель отклонила запрос.

    Приходит как HTTP 200, поэтому raise_for_status молчит: в ответе либо
    заполнено message.refusal при content = null, либо finish_reason =
    content_filter. Без явной обработки это доезжало до парсера JSON и
    превращалось в невнятную пятисотку. #38
    """


class LLMTruncated(Exception):
    """Ответ упёрся в бюджет вывода (finish_reason = length) и оборван."""


# Имя параметра лимита вывода. У моделей с рассуждением OpenAI старый
# max_tokens отклоняет, а новый покрывает и рассуждение, и видимый ответ.
# Провайдер, знающий только старое имя, ответит 400 с упоминанием нового —
# тогда повторяем запрос со старым, вместо того чтобы гадать по документации.
BUDGET_PARAM = "max_completion_tokens"
LEGACY_BUDGET_PARAM = "max_tokens"


def _post_chat(base_url, api_key, payload):
    return httpx.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=60.0,
    )


def _call_openai_compatible(base_url, model, api_key, system_prompt, user_prompt,
                            max_tokens=None, effort=None, history=None):
    """Универсальный клиент для OpenAI и Deepseek (одинаковый протокол)."""
    budget = max_tokens or LLM_MAX_TOKENS
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            *_turns(history, user_prompt),
        ],
        "response_format": {"type": "json_object"},
        "reasoning_effort": clean_effort(effort),
    }
    res = _post_chat(base_url, api_key, {**body, BUDGET_PARAM: budget})
    if res.status_code == 400 and BUDGET_PARAM in res.text:
        res = _post_chat(base_url, api_key, {**body, LEGACY_BUDGET_PARAM: budget})
    res.raise_for_status()
    data = res.json()

    choice = (data.get("choices") or [{}])[0]
    message = choice.get("message") or {}
    finish = choice.get("finish_reason")

    if message.get("refusal"):
        raise LLMRefused(message["refusal"])
    if finish == "content_filter":
        raise LLMRefused("сработал фильтр безопасности провайдера")
    if finish == "length":
        raise LLMTruncated("ответ не поместился в бюджет вывода")

    text = message.get("content")
    if not text:
        raise ValueError("провайдер вернул пустой ответ без объяснения")

    usage = data.get("usage", {})
    return {
        "text": text,
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }


def clean_effort(effort):
    """Неизвестный уровень молча заменяем дефолтом: чужая строка в теле
    запроса даёт 400 от провайдера, а конфиг мог быть записан до #38."""
    return effort if effort in LLM_EFFORT_LEVELS else LLM_DEFAULT_EFFORT


def call_llm(provider, model, api_key, system_prompt, user_prompt, effort=None,
             history=None):
    """history — прошлые реплики диалога [{role: user|assistant, content}] (#46)."""
    if provider == "anthropic":
        # Провайдер вне интерфейса (#38): ключа нет. Уровень рассуждения у
        # Anthropic задаётся не reasoning_effort, а output_config.effort —
        # прокинуть его сюда придётся вместе с возвратом провайдера.
        return _call_anthropic(model, api_key, system_prompt, user_prompt,
                               history=history)
    if provider == "openai":
        return _call_openai_compatible("https://api.openai.com/v1", model, api_key,
                                       system_prompt, user_prompt, effort=effort,
                                       history=history)
    if provider == "deepseek":
        return _call_openai_compatible("https://api.deepseek.com/v1", model, api_key,
                                       system_prompt, user_prompt, effort=effort,
                                       history=history)
    raise ValueError(f"Unknown provider: {provider}")


def parse_llm_json(text):
    """Извлекает первый JSON-объект из ответа LLM."""
    # Сначала пытаемся распарсить весь текст
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    # Иначе — между первой { и последней }
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        raise ValueError("No JSON object in LLM response")
    return json.loads(m.group(0))


# ── Advice context + storage ─────────────────────────────────────────────────


def current_plan_week_idx(plan_start=None, weeks_count=0, weeks=None):
    """0-based индекс текущей недели плана.

    Тонкая обёртка над `compliance.current_week_idx` (#41): раньше здесь была
    своя арифметика, отсчитывавшая семидневки от plan_start, из-за чего
    подсвечивалась соседняя неделя (#40). Недели передаём, чтобы отсчёт шёл
    от подписи первой строки, а не от разошедшегося с ней plan_start.
    """
    return current_week_idx(plan_start, weeks_count, weeks=weeks)


def build_plan_compliance(bucket, sub, plan_id):
    """План против факта для одного плана; None — плана нет (#41).

    Читающая операция: всё производное и считается на лету, в GCS ничего
    не пишется, версий не создаётся.
    """
    plan = find_plan(read_plans_index(bucket, sub), plan_id)
    if not plan:
        return None

    weeks = read_plan_weeks(bucket, sub, plan_id)
    plan_start = plan.get("plan_start")
    result = plan_compliance(weeks, read_runs(bucket, sub), plan_start, plan_id)
    result["plan_id"] = plan_id
    result["plan_start"] = plan_start or ""
    result["current_week"] = current_week_idx(plan_start, len(weeks), weeks=weeks)
    return result


def build_plan_weather(bucket, sub, plan_id):
    """Погода и советы к дням плана (#72); None — плана нет.

    Читающая операция, как и план/факт: в GCS ничего не пишется, прогноз
    живёт только в памяти инстанса.
    """
    plan = find_plan(read_plans_index(bucket, sub), plan_id)
    if not plan:
        return None
    profile = read_athlete_profile(bucket, sub)[0]
    report = weather_report(profile, read_plan_weeks(bucket, sub, plan_id),
                            plan.get("plan_start"))
    report["plan_id"] = plan_id
    return report


def compute_hr_drift(details):
    """Возвращает рост среднего пульса от первой половины тренировки ко второй, в %.
    + значит пульс рос (норма для длительной, риск при коротких).
    Использует samples; fallback — laps. None если данных мало.
    """
    samples = details.get("samples", {}) or {}
    hrs = [h for h in (samples.get("hr") or []) if h]
    if len(hrs) >= 4:
        mid = len(hrs) // 2
        avg1 = sum(hrs[:mid]) / mid
        avg2 = sum(hrs[mid:]) / (len(hrs) - mid)
        if avg1 > 0:
            return round((avg2 - avg1) / avg1 * 100, 1)
    # Fallback — лапы
    laps = details.get("laps", []) or []
    hr_laps = [l.get("avg_hr") for l in laps if l.get("avg_hr")]
    if len(hr_laps) >= 4:
        mid = len(hr_laps) // 2
        avg1 = sum(hr_laps[:mid]) / mid
        avg2 = sum(hr_laps[mid:]) / (len(hr_laps) - mid)
        if avg1 > 0:
            return round((avg2 - avg1) / avg1 * 100, 1)
    return None


def lap_paces_str(details, limit=15):
    """Возвращает строку темпов по лапам через запятую (ограничиваем количество)."""
    laps = details.get("laps", []) or []
    paces = [l.get("pace") for l in laps if l.get("pace")]
    if not paces:
        return None
    if len(paces) > limit:
        return ", ".join(paces[:limit]) + f" … (+{len(paces) - limit})"
    return ", ".join(paces)


def half_paces(details):
    """(темп первой половины, темп второй) в сек/км по кругам.

    None, если кругов меньше четырёх: на двух-трёх кругах «половина» — это
    разминка против заминки, а не раскладка сил.
    """
    laps = [l for l in (details.get("laps") or [])
            if l.get("dist_km") and l.get("duration_sec")]
    if len(laps) < 4:
        return None
    mid = len(laps) // 2

    def pace(part):
        return int(sum(l["duration_sec"] for l in part) / sum(l["dist_km"] for l in part))

    return pace(laps[:mid]), pace(laps[mid:])


def hr_zone_minutes(details, zones):
    """Время в пульсовых зонах по сэмплам: [{name, min, pct}]. [] — считать не из чего.

    zones — как в compute_athlete_derived: [{name, from, to}] по возрастанию.
    Пульс ниже первой зоны идёт отдельной строкой, а не теряется: иначе
    проценты остальных зон завышены. Гистограмма та же, по которой считается
    нагрузка (#66), поэтому время в зонах у разбора и у нагрузки не расходится.
    """
    if not zones:
        return []
    seconds = zone_seconds(hr_histogram(details), zones)     # [0] — ниже первой зоны
    total = sum(seconds)
    if total <= 0:
        return []
    names = ["ниже " + zones[0]["name"].split()[0]] + [zone["name"] for zone in zones]
    return [{"name": name, "min": round(sec / 60, 1), "pct": round(sec / total * 100)}
            for name, sec in zip(names, seconds) if sec > 0]


# ── Нагрузка (#66) ────────────────────────────────────────────────────────────
#
# Нагрузка считается по сэмплам FIT, а они лежат по объекту на пробежку —
# читать их на каждый запрос нельзя. Поэтому из них один раз строятся
# гистограммы (load.run_hist_entry) и копятся в derived/run_hist.json.
# Гистограммы не зависят от профиля, так что кэш не устаревает при правке зон,
# пульса покоя или порога. Это производное от неизменяемого FIT, а не
# бизнес-запись: объект восстанавливается целиком и поэтому перезаписывается,
# с предусловием по поколению, как счётчик обращений к ИИ.

RUN_HIST_FILL_LIMIT = 60        # столько details.json дочитывается за вызов
RUN_HIST_WRITE_ATTEMPTS = 3


def _load_run_hist(bucket, sub):
    """(записи кэша как в JSON, поколение объекта). Нет, битый или другой
    версии — пустой: пересоберётся. 0 — объекта нет."""
    blob = bucket.get_blob(p_run_hist(sub))
    if blob is None:
        return {}, 0
    try:
        data = json.loads(blob.download_as_text())
    except (ValueError, NotFound):
        return {}, blob.generation
    runs = data.get("runs") if isinstance(data, dict) and data.get("v") == HIST_VERSION else None
    return (dict(runs) if isinstance(runs, dict) else {}), blob.generation


def _write_run_hist(bucket, sub, entries, generation):
    """Запись с предусловием. False — объект успели изменить: не ошибка,
    недостающее дочитается в следующий раз."""
    try:
        bucket.blob(p_run_hist(sub)).upload_from_string(
            json.dumps({"v": HIST_VERSION, "runs": entries},
                       ensure_ascii=False, separators=(",", ":")),
            content_type="application/json", if_generation_match=generation)
        return True
    except (PreconditionFailed, NotFound):
        return False


def remember_run_hist(bucket, sub, run_id, details):
    """Гистограммы пробежки — в кэш (при прикреплении FIT)."""
    entry = run_hist_entry(details)
    for _ in range(RUN_HIST_WRITE_ATTEMPTS):
        entries, generation = _load_run_hist(bucket, sub)
        entries[str(run_id)] = entry
        if _write_run_hist(bucket, sub, entries, generation):
            return True
    return False


def build_training_load(bucket, sub, runs=None, profile=None, races=None,
                        fill_limit=RUN_HIST_FILL_LIMIT, today=None):
    """Нагрузка по двум осям: недели, 7 дней, цена работы, нагрузка пробежек.

    Читающая операция, кроме кэша гистограмм: его она дозаполняет не больше
    чем fill_limit пробежками за вызов, остаток — в pending. fill_limit=0 —
    только чтение: так считает тренер, в данные спортсмена он не пишет.
    Детали читаются только у пробежек из runs.json самого sub и только через
    read_run_details, поэтому правило #53 о legacy-деталях здесь не обходится.
    runs, profile и races можно передать уже прочитанными.
    """
    today = today or datetime.utcnow().date()
    runs = read_runs(bucket, sub) if runs is None else runs
    runs = [r for r in runs if isinstance(r, dict) and not r.get("deleted", False)]
    profile = read_athlete_profile(bucket, sub)[0] if profile is None else profile
    races = read_races(bucket, sub) if races is None else races

    entries, generation = _load_run_hist(bucket, sub)
    missing = [r for r in runs if r.get("details_available") and str(r.get("id")) not in entries]
    added = False
    for run in missing[:max(0, fill_limit)]:
        try:
            details = read_run_details(bucket, sub, run["id"])
        except ValueError:
            details = None          # битый JSON сам не починится — перечитывать незачем
        except Exception:
            # Сбой сети или хранилища — пробежка остаётся в pending до следующего раза.
            logging.exception("run_hist: детали пробежки %s не прочитаны", run.get("id"))
            continue
        entries[str(run["id"])] = run_hist_entry(details)    # нет деталей — пустые гистограммы
        added = True
    if added:
        _write_run_hist(bucket, sub, entries, generation)
    pending = sum(1 for r in runs if r.get("details_available") and str(r.get("id")) not in entries)

    ctx = axes(profile, compute_athlete_derived(profile), races, today)
    result = weekly_load(runs, {rid: clean_hist_entry(e) for rid, e in entries.items()},
                         ctx, today)
    threshold = ctx["threshold_pace_sec"]
    result["internal"] = {"method": ctx["internal"], "zones_source": ctx["zones_source"]}
    result["external"] = {"method": "rtss" if threshold else None,
                          "threshold_pace": format_duration(threshold),
                          "threshold_source": ctx["threshold_source"],
                          "threshold_basis": ctx["threshold_basis"]}
    result["pending"] = pending
    return result


def _pick(source, keys):
    return {key: source.get(key) for key in keys} if isinstance(source, dict) else None


_LOAD_AXIS_KEYS = ("load", "unknown", "chronic", "ratio", "band")
_LOAD_COST_KEYS = ("dev_pct", "band")
_LOAD_SPLIT_KEYS = ("easy", "moderate", "hard")
_LOAD_RUN_KEYS = ("int", "int_est", "ext", "ext_est")


def coach_load_view(load):
    """Нагрузка спортсмена для тренера — собрана заново по белому списку.

    Порога, его основания (забег из «Стартов») и источника зон здесь нет по
    построению: поле, добавленное в нагрузку позже, само тренеру не уйдёт.
    Названия методов остаются — без них непонятна шкала.
    """
    def period(row, extra):
        return {**_pick(row, extra), **_pick(row, _LOAD_SPLIT_KEYS),
                "int": _pick(row.get("int"), _LOAD_AXIS_KEYS),
                "ext": _pick(row.get("ext"), _LOAD_AXIS_KEYS),
                "cost": _pick(row.get("cost"), _LOAD_COST_KEYS)}

    return {
        "internal": {"method": (load.get("internal") or {}).get("method")},
        "external": {"method": (load.get("external") or {}).get("method")},
        "pending": load.get("pending", 0),
        "today": load.get("today"),
        "weeks": [period(w, ("start", "end", "partial", "runs")) for w in load.get("weeks", [])],
        "rolling": period(load.get("rolling") or {}, ("start", "end", "runs")),
        "runs": {str(rid): _pick(r, _LOAD_RUN_KEYS) for rid, r in (load.get("runs") or {}).items()},
    }


_WEATHER_DAY_KEYS = ("date", "field", "week_idx", "workout", "kind", "weather", "level",
                     "why", "flags", "venue", "clothing", "notes")


def coach_weather_view(report):
    """Погода к плану спортсмена для тренера (#72) — собрана заново по белому
    списку. Координат места здесь нет по построению: тренеру хватает города."""
    location = report.get("location")
    return {
        "plan_id": report.get("plan_id"),
        "status": report.get("status"),
        "source": report.get("source"),
        "run_time": report.get("run_time"),
        "today": report.get("today"),
        "location": {"name": location.get("name", "")} if isinstance(location, dict) else None,
        "days": [_pick(d, _WEATHER_DAY_KEYS) for d in report.get("days") or []],
    }


LLM_RACES_DAYS = 730           # забеги в контексте — за два года (#81)
LLM_RACES_MAX = 10


def build_llm_context(bucket, sub):
    """Собирает компактный богатый контекст для LLM: активный план и его пробежки."""
    today = datetime.utcnow().date()
    plans_index = read_plans_index(bucket, sub)
    active_plan = find_plan(plans_index, plans_index.get("active_plan_id"))
    plan_id = active_plan["id"] if active_plan else None

    # Runs активного плана (#25)
    all_runs = read_runs(bucket, sub)
    active_runs = [r for r in all_runs if not r.get("deleted", False)]
    if plan_id:
        active_runs = [r for r in active_runs if r.get("plan_id") == plan_id]
    active_runs.sort(key=lambda r: r.get("date", ""), reverse=True)
    last_runs = active_runs[:14]

    # Для пробежек с FIT-данными подгружаем детали (лапы + HR-drift)
    for r in last_runs:
        if r.get("details_available"):
            try:
                details = read_run_details(bucket, sub, r["id"])
                if details:
                    r["_lap_paces"] = lap_paces_str(details)
                    r["_hr_drift_pct"] = compute_hr_drift(details)
            except Exception:
                pass

    # Профиль спортсмена (#32)
    profile, profile_version, _ = read_athlete_profile(bucket, sub)

    # Races
    all_races = read_races(bucket, sub)
    active_races = [r for r in all_races if not r.get("deleted", False)]
    active_races.sort(key=lambda r: str(r.get("date", "")), reverse=True)
    since = (today - timedelta(days=LLM_RACES_DAYS)).isoformat()
    last_races = [r for r in active_races if str(r.get("date", "")) >= since][:LLM_RACES_MAX]

    # Нагрузка (#66) — по всем пробежкам, а не по плану: устаёт организм, а не
    # план. Сбой расчёта не роняет ход ИИ: модель получит «нагрузка недоступна».
    try:
        training_load = build_training_load(bucket, sub, runs=all_runs,
                                            profile=profile, races=all_races)
    except Exception:
        logging.exception("load: нагрузка для контекста ИИ не посчитана")
        training_load = None

    # История (#81): месяцы за год по всем пробежкам и итоги прошлых планов.
    # Недели читаются только у прошлых планов, попавших в сводку. Сбой, как и
    # у нагрузки, ход не роняет.
    try:
        candidates = past_plan_candidates(plans_index, today)
        history = {
            "months": monthly_summary(all_runs, (training_load or {}).get("runs"), today),
            "plans": past_plans(candidates,
                                {p["id"]: read_plan_weeks(bucket, sub, p["id"]) for p in candidates},
                                all_runs, all_races),
        }
    except Exception:
        logging.exception("history: история для контекста ИИ не посчитана")
        history = None

    # Plan (недели активного плана)
    plan = None
    plan_version = None
    if plan_id:
        plan_manifest = read_plan_manifest(bucket, sub, plan_id)
        if plan_manifest:
            plan_data = read_plan_version(bucket, plan_manifest["gcs_object_path"])
            if plan_data:
                plan = plan_data["weeks"]
                plan_version = plan_data["version"]

    # Погода (#72) к дням активного плана. Сбой провайдера не роняет ход ИИ:
    # блока прогноза в промпте просто не будет.
    weather = None
    if profile.get("location"):
        try:
            weather = weather_report(profile, plan or [], (active_plan or {}).get("plan_start"))
        except Exception:
            logging.exception("weather: прогноз для контекста ИИ не собран")

    week_idx = current_plan_week_idx(active_plan.get("plan_start") if active_plan else None,
                                     len(plan) if plan else 0, weeks=plan)
    current_week = plan[week_idx] if plan and 0 <= week_idx < len(plan) else None
    next_week = plan[week_idx + 1] if plan and (week_idx + 1) < len(plan) else None

    # Простые эвристики
    paces = []
    hard_count = 0
    total_km = 0.0
    for r in last_runs:
        if r.get("pace"):
            m = re.match(r"(\d+):(\d+)", r["pace"])
            if m:
                paces.append(int(m.group(1)) + int(m.group(2)) / 60)
        if r.get("feel") in ("hard", "bad"):
            hard_count += 1
        total_km += float(r.get("dist", 0) or 0)
    avg_pace = (sum(paces) / len(paces)) if paces else None

    # Выполнение плана (#41). Считаем по всем пробежкам плана, а не по
    # последним 14: недельные итоги должны быть полными. В промпт уходят
    # последние 4 недели — на большем горизонте это уже история, а не то,
    # от чего отталкиваются на ближайшей неделе.
    compliance = None
    if plan:
        full = plan_compliance(plan, all_runs,
                               (active_plan or {}).get("plan_start"), plan_id)
        if full["dated"]:
            first = max(0, week_idx - 3)
            compliance = {
                "weeks": [dict(w, idx=i)
                          for i, w in enumerate(full["weeks"])][first:week_idx + 1],
                "totals": full["totals"],
            }

    return {
        "compliance": compliance,
        "profile": profile,
        "profile_derived": athlete_derived(profile, all_races),
        "profile_version": profile_version,
        "load": training_load,
        "history": history,
        "weather": weather,
        "personal_bests": personal_bests(all_races),
        "last_runs": last_runs,
        "last_races": last_races,
        "current_week": current_week,
        "next_week": next_week,
        "week_idx": week_idx,
        "weeks_total": len(plan) if plan else 0,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "plan_weeks": plan or [],
        "plan_start": (active_plan or {}).get("plan_start"),
        "race": {f: (active_plan or {}).get(f, "") for f in PLAN_META_FIELDS},
        "heuristics": {
            "avg_pace_min_per_km": avg_pace,
            "hard_or_bad_count": hard_count,
            "total_km_last_14": round(total_km, 1),
        },
    }


def read_advice_usage(bucket, sub):
    """Дневной счётчик обращений пользователя к LLM. Сбрасывается при смене даты.

    Имя и путь остались от разовых рекомендаций (/advise), на смену
    которым пришёл диалог с ИИ-тренером (#46): счётчик тот же, и
    переносить его незачем.
    """
    today = datetime.utcnow().date().isoformat()
    blob = bucket.blob(p_advice_usage(sub))
    if not blob.exists():
        return {"date": today, "count": 0}
    data = json.loads(blob.download_as_text())
    if data.get("date") != today:
        return {"date": today, "count": 0}
    return data


# Счётчик — один объект на пользователя, и «прочитал, прибавил, записал» из
# двух запросов разом теряет одно из прибавлений. Поэтому запись идёт с
# предусловием: она проходит, только если объект с момента чтения не меняли
# (if_generation_match). Не прошла — читаем заново и повторяем (#53; та же
# основа у всех записей, #35 — update_json).


class UsageBusy(StorageBusy):
    """Счётчик не удалось обновить: его всё время меняют параллельные запросы."""


def _update_advice_usage(bucket, sub, change):
    """Меняет сегодняшний счётчик атомарно. change(usage) правит его на месте
    и возвращает False, если записывать нечего. Возвращает счётчик или None."""
    today = datetime.utcnow().date().isoformat()

    def apply(usage):
        if usage.get("date") != today:
            usage.clear()
            usage.update(date=today, count=0)
        return None if change(usage) is False else usage

    return update_json(bucket, p_advice_usage(sub), apply, dict, busy=UsageBusy)


def reserve_advice_usage(bucket, sub, limit):
    """Занимает одну попытку за сегодня до обращения к модели.

    None — лимит исчерпан. Проверка и прибавление — одна атомарная запись,
    поэтому одновременные запросы не проходят лимит все разом. UsageBusy —
    счётчик не поддался: попытка не занята, модель звать нельзя.
    """
    def take(usage):
        if usage.get("count", 0) >= limit:
            return False
        usage["count"] = usage.get("count", 0) + 1

    return _update_advice_usage(bucket, sub, take)


def release_advice_usage(bucket, sub):
    """Возвращает занятую попытку: ответа от модели не получили.

    Сбой здесь не поднимается: попытка просто остаётся потраченной — это
    хуже для пользователя, но не для лимита.
    """
    def give_back(usage):
        if usage.get("count", 0) <= 0:
            return False                 # сутки сменились — возвращать нечего
        usage["count"] -= 1

    try:
        _update_advice_usage(bucket, sub, give_back)
    except UsageBusy:
        pass


# ── User registry (мульти-пользователь) ──────────────────────────────────────

_registry_cache = {"data": None, "ts": 0.0}


def _load_registry(bucket):
    blob = bucket.blob(USERS_REGISTRY)
    if not blob.exists():
        return {"users": {}}
    data = json.loads(blob.download_as_text())
    data.setdefault("users", {})
    return data


def read_registry(bucket):
    """Реестр с in-memory кэшем (TTL). Cloud Run держит инстанс тёплым."""
    now = time.time()
    if _registry_cache["data"] is not None and now - _registry_cache["ts"] < REGISTRY_TTL_SEC:
        return _registry_cache["data"]
    data = _load_registry(bucket)
    _registry_cache["data"] = data
    _registry_cache["ts"] = now
    return data


def update_registry(bucket, change):
    """Меняет реестр атомарно (update_json) и обновляет кэш инстанса тем, что
    теперь лежит в хранилище. change(registry) — как в update_json."""
    seen = {}

    def apply(registry):
        registry.setdefault("users", {})
        seen["data"] = registry
        return change(registry)

    result = update_json(bucket, USERS_REGISTRY, apply, lambda: {"users": {}})
    _registry_cache["data"] = seen["data"]
    _registry_cache["ts"] = time.time()
    return result


def _fresh_registry(bucket):
    """Реестр мимо кэша: кэш живёт REGISTRY_TTL_SEC на каждом инстансе, а
    права доступа должны действовать сразу."""
    data = _load_registry(bucket)
    _registry_cache["data"] = data
    _registry_cache["ts"] = time.time()
    return data


def append_user_event(bucket, sub, event, actor, details=None):
    """Append-only аудит переходов (register/approve/reject, тренер #44)."""
    payload = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "sub": sub, "event": event, "actor": actor,
    }
    if details:
        payload["details"] = details

    def make():
        # Случайный хвост — как у сообщений чата: время одно на несколько
        # событий подряд (грубые часы), а затирать событие нельзя (#35).
        ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
        return f"users/events/{ts}-{sub}-{event}-{secrets_mod.token_hex(4)}.json", payload

    create_unique_json(bucket, make)


def _is_admin(bucket, sub):
    """Админ ли пользователь по реестру. Роль выдаётся один раз при
    регистрации и не меняется, поэтому кэша реестра здесь достаточно."""
    return (read_registry(bucket).get("users", {}).get(sub) or {}).get("role") == "admin"


class RegistrationClosed(Exception):
    """Поднимается, когда лимит pending достигнут — новых не регистрируем."""


def resolve_user(bucket, token_info):
    """Находит/создаёт запись пользователя по Google sub. Возвращает запись реестра.
    Новый sub → pending (или approved+admin, если подтверждённый email в ADMIN_EMAILS).
    Поднимает RegistrationClosed при переполнении pending.
    """
    sub = token_info.get("sub")
    email = token_info.get("email", "")
    name = token_info.get("name", "")
    registry = read_registry(bucket)
    users = registry.setdefault("users", {})

    if sub in users:
        return users[sub]

    # Совпадения адреса мало: админом делает только адрес, который Google
    # подтвердил (#53). Иначе это обычная заявка, как у всех.
    verified = token_info.get("email_verified")
    is_admin = email in ADMIN_EMAILS and (verified is True or verified == "true")
    existing = {}

    def register(registry):
        # Записи в кэше нет — смотрим в свежий реестр: регистрацию мог успеть
        # параллельный запрос того же пользователя.
        users = registry["users"]
        if sub in users:
            existing["rec"] = users[sub]
            return None
        if not is_admin:
            pending = sum(1 for u in users.values() if u.get("status") == "pending")
            if pending >= MAX_PENDING:
                raise RegistrationClosed()
        now = datetime.utcnow().isoformat() + "Z"
        users[sub] = {
            "sub": sub, "email": email, "name": name,
            "status": "approved" if is_admin else "pending",
            "role": "admin" if is_admin else "user",
            "created_at": now, "updated_at": now,
            "approved_by": sub if is_admin else None,
        }
        return users[sub]

    rec = update_registry(bucket, register)
    if rec is None:
        return existing["rec"]
    append_user_event(bucket, sub, "register", sub)
    return rec


def set_user_status(bucket, target_sub, status, actor_sub):
    """Меняет статус пользователя (lifecycle-метаданные). Возвращает запись или None."""
    def change(registry):
        rec = registry["users"].get(target_sub)
        if rec is None:
            return None
        rec["status"] = status
        rec["updated_at"] = datetime.utcnow().isoformat() + "Z"
        if status == "approved":
            rec["approved_by"] = actor_sub
        return rec

    rec = update_registry(bucket, change)
    if rec is not None:
        append_user_event(bucket, target_sub, status, actor_sub)
    return rec


# ── Тренер (#44): роль и связь «спортсмен → тренер» ──────────────────────────
# Обе отметки живут в реестре, а не в профиле: реестр и так читается на каждый
# запрос, а список спортсменов тренера получается фильтром, без обхода чужих
# профилей. Это авторизационные метаданные; история — в users/events/.

class CoachLinkError(ValueError):
    """Этого тренера выбрать нельзя. Код причины — str(исключения)."""


def _is_active_coach(rec):
    return bool(rec and rec.get("is_coach") and rec.get("status") == "approved")


def set_coach_flag(bucket, target_sub, is_coach, actor_sub):
    """Админ назначает или снимает тренера. Возвращает запись или None.

    Снятие заодно отвязывает его спортсменов: иначе у них в профиле остался бы
    тренер, которого уже нет, а при повторном назначении доступ вернулся бы
    без их ведома.
    """
    released = []

    def change(registry):
        released.clear()            # при повторе собирается заново
        users = registry["users"]
        rec = users.get(target_sub)
        if rec is None:
            return None
        now = datetime.utcnow().isoformat() + "Z"
        rec["is_coach"] = bool(is_coach)
        rec["updated_at"] = now
        if not is_coach:
            for user in users.values():
                if user.get("coach_sub") == target_sub:
                    user["coach_sub"] = None
                    user["updated_at"] = now
                    released.append(user["sub"])
        return rec

    rec = update_registry(bucket, change)
    if rec is None:
        return None
    append_user_event(bucket, target_sub,
                      "coach_grant" if is_coach else "coach_revoke", actor_sub)
    for sub in released:
        append_user_event(bucket, sub, "coach_clear", actor_sub,
                          {"previous": target_sub, "reason": "coach_revoked"})
    return rec


def set_user_coach(bucket, sub, coach_sub):
    """Спортсмен выбирает тренера или отказывается от него (coach_sub=None).

    Доступ к данным даёт именно эта запись, поэтому ставит её только сам
    владелец данных. Поднимает CoachLinkError, если выбрать нельзя.
    """
    coach_sub = coach_sub or None
    seen = {}

    def change(registry):
        users = registry["users"]
        rec = users.get(sub)
        if rec is None:
            raise CoachLinkError("user_not_found")
        if coach_sub:
            if coach_sub == sub:
                raise CoachLinkError("cannot_coach_yourself")
            if not _is_active_coach(users.get(coach_sub)):
                raise CoachLinkError("not_a_coach")
        seen.update(rec=rec, previous=rec.get("coach_sub"))
        if seen["previous"] == coach_sub:
            return None
        rec["coach_sub"] = coach_sub
        # Разрешение править планы давалось прежнему тренеру. Новому действует
        # значение по умолчанию (#60).
        rec.pop("coach_plan_edit", None)
        rec["updated_at"] = datetime.utcnow().isoformat() + "Z"
        return rec

    if update_registry(bucket, change) is not None:
        append_user_event(bucket, sub, "coach_select" if coach_sub else "coach_clear", sub,
                          {"coach_sub": coach_sub, "previous": seen["previous"]})
    return seen["rec"]


def _coach_card(rec):
    """Что о тренере видят остальные: имя, но не почта."""
    return {"sub": rec["sub"], "name": rec.get("name") or "Тренер"}


def list_coaches(bucket):
    """Кого сейчас можно выбрать тренером."""
    users = read_registry(bucket).get("users", {})
    cards = [_coach_card(u) for u in users.values() if _is_active_coach(u)]
    return sorted(cards, key=lambda card: card["name"].lower())


def current_coach(bucket, sub):
    """Действующий тренер пользователя или None.

    Связь на тренера, которого с тех пор отклонили, считается недействующей —
    запись в реестре остаётся, но доступа она не даёт.
    """
    users = read_registry(bucket).get("users", {})
    coach = users.get((users.get(sub) or {}).get("coach_sub"))
    return _coach_card(coach) if _is_active_coach(coach) else None


def _is_coached_by(athlete, coach_sub):
    return bool(athlete and athlete.get("status") == "approved"
                and athlete.get("coach_sub") == coach_sub)


def coach_plan_edit_allowed(rec):
    """Разрешил ли спортсмен своему тренеру править планы (#60). Поля нет —
    разрешил: по умолчанию переключатель включён."""
    return (rec or {}).get("coach_plan_edit") is not False


def coach_access(bucket, coach_sub, athlete_sub):
    """Единственная проверка права тренера на данные спортсмена:
    None — права нет, "view" — только просмотр, "edit" — и правка планов.

    Реестр читается мимо кэша: спортсмен, снявший тренера или выключивший
    правку, закрывает доступ сразу, а не через REGISTRY_TTL_SEC на соседнем
    инстансе.
    """
    users = _fresh_registry(bucket).get("users", {})
    athlete = users.get(athlete_sub)
    if not (_is_active_coach(users.get(coach_sub)) and _is_coached_by(athlete, coach_sub)):
        return None
    return "edit" if coach_plan_edit_allowed(athlete) else "view"


def set_coach_plan_edit(bucket, sub, allow):
    """Спортсмен разрешает или запрещает тренеру править планы (#60).

    Разрешение — часть связи с тренером, поэтому без действующего тренера
    ставить нечего: CoachLinkError("no_coach").
    """
    allow = bool(allow)
    seen = {}

    def change(registry):
        users = registry["users"]
        rec = users.get(sub)
        if rec is None or not _is_active_coach(users.get(rec.get("coach_sub"))):
            raise CoachLinkError("no_coach")
        seen["rec"] = rec
        if coach_plan_edit_allowed(rec) == allow:
            return None
        rec["coach_plan_edit"] = allow
        rec["updated_at"] = datetime.utcnow().isoformat() + "Z"
        return rec

    rec = update_registry(bucket, change)
    if rec is not None:
        append_user_event(bucket, sub, "coach_plan_edit_allow" if allow else "coach_plan_edit_deny",
                          sub, {"coach_sub": rec["coach_sub"]})
    return seen["rec"]


def my_coach(bucket, sub, fresh=False):
    """(карточка действующего тренера или None, можно ли ему править планы) —
    из одного снимка реестра, чтобы тренер и переключатель не разошлись.

    fresh — мимо кэша инстанса: карточке «Мой тренер» нужно то, что стоит на
    самом деле; счётчику непрочитанных хватает кэша."""
    users = (_fresh_registry(bucket) if fresh else read_registry(bucket)).get("users", {})
    rec = users.get(sub) or {}
    coach = users.get(rec.get("coach_sub"))
    return (_coach_card(coach) if _is_active_coach(coach) else None), coach_plan_edit_allowed(rec)


def list_athletes_of(bucket, coach_sub):
    """Спортсмены тренера: [{sub, name, can_edit}]. None — пользователь не тренер."""
    users = _fresh_registry(bucket).get("users", {})
    if not _is_active_coach(users.get(coach_sub)):
        return None
    athletes = [{"sub": u["sub"], "name": u.get("name") or u.get("email") or "Спортсмен",
                 "can_edit": coach_plan_edit_allowed(u)}
                for u in users.values() if _is_coached_by(u, coach_sub)]
    return sorted(athletes, key=lambda a: a["name"].lower())


def active_coach_sub(bucket, sub):
    """sub действующего тренера пользователя или None — по свежему реестру."""
    users = _fresh_registry(bucket).get("users", {})
    coach_sub = (users.get(sub) or {}).get("coach_sub")
    return coach_sub if _is_active_coach(users.get(coach_sub)) else None


# ── Чат тренера и спортсмена (#44) ───────────────────────────────────────────
# Ветка одна на пару и лежит в namespace спортсмена: при смене тренера она
# остаётся в хранилище, но новому тренеру не видна — у него своя.
# Сообщение — отдельный неизменяемый объект. Ничего не перезаписывается, и
# одновременная отправка с двух сторон ничего не теряет: общего файла,
# который читали бы и писали обратно, просто нет.

CHAT_TEXT_MAX = 2000
CHAT_PAGE = 50
CHAT_ROLES = ("athlete", "coach")
# Идентификатор = время + автор + случайный хвост. Время впереди, поэтому
# сортировка имён объектов и есть порядок сообщений; автор в имени позволяет
# считать непрочитанные одним list_blobs, не скачивая сообщения.
_CHAT_ID_RE = re.compile(r"^\d{8}T\d{12}-(athlete|coach)-[0-9a-f]{8}$")


class ChatError(ValueError):
    """Сообщение или курсор не приняты. Код причины — str(исключения)."""


def _check_chat_cursor(cursor):
    if cursor and not _CHAT_ID_RE.match(str(cursor)):      # пустой курсор = его нет
        raise ChatError("bad_cursor")


# Нижний слой общий для чата с тренером и разборов с ИИ (#46): и там, и там
# ветка — это префикс, под которым лежат сообщения-объекты.

def _message_ids(bucket, prefix, id_re):
    """Идентификаторы сообщений под префиксом, от старых к новым."""
    ids = (blob.name[len(prefix):-len(".json")]
           for blob in bucket.list_blobs(prefix=prefix) if blob.name.endswith(".json"))
    return sorted(i for i in ids if id_re.match(i))


def _next_message_id(ids, role):
    """(id, время) для нового сообщения ветки, в которой уже лежат ids.

    Время в идентификаторе строго растёт внутри ветки: иначе при грубых или
    разошедшихся часах инстансов новое сообщение встало бы раньше уже
    показанного, и опрос по курсору after его бы пропустил.
    """
    now = datetime.utcnow()
    if ids:
        last = datetime.strptime(ids[-1][:21], "%Y%m%dT%H%M%S%f")
        if now <= last:
            now = last + timedelta(microseconds=1)
    return f"{now.strftime('%Y%m%dT%H%M%S%f')}-{role}-{secrets_mod.token_hex(4)}", now


def _load_json_objects(bucket, paths, skip_broken=False):
    """Объект на сообщение — это запрос на сообщение; читаем параллельно.
    skip_broken — нечитаемый объект даёт None вместо исключения."""
    def load(path):
        try:
            return json.loads(bucket.blob(path).download_as_text())
        except Exception:
            if not skip_broken:
                raise
            return None

    if len(paths) > 1:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(load, paths))
    return [load(path) for path in paths]


def _chat_ids(bucket, athlete, coach):
    return _message_ids(bucket, p_chat_prefix(athlete, coach), _CHAT_ID_RE)


def append_chat_message(bucket, athlete, coach, from_sub, role, text, extra=None):
    """Пишет сообщение новым объектом и возвращает его.

    extra — служебные поля сообщения, которое пишет сервер (уведомление о
    правке плана, #60). Основные поля ими не перекрыть.
    """
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise ChatError("empty_message")
    if len(text) > CHAT_TEXT_MAX:
        raise ChatError("message_too_long")
    ids = _chat_ids(bucket, athlete, coach)

    def make():
        msg_id, now = _next_message_id(ids, role)
        message = {**(extra or {}), "id": msg_id, "ts": now.isoformat() + "Z",
                   "from_sub": from_sub, "from_role": role, "text": text}
        return p_chat_msg(athlete, coach, msg_id), message

    return create_unique_json(bucket, make)


# Что изменил тренер → начало сообщения спортсмену (#60).
PLAN_CHANGE_TEXT = {"create": "Тренер создал план",
                    "weeks": "Тренер изменил план",
                    "meta": "Тренер изменил карточку гонки плана"}


def plan_label(plan):
    """Название плана так же, как его показывает фронтенд (planLabel)."""
    return (plan.get("race_name")
            or (f"Забег {plan['race_date']}" if plan.get("race_date") else "Без названия"))


def notify_plan_change(bucket, athlete, coach, change, plan, version=None, comment=""):
    """Сообщение в чат пары о том, что тренер поменял план спортсмена.

    Обычное сообщение от роли тренера: бейдж непрочитанных у спортсмена уже
    умеет его показать. kind и plan_id нужны фронтенду, чтобы выделить его
    и открыть план.
    """
    text = f"{PLAN_CHANGE_TEXT[change]} «{plan_label(plan)}»"
    if comment:
        text += f": {comment}"
    extra = {"kind": "plan_change", "change": change, "plan_id": plan["id"]}
    if version is not None:
        extra["plan_version"] = version
    return append_chat_message(bucket, athlete, coach, coach, "coach",
                               text[:CHAT_TEXT_MAX], extra)


def read_chat(bucket, athlete, coach, after=None, before=None, limit=CHAT_PAGE):
    """Страница ветки: последние `limit` сообщений из подходящих под курсоры.

    after — только новее этого id (опрос), before — только старше («показать
    более ранние»). has_more — за пределами страницы остались более ранние.
    """
    _check_chat_cursor(after)
    _check_chat_cursor(before)
    ids = _chat_ids(bucket, athlete, coach)
    if after:
        ids = [i for i in ids if i > after]
    if before:
        ids = [i for i in ids if i < before]
    page = ids[-limit:]
    messages = _load_json_objects(bucket, [p_chat_msg(athlete, coach, i) for i in page])
    return {"messages": messages, "has_more": len(ids) > len(page)}


def _chat_last_read(bucket, athlete, coach, role):
    blob = bucket.blob(p_chat_read(athlete, coach, role))
    if not blob.exists():
        return ""
    return json.loads(blob.download_as_text()).get("last_read_id") or ""


def chat_unread(bucket, athlete, coach, reader_role):
    """Сколько сообщений собеседника читатель ещё не видел."""
    last = _chat_last_read(bucket, athlete, coach, reader_role)
    mine = f"-{reader_role}-"
    return sum(1 for i in _chat_ids(bucket, athlete, coach) if i > last and mine not in i)


def mark_chat_read(bucket, athlete, coach, reader_role, last_id=None):
    """Сдвигает отметку «прочитано до» и возвращает её. Только вперёд и не
    дальше последнего существующего сообщения — «прочитать» ещё не
    написанное нельзя. Отметка — lifecycle-метаданные, не бизнес-запись."""
    _check_chat_cursor(last_id)
    ids = _chat_ids(bucket, athlete, coach)
    if not ids:
        return _chat_last_read(bucket, athlete, coach, reader_role)
    target = min(last_id, ids[-1]) if last_id else ids[-1]
    seen = {}

    def advance(marker):
        # Сверка с отметкой — на свежем чтении: две вкладки разом не
        # откатят её назад (#35).
        seen["last"] = marker.get("last_read_id") or ""
        if target <= seen["last"]:
            return None
        marker.update(last_read_id=target, updated_at=datetime.utcnow().isoformat() + "Z")
        return target

    return update_json(bucket, p_chat_read(athlete, coach, reader_role), advance, dict) \
        or seen["last"]


# ── Разборы с ИИ-тренером (#46) ───────────────────────────────────────────────
# Разбор — ветка диалога спортсмена с LLM. Хранится как чат с тренером:
# карточка ветки и каждое сообщение — отдельные неизменяемые объекты. Скрытие
# разбора — объект-событие рядом; сама ветка остаётся в хранилище.

AI_TITLE_MAX = 80
AI_HISTORY_WINDOW = 12        # столько последних реплик ветки уходит модели
_AI_ID_TIME = "%Y%m%dT%H%M%S%f"
_AI_THREAD_ID_RE = re.compile(r"^\d{8}T\d{12}-[0-9a-f]{8}$")
_AI_MSG_ID_RE = re.compile(r"^\d{8}T\d{12}-(athlete|ai)-[0-9a-f]{8}$")


class AICoachError(ValueError):
    """Запрос к разбору не принят. Код причины — str(исключения)."""


def clean_ai_text(text):
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise AICoachError("empty_message")
    if len(text) > CHAT_TEXT_MAX:
        raise AICoachError("message_too_long")
    return text


def find_own_run(bucket, sub, run_id):
    """Своя не скрытая пробежка по id; None — такой нет.

    Читается только журнал самого пользователя, поэтому чужой id здесь
    просто не находится — отдельной проверки владения не нужно.
    """
    try:
        run_id = int(run_id)
    except (TypeError, ValueError):
        return None
    return next((r for r in read_runs(bucket, sub)
                 if r.get("id") == run_id and not r.get("deleted", False)), None)


def run_title(run):
    """«Длительный 14.2 км · 2026-10-03» — подпись пробежки в разборе."""
    label = TYPE_LABELS.get(run.get("type"), "тренировка").capitalize()
    try:
        dist = f"{float(run.get('dist')):g} км"
    except (TypeError, ValueError):
        dist = ""
    return " · ".join(part for part in (f"{label} {dist}".strip(), run.get("date")) if part)


def create_ai_thread(bucket, sub, title, run=None, created_by="api"):
    """Заводит разбор и возвращает его карточку. LLM здесь не вызывается.

    run — пробежка, которой разбор посвящён: она остаётся в фокусе на всём
    его протяжении и даёт заголовок, если своего не задали.
    """
    title = " ".join(str(title or "").split())[:AI_TITLE_MAX]

    def make():
        now = datetime.utcnow()
        thread = {
            "id": f"{now.strftime(_AI_ID_TIME)}-{secrets_mod.token_hex(4)}",
            "kind": "run" if run else "general",
            "run_id": run["id"] if run else None,
            "title": title or (run_title(run) if run else "Разбор"),
            "status": "active",
            "created_at": now.isoformat() + "Z",
            "created_by": created_by,
        }
        return p_ai_thread(sub, thread["id"]), thread

    return create_unique_json(bucket, make)


def read_ai_thread(bucket, sub, thread_id):
    """Карточка разбора; None — такого нет или он скрыт."""
    if not _AI_THREAD_ID_RE.match(str(thread_id)):
        return None
    blob = bucket.blob(p_ai_thread(sub, thread_id))
    if not blob.exists() or bucket.blob(p_ai_archived(sub, thread_id)).exists():
        return None
    return json.loads(blob.download_as_text())


def list_ai_threads(bucket, sub):
    """Разборы пользователя, сначала с самой свежей репликой.

    Число сообщений и время последнего берутся из имён объектов, одним
    list_blobs; скачиваются только карточки. Разбор без единого сообщения в
    список не попадает: он остаётся, когда первый ход не дошёл до модели.
    """
    root = p_ai_root(sub)
    found = {}
    for blob in bucket.list_blobs(prefix=root):
        thread_id, _, rest = blob.name[len(root):].partition("/")
        if not _AI_THREAD_ID_RE.match(thread_id):
            continue
        entry = found.setdefault(thread_id, {"ids": [], "card": False, "archived": False})
        if rest == "thread.json":
            entry["card"] = True
        elif rest == "archived.json":
            entry["archived"] = True
        elif rest.startswith("m/") and _AI_MSG_ID_RE.match(rest[2:-len(".json")]):
            entry["ids"].append(rest[2:-len(".json")])

    visible = sorted((tid for tid, e in found.items()
                      if e["card"] and e["ids"] and not e["archived"]),
                     key=lambda tid: max(found[tid]["ids"]), reverse=True)
    threads = _load_json_objects(bucket, [p_ai_thread(sub, tid) for tid in visible])
    for thread in threads:
        ids = found[thread["id"]]["ids"]
        last = datetime.strptime(max(ids)[:21], _AI_ID_TIME)
        thread["messages"] = len(ids)
        thread["last_ts"] = last.isoformat() + "Z"
    return threads


def archive_ai_thread(bucket, sub, thread_id, archived_by="api"):
    """Скрывает разбор из списка. False — скрывать нечего. Если параллельный
    запрос успел скрыть первым, его событие остаётся как есть."""
    if not read_ai_thread(bucket, sub, thread_id):
        return False
    create_json(bucket, p_ai_archived(sub, thread_id),
                {"archived_at": datetime.utcnow().isoformat() + "Z",
                 "archived_by": archived_by})
    return True


def append_ai_message(bucket, sub, thread_id, role, text, extra=None):
    """Пишет реплику разбора новым объектом и возвращает её."""
    ids = _message_ids(bucket, p_ai_msg_prefix(sub, thread_id), _AI_MSG_ID_RE)

    def make():
        msg_id, now = _next_message_id(ids, role)
        message = {"id": msg_id, "ts": now.isoformat() + "Z", "role": role,
                   "text": text, **(extra or {})}
        return p_ai_msg(sub, thread_id, msg_id), message

    return create_unique_json(bucket, make)


def read_ai_messages(bucket, sub, thread_id):
    ids = _message_ids(bucket, p_ai_msg_prefix(sub, thread_id), _AI_MSG_ID_RE)
    return _load_json_objects(bucket, [p_ai_msg(sub, thread_id, i) for i in ids])


def ai_history(messages, window=AI_HISTORY_WINDOW):
    """Последние реплики ветки в виде, который принимает call_llm.

    Ответы ИИ отдаются тем же JSON-конвертом, в каком модель их писала:
    увидев свои прошлые ответы простым текстом, она начинает отвечать так же
    и ломает JSON-режим.
    """
    recent = messages[-window:]
    while recent and recent[0]["role"] != "athlete":    # диалог открывает вопрос
        recent = recent[1:]

    def envelope(message):
        data = {"reply": message["text"]}
        proposal = message.get("proposal")
        if proposal:        # модель должна помнить, что именно она предлагала
            data["proposal"] = {
                "summary": proposal["summary"],
                "changes": [{k: c[k] for k in ("week", "day", "text", "reason")}
                            for c in proposal["changes"]]}
        memory = _memory_envelope(message.get("memory_changes"))
        if memory:          # и что записала в память (#80)
            data["memory"] = memory
        return json.dumps(data, ensure_ascii=False)

    return [{"role": "user", "content": m["text"]} if m["role"] == "athlete" else
            {"role": "assistant", "content": envelope(m)}
            for m in recent]


def parse_coach_reply(text):
    """(ответ, весь конверт) из ответа модели на ход диалога.

    Модель, ответившая вне JSON, не повод терять ход: тогда весь её текст и
    есть ответ. Пустой ответ — ошибка, показывать спортсмену нечего.
    """
    try:
        envelope = parse_llm_json(text)
    except ValueError:
        envelope = None
    if isinstance(envelope, dict):
        reply = envelope.get("reply")
    else:
        envelope, reply = {}, text
    reply = reply.strip() if isinstance(reply, str) else ""
    if not reply:
        raise ValueError("модель вернула пустой ответ")
    return reply, envelope


AI_FOCUS_RUNS_MAX = 3         # подробных блоков по пробежкам на один ход


def ai_focus_run_ids(thread, messages, run_id=None, window=AI_HISTORY_WINDOW):
    """Какие пробежки разбираются на этом ходу, от давно упомянутой к свежей.

    Пробежка разбора, прикреплённые к репликам окна и прикреплённая сейчас.
    Повторное упоминание поднимает пробежку в конец; лишние отбрасываются с
    начала — подробный блок стоит токенов, а разговор уже ушёл дальше.
    """
    mentioned = ([thread.get("run_id")]
                 + [m.get("run_id") for m in messages[-window:]] + [run_id])
    ids = []
    for rid in mentioned:
        if rid is None:
            continue
        if rid in ids:
            ids.remove(rid)
        ids.append(rid)
    return ids[-AI_FOCUS_RUNS_MAX:]


def build_run_focus(bucket, sub, run, ctx):
    """Подробные данные одной пробежки для разбора (форматирует llm_prompt)."""
    focus = {"run": run, "laps": [], "hr_drift_pct": None, "half_paces": None,
             "zones": [], "zones_manual": False,
             "hr_max": None, "hr_max_estimated": False, "planned": None}

    details = None
    if run.get("details_available"):
        try:
            details = read_run_details(bucket, sub, run["id"])
        except Exception:
            details = None          # без деталей разбор идёт по сводке
    if details:
        derived = ctx.get("profile_derived") or {}
        focus["laps"] = details.get("laps") or []
        focus["hr_drift_pct"] = compute_hr_drift(details)
        focus["half_paces"] = half_paces(details)
        focus["zones"] = hr_zone_minutes(details, derived.get("hr_zones"))
        focus["zones_manual"] = derived.get("hr_zones_source") == "manual"
        focus["hr_max"] = derived.get("hr_max_effective")
        focus["hr_max_estimated"] = bool(derived.get("hr_max_estimated"))

    # Что стояло в плане на этот день. У пробежки без привязки (до #25)
    # смотрим активный план: дата всё равно должна попасть в его недели.
    plan_id = run.get("plan_id") or ctx.get("plan_id")
    if plan_id and plan_id == ctx.get("plan_id"):
        weeks, plan_start = ctx.get("plan_weeks") or [], ctx.get("plan_start")
    elif plan_id:
        plan = find_plan(read_plans_index(bucket, sub), plan_id)
        weeks = read_plan_weeks(bucket, sub, plan_id) if plan else []
        plan_start = (plan or {}).get("plan_start")
    else:
        weeks, plan_start = [], None
    hit = planned_for_date(weeks, run.get("date"), plan_start)
    if hit:
        idx, field, text = hit
        focus["planned"] = {"week": idx + 1, "day": field, "text": text,
                            "phase": weeks[idx].get("type"),
                            "accent": weeks[idx].get("accent", "")}
    return focus


def build_ai_turn(bucket, sub, thread, messages, run=None):
    """Всё, что уходит модели на очередной ход, кроме самого вопроса.

    Контекст пересобирается на каждый ход: между репликами спортсмен мог
    добавить пробежку или поправить план. run — пробежка, прикреплённая к
    новому вопросу. Память (#80) общая для всех разборов и тоже читается
    заново: её могли поправить в другом разборе или руками.
    """
    ctx = build_llm_context(bucket, sub)
    own = {r.get("id"): r for r in read_runs(bucket, sub) if not r.get("deleted", False)}
    focus_ids = [rid for rid in ai_focus_run_ids(thread, messages, run and run["id"])
                 if rid in own]       # скрытая после разбора пробежка выпадает молча
    blocks = [format_run_focus(build_run_focus(bucket, sub, own[rid], ctx))
              for rid in focus_ids]
    window = ai_plan_window(ctx["plan_weeks"], ctx["plan_start"], ctx["week_idx"])
    memory, memory_version = read_ai_memory(bucket, sub)
    instructions = coach_chat_instructions(with_proposals=bool(window))
    data = coach_chat_data(format_context_for_llm(ctx), blocks,
                           format_plan_window(window) if window else "",
                           format_memory_block(memory_facts(memory)))
    return {
        "ctx": ctx,
        "memory": memory,
        "memory_version": memory_version,
        "focus_run_ids": focus_ids,
        "plan_window": window,
        "instructions": instructions,
        "data": data,
        "system": instructions + "\n\n" + data,
        "history": ai_history(messages),
    }


# ── Правки плана от ИИ-тренера (#46) ──────────────────────────────────────────
# Модель план не меняет: она возвращает предложение, сервер сверяет его с
# планом и хранит в реплике, а применяет спортсмен — отдельным запросом,
# который пишет обычную новую версию плана.

AI_PLAN_WINDOW_WEEKS = 4      # текущая неделя и три следующие открыты для правок
AI_PROPOSAL_MAX_CHANGES = 14
AI_PROPOSAL_TEXT_MAX = 200


def ai_plan_window(weeks, plan_start, week_idx, today=None, count=AI_PLAN_WINDOW_WEEKS):
    """Недели плана, которые ИИ может править, с датой и текстом каждого дня.

    [] — плана нет или он ничем не датирован: править ячейку, не зная её
    даты, значит гадать. Недели, закончившиеся до сегодня, в окно не входят,
    прошедшие дни текущей помечены и правке не подлежат.
    """
    today = to_date(today) or datetime.utcnow().date()
    if not weeks or anchor_source(plan_start, weeks) == UNDATED:
        return []
    window = []
    for idx in range(max(week_idx, 0), min(len(weeks), week_idx + count)):
        start, end = week_window(weeks, idx, plan_start)
        if end < today:
            continue
        days = week_days(start, end)
        window.append({
            "week": idx + 1,
            "current": start <= today <= end,
            "start": start.isoformat(), "end": end.isoformat(),
            "phase": weeks[idx].get("type"),
            "days": [{"field": field, "date": day.isoformat(),
                      "text": str(weeks[idx].get(field) or "").strip(),
                      "past": day < today}
                     for day, field in days],
        })
    return window


def _one_line(value, limit=AI_PROPOSAL_TEXT_MAX):
    return " ".join(value.split())[:limit] if isinstance(value, str) else ""


def clean_proposal(raw, window):
    """Предложение модели, сверенное с окном плана; None — применять нечего.

    Модель может ошибиться номером недели, выдумать день, тронуть прошедший
    или вернуть то, что и так стоит в плане. Такая правка отбрасывается
    молча — текстовый ответ тренера от этого не страдает. У каждой принятой
    правки сохраняется, что было в ячейке: карточку «было → стало» можно
    показать и после того, как план ушёл вперёд.
    """
    if not isinstance(raw, dict) or not isinstance(raw.get("changes"), list):
        return None
    cells = {(week["week"], day["field"]): day for week in window for day in week["days"]}
    changes = {}
    for item in raw["changes"]:
        if not isinstance(item, dict):
            continue
        week, field, text = item.get("week"), item.get("day"), item.get("text")
        if isinstance(week, bool) or not isinstance(week, int) \
                or not isinstance(field, str) or not isinstance(text, str):
            continue
        cell = cells.get((week, field))
        text = _one_line(text)
        if cell is None or cell["past"] or text == cell["text"]:
            continue
        changes.pop((week, field), None)        # ячейка повторилась — в силе последняя правка
        changes[(week, field)] = {"week": week, "day": field, "date": cell["date"],
                                  "old": cell["text"], "text": text,
                                  "reason": _one_line(item.get("reason"))}
    if not changes:
        return None
    ordered = sorted(changes.values(), key=lambda change: change["date"])
    return {"summary": _one_line(raw.get("summary")) or "Корректировка плана",
            "changes": ordered[:AI_PROPOSAL_MAX_CHANGES]}


def read_ai_message(bucket, sub, thread_id, msg_id):
    if not _AI_MSG_ID_RE.match(str(msg_id)):
        return None
    blob = bucket.blob(p_ai_msg(sub, thread_id, msg_id))
    return json.loads(blob.download_as_text()) if blob.exists() else None


def _active_plan_version(bucket, sub):
    """(id активного плана, номер его текущей версии, манифест) или тройка None."""
    active = get_active_plan(bucket, sub)
    manifest = read_plan_manifest(bucket, sub, active["id"]) if active else None
    if not manifest:
        return None, None, None
    return active["id"], manifest["current_version"], manifest


def _proposal_is_current(proposal, plan_id, version, today):
    """Предложение ещё применимо: план тот же, той же версии, и ни один из
    затронутых дней не успел пройти."""
    return (plan_id is not None
            and proposal.get("plan_id") == plan_id
            and proposal.get("plan_version") == version
            and all((to_date(c.get("date")) or today) >= today
                    for c in proposal.get("changes", [])))


def mark_proposal_states(bucket, sub, thread_id, messages, today=None):
    """Проставляет репликам с предложением proposal_state: applied / stale / open.

    Состояние считается на чтении и нигде не хранится: «устарело» — это
    свойство текущего плана, а не реплики.
    """
    proposed = [m for m in messages if m.get("proposal")]
    if not proposed:
        return messages
    today = to_date(today) or datetime.utcnow().date()
    prefix = p_ai_applied_prefix(sub, thread_id)
    applied = {blob.name[len(prefix):-len(".json")]
               for blob in bucket.list_blobs(prefix=prefix)}
    plan_id, version, _ = _active_plan_version(bucket, sub)
    for message in proposed:
        if message["id"] in applied:
            message["proposal_state"] = "applied"
        elif _proposal_is_current(message["proposal"], plan_id, version, today):
            message["proposal_state"] = "open"
        else:
            message["proposal_state"] = "stale"
    return messages


def apply_ai_proposal(bucket, sub, thread_id, msg_id, applied_by="api", today=None):
    """Применяет предложение из реплики: новая версия плана и событие применения.

    None — реплики нет. AICoachError: no_proposal, already_applied,
    proposal_stale. Прежняя версия плана остаётся в хранилище, вернуться к
    ней можно обычной правкой.

    Версия плана пишется поверх той, на которую смотрело предложение
    (base_version), поэтому из двух одновременных запросов план меняет один;
    второй получает proposal_stale, а фронтенд, перечитав разбор, видит
    «применено» (#35).
    """
    message = read_ai_message(bucket, sub, thread_id, msg_id)
    if not message:
        return None
    proposal = message.get("proposal")
    if not proposal:
        raise AICoachError("no_proposal")
    marker_blob = bucket.blob(p_ai_applied(sub, thread_id, msg_id))
    if marker_blob.exists():
        raise AICoachError("already_applied")

    today = to_date(today) or datetime.utcnow().date()
    plan_id, version, manifest = _active_plan_version(bucket, sub)
    if not _proposal_is_current(proposal, plan_id, version, today):
        raise AICoachError("proposal_stale")
    current = read_plan_version(bucket, manifest["gcs_object_path"]) or {}
    weeks = [dict(week) for week in current.get("weeks", [])]
    if any(not 1 <= change["week"] <= len(weeks) for change in proposal["changes"]):
        raise AICoachError("proposal_stale")
    for change in proposal["changes"]:
        weeks[change["week"] - 1][change["day"]] = change["text"]

    try:
        saved = save_plan_weeks(bucket, sub, plan_id, weeks,
                                f"ИИ-тренер: {proposal['summary']}", applied_by,
                                base_version=version, author=plan_author("ai", sub))
    except PlanStale:
        # План переписали между сверкой выше и записью.
        raise AICoachError("proposal_stale")
    marker = {
        "thread_id": thread_id, "message_id": msg_id,
        "applied_at": datetime.utcnow().isoformat() + "Z", "applied_by": applied_by,
        "plan_id": plan_id, "from_version": version, "to_version": saved["version"],
    }
    create_json(bucket, p_ai_applied(sub, thread_id, msg_id), marker)
    return marker


# ── Память ИИ-тренера (#80) ───────────────────────────────────────────────────
# Факты, которые тренер выносит из разборов: что спортсмен рассказал сам и о
# чём договорились. Документ один на спортсмена и общий для всех разборов.
# Хранится версиями, как профиль: любое изменение — новый объект версии,
# прошлые не трогаются. Скрытый факт остаётся в документе со status hidden;
# в текущей версии держим последние AI_MEMORY_HIDDEN_KEEP скрытых, более
# старые остаются в прошлых версиях.

AI_MEMORY_HIDDEN_KEEP = 50
AI_MEMORY_HISTORY_LIMIT = 50
MEMORY_EDIT_OPS = ("add", "edit", "hide", "restore")
_MEMORY_FACT_ID_RE = re.compile(r"^m[1-9][0-9]{0,5}$")
_MEMORY_OP_WORDS = {"add": "добавлено", "edit": "изменено", "hide": "скрыто",
                    "restore": "возвращено"}


class MemoryStale(VersionStale):
    """Правка памяти сделана поверх версии, которая уже не текущая."""


def _memory_doc(payload):
    """Документ памяти {next_id, facts} из payload версии; нет или битый — пустой."""
    payload = payload if isinstance(payload, dict) else {}
    facts = [dict(f) for f in payload.get("facts") or []
             if isinstance(f, dict) and _MEMORY_FACT_ID_RE.match(str(f.get("id")))
             and isinstance(f.get("text"), str) and _memory_kind(f.get("kind"))]
    taken = max((int(f["id"][1:]) for f in facts), default=0)
    next_id = payload.get("next_id")
    if type(next_id) is not int or next_id <= taken:
        next_id = taken + 1
    return {"next_id": next_id, "facts": facts}


def memory_facts(doc, status="active"):
    return [f for f in doc["facts"] if f.get("status") == status]


def read_ai_memory(bucket, sub):
    """(документ памяти, номер текущей версии). Памяти ещё нет — пустая и 0."""
    manifest = _read_json(bucket, p_ai_memory_manifest(sub))
    if not manifest:
        return _memory_doc(None), 0
    return (_memory_doc(_read_json(bucket, manifest["gcs_object_path"])),
            manifest["current_version"])


def _memory_kind(value):
    return value if isinstance(value, str) and value in MEMORY_KIND_LABELS else None


def _memory_items(value):
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _memory_ref(value):
    """id факта, как его вернула модель: «m3» или «[m3]»."""
    return value.strip().strip("[]").strip() if isinstance(value, str) else None


def clean_memory_ops(raw, facts):
    """Изменения памяти из ответа модели, сверенные с фактами; [] — менять нечего.

    raw — {"add": [{kind, text}], "edit": [{id, text}], "forget": [{id}]},
    facts — активные факты. Модель может сослаться на факт, которого нет,
    вернуть пустой текст, чужой вид или то, что уже записано. Такое
    отбрасывается молча: ответ тренера от этого не страдает. Факт меняется
    одной операцией за ход, всего их не больше MEMORY_OPS_MAX. Забытое идёт
    первым: оно освобождает место под новое.
    """
    if not isinstance(raw, dict):
        return []
    active = {f["id"]: f for f in facts}
    known = {f["text"].casefold() for f in facts}
    ops, touched = [], set()
    for item in _memory_items(raw.get("forget")):
        fact_id = _memory_ref(item.get("id"))
        if fact_id in active and fact_id not in touched:
            touched.add(fact_id)
            ops.append({"op": "hide", "id": fact_id})
    for item in _memory_items(raw.get("edit")):
        fact_id, text = _memory_ref(item.get("id")), _one_line(item.get("text"), MEMORY_TEXT_MAX)
        fact = active.get(fact_id)
        if not fact or fact_id in touched or not text:
            continue
        kind = _memory_kind(item.get("kind")) or fact["kind"]
        if (text, kind) == (fact["text"], fact["kind"]):
            continue
        touched.add(fact_id)
        ops.append({"op": "edit", "id": fact_id, "kind": kind, "text": text})
    for item in _memory_items(raw.get("add")):
        text, kind = _one_line(item.get("text"), MEMORY_TEXT_MAX), _memory_kind(item.get("kind"))
        if not text or not kind or text.casefold() in known:
            continue
        known.add(text.casefold())
        ops.append({"op": "add", "kind": kind, "text": text})
    return ops[:MEMORY_OPS_MAX]


def clean_memory_edit(body, doc):
    """Правка памяти спортсменом → операция для apply_memory_ops.

    AICoachError: invalid_fact — нет такой операции, пустой или длинный
    текст, чужой вид; fact_not_found — факта нет или он не в том состоянии;
    memory_full — места под ещё один активный факт нет.
    """
    op, kind = body.get("op"), body.get("kind")
    if op not in MEMORY_EDIT_OPS or (kind is not None and not _memory_kind(kind)):
        raise AICoachError("invalid_fact")
    full = len(memory_facts(doc)) >= MEMORY_FACTS_MAX
    if op in ("add", "edit"):
        text = body.get("text")
        text = " ".join(text.split()) if isinstance(text, str) else ""
        if not text or len(text) > MEMORY_TEXT_MAX:
            raise AICoachError("invalid_fact")
    if op == "add":
        if kind is None:
            raise AICoachError("invalid_fact")
        if full:
            raise AICoachError("memory_full")
        return {"op": "add", "kind": kind, "text": text}

    fact = next((f for f in doc["facts"] if f["id"] == body.get("id")), None)
    if not fact or fact.get("status") != ("hidden" if op == "restore" else "active"):
        raise AICoachError("fact_not_found")
    if op == "edit":
        return {"op": "edit", "id": fact["id"], "kind": kind or fact["kind"], "text": text}
    if op == "restore" and full:
        raise AICoachError("memory_full")
    return {"op": op, "id": fact["id"]}


def apply_memory_ops(doc, ops, role, now, thread_id=None):
    """(новый документ, изменения) — чистая функция; изменений нет — [].

    Изменения считаются по тому документу, поверх которого пишем: пока
    модель думала, факт могли скрыть, а память — заполнить. Операция,
    которая к документу уже не подходит, пропускается.
    """
    facts = [dict(f) for f in doc["facts"]]
    by_id = {f["id"]: f for f in facts}
    next_id = doc["next_id"]
    changes = []

    def has_room():
        return sum(1 for f in facts if f.get("status") == "active") < MEMORY_FACTS_MAX

    def note(op, fact, **extra):
        changes.append({"op": op, "id": fact["id"], "kind": fact["kind"],
                        "text": fact["text"], **extra})

    for op in ops:
        fact = by_id.get(op.get("id"))
        status = fact.get("status") if fact else None
        if op["op"] == "add" and has_room():
            fact = {"id": f"m{next_id}", "kind": op["kind"], "text": op["text"],
                    "status": "active", "source": role, "source_thread_id": thread_id,
                    "created_at": now, "updated_at": now}
            next_id += 1
            facts.append(fact)
            by_id[fact["id"]] = fact
            note("add", fact)
        elif op["op"] == "edit" and status == "active" \
                and (op["text"], op["kind"]) != (fact["text"], fact["kind"]):
            old = fact["text"]
            fact.update(kind=op["kind"], text=op["text"], updated_at=now, updated_by=role)
            note("edit", fact, old=old)
        elif op["op"] == "hide" and status == "active":
            fact.update(status="hidden", hidden_at=now, hidden_by=role)
            note("hide", fact)
        elif op["op"] == "restore" and status == "hidden" and has_room():
            fact.update(status="active", restored_at=now)
            fact.pop("hidden_at", None)
            fact.pop("hidden_by", None)
            note("restore", fact)

    hidden = sorted((f for f in facts if f.get("status") == "hidden"),
                    key=lambda f: f.get("hidden_at") or "")
    dropped = {f["id"] for f in hidden[:-AI_MEMORY_HIDDEN_KEEP]}
    return {"next_id": next_id, "facts": [f for f in facts if f["id"] not in dropped]}, changes


def _memory_reason(role, changes):
    """«ИИ-тренер: добавлено 2, скрыто 1» — change_reason версии памяти."""
    counts = [f"{word} {n}" for op, word in _MEMORY_OP_WORDS.items()
              if (n := sum(1 for c in changes if c["op"] == op))]
    return ("ИИ-тренер" if role == "ai" else "Спортсмен") + ": " + ", ".join(counts)


def write_ai_memory(bucket, sub, ops, role, created_by="api", base_version=None,
                    thread_id=None):
    """Новая версия памяти с изменениями ops → её payload; None — писать нечего.

    Без base_version изменения ложатся на версию, текущую в момент записи:
    claim перечитывает её сам, поэтому повтор commit_version после чужой
    записи не теряет ни её, ни наших изменений. Так пишет модель. Правка
    спортсмена идёт с base_version, и поверх устаревшей версии это
    MemoryStale. В payload лежат и сами изменения — по ним история
    показывает, что именно поменялось.
    """
    now = datetime.utcnow().isoformat() + "Z"

    def claim(version, current):
        prev = _read_json(bucket, p_ai_memory_ver(sub, current)) if current else None
        doc, changes = apply_memory_ops(_memory_doc(prev), ops, role, now, thread_id)
        if not changes:
            raise _Unchanged(prev)
        path = p_ai_memory_ver(sub, version)
        payload = {"version": version, "is_current": True, "created_at": now,
                   "created_by": created_by, "author": role,
                   "change_reason": _memory_reason(role, changes),
                   "supersedes_version": current or None,
                   "source_thread_id": thread_id, "changes": changes, **doc}
        if not create_json(bucket, path, payload):
            return None
        manifest = {"current_version": version, "gcs_object_path": path,
                    "updated_at": now, "created_by": created_by, "author": role,
                    "checksum": hashlib.sha256(_dumps(payload).encode()).hexdigest()}
        return manifest, payload

    try:
        return commit_version(bucket, p_ai_memory_manifest(sub), claim,
                              base_version=base_version, stale=MemoryStale)
    except _Unchanged:
        return None


def memory_view(doc, version):
    """Память для спортсмена: активные и скрытые факты, виды и пределы."""
    hidden = sorted(memory_facts(doc, "hidden"), key=lambda f: f.get("hidden_at") or "",
                    reverse=True)
    return {"version": version, "facts": memory_facts(doc), "hidden": hidden,
            "kinds": [{"id": kind, "label": label} for kind, label in MEMORY_KIND_LABELS.items()],
            "limits": {"facts": MEMORY_FACTS_MAX, "text": MEMORY_TEXT_MAX}}


def read_ai_memory_history(bucket, sub, limit=AI_MEMORY_HISTORY_LIMIT):
    """Версии памяти от новых к старым: кто, когда и что поменял.

    Как у профиля, читается окно последних версий; версии, не попавшие в
    манифест (#35), в историю не входят.
    """
    manifest = _read_json(bucket, p_ai_memory_manifest(sub))
    if not manifest:
        return []
    current = manifest["current_version"]
    numbers = list(range(max(1, current - limit + 1), current + 1))
    payloads = dict(zip(numbers, _load_json_objects(
        bucket, [p_ai_memory_ver(sub, v) for v in numbers], skip_broken=True)))
    history = []
    for version in version_chain(payloads, current):
        data = payloads[version]
        if not isinstance(data, dict):
            continue
        history.append({
            "version": data.get("version", version),
            "created_at": data.get("created_at"),
            "author": data.get("author"),
            "change_reason": data.get("change_reason", ""),
            "source_thread_id": data.get("source_thread_id"),
            "changes": data.get("changes") or [],
            "facts": len(memory_facts(_memory_doc(data))),
        })
    return history


def _memory_envelope(changes):
    """Изменения памяти в том виде, в каком их пишет модель (для ai_history)."""
    memory = {}
    for change in changes or []:
        if change.get("op") == "add":
            memory.setdefault("add", []).append({"kind": change["kind"], "text": change["text"]})
        elif change.get("op") == "edit":
            memory.setdefault("edit", []).append({"id": change["id"], "text": change["text"]})
        elif change.get("op") == "hide":
            memory.setdefault("forget", []).append({"id": change["id"]})
    return memory


# ── Legacy → per-user миграция (админ, одноразово, идемпотентно) ──────────────

def migrate_legacy_to_user(bucket, sub):
    """Копирует глобальные (до multi-user) объекты в namespace админа.
    Пер-объектная идемпотентность: каждый объект проверяется независимо.
    FIT-детали (runs/{id}/*) НЕ копируются здесь — переезжают лениво при
    первом GET /runs/{id}/details (см. read_run_details).
    """
    report = {"copied": [], "skipped": [], "errors": []}

    def copy_obj(src, dst):
        try:
            src_blob = bucket.blob(src)
            if not src_blob.exists():
                report["skipped"].append(f"{src} (нет источника)")
                return
            if not copy_once(bucket, src_blob, dst):
                report["skipped"].append(f"{dst} (уже есть)")
                return
            report["copied"].append(dst)
        except Exception as e:
            report["errors"].append(f"{src}->{dst}: {str(e)[:120]}")

    def migrate_versioned(legacy_manifest_path, ver_src, ver_dst, dst_manifest_path):
        man_blob = bucket.blob(legacy_manifest_path)
        if not man_blob.exists():
            report["skipped"].append(f"{legacy_manifest_path} (нет источника)")
            return
        man = json.loads(man_blob.download_as_text())
        cur = man.get("current_version", 1)
        for v in range(1, cur + 1):
            copy_obj(ver_src(v), ver_dst(v))
        # Манифест не копируем как есть — пишем свежий с per-user gcs_object_path
        new_man = dict(man)
        new_man["gcs_object_path"] = ver_dst(cur)
        if create_json(bucket, dst_manifest_path, new_man):
            report["copied"].append(dst_manifest_path)
        else:
            report["skipped"].append(f"{dst_manifest_path} (уже есть)")

    copy_obj(LEGACY_RUNS, p_runs(sub))
    copy_obj(LEGACY_RACES, p_races(sub))
    # Сид профиля Alexander'а (исторические константы приложения)
    if write_legacy_race_profile(bucket, sub, {
            "race_name": "Полумарафон", "race_date": "2026-08-09",
            "target_time": "1:40", "plan_start": "2026-05-10"}):
        report["copied"].append(p_profile(sub))
    else:
        report["skipped"].append(f"{p_profile(sub)} (уже есть)")
    # Глобальный план → per-user одиночный план; дальше его подхватит
    # migrate_single_plan() и переведёт в реестр планов (#25).
    migrate_versioned(
        LEGACY_PLAN_MANIFEST,
        lambda v: f"plan/v{v}/plan.json", lambda v: p_singleplan_ver(sub, v),
        p_singleplan_manifest(sub))
    migrate_versioned(
        LEGACY_ADVICE_MANIFEST,
        lambda v: f"advice/v{v}/recommendation.json", lambda v: p_advice_ver(sub, v),
        p_advice_manifest(sub))
    return report
