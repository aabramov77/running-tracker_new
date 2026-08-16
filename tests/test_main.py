"""Unit tests for pure helper functions in main.py and llm_prompt.py.

These lock in behavior we previously verified manually (cadence fix, pace/time
formatting, LLM JSON extraction, key masking, HR-drift, FIT parsing).
"""
from datetime import date
from pathlib import Path

import pytest

import llm_prompt
from conftest import FIT_FIXTURE, SYNTHETIC_FIT


# ── Cadence (_spm) ────────────────────────────────────────────────────────────

def test_spm_none_when_no_cadence(main_module):
    assert main_module._spm({}) is None


def test_spm_doubles_base(main_module):
    # FIT stores cadence per-leg; we return steps/min = (base + frac) * 2
    assert main_module._spm({"avg_cadence": 90}) == 180


def test_spm_includes_fractional(main_module):
    assert main_module._spm({"avg_running_cadence": 88, "avg_fractional_cadence": 0.5}) == 177


# ── Pace / duration formatting ────────────────────────────────────────────────

@pytest.mark.parametrize("sec,expected", [
    (None, None), (0, None), (-5, None),
    (287, "4:47"), (65, "1:05"), (3600, "60:00"),
])
def test_fmt_pace(main_module, sec, expected):
    assert main_module._fmt_pace(sec) == expected


@pytest.mark.parametrize("sec,expected", [
    (None, None), (0, None),
    (65, "1:05"), (600, "10:00"), (3665, "1:01:05"), (7200, "2:00:00"),
])
def test_fmt_duration(main_module, sec, expected):
    assert main_module._fmt_duration(sec) == expected


# ── LLM JSON extraction ───────────────────────────────────────────────────────

@pytest.mark.parametrize("text,expected", [
    ('{"a":1}', {"a": 1}),
    ('   {"a":1}   ', {"a": 1}),
    ('Hello: {"assessment":"ok","adjustments":[],"warnings":[]}',
     {"assessment": "ok", "adjustments": [], "warnings": []}),
    ('prefix\n{"x":2}\nsuffix', {"x": 2}),
])
def test_parse_llm_json_ok(main_module, text, expected):
    assert main_module.parse_llm_json(text) == expected


def test_parse_llm_json_raises_without_json(main_module):
    with pytest.raises(Exception):
        main_module.parse_llm_json("no json here at all")


# ── Key masking ───────────────────────────────────────────────────────────────

@pytest.mark.parametrize("key,expected", [
    ("", ""),
    ("a", "***"),
    ("short", "***"),
    ("sk-ant-test-1234567890", "sk-ant***7890"),
])
def test_mask_key(main_module, key, expected):
    assert main_module.mask_key(key) == expected


# ── HR-drift / lap paces ──────────────────────────────────────────────────────

def test_hr_drift_none_when_empty(main_module):
    assert main_module.compute_hr_drift({}) is None


def test_hr_drift_none_when_too_few_laps(main_module):
    assert main_module.compute_hr_drift({"laps": [{"avg_hr": 150}, {"avg_hr": 155}]}) is None


def test_hr_drift_positive_when_hr_rises(main_module):
    details = {"samples": {"hr": [120, 120, 140, 140]}}
    drift = main_module.compute_hr_drift(details)
    assert drift is not None and drift > 0


def test_lap_paces_str_none_when_empty(main_module):
    assert main_module.lap_paces_str({}) is None


def test_lap_paces_str_truncates(main_module):
    laps = [{"pace": "5:00"} for _ in range(20)]
    s = main_module.lap_paces_str({"laps": laps}, limit=5)
    assert "(+15)" in s


# ── format_context_for_llm ────────────────────────────────────────────────────

def test_format_context_smoke(main_module):
    ctx = {
        "last_runs": [
            {"date": "2026-05-15", "type": "long", "dist": 14.2, "time": "1:18",
             "pace": "5:30", "hr": 145, "feel": "good"},
        ],
        "last_races": [],
        "current_week": {"w": 2, "type": "dev", "accent": "Развитие",
                         "mon": "8км", "tue": "6км", "wed": "6x1км",
                         "thu": "отдых-бег", "fri": "10км", "sat": "4x2км",
                         "sun": "14км"},
        "next_week": None,
        "week_idx": 1,
        "plan_version": 1,
        "heuristics": {"avg_pace_min_per_km": 5.5, "hard_or_bad_count": 0,
                       "total_km_last_14": 14.2},
    }
    text = llm_prompt.format_context_for_llm(ctx)
    assert "2026-05-15" in text
    assert "14.2" in text
    # 7-day calendar (#23): Tue/Thu now included in the plan context
    assert "вт=6км" in text
    assert "чт=отдых-бег" in text


def test_format_context_includes_profile_block(main_module):
    """#32: заполненный профиль печатается первым блоком промпта."""
    ctx = {
        "profile": {"sex": "m", "birth_date": "1979-03-01", "height_cm": 182,
                    "weight_kg": 74.0, "hr_max": 178, "hr_threshold": 162,
                    "hr_rest": 48, "years_running": 6, "weekly_km_typical": 45,
                    "sessions_per_week": 4, "available_days": ["mon", "wed", "sat", "sun"],
                    "long_run_day": "sun", "injuries": "правое ахилловое",
                    "notes": "во вторник тренировок не бывает"},
        "profile_derived": {"age": 47, "bmi": 22.3, "hr_max_estimated": None},
        "personal_bests": [{"km": 10, "time": "44:30", "date": "2026-07-04"}],
        "last_runs": [], "last_races": [], "current_week": None, "next_week": None,
        "week_idx": 0, "plan_version": 1,
        "heuristics": {"avg_pace_min_per_km": None, "hard_or_bad_count": 0,
                       "total_km_last_14": 0},
    }
    text = llm_prompt.format_context_for_llm(ctx)
    assert text.startswith("Профиль: М, 47 лет, 182 см, 74 кг (ИМТ 22.3)")
    assert "макс 178" in text and "ПАНО 162" in text
    assert "доступные дни — пн, ср, сб, вс" in text and "длительная — вс" in text
    assert "10 км 44:30 (2026-07-04)" in text
    assert "Ограничения: правое ахилловое" in text
    assert "От спортсмена: во вторник" in text
    # профиль идёт до цели и тренировок
    assert text.index("Профиль:") < text.index("Цель:")


def test_format_context_without_profile_has_no_block(main_module):
    """Пустой профиль не должен добавлять в промпт пустых строк-заглушек."""
    ctx = {
        "profile": main_module.empty_athlete_profile(),
        "profile_derived": main_module.compute_athlete_derived(main_module.empty_athlete_profile()),
        "personal_bests": [],
        "last_runs": [], "last_races": [], "current_week": None, "next_week": None,
        "week_idx": 0, "plan_version": None,
        "heuristics": {"avg_pace_min_per_km": None, "hard_or_bad_count": 0,
                       "total_km_last_14": 0},
    }
    text = llm_prompt.format_context_for_llm(ctx)
    assert text.startswith("Цель:")
    assert "Профиль:" not in text and "Ограничения:" not in text


@pytest.mark.parametrize("n,expected", [
    (1, "тренировка"), (2, "тренировки"), (4, "тренировки"),
    (5, "тренировок"), (11, "тренировок"), (14, "тренировок"), (21, "тренировка"),
])
def test_plural_ru(n, expected):
    assert llm_prompt.plural_ru(n, "тренировка", "тренировки", "тренировок") == expected


def test_profile_block_keeps_zero_values(main_module):
    """Ноль — валидный ответ новичка, а не «не заполнено»."""
    profile = {**main_module.empty_athlete_profile(),
               "years_running": 0, "weekly_km_typical": 0, "sessions_per_week": 0}
    lines = "\n".join(llm_prompt.format_profile_block(profile, {}, []))
    assert "стаж 0 лет" in lines
    assert "обычный объём 0 км/нед" in lines
    assert "0 тренировок в неделю" in lines
    # незаполненные поля по-прежнему пропускаются
    assert "Опыт:" not in "\n".join(
        llm_prompt.format_profile_block(main_module.empty_athlete_profile(), {}, []))


def test_profile_block_marks_estimated_hr_max(main_module):
    """Оценка HRmax помечается — модель не должна принять её за измерение."""
    profile = {**main_module.empty_athlete_profile(), "birth_date": "1986-01-01"}
    derived = main_module.compute_athlete_derived(profile, today=date(2026, 8, 16))
    lines = "\n".join(llm_prompt.format_profile_block(profile, derived, []))
    assert "оценка по возрасту" in lines
    # с измеренным пульсом оценки в промпте нет
    measured = {**profile, "hr_max": 190}
    lines2 = "\n".join(llm_prompt.format_profile_block(
        measured, main_module.compute_athlete_derived(measured, today=date(2026, 8, 16)), []))
    assert "оценка" not in lines2 and "макс 190" in lines2


def test_week_days_str_skips_empty():
    # 5-day legacy week (no tue/thu) renders without them, no crash
    legacy = {"mon": "8", "wed": "6x1", "fri": "10", "sat": "4x2", "sun": "14"}
    s = llm_prompt._week_days_str(legacy)
    assert "пн=8" in s and "вс=14" in s
    assert "вт=" not in s and "чт=" not in s
    # empty week
    assert llm_prompt._week_days_str({}) == "(пусто)"


# ── FIT parsing ───────────────────────────────────────────────────────────────

def test_parse_synthetic_fit_summary_and_cadence(main_module):
    """Runs everywhere (incl. CI) — uses the committed synthetic GPS-free fixture.
    Guards the cadence regression: per-leg cadence must be doubled to steps/min."""
    assert SYNTHETIC_FIT.exists(), (
        "synthetic_activity.fit missing — regenerate via "
        "python tests/fixtures/make_synthetic_fit.py")
    parsed = main_module.parse_fit_file(SYNTHETIC_FIT.read_bytes())
    summary = parsed["summary"]
    assert summary["dist_km"] > 0
    # Cadence fix: running cadence is ~150-190 spm, not the per-leg ~88.
    assert summary["avg_cadence"] is not None and summary["avg_cadence"] > 150
    assert len(parsed["laps"]) > 1
    assert len(parsed["samples"]["t_offset_sec"]) > 0
    # Lap paces formatted as m:ss
    assert all(":" in lap["pace"] for lap in parsed["laps"] if lap.get("pace"))


@pytest.mark.skipif(not FIT_FIXTURE.exists(),
                    reason="no real FIT fixture (local-only, gitignored)")
def test_parse_real_fit_if_present(main_module):
    """Extra confidence on a real Garmin export when available locally."""
    parsed = main_module.parse_fit_file(FIT_FIXTURE.read_bytes())
    summary = parsed["summary"]
    assert summary["dist_km"] > 0
    assert summary["avg_cadence"] is not None and summary["avg_cadence"] > 150
    assert len(parsed["laps"]) > 1
