"""Память ИИ-тренера (#80): хранилище, сверка изменений, ход разбора, маршруты.

Модель не вызывается: `call_llm` подменён и отвечает из очереди, как в
test_ai_coach.py, поэтому проверяется то, что уходит модели и что остаётся в
хранилище после хода.
"""
import json

import pytest

from conftest import FakeRequest, interleave

SUB, OTHER = "u1", "u2"
STRANGER = {"sub": OTHER, "email": "other@example.com"}
KNEE = "После длительных ноет левое колено"
TRIP = "С 10 по 20 ноября в отпуске, бегать сможет только на дорожке"


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


def _new_thread(api, title="Разбор", **who):
    body, code, _ = api(FakeRequest("POST", "/ai-coach/threads", {"title": title}), **who)
    assert code == 201
    return json.loads(body)["thread"]


def _say(api, thread_id, text="Как дела?", **who):
    body, code, _ = api(FakeRequest("POST", f"/ai-coach/threads/{thread_id}/messages",
                                    {"text": text}), **who)
    assert code == 201, body
    return json.loads(body)["messages"]


def _memory(api, **who):
    body, code, _ = api(FakeRequest("GET", "/ai-coach/memory"), **who)
    assert code == 200
    return json.loads(body)


def _edit(api, who=None, **body):
    response = api(FakeRequest("POST", "/ai-coach/memory", body), **(who or {}))
    return json.loads(response[0]), response[1]


def _objects(bucket, sub=SUB):
    prefix = f"users/{sub}/ai_coach/memory/"
    return {k: v for k, v in bucket._store.items() if k.startswith(prefix)}


def _add(text, kind="health"):
    return {"op": "add", "kind": kind, "text": text}


def _seed(storage_module, bucket, *texts, role="ai", sub=SUB):
    """Факты одной версией памяти → её payload."""
    return storage_module.write_ai_memory(bucket, sub, [_add(t) for t in texts], role,
                                          created_by="runner@example.com", thread_id="t1")


# ── хранилище: версии ─────────────────────────────────────────────────────────

def test_memory_that_was_never_written_is_empty_at_version_zero(storage_module, fake_bucket):
    assert storage_module.read_ai_memory(fake_bucket, SUB) == ({"next_id": 1, "facts": []}, 0)
    assert storage_module.read_ai_memory_history(fake_bucket, SUB) == []


def test_each_change_is_a_new_version_and_the_old_one_stays(storage_module, fake_bucket):
    first = _seed(storage_module, fake_bucket, KNEE)
    v1 = fake_bucket._store[f"users/{SUB}/ai_coach/memory/v1/memory.json"]
    second = storage_module.write_ai_memory(fake_bucket, SUB, [{"op": "hide", "id": "m1"}],
                                            "athlete", created_by="runner@example.com",
                                            base_version=1)

    assert (first["version"], second["version"]) == (1, 2)
    assert fake_bucket._store[f"users/{SUB}/ai_coach/memory/v1/memory.json"] == v1
    assert (second["author"], second["supersedes_version"]) == ("athlete", 1)
    assert second["change_reason"] == "Спортсмен: скрыто 1"
    assert second["changes"] == [{"op": "hide", "id": "m1", "kind": "health", "text": KNEE}]
    (fact,) = second["facts"]                     # скрытый факт остаётся в документе
    assert (fact["status"], fact["hidden_by"]) == ("hidden", "athlete")

    doc, version = storage_module.read_ai_memory(fake_bucket, SUB)
    assert version == 2 and storage_module.memory_facts(doc) == []
    manifest = json.loads(fake_bucket._store[f"users/{SUB}/ai_coach/memory/manifest.json"])
    assert manifest["gcs_object_path"].endswith("/v2/memory.json") and manifest["checksum"]


def test_fact_records_who_wrote_it_and_where(storage_module, fake_bucket):
    payload = _seed(storage_module, fake_bucket, KNEE)
    (fact,) = payload["facts"]
    assert fact["id"] == "m1" and payload["next_id"] == 2
    assert (fact["source"], fact["source_thread_id"], fact["status"]) == ("ai", "t1", "active")
    assert payload["change_reason"] == "ИИ-тренер: добавлено 1"
    assert payload["source_thread_id"] == "t1" and payload["created_by"] == "runner@example.com"


def test_ids_are_never_reused(storage_module, fake_bucket):
    _seed(storage_module, fake_bucket, KNEE)
    storage_module.write_ai_memory(fake_bucket, SUB, [{"op": "hide", "id": "m1"}], "ai")
    payload = _seed(storage_module, fake_bucket, TRIP)
    assert [f["id"] for f in payload["facts"]] == ["m1", "m2"]


def test_a_change_that_changes_nothing_writes_no_version(storage_module, fake_bucket):
    _seed(storage_module, fake_bucket, KNEE)
    before = dict(fake_bucket._store)
    same = {"op": "edit", "id": "m1", "kind": "health", "text": KNEE}
    assert storage_module.write_ai_memory(fake_bucket, SUB, [same], "athlete") is None
    assert storage_module.write_ai_memory(fake_bucket, SUB, [{"op": "hide", "id": "m9"}], "ai") is None
    assert fake_bucket._store == before


def test_athlete_edit_over_a_stale_version_is_refused(storage_module, fake_bucket):
    _seed(storage_module, fake_bucket, KNEE)
    with pytest.raises(storage_module.MemoryStale):
        storage_module.write_ai_memory(fake_bucket, SUB, [_add(TRIP)], "athlete", base_version=0)
    assert storage_module.read_ai_memory(fake_bucket, SUB)[1] == 1


@pytest.mark.parametrize("before_read", [False, True], ids=["после чтения", "до чтения"])
def test_model_write_lands_on_top_of_a_parallel_one(storage_module, fake_bucket, monkeypatch,
                                                    before_read):
    """Модель пишет без base_version: если между её чтением и записью память
    поправили, изменения ложатся поверх свежей версии и не теряют чужие."""
    _seed(storage_module, fake_bucket, KNEE)
    interleave(fake_bucket, monkeypatch,
               lambda: storage_module.write_ai_memory(fake_bucket, SUB, [_add(TRIP, "life")],
                                                      "athlete", base_version=1),
               before_read=before_read)

    mine = storage_module.write_ai_memory(fake_bucket, SUB, [_add("Не любит интервалы утром",
                                                                   "preference")], "ai")

    doc, version = storage_module.read_ai_memory(fake_bucket, SUB)
    assert version == mine["version"] > 2
    assert [f["text"] for f in storage_module.memory_facts(doc)] == \
        [KNEE, TRIP, "Не любит интервалы утром"]
    history = storage_module.read_ai_memory_history(fake_bucket, SUB)
    assert [h["version"] for h in history] == [mine["version"], 2, 1]


def test_old_hidden_facts_leave_the_current_version_but_not_history(storage_module, fake_bucket,
                                                                     monkeypatch):
    monkeypatch.setattr(storage_module, "AI_MEMORY_HIDDEN_KEEP", 2)
    _seed(storage_module, fake_bucket, "Факт 1", "Факт 2", "Факт 3")
    for fact_id in ("m1", "m2", "m3"):
        storage_module.write_ai_memory(fake_bucket, SUB, [{"op": "hide", "id": fact_id}], "athlete")

    doc, version = storage_module.read_ai_memory(fake_bucket, SUB)
    assert [f["id"] for f in storage_module.memory_facts(doc, "hidden")] == ["m2", "m3"]
    v2 = json.loads(fake_bucket._store[f"users/{SUB}/ai_coach/memory/v2/memory.json"])
    assert v2["facts"][0]["id"] == "m1"           # в прошлой версии он есть


def test_history_lists_versions_newest_first(storage_module, fake_bucket):
    _seed(storage_module, fake_bucket, KNEE)
    storage_module.write_ai_memory(fake_bucket, SUB, [{"op": "hide", "id": "m1"}], "athlete")
    newest, oldest = storage_module.read_ai_memory_history(fake_bucket, SUB)
    assert (newest["version"], newest["author"], newest["facts"]) == (2, "athlete", 0)
    assert (oldest["version"], oldest["author"], oldest["facts"]) == (1, "ai", 1)
    assert oldest["changes"][0]["text"] == KNEE and oldest["source_thread_id"] == "t1"


# ── сверка изменений от модели ────────────────────────────────────────────────

def _facts(storage_module, bucket, *texts):
    _seed(storage_module, bucket, *texts)
    return storage_module.memory_facts(storage_module.read_ai_memory(bucket, SUB)[0])


def test_model_changes_are_mapped_to_operations(storage_module, fake_bucket):
    facts = _facts(storage_module, fake_bucket, KNEE, TRIP)
    raw = {"add": [{"kind": "preference", "text": "  Любит  бегать\nутром "}],
           "edit": [{"id": "m1", "text": "Колено прошло, болей нет"}],
           "forget": [{"id": "[m2]"}]}
    assert storage_module.clean_memory_ops(raw, facts) == [
        {"op": "hide", "id": "m2"},
        {"op": "edit", "id": "m1", "kind": "health", "text": "Колено прошло, болей нет"},
        {"op": "add", "kind": "preference", "text": "Любит бегать утром"},
    ]


@pytest.mark.parametrize("raw", [
    None, "запомни", [], {}, {"add": "не список"}, {"add": ["не словарь"]},
    {"add": [{"kind": "mood", "text": "Хорошее настроение"}]},          # чужой вид
    {"add": [{"kind": ["health"], "text": "Вид списком"}]},
    {"add": [{"kind": "health", "text": "   "}]},
    {"add": [{"kind": "health", "text": 42}]},
    {"add": [{"kind": "health", "text": KNEE.upper()}]},                # уже записано
    {"edit": [{"id": "m9", "text": "Нет такого"}]},
    {"edit": [{"id": "m1", "text": KNEE}]},                             # ничего не меняет
    {"edit": [{"id": "m1", "text": ""}]},
    {"forget": [{"id": "m9"}]}, {"forget": [{"id": 1}]}, {"forget": [{}]},
])
def test_unusable_model_changes_are_dropped(storage_module, fake_bucket, raw):
    facts = _facts(storage_module, fake_bucket, KNEE)
    assert storage_module.clean_memory_ops(raw, facts) == []


def test_a_fact_is_changed_once_per_turn(storage_module, fake_bucket):
    facts = _facts(storage_module, fake_bucket, KNEE)
    raw = {"forget": [{"id": "m1"}], "edit": [{"id": "m1", "text": "Новый текст"}]}
    assert storage_module.clean_memory_ops(raw, facts) == [{"op": "hide", "id": "m1"}]
    twice = {"add": [{"kind": "life", "text": "Переехал"}, {"kind": "life", "text": "переехал"}]}
    assert len(storage_module.clean_memory_ops(twice, facts)) == 1


def test_model_changes_are_capped_and_trimmed(storage_module):
    from domain import MEMORY_OPS_MAX, MEMORY_TEXT_MAX
    raw = {"add": [{"kind": "life", "text": f"Факт {i} " + "очень " * 80} for i in range(9)]}
    ops = storage_module.clean_memory_ops(raw, [])
    assert len(ops) == MEMORY_OPS_MAX
    assert all(len(op["text"]) == MEMORY_TEXT_MAX for op in ops)


def test_full_memory_takes_no_new_fact_but_forgetting_makes_room(storage_module, fake_bucket):
    from domain import MEMORY_FACTS_MAX
    _seed(storage_module, fake_bucket, *[f"Факт {i}" for i in range(MEMORY_FACTS_MAX)])
    assert storage_module.write_ai_memory(fake_bucket, SUB, [_add(KNEE)], "ai") is None
    payload = storage_module.write_ai_memory(
        fake_bucket, SUB, [{"op": "hide", "id": "m1"}, _add(KNEE)], "ai")
    assert [c["op"] for c in payload["changes"]] == ["hide", "add"]
    assert len(storage_module.memory_facts(payload)) == MEMORY_FACTS_MAX


# ── правка спортсменом ────────────────────────────────────────────────────────

@pytest.mark.parametrize("body,code", [
    ({}, "invalid_fact"),
    ({"op": "drop", "id": "m1"}, "invalid_fact"),
    ({"op": "add", "text": "Без вида"}, "invalid_fact"),
    ({"op": "add", "kind": "mood", "text": "Чужой вид"}, "invalid_fact"),
    ({"op": "add", "kind": "health", "text": "  "}, "invalid_fact"),
    ({"op": "add", "kind": "health", "text": "x" * 301}, "invalid_fact"),
    ({"op": "edit", "id": "m1", "text": ["список"]}, "invalid_fact"),
    ({"op": "edit", "id": "m9", "text": "Нет такого"}, "fact_not_found"),
    ({"op": "hide", "id": "m9"}, "fact_not_found"),
    ({"op": "restore", "id": "m1"}, "fact_not_found"),            # он не скрыт
])
def test_bad_athlete_edit_is_named(storage_module, fake_bucket, body, code):
    _seed(storage_module, fake_bucket, KNEE)
    doc, _ = storage_module.read_ai_memory(fake_bucket, SUB)
    with pytest.raises(storage_module.AICoachError) as refused:
        storage_module.clean_memory_edit(body, doc)
    assert str(refused.value) == code


# ── ход разбора ───────────────────────────────────────────────────────────────

def test_model_memory_reaches_storage_and_the_answer(api, llm):
    thread = _new_thread(api)
    llm["replies"].append(_reply("Учту колено.", memory={"add": [{"kind": "health", "text": KNEE}]}))
    _, answer = _say(api, thread["id"], "У меня колено ноет после длительных")

    assert answer["based_on_memory_version"] == 0
    assert answer["memory_version"] == 1
    assert answer["memory_changes"] == [{"op": "add", "id": "m1", "kind": "health", "text": KNEE}]
    (fact,) = _memory(api)["facts"]
    assert (fact["text"], fact["source"], fact["source_thread_id"]) == (KNEE, "ai", thread["id"])


def test_memory_is_shared_by_every_thread(api, llm):
    first = _new_thread(api, "Колено")
    llm["replies"].append(_reply(memory={"add": [{"kind": "health", "text": KNEE}]}))
    _say(api, first["id"])
    _say(api, _new_thread(api, "Итоги недели")["id"])

    assert "Память пока пуста." in llm["calls"][0]["system"]
    assert "[m1]" not in llm["calls"][0]["system"]
    assert "=== Что ты помнишь о спортсмене" in llm["calls"][1]["system"]
    assert f"здоровье: {KNEE}" in llm["calls"][1]["system"]
    assert llm["calls"][1]["history"] == []           # другой разбор, своей истории нет


def test_model_is_reminded_what_it_wrote_to_memory(api, llm):
    thread = _new_thread(api)
    llm["replies"].append(_reply("Запомнил.", memory={"add": [{"kind": "life", "text": TRIP}]}))
    _say(api, thread["id"], "Я в отпуске")
    _say(api, thread["id"], "Что дальше?")

    envelope = json.loads(llm["calls"][1]["history"][1]["content"])
    assert envelope == {"reply": "Запомнил.", "memory": {"add": [{"kind": "life", "text": TRIP}]}}


def test_model_can_correct_and_forget(api, llm, storage_module, fake_bucket):
    _seed(storage_module, fake_bucket, KNEE, TRIP)
    thread = _new_thread(api)
    llm["replies"].append(_reply(memory={"edit": [{"id": "m1", "text": "Колено прошло"}],
                                         "forget": [{"id": "m2"}]}))
    _, answer = _say(api, thread["id"])
    assert [(c["op"], c["id"]) for c in answer["memory_changes"]] == [("hide", "m2"), ("edit", "m1")]
    assert answer["memory_changes"][1]["old"] == KNEE
    memory = _memory(api)
    assert [f["text"] for f in memory["facts"]] == ["Колено прошло"]
    assert [f["text"] for f in memory["hidden"]] == [TRIP]


def test_unusable_memory_from_the_model_costs_nothing(api, llm, fake_bucket):
    thread = _new_thread(api)
    llm["replies"].append(_reply(memory={"forget": [{"id": "m7"}], "add": [{"kind": "x", "text": "y"}]}))
    _, answer = _say(api, thread["id"])
    assert "memory_changes" not in answer and _objects(fake_bucket) == {}


def test_failed_memory_write_does_not_lose_the_answer(api, llm, patched_api, monkeypatch):
    def broken(*args, **kwargs):
        raise patched_api.StorageBusy()

    monkeypatch.setattr(patched_api, "write_ai_memory", broken)
    thread = _new_thread(api)
    llm["replies"].append(_reply("Ответ есть.", memory={"add": [{"kind": "health", "text": KNEE}]}))
    body, code, _ = api(FakeRequest("POST", f"/ai-coach/threads/{thread['id']}/messages",
                                    {"text": "Колено"}))
    assert code == 201
    data = json.loads(body)
    assert data["messages"][1]["text"] == "Ответ есть." and "memory_changes" not in data["messages"][1]
    assert data["usage"]["count"] == 1


def test_preview_shows_the_memory_block(api, storage_module, fake_bucket):
    _seed(storage_module, fake_bucket, KNEE)
    body, code, _ = api(FakeRequest("GET", "/advise/preview"))
    assert code == 200
    prompt = json.loads(body)["prompt"]
    assert f"здоровье: {KNEE}" in prompt and "[m1] " in prompt


# ── промпт ────────────────────────────────────────────────────────────────────

def test_memory_block_groups_facts_by_kind_and_counts_them():
    import llm_prompt
    facts = [{"id": "m2", "kind": "life", "text": TRIP, "created_at": "2026-11-01T08:00:00Z",
              "updated_at": "2026-11-03T08:00:00Z"},
             {"id": "m1", "kind": "health", "text": KNEE, "created_at": "2026-10-12T08:00:00Z"}]
    text = llm_prompt.format_memory_block(facts)
    assert text.splitlines()[1:] == [f"[m1] 2026-10-12, здоровье: {KNEE}",
                                     f"[m2] 2026-11-03, обстоятельства: {TRIP}",
                                     "(фактов 2 из 30)"]


def test_instructions_explain_memory_with_either_format():
    import llm_prompt
    from domain import MEMORY_FACTS_MAX, MEMORY_OPS_MAX
    for with_proposals in (True, False):
        text = llm_prompt.coach_chat_instructions(with_proposals)
        for rule in ('"memory": null', '"forget"', "health (здоровье)", "agreement (договорённости)",
                     f"Не больше {MEMORY_OPS_MAX} изменений", f"помещается {MEMORY_FACTS_MAX} фактов",
                     "Не записывай оценок личности", "Говори, что запомнил"):
            assert rule in text, rule
    assert "proposal" not in llm_prompt.coach_chat_instructions(False)


def test_memory_block_goes_right_after_the_data():
    import llm_prompt
    data = llm_prompt.coach_chat_data("КОНТЕКСТ", ["БЛОК"], "ОКНО ПЛАНА", "ПАМЯТЬ")
    assert data.index("КОНТЕКСТ") < data.index("ПАМЯТЬ") < data.index("ОКНО ПЛАНА") < data.index("БЛОК")


# ── маршруты ──────────────────────────────────────────────────────────────────

def test_empty_memory_view(api):
    memory = _memory(api)
    assert (memory["version"], memory["facts"], memory["hidden"]) == (0, [], [])
    assert [k["id"] for k in memory["kinds"]] == ["health", "life", "preference", "agreement"]
    assert memory["limits"] == {"facts": 30, "text": 300}


def test_athlete_adds_edits_hides_and_restores(api, fake_bucket):
    added, code = _edit(api, base_version=0, op="add", kind="agreement", text="  Контрольная  5 км 1 декабря ")
    assert code == 201 and added["version"] == 1
    (fact,) = added["facts"]
    assert (fact["text"], fact["source"]) == ("Контрольная 5 км 1 декабря", "athlete")

    edited, code = _edit(api, base_version=1, op="edit", id="m1", text="Контрольная 5 км 8 декабря")
    assert code == 201 and edited["facts"][0]["text"] == "Контрольная 5 км 8 декабря"
    hidden, code = _edit(api, base_version=2, op="hide", id="m1")
    assert code == 201 and hidden["facts"] == [] and hidden["hidden"][0]["id"] == "m1"
    restored, code = _edit(api, base_version=3, op="restore", id="m1")
    assert code == 201 and restored["facts"][0]["id"] == "m1" and restored["hidden"] == []

    assert len([k for k in _objects(fake_bucket) if k.endswith("memory.json")]) == 4
    body, code, _ = api(FakeRequest("GET", "/ai-coach/memory/history"))
    versions = json.loads(body)["versions"]
    assert [v["version"] for v in versions] == [4, 3, 2, 1]
    assert {v["author"] for v in versions} == {"athlete"}


def test_edit_that_changes_nothing_keeps_the_version(api):
    _edit(api, base_version=0, op="add", kind="health", text=KNEE)
    same, code = _edit(api, base_version=1, op="edit", id="m1", text=KNEE)
    assert code == 200 and same["version"] == 1


@pytest.mark.parametrize("body,status,error", [
    ({"op": "add", "kind": "health", "text": "Без версии"}, 400, "Missing base_version"),
    ({"base_version": "1", "op": "hide", "id": "m1"}, 400, "invalid base_version"),
    ({"base_version": 0, "op": "add", "kind": "health", "text": "Поверх старой"}, 409, "memory_stale"),
    ({"base_version": 1, "op": "add", "kind": "mood", "text": "Чужой вид"}, 400, "invalid_fact"),
    ({"base_version": 1, "op": "hide", "id": "m5"}, 404, "fact_not_found"),
])
def test_refused_edits_say_why_and_write_nothing(api, fake_bucket, body, status, error):
    _edit(api, base_version=0, op="add", kind="health", text=KNEE)
    before = _objects(fake_bucket)
    data, code = _edit(api, **body)
    assert (code, data["error"]) == (status, error)
    assert _objects(fake_bucket) == before


def test_full_memory_takes_no_more_from_the_athlete(api, storage_module, fake_bucket):
    from domain import MEMORY_FACTS_MAX
    _seed(storage_module, fake_bucket, *[f"Факт {i}" for i in range(MEMORY_FACTS_MAX)], role="athlete")
    data, code = _edit(api, base_version=1, op="add", kind="life", text="Ещё один")
    assert (code, data["error"]) == (400, "memory_full")
    _edit(api, base_version=1, op="hide", id="m1")
    _edit(api, base_version=2, op="add", kind="life", text="Ещё один")
    data, code = _edit(api, base_version=3, op="restore", id="m1")
    assert (code, data["error"]) == (400, "memory_full")


def test_memory_is_private(api, storage_module, fake_bucket, api_module):
    _seed(storage_module, fake_bucket, KNEE)
    assert _memory(api, **STRANGER)["facts"] == []
    data, code = _edit(api, who=STRANGER, base_version=1, op="hide", id="m1")
    assert (code, data["error"]) == (409, "memory_stale")      # у него своя память, версии 0
    assert _memory(api)["facts"][0]["text"] == KNEE
    # тренеру-человеку маршрутов к памяти спортсмена нет
    assert all(path.startswith(r"^/ai-coach/memory")
               for _, path, _, _ in api_module.ROUTES if "memory" in path)
