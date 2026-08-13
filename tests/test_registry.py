"""Tests for the multi-user registry, user resolution, status transitions, and
legacy migration — against the in-memory fake bucket."""
import pytest


ADMIN_TOKEN = {"sub": "admin-sub", "email": "aabramov77@gmail.com", "name": "Alex"}
USER_TOKEN = {"sub": "u1", "email": "runner@example.com", "name": "Runner"}


@pytest.fixture(autouse=True)
def _reset_registry_cache(main_module):
    """The registry cache is module-global; clear it between tests."""
    main_module._registry_cache["data"] = None
    main_module._registry_cache["ts"] = 0.0
    yield


# ── resolve_user ──────────────────────────────────────────────────────────────

def test_admin_first_login_is_approved(main_module, fake_bucket):
    rec = main_module.resolve_user(fake_bucket, ADMIN_TOKEN)
    assert rec["status"] == "approved"
    assert rec["role"] == "admin"
    assert rec["approved_by"] == "admin-sub"


def test_new_user_is_pending(main_module, fake_bucket):
    rec = main_module.resolve_user(fake_bucket, USER_TOKEN)
    assert rec["status"] == "pending"
    assert rec["role"] == "user"
    assert rec["approved_by"] is None


def test_resolve_is_idempotent(main_module, fake_bucket):
    a = main_module.resolve_user(fake_bucket, USER_TOKEN)
    b = main_module.resolve_user(fake_bucket, USER_TOKEN)
    assert a == b
    reg = main_module.read_registry(fake_bucket)
    assert len(reg["users"]) == 1


def test_register_writes_audit_event(main_module, fake_bucket):
    main_module.resolve_user(fake_bucket, USER_TOKEN)
    events = [b.name for b in fake_bucket.list_blobs(prefix="users/events/")]
    assert any("register" in n for n in events)


def test_registration_closed_at_limit(main_module, fake_bucket, monkeypatch):
    monkeypatch.setattr(main_module, "MAX_PENDING", 2)
    main_module.resolve_user(fake_bucket, {"sub": "a", "email": "a@x.com"})
    main_module.resolve_user(fake_bucket, {"sub": "b", "email": "b@x.com"})
    with pytest.raises(main_module.RegistrationClosed):
        main_module.resolve_user(fake_bucket, {"sub": "c", "email": "c@x.com"})


def test_admin_bypasses_registration_limit(main_module, fake_bucket, monkeypatch):
    monkeypatch.setattr(main_module, "MAX_PENDING", 0)
    # non-admin blocked
    with pytest.raises(main_module.RegistrationClosed):
        main_module.resolve_user(fake_bucket, USER_TOKEN)
    # admin still gets in
    rec = main_module.resolve_user(fake_bucket, ADMIN_TOKEN)
    assert rec["status"] == "approved"


# ── set_user_status ───────────────────────────────────────────────────────────

def test_approve_and_reject(main_module, fake_bucket):
    main_module.resolve_user(fake_bucket, USER_TOKEN)
    rec = main_module.set_user_status(fake_bucket, "u1", "approved", "admin-sub")
    assert rec["status"] == "approved" and rec["approved_by"] == "admin-sub"

    rec2 = main_module.set_user_status(fake_bucket, "u1", "rejected", "admin-sub")
    assert rec2["status"] == "rejected"

    # audit events recorded
    events = [b.name for b in fake_bucket.list_blobs(prefix="users/events/")]
    assert any("approved" in n for n in events)
    assert any("rejected" in n for n in events)


def test_set_status_unknown_user(main_module, fake_bucket):
    assert main_module.set_user_status(fake_bucket, "ghost", "approved", "admin-sub") is None


# ── registry cache ────────────────────────────────────────────────────────────

def test_write_registry_updates_cache(main_module, fake_bucket):
    main_module.resolve_user(fake_bucket, ADMIN_TOKEN)
    # cache now warm; a fresh read returns the admin without hitting a cold load
    reg = main_module.read_registry(fake_bucket)
    assert "admin-sub" in reg["users"]


# ── legacy migration ──────────────────────────────────────────────────────────

def test_migrate_legacy_copies_and_is_idempotent(main_module, fake_bucket):
    # seed legacy global objects
    fake_bucket.blob("runs.json").upload_from_string('[{"id": 1, "dist": 10}]')
    fake_bucket.blob("races.json").upload_from_string('[{"id": 2}]')
    fake_bucket.blob("plan/v1/plan.json").upload_from_string('{"weeks": [{"w": 1}]}')
    fake_bucket.blob("plan/manifest.json").upload_from_string(
        '{"current_version": 1, "gcs_object_path": "plan/v1/plan.json"}')

    report = main_module.migrate_legacy_to_user(fake_bucket, "admin-sub")
    assert f"users/admin-sub/runs.json" in report["copied"]
    assert fake_bucket.blob("users/admin-sub/runs.json").exists()
    assert fake_bucket.blob("users/admin-sub/races.json").exists()
    assert fake_bucket.blob("users/admin-sub/plan/v1/plan.json").exists()

    # profile seeded with Alexander's historical race constants
    prof = main_module.read_profile(fake_bucket, "admin-sub")
    assert prof["race_date"] == "2026-08-09" and prof["target_time"] == "1:40"

    # per-user plan manifest points INTO the user namespace (not the legacy path)
    import json
    man = json.loads(fake_bucket.blob("users/admin-sub/plan/manifest.json").download_as_text())
    assert man["gcs_object_path"] == "users/admin-sub/plan/v1/plan.json"

    # legacy originals untouched (no physical delete)
    assert fake_bucket.blob("runs.json").exists()

    # second run: everything skipped, nothing errors
    report2 = main_module.migrate_legacy_to_user(fake_bucket, "admin-sub")
    assert report2["copied"] == []
    assert report2["errors"] == []
    assert any("уже есть" in s for s in report2["skipped"])
