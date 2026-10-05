"""ИИ-тренер (#46): разборы — хранилище, ход диалога и маршруты.

Модель не вызывается: `call_llm` подменён и отвечает из очереди, поэтому
проверяется то, что уходит провайдеру и что остаётся в хранилище после хода.
"""
import json

import pytest

from conftest import FakeRequest

SUB, OTHER = "u1", "u2"
ADMIN = {"sub": "admin-sub", "email": "aabramov77@gmail.com"}
STRANGER = {"sub": OTHER, "email": "other@example.com"}


def _reply(text="Неделя прошла ровно.", **extra):
    return {"text": json.dumps({"reply": text, **extra}, ensure_ascii=False),
            "input_tokens": 900, "output_tokens": 60}


@pytest.fixture
def llm(patched_api, fake_bucket, monkeypatch):
    """Подменяет модель: записывает вызовы и отвечает из очереди `replies`.
    Исключение в очереди поднимается вместо ответа."""
    patched_api.write_llm_config_version(fake_bucket, "openai", "gpt-test", "sk-test")
    calls, replies = [], []

    def fake(provider, model, api_key, system_prompt, user_prompt, effort=None, history=None):
        calls.append({"system": system_prompt, "user": user_prompt, "history": history})
        reply = replies.pop(0) if replies else _reply()
        if isinstance(reply, Exception):
            raise reply
        return reply

    monkeypatch.setattr(patched_api, "call_llm", fake)
    return {"calls": calls, "replies": replies}


def _json(response):
    return json.loads(response[0])


def _new_thread(api, title="Итоги недели", **who):
    body, code, _ = api(FakeRequest("POST", "/ai-coach/threads", {"title": title}), **who)
    assert code == 201
    return json.loads(body)["thread"]


def _say(api, thread_id, text="Как прошла неделя?", **who):
    return api(FakeRequest("POST", f"/ai-coach/threads/{thread_id}/messages",
                           {"text": text}), **who)


def _ai_objects(bucket, sub=SUB):
    return {k: v for k, v in bucket._store.items() if k.startswith(f"users/{sub}/ai_coach/")}


# ── хранилище: ветки ──────────────────────────────────────────────────────────

def test_thread_card_roundtrip(storage_module, fake_bucket):
    thread = storage_module.create_ai_thread(fake_bucket, SUB, "  Итоги \n недели  ",
                                             created_by="runner@example.com")
    assert thread["title"] == "Итоги недели"
    assert thread["created_by"] == "runner@example.com"
    assert thread["created_at"].endswith("Z")
    assert storage_module.read_ai_thread(fake_bucket, SUB, thread["id"]) == thread


@pytest.mark.parametrize("title", [None, "", "   ", 42])
def test_thread_without_a_usable_title_still_gets_one(storage_module, fake_bucket, title):
    assert storage_module.create_ai_thread(fake_bucket, SUB, title)["title"]


def test_thread_title_is_capped(storage_module, fake_bucket):
    thread = storage_module.create_ai_thread(fake_bucket, SUB, "ы" * 500)
    assert len(thread["title"]) == storage_module.AI_TITLE_MAX


@pytest.mark.parametrize("bad", ["", "nope", "../../registry", "20260101T000000000000-zzzzzzzz"])
def test_malformed_thread_id_is_not_found(storage_module, fake_bucket, bad):
    assert storage_module.read_ai_thread(fake_bucket, SUB, bad) is None


def test_list_shows_freshest_thread_first_with_counts(storage_module, fake_bucket):
    first = storage_module.create_ai_thread(fake_bucket, SUB, "первый")
    second = storage_module.create_ai_thread(fake_bucket, SUB, "второй")
    storage_module.append_ai_message(fake_bucket, SUB, second["id"], "athlete", "в")
    storage_module.append_ai_message(fake_bucket, SUB, first["id"], "athlete", "а")
    last = storage_module.append_ai_message(fake_bucket, SUB, first["id"], "ai", "б")

    listed = storage_module.list_ai_threads(fake_bucket, SUB)
    assert [t["title"] for t in listed] == ["первый", "второй"]
    assert [t["messages"] for t in listed] == [2, 1]
    assert listed[0]["last_ts"] == last["ts"]


def test_list_skips_a_thread_nobody_spoke_in(storage_module, fake_bucket):
    """Первый ход не дошёл до модели — пустой разбор в списке был бы мусором."""
    storage_module.create_ai_thread(fake_bucket, SUB, "пустой")
    assert storage_module.list_ai_threads(fake_bucket, SUB) == []


def test_archive_hides_the_thread_but_keeps_every_object(storage_module, fake_bucket):
    thread = storage_module.create_ai_thread(fake_bucket, SUB, "скрываемый")
    storage_module.append_ai_message(fake_bucket, SUB, thread["id"], "athlete", "вопрос")
    before = _ai_objects(fake_bucket)

    assert storage_module.archive_ai_thread(fake_bucket, SUB, thread["id"], "me") is True

    assert storage_module.list_ai_threads(fake_bucket, SUB) == []
    assert storage_module.read_ai_thread(fake_bucket, SUB, thread["id"]) is None
    after = _ai_objects(fake_bucket)
    assert before.items() <= after.items()            # ничего не изменено и не удалено
    (marker,) = set(after) - set(before)
    assert marker.endswith("/archived.json")
    assert json.loads(after[marker])["archived_by"] == "me"


def test_archive_of_unknown_thread_reports_nothing_to_hide(storage_module, fake_bucket):
    assert storage_module.archive_ai_thread(fake_bucket, SUB, "nope") is False
    thread = storage_module.create_ai_thread(fake_bucket, SUB, "т")
    assert storage_module.archive_ai_thread(fake_bucket, SUB, thread["id"]) is True
    assert storage_module.archive_ai_thread(fake_bucket, SUB, thread["id"]) is False


# ── хранилище: реплики ────────────────────────────────────────────────────────

def test_messages_keep_their_order_and_are_never_rewritten(storage_module, fake_bucket):
    thread = storage_module.create_ai_thread(fake_bucket, SUB, "т")
    first = storage_module.append_ai_message(fake_bucket, SUB, thread["id"], "athlete", "раз")
    path = storage_module.p_ai_msg(SUB, thread["id"], first["id"])
    stored = fake_bucket._store[path]

    for i, role in enumerate(["ai", "athlete", "ai"]):
        storage_module.append_ai_message(fake_bucket, SUB, thread["id"], role, f"m{i}")

    messages = storage_module.read_ai_messages(fake_bucket, SUB, thread["id"])
    assert [m["text"] for m in messages] == ["раз", "m0", "m1", "m2"]
    assert [m["role"] for m in messages] == ["athlete", "ai", "athlete", "ai"]
    assert fake_bucket._store[path] == stored


def test_message_extra_fields_are_stored_with_it(storage_module, fake_bucket):
    thread = storage_module.create_ai_thread(fake_bucket, SUB, "т")
    storage_module.append_ai_message(fake_bucket, SUB, thread["id"], "ai", "ответ",
                                     {"model": "gpt-test", "input_tokens": 7})
    (message,) = storage_module.read_ai_messages(fake_bucket, SUB, thread["id"])
    assert message["model"] == "gpt-test" and message["input_tokens"] == 7


@pytest.mark.parametrize("text,code", [("", "empty_message"), ("   ", "empty_message"),
                                       (None, "empty_message"), (17, "empty_message"),
                                       ("я" * 2001, "message_too_long")])
def test_message_text_is_validated(storage_module, text, code):
    with pytest.raises(storage_module.AICoachError, match=code):
        storage_module.clean_ai_text(text)


def test_message_text_is_trimmed(storage_module):
    assert storage_module.clean_ai_text("  вопрос \n") == "вопрос"


# ── история для модели ────────────────────────────────────────────────────────

def _dialogue(n):
    return [{"role": "athlete" if i % 2 == 0 else "ai", "text": f"m{i}"} for i in range(n)]


def test_history_maps_roles_and_wraps_answers_in_the_reply_envelope(storage_module):
    history = storage_module.ai_history(_dialogue(2))
    assert history[0] == {"role": "user", "content": "m0"}
    assert history[1]["role"] == "assistant"
    assert json.loads(history[1]["content"]) == {"reply": "m1"}


def test_history_is_a_window_over_the_latest_turns(storage_module):
    history = storage_module.ai_history(_dialogue(40))
    assert len(history) == storage_module.AI_HISTORY_WINDOW
    assert history[-1]["role"] == "assistant"
    assert json.loads(history[-1]["content"]) == {"reply": "m39"}


def test_history_never_opens_with_an_answer(storage_module):
    """Окно могло разрезать пару; провайдеры ждут первым ход пользователя."""
    history = storage_module.ai_history(_dialogue(40), window=5)
    assert history[0]["role"] == "user"
    assert len(history) == 4


def test_history_of_a_new_thread_is_empty(storage_module):
    assert storage_module.ai_history([]) == []


# ── разбор ответа модели ──────────────────────────────────────────────────────

def test_reply_is_taken_from_the_envelope(storage_module):
    reply, envelope = storage_module.parse_coach_reply('{"reply": " Всё хорошо. ", "x": 1}')
    assert reply == "Всё хорошо." and envelope["x"] == 1


def test_reply_survives_text_around_the_json(storage_module):
    reply, _ = storage_module.parse_coach_reply('Вот ответ:\n{"reply": "Ок"}\nУдачи!')
    assert reply == "Ок"


def test_plain_text_answer_is_used_as_is(storage_module):
    """Модель вышла из JSON-режима — ход всё равно состоялся."""
    reply, envelope = storage_module.parse_coach_reply("Просто текст без скобок.")
    assert reply == "Просто текст без скобок." and envelope == {}


@pytest.mark.parametrize("text", ["", "   ", '{"reply": ""}', '{"reply": null}',
                                  '{"answer": "не тот ключ"}', '{"reply": 5}'])
def test_empty_reply_is_an_error(storage_module, text):
    with pytest.raises(ValueError):
        storage_module.parse_coach_reply(text)


# ── ход диалога через HTTP ────────────────────────────────────────────────────

def test_turn_stores_question_and_answer(api, llm, fake_bucket, storage_module):
    thread = _new_thread(api)
    body, code, _ = _say(api, thread["id"], "  Как прошла неделя?  ")
    assert code == 201
    question, answer = json.loads(body)["messages"]

    assert question["role"] == "athlete" and question["text"] == "Как прошла неделя?"
    assert answer["role"] == "ai" and answer["text"] == "Неделя прошла ровно."
    assert question["id"] < answer["id"]
    assert storage_module.read_ai_messages(fake_bucket, SUB, thread["id"]) == [question, answer]


def test_answer_records_what_it_was_based_on(api, llm):
    thread = _new_thread(api)
    _, answer = _json(_say(api, thread["id"]))["messages"]
    assert (answer["provider"], answer["model"]) == ("openai", "gpt-test")
    assert (answer["input_tokens"], answer["output_tokens"]) == (900, 60)
    assert answer["based_on_llm_config_version"] == 1
    for field in ("based_on_plan_id", "based_on_plan_version",
                  "based_on_profile_version", "based_on_runs"):
        assert field in answer


def test_model_gets_the_question_fresh_context_and_no_history_at_first(api, llm):
    api(FakeRequest("POST", "/", json_body={"id": 1, "date": "2026-08-16", "dist": 12.5}))
    thread = _new_thread(api)
    _say(api, thread["id"], "Что с объёмом?")

    (call,) = llm["calls"]
    assert call["user"] == "Что с объёмом?"
    assert call["history"] == []
    assert "беговой тренер" in call["system"]
    assert "JSON" in call["system"]                 # без слова провайдер отвергнет JSON-режим
    assert "2026-08-16" in call["system"] and "12.5км" in call["system"]


def test_follow_up_carries_the_earlier_turns(api, llm):
    thread = _new_thread(api)
    llm["replies"].append(_reply("Первый ответ."))
    _say(api, thread["id"], "Первый вопрос")
    _say(api, thread["id"], "А почему?")

    history = llm["calls"][1]["history"]
    assert [h["role"] for h in history] == ["user", "assistant"]
    assert history[0]["content"] == "Первый вопрос"
    assert json.loads(history[1]["content"]) == {"reply": "Первый ответ."}
    assert llm["calls"][1]["user"] == "А почему?"


def test_context_is_rebuilt_on_every_turn(api, llm):
    """Пробежка, добавленная между репликами, видна уже следующему ходу."""
    thread = _new_thread(api)
    _say(api, thread["id"])
    api(FakeRequest("POST", "/", json_body={"id": 7, "date": "2026-08-20", "dist": 21.1}))
    _say(api, thread["id"])
    assert "21.1км" not in llm["calls"][0]["system"]
    assert "21.1км" in llm["calls"][1]["system"]


def test_plain_text_from_the_model_still_becomes_an_answer(api, llm):
    thread = _new_thread(api)
    llm["replies"].append({"text": "Ответ без JSON.", "input_tokens": 1, "output_tokens": 1})
    body, code, _ = _say(api, thread["id"])
    assert code == 201
    assert json.loads(body)["messages"][1]["text"] == "Ответ без JSON."


# ── сбои: в разборе не остаётся вопроса без ответа ───────────────────────────

def _failures(api_module):
    return [(api_module.LLMRefused("нет медицинских рекомендаций"), 422, "отклонила"),
            (api_module.LLMTruncated("оборвано"), 502, "глубину рассуждения"),
            (RuntimeError("connection reset"), 502, "connection reset"),
            ({"text": '{"reply": ""}', "input_tokens": 1, "output_tokens": 1}, 502, "пустой ответ")]


@pytest.mark.parametrize("case", range(4))
def test_failed_turn_writes_nothing_and_costs_nothing(api, llm, patched_api, fake_bucket, case):
    failure, expected_code, fragment = _failures(patched_api)[case]
    thread = _new_thread(api)
    before = _ai_objects(fake_bucket)
    llm["replies"].append(failure)

    body, code, _ = _say(api, thread["id"])

    assert code == expected_code and fragment in body
    assert _ai_objects(fake_bucket) == before
    assert patched_api.read_advice_usage(fake_bucket, SUB)["count"] == 0


def test_turn_after_a_failure_goes_through(api, llm):
    thread = _new_thread(api)
    llm["replies"].append(RuntimeError("timeout"))
    assert _say(api, thread["id"])[1] == 502
    body, code, _ = _say(api, thread["id"])
    assert code == 201 and len(json.loads(body)["messages"]) == 2


# ── отказы до вызова модели ───────────────────────────────────────────────────

@pytest.mark.parametrize("text,error", [("", "empty_message"), ("   ", "empty_message"),
                                        ("я" * 2001, "message_too_long")])
def test_bad_text_is_rejected_before_the_model(api, llm, text, error):
    thread = _new_thread(api)
    body, code, _ = _say(api, thread["id"], text)
    assert code == 400 and error in body
    assert llm["calls"] == []


def test_turn_without_llm_config_is_refused(api):
    thread = _new_thread(api)
    body, code, _ = _say(api, thread["id"])
    assert code == 400 and "LLM config not set" in body


@pytest.mark.parametrize("thread_id", ["20260101T000000000000-deadbeef", "nope"])
def test_unknown_thread_is_404_everywhere(api, llm, thread_id):
    base = f"/ai-coach/threads/{thread_id}"
    assert api(FakeRequest("GET", base))[1] == 404
    assert api(FakeRequest("POST", f"{base}/messages", {"text": "вопрос"}))[1] == 404
    assert api(FakeRequest("POST", f"{base}/archive"))[1] == 404
    assert llm["calls"] == []


def test_archived_thread_takes_no_more_messages(api, llm):
    thread = _new_thread(api)
    _say(api, thread["id"])
    assert api(FakeRequest("POST", f"/ai-coach/threads/{thread['id']}/archive"))[1] == 200
    assert _say(api, thread["id"])[1] == 404
    assert _json(api(FakeRequest("GET", "/ai-coach/threads")))["threads"] == []


# ── суточный лимит ────────────────────────────────────────────────────────────

def _spend(storage_module, bucket, sub, count):
    for _ in range(count):
        storage_module.increment_advice_usage(bucket, sub)


def test_usage_counts_each_message_and_is_reported(api, llm):
    thread = _new_thread(api)
    assert _json(_say(api, thread["id"]))["usage"] == {"count": 1, "limit": 30}
    assert _json(_say(api, thread["id"]))["usage"] == {"count": 2, "limit": 30}
    assert _json(api(FakeRequest("GET", "/ai-coach/threads")))["usage"] == {"count": 2, "limit": 30}


def test_daily_limit_stops_the_turn_before_the_model(api, llm, storage_module, fake_bucket):
    thread = _new_thread(api)
    _spend(storage_module, fake_bucket, SUB, 30)
    body, code, _ = _say(api, thread["id"])
    assert code == 429
    assert json.loads(body) == {"error": "daily_limit_reached", "limit": 30}
    assert llm["calls"] == []


def test_admin_has_a_higher_limit(api, llm, storage_module, fake_bucket):
    thread = _new_thread(api, **ADMIN)
    _spend(storage_module, fake_bucket, ADMIN["sub"], 30)
    body, code, _ = _say(api, thread["id"], **ADMIN)
    assert code == 201 and json.loads(body)["usage"] == {"count": 31, "limit": 300}


# ── изоляция ──────────────────────────────────────────────────────────────────

def test_thread_is_invisible_to_another_user(api, llm, fake_bucket):
    thread = _new_thread(api)
    _say(api, thread["id"], "личный вопрос")
    base = f"/ai-coach/threads/{thread['id']}"
    before = _ai_objects(fake_bucket)

    assert _json(api(FakeRequest("GET", "/ai-coach/threads"), **STRANGER))["threads"] == []
    assert api(FakeRequest("GET", base), **STRANGER)[1] == 404
    assert api(FakeRequest("POST", f"{base}/messages", {"text": "чужой"}), **STRANGER)[1] == 404
    assert api(FakeRequest("POST", f"{base}/archive"), **STRANGER)[1] == 404

    assert _ai_objects(fake_bucket) == before
    assert _ai_objects(fake_bucket, OTHER) == {}


def test_thread_view_returns_card_and_messages(api, llm):
    thread = _new_thread(api, "Разбор длительной")
    _say(api, thread["id"])
    view = _json(api(FakeRequest("GET", f"/ai-coach/threads/{thread['id']}")))
    assert view["thread"]["title"] == "Разбор длительной"
    assert [m["role"] for m in view["messages"]] == ["athlete", "ai"]
    assert view["usage"]["count"] == 1

    (listed,) = _json(api(FakeRequest("GET", "/ai-coach/threads")))["threads"]
    assert listed["id"] == thread["id"] and listed["messages"] == 2


# ══ Фаза 2: разбор выбранной тренировки ══════════════════════════════════════

DETAILS = {
    "laps": [
        {"lap": 1, "dist_km": 1.0, "duration_sec": 330, "pace": "5:30", "avg_hr": 138, "max_hr": 145, "cadence": 170},
        {"lap": 2, "dist_km": 1.0, "duration_sec": 320, "pace": "5:20", "avg_hr": 146, "max_hr": 151, "cadence": 172},
        {"lap": 3, "dist_km": 1.0, "duration_sec": 310, "pace": "5:10", "avg_hr": 152, "max_hr": 158, "cadence": 174},
        {"lap": 4, "dist_km": 1.0, "duration_sec": 300, "pace": "5:00", "avg_hr": 158, "max_hr": 166, "cadence": 176},
    ],
    "samples": {"t_offset_sec": [0, 20, 40, 60, 80], "hr": [100, 120, 140, 160, 170],
                "pace_sec_per_km": [330, 325, 315, 305, 300], "altitude_m": [10, 10, 11, 11, 12]},
}
ZONES = [{"name": "Z1 восстановление", "from": 95, "to": 114},
         {"name": "Z2 аэробная", "from": 114, "to": 133},
         {"name": "Z3 темповая", "from": 133, "to": 152},
         {"name": "Z4 ПАНО", "from": 152, "to": 171},
         {"name": "Z5 максимальная", "from": 171, "to": 190}]
RUN_TITLE = "Длительный 4 км · 2026-08-23"
FOCUS_MARKER = "Тренировки, выбранные спортсменом для разбора"


def _seed_details(bucket, run_id, details=DETAILS, sub=SUB):
    path = f"users/{sub}/runs/{run_id}/v1/details.json"
    bucket.blob(path).upload_from_string(json.dumps(details))
    bucket.blob(f"users/{sub}/runs/{run_id}/manifest.json").upload_from_string(
        json.dumps({"current_version": 1, "gcs_object_path": path}))


def _seed_reviewable_run(api, patched_api, fake_bucket):
    """План на две недели и длительная с деталями в воскресенье первой.
    Возвращает id пробежки."""
    plan = patched_api.create_plan(fake_bucket, SUB, {"race_name": "HM", "plan_start": "2026-08-17"})
    patched_api.save_plan_weeks(fake_bucket, SUB, plan["id"], [
        {"w": 1, "start": "17.08", "type": "dev", "accent": "Развитие",
         "mon": "10 км", "sun": "16 км легко"},
        {"w": 2, "start": "24.08", "type": "peak", "mon": "12 км"},
    ], "seed")
    api(FakeRequest("POST", "/", json_body={
        "id": 501, "date": "2026-08-23", "dist": 4.0, "type": "long", "time": "21:00",
        "pace": "5:15", "hr": 149, "feel": "hard", "notes": "тяжело в конце"}))
    runs = patched_api.read_runs(fake_bucket, SUB)
    next(r for r in runs if r["id"] == 501)["details_available"] = True
    patched_api.write_runs(fake_bucket, SUB, runs)
    _seed_details(fake_bucket, 501)
    return 501


def _run_thread(api, run_id, **who):
    body, code, _ = api(FakeRequest("POST", "/ai-coach/threads", {"run_id": run_id}), **who)
    assert code == 201
    return json.loads(body)["thread"]


# ── расчёты по деталям ────────────────────────────────────────────────────────

def test_half_paces_compare_the_two_halves_of_the_laps(storage_module):
    assert storage_module.half_paces(DETAILS) == (325, 305)


@pytest.mark.parametrize("laps", [[], DETAILS["laps"][:3],
                                  [{"lap": 1, "dist_km": 0, "duration_sec": 0}] * 6])
def test_half_paces_need_enough_real_laps(storage_module, laps):
    assert storage_module.half_paces({"laps": laps}) is None


def test_zone_minutes_follow_the_samples(storage_module):
    """Сэмпл держит время до следующего: по 20 секунд в Z1–Z4, последний не в счёт."""
    zones = storage_module.hr_zone_minutes(DETAILS, ZONES)
    assert [(z["name"], z["min"], z["pct"]) for z in zones] == [
        ("Z1 восстановление", 0.3, 25), ("Z2 аэробная", 0.3, 25),
        ("Z3 темповая", 0.3, 25), ("Z4 ПАНО", 0.3, 25)]


def test_zone_minutes_keep_time_below_the_first_zone(storage_module):
    """Иначе проценты остальных зон завышены."""
    details = {"samples": {"t_offset_sec": [0, 10, 20], "hr": [80, 120, 120]}}
    zones = storage_module.hr_zone_minutes(details, ZONES)
    assert [(z["name"], z["pct"]) for z in zones] == [("ниже Z1", 50), ("Z2 аэробная", 50)]


def test_zone_minutes_do_not_count_a_watch_pause(storage_module):
    details = {"samples": {"t_offset_sec": [0, 10, 910, 920], "hr": [120, 120, 120, 120]}}
    (zone,) = storage_module.hr_zone_minutes(details, ZONES)
    assert zone["min"] == round((10 + storage_module.ZONE_GAP_CAP_SEC + 10) / 60, 1)


@pytest.mark.parametrize("details,zones", [
    ({}, ZONES), (DETAILS, []), (DETAILS, None),
    ({"samples": {"t_offset_sec": [0], "hr": [120]}}, ZONES),
    ({"samples": {"t_offset_sec": [0, 5, 10], "hr": [None, None, None]}}, ZONES),
    ({"samples": {"t_offset_sec": [0, 5, 10], "hr": [120]}}, ZONES),
])
def test_zone_minutes_without_data_are_empty(storage_module, details, zones):
    assert storage_module.hr_zone_minutes(details, zones) == []


# ── какие пробежки в фокусе ───────────────────────────────────────────────────

def _asked(run_id=None):
    return {"role": "athlete", "text": "в", **({"run_id": run_id} if run_id else {})}


def test_focus_is_the_thread_run_plus_attached_ones(storage_module):
    ids = storage_module.ai_focus_run_ids({"run_id": 1}, [_asked(2), _asked()], run_id=3)
    assert ids == [1, 2, 3]


def test_focus_of_a_general_thread_without_attachments_is_empty(storage_module):
    assert storage_module.ai_focus_run_ids({"run_id": None}, [_asked(), _asked()]) == []


def test_focus_keeps_the_latest_runs_and_moves_a_repeat_to_the_end(storage_module):
    messages = [_asked(2), _asked(3), _asked(4)]
    assert storage_module.ai_focus_run_ids({"run_id": 1}, messages) == [2, 3, 4]
    assert storage_module.ai_focus_run_ids({"run_id": 1}, messages, run_id=2) == [3, 4, 2]


def test_focus_forgets_runs_attached_before_the_history_window(storage_module):
    messages = [_asked(9)] + [_asked() for _ in range(storage_module.AI_HISTORY_WINDOW)]
    assert storage_module.ai_focus_run_ids({"run_id": None}, messages) == []


def test_run_title_survives_missing_fields(storage_module):
    assert storage_module.run_title({"type": "tempo", "dist": 10.0, "date": "2026-08-20"}) == \
        "Темповый 10 км · 2026-08-20"
    assert storage_module.run_title({}) == "Тренировка"
    assert storage_module.run_title({"dist": "много", "date": "2026-08-20"}) == "Тренировка · 2026-08-20"


# ── блок пробежки в промпте ───────────────────────────────────────────────────

def _focus(storage_module, **over):
    run = {"id": 501, "date": "2026-08-23", "dist": 4.0, "type": "long", "time": "21:00",
           "pace": "5:15", "hr": 149, "max_hr": 166, "feel": "hard", "notes": "тяжело в конце"}
    focus = {"run": run, "laps": DETAILS["laps"], "hr_drift_pct": 8.9,
             "half_paces": (325, 305), "zones": storage_module.hr_zone_minutes(DETAILS, ZONES),
             "hr_max": 190, "hr_max_estimated": False,
             "planned": {"week": 1, "day": "sun", "text": "16 км легко", "phase": "dev", "accent": ""}}
    focus.update(over)
    return focus


def _block(storage_module, **over):
    import llm_prompt
    return llm_prompt.format_run_focus(_focus(storage_module, **over))


def test_run_block_puts_plan_laps_and_derived_figures_together(storage_module):
    text = _block(storage_module)
    assert "Тренировка 2026-08-23, длительный" in text
    assert "4.0 км, за 21:00, темп 5:15/км, пульс ср. 149, макс 166" in text
    assert "Ощущения спортсмена: тяжело" in text and "тяжело в конце" in text
    assert "По плану в этот день (неделя 1, Развитие): «16 км легко»" in text
    assert "  1: 1 км, за 5:30, темп 5:30/км, пульс 138/145, каденс 170" in text
    assert "Темп по половинам (по кругам): 5:25 → 5:05/км" in text
    assert "HR-drift: +8.9%" in text
    assert "Время в пульсовых зонах (макс. пульс 190): Z1 восстановление — 0.3 мин (25%)" in text


def test_run_block_says_when_max_hr_is_only_an_estimate(storage_module):
    assert "оценка по возрасту, не измерялся" in _block(storage_module, hr_max_estimated=True)


def test_run_block_distinguishes_a_rest_day_from_an_unknown_plan(storage_module):
    rest = _block(storage_module, planned={"week": 2, "day": "tue", "text": "", "phase": "peak"})
    assert "(неделя 2, Пик формы) тренировки не было" in rest
    unknown = _block(storage_module, planned=None)
    assert "неизвестно" in unknown and "тренировки не было" not in unknown


def test_run_block_without_details_admits_it(storage_module):
    text = _block(storage_module, laps=[], zones=[], half_paces=None, hr_drift_pct=None)
    assert "Детальных данных" in text and "только сводка" in text
    assert "Круги" not in text and "HR-drift" not in text


def test_run_block_caps_the_lap_table(storage_module):
    import llm_prompt
    laps = [dict(DETAILS["laps"][0], lap=i) for i in range(1, 61)]
    text = _block(storage_module, laps=laps)
    assert f"  {llm_prompt.FOCUS_LAPS_MAX}: " in text
    assert f"  {llm_prompt.FOCUS_LAPS_MAX + 1}: " not in text
    assert f"и ещё {60 - llm_prompt.FOCUS_LAPS_MAX}" in text


# ── разбор тренировки через HTTP ──────────────────────────────────────────────

def test_run_thread_is_named_after_the_run_and_keeps_it_in_focus(api, llm, patched_api, fake_bucket):
    run_id = _seed_reviewable_run(api, patched_api, fake_bucket)
    thread = _run_thread(api, run_id)
    assert (thread["kind"], thread["run_id"], thread["title"]) == ("run", run_id, RUN_TITLE)

    _say(api, thread["id"], "Разбери тренировку")
    _say(api, thread["id"], "А что с пульсом?")
    for call in llm["calls"]:                      # фокус держится весь разбор
        system = call["system"]
        assert FOCUS_MARKER in system
        assert "По плану в этот день (неделя 1, Развитие): «16 км легко»" in system
        assert "  4: 1 км, за 5:00, темп 5:00/км, пульс 158/166, каденс 176" in system
        assert "Темп по половинам (по кругам): 5:25 → 5:05/км" in system
        assert "HR-drift" in system


def test_run_can_be_attached_to_a_question_in_any_thread(api, llm, patched_api, fake_bucket):
    run_id = _seed_reviewable_run(api, patched_api, fake_bucket)
    thread = _new_thread(api)
    _say(api, thread["id"], "Общий вопрос")
    body, code, _ = api(FakeRequest("POST", f"/ai-coach/threads/{thread['id']}/messages",
                                    {"text": "Сравни с этой", "run_id": run_id}))
    assert code == 201
    question, answer = json.loads(body)["messages"]
    assert (question["run_id"], question["run_title"]) == (run_id, RUN_TITLE)
    assert answer["run_ids"] == [run_id]

    _say(api, thread["id"], "И ещё вопрос")
    assert [FOCUS_MARKER in call["system"] for call in llm["calls"]] == [False, True, True]


def test_general_question_carries_no_run_block(api, llm, patched_api, fake_bucket):
    _seed_reviewable_run(api, patched_api, fake_bucket)
    thread = _new_thread(api)
    _, answer = _json(_say(api, thread["id"]))["messages"]
    assert answer["run_ids"] == []
    assert FOCUS_MARKER not in llm["calls"][0]["system"]


def test_zones_reach_the_prompt_when_the_profile_gives_a_max_hr(api, llm, patched_api, fake_bucket):
    run_id = _seed_reviewable_run(api, patched_api, fake_bucket)
    assert api(FakeRequest("POST", "/profile", {"profile": {"hr_max": 190}}))[1] == 201
    thread = _run_thread(api, run_id)
    _say(api, thread["id"])
    assert "Время в пульсовых зонах (макс. пульс 190): " in llm["calls"][0]["system"]


def test_run_without_fit_details_is_reviewed_by_its_summary(api, llm):
    api(FakeRequest("POST", "/", json_body={"id": 7, "date": "2026-08-20", "dist": 8.0,
                                            "pace": "5:40"}))
    thread = _run_thread(api, 7)
    assert _say(api, thread["id"])[1] == 201
    system = llm["calls"][0]["system"]
    assert "Тренировка 2026-08-20" in system and "только сводка" in system


@pytest.mark.parametrize("run_id", [999, "abc", "", [], {"id": 1}])
def test_unknown_run_is_404_and_nothing_is_created(api, llm, fake_bucket, run_id):
    thread = _new_thread(api)
    before = _ai_objects(fake_bucket)
    assert api(FakeRequest("POST", "/ai-coach/threads", {"run_id": run_id}))[1] == 404
    body, code, _ = api(FakeRequest("POST", f"/ai-coach/threads/{thread['id']}/messages",
                                    {"text": "вопрос", "run_id": run_id}))
    assert code == 404 and "run not found" in body
    assert _ai_objects(fake_bucket) == before and llm["calls"] == []


def test_another_users_run_cannot_be_reviewed(api, llm, patched_api, fake_bucket):
    run_id = _seed_reviewable_run(api, patched_api, fake_bucket)
    assert api(FakeRequest("POST", "/ai-coach/threads", {"run_id": run_id}), **STRANGER)[1] == 404
    thread = _new_thread(api, **STRANGER)
    response = api(FakeRequest("POST", f"/ai-coach/threads/{thread['id']}/messages",
                               {"text": "чужая", "run_id": run_id}), **STRANGER)
    assert response[1] == 404 and llm["calls"] == []


def test_hidden_run_cannot_be_attached_and_drops_out_of_focus(api, llm, patched_api, fake_bucket):
    run_id = _seed_reviewable_run(api, patched_api, fake_bucket)
    thread = _run_thread(api, run_id)
    _say(api, thread["id"])
    assert api(FakeRequest("DELETE", "/", args={"id": str(run_id)}))[1] == 200

    assert api(FakeRequest("POST", "/ai-coach/threads", {"run_id": run_id}))[1] == 404
    body, code, _ = _say(api, thread["id"], "Что дальше?")
    assert code == 201                               # разбор продолжается без блока
    assert json.loads(body)["messages"][1]["run_ids"] == []
    assert FOCUS_MARKER not in llm["calls"][-1]["system"]


def test_planned_day_comes_from_the_runs_own_plan(api, llm, patched_api, fake_bucket):
    """Пробежка привязана к прежнему плану — текст дня берётся из него, а не
    из активного."""
    run_id = _seed_reviewable_run(api, patched_api, fake_bucket)
    other = patched_api.create_plan(fake_bucket, SUB, {"race_name": "Новый", "plan_start": "2026-08-17"})
    patched_api.save_plan_weeks(fake_bucket, SUB, other["id"],
                                [{"w": 1, "start": "17.08", "sun": "отдых"}], "seed")
    thread = _run_thread(api, run_id)
    _say(api, thread["id"])
    assert "«16 км легко»" in llm["calls"][0]["system"]


# ══ Фаза 3: правки плана кнопкой ═════════════════════════════════════════════

import datetime as dt

WINDOW_WEEKS = [
    {"w": 1, "start": "17.08", "type": "dev", "mon": "10 км", "wed": " 8 км ", "sun": "16 км"},
    {"w": 2, "start": "24.08", "type": "peak", "mon": "12 км", "wed": "6×800 м"},
    {"w": 3, "start": "31.08", "type": "taper", "mon": "8 км"},
]
WEDNESDAY = dt.date(2026, 8, 19)          # среда первой недели WINDOW_WEEKS


def _window(storage_module, today=WEDNESDAY, weeks=WINDOW_WEEKS, week_idx=0, **kw):
    return storage_module.ai_plan_window(weeks, "2026-08-17", week_idx, today=today, **kw)


def _change(week=2, day="wed", text="6 км легко", reason="разгрузка", **over):
    return {"week": week, "day": day, "text": text, "reason": reason, **over}


def _proposal(*changes, summary="Разгрузить вторую неделю"):
    return {"summary": summary, "changes": list(changes) or [_change()]}


# ── окно плана, открытое для правок ───────────────────────────────────────────

def test_plan_window_lists_weeks_days_and_what_already_passed(storage_module):
    first, second, third = _window(storage_module)
    assert (first["week"], first["current"], first["start"], first["end"]) == \
        (1, True, "2026-08-17", "2026-08-23")
    assert (second["week"], second["current"], second["phase"]) == (2, False, "peak")
    assert third["week"] == 3

    by_field = {d["field"]: d for d in first["days"]}
    assert [d["field"] for d in first["days"]] == ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    assert by_field["wed"] == {"field": "wed", "date": "2026-08-19", "text": "8 км", "past": False}
    assert by_field["mon"]["past"] and by_field["tue"]["past"]      # сегодня — среда
    assert not by_field["sun"]["past"] and by_field["thu"]["text"] == ""
    assert not any(d["past"] for d in second["days"])


def test_plan_window_orders_days_by_date_for_a_sunday_first_row(storage_module):
    weeks = [{"start": "16.08", "sun": "длительная", "mon": "отдых"}]
    (week,) = storage_module.ai_plan_window(weeks, "2026-08-16", 0, today="2026-08-16")
    assert [d["field"] for d in week["days"]][0] == "sun"
    assert week["days"][0]["date"] == "2026-08-16"


def test_plan_window_is_capped_and_starts_at_the_current_week(storage_module):
    weeks = [{"start": (dt.date(2026, 8, 17) + dt.timedelta(days=7 * i)).strftime("%d.%m.%Y")}
             for i in range(10)]
    window = storage_module.ai_plan_window(weeks, "2026-08-17", 2, today="2026-09-01")
    assert [w["week"] for w in window] == [3, 4, 5, 6]


@pytest.mark.parametrize("weeks,plan_start,today", [
    ([], "2026-08-17", WEDNESDAY),                                  # плана нет
    ([{"mon": "10 км"}], None, WEDNESDAY),                          # план не датирован
    (WINDOW_WEEKS, "2026-08-17", dt.date(2026, 12, 1)),             # план закончился
])
def test_plan_window_is_empty_when_there_is_nothing_safe_to_edit(storage_module, weeks,
                                                                 plan_start, today):
    idx = max(len(weeks) - 1, 0)
    assert storage_module.ai_plan_window(weeks, plan_start, idx, today=today) == []


def test_plan_window_text_names_every_editable_cell(storage_module):
    import llm_prompt
    text = llm_prompt.format_plan_window(_window(storage_module))
    assert "Неделя 1 (текущая), 2026-08-17 – 2026-08-23, Развитие" in text
    assert "  mon (пн 2026-08-17): 10 км (прошёл, менять нельзя)" in text
    assert "  wed (ср 2026-08-19): 8 км" in text and "8 км (прошёл" not in text
    assert "  thu (чт 2026-08-20): —" in text
    assert "Неделя 2, 2026-08-24 – 2026-08-30, Пик формы" in text


def test_reply_format_offers_proposals_only_with_a_plan_window(storage_module):
    import llm_prompt
    with_plan = llm_prompt.coach_chat_system_prompt("ctx", (), "=== План: что можно менять ===")
    without = llm_prompt.coach_chat_system_prompt("ctx")
    assert '"proposal"' in with_plan and "План: что можно менять" in with_plan
    assert "proposal" not in without and "JSON" in without


# ── проверка предложения модели ───────────────────────────────────────────────

def test_valid_proposal_keeps_what_the_cell_held(storage_module):
    cleaned = storage_module.clean_proposal(_proposal(), _window(storage_module))
    assert cleaned == {"summary": "Разгрузить вторую неделю", "changes": [
        {"week": 2, "day": "wed", "date": "2026-08-26", "old": "6×800 м",
         "text": "6 км легко", "reason": "разгрузка"}]}


@pytest.mark.parametrize("bad", [
    _change(week=9), _change(week=0), _change(week="2"), _change(week=True), _change(week=2.0),
    _change(day="funday"), _change(day=None), _change(day=["wed"]),
    _change(week=1, day="mon"),                  # понедельник первой недели уже прошёл
    _change(text="6×800 м"),                     # то же, что и так стоит в плане
    _change(text="  6×800   м "),                # то же после схлопывания пробелов
    _change(text=None), _change(text=42),
    "не правка", None, ["week", 2],
])
def test_bad_change_is_dropped_and_nothing_else_makes_no_proposal(storage_module, bad):
    assert storage_module.clean_proposal({"changes": [bad]}, _window(storage_module)) is None


def test_bad_changes_do_not_take_the_good_ones_with_them(storage_module):
    raw = _proposal(_change(week=9), _change(), _change(day="funday"), _change(week=3, day="mon", text=""))
    cleaned = storage_module.clean_proposal(raw, _window(storage_module))
    assert [(c["week"], c["day"], c["text"]) for c in cleaned["changes"]] == \
        [(2, "wed", "6 км легко"), (3, "mon", "")]           # пустой текст — день отдыха


@pytest.mark.parametrize("raw", [None, "текст", [], {}, {"changes": "нет"}, {"changes": []},
                                 {"summary": "без правок"}])
def test_malformed_proposal_is_no_proposal(storage_module, raw):
    assert storage_module.clean_proposal(raw, _window(storage_module)) is None


def test_proposal_for_a_plan_without_a_window_is_dropped(storage_module):
    assert storage_module.clean_proposal(_proposal(), []) is None


def test_repeated_cell_keeps_the_last_change(storage_module):
    raw = _proposal(_change(text="первый вариант"), _change(text="второй вариант"))
    (change,) = storage_module.clean_proposal(raw, _window(storage_module))["changes"]
    assert change["text"] == "второй вариант"


def test_changes_are_ordered_by_date_capped_and_trimmed(storage_module):
    weeks = [{"start": (dt.date(2026, 8, 17) + dt.timedelta(days=7 * i)).strftime("%d.%m.%Y")}
             for i in range(4)]
    window = storage_module.ai_plan_window(weeks, "2026-08-17", 0, today="2026-08-17")
    changes = [_change(week=w, day=d, text=f"тренировка {w}{d}" + " очень" * 80, reason=None)
               for w in (4, 3, 2, 1) for d in ("sun", "wed", "mon", "fri", "tue")]
    cleaned = storage_module.clean_proposal({"summary": 7, "changes": changes}, window)

    assert cleaned["summary"] == "Корректировка плана"
    assert len(cleaned["changes"]) == storage_module.AI_PROPOSAL_MAX_CHANGES
    dates = [c["date"] for c in cleaned["changes"]]
    assert dates == sorted(dates) and dates[0] == "2026-08-17"
    assert all(len(c["text"]) == storage_module.AI_PROPOSAL_TEXT_MAX for c in cleaned["changes"])
    assert all(c["reason"] == "" for c in cleaned["changes"])


def test_history_reminds_the_model_what_it_proposed(storage_module):
    cleaned = storage_module.clean_proposal(_proposal(), _window(storage_module))
    messages = [{"role": "athlete", "text": "Облегчи неделю"},
                {"role": "ai", "text": "Предлагаю так.",
                 "proposal": {**cleaned, "plan_id": "p1", "plan_version": 3}}]
    envelope = json.loads(storage_module.ai_history(messages)[1]["content"])
    assert envelope == {"reply": "Предлагаю так.", "proposal": {
        "summary": "Разгрузить вторую неделю",
        "changes": [{"week": 2, "day": "wed", "text": "6 км легко", "reason": "разгрузка"}]}}


# ── предложение и его применение через HTTP ───────────────────────────────────
# Даты плана считаются от сегодняшнего дня: сервер сам решает, что уже
# прошло, и план из прошлого года правке бы не подлежал.

def _seed_current_plan(patched_api, fake_bucket, sub=SUB, name="HM"):
    """План из трёх недель, первая — текущая. Возвращает id плана."""
    today = dt.datetime.utcnow().date()
    monday = today - dt.timedelta(days=today.weekday())
    plan = patched_api.create_plan(fake_bucket, sub, {"race_name": name,
                                                      "plan_start": monday.isoformat()})
    patched_api.save_plan_weeks(fake_bucket, sub, plan["id"], [
        {"w": i + 1, "start": (monday + dt.timedelta(days=7 * i)).strftime("%d.%m.%Y"),
         "type": "dev", "mon": "10 км", "wed": "6×800 м", "sun": "16 км"}
        for i in range(3)], "seed", "runner@example.com")
    return plan["id"]


def _propose(api, llm, *changes, thread=None, text="Облегчи вторую неделю"):
    """Ход, на который модель отвечает предложением. Возвращает (thread, ответ ИИ)."""
    thread = thread or _new_thread(api)
    llm["replies"].append(_reply("Предлагаю разгрузить.", proposal=_proposal(*changes)))
    body, code, _ = _say(api, thread["id"], text)
    assert code == 201
    return thread, json.loads(body)["messages"][1]


def _apply(api, thread, message_id, **who):
    return api(FakeRequest("POST",
                           f"/ai-coach/threads/{thread['id']}/messages/{message_id}/apply"), **who)


def _plan_state(patched_api, fake_bucket, plan_id, sub=SUB):
    import storage
    manifest = storage.read_plan_manifest(fake_bucket, sub, plan_id)
    return manifest, storage.read_plan_weeks(fake_bucket, sub, plan_id)


def test_model_sees_the_editable_plan_and_the_proposal_format(api, llm, patched_api, fake_bucket):
    _seed_current_plan(patched_api, fake_bucket)
    thread = _new_thread(api)
    _say(api, thread["id"])
    system = llm["calls"][0]["system"]
    assert "=== План: что можно менять ===" in system
    assert "Неделя 2, " in system and "): 6×800 м" in system
    assert '"proposal"' in system


def test_without_a_plan_the_model_is_not_offered_proposals(api, llm):
    thread = _new_thread(api)
    llm["replies"].append(_reply("Плана нет.", proposal=_proposal()))
    _, answer = _json(_say(api, thread["id"]))["messages"]
    assert "proposal" not in llm["calls"][0]["system"]
    assert "proposal" not in answer and "proposal_state" not in answer


def test_proposal_is_stored_with_the_plan_version_it_refers_to(api, llm, patched_api, fake_bucket, storage_module):
    plan_id = _seed_current_plan(patched_api, fake_bucket)
    thread, answer = _propose(api, llm)

    proposal = answer["proposal"]
    assert (proposal["plan_id"], proposal["plan_version"]) == (plan_id, 1)
    assert proposal["summary"] == "Разгрузить вторую неделю"
    (change,) = proposal["changes"]
    assert (change["week"], change["day"], change["old"], change["text"]) == \
        (2, "wed", "6×800 м", "6 км легко")
    assert answer["proposal_state"] == "open"

    # Состояние считается на чтении и в объект реплики не пишется.
    stored = storage_module.read_ai_message(fake_bucket, SUB, thread["id"], answer["id"])
    assert stored["proposal"] == proposal and "proposal_state" not in stored
    # Сам план предложение не трогает.
    assert _plan_state(patched_api, fake_bucket, plan_id)[0]["current_version"] == 1


def test_unusable_proposal_does_not_cost_the_answer(api, llm, patched_api, fake_bucket):
    _seed_current_plan(patched_api, fake_bucket)
    thread, answer = _propose(api, llm, _change(week=40), _change(day="funday"))
    assert answer["text"] == "Предлагаю разгрузить."
    assert "proposal" not in answer and "proposal_state" not in answer


def test_apply_writes_a_new_plan_version_and_keeps_the_old_one(api, llm, patched_api, fake_bucket):
    plan_id = _seed_current_plan(patched_api, fake_bucket)
    thread, answer = _propose(api, llm, _change(), _change(week=3, day="sun", text="12 км легко"))
    old_path = _plan_state(patched_api, fake_bucket, plan_id)[0]["gcs_object_path"]
    old_object = fake_bucket._store[old_path]

    body, code, _ = _apply(api, thread, answer["id"])
    assert code == 201
    applied = json.loads(body)["applied"]
    assert (applied["plan_id"], applied["from_version"], applied["to_version"]) == (plan_id, 1, 2)
    assert applied["applied_by"] == "runner@example.com"

    manifest, weeks = _plan_state(patched_api, fake_bucket, plan_id)
    assert manifest["current_version"] == 2
    assert manifest["change_reason"] == "ИИ-тренер: Разгрузить вторую неделю"
    assert manifest["created_by"] == "runner@example.com"
    assert weeks[1]["wed"] == "6 км легко" and weeks[2]["sun"] == "12 км легко"
    # остальное — как было
    assert weeks[0] == {**weeks[0], "mon": "10 км", "wed": "6×800 м", "sun": "16 км"}
    assert weeks[1]["mon"] == "10 км" and weeks[1]["sun"] == "16 км"
    version = json.loads(fake_bucket._store[manifest["gcs_object_path"]])
    assert version["supersedes_version"] == 1
    assert fake_bucket._store[old_path] == old_object           # прежняя версия цела


def test_applied_proposal_is_reported_and_cannot_be_applied_twice(api, llm, patched_api, fake_bucket):
    plan_id = _seed_current_plan(patched_api, fake_bucket)
    thread, answer = _propose(api, llm)
    assert _apply(api, thread, answer["id"])[1] == 201

    view = _json(api(FakeRequest("GET", f"/ai-coach/threads/{thread['id']}")))
    assert view["messages"][1]["proposal_state"] == "applied"

    body, code, _ = _apply(api, thread, answer["id"])
    assert code == 409 and json.loads(body) == {"error": "already_applied"}
    assert _plan_state(patched_api, fake_bucket, plan_id)[0]["current_version"] == 2


def test_proposal_goes_stale_when_the_plan_moves_on(api, llm, patched_api, fake_bucket):
    plan_id = _seed_current_plan(patched_api, fake_bucket)
    thread, answer = _propose(api, llm)
    _, weeks = _plan_state(patched_api, fake_bucket, plan_id)
    weeks[0]["fri"] = "5 км"                                     # спортсмен поправил план сам
    assert api(FakeRequest("POST", f"/plans/{plan_id}/weeks", {"weeks": weeks}))[1] == 201
    before = dict(fake_bucket._store)

    body, code, _ = _apply(api, thread, answer["id"])
    assert code == 409 and json.loads(body) == {"error": "proposal_stale"}
    manifest, after = _plan_state(patched_api, fake_bucket, plan_id)
    assert manifest["current_version"] == 2 and after[1]["wed"] == "6×800 м"
    assert {k: v for k, v in fake_bucket._store.items() if "/ai_coach/" in k or "/plans/" in k} == \
           {k: v for k, v in before.items() if "/ai_coach/" in k or "/plans/" in k}

    view = _json(api(FakeRequest("GET", f"/ai-coach/threads/{thread['id']}")))
    assert view["messages"][1]["proposal_state"] == "stale"


def test_proposal_goes_stale_when_another_plan_becomes_active(api, llm, patched_api, fake_bucket):
    first = _seed_current_plan(patched_api, fake_bucket)
    thread, answer = _propose(api, llm)
    _seed_current_plan(patched_api, fake_bucket, name="Другой")   # новый план становится активным

    assert json.loads(_apply(api, thread, answer["id"])[0]) == {"error": "proposal_stale"}
    assert _plan_state(patched_api, fake_bucket, first)[0]["current_version"] == 1


def test_applying_one_proposal_makes_the_earlier_one_stale(api, llm, patched_api, fake_bucket):
    _seed_current_plan(patched_api, fake_bucket)
    thread, first = _propose(api, llm)
    _, second = _propose(api, llm, _change(week=3, day="mon", text="отдых"), thread=thread)
    assert _apply(api, thread, second["id"])[1] == 201

    states = [m.get("proposal_state") for m in
              _json(api(FakeRequest("GET", f"/ai-coach/threads/{thread['id']}")))["messages"]]
    assert states == [None, "stale", None, "applied"]
    assert _apply(api, thread, first["id"])[1] == 409


def test_proposal_goes_stale_once_its_day_has_passed(api, llm, patched_api, fake_bucket, storage_module):
    plan_id = _seed_current_plan(patched_api, fake_bucket)
    thread, answer = _propose(api, llm)
    later = dt.datetime.utcnow().date() + dt.timedelta(days=30)

    with pytest.raises(storage_module.AICoachError, match="proposal_stale"):
        storage_module.apply_ai_proposal(fake_bucket, SUB, thread["id"], answer["id"], today=later)
    messages = storage_module.mark_proposal_states(
        fake_bucket, SUB, thread["id"],
        storage_module.read_ai_messages(fake_bucket, SUB, thread["id"]), today=later)
    assert messages[1]["proposal_state"] == "stale"
    assert _plan_state(patched_api, fake_bucket, plan_id)[0]["current_version"] == 1


def test_apply_refuses_what_is_not_a_proposal(api, llm, patched_api, fake_bucket):
    plan_id = _seed_current_plan(patched_api, fake_bucket)
    thread = _new_thread(api)
    question, answer = _json(_say(api, thread["id"]))["messages"]      # ответ без предложения

    for message_id in (question["id"], answer["id"]):
        body, code, _ = _apply(api, thread, message_id)
        assert code == 400 and json.loads(body) == {"error": "no_proposal"}
    assert _apply(api, thread, "20260101T000000000000-ai-deadbeef")[1] == 404
    assert _apply(api, thread, "nope")[1] == 404
    assert _apply(api, {"id": "20260101T000000000000-deadbeef"}, answer["id"])[1] == 404
    assert _plan_state(patched_api, fake_bucket, plan_id)[0]["current_version"] == 1


def test_another_user_cannot_apply_my_proposal(api, llm, patched_api, fake_bucket):
    plan_id = _seed_current_plan(patched_api, fake_bucket)
    _seed_current_plan(patched_api, fake_bucket, sub=OTHER)
    thread, answer = _propose(api, llm)
    assert _apply(api, thread, answer["id"], **STRANGER)[1] == 404
    assert _plan_state(patched_api, fake_bucket, plan_id)[0]["current_version"] == 1


def test_next_turn_sees_the_applied_plan_and_the_earlier_proposal(api, llm, patched_api, fake_bucket):
    _seed_current_plan(patched_api, fake_bucket)
    thread, answer = _propose(api, llm)
    _apply(api, thread, answer["id"])
    _say(api, thread["id"], "Спасибо, что дальше?")

    call = llm["calls"][-1]
    assert "): 6 км легко" in call["system"]                     # окно плана уже с правкой
    remembered = json.loads(call["history"][1]["content"])
    assert remembered["proposal"]["changes"][0]["text"] == "6 км легко"
