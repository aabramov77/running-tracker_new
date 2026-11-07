"""История за 12 месяцев в контексте ИИ-тренера (#81).

history.py — чистые функции: месяцы за год и итоги прошлых планов.
llm_prompt — блок для модели. build_llm_context — сборка на фейковом бакете.
"""
import datetime as dt
import json

import pytest

import history
import llm_prompt
from conftest import FakeRequest

TODAY = dt.date(2026, 11, 7)
SUB = "u1"


def _run(rid, date, dist, type_="easy", **extra):
    return {"id": rid, "date": date, "dist": dist, "type": type_, **extra}


# ── месяцы ────────────────────────────────────────────────────────────────────

def test_no_runs_no_months():
    assert history.monthly_summary([], {}, TODAY) == []
    assert history.monthly_summary([_run(1, "2026-11-01", 5, deleted=True)], {}, TODAY) == []


def test_months_run_from_the_first_run_to_the_current_one_with_gaps_kept():
    runs = [_run(1, "2026-08-10", 10), _run(2, "2026-11-02", 8)]
    months = history.monthly_summary(runs, {}, TODAY)
    assert [(m["month"], m["runs"], m["partial"]) for m in months] == [
        ("2026-08", 1, False), ("2026-09", 0, False), ("2026-10", 0, False), ("2026-11", 1, True)]


def test_window_is_capped_at_twelve_months():
    runs = [_run(1, "2024-03-01", 10), _run(2, "2026-01-15", 12)]
    months = history.monthly_summary(runs, {}, TODAY)
    assert len(months) == 12
    assert (months[0]["month"], months[-1]["month"]) == ("2025-12", "2026-11")
    assert months[1]["runs"] == 1                      # 2026-01; пробежка 2024 года не в окне


def test_month_totals():
    runs = [_run(1, "2026-10-01", 10, feel="hard"), _run(2, "2026-10-05", "21.1", "long"),
            _run(3, "2026-10-09", 6, "interval", feel="bad"), _run(4, "2026-10-12", 30, deleted=True),
            _run(5, "2026-10-20", "мусор"), _run(6, "2026-12-01", 5)]          # будущее — опечатка
    (october, november) = history.monthly_summary(runs, {}, TODAY)
    assert (october["runs"], october["km"], october["longest_km"], october["hard"]) == (4, 37.1, 21.1, 2)
    assert november["runs"] == 0


def test_easy_pace_and_hr_are_weighted_by_distance_and_only_for_easy_runs():
    runs = [_run(1, "2026-10-01", 10, pace="6:00", hr="140"),
            _run(2, "2026-10-03", 5, time="27:30", hr=150),       # темп из времени: 5:30
            _run(3, "2026-10-05", 8, "tempo", pace="4:40", hr=170),
            _run(4, "2026-10-07", 4, pace="мусор")]
    (october, _) = history.monthly_summary(runs, {}, TODAY)
    assert october["easy_pace_sec"] == round((360 * 10 + 330 * 5) / 15)       # 5:50
    assert october["easy_hr"] == round((140 * 10 + 150 * 5) / 15)


def test_load_is_summed_and_counts_runs_without_it():
    runs = [_run(1, "2026-10-01", 10), _run(2, "2026-10-03", 5), _run(3, "2026-11-01", 5)]
    loads = {"1": {"ext": 60, "int": 80}, "2": {"ext": None, "int": 40}, "3": {"ext": 30, "int": None}}
    october, november = history.monthly_summary(runs, loads, TODAY)
    assert (october["ext"], october["ext_unknown"], october["int"], october["int_unknown"]) == (60, 1, 120, 0)
    assert (november["ext"], november["int_unknown"]) == (30, 1)


def test_an_axis_no_run_has_is_left_out():
    runs = [_run(1, "2026-10-01", 10)]
    (october, _) = history.monthly_summary(runs, {"1": {"ext": None, "int": 55}}, TODAY)
    assert october["ext"] is None and october["ext_unknown"] is None and october["int"] == 55


# ── прошлые планы ─────────────────────────────────────────────────────────────

def _plan(pid, race_date, **extra):
    return {"id": pid, "race_name": f"Гонка {pid}", "race_date": race_date, "target_time": "1:45:00",
            "plan_start": "", "archived": False, **extra}


def test_past_plans_are_the_latest_finished_ones_that_are_not_hidden_or_active():
    index = {"active_plan_id": "now", "plans": [
        _plan("now", "2026-05-01"), _plan("old", "2025-04-20"), _plan("hm", "2026-09-21"),
        _plan("gone", "2026-10-01", archived=True), _plan("next", "2027-04-01"),
        _plan("nodate", ""), _plan("spring", "2026-04-26"), _plan("winter", "2026-02-01")]}
    picked = history.past_plan_candidates(index, TODAY)
    assert [p["id"] for p in picked] == ["hm", "spring", "winter"]


def test_race_result_is_the_nearest_race_within_three_days():
    races = [{"date": "2026-09-25", "dist_label": "HM", "time": "1:40:00"},
             {"date": "2026-09-21", "dist_label": "HM", "time": "1:46:12", "deleted": True},
             {"date": "2026-09-20", "dist_label": "HM", "time": "1:46:30"},
             {"date": "2026-09-23", "dist_label": "10km", "time": "45:00"}]
    assert history.race_result(races, "2026-09-21") == \
        {"date": "2026-09-20", "dist_label": "HM", "seconds": 6390}
    assert history.race_result(races, "2026-09-10") is None
    assert history.race_result(races, "") is None


def test_past_plan_carries_compliance_and_result():
    weeks = [{"w": 1, "start": "05.10.2026", "mon": "10 км", "wed": "8 км"}]
    runs = [_run(1, "2026-10-05", 10, plan_id="p1"), _run(2, "2026-10-07", 6, plan_id="p1"),
            _run(3, "2026-10-07", 9, plan_id="other")]
    races = [{"date": "2026-10-11", "dist_label": "10km", "time": "44:10"}]
    (plan,) = history.past_plans([_plan("p1", "2026-10-11", plan_start="2026-10-05")],
                                 {"p1": weeks}, runs, races)
    assert plan["weeks"] == 1 and plan["result"]["seconds"] == 2650
    totals = plan["compliance"]
    assert (totals["planned_km"], totals["actual_km"], totals["pct"]) == (18, 16, 89)


def test_undated_or_empty_past_plan_has_no_compliance():
    plans = [_plan("a", "2026-10-11"), _plan("b", "2026-09-01")]
    undated, empty = history.past_plans(plans, {"a": [{"mon": "10 км"}]}, [], [])
    assert (undated["weeks"], undated["compliance"]) == (1, None)
    assert (empty["weeks"], empty["compliance"], empty["result"]) == (0, None, None)


# ── блок промпта ──────────────────────────────────────────────────────────────

def _month(month="2026-10", **over):
    row = {"month": month, "partial": False, "runs": 3, "km": 31.5, "longest_km": 16.0, "hard": 0,
           "easy_pace_sec": 350, "easy_hr": 141, "ext": 180, "ext_unknown": 0,
           "int": 240, "int_unknown": 0}
    return {**row, **over}


def test_history_block_lines():
    months = [_month("2026-08", hard=1), _month("2026-09", runs=0),
              _month("2026-10", ext_unknown=1, int_unknown=3, easy_hr=None),
              _month("2026-11", partial=True, runs=1, km=8, longest_km=8, ext=None, int=None,
                     ext_unknown=None, int_unknown=None, easy_pace_sec=None, easy_hr=None)]
    lines = llm_prompt.format_history_block({"months": months, "plans": []})
    assert lines[0].startswith("История по месяцам (все пробежки, до 12 месяцев")
    assert lines[1:] == [
        "  - 2026-08: 31.5 км, 3 пробежки, самая длинная 16 км; лёгкие: темп 5:50/км, пульс 141; "
        "rTSS 180, TRIMP 240; тяжело или плохо: 1",
        "  - 2026-09: нет пробежек",
        "  - 2026-10: 31.5 км, 3 пробежки, самая длинная 16 км; лёгкие: темп 5:50/км; rTSS не меньше 180",
        "  - 2026-11 (текущий, неполный): 8 км, 1 пробежка, самая длинная 8 км",
    ]


def test_history_block_plan_lines():
    plans = [
        {"race_name": "Осенний полумарафон", "race_date": "2026-09-21", "target_time": "1:45:00",
         "weeks": 12, "compliance": {"planned_km": 520, "actual_km": 463.4, "pct": 89, "complete": True},
         "result": {"date": "2026-09-21", "dist_label": "HM", "seconds": 6372}},
        {"race_name": "", "race_date": "2026-05-01", "target_time": "", "weeks": 8,
         "compliance": {"planned_km": 200, "actual_km": 150, "pct": None, "complete": False},
         "result": None},
        {"race_name": "Без дат", "race_date": "2026-03-01", "target_time": "", "weeks": 4,
         "compliance": None, "result": {"date": "2026-03-01", "dist_label": "x", "seconds": None}},
    ]
    lines = llm_prompt.format_history_block({"months": [], "plans": plans})
    assert lines == [
        "История по месяцам: пробежек нет.",
        "Прошлые планы (последние, с гонкой в прошлом):",
        "  - «Осенний полумарафон», старт 2026-09-21, цель 1:45:00 — выполнение 89% "
        "(463.4 из 520 км); результат 1:46:12 на 21.1 км (2026-09-21)",
        "  - «без названия», старт 2026-05-01 — факт 150 км, план не меньше 200 км "
        "(точнее из текста плана не вывести); результата в «Стартах» нет",
        "  - «Без дат», старт 2026-03-01 — выполнение не посчитать: план без дат; "
        "результата в «Стартах» нет",
    ]


def test_failed_history_says_so_and_old_context_has_no_block(storage_module, fake_bucket):
    assert llm_prompt.format_history_block(None) == ["История: посчитать не удалось."]
    ctx = storage_module.build_llm_context(fake_bucket, SUB)
    assert "История по месяцам" in llm_prompt.format_context_for_llm(ctx)
    del ctx["history"]
    assert "История" not in llm_prompt.format_context_for_llm(ctx)


def test_instructions_say_history_is_background():
    for with_proposals in (True, False):
        text = llm_prompt.coach_chat_instructions(with_proposals)
        assert "История по месяцам и итоги прошлых планов — фон" in text
        assert "помесячная история за год" in text


# ── сборка контекста ──────────────────────────────────────────────────────────

def _today():
    return dt.datetime.utcnow().date()


def test_context_carries_history_months_and_past_plans(api, patched_api, fake_bucket,
                                                        storage_module):
    today = _today()
    race_day = today - dt.timedelta(days=40)
    start = race_day - dt.timedelta(days=race_day.weekday() + 7)          # понедельник за неделю до гонки
    past = patched_api.create_plan(fake_bucket, SUB, {"race_name": "Осенний 10К",
                                                      "race_date": race_day.isoformat(),
                                                      "target_time": "45:00",
                                                      "plan_start": start.isoformat()})
    patched_api.save_plan_weeks(fake_bucket, SUB, past["id"], [
        {"w": 1, "start": start.strftime("%d.%m.%Y"), "mon": "10 км"},
        {"w": 2, "start": (start + dt.timedelta(days=7)).strftime("%d.%m.%Y"), "mon": "8 км"}], "seed")
    patched_api.create_plan(fake_bucket, SUB, {"race_name": "Весна"})       # он и станет активным
    api(FakeRequest("POST", "/", json_body={"id": 1, "date": start.isoformat(), "dist": 10,
                                            "type": "easy", "plan_id": past["id"]}))
    api(FakeRequest("POST", "/races", {"name": "Осенний 10К", "date": race_day.isoformat(),
                                       "dist_label": "10km", "time": "44:30"}))

    ctx = storage_module.build_llm_context(fake_bucket, SUB)
    (plan,) = ctx["history"]["plans"]
    assert plan["race_name"] == "Осенний 10К" and plan["result"]["seconds"] == 2670
    assert plan["compliance"]["actual_km"] == 10 and plan["compliance"]["planned_km"] == 18
    assert ctx["history"]["months"][0]["runs"] == 1

    body, code, _ = api(FakeRequest("GET", "/advise/preview"))
    prompt = json.loads(body)["prompt"]
    assert prompt.index("Нагрузка (все пробежки") < prompt.index("История по месяцам") \
        < prompt.index("Последние 14 тренировок")
    assert "«Осенний 10К»" in prompt and "результат 44:30 на 10 км" in prompt


def test_races_reach_back_two_years_and_ten_at_most(api, fake_bucket, storage_module):
    today = _today()
    for i in range(12):
        day = today - dt.timedelta(days=30 * i + 1)
        api(FakeRequest("POST", "/races", {"id": i + 1, "name": f"Старт {i}", "date": day.isoformat(),
                                           "dist_label": "5km", "time": "22:00"}))
    old = today - dt.timedelta(days=800)
    api(FakeRequest("POST", "/races", {"id": 99, "name": "Давний", "date": old.isoformat(),
                                       "dist_label": "5km", "time": "21:00"}))
    races = storage_module.build_llm_context(fake_bucket, SUB)["last_races"]
    assert len(races) == 10 and races[0]["name"] == "Старт 0"
    assert "Давний" not in [r["name"] for r in races]


def test_history_failure_does_not_break_the_context(storage_module, fake_bucket, monkeypatch):
    def broken(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(storage_module, "monthly_summary", broken)
    ctx = storage_module.build_llm_context(fake_bucket, SUB)
    assert ctx["history"] is None
    assert "История: посчитать не удалось." in llm_prompt.format_context_for_llm(ctx)
