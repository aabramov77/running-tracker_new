"""Сопоставление плана тренировок с фактически выполненными пробежками (#41).

Слой домена по раскладке #36: только чистые вычисления, в GCS модуль не ходит
и о `storage` не знает. Импортирует `domain` ради порядка дней недели.

Две вещи, ради которых модуль вообще появился:

1. **Границы недели.** Раньше индекс недели считался как «сколько семидневок
   прошло от plan_start» — без выравнивания на понедельник и со смешением
   UTC и локального времени. Если plan_start приходился не на понедельник,
   границы недель уезжали относительно подписей в плане (#40). Здесь неделя
   всегда понедельник→воскресенье, а арифметика идёт в целых днях.

2. **Плановый объём из свободного текста.** Ячейка дня — это «10 км лёгкий»
   или «интервалы 6×800м». Второе в километры не переводится без разминки и
   заминки, поэтому такая ячейка честно помечается как нераспознанная и в
   объём недели не попадает. Угадать её значит показать правдоподобное и
   неверное число — а одна такая цифра обесценивает весь отчёт.
"""
import re
from datetime import date, datetime, timedelta

from domain import PLAN_DAYS

# Исторический дефолт: планы, заведённые до появления поля plan_start.
DEFAULT_PLAN_START = "2026-05-10"
DEFAULT_WEEKS = 13

DAY_FIELDS = [field for field, _ in PLAN_DAYS]  # mon..sun, индекс = date.weekday()


# ── Даты и границы недель ────────────────────────────────────────────────────

def to_date(value):
    """'2026-05-10' | date | datetime → date. Мусор и пустое → None."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not value:
        return None
    try:
        return datetime.strptime(str(value)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


# Подпись недели: «11.05», «11.05.2026» или ISO «2026-05-11».
_LABEL_RE = re.compile(r"^\s*(\d{1,2})[.\-/](\d{1,2})(?:[.\-/](\d{2,4}))?\s*$")


def label_to_date(label, year_hint=None):
    """Подпись недели → date. Год в «11.05» отсутствует — берём ближайший
    к plan_start, чтобы план, переходящий через Новый год, не разъезжался.
    """
    iso = to_date(label)
    if iso:
        return iso
    match = _LABEL_RE.match(str(label or ""))
    if not match:
        return None
    day, month, year = int(match.group(1)), int(match.group(2)), match.group(3)
    if year:
        year = int(year)
        return _safe_date(year + 2000 if year < 100 else year, month, day)

    hint = to_date(year_hint) or to_date(DEFAULT_PLAN_START)
    candidates = [d for d in (_safe_date(hint.year + shift, month, day)
                              for shift in (-1, 0, 1)) if d]
    return min(candidates, key=lambda d: abs((d - hint).days)) if candidates else None


def _safe_date(year, month, day):
    try:
        return date(year, month, day)
    except ValueError:      # 29.02 в невисокосном году
        return None


def plan_week_zero(plan_start=None, weeks=None):
    """День, с которого начинается первая строка плана.

    Приоритет — подпись первой строки: её человек заполняет по календарю и
    с ней же сверяет цифры. `plan_start` нередко остался с прежних времён и
    указывает на другой день — расхождение между ними и есть #40.

    Ни к какому дню недели дата не притягивается. Планы размечают
    по-разному: бывают строки пн→вс, бывают вс→сб. Навязывать понедельник
    значит считать факт по окну, которое не совпадает с подписью строки, —
    в строке «24.05–30.05» показался бы объём недели 18–24.05.
    """
    first = (weeks or [None])[0]
    if first:
        labelled = label_to_date((first or {}).get("start"), plan_start)
        if labelled:
            return labelled
    return to_date(plan_start) or to_date(DEFAULT_PLAN_START)


def week_windows(weeks, plan_start=None):
    """Окна всех строк плана: [(первый день, последний день)].

    Окно берётся из подписи каждой строки, а не отсчитывается от первой:
    так подпись и расчёт совпадают по построению и не разъезжаются на
    вставленной неделе, опечатке в дате или смене разметки посреди плана.
    Строка без подписи получает окно от якоря плана.

    Обычно окно — семь дней. Короче оно в двух случаях: строка сама
    подписана короче («04.10–04.10» — план стартует в воскресенье) либо в
    эти семь дней уже начинается другая строка. Иначе один день принадлежал
    бы двум строкам сразу: пробежка засчитывалась дважды, текущей неделей
    оказывалась предыдущая, а на дату находилась чужая пустая ячейка.
    """
    weeks = weeks or []
    zero = plan_week_zero(plan_start, weeks)
    starts = [label_to_date((row or {}).get("start"), plan_start)
              or zero + timedelta(days=7 * idx)
              for idx, row in enumerate(weeks)]

    windows = []
    for row, first in zip(weeks, starts):
        last = first + timedelta(days=6)
        labelled_end = label_to_date((row or {}).get("end"), first)
        if labelled_end and first <= labelled_end < last:
            last = labelled_end
        for other in starts:
            if first < other <= last:
                last = other - timedelta(days=1)
        windows.append((first, last))
    return windows


def week_window(weeks, idx, plan_start=None):
    """(первый день, последний день) строки idx — см. week_windows."""
    if weeks and 0 <= idx < len(weeks):
        return week_windows(weeks, plan_start)[idx]
    first = plan_week_zero(plan_start, weeks) + timedelta(days=7 * idx)
    return first, first + timedelta(days=6)


def day_date(week_start, field):
    """Дата колонки дня внутри окна строки.

    В семи подряд идущих днях каждый день недели встречается ровно один
    раз, поэтому колонки Пн–Вс раскладываются однозначно при любом дне
    начала: у строки вс→сб «Вс» окажется первым днём окна, а не последним.
    """
    weekday = DAY_FIELDS.index(field)
    return week_start + timedelta(days=(weekday - week_start.weekday()) % 7)


def week_days(week_start, week_end=None):
    """[(дата, колонка)] дней окна по порядку дат.

    У строки короче семи дней колонок меньше: день за её последней датой
    принадлежит другой строке или не принадлежит ни одной.
    """
    last = week_end or week_start + timedelta(days=6)
    days = sorted((day_date(week_start, field), field) for field in DAY_FIELDS)
    return [(day, field) for day, field in days if day <= last]


LABEL = "label"
PLAN_START = "plan_start"
DEFAULT = "default"


def anchor_source(plan_start=None, weeks=None):
    """Откуда взялся якорь недели 0.

    `default` означает, что план не датирован ничем — ни подписью первой
    строки, ни `plan_start`, — и недели разложены по историческому умолчанию.
    Показывать по такому плану факт нельзя: он лёг бы на произвольные даты,
    поэтому интерфейсу нужно не число, а объяснение, чего не хватает.
    """
    first = (weeks or [None])[0]
    if first and label_to_date((first or {}).get("start"), plan_start):
        return LABEL
    return PLAN_START if to_date(plan_start) else DEFAULT


def planned_for_date(weeks, day, plan_start=None):
    """Что стояло в плане на дату: (индекс недели, поле дня, текст) или None.

    None — дата вне плана либо план ничем не датирован: подставить тренировку
    с угаданного дня хуже, чем не подставить никакой (#46).
    """
    day = to_date(day)
    if day is None or not weeks or anchor_source(plan_start, weeks) == DEFAULT:
        return None
    for idx, (start, end) in enumerate(week_windows(weeks, plan_start)):
        if start <= day <= end:
            field = DAY_FIELDS[day.weekday()]
            return idx, field, str((weeks[idx] or {}).get(field) or "").strip()
    return None


def plan_week_range(plan_start, idx, weeks=None):
    """(первый день, последний день) недели idx, 0-based."""
    return week_window(weeks, idx, plan_start)


def week_index_of(plan_start, day, weeks=None):
    """0-based индекс недели плана, в которую попадает day. Может быть < 0."""
    day = to_date(day)
    if day is None:
        return None
    return (day - plan_week_zero(plan_start, weeks)).days // 7


def current_week_idx(plan_start=None, weeks_count=0, today=None, weeks=None):
    """Индекс текущей недели, прижатый к границам плана.

    `today` — параметр, а не utcnow() внутри: иначе поведение на границе
    недели невоспроизводимо в тестах, а именно на границе всё и ломалось.
    """
    day = to_date(today) or date.today()
    n = weeks_count or (len(weeks) if weeks else 0) or DEFAULT_WEEKS

    if weeks:
        # Идём по окнам строк: подписи могут идти не ровно через семь дней,
        # поэтому арифметикой индекс не вычислить.
        latest_started = None
        for i, (start, end) in enumerate(week_windows(weeks, plan_start)):
            if start <= day <= end:
                return min(i, n - 1)
            if start <= day:
                latest_started = i
        # День вне окон: до плана — первая строка, в разрыве или после
        # плана — последняя начавшаяся.
        return min(latest_started if latest_started is not None else 0, n - 1)

    idx = week_index_of(plan_start, day, weeks)
    if idx is None:
        return 0
    return max(0, min(n - 1, idx))


# ── Разбор планового дня ─────────────────────────────────────────────────────

REST = "rest"
KM = "km"
UNPARSED = "unparsed"

_REST_WORDS = {"отдых", "выходной", "выходные", "rest", "off", "-", "—", "–", "0"}

# Число с «км»/«km». Отрицательный lookbehind не даёт зацепиться за хвост
# другого числа («6х800м» не должно дать «00 м»).
_KM_RE = re.compile(r"(?<![\d,.])(\d+(?:[.,]\d+)?)\s*(?:км|km)\b", re.IGNORECASE)

# Диапазон: «14–16 км», «6-8 км». Дефис, минус и оба тире.
_RANGE_RE = re.compile(
    r"(?<![\d,.])(\d+(?:[.,]\d+)?)\s*[-–—]\s*(\d+(?:[.,]\d+)?)\s*(?:км|km)\b",
    re.IGNORECASE)

# Интервалы: «6×800», «6x800», «6х800» (латинская x и кириллическая х).
_INTERVAL_RE = re.compile(r"\d\s*[×xх*]\s*\d", re.IGNORECASE)

# Длительность вместо расстояния: «45 мин», «1 ч».
_DURATION_RE = re.compile(r"\d+\s*(?:мин|минут\w*|min|ч\b|час\w*|h\b)", re.IGNORECASE)


def _to_km(text):
    try:
        return float(text.replace(",", "."))
    except (ValueError, AttributeError):
        return None


def parse_planned_day(text):
    """Ячейка дня плана → {"kind": rest|km|unparsed, "km", "exact"}.

    `km` — это всегда **нижняя граница** планового объёма ячейки, а `exact`
    говорит, точна ли она. Отсюда единый и честный смысл у суммы за неделю:
    «не меньше N километров». Он верен и когда ячейка не разобралась вовсе
    (вносит 0), и когда в ней диапазон (вносит нижнюю границу).

    Округлять «14–16 км» до 16 нельзя: это выглядит точным числом, не будучи
    им. Догадываться об объёме интервалов — тоже: «3×1 км» без разминки и
    заминки не километраж тренировки.

    Несколько отдельных чисел с «км» дают unparsed: «10 км (5 км в темпе)» —
    это 10, а не 15, и отличить такое от «2 км + 2 км» разбором текста нельзя.
    """
    raw = (text or "").strip()
    if not raw or raw.lower() in _REST_WORDS:
        return {"kind": REST, "km": None, "exact": True}

    # Интервалы и длительности идут до разбора чисел: в такой ячейке любое
    # найденное число описывает отрезок, а не объём тренировки.
    if _INTERVAL_RE.search(raw) or _DURATION_RE.search(raw):
        return {"kind": UNPARSED, "km": None, "exact": False}

    span = _RANGE_RE.search(raw)
    if span:
        low, high = _to_km(span.group(1)), _to_km(span.group(2))
        if low is not None and high is not None and low <= high:
            return {"kind": KM, "km": low, "exact": False}
        return {"kind": UNPARSED, "km": None, "exact": False}

    matches = _KM_RE.findall(raw)
    if len(matches) != 1:
        return {"kind": UNPARSED, "km": None, "exact": False}

    km = _to_km(matches[0])
    if km is None:
        return {"kind": UNPARSED, "km": None, "exact": False}
    return {"kind": KM, "km": km, "exact": True}


# ── Сопоставление ────────────────────────────────────────────────────────────

DONE = "done"
MISSED = "missed"
EXTRA = "extra"
EMPTY = "empty"


def _runs_by_date(runs, plan_id=None):
    """Пробежки → {date: [run]}. Удалённые и чужие планы отсеиваются."""
    buckets = {}
    for run in runs or []:
        if run.get("deleted"):
            continue
        if plan_id is not None and run.get("plan_id") != plan_id:
            continue
        day = to_date(run.get("date"))
        if day is None:
            continue
        buckets.setdefault(day, []).append(run)
    return buckets


def _run_km(runs):
    total = 0.0
    for run in runs:
        try:
            total += float(run.get("dist") or 0)
        except (TypeError, ValueError):
            continue
    return round(total, 2)


def week_compliance(week, week_start, runs_by_date, week_end=None):
    """Один ряд плана против факта. week_start — первый день окна строки,
    week_end — последний; без него окно семидневное."""
    last = week_end or week_start + timedelta(days=6)
    days = []
    planned_km = 0.0
    unparsed = 0
    approx = 0
    planned_sessions = 0
    actual_km = 0.0
    actual_sessions = 0
    missed = 0
    extra = 0

    for field in DAY_FIELDS:
        this_day = day_date(week_start, field)
        if this_day > last:
            continue    # день вне окна короткой строки — не её день
        plan = parse_planned_day((week or {}).get(field))
        day_runs = runs_by_date.get(this_day, [])
        day_km = _run_km(day_runs)

        if plan["kind"] == KM:
            planned_km += plan["km"]
            planned_sessions += 1
            if not plan["exact"]:
                approx += 1
        elif plan["kind"] == UNPARSED:
            unparsed += 1
            planned_sessions += 1

        if day_runs:
            actual_km += day_km
            actual_sessions += 1

        if plan["kind"] in (KM, UNPARSED):
            status = DONE if day_runs else MISSED
        else:
            status = EXTRA if day_runs else EMPTY
        if status == MISSED:
            missed += 1
        elif status == EXTRA:
            extra += 1

        days.append({
            "field": field,
            "date": this_day.isoformat(),
            "planned_text": (week or {}).get(field) or "",
            "planned_kind": plan["kind"],
            "planned_km": plan["km"],
            "planned_exact": plan["exact"],
            "actual_km": day_km if day_runs else None,
            "runs": len(day_runs),
            "status": status,
        })

    # `complete` — плановый объём известен точно. Иначе planned_km это
    # нижняя граница: «не меньше N».
    complete = unparsed == 0 and approx == 0
    planned_km = round(planned_km, 2)
    actual_km = round(actual_km, 2)
    # Процент и дельту показываем только от точного плана — от нижней
    # границы они завышают выполнение и вводят в заблуждение.
    pct = round(actual_km / planned_km * 100) if complete and planned_km > 0 else None

    return {
        "start": week_start.isoformat(),
        "end": last.isoformat(),
        "planned_km": planned_km,
        "actual_km": actual_km,
        "delta_km": round(actual_km - planned_km, 2) if complete else None,
        "pct": pct,
        "complete": complete,
        "unparsed": unparsed,
        "approx": approx,
        "planned_sessions": planned_sessions,
        "actual_sessions": actual_sessions,
        "missed": missed,
        "extra": extra,
        "days": days,
    }


def plan_compliance(weeks, runs, plan_start=None, plan_id=None):
    """План целиком против факта: по неделям и итогом."""
    weeks = weeks or []
    runs_by_date = _runs_by_date(runs, plan_id)
    zero = plan_week_zero(plan_start, weeks)

    rows = [week_compliance(week, start, runs_by_date, end)
            for week, (start, end) in zip(weeks, week_windows(weeks, plan_start))]

    complete = all(r["complete"] for r in rows) if rows else True
    planned_km = round(sum(r["planned_km"] for r in rows), 2)
    actual_km = round(sum(r["actual_km"] for r in rows), 2)
    source = anchor_source(plan_start, weeks)

    return {
        "anchor": zero.isoformat(),
        "anchor_source": source,
        # Даты недель угаданы — факт по ним раскладывать бессмысленно.
        "dated": source != DEFAULT,
        "weeks": rows,
        "totals": {
            "planned_km": planned_km,
            "actual_km": actual_km,
            "delta_km": round(actual_km - planned_km, 2) if complete else None,
            "pct": (round(actual_km / planned_km * 100)
                    if complete and planned_km > 0 else None),
            "complete": complete,
            "unparsed": sum(r["unparsed"] for r in rows),
            "approx": sum(r["approx"] for r in rows),
            "missed": sum(r["missed"] for r in rows),
            "extra": sum(r["extra"] for r in rows),
            "weeks_total": len(rows),
        },
    }
