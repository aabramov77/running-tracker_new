import hashlib
import io
import json
import re
import httpx
import functions_framework
from google.cloud import storage
from datetime import datetime
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests
from fitparse import FitFile

CLIENT_ID = "463368957110-f1649h2mjd1hbkj5307jllcv3e0hslbc.apps.googleusercontent.com"
ALLOWED_EMAIL = "aabramov77@gmail.com"

BUCKET_NAME = "running-tracker-aabramov77"
OBJECT_NAME = "runs.json"
RACES_OBJECT = "races.json"
PLAN_MANIFEST = "plan/manifest.json"
LLM_CONFIG_MANIFEST = "config/llm/manifest.json"
ADVICE_MANIFEST = "advice/manifest.json"

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


# ── Runs helpers ──────────────────────────────────────────────────────────────

def read_runs():
    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(OBJECT_NAME)
    if not blob.exists():
        return []
    return json.loads(blob.download_as_text())


def write_runs(runs):
    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)
    bucket.blob(OBJECT_NAME).upload_from_string(
        json.dumps(runs, ensure_ascii=False, indent=2),
        content_type="application/json"
    )


# ── Races helpers ─────────────────────────────────────────────────────────────

def read_races():
    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)
    blob = bucket.blob(RACES_OBJECT)
    if not blob.exists():
        return []
    return json.loads(blob.download_as_text())


def write_races(races):
    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)
    bucket.blob(RACES_OBJECT).upload_from_string(
        json.dumps(races, ensure_ascii=False, indent=2),
        content_type="application/json"
    )


# ── FIT parsing + run details helpers ────────────────────────────────────────

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
            "avg_cadence": session.get("avg_running_cadence") or session.get("avg_cadence"),
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
            "cadence": lap.get("avg_running_cadence") or lap.get("avg_cadence"),
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


def write_run_with_fit(bucket, run_id, fit_bytes, parsed):
    """Заливает activity.fit + details.json + manifest.json в runs/{id}/v1/."""
    now = datetime.utcnow().isoformat() + "Z"
    fit_path = f"runs/{run_id}/v1/activity.fit"
    details_path = f"runs/{run_id}/v1/details.json"
    manifest_path = f"runs/{run_id}/manifest.json"

    # FIT
    bucket.blob(fit_path).upload_from_string(fit_bytes, content_type="application/octet-stream")

    # details.json
    details = {
        "version": 1,
        "is_current": True,
        "created_at": now,
        "source": "garmin_fit",
        "fit_object_path": fit_path,
        "date": parsed.get("date"),
        "summary": parsed.get("summary", {}),
        "laps": parsed.get("laps", []),
        "samples": parsed.get("samples", {"t_offset_sec": [], "hr": [], "pace_sec_per_km": [], "altitude_m": []}),
    }
    bucket.blob(details_path).upload_from_string(
        json.dumps(details, ensure_ascii=False, indent=2, default=str),
        content_type="application/json"
    )

    # manifest
    bucket.blob(manifest_path).upload_from_string(
        json.dumps({
            "current_version": 1,
            "gcs_object_path": details_path,
            "updated_at": now,
        }, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return details


def read_run_details(bucket, run_id):
    manifest_blob = bucket.blob(f"runs/{run_id}/manifest.json")
    if not manifest_blob.exists():
        return None
    manifest = json.loads(manifest_blob.download_as_text())
    details_blob = bucket.blob(manifest["gcs_object_path"])
    if not details_blob.exists():
        return None
    return json.loads(details_blob.download_as_text())


# ── Plan helpers ──────────────────────────────────────────────────────────────

def read_plan_manifest(bucket):
    blob = bucket.blob(PLAN_MANIFEST)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def read_plan_version(bucket, object_path):
    blob = bucket.blob(object_path)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def write_plan_version(bucket, version, weeks, change_reason, created_by="api"):
    object_path = f"plan/v{version}/plan.json"
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
    bucket.blob(PLAN_MANIFEST).upload_from_string(
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


def build_llm_context(bucket):
    """Собирает компактный богатый контекст для LLM."""
    # Runs
    runs_blob = bucket.blob(OBJECT_NAME)
    all_runs = json.loads(runs_blob.download_as_text()) if runs_blob.exists() else []
    active_runs = [r for r in all_runs if not r.get("deleted", False)]
    active_runs.sort(key=lambda r: r.get("date", ""), reverse=True)
    last_runs = active_runs[:14]

    # Races
    races_blob = bucket.blob(RACES_OBJECT)
    all_races = json.loads(races_blob.download_as_text()) if races_blob.exists() else []
    active_races = [r for r in all_races if not r.get("deleted", False)]
    active_races.sort(key=lambda r: r.get("date", ""), reverse=True)
    last_races = active_races[:3]

    # Plan
    plan_manifest = read_plan_manifest(bucket)
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
        lines.append(f"  вс={cw.get('sun')}; пн={cw.get('mon')}; ср={cw.get('wed')}; пт={cw.get('fri')}; сб={cw.get('sat')}")
    nw = ctx.get("next_week")
    if nw:
        lines.append("План следующей недели:")
        lines.append(f"  вс={nw.get('sun')}; пн={nw.get('mon')}; ср={nw.get('wed')}; пт={nw.get('fri')}; сб={nw.get('sat')}")

    lines.append("")
    lines.append("Последние 14 тренировок (сначала свежие):")
    for r in ctx["last_runs"]:
        t = TYPE_LABELS.get(r.get("type"), r.get("type", ""))
        feel = FEEL_LABELS.get(r.get("feel"), "")
        parts = [r.get("date", "?"), t, f"{r.get('dist', '?')}км"]
        if r.get("time"): parts.append(r["time"])
        if r.get("pace"): parts.append(f"темп {r['pace']}/км")
        if r.get("hr"): parts.append(f"пульс {r['hr']}")
        if feel: parts.append(f"ощ:{feel}")
        line = "  - " + " ".join(parts)
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


def read_advice_manifest(bucket):
    blob = bucket.blob(ADVICE_MANIFEST)
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def read_latest_advice(bucket):
    manifest = read_advice_manifest(bucket)
    if not manifest:
        return None
    blob = bucket.blob(manifest["gcs_object_path"])
    if not blob.exists():
        return None
    return json.loads(blob.download_as_text())


def write_advice_version(bucket, recommendation, ctx, provider, model, input_tokens, output_tokens, llm_config_version, created_by="aabramov77"):
    manifest = read_advice_manifest(bucket)
    next_version = (manifest["current_version"] + 1) if manifest else 1
    object_path = f"advice/v{next_version}/recommendation.json"
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
    bucket.blob(ADVICE_MANIFEST).upload_from_string(
        json.dumps(new_manifest, ensure_ascii=False, indent=2),
        content_type="application/json"
    )
    return payload


# ── Auth ───────────────────────────────────────────────────────────────────────

def verify_token(request):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return None
    token = auth[7:]
    try:
        info = id_token.verify_oauth2_token(
            token, google_requests.Request(), CLIENT_ID
        )
        if info.get("email") != ALLOWED_EMAIL:
            return None
        return info
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

    if request.method == "OPTIONS":
        return ("", 204, headers)

    user = verify_token(request)
    if not user:
        return (json.dumps({"error": "Unauthorized"}), 401, headers)

    path = request.path.rstrip("/") or "/"

    try:
        # ── /runs/upload-fit ─────────────────────────────────────────────────
        if path == "/runs/upload-fit":
            if request.method != "POST":
                return (json.dumps({"error": "Method not allowed"}), 405, headers)
            fit_file = request.files.get("fit") if request.files else None
            if not fit_file:
                return (json.dumps({"error": "No 'fit' file in multipart upload"}), 400, headers)
            try:
                fit_bytes = fit_file.read()
                parsed = parse_fit_file(fit_bytes)
            except Exception as e:
                return (json.dumps({"error": f"FIT parse failed: {str(e)[:300]}"}), 400, headers)

            if not parsed.get("summary", {}).get("dist_km"):
                return (json.dumps({"error": "FIT file has no session/distance data — not a valid activity?"}), 400, headers)

            type_ = request.form.get("type", "easy")
            feel = request.form.get("feel", "good")
            notes = request.form.get("notes", "")

            run_id = int(datetime.now().timestamp() * 1000)
            summary = parsed.get("summary", {})

            client = get_storage_client()
            bucket = client.bucket(BUCKET_NAME)

            # Сначала пишем FIT/details — если упадёт, не получим осиротевшую запись в runs.json
            write_run_with_fit(bucket, run_id, fit_bytes, parsed)

            run = {
                "id": run_id,
                "date": parsed.get("date"),
                "dist": summary.get("dist_km"),
                "type": type_,
                "time": _fmt_duration(summary.get("duration_sec")),
                "pace": _fmt_pace(summary.get("avg_pace_sec_per_km")),
                "hr": summary.get("avg_hr"),
                "feel": feel,
                "notes": notes,
                "deleted": False,
                "details_available": True,
                "max_hr": summary.get("max_hr"),
                "avg_cadence": summary.get("avg_cadence"),
                "total_ascent_m": summary.get("total_ascent_m"),
                "calories": summary.get("calories"),
            }

            all_runs = read_runs()
            all_runs = [r for r in all_runs if r.get("id") != run_id]
            all_runs.insert(0, run)
            write_runs(all_runs)

            return (json.dumps(run, ensure_ascii=False), 201, {
                **headers, "Content-Type": "application/json"
            })

        # ── /runs/{id}/details ───────────────────────────────────────────────
        details_match = re.match(r"^/runs/(\d+)/details$", path)
        if details_match:
            if request.method != "GET":
                return (json.dumps({"error": "Method not allowed"}), 405, headers)
            run_id = int(details_match.group(1))
            client = get_storage_client()
            bucket = client.bucket(BUCKET_NAME)
            details = read_run_details(bucket, run_id)
            if not details:
                return (json.dumps({"error": "Run details not found"}), 404, headers)
            return (json.dumps(details, ensure_ascii=False, default=str), 200, {
                **headers, "Content-Type": "application/json"
            })

        # ── /config/llm and /config/llm/test ─────────────────────────────────
        if path == "/config/llm":
            client = get_storage_client()
            bucket = client.bucket(BUCKET_NAME)
            if request.method == "GET":
                cfg = read_llm_config_full(bucket)
                if not cfg:
                    return (json.dumps({"configured": False}, ensure_ascii=False), 200, {
                        **headers, "Content-Type": "application/json"
                    })
                return (json.dumps({
                    "configured": True,
                    "version": cfg["version"],
                    "provider": cfg["provider"],
                    "model": cfg["model"],
                    "api_key_masked": mask_key(cfg.get("api_key", "")),
                    "updated_at": cfg.get("created_at"),
                }, ensure_ascii=False), 200, {**headers, "Content-Type": "application/json"})

            elif request.method == "POST":
                body = request.get_json(silent=True) or {}
                provider = body.get("provider")
                model = body.get("model")
                api_key = body.get("api_key", "").strip()
                if provider not in ("anthropic", "openai", "deepseek"):
                    return (json.dumps({"error": "Invalid provider"}), 400, headers)
                if not model:
                    return (json.dumps({"error": "Missing model"}), 400, headers)
                if not api_key:
                    return (json.dumps({"error": "Missing api_key"}), 400, headers)
                result = write_llm_config_version(bucket, provider, model, api_key, created_by=user.get("email", "api"))
                return (json.dumps(result, ensure_ascii=False), 201, {
                    **headers, "Content-Type": "application/json"
                })
            else:
                return (json.dumps({"error": "Method not allowed"}), 405, headers)

        if path == "/config/llm/test":
            if request.method != "POST":
                return (json.dumps({"error": "Method not allowed"}), 405, headers)
            client = get_storage_client()
            bucket = client.bucket(BUCKET_NAME)
            cfg = read_llm_config_full(bucket)
            if not cfg:
                return (json.dumps({"ok": False, "error": "LLM config not set"}), 400, headers)
            try:
                t0 = datetime.utcnow()
                res = call_llm(
                    cfg["provider"], cfg["model"], cfg["api_key"],
                    "Ты помощник. Отвечай строго: {\"ok\":true}",
                    "Верни строго JSON {\"ok\":true}"
                )
                latency_ms = int((datetime.utcnow() - t0).total_seconds() * 1000)
                return (json.dumps({
                    "ok": True,
                    "latency_ms": latency_ms,
                    "input_tokens": res["input_tokens"],
                    "output_tokens": res["output_tokens"],
                    "sample_response": res["text"][:200],
                }, ensure_ascii=False), 200, {**headers, "Content-Type": "application/json"})
            except httpx.HTTPStatusError as e:
                return (json.dumps({
                    "ok": False,
                    "error": f"Provider {e.response.status_code}: {e.response.text[:200]}"
                }, ensure_ascii=False), 200, {**headers, "Content-Type": "application/json"})
            except Exception as e:
                return (json.dumps({"ok": False, "error": str(e)[:200]}, ensure_ascii=False), 200, {
                    **headers, "Content-Type": "application/json"
                })

        # ── /advise ──────────────────────────────────────────────────────────
        if path == "/advise":
            client = get_storage_client()
            bucket = client.bucket(BUCKET_NAME)
            if request.method == "GET":
                latest = read_latest_advice(bucket)
                if not latest:
                    return (json.dumps({"available": False}, ensure_ascii=False), 200, {
                        **headers, "Content-Type": "application/json"
                    })
                return (json.dumps({"available": True, **latest}, ensure_ascii=False), 200, {
                    **headers, "Content-Type": "application/json"
                })

            elif request.method == "POST":
                cfg = read_llm_config_full(bucket)
                if not cfg or not cfg.get("api_key"):
                    return (json.dumps({"error": "LLM config not set. Откройте Настройки и задайте провайдера и ключ."}), 400, headers)
                ctx = build_llm_context(bucket)
                if not ctx["last_runs"]:
                    return (json.dumps({"error": "Нужна хотя бы одна пробежка для рекомендаций"}), 400, headers)
                user_prompt = format_context_for_llm(ctx)
                try:
                    llm_res = call_llm(cfg["provider"], cfg["model"], cfg["api_key"], SYSTEM_PROMPT, user_prompt)
                except httpx.HTTPStatusError as e:
                    return (json.dumps({"error": f"Provider {e.response.status_code}: {e.response.text[:300]}"}), 502, headers)
                except Exception as e:
                    return (json.dumps({"error": f"LLM call failed: {str(e)[:300]}"}), 502, headers)

                try:
                    recommendation = parse_llm_json(llm_res["text"])
                except Exception as e:
                    return (json.dumps({
                        "error": f"Cannot parse LLM response as JSON: {str(e)[:200]}",
                        "raw_text": llm_res["text"][:500]
                    }), 502, headers)

                payload = write_advice_version(
                    bucket, recommendation, ctx,
                    cfg["provider"], cfg["model"],
                    llm_res["input_tokens"], llm_res["output_tokens"],
                    cfg["version"],
                    created_by=user.get("email", "api")
                )
                return (json.dumps({"available": True, **payload}, ensure_ascii=False), 201, {
                    **headers, "Content-Type": "application/json"
                })
            else:
                return (json.dumps({"error": "Method not allowed"}), 405, headers)

        # ── /races ───────────────────────────────────────────────────────────
        if path == "/races":
            if request.method == "GET":
                all_races = read_races()
                active_races = [r for r in all_races if not r.get("deleted", False)]
                return (json.dumps(active_races, ensure_ascii=False), 200, {
                    **headers, "Content-Type": "application/json"
                })

            elif request.method == "POST":
                body = request.get_json(silent=True)
                if not body:
                    return (json.dumps({"error": "Invalid JSON"}), 400, headers)

                for field in ["name", "date", "dist_label", "time"]:
                    if field not in body:
                        return (json.dumps({"error": f"Missing field: {field}"}), 400, headers)

                race = {
                    "id": body.get("id", int(datetime.now().timestamp() * 1000)),
                    "name": body["name"],
                    "date": body["date"],
                    "dist_label": body["dist_label"],
                    "time": body["time"],
                    "deleted": False,
                }

                all_races = read_races()
                all_races = [r for r in all_races if r.get("id") != race["id"]]
                all_races.insert(0, race)
                write_races(all_races)

                return (json.dumps(race, ensure_ascii=False), 201, {
                    **headers, "Content-Type": "application/json"
                })

            elif request.method == "DELETE":
                race_id = request.args.get("id")
                if not race_id:
                    return (json.dumps({"error": "Missing id parameter"}), 400, headers)

                all_races = read_races()
                race_id = int(race_id)

                target = next((r for r in all_races if r.get("id") == race_id), None)
                if not target:
                    return (json.dumps({"error": "Race not found"}), 404, headers)

                target["deleted"] = True
                target["deleted_at"] = datetime.utcnow().isoformat() + "Z"
                write_races(all_races)

                return (json.dumps({"soft_deleted": race_id, "deleted_at": target["deleted_at"]}), 200, {
                    **headers, "Content-Type": "application/json"
                })

            else:
                return (json.dumps({"error": "Method not allowed"}), 405, headers)

        # ── /plan ────────────────────────────────────────────────────────────
        if path == "/plan":
            client = get_storage_client()
            bucket = client.bucket(BUCKET_NAME)

            if request.method == "GET":
                manifest = read_plan_manifest(bucket)
                if not manifest:
                    # Авто-seed: первый запрос создаёт v1 из INITIAL_PLAN
                    write_plan_version(bucket, 1, INITIAL_PLAN, "initial seed", "auto-seed")
                    return (json.dumps(INITIAL_PLAN, ensure_ascii=False), 200, {
                        **headers, "Content-Type": "application/json"
                    })
                plan_data = read_plan_version(bucket, manifest["gcs_object_path"])
                if not plan_data:
                    return (json.dumps({"error": "plan version missing"}), 500, headers)
                return (json.dumps(plan_data["weeks"], ensure_ascii=False), 200, {
                    **headers, "Content-Type": "application/json"
                })

            elif request.method == "POST":
                body = request.get_json(silent=True)
                if not body or "weeks" not in body:
                    return (json.dumps({"error": "Missing weeks"}), 400, headers)
                manifest = read_plan_manifest(bucket)
                next_version = (manifest["current_version"] + 1) if manifest else 1
                result = write_plan_version(
                    bucket,
                    next_version,
                    body["weeks"],
                    body.get("change_reason", ""),
                    body.get("created_by", "api"),
                )
                return (json.dumps(result, ensure_ascii=False), 201, {
                    **headers, "Content-Type": "application/json"
                })

            else:
                return (json.dumps({"error": "Method not allowed"}), 405, headers)

        # ── / (runs) ─────────────────────────────────────────────────────────
        else:
            if request.method == "GET":
                all_runs = read_runs()
                active_runs = [r for r in all_runs if not r.get("deleted", False)]
                return (json.dumps(active_runs, ensure_ascii=False), 200, {
                    **headers, "Content-Type": "application/json"
                })

            elif request.method == "POST":
                body = request.get_json(silent=True)
                if not body:
                    return (json.dumps({"error": "Invalid JSON"}), 400, headers)

                for field in ["date", "dist"]:
                    if field not in body:
                        return (json.dumps({"error": f"Missing field: {field}"}), 400, headers)

                run = {
                    "id": body.get("id", int(datetime.now().timestamp() * 1000)),
                    "date": body["date"],
                    "dist": float(body["dist"]),
                    "type": body.get("type", "easy"),
                    "time": body.get("time", ""),
                    "pace": body.get("pace", ""),
                    "hr": body.get("hr"),
                    "feel": body.get("feel", "good"),
                    "notes": body.get("notes", ""),
                    "deleted": False,
                }

                all_runs = read_runs()
                all_runs = [r for r in all_runs if r.get("id") != run["id"]]
                all_runs.insert(0, run)
                write_runs(all_runs)

                return (json.dumps(run, ensure_ascii=False), 201, {
                    **headers, "Content-Type": "application/json"
                })

            elif request.method == "DELETE":
                run_id = request.args.get("id")
                if not run_id:
                    return (json.dumps({"error": "Missing id parameter"}), 400, headers)

                all_runs = read_runs()
                run_id = int(run_id)

                target = next((r for r in all_runs if r.get("id") == run_id), None)
                if not target:
                    return (json.dumps({"error": "Run not found"}), 404, headers)

                target["deleted"] = True
                target["deleted_at"] = datetime.utcnow().isoformat() + "Z"
                write_runs(all_runs)

                return (json.dumps({"soft_deleted": run_id, "deleted_at": target["deleted_at"]}), 200, {
                    **headers, "Content-Type": "application/json"
                })

            else:
                return (json.dumps({"error": "Method not allowed"}), 405, headers)

    except Exception as e:
        return (json.dumps({"error": str(e)}), 500, {
            **headers, "Content-Type": "application/json"
        })
