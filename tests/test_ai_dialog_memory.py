"""Память диалогов ИИ-тренера (#82): весь разбор, краткое содержание, прошлые разборы.

Модель не вызывается: `call_llm` подменён и отвечает из очереди, как в
test_ai_coach.py, поэтому проверяется то, что уходит модели и что остаётся в
репликах после хода.
"""
import json

import pytest

import llm_prompt
from conftest import FakeRequest

SUB = "u1"
KNEE = "Спортсмен жаловался на колено после длительной; договорились неделю без интервалов."
TRIP = "Обсуждали отпуск 20–30 ноября: бег только на дорожке, объём 40 км."


def _reply(text="Принято.", **extra):
    return {"text": json.dumps({"reply": text, **extra}, ensure_ascii=False),
            "input_tokens": 900, "output_tokens": 60}


@pytest.fixture
def llm(patched_api, fake_bucket, monkeypatch):
    """Подменяет модель: записывает вызовы и отвечает из очереди `replies`."""
    patched_api.write_llm_config_version(fake_bucket, "openai", "gpt-test", "sk-test")
    calls, replies = [], []

    def fake(provider, model, api_key, system_prompt, user_prompt, effort=None, history=None):
        calls.append({"system": system_prompt, "user": user_prompt, "history": history})
        return replies.pop(0) if replies else _reply()

    monkeypatch.setattr(patched_api, "call_llm", fake)
    return {"calls": calls, "replies": replies}


def _new_thread(api, title="Разбор"):
    body, code, _ = api(FakeRequest("POST", "/ai-coach/threads", {"title": title}))
    assert code == 201
    return json.loads(body)["thread"]


def _say(api, thread_id, text="Как дела?"):
    body, code, _ = api(FakeRequest("POST", f"/ai-coach/threads/{thread_id}/messages",
                                    {"text": text}))
    assert code == 201, body
    return json.loads(body)["messages"]


def _talk(api, llm, title, summary, text="Вопрос"):
    """Разбор с одним ходом, после которого у него есть краткое содержание."""
    thread = _new_thread(api, title)
    llm["replies"].append(_reply(summary=summary))
    _say(api, thread["id"], text)
    return thread


# ── краткое содержание ────────────────────────────────────────────────────────

def test_summary_is_cleaned_and_capped(storage_module):
    clean = storage_module.clean_thread_summary
    assert clean("  Колено.\n\nДоговорились   о разгрузке. ") == "Колено. Договорились о разгрузке."
    assert len(clean("очень " * 400)) == storage_module.AI_SUMMARY_MAX
    for bad in (None, 42, ["список"], "   "):
        assert clean(bad) == ""


def test_latest_summary_is_taken_from_the_freshest_answer(storage_module):
    messages = [{"role": "ai", "text": "1", "thread_summary": "старое"},
                {"role": "athlete", "text": "2", "thread_summary": "не моё"},
                {"role": "ai", "text": "3", "thread_summary": "свежее"},
                {"role": "ai", "text": "4"}]
    assert storage_module.latest_thread_summary(messages) == "свежее"
    assert storage_module.latest_thread_summary([]) == ""


def test_only_the_last_answer_carries_the_summary_in_the_history(storage_module):
    messages = [{"role": "athlete", "text": "в1"}, {"role": "ai", "text": "о1", "thread_summary": "с1"},
                {"role": "athlete", "text": "в2"}, {"role": "ai", "text": "о2", "thread_summary": "с2"}]
    history = storage_module.ai_history(messages)
    assert json.loads(history[1]["content"]) == {"reply": "о1"}
    assert json.loads(history[3]["content"]) == {"reply": "о2", "summary": "с2"}


def test_answer_stores_the_summary_from_the_model(api, llm, storage_module, fake_bucket):
    thread = _new_thread(api)
    llm["replies"].append(_reply("Ответ.", summary=f"  {KNEE}\n"))
    _, answer = _say(api, thread["id"])
    assert answer["thread_summary"] == KNEE
    body, _, _ = api(FakeRequest("GET", f"/ai-coach/threads/{thread['id']}"))
    assert json.loads(body)["messages"][1]["thread_summary"] == KNEE


def test_missing_summary_carries_the_previous_one_over(api, llm):
    thread = _talk(api, llm, "Колено", KNEE)
    llm["replies"].append(_reply("Без содержания."))
    _, answer = _say(api, thread["id"], "А дальше?")
    assert answer["thread_summary"] == KNEE
    llm["replies"].append(_reply(summary=42))
    _, answer = _say(api, thread["id"], "И ещё?")
    assert answer["thread_summary"] == KNEE


def test_thread_without_any_summary_stores_none(api, llm):
    _, answer = _say(api, _new_thread(api)["id"])
    assert "thread_summary" not in answer


# ── весь разбор и его начало ──────────────────────────────────────────────────

def test_a_usual_thread_is_sent_whole(api, llm):
    thread = _new_thread(api)
    for i in range(10):
        _say(api, thread["id"], f"Вопрос {i}")
    history = llm["calls"][-1]["history"]
    assert len(history) == 18 and history[0]["content"] == "Вопрос 0"
    assert "=== Начало этого разбора" not in llm["calls"][-1]["system"]


def test_a_long_thread_gets_its_start_as_a_summary(api, llm, storage_module, monkeypatch):
    monkeypatch.setattr(storage_module, "AI_HISTORY_CHARS", 120)
    thread = _new_thread(api)
    llm["replies"].append(_reply("Первый подробный ответ " * 3, summary=KNEE))
    _say(api, thread["id"], "Первый вопрос про колено")
    llm["replies"].append(_reply("Второй подробный ответ " * 3))
    _say(api, thread["id"], "Второй вопрос")
    _say(api, thread["id"], "Третий вопрос")

    last = llm["calls"][-1]
    assert [h["content"] for h in last["history"] if h["role"] == "user"] == ["Второй вопрос"]
    assert "=== Начало этого разбора ===" in last["system"]
    assert f"Кратко о разборе: {KNEE}" in last["system"]


def test_thread_start_without_a_summary_says_so():
    assert llm_prompt.format_thread_start("") == (
        "=== Начало этого разбора ===\nРанние реплики этого разбора не показаны. "
        "Краткого содержания нет.")


# ── прошлые разборы ───────────────────────────────────────────────────────────

def test_a_new_thread_sees_the_past_ones(api, llm):
    _talk(api, llm, "Колено", KNEE)
    _talk(api, llm, "Отпуск", TRIP)
    _say(api, _new_thread(api, "План на неделю")["id"])

    system = llm["calls"][-1]["system"]
    block = system[system.index("=== Прошлые разборы"):]
    assert block.index(f"«Отпуск»") < block.index(f"«Колено»")       # сначала свежие
    assert TRIP in block and KNEE in block
    assert "«План на неделю»" not in block                             # текущий не в прошлых


def test_the_first_thread_has_no_past_block(api, llm):
    _say(api, _new_thread(api)["id"])
    assert "=== Прошлые разборы" not in llm["calls"][0]["system"]


def test_hidden_threads_are_not_remembered(api, llm):
    hidden = _talk(api, llm, "Скрытый", KNEE)
    api(FakeRequest("POST", f"/ai-coach/threads/{hidden['id']}/archive"))
    _talk(api, llm, "Видимый", TRIP)
    system = llm["calls"][-1]["system"]
    assert "«Скрытый»" not in system and KNEE not in system


def test_past_threads_are_capped(api, llm, storage_module):
    for i in range(storage_module.AI_PAST_THREADS + 2):
        _talk(api, llm, f"Разбор {i}", f"Содержание {i}.")
    _say(api, _new_thread(api, "Текущий")["id"])
    block = llm["calls"][-1]["system"].split("=== Прошлые разборы")[1].split("===")[1]
    titles = [line for line in block.splitlines() if line.startswith("- «")]
    assert len(titles) == storage_module.AI_PAST_THREADS
    assert "Содержание 0." not in block and "Содержание 6." in block


def test_a_past_thread_from_before_summaries_shows_its_title(api, llm):
    _say(api, _new_thread(api, "Старый разбор")["id"])               # модель содержания не дала
    _say(api, _new_thread(api, "Новый")["id"])
    assert "- «Старый разбор», " in llm["calls"][-1]["system"]
    assert ": краткого содержания нет" in llm["calls"][-1]["system"]


def test_preview_shows_past_threads(api, llm):
    _talk(api, llm, "Колено", KNEE)
    body, code, _ = api(FakeRequest("GET", "/advise/preview"))
    assert code == 200 and KNEE in json.loads(body)["prompt"]


# ── промпт ────────────────────────────────────────────────────────────────────

def test_past_threads_block_lines():
    text = llm_prompt.format_past_threads([
        {"title": "Колено", "last_ts": "2026-11-07T10:00:00Z", "summary": KNEE},
        {"title": "Старый", "last_ts": "2026-10-01T10:00:00Z", "summary": ""}])
    assert text.splitlines() == ["=== Прошлые разборы (кратко, сначала свежие) ===",
                                 f"- «Колено», 2026-11-07: {KNEE}",
                                 "- «Старый», 2026-10-01: краткого содержания нет"]
    assert llm_prompt.format_past_threads([]) == ""


def test_data_blocks_order():
    data = llm_prompt.coach_chat_data("КОНТЕКСТ", ["БЛОК"], "ОКНО ПЛАНА", "ПАМЯТЬ",
                                      "ПРОШЛЫЕ", "НАЧАЛО")
    order = [data.index(x) for x in ("КОНТЕКСТ", "ПАМЯТЬ", "ПРОШЛЫЕ", "НАЧАЛО", "ОКНО ПЛАНА", "БЛОК")]
    assert order == sorted(order)


def test_instructions_ask_for_a_summary_with_either_format():
    for with_proposals in (True, False):
        text = llm_prompt.coach_chat_instructions(with_proposals)
        for rule in ('"summary": "краткое содержание всего разбора"', "Память о разговорах",
                     "В summary каждого ответа", "не додумывай", "«Начало этого разбора»"):
            assert rule in text, rule
    assert "proposal" not in llm_prompt.coach_chat_instructions(False)
