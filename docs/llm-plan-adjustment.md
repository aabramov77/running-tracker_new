# План: LLM-рекомендации + полные данные тренировок (FIT)

## Context

Сейчас раздел «Корректировка» (`renderAdjust()` в `app.js`) генерирует советы по жёстко
зашитым правилам, а для каждой пробежки в `runs.json` хранятся только агрегаты
(дист, время, темп, средний пульс, ощущения). Этого мало:
- эвристики не учитывают фазу плана, тренд недели, пульсовые зоны, динамику в рамках одной тренировки
- LLM, получив только summary, не сможет оценить качество интервалов, дрейф пульса, прогрессию темпа

Цель — два связанных улучшения:

1. **Расширить хранение данных** — для каждой пробежки сохранять полный набор из Garmin FIT
   (по-круговые показатели + сэмплы по времени: пульс, темп, высота, каденс).
2. **Добавить LLM-рекомендации** — кнопка в «Корректировке», которая шлёт богатый контекст
   (полные тренировки + план + забеги) во внешнюю LLM (Claude API) и показывает экспертный совет.

---

## Часть А. Расширение хранения тренировок (FIT)

### А.1 Формат и место хранения

Сейчас CSV-импорт берёт только Summary. Будем дополнительно принимать **FIT** — родной
бинарный формат Garmin с поминутными сэмплами. CSV оставляем как альтернативу.

**Структура в GCS (per CLAUDE.md — иммутабельные версии, append-only):**
```
gs://running-tracker-aabramov77/
  runs.json                          # компактный список (как сейчас, без изменений по структуре)
  runs/{run_id}/v1/activity.fit      # ОРИГИНАЛ FIT (бинарь, никогда не перезаписываем)
  runs/{run_id}/v1/details.json      # СТРУКТУРИРОВАННЫЕ данные, извлечённые из FIT
  runs/{run_id}/manifest.json        # текущая версия деталей (если редактируем — растёт v2, v3…)
```

`runs.json` — лёгкий, грузится при старте. Полные данные (`details.json`) грузятся on-demand
(модалка детали тренировки + контекст для LLM).

### А.2 Что лежит в `details.json`

```json
{
  "version": 1,
  "is_current": true,
  "created_at": "...",
  "source": "garmin_fit",
  "fit_object_path": "runs/{id}/v1/activity.fit",
  "summary": {
    "dist_km": 10.45, "duration_sec": 3665, "avg_pace_sec_per_km": 351,
    "avg_hr": 142, "max_hr": 155, "avg_cadence": 174,
    "total_ascent_m": 48, "total_descent_m": 46, "calories": 794,
    "avg_power_w": 343, "max_power_w": 427
  },
  "laps": [
    {"lap": 1, "dist_km": 1.0, "duration_sec": 344, "pace": "5:44", "avg_hr": 125, "max_hr": 136, "cadence": 169, "ascent_m": 5}
  ],
  "samples": {
    "t_offset_sec": [0, 5, 10],
    "hr": [110, 118, 125],
    "pace_sec_per_km": [380, 365, 355],
    "altitude_m": [120, 121, 121]
  }
}
```

`samples` опционально. При импорте CSV (Laps) — `laps` есть, `samples` пусто.

### А.3 Парсинг FIT

**Бэкенд (Python):** библиотека `fitparse==1.*`. Парсим в Cloud Run при загрузке файла,
сразу строим `details.json` и заливаем в GCS.

**Маршрут:** новый `POST /runs/upload-fit` (multipart) → бэкенд парсит → создаёт run в `runs.json`
+ заливает FIT и details.json → возвращает созданный run.

### А.4 Изменения UI на «+ Пробежка»

Добавить кнопку загрузки рядом с CSV:
```
📁 Загрузить из Garmin (.fit)
```
При выборе файла — multipart POST на `/runs/upload-fit`, после успеха те же поля заполняются
в форме (как сейчас при CSV), но в `runs.json` пишутся ВСЕ агрегаты (max HR, набор, каденс,
калории) + ID связки с `details.json`.

### А.5 Обратная совместимость

- Существующие CSV-импорты продолжают работать (Summary → форма)
- В `runs.json` старые записи без `details_available` — флаг показывает, есть ли подробности
- Модалка «детали тренировки» при `details_available: true` подгружает `details.json` и показывает
  лапы + график пульса. Иначе — только текущие поля.

---

## Часть Б. LLM-рекомендации (мульти-провайдер)

### Б.1 Архитектура

```
┌──────────┐       ┌──────────────────┐      ┌─────────────────────────┐
│ index    │ POST  │ Cloud Run        │ POST │ Anthropic  / OpenAI /   │
│ app.js   │ ───►  │ /advise          │ ───► │ Deepseek (по конфигу)   │
│          │       │ - verify token   │      │                         │
│          │ ◄───  │ - load llm cfg   │ ◄─── │                         │
│          │       │ - call provider  │      │                         │
│          │       │ - store in GCS   │      │                         │
└──────────┘       └──────────────────┘      └─────────────────────────┘
```

Все три провайдера вызываются с бэкенда. Фронт не видит ключи и не общается с LLM напрямую.

### Б.1.1 Хранение API-токена — GCS приватный объект

**Решение зафиксировано:** ключ хранится как приватный объект в GCS-бакете
`running-tracker-aabramov77`, версионируется по правилу CLAUDE.md.

Почему это безопасно для нашего сетапа:
- Бакет уже **приватный**: внешние HTTP-запросы получают `403 AccessDenied`
- Доступ к объектам имеет **только Cloud Run service account** (тот же, что читает `runs.json`, `races.json`, плагин)
- В IAM бакета нет ролей для `allUsers` / `allAuthenticatedUsers`
- Внешние запросы на чтение `config/llm/...` проксируются только через `/config/llm`, который требует валидный Google OAuth token аутентифицированного пользователя (`aabramov77@gmail.com`)

**Маршрут жизни ключа:**

```
[Пользователь]   ввод в UI «Настройки»
        │
        ▼
[Frontend]       POST /config/llm  { provider, model, api_key }
        │       (Authorization: Bearer <google_id_token>)
        ▼
[Cloud Run]      verify_token() → пишет config/llm/v{N+1}/config.json в GCS
        │
        ▼
[GCS]            config/llm/v{N+1}/config.json   ← полный ключ ЗДЕСЬ
                 config/llm/manifest.json        ← обновлён указатель

При чтении GET /config/llm:
[Cloud Run]      читает текущую версию → отдаёт фронту маску `sk-ant-***abc1`
                 (полный ключ НИКОГДА не покидает бэк, кроме как при вызове LLM)
```

**Защита фронта от утечки:**
- `GET /config/llm` всегда возвращает только маску
- Поле в UI работает в режиме «показать маску / ввести новый» — старый ключ нельзя «прочитать» из формы
- Очистка ключа = POST с пустым `api_key` → создаётся новая версия с пустым полем (логическое удаление)

### Б.1.2 Структура конфига в GCS

`config/llm/v{N}/config.json` (иммутабельный):
```json
{
  "version": N,
  "is_current": true,
  "created_at": "ISO-8601",
  "created_by": "aabramov77",
  "provider": "anthropic",          // "anthropic" | "openai" | "deepseek"
  "model": "claude-sonnet-4-5",     // зависит от provider
  "api_key": "sk-ant-api03-...",    // полный ключ (только в GCS, не возвращается фронту)
  "supersedes_version": N-1
}
```

`config/llm/manifest.json` (перезаписывается при апдейте):
```json
{
  "current_version": N,
  "gcs_object_path": "config/llm/v{N}/config.json",
  "updated_at": "..."
}
```

### Б.1.3 Эндпоинты конфигурации

| Метод | Путь | Возвращает / принимает |
|---|---|---|
| `GET` | `/config/llm` | Текущая конфигурация **с маскированным ключом** (`sk-ant-***abc1`), без сырого ключа |
| `POST` | `/config/llm` | Сохранить новую конфигурацию: `{provider, model, api_key}` → создаётся новая версия в GCS |
| `POST` | `/config/llm/test` | Делает короткий «ping»-запрос к LLM текущим ключом → `{ok: true/false, latency_ms, sample_response}` |

### Б.1.4 Абстракция провайдера на бэке

Единый модуль `llm_clients.py`:
```python
def call_llm(provider, model, api_key, system_prompt, user_prompt) -> dict:
    """
    Возвращает {text, input_tokens, output_tokens, model, provider}.
    Внутри — три ветки.
    """
    if provider == "anthropic":
        return _call_anthropic(model, api_key, system_prompt, user_prompt)
    if provider == "openai":
        return _call_openai(model, api_key, system_prompt, user_prompt)
    if provider == "deepseek":
        return _call_deepseek(model, api_key, system_prompt, user_prompt)
    raise ValueError(f"Unknown provider: {provider}")
```

Реализации:

| Провайдер | Endpoint | Auth header | Тело запроса | SDK |
|---|---|---|---|---|
| Anthropic | `https://api.anthropic.com/v1/messages` | `x-api-key: <key>` + `anthropic-version: 2023-06-01` | `{model, system, messages, max_tokens}` | `anthropic` или httpx |
| OpenAI | `https://api.openai.com/v1/chat/completions` | `Authorization: Bearer <key>` | `{model, messages, response_format: {type:"json_object"}}` | `openai` или httpx |
| Deepseek | `https://api.deepseek.com/v1/chat/completions` | `Authorization: Bearer <key>` | как у OpenAI (совместимый формат) | httpx (OpenAI-совместимо) |

Чтобы не тянуть три SDK — можно реализовать всё через `httpx` (один лёгкий HTTP-клиент).

### Б.1.5 UI «Настройки LLM»

Новая модалка или вкладка «⚙ Настройки» с полями:

```
Провайдер:  [Anthropic ▾]   (выбор из 3)
Модель:     [claude-sonnet-4-5 ▾]   (опции зависят от провайдера)
API ключ:   [••••••••abc1]  [Изменить]  (показывает маску из бэка)
            [Сохранить]  [Проверить ключ]
```

Дропдаун моделей зависит от провайдера:
- **Anthropic:** `claude-sonnet-4-5`, `claude-haiku-4-5`, `claude-opus-4-5`
- **OpenAI:** `gpt-4o`, `gpt-4o-mini`, `gpt-4-turbo`
- **Deepseek:** `deepseek-chat`, `deepseek-reasoner`

Поле «API ключ» работает так:
- При входе на страницу — GET `/config/llm` возвращает маску → отображается
- Кнопка «Изменить» открывает поле для ввода нового ключа
- «Сохранить» → POST `/config/llm` с новым `{provider, model, api_key}` → бэк создаёт новую версию
- «Проверить ключ» → POST `/config/llm/test` → показывает «✓ работает (latency 800 мс)» или ошибку

### Б.2 Что слать в LLM (компактно, но богато)

Не отправляем FIT и не отправляем сэмплы по секундам — токены дорогие. Отправляем:
- Цель забега (1:40 на 21.1 км, дата 09.08.2026)
- Текущая неделя плана (N из 13) + фаза (development/peak/taper)
- Краткая выжимка плана на ближайшие 2 недели
- Последние **14 тренировок** в формате:
  ```
  2026-05-15 long 14.2km 1:18 avg-pace 5:30 avg-hr 145 max-hr 162 feel good
    laps: 5:35,5:30,5:28,5:25,5:22,5:30,5:35...
    hr-drift: +8% (пульс рос к концу — норма для long)
  ```
- Все забеги из `races.json`
- Существующие эвристики из `renderAdjust()` (как доп. сигнал для LLM)

Дрифт пульса вычисляем на бэке из `samples` или из `laps` (avg_hr по первой и последней половине).

### Б.3 Эндпоинты

- `POST /advise` — запросить новую рекомендацию (вызов LLM, запись в GCS, возврат)
- `GET /advise` — последняя рекомендация без вызова LLM

---

## Промпт системный

```
Ты опытный беговой тренер. Анализируешь данные тренировок бегуна,
готовящегося к полумарафону 09.08.2026 с целью 1:40 (темп 4:44/км).
Текущая неделя плана — N из 13. Дай рекомендации:
1. Общая оценка прогресса (1-2 предложения)
2. Корректировки на следующую неделю (конкретные изменения по дням)
3. Предупреждения (если есть риски)
Отвечай строго в JSON: {assessment, adjustments: [{day, change}], warnings: []}
```

Один и тот же промпт работает с любым из 3 провайдеров. Парсинг JSON-ответа также общий
(подстраховка: если LLM вернул текст вокруг JSON, ищем первый `{` и последний `}`).

## Структура `advice/v{N}/recommendation.json`

```json
{
  "version": N,
  "is_current": true,
  "created_at": "ISO-8601",
  "created_by": "aabramov77",
  "based_on_runs": [run IDs],
  "based_on_plan_version": M,
  "based_on_llm_config_version": K,
  "provider": "anthropic",
  "model": "claude-sonnet-4-5",
  "input_tokens": 0,
  "output_tokens": 0,
  "recommendation": {
    "assessment": "...",
    "adjustments": [{"day":"среда", "change":"..."}],
    "warnings": []
  }
}
```

Соответствует политике CLAUDE.md (append-only, versioned).

---

## Файлы для изменения

| Файл | Что меняется |
|---|---|
| `requirements.txt` | +`httpx`, +`fitparse` (anthropic SDK не нужен — все три провайдера через httpx) |
| `main.py` | +парсер FIT; +хелперы для деталей пробежки; +хелперы конфига LLM (`read_llm_config()`, `write_llm_config_version()`, `mask_key()`); +модуль `llm_clients` (3 функции `_call_anthropic/openai/deepseek` + диспетчер `call_llm()`); +эндпоинты `/runs/upload-fit`, `/runs/{id}/details`, `/config/llm` (GET/POST), `/config/llm/test` (POST), `/advise` (GET/POST); +`build_llm_context()` |
| `app.js` | +`uploadGarminFit()` (multipart); +`loadRunDetails(id)` для модалки; в `showRunDetail()` — рендер lap-деталей + график; +`requestLlmAdvice()`, `renderLlmAdvice()`, `loadLatestAdvice()`; +`openLlmSettings()`, `saveLlmConfig()`, `testLlmKey()` |
| `index.html` | +input FIT-файла рядом с CSV; +кнопка ИИ в `#tab-adjust`; +модалка/таб «⚙ Настройки» с формой провайдер/модель/ключ; cache-buster v9 |
| `style.css` | без изменений (используем существующие `.suggestion`, `.chart-wrap`, `.run-detail-overlay`) |

**Ручных шагов в GCP — НЕТ.** Ключ задаётся через UI приложения. Secret Manager не используется.

---

## Стоимость / Сравнение провайдеров (на ~5K input + 1K output / запрос)

| Провайдер / модель | $ за запрос (≈) | Качество тренерских советов | Скорость |
|---|---|---|---|
| Claude Sonnet 4.5 | $0.03 | ⭐⭐⭐⭐⭐ | 5-10 сек |
| Claude Haiku 4.5 | $0.003 | ⭐⭐⭐⭐ | 2-4 сек |
| GPT-4o | $0.03 | ⭐⭐⭐⭐⭐ | 5-10 сек |
| GPT-4o-mini | $0.002 | ⭐⭐⭐ | 2-4 сек |
| Deepseek-chat | $0.001 | ⭐⭐⭐⭐ (часто удивляет) | 3-6 сек |
| Deepseek-reasoner | $0.005 | ⭐⭐⭐⭐⭐ (chain-of-thought) | 10-20 сек |

Можно стартовать на Deepseek (дёшево + достойно) и при необходимости переключаться без redeploy.

Защита от спама: на фронте — кнопка disable на 60 секунд после клика. На бэке — необязательно (один аутентифицированный пользователь).

---

## Открытые вопросы (нужен ответ до реализации)

1. **UI для настроек:** отдельная модалка с шестерёнкой в header, или новый таб «⚙ Настройки» в навигации?

2. **Стартовый провайдер по умолчанию:** Deepseek (дёшево) или Claude Sonnet (качество)? Или вообще не задавать дефолт — пользователь сам выбирает при первом заходе.

3. **Контекст:** сколько последних пробежек слать LLM? Предлагаю **14** (2 недели) + последние 3 забега.

4. **Регенерация:** автоматически при новой пробежке или только по кнопке? Предлагаю **только по кнопке** (контроль стоимости).

5. **Применение рекомендаций к плану:** LLM возвращает текстовые советы (фаза 1), или должна
   выдавать готовые правки для автоматического `POST /plan` (фаза 2 — отдельный шаг)?

6. **FIT samples:** хранить ли поминутные сэмплы (HR, темп, высота)? +50-200KB на тренировку.
   Альтернатива — только laps (минимум).

7. **Существующие пробежки (без FIT):** оставить как есть (`details_available: false`) или
   разрешить апгрейд — отдельная кнопка «Дозагрузить FIT для этой пробежки»?

8. **График в детали тренировки:** показывать ли HR/pace по времени, если есть samples?

---

## Верификация

**Часть А (FIT):**
1. Загрузить FIT в форму «+ Пробежка» → форма заполняется + в GCS появляются `runs/{id}/v1/activity.fit` и `details.json`
2. В журнале кликнуть на пробежку → в модалке отображаются laps + (если есть) график пульса
3. CSV-импорт продолжает работать (регрессия)
4. Старые пробежки (без FIT) открываются как раньше

**Часть Б (LLM настройки + рекомендации):**
5. Открыть «⚙ Настройки» → форма пустая (никакой конфиг не задан)
6. Выбрать провайдера Deepseek, ввести ключ, выбрать модель `deepseek-chat`, нажать «Сохранить» → в GCS появляется `config/llm/v1/config.json`
7. Нажать «Проверить ключ» → запрос проходит, видим «✓ работает, latency 1.2с»
8. Открыть «Корректировка» → видны эвристики + кнопка ИИ
9. Нажать «🤖 Получить рекомендации ИИ» → loading → ответ от Deepseek
10. Переключить провайдера на Anthropic с новым ключом, сохранить → создаётся `config/llm/v2/config.json`, manifest обновляется
11. Снова запросить рекомендации → теперь идёт в Anthropic, в `advice/v{N+1}/recommendation.json` поле `provider="anthropic"`
12. Ввести заведомо невалидный ключ → «Проверить ключ» → понятная ошибка `401 Unauthorized from provider`
13. Удалить конфиг (или ввести пустой ключ) → POST /advise → 400 `LLM config not set`
