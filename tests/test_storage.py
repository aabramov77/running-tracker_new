"""Tests for GCS-bound helpers in main.py, against an in-memory fake bucket.

These exercise the read/write/version/cleanup logic that the pure-function tests
can't reach — without touching real Google Cloud Storage. The fake (conftest
FakeBucket) implements only the blob/list_blobs/copy_blob surface main.py uses.
"""
import json
import time


# ── runs / races round-trips (helpers use get_storage_client internally) ──────

def test_runs_roundtrip(patched_main):
    assert patched_main.read_runs() == []          # empty store → []
    runs = [{"id": 1, "date": "2026-05-01", "dist": 10.0, "deleted": False}]
    patched_main.write_runs(runs)
    assert patched_main.read_runs() == runs


def test_races_roundtrip(patched_main):
    assert patched_main.read_races() == []
    races = [{"id": 7, "name": "HM", "date": "2026-05-01", "dist_label": "HM",
              "time": "1:40:00", "deleted": False}]
    patched_main.write_races(races)
    assert patched_main.read_races() == races


# ── plan versioning (immutable versions + mutable manifest) ───────────────────

def test_plan_versioning(main_module, fake_bucket):
    w1 = [{"w": 1, "type": "dev", "sun": "10км"}]
    main_module.write_plan_version(fake_bucket, 1, w1, "seed")
    man = main_module.read_plan_manifest(fake_bucket)
    assert man["current_version"] == 1
    v1 = main_module.read_plan_version(fake_bucket, man["gcs_object_path"])
    assert v1["weeks"] == w1 and v1["is_current"] is True

    w2 = [{"w": 1, "type": "dev", "sun": "12км"}]
    main_module.write_plan_version(fake_bucket, 2, w2, "edit")
    man2 = main_module.read_plan_manifest(fake_bucket)
    assert man2["current_version"] == 2
    # v1 still readable — immutable history preserved
    assert main_module.read_plan_version(fake_bucket, "plan/v1/plan.json")["weeks"] == w1
    assert main_module.read_plan_version(fake_bucket, man2["gcs_object_path"])["weeks"] == w2


# ── LLM config versioning (key stored full in GCS, manifest points to current) ─

def test_llm_config_versioning(main_module, fake_bucket):
    assert main_module.read_llm_config_full(fake_bucket) is None
    main_module.write_llm_config_version(fake_bucket, "deepseek", "deepseek-chat",
                                         "sk-secret-1234567890")
    cfg = main_module.read_llm_config_full(fake_bucket)
    assert cfg["provider"] == "deepseek"
    assert cfg["api_key"] == "sk-secret-1234567890"   # full key persisted
    assert cfg["version"] == 1

    main_module.write_llm_config_version(fake_bucket, "anthropic", "claude-x", "sk-ant-999")
    cfg2 = main_module.read_llm_config_full(fake_bucket)
    assert cfg2["version"] == 2 and cfg2["provider"] == "anthropic"


# ── advice round-trip ─────────────────────────────────────────────────────────

def test_advice_roundtrip(main_module, fake_bucket):
    assert main_module.read_latest_advice(fake_bucket) is None
    ctx = {"last_runs": [{"id": 11}, {"id": 12}], "plan_version": 3}
    rec = {"assessment": "ok", "adjustments": [], "warnings": []}
    main_module.write_advice_version(fake_bucket, rec, ctx, "deepseek", "deepseek-chat",
                                     100, 50, llm_config_version=1)
    latest = main_module.read_latest_advice(fake_bucket)
    assert latest["recommendation"] == rec
    assert latest["based_on_runs"] == [11, 12]
    assert latest["provider"] == "deepseek" and latest["version"] == 1


# ── tmp cleanup (ephemeral data, age-based) ───────────────────────────────────

def test_cleanup_old_tmp(main_module, fake_bucket):
    now = int(time.time())
    old = now - 100_000        # > 24h
    fresh = now                # < 24h
    fake_bucket.blob(f"tmp/{old}-aaaa/activity.fit").upload_from_string(b"x")
    fake_bucket.blob(f"tmp/{old}-aaaa/details.json").upload_from_string("{}")
    fake_bucket.blob(f"tmp/{fresh}-bbbb/details.json").upload_from_string("{}")

    deleted = main_module.cleanup_old_tmp(fake_bucket, max_age_hours=24)
    assert deleted == 2        # both objects under the old token
    names = [b.name for b in fake_bucket.list_blobs(prefix="tmp/")]
    assert all("aaaa" not in n for n in names)
    assert any("bbbb" in n for n in names)


# ── FIT tmp → permanent promotion ─────────────────────────────────────────────

def test_parse_fit_tmp_then_attach(main_module, fake_bucket):
    parsed = {
        "date": "2026-05-01",
        "summary": {"dist_km": 3.0, "avg_cadence": 176, "max_hr": 170,
                    "total_ascent_m": 24, "calories": 210},
        "laps": [{"lap": 1, "pace": "5:00"}],
        "samples": {"t_offset_sec": [0, 5], "hr": [120, 130],
                    "pace_sec_per_km": [300, 300], "altitude_m": [60, 61]},
    }
    token = main_module.write_parsed_fit_to_tmp(fake_bucket, b"FITBYTES", parsed)
    assert fake_bucket.blob(f"tmp/{token}/activity.fit").exists()
    assert fake_bucket.blob(f"tmp/{token}/details.json").exists()

    run = {"id": 123, "date": "2026-05-01"}
    main_module.attach_fit_details_to_run(fake_bucket, run, token)

    # run dict enriched from summary
    assert run["details_available"] is True
    assert run["avg_cadence"] == 176
    assert run["calories"] == 210
    # permanent objects written
    assert fake_bucket.blob("runs/123/v1/activity.fit").exists()
    assert fake_bucket.blob("runs/123/v1/details.json").exists()
    assert fake_bucket.blob("runs/123/manifest.json").exists()
    # tmp cleaned
    assert not fake_bucket.blob(f"tmp/{token}/activity.fit").exists()
    assert not fake_bucket.blob(f"tmp/{token}/details.json").exists()
    # read_run_details resolves via manifest
    details = main_module.read_run_details(fake_bucket, 123)
    assert details["summary"]["avg_cadence"] == 176
    assert details["fit_object_path"] == "runs/123/v1/activity.fit"


def test_attach_fails_on_missing_token(main_module, fake_bucket):
    import pytest
    with pytest.raises(ValueError):
        main_module.attach_fit_details_to_run(fake_bucket, {"id": 1}, "nonexistent-token")
