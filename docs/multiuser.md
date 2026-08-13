# План: Многопользовательский режим

## Context

Сейчас приложение жёстко однопользовательское: `ALLOWED_EMAIL = "aabramov77@gmail.com"`
в `main.py` пропускает только один Google-аккаунт, а все данные лежат в GCS по
**глобальным** путям (`runs.json`, `races.json`, `plan/*`, `advice/*`, `runs/{id}/*`).

Цель — пустить других пользователей с полной изоляцией данных. Решения (согласованы):
- **Доступ:** новый пользователь логинится через Google → попадает в статус `pending` →
  админ (Alexander) одобряет/отклоняет. Только `approved` видят приложение.
- **LLM:** один общий ключ приложения (управляет админ через Настройки). Все
  одобренные пользователи пользуются им для своих рекомендаций. Конфиг LLM
  остаётся **глобальным** (не per-user), но запись — только админ.
- **Изоляция:** каждый пользователь видит только свои тренировки/забеги/план/советы.
  Никакого шеринга, тренеров, рейтингов.

**Идентификатор пользователя:** Google-claim `sub` (стабильный неизменный ID
аккаунта). Email/имя храним в профиле для отображения, но namespace путей — по `sub`.

---

## Архитектура данных (после)

Все per-user данные переезжают под префикс `users/{sub}/`:

| Сейчас (глобально) | Станет (per-user) |
|---|---|
| `runs.json` | `users/{sub}/runs.json` |
| `races.json` | `users/{sub}/races.json` |
| `plan/manifest.json` + `plan/v{N}/plan.json` | `users/{sub}/plan/manifest.json` + `users/{sub}/plan/v{N}/plan.json` |
| `advice/manifest.json` + `advice/v{N}/recommendation.json` | `users/{sub}/advice/...` |
| `runs/{id}/v1/...` + `runs/{id}/manifest.json` | `users/{sub}/runs/{id}/...` |
| `tmp/{token}/...` | `tmp/{sub}/{token}/...` |

**План тренировок — у каждого пользователя свой и при первом входе ПУСТОЙ.**
Путь `users/{sub}/plan/...`.
- **Убираем авто-сид `INITIAL_PLAN`.** `GET /plan` для пользователя без манифеста
  возвращает `[]` (пустой план), а не копию шаблона. Новый пользователь строит план
  с нуля через **конструктор** (новый UI — см. Шаг 2.6).
- `INITIAL_PLAN` остаётся в `main.py` только как исторический источник данных
  Alexander'а (его существующий план мигрируется как есть, см. Шаг 3). Для новых
  пользователей он не используется → фактически становится мёртвым кодом, оставляем.
- Каждый редактирует свою копию независимо — правки одного не влияют на других.

**Остаются глобальными** (управляет админ):
- `config/llm/manifest.json` + `config/llm/v{N}/config.json` — общий ключ LLM

**Новое — реестр пользователей:**
- `users/registry.json` — мутабельный список пользователей для быстрого чтения
  (статус — это lifecycle-метаданные, изменять разрешено правилом CLAUDE.md)
- `users/events/{ts}-{sub}-{event}.json` — append-only аудит-лог переходов
  (register / approve / reject) для полной истории изменений

Запись в `users/registry.json` структуры:
```json
{
  "users": {
    "<sub>": {
      "sub": "...", "email": "...", "name": "...",
      "status": "pending|approved|rejected",
      "role": "admin|user",
      "created_at": "ISO", "updated_at": "ISO",
      "approved_by": "<admin-sub>|null"
    }
  }
}
```

---

## Шаг 1. `main.py` — реестр пользователей и авторизация

### 1.1. Константы
```python
ADMIN_EMAILS = {"aabramov77@gmail.com"}   # кто получает role=admin при первом логине
USERS_REGISTRY = "users/registry.json"
```
Удалить `ALLOWED_EMAIL`. `OBJECT_NAME`/`RACES_OBJECT`/`PLAN_MANIFEST`/`ADVICE_MANIFEST`
больше не глобальные константы путей — пути строятся из `sub` (см. 1.4).
`LLM_CONFIG_MANIFEST` остаётся глобальным.

### 1.2. `verify_token()` — только проверка подписи
Убрать проверку `ALLOWED_EMAIL`. Возвращать полный `info` (содержит `sub`, `email`,
`name`) при валидной подписи и совпадении `aud == CLIENT_ID`. Иначе `None`.

### 1.3. Реестр + резолвинг пользователя
Новые хелперы:
```python
def read_registry(bucket) -> dict          # {} если нет файла
def write_registry(bucket, registry)       # перезапись (статус = lifecycle)
def append_user_event(bucket, sub, event, actor)   # append-only аудит
def resolve_user(bucket, token_info):
    """Находит/создаёт пользователя. Возвращает запись из реестра.
       Новый sub → создаём status=pending (или approved+admin если email в ADMIN_EMAILS),
       пишем аудит-событие register."""
```
Первый логин админа (`email in ADMIN_EMAILS`) сразу `status=approved, role=admin`.

**Митигация п.1+9 — in-memory кэш реестра (TTL).** Чтобы не читать
`users/registry.json` из GCS на каждый запрос:
```python
_registry_cache = {"data": None, "ts": 0}
REGISTRY_TTL_SEC = 30
def read_registry(bucket):
    now = time.time()
    if _registry_cache["data"] is not None and now - _registry_cache["ts"] < REGISTRY_TTL_SEC:
        return _registry_cache["data"]
    data = _load_registry_from_gcs(bucket)   # фактический GET
    _registry_cache.update(data=data, ts=now)
    return data
def write_registry(bucket, registry):
    _save_registry_to_gcs(bucket, registry)
    _registry_cache.update(data=registry, ts=time.time())   # инвалидация
```
Cloud Run держит инстанс тёплым между запросами, поэтому кэш реально живёт.
TTL 30с — баланс между свежестью (одобрение применится почти сразу) и нагрузкой.
**Спам-защита (п.9):** в `resolve_user` для нового `sub` ограничить рост реестра —
если число `pending` ≥ лимита (например 50), новые pending не создаём, отдаём
`403 registration_closed`. Простая защита от наполнения файла.

### 1.4. Per-user пути
**Митигация п.2+3 — единая точка построения путей.** Все per-user пути строятся
ТОЛЬКО через хелперы, никаких inline-литералов `f"users/{sub}/..."` в хендлерах:
```python
def upfx(sub) -> str: return f"users/{sub}/"
def p_runs(sub):   return f"{upfx(sub)}runs.json"
def p_races(sub):  return f"{upfx(sub)}races.json"
def p_plan_manifest(sub): return f"{upfx(sub)}plan/manifest.json"
def p_plan_ver(sub, v):   return f"{upfx(sub)}plan/v{v}/plan.json"
def p_advice_manifest(sub): return f"{upfx(sub)}advice/manifest.json"
def p_advice_ver(sub, v):   return f"{upfx(sub)}advice/v{v}/recommendation.json"
def p_run_dir(sub, rid):    return f"{upfx(sub)}runs/{rid}/"
def p_tmp(sub, token):      return f"tmp/{sub}/{token}/"
```
Правило: **ни одна data-функция не обращается к bucket без `sub`-параметра.**
Code-review гейт: grep по `bucket.blob(` — каждый вызов должен идти через `p_*`
хелпер или принимать построенный путь. Это снижает риск забыть namespace.

Переписать существующие хелперы, добавив `sub`:
- `read_runs(bucket, sub)` / `write_runs(bucket, sub, runs)` → `users/{sub}/runs.json`
- `read_races(bucket, sub)` / `write_races(bucket, sub, races)` → `users/{sub}/races.json`
- план: `read_plan_manifest`, `write_plan_version`, путь версии → под `users/{sub}/plan/`.
  **В `GET /plan` убрать авто-сид `INITIAL_PLAN`** — при отсутствии манифеста
  возвращать `[]` (пустой план для нового пользователя)
- advice: `read_advice_manifest`, `read_latest_advice`, `write_advice_version` → под `users/{sub}/advice/`
- fit/details: `write_run_with_fit`, `read_run_details`, `attach_fit_details_to_run`,
  `write_parsed_fit_to_tmp`, `cleanup_old_tmp` → под `users/{sub}/runs/...` и `tmp/{sub}/...`
- `build_llm_context(bucket, sub)` → читает per-user runs/races/plan

LLM-конфиг хелперы (`read_llm_manifest`, `read_llm_config_full`,
`write_llm_config_version`) — **без изменений** (глобальный путь).

### 1.5. Гейтинг в `runs_api()`
После `verify_token`:
```python
token_info = verify_token(request)
if not token_info:
    return 401
user = resolve_user(bucket, token_info)    # создаёт pending при первом визите
sub = user["sub"]

# /me доступен всегда (любой валидный токен) — чтобы фронт показал статус
if path == "/me":
    return {status, role, email, name}

# Не одобрен → 403 для всех остальных эндпоинтов
if user["status"] != "approved":
    return 403 {"error": "pending_approval" | "rejected", "status": ...}

# admin-эндпоинты
if path.startswith("/admin/"):
    if user["role"] != "admin": return 403
    ... handle ...
```
Все существующие хендлеры получают `sub` и передают его в хелперы.

### 1.6. Новые эндпоинты
| Метод | Путь | Доступ | Назначение |
|---|---|---|---|
| GET | `/me` | любой валидный токен | `{status, role, email, name}` — фронт решает что показать |
| GET | `/admin/users` | admin | список всех пользователей из реестра |
| POST | `/admin/users/approve` | admin | body `{sub}` → status=approved + аудит |
| POST | `/admin/users/reject` | admin | body `{sub}` → status=rejected + аудит |
| POST | `/admin/migrate-legacy` | admin | копирует глобальные данные в namespace админа (см. Шаг 3 — пер-объектная идемпотентность, FIT лениво) |

### 1.7. LLM-конфиг — только админ
В обработчике `POST /config/llm` и `POST /config/llm/test` добавить проверку
`user["role"] == "admin"` → иначе 403. `GET /config/llm` тоже admin (там маска ключа).
`/advise` остаётся доступен всем approved — использует глобальный конфиг + данные
вызвавшего пользователя.

### 1.8. Митигация п.5 — дневной лимит на `/advise` (общий ключ = твои расходы)
Поскольку ключ общий и платит админ, ограничиваем число LLM-вызовов на
пользователя в сутки. Счётчик в per-user файле `users/{sub}/advice/usage.json`:
```json
{ "date": "2026-05-24", "count": 3 }
```
В `POST /advise` перед вызовом LLM:
```python
usage = read_advice_usage(bucket, sub)   # сброс если date != today
DAILY_ADVISE_LIMIT = 10                   # настраиваемо
if usage["count"] >= DAILY_ADVISE_LIMIT:
    return 429 {"error": "daily_limit_reached", "limit": DAILY_ADVISE_LIMIT}
# ... вызов LLM ...
increment_advice_usage(bucket, sub)       # count+1, date=today
```
Админу можно дать больший/безлимитный порог (`role == "admin"`). Лимит — мягкая
защита от перерасхода, не строгая бизнес-логика. GET `/advise` (чтение последней
рекомендации) не считается — лимитируем только POST (реальный вызов LLM).

---

## Шаг 2. `app.js` / `index.html` — статусы, админка, namespace кэша

### 2.1. Поток после логина
В `handleCredentialResponse` / автологине: вместо прямого `initApp()` сначала
`GET /me`:
- `approved` → `initApp()` как сейчас
- `pending` → показать экран «Заявка на рассмотрении» (новый оверлей)
- `rejected` → показать экран «Доступ отклонён»
- сетевой сбой/401 → существующий `handleAuthError()`

### 2.2. Namespace localStorage по sub
Сейчас ключи глобальные: `running_tracker_runs`, `..._races`, `..._plan`.
На общем браузере два аккаунта затрут данные друг друга. Решение: добавлять
суффикс `_{sub}` ко всем кэш-ключам. `g_id_token` оставить как есть (один
активный токен). При логине нового sub старый кэш просто не читается.

### 2.3. Admin UI
Если `/me` вернул `role=admin` — показать вкладку «Пользователи» (или секцию в
Настройках): список из `GET /admin/users` со статусами и кнопками
«Одобрить»/«Отклонить» → `POST /admin/users/{approve,reject}`.

### 2.4. LLM-настройки только админу
Секцию «Настройки LLM» (провайдер/модель/ключ) показывать только при
`role=admin`. Обычным пользователям — короткая заметка «ИИ настроен
администратором», кнопка «Запросить рекомендации» работает как раньше.

### 2.5. Экраны статуса
Новые оверлеи (по образцу `#login-screen`): `#pending-screen`, `#rejected-screen`
с кнопкой «Выйти». Cache-buster `app.js?v=15`, `style.css?v=15` если правим css.

### 2.6. Конструктор плана (новый UI)
Сейчас `renderPlan()` + режим редактирования умеют менять только 5 day-ячеек
(Вс/Пн/Ср/Пт/Сб) у существующих строк. Для пустого плана нового пользователя
нужен полноценный конструктор. Расширяем существующий режим редактирования
(`togglePlanEdit`/`collectPlanEdits`/`savePlanEdits`):

- **Пустое состояние:** если `PLAN` пуст — вкладка показывает заглушку
  «План пуст» + кнопку «Создать план» (входит в режим редактирования с 0 строк).
- **Добавление недели:** кнопка «+ Неделя» добавляет строку. Номер недели `w`
  проставляется автоматически (длина массива + 1).
- **Редактируемые поля в строке** (в режиме edit): `start`, `end` (даты —
  текстовые поля как в данных, формат `дд.мм`), `accent` (текст), `type`
  (select: dev/peak/taper/load/race — управляет цветом бейджа), и 5 day-ячеек
  (как сейчас).
- **Удаление недели:** кнопка «✕» в строке убирает её из массива (с
  перенумерацией `w`). Поскольку план версионируется при сохранении — это не
  физическое удаление истории, а новая версия.
- **Сохранение:** `collectPlanEdits()` собирает весь массив недель (включая новые
  поля) → `savePlanEdits()` шлёт `POST /plan` → бэк пишет новую версию. Бэк
  уже принимает произвольный `weeks` — серверных изменений для конструктора нет.
- `renderPlan()` расширяется: `dayCell()` дополняется ячейками дат/акцента/типа
  в режиме edit; обычный режим рендерится как сейчас.

Объём: преимущественно фронт (`app.js` + немного разметки/стилей). Бэкенд
`/plan` POST не меняется.

---

## Шаг 3. Миграция данных Alexander (одноразово)

Существующие прод-данные лежат по глобальным путям. После деплоя:
1. Залогиниться админом → автоматически создаётся `approved+admin` запись, узнаём `sub`.
2. Вызвать `POST /admin/migrate-legacy` (кнопка в admin UI или curl).
3. Проверить, что журнал/план/советы на месте под админ-аккаунтом.
4. Глобальные объекты **не удаляем** (CLAUDE.md — no physical delete); они просто
   перестают читаться.

**Митигация п.6 — пер-объектная идемпотентность + ленивый FIT.** Миграция не
полагается на единый sentinel «есть runs.json». Логика:
- Лёгкие объекты копируются по отдельности, каждый со своей проверкой
  «existsв namespace? → skip, иначе copy»: `runs.json`, `races.json`,
  `plan/manifest.json` + все `plan/v*/plan.json`, `advice/manifest.json` +
  `advice/v*/recommendation.json`. Падение на одном не блокирует остальные при
  повторном вызове (каждый объект проверяется независимо).
- **FIT-объекты `runs/{id}/v1/*` НЕ копируем в этом запросе** (их может быть
  много × сотни KB → риск таймаута HTTP). Вместо этого: `runs.json` уже содержит
  `details_available`, а при первом `GET /runs/{id}/details` если в per-user
  namespace деталей нет — лениво скопировать из глобального `runs/{id}/*`
  (fallback-чтение глобального пути + копирование в namespace). Так тяжёлые
  объекты переезжают по мере обращения, без таймаута.
- Эндпоинт возвращает отчёт `{copied: [...], skipped: [...], errors: [...]}` —
  видно что реально перенеслось. Повторный вызов безопасен (idempotent per-object).

> Тестировать всю миграцию сначала на **dev-стенде** (у нас уже есть dev/prod split):
> dev-bucket содержит копию прод-данных, можно безопасно прогнать.

---

## Файлы для изменения

| Файл | Изменения |
|---|---|
| `main.py` | Реестр (`read/write_registry`, `append_user_event`, `resolve_user`); `verify_token` без ALLOWED_EMAIL; per-user пути во всех data-хелперах (+`sub` параметр); гейт статуса/роли в `runs_api`; эндпоинты `/me`, `/admin/users`, `/admin/users/approve|reject`, `/admin/migrate-legacy`; admin-гейт на `/config/llm*` |
| `app.js` | `GET /me` после логина; экраны pending/rejected; namespace кэша по sub; admin-вкладка «Пользователи»; LLM-настройки только админу; **конструктор плана** (пустое состояние, +неделя, удаление, редактирование дат/акцента/типа) |
| `index.html` | Оверлеи `#pending-screen`/`#rejected-screen`; вкладка/секция «Пользователи»; кнопки конструктора плана; cache-buster v15 |
| `style.css` | Стили экранов статуса, admin-списка, конструктора плана; cache-buster v15 |
| `docs/multiuser.md` | Новый — этот план + схема реестра + процедура миграции |

`requirements.txt` — без изменений. Cloud Run — **redeploy** после правок `main.py`
(CD сделает автоматически на push в ветку).

---

## CLAUDE.md-compliance

- **No physical delete:** отклонение пользователя = смена `status` (lifecycle-метаданные,
  разрешено). Глобальные legacy-объекты не удаляются. Per-user runs/races —
  существующий soft-delete.
- **History:** аудит-лог `users/events/*` фиксирует кто/когда/что менял в реестре.
  План/советы/конфиг — уже версионируются.
- **Statuses как lifecycle:** `status`, `updated_at`, `approved_by` — допустимые
  изменяемые поля.

---

## Безопасность

- Авторизация серверная: каждый запрос резолвит `sub` из подписанного Google-токена,
  пути строятся из этого `sub` — пользователь физически не может прочитать чужой
  namespace (нет параметра, которым он задаёт чужой путь).
- Admin-роль проверяется по `role` из реестра, который проставляется только по
  `ADMIN_EMAILS` при первом логине. Нельзя самоповыситься через API.
- `/me` — единственный эндпоинт, доступный не-approved, и он отдаёт только данные
  самого вызывающего.
- Общий LLM-ключ виден (в маске) и редактируется только админом.

---

## Верификация (на dev-стенде, потом prod)

1. **Админ-логин:** залогиниться `aabramov77@gmail.com` → `/me` отдаёт
   `status=approved, role=admin`. Приложение открывается.
2. **Миграция:** `POST /admin/migrate-legacy` → журнал/план/советы появляются под
   админом. Повторный вызов — без дублей (идемпотентность).
3. **Новый пользователь:** залогиниться ВТОРЫМ Google-аккаунтом (на dev) →
   видит экран «Заявка на рассмотрении». `/me` = `pending`. Любой data-эндпоинт = 403.
4. **Одобрение:** под админом в «Пользователи» нажать «Одобрить» второго →
   у него после релогина/обновления открывается профиль с **пустым планом**
   («План пуст» + «Создать план») и пустыми журналом/забегами.
4a. **Конструктор:** второй создаёт план — «+ Неделя», заполняет даты/акцент/тип/дни,
   сохраняет → `users/{sub2}/plan/v1`. Перезагрузка → план на месте.
5. **Изоляция:** второй пользователь добавляет пробежку → она в `users/{sub2}/runs.json`,
   в namespace админа НЕ появляется, и наоборот.
5a. **План независим:** второй редактирует свой план → правки в
   `users/{sub2}/plan/v{N}`, у админа план (мигрированный 13-недельный) не меняется.
6. **LLM:** второй (не админ) не видит секцию ввода ключа, но «Запросить
   рекомендации» работает (общий ключ). Админ видит и может менять ключ.
6a. **Дневной лимит:** превысить `DAILY_ADVISE_LIMIT` вызовов `/advise` за день →
   `429 daily_limit_reached`. На следующий день счётчик сброшен.
7. **Отклонение:** отклонить тестового пользователя → у него экран «Доступ отклонён»,
   data-эндпоинты 403.
8. **Кэш-изоляция:** в одном браузере выйти из одного аккаунта, войти другим →
   данные не перемешиваются (namespace localStorage по sub).
9. **Grep-аудит путей:** ни один `bucket.blob(` в per-user коде не использует
   inline-литерал — только `p_*` хелперы (защита от забытого namespace).

---

## Фазирование (предложение)

- **Фаза 1 (backend):** реестр, авторизация, per-user пути, пустой план без
  авто-сида, `/me`, admin-эндпоинты, миграция. Деплой на dev, проверка через
  curl + два аккаунта.
- **Фаза 2 (frontend — доступ):** экраны pending/rejected, admin-вкладка
  «Пользователи», namespace кэша по sub, LLM-настройки только админу.
- **Фаза 3 (frontend — конструктор плана):** пустое состояние, +неделя, удаление,
  редактирование дат/акцента/типа. Можно отдельным PR после того как доступ и
  изоляция заработают.
- Все фазы тестируются на dev до промоушена в prod. Backend-first упрощает отладку.

---

## Не делаем сейчас (осознанный долг)

- **Гонка записи в `registry.json`** (п.4): два одновременных события могут
  затереть друг друга (lost update). Для приложения такого масштаба маловероятно;
  при росте — перейти на оптимистичную блокировку по generation/etag.
- **Валидация дат в конструкторе плана** (п.7): даты `дд.мм` — свободный текст без
  проверки. Кривой ввод может сбить определение текущей недели. Принять пока.
- **Полная очистка localStorage при смене аккаунта** (п.8): namespace по sub
  изолирует чтение, но старые ключи остаются в хранилище. Не критично.
- **Передача/смена админа без редеплоя** (п.10): `ADMIN_EMAILS` хардкод.
- Оптимистичные блокировки на запись per-user данных (у каждого свой файл —
  кросс-юзерных гонок нет; внутри одного юзера правки редки)
- Тренерские связи / шеринг / рейтинги (изоляция — окончательное решение)
- Удаление/экспорт аккаунта (GDPR-flow) — отдельная задача при необходимости
