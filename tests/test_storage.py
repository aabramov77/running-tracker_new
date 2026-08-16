"""Tests for GCS-bound helpers in storage.py, against an in-memory fake bucket.

Multi-user: all data helpers are namespaced by `sub`. The fake (conftest
FakeBucket) implements only the blob/list_blobs/copy_blob surface we use.
"""
import time

import pytest

import domain

SUB = "user-abc"
SUB2 = "user-xyz"


# ── runs / races round-trips (per-user namespace) ─────────────────────────────

def test_runs_roundtrip(storage_module, fake_bucket):
    assert storage_module.read_runs(fake_bucket, SUB) == []
    runs = [{"id": 1, "date": "2026-05-01", "dist": 10.0, "deleted": False}]
    storage_module.write_runs(fake_bucket, SUB, runs)
    assert storage_module.read_runs(fake_bucket, SUB) == runs
    # isolation: another user sees nothing
    assert storage_module.read_runs(fake_bucket, SUB2) == []


def test_races_roundtrip(storage_module, fake_bucket):
    assert storage_module.read_races(fake_bucket, SUB) == []
    races = [{"id": 7, "name": "HM", "date": "2026-05-01", "dist_label": "HM",
              "time": "1:40:00", "deleted": False}]
    storage_module.write_races(fake_bucket, SUB, races)
    assert storage_module.read_races(fake_bucket, SUB) == races
    assert storage_module.read_races(fake_bucket, SUB2) == []


def test_runs_namespaced_paths(storage_module, fake_bucket):
    storage_module.write_runs(fake_bucket, SUB, [{"id": 1}])
    assert f"users/{SUB}/runs.json" in fake_bucket._store
    assert "runs.json" not in fake_bucket._store   # no global write


# ── legacy race profile (до #25; сейчас — только источник для миграции) ───────

def test_legacy_race_profile_defaults_and_roundtrip(storage_module, fake_bucket):
    # no profile yet → defaults (all empty)
    p = storage_module.read_legacy_race_profile(fake_bucket, SUB)
    assert p == {"race_name": "", "race_date": "", "target_time": "", "plan_start": ""}

    storage_module.write_legacy_race_profile(fake_bucket, SUB, {
        "race_name": "Берлин", "race_date": "2026-09-27",
        "target_time": "3:30", "plan_start": "2026-06-01", "junk": "ignored"})
    p2 = storage_module.read_legacy_race_profile(fake_bucket, SUB)
    assert p2["race_name"] == "Берлин" and p2["target_time"] == "3:30"
    assert "junk" not in p2                       # only known fields kept
    # isolation
    assert storage_module.read_legacy_race_profile(fake_bucket, SUB2)["race_name"] == ""


# ── athlete profile (#32): версии, валидация, вычисляемые, рекорды ────────────

FILLED_PROFILE = {
    "full_name": "Иванов Иван", "birth_date": "1979-03-01", "sex": "m",
    "height_cm": "182", "weight_kg": "74.4", "hr_max": "178",
    "hr_threshold": "162", "hr_rest": "48", "vo2max": "52",
    "years_running": "6", "weekly_km_typical": "45", "sessions_per_week": "4",
    "available_days": ["sat", "mon", "wed", "sun"], "long_run_day": "sun",
    "injuries": "  правое ахилловое  ", "notes": "цель — разменять 1:40",
}


def test_athlete_profile_empty_when_never_saved(storage_module, fake_bucket):
    profile, version, updated_at = storage_module.read_athlete_profile(fake_bucket, SUB)
    assert version == 0 and updated_at is None
    assert profile["full_name"] == "" and profile["hr_max"] is None
    assert profile["available_days"] == []


def test_athlete_profile_cleaning(storage_module):
    profile, errors = storage_module.clean_athlete_profile(FILLED_PROFILE)
    assert errors == {}
    assert profile["height_cm"] == 182 and isinstance(profile["height_cm"], int)
    assert profile["weight_kg"] == 74.4
    assert profile["injuries"] == "правое ахилловое"          # обрезаны пробелы
    assert profile["available_days"] == ["mon", "wed", "sat", "sun"]  # порядок Пн→Вс


@pytest.mark.parametrize("field,value", [
    ("height_cm", 300), ("weight_kg", 10), ("hr_max", 250),
    ("hr_rest", 5), ("vo2max", 5), ("sessions_per_week", 20),
    ("height_cm", "высокий"),
])
def test_athlete_profile_rejects_out_of_range(storage_module, field, value):
    _, errors = storage_module.clean_athlete_profile({field: value})
    assert field in errors


def test_athlete_profile_rejects_inconsistent_hr(storage_module):
    _, errors = storage_module.clean_athlete_profile({"hr_max": 170, "hr_threshold": 175})
    assert "hr_threshold" in errors
    _, errors2 = storage_module.clean_athlete_profile({"hr_max": 170, "hr_rest": 180})
    assert "hr_rest" in errors2


@pytest.mark.parametrize("value,field", [
    ("2030-01-01", "birth_date"),      # в будущем
    ("01.03.1979", "birth_date"),      # не ISO
])
def test_athlete_profile_rejects_bad_birth_date(storage_module, value, field):
    _, errors = storage_module.clean_athlete_profile({"birth_date": value})
    assert field in errors


def test_athlete_profile_rejects_long_run_on_unavailable_day(storage_module):
    """Иначе промпт противоречит сам себе: длительная в день, когда бегать нельзя."""
    _, errors = storage_module.clean_athlete_profile(
        {"available_days": ["mon", "wed", "sat"], "long_run_day": "sun"})
    assert "long_run_day" in errors
    # согласованное расписание проходит
    ok, no_errors = storage_module.clean_athlete_profile(
        {"available_days": ["mon", "wed", "sun"], "long_run_day": "sun"})
    assert no_errors == {} and ok["long_run_day"] == "sun"
    # пустой список доступных дней = ограничений нет
    _, no_errors2 = storage_module.clean_athlete_profile({"long_run_day": "sun"})
    assert no_errors2 == {}


def test_athlete_profile_rejects_unknown_days(storage_module):
    _, errors = storage_module.clean_athlete_profile({"available_days": ["mon", "funday"]})
    assert "available_days" in errors
    _, errors2 = storage_module.clean_athlete_profile({"long_run_day": "funday"})
    assert "long_run_day" in errors2
    _, errors3 = storage_module.clean_athlete_profile({"sex": "x"})
    assert "sex" in errors3


def test_athlete_profile_versions_are_immutable(storage_module, fake_bucket):
    profile, _ = storage_module.clean_athlete_profile(FILLED_PROFILE)
    v1 = storage_module.write_athlete_version(fake_bucket, SUB, profile, "первое заполнение")
    assert v1["version"] == 1 and v1["supersedes_version"] is None

    heavier = {**profile, "weight_kg": 72.0}
    v2 = storage_module.write_athlete_version(fake_bucket, SUB, heavier, "взвесился")
    assert v2["version"] == 2 and v2["supersedes_version"] == 1

    # v1 не тронут — политика append-only
    import json
    stored_v1 = json.loads(fake_bucket.blob(f"users/{SUB}/athlete/v1/profile.json").download_as_text())
    assert stored_v1["profile"]["weight_kg"] == 74.4

    current, version, updated_at = storage_module.read_athlete_profile(fake_bucket, SUB)
    assert version == 2 and current["weight_kg"] == 72.0 and updated_at

    # изоляция между пользователями
    other, other_version, _ = storage_module.read_athlete_profile(fake_bucket, SUB2)
    assert other_version == 0 and other["full_name"] == ""


def test_athlete_history_tracks_measurements(storage_module, fake_bucket):
    profile, _ = storage_module.clean_athlete_profile(FILLED_PROFILE)
    storage_module.write_athlete_version(fake_bucket, SUB, profile, "первое заполнение")
    storage_module.write_athlete_version(fake_bucket, SUB, {**profile, "weight_kg": 72.0}, "взвесился")

    history = storage_module.read_athlete_history(fake_bucket, SUB)
    assert [h["version"] for h in history] == [1, 2]
    assert [h["weight_kg"] for h in history] == [74.4, 72.0]
    assert history[1]["change_reason"] == "взвесился"
    assert storage_module.read_athlete_history(fake_bucket, SUB2) == []


def test_athlete_history_is_bounded_to_latest_versions(storage_module, fake_bucket):
    """Каждая версия — отдельный объект; полный обход упёрся бы в таймаут."""
    profile = storage_module.empty_athlete_profile()
    for i in range(1, 8):
        storage_module.write_athlete_version(fake_bucket, SUB, {**profile, "weight_kg": 70 + i}, f"v{i}")

    limited = storage_module.read_athlete_history(fake_bucket, SUB, limit=3)
    assert [h["version"] for h in limited] == [5, 6, 7]      # последние, не первые
    assert [h["weight_kg"] for h in limited] == [75, 76, 77]
    # окно больше числа версий — отдаём всё, без пустых дыр
    assert len(storage_module.read_athlete_history(fake_bucket, SUB, limit=100)) == 7


def test_athlete_derived_values(storage_module):
    from datetime import date
    profile, _ = storage_module.clean_athlete_profile(FILLED_PROFILE)
    derived = storage_module.compute_athlete_derived(profile, today=date(2026, 8, 16))
    assert derived["age"] == 47
    assert derived["bmi"] == 22.5
    assert derived["hr_max_estimated"] is None        # пульс измерен — оценка не нужна
    assert derived["hr_max_effective"] == 178
    assert len(derived["hr_zones"]) == 5
    assert derived["hr_zones"][0]["from"] == 89       # 50% от 178
    assert derived["hr_zones"][-1]["to"] == 178


def test_athlete_derived_estimates_hr_max_without_measurement(storage_module):
    from datetime import date
    profile = {**storage_module.empty_athlete_profile(), "birth_date": "1986-08-16"}
    derived = storage_module.compute_athlete_derived(profile, today=date(2026, 8, 16))
    assert derived["age"] == 40
    assert derived["hr_max_estimated"] == 180         # Танака: 208 − 0.7×40
    assert derived["hr_max_effective"] == 180
    assert derived["bmi"] is None                     # роста и веса нет


def test_athlete_derived_empty_profile_is_all_none(storage_module):
    derived = storage_module.compute_athlete_derived(storage_module.empty_athlete_profile())
    assert derived["age"] is None and derived["bmi"] is None
    assert derived["hr_max_effective"] is None and derived["hr_zones"] == []


@pytest.mark.parametrize("text,expected", [
    ("44:30", 2670), ("1:47:20", 6440), ("0:59", 59),
    ("", None), ("не время", None), ("44", None), ("1:2:3:4", None),
])
def test_parse_time_to_sec(text, expected):
    assert domain.parse_time_to_sec(text) == expected


def test_personal_bests_picks_fastest_and_skips_deleted():
    races = [
        {"dist_label": "10km", "time": "46:10", "date": "2026-05-30"},
        {"dist_label": "10km", "time": "44:30", "date": "2026-07-04"},   # лучший
        {"dist_label": "10km", "time": "41:00", "date": "2026-07-20", "deleted": True},
        {"dist_label": "HM", "time": "1:47:20", "date": "2025-09-15"},
        {"dist_label": "HM", "time": "битое", "date": "2025-10-01"},     # без времени
        {"dist_label": "чего-то", "time": "30:00", "date": "2026-01-01"},
    ]
    bests = domain.personal_bests(races)
    assert [b["dist_label"] for b in bests] == ["10km", "HM"]   # по возрастанию км
    assert bests[0]["time"] == "44:30" and bests[0]["date"] == "2026-07-04"
    assert domain.personal_bests([]) == []


# ── plan versioning (per-user, immutable versions) ────────────────────────────

PID = "plan-1"


def test_plan_versioning(storage_module, fake_bucket):
    w1 = [{"w": 1, "type": "dev", "sun": "10км"}]
    storage_module.write_plan_version(fake_bucket, SUB, PID, 1, w1, "seed")
    man = storage_module.read_plan_manifest(fake_bucket, SUB, PID)
    assert man["current_version"] == 1
    v1 = storage_module.read_plan_version(fake_bucket, man["gcs_object_path"])
    assert v1["weeks"] == w1

    w2 = [{"w": 1, "type": "dev", "sun": "12км"}]
    storage_module.write_plan_version(fake_bucket, SUB, PID, 2, w2, "edit")
    man2 = storage_module.read_plan_manifest(fake_bucket, SUB, PID)
    assert man2["current_version"] == 2
    # v1 still readable — immutable history preserved
    assert storage_module.read_plan_version(
        fake_bucket, f"users/{SUB}/plans/{PID}/v1/plan.json")["weeks"] == w1
    assert storage_module.read_plan_weeks(fake_bucket, SUB, PID) == w2
    # other user / other plan has nothing
    assert storage_module.read_plan_manifest(fake_bucket, SUB2, PID) is None
    assert storage_module.read_plan_weeks(fake_bucket, SUB, "other-plan") == []


def test_save_plan_weeks_increments_version(storage_module, fake_bucket):
    storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1}], "first")
    r2 = storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1}, {"w": 2}], "second")
    assert r2["version"] == 2
    assert len(storage_module.read_plan_weeks(fake_bucket, SUB, PID)) == 2


# ── plans registry (#25) ──────────────────────────────────────────────────────

def test_create_and_switch_plans(storage_module, fake_bucket):
    a = storage_module.create_plan(fake_bucket, SUB, {"race_name": "HM", "race_date": "2026-08-09"})
    assert storage_module.get_active_plan(fake_bucket, SUB)["id"] == a["id"]   # first is active

    b = storage_module.create_plan(fake_bucket, SUB, {"race_name": "Marathon"})
    assert storage_module.get_active_plan(fake_bucket, SUB)["id"] == b["id"]   # new becomes active

    storage_module.set_active_plan(fake_bucket, SUB, a["id"])
    assert storage_module.get_active_plan(fake_bucket, SUB)["race_name"] == "HM"
    # unknown id → None, active unchanged
    assert storage_module.set_active_plan(fake_bucket, SUB, "nope") is None
    assert storage_module.get_active_plan(fake_bucket, SUB)["id"] == a["id"]
    # isolation: other user has no plans
    assert storage_module.read_plans_index(fake_bucket, SUB2)["plans"] == []


def test_update_plan_meta(storage_module, fake_bucket):
    p = storage_module.create_plan(fake_bucket, SUB, {"race_name": "HM"})
    upd = storage_module.update_plan_meta(fake_bucket, SUB, p["id"],
                                       {"race_name": "Берлин", "target_time": "3:30"})
    assert upd["race_name"] == "Берлин" and upd["target_time"] == "3:30"
    assert storage_module.update_plan_meta(fake_bucket, SUB, "ghost", {}) is None


def test_archive_plan_switches_active(storage_module, fake_bucket):
    a = storage_module.create_plan(fake_bucket, SUB, {"race_name": "A"})
    b = storage_module.create_plan(fake_bucket, SUB, {"race_name": "B"})   # active
    storage_module.archive_plan(fake_bucket, SUB, b["id"])
    index = storage_module.read_plans_index(fake_bucket, SUB)
    assert index["active_plan_id"] == a["id"]            # fell back to remaining plan
    assert len(storage_module.active_plans(index)) == 1     # archived hidden
    assert len(index["plans"]) == 2                      # but not deleted (soft)
    # archived plan can't be activated
    assert storage_module.set_active_plan(fake_bucket, SUB, b["id"]) is None


# ── LLM config versioning (GLOBAL — shared key, admin-managed) ────────────────

def test_llm_config_versioning(storage_module, fake_bucket):
    assert storage_module.read_llm_config_full(fake_bucket) is None
    storage_module.write_llm_config_version(fake_bucket, "deepseek", "deepseek-chat",
                                         "sk-secret-1234567890")
    cfg = storage_module.read_llm_config_full(fake_bucket)
    assert cfg["provider"] == "deepseek"
    assert cfg["api_key"] == "sk-secret-1234567890"
    assert cfg["version"] == 1
    # global path, not per-user
    assert "config/llm/v1/config.json" in fake_bucket._store

    storage_module.write_llm_config_version(fake_bucket, "anthropic", "claude-x", "sk-ant-999")
    assert storage_module.read_llm_config_full(fake_bucket)["version"] == 2


def test_llm_context_uses_only_active_plan(storage_module, fake_bucket):
    """#25: контекст для LLM берёт недели активного плана и только его пробежки."""
    a = storage_module.create_plan(fake_bucket, SUB, {"race_name": "A", "plan_start": "2026-05-10"})
    b = storage_module.create_plan(fake_bucket, SUB, {"race_name": "B", "plan_start": "2026-06-01"})
    storage_module.save_plan_weeks(fake_bucket, SUB, a["id"], [{"w": 1, "mon": "A-неделя"}])
    storage_module.save_plan_weeks(fake_bucket, SUB, b["id"], [{"w": 1, "mon": "B-неделя"}])
    storage_module.write_runs(fake_bucket, SUB, [
        {"id": 1, "date": "2026-05-20", "dist": 10, "plan_id": a["id"]},
        {"id": 2, "date": "2026-06-10", "dist": 5, "plan_id": b["id"]},
    ])

    # активен B (создан последним)
    ctx = storage_module.build_llm_context(fake_bucket, SUB)
    assert ctx["plan_id"] == b["id"]
    assert [r["id"] for r in ctx["last_runs"]] == [2]
    assert ctx["race"]["race_name"] == "B"
    assert ctx["weeks_total"] == 1

    # переключаемся на A → другой набор пробежек и недель
    storage_module.set_active_plan(fake_bucket, SUB, a["id"])
    ctx_a = storage_module.build_llm_context(fake_bucket, SUB)
    assert [r["id"] for r in ctx_a["last_runs"]] == [1]
    assert ctx_a["race"]["race_name"] == "A"
    text = storage_module.format_context_for_llm(ctx_a)
    assert "A" in text and "A-неделя" in text


# ── advice round-trip + daily usage ───────────────────────────────────────────

def test_advice_roundtrip(storage_module, fake_bucket):
    assert storage_module.read_latest_advice(fake_bucket, SUB) is None
    ctx = {"last_runs": [{"id": 11}, {"id": 12}], "plan_version": 3}
    rec = {"assessment": "ok", "adjustments": [], "warnings": []}
    storage_module.write_advice_version(fake_bucket, SUB, rec, ctx, "deepseek", "deepseek-chat",
                                     100, 50, llm_config_version=1)
    latest = storage_module.read_latest_advice(fake_bucket, SUB)
    assert latest["recommendation"] == rec
    assert latest["based_on_runs"] == [11, 12]
    assert storage_module.read_latest_advice(fake_bucket, SUB2) is None


def test_advice_usage_counter(storage_module, fake_bucket):
    assert storage_module.read_advice_usage(fake_bucket, SUB)["count"] == 0
    storage_module.increment_advice_usage(fake_bucket, SUB)
    storage_module.increment_advice_usage(fake_bucket, SUB)
    assert storage_module.read_advice_usage(fake_bucket, SUB)["count"] == 2
    # separate user unaffected
    assert storage_module.read_advice_usage(fake_bucket, SUB2)["count"] == 0


# ── tmp cleanup (per-user, age-based) ─────────────────────────────────────────

def test_cleanup_old_tmp(storage_module, fake_bucket):
    now = int(time.time())
    old, fresh = now - 100_000, now
    fake_bucket.blob(f"tmp/{SUB}/{old}-aaaa/activity.fit").upload_from_string(b"x")
    fake_bucket.blob(f"tmp/{SUB}/{old}-aaaa/details.json").upload_from_string("{}")
    fake_bucket.blob(f"tmp/{SUB}/{fresh}-bbbb/details.json").upload_from_string("{}")
    # another user's old tmp must NOT be touched
    fake_bucket.blob(f"tmp/{SUB2}/{old}-cccc/details.json").upload_from_string("{}")

    deleted = storage_module.cleanup_old_tmp(fake_bucket, SUB, max_age_hours=24)
    assert deleted == 2
    names = [b.name for b in fake_bucket.list_blobs(prefix="tmp/")]
    assert all("aaaa" not in n for n in names)
    assert any("bbbb" in n for n in names)
    assert any("cccc" in n for n in names)   # SUB2 untouched


# ── FIT tmp → permanent promotion (per-user) ──────────────────────────────────

def test_parse_fit_tmp_then_attach(storage_module, fake_bucket):
    parsed = {
        "date": "2026-05-01",
        "summary": {"dist_km": 3.0, "avg_cadence": 176, "max_hr": 170,
                    "total_ascent_m": 24, "calories": 210},
        "laps": [{"lap": 1, "pace": "5:00"}],
        "samples": {"t_offset_sec": [0, 5], "hr": [120, 130],
                    "pace_sec_per_km": [300, 300], "altitude_m": [60, 61]},
    }
    token = storage_module.write_parsed_fit_to_tmp(fake_bucket, SUB, b"FITBYTES", parsed)
    assert fake_bucket.blob(f"tmp/{SUB}/{token}/activity.fit").exists()

    run = {"id": 123, "date": "2026-05-01"}
    storage_module.attach_fit_details_to_run(fake_bucket, SUB, run, token)

    assert run["details_available"] is True
    assert run["avg_cadence"] == 176 and run["calories"] == 210
    assert fake_bucket.blob(f"users/{SUB}/runs/123/v1/activity.fit").exists()
    assert fake_bucket.blob(f"users/{SUB}/runs/123/v1/details.json").exists()
    assert fake_bucket.blob(f"users/{SUB}/runs/123/manifest.json").exists()
    # tmp cleaned
    assert not fake_bucket.blob(f"tmp/{SUB}/{token}/activity.fit").exists()
    # read back via per-user manifest
    details = storage_module.read_run_details(fake_bucket, SUB, 123)
    assert details["summary"]["avg_cadence"] == 176


def test_attach_fails_on_missing_token(storage_module, fake_bucket):
    with pytest.raises(ValueError):
        storage_module.attach_fit_details_to_run(fake_bucket, SUB, {"id": 1}, "nonexistent")


def test_read_run_details_lazy_legacy_fallback(storage_module, fake_bucket):
    """Admin's migrated run: details live at the legacy global path; first read
    lazily copies them into the user namespace."""
    fake_bucket.blob("runs/999/v1/activity.fit").upload_from_string(b"FIT")
    fake_bucket.blob("runs/999/v1/details.json").upload_from_string(
        '{"summary": {"avg_cadence": 165}, "laps": [], "samples": {}}')
    fake_bucket.blob("runs/999/manifest.json").upload_from_string(
        '{"current_version": 1, "gcs_object_path": "runs/999/v1/details.json"}')

    details = storage_module.read_run_details(fake_bucket, SUB, 999)
    assert details["summary"]["avg_cadence"] == 165
    # now materialized in the user namespace
    assert fake_bucket.blob(f"users/{SUB}/runs/999/v1/details.json").exists()
    assert fake_bucket.blob(f"users/{SUB}/runs/999/manifest.json").exists()
