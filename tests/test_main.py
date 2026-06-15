"""Unit tests for pure helper functions in main.py.

These lock in behavior we previously verified manually (cadence fix, pace/time
formatting, LLM JSON extraction, key masking, HR-drift, FIT parsing).
"""
from pathlib import Path

import pytest

from conftest import FIT_FIXTURE


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
                         "sun": "14км", "mon": "8км", "wed": "6x1км",
                         "fri": "10км", "sat": "4x2км"},
        "next_week": None,
        "week_idx": 1,
        "plan_version": 1,
        "heuristics": {"avg_pace_min_per_km": 5.5, "hard_or_bad_count": 0,
                       "total_km_last_14": 14.2},
    }
    text = main_module.format_context_for_llm(ctx)
    assert "2026-05-15" in text
    assert "14.2" in text


# ── FIT parsing (needs local fixture; skipped if absent) ──────────────────────

@pytest.mark.skipif(not FIT_FIXTURE.exists(),
                    reason="no FIT fixture at tests/fixtures/sample_activity.fit")
def test_parse_fit_summary_and_cadence(main_module):
    parsed = main_module.parse_fit_file(FIT_FIXTURE.read_bytes())
    summary = parsed["summary"]
    assert summary["dist_km"] > 0
    # Cadence fix: real running cadence is ~150-190 spm, not the per-leg ~88.
    assert summary["avg_cadence"] is not None and summary["avg_cadence"] > 150
    assert len(parsed["laps"]) > 1
    assert len(parsed["samples"]["t_offset_sec"]) > 0
    # Lap paces formatted as m:ss
    assert all(":" in lap["pace"] for lap in parsed["laps"] if lap.get("pace"))
