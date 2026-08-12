"""Tests for GCS-bound helpers in main.py, against an in-memory fake bucket.

Multi-user: all data helpers are namespaced by `sub`. The fake (conftest
FakeBucket) implements only the blob/list_blobs/copy_blob surface main.py uses.
"""
import time

import pytest

SUB = "user-abc"
SUB2 = "user-xyz"


# ── runs / races round-trips (per-user namespace) ─────────────────────────────

def test_runs_roundtrip(main_module, fake_bucket):
    assert main_module.read_runs(fake_bucket, SUB) == []
    runs = [{"id": 1, "date": "2026-05-01", "dist": 10.0, "deleted": False}]
    main_module.write_runs(fake_bucket, SUB, runs)
    assert main_module.read_runs(fake_bucket, SUB) == runs
    # isolation: another user sees nothing
    assert main_module.read_runs(fake_bucket, SUB2) == []


def test_races_roundtrip(main_module, fake_bucket):
    assert main_module.read_races(fake_bucket, SUB) == []
    races = [{"id": 7, "name": "HM", "date": "2026-05-01", "dist_label": "HM",
              "time": "1:40:00", "deleted": False}]
    main_module.write_races(fake_bucket, SUB, races)
    assert main_module.read_races(fake_bucket, SUB) == races
    assert main_module.read_races(fake_bucket, SUB2) == []


def test_runs_namespaced_paths(main_module, fake_bucket):
    main_module.write_runs(fake_bucket, SUB, [{"id": 1}])
    assert f"users/{SUB}/runs.json" in fake_bucket._store
    assert "runs.json" not in fake_bucket._store   # no global write


# ── plan versioning (per-user, immutable versions) ────────────────────────────

def test_plan_versioning(main_module, fake_bucket):
    w1 = [{"w": 1, "type": "dev", "sun": "10км"}]
    main_module.write_plan_version(fake_bucket, SUB, 1, w1, "seed")
    man = main_module.read_plan_manifest(fake_bucket, SUB)
    assert man["current_version"] == 1
    v1 = main_module.read_plan_version(fake_bucket, man["gcs_object_path"])
    assert v1["weeks"] == w1

    w2 = [{"w": 1, "type": "dev", "sun": "12км"}]
    main_module.write_plan_version(fake_bucket, SUB, 2, w2, "edit")
    man2 = main_module.read_plan_manifest(fake_bucket, SUB)
    assert man2["current_version"] == 2
    # v1 still readable — immutable history preserved
    assert main_module.read_plan_version(fake_bucket, f"users/{SUB}/plan/v1/plan.json")["weeks"] == w1
    # other user has no plan
    assert main_module.read_plan_manifest(fake_bucket, SUB2) is None


# ── LLM config versioning (GLOBAL — shared key, admin-managed) ────────────────

def test_llm_config_versioning(main_module, fake_bucket):
    assert main_module.read_llm_config_full(fake_bucket) is None
    main_module.write_llm_config_version(fake_bucket, "deepseek", "deepseek-chat",
                                         "sk-secret-1234567890")
    cfg = main_module.read_llm_config_full(fake_bucket)
    assert cfg["provider"] == "deepseek"
    assert cfg["api_key"] == "sk-secret-1234567890"
    assert cfg["version"] == 1
    # global path, not per-user
    assert "config/llm/v1/config.json" in fake_bucket._store

    main_module.write_llm_config_version(fake_bucket, "anthropic", "claude-x", "sk-ant-999")
    assert main_module.read_llm_config_full(fake_bucket)["version"] == 2


# ── advice round-trip + daily usage ───────────────────────────────────────────

def test_advice_roundtrip(main_module, fake_bucket):
    assert main_module.read_latest_advice(fake_bucket, SUB) is None
    ctx = {"last_runs": [{"id": 11}, {"id": 12}], "plan_version": 3}
    rec = {"assessment": "ok", "adjustments": [], "warnings": []}
    main_module.write_advice_version(fake_bucket, SUB, rec, ctx, "deepseek", "deepseek-chat",
                                     100, 50, llm_config_version=1)
    latest = main_module.read_latest_advice(fake_bucket, SUB)
    assert latest["recommendation"] == rec
    assert latest["based_on_runs"] == [11, 12]
    assert main_module.read_latest_advice(fake_bucket, SUB2) is None


def test_advice_usage_counter(main_module, fake_bucket):
    assert main_module.read_advice_usage(fake_bucket, SUB)["count"] == 0
    main_module.increment_advice_usage(fake_bucket, SUB)
    main_module.increment_advice_usage(fake_bucket, SUB)
    assert main_module.read_advice_usage(fake_bucket, SUB)["count"] == 2
    # separate user unaffected
    assert main_module.read_advice_usage(fake_bucket, SUB2)["count"] == 0


# ── tmp cleanup (per-user, age-based) ─────────────────────────────────────────

def test_cleanup_old_tmp(main_module, fake_bucket):
    now = int(time.time())
    old, fresh = now - 100_000, now
    fake_bucket.blob(f"tmp/{SUB}/{old}-aaaa/activity.fit").upload_from_string(b"x")
    fake_bucket.blob(f"tmp/{SUB}/{old}-aaaa/details.json").upload_from_string("{}")
    fake_bucket.blob(f"tmp/{SUB}/{fresh}-bbbb/details.json").upload_from_string("{}")
    # another user's old tmp must NOT be touched
    fake_bucket.blob(f"tmp/{SUB2}/{old}-cccc/details.json").upload_from_string("{}")

    deleted = main_module.cleanup_old_tmp(fake_bucket, SUB, max_age_hours=24)
    assert deleted == 2
    names = [b.name for b in fake_bucket.list_blobs(prefix="tmp/")]
    assert all("aaaa" not in n for n in names)
    assert any("bbbb" in n for n in names)
    assert any("cccc" in n for n in names)   # SUB2 untouched


# ── FIT tmp → permanent promotion (per-user) ──────────────────────────────────

def test_parse_fit_tmp_then_attach(main_module, fake_bucket):
    parsed = {
        "date": "2026-05-01",
        "summary": {"dist_km": 3.0, "avg_cadence": 176, "max_hr": 170,
                    "total_ascent_m": 24, "calories": 210},
        "laps": [{"lap": 1, "pace": "5:00"}],
        "samples": {"t_offset_sec": [0, 5], "hr": [120, 130],
                    "pace_sec_per_km": [300, 300], "altitude_m": [60, 61]},
    }
    token = main_module.write_parsed_fit_to_tmp(fake_bucket, SUB, b"FITBYTES", parsed)
    assert fake_bucket.blob(f"tmp/{SUB}/{token}/activity.fit").exists()

    run = {"id": 123, "date": "2026-05-01"}
    main_module.attach_fit_details_to_run(fake_bucket, SUB, run, token)

    assert run["details_available"] is True
    assert run["avg_cadence"] == 176 and run["calories"] == 210
    assert fake_bucket.blob(f"users/{SUB}/runs/123/v1/activity.fit").exists()
    assert fake_bucket.blob(f"users/{SUB}/runs/123/v1/details.json").exists()
    assert fake_bucket.blob(f"users/{SUB}/runs/123/manifest.json").exists()
    # tmp cleaned
    assert not fake_bucket.blob(f"tmp/{SUB}/{token}/activity.fit").exists()
    # read back via per-user manifest
    details = main_module.read_run_details(fake_bucket, SUB, 123)
    assert details["summary"]["avg_cadence"] == 176


def test_attach_fails_on_missing_token(main_module, fake_bucket):
    with pytest.raises(ValueError):
        main_module.attach_fit_details_to_run(fake_bucket, SUB, {"id": 1}, "nonexistent")


def test_read_run_details_lazy_legacy_fallback(main_module, fake_bucket):
    """Admin's migrated run: details live at the legacy global path; first read
    lazily copies them into the user namespace."""
    fake_bucket.blob("runs/999/v1/activity.fit").upload_from_string(b"FIT")
    fake_bucket.blob("runs/999/v1/details.json").upload_from_string(
        '{"summary": {"avg_cadence": 165}, "laps": [], "samples": {}}')
    fake_bucket.blob("runs/999/manifest.json").upload_from_string(
        '{"current_version": 1, "gcs_object_path": "runs/999/v1/details.json"}')

    details = main_module.read_run_details(fake_bucket, SUB, 999)
    assert details["summary"]["avg_cadence"] == 165
    # now materialized in the user namespace
    assert fake_bucket.blob(f"users/{SUB}/runs/999/v1/details.json").exists()
    assert fake_bucket.blob(f"users/{SUB}/runs/999/manifest.json").exists()
