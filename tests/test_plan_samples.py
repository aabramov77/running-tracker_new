"""Sample plan files (#28) must match the documented import format.

The importer itself is JS (verified in preview), but these samples are what
users download as a template and what the docs describe — this guards them
from drifting away from the contract in docs/plan-import-format.md.
"""
import csv
import json

import pytest

from conftest import REPO

CSV_SAMPLE = REPO / "docs" / "samples" / "plan-example.csv"
JSON_SAMPLE = REPO / "docs" / "samples" / "plan-example.json"
FORMAT_DOC = REPO / "docs" / "plan-import-format.md"

DAY_FIELDS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
DAY_HEADERS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
VALID_TYPES = {"dev", "peak", "taper", "load", "race"}


def _csv_rows():
    with open(CSV_SAMPLE, encoding="utf-8") as f:
        return list(csv.reader(f))


# ── CSV sample ────────────────────────────────────────────────────────────────

def test_csv_sample_exists():
    assert CSV_SAMPLE.exists(), "docs/samples/plan-example.csv missing"


def test_csv_header_matches_contract():
    header = _csv_rows()[0]
    assert header[:5] == ["Нед", "Начало", "Конец", "Акцент", "Тип"]
    assert header[5:] == DAY_HEADERS, "day columns must be Пн→Вс in order"


def test_csv_rows_are_well_formed():
    rows = _csv_rows()
    header, data = rows[0], [r for r in rows[1:] if any(c.strip() for c in r)]
    assert data, "sample must contain at least one week"
    for i, row in enumerate(data, start=2):
        assert len(row) == len(header), f"row {i}: column count differs from header"
        assert row[4] in VALID_TYPES, f"row {i}: unknown type {row[4]!r}"
    # week numbers are sequential from 1
    assert [r[0] for r in data] == [str(i) for i in range(1, len(data) + 1)]


def test_csv_sample_has_no_bom():
    """Экспорт добавляет BOM, но образец в репозитории хранится без него."""
    assert not CSV_SAMPLE.read_bytes().startswith(b"\xef\xbb\xbf")


# ── JSON sample ───────────────────────────────────────────────────────────────

def test_json_sample_matches_contract():
    data = json.loads(JSON_SAMPLE.read_text(encoding="utf-8"))
    assert data["format"] == "running-tracker-plan"
    assert data["version"] == 1

    race = data["race"]
    for field in ("race_name", "race_date", "target_time", "plan_start"):
        assert field in race, f"race.{field} missing"

    weeks = data["weeks"]
    assert weeks, "weeks must not be empty"
    for i, w in enumerate(weeks, start=1):
        assert w["w"] == i, f"week {i}: numbering must be sequential"
        assert w["type"] in VALID_TYPES, f"week {i}: unknown type {w['type']!r}"
        for field in ("start", "end", "accent", *DAY_FIELDS):
            assert field in w, f"week {i}: field {field} missing"
            assert isinstance(w[field], str), f"week {i}: {field} must be a string"


def test_both_samples_describe_the_same_plan_shape():
    """CSV и JSON — одна и та же модель недели, только разное представление."""
    csv_weeks = [r for r in _csv_rows()[1:] if any(c.strip() for c in r)]
    json_weeks = json.loads(JSON_SAMPLE.read_text(encoding="utf-8"))["weeks"]
    # первая неделя совпадает по содержимому (образцы держим синхронными)
    c0, j0 = csv_weeks[0], json_weeks[0]
    assert c0[1] == j0["start"] and c0[2] == j0["end"]
    assert c0[3] == j0["accent"] and c0[4] == j0["type"]
    assert c0[5:] == [j0[f] for f in DAY_FIELDS]


# ── docs ──────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("needle", [
    "plan-example.csv", "plan-example.json",
    "running-tracker-plan", "plan_start",
    *DAY_HEADERS,
    *sorted(VALID_TYPES),
])
def test_format_doc_covers_contract(needle):
    doc = FORMAT_DOC.read_text(encoding="utf-8")
    assert needle in doc, f"docs/plan-import-format.md does not mention {needle!r}"
