"""HTTP-слой: проверка токена, таблица маршрутов, хендлеры.

Выделено из main.py (#36, фаза 3). Точка входа Cloud Run Function осталась
в main.py — здесь живёт вся её начинка. Модуль зависит от storage, но не
наоборот.
"""
import json
import re
from datetime import datetime

import httpx
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from config import (ADMIN_DAILY_ADVISE_LIMIT, BUCKET_NAME, CLIENT_ID,
                    DAILY_ADVISE_LIMIT, LLM_DEFAULT_EFFORT, LLM_EFFORT_LEVELS)
from domain import personal_bests
from llm_prompt import SYSTEM_PROMPT, format_context_for_llm
from storage import (LLMRefused, LLMTruncated, RegistrationClosed,
                     _fmt_duration, _fmt_pace, archive_plan,
                     attach_fit_details_to_run, build_llm_context, call_llm,
                     clean_athlete_profile, clean_effort, cleanup_old_tmp,
                     compute_athlete_derived, create_plan, find_plan,
                     get_active_plan, get_storage_client,
                     increment_advice_usage, mask_key, migrate_legacy_to_user,
                     parse_fit_file, parse_llm_json, read_advice_usage,
                     read_athlete_history, read_athlete_profile,
                     read_latest_advice, read_llm_config_full, read_plan_weeks,
                     read_plans_index, read_races, read_registry,
                     read_run_details, read_runs, resolve_user, save_plan_weeks,
                     set_active_plan, set_user_status, update_plan_meta,
                     write_advice_version, write_athlete_version,
                     write_llm_config_version, write_parsed_fit_to_tmp,
                     write_races, write_runs)


# ── Auth ──────────────────────────────────────────────────────────────────────

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


# ── HTTP: ответы и контекст запроса ───────────────────────────────────────────

CORS_HEADERS = {
    "Access-Control-Allow-Origin": "*",
    "Access-Control-Allow-Methods": "GET, POST, DELETE, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type, Authorization",
}
JSON_HEADERS = {**CORS_HEADERS, "Content-Type": "application/json"}


def jresp(obj, code, extra_headers=None):
    headers = {**JSON_HEADERS, **extra_headers} if extra_headers else JSON_HEADERS
    return (json.dumps(obj, ensure_ascii=False, default=str), code, headers)


class Ctx:
    """Всё, что нужно хендлеру: запрос, бакет, пользователь и группы из пути."""

    def __init__(self, request, bucket, user, args):
        self.request = request
        self.bucket = bucket
        self.user = user
        self.sub = user["sub"]
        self.is_admin = user["role"] == "admin"
        self.email = user.get("email", "api")
        self.args = args          # группы из регулярного выражения пути

    def body(self):
        return self.request.get_json(silent=True) or {}


# ── Хендлеры ──────────────────────────────────────────────────────────────────

def h_admin_users(c):
    reg = read_registry(c.bucket)
    return jresp({"users": list(reg.get("users", {}).values())}, 200)


def h_admin_user_status(c):
    target = c.body().get("sub")
    if not target:
        return jresp({"error": "Missing sub"}, 400)
    status = "approved" if c.args[0] == "approve" else "rejected"
    rec = set_user_status(c.bucket, target, status, c.sub)
    if not rec:
        return jresp({"error": "user not found"}, 404)
    return jresp({"ok": True, "user": rec}, 200)


def h_admin_migrate_legacy(c):
    return jresp(migrate_legacy_to_user(c.bucket, c.sub), 200)


def h_parse_fit(c):
    fit_file = c.request.files.get("fit") if c.request.files else None
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
        cleanup_old_tmp(c.bucket, c.sub, max_age_hours=24)
    except Exception:
        pass  # best-effort

    token = write_parsed_fit_to_tmp(c.bucket, c.sub, fit_bytes, parsed)
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


def h_run_details(c):
    run_id = int(c.args[0])
    # Ownership: run_id должен быть в runs.json пользователя (иначе ленивый
    # fallback мог бы утащить чужие/legacy данные в чужой namespace).
    own_ids = {r.get("id") for r in read_runs(c.bucket, c.sub)}
    if run_id not in own_ids:
        return jresp({"error": "Run details not found"}, 404)
    details = read_run_details(c.bucket, c.sub, run_id)
    if not details:
        return jresp({"error": "Run details not found"}, 404)
    return jresp(details, 200)


def h_llm_config_get(c):
    cfg = read_llm_config_full(c.bucket)
    if not cfg:
        return jresp({"configured": False}, 200)
    return jresp({
        "configured": True,
        "version": cfg["version"],
        "provider": cfg["provider"],
        "model": cfg["model"],
        "api_key_masked": mask_key(cfg.get("api_key", "")),
        "effort": clean_effort(cfg.get("effort")),   # конфиг мог быть записан до #38
        "effort_levels": list(LLM_EFFORT_LEVELS),
        "default_effort": LLM_DEFAULT_EFFORT,
        "updated_at": cfg.get("created_at"),
    }, 200)


def h_llm_config_post(c):
    body = c.body()
    provider = body.get("provider")
    model = body.get("model")
    api_key = body.get("api_key", "").strip()
    if provider not in ("anthropic", "openai", "deepseek"):
        return jresp({"error": "Invalid provider"}, 400)
    if not model:
        return jresp({"error": "Missing model"}, 400)
    if not api_key:
        return jresp({"error": "Missing api_key"}, 400)
    effort = body.get("effort") or LLM_DEFAULT_EFFORT
    if effort not in LLM_EFFORT_LEVELS:
        return jresp({"error": "Invalid effort", "allowed": list(LLM_EFFORT_LEVELS)}, 400)
    result = write_llm_config_version(c.bucket, provider, model, api_key,
                                      effort=effort, created_by=c.email)
    return jresp(result, 201)


def h_llm_config_test(c):
    cfg = read_llm_config_full(c.bucket)
    if not cfg:
        return jresp({"ok": False, "error": "LLM config not set"}, 400)
    try:
        t0 = datetime.utcnow()
        res = call_llm(
            cfg["provider"], cfg["model"], cfg["api_key"],
            "Ты помощник. Отвечай строго: {\"ok\":true}",
            "Верни строго JSON {\"ok\":true}",
            effort=cfg.get("effort")
        )
        latency_ms = int((datetime.utcnow() - t0).total_seconds() * 1000)
        return jresp({
            "ok": True, "latency_ms": latency_ms,
            "input_tokens": res["input_tokens"],
            "output_tokens": res["output_tokens"],
            "sample_response": res["text"][:200],
        }, 200)
    except LLMRefused as e:
        return jresp({"ok": False, "error": f"Модель отклонила запрос: {str(e)[:200]}"}, 200)
    except httpx.HTTPStatusError as e:
        return jresp({"ok": False, "error": f"Provider {e.response.status_code}: {e.response.text[:200]}"}, 200)
    except Exception as e:
        return jresp({"ok": False, "error": str(e)[:200]}, 200)


def h_advise_preview(c):
    ctx = build_llm_context(c.bucket, c.sub)
    return jresp({"prompt": format_context_for_llm(ctx),
                  "system_prompt": SYSTEM_PROMPT}, 200)


def h_advise_get(c):
    latest = read_latest_advice(c.bucket, c.sub)
    if not latest:
        return jresp({"available": False}, 200)
    return jresp({"available": True, **latest}, 200)


def h_advise_post(c):
    cfg = read_llm_config_full(c.bucket)
    if not cfg or not cfg.get("api_key"):
        return jresp({"error": "LLM config not set. Обратитесь к администратору."}, 400)
    limit = ADMIN_DAILY_ADVISE_LIMIT if c.is_admin else DAILY_ADVISE_LIMIT
    usage = read_advice_usage(c.bucket, c.sub)
    if usage.get("count", 0) >= limit:
        return jresp({"error": "daily_limit_reached", "limit": limit}, 429)
    ctx = build_llm_context(c.bucket, c.sub)
    if not ctx["last_runs"]:
        return jresp({"error": "Нужна хотя бы одна пробежка для рекомендаций"}, 400)
    user_prompt = format_context_for_llm(ctx)
    try:
        llm_res = call_llm(cfg["provider"], cfg["model"], cfg["api_key"],
                           SYSTEM_PROMPT, user_prompt, effort=cfg.get("effort"))
    except LLMRefused as e:
        return jresp({"error": f"Модель отклонила запрос: {str(e)[:300]}"}, 422)
    except LLMTruncated:
        return jresp({"error": "Ответ не поместился в лимит токенов — "
                               "понизьте глубину рассуждения в настройках."}, 502)
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
        c.bucket, c.sub, recommendation, ctx,
        cfg["provider"], cfg["model"],
        llm_res["input_tokens"], llm_res["output_tokens"],
        cfg["version"], created_by=c.email)
    increment_advice_usage(c.bucket, c.sub)
    return jresp({"available": True, **payload}, 201)


def h_profile_history(c):
    return jresp(read_athlete_history(c.bucket, c.sub), 200)


def profile_response(bucket, sub, profile, version, updated_at):
    return {"profile": profile,
            "derived": compute_athlete_derived(profile),
            "personal_bests": personal_bests(read_races(bucket, sub)),
            "version": version,
            "updated_at": updated_at}


def h_profile_get(c):
    return jresp(profile_response(c.bucket, c.sub, *read_athlete_profile(c.bucket, c.sub)), 200)


def h_profile_post(c):
    body = c.body()
    profile, errors = clean_athlete_profile(body.get("profile") or body)
    if errors:
        return jresp({"error": "validation_failed", "fields": errors}, 400)
    payload = write_athlete_version(
        c.bucket, c.sub, profile,
        change_reason=(body.get("change_reason") or "").strip(),
        created_by=c.email)
    return jresp(profile_response(c.bucket, c.sub, profile,
                                  payload["version"], payload["created_at"]), 201)


def h_races_get(c):
    active = [r for r in read_races(c.bucket, c.sub) if not r.get("deleted", False)]
    return jresp(active, 200)


def h_races_post(c):
    body = c.request.get_json(silent=True)
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
    all_races = read_races(c.bucket, c.sub)
    all_races = [r for r in all_races if r.get("id") != race["id"]]
    all_races.insert(0, race)
    write_races(c.bucket, c.sub, all_races)
    return jresp(race, 201)


def h_races_delete(c):
    race_id = c.request.args.get("id")
    if not race_id:
        return jresp({"error": "Missing id parameter"}, 400)
    all_races = read_races(c.bucket, c.sub)
    race_id = int(race_id)
    target = next((r for r in all_races if r.get("id") == race_id), None)
    if not target:
        return jresp({"error": "Race not found"}, 404)
    target["deleted"] = True
    target["deleted_at"] = datetime.utcnow().isoformat() + "Z"
    write_races(c.bucket, c.sub, all_races)
    return jresp({"soft_deleted": race_id, "deleted_at": target["deleted_at"]}, 200)


def h_plans_get(c):
    # первый вызов запускает ленивую миграцию одиночного плана
    return jresp(read_plans_index(c.bucket, c.sub), 200)


def h_plans_post(c):
    return jresp(create_plan(c.bucket, c.sub, c.body()), 201)


def h_plan_activate(c):
    plan = set_active_plan(c.bucket, c.sub, c.body().get("plan_id"))
    if not plan:
        return jresp({"error": "plan not found"}, 404)
    return jresp({"ok": True, "active_plan_id": plan["id"]}, 200)


def h_plan_meta(c):
    plan = update_plan_meta(c.bucket, c.sub, c.args[0], c.body())
    if not plan:
        return jresp({"error": "plan not found"}, 404)
    return jresp(plan, 200)


def h_plan_archive(c):
    plan = archive_plan(c.bucket, c.sub, c.args[0])
    if not plan:
        return jresp({"error": "plan not found"}, 404)
    return jresp({"ok": True, "plan": plan}, 200)


def h_plan_weeks_get(c):
    plan_id = c.args[0]
    if not find_plan(read_plans_index(c.bucket, c.sub), plan_id):
        return jresp({"error": "plan not found"}, 404)
    return jresp(read_plan_weeks(c.bucket, c.sub, plan_id), 200)


def h_plan_weeks_post(c):
    plan_id = c.args[0]
    if not find_plan(read_plans_index(c.bucket, c.sub), plan_id):
        return jresp({"error": "plan not found"}, 404)
    body = c.request.get_json(silent=True)
    if not body or "weeks" not in body:
        return jresp({"error": "Missing weeks"}, 400)
    result = save_plan_weeks(c.bucket, c.sub, plan_id, body["weeks"],
                             body.get("change_reason", ""), c.email)
    return jresp(result, 201)


def h_active_plan_weeks_get(c):
    active = get_active_plan(c.bucket, c.sub)
    if not active:
        return jresp([], 200)   # планов нет — строится через конструктор
    return jresp(read_plan_weeks(c.bucket, c.sub, active["id"]), 200)


def h_active_plan_weeks_post(c):
    body = c.request.get_json(silent=True)
    if not body or "weeks" not in body:
        return jresp({"error": "Missing weeks"}, 400)
    active = get_active_plan(c.bucket, c.sub)
    if not active:
        return jresp({"error": "no active plan"}, 400)
    result = save_plan_weeks(c.bucket, c.sub, active["id"], body["weeks"],
                             body.get("change_reason", ""), c.email)
    return jresp(result, 201)


def h_runs_get(c):
    active = [r for r in read_runs(c.bucket, c.sub) if not r.get("deleted", False)]
    return jresp(active, 200)


def h_runs_post(c):
    body = c.request.get_json(silent=True)
    if not body:
        return jresp({"error": "Invalid JSON"}, 400)
    for field in ["date", "dist"]:
        if field not in body:
            return jresp({"error": f"Missing field: {field}"}, 400)
    # Привязка к плану: явный plan_id или активный план (#25)
    plan_id = body.get("plan_id")
    if plan_id is None:
        active = get_active_plan(c.bucket, c.sub)
        plan_id = active["id"] if active else None

    run = {
        "id": body.get("id", int(datetime.now().timestamp() * 1000)),
        "date": body["date"], "dist": float(body["dist"]),
        "type": body.get("type", "easy"), "time": body.get("time", ""),
        "pace": body.get("pace", ""), "hr": body.get("hr"),
        "feel": body.get("feel", "good"), "notes": body.get("notes", ""),
        "plan_id": plan_id,
        "deleted": False,
    }
    fit_token = body.get("fit_token")
    if fit_token:
        try:
            attach_fit_details_to_run(c.bucket, c.sub, run, fit_token)
        except Exception as e:
            return jresp({"error": f"Failed to attach FIT details: {str(e)[:300]}"}, 400)

    all_runs = read_runs(c.bucket, c.sub)
    all_runs = [r for r in all_runs if r.get("id") != run["id"]]
    all_runs.insert(0, run)
    write_runs(c.bucket, c.sub, all_runs)
    return jresp(run, 201)


def h_runs_delete(c):
    run_id = c.request.args.get("id")
    if not run_id:
        return jresp({"error": "Missing id parameter"}, 400)
    all_runs = read_runs(c.bucket, c.sub)
    run_id = int(run_id)
    target = next((r for r in all_runs if r.get("id") == run_id), None)
    if not target:
        return jresp({"error": "Run not found"}, 404)
    target["deleted"] = True
    target["deleted_at"] = datetime.utcnow().isoformat() + "Z"
    write_runs(c.bucket, c.sub, all_runs)
    return jresp({"soft_deleted": run_id, "deleted_at": target["deleted_at"]}, 200)


# ── Таблица маршрутов (#36) ───────────────────────────────────────────────────
#
# (метод, шаблон пути, хендлер, только для админа). Раньше это была цепочка
# if-ов, где корректность держалась на порядке строк: /advise/preview обязан
# был стоять выше /advise, /profile/history — выше /profile. Здесь шаблоны
# заякорены, поэтому порядок объявления ни на что не влияет.

ROUTES = [
    ("GET",    r"^/admin/users$",                h_admin_users,          True),
    ("POST",   r"^/admin/users/(approve|reject)$", h_admin_user_status,  True),
    ("POST",   r"^/admin/migrate-legacy$",       h_admin_migrate_legacy, True),

    ("POST",   r"^/runs/parse-fit$",             h_parse_fit,            False),
    ("GET",    r"^/runs/(\d+)/details$",         h_run_details,          False),

    ("GET",    r"^/config/llm$",                 h_llm_config_get,       True),
    ("POST",   r"^/config/llm$",                 h_llm_config_post,      True),
    ("POST",   r"^/config/llm/test$",            h_llm_config_test,      True),

    ("GET",    r"^/advise$",                     h_advise_get,           False),
    ("POST",   r"^/advise$",                     h_advise_post,          False),
    ("GET",    r"^/advise/preview$",             h_advise_preview,       False),

    ("GET",    r"^/profile$",                    h_profile_get,          False),
    ("POST",   r"^/profile$",                    h_profile_post,         False),
    ("GET",    r"^/profile/history$",            h_profile_history,      False),

    ("GET",    r"^/races$",                      h_races_get,            False),
    ("POST",   r"^/races$",                      h_races_post,           False),
    ("DELETE", r"^/races$",                      h_races_delete,         False),

    ("GET",    r"^/plans$",                      h_plans_get,            False),
    ("POST",   r"^/plans$",                      h_plans_post,           False),
    ("POST",   r"^/plans/active$",               h_plan_activate,        False),
    ("POST",   r"^/plans/([\w-]+)/meta$",        h_plan_meta,            False),
    ("POST",   r"^/plans/([\w-]+)/archive$",     h_plan_archive,         False),
    ("GET",    r"^/plans/([\w-]+)/weeks$",       h_plan_weeks_get,       False),
    ("POST",   r"^/plans/([\w-]+)/weeks$",       h_plan_weeks_post,      False),

    ("GET",    r"^/plan$",                       h_active_plan_weeks_get,  False),
    ("POST",   r"^/plan$",                       h_active_plan_weeks_post, False),

    ("GET",    r"^/$",                           h_runs_get,             False),
    ("POST",   r"^/$",                           h_runs_post,            False),
    ("DELETE", r"^/$",                           h_runs_delete,          False),
]


def match_route(path, method):
    """(маршрут, совпадение, разрешённые методы).

    Сначала собираем все маршруты с совпавшим путём, потом выбираем по методу —
    поэтому неизвестный метод на известном пути даёт единообразный 405 с Allow,
    а не проваливается в другую ветку.
    """
    matched = [(route, m) for route in ROUTES
               if (m := re.match(route[1], path))]
    if not matched:
        return None, None, None
    for route, m in matched:
        if route[0] == method:
            return route, m, None
    return None, None, sorted({r[0] for r, _ in matched} | {"OPTIONS"})


# ── HTTP handler ──────────────────────────────────────────────────────────────

def handle_request(request):
    if request.method == "OPTIONS":
        return ("", 204, CORS_HEADERS)

    token_info = verify_token(request)
    if not token_info:
        return jresp({"error": "Unauthorized"}, 401)

    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)

    try:
        user = resolve_user(bucket, token_info)
    except RegistrationClosed:
        return jresp({"error": "registration_closed"}, 403)

    path = request.path.rstrip("/") or "/"

    # /me вне таблицы намеренно: он обязан отвечать до проверки одобрения —
    # именно из него фронт узнаёт, что заявка ещё на рассмотрении.
    if path == "/me":
        return jresp({"status": user["status"], "role": user["role"],
                      "email": user.get("email"), "name": user.get("name")}, 200)

    # Не одобрен → 403 на всё остальное, ещё до разбора маршрута: иначе по коду
    # ответа можно было бы перебирать существующие пути.
    if user["status"] != "approved":
        return jresp({
            "error": "pending_approval" if user["status"] == "pending" else "rejected",
            "status": user["status"],
        }, 403)

    route, match, allow = match_route(path, request.method)
    if allow:
        return jresp({"error": "Method not allowed"}, 405, {"Allow": ", ".join(allow)})
    if not route:
        return jresp({"error": "Not found"}, 404)

    _, _, handler, admin_only = route
    if admin_only and user["role"] != "admin":
        return jresp({"error": "forbidden"}, 403)

    try:
        return handler(Ctx(request, bucket, user, match.groups()))
    except Exception as e:
        return jresp({"error": str(e)}, 500)
