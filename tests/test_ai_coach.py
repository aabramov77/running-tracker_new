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
