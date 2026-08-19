"""Данные: GCS, версии, реестр пользователей, разбор FIT, контекст для LLM.

Выделено из main.py (#36, фаза 3). Модуль ничего не знает про HTTP — на вход
приходят bucket и sub, на выходе данные. Зависимости идут в одну сторону:
config → domain/llm_prompt → storage → api → main.
"""
import hashlib
import io
import json
import re
import secrets as secrets_mod
import time
from datetime import datetime

import httpx
from fitparse import FitFile
from google.cloud import storage as gcs

from config import (ADMIN_EMAILS, BUCKET_NAME, LLM_CONFIG_MANIFEST,
                    LLM_DEFAULT_EFFORT, LLM_EFFORT_LEVELS, LLM_MAX_TOKENS,
                    MAX_PENDING, REGISTRY_TTL_SEC, USERS_REGISTRY)
from domain import HR_ZONE_BOUNDS, PLAN_DAYS, personal_bests
from llm_prompt import SYSTEM_PROMPT, format_context_for_llm

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
# Одиночный план до #25 — только для ленивой миграции
def p_singleplan_manifest(sub): return f"{upfx(sub)}plan/manifest.json"
def p_singleplan_ver(sub, v):   return f"{upfx(sub)}plan/v{v}/plan.json"
def p_advice_manifest(sub):    return f"{upfx(sub)}advice/manifest.json"
def p_advice_ver(sub, v):      return f"{upfx(sub)}advice/v{v}/recommendation.json"
def p_advice_usage(sub):       return f"{upfx(sub)}advice/usage.json"
def p_profile(sub):            return f"{upfx(sub)}profile.json"   # legacy: гонка до #25
def p_athlete_manifest(sub):   return f"{upfx(sub)}athlete/manifest.json"
def p_athlete_ver(sub, v):     return f"{upfx(sub)}athlete/v{v}/profile.json"
def p_run_manifest(sub, rid):  return f"{upfx(sub)}runs/{rid}/manifest.json"
def p_run_fit(sub, rid):       return f"{upfx(sub)}runs/{rid}/v1/activity.fit"
def p_run_details(sub, rid):   return f"{upfx(sub)}runs/{rid}/v1/details.json"
def p_tmp_fit(sub, token):     return f"tmp/{sub}/{token}/activity.fit"
def p_tmp_details(sub, token): return f"tmp/{sub}/{token}/details.json"

# Legacy (глобальные, до multi-user) — только для миграции/ленивого fallback
LEGACY_RUNS = "runs.json"
LEGACY_RACES = "races.json"
LEGACY_PLAN_MANIFEST = "plan/manifest.json"
LEGACY_ADVICE_MANIFEST = "advice/manifest.json"
def legacy_run_manifest(rid): return f"runs/{rid}/manifest.json"
def legacy_run_fit(rid):      return f"runs/{rid}/v1/activity.fit"


# ── Runs helpers ──────────────────────────────────────────────────────────────

def read_runs(bucket, sub):
    blob = bucket.blob(p_runs(sub))
    if not blob.exists():
        return []
    return json.loads(blob.download_as_text())


def write_runs(bucket, sub, runs):
    bucket.blob(p_runs(sub)).upload_from_string(
        json.dumps(runs, ensure_ascii=False, indent=2),
        content_type="application/json"
    )


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
    clean = {k: (profile.get(k) or "") for k in PROFILE_FIELDS}
    bucket.blob(p_profile(sub)).upload_from_string(
        json.dumps(clean, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return clean


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
ATHLETE_HISTORY_FIELDS = ("weight_kg", "hr_max", "hr_threshold", "hr_rest", "vo2max")


def empty_athlete_profile():
    profile = {f: "" for f in ATHLETE_TEXT_FIELDS}
    profile.update({f: None for f in ATHLETE_NUM_FIELDS})
    profile["available_days"] = []
    return profile


def clean_athlete_profile(raw):
    """Валидация и нормализация входных данных. Возвращает (профиль, ошибки)."""
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
        except (TypeError, ValueError):
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


def compute_athlete_derived(profile, today=None):
    """Возраст, ИМТ, оценка HRmax и пульсовые зоны. Не хранится — считается на лету."""
    age = athlete_age(profile.get("birth_date"), today)
    derived = {"age": age, "bmi": None, "hr_max_estimated": None,
               "hr_max_effective": None, "hr_zones": []}

    height, weight = profile.get("height_cm"), profile.get("weight_kg")
    if height and weight:
        derived["bmi"] = round(weight / (height / 100) ** 2, 1)

    if not profile.get("hr_max") and age is not None:
        derived["hr_max_estimated"] = int(round(208 - 0.7 * age))   # формула Танаки

    effective = profile.get("hr_max") or derived["hr_max_estimated"]
    derived["hr_max_effective"] = effective
    if effective:
        derived["hr_zones"] = [
            {"name": name,
             "from": int(round(effective * low / 100)),
             "to": int(round(effective * high / 100))}
            for name, low, high in HR_ZONE_BOUNDS
        ]
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
    manifest = read_athlete_manifest(bucket, sub)
    next_version = (manifest["current_version"] + 1) if manifest else 1
    object_path = p_athlete_ver(sub, next_version)
    now = datetime.utcnow().isoformat() + "Z"

    payload = {
        "version": next_version,
        "is_current": True,
        "created_at": now,
        "created_by": created_by,
        "change_reason": change_reason or "profile update",
        "supersedes_version": next_version - 1 if next_version > 1 else None,
        "profile": profile,
    }
    bucket.blob(object_path).upload_from_string(
        json.dumps(payload, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    bucket.blob(p_athlete_manifest(sub)).upload_from_string(
        json.dumps({"current_version": next_version,
                    "gcs_object_path": object_path,
                    "updated_at": now}, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return payload


ATHLETE_HISTORY_LIMIT = 50


def read_athlete_history(bucket, sub, limit=ATHLETE_HISTORY_LIMIT):
    """Сводка по последним версиям профиля — для динамики веса и пульса.

    Каждая версия — отдельный объект в GCS, поэтому читаем ограниченное окно:
    у пользователя с сотнями сохранений полный обход упёрся бы в таймаут.
    """
    manifest = read_athlete_manifest(bucket, sub)
    if not manifest:
        return []
    current = manifest["current_version"]
    history = []
    for version in range(max(1, current - limit + 1), current + 1):
        blob = bucket.blob(p_athlete_ver(sub, version))
        if not blob.exists():
            continue
        data = json.loads(blob.download_as_text())
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
    blob = bucket.blob(p_races(sub))
    if not blob.exists():
        return []
    return json.loads(blob.download_as_text())


def write_races(bucket, sub, races):
    bucket.blob(p_races(sub)).upload_from_string(
        json.dumps(races, ensure_ascii=False, indent=2),
        content_type="application/json"
    )


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


def _fmt_duration(sec):
    if not sec or sec <= 0:
        return None
    sec = int(sec)
    if sec >= 3600:
        return f"{sec // 3600}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"
    return f"{sec // 60}:{sec % 60:02d}"


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


def read_run_details(bucket, sub, run_id):
    """Читает детали пробежки из namespace пользователя.
    Ленивый fallback: если в per-user namespace деталей нет, но есть legacy
    (глобальные) — копирует их в namespace и возвращает. Вызывающий обязан
    заранее убедиться, что run_id принадлежит этому пользователю (есть в его
    runs.json) — иначе ленивый fallback мог бы утащить чужие данные.
    """
    man_blob = bucket.blob(p_run_manifest(sub, run_id))
    if man_blob.exists():
        manifest = json.loads(man_blob.download_as_text())
        details_blob = bucket.blob(manifest["gcs_object_path"])
        return json.loads(details_blob.download_as_text()) if details_blob.exists() else None

    # Ленивый перенос legacy (только данные Alexander'а до multi-user)
    legacy_man = bucket.blob(legacy_run_manifest(run_id))
    if not legacy_man.exists():
        return None
    lman = json.loads(legacy_man.download_as_text())
    legacy_details = bucket.blob(lman["gcs_object_path"])
    if not legacy_details.exists():
        return None

    legacy_fit = bucket.blob(legacy_run_fit(run_id))
    if legacy_fit.exists():
        bucket.copy_blob(legacy_fit, bucket, p_run_fit(sub, run_id))
    bucket.copy_blob(legacy_details, bucket, p_run_details(sub, run_id))
    bucket.blob(p_run_manifest(sub, run_id)).upload_from_string(
        json.dumps({
            "current_version": 1,
            "gcs_object_path": p_run_details(sub, run_id),
            "updated_at": datetime.utcnow().isoformat() + "Z",
        }, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
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


def attach_fit_details_to_run(bucket, sub, run, fit_token):
    """Переносит tmp/{sub}/{token}/* → users/{sub}/runs/{id}/v1/* и обновляет run.
    Бизнес-записи пишутся как иммутабельные версии; tmp — ephemeral cleanup.
    """
    run_id = run["id"]
    tmp_fit = bucket.blob(p_tmp_fit(sub, fit_token))
    tmp_details = bucket.blob(p_tmp_details(sub, fit_token))
    if not tmp_fit.exists() or not tmp_details.exists():
        raise ValueError(f"FIT token expired or invalid: {fit_token}")

    raw_details = json.loads(tmp_details.download_as_text())
    summary = raw_details.get("summary", {}) or {}

    now = datetime.utcnow().isoformat() + "Z"
    fit_path = p_run_fit(sub, run_id)
    details_path = p_run_details(sub, run_id)

    bucket.copy_blob(tmp_fit, bucket, fit_path)

    final_details = {
        "version": 1,
        "is_current": True,
        "created_at": now,
        "source": "garmin_fit",
        "fit_object_path": fit_path,
        "date": raw_details.get("date"),
        "summary": summary,
        "laps": raw_details.get("laps", []),
        "samples": raw_details.get("samples", {}),
    }
    bucket.blob(details_path).upload_from_string(
        json.dumps(final_details, ensure_ascii=False, indent=2, default=str),
        content_type="application/json"
    )
    bucket.blob(p_run_manifest(sub, run_id)).upload_from_string(
        json.dumps({
            "current_version": 1,
            "gcs_object_path": details_path,
            "updated_at": now,
        }, ensure_ascii=False, indent=2),
        content_type="application/json"
    )

    tmp_fit.delete()
    tmp_details.delete()

    run["details_available"] = True
    run["max_hr"] = summary.get("max_hr")
    run["avg_cadence"] = summary.get("avg_cadence")
    run["total_ascent_m"] = summary.get("total_ascent_m")
    run["calories"] = summary.get("calories")


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


def read_plan_weeks(bucket, sub, plan_id):
    """Недели текущей версии плана; [] если плана/версии нет."""
    if not plan_id:
        return []
    manifest = read_plan_manifest(bucket, sub, plan_id)
    if not manifest:
        return []
    data = read_plan_version(bucket, manifest["gcs_object_path"])
    return (data or {}).get("weeks", [])


def write_plan_version(bucket, sub, plan_id, version, weeks, change_reason, created_by="api"):
    object_path = p_plan_ver(sub, plan_id, version)
    now = datetime.utcnow().isoformat() + "Z"

    payload = {
        "version": version,
        "is_current": True,
        "created_at": now,
        "created_by": created_by,
        "change_reason": change_reason or "",
        "supersedes_version": version - 1 if version > 1 else None,
        "weeks": weeks,
    }
    payload_str = json.dumps(payload, ensure_ascii=False, indent=2)
    checksum = hashlib.sha256(payload_str.encode()).hexdigest()

    # Записываем иммутабельную версию
    bucket.blob(object_path).upload_from_string(
        payload_str, content_type="application/json"
    )

    # Обновляем манифест (единственный перезаписываемый объект)
    manifest = {
        "current_version": version,
        "gcs_object_path": object_path,
        "created_at": now,
        "created_by": created_by,
        "change_reason": payload["change_reason"],
        "checksum": checksum,
    }
    bucket.blob(p_plan_manifest(sub, plan_id)).upload_from_string(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return {"version": version, "gcs_object_path": object_path}


def save_plan_weeks(bucket, sub, plan_id, weeks, change_reason="", created_by="api"):
    """Пишет следующую версию недель плана."""
    manifest = read_plan_manifest(bucket, sub, plan_id)
    next_version = (manifest["current_version"] + 1) if manifest else 1
    return write_plan_version(bucket, sub, plan_id, next_version, weeks,
                              change_reason, created_by)


# ── Plans registry (#25: несколько планов на пользователя) ────────────────────

PLAN_META_FIELDS = ("race_name", "race_date", "target_time", "plan_start")


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


def write_plans_index(bucket, sub, index):
    bucket.blob(p_plans_index(sub)).upload_from_string(
        json.dumps(index, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return index


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


def create_plan(bucket, sub, meta, make_active=True):
    index = read_plans_index(bucket, sub)
    plan = {
        "id": _new_plan_id(),
        "created_at": datetime.utcnow().isoformat() + "Z",
        "archived": False,
        **{f: (meta.get(f) or "") for f in PLAN_META_FIELDS},
    }
    index["plans"].append(plan)
    if make_active or not index.get("active_plan_id"):
        index["active_plan_id"] = plan["id"]
    write_plans_index(bucket, sub, index)
    return plan


def set_active_plan(bucket, sub, plan_id):
    index = read_plans_index(bucket, sub)
    plan = find_plan(index, plan_id)
    if not plan or plan.get("archived"):
        return None
    index["active_plan_id"] = plan_id
    write_plans_index(bucket, sub, index)
    return plan


def update_plan_meta(bucket, sub, plan_id, meta):
    index = read_plans_index(bucket, sub)
    plan = find_plan(index, plan_id)
    if not plan:
        return None
    for f in PLAN_META_FIELDS:
        if f in meta:
            plan[f] = meta.get(f) or ""
    plan["updated_at"] = datetime.utcnow().isoformat() + "Z"
    write_plans_index(bucket, sub, index)
    return plan


def archive_plan(bucket, sub, plan_id):
    """Логическое удаление: archived=true. Объекты плана не удаляются."""
    index = read_plans_index(bucket, sub)
    plan = find_plan(index, plan_id)
    if not plan:
        return None
    plan["archived"] = True
    plan["archived_at"] = datetime.utcnow().isoformat() + "Z"
    if index.get("active_plan_id") == plan_id:
        remaining = active_plans(index)
        index["active_plan_id"] = remaining[0]["id"] if remaining else None
    write_plans_index(bucket, sub, index)
    return plan


def migrate_single_plan(bucket, sub):
    """Ленивая миграция одиночного плана (до #25) в реестр планов.

    Данные гонки берём из profile.json, текущие недели — из users/{sub}/plan/.
    Старые версии остаются по legacy-пути (append-only, ничего не удаляем):
    текущие недели переписываются как v1 нового плана.
    Всем существующим пробежкам проставляется plan_id.
    """
    index = _empty_plans_index()

    old_manifest_blob = bucket.blob(p_singleplan_manifest(sub))
    profile = read_legacy_race_profile(bucket, sub)
    has_profile = any(profile.get(f) for f in PLAN_META_FIELDS)

    if not old_manifest_blob.exists() and not has_profile:
        # Нечего мигрировать — пустой реестр
        return write_plans_index(bucket, sub, index)

    plan = {
        "id": _new_plan_id(),
        "created_at": datetime.utcnow().isoformat() + "Z",
        "archived": False,
        **{f: profile.get(f, "") for f in PLAN_META_FIELDS},
    }
    index["plans"].append(plan)
    index["active_plan_id"] = plan["id"]
    write_plans_index(bucket, sub, index)

    # Текущие недели старого плана → v1 нового
    if old_manifest_blob.exists():
        old_manifest = json.loads(old_manifest_blob.download_as_text())
        old_data = read_plan_version(bucket, old_manifest.get("gcs_object_path", ""))
        weeks = (old_data or {}).get("weeks", [])
        if weeks:
            write_plan_version(bucket, sub, plan["id"], 1, weeks,
                               "migrated from single plan (#25)", "auto-migrate")

    # Привязываем существующие пробежки к мигрированному плану
    runs = read_runs(bucket, sub)
    changed = False
    for r in runs:
        if not r.get("plan_id"):
            r["plan_id"] = plan["id"]
            changed = True
    if changed:
        write_runs(bucket, sub, runs)

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
    manifest = read_llm_manifest(bucket)
    next_version = (manifest["current_version"] + 1) if manifest else 1
    object_path = f"config/llm/v{next_version}/config.json"
    now = datetime.utcnow().isoformat() + "Z"

    payload = {
        "version": next_version,
        "is_current": True,
        "created_at": now,
        "created_by": created_by,
        "provider": provider,
        "model": model,
        "api_key": api_key,
        "effort": clean_effort(effort),
        "supersedes_version": next_version - 1 if next_version > 1 else None,
    }
    payload_str = json.dumps(payload, ensure_ascii=False, indent=2)

    bucket.blob(object_path).upload_from_string(
        payload_str, content_type="application/json"
    )

    new_manifest = {
        "current_version": next_version,
        "gcs_object_path": object_path,
        "updated_at": now,
        "provider": provider,
        "model": model,
    }
    bucket.blob(LLM_CONFIG_MANIFEST).upload_from_string(
        json.dumps(new_manifest, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return {"version": next_version, "provider": provider, "model": model,
            "effort": payload["effort"]}


# ── LLM clients (Anthropic / OpenAI / Deepseek) ──────────────────────────────

def _call_anthropic(model, api_key, system_prompt, user_prompt, max_tokens=1500):
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
            "messages": [{"role": "user", "content": user_prompt}],
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
                            max_tokens=None, effort=None):
    """Универсальный клиент для OpenAI и Deepseek (одинаковый протокол)."""
    budget = max_tokens or LLM_MAX_TOKENS
    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "response_format": {"type": "json_object"},
        "reasoning_effort": clean_effort(effort),
    }
    res = _post_chat(base_url, api_key, {**body, BUDGET_PARAM: budget})
    if res.status_code == 400 and BUDGET_PARAM in res.text:
        res = _post_chat(base_url, api_key, {**body, LEGACY_BUDGET_PARAM: budget})
    res.raise_for_status()
    data = res.json()
    text = data["choices"][0]["message"]["content"]
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


def call_llm(provider, model, api_key, system_prompt, user_prompt, effort=None):
    if provider == "anthropic":
        # Провайдер вне интерфейса (#38): ключа нет. Уровень рассуждения у
        # Anthropic задаётся не reasoning_effort, а output_config.effort —
        # прокинуть его сюда придётся вместе с возвратом провайдера.
        return _call_anthropic(model, api_key, system_prompt, user_prompt)
    if provider == "openai":
        return _call_openai_compatible("https://api.openai.com/v1", model, api_key,
                                       system_prompt, user_prompt, effort=effort)
    if provider == "deepseek":
        return _call_openai_compatible("https://api.deepseek.com/v1", model, api_key,
                                       system_prompt, user_prompt, effort=effort)
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


def current_plan_week_idx(plan_start=None, weeks_count=0):
    """0-based индекс текущей недели плана от его plan_start.
    Без plan_start — исторический дефолт 2026-05-10; без длины плана — 13 недель.
    """
    start = datetime(2026, 5, 10)
    if plan_start:
        try:
            start = datetime.strptime(plan_start[:10], "%Y-%m-%d")
        except (ValueError, TypeError):
            pass
    n = weeks_count if weeks_count else 13
    diff = (datetime.utcnow() - start).days // 7
    return max(0, min(n - 1, diff))


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


def build_llm_context(bucket, sub):
    """Собирает компактный богатый контекст для LLM: активный план и его пробежки."""
    active_plan = get_active_plan(bucket, sub)
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
    active_races.sort(key=lambda r: r.get("date", ""), reverse=True)
    last_races = active_races[:3]

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

    week_idx = current_plan_week_idx(active_plan.get("plan_start") if active_plan else None,
                                     len(plan) if plan else 0)
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

    return {
        "profile": profile,
        "profile_derived": compute_athlete_derived(profile),
        "profile_version": profile_version,
        "personal_bests": personal_bests(all_races),
        "last_runs": last_runs,
        "last_races": last_races,
        "current_week": current_week,
        "next_week": next_week,
        "week_idx": week_idx,
        "weeks_total": len(plan) if plan else 0,
        "plan_id": plan_id,
        "plan_version": plan_version,
        "race": {f: (active_plan or {}).get(f, "") for f in PLAN_META_FIELDS},
        "heuristics": {
            "avg_pace_min_per_km": avg_pace,
            "hard_or_bad_count": hard_count,
            "total_km_last_14": round(total_km, 1),
        },
    }


def read_advice_manifest(bucket, sub):
    blob = bucket.blob(p_advice_manifest(sub))
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def read_latest_advice(bucket, sub):
    manifest = read_advice_manifest(bucket, sub)
    if not manifest:
        return None
    blob = bucket.blob(manifest["gcs_object_path"])
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def read_advice_usage(bucket, sub):
    """Дневной счётчик вызовов /advise. Сбрасывается при смене даты."""
    today = datetime.utcnow().date().isoformat()
    blob = bucket.blob(p_advice_usage(sub))
    if not blob.exists():
        return {"date": today, "count": 0}
    data = json.loads(blob.download_as_text())
    if data.get("date") != today:
        return {"date": today, "count": 0}
    return data


def increment_advice_usage(bucket, sub):
    usage = read_advice_usage(bucket, sub)
    usage["count"] = usage.get("count", 0) + 1
    bucket.blob(p_advice_usage(sub)).upload_from_string(
        json.dumps(usage, ensure_ascii=False), content_type="application/json"
    )
    return usage


def write_advice_version(bucket, sub, recommendation, ctx, provider, model, input_tokens, output_tokens, llm_config_version, created_by="api"):
    manifest = read_advice_manifest(bucket, sub)
    next_version = (manifest["current_version"] + 1) if manifest else 1
    object_path = p_advice_ver(sub, next_version)
    now = datetime.utcnow().isoformat() + "Z"

    payload = {
        "version": next_version,
        "is_current": True,
        "created_at": now,
        "created_by": created_by,
        "based_on_runs": [r.get("id") for r in ctx["last_runs"]],
        "based_on_plan_id": ctx.get("plan_id"),
        "based_on_plan_version": ctx["plan_version"],
        "based_on_profile_version": ctx.get("profile_version", 0),
        "based_on_llm_config_version": llm_config_version,
        "provider": provider,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "recommendation": recommendation,
        "supersedes_version": next_version - 1 if next_version > 1 else None,
    }
    bucket.blob(object_path).upload_from_string(
        json.dumps(payload, ensure_ascii=False, indent=2),
        content_type="application/json"
    )

    new_manifest = {
        "current_version": next_version,
        "gcs_object_path": object_path,
        "updated_at": now,
    }
    bucket.blob(p_advice_manifest(sub)).upload_from_string(
        json.dumps(new_manifest, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return payload


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


def write_registry(bucket, registry):
    bucket.blob(USERS_REGISTRY).upload_from_string(
        json.dumps(registry, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    _registry_cache["data"] = registry
    _registry_cache["ts"] = time.time()


def append_user_event(bucket, sub, event, actor):
    """Append-only аудит переходов (register/approve/reject)."""
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
    bucket.blob(f"users/events/{ts}-{sub}-{event}.json").upload_from_string(
        json.dumps({
            "ts": datetime.utcnow().isoformat() + "Z",
            "sub": sub, "event": event, "actor": actor,
        }, ensure_ascii=False, indent=2),
        content_type="application/json"
    )


class RegistrationClosed(Exception):
    """Поднимается, когда лимит pending достигнут — новых не регистрируем."""


def resolve_user(bucket, token_info):
    """Находит/создаёт запись пользователя по Google sub. Возвращает запись реестра.
    Новый sub → pending (или approved+admin если email в ADMIN_EMAILS).
    Поднимает RegistrationClosed при переполнении pending.
    """
    sub = token_info.get("sub")
    email = token_info.get("email", "")
    name = token_info.get("name", "")
    registry = read_registry(bucket)
    users = registry.setdefault("users", {})

    if sub in users:
        return users[sub]

    now = datetime.utcnow().isoformat() + "Z"
    is_admin = email in ADMIN_EMAILS
    if not is_admin:
        pending = sum(1 for u in users.values() if u.get("status") == "pending")
        if pending >= MAX_PENDING:
            raise RegistrationClosed()

    rec = {
        "sub": sub, "email": email, "name": name,
        "status": "approved" if is_admin else "pending",
        "role": "admin" if is_admin else "user",
        "created_at": now, "updated_at": now,
        "approved_by": sub if is_admin else None,
    }
    users[sub] = rec
    write_registry(bucket, registry)
    append_user_event(bucket, sub, "register", sub)
    return rec


def set_user_status(bucket, target_sub, status, actor_sub):
    """Меняет статус пользователя (lifecycle-метаданные). Возвращает запись или None."""
    registry = read_registry(bucket)
    users = registry.get("users", {})
    if target_sub not in users:
        return None
    rec = users[target_sub]
    rec["status"] = status
    rec["updated_at"] = datetime.utcnow().isoformat() + "Z"
    if status == "approved":
        rec["approved_by"] = actor_sub
    write_registry(bucket, registry)
    append_user_event(bucket, target_sub, status, actor_sub)
    return rec


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
            if bucket.blob(dst).exists():
                report["skipped"].append(f"{dst} (уже есть)")
                return
            bucket.copy_blob(src_blob, bucket, dst)
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
        if bucket.blob(dst_manifest_path).exists():
            report["skipped"].append(f"{dst_manifest_path} (уже есть)")
        else:
            new_man = dict(man)
            new_man["gcs_object_path"] = ver_dst(cur)
            bucket.blob(dst_manifest_path).upload_from_string(
                json.dumps(new_man, ensure_ascii=False, indent=2),
                content_type="application/json"
            )
            report["copied"].append(dst_manifest_path)

    copy_obj(LEGACY_RUNS, p_runs(sub))
    copy_obj(LEGACY_RACES, p_races(sub))
    # Сид профиля Alexander'а (исторические константы приложения)
    if bucket.blob(p_profile(sub)).exists():
        report["skipped"].append(f"{p_profile(sub)} (уже есть)")
    else:
        write_legacy_race_profile(bucket, sub, {
            "race_name": "Полумарафон", "race_date": "2026-08-09",
            "target_time": "1:40", "plan_start": "2026-05-10",
        })
        report["copied"].append(p_profile(sub))
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
