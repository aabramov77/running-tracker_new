"""Новый план с ИИ (#69): бриф, календарь, оценка цели, каркас, генерация и
создание плана из черновика.

Чистые функции проверяются напрямую (`plan_builder`), ход разбора и маршруты —
через `api` с подменённой моделью, как в test_ai_coach.py.
"""
from datetime import date

import pytest

import plan_builder as pb
from compliance import week_windows

TODAY = date(2026, 11, 15)          # воскресенье


def _race_brief(**over):
    raw = {"type": "race", "title": "Весенний полумарафон", "start_date": "2026-11-23",
           "race_date": "2027-03-14", "distance_km": 21.1, "target_time": "1:45:00",
           "sessions_per_week": 4, "days": ["tue", "thu", "sat", "sun"],
           "long_run_day": "sun", "notes": "колено после травмы"}
    raw.update(over)
    return raw


def _general_brief(**over):
    raw = {"type": "general", "title": "Аэробная база", "start_date": "2026-11-16",
           "weeks": 6, "sessions_per_week": 3, "days": [], "long_run_day": ""}
    raw.update(over)
    return raw


def _clean(raw, today=TODAY):
    brief, reason = pb.clean_brief(raw, today)
    assert reason is None, reason
    return brief


def _rejected(raw, today=TODAY):
    brief, reason = pb.clean_brief(raw, today)
    assert brief is None
    return reason


# ── бриф ──────────────────────────────────────────────────────────────────────

def test_race_brief_is_accepted_and_normalized():
    brief = _clean(_race_brief(distance_km="21,1", days=["sun", "tue", "sat", "thu"]))
    assert brief["type"] == "race" and brief["distance_km"] == 21.1
    assert brief["target_time"] == "1:45:00"
    assert brief["days"] == ["tue", "thu", "sat", "sun"]          # по порядку недели
    assert brief["weeks"] == 16 and brief["notes"] == "колено после травмы"


def test_general_brief_is_accepted_without_race_fields():
    brief = _clean(_general_brief(race_date="2027-01-01", target_time="40:00"))
    assert brief["weeks"] == 6
    assert "race_date" not in brief and "target_time" not in brief


@pytest.mark.parametrize("target", ["", None])
def test_empty_target_means_finish(target):
    assert _clean(_race_brief(target_time=target))["target_time"] == ""


def test_hours_and_minutes_target_is_read_as_such():
    """«1:45» к полумарафону — 1 ч 45 мин, а не 1 мин 45 с."""
    assert _clean(_race_brief(target_time="1:45"))["target_time"] == "1:45:00"
    assert _clean(_race_brief(distance_km=10, target_time="45:00"))["target_time"] == "45:00"


def test_start_defaults_to_the_next_monday():
    brief = _clean(_race_brief(start_date=None))
    assert brief["start_date"] == "2026-11-16"
    assert pb.next_monday(date(2026, 11, 16)) == date(2026, 11, 23)  # понедельник → следующий


@pytest.mark.parametrize("raw, reason", [
    (_race_brief(race_date="2026-11-20"), "race_date"),           # старт раньше начала
    (_race_brief(race_date="весной"), "race_date"),
    (_race_brief(start_date="2026-11-10"), "start_date"),         # начало в прошлом
    (_general_brief(weeks=20), "weeks"),
    (_general_brief(weeks=0), "weeks"),
    (_race_brief(long_run_day="mon"), "long_run_day"),            # не из доступных
    (_race_brief(days=["sunday"]), "days"),
    (_race_brief(distance_km=0.5), "distance_km"),
    (_race_brief(target_time="быстро"), "target_time"),
    (_race_brief(target_time="0:30"), "target_time"),             # нереальный темп
    (_race_brief(sessions_per_week=0), "sessions_per_week"),
    (_race_brief(title="  "), "title"),
    (_race_brief(type="marathon"), "type"),
    ("бриф", "not_an_object"),
])
def test_bad_brief_is_rejected(raw, reason):
    assert _rejected(raw) == reason


def test_long_run_day_is_free_when_days_are_not_limited():
    assert _clean(_general_brief(long_run_day="sat"))["long_run_day"] == "sat"


# ── календарь ─────────────────────────────────────────────────────────────────

def test_race_on_sunday_gives_sixteen_weeks():
    weeks = pb.brief_calendar(_clean(_race_brief()))
    assert len(weeks) == 16
    assert (weeks[0]["start"], weeks[0]["end"]) == ("23.11", "29.11")
    assert (weeks[-1]["start"], weeks[-1]["end"]) == ("08.03", "14.03")
    assert [w["w"] for w in weeks] == list(range(1, 17))


def test_start_on_thursday_gives_a_short_first_week():
    weeks = pb.brief_calendar(_clean(_race_brief(start_date="2026-11-19")))
    assert (weeks[0]["start"], weeks[0]["end"]) == ("19.11", "22.11")
    assert weeks[1]["start"] == "23.11" and len(weeks) == 17


def test_calendar_windows_match_the_plan_reading_across_new_year():
    """Подписи календаря читаются планом в те же даты — и через Новый год."""
    brief = _clean(_race_brief(start_date="2026-11-19"))
    weeks = pb.brief_calendar(brief)
    windows = week_windows(weeks, brief["start_date"])
    assert [(s.isoformat(), e.isoformat()) for s, e in windows] == \
           [(w["start_date"], w["end_date"]) for w in weeks]
    assert any(w["start_date"].startswith("2027-") for w in weeks)


def test_long_plan_is_labelled_with_the_year():
    """Дальше полугода от начала «ДД.ММ» прочиталось бы не тем годом."""
    brief = _clean(_race_brief(start_date="2026-11-16", race_date="2027-06-06"))
    weeks = pb.brief_calendar(brief)
    assert weeks[0]["start"] == "16.11.2026" and weeks[-1]["end"] == "06.06.2027"
    windows = week_windows(weeks, brief["start_date"])
    assert windows[-1][1] == date(2027, 6, 6)


def test_too_long_plan_is_rejected():
    assert _rejected(_race_brief(start_date="2026-11-16", race_date="2027-06-20")) == "too_long"
    assert _clean(_race_brief(start_date="2026-11-16", race_date="2027-06-13"))["weeks"] == 30


def test_general_calendar_has_the_requested_weeks():
    weeks = pb.brief_calendar(_clean(_general_brief(start_date="2026-11-18", weeks=3)))
    assert [(w["start"], w["end"]) for w in weeks] == \
           [("18.11", "22.11"), ("23.11", "29.11"), ("30.11", "06.12")]


def test_race_day_and_its_cell():
    brief = _clean(_race_brief())
    assert pb.race_day(brief) == (16, "sun")
    assert pb.race_cell(brief) == "СТАРТ 21.1 км, цель 1:45"
    assert pb.race_cell(_clean(_race_brief(target_time=""))) == "СТАРТ 21.1 км"
    assert pb.race_day(_clean(_general_brief())) is None


# ── оценка цели ───────────────────────────────────────────────────────────────

def _race(dist_label, time, day):
    return {"dist_label": dist_label, "time": time, "date": day, "name": "забег"}


def test_prediction_from_a_ten_k_within_a_year():
    brief = _clean(_race_brief())
    result = pb.goal_assessment(brief, [_race("10km", "45:00", "2026-09-20")], TODAY)
    expected = round(2700 * (21.1 / 10) ** 1.06)
    assert result["prediction"]["seconds"] == expected
    assert result["gap_sec"] == 6300 - expected          # цель медленнее прогноза
    assert result["weeks"] == 16 and result["min_weeks"] == 8


def test_no_races_this_year_means_no_prediction():
    brief = _clean(_race_brief())
    result = pb.goal_assessment(brief, [_race("10km", "45:00", "2024-09-20")], TODAY)
    assert result["prediction"] is None and result["gap_sec"] is None


def test_finish_goal_has_prediction_but_no_gap():
    brief = _clean(_race_brief(target_time=""))
    result = pb.goal_assessment(brief, [_race("10km", "45:00", "2026-09-20")], TODAY)
    assert result["prediction"] and result["gap_sec"] is None


def test_general_plan_has_no_goal_assessment():
    assert pb.goal_assessment(_clean(_general_brief()), [], TODAY) is None


@pytest.mark.parametrize("km, weeks", [(5, 4), (10, 6), (21.1, 8), (42.2, 12), (3, 4)])
def test_minimum_weeks_by_distance(km, weeks):
    assert pb.min_weeks_for(km) == weeks


# ── каркас ────────────────────────────────────────────────────────────────────

def _outline(kms, types=None, **over):
    types = types or ["dev"] * len(kms)
    raw = {"weeks": [{"week": i + 1, "type": t, "km": km, "accent": f"акцент {i + 1}",
                      "key": "длительная"} for i, (km, t) in enumerate(zip(kms, types))],
           "paces": [{"name": "лёгкий", "value": "5:50–6:20"}]}
    raw.update(over)
    return raw


def _calendar(weeks):
    return pb.brief_calendar(_clean(_general_brief(weeks=weeks)))


def test_outline_is_accepted_and_sorted():
    raw = _outline([30, 33, 25])
    raw["weeks"].reverse()
    outline, reason = pb.clean_outline(raw, _calendar(3))
    assert reason is None
    assert [w["week"] for w in outline["weeks"]] == [1, 2, 3]
    assert outline["weeks"][0] == {"week": 1, "type": "dev", "km": 30, "accent": "акцент 1",
                                   "key": "длительная"}
    assert outline["paces"] == [{"name": "лёгкий", "value": "5:50–6:20"}]


def test_outline_with_fewer_weeks_than_the_calendar_is_rejected():
    outline, reason = pb.clean_outline(_outline([30] * 15), _calendar(16))
    assert outline is None and reason == "weeks"


@pytest.mark.parametrize("change, reason", [
    ({"type": "base"}, "type"), ({"km": 301}, "km"), ({"km": "много"}, "km"),
    ({"week": 0}, "weeks"), ({"week": "один"}, "week"),
])
def test_bad_outline_week_is_rejected(change, reason):
    raw = _outline([30, 33])
    raw["weeks"][1].update(change)
    assert pb.clean_outline(raw, _calendar(2)) == (None, reason)


def test_duplicate_week_is_rejected():
    raw = _outline([30, 33])
    raw["weeks"][1]["week"] = 1
    assert pb.clean_outline(raw, _calendar(2)) == (None, "week")


def test_paces_are_capped_and_bad_ones_dropped():
    paces = [{"name": f"темп {i}", "value": "5:00"} for i in range(10)] + ["мусор", {"name": ""}]
    outline, _ = pb.clean_outline(_outline([30], paces=paces), _calendar(1))
    assert len(outline["paces"]) == 8


def test_volume_jump_is_warned():
    outline, _ = pb.clean_outline(_outline([36, 40, 50]), _calendar(3))
    warnings = pb.outline_warnings(outline)
    assert len(warnings) == 1 and warnings[0].startswith("Неделя 3: объём 40 → 50 км (+25 %)")


def test_growth_after_a_recovery_week_is_not_a_jump():
    outline, _ = pb.clean_outline(_outline([40, 28, 42], ["dev", "load", "dev"]), _calendar(3))
    assert pb.outline_warnings(outline) == []


def test_first_week_above_the_usual_volume_is_warned():
    outline, _ = pb.clean_outline(_outline([40]), _calendar(1))
    assert pb.outline_warnings(outline, usual_km=30)[0].startswith("Неделя 1: 40 км")
    assert pb.outline_warnings(outline, usual_km=35) == []
    assert pb.outline_warnings(outline, usual_km=None) == []


def test_too_few_weeks_before_the_race_is_warned():
    brief = _clean(_race_brief(start_date="2026-11-16", race_date="2026-12-13"))   # 4 недели
    assessment = pb.goal_assessment(brief, [], TODAY)
    outline, _ = pb.clean_outline(_outline([20, 22, 24, 15]), pb.brief_calendar(brief))
    assert any("разумно не меньше 8" in w for w in pb.outline_warnings(outline, None, assessment))


# ── инструкции и блоки данных ─────────────────────────────────────────────────

import llm_prompt  # noqa: E402


def test_plan_builder_instructions_have_steps_and_no_proposals():
    text = llm_prompt.plan_builder_instructions()
    for rule in ("1. Бриф.", "2. Каркас. Только после того как спортсмен подтвердил бриф",
                 "3. Недели.", "Не пиши, что план создан или сохранён",
                 "спрашивай только то, чего там нет", "не больше чем на 10–15 % в неделю",
                 "Длительная — не больше трети", '"brief": null', '"outline": null',
                 '"memory": null', "План длиннее 30 недель не составляется",
                 "Не записывай оценок личности", "по одному показателю"):
        assert rule in text, rule
    assert "proposal" not in text


def test_regular_coach_offers_the_plan_builder():
    for with_proposals in (True, False):
        text = llm_prompt.coach_chat_instructions(with_proposals)
        assert "«🤖 Составить с ИИ»" in text
        assert "План по неделям в этом ответе не расписывай" in text
        assert "Сейчас ты составляешь" not in text


def test_brief_block_lists_the_calendar_and_the_race_week():
    brief = _clean(_race_brief())
    text = llm_prompt.format_brief_block(brief, pb.brief_calendar(brief))
    assert "Тип: к старту" in text and "Старт: 2027-03-14, 21.1 км, цель 1:45:00" in text
    assert "дни: вт, чт, сб, вс; длительная: вс" in text
    assert "Календарь плана — 16 недель:" in text
    assert "  Неделя 1: 23.11–29.11" in text
    assert "  Неделя 16: 08.03–14.03 (старт 14.03)" in text


def test_brief_block_of_a_general_plan():
    brief = _clean(_general_brief())
    text = llm_prompt.format_brief_block(brief, pb.brief_calendar(brief))
    assert "общий, без старта" in text and "Старт:" not in text
    assert "дни: без ограничений" in text and "6 недель" in text


def test_goal_assessment_block():
    brief = _clean(_race_brief())
    text = llm_prompt.format_goal_assessment(
        pb.goal_assessment(brief, [_race("10km", "45:00", "2026-09-20")], TODAY))
    assert "До старта 16 недель; для 21.1 км разумно не меньше 8." in text
    assert "Прогноз по формуле Ригеля от забега 10km за 45:00 (2026-09-20)" in text
    assert "цель медленнее прогноза" in text
    empty = llm_prompt.format_goal_assessment(pb.goal_assessment(brief, [], TODAY))
    assert "Прогноза нет" in empty
    assert llm_prompt.format_goal_assessment(None) == ""


def test_outline_block():
    outline, _ = pb.clean_outline(_outline([30, 28], ["dev", "load"]), _calendar(2))
    text = llm_prompt.format_outline_block(outline)
    assert "  Неделя 2 (Разгрузка): 28 км — акцент 2; ключевые: длительная" in text
    assert "Ориентиры темпа: лёгкий 5:50–6:20" in text


# ── ход разбора «Новый план» через HTTP ───────────────────────────────────────

import datetime as dt  # noqa: E402
import json  # noqa: E402

from conftest import FakeRequest  # noqa: E402

SUB = "u1"


def _reply(text="Принято.", **extra):
    return {"text": json.dumps({"reply": text, **extra}, ensure_ascii=False),
            "input_tokens": 900, "output_tokens": 60}


@pytest.fixture
def llm(patched_api, fake_bucket, monkeypatch):
    """Модель из очереди `replies`, как в test_ai_coach.py."""
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


def _live_brief(weeks=16, **over):
    """Бриф к старту от ближайшего понедельника: даты от настоящего «сегодня»,
    как их видит сервер."""
    start = pb.next_monday(dt.datetime.utcnow().date())
    race = start + dt.timedelta(days=7 * weeks - 1)              # воскресенье
    return _race_brief(start_date=start.isoformat(), race_date=race.isoformat(), **over)


def _live_outline(weeks=16, kms=None):
    kms = kms or [30 + i for i in range(weeks)]
    types = ["dev"] * (weeks - 1) + ["race"]
    return _outline(kms, types)


def _plan_thread(api):
    body, code, _ = api(FakeRequest("POST", "/ai-coach/threads", {"kind": "plan"}))
    assert code == 201
    return json.loads(body)["thread"]


def _ask(api, thread_id, text="Хочу план к полумарафону"):
    body, code, _ = api(FakeRequest("POST", f"/ai-coach/threads/{thread_id}/messages",
                                    {"text": text}))
    assert code == 201, body
    return json.loads(body)["messages"][1]


def test_plan_thread_turn_uses_the_builder_instructions(api, llm):
    thread = _plan_thread(api)
    _ask(api, thread["id"])
    system = llm["calls"][0]["system"]
    assert "Сейчас ты составляешь со спортсменом новый план" in system
    assert '"brief": null' in system and "proposal" not in system
    assert "Бриф нового плана" not in system                     # брифа ещё нет


def test_brief_is_stored_in_the_answer(api, llm, fake_bucket):
    thread = _plan_thread(api)
    llm["replies"].append(_reply("Проверьте бриф.", brief=_live_brief()))
    answer = _ask(api, thread["id"])
    assert answer["brief"]["weeks"] == 16 and answer["brief"]["title"] == "Весенний полумарафон"
    assert "draft_rejected" not in answer
    body, _, _ = api(FakeRequest("GET", f"/ai-coach/threads/{thread['id']}"))
    assert json.loads(body)["messages"][1]["brief"] == answer["brief"]
    # следующий ход видит бриф, календарь и оценку цели
    _ask(api, thread["id"], "Всё верно")
    system = llm["calls"][1]["system"]
    assert "=== Бриф нового плана (последний принятый) ===" in system
    assert "Календарь плана — 16 недель:" in system and "=== Оценка цели ===" in system


def test_bad_brief_is_marked_and_the_reply_kept(api, llm):
    thread = _plan_thread(api)
    llm["replies"].append(_reply("Вот бриф.", brief=_live_brief(target_time="быстро")))
    answer = _ask(api, thread["id"])
    assert answer["text"] == "Вот бриф."
    assert "brief" not in answer and answer["draft_rejected"] == ["brief"]


def test_outline_without_a_brief_is_rejected(api, llm):
    thread = _plan_thread(api)
    llm["replies"].append(_reply("Каркас.", outline=_live_outline()))
    answer = _ask(api, thread["id"])
    assert "outline" not in answer and answer["draft_rejected"] == ["outline"]


def test_outline_follows_the_brief_of_an_earlier_answer(api, llm):
    thread = _plan_thread(api)
    llm["replies"].append(_reply("Бриф.", brief=_live_brief()))
    first = _ask(api, thread["id"])
    kms = [30, 33, 40] + [40] * 13
    llm["replies"].append(_reply("Каркас.", outline=_live_outline(kms=kms)))
    answer = _ask(api, thread["id"], "Всё верно — предложи каркас")
    outline = answer["outline"]
    assert outline["brief_message_id"] == first["id"]
    assert len(outline["weeks"]) == 16
    assert any(w.startswith("Неделя 3: объём 33 → 40 км") for w in outline["warnings"])
    # следующий ход видит этот каркас
    _ask(api, thread["id"], "А что на неделе 3?")
    assert "=== Последний каркас ===" in llm["calls"][2]["system"]


def test_outline_in_the_same_answer_as_the_brief(api, llm):
    thread = _plan_thread(api)
    llm["replies"].append(_reply("Бриф и каркас.", brief=_live_brief(weeks=4),
                                 outline=_live_outline(weeks=4)))
    answer = _ask(api, thread["id"])
    assert answer["brief"]["weeks"] == 4 and len(answer["outline"]["weeks"]) == 4
    assert "brief_message_id" not in answer["outline"]           # бриф этой же реплики


def test_outline_for_an_old_brief_is_not_shown_after_the_brief_changed(api, llm):
    thread = _plan_thread(api)
    llm["replies"].append(_reply("Бриф.", brief=_live_brief()))
    _ask(api, thread["id"])
    llm["replies"].append(_reply("Каркас.", outline=_live_outline()))
    _ask(api, thread["id"], "Да")
    llm["replies"].append(_reply("Новый бриф.", brief=_live_brief(weeks=12)))
    _ask(api, thread["id"], "Давай старт на месяц раньше")
    _ask(api, thread["id"], "Что дальше?")
    system = llm["calls"][3]["system"]
    assert "Календарь плана — 12 недель:" in system
    assert "=== Последний каркас ===" not in system


def test_proposal_in_a_plan_thread_is_dropped(api, llm, patched_api, fake_bucket):
    today = dt.datetime.utcnow().date()
    monday = today - dt.timedelta(days=today.weekday())
    plan = patched_api.create_plan(fake_bucket, SUB, {"race_name": "HM",
                                                      "plan_start": monday.isoformat()})
    patched_api.save_plan_weeks(fake_bucket, SUB, plan["id"], [
        {"w": i + 1, "start": (monday + dt.timedelta(days=7 * i)).strftime("%d.%m.%Y"),
         "type": "dev", "mon": "10 км", "sun": "16 км"} for i in range(3)],
        "seed", "runner@example.com")
    thread = _plan_thread(api)
    proposal = {"summary": "x", "changes": [{"week": 2, "day": "mon", "text": "8 км"}]}
    llm["replies"].append(_reply("Ок.", proposal=proposal))
    answer = _ask(api, thread["id"])
    assert "proposal" not in answer and "proposal_state" not in answer
    assert "=== План: что можно менять ===" not in llm["calls"][0]["system"]


def test_usual_weekly_km(storage_module):
    today = dt.date(2026, 11, 15)
    runs = [{"date": f"2026-11-{d:02d}", "dist": 10} for d in (1, 5, 9, 12)]
    assert storage_module.usual_weekly_km({}, runs, today) == 10
    assert storage_module.usual_weekly_km({"weekly_km_typical": 42}, runs, today) == 42
    assert storage_module.usual_weekly_km({}, runs[:3], today) is None
    old = [{"date": "2026-09-01", "dist": 10}] * 5
    assert storage_module.usual_weekly_km({}, old, today) is None


def test_plan_thread_shows_up_in_past_threads_and_preview_works(api, llm):
    thread = _plan_thread(api)
    llm["replies"].append(_reply("Бриф.", brief=_live_brief(),
                                 summary="Согласовали бриф полумарафона за 16 недель."))
    _ask(api, thread["id"])
    body, code, _ = api(FakeRequest("POST", "/ai-coach/threads", {"title": "Итоги недели"}))
    other = json.loads(body)["thread"]
    _ask(api, other["id"], "Как неделя?")
    system = llm["calls"][1]["system"]
    assert "«Новый план»" in system and "Согласовали бриф полумарафона" in system
    assert "Сейчас ты составляешь" not in system                  # обычный разбор
    body, code, _ = api(FakeRequest("GET", "/advise/preview"))
    assert code == 200 and "«🤖 Составить с ИИ»" in json.loads(body)["system_prompt"]
