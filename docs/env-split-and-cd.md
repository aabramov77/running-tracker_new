# План: Разделение окружений (dev / prod) + CD

## Context

Сейчас приложение существует в одном экземпляре: один Cloud Run (`runs-api`),
один GCS bucket (`running-tracker-aabramov77`), один URL фронта (`aabramov77.github.io/running-tracker_new`).
Любое изменение в `Dev`-ветке после мержа в `main` сразу попадает в прод вместе с
реальными данными. Ручной деплой Cloud Run после каждого изменения `main.py`
утомителен и подвержен ошибкам (забыли передеплоить — увидели через неделю).

**Цели:**
1. Полная изоляция prod-данных от экспериментов
2. Автоматический деплой бэка по push (без ручных команд)
3. Возможность тестировать изменения на dev-стенде до промоушена в prod

**Подход:** Вариант 1 из предварительного обсуждения — два независимых набора
ресурсов (бакет + Cloud Run сервис), фронт через Cloudflare Pages для dev и
GitHub Pages для prod.

---

## Целевая архитектура

```
┌─────────────────────────────────────┐    ┌─────────────────────────────────────┐
│ DEV                                  │    │ PROD                                 │
│ ─────────────────────────────────── │    │ ─────────────────────────────────── │
│ Frontend:  Cloudflare Pages         │    │ Frontend:  GitHub Pages              │
│   running-tracker-dev.pages.dev      │    │   aabramov77.github.io/...          │
│                                      │    │                                      │
│ Backend:   Cloud Run runs-api-dev   │    │ Backend:   Cloud Run runs-api-prod  │
│   env: BUCKET_NAME=...-dev          │    │   env: BUCKET_NAME=...              │
│                                      │    │                                      │
│ Data:      gs://...-aabramov77-dev  │    │ Data:      gs://...-aabramov77       │
│                                      │    │                                      │
│ Branch:    Dev → auto-deploy         │    │ Branch:    main → auto-deploy        │
└─────────────────────────────────────┘    └─────────────────────────────────────┘
                  ▲                                          ▲
                  └────── один OAuth Client ID (с двумя origins) ──────┘
                  └────── один GitHub репозиторий (две ветки)   ──────┘
```

---

## Шаг 0. Подготовительные действия (вручную, до коммитов)

### 0.1. Создать dev GCS bucket
```bash
gcloud storage buckets create gs://running-tracker-aabramov77-dev \
  --location=europe-west1 \
  --uniform-bucket-level-access \
  --project=<твой-project-id>
```

### 0.2. Скопировать снапшот данных prod → dev (для реалистичного тестирования)
```bash
gsutil -m cp -r gs://running-tracker-aabramov77/* gs://running-tracker-aabramov77-dev/
```
Достаточно одного раза. Дальше окружения расходятся.

### 0.3. Создать dev Cloud Run сервис (первый ручной деплой)
Из того же `main.py`, но с env var `BUCKET_NAME` указывающей на dev-bucket:
```bash
gcloud run deploy runs-api-dev \
  --source=. \
  --region=europe-west1 \
  --allow-unauthenticated \
  --set-env-vars=BUCKET_NAME=running-tracker-aabramov77-dev \
  --project=<project-id>
```
Запишет URL вида `https://runs-api-dev-XXXXXX.europe-west1.run.app/`.

### 0.4. Переименовать существующий Cloud Run (опционально)
Сервис назван `runs-api`. Для симметрии можно оставить как есть или переименовать в `runs-api-prod`.
**Рекомендация:** оставить `runs-api` — переименование = смена URL → правки в коде + OAuth origins.

### 0.5. OAuth — добавить dev-origin
В Google Cloud Console → APIs & Services → Credentials → существующий OAuth 2.0 Client ID:
- Authorized JavaScript origins: добавить `https://running-tracker-dev.pages.dev` (или твой dev URL)
- Сохранить

### 0.6. Workload Identity Federation для GitHub Actions
Чтобы не хранить service account JSON в GitHub secrets, настроим WIF:
```bash
# Pool
gcloud iam workload-identity-pools create github-pool \
  --location=global --project=<project-id>

# Provider (привязан к GitHub)
gcloud iam workload-identity-pools providers create-oidc github-provider \
  --location=global --workload-identity-pool=github-pool \
  --issuer-uri="https://token.actions.githubusercontent.com" \
  --attribute-mapping="google.subject=assertion.sub,attribute.repository=assertion.repository" \
  --attribute-condition='assertion.repository == "aabramov77/running-tracker_new"'

# Service account для деплоя
gcloud iam service-accounts create gh-deployer --project=<project-id>

# Роли SA — полный набор для `gcloud run deploy --source=.`
# (триггерит Cloud Build → push в Artifact Registry → deploy Cloud Run)
for role in \
  roles/run.admin \
  roles/iam.serviceAccountUser \
  roles/cloudbuild.builds.editor \
  roles/artifactregistry.writer \
  roles/storage.admin \
  roles/logging.logWriter \
; do
  gcloud projects add-iam-policy-binding <project-id> \
    --member="serviceAccount:gh-deployer@<project-id>.iam.gserviceaccount.com" \
    --role="$role"
done

# Привязка WIF → SA
gcloud iam service-accounts add-iam-policy-binding \
  gh-deployer@<project-id>.iam.gserviceaccount.com \
  --role="roles/iam.workloadIdentityUser" \
  --member="principalSet://iam.googleapis.com/projects/<project-number>/locations/global/workloadIdentityPools/github-pool/attribute.repository/aabramov77/running-tracker_new"
```

В GitHub repo Settings → Secrets and variables → Actions → создать
переменные (**Variables**, не Secrets):
- `GCP_PROJECT_ID`
- `GCP_PROJECT_NUMBER`
- `WIF_PROVIDER_PATH` (вида `projects/<num>/locations/global/workloadIdentityPools/github-pool/providers/github-provider`)
- `GCP_SA_EMAIL` (`gh-deployer@<project-id>.iam.gserviceaccount.com`)

### 0.7. Cloudflare Pages для dev фронта
- Зарегистрироваться/залогиниться на dash.cloudflare.com (бесплатно)
- Pages → Connect to Git → выбрать репо `running-tracker_new`
- Production branch: `Dev`
- Build settings: **No build command** (статика), Output directory: `/` (корень)
- После первого деплоя получим URL `running-tracker-new-dev.pages.dev` (или похожий)

---

## Шаг 1. Изменения в коде

### 1.1. `main.py` — bucket из env var

Сейчас:
```python
BUCKET_NAME = "running-tracker-aabramov77"
```

Заменить на:
```python
import os
BUCKET_NAME = os.environ.get("BUCKET_NAME", "running-tracker-aabramov77")
```

Дефолт оставляем prod-бакет для локальной разработки и совместимости со старым деплоем.

### 1.2. `app.js` — выбор API URL по hostname

Сейчас:
```javascript
const API_URL = 'https://runs-api-463368957110.europe-west1.run.app/';
```

Заменить на:
```javascript
const PROD_API_URL = 'https://runs-api-463368957110.europe-west1.run.app/';
const DEV_API_URL  = 'https://runs-api-dev-XXXXXX.europe-west1.run.app/';  // подставить реальный URL после шага 0.3

const API_URL = (window.location.hostname === 'aabramov77.github.io')
  ? PROD_API_URL
  : DEV_API_URL;
```

Cache-buster `app.js?v=13` в `index.html`.

### 1.3. (Опционально) Визуальный индикатор окружения

Чтобы случайно не путать вкладки prod/dev — добавить в header маленький бейдж:
```javascript
// В initApp() или сразу после загрузки
if (API_URL === DEV_API_URL) {
  const badge = document.createElement('span');
  badge.textContent = 'DEV';
  badge.style.cssText = 'position:fixed;top:8px;left:8px;background:var(--c-warn);color:white;padding:2px 8px;border-radius:4px;font-size:11px;z-index:300';
  document.body.appendChild(badge);
}
```

---

## Шаг 2. GitHub Actions workflows

Создать `.github/workflows/deploy-cloudrun.yml`:

```yaml
name: Deploy Cloud Run

on:
  push:
    branches: [Dev, main]
    paths:
      - 'main.py'
      - 'requirements.txt'
      - '.github/workflows/deploy-cloudrun.yml'

permissions:
  contents: read
  id-token: write   # для Workload Identity Federation

jobs:
  deploy:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4

      - id: env
        run: |
          if [ "${{ github.ref_name }}" = "main" ]; then
            echo "service=runs-api"             >> $GITHUB_OUTPUT
            echo "bucket=running-tracker-aabramov77" >> $GITHUB_OUTPUT
          else
            echo "service=runs-api-dev"         >> $GITHUB_OUTPUT
            echo "bucket=running-tracker-aabramov77-dev" >> $GITHUB_OUTPUT
          fi

      - id: auth
        uses: google-github-actions/auth@v2
        with:
          workload_identity_provider: ${{ vars.WIF_PROVIDER_PATH }}
          service_account: ${{ vars.GCP_SA_EMAIL }}

      - uses: google-github-actions/deploy-cloudrun@v2
        with:
          service: ${{ steps.env.outputs.service }}
          source: .
          region: europe-west1
          env_vars: |
            BUCKET_NAME=${{ steps.env.outputs.bucket }}
          flags: --allow-unauthenticated
```

**Что происходит:**
- Push в `Dev` → деплой `runs-api-dev` с dev-bucket
- Push в `main` → деплой `runs-api-prod` с prod-bucket
- Триггер только при изменениях, которые реально влияют на Cloud Run

**Фронт:** Cloudflare Pages подхватывает push в `Dev` автоматически. GitHub Pages —
push в `main` (уже работает). Никакого workflow для фронта не нужно.

---

## Файлы для изменения

| Файл | Изменения |
|---|---|
| `main.py` | `BUCKET_NAME = os.environ.get(...)` (одна строка + импорт `os`) |
| `app.js` | Per-hostname выбор `API_URL` + опциональный DEV-бейдж |
| `index.html` | Cache-buster `app.js?v=13` |
| `.github/workflows/deploy-cloudrun.yml` | **Новый** — CD pipeline |

---

## Порядок раскатки (минимизируем риски)

1. **Шаг 0** — вся ручная подготовка GCP/Cloudflare/GitHub Variables. Здесь
   ничего не ломается, потому что код пока работает по-старому.
2. **PR в Dev** с правками `main.py`/`app.js`/`index.html` + workflow. На dev-стенде
   протестировать через `running-tracker-new-dev.pages.dev` → `runs-api-dev`.
3. После проверки — PR Dev → main. CI задеплоит prod-сервис с новой переменной
   `BUCKET_NAME`, GitHub Pages подхватит обновлённый фронт.
4. Финальная проверка prod через `aabramov77.github.io/running-tracker_new`.

---

## Верификация (по итогам всех шагов)

### Изоляция данных
- [ ] Загрузить тестовую пробежку на `dev.pages.dev` → она появляется в dev-bucket,
  в prod-bucket объект НЕ создаётся
- [ ] Загрузить пробежку на `aabramov77.github.io` → она в prod-bucket
- [ ] В журнале на dev и prod показываются разные списки

### CD pipeline
- [ ] Push в `Dev` с правкой `main.py` (например, добавить логирование) →
  GitHub Actions запускается → Cloud Run `runs-api-dev` обновляется без
  ручного вмешательства
- [ ] То же для `main` → деплой `runs-api` (prod)
- [ ] Push с правкой только `app.js` (не трогая `main.py`) НЕ запускает
  Cloud Run workflow (триггер по paths)

### OAuth
- [ ] Логин Google работает на обоих URL
- [ ] Токен с одного домена не используется на другом (это уже security-механика
  Google, но проверить)

### LLM (Phase 1)
- [ ] Настройки LLM на dev → ключ хранится в dev-bucket, не виден на prod
- [ ] Можно держать разные провайдеры/модели в окружениях

### Failsafe
- [ ] DEV-бейдж в углу страницы предотвращает путаницу — пользователь
  визуально понимает, что он не на prod

---

## Эстимат стоимости

| Сервис | Free tier | Реальное использование |
|---|---|---|
| Cloud Run | 2M invocations/мес | Десятки запросов/день |
| GCS (storage + ops) | 5 GB + классы операций | Менее 100 MB, несколько тысяч ops |
| GitHub Actions | 2000 min/мес для private, безлимит для public | 2 деплоя × 2 мин ≈ единицы минут/день |
| Cloudflare Pages | 500 builds/мес, безлимит трафика | Push'ы в Dev — десятки/мес |
| Workload Identity | бесплатно | — |

**Итого:** $0/мес при текущих объёмах. Дублирование Cloud Run и bucket
бесплатно благодаря free tier.

---

## Что НЕ делаем в этом плане (можем потом)

- **Канареечный деплой / постепенный rollout** — overkill для single-user app
- **Отдельный staging** между dev и prod — два окружения достаточно
- **Pre-deploy тесты в CI** — пока тесты только локальные/ручные
- **Cron-cleanup для tmp/** на бэке — лениво чистим в `parse-fit`, хватает
- **Метрики/алерты** — Cloud Run UI показывает базовое
