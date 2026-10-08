"""Усиление безопасности перед расширением круга пользователей (#53).

Каждый раздел — своя находка аудита; тесты раздела до исправления падали.
Экранирование на фронтенде проверяется в test_frontend_sanity.py.
"""
import json
import logging

import pytest

from conftest import FakeRequest, NotFound, PreconditionFailed

ADMIN = {"sub": "admin-sub", "email": "aabramov77@gmail.com"}
COACH = {"sub": "c1", "email": "c1@example.com"}
XSS = "<img src=x onerror=alert(1)>"
MB = 1024 * 1024


def _json(response):
    return json.loads(response[0])


def _status(response):
    return response[1]


def _post(api, path, body, **who):
    return api(FakeRequest("POST", path, body), **who)


def _rejected(response, field):
    """Ответ — отказ в проверке, и виновато ровно это поле."""
    body, code, _ = response
    assert code == 400, body
    answer = json.loads(body)
    assert answer["error"] == "validation_failed", body
    assert list(answer["fields"]) == [field], body


# ── 2. Проверка входных данных: пробежка ──────────────────────────────────────

RUN = {"id": 1700000000000, "date": "2026-08-16", "dist": 10.5, "type": "long",
       "time": "55:30", "pace": "5:17", "hr": 148, "feel": "good", "notes": "ровно"}


@pytest.mark.parametrize("field,value", [
    ("id", XSS), ("id", "17"), ("id", 1.5), ("id", True), ("id", 0), ("id", -3),
    ("id", 2 ** 60), ("id", [1]),
    ("date", "16.08.2026"), ("date", "2026-02-30"), ("date", 20260816), ("date", XSS),
    ("date", None),
    ("dist", 0), ("dist", -5), ("dist", "много"), ("dist", 1000.5), ("dist", float("nan")),
    ("dist", float("inf")), ("dist", True), ("dist", [10]), ("dist", None),
    ("type", "sprint"), ("type", XSS), ("type", ["easy"]),
    ("feel", XSS), ("feel", {"a": 1}),
    ("hr", XSS), ("hr", 29), ("hr", 251), ("hr", [150]), ("hr", True),
    ("time", "1" * 33), ("time", {"h": 1}), ("pace", ["5:00"]),
    ("notes", "я" * 2001), ("notes", {"a": 1}),
    ("plan_id", "чужой-план"), ("plan_id", 5),
    ("fit_token", "../../users/u2/runs/1/v1"), ("fit_token", "1700000000-zzzzzzzz"),
    ("fit_token", 5),
])
def test_run_with_a_bad_field_is_rejected_and_nothing_is_stored(api, patched_api, fake_bucket,
                                                               field, value):
    _rejected(_post(api, "/", {**RUN, field: value}), field)
    assert patched_api.read_runs(fake_bucket, "u1") == []


def test_run_from_the_form_is_stored_as_sent(api):
    """Ровно то, что шлёт saveRun, проверку проходит без изменений."""
    body, code, _ = _post(api, "/", RUN)
    assert code == 201 and json.loads(body) == {**RUN, "plan_id": None, "deleted": False}


def test_run_fields_are_normalised_and_defaults_survive(api):
    body, code, _ = _post(api, "/", {"date": "2026-08-16", "dist": "10.5", "hr": "148",
                                     "type": None, "time": None, "plan_id": ""})
    run = json.loads(body)
    assert code == 201 and isinstance(run["id"], int)
    assert {k: run[k] for k in ("dist", "hr", "type", "feel", "time", "pace", "notes")} == {
        "dist": 10.5, "hr": 148, "type": "easy", "feel": "good", "time": "", "pace": "",
        "notes": ""}


@pytest.mark.parametrize("empty", [None, ""])
def test_run_without_pulse_keeps_it_empty(api, empty):
    assert _json(_post(api, "/", {**RUN, "hr": empty}))["hr"] is None


def test_run_fields_the_server_owns_cannot_be_set_by_the_client(api):
    run = _json(_post(api, "/", {**RUN, "deleted": True, "details_available": True,
                                 "max_hr": XSS, "html": XSS}))
    assert run["deleted"] is False
    assert not {"details_available", "max_hr", "html"} & set(run)


def test_run_goes_only_into_a_plan_of_its_owner(api, patched_api, fake_bucket):
    mine = patched_api.create_plan(fake_bucket, "u1", {"race_name": "Мой"})
    theirs = patched_api.create_plan(fake_bucket, "u2", {"race_name": "Чужой"})
    _rejected(_post(api, "/", {**RUN, "plan_id": theirs["id"]}), "plan_id")
    assert _json(_post(api, "/", {**RUN, "plan_id": mine["id"]}))["plan_id"] == mine["id"]


def test_fit_token_is_attached_and_a_dead_one_says_nothing_about_storage(api, patched_api,
                                                                        fake_bucket):
    api(FakeRequest("GET", "/"))                                  # регистрирует u1
    parsed = {"date": "2026-08-16", "summary": {"dist_km": 10.5, "max_hr": 171}, "laps": []}
    token = patched_api.write_parsed_fit_to_tmp(fake_bucket, "u1", b"FIT", parsed)
    run = _json(_post(api, "/", {**RUN, "fit_token": token}))
    assert run["details_available"] is True and run["max_hr"] == 171

    body, code, _ = _post(api, "/", {**RUN, "id": 2, "fit_token": token})   # токен уже израсходован
    assert code == 400
    assert json.loads(body) == {"error": "Failed to attach FIT details: token expired or invalid"}


@pytest.mark.parametrize("path", ["/", "/races", "/plan"])
@pytest.mark.parametrize("body", [["date", "dist", "name", "dist_label", "time", "weeks"], 42])
def test_json_that_is_not_an_object_is_a_400(api, patched_api, fake_bucket, path, body):
    patched_api.create_plan(fake_bucket, "u1", {"race_name": "HM"})
    assert _status(_post(api, path, body)) == 400


# ── 2. Проверка входных данных: старт ─────────────────────────────────────────

RACE = {"id": 1700000000001, "name": "Московский полумарафон", "date": "2026-08-09",
        "dist_label": "HM", "time": "1:45:30"}


@pytest.mark.parametrize("field,value", [
    ("id", XSS), ("id", 1.5), ("id", True),
    ("date", "09.08.2026"), ("date", 20260809), ("date", XSS),
    ("name", ""), ("name", "   "), ("name", "я" * 201), ("name", {"a": 1}),
    ("dist_label", "100km"), ("dist_label", XSS), ("dist_label", ["HM"]),
    ("time", ""), ("time", "1" * 33), ("time", ["1:45"]),
])
def test_race_with_a_bad_field_is_rejected_and_nothing_is_stored(api, patched_api, fake_bucket,
                                                                field, value):
    _rejected(_post(api, "/races", {**RACE, field: value}), field)
    assert patched_api.read_races(fake_bucket, "u1") == []


def test_race_from_the_form_is_stored_as_sent(api):
    body, code, _ = _post(api, "/races", RACE)
    assert code == 201 and json.loads(body) == {**RACE, "deleted": False}


# ── 2. Нечисловой ?id= ────────────────────────────────────────────────────────

@pytest.mark.parametrize("path", ["/", "/races"])
@pytest.mark.parametrize("bad", ["abc", "1.5", "1;2", XSS])
def test_delete_with_a_non_numeric_id_is_a_400(api, path, bad):
    body, code, _ = api(FakeRequest("DELETE", path, args={"id": bad}))
    assert (code, json.loads(body)) == (400, {"error": "Invalid id parameter"})


@pytest.mark.parametrize("path", ["/", "/races"])
def test_delete_still_tells_a_missing_id_from_an_unknown_one(api, path):
    assert _json(api(FakeRequest("DELETE", path))) == {"error": "Missing id parameter"}
    assert _status(api(FakeRequest("DELETE", path, args={"id": "999"}))) == 404


# ── 2. Проверка входных данных: недели плана ──────────────────────────────────

WEEK = {"w": 1, "start": "17.08", "end": "23.08", "accent": "Развитие", "type": "dev",
        "mon": "10 км", "tue": "", "wed": "6×800 м", "thu": "", "fri": "8 км",
        "sat": "", "sun": "16 км легко"}


def _plan(patched_api, fake_bucket, sub="u1"):
    return patched_api.create_plan(fake_bucket, sub, {"race_name": "HM"})["id"]


def _plan_objects(fake_bucket):
    return {name: data for name, data in fake_bucket._store.items() if "/plans/" in name}


@pytest.mark.parametrize("shortcut", [True, False], ids=["/plan", "/plans/id/weeks"])
@pytest.mark.parametrize("weeks,field", [
    ("строка", "weeks"), ({"w": 1}, "weeks"), (None, "weeks"), ([{"w": 1}] * 105, "weeks"),
    ([1], "weeks[0]"), ([None], "weeks[0]"), ([WEEK, ["mon"]], "weeks[1]"),
    ([{"w": XSS}], "weeks[0].w"), ([{"w": 1.5}], "weeks[0].w"), ([{"w": True}], "weeks[0].w"),
    ([{"w": 1000}], "weeks[0].w"),
    ([{"w": 1, "mon": {"html": XSS}}], "weeks[0].mon"), ([{"sun": ["16 км"]}], "weeks[0].sun"),
    ([{"w": 1, "mon": "я" * 501}], "weeks[0].mon"), ([{"accent": "я" * 201}], "weeks[0].accent"),
    ([WEEK, {"start": "1" * 33}], "weeks[1].start"), ([{"type": True}], "weeks[0].type"),
])
def test_plan_weeks_of_a_wrong_shape_are_rejected_and_nothing_is_written(
        api, patched_api, fake_bucket, shortcut, weeks, field):
    plan_id = _plan(patched_api, fake_bucket)
    before = _plan_objects(fake_bucket)
    path = "/plan" if shortcut else f"/plans/{plan_id}/weeks"
    _rejected(_post(api, path, {"weeks": weeks}), field)
    assert _plan_objects(fake_bucket) == before


def test_plan_week_keeps_only_the_known_fields(api, patched_api, fake_bucket):
    plan_id = _plan(patched_api, fake_bucket)
    week = {**WEEK, "html": XSS, "days": [1, 2], "constructor": {"x": 1}}
    assert _status(_post(api, "/plan", {"weeks": [week]})) == 201
    assert patched_api.read_plan_weeks(fake_bucket, "u1", plan_id) == [WEEK]


def test_sparse_and_old_weeks_pass_without_gaining_fields(api, patched_api, fake_bucket):
    """План до #23 хранится без вт и чт, а фронтенд шлёт его назад как есть:
    проверка не должна ни отвергать такую неделю, ни дописывать ей поля."""
    plan_id = _plan(patched_api, fake_bucket)
    weeks = [{"w": 1, "sun": "10 км"}, {"w": "2", "mon": None, "wed": 8}, {}]
    assert _status(_post(api, f"/plans/{plan_id}/weeks", {"weeks": weeks})) == 201
    assert patched_api.read_plan_weeks(fake_bucket, "u1", plan_id) == [
        {"w": 1, "sun": "10 км"}, {"w": 2, "mon": "", "wed": "8"}, {}]


def test_a_plan_can_still_be_emptied(api, patched_api, fake_bucket):
    plan_id = _plan(patched_api, fake_bucket)
    assert _status(_post(api, "/plan", {"weeks": [WEEK]})) == 201
    assert _status(_post(api, "/plan", {"weeks": []})) == 201
    assert patched_api.read_plan_weeks(fake_bucket, "u1", plan_id) == []


@pytest.mark.parametrize("sent,stored", [
    ("manual edit", "manual edit"), ({"html": XSS}, ""), (None, ""),
    ("строка\nза строкой  ", "строка за строкой"), ("я" * 500, "я" * 200),
])
def test_change_reason_is_stored_as_a_short_single_line(api, patched_api, storage_module,
                                                        fake_bucket, sent, stored):
    plan_id = _plan(patched_api, fake_bucket)
    assert _status(_post(api, "/plan", {"weeks": [WEEK], "change_reason": sent})) == 201
    assert storage_module.read_plan_manifest(fake_bucket, "u1", plan_id)["change_reason"] == stored


# ── 2. Проверка входных данных: карточка плана ────────────────────────────────

@pytest.mark.parametrize("field,value", [
    ("race_name", "я" * 201), ("race_name", {"html": XSS}), ("race_date", ["2026-08-09"]),
    ("target_time", "1" * 33), ("plan_start", True),
])
def test_plan_card_is_validated_on_create_and_on_edit(api, patched_api, fake_bucket,
                                                      field, value):
    plan_id = _plan(patched_api, fake_bucket)
    before = patched_api.read_plans_index(fake_bucket, "u1")
    for path in ("/plans", f"/plans/{plan_id}/meta"):
        _rejected(_post(api, path, {field: value}), field)
    assert patched_api.read_plans_index(fake_bucket, "u1") == before


def test_plan_card_takes_what_the_form_and_the_import_send(api):
    card = {"race_name": "Московский полумарафон", "race_date": "2026-08-09",
            "target_time": "1:40", "plan_start": "2026-05-10"}
    body, code, _ = _post(api, "/plans", {**card, "id": "чужой", "archived": True, "html": XSS})
    plan = json.loads(body)
    assert code == 201 and {k: plan[k] for k in card} == card
    assert plan["id"] != "чужой" and plan["archived"] is False and "html" not in plan

    # правка меняет только пришедшие поля; число из импортированного файла — тоже текст
    edited = _json(_post(api, f"/plans/{plan['id']}/meta", {"target_time": 1.4,
                                                           "race_date": None}))
    assert {k: edited[k] for k in card} == {**card, "target_time": "1.4", "race_date": ""}


# ── 2. Предел размера тела ────────────────────────────────────────────────────

def test_oversized_json_is_a_413_and_nothing_is_stored(api, patched_api, fake_bucket):
    body, code, _ = api(FakeRequest("POST", "/", RUN, content_length=MB + 1))
    assert (code, json.loads(body)) == (413, {"error": "payload_too_large", "limit_bytes": MB})
    assert patched_api.read_runs(fake_bucket, "u1") == []
    assert _status(api(FakeRequest("POST", "/", RUN, content_length=MB))) == 201


def test_fit_upload_has_its_own_larger_limit(api):
    # файла в запросе нет — отказ уже по существу, а не по размеру
    assert _status(api(FakeRequest("POST", "/runs/parse-fit", content_length=5 * MB))) == 400
    body, code, _ = api(FakeRequest("POST", "/runs/parse-fit", content_length=10 * MB + 1))
    assert (code, json.loads(body)["limit_bytes"]) == (413, 10 * MB)


def test_size_limit_does_not_wait_for_a_valid_token(patched_api, monkeypatch):
    monkeypatch.setattr(patched_api, "verify_token", lambda request: None)
    big = FakeRequest("POST", "/", RUN, content_length=MB + 1)
    assert _status(patched_api.handle_request(big)) == 413
    assert _status(patched_api.handle_request(FakeRequest("POST", "/", RUN))) == 401


def test_body_without_a_declared_length_is_capped_by_the_framework(api):
    """У chunked-тела длины в заголовке нет. Предел передаётся фреймворку, а
    его отказ при чтении — ошибка с кодом 413 — становится тем же ответом."""
    class TooLarge(Exception):
        code = 413

    def get_json(silent=False):
        raise TooLarge()

    request = FakeRequest("POST", "/", RUN)
    request.get_json = get_json
    body, code, _ = api(request)
    assert request.max_content_length == MB
    assert (code, json.loads(body)) == (413, {"error": "payload_too_large", "limit_bytes": MB})

    fit = FakeRequest("POST", "/runs/parse-fit")
    api(fit)
    assert fit.max_content_length == 10 * MB


# ── 3. Дневной лимит ИИ и одновременные запросы ───────────────────────────────

@pytest.fixture
def model(patched_api, fake_bucket, monkeypatch):
    """Подменённая модель. calls — сколько раз её позвали; during — что
    происходит, пока она «думает»; fail — исключение вместо ответа."""
    patched_api.write_llm_config_version(fake_bucket, "openai", "gpt-test", "sk-test")
    state = {"calls": 0, "during": None, "fail": None}

    def fake(provider, model, api_key, system_prompt, user_prompt, effort=None, history=None):
        state["calls"] += 1
        if state["during"]:
            state["during"]()
        if state["fail"]:
            raise state["fail"]
        return {"text": json.dumps({"reply": "Неделя прошла ровно."}, ensure_ascii=False),
                "input_tokens": 1, "output_tokens": 1}

    monkeypatch.setattr(patched_api, "call_llm", fake)
    return state


def _thread(api):
    return _json(_post(api, "/ai-coach/threads", {"title": "Итоги недели"}))["thread"]["id"]


def _say(api, thread_id):
    return _post(api, f"/ai-coach/threads/{thread_id}/messages", {"text": "Как неделя?"})


def _used(patched_api, fake_bucket, sub="u1"):
    return patched_api.read_advice_usage(fake_bucket, sub)["count"]


def test_parallel_messages_do_not_all_slip_under_the_daily_limit(api, model, patched_api,
                                                                fake_bucket, monkeypatch):
    """Пять запросов разом при лимите 1: пока модель отвечает первому, приходят
    остальные четыре. До #53 счётчик рос только после ответа — проходили все пять."""
    monkeypatch.setattr(patched_api, "DAILY_AI_COACH_LIMIT", 1)
    thread = _thread(api)
    late = []

    def the_rest_arrive():
        model["during"] = None                       # опоздавшие новых не порождают
        late.extend(_say(api, thread) for _ in range(4))

    model["during"] = the_rest_arrive
    first = _say(api, thread)

    assert _status(first) == 201
    assert [_status(r) for r in late] == [429] * 4
    assert all(_json(r) == {"error": "daily_limit_reached", "limit": 1} for r in late)
    assert model["calls"] == 1
    assert _used(patched_api, fake_bucket) == 1


def _interleave(fake_bucket, monkeypatch, competitor, before_read=False):
    """Один раз вклинивает competitor() в чужое «прочитал счётчик — записал».

    before_read — ещё раньше: между чтением номера поколения и содержимого.
    """
    real_get, pending = fake_bucket.get_blob, [competitor]

    def get_blob(name):
        blob = real_get(name)
        if not pending:
            return blob
        run = pending.pop()
        if blob is None or before_read:
            run()
            return blob
        read = blob.download_as_text

        def download_as_text():
            text = read()
            run()
            return text

        blob.download_as_text = download_as_text
        return blob

    monkeypatch.setattr(fake_bucket, "get_blob", get_blob)


@pytest.mark.parametrize("already_used", [0, 1], ids=["счётчика ещё нет", "счётчик есть"])
@pytest.mark.parametrize("before_read", [False, True], ids=["после чтения", "до чтения"])
def test_two_writers_never_lose_an_attempt_or_share_the_last_one(storage_module, fake_bucket,
                                                                monkeypatch, already_used,
                                                                before_read):
    """Оба прочитали одно и то же число. Запись с предусловием пропускает
    одного, второй перечитывает счётчик — и видит уже занятую попытку."""
    reserve = storage_module.reserve_advice_usage
    for _ in range(already_used):
        reserve(fake_bucket, "u1", limit=10)
    rival = []
    _interleave(fake_bucket, monkeypatch,
                lambda: rival.append(reserve(fake_bucket, "u1", limit=already_used + 1)),
                before_read=before_read)

    mine = reserve(fake_bucket, "u1", limit=already_used + 1)

    assert rival[0]["count"] == already_used + 1     # последняя попытка досталась сопернику
    assert mine is None                              # а не обоим
    assert storage_module.read_advice_usage(fake_bucket, "u1")["count"] == already_used + 1
    # при запасе по лимиту второе прибавление не теряется
    _interleave(fake_bucket, monkeypatch, lambda: reserve(fake_bucket, "u1", limit=10))
    assert reserve(fake_bucket, "u1", limit=10)["count"] == already_used + 3


def test_attempt_is_returned_when_the_model_fails(api, model, patched_api, fake_bucket,
                                                  monkeypatch):
    monkeypatch.setattr(patched_api, "DAILY_AI_COACH_LIMIT", 1)
    thread = _thread(api)
    model["fail"] = RuntimeError("timeout")
    assert _status(_say(api, thread)) == 502
    assert _used(patched_api, fake_bucket) == 0

    model["fail"] = None
    assert _status(_say(api, thread)) == 201         # единственная попытка не сгорела
    assert _status(_say(api, thread)) == 429


def test_attempt_is_returned_when_the_turn_breaks_before_the_model(api, model, patched_api,
                                                                   fake_bucket, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("хранилище недоступно")

    thread = _thread(api)
    monkeypatch.setattr(patched_api, "build_ai_turn", broken)
    assert _status(_say(api, thread)) == 500
    assert model["calls"] == 0 and _used(patched_api, fake_bucket) == 0


def test_counter_that_cannot_be_written_stops_the_turn(api, model, fake_bucket, monkeypatch):
    """Каждую запись счётчика кто-то опережает. Попытку занять не удалось —
    значит, и модель звать нельзя: иначе лимит снова обходится нагрузкой."""
    thread = _thread(api)
    real_get = fake_bucket.get_blob

    def get_blob(name):
        blob = real_get(name)
        if name.endswith("/advice/usage.json"):
            fake_bucket.blob(name).upload_from_string('{"date": "2000-01-01", "count": 0}')
        return blob

    monkeypatch.setattr(fake_bucket, "get_blob", get_blob)
    body, code, _ = _say(api, thread)
    assert (code, json.loads(body)) == (429, {"error": "usage_busy"})
    assert model["calls"] == 0


def test_counter_starts_over_on_a_new_day(storage_module, fake_bucket):
    path = "users/u1/advice/usage.json"
    fake_bucket.blob(path).upload_from_string('{"date": "2000-01-01", "count": 30}')
    assert storage_module.reserve_advice_usage(fake_bucket, "u1", limit=1)["count"] == 1
    assert storage_module.reserve_advice_usage(fake_bucket, "u1", limit=1) is None
    # вчерашнюю попытку сегодня не возвращают: счётчик уже начат заново
    fake_bucket.blob(path).upload_from_string('{"date": "2000-01-01", "count": 30}')
    storage_module.release_advice_usage(fake_bucket, "u1")
    assert json.loads(fake_bucket.blob(path).download_as_text())["count"] == 30


def test_fake_bucket_honours_generation_preconditions(fake_bucket):
    """Основа для записей с предусловием (#35): фейк ведёт себя как GCS."""
    fake_bucket.blob("a.json").upload_from_string("1", if_generation_match=0)
    with pytest.raises(PreconditionFailed):          # 0 — «объекта ещё нет», а он есть
        fake_bucket.blob("a.json").upload_from_string("2", if_generation_match=0)

    seen = fake_bucket.get_blob("a.json")
    fake_bucket.blob("a.json").upload_from_string("3", if_generation_match=seen.generation)
    with pytest.raises(PreconditionFailed):          # поколение с тех пор сменилось
        fake_bucket.blob("a.json").upload_from_string("4", if_generation_match=seen.generation)
    with pytest.raises(NotFound):                    # прочитанного поколения больше нет
        seen.download_as_text()

    fresh = fake_bucket.blob("a.json")
    assert fresh.generation is None and fresh.download_as_text() == "3"
    fresh.reload()
    assert fresh.generation > seen.generation
    assert fake_bucket.get_blob("нет.json") is None
    with pytest.raises(NotFound):
        fake_bucket.blob("нет.json").reload()


# ── 4. Чужие legacy-детали ────────────────────────────────────────────────────

LEGACY = {"summary": {"avg_hr": 151}, "laps": [{"lap": 1, "pace": "4:58"}], "samples": {}}


def _legacy_details(fake_bucket, run_id=999):
    """Детали пробежки админа в корне бакета — как до мультипользовательского режима."""
    fake_bucket.blob(f"runs/{run_id}/v1/activity.fit").upload_from_string(b"FIT")
    fake_bucket.blob(f"runs/{run_id}/v1/details.json").upload_from_string(json.dumps(LEGACY))
    fake_bucket.blob(f"runs/{run_id}/manifest.json").upload_from_string(json.dumps(
        {"current_version": 1, "gcs_object_path": f"runs/{run_id}/v1/details.json"}))


def test_claiming_a_legacy_run_id_does_not_hand_over_its_details(api, fake_bucket):
    """id пробежки задаёт клиент. До #53 совпадения с legacy-id хватало: детали
    копировались в namespace этого пользователя и отдавались ему."""
    _legacy_details(fake_bucket)
    assert _status(_post(api, "/", {**RUN, "id": 999})) == 201

    body, code, _ = api(FakeRequest("GET", "/runs/999/details"))
    assert code == 404 and "avg_hr" not in body
    assert not [name for name in fake_bucket._store if name.startswith("users/u1/runs/")]


def test_admin_keeps_own_legacy_details_and_so_does_the_admins_coach(api, fake_bucket):
    _legacy_details(fake_bucket)
    assert _status(_post(api, "/", {**RUN, "id": 999}, **ADMIN)) == 201
    api(FakeRequest("GET", "/me"), **COACH)
    assert _status(_post(api, "/admin/users/coach", {"sub": "c1", "is_coach": True}, **ADMIN)) == 200
    assert _status(_post(api, "/my/coach", {"coach_sub": "c1"}, **ADMIN)) == 200

    # первым приходит тренер: перенос идёт в namespace админа, а не тренера
    seen = api(FakeRequest("GET", "/coach/athletes/admin-sub/runs/999/details"), **COACH)
    assert _status(seen) == 200 and _json(seen) == LEGACY
    assert fake_bucket.blob("users/admin-sub/runs/999/manifest.json").exists()
    assert not [name for name in fake_bucket._store if name.startswith("users/c1/runs/")]
    assert _json(api(FakeRequest("GET", "/runs/999/details"), **ADMIN)) == LEGACY


def test_legacy_fallback_is_refused_to_everyone_but_the_admin(storage_module, fake_bucket):
    _legacy_details(fake_bucket)
    storage_module.resolve_user(fake_bucket, {"sub": "u1", "email": "runner@example.com",
                                              "email_verified": True})
    for sub in ("u1", "ghost"):                      # ghost в реестре нет вовсе
        assert storage_module.read_run_details(fake_bucket, sub, 999) is None
    assert not [name for name in fake_bucket._store
                if name.startswith("users/") and "/runs/" in name]


# ── 5. Тексты ошибок ──────────────────────────────────────────────────────────

def test_unhandled_error_goes_to_the_log_and_not_to_the_client(api, patched_api, monkeypatch,
                                                              caplog):
    secret = "403 on gs://running-tracker-aabramov77/users/u9/runs.json, key sk-live-1234"

    def broken(*args, **kwargs):
        raise RuntimeError(secret)

    monkeypatch.setattr(patched_api, "read_runs", broken)
    with caplog.at_level(logging.ERROR):
        body, code, _ = api(FakeRequest("GET", "/"))

    assert (code, json.loads(body)) == (500, {"error": "internal_error"})
    assert "sk-live" not in body and "gs://" not in body
    assert secret in caplog.text and "GET /" in caplog.text


# ── 6. Админ по email — только при подтверждённом адресе ──────────────────────

@pytest.mark.parametrize("claim", [{}, {"email_verified": False}, {"email_verified": None},
                                   {"email_verified": "false"}, {"email_verified": 1}])
def test_admin_email_without_verification_is_an_ordinary_applicant(storage_module, fake_bucket,
                                                                  claim):
    rec = storage_module.resolve_user(fake_bucket, {
        "sub": "x1", "email": ADMIN["email"], "name": "Mallory", **claim})
    assert (rec["role"], rec["status"], rec["approved_by"]) == ("user", "pending", None)


@pytest.mark.parametrize("verified", [True, "true"])
def test_verified_admin_email_still_makes_an_admin(storage_module, fake_bucket, verified):
    rec = storage_module.resolve_user(fake_bucket, {
        "sub": "admin-sub", "email": ADMIN["email"], "email_verified": verified})
    assert (rec["role"], rec["status"]) == ("admin", "approved")


def test_unverified_admin_email_opens_no_admin_routes(api):
    who = {"sub": "x1", "email": ADMIN["email"], "email_verified": False}
    assert _json(api(FakeRequest("GET", "/me"), **who))["role"] == "user"
    assert _status(api(FakeRequest("GET", "/admin/users"), **who)) == 403
    assert _status(api(FakeRequest("GET", "/config/llm"), **who)) == 403


def test_unverified_admin_email_does_not_bypass_the_registration_limit(storage_module,
                                                                      fake_bucket, monkeypatch):
    monkeypatch.setattr(storage_module, "MAX_PENDING", 0)
    with pytest.raises(storage_module.RegistrationClosed):
        storage_module.resolve_user(fake_bucket, {"sub": "x1", "email": ADMIN["email"]})
