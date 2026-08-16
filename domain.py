"""Словарь предметной области: подписи, дни недели, дистанции.

Выделено из main.py (#36, фаза 1). Здесь только константы и чистые вычисления
без обращений к GCS — модуль импортируют и хранилище, и слой промпта, поэтому
он не должен зависеть ни от того, ни от другого.
"""

# Порядок и подписи дней недели плана (7 дней, Пн→Вс). #23
PLAN_DAYS = [("mon", "пн"), ("tue", "вт"), ("wed", "ср"), ("thu", "чт"),
             ("fri", "пт"), ("sat", "сб"), ("sun", "вс")]

SEX_LABELS = {"m": "М", "f": "Ж"}

# Границы пульсовых зон в % от максимального пульса — ориентир, не медицинская
# рекомендация.
HR_ZONE_BOUNDS = [
    ("Z1 восстановление", 50, 60),
    ("Z2 аэробная",       60, 70),
    ("Z3 темповая",       70, 80),
    ("Z4 ПАНО",           80, 90),
    ("Z5 максимальная",   90, 100),
]

PLAN_PHASE_LABELS = {
    "dev": "Развитие",
    "peak": "Пик формы",
    "taper": "Тейпер",
    "load": "Разгрузка",
    "race": "Соревнование (подводка)",
}

FEEL_LABELS = {
    "great": "отлично",
    "good": "хорошо",
    "ok": "нормально",
    "hard": "тяжело",
    "bad": "плохо",
}

TYPE_LABELS = {
    "easy": "лёгкий",
    "interval": "интервалы",
    "tempo": "темповый",
    "long": "длительный",
    "race": "соревнование",
    "recovery": "восстановительный",
}

DIST_LABEL_KM = {"4.2km": 4.2, "5km": 5, "10km": 10, "HM": 21.0975, "M": 42.195}

def parse_time_to_sec(text):
    """«44:30» → 2670, «1:47:20» → 6440. Мусор → None."""
    if not text:
        return None
    parts = str(text).strip().split(":")
    if not 2 <= len(parts) <= 3 or not all(p.strip().isdigit() for p in parts):
        return None
    nums = [int(p) for p in parts]
    if len(parts) == 2:
        return nums[0] * 60 + nums[1]
    return nums[0] * 3600 + nums[1] * 60 + nums[2]

def personal_bests(races):
    """Лучший результат на каждой дистанции из раздела «Старты» (#32).

    Отдельных полей в профиле не заводим: расхождение между «Стартами» и
    профилем дало бы неверный контекст для LLM.
    """
    best = {}
    for race in races:
        if race.get("deleted"):
            continue
        label = race.get("dist_label")
        seconds = parse_time_to_sec(race.get("time"))
        if label not in DIST_LABEL_KM or seconds is None:
            continue
        current = best.get(label)
        if current is None or seconds < current["seconds"]:
            best[label] = {"dist_label": label, "km": DIST_LABEL_KM[label],
                           "time": race.get("time"), "date": race.get("date", ""),
                           "seconds": seconds}
    return sorted(best.values(), key=lambda b: b["km"])
