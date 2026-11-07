"""Конфигурация приложения.

Отдельный модуль, чтобы и хранилище, и HTTP-слой брали константы из одного
места и не тянули друг друга ради них (#36, фаза 3).
"""
import os

CLIENT_ID = "463368957110-f1649h2mjd1hbkj5307jllcv3e0hslbc.apps.googleusercontent.com"

# Кто получает role=admin при первом логине. Остальные — pending до одобрения.
ADMIN_EMAILS = {"aabramov77@gmail.com"}

BUCKET_NAME = os.environ.get("BUCKET_NAME", "running-tracker-aabramov77")

# Глобальные (не per-user) объекты
USERS_REGISTRY = "users/registry.json"
LLM_CONFIG_MANIFEST = "config/llm/manifest.json"   # общий ключ LLM (управляет админ)

# Лимиты
REGISTRY_TTL_SEC = 30       # кэш реестра в памяти тёплого инстанса
MAX_PENDING = 50            # защита от наполнения реестра неодобренными
# Диалог с ИИ-тренером (#46): сообщений на пользователя в сутки. Ключ у
# провайдера общий на всех, поэтому лимит — на каждого.
DAILY_AI_COACH_LIMIT = 30
ADMIN_DAILY_AI_COACH_LIMIT = 300

# Пределы размера тела запроса (#53). JSON здесь — пробежка, план, сообщение:
# мегабайта хватает с большим запасом. FIT долгой тренировки — единицы мегабайт.
MAX_JSON_BODY_BYTES = 1024 * 1024
MAX_FIT_BODY_BYTES = 10 * 1024 * 1024

# Общий бюджет вывода LLM: у моделей с рассуждением он покрывает и рассуждение,
# и видимый ответ, поэтому прежних 1500 не хватало — ответ обрывался. #38
LLM_MAX_TOKENS = 8000

# Глубина рассуждения. У OpenAI набор шире (none…max), но какие уровни
# принимает DeepSeek — по документации не подтвердилось, поэтому предлагаем
# пересечение, которое заведомо есть у обоих. Дефолт совпадает с дефолтом
# OpenAI. #38
LLM_EFFORT_LEVELS = ("low", "medium", "high")
LLM_DEFAULT_EFFORT = "medium"

# Прогноз погоды (#72): Open-Meteo, без ключа. Бесплатный тариф — только для
# некоммерческого использования и с подписью источника; платный отличается
# адресом (customer-api.open-meteo.com) и параметром apikey.
WEATHER_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
WEATHER_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
WEATHER_TTL_SEC = 30 * 60          # прогноз в памяти инстанса
WEATHER_FAIL_TTL_SEC = 2 * 60      # провайдер не ответил — столько не переспрашиваем
GEOCODE_TTL_SEC = 24 * 3600
WEATHER_TIMEOUT_SEC = 5
WEATHER_DEFAULT_RUN_TIME = "08:00"
