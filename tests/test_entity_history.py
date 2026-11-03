"""Пробежки и старты — сущности с версиями (#75).

Создание, правка, скрытие и восстановление — новые версии; runs.json и
races.json — производный индекс текущих версий. Гонки — в
test_write_preconditions.py.
"""
import json

import pytest

from conftest import FakeRequest, put_json

SUB = "u-75"
EMAIL = "runner@example.com"
RUN = {"id": 1, "date": "2026-11-01", "dist": 10.0, "type": "easy", "time": "55:00",
       "pace": "5:30", "hr": 140, "feel": "good", "notes": "", "plan_id": None}
RACE = {"id": 1, "name": "Осенний 10К", "date": "2026-11-01", "dist_label": "10km",
        "time": "45:00"}
FIELDS = {"run": RUN, "race": RACE}
FIT = {"date": "2026-11-01", "laps": [], "samples": {},
       "summary": {"max_hr": 171, "avg_cadence": 176, "total_ascent_m": 40, "calories": 600}}


def _fn(storage_module, action, kind):
    return getattr(storage_module, f"{action}_{kind}")


def _read(storage_module, bucket, kind):
    return getattr(storage_module, f"read_{kind}s")(bucket, SUB)


def _payload(storage_module, bucket, kind, version, eid=1):
    path = getattr(storage_module, f"p_{kind}_ver")(SUB, eid, version)
    return json.loads(bucket._store[path])


def _manifest(storage_module, bucket, kind, eid=1):
    return json.loads(bucket._store[getattr(storage_module, f"p_{kind}_manifest")(SUB, eid)])


def _history(storage_module, bucket, kind, eid=1):
    return getattr(storage_module, f"read_{kind}_history")(bucket, SUB, eid)


def _versions(storage_module, bucket, kind, eid=1):
    return [v["version"] for v in _history(storage_module, bucket, kind, eid)["versions"]]


def _fit_token(storage_module, bucket):
    return storage_module.write_parsed_fit_to_tmp(bucket, SUB, b"FIT", FIT)


# ── Создание и правка ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["run", "race"])
def test_create_is_the_first_version(storage_module, fake_bucket, kind):
    entry = _fn(storage_module, "save", kind)(fake_bucket, SUB, FIELDS[kind], created_by=EMAIL)

    assert entry == {**FIELDS[kind], "deleted": False, "version": 1}
    assert _read(storage_module, fake_bucket, kind) == [entry]
    payload = _payload(storage_module, fake_bucket, kind, 1)
    assert {k: payload[k] for k in ("status", "supersedes_version", "change_reason",
                                    "author", "created_by")} == \
        {"status": "ACTIVE", "supersedes_version": None, "change_reason": "создание",
         "author": "athlete", "created_by": EMAIL}
    assert payload[kind] == {k: v for k, v in FIELDS[kind].items() if k != "id"}
    manifest = _manifest(storage_module, fake_bucket, kind)
    assert (manifest["current_version"], manifest["status"]) == (1, "ACTIVE")
    assert manifest["gcs_object_path"].endswith(f"/v1/{kind}.json")


@pytest.mark.parametrize("kind", ["run", "race"])
def test_edit_is_a_new_version_and_the_old_one_stays(storage_module, fake_bucket, kind):
    save = _fn(storage_module, "save", kind)
    save(fake_bucket, SUB, FIELDS[kind])
    entry = save(fake_bucket, SUB, {**FIELDS[kind], "date": "2026-11-02"},
                 change_reason="опечатка в дате")

    assert (entry["version"], entry["date"]) == (2, "2026-11-02")
    assert _read(storage_module, fake_bucket, kind) == [entry]
    assert _payload(storage_module, fake_bucket, kind, 1)[kind]["date"] == "2026-11-01"
    assert _payload(storage_module, fake_bucket, kind, 2)["supersedes_version"] == 1
    history = _history(storage_module, fake_bucket, kind)
    assert history[f"{kind}_id"] == 1 and history["has_more"] is False
    assert [(v["version"], v["change_reason"], v[kind]["date"]) for v in history["versions"]] == \
        [(2, "опечатка в дате", "2026-11-02"), (1, "создание", "2026-11-01")]


@pytest.mark.parametrize("kind", ["run", "race"])
def test_edit_without_changes_writes_nothing(storage_module, fake_bucket, kind):
    """Повтор запроса после сбоя или двойной клик новой версии не дают."""
    save = _fn(storage_module, "save", kind)
    first = save(fake_bucket, SUB, FIELDS[kind])
    before = dict(fake_bucket._store)

    assert save(fake_bucket, SUB, FIELDS[kind]) == first
    assert fake_bucket._store == before


def test_edit_keeps_the_fit_and_the_plan(storage_module, fake_bucket):
    """Правка без FIT: поля из FIT, детали и план остаются прежними."""
    storage_module.save_run(fake_bucket, SUB, {**RUN, "plan_id": "plan-1"},
                            fit_token=_fit_token(storage_module, fake_bucket))
    entry = storage_module.save_run(fake_bucket, SUB, {**RUN, "dist": 10.5},
                                    default_plan_id="plan-2")

    assert (entry["version"], entry["dist"], entry["plan_id"]) == (2, 10.5, "plan-1")
    assert (entry["details_available"], entry["max_hr"], entry["calories"]) == (True, 171, 600)
    manifest = _manifest(storage_module, fake_bucket, "run")
    assert manifest["details_object_path"] == storage_module.p_run_details(SUB, 1, 1)
    assert storage_module.read_run_details(fake_bucket, SUB, 1)["summary"]["max_hr"] == 171
    assert not fake_bucket.blob(storage_module.p_run_details(SUB, 1, 2)).exists()
    assert [(v["version"], v["fit_attached"]) for v in
            _history(storage_module, fake_bucket, "run")["versions"]] == [(2, False), (1, True)]


def test_new_run_goes_into_the_default_plan(storage_module, fake_bucket):
    entry = storage_module.save_run(fake_bucket, SUB, RUN, default_plan_id="active")
    assert entry["plan_id"] == "active"


def test_run_with_fit_is_one_version(storage_module, fake_bucket):
    """Пробежка и её FIT — одна папка версии: детали без пробежки не остаются."""
    entry = storage_module.save_run(fake_bucket, SUB, RUN,
                                    fit_token=_fit_token(storage_module, fake_bucket))
    assert entry["version"] == 1 and entry["details_available"] is True
    assert sorted(n.rsplit("/", 1)[-1] for n in fake_bucket._store
                  if n.startswith(f"users/{SUB}/runs/1/v1/")) == \
        ["activity.fit", "details.json", "run.json"]
    payload = _payload(storage_module, fake_bucket, "run", 1)
    assert payload["fit_object_path"] == storage_module.p_run_fit(SUB, 1, 1)
    assert not any(n.startswith("tmp/") for n in fake_bucket._store)


# ── Скрытие и восстановление ──────────────────────────────────────────────────

@pytest.mark.parametrize("kind", ["run", "race"])
def test_hide_and_restore_are_versions(storage_module, fake_bucket, kind):
    _fn(storage_module, "save", kind)(fake_bucket, SUB, FIELDS[kind])

    hidden = _fn(storage_module, "delete", kind)(fake_bucket, SUB, 1, deleted_by=EMAIL)
    assert (hidden["version"], hidden["deleted"]) == (2, True) and hidden["deleted_at"]
    payload = _payload(storage_module, fake_bucket, kind, 2)
    assert (payload["status"], payload["deleted_by"], payload[kind]) == \
        ("DELETED", EMAIL, _payload(storage_module, fake_bucket, kind, 1)[kind])

    restored = _fn(storage_module, "restore", kind)(fake_bucket, SUB, 1, restored_by=EMAIL)
    assert restored == {**FIELDS[kind], "deleted": False, "version": 3}
    assert _read(storage_module, fake_bucket, kind) == [restored]
    assert _payload(storage_module, fake_bucket, kind, 3)["deleted_at"] is None

    assert _fn(storage_module, "restore", kind)(fake_bucket, SUB, 1) == restored   # без записи
    assert [(v["version"], v["status"], v["change_reason"]) for v in
            _history(storage_module, fake_bucket, kind)["versions"]] == \
        [(3, "ACTIVE", "восстановление"), (2, "DELETED", "скрытие"), (1, "ACTIVE", "создание")]


@pytest.mark.parametrize("kind", ["run", "race"])
def test_a_hidden_one_is_not_edited(storage_module, fake_bucket, kind):
    save = _fn(storage_module, "save", kind)
    save(fake_bucket, SUB, FIELDS[kind])
    _fn(storage_module, "delete", kind)(fake_bucket, SUB, 1)

    with pytest.raises(storage_module.EntityDeleted):
        save(fake_bucket, SUB, {**FIELDS[kind], "date": "2026-11-02"})
    assert _versions(storage_module, fake_bucket, kind) == [2, 1]


@pytest.mark.parametrize("kind", ["run", "race"])
def test_unknown_id_has_no_history_and_nothing_to_restore(storage_module, fake_bucket, kind):
    assert _fn(storage_module, "restore", kind)(fake_bucket, SUB, 404) is None
    assert _fn(storage_module, "delete", kind)(fake_bucket, SUB, 404) is None
    assert _history(storage_module, fake_bucket, kind, 404) is None
    assert fake_bucket._store == {}


@pytest.mark.parametrize("kind,stale", [("run", "RunStale"), ("race", "RaceStale")])
def test_edit_over_an_old_version_is_stale(storage_module, fake_bucket, kind, stale):
    save = _fn(storage_module, "save", kind)
    save(fake_bucket, SUB, FIELDS[kind])
    save(fake_bucket, SUB, {**FIELDS[kind], "date": "2026-11-02"}, base_version=1)

    with pytest.raises(getattr(storage_module, stale)):
        save(fake_bucket, SUB, {**FIELDS[kind], "date": "2026-11-03"}, base_version=1)
    assert save(fake_bucket, SUB, {**FIELDS[kind], "date": "2026-11-03"},
                base_version=2)["version"] == 3


# ── Миграция записей до #75 ───────────────────────────────────────────────────

def _old_details(storage_module, bucket, run_id):
    """Детали FIT и манифест в формате до #75: манифест указывает на детали."""
    details_path = storage_module.p_run_details(SUB, run_id, 1)
    put_json(bucket, details_path, {"version": 1, "supersedes_version": None,
                                    "created_at": "2026-10-01T10:00:00Z", "summary": {"max_hr": 160}})
    put_json(bucket, storage_module.p_run_fit(SUB, run_id, 1), {"fit": True})
    put_json(bucket, storage_module.p_run_manifest(SUB, run_id),
             {"current_version": 1, "gcs_object_path": details_path})


def _old_data(storage_module, bucket):
    _old_details(storage_module, bucket, 1)
    put_json(bucket, storage_module.p_runs(SUB), [
        {**RUN, "id": 2, "deleted": True, "deleted_at": "2026-10-05T00:00:00Z"},
        {**RUN, "details_available": True, "max_hr": 160, "deleted": False},
    ])
    put_json(bucket, storage_module.p_races(SUB), [{**RACE, "deleted": False}])


def test_migration_turns_index_entries_into_versions(storage_module, fake_bucket):
    _old_data(storage_module, fake_bucket)

    report = storage_module.migrate_entity_history(fake_bucket, SUB)

    assert report == {"runs": {"migrated": 2, "entries": 2, "changed": True},
                      "races": {"migrated": 1, "entries": 1, "changed": True}}
    # пробежка с деталями до #75: базовая версия v2 поверх v1 деталей
    base = _payload(storage_module, fake_bucket, "run", 2)
    assert (base["supersedes_version"], base["author"], base["created_by"]) == \
        (1, "migration", "auto-migrate")
    assert base["details_object_path"] == storage_module.p_run_details(SUB, 1, 1)
    assert base["fit_object_path"] == storage_module.p_run_fit(SUB, 1, 1)
    assert base["run"]["max_hr"] == 160 and "deleted" not in base["run"]
    assert storage_module.read_run_details(fake_bucket, SUB, 1)["summary"] == {"max_hr": 160}
    versions = _history(storage_module, fake_bucket, "run")["versions"]
    assert [(v["version"], v["change_reason"], v["run"] is None) for v in versions] == \
        [(2, "перенос из runs.json (#75)", False), (1, "детали FIT", True)]
    # скрытая — базовая версия DELETED с прежним deleted_at
    hidden = _payload(storage_module, fake_bucket, "run", 1, eid=2)
    assert (hidden["status"], hidden["deleted_at"]) == ("DELETED", "2026-10-05T00:00:00Z")
    # индекс пересобран: id от новых к старым, у каждой записи version
    assert [(e["id"], e["version"], e["deleted"]) for e in
            storage_module.read_runs(fake_bucket, SUB)] == [(2, 1, True), (1, 2, False)]
    assert storage_module.read_races(fake_bucket, SUB) == [{**RACE, "deleted": False, "version": 1}]


def test_migration_again_writes_nothing(storage_module, fake_bucket):
    _old_data(storage_module, fake_bucket)
    storage_module.migrate_entity_history(fake_bucket, SUB)
    before = dict(fake_bucket._store)

    report = storage_module.migrate_entity_history(fake_bucket, SUB)

    assert report["runs"] == {"migrated": 0, "entries": 2, "changed": False}
    assert fake_bucket._store == before


@pytest.mark.parametrize("kind", ["run", "race"])
def test_first_write_moves_an_old_entry_into_versions(storage_module, fake_bucket, kind):
    """До миграции правка не теряет прежних значений: сначала базовая версия."""
    put_json(fake_bucket, getattr(storage_module, f"p_{kind}s")(SUB),
             [{**FIELDS[kind], "deleted": False}])

    _fn(storage_module, "save", kind)(fake_bucket, SUB, {**FIELDS[kind], "date": "2026-11-02"})

    assert _versions(storage_module, fake_bucket, kind) == [2, 1]
    base = _payload(storage_module, fake_bucket, kind, 1)
    assert (base["author"], base[kind]["date"]) == ("migration", "2026-11-01")
    assert _read(storage_module, fake_bucket, kind)[0]["version"] == 2


def test_hiding_an_old_entry_keeps_it_in_history(storage_module, fake_bucket):
    put_json(fake_bucket, storage_module.p_runs(SUB), [{**RUN, "deleted": False}])
    assert storage_module.delete_run(fake_bucket, SUB, 1)["version"] == 2
    assert [v["status"] for v in _history(storage_module, fake_bucket, "run")["versions"]] == \
        ["DELETED", "ACTIVE"]


def test_admins_legacy_details_are_linked(storage_module, fake_bucket):
    """Детали админа в корне бакета (до мультипользовательского режима):
    миграция копирует их, и базовая версия на них ссылается."""
    storage_module.resolve_user(fake_bucket, {"sub": SUB, "email": "aabramov77@gmail.com",
                                              "email_verified": True})
    put_json(fake_bucket, "runs/5/v1/details.json", {"summary": {"avg_hr": 151}})
    put_json(fake_bucket, "runs/5/manifest.json",
             {"current_version": 1, "gcs_object_path": "runs/5/v1/details.json"})
    put_json(fake_bucket, storage_module.p_runs(SUB),
             [{**RUN, "id": 5, "details_available": True, "deleted": False}])

    assert storage_module.migrate_entity_history(fake_bucket, SUB)["runs"]["migrated"] == 1

    assert _manifest(storage_module, fake_bucket, "run", 5)["details_object_path"] == \
        storage_module.p_run_details(SUB, 5, 1)
    assert storage_module.read_run_details(fake_bucket, SUB, 5) == {"summary": {"avg_hr": 151}}
    assert _versions(storage_module, fake_bucket, "run", 5) == [2, 1]


def test_old_single_plan_binds_runs_with_versions(storage_module, fake_bucket):
    """Перенос одиночного плана (#25) привязывает пробежки новой версией:
    правку индекса стёрла бы пересборка."""
    put_json(fake_bucket, storage_module.p_profile(SUB), {"race_name": "HM"})
    put_json(fake_bucket, storage_module.p_runs(SUB), [{**RUN, "deleted": False}])

    plan_id = storage_module.read_plans_index(fake_bucket, SUB)["active_plan_id"]

    assert storage_module.read_runs(fake_bucket, SUB)[0]["plan_id"] == plan_id
    assert _versions(storage_module, fake_bucket, "run") == [2, 1]
    assert _payload(storage_module, fake_bucket, "run", 2)["change_reason"] == \
        "привязка к плану (#25)"
    storage_module.rebuild_index(fake_bucket, storage_module.RUNS, SUB)
    assert storage_module.read_runs(fake_bucket, SUB)[0]["plan_id"] == plan_id


# ── Индекс ────────────────────────────────────────────────────────────────────

def test_rebuild_restores_a_lagging_index(storage_module, fake_bucket):
    """Индекс отстал (сбой между версией и индексом): пересборка его чинит,
    а запись без версий оставляет как есть."""
    for run_id in (1, 2):
        storage_module.save_run(fake_bucket, SUB, {**RUN, "id": run_id})
    current = storage_module.save_run(fake_bucket, SUB, {**RUN, "dist": 12.0})
    unmigrated = {**RUN, "id": 3, "deleted": False}
    put_json(fake_bucket, storage_module.p_runs(SUB),
             [unmigrated, {**RUN, "deleted": False, "version": 1}])

    assert storage_module.rebuild_index(fake_bucket, storage_module.RUNS, SUB) == \
        {"entries": 3, "changed": True}
    runs = storage_module.read_runs(fake_bucket, SUB)
    assert [r["id"] for r in runs] == [3, 2, 1]
    assert runs[0] == unmigrated and runs[2] == current
    assert storage_module.rebuild_index(fake_bucket, storage_module.RUNS, SUB)["changed"] is False


def test_details_reader_understands_both_manifests(storage_module, fake_bucket):
    _old_details(storage_module, fake_bucket, 1)
    assert storage_module.read_run_details(fake_bucket, SUB, 1)["summary"] == {"max_hr": 160}
    storage_module.save_run(fake_bucket, SUB, {**RUN, "id": 2})
    assert storage_module.read_run_details(fake_bucket, SUB, 2) is None


# ── HTTP ──────────────────────────────────────────────────────────────────────

def _call(api, method, path, body=None, **kw):
    response = api(FakeRequest(method, path, json_body=body, args=kw.pop("args", None)), **kw)
    return json.loads(response[0]), response[1]


@pytest.mark.parametrize("kind,root", [("run", "/"), ("race", "/races")])
def test_edit_hide_restore_and_history_over_http(api, kind, root):
    base = "/runs/1" if kind == "run" else "/races/1"
    body = {k: v for k, v in FIELDS[kind].items() if v is not None}

    assert _call(api, "POST", root, body)[0]["version"] == 1
    edited, code = _call(api, "POST", root, {**body, "date": "2026-11-02", "change_reason": "дата"})
    assert (code, edited["version"]) == (201, 2)

    history, code = _call(api, "GET", f"{base}/history")
    assert code == 200 and [v["version"] for v in history["versions"]] == [2, 1]
    assert history["versions"][0]["change_reason"] == "дата"
    assert EMAIL not in json.dumps(history, ensure_ascii=False)   # почта — только в объекте

    assert _call(api, "DELETE", root, args={"id": "1"})[1] == 200
    assert _call(api, "POST", root, body) == ({"error": f"{kind}_deleted"}, 409)
    restored, code = _call(api, "POST", f"{base}/restore")
    assert (code, restored["deleted"], restored["version"]) == (200, False, 4)
    assert [e["id"] for e in _call(api, "GET", root)[0]] == [1]


@pytest.mark.parametrize("kind,root", [("run", "/"), ("race", "/races")])
def test_stale_and_invalid_base_version_over_http(api, kind, root):
    body = {k: v for k, v in FIELDS[kind].items() if v is not None}
    _call(api, "POST", root, body)
    _call(api, "POST", root, {**body, "date": "2026-11-02"})

    assert _call(api, "POST", root, {**body, "base_version": 1}) == ({"error": f"{kind}_stale"}, 409)
    assert _call(api, "POST", root, {**body, "base_version": "2"}) == \
        ({"error": "invalid base_version"}, 400)
    assert _call(api, "POST", root, {**body, "base_version": 2})[1] == 201


@pytest.mark.parametrize("path", ["/runs/9/history", "/races/9/history"])
def test_unknown_history_is_404(api, path):
    assert _call(api, "GET", path)[1] == 404


@pytest.mark.parametrize("path", ["/runs/9/restore", "/races/9/restore"])
def test_unknown_restore_is_404(api, path):
    assert _call(api, "POST", path)[1] == 404


def test_someone_elses_history_is_not_found(api):
    """id задаёт клиент, но путь строится из sub токена: чужой пробежки нет."""
    _call(api, "POST", "/", {"id": 1, "date": "2026-11-01", "dist": 5}, sub="u2",
          email="u2@example.com")
    assert _call(api, "GET", "/runs/1/history")[1] == 404
    assert _call(api, "POST", "/runs/1/restore")[1] == 404


@pytest.mark.parametrize("kind,root,hidden", [("run", "/", "/runs/hidden"),
                                              ("race", "/races", "/races/hidden")])
def test_hidden_list_shows_only_own_hidden_ones(api, kind, root, hidden):
    """Список скрытых (#76) — явное чтение: обычный список их не отдаёт."""
    body = {k: v for k, v in FIELDS[kind].items() if v is not None}
    for eid in (1, 2, 3):
        _call(api, "POST", root, {**body, "id": eid})
    for eid in (2, 1):
        _call(api, "DELETE", root, args={"id": str(eid)})
    _call(api, "POST", root, {**body, "id": 9}, sub="u2", email="u2@example.com")
    _call(api, "DELETE", root, args={"id": "9"}, sub="u2", email="u2@example.com")

    listed, code = _call(api, "GET", hidden)
    assert code == 200 and sorted(e["id"] for e in listed) == [1, 2]
    assert all(e["deleted"] and e["deleted_at"] for e in listed)
    assert [e["id"] for e in _call(api, "GET", root)[0]] == [3]

    _call(api, "POST", f"/{kind}s/1/restore")
    assert [e["id"] for e in _call(api, "GET", hidden)[0]] == [2]


def test_hidden_list_puts_the_latest_hidden_first(api, fake_bucket):
    """Порядок — по deleted_at, а не по месту в индексе. Время задано явно:
    два скрытия подряд на Windows могут получить одну и ту же отметку."""
    _call(api, "GET", "/")                                    # регистрирует u1
    put_json(fake_bucket, "users/u1/runs.json", [
        {**RUN, "id": 1, "deleted": True, "deleted_at": "2026-11-01T10:00:00Z"},
        {**RUN, "id": 2, "deleted": True, "deleted_at": "2026-11-03T10:00:00Z"},
        {**RUN, "id": 3, "deleted": False},
        {**RUN, "id": 4, "deleted": True},                    # скрыта до отметок времени
    ])
    assert [e["id"] for e in _call(api, "GET", "/runs/hidden")[0]] == [2, 1, 4]


def test_migrate_history_is_for_the_admin(api, patched_api, fake_bucket):
    _call(api, "GET", "/")                                    # регистрирует u1
    put_json(fake_bucket, "users/u1/runs.json", [{**RUN, "deleted": False}])
    assert _call(api, "POST", "/admin/migrate-history")[1] == 403

    report, code = _call(api, "POST", "/admin/migrate-history", sub="admin-sub",
                         email="aabramov77@gmail.com")
    assert code == 200 and report["u1"]["runs"]["migrated"] == 1
    assert patched_api.read_runs(fake_bucket, "u1")[0]["version"] == 1
