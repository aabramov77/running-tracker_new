"""Tests for GCS-bound helpers in storage.py, against an in-memory fake bucket.

Multi-user: all data helpers are namespaced by `sub`. The fake (conftest
FakeBucket) implements only the blob/list_blobs/copy_blob surface we use.
"""
import json
import time
from datetime import date
from pathlib import Path

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


# ── ручные пульсовые зоны (#56) ───────────────────────────────────────────────

MANUAL_ZONES = [100, 125, 140, 155, 168]      # нижние границы Z1…Z5


def test_manual_zone_starts_are_cleaned(storage_module):
    """Форма шлёт строки; в профиле — целые."""
    profile, errors = storage_module.clean_athlete_profile(
        {"hr_max": 178, "hr_zones_custom": ["100", 125.4, 140, 155, "168"]})
    assert errors == {} and profile["hr_zones_custom"] == MANUAL_ZONES


@pytest.mark.parametrize("value", [None, "", []])
def test_no_manual_zones_means_calculated_ones(storage_module, value):
    profile, errors = storage_module.clean_athlete_profile({"hr_zones_custom": value})
    assert errors == {} and profile["hr_zones_custom"] is None
    assert storage_module.empty_athlete_profile()["hr_zones_custom"] is None


@pytest.mark.parametrize("value", [
    [100, 125, 140, 155],                    # не все зоны
    [100, 125, 140, 155, 168, 180],          # лишняя
    [100, 125, 125, 155, 168],               # зона нулевой ширины
    [100, 140, 125, 155, 168],               # не по возрастанию
    [20, 125, 140, 155, 168],                # ниже допустимого
    [100, 125, 140, 155, 240],               # выше допустимого
    [100, 125, "", 155, 168],                # незаполненное поле формы
    [100, 125, None, 155, 168], [100, 125, [140], 155, 168], [100, 125, True, 155, 168],
    [100, 125, float("nan"), 155, 168],
    "100,125,140,155,168", 150, {"z1": 100}, True,
])
def test_bad_manual_zones_are_rejected(storage_module, value):
    profile, errors = storage_module.clean_athlete_profile({"hr_zones_custom": value})
    assert list(errors) == ["hr_zones_custom"] and profile["hr_zones_custom"] is None


def test_manual_zone_five_must_start_below_the_measured_max(storage_module):
    _, errors = storage_module.clean_athlete_profile({"hr_max": 168, "hr_zones_custom": MANUAL_ZONES})
    assert list(errors) == ["hr_zones_custom"]
    # оценка по возрасту — не измерение: зоны выше неё допустимы
    _, no_errors = storage_module.clean_athlete_profile(
        {"birth_date": "1946-08-16", "hr_zones_custom": MANUAL_ZONES})
    assert no_errors == {}


def test_manual_zones_replace_the_calculated_ones(storage_module):
    from datetime import date
    today = date(2026, 8, 16)
    profile, _ = storage_module.clean_athlete_profile({**FILLED_PROFILE, "hr_zones_custom": MANUAL_ZONES})
    derived = storage_module.compute_athlete_derived(profile, today=today)
    assert derived["hr_zones_source"] == "manual"
    assert [(z["from"], z["to"]) for z in derived["hr_zones"]] == [
        (100, 125), (125, 140), (140, 155), (155, 168), (168, 178)]     # верх Z5 — HRmax
    assert [z["name"] for z in derived["hr_zones"]] == [name for name, _, _ in domain.HR_ZONE_BOUNDS]
    # расчётные остаются под рукой — с них начинается ручная правка
    assert (derived["hr_zones_auto"][0]["from"], derived["hr_zones_auto"][-1]["to"]) == (89, 178)

    auto = storage_module.compute_athlete_derived({**profile, "hr_zones_custom": None}, today=today)
    assert auto["hr_zones_source"] == "auto" and auto["hr_zones"] == auto["hr_zones_auto"]


def test_manual_zone_five_has_no_top_without_a_higher_max(storage_module):
    from datetime import date
    bare = {**storage_module.empty_athlete_profile(), "hr_zones_custom": MANUAL_ZONES}
    derived = storage_module.compute_athlete_derived(bare)
    assert derived["hr_zones"][-1] == {"name": "Z5 максимальная", "from": 168, "to": None}
    assert derived["hr_zones_auto"] == []
    # оценка по возрасту ниже начала Z5 — верхом зоны она быть не может
    aged = storage_module.compute_athlete_derived({**bare, "birth_date": "1946-08-16"},
                                                  today=date(2026, 8, 16))
    assert aged["hr_max_estimated"] == 152 and aged["hr_zones"][-1]["to"] is None


@pytest.mark.parametrize("stored", [[100, 125], "100", [100, 125, "140", 155, 168], {"z1": 100}])
def test_malformed_stored_zones_fall_back_to_calculated(storage_module, stored):
    """Чтение терпимо к тому, что уже лежит в бакете."""
    profile = {**storage_module.empty_athlete_profile(), "hr_max": 178, "hr_zones_custom": stored}
    derived = storage_module.compute_athlete_derived(profile)
    assert derived["hr_zones_source"] == "auto" and derived["hr_zones"][0]["from"] == 89


def test_manual_zones_are_versioned_with_the_profile(storage_module, fake_bucket):
    import json
    profile, _ = storage_module.clean_athlete_profile(FILLED_PROFILE)
    # версия, записанная до #56, поля не содержит вовсе
    before = {k: v for k, v in profile.items() if k != "hr_zones_custom"}
    storage_module.write_athlete_version(fake_bucket, SUB, before, "до ручных зон")
    assert storage_module.read_athlete_profile(fake_bucket, SUB)[0]["hr_zones_custom"] is None

    storage_module.write_athlete_version(
        fake_bucket, SUB, {**profile, "hr_zones_custom": MANUAL_ZONES}, "зоны по тесту")
    assert storage_module.read_athlete_profile(fake_bucket, SUB)[0]["hr_zones_custom"] == MANUAL_ZONES

    storage_module.write_athlete_version(fake_bucket, SUB, profile, "вернул авторасчёт")
    current, version, _ = storage_module.read_athlete_profile(fake_bucket, SUB)
    assert version == 3 and current["hr_zones_custom"] is None
    # сброс — новая версия; ручные зоны остались в истории
    v2 = json.loads(fake_bucket.blob(f"users/{SUB}/athlete/v2/profile.json").download_as_text())
    assert v2["profile"]["hr_zones_custom"] == MANUAL_ZONES


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


def test_read_plan_state_carries_the_version(storage_module, fake_bucket):
    empty = {"plan_id": PID, "version": 0, "weeks": []}
    assert storage_module.read_plan_state(fake_bucket, SUB, PID) == empty
    assert storage_module.read_plan_state(fake_bucket, SUB, None) == {**empty, "plan_id": None}

    storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1}], "first")
    storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1}, {"w": 2}], "second")
    assert storage_module.read_plan_state(fake_bucket, SUB, PID) == {
        "plan_id": PID, "version": 2, "weeks": [{"w": 1}, {"w": 2}]}


def test_save_plan_weeks_accepts_the_version_it_was_based_on(storage_module, fake_bucket):
    # 0 — «недель ещё не было»: первая запись тоже идёт со сверкой
    first = storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1}], "first",
                                           base_version=0)
    second = storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1, "sun": "12км"}],
                                            "second", base_version=first["version"])
    assert (first["version"], second["version"]) == (1, 2)
    assert storage_module.read_plan_weeks(fake_bucket, SUB, PID) == [{"w": 1, "sun": "12км"}]


@pytest.mark.parametrize("stale", [0, 1, 3])
def test_save_plan_weeks_rejects_a_stale_version(storage_module, fake_bucket, stale):
    storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1}], "first")
    storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1, "sun": "12км"}], "second")
    before = dict(fake_bucket._store)

    with pytest.raises(storage_module.PlanStale):
        storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1, "sun": "чужое"}],
                                       "late", base_version=stale)
    # ни новой версии, ни тронутого манифеста
    assert fake_bucket._store == before
    assert storage_module.read_plan_manifest(fake_bucket, SUB, PID)["current_version"] == 2


def test_save_plan_weeks_rejects_a_first_write_over_existing_weeks(storage_module, fake_bucket):
    """Два устройства открыли пустой план: второе не должно затереть первое."""
    storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1}], "first", base_version=0)
    with pytest.raises(storage_module.PlanStale):
        storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 9}], "second", base_version=0)
    assert storage_module.read_plan_weeks(fake_bucket, SUB, PID) == [{"w": 1}]


# ── автор версии и история плана (#60) ────────────────────────────────────────

def test_plan_version_records_its_author(storage_module, fake_bucket):
    coach = storage_module.plan_author("coach", "c1")
    storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1}], "правка тренера",
                                   "c1@example.com", base_version=0, author=coach)
    manifest = storage_module.read_plan_manifest(fake_bucket, SUB, PID)
    assert manifest["author"] == {"role": "coach", "sub": "c1"}
    version = storage_module.read_plan_version(fake_bucket, manifest["gcs_object_path"])
    assert version["author"] == {"role": "coach", "sub": "c1"}
    assert version["created_by"] == "c1@example.com"


def test_plan_version_without_author_stays_as_before(storage_module, fake_bucket):
    storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1}], "seed")
    manifest = storage_module.read_plan_manifest(fake_bucket, SUB, PID)
    assert "author" not in manifest
    assert "author" not in storage_module.read_plan_version(fake_bucket, manifest["gcs_object_path"])


def _registered(storage_module, fake_bucket, sub, name):
    storage_module.resolve_user(fake_bucket, {"sub": sub, "email": f"{sub}@example.com",
                                              "name": name})


def test_plan_history_newest_first_with_names_but_no_emails(storage_module, fake_bucket):
    _registered(storage_module, fake_bucket, SUB, "Аня")
    _registered(storage_module, fake_bucket, "c1", "Пётр Тренер")
    save, author = storage_module.save_plan_weeks, storage_module.plan_author
    save(fake_bucket, SUB, PID, [{"w": 1}], "своя", f"{SUB}@example.com",
         author=author("athlete", SUB))
    save(fake_bucket, SUB, PID, [{"w": 2}], "тренер поправил", "c1@example.com",
         author=author("coach", "c1"))
    save(fake_bucket, SUB, PID, [{"w": 3}], "ИИ-тренер: легче", f"{SUB}@example.com",
         author=author("ai", SUB))

    history = storage_module.read_plan_history(fake_bucket, SUB, PID)
    assert history["has_more"] is False
    assert [(v["version"], v["author_role"], v["author_sub"], v["author_name"], v["change_reason"])
            for v in history["versions"]] == [
        (3, "ai", SUB, "Аня", "ИИ-тренер: легче"),
        (2, "coach", "c1", "Пётр Тренер", "тренер поправил"),
        (1, "athlete", SUB, "Аня", "своя"),
    ]
    assert all(v["created_at"].endswith("Z") for v in history["versions"])
    assert "@" not in json.dumps(history, ensure_ascii=False)


@pytest.mark.parametrize("created_by,reason,role", [
    ("auto-migrate", "migrated from single plan (#25)", "migration"),
    ("runner@example.com", "ИИ-тренер: сдвинул интервалы", "ai"),
    ("runner@example.com", "manual edit", "athlete"),
])
def test_history_guesses_the_author_of_versions_written_before_60(storage_module, fake_bucket,
                                                                  created_by, reason, role):
    storage_module.write_plan_version(fake_bucket, SUB, PID, 1, [{"w": 1}], reason, created_by)
    (version,) = storage_module.read_plan_history(fake_bucket, SUB, PID)["versions"]
    assert version["author_role"] == role
    assert version["author_sub"] == (None if role == "migration" else SUB)


def test_plan_history_is_bounded_and_says_so(storage_module, fake_bucket):
    for i in range(5):
        storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": i}], f"v{i + 1}")
    history = storage_module.read_plan_history(fake_bucket, SUB, PID, limit=3)
    assert [v["version"] for v in history["versions"]] == [5, 4, 3]
    assert history["has_more"] is True


def test_plan_history_ignores_other_plans_and_stray_objects(storage_module, fake_bucket):
    storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": 1}], "мой")
    storage_module.save_plan_weeks(fake_bucket, SUB, f"{PID}0", [{"w": 1}], "соседний")
    fake_bucket.blob(f"users/{SUB}/plans/{PID}/v2/notes.txt").upload_from_string("x")
    history = storage_module.read_plan_history(fake_bucket, SUB, PID)
    assert [(v["version"], v["change_reason"]) for v in history["versions"]] == [(1, "мой")]
    assert storage_module.read_plan_history(fake_bucket, SUB, "nope")["versions"] == []


def test_coach_plan_is_marked_and_does_not_take_over(storage_module, fake_bucket):
    mine = storage_module.create_plan(fake_bucket, SUB, {"race_name": "Мой"})
    theirs = storage_module.create_plan(fake_bucket, SUB, {"race_name": "От тренера"},
                                        make_active=False,
                                        author=storage_module.plan_author("coach", "c1"))
    assert (theirs["created_by_role"], theirs["created_by_sub"]) == ("coach", "c1")
    assert "created_by_role" not in mine
    assert storage_module.get_active_plan(fake_bucket, SUB)["id"] == mine["id"]


def test_plan_meta_edit_records_who_made_it(storage_module, fake_bucket):
    plan = storage_module.create_plan(fake_bucket, SUB, {"race_name": "HM"})
    upd = storage_module.update_plan_meta(fake_bucket, SUB, plan["id"], {"target_time": "1:35"},
                                          author=storage_module.plan_author("coach", "c1"))
    assert (upd["updated_by_role"], upd["updated_by_sub"]) == ("coach", "c1")


def test_plan_meta_edit_keeps_the_previous_values(storage_module, fake_bucket):
    """Карточка гонки живёт в перезаписываемом реестре: прежние значения
    уходят в неизменяемое событие на каждую правку."""
    plan = storage_module.create_plan(fake_bucket, SUB, {"race_name": "HM", "plan_start": "2026-09-07"})
    coach = storage_module.plan_author("coach", "c1")
    storage_module.update_plan_meta(fake_bucket, SUB, plan["id"], {"plan_start": "2026-09-14"},
                                    author=coach)
    storage_module.update_plan_meta(fake_bucket, SUB, plan["id"], {"target_time": "1:35"})

    prefix = f"users/{SUB}/plans/{plan['id']}/meta/"
    events = [json.loads(b.download_as_text()) for b in fake_bucket.list_blobs(prefix=prefix)]
    assert [(e["before"]["plan_start"], e["after"]["plan_start"]) for e in events] == [
        ("2026-09-07", "2026-09-14"), ("2026-09-14", "2026-09-14")]
    assert events[0]["author"] == coach and events[1]["author"] is None
    assert (events[1]["before"]["target_time"], events[1]["after"]["target_time"]) == ("", "1:35")
    # событие — не версия недель: история плана его не видит
    assert storage_module.read_plan_history(fake_bucket, SUB, plan["id"])["versions"] == []


def test_unchanged_race_card_writes_nothing(storage_module, fake_bucket):
    """Форма шлёт все поля: сохранение без правок не двигает updated_at и не
    пишет событие — ни у спортсмена, ни у тренера."""
    plan = storage_module.create_plan(fake_bucket, SUB, {"race_name": "HM", "target_time": "1:40"})
    before = dict(fake_bucket._store)
    same = storage_module.update_plan_meta(fake_bucket, SUB, plan["id"],
                                           {"race_name": "HM", "target_time": "1:40", "race_date": ""},
                                           author=storage_module.plan_author("athlete", SUB))
    assert same["id"] == plan["id"] and "updated_at" not in same
    assert fake_bucket._store == before
    assert storage_module.update_plan_meta(fake_bucket, SUB, "ghost", {"race_name": "X"}) is None


def test_plans_index_write_does_not_lose_a_concurrent_change(storage_module, fake_bucket):
    """Тренер создаёт план, пока спортсмен переключает активный: запись по
    устаревшему реестру отклоняется и повторяется поверх свежего."""
    first = storage_module.create_plan(fake_bucket, SUB, {"race_name": "A"})
    second = storage_module.create_plan(fake_bucket, SUB, {"race_name": "B"})       # активен
    raced = []

    def add_with_a_race(index):
        if not raced:                         # «соседний запрос» успел между чтением и записью
            raced.append(True)
            storage_module.set_active_plan(fake_bucket, SUB, first["id"])
        index["plans"].append({"id": "coach-plan", "archived": False})
        return index["plans"][-1]

    storage_module._update_plans_index(fake_bucket, SUB, add_with_a_race)
    index = storage_module.read_plans_index(fake_bucket, SUB)
    assert [p["id"] for p in index["plans"]] == [first["id"], second["id"], "coach-plan"]
    assert index["active_plan_id"] == first["id"]           # переключение спортсмена цело


def test_plans_index_gives_up_when_it_keeps_changing(storage_module, fake_bucket):
    plan = storage_module.create_plan(fake_bucket, SUB, {"race_name": "A"})

    def always_raced(index):
        storage_module.set_active_plan(fake_bucket, SUB, plan["id"])    # каждый раз кто-то пишет
        return index

    with pytest.raises(storage_module.PlansIndexBusy):
        storage_module._update_plans_index(fake_bucket, SUB, always_raced)


def test_unreadable_version_drops_out_of_the_history(storage_module, fake_bucket):
    for i in range(3):
        storage_module.save_plan_weeks(fake_bucket, SUB, PID, [{"w": i}], f"v{i + 1}")
    fake_bucket.blob(f"users/{SUB}/plans/{PID}/v2/plan.json").upload_from_string("{не json")
    history = storage_module.read_plan_history(fake_bucket, SUB, PID)
    assert [v["version"] for v in history["versions"]] == [3, 1]


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


# ── daily usage of the LLM ───────────────────────────────────────────────────

def test_advice_usage_counter(storage_module, fake_bucket):
    assert storage_module.read_advice_usage(fake_bucket, SUB)["count"] == 0
    assert storage_module.reserve_advice_usage(fake_bucket, SUB, limit=5)["count"] == 1
    assert storage_module.reserve_advice_usage(fake_bucket, SUB, limit=5)["count"] == 2
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
    # legacy-данные принадлежат админу, и перенос положен только ему (#53)
    storage_module.resolve_user(fake_bucket, {
        "sub": SUB, "email": "aabramov77@gmail.com", "email_verified": True})
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


# ── Нагрузка (#66): кэш гистограмм и пороговый темп ──────────────────────────

LOAD_TODAY = date(2026, 10, 20)
HIST_PATH = f"users/{SUB}/derived/run_hist.json"
FIT_SAMPLES = {"samples": {"t_offset_sec": [0, 300, 600], "hr": [130, 130, 130],
                           "pace_sec_per_km": [300, 300, 300]}}


def _fit_runs(storage_module, bucket, count):
    runs = [{"id": i, "date": "2026-10-13", "dist": 5.0, "type": "easy", "time": "25:00",
             "pace": "5:00", "hr": 130, "details_available": True, "deleted": False}
            for i in range(1, count + 1)]
    storage_module.write_runs(bucket, SUB, runs)
    return runs


@pytest.fixture
def details_reads(storage_module, monkeypatch):
    """Подменяет чтение деталей: считает обращения и отдаёт DETAILS[run_id]
    или FIT_SAMPLES."""
    reads, answers = [], {}

    def fake(bucket, sub, run_id):
        reads.append(run_id)
        return answers.get(run_id, FIT_SAMPLES)

    monkeypatch.setattr(storage_module, "read_run_details", fake)
    fake.reads, fake.answers = reads, answers
    return fake


def _load(storage_module, bucket, **kw):
    return storage_module.build_training_load(bucket, SUB, today=LOAD_TODAY, **kw)


def _cached(bucket):
    raw = bucket._store.get(HIST_PATH)
    return json.loads(raw) if raw else None


def test_attaching_fit_puts_its_histograms_into_the_cache(storage_module, fake_bucket):
    """Синтетический FIT: пульс 120→170, подъём 60→75 м при 3,3 м/с."""
    fit = (Path(__file__).parent / "fixtures" / "synthetic_activity.fit").read_bytes()
    parsed = storage_module.parse_fit_file(fit)
    token = storage_module.write_parsed_fit_to_tmp(fake_bucket, SUB, fit, parsed)
    storage_module.attach_fit_details_to_run(fake_bucket, SUB, {"id": 123}, token)

    entry = _cached(fake_bucket)["runs"]["123"]
    pulses = [int(k) for k in entry["hr"]]
    assert pulses and min(pulses) >= 120 and max(pulses) <= 170
    speeds = [int(k) for k in entry["spd"]]
    assert speeds and min(speeds) > 330            # подъём — дороже ровного 3,3 м/с


def test_cache_failure_does_not_lose_the_run(storage_module, fake_bucket, monkeypatch):
    def broken(*args):
        raise RuntimeError("кэш недоступен")

    monkeypatch.setattr(storage_module, "remember_run_hist", broken)
    token = storage_module.write_parsed_fit_to_tmp(fake_bucket, SUB, b"FIT", FIT_SAMPLES)
    run = {"id": 5}
    storage_module.attach_fit_details_to_run(fake_bucket, SUB, run, token)
    assert run["details_available"] is True
    assert fake_bucket.blob(f"users/{SUB}/runs/5/v1/details.json").exists()
    assert _cached(fake_bucket) is None


def test_old_runs_are_filled_at_most_60_per_call(storage_module, fake_bucket, details_reads):
    _fit_runs(storage_module, fake_bucket, 100)
    assert _load(storage_module, fake_bucket)["pending"] == 40
    assert len(details_reads.reads) == 60 and len(_cached(fake_bucket)["runs"]) == 60

    assert _load(storage_module, fake_bucket)["pending"] == 0
    assert len(details_reads.reads) == 100 and len(set(details_reads.reads)) == 100


def test_cached_runs_are_not_read_again(storage_module, fake_bucket, details_reads):
    _fit_runs(storage_module, fake_bucket, 3)
    first = _load(storage_module, fake_bucket)
    assert _load(storage_module, fake_bucket) == first
    assert sorted(details_reads.reads) == [1, 2, 3]


def test_fit_without_pulse_or_speed_is_cached_empty(storage_module, fake_bucket, details_reads):
    _fit_runs(storage_module, fake_bucket, 2)
    details_reads.answers.update({1: {"laps": []}, 2: None})       # 2 — деталей нет вовсе
    result = _load(storage_module, fake_bucket, profile={"hr_max": 190})
    assert _cached(fake_bucket)["runs"] == {"1": {"hr": {}, "spd": {}}, "2": {"hr": {}, "spd": {}}}
    assert result["runs"]["1"]["int_est"] is True                # оценка по среднему пульсу
    _load(storage_module, fake_bucket)
    assert sorted(details_reads.reads) == [1, 2]


def test_new_algorithm_version_rebuilds_the_cache(storage_module, fake_bucket, details_reads):
    _fit_runs(storage_module, fake_bucket, 1)
    fake_bucket.blob(HIST_PATH).upload_from_string(
        json.dumps({"v": 0, "runs": {"1": {"hr": {"999": 1}, "spd": {}}}}))
    _load(storage_module, fake_bucket)
    assert details_reads.reads == [1]
    assert _cached(fake_bucket)["v"] == storage_module.HIST_VERSION
    assert _cached(fake_bucket)["runs"]["1"]["hr"] == {"130": 60}


def test_lost_generation_race_is_not_an_error(storage_module, fake_bucket, monkeypatch):
    """Пока мы читали детали, кэш записал соседний инстанс: наша запись не
    проходит и его не затирает, а ответ всё равно посчитан по сэмплам."""
    _fit_runs(storage_module, fake_bucket, 1)
    other = json.dumps({"v": storage_module.HIST_VERSION, "runs": {"77": {"hr": {}, "spd": {}}}})

    def racing(bucket, sub, run_id):
        fake_bucket.blob(HIST_PATH).upload_from_string(other)
        return FIT_SAMPLES

    monkeypatch.setattr(storage_module, "read_run_details", racing)
    result = _load(storage_module, fake_bucket, profile={"hr_max": 190})
    assert result["runs"]["1"]["int"] is not None and result["runs"]["1"]["int_est"] is False
    assert fake_bucket._store[HIST_PATH].decode() == other


def test_read_only_call_writes_nothing(storage_module, fake_bucket, details_reads):
    _fit_runs(storage_module, fake_bucket, 3)
    before = dict(fake_bucket._store)
    result = _load(storage_module, fake_bucket, fill_limit=0, profile={"hr_max": 190})
    assert fake_bucket._store == before and details_reads.reads == []
    assert result["pending"] == 3
    assert all(r["int_est"] for r in result["runs"].values())       # пока по средним


def test_profile_changes_move_the_numbers_without_a_rebuild(storage_module, fake_bucket,
                                                            details_reads):
    _fit_runs(storage_module, fake_bucket, 1)
    edwards = _load(storage_module, fake_bucket, profile={"hr_max": 190})
    banister = _load(storage_module, fake_bucket,
                     profile={"hr_max": 190, "hr_rest": 50, "sex": "m"})
    assert edwards["internal"]["method"] == "edwards" and banister["internal"]["method"] == "banister"
    assert edwards["runs"]["1"]["int"] != banister["runs"]["1"]["int"]

    slow = _load(storage_module, fake_bucket, profile={"threshold_pace_sec": 300})
    fast = _load(storage_module, fake_bucket, profile={"threshold_pace_sec": 240})
    assert slow["runs"]["1"]["ext"] > fast["runs"]["1"]["ext"]
    assert details_reads.reads == [1]


@pytest.mark.parametrize("value,expected", [
    ("4:35", 275), (" 4:35 ", 275), (275, 275), (275.4, 275), ("2:30", 150), ("10:00", 600),
    ("", None), ("  ", None), (None, None),
])
def test_threshold_pace_is_cleaned(storage_module, value, expected):
    assert storage_module.clean_threshold_pace(value) == (expected, None)


def test_threshold_pace_is_versioned_with_the_profile(storage_module, fake_bucket):
    for pace in ("4:35", "4:20"):
        profile, errors = storage_module.clean_athlete_profile({"threshold_pace_sec": pace})
        assert not errors
        storage_module.write_athlete_version(fake_bucket, SUB, profile)
    history = storage_module.read_athlete_history(fake_bucket, SUB)
    assert [v["threshold_pace_sec"] for v in history] == [275, 260]
    assert fake_bucket.blob(f"users/{SUB}/athlete/v1/profile.json").exists()


def test_old_profile_version_reads_without_a_threshold(storage_module, fake_bucket):
    fake_bucket.blob(f"users/{SUB}/athlete/v1/profile.json").upload_from_string(
        json.dumps({"version": 1, "profile": {"hr_max": 185}}))
    fake_bucket.blob(f"users/{SUB}/athlete/manifest.json").upload_from_string(
        json.dumps({"current_version": 1,
                    "gcs_object_path": f"users/{SUB}/athlete/v1/profile.json"}))
    profile, _, _ = storage_module.read_athlete_profile(fake_bucket, SUB)
    assert profile["threshold_pace_sec"] is None
    races = [{"dist_label": "10km", "time": "45:30", "date": "2026-09-14"}]
    derived = storage_module.athlete_derived(profile, races, LOAD_TODAY)
    assert (derived["threshold_pace_sec"], derived["threshold_source"]) == (277, "races")


def test_coach_view_hides_the_threshold_and_the_zone_source(storage_module, fake_bucket,
                                                            details_reads):
    _fit_runs(storage_module, fake_bucket, 2)
    races = [{"dist_label": "10km", "time": "45:30", "date": "2026-09-14"}]
    load = _load(storage_module, fake_bucket, profile={"hr_max": 190}, races=races)
    assert load["external"]["threshold_basis"]["time"] == "45:30"
    view = storage_module.coach_load_view(load)
    text = json.dumps(view, ensure_ascii=False)
    for secret in ("threshold", "basis", "zones_source", "45:30", "4:37"):
        assert secret not in text, secret
    assert view["weeks"] == [{k: w[k] for k in view["weeks"][0]} for w in load["weeks"]]
    assert view["runs"] == load["runs"] and view["rolling"]["int"] == load["rolling"]["int"]


def test_broken_details_are_cached_empty_but_a_failed_read_is_retried(storage_module, fake_bucket,
                                                                     monkeypatch):
    """Битый details.json сам не починится — его не перечитываем. Сбой чтения
    временный — пробежка остаётся в pending и читается в следующий раз."""
    _fit_runs(storage_module, fake_bucket, 2)
    reads = []

    def flaky(bucket, sub, run_id):
        reads.append(run_id)
        if run_id == 1:
            raise json.JSONDecodeError("битый", "{", 0)
        raise RuntimeError("503 от хранилища")

    monkeypatch.setattr(storage_module, "read_run_details", flaky)
    assert _load(storage_module, fake_bucket)["pending"] == 1
    assert _cached(fake_bucket)["runs"] == {"1": {"hr": {}, "spd": {}}}
    _load(storage_module, fake_bucket)
    assert reads == [1, 2, 2]
