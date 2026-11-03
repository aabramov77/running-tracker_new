"""Запись в GCS только с предусловием (#35).

Сторож разбирает код и не даёт вернуть запись без if_generation_match.
Остальные тесты — гонки: конкурент вклинивается между чтением и записью
(conftest.interleave), и ни версия, ни чужая правка не теряются.
"""
import ast
import json

import pytest

from conftest import REPO, interleave, put_json

SUB = "u-35"
PID = "plan-35"
ADMIN_TOKEN = {"sub": "admin-sub", "email": "aabramov77@gmail.com",
               "email_verified": True, "name": "Alex"}


def _token(sub):
    return {"sub": sub, "email": f"{sub}@example.com", "name": sub.title()}


# ── Сторож ────────────────────────────────────────────────────────────────────

# Временные tmp/ пишутся и удаляются без предусловия: это не бизнес-данные.
TMP_WRITERS = {"write_parsed_fit_to_tmp"}
TMP_DELETERS = {"cleanup_old_tmp", "_drop_tmp_fit"}


def _calls(module_file, method):
    """[(имена объемлющих функций, вызов)] для всех .method(...) в модуле."""
    found = []

    def visit(node, scope):
        for child in ast.iter_child_nodes(node):
            inner = scope + (child.name,) if isinstance(child, ast.FunctionDef) else scope
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute)
                    and child.func.attr == method):
                found.append((inner, child))
            visit(child, inner)

    visit(ast.parse((REPO / module_file).read_text(encoding="utf-8")), ())
    return found


def test_the_guard_sees_the_writes():
    """Сторож не проходит впустую: записи в storage.py он находит."""
    assert len(_calls("storage.py", "upload_from_string")) >= 4
    assert _calls("storage.py", "copy_blob")


@pytest.mark.parametrize("module_file", ["storage.py", "api.py"])
@pytest.mark.parametrize("method", ["upload_from_string", "copy_blob"])
def test_every_write_has_a_precondition(module_file, method):
    bare = [f"{module_file}:{call.lineno} в {'.'.join(scope) or 'модуле'}"
            for scope, call in _calls(module_file, method)
            if not any(k.arg == "if_generation_match" for k in call.keywords)
            and not TMP_WRITERS & set(scope)]
    assert bare == []


@pytest.mark.parametrize("module_file", ["storage.py", "api.py"])
def test_only_temporary_objects_are_deleted(module_file):
    deletes = [f"{module_file}:{call.lineno}" for scope, call in _calls(module_file, "delete")
               if not TMP_DELETERS & set(scope)]
    assert deletes == []


# ── Версии плана ──────────────────────────────────────────────────────────────

def _plan_object(bucket, version):
    return json.loads(bucket._store[f"users/{SUB}/plans/{PID}/v{version}/plan.json"])


def _history(storage_module, bucket):
    return [v["version"] for v in storage_module.read_plan_history(bucket, SUB, PID)["versions"]]


@pytest.mark.parametrize("before_read", [False, True], ids=["после чтения", "до чтения"])
def test_concurrent_plan_saves_keep_both_versions(storage_module, fake_bucket, monkeypatch,
                                                  before_read):
    """Два сохранения без сверки: оба становятся версиями, объект первого не
    затёрт, манифест — на последнем."""
    save = storage_module.save_plan_weeks
    save(fake_bucket, SUB, PID, [{"w": 1}], "v1")
    rival = []
    interleave(fake_bucket, monkeypatch,
               lambda: rival.append(save(fake_bucket, SUB, PID, [{"w": "rival"}], "rival")),
               before_read=before_read)

    mine = save(fake_bucket, SUB, PID, [{"w": "mine"}], "mine")

    assert _plan_object(fake_bucket, rival[0]["version"])["weeks"] == [{"w": "rival"}]
    assert mine["version"] > rival[0]["version"]
    assert storage_module.read_plan_state(fake_bucket, SUB, PID) == \
        {"plan_id": PID, "version": mine["version"], "weeks": [{"w": "mine"}]}
    assert _history(storage_module, fake_bucket) == [mine["version"], rival[0]["version"], 1]


@pytest.mark.parametrize("before_read", [False, True], ids=["после чтения", "до чтения"])
def test_concurrent_edit_of_the_same_version_is_stale(storage_module, fake_bucket, monkeypatch,
                                                      before_read):
    """Две правки поверх v1: проходит одна, вторая — PlanStale. Объект
    проигравшей не перезаписывает чужой и в историю не попадает."""
    save = storage_module.save_plan_weeks
    save(fake_bucket, SUB, PID, [{"w": 1}], "v1")
    interleave(fake_bucket, monkeypatch,
               lambda: save(fake_bucket, SUB, PID, [{"w": "rival"}], "rival", base_version=1),
               before_read=before_read)

    with pytest.raises(storage_module.PlanStale):
        save(fake_bucket, SUB, PID, [{"w": "mine"}], "mine", base_version=1)

    assert storage_module.read_plan_state(fake_bucket, SUB, PID)["weeks"] == [{"w": "rival"}]
    assert _plan_object(fake_bucket, 2)["weeks"] == [{"w": "rival"}]
    assert _history(storage_module, fake_bucket) == [2, 1]


def test_version_left_by_a_crash_is_skipped(storage_module, fake_bucket):
    """Объект v2 есть, а манифест на v1 (запрос упал между записями): v2 не
    трогаем, новая версия встаёт следующей, история v2 не показывает."""
    save = storage_module.save_plan_weeks
    save(fake_bucket, SUB, PID, [{"w": 1}], "v1")
    put_json(fake_bucket, f"users/{SUB}/plans/{PID}/v2/plan.json",
             {"version": 2, "supersedes_version": 1, "weeks": [{"w": "сбой"}]})

    saved = save(fake_bucket, SUB, PID, [{"w": 2}], "v2", base_version=1)

    assert saved["version"] == 3
    assert _plan_object(fake_bucket, 3)["supersedes_version"] == 1
    assert _plan_object(fake_bucket, 2)["weeks"] == [{"w": "сбой"}]
    assert storage_module.read_plan_weeks(fake_bucket, SUB, PID) == [{"w": 2}]
    assert _history(storage_module, fake_bucket) == [3, 1]


# ── Профиль и конфиг LLM ──────────────────────────────────────────────────────

def test_concurrent_profile_saves_keep_both_versions(storage_module, fake_bucket, monkeypatch):
    write = storage_module.write_athlete_version
    profile = storage_module.empty_athlete_profile()
    write(fake_bucket, SUB, {**profile, "weight_kg": 70.0}, "v1")
    interleave(fake_bucket, monkeypatch,
               lambda: write(fake_bucket, SUB, {**profile, "weight_kg": 71.0}, "соперник"))

    mine = write(fake_bucket, SUB, {**profile, "weight_kg": 72.0}, "моё")

    assert storage_module.read_athlete_profile(fake_bucket, SUB)[0]["weight_kg"] == 72.0
    history = storage_module.read_athlete_history(fake_bucket, SUB)
    assert [h["weight_kg"] for h in history] == [70.0, 71.0, 72.0]
    assert history[-1]["version"] == mine["version"]


def test_profile_history_skips_a_version_left_by_a_crash(storage_module, fake_bucket):
    write = storage_module.write_athlete_version
    profile = storage_module.empty_athlete_profile()
    write(fake_bucket, SUB, {**profile, "weight_kg": 70.0}, "v1")
    put_json(fake_bucket, f"users/{SUB}/athlete/v2/profile.json",
             {"version": 2, "supersedes_version": 1, "profile": {"weight_kg": 99.0}})

    assert write(fake_bucket, SUB, {**profile, "weight_kg": 71.0}, "v3")["version"] == 3
    history = storage_module.read_athlete_history(fake_bucket, SUB)
    assert [(h["version"], h["weight_kg"]) for h in history] == [(1, 70.0), (3, 71.0)]


def test_concurrent_llm_config_saves_keep_both_versions(storage_module, fake_bucket, monkeypatch):
    write = storage_module.write_llm_config_version
    write(fake_bucket, "openai", "gpt-a", "sk-1")
    interleave(fake_bucket, monkeypatch, lambda: write(fake_bucket, "deepseek", "ds", "sk-2"))

    mine = write(fake_bucket, "openai", "gpt-b", "sk-3")

    assert mine["version"] == 4                  # v3 — объект, проигравший гонку
    assert json.loads(fake_bucket._store["config/llm/v2/config.json"])["model"] == "ds"
    assert storage_module.read_llm_config_full(fake_bucket)["model"] == "gpt-b"


# ── Детали пробежки ───────────────────────────────────────────────────────────

def test_fit_attached_again_is_a_new_version(storage_module, fake_bucket):
    def attach(date):
        token = storage_module.write_parsed_fit_to_tmp(
            fake_bucket, SUB, f"fit {date}".encode(), {"date": date, "summary": {"max_hr": 150}})
        return storage_module.save_run(fake_bucket, SUB, {"id": 7, "dist": 5.0}, fit_token=token)

    attach("2026-11-01")
    assert attach("2026-11-02")["details_available"] is True

    details = storage_module.read_run_details(fake_bucket, SUB, 7)
    assert (details["version"], details["date"], details["supersedes_version"]) == (2, "2026-11-02", 1)
    first = json.loads(fake_bucket._store[storage_module.p_run_details(SUB, 7, 1)])
    assert first["date"] == "2026-11-01"
    assert fake_bucket._store[storage_module.p_run_fit(SUB, 7, 1)] == b"fit 2026-11-01"
    assert fake_bucket._store[storage_module.p_run_fit(SUB, 7, 2)] == b"fit 2026-11-02"
    assert not any(name.startswith("tmp/") for name in fake_bucket._store)


# ── Пробежки и старты (#75) ───────────────────────────────────────────────────

KINDS = {"run": {"id": 1, "date": "2026-11-01", "dist": 5.0, "plan_id": None},
         "race": {"id": 1, "name": "Осенний 10К", "date": "2026-11-01",
                  "dist_label": "10km", "time": "45:00"}}


def _kind(storage_module, kind):
    """save, delete и read для пробежки или старта."""
    return (getattr(storage_module, f"save_{kind}"), getattr(storage_module, f"delete_{kind}"),
            getattr(storage_module, f"read_{kind}s"))


@pytest.fixture
def entities(storage_module, fake_bucket):
    """Номера версий в истории сущности — короче в проверках."""
    def versions(kind, eid=1):
        history = getattr(storage_module, f"read_{kind}_history")(fake_bucket, SUB, eid)
        return [v["version"] for v in history["versions"]]
    return versions


@pytest.mark.parametrize("kind", ["run", "race"])
def test_concurrent_add_and_delete_are_both_kept(storage_module, fake_bucket, monkeypatch, kind):
    """Пробежку добавили с телефона, пока с ноутбука скрывали другую."""
    save, delete, read = _kind(storage_module, kind)
    save(fake_bucket, SUB, KINDS[kind])
    interleave(fake_bucket, monkeypatch, lambda: save(fake_bucket, SUB, {**KINDS[kind], "id": 2}))

    assert delete(fake_bucket, SUB, 1)["deleted"] is True

    entries = {e["id"]: e for e in read(fake_bucket, SUB)}
    assert entries[2] == {**KINDS[kind], "id": 2, "deleted": False, "version": 1}
    assert entries[1]["deleted"] is True and entries[1]["version"] == 2


@pytest.mark.parametrize("kind", ["run", "race"])
@pytest.mark.parametrize("before_read", [False, True], ids=["после чтения", "до чтения"])
def test_concurrent_edits_keep_both_versions(storage_module, fake_bucket, monkeypatch, entities,
                                             kind, before_read):
    """Две правки без сверки: обе — версии, индекс на последней."""
    save, _, read = _kind(storage_module, kind)
    save(fake_bucket, SUB, KINDS[kind])
    rival = []
    interleave(fake_bucket, monkeypatch,
               lambda: rival.append(save(fake_bucket, SUB, {**KINDS[kind], "date": "2026-11-02"})),
               before_read=before_read)

    mine = save(fake_bucket, SUB, {**KINDS[kind], "date": "2026-11-03"})

    assert rival[0]["version"] == 2 and mine["version"] > 2
    assert entities(kind) == [mine["version"], 2, 1]
    assert read(fake_bucket, SUB) == [mine]


@pytest.mark.parametrize("kind", ["run", "race"])
def test_concurrent_edit_of_the_same_version_is_stale(storage_module, fake_bucket, monkeypatch,
                                                      entities, kind):
    save, _, read = _kind(storage_module, kind)
    save(fake_bucket, SUB, KINDS[kind])
    interleave(fake_bucket, monkeypatch,
               lambda: save(fake_bucket, SUB, {**KINDS[kind], "date": "2026-11-02"}, base_version=1))

    with pytest.raises(storage_module.VersionStale):
        save(fake_bucket, SUB, {**KINDS[kind], "date": "2026-11-03"}, base_version=1)

    assert entities(kind) == [2, 1]
    assert read(fake_bucket, SUB)[0]["date"] == "2026-11-02"


def test_index_keeps_a_newer_version_written_in_between(storage_module, fake_bucket, monkeypatch):
    """Моя версия зафиксирована, а до записи индекса соперник выпустил
    следующую и уже поставил её: моя запись индекс назад не откатывает."""
    storage_module.save_run(fake_bucket, SUB, KINDS["run"])
    real, pending = storage_module.update_json, []

    def update_json(bucket, path, change, empty, busy=storage_module.StorageBusy):
        if pending and path == storage_module.p_runs(SUB):
            pending.pop()()
        return real(bucket, path, change, empty, busy)

    monkeypatch.setattr(storage_module, "update_json", update_json)
    pending.append(lambda: storage_module.save_run(fake_bucket, SUB, {**KINDS["run"], "dist": 9.0}))

    mine = storage_module.save_run(fake_bucket, SUB, {**KINDS["run"], "dist": 8.0})

    assert mine["version"] == 2
    assert storage_module.read_runs(fake_bucket, SUB) == [
        {**KINDS["run"], "dist": 9.0, "deleted": False, "version": 3}]


def test_second_delete_keeps_the_first_timestamp(storage_module, fake_bucket, entities):
    storage_module.save_run(fake_bucket, SUB, KINDS["run"])
    first = storage_module.delete_run(fake_bucket, SUB, 1)["deleted_at"]
    assert storage_module.delete_run(fake_bucket, SUB, 1)["deleted_at"] == first
    assert entities("run") == [2, 1]
    assert storage_module.delete_run(fake_bucket, SUB, 404) is None


def test_two_migrations_at_once_write_one_baseline(storage_module, fake_bucket, monkeypatch,
                                                   entities):
    """Миграцию запустили дважды разом: базовая версия у пробежки одна."""
    put_json(fake_bucket, storage_module.p_runs(SUB), [{**KINDS["run"], "deleted": False}])
    rival = []
    interleave(fake_bucket, monkeypatch,
               lambda: rival.append(storage_module.migrate_entity_history(fake_bucket, SUB)))

    mine = storage_module.migrate_entity_history(fake_bucket, SUB)

    assert (rival[0]["runs"]["migrated"], mine["runs"]["migrated"]) == (1, 0)
    assert entities("run") == [1]
    assert storage_module.read_runs(fake_bucket, SUB)[0]["version"] == 1


def test_write_during_migration_keeps_the_old_values(storage_module, fake_bucket, monkeypatch,
                                                     entities):
    """Правка пришла, пока шла миграция: прежние значения — в базовой версии,
    правка — поверх неё, второй базовой версии нет."""
    put_json(fake_bucket, storage_module.p_runs(SUB), [{**KINDS["run"], "deleted": False}])
    interleave(fake_bucket, monkeypatch,
               lambda: storage_module.save_run(fake_bucket, SUB, {**KINDS["run"], "dist": 7.0}))

    storage_module.migrate_entity_history(fake_bucket, SUB)

    assert entities("run") == [2, 1]
    old = json.loads(fake_bucket._store[storage_module.p_run_ver(SUB, 1, 1)])
    assert (old["author"], old["run"]["dist"]) == ("migration", 5.0)
    assert storage_module.read_runs(fake_bucket, SUB)[0]["dist"] == 7.0


def test_run_version_left_by_a_crash_is_skipped(storage_module, fake_bucket, entities):
    """run.json v2 есть, а манифест на v1: v2 не трогаем, правка встаёт v3."""
    storage_module.save_run(fake_bucket, SUB, KINDS["run"])
    put_json(fake_bucket, storage_module.p_run_ver(SUB, 1, 2),
             {"id": 1, "version": 2, "supersedes_version": 1, "status": "ACTIVE",
              "run": {"dist": 99.0}})

    assert storage_module.save_run(fake_bucket, SUB, {**KINDS["run"], "dist": 6.0})["version"] == 3
    assert json.loads(fake_bucket._store[storage_module.p_run_ver(SUB, 1, 3)])["supersedes_version"] == 1
    assert entities("run") == [3, 1]


# ── Реестр пользователей ──────────────────────────────────────────────────────

def _users(bucket):
    return json.loads(bucket._store["users/registry.json"])["users"]


def _events(bucket, sub, event):
    return [n for n in bucket._store if n.startswith("users/events/") and f"-{sub}-{event}-" in n]


def test_two_registrations_at_once_keep_both(storage_module, fake_bucket, monkeypatch):
    storage_module.resolve_user(fake_bucket, ADMIN_TOKEN)
    interleave(fake_bucket, monkeypatch,
               lambda: storage_module.resolve_user(fake_bucket, _token("anna")))

    storage_module.resolve_user(fake_bucket, _token("boris"))

    assert {"admin-sub", "anna", "boris"} <= set(_users(fake_bucket))
    assert len(_events(fake_bucket, "anna", "register")) == 1
    assert len(_events(fake_bucket, "boris", "register")) == 1


def test_same_user_registering_twice_at_once_is_one_record(storage_module, fake_bucket,
                                                           monkeypatch):
    """Две вкладки при первом входе: запись и событие — по одному."""
    storage_module.resolve_user(fake_bucket, ADMIN_TOKEN)
    interleave(fake_bucket, monkeypatch,
               lambda: storage_module.resolve_user(fake_bucket, _token("anna")))

    rec = storage_module.resolve_user(fake_bucket, _token("anna"))

    assert rec == _users(fake_bucket)["anna"]
    assert len(_events(fake_bucket, "anna", "register")) == 1


def test_approval_while_an_athlete_picks_a_coach(storage_module, fake_bucket, monkeypatch):
    """Админ одобряет заявку, пока спортсмен выбирает тренера: обе правки целы,
    событие о выборе тренера — одно."""
    for sub in ("admin", "coach", "athlete", "newbie"):
        storage_module.resolve_user(fake_bucket, ADMIN_TOKEN if sub == "admin" else _token(sub))
    for sub in ("coach", "athlete"):
        storage_module.set_user_status(fake_bucket, sub, "approved", "admin-sub")
    storage_module.set_coach_flag(fake_bucket, "coach", True, "admin-sub")
    interleave(fake_bucket, monkeypatch,
               lambda: storage_module.set_user_status(fake_bucket, "newbie", "approved", "admin-sub"))

    storage_module.set_user_coach(fake_bucket, "athlete", "coach")

    users = _users(fake_bucket)
    assert users["newbie"]["status"] == "approved"
    assert users["athlete"]["coach_sub"] == "coach"
    assert len(_events(fake_bucket, "athlete", "coach_select")) == 1


def test_coach_revoke_sees_a_choice_made_meanwhile(storage_module, fake_bucket, monkeypatch):
    """Спортсмен выбрал тренера, пока админ снимал роль: отвязка видит и
    этот выбор, а не оставляет связь с уже не тренером."""
    for sub in ("admin", "coach", "athlete"):
        storage_module.resolve_user(fake_bucket, ADMIN_TOKEN if sub == "admin" else _token(sub))
    for sub in ("coach", "athlete"):
        storage_module.set_user_status(fake_bucket, sub, "approved", "admin-sub")
    storage_module.set_coach_flag(fake_bucket, "coach", True, "admin-sub")
    interleave(fake_bucket, monkeypatch,
               lambda: storage_module.set_user_coach(fake_bucket, "athlete", "coach"))

    storage_module.set_coach_flag(fake_bucket, "coach", False, "admin-sub")

    users = _users(fake_bucket)
    assert users["coach"]["is_coach"] is False and users["athlete"]["coach_sub"] is None
    assert len(_events(fake_bucket, "athlete", "coach_clear")) == 1


# ── Отметка «прочитано» ───────────────────────────────────────────────────────

def test_read_marker_never_moves_back(storage_module, fake_bucket, monkeypatch):
    """Две вкладки отмечают прочитанное разом: отметка не откатывается назад."""
    ids = [storage_module.append_chat_message(fake_bucket, "athlete", "coach", "coach",
                                              "coach", f"m{i}")["id"] for i in range(3)]
    interleave(fake_bucket, monkeypatch,
               lambda: storage_module.mark_chat_read(fake_bucket, "athlete", "coach",
                                                     "athlete", ids[2]))

    assert storage_module.mark_chat_read(fake_bucket, "athlete", "coach", "athlete", ids[0]) == ids[2]
    assert storage_module.chat_unread(fake_bucket, "athlete", "coach", "athlete") == 0
