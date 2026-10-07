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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

import httpx
from fitparse import FitFile
from google.cloud import storage as gcs

from config import (ADMIN_EMAILS, BUCKET_NAME, LLM_CONFIG_MANIFEST,
                    LLM_DEFAULT_EFFORT, LLM_EFFORT_LEVELS, LLM_MAX_TOKENS,
                    MAX_PENDING, REGISTRY_TTL_SEC, USERS_REGISTRY)
from compliance import (DEFAULT as UNDATED, anchor_source,
                        current_week_idx, plan_compliance,
                        planned_for_date, to_date, week_days, week_window)
from domain import HR_ZONE_BOUNDS, PLAN_DAYS, TYPE_LABELS, personal_bests
from llm_prompt import (coach_chat_data, coach_chat_instructions,
                        format_context_for_llm, format_plan_window,
                        format_run_focus)

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


class PlanStale(Exception):
    """Правка сделана поверх версии плана, которая уже не текущая."""


def save_plan_weeks(bucket, sub, plan_id, weeks, change_reason="", created_by="api",
                    base_version=None):
    """Пишет следующую версию недель плана.

    base_version — версия, поверх которой сделана правка (0 — недель ещё не
    было). Текущая уже другая → PlanStale, ничего не пишется. None — без
    сверки: так пишет фронтенд, загруженный до #51.

    Сверка и запись идут не атомарно: два одновременных запроса с одной
    base_version пройдут оба. Закрыто окно «прочитал — правил — записал»;
    полное решение — предусловия записи (#35).
    """
    manifest = read_plan_manifest(bucket, sub, plan_id)
    current = manifest["current_version"] if manifest else 0
    if base_version is not None and base_version != current:
        raise PlanStale("plan_stale")
    return write_plan_version(bucket, sub, plan_id, current + 1, weeks,
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


# Сэмпл «держит» время до следующего. Разрыв длиннее — часы стояли на паузе,
# и записывать его в зону последнего пульса нельзя.
ZONE_GAP_CAP_SEC = 30


def hr_zone_minutes(details, zones):
    """Время в пульсовых зонах по сэмплам: [{name, min, pct}]. [] — считать не из чего.

    zones — как в compute_athlete_derived: [{name, from, to}] по возрастанию.
    Пульс ниже первой зоны идёт отдельной строкой, а не теряется: иначе
    проценты остальных зон завышены.
    """
    samples = details.get("samples") or {}
    times, hrs = samples.get("t_offset_sec") or [], samples.get("hr") or []
    if not zones or len(times) < 2 or len(times) != len(hrs):
        return []

    seconds = [0.0] * (len(zones) + 1)          # [0] — ниже первой зоны
    for i in range(len(times) - 1):
        span = min(times[i + 1] - times[i], ZONE_GAP_CAP_SEC)
        if not hrs[i] or span <= 0:
            continue
        slot = sum(1 for zone in zones if hrs[i] >= zone["from"])
        seconds[slot] += span

    total = sum(seconds)
    if total <= 0:
        return []
    names = ["ниже " + zones[0]["name"].split()[0]] + [zone["name"] for zone in zones]
    return [{"name": name, "min": round(sec / 60, 1), "pct": round(sec / total * 100)}
            for name, sec in zip(names, seconds) if sec > 0]


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


def increment_advice_usage(bucket, sub):
    usage = read_advice_usage(bucket, sub)
    usage["count"] = usage.get("count", 0) + 1
    bucket.blob(p_advice_usage(sub)).upload_from_string(
        json.dumps(usage, ensure_ascii=False), content_type="application/json"
    )
    return usage


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


def _fresh_registry(bucket):
    """Реестр мимо кэша. Кэш живёт REGISTRY_TTL_SEC на каждом инстансе, и
    запись поверх устаревшей копии затёрла бы чужое изменение."""
    data = _load_registry(bucket)
    _registry_cache["data"] = data
    _registry_cache["ts"] = time.time()
    return data


def append_user_event(bucket, sub, event, actor, details=None):
    """Append-only аудит переходов (register/approve/reject, тренер #44)."""
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%S%f")
    payload = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "sub": sub, "event": event, "actor": actor,
    }
    if details:
        payload["details"] = details
    bucket.blob(f"users/events/{ts}-{sub}-{event}.json").upload_from_string(
        json.dumps(payload, ensure_ascii=False, indent=2),
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

    # Записи в кэше нет — перед записью берём свежий реестр (см. _fresh_registry).
    registry = _fresh_registry(bucket)
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
    registry = _fresh_registry(bucket)
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
    registry = _fresh_registry(bucket)
    users = registry.get("users", {})
    rec = users.get(target_sub)
    if rec is None:
        return None

    now = datetime.utcnow().isoformat() + "Z"
    rec["is_coach"] = bool(is_coach)
    rec["updated_at"] = now
    released = []
    if not is_coach:
        for user in users.values():
            if user.get("coach_sub") == target_sub:
                user["coach_sub"] = None
                user["updated_at"] = now
                released.append(user["sub"])

    write_registry(bucket, registry)
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
    registry = _fresh_registry(bucket)
    users = registry.get("users", {})
    rec = users.get(sub)
    if rec is None:
        raise CoachLinkError("user_not_found")
    if coach_sub:
        if coach_sub == sub:
            raise CoachLinkError("cannot_coach_yourself")
        if not _is_active_coach(users.get(coach_sub)):
            raise CoachLinkError("not_a_coach")

    previous = rec.get("coach_sub")
    if previous == coach_sub:
        return rec

    rec["coach_sub"] = coach_sub
    rec["updated_at"] = datetime.utcnow().isoformat() + "Z"
    write_registry(bucket, registry)
    append_user_event(bucket, sub, "coach_select" if coach_sub else "coach_clear", sub,
                      {"coach_sub": coach_sub, "previous": previous})
    return rec


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


def coach_can_access(bucket, coach_sub, athlete_sub):
    """Единственная проверка права тренера на данные спортсмена.

    Реестр читается мимо кэша: спортсмен, снявший тренера, закрывает доступ
    сразу, а не через REGISTRY_TTL_SEC на соседнем инстансе.
    """
    users = _fresh_registry(bucket).get("users", {})
    return (_is_active_coach(users.get(coach_sub))
            and _is_coached_by(users.get(athlete_sub), coach_sub))


def list_athletes_of(bucket, coach_sub):
    """Спортсмены тренера: [{sub, name}]. None — пользователь не тренер."""
    users = _fresh_registry(bucket).get("users", {})
    if not _is_active_coach(users.get(coach_sub)):
        return None
    athletes = [{"sub": u["sub"], "name": u.get("name") or u.get("email") or "Спортсмен"}
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


def _load_json_objects(bucket, paths):
    """Объект на сообщение — это запрос на сообщение; читаем параллельно."""
    def load(path):
        return json.loads(bucket.blob(path).download_as_text())

    if len(paths) > 1:
        with ThreadPoolExecutor(max_workers=8) as pool:
            return list(pool.map(load, paths))
    return [load(path) for path in paths]


def _chat_ids(bucket, athlete, coach):
    return _message_ids(bucket, p_chat_prefix(athlete, coach), _CHAT_ID_RE)


def append_chat_message(bucket, athlete, coach, from_sub, role, text):
    """Пишет сообщение новым объектом и возвращает его."""
    text = text.strip() if isinstance(text, str) else ""
    if not text:
        raise ChatError("empty_message")
    if len(text) > CHAT_TEXT_MAX:
        raise ChatError("message_too_long")
    msg_id, now = _next_message_id(_chat_ids(bucket, athlete, coach), role)
    message = {"id": msg_id, "ts": now.isoformat() + "Z",
               "from_sub": from_sub, "from_role": role, "text": text}
    bucket.blob(p_chat_msg(athlete, coach, msg_id)).upload_from_string(
        json.dumps(message, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return message


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
    current = _chat_last_read(bucket, athlete, coach, reader_role)
    if not ids:
        return current
    target = min(last_id, ids[-1]) if last_id else ids[-1]
    if target <= current:
        return current
    bucket.blob(p_chat_read(athlete, coach, reader_role)).upload_from_string(
        json.dumps({"last_read_id": target,
                    "updated_at": datetime.utcnow().isoformat() + "Z"},
                   ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return target


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
    now = datetime.utcnow()
    title = " ".join(str(title or "").split())[:AI_TITLE_MAX]
    thread = {
        "id": f"{now.strftime(_AI_ID_TIME)}-{secrets_mod.token_hex(4)}",
        "kind": "run" if run else "general",
        "run_id": run["id"] if run else None,
        "title": title or (run_title(run) if run else "Разбор"),
        "status": "active",
        "created_at": now.isoformat() + "Z",
        "created_by": created_by,
    }
    bucket.blob(p_ai_thread(sub, thread["id"])).upload_from_string(
        json.dumps(thread, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return thread


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
    """Скрывает разбор из списка. False — скрывать нечего."""
    if not read_ai_thread(bucket, sub, thread_id):
        return False
    bucket.blob(p_ai_archived(sub, thread_id)).upload_from_string(
        json.dumps({"archived_at": datetime.utcnow().isoformat() + "Z",
                    "archived_by": archived_by}, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return True


def append_ai_message(bucket, sub, thread_id, role, text, extra=None):
    """Пишет реплику разбора новым объектом и возвращает её."""
    prefix = p_ai_msg_prefix(sub, thread_id)
    msg_id, now = _next_message_id(_message_ids(bucket, prefix, _AI_MSG_ID_RE), role)
    message = {"id": msg_id, "ts": now.isoformat() + "Z", "role": role,
               "text": text, **(extra or {})}
    bucket.blob(p_ai_msg(sub, thread_id, msg_id)).upload_from_string(
        json.dumps(message, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return message


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
             "zones": [], "hr_max": None, "hr_max_estimated": False, "planned": None}

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
    новому вопросу.
    """
    ctx = build_llm_context(bucket, sub)
    own = {r.get("id"): r for r in read_runs(bucket, sub) if not r.get("deleted", False)}
    focus_ids = [rid for rid in ai_focus_run_ids(thread, messages, run and run["id"])
                 if rid in own]       # скрытая после разбора пробежка выпадает молча
    blocks = [format_run_focus(build_run_focus(bucket, sub, own[rid], ctx))
              for rid in focus_ids]
    window = ai_plan_window(ctx["plan_weeks"], ctx["plan_start"], ctx["week_idx"])
    instructions = coach_chat_instructions(with_proposals=bool(window))
    data = coach_chat_data(format_context_for_llm(ctx), blocks,
                           format_plan_window(window) if window else "")
    return {
        "ctx": ctx,
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

    Проверка «не применено ли уже» и запись идут не атомарно: два
    одновременных запроса создадут две одинаковые версии плана подряд.
    Данные при этом не теряются; полное решение — предусловия записи (#35).
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
                                base_version=version)
    except PlanStale:
        # План переписали между сверкой выше и записью.
        raise AICoachError("proposal_stale")
    marker = {
        "thread_id": thread_id, "message_id": msg_id,
        "applied_at": datetime.utcnow().isoformat() + "Z", "applied_by": applied_by,
        "plan_id": plan_id, "from_version": version, "to_version": saved["version"],
    }
    marker_blob.upload_from_string(json.dumps(marker, ensure_ascii=False, indent=2),
                                   content_type="application/json")
    return marker


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
