import hashlib
import io
import json
import os
import re
import secrets as secrets_mod
import time
import httpx
import functions_framework
from google.cloud import storage
from datetime import datetime
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from fitparse import FitFile

CLIENT_ID = "463368957110-f1649h2mjd1hbkj5307jllcv3e0hslbc.apps.googleusercontent.com"

# Кто получает role=admin при первом логине. Остальные — pending до одобрения.
ADMIN_EMAILS = {"aabramov77@gmail.com"}

BUCKET_NAME = os.environ.get("BUCKET_NAME", "running-tracker-aabramov77")

# Глобальные (не per-user) объекты
USERS_REGISTRY = "users/registry.json"
LLM_CONFIG_MANIFEST = "config/llm/manifest.json"   # общий ключ LLM (управляет админ)

# Лимиты
REGISTRY_TTL_SEC = 30       # кэш реестра в памяти тёплого инстанса
MAX_PENDING = 50            # защита от наполнения реестра неодобренными
DAILY_ADVISE_LIMIT = 10     # вызовов /advise на пользователя в сутки (общий ключ)
ADMIN_DAILY_ADVISE_LIMIT = 100

RACE_DATE = "2026-08-09"
RACE_TARGET_TIME = "1:40"
RACE_TARGET_PACE = "4:44/км"
RACE_DISTANCE_KM = 21.0975

INITIAL_PLAN = [
    {"w":1,"start":"10.05","end":"16.05","accent":"Развитие","type":"dev","sun":"12 км легко","mon":"6–8 км легко, пульс 130–140","wed":"3×7 мин по 4:35–4:40","fri":"8–10 км средний 5:30–5:40","sat":"8 км по 5:05–5:15"},
    {"w":2,"start":"17.05","end":"23.05","accent":"Развитие","type":"dev","sun":"14–16 км легко","mon":"7–8 км легко","wed":"6×1 км по 4:30–4:35","fri":"10 км средний","sat":"4×2 км по 4:48–4:50"},
    {"w":3,"start":"24.05","end":"30.05","accent":"Подводка + 10 км","type":"race","sun":"10–12 км очень легко","mon":"8 км легко","wed":"4×1 км по 4:30–4:35","fri":"6–8 км очень легко","sat":"СТАРТ 10 км"},
    {"w":4,"start":"31.05","end":"06.06","accent":"Разгрузка","type":"load","sun":"18 км легко, пульс 140–150","mon":"6 км очень легко","wed":"4×1 км по 4:35–4:40","fri":"8–10 км легко","sat":"6–8 км по 4:55–5:00"},
    {"w":5,"start":"07.06","end":"13.06","accent":"Развитие","type":"dev","sun":"14–16 км легко","mon":"8 км легко","wed":"4×2 км по 4:32–4:38","fri":"10–11 км средний","sat":"2×4 км по 4:48–4:50"},
    {"w":6,"start":"14.06","end":"20.06","accent":"Развитие","type":"dev","sun":"18–20 км легко","mon":"8–9 км легко","wed":"3×3 км по 4:35–4:40","fri":"11–12 км средний","sat":"10 км по 4:50"},
    {"w":7,"start":"21.06","end":"27.06","accent":"Развитие","type":"dev","sun":"20 км, прогрессия к 5:10","mon":"8–9 км легко","wed":"Пирамида 1+2+3+2+1 км","fri":"10–11 км средний","sat":"2×5 км по 4:48–4:50"},
    {"w":8,"start":"28.06","end":"04.07","accent":"Подводка + 10 км","type":"race","sun":"12–14 км очень легко","mon":"6–7 км легко","wed":"4×1 км по 4:30–4:35","fri":"6–8 км очень легко","sat":"СТАРТ 10 км"},
    {"w":9,"start":"05.07","end":"11.07","accent":"Пик формы","type":"peak","sun":"16 км легко","mon":"8 км легко","wed":"5×1 км по 4:25–4:30","fri":"11 км средний","sat":"12 км по 4:48–4:50"},
    {"w":10,"start":"12.07","end":"18.07","accent":"Пик формы","type":"peak","sun":"20 км с прогрессией","mon":"8–9 км легко","wed":"3×3 км по 4:32–4:38","fri":"10–11 км средний","sat":"3×3 км по 4:44–4:48"},
    {"w":11,"start":"19.07","end":"25.07","accent":"Пик формы","type":"peak","sun":"18 км легко","mon":"8 км легко","wed":"5×1 км по 4:25–4:30","fri":"10 км средний","sat":"10–12 км по 4:48–4:50"},
    {"w":12,"start":"26.07","end":"01.08","accent":"Тейпер","type":"taper","sun":"14–16 км легко","mon":"6–7 км легко","wed":"6×400 м по 4:00–4:10","fri":"6–8 км легко","sat":"4–6 км по 4:44–4:48"},
    {"w":13,"start":"02.08","end":"08.08","accent":"Тейпер + ПМ","type":"taper","sun":"СТАРТ 21,1 км","mon":"5–6 км легко","wed":"4×400 м бодро","fri":"4–5 км очень легко","sat":"20–25 мин + ускорения"},
]


def get_storage_client():
    return storage.Client()


# ── Per-user path builders (единственная точка построения путей) ──────────────
# Правило: ни одна data-функция не обращается к bucket без sub. Все пути — через p_*.

def upfx(sub):                 return f"users/{sub}/"
def p_runs(sub):               return f"{upfx(sub)}runs.json"
def p_races(sub):              return f"{upfx(sub)}races.json"
def p_plan_manifest(sub):      return f"{upfx(sub)}plan/manifest.json"
def p_plan_ver(sub, v):        return f"{upfx(sub)}plan/v{v}/plan.json"
def p_advice_manifest(sub):    return f"{upfx(sub)}advice/manifest.json"
def p_advice_ver(sub, v):      return f"{upfx(sub)}advice/v{v}/recommendation.json"
def p_advice_usage(sub):       return f"{upfx(sub)}advice/usage.json"
def p_profile(sub):            return f"{upfx(sub)}profile.json"
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


# ── Profile helpers (per-user: гонка, цель, старт плана) ──────────────────────

PROFILE_DEFAULT = {"race_name": "", "race_date": "", "target_time": "", "plan_start": ""}
PROFILE_FIELDS = ("race_name", "race_date", "target_time", "plan_start")


def read_profile(bucket, sub):
    blob = bucket.blob(p_profile(sub))
    if not blob.exists():
        return dict(PROFILE_DEFAULT)
    data = json.loads(blob.download_as_text())
    return {**PROFILE_DEFAULT, **{k: data.get(k, "") for k in PROFILE_FIELDS}}


def write_profile(bucket, sub, profile):
    clean = {k: (profile.get(k) or "") for k in PROFILE_FIELDS}
    bucket.blob(p_profile(sub)).upload_from_string(
        json.dumps(clean, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return clean


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


# ── Plan helpers ──────────────────────────────────────────────────────────────

def read_plan_manifest(bucket, sub):
    blob = bucket.blob(p_plan_manifest(sub))
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def read_plan_version(bucket, object_path):
    blob = bucket.blob(object_path)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def write_plan_version(bucket, sub, version, weeks, change_reason, created_by="api"):
    object_path = p_plan_ver(sub, version)
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
    bucket.blob(p_plan_manifest(sub)).upload_from_string(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return {"version": version, "gcs_object_path": object_path}


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


def write_llm_config_version(bucket, provider, model, api_key, created_by="aabramov77"):
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
    return {"version": next_version, "provider": provider, "model": model}


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


def _call_openai_compatible(base_url, model, api_key, system_prompt, user_prompt, max_tokens=1500):
    """Универсальный клиент для OpenAI и Deepseek (одинаковый протокол)."""
    res = httpx.post(
        f"{base_url}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
        },
        timeout=60.0,
    )
    res.raise_for_status()
    data = res.json()
    text = data["choices"][0]["message"]["content"]
    usage = data.get("usage", {})
    return {
        "text": text,
        "input_tokens": usage.get("prompt_tokens", 0),
        "output_tokens": usage.get("completion_tokens", 0),
    }


def call_llm(provider, model, api_key, system_prompt, user_prompt):
    if provider == "anthropic":
        return _call_anthropic(model, api_key, system_prompt, user_prompt)
    if provider == "openai":
        return _call_openai_compatible("https://api.openai.com/v1", model, api_key, system_prompt, user_prompt)
    if provider == "deepseek":
        return _call_openai_compatible("https://api.deepseek.com/v1", model, api_key, system_prompt, user_prompt)
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

PLAN_PHASE_LABELS = {
    "dev": "Развитие",
    "peak": "Пик формы",
    "taper": "Тейпер",
    "load": "Разгрузка",
    "race": "Соревнование (подводка)",
}

FEEL_LABELS = {
    "great": "отлично",
    "good": "хорошо",
    "ok": "нормально",
    "hard": "тяжело",
    "bad": "плохо",
}

TYPE_LABELS = {
    "easy": "лёгкий",
    "interval": "интервалы",
    "tempo": "темповый",
    "long": "длительный",
    "race": "соревнование",
    "recovery": "восстановительный",
}

DIST_LABEL_KM = {"4.2km": 4.2, "5km": 5, "10km": 10, "HM": 21.0975, "M": 42.195}


def current_plan_week_idx():
    """0-based индекс текущей недели от 2026-05-10."""
    diff = (datetime.utcnow() - datetime(2026, 5, 10)).days // 7
    return max(0, min(12, diff))


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
    """Собирает компактный богатый контекст для LLM (данные пользователя sub)."""
    # Runs
    all_runs = read_runs(bucket, sub)
    active_runs = [r for r in all_runs if not r.get("deleted", False)]
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

    # Races
    all_races = read_races(bucket, sub)
    active_races = [r for r in all_races if not r.get("deleted", False)]
    active_races.sort(key=lambda r: r.get("date", ""), reverse=True)
    last_races = active_races[:3]

    # Plan
    plan_manifest = read_plan_manifest(bucket, sub)
    plan = None
    plan_version = None
    if plan_manifest:
        plan_data = read_plan_version(bucket, plan_manifest["gcs_object_path"])
        if plan_data:
            plan = plan_data["weeks"]
            plan_version = plan_data["version"]

    week_idx = current_plan_week_idx()
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
        "last_runs": last_runs,
        "last_races": last_races,
        "current_week": current_week,
        "next_week": next_week,
        "week_idx": week_idx,
        "plan_version": plan_version,
        "heuristics": {
            "avg_pace_min_per_km": avg_pace,
            "hard_or_bad_count": hard_count,
            "total_km_last_14": round(total_km, 1),
        },
    }


# Порядок и подписи дней недели плана (7 дней, Пн→Вс). #23
PLAN_DAYS = [("mon", "пн"), ("tue", "вт"), ("wed", "ср"), ("thu", "чт"),
             ("fri", "пт"), ("sat", "сб"), ("sun", "вс")]


def _week_days_str(week):
    """Строка тренировок недели по дням; пустые/отсутствующие дни пропускаются."""
    parts = [f"{label}={week.get(field)}" for field, label in PLAN_DAYS if week.get(field)]
    return "; ".join(parts) if parts else "(пусто)"


def format_context_for_llm(ctx):
    """Превращает контекст в текстовый user prompt."""
    lines = []
    lines.append(f"Цель: полумарафон {RACE_DATE}, {RACE_TARGET_TIME} (темп {RACE_TARGET_PACE})")
    lines.append(f"Сегодня: {datetime.utcnow().date().isoformat()}")
    lines.append(f"Текущая неделя плана: {ctx['week_idx'] + 1} из 13")

    cw = ctx.get("current_week")
    if cw:
        phase = PLAN_PHASE_LABELS.get(cw.get("type"), cw.get("type"))
        lines.append(f"Фаза: {phase} — {cw.get('accent', '')}")
        lines.append("План текущей недели:")
        lines.append("  " + _week_days_str(cw))
    nw = ctx.get("next_week")
    if nw:
        lines.append("План следующей недели:")
        lines.append("  " + _week_days_str(nw))

    lines.append("")
    lines.append("Последние 14 тренировок (сначала свежие):")
    for r in ctx["last_runs"]:
        t = TYPE_LABELS.get(r.get("type"), r.get("type", ""))
        feel = FEEL_LABELS.get(r.get("feel"), "")
        parts = [r.get("date", "?"), t, f"{r.get('dist', '?')}км"]
        if r.get("time"): parts.append(r["time"])
        if r.get("pace"): parts.append(f"темп {r['pace']}/км")
        if r.get("hr"): parts.append(f"пульс ср.{r['hr']}")
        if r.get("max_hr"): parts.append(f"макс {r['max_hr']}")
        if r.get("avg_cadence"): parts.append(f"каденс {r['avg_cadence']}")
        if r.get("total_ascent_m"): parts.append(f"набор {r['total_ascent_m']}м")
        if feel: parts.append(f"ощ:{feel}")
        line = "  - " + " ".join(parts)
        if r.get("_lap_paces"):
            line += f"\n    лапы: {r['_lap_paces']}"
        if r.get("_hr_drift_pct") is not None:
            d = r["_hr_drift_pct"]
            sign = "+" if d >= 0 else ""
            line += f"\n    HR-drift: {sign}{d}% (изменение среднего пульса 1-я→2-я половина)"
        if r.get("notes"):
            line += f"\n    заметки: {r['notes']}"
        lines.append(line)

    if ctx["last_races"]:
        lines.append("")
        lines.append("Последние забеги:")
        for race in ctx["last_races"]:
            label = race.get("dist_label", "")
            km = DIST_LABEL_KM.get(label, "?")
            lines.append(f"  - {race.get('date', '?')} {race.get('name', '?')} {km}км {race.get('time', '?')}")

    h = ctx["heuristics"]
    lines.append("")
    lines.append("Эвристики:")
    if h["avg_pace_min_per_km"] is not None:
        ap = h["avg_pace_min_per_km"]
        m = int(ap); s = round((ap - m) * 60)
        lines.append(f"  - средний темп за 14 тренировок: {m}:{s:02d}/км")
    lines.append(f"  - тяжёлых/плохих тренировок: {h['hard_or_bad_count']}")
    lines.append(f"  - суммарно: {h['total_km_last_14']} км")

    return "\n".join(lines)


SYSTEM_PROMPT = """Ты опытный беговой тренер. Анализируешь данные тренировок бегуна, готовящегося к полумарафону.

Дай рекомендации СТРОГО в JSON-формате без лишнего текста до или после:
{
  "assessment": "1-2 предложения общей оценки прогресса",
  "adjustments": [
    {"day": "среда", "change": "конкретное предложение по корректировке"}
  ],
  "warnings": ["предупреждение если есть риски"]
}

Если корректировок не нужно — пустой массив adjustments. Если предупреждений нет — пустой массив warnings.
Отвечай на русском языке."""


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
        "based_on_plan_version": ctx["plan_version"],
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
        write_profile(bucket, sub, {
            "race_name": "Полумарафон", "race_date": "2026-08-09",
            "target_time": "1:40", "plan_start": "2026-05-10",
        })
        report["copied"].append(p_profile(sub))
    migrate_versioned(
        LEGACY_PLAN_MANIFEST,
        lambda v: f"plan/v{v}/plan.json", lambda v: p_plan_ver(sub, v),
        p_plan_manifest(sub))
    migrate_versioned(
        LEGACY_ADVICE_MANIFEST,
        lambda v: f"advice/v{v}/recommendation.json", lambda v: p_advice_ver(sub, v),
        p_advice_manifest(sub))
    return report


# ── Auth ───────────────────────────────────────────────────────────────────────

def verify_token(request):
    """Проверяет подпись Google ID-токена. Возвращает info (sub/email/name) или None.
    Авторизация (кто допущен) решается отдельно через реестр — resolve_user.
    """
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        return id_token.verify_oauth2_token(token, google_requests.Request(), CLIENT_ID)
    except Exception:
        return None


# ── HTTP handler ──────────────────────────────────────────────────────────────

@functions_framework.http
def runs_api(request):
    headers = {
        "Access-Control-Allow-Origin": "*",
        "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
    }
    jhead = {**headers, "Content-Type": "application/json"}

    def jresp(obj, code):
        return (json.dumps(obj, ensure_ascii=False, default=str), code, jhead)

    if request.method == "OPTIONS":
        return ("", 204, headers)

    token_info = verify_token(request)
    if not token_info:
        return jresp({"error": "Unauthorized"}, 401)

    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)

    try:
        user = resolve_user(bucket, token_info)
    except RegistrationClosed:
        return jresp({"error": "registration_closed"}, 403)
    sub = user["sub"]
    is_admin = user["role"] == "admin"

    path = request.path.rstrip("/") or "/"

    # /me — доступен любому валидному токену (даже pending/rejected)
    if path == "/me":
        return jresp({"status": user["status"], "role": user["role"],
                      "email": user.get("email"), "name": user.get("name")}, 200)

    # Не одобрен → 403 на всё остальное
    if user["status"] != "approved":
        return jresp({
            "error": "pending_approval" if user["status"] == "pending" else "rejected",
            "status": user["status"],
        }, 403)

    try:
        # ── admin эндпоинты ──────────────────────────────────────────────────
        if path.startswith("/admin/"):
            if not is_admin:
                return jresp({"error": "forbidden"}, 403)

            if path == "/admin/users" and request.method == "GET":
                reg = read_registry(bucket)
                return jresp({"users": list(reg.get("users", {}).values())}, 200)

            if path in ("/admin/users/approve", "/admin/users/reject") and request.method == "POST":
                body = request.get_json(silent=True) or {}
                target = body.get("sub")
                if not target:
                    return jresp({"error": "Missing sub"}, 400)
                status = "approved" if path.endswith("approve") else "rejected"
                rec = set_user_status(bucket, target, status, sub)
                if not rec:
                    return jresp({"error": "user not found"}, 404)
                return jresp({"ok": True, "user": rec}, 200)

            if path == "/admin/migrate-legacy" and request.method == "POST":
                return jresp(migrate_legacy_to_user(bucket, sub), 200)

            return jresp({"error": "Not found"}, 404)

        # ── /runs/parse-fit ──────────────────────────────────────────────────
        if path == "/runs/parse-fit":
            if request.method != "POST":
                return jresp({"error": "Method not allowed"}, 405)
            fit_file = request.files.get("fit") if request.files else None
            if not fit_file:
                return jresp({"error": "No 'fit' file in multipart upload"}, 400)
            try:
                fit_bytes = fit_file.read()
                parsed = parse_fit_file(fit_bytes)
            except Exception as e:
                return jresp({"error": f"FIT parse failed: {str(e)[:300]}"}, 400)

            if not parsed.get("summary", {}).get("dist_km"):
                return jresp({"error": "FIT file has no session/distance data — not a valid activity?"}, 400)

            try:
                cleanup_old_tmp(bucket, sub, max_age_hours=24)
            except Exception:
                pass  # best-effort

            token = write_parsed_fit_to_tmp(bucket, sub, fit_bytes, parsed)
            summary = parsed.get("summary", {})
            return jresp({
                "fit_token": token,
                "date": parsed.get("date"),
                "dist": summary.get("dist_km"),
                "time": _fmt_duration(summary.get("duration_sec")),
                "pace": _fmt_pace(summary.get("avg_pace_sec_per_km")),
                "hr": summary.get("avg_hr"),
                "max_hr": summary.get("max_hr"),
                "avg_cadence": summary.get("avg_cadence"),
                "total_ascent_m": summary.get("total_ascent_m"),
                "calories": summary.get("calories"),
            }, 200)

        # ── /runs/{id}/details ───────────────────────────────────────────────
        details_match = re.match(r"^/runs/(\d+)/details$", path)
        if details_match:
            if request.method != "GET":
                return jresp({"error": "Method not allowed"}, 405)
            run_id = int(details_match.group(1))
            # Ownership: run_id должен быть в runs.json пользователя (иначе ленивый
            # fallback мог бы утащить чужие/legacy данные в чужой namespace).
            own_ids = {r.get("id") for r in read_runs(bucket, sub)}
            if run_id not in own_ids:
                return jresp({"error": "Run details not found"}, 404)
            details = read_run_details(bucket, sub, run_id)
            if not details:
                return jresp({"error": "Run details not found"}, 404)
            return jresp(details, 200)

        # ── /config/llm — только админ (общий ключ) ──────────────────────────
        if path == "/config/llm":
            if not is_admin:
                return jresp({"error": "forbidden"}, 403)
            if request.method == "GET":
                cfg = read_llm_config_full(bucket)
                if not cfg:
                    return jresp({"configured": False}, 200)
                return jresp({
                    "configured": True,
                    "version": cfg["version"],
                    "provider": cfg["provider"],
                    "model": cfg["model"],
                    "api_key_masked": mask_key(cfg.get("api_key", "")),
                    "updated_at": cfg.get("created_at"),
                }, 200)
            elif request.method == "POST":
                body = request.get_json(silent=True) or {}
                provider = body.get("provider")
                model = body.get("model")
                api_key = body.get("api_key", "").strip()
                if provider not in ("anthropic", "openai", "deepseek"):
                    return jresp({"error": "Invalid provider"}, 400)
                if not model:
                    return jresp({"error": "Missing model"}, 400)
                if not api_key:
                    return jresp({"error": "Missing api_key"}, 400)
                result = write_llm_config_version(bucket, provider, model, api_key, created_by=user.get("email", "api"))
                return jresp(result, 201)
            else:
                return jresp({"error": "Method not allowed"}, 405)

        if path == "/config/llm/test":
            if not is_admin:
                return jresp({"error": "forbidden"}, 403)
            if request.method != "POST":
                return jresp({"error": "Method not allowed"}, 405)
            cfg = read_llm_config_full(bucket)
            if not cfg:
                return jresp({"ok": False, "error": "LLM config not set"}, 400)
            try:
                t0 = datetime.utcnow()
                res = call_llm(
                    cfg["provider"], cfg["model"], cfg["api_key"],
                    "Ты помощник. Отвечай строго: {\"ok\":true}",
                    "Верни строго JSON {\"ok\":true}"
                )
                latency_ms = int((datetime.utcnow() - t0).total_seconds() * 1000)
                return jresp({
                    "ok": True, "latency_ms": latency_ms,
                    "input_tokens": res["input_tokens"],
                    "output_tokens": res["output_tokens"],
                    "sample_response": res["text"][:200],
                }, 200)
            except httpx.HTTPStatusError as e:
                return jresp({"ok": False, "error": f"Provider {e.response.status_code}: {e.response.text[:200]}"}, 200)
            except Exception as e:
                return jresp({"ok": False, "error": str(e)[:200]}, 200)

        # ── /advise — все approved; общий ключ; дневной лимит; per-user данные ─
        if path == "/advise":
            if request.method == "GET":
                latest = read_latest_advice(bucket, sub)
                if not latest:
                    return jresp({"available": False}, 200)
                return jresp({"available": True, **latest}, 200)

            elif request.method == "POST":
                cfg = read_llm_config_full(bucket)
                if not cfg or not cfg.get("api_key"):
                    return jresp({"error": "LLM config not set. Обратитесь к администратору."}, 400)
                limit = ADMIN_DAILY_ADVISE_LIMIT if is_admin else DAILY_ADVISE_LIMIT
                usage = read_advice_usage(bucket, sub)
                if usage.get("count", 0) >= limit:
                    return jresp({"error": "daily_limit_reached", "limit": limit}, 429)
                ctx = build_llm_context(bucket, sub)
                if not ctx["last_runs"]:
                    return jresp({"error": "Нужна хотя бы одна пробежка для рекомендаций"}, 400)
                user_prompt = format_context_for_llm(ctx)
                try:
                    llm_res = call_llm(cfg["provider"], cfg["model"], cfg["api_key"], SYSTEM_PROMPT, user_prompt)
                except httpx.HTTPStatusError as e:
                    return jresp({"error": f"Provider {e.response.status_code}: {e.response.text[:300]}"}, 502)
                except Exception as e:
                    return jresp({"error": f"LLM call failed: {str(e)[:300]}"}, 502)
                try:
                    recommendation = parse_llm_json(llm_res["text"])
                except Exception as e:
                    return jresp({"error": f"Cannot parse LLM response as JSON: {str(e)[:200]}",
                                  "raw_text": llm_res["text"][:500]}, 502)

                payload = write_advice_version(
                    bucket, sub, recommendation, ctx,
                    cfg["provider"], cfg["model"],
                    llm_res["input_tokens"], llm_res["output_tokens"],
                    cfg["version"], created_by=user.get("email", "api"))
                increment_advice_usage(bucket, sub)
                return jresp({"available": True, **payload}, 201)
            else:
                return jresp({"error": "Method not allowed"}, 405)

        # ── /profile — per-user (гонка, цель, старт плана) ────────────────────
        if path == "/profile":
            if request.method == "GET":
                return jresp(read_profile(bucket, sub), 200)
            elif request.method == "POST":
                body = request.get_json(silent=True) or {}
                return jresp(write_profile(bucket, sub, body), 200)
            else:
                return jresp({"error": "Method not allowed"}, 405)

        # ── /races ───────────────────────────────────────────────────────────
        if path == "/races":
            if request.method == "GET":
                active = [r for r in read_races(bucket, sub) if not r.get("deleted", False)]
                return jresp(active, 200)

            elif request.method == "POST":
                body = request.get_json(silent=True)
                if not body:
                    return jresp({"error": "Invalid JSON"}, 400)
                for field in ["name", "date", "dist_label", "time"]:
                    if field not in body:
                        return jresp({"error": f"Missing field: {field}"}, 400)
                race = {
                    "id": body.get("id", int(datetime.now().timestamp() * 1000)),
                    "name": body["name"], "date": body["date"],
                    "dist_label": body["dist_label"], "time": body["time"],
                    "deleted": False,
                }
                all_races = read_races(bucket, sub)
                all_races = [r for r in all_races if r.get("id") != race["id"]]
                all_races.insert(0, race)
                write_races(bucket, sub, all_races)
                return jresp(race, 201)

            elif request.method == "DELETE":
                race_id = request.args.get("id")
                if not race_id:
                    return jresp({"error": "Missing id parameter"}, 400)
                all_races = read_races(bucket, sub)
                race_id = int(race_id)
                target = next((r for r in all_races if r.get("id") == race_id), None)
                if not target:
                    return jresp({"error": "Race not found"}, 404)
                target["deleted"] = True
                target["deleted_at"] = datetime.utcnow().isoformat() + "Z"
                write_races(bucket, sub, all_races)
                return jresp({"soft_deleted": race_id, "deleted_at": target["deleted_at"]}, 200)

            else:
                return jresp({"error": "Method not allowed"}, 405)

        # ── /plan — per-user; новый пользователь → пустой план (без авто-сида) ─
        if path == "/plan":
            if request.method == "GET":
                manifest = read_plan_manifest(bucket, sub)
                if not manifest:
                    return jresp([], 200)   # пустой план — строится через конструктор
                plan_data = read_plan_version(bucket, manifest["gcs_object_path"])
                if not plan_data:
                    return jresp({"error": "plan version missing"}, 500)
                return jresp(plan_data["weeks"], 200)

            elif request.method == "POST":
                body = request.get_json(silent=True)
                if not body or "weeks" not in body:
                    return jresp({"error": "Missing weeks"}, 400)
                manifest = read_plan_manifest(bucket, sub)
                next_version = (manifest["current_version"] + 1) if manifest else 1
                result = write_plan_version(
                    bucket, sub, next_version, body["weeks"],
                    body.get("change_reason", ""), body.get("created_by", user.get("email", "api")))
                return jresp(result, 201)

            else:
                return jresp({"error": "Method not allowed"}, 405)

        # ── / (runs) ─────────────────────────────────────────────────────────
        else:
            if request.method == "GET":
                active = [r for r in read_runs(bucket, sub) if not r.get("deleted", False)]
                return jresp(active, 200)

            elif request.method == "POST":
                body = request.get_json(silent=True)
                if not body:
                    return jresp({"error": "Invalid JSON"}, 400)
                for field in ["date", "dist"]:
                    if field not in body:
                        return jresp({"error": f"Missing field: {field}"}, 400)
                run = {
                    "id": body.get("id", int(datetime.now().timestamp() * 1000)),
                    "date": body["date"], "dist": float(body["dist"]),
                    "type": body.get("type", "easy"), "time": body.get("time", ""),
                    "pace": body.get("pace", ""), "hr": body.get("hr"),
                    "feel": body.get("feel", "good"), "notes": body.get("notes", ""),
                    "deleted": False,
                }
                fit_token = body.get("fit_token")
                if fit_token:
                    try:
                        attach_fit_details_to_run(bucket, sub, run, fit_token)
                    except Exception as e:
                        return jresp({"error": f"Failed to attach FIT details: {str(e)[:300]}"}, 400)

                all_runs = read_runs(bucket, sub)
                all_runs = [r for r in all_runs if r.get("id") != run["id"]]
                all_runs.insert(0, run)
                write_runs(bucket, sub, all_runs)
                return jresp(run, 201)

            elif request.method == "DELETE":
                run_id = request.args.get("id")
                if not run_id:
                    return jresp({"error": "Missing id parameter"}, 400)
                all_runs = read_runs(bucket, sub)
                run_id = int(run_id)
                target = next((r for r in all_runs if r.get("id") == run_id), None)
                if not target:
                    return jresp({"error": "Run not found"}, 404)
                target["deleted"] = True
                target["deleted_at"] = datetime.utcnow().isoformat() + "Z"
                write_runs(bucket, sub, all_runs)
                return jresp({"soft_deleted": run_id, "deleted_at": target["deleted_at"]}, 200)

            else:
                return jresp({"error": "Method not allowed"}, 405)

    except Exception as e:
        return jresp({"error": str(e)}, 500)
