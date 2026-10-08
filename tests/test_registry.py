"""Tests for the multi-user registry, user resolution, status transitions, and
legacy migration — against the in-memory fake bucket."""
import pytest


ADMIN_TOKEN = {"sub": "admin-sub", "email": "aabramov77@gmail.com",
               "email_verified": True, "name": "Alex"}
USER_TOKEN = {"sub": "u1", "email": "runner@example.com", "name": "Runner"}


# ── resolve_user ──────────────────────────────────────────────────────────────

def test_admin_first_login_is_approved(storage_module, fake_bucket):
    rec = storage_module.resolve_user(fake_bucket, ADMIN_TOKEN)
    assert rec["status"] == "approved"
    assert rec["role"] == "admin"
    assert rec["approved_by"] == "admin-sub"


def test_new_user_is_pending(storage_module, fake_bucket):
    rec = storage_module.resolve_user(fake_bucket, USER_TOKEN)
    assert rec["status"] == "pending"
    assert rec["role"] == "user"
    assert rec["approved_by"] is None


def test_resolve_is_idempotent(storage_module, fake_bucket):
    a = storage_module.resolve_user(fake_bucket, USER_TOKEN)
    b = storage_module.resolve_user(fake_bucket, USER_TOKEN)
    assert a == b
    reg = storage_module.read_registry(fake_bucket)
    assert len(reg["users"]) == 1


def test_registry_never_holds_personal_profile_data(storage_module, fake_bucket):
    """#32: ФИО и дата рождения — персданные, в админский список не попадают.

    /admin/users отдаёт записи реестра как есть, поэтому проверяем сам реестр.
    """
    storage_module.resolve_user(fake_bucket, USER_TOKEN)
    profile, _ = storage_module.clean_athlete_profile(
        {"full_name": "Иванов Иван", "birth_date": "1979-03-01", "weight_kg": 74})
    storage_module.write_athlete_version(fake_bucket, "u1", profile, "тест")

    record = storage_module.read_registry(fake_bucket)["users"]["u1"]
    for field in ("full_name", "birth_date", "weight_kg", "hr_max", "injuries", "notes"):
        assert field not in record
    assert "Иванов" not in str(record)


def test_register_writes_audit_event(storage_module, fake_bucket):
    storage_module.resolve_user(fake_bucket, USER_TOKEN)
    events = [b.name for b in fake_bucket.list_blobs(prefix="users/events/")]
    assert any("register" in n for n in events)


def test_registration_closed_at_limit(storage_module, fake_bucket, monkeypatch):
    monkeypatch.setattr(storage_module, "MAX_PENDING", 2)
    storage_module.resolve_user(fake_bucket, {"sub": "a", "email": "a@x.com"})
    storage_module.resolve_user(fake_bucket, {"sub": "b", "email": "b@x.com"})
    with pytest.raises(storage_module.RegistrationClosed):
        storage_module.resolve_user(fake_bucket, {"sub": "c", "email": "c@x.com"})


def test_admin_bypasses_registration_limit(storage_module, fake_bucket, monkeypatch):
    monkeypatch.setattr(storage_module, "MAX_PENDING", 0)
    # non-admin blocked
    with pytest.raises(storage_module.RegistrationClosed):
        storage_module.resolve_user(fake_bucket, USER_TOKEN)
    # admin still gets in
    rec = storage_module.resolve_user(fake_bucket, ADMIN_TOKEN)
    assert rec["status"] == "approved"


# ── set_user_status ───────────────────────────────────────────────────────────

def test_approve_and_reject(storage_module, fake_bucket):
    storage_module.resolve_user(fake_bucket, USER_TOKEN)
    rec = storage_module.set_user_status(fake_bucket, "u1", "approved", "admin-sub")
    assert rec["status"] == "approved" and rec["approved_by"] == "admin-sub"

    rec2 = storage_module.set_user_status(fake_bucket, "u1", "rejected", "admin-sub")
    assert rec2["status"] == "rejected"

    # audit events recorded
    events = [b.name for b in fake_bucket.list_blobs(prefix="users/events/")]
    assert any("approved" in n for n in events)
    assert any("rejected" in n for n in events)


def test_set_status_unknown_user(storage_module, fake_bucket):
    assert storage_module.set_user_status(fake_bucket, "ghost", "approved", "admin-sub") is None


# ── registry cache ────────────────────────────────────────────────────────────

def test_write_registry_updates_cache(storage_module, fake_bucket):
    storage_module.resolve_user(fake_bucket, ADMIN_TOKEN)
    # cache now warm; a fresh read returns the admin without hitting a cold load
    reg = storage_module.read_registry(fake_bucket)
    assert "admin-sub" in reg["users"]


# ── legacy migration ──────────────────────────────────────────────────────────

def test_migrate_legacy_copies_and_is_idempotent(storage_module, fake_bucket):
    # seed legacy global objects
    fake_bucket.blob("runs.json").upload_from_string('[{"id": 1, "dist": 10}]')
    fake_bucket.blob("races.json").upload_from_string('[{"id": 2}]')
    fake_bucket.blob("plan/v1/plan.json").upload_from_string('{"weeks": [{"w": 1}]}')
    fake_bucket.blob("plan/manifest.json").upload_from_string(
        '{"current_version": 1, "gcs_object_path": "plan/v1/plan.json"}')

    report = storage_module.migrate_legacy_to_user(fake_bucket, "admin-sub")
    assert f"users/admin-sub/runs.json" in report["copied"]
    assert fake_bucket.blob("users/admin-sub/runs.json").exists()
    assert fake_bucket.blob("users/admin-sub/races.json").exists()
    assert fake_bucket.blob("users/admin-sub/plan/v1/plan.json").exists()

    # profile seeded with Alexander's historical race constants
    prof = storage_module.read_legacy_race_profile(fake_bucket, "admin-sub")
    assert prof["race_date"] == "2026-08-09" and prof["target_time"] == "1:40"

    # per-user plan manifest points INTO the user namespace (not the legacy path)
    import json
    man = json.loads(fake_bucket.blob("users/admin-sub/plan/manifest.json").download_as_text())
    assert man["gcs_object_path"] == "users/admin-sub/plan/v1/plan.json"

    # legacy originals untouched (no physical delete)
    assert fake_bucket.blob("runs.json").exists()

    # second run: everything skipped, nothing errors
    report2 = storage_module.migrate_legacy_to_user(fake_bucket, "admin-sub")
    assert report2["copied"] == []
    assert report2["errors"] == []
    assert any("уже есть" in s for s in report2["skipped"])


# ── #25: ленивая миграция одиночного плана → реестр планов ────────────────────

SUB = "u-single"


def _seed_single_plan(storage_module, fake_bucket, sub=SUB):
    """Состояние пользователя до #25: profile.json + users/{sub}/plan/*."""
    storage_module.write_legacy_race_profile(fake_bucket, sub, {
        "race_name": "Полумарафон", "race_date": "2026-08-09",
        "target_time": "1:40", "plan_start": "2026-05-10"})
    fake_bucket.blob(f"users/{sub}/plan/v1/plan.json").upload_from_string(
        '{"version": 1, "weeks": [{"w": 1, "mon": "8км"}]}')
    fake_bucket.blob(f"users/{sub}/plan/manifest.json").upload_from_string(
        f'{{"current_version": 1, "gcs_object_path": "users/{sub}/plan/v1/plan.json"}}')


def test_lazy_migration_creates_plan_from_profile(storage_module, fake_bucket):
    _seed_single_plan(storage_module, fake_bucket)
    storage_module.write_runs(fake_bucket, SUB, [{"id": 1, "dist": 10}, {"id": 2, "dist": 5}])

    index = storage_module.read_plans_index(fake_bucket, SUB)   # triggers migration
    assert len(index["plans"]) == 1
    plan = index["plans"][0]
    assert index["active_plan_id"] == plan["id"]
    # race metadata carried over from profile.json
    assert plan["race_name"] == "Полумарафон" and plan["target_time"] == "1:40"
    # current weeks became v1 of the new plan
    assert storage_module.read_plan_weeks(fake_bucket, SUB, plan["id"]) == [{"w": 1, "mon": "8км"}]
    # existing runs attached to the migrated plan
    assert all(r["plan_id"] == plan["id"] for r in storage_module.read_runs(fake_bucket, SUB))
    # legacy single-plan objects untouched (no physical delete)
    assert fake_bucket.blob(f"users/{SUB}/plan/v1/plan.json").exists()


def test_lazy_migration_is_idempotent(storage_module, fake_bucket):
    _seed_single_plan(storage_module, fake_bucket)
    first = storage_module.read_plans_index(fake_bucket, SUB)
    second = storage_module.read_plans_index(fake_bucket, SUB)
    assert first == second
    assert len(second["plans"]) == 1


def test_lazy_migration_noop_for_fresh_user(storage_module, fake_bucket):
    """Новый пользователь без плана и профиля → пустой реестр, ничего не создаётся."""
    index = storage_module.read_plans_index(fake_bucket, "u-fresh")
    assert index["plans"] == [] and index["active_plan_id"] is None
    assert storage_module.get_active_plan(fake_bucket, "u-fresh") is None


# ── Тренер (#44): роль и связь «спортсмен → тренер» ──────────────────────────

COACH_TOKEN = {"sub": "c1", "email": "coach@example.com", "name": "Пётр Тренер"}


def _approved(storage_module, fake_bucket, token):
    storage_module.resolve_user(fake_bucket, token)
    storage_module.set_user_status(fake_bucket, token["sub"], "approved", "admin-sub")


def _coach_and_athlete(storage_module, fake_bucket):
    _approved(storage_module, fake_bucket, COACH_TOKEN)
    _approved(storage_module, fake_bucket, USER_TOKEN)
    storage_module.set_coach_flag(fake_bucket, "c1", True, "admin-sub")


def _events(fake_bucket):
    return [b.name for b in fake_bucket.list_blobs(prefix="users/events/")]


def test_admin_grants_and_revokes_coach(storage_module, fake_bucket):
    _approved(storage_module, fake_bucket, COACH_TOKEN)
    assert storage_module.list_coaches(fake_bucket) == []

    rec = storage_module.set_coach_flag(fake_bucket, "c1", True, "admin-sub")
    assert rec["is_coach"] is True
    assert [c["sub"] for c in storage_module.list_coaches(fake_bucket)] == ["c1"]

    storage_module.set_coach_flag(fake_bucket, "c1", False, "admin-sub")
    assert storage_module.list_coaches(fake_bucket) == []
    events = _events(fake_bucket)
    assert any("c1-coach_grant" in n for n in events)
    assert any("c1-coach_revoke" in n for n in events)


def test_coach_flag_unknown_user(storage_module, fake_bucket):
    assert storage_module.set_coach_flag(fake_bucket, "ghost", True, "admin-sub") is None


def test_coach_card_shows_name_but_not_email(storage_module, fake_bucket):
    """Список тренеров видят все одобренные пользователи — почту не раздаём."""
    _coach_and_athlete(storage_module, fake_bucket)
    (card,) = storage_module.list_coaches(fake_bucket)
    assert card == {"sub": "c1", "name": "Пётр Тренер"}


def test_unapproved_coach_cannot_be_selected(storage_module, fake_bucket):
    storage_module.resolve_user(fake_bucket, COACH_TOKEN)          # pending
    storage_module.set_coach_flag(fake_bucket, "c1", True, "admin-sub")
    _approved(storage_module, fake_bucket, USER_TOKEN)

    assert storage_module.list_coaches(fake_bucket) == []
    with pytest.raises(storage_module.CoachLinkError, match="not_a_coach"):
        storage_module.set_user_coach(fake_bucket, "u1", "c1")


def test_athlete_selects_and_clears_coach(storage_module, fake_bucket):
    _coach_and_athlete(storage_module, fake_bucket)
    assert storage_module.current_coach(fake_bucket, "u1") is None

    storage_module.set_user_coach(fake_bucket, "u1", "c1")
    assert storage_module.current_coach(fake_bucket, "u1") == {"sub": "c1", "name": "Пётр Тренер"}

    storage_module.set_user_coach(fake_bucket, "u1", None)
    assert storage_module.current_coach(fake_bucket, "u1") is None

    events = _events(fake_bucket)
    assert any("u1-coach_select" in n for n in events)
    assert any("u1-coach_clear" in n for n in events)


def test_coach_select_event_records_who_was_chosen(storage_module, fake_bucket):
    import json
    _coach_and_athlete(storage_module, fake_bucket)
    storage_module.set_user_coach(fake_bucket, "u1", "c1")
    (name,) = [n for n in _events(fake_bucket) if "u1-coach_select" in n]
    event = json.loads(fake_bucket.blob(name).download_as_text())
    assert event["actor"] == "u1"
    assert event["details"] == {"coach_sub": "c1", "previous": None}


@pytest.mark.parametrize("target,reason", [
    ("u1", "cannot_coach_yourself"),
    ("admin-sub", "not_a_coach"),      # одобрен, но тренером не назначен
    ("ghost", "not_a_coach"),
])
def test_coach_selection_is_validated(storage_module, fake_bucket, target, reason):
    storage_module.resolve_user(fake_bucket, ADMIN_TOKEN)
    _coach_and_athlete(storage_module, fake_bucket)
    with pytest.raises(storage_module.CoachLinkError, match=reason):
        storage_module.set_user_coach(fake_bucket, "u1", target)
    assert storage_module.current_coach(fake_bucket, "u1") is None


def test_selecting_the_same_coach_again_is_a_noop(storage_module, fake_bucket):
    _coach_and_athlete(storage_module, fake_bucket)
    storage_module.set_user_coach(fake_bucket, "u1", "c1")
    storage_module.set_user_coach(fake_bucket, "u1", "c1")
    assert len([n for n in _events(fake_bucket) if "coach_select" in n]) == 1


def test_revoking_coach_releases_athletes_for_good(storage_module, fake_bucket):
    """Снятие роли отвязывает спортсменов, и повторное назначение доступ
    не возвращает: выбрать тренера снова может только сам спортсмен."""
    _coach_and_athlete(storage_module, fake_bucket)
    storage_module.set_user_coach(fake_bucket, "u1", "c1")

    storage_module.set_coach_flag(fake_bucket, "c1", False, "admin-sub")
    assert storage_module.read_registry(fake_bucket)["users"]["u1"]["coach_sub"] is None
    assert any("u1-coach_clear" in n for n in _events(fake_bucket))

    storage_module.set_coach_flag(fake_bucket, "c1", True, "admin-sub")
    assert storage_module.current_coach(fake_bucket, "u1") is None


def test_rejected_coach_stops_being_anyones_coach(storage_module, fake_bucket):
    _coach_and_athlete(storage_module, fake_bucket)
    storage_module.set_user_coach(fake_bucket, "u1", "c1")
    storage_module.set_user_status(fake_bucket, "c1", "rejected", "admin-sub")
    assert storage_module.current_coach(fake_bucket, "u1") is None
    assert storage_module.list_coaches(fake_bucket) == []


def test_coach_writes_do_not_clobber_another_instances_change(storage_module, fake_bucket):
    """Кэш реестра у каждого инстанса свой. Запись по устаревшей копии
    затёрла бы пользователя, зарегистрированного на соседнем инстансе."""
    import json
    _coach_and_athlete(storage_module, fake_bucket)      # кэш этого «инстанса» прогрет

    blob = fake_bucket.blob("users/registry.json")
    remote = json.loads(blob.download_as_text())
    remote["users"]["u9"] = {"sub": "u9", "status": "pending", "role": "user"}
    blob.upload_from_string(json.dumps(remote))          # «соседний инстанс»

    storage_module.set_user_coach(fake_bucket, "u1", "c1")
    storage_module.set_coach_flag(fake_bucket, "c1", True, "admin-sub")

    # blob помнит поколение своей записи, как настоящий Blob, — читаем заново
    stored = json.loads(fake_bucket.blob("users/registry.json").download_as_text())["users"]
    assert "u9" in stored
    assert stored["u1"]["coach_sub"] == "c1"


# ── Тренер (#44): право на данные спортсмена ─────────────────────────────────

def test_coach_can_access_only_the_athlete_who_chose_them(storage_module, fake_bucket):
    storage_module.resolve_user(fake_bucket, ADMIN_TOKEN)
    _coach_and_athlete(storage_module, fake_bucket)
    can = storage_module.coach_can_access

    assert not can(fake_bucket, "c1", "u1")               # ещё не выбрал
    storage_module.set_user_coach(fake_bucket, "u1", "c1")
    assert can(fake_bucket, "c1", "u1")

    assert not can(fake_bucket, "u1", "c1")               # связь не симметрична
    assert not can(fake_bucket, "admin-sub", "u1")        # админ — не тренер
    assert not can(fake_bucket, "c1", "admin-sub")
    assert not can(fake_bucket, "c1", "ghost")
    assert not can(fake_bucket, "ghost", "u1")
    assert not can(fake_bucket, "c1", "c1")


@pytest.mark.parametrize("who", ["c1", "u1"])
def test_rejecting_either_side_closes_access(storage_module, fake_bucket, who):
    _coach_and_athlete(storage_module, fake_bucket)
    storage_module.set_user_coach(fake_bucket, "u1", "c1")
    storage_module.set_user_status(fake_bucket, who, "rejected", "admin-sub")
    assert not storage_module.coach_can_access(fake_bucket, "c1", "u1")


def test_stale_instance_does_not_bring_a_dropped_coach_back(storage_module, fake_bucket):
    """Спортсмен снял тренера на одном инстансе, а на другом — с ещё прогретым
    кэшем — админ одобрил новичка. Запись реестра из кэша вернула бы связь."""
    import json
    _coach_and_athlete(storage_module, fake_bucket)
    storage_module.set_user_coach(fake_bucket, "u1", "c1")           # кэш помнит связь

    blob = fake_bucket.blob("users/registry.json")
    remote = json.loads(blob.download_as_text())
    remote["users"]["u1"]["coach_sub"] = None
    blob.upload_from_string(json.dumps(remote))                      # «соседний инстанс»
    storage_module._registry_cache["ts"] = __import__("time").time() # кэш всё ещё «свежий»

    storage_module.resolve_user(fake_bucket, {"sub": "u7", "email": "n@example.com", "name": "N"})
    storage_module.set_user_status(fake_bucket, "u7", "approved", "admin-sub")

    stored = json.loads(fake_bucket.blob("users/registry.json").download_as_text())
    assert stored["users"]["u1"]["coach_sub"] is None
    assert not storage_module.coach_can_access(fake_bucket, "c1", "u1")


def test_list_athletes_of(storage_module, fake_bucket):
    _coach_and_athlete(storage_module, fake_bucket)
    assert storage_module.list_athletes_of(fake_bucket, "u1") is None      # не тренер
    assert storage_module.list_athletes_of(fake_bucket, "c1") == []

    storage_module.set_user_coach(fake_bucket, "u1", "c1")
    assert storage_module.list_athletes_of(fake_bucket, "c1") == [{"sub": "u1", "name": "Runner"}]
