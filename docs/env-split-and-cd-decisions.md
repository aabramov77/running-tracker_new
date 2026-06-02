# Разделение dev/prod + CD — что сделали и почему

> Документ-ретроспектива. Дополняет `env-split-and-cd.md` (тот — пошаговый план реализации).
> Здесь описано: почему выбраны конкретные инструменты, что и зачем настроено в каждом,
> и какие неочевидные моменты всплыли при боевом запуске.

---

## Архитектура «до» и «после»

**Было:**
- Один Cloud Run сервис `runs-api`
- Один GCS bucket `running-tracker-aabramov77` со всеми данными
- Один URL фронта `aabramov77.github.io/running-tracker_new`
- Деплой бэка — руками через `gcloud run deploy` после каждого изменения `main.py`

**Стало:**

| | Dev | Prod |
|---|---|---|
| Фронт | `running-tracker-new-4ih.pages.dev` (Cloudflare Pages) | `aabramov77.github.io/...` (GitHub Pages) |
| Бэк | Cloud Run `runs-api-dev` | Cloud Run `runs-api` |
| Данные | GCS `running-tracker-aabramov77-dev` | GCS `running-tracker-aabramov77` |
| Триггер деплоя | push в ветку `Dev` | push в ветку `main` |
| Авторизация в GCP из CI | Workload Identity Federation | то же |

Один OAuth Client ID с двумя origins, один GitHub-репозиторий с двумя ветками.

---

## Выбранные инструменты — почему именно эти

### Cloudflare Pages для dev-фронта

**Альтернативы:** второй GitHub repo, Netlify, Vercel.

**Почему:** GitHub Pages умеет публиковать только одну ветку на репо — а нам нужно
два URL из одного `running-tracker_new`. Cloudflare Pages привязывается к репо
и автоматически собирает per-branch deployment: каждый push в `Dev` обновляет
dev-URL. Бесплатно, без сборки (статика), 5 минут на подключение.

### GitHub Pages для prod-фронта

**Почему оставили:** Уже работает, URL знакомый, менять без причины нет смысла.
GitHub Pages + Cloudflare Pages — стандартное сочетание для hobby/single-developer
проектов.

### Cloud Run + Cloud Functions (`gcloud run deploy --source=. --function=runs_api`)

**Альтернативы:** GKE, App Engine, чистый Cloud Functions Gen 1.

**Почему:** Уже использовался для prod, free tier с большим запасом
(2M вызовов/мес), serverless (платим только за реальные запросы), бесшовно работает
с `functions_framework`-кодом. Cloud Run 2nd gen фактически и есть Cloud Functions
под капотом.

### GitHub Actions для CD

**Альтернативы:** Cloud Build, GitLab CI, Jenkins.

**Почему:** Репозиторий уже на GitHub, для public-репо безлимит бесплатных
минут. Готовые official-actions от Google (`google-github-actions/auth`,
`setup-gcloud`, `deploy-cloudrun`) — без самописных скриптов.

### Workload Identity Federation (WIF) вместо service account JSON

**Альтернатива:** скачать JSON-ключ SA и положить в GitHub Secret.

**Почему:**
- JSON-ключи легко утекают (попадают в логи, в репо случайно, в скриншоты)
- Ключ нужно ротировать, иначе он живёт вечно
- WIF короткоживущий: GitHub Actions при каждом запуске генерирует OIDC-токен,
  GCP проверяет, что он подписан GitHub'ом И что repo матчит наш паттерн,
  и выдаёт временный access token на 1 час
- Нечего хранить и нечего украсть

### Два разных GCS bucket вместо одного с префиксами

**Альтернатива:** один bucket с `dev/` и `prod/` префиксами.

**Почему два:** Один баг в логике роутинга префиксов → перепутали окружения,
и тестовые данные пишутся в prod. С отдельными бакетами это физически
невозможно: код через env var знает имя своего бакета и не имеет доступа
к чужому. Дополнительная безопасность через IAM: runtime SA dev-сервиса
физически НЕ имеет права писать в prod-bucket.

---

## Что и зачем настроили в каждом инструменте

### GCS — два bucket-а

**`running-tracker-aabramov77-dev`** (новый)
- Создан с `--uniform-bucket-level-access` — упрощает IAM (нет per-object ACL)
- Регион `europe-west1` — совпадает с Cloud Run для минимума latency
- Скопирована копия prod-данных через `gsutil -m cp -r` — чтобы dev-стенд
  имел реалистичные данные с самого начала

**`running-tracker-aabramov77`** (prod) — не трогали.

### Cloud Run — два сервиса

**`runs-api-dev`** (новый)
- Деплой: `gcloud run deploy --source=. --function=runs_api`
- `--source=.` запускает Cloud Build, который собирает Docker image
  через Google Buildpacks (распознаёт Python + `functions-framework`)
- `--function=runs_api` указывает entry-point HTTP-функции — без этого
  buildpack не знает что вызывать
- `--allow-unauthenticated` — нужно, потому что фронт ходит из браузера
  БЕЗ GCP-токена (OAuth-проверка делается внутри функции, не на Cloud
  Run-уровне)
- Env var `BUCKET_NAME=running-tracker-aabramov77-dev` — передаётся через
  `--set-env-vars`, тот же `main.py` понимает в какой bucket писать

**`runs-api`** (prod) — деплоится тем же workflow при push в main, но с
`BUCKET_NAME=running-tracker-aabramov77`.

### Service Accounts — три штуки в работе

1. **`gh-deployer@<project>`** — кого GitHub Actions «олицетворяет» при деплое
   - 6 ролей: `run.admin` (деплой Cloud Run), `iam.serviceAccountUser`
     (действовать от имени runtime SA), `cloudbuild.builds.editor`
     (запуск Cloud Build), `artifactregistry.writer` (push образа),
     `storage.admin` (загрузка исходников в staging-bucket Cloud Build),
     `logging.logWriter` (запись build-логов)
   - Половину ролей выдали в плане сразу, остальные «добивали» по мере того,
     как Cloud Build об них спотыкался — нет одной официальной шпаргалки,
     какой именно набор нужен для `--source=.`

2. **`<project-number>-compute@developer.gserviceaccount.com`** — default
   compute SA, под которым РАБОТАЕТ deployed Cloud Run
   - Ему нужен `roles/storage.objectAdmin` на bucket-е, потому что в runtime
     `main.py` читает и пишет JSON-файлы в GCS
   - На prod-бакете эта привязка существовала исторически. На dev-бакете её
     сначала не было → save возвращал 500, а read молча отдавал пустой список
     (потому что `read_runs()` ловит «blob не существует» и возвращает `[]`)

3. **OAuth 2.0 Client ID** — для логина пользователя через Google
   - Один Client ID, в него добавили второй authorized origin (`pages.dev`)
   - Тот же CLIENT_ID в коде `main.py` и `index.html` — менять не пришлось

### Workload Identity Federation — узлы конфигурации

- **Workload Identity Pool** `github-pool` — контейнер для внешних
  identity-провайдеров
- **OIDC Provider** `github-provider` в этом пуле:
  - `issuer-uri = https://token.actions.githubusercontent.com` — GCP доверяет
    токенам этой стороны
  - `attribute-mapping` — переводит claims из GitHub-токена в GCP-атрибуты,
    ключевой: `attribute.repository = assertion.repository`
  - `attribute-condition = assertion.repository == "aabramov77/running-tracker_new"` —
    ОЧЕНЬ важно: без этой строки ЛЮБОЙ GitHub Actions из любого репо мира мог бы
    запросить access token нашего SA. С ней — только наш репо
- **Привязка SA → WIF**: `roles/iam.workloadIdentityUser` на `gh-deployer`
  для principal `principalSet://...attribute.repository/aabramov77/running-tracker_new` —
  теперь только этот репо может «олицетворять» SA

### GitHub Actions — workflow `deploy-cloudrun.yml`

**Триггеры:**
- `push: branches: [Dev, main]` — раздельно для двух окружений
- `paths: main.py | requirements.txt | .github/workflows/deploy-cloudrun.yml` —
  workflow НЕ запускается на правки фронта (это бы тратило минуты бесплатно,
  но всё равно лишнее)
- `workflow_dispatch` — ручной запуск из UI на случай если нужно передеплоить
  без изменений (например, чтобы подхватить обновлённый container base image)

**Шаги:**
1. `actions/checkout@v4` — забрать код
2. `Resolve target environment` — bash определяет: push в `main` → service=`runs-api`,
   bucket=prod; иначе → `runs-api-dev`, bucket=dev
3. `google-github-actions/auth@v2` — OIDC handshake с WIF, получение временного
   GCP-токена
4. `setup-gcloud@v2` — установить gcloud CLI на runner
5. `gcloud run deploy` — деплой выбранного сервиса с правильным `BUCKET_NAME`
   через env var
6. `Print service URL` — печатает URL результата в job summary, чтобы видеть
   его сразу

**Permissions:**
- `contents: read` — workflow читает код
- `id-token: write` — нужно для генерации OIDC-токена (без этой строки
  `auth@v2` не работает)

### GitHub Variables (не Secrets)

Четыре переменные на уровне Actions:
- `GCP_PROJECT_ID` — `project-c32efa72-003b-4498-a26`
- `GCP_PROJECT_NUMBER` — численный ID проекта
- `WIF_PROVIDER_PATH` — полный путь к provider'у вида
  `projects/<num>/locations/global/workloadIdentityPools/github-pool/providers/github-provider`
- `GCP_SA_EMAIL` — `gh-deployer@<project>.iam.gserviceaccount.com`

Это **Variables, а не Secrets**, потому что в них нет ничего секретного:
project ID и SA email не дают доступа сами по себе, реальная авторизация —
через WIF.

### Код

**`main.py`** — одна строка:
```python
BUCKET_NAME = os.environ.get("BUCKET_NAME", "running-tracker-aabramov77")
```
Дефолт — prod-bucket, чтобы локальный запуск без env var работал «как раньше».

**`app.js`** — выбор API URL по hostname:
```javascript
const IS_PROD = (window.location.hostname === 'aabramov77.github.io');
const API_URL = IS_PROD ? PROD_API_URL : DEV_API_URL;
```
Плюс маленький оранжевый бейдж **DEV** в углу страницы на не-prod хостах —
чтобы случайно не путать вкладки. Это psychological failsafe: технически dev
и prod уже изолированы, но человек смотрит на одинаковый UI и может думать,
что находится не там.

---

## Чему научились (что не было очевидно в плане)

1. **`gcloud run deploy --source=.` тащит за собой Cloud Build → Artifact
   Registry → Cloud Run.** Деплой-SA нужны права на ВСЕ три, не только
   Cloud Run.

2. **Сервис-аккаунт деплоя ≠ сервис-аккаунт runtime.** Дать `gh-deployer`
   доступ к bucket недостаточно — нужно ещё default compute SA, под
   которым реально работает контейнер.

3. **GCS read «молча» возвращает пустой список** для отсутствующих/недоступных
   blob-ов, потому что `read_runs()` ловит исключение. Save валится 500.
   Это маскирует проблему: журнал пустой, кнопка не работает, причина не
   очевидна без логов.

4. **WIF `attribute-condition` критичен для безопасности.** Без него любой
   GitHub-репозиторий мог бы получить токен нашего SA.

5. **Cloud Shell стартует в пустой `~`** — для деплоя через `--source=.`
   нужно сначала склонировать репо в Cloud Shell и `cd` в него, иначе
   gcloud собирает пустую папку (фейл: "no main.py").

---

## Связанные документы

- [`env-split-and-cd.md`](./env-split-and-cd.md) — пошаговая инструкция реализации
  (команды gcloud, конфиг workflow YAML, верификационный чек-лист)
- [`llm-plan-adjustment.md`](./llm-plan-adjustment.md) — LLM-рекомендации
  (как теперь два независимых конфига `config/llm/...` в двух бакетах)
- [`fit-upload-two-step.md`](./fit-upload-two-step.md) — двухшаговая FIT-загрузка
  (тестируется на dev до промоушена в prod)
