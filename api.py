"""HTTP-слой: проверка токена, таблица маршрутов, хендлеры.

Выделено из main.py (#36, фаза 3). Точка входа Cloud Run Function осталась
в main.py — здесь живёт вся её начинка. Модуль зависит от storage, но не
наоборот.
"""
import json
import logging
import re
from datetime import datetime

import httpx
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from config import (ADMIN_DAILY_AI_COACH_LIMIT, BUCKET_NAME, CLIENT_ID,
                    DAILY_AI_COACH_LIMIT, LLM_DEFAULT_EFFORT, LLM_EFFORT_LEVELS,
                    MAX_FIT_BODY_BYTES, MAX_JSON_BODY_BYTES)
from domain import personal_bests
from storage import (AICoachError, ChatError, CoachLinkError, LLMRefused,
                     LLMTruncated, PlanStale, RegistrationClosed, UsageBusy,
                     _fmt_duration, _fmt_pace,
                     active_coach_sub, append_ai_message, append_chat_message,
                     apply_ai_proposal,
                     archive_ai_thread, archive_plan,
                     attach_fit_details_to_run, build_ai_turn,
                     build_plan_compliance, call_llm,
                     chat_unread, mark_chat_read, read_chat,
                     clean_ai_text, clean_athlete_profile, clean_change_reason,
                     clean_effort, clean_plan_meta, clean_plan_weeks,
                     clean_proposal, clean_race, clean_run,
                     cleanup_old_tmp, coach_can_access,
                     compute_athlete_derived, create_ai_thread, create_plan,
                     current_coach, find_own_run, find_plan, get_active_plan,
                     get_storage_client,
                     list_ai_threads, list_athletes_of, list_coaches,
                     mark_proposal_states, mask_key,
                     migrate_legacy_to_user, parse_coach_reply, parse_fit_file,
                     read_advice_usage, read_ai_messages,
                     read_ai_thread, read_athlete_history,
                     read_athlete_profile,
                     read_llm_config_full, read_plan_state, read_plan_weeks,
                     read_plans_index,
                     read_races, read_registry, read_run_details, read_runs,
                     release_advice_usage, reserve_advice_usage,
                     resolve_user, run_title, save_plan_weeks, set_active_plan,
                     set_coach_flag, set_user_coach, set_user_status,
                     update_plan_meta,
                     write_athlete_version, write_llm_config_version,
                     write_parsed_fit_to_tmp, write_races, write_runs)


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


def validation_failed(errors):
    """Ответ на тело, не прошедшее clean_* из storage: {поле: что не так}."""
    return jresp({"error": "validation_failed", "fields": errors}, 400)


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
        """JSON-объект из тела; всё остальное (массив, число, не JSON) — {}."""
        data = self.request.get_json(silent=True)
        return data if isinstance(data, dict) else {}

    def query_id(self):
        """(?id= как целое, None) либо (None, готовый ответ 400)."""
        raw = self.request.args.get("id")
        if not raw:
            return None, jresp({"error": "Missing id parameter"}, 400)
        try:
            return int(raw), None
        except (TypeError, ValueError):
            return None, jresp({"error": "Invalid id parameter"}, 400)


# ── Хендлеры ──────────────────────────────────────────────────────────────────

def h_admin_users(c):
    reg = read_registry(c.bucket)
    return jresp({"users": list(reg.get("users", {}).values())}, 200)


def h_admin_user_status(c):
    target = c.body().get("sub")
    if not target:
        return jresp({"error": "Missing sub"}, 400)
    if not isinstance(target, str):
        return jresp({"error": "Invalid sub"}, 400)
    status = "approved" if c.args[0] == "approve" else "rejected"
    rec = set_user_status(c.bucket, target, status, c.sub)
    if not rec:
        return jresp({"error": "user not found"}, 404)
    return jresp({"ok": True, "user": rec}, 200)


def h_admin_user_coach(c):
    body = c.body()
    target = body.get("sub")
    if not target or not isinstance(body.get("is_coach"), bool):
        return jresp({"error": "Missing sub or is_coach"}, 400)
    if not isinstance(target, str):
        return jresp({"error": "Invalid sub"}, 400)
    rec = set_coach_flag(c.bucket, target, body["is_coach"], c.sub)
    if not rec:
        return jresp({"error": "user not found"}, 404)
    return jresp({"ok": True, "user": rec}, 200)


# ── Тренер (#44): выбор тренера спортсменом ───────────────────────────────────

def h_coaches(c):
    # Себя в списке нет: выбрать себя тренером всё равно нельзя.
    coaches = [coach for coach in list_coaches(c.bucket) if coach["sub"] != c.sub]
    return jresp({"coaches": coaches}, 200)


def h_my_coach_get(c):
    coach = current_coach(c.bucket, c.sub)
    unread = chat_unread(c.bucket, c.sub, coach["sub"], "athlete") if coach else 0
    return jresp({"coach": coach, "unread": unread}, 200)


def h_my_coach_post(c):
    body = c.body()
    if "coach_sub" not in body:
        return jresp({"error": "Missing coach_sub"}, 400)
    coach_sub = body["coach_sub"]
    # Отказ от тренера — это null или пустая строка. Ноль, false и пустой
    # список отказом не считаются: снять тренера случайным значением нельзя.
    if coach_sub is not None and not isinstance(coach_sub, str):
        return jresp({"error": "Invalid coach_sub"}, 400)
    try:
        set_user_coach(c.bucket, c.sub, coach_sub)
    except CoachLinkError as e:
        return jresp({"error": str(e)}, 400)
    return jresp({"coach": current_coach(c.bucket, c.sub)}, 200)


# ── Тренер (#44): просмотр данных спортсмена ──────────────────────────────────
# Только чтение. Профиль спортсмена сюда не входит намеренно: тренеру открыты
# планы, выполнение и журнал — но не вес, пульс и травмы.

class Forbidden(Exception):
    """Нет права на чужие данные → 403 (см. handle_request)."""


def _coached_athlete(c):
    """sub спортсмена из пути — если текущий пользователь его тренер.

    Ответ одинаков для чужого спортсмена и для несуществующего sub, поэтому
    перебором идентификаторов ничего не узнать.
    """
    athlete = c.args[0]
    if not coach_can_access(c.bucket, c.sub, athlete):
        raise Forbidden()
    return athlete


def h_coach_athletes(c):
    athletes = list_athletes_of(c.bucket, c.sub)
    if athletes is None:
        raise Forbidden()
    for athlete in athletes:
        athlete["unread"] = chat_unread(c.bucket, athlete["sub"], c.sub, "coach")
    return jresp({"athletes": athletes}, 200)


def h_coach_plans(c):
    return jresp(read_plans_index(c.bucket, _coached_athlete(c)), 200)


def h_coach_plan_weeks(c):
    athlete, plan_id = _coached_athlete(c), c.args[1]
    if not find_plan(read_plans_index(c.bucket, athlete), plan_id):
        return jresp({"error": "plan not found"}, 404)
    return jresp(read_plan_weeks(c.bucket, athlete, plan_id), 200)


def h_coach_plan_compliance(c):
    result = build_plan_compliance(c.bucket, _coached_athlete(c), c.args[1])
    if result is None:
        return jresp({"error": "plan not found"}, 404)
    return jresp(result, 200)


def h_coach_runs(c):
    runs = read_runs(c.bucket, _coached_athlete(c))
    return jresp([r for r in runs if not r.get("deleted", False)], 200)


def h_coach_run_details(c):
    # Скрытую спортсменом пробежку тренер не видит ни в журнале, ни по id.
    return _run_details_response(c.bucket, _coached_athlete(c), int(c.args[1]),
                                 include_deleted=False)


# ── Тренер (#44): чат ─────────────────────────────────────────────────────────
# Ветку задаёт пара (спортсмен, тренер). Обе стороны ходят в одни и те же
# функции; отличается только то, как пара получается из запроса.

class NoCoach(Exception):
    """У спортсмена нет действующего тренера → 409 (см. handle_request)."""


def _my_thread(c):
    """Ветка спортсмена с его нынешним тренером."""
    coach = active_coach_sub(c.bucket, c.sub)
    if not coach:
        raise NoCoach()
    return c.sub, coach, "athlete"


def _athlete_thread(c):
    """Ветка тренера со спортсменом из пути."""
    return _coached_athlete(c), c.sub, "coach"


def _chat_get(c, thread):
    athlete, coach, _ = thread
    args = c.request.args
    try:
        page = read_chat(c.bucket, athlete, coach,
                         after=args.get("after"), before=args.get("before"))
    except ChatError as e:
        return jresp({"error": str(e)}, 400)
    return jresp(page, 200)


def _chat_post(c, thread):
    athlete, coach, role = thread
    try:
        message = append_chat_message(c.bucket, athlete, coach, c.sub, role,
                                      c.body().get("text"))
    except ChatError as e:
        return jresp({"error": str(e)}, 400)
    return jresp({"message": message}, 201)


def _chat_read(c, thread):
    athlete, coach, role = thread
    try:
        mark_chat_read(c.bucket, athlete, coach, role, c.body().get("last_id"))
    except ChatError as e:
        return jresp({"error": str(e)}, 400)
    return jresp({"unread": chat_unread(c.bucket, athlete, coach, role)}, 200)


def h_my_chat_get(c):       return _chat_get(c, _my_thread(c))
def h_my_chat_post(c):      return _chat_post(c, _my_thread(c))
def h_my_chat_read(c):      return _chat_read(c, _my_thread(c))
def h_coach_chat_get(c):    return _chat_get(c, _athlete_thread(c))
def h_coach_chat_post(c):   return _chat_post(c, _athlete_thread(c))
def h_coach_chat_read(c):   return _chat_read(c, _athlete_thread(c))


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


def _run_details_response(bucket, sub, run_id, include_deleted=True):
    # Ownership: run_id должен быть в runs.json пользователя (иначе ленивый
    # fallback мог бы утащить чужие/legacy данные в чужой namespace).
    own_ids = {r.get("id") for r in read_runs(bucket, sub)
               if include_deleted or not r.get("deleted", False)}
    if run_id not in own_ids:
        return jresp({"error": "Run details not found"}, 404)
    details = read_run_details(bucket, sub, run_id)
    if not details:
        return jresp({"error": "Run details not found"}, 404)
    return jresp(details, 200)


def h_run_details(c):
    return _run_details_response(c.bucket, c.sub, int(c.args[0]))


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
    api_key = body.get("api_key")
    if provider not in ("anthropic", "openai", "deepseek"):
        return jresp({"error": "Invalid provider"}, 400)
    # Модель и ключ уходят провайдеру как есть: в конфиг пишется только текст.
    for field, value in (("model", model), ("api_key", api_key)):
        if value is not None and not isinstance(value, str):
            return jresp({"error": f"Invalid {field}"}, 400)
    api_key = (api_key or "").strip()
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


def _llm_failure(reason, status, cfg, detail, error=False):
    """Ответ на сбой модели: клиенту — код причины, подробности — в лог (#53).

    Провайдер описывает отказ своими словами, и в них бывают id организации,
    лимиты и фрагмент ключа. Фразу по коду подбирает фронтенд (aiErrorText);
    админ видит текст провайдера по кнопке «Проверить ключ».
    """
    log = logging.exception if error else logging.warning
    log("LLM %s (%s/%s): %s", reason, cfg.get("provider"), cfg.get("model"), detail)
    return jresp({"error": reason}, status)


def _ask_llm(cfg, system_prompt, user_prompt, history=None):
    """(ответ LLM, None) либо (None, готовый ответ с ошибкой).

    Сбои провайдера приходят в разном виде — исключением, отказом внутри
    HTTP 200, обрывом по бюджету. Раскладка по кодам одна на всех, кто зовёт
    модель.
    """
    try:
        return call_llm(cfg["provider"], cfg["model"], cfg["api_key"],
                        system_prompt, user_prompt, effort=cfg.get("effort"),
                        history=history), None
    except LLMRefused as e:
        return None, _llm_failure("llm_refused", 422, cfg, e)
    except LLMTruncated as e:
        return None, _llm_failure("llm_truncated", 502, cfg, e)
    except httpx.HTTPStatusError as e:
        detail = f"HTTP {e.response.status_code}: {e.response.text[:1000]}"
        return None, _llm_failure("llm_provider_error", 502, cfg, detail, error=True)
    except Exception as e:
        return None, _llm_failure("llm_failed", 502, cfg, e, error=True)


def h_advise_preview(c):
    """Данные, которые ИИ-тренер получает на каждый ход, — без самого
    вопроса и без тренировок, выбранных для разбора. Путь остался от
    разовых рекомендаций: на него смотрит кнопка в Профиле."""
    turn = build_ai_turn(c.bucket, c.sub, {"run_id": None}, [])
    return jresp({"prompt": turn["data"],
                  "system_prompt": turn["instructions"]}, 200)


# ── ИИ-тренер (#46): разборы ──────────────────────────────────────────────────
# Все маршруты работают только с c.sub: чужой разбор недостижим по построению
# пути, отдельной проверки доступа здесь нет и быть не должно.

def _ai_limit(c):
    return ADMIN_DAILY_AI_COACH_LIMIT if c.is_admin else DAILY_AI_COACH_LIMIT


def _ai_usage(c):
    """Израсходовано и разрешено сообщений за сутки."""
    return {"count": read_advice_usage(c.bucket, c.sub).get("count", 0),
            "limit": _ai_limit(c)}


def h_ai_threads_get(c):
    return jresp({"threads": list_ai_threads(c.bucket, c.sub),
                  "usage": _ai_usage(c)}, 200)


class RunNotFound(Exception):
    """К разбору прикладывают пробежку, которой у пользователя нет → 404."""


def _attached_run(c):
    """Пробежка из тела запроса (run_id) или None, если её не прикладывали."""
    run_id = c.body().get("run_id")
    if run_id is None:
        return None
    run = find_own_run(c.bucket, c.sub, run_id)
    if not run:
        raise RunNotFound()
    return run


def h_ai_threads_post(c):
    thread = create_ai_thread(c.bucket, c.sub, c.body().get("title"),
                              run=_attached_run(c), created_by=c.email)
    return jresp({"thread": thread}, 201)


def h_ai_thread_get(c):
    thread = read_ai_thread(c.bucket, c.sub, c.args[0])
    if not thread:
        return jresp({"error": "thread not found"}, 404)
    messages = mark_proposal_states(c.bucket, c.sub, thread["id"],
                                    read_ai_messages(c.bucket, c.sub, thread["id"]))
    return jresp({"thread": thread, "messages": messages, "usage": _ai_usage(c)}, 200)


def h_ai_thread_archive(c):
    if not archive_ai_thread(c.bucket, c.sub, c.args[0], archived_by=c.email):
        return jresp({"error": "thread not found"}, 404)
    return jresp({"ok": True}, 200)


def h_ai_message_post(c):
    thread = read_ai_thread(c.bucket, c.sub, c.args[0])
    if not thread:
        return jresp({"error": "thread not found"}, 404)
    try:
        text = clean_ai_text(c.body().get("text"))
    except AICoachError as e:
        return jresp({"error": str(e)}, 400)
    run = _attached_run(c)
    cfg = read_llm_config_full(c.bucket)
    if not cfg or not cfg.get("api_key"):
        return jresp({"error": "LLM config not set. Обратитесь к администратору."}, 400)
    # Попытка занимается до обращения к модели (#53). «Лимит не исчерпан» и
    # прибавление — одна атомарная запись: при проверке до вызова и прибавлении
    # после одновременные запросы проходили проверку все разом.
    limit = _ai_limit(c)
    try:
        reserved = reserve_advice_usage(c.bucket, c.sub, limit)
    except UsageBusy:
        return jresp({"error": "usage_busy"}, 429)
    if reserved is None:
        return jresp({"error": "daily_limit_reached", "limit": limit}, 429)
    usage = {"count": reserved["count"], "limit": limit}

    # Ответа нет — попытка возвращается: сбой модели пользователю не в счёт.
    try:
        turn = build_ai_turn(c.bucket, c.sub, thread,
                             read_ai_messages(c.bucket, c.sub, thread["id"]), run=run)
        llm_res, failure = _ask_llm(cfg, turn["system"], text, history=turn["history"])
        if not failure:
            try:
                reply, envelope = parse_coach_reply(llm_res["text"])
            except ValueError as e:
                failure = _llm_failure("llm_empty_reply", 502, cfg, e)
    except Exception:
        release_advice_usage(c.bucket, c.sub)
        raise
    if failure:
        release_advice_usage(c.bucket, c.sub)
        return failure

    # Вопрос пишется только вместе с ответом: сбой модели не оставляет в
    # разборе реплику, на которую никто не ответил.
    ctx = turn["ctx"]
    proposal = clean_proposal(envelope.get("proposal"), turn["plan_window"])
    if proposal:
        # К какой версии плана относится «было» — по ней apply решает,
        # применимо ли ещё предложение.
        proposal.update(plan_id=ctx["plan_id"], plan_version=ctx["plan_version"])
    asked = {"created_by": c.email}
    if run:
        # Подпись хранится снимком: позже пробежку могут скрыть или поправить,
        # а в разборе должно остаться, о чём шла речь.
        asked.update(run_id=run["id"], run_title=run_title(run))
    question = append_ai_message(c.bucket, c.sub, thread["id"], "athlete", text, asked)
    answer = append_ai_message(c.bucket, c.sub, thread["id"], "ai", reply, {
        "run_ids": turn["focus_run_ids"],
        "provider": cfg["provider"], "model": cfg["model"],
        "input_tokens": llm_res["input_tokens"],
        "output_tokens": llm_res["output_tokens"],
        "based_on_llm_config_version": cfg["version"],
        "based_on_plan_id": ctx.get("plan_id"),
        "based_on_plan_version": ctx.get("plan_version"),
        "based_on_profile_version": ctx.get("profile_version", 0),
        "based_on_runs": [r.get("id") for r in ctx["last_runs"]],
        **({"proposal": proposal} if proposal else {}),
    })
    if proposal:
        answer = {**answer, "proposal_state": "open"}
    return jresp({"messages": [question, answer], "usage": usage}, 201)


def h_ai_proposal_apply(c):
    thread = read_ai_thread(c.bucket, c.sub, c.args[0])
    if not thread:
        return jresp({"error": "thread not found"}, 404)
    try:
        applied = apply_ai_proposal(c.bucket, c.sub, thread["id"], c.args[1],
                                    applied_by=c.email)
    except AICoachError as e:
        # Нечего применять — ошибка запроса; уже применено или устарело —
        # конфликт с текущим состоянием плана.
        return jresp({"error": str(e)}, 400 if str(e) == "no_proposal" else 409)
    if not applied:
        return jresp({"error": "message not found"}, 404)
    return jresp({"applied": applied}, 201)


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
    # Профиль приходит в поле profile либо плоским телом. Что-то кроме объекта
    # в profile — ошибка, а не повод взять вместо него тело.
    raw = body.get("profile")
    profile, errors = clean_athlete_profile(body if raw is None else raw)
    if errors:
        return validation_failed(errors)
    payload = write_athlete_version(
        c.bucket, c.sub, profile,
        change_reason=clean_change_reason(body.get("change_reason")),
        created_by=c.email)
    return jresp(profile_response(c.bucket, c.sub, profile,
                                  payload["version"], payload["created_at"]), 201)


def h_races_get(c):
    active = [r for r in read_races(c.bucket, c.sub) if not r.get("deleted", False)]
    return jresp(active, 200)


def _new_id():
    return int(datetime.now().timestamp() * 1000)


def h_races_post(c):
    body = c.body()
    if not body:
        return jresp({"error": "Invalid JSON"}, 400)
    for field in ["name", "date", "dist_label", "time"]:
        if field not in body:
            return jresp({"error": f"Missing field: {field}"}, 400)
    clean, errors = clean_race(body)
    if errors:
        return validation_failed(errors)
    race = {
        "id": clean["id"] or _new_id(),
        "name": clean["name"], "date": clean["date"],
        "dist_label": clean["dist_label"], "time": clean["time"],
        "deleted": False,
    }
    all_races = read_races(c.bucket, c.sub)
    all_races = [r for r in all_races if r.get("id") != race["id"]]
    all_races.insert(0, race)
    write_races(c.bucket, c.sub, all_races)
    return jresp(race, 201)


def h_races_delete(c):
    race_id, bad = c.query_id()
    if bad:
        return bad
    all_races = read_races(c.bucket, c.sub)
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
    meta, errors = clean_plan_meta(c.body())
    if errors:
        return validation_failed(errors)
    return jresp(create_plan(c.bucket, c.sub, meta), 201)


def h_plan_activate(c):
    plan = set_active_plan(c.bucket, c.sub, c.body().get("plan_id"))
    if not plan:
        return jresp({"error": "plan not found"}, 404)
    return jresp({"ok": True, "active_plan_id": plan["id"]}, 200)


def h_plan_meta(c):
    meta, errors = clean_plan_meta(c.body())
    if errors:
        return validation_failed(errors)
    plan = update_plan_meta(c.bucket, c.sub, c.args[0], meta)
    if not plan:
        return jresp({"error": "plan not found"}, 404)
    return jresp(plan, 200)


def h_plan_archive(c):
    plan = archive_plan(c.bucket, c.sub, c.args[0])
    if not plan:
        return jresp({"error": "plan not found"}, 404)
    return jresp({"ok": True, "plan": plan}, 200)


def _plan_weeks_resp(c, plan_id):
    """Недели плана. С ?meta=1 — вместе с plan_id и версией, которую клиент
    вернёт в base_version; без параметра — голый массив, как его ждёт
    фронтенд, загруженный до #51."""
    state = read_plan_state(c.bucket, c.sub, plan_id)
    return jresp(state if c.request.args.get("meta") == "1" else state["weeks"], 200)


def _plan_stale():
    # Как proposal_stale у ИИ-тренера: правка опирается на план, которого уже нет.
    return jresp({"error": "plan_stale"}, 409)


def _save_plan_weeks(c, plan_id, body):
    base_version = body.get("base_version")
    if base_version is not None and (type(base_version) is not int or base_version < 0):
        return jresp({"error": "invalid base_version"}, 400)
    weeks, errors = clean_plan_weeks(body["weeks"])
    if errors:
        return validation_failed(errors)
    try:
        result = save_plan_weeks(c.bucket, c.sub, plan_id, weeks,
                                 clean_change_reason(body.get("change_reason")), c.email,
                                 base_version=base_version)
    except PlanStale:
        return _plan_stale()
    return jresp(result, 201)


def h_plan_weeks_get(c):
    plan_id = c.args[0]
    if not find_plan(read_plans_index(c.bucket, c.sub), plan_id):
        return jresp({"error": "plan not found"}, 404)
    return _plan_weeks_resp(c, plan_id)


def h_plan_compliance(c):
    result = build_plan_compliance(c.bucket, c.sub, c.args[0])
    if result is None:
        return jresp({"error": "plan not found"}, 404)
    return jresp(result, 200)


def h_plan_weeks_post(c):
    plan_id = c.args[0]
    if not find_plan(read_plans_index(c.bucket, c.sub), plan_id):
        return jresp({"error": "plan not found"}, 404)
    body = c.body()
    if "weeks" not in body:
        return jresp({"error": "Missing weeks"}, 400)
    return _save_plan_weeks(c, plan_id, body)


def h_active_plan_weeks_get(c):
    active = get_active_plan(c.bucket, c.sub)
    # планов нет — пустые недели, план строится через конструктор
    return _plan_weeks_resp(c, active["id"] if active else None)


def h_active_plan_weeks_post(c):
    body = c.body()
    if "weeks" not in body:
        return jresp({"error": "Missing weeks"}, 400)
    active = get_active_plan(c.bucket, c.sub)
    if not active:
        return jresp({"error": "no active plan"}, 400)
    if body.get("base_plan_id") not in (None, active["id"]):
        # Активным успели сделать другой план, а номера версий у планов свои.
        return _plan_stale()
    return _save_plan_weeks(c, active["id"], body)


def h_runs_get(c):
    active = [r for r in read_runs(c.bucket, c.sub) if not r.get("deleted", False)]
    return jresp(active, 200)


def h_runs_post(c):
    body = c.body()
    if not body:
        return jresp({"error": "Invalid JSON"}, 400)
    for field in ["date", "dist"]:
        if field not in body:
            return jresp({"error": f"Missing field: {field}"}, 400)
    index = read_plans_index(c.bucket, c.sub)
    clean, errors = clean_run(body, index)
    if errors:
        return validation_failed(errors)
    # Привязка к плану: явный plan_id или активный план (#25)
    plan_id = clean["plan_id"]
    if plan_id is None:
        active = find_plan(index, index.get("active_plan_id"))
        plan_id = active["id"] if active else None

    run = {
        "id": clean["id"] or _new_id(),
        "date": clean["date"], "dist": clean["dist"],
        "type": clean["type"], "time": clean["time"],
        "pace": clean["pace"], "hr": clean["hr"],
        "feel": clean["feel"], "notes": clean["notes"],
        "plan_id": plan_id,
        "deleted": False,
    }
    if clean["fit_token"]:
        try:
            attach_fit_details_to_run(c.bucket, c.sub, run, clean["fit_token"])
        except ValueError:
            # Токена нет или он просрочен. Прочие сбои — не ошибка запроса:
            # они уходят в лог и отдаются как internal_error.
            return jresp({"error": "Failed to attach FIT details: "
                                   "token expired or invalid"}, 400)

    all_runs = read_runs(c.bucket, c.sub)
    all_runs = [r for r in all_runs if r.get("id") != run["id"]]
    all_runs.insert(0, run)
    write_runs(c.bucket, c.sub, all_runs)
    return jresp(run, 201)


def h_runs_delete(c):
    run_id, bad = c.query_id()
    if bad:
        return bad
    all_runs = read_runs(c.bucket, c.sub)
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
    ("POST",   r"^/admin/users/coach$",          h_admin_user_coach,     True),
    ("POST",   r"^/admin/migrate-legacy$",       h_admin_migrate_legacy, True),

    ("GET",    r"^/coaches$",                    h_coaches,              False),
    ("GET",    r"^/my/coach$",                   h_my_coach_get,         False),
    ("POST",   r"^/my/coach$",                   h_my_coach_post,        False),
    ("GET",    r"^/my/coach/chat$",              h_my_chat_get,          False),
    ("POST",   r"^/my/coach/chat$",              h_my_chat_post,         False),
    ("POST",   r"^/my/coach/chat/read$",         h_my_chat_read,         False),

    ("GET",    r"^/coach/athletes$",                                    h_coach_athletes,        False),
    ("GET",    r"^/coach/athletes/([\w-]+)/plans$",                     h_coach_plans,           False),
    ("GET",    r"^/coach/athletes/([\w-]+)/plans/([\w-]+)/weeks$",      h_coach_plan_weeks,      False),
    ("GET",    r"^/coach/athletes/([\w-]+)/plans/([\w-]+)/compliance$", h_coach_plan_compliance, False),
    ("GET",    r"^/coach/athletes/([\w-]+)/runs$",                      h_coach_runs,            False),
    ("GET",    r"^/coach/athletes/([\w-]+)/runs/(\d+)/details$",        h_coach_run_details,     False),
    ("GET",    r"^/coach/athletes/([\w-]+)/chat$",                      h_coach_chat_get,        False),
    ("POST",   r"^/coach/athletes/([\w-]+)/chat$",                      h_coach_chat_post,       False),
    ("POST",   r"^/coach/athletes/([\w-]+)/chat/read$",                 h_coach_chat_read,       False),

    ("POST",   r"^/runs/parse-fit$",             h_parse_fit,            False),
    ("GET",    r"^/runs/(\d+)/details$",         h_run_details,          False),

    ("GET",    r"^/config/llm$",                 h_llm_config_get,       True),
    ("POST",   r"^/config/llm$",                 h_llm_config_post,      True),
    ("POST",   r"^/config/llm/test$",            h_llm_config_test,      True),

    ("GET",    r"^/advise/preview$",             h_advise_preview,       False),

    ("GET",    r"^/ai-coach/threads$",                     h_ai_threads_get,    False),
    ("POST",   r"^/ai-coach/threads$",                     h_ai_threads_post,   False),
    ("GET",    r"^/ai-coach/threads/([\w-]+)$",            h_ai_thread_get,     False),
    ("POST",   r"^/ai-coach/threads/([\w-]+)/archive$",    h_ai_thread_archive, False),
    ("POST",   r"^/ai-coach/threads/([\w-]+)/messages$",   h_ai_message_post,   False),
    ("POST",   r"^/ai-coach/threads/([\w-]+)/messages/([\w-]+)/apply$", h_ai_proposal_apply, False),

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
    ("GET",    r"^/plans/([\w-]+)/compliance$",  h_plan_compliance,      False),

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

def _body_limit(path):
    return MAX_FIT_BODY_BYTES if path == "/runs/parse-fit" else MAX_JSON_BODY_BYTES


def _too_large(limit):
    return jresp({"error": "payload_too_large", "limit_bytes": limit}, 413)


def handle_request(request):
    if request.method == "OPTIONS":
        return ("", 204, CORS_HEADERS)

    path = request.path.rstrip("/") or "/"

    # Предел размера тела (#53) — до всего остального: объявленная длина
    # известна сразу. content_length есть не у всякого запроса, поэтому getattr.
    limit = _body_limit(path)
    if (getattr(request, "content_length", None) or 0) > limit:
        return _too_large(limit)
    # Тело без объявленной длины (chunked) так не поймать. Тот же предел
    # отдаём фреймворку: он оборвёт чтение и поднимет ошибку с кодом 413 —
    # её разбирает except внизу. Там, где атрибут только для чтения, остаётся
    # проверка выше.
    try:
        request.max_content_length = limit
    except AttributeError:
        pass

    token_info = verify_token(request)
    if not token_info:
        return jresp({"error": "Unauthorized"}, 401)

    client = get_storage_client()
    bucket = client.bucket(BUCKET_NAME)

    try:
        user = resolve_user(bucket, token_info)
    except RegistrationClosed:
        return jresp({"error": "registration_closed"}, 403)

    # /me вне таблицы намеренно: он обязан отвечать до проверки одобрения —
    # именно из него фронт узнаёт, что заявка ещё на рассмотрении.
    if path == "/me":
        return jresp({"status": user["status"], "role": user["role"],
                      "email": user.get("email"), "name": user.get("name"),
                      "is_coach": bool(user.get("is_coach")),
                      "coach": current_coach(bucket, user["sub"])}, 200)

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
    except Forbidden:
        return jresp({"error": "forbidden"}, 403)
    except RunNotFound:
        return jresp({"error": "run not found"}, 404)
    except NoCoach:
        return jresp({"error": "no_coach"}, 409)
    except Exception as e:
        if getattr(e, "code", None) == 413:     # тело больше max_content_length
            return _too_large(limit)
        # Текст исключения клиенту не отдаём (#53): в нём бывают пути объектов
        # и ответы провайдера. Он уходит в лог вместе с трассировкой.
        logging.exception("Unhandled error: %s %s", request.method, path)
        return jresp({"error": "internal_error"}, 500)
