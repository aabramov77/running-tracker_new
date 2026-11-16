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


# ── генерация недель ──────────────────────────────────────────────────────────

import re  # noqa: E402
import threading  # noqa: E402


def _week_cells(n):
    return {"week": n, "mon": "", "tue": f"8 км легко н{n}", "wed": "", "thu": "10 км: 3×2 км",
            "fri": "", "sat": "6 км восстановительный", "sun": "16 км длительный"}


class FakeWeeksModel:
    """Отвечает на запрос куска по номерам недель из вопроса. skip — недели,
    которые модель «забывает» (в первый раз или всегда)."""

    def __init__(self, skip_once=(), skip_always=(), cells=_week_cells):
        self.skip_once, self.skip_always, self.cells = set(skip_once), set(skip_always), cells
        self.asked, self.lock = [], threading.Lock()

    def numbers(self, user):
        found = [int(x) for x in re.findall(r"\d+", user)]
        return list(range(found[0], found[-1] + 1))

    def __call__(self, system, user):
        numbers = self.numbers(user)
        with self.lock:
            self.asked.append((numbers, system))
            skipped = {n for n in numbers if n in self.skip_always or n in self.skip_once}
            self.skip_once -= skipped
        weeks = [self.cells(n) for n in numbers if n not in skipped]
        return {"text": json.dumps({"weeks": weeks}, ensure_ascii=False),
                "input_tokens": 1000, "output_tokens": 300}


def _use_model(patched_api, monkeypatch, model):
    monkeypatch.setattr(patched_api, "call_llm",
                        lambda provider, mdl, key, system, user, effort=None, history=None:
                        model(system, user))


def _draft_inputs(**over):
    brief = _clean(_race_brief(**over))
    kms = [24 + i for i in range(brief["weeks"])]
    types = ["dev"] * (brief["weeks"] - 1) + ["race"]
    outline, _ = pb.clean_outline(_outline(kms, types), pb.brief_calendar(brief))
    return brief, outline


def test_chunk_table_marks_the_days():
    brief, outline = _draft_inputs(start_date="2026-11-19")      # с четверга
    calendar = pb.brief_calendar(brief)
    rows = pb.chunk_rows(brief, calendar, outline, [1, 17])
    text = llm_prompt.format_chunk_days(rows)
    assert "Неделя 1, 19.11–22.11, Развитие: 24 км" in text
    assert "  mon (пн 16.11): вне плана — оставь пустым" in text
    assert "  thu (чт 19.11)\n" in text
    assert "  wed (ср 25.11)" not in text                          # недели 2 в куске нет
    assert "  mon (пн 08.03): недоступный день — отдых" in text
    assert "  sun (вс 14.03): день старта — оставь пустым" in text
    assert "  sat (сб 13.03)\n" in text


def test_sixteen_weeks_in_four_parallel_calls(storage_module):
    brief, outline = _draft_inputs()
    model = FakeWeeksModel()
    draft = storage_module.generate_plan_draft(brief, outline, model, ["Возраст: 40 лет"])
    assert draft["calls"] == 4
    assert sorted(n for ns, _ in model.asked for n in ns) == list(range(1, 17))
    assert draft["input_tokens"] == 4000 and draft["output_tokens"] == 1200
    weeks = draft["weeks"]
    assert len(weeks) == 16 and weeks[0]["start"] == "23.11" and weeks[-1]["end"] == "14.03"
    assert weeks[0]["type"] == "dev" and weeks[0]["accent"] == "акцент 1"
    assert weeks[4]["tue"] == "8 км легко н5"
    assert weeks[-1]["sun"] == "СТАРТ 21.1 км, цель 1:45"           # не «16 км длительный»
    system = model.asked[0][1]
    assert "=== Спортсмен ===\nВозраст: 40 лет" in system and "=== Каркас плана ===" in system
    assert "=== Расписать недели ===" in system


def test_days_before_the_start_stay_empty(storage_module):
    brief, outline = _draft_inputs(start_date="2026-11-19")
    draft = storage_module.generate_plan_draft(brief, outline, FakeWeeksModel())
    first = draft["weeks"][0]
    assert first["tue"] == "" and first["thu"] == "10 км: 3×2 км"


def test_missing_week_is_asked_again_alone(storage_module):
    brief, outline = _draft_inputs()
    model = FakeWeeksModel(skip_once={7})
    draft = storage_module.generate_plan_draft(brief, outline, model)
    assert draft["calls"] == 5 and [7] in [ns for ns, _ in model.asked]
    assert draft["weeks"][6]["tue"] == "8 км легко н7"


def test_week_missing_twice_fails_the_draft(storage_module):
    brief, outline = _draft_inputs()
    with pytest.raises(storage_module.PlanDraftIncomplete):
        storage_module.generate_plan_draft(brief, outline, FakeWeeksModel(skip_always={7}))


def test_garbage_reply_counts_as_missing_weeks(storage_module):
    brief, outline = _draft_inputs()
    calls = []

    def model(system, user):
        calls.append(user)
        return {"text": "не JSON", "input_tokens": 1, "output_tokens": 1}

    with pytest.raises(storage_module.PlanDraftIncomplete):
        storage_module.generate_plan_draft(brief, outline, model)
    # первый провалившийся кусок отменяет ещё не начатые: лишних вызовов нет
    assert 2 <= len(calls) <= 8


def test_draft_warnings():
    brief, outline = _draft_inputs()
    calendar = pb.brief_calendar(brief)
    cells = {n: {**{f: "" for f in pb.DAY_FIELDS}, "tue": "5 км", "thu": "5 км",
                 "sat": "6 км", "sun": "8 км"} for n in range(1, 17)}
    cells[2].update(mon="5 км легко")                                # пн — не из доступных
    cells[4].update(sun="40 км")                                     # объём сильно выше каркаса
    weeks = pb.assemble_weeks(brief, calendar, outline, cells)
    warnings = pb.draft_warnings(brief, calendar, outline, weeks)
    assert "Неделя 2: тренировка в пн — не из доступных дней" in warnings
    assert "Неделя 2: тренировок 5 — больше, чем в брифе (4)" in warnings
    assert any(w.startswith("Неделя 4: по ячейкам не меньше 56 км") for w in warnings)
    assert not any(w.startswith("Неделя 3") for w in warnings)


def test_race_on_an_unavailable_day_is_not_warned():
    brief, outline = _draft_inputs(race_date="2027-03-13")          # суббота
    calendar = pb.brief_calendar(brief)
    cells = {n: {f: "" for f in pb.DAY_FIELDS} for n in range(1, 17)}
    brief = {**brief, "days": ["tue", "thu", "sun"]}
    weeks = pb.assemble_weeks(brief, calendar, outline, cells)
    assert weeks[-1]["sat"].startswith("СТАРТ")
    assert pb.draft_warnings(brief, calendar, outline, weeks) == []


# ── генерация через HTTP ──────────────────────────────────────────────────────

def _outline_message(api, llm, weeks=8):
    thread = _plan_thread(api)
    llm["replies"].append(_reply("Бриф.", brief=_live_brief(weeks=weeks)))
    _ask(api, thread["id"])
    llm["replies"].append(_reply("Каркас.", outline=_live_outline(weeks=weeks)))
    return thread, _ask(api, thread["id"], "Всё верно")


def _generate(api, thread_id, message_id):
    return api(FakeRequest("POST", f"/ai-coach/threads/{thread_id}/messages/{message_id}/weeks"))


def _usage(api):
    body, _, _ = api(FakeRequest("GET", "/ai-coach/threads"))
    return json.loads(body)["usage"]["count"]


def _messages(api, thread_id):
    body, _, _ = api(FakeRequest("GET", f"/ai-coach/threads/{thread_id}"))
    return json.loads(body)["messages"]


def test_weeks_route_writes_a_draft_for_one_attempt(api, llm, patched_api, monkeypatch):
    thread, outline_msg = _outline_message(api, llm)
    _use_model(patched_api, monkeypatch, FakeWeeksModel())
    before = _usage(api)
    body, code, _ = _generate(api, thread["id"], outline_msg["id"])
    assert code == 201, body
    data = json.loads(body)
    question, answer = data["messages"]
    assert question["role"] == "athlete" and question["text"] == "Расписать план по неделям"
    draft = answer["plan_draft"]
    assert len(draft["weeks"]) == 8 and draft["outline_message_id"] == outline_msg["id"]
    assert draft["brief"]["weeks"] == 8 and answer["llm_calls"] == 2
    assert answer["plan_draft_state"] == "open"
    assert answer["text"].startswith("План расписан по неделям: 8 нед.")
    assert _usage(api) == before + 1 == data["usage"]["count"]
    assert _messages(api, thread["id"])[-1]["plan_draft"] == draft


def test_bad_draft_writes_nothing_and_returns_the_attempt(api, llm, patched_api, monkeypatch):
    thread, outline_msg = _outline_message(api, llm)
    _use_model(patched_api, monkeypatch, FakeWeeksModel(skip_always={3}))
    before, count = _usage(api), len(_messages(api, thread["id"]))
    body, code, _ = _generate(api, thread["id"], outline_msg["id"])
    assert code == 502 and json.loads(body)["error"] == "llm_bad_draft"
    assert _usage(api) == before and len(_messages(api, thread["id"])) == count


def test_provider_failure_on_a_chunk_writes_nothing(api, llm, patched_api, monkeypatch):
    import httpx
    thread, outline_msg = _outline_message(api, llm)

    def broken(provider, mdl, key, system, user, effort=None, history=None):
        request = httpx.Request("POST", "https://api.example/chat")
        raise httpx.HTTPStatusError("boom", request=request,
                                    response=httpx.Response(429, request=request, text="rate"))

    monkeypatch.setattr(patched_api, "call_llm", broken)
    before, count = _usage(api), len(_messages(api, thread["id"]))
    body, code, _ = _generate(api, thread["id"], outline_msg["id"])
    assert code == 502 and json.loads(body)["error"] == "llm_provider_error"
    assert _usage(api) == before and len(_messages(api, thread["id"])) == count


def test_second_generation_adds_a_second_draft(api, llm, patched_api, monkeypatch):
    thread, outline_msg = _outline_message(api, llm)
    _use_model(patched_api, monkeypatch, FakeWeeksModel())
    first = json.loads(_generate(api, thread["id"], outline_msg["id"])[0])["messages"][1]
    second = json.loads(_generate(api, thread["id"], outline_msg["id"])[0])["messages"][1]
    drafts = [m for m in _messages(api, thread["id"]) if m.get("plan_draft")]
    assert [m["id"] for m in drafts] == [first["id"], second["id"]]
    assert drafts[0]["plan_draft"] == first["plan_draft"]


def test_weeks_route_errors(api, llm):
    thread, outline_msg = _outline_message(api, llm)
    brief_msg = _messages(api, thread["id"])[1]
    body, code, _ = _generate(api, thread["id"], brief_msg["id"])
    assert code == 400 and json.loads(body)["error"] == "no_outline"
    body, code, _ = _generate(api, "20260101T000000000000-deadbeef", outline_msg["id"])
    assert code == 404 and json.loads(body)["error"] == "thread not found"
    body, code, _ = _generate(api, thread["id"], "20260101T000000000000-ai-deadbeef")
    assert code == 404 and json.loads(body)["error"] == "message not found"


# ── создание плана из черновика ───────────────────────────────────────────────

def _draft_message(api, llm, patched_api, monkeypatch, weeks=8):
    thread, outline_msg = _outline_message(api, llm, weeks=weeks)
    _use_model(patched_api, monkeypatch, FakeWeeksModel())
    body, code, _ = _generate(api, thread["id"], outline_msg["id"])
    assert code == 201, body
    return thread, json.loads(body)["messages"][1]


def _create(api, thread_id, message_id, **body):
    return api(FakeRequest("POST",
                           f"/ai-coach/threads/{thread_id}/messages/{message_id}/create-plan",
                           body))


def _plans(api):
    body, _, _ = api(FakeRequest("GET", "/plans"))
    return json.loads(body)


def _history(api, plan_id):
    body, _, _ = api(FakeRequest("GET", f"/plans/{plan_id}/history"))
    return json.loads(body)["versions"]


def test_plan_from_a_draft_without_edits(api, llm, patched_api, monkeypatch):
    thread, answer = _draft_message(api, llm, patched_api, monkeypatch)
    body, code, _ = _create(api, thread["id"], answer["id"], make_active=True)
    assert code == 201, body
    created = json.loads(body)
    plan = created["plan"]
    brief = answer["plan_draft"]["brief"]
    assert created["version"] == 1 and created["active_plan_id"] == plan["id"]
    assert plan["race_name"] == "Весенний полумарафон" and plan["race_date"] == brief["race_date"]
    assert plan["target_time"] == "1:45" and plan["plan_start"] == brief["start_date"]
    listed = next(p for p in _plans(api)["plans"] if p["id"] == plan["id"])
    assert listed["created_by_role"] == "ai"
    assert listed["ai_source"] == {"thread_id": thread["id"], "message_id": answer["id"]}
    body, _, _ = api(FakeRequest("GET", f"/plans/{plan['id']}/weeks"))
    assert json.loads(body) == answer["plan_draft"]["weeks"]
    history = _history(api, plan["id"])
    assert [(v["version"], v["author_role"], v["change_reason"]) for v in history] == \
           [(1, "ai", "ИИ-тренер: новый план")]


def test_plan_from_an_edited_draft_has_two_versions(api, llm, patched_api, monkeypatch):
    thread, answer = _draft_message(api, llm, patched_api, monkeypatch)
    weeks = json.loads(json.dumps(answer["plan_draft"]["weeks"]))
    weeks[1]["tue"] = "10 км легко"
    body, code, _ = _create(api, thread["id"], answer["id"], make_active=True, weeks=weeks)
    created = json.loads(body)
    assert code == 201 and created["version"] == 2
    history = _history(api, created["plan"]["id"])
    assert [(v["version"], v["author_role"], v["change_reason"]) for v in history] == \
           [(2, "athlete", "Правка черновика ИИ-тренера"), (1, "ai", "ИИ-тренер: новый план")]
    body, _, _ = api(FakeRequest("GET", f"/plans/{created['plan']['id']}/weeks"))
    assert json.loads(body)[1]["tue"] == "10 км легко"


def test_unchanged_weeks_give_one_version(api, llm, patched_api, monkeypatch):
    thread, answer = _draft_message(api, llm, patched_api, monkeypatch)
    body, code, _ = _create(api, thread["id"], answer["id"], make_active=True,
                            weeks=answer["plan_draft"]["weeks"])
    assert code == 201 and json.loads(body)["version"] == 1


def test_activate_later_keeps_the_active_plan(api, llm, patched_api, fake_bucket, monkeypatch):
    autumn = patched_api.create_plan(fake_bucket, SUB, {"race_name": "Осенний марафон"})
    thread, answer = _draft_message(api, llm, patched_api, monkeypatch)
    body, code, _ = _create(api, thread["id"], answer["id"], make_active=False)
    created = json.loads(body)
    assert code == 201 and created["active_plan_id"] == autumn["id"]
    plans = _plans(api)
    assert plans["active_plan_id"] == autumn["id"]
    assert created["plan"]["id"] in [p["id"] for p in plans["plans"]]


def test_without_an_active_plan_the_new_one_becomes_active(api, llm, patched_api, monkeypatch):
    thread, answer = _draft_message(api, llm, patched_api, monkeypatch)
    body, code, _ = _create(api, thread["id"], answer["id"], make_active=False)
    assert code == 201 and _plans(api)["active_plan_id"] == json.loads(body)["plan"]["id"]


def test_second_create_is_already_created(api, llm, patched_api, monkeypatch):
    thread, answer = _draft_message(api, llm, patched_api, monkeypatch)
    first = json.loads(_create(api, thread["id"], answer["id"], make_active=True)[0])
    body, code, _ = _create(api, thread["id"], answer["id"], make_active=True)
    assert code == 409
    assert json.loads(body) == {"error": "already_created", "plan_id": first["plan"]["id"]}
    assert len(_plans(api)["plans"]) == 1


def test_bad_weeks_create_nothing(api, llm, patched_api, monkeypatch):
    thread, answer = _draft_message(api, llm, patched_api, monkeypatch)
    weeks = json.loads(json.dumps(answer["plan_draft"]["weeks"]))
    weeks[0]["mon"] = "x" * 501
    body, code, _ = _create(api, thread["id"], answer["id"], make_active=True, weeks=weeks)
    assert code == 400 and json.loads(body)["error"] == "validation_failed"
    assert _plans(api)["plans"] == []


def test_create_errors(api, llm, patched_api, monkeypatch):
    thread, answer = _draft_message(api, llm, patched_api, monkeypatch)
    outline_id = answer["plan_draft"]["outline_message_id"]
    body, code, _ = _create(api, thread["id"], outline_id, make_active=True)
    assert code == 400 and json.loads(body)["error"] == "no_plan_draft"
    body, code, _ = _create(api, thread["id"], "20260101T000000000000-ai-deadbeef")
    assert code == 404 and json.loads(body)["error"] == "message not found"
    body, code, _ = _create(api, "20260101T000000000000-deadbeef", answer["id"])
    assert code == 404 and json.loads(body)["error"] == "thread not found"


def _race_on_index_read(fake_bucket, monkeypatch, competitor):
    """competitor() вклинивается после чтения реестра планов внутри условной
    записи — данные у пишущего к записи уже устарели."""
    real_get, state = fake_bucket.get_blob, {"reads": 0, "done": False}

    def get_blob(name):
        blob = real_get(name)
        if name.endswith("plans/index.json") and not state["done"] and blob is not None:
            state["reads"] += 1
            if state["reads"] == 2:                     # первое — проверка наличия
                state["done"] = True
                read = blob.download_as_text

                def download_as_text():
                    text = read()
                    competitor()
                    return text

                blob.download_as_text = download_as_text
        return blob

    monkeypatch.setattr(fake_bucket, "get_blob", get_blob)


def test_two_concurrent_creates_make_one_plan(api, llm, patched_api, fake_bucket, monkeypatch,
                                              storage_module):
    thread, answer = _draft_message(api, llm, patched_api, monkeypatch)
    patched_api.create_plan(fake_bucket, SUB, {"race_name": "Текущий"})   # реестр уже есть
    results = []

    def competitor():
        results.append(storage_module.create_plan_from_draft(
            fake_bucket, SUB, thread["id"], answer["id"], make_active=True))

    _race_on_index_read(fake_bucket, monkeypatch, competitor)
    with pytest.raises(storage_module.PlanAlreadyCreated):
        storage_module.create_plan_from_draft(fake_bucket, SUB, thread["id"], answer["id"],
                                              make_active=True)
    plans = storage_module.read_plans_index(fake_bucket, SUB)["plans"]
    assert results and len([p for p in plans if p.get("ai_source")]) == 1


def test_plan_without_weeks_is_finished_on_retry(api, llm, patched_api, fake_bucket, monkeypatch,
                                                 storage_module):
    thread, answer = _draft_message(api, llm, patched_api, monkeypatch)
    real_save = storage_module.save_plan_weeks

    def broken(*args, **kwargs):
        raise RuntimeError("сбой между записями")

    monkeypatch.setattr(storage_module, "save_plan_weeks", broken)
    with pytest.raises(RuntimeError):
        storage_module.create_plan_from_draft(fake_bucket, SUB, thread["id"], answer["id"], True)
    monkeypatch.setattr(storage_module, "save_plan_weeks", real_save)
    created = storage_module.create_plan_from_draft(fake_bucket, SUB, thread["id"], answer["id"],
                                                    True)
    assert created["version"] == 1
    assert len(storage_module.read_plans_index(fake_bucket, SUB)["plans"]) == 1


def test_draft_state_on_read(api, llm, patched_api, monkeypatch):
    thread, answer = _draft_message(api, llm, patched_api, monkeypatch)
    assert _messages(api, thread["id"])[-1]["plan_draft_state"] == "open"
    created = json.loads(_create(api, thread["id"], answer["id"], make_active=True)[0])
    last = _messages(api, thread["id"])[-1]
    assert last["plan_draft_state"] == "created" and last["plan_id"] == created["plan"]["id"]
    api(FakeRequest("POST", f"/plans/{created['plan']['id']}/archive"))
    assert _messages(api, thread["id"])[-1]["plan_draft_state"] == "created"
